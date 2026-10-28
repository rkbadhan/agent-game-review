"""Store path safety: no run_id can write or read outside the store (F1)."""

import json
import os

import pytest

from agr.store import _TAIL_SCAN_CHUNK, InvalidRunId, Store, validate_run_id

_ESCAPE_ATTEMPTS = [
    "/etc/passwd",
    "../../../../etc/passwd",
    "..",
    ".",
    "foo/../../bar",
    "foo/..",
    "../foo",
    "~/evil",
    "C:\\Windows\\System32",
    "C:/Windows/System32",
    "\\\\server\\share\\file",
    "namespace/./sneaky",
    "trailing/slash/",
    "//double/leading/slash",
    "null\x00byte",
    "",
    None,
    123,
]


@pytest.mark.parametrize("bad_run_id", _ESCAPE_ATTEMPTS)
def test_validate_run_id_rejects_escapes(bad_run_id):
    with pytest.raises(InvalidRunId):
        validate_run_id(bad_run_id)


_LEGITIMATE_IDS = [
    "r1",
    "chess_best_move__seed42",
    "build_task__ignored_failure",
    "harbor__terminal-bench/crack-7z-hash__0b9b1946-1866-4e40-ac98-b0510a611a78",
    "harbor__terminal-bench/nginx-request-logging__09d92d58-8cf2-4000-839b-8aa66579c051",
]


@pytest.mark.parametrize("run_id", _LEGITIMATE_IDS)
def test_validate_run_id_preserves_legitimate_ids(run_id):
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize("bad_run_id", _ESCAPE_ATTEMPTS)
def test_store_methods_reject_escaping_run_ids_before_touching_disk(tmp_path, bad_run_id):
    if not isinstance(bad_run_id, str):
        pytest.skip("non-string ids are covered by validate_run_id directly")
    store_root = tmp_path / "store"
    store = Store(str(store_root))

    for op in (
        lambda: store.write_source(bad_run_id, "capture_x", {"a": 1}),
        lambda: store.write_derived(bad_run_id, "capture_x", "foo.json", {}),
        lambda: store.register_capture(bad_run_id, "capture_x", "sha256:x", "v1", "complete"),
        lambda: store.read_index(bad_run_id),
        lambda: store.latest_capture_id(bad_run_id),
    ):
        with pytest.raises(InvalidRunId):
            op()

    # Nothing was ever written under the store root, and nothing appeared
    # anywhere outside it either.
    assert not any(store_root.rglob("*"))
    assert not (tmp_path.parent / "etc").exists()


# --- capture identity and atomic writes (review 2026-10-02 #4) -----------------


def test_register_capture_refuses_to_supersede_itself(tmp_path):
    store = Store(str(tmp_path / "store"))
    store.register_capture("r1", "capture_x", "sha256:aa", "adapter-v1", "complete")

    with pytest.raises(ValueError, match="already registered"):
        store.register_capture("r1", "capture_x", "sha256:aa", "adapter-v2", "complete")

    assert [e["capture_id"] for e in store.read_index("r1")] == ["capture_x"]


def test_capture_id_depends_on_source_hash_and_adapter_version():
    from agr.store import capture_id_for

    assert capture_id_for("sha256:aa", "v1") == capture_id_for("sha256:aa", "v1")
    assert capture_id_for("sha256:aa", "v1") != capture_id_for("sha256:aa", "v2")
    assert capture_id_for("sha256:aa", "v1") != capture_id_for("sha256:bb", "v1")


def _crash_before_replace():
    """Patch os.replace so a write dies after its temp file is written."""
    import os

    def boom(src, dst):
        raise OSError("simulated crash before replace")

    mp = pytest.MonkeyPatch()
    mp.setattr(os, "replace", boom)
    return mp


def test_write_source_crash_leaves_no_truncated_source_and_can_be_retried(tmp_path):
    import os
    from agr.store import canonical_bytes

    store = Store(str(tmp_path / "store"))
    doc = {"atif_version": "1.7", "steps": [{"step_id": "s1"}]}
    cap_dir = os.path.join(store.root, "runs", "r1", "captures", "capture_x")

    mp = _crash_before_replace()
    try:
        with pytest.raises(OSError):
            store.write_source("r1", "capture_x", doc)
    finally:
        mp.undo()
    assert os.listdir(cap_dir) == []  # no source.json, no stray temp file

    store.write_source("r1", "capture_x", doc)  # a retry is not blocked by the failed attempt
    assert canonical_bytes(store.read_source("r1", "capture_x")) == canonical_bytes(doc)


@pytest.mark.parametrize("writer, reader", [
    (lambda s, payload: s.write_derived("r1", "capture_x", "thing.json", payload),
     lambda s: s.read_derived("r1", "capture_x", "thing.json")),
    (lambda s, payload: s.write_review("r1", "capture_x", "model:scripted", payload),
     lambda s: s.read_review_slot("r1", "capture_x", "model:scripted")),
])
def test_derived_and_review_writes_keep_the_old_file_if_a_write_dies(tmp_path, writer, reader):
    store = Store(str(tmp_path / "store"))
    writer(store, {"version": 1})

    mp = _crash_before_replace()
    try:
        with pytest.raises(OSError):
            writer(store, {"version": 2})
    finally:
        mp.undo()

    assert reader(store) == {"version": 1}


