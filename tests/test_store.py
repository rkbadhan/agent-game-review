"""Store path safety: no run_id can write or read outside the store (F1)."""

import pytest

from agr.store import InvalidRunId, Store, validate_run_id

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