def test_write_comparison_is_atomic(tmp_path, monkeypatch):
    import os
    store = Store(str(tmp_path))
    store.write_comparison("c1", {"v": 1})

    def boom(*a, **k):
        raise OSError("crash before replace")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_comparison("c1", {"v": 2})
    monkeypatch.undo()
    assert store.read_comparison("c1") == {"v": 1}
    assert not [p for p in os.listdir(os.path.dirname(store._comparison_path("c1"))) if p.startswith(".write-")]


def test_read_events_skips_torn_trailing_line(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    store.append_event({"a": 2})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write('{"a": 3, "tru')
    assert store.read_events() == [{"a": 1}, {"a": 2}]


def test_read_events_still_raises_on_mid_file_corruption(tmp_path):
    import json
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    store.append_event({"a": 2})
    with pytest.raises(json.JSONDecodeError):
        store.read_events()


def test_append_after_torn_line_keeps_log_readable(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write('{"a": 3, "tru')
    store.append_event({"a": 4})
    store.append_event({"a": 5})
    assert store.read_events() == [{"a": 1}, {"a": 4}, {"a": 5}]


def test_append_keeps_unterminated_complete_record(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write('{"keep": 2}')
    store.append_event({"b": 1})
    assert store.read_events() == [{"a": 1}, {"keep": 2}, {"b": 1}]


def test_append_to_file_with_only_torn_line(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "w", encoding="utf-8") as fh:
        fh.write('{"a":')
    store.append_event({"b": 1})
    assert store.read_events() == [{"b": 1}]


def test_concurrent_appends_lose_nothing(tmp_path):
    import threading
    store = Store(str(tmp_path))

    def work(t):
        for i in range(200):
            store.append_event({"t": t, "i": i, "pad": "x" * 300})
    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.read_events()) == 1600


def test_append_repairs_torn_line_longer_than_scan_chunk(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write('{"pad": "' + "x" * 20000)
    store.append_event({"b": 1})
    assert store.read_events() == [{"a": 1}, {"b": 1}]


def test_read_events_tolerates_invalid_utf8_torn_tail(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "ab") as fh:
        fh.write(b'{"a": "\xff\xfe')
    assert store.read_events() == [{"a": 1}]


@pytest.mark.parametrize("tail_len", [_TAIL_SCAN_CHUNK - 1, _TAIL_SCAN_CHUNK, _TAIL_SCAN_CHUNK + 1])
@pytest.mark.parametrize("prefix", [b"", b'{"a": 1}\n'])
def test_append_repairs_torn_tail_at_chunk_boundaries(tmp_path, tail_len, prefix):
    store = Store(str(tmp_path))
    os.makedirs(os.path.dirname(store._events_path()), exist_ok=True)
    with open(store._events_path(), "wb") as fh:
        fh.write(prefix + b'{"p": "' + b"x" * (tail_len - 7))
    store.append_event({"b": 1})
    expected = ([{"a": 1}] if prefix else []) + [{"b": 1}]
    assert store.read_events() == expected


def test_append_keeps_long_unterminated_complete_record(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    rec = json.dumps({"keep": "x" * 20000})
    with open(store._events_path(), "a", encoding="utf-8") as fh:
        fh.write(rec)
    store.append_event({"b": 1})
    assert [list(e) for e in store.read_events()] == [["a"], ["keep"], ["b"]]


def _append_many(root, n, start):
    start.wait()
    s = Store(root)
    for i in range(n):
        s.append_event({"i": i, "pad": "x" * 9000})


def test_concurrent_process_appends_lose_nothing(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    procs = [ctx.Process(target=_append_many, args=(str(tmp_path), 200, start)) for _ in range(4)]
    [p.start() for p in procs]
    start.set()  # release together so the appends actually overlap
    [p.join(timeout=120) for p in procs]
    assert not any(p.is_alive() for p in procs)
    assert all(p.exitcode == 0 for p in procs)
    assert len(Store(str(tmp_path)).read_events()) == 800


def test_read_events_raises_on_invalid_utf8_mid_file(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "ab") as fh:
        fh.write(b'{"x": "\xff"}\n')
    store.append_event({"b": 1})
    with pytest.raises(ValueError):
        store.read_events()


def test_read_events_treats_bare_cr_as_torn_not_terminator(tmp_path):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "ab") as fh:
        fh.write(b'{"a": 2, "tr\r')
    assert store.read_events() == [{"a": 1}]


@pytest.mark.parametrize("tail", [
    b'\xef\xbb\xbf{"k": 2}',
    b'\x00{\x00}',
    b'\xff\xfe{\x00}\x00',
    b'{"k": "\xed\xa0\x80"}',
])
def test_append_and_read_agree_on_non_utf8_tails(tmp_path, tail):
    store = Store(str(tmp_path))
    store.append_event({"a": 1})
    with open(store._events_path(), "ab") as fh:
        fh.write(tail)
    assert store.read_events() == [{"a": 1}]
    store.append_event({"b": 1})
    assert store.read_events() == [{"a": 1}, {"b": 1}]
