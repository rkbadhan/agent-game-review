"""Argument-shape distribution per failing-call signature (item 31, 2026-09-08).

Builds a store from real Claude-adapter conversions (so tool_input is
genuinely retained, matching how this feature is actually used) and checks
the shape distribution groups correctly by (tool, error_signature).
"""

from __future__ import annotations

import json

import pytest

from agr import argument_shapes
from agr.ingest_claude import convert
from agr.pipeline import analyze
from agr.store import Store


def _session_line(obj) -> str:
    return json.dumps(obj)


def _write_session(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(_session_line(l) for l in lines), encoding="utf-8")
    return p


def _edit_failure_session(tmp_path, name, session_id, file_path, extra_input=None):
    tool_input = {"file_path": file_path, "old_string": "foo", "new_string": "bar"}
    if extra_input:
        tool_input.update(extra_input)
    lines = [
        {"type": "user", "sessionId": session_id,
         "message": {"role": "user", "content": "Fix the bug."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": tool_input}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "String to replace not found in file.", "is_error": True}]}},
    ]
    return _write_session(tmp_path, name, lines)


def _bash_failure_session(tmp_path, name, session_id):
    lines = [
        {"type": "user", "sessionId": session_id,
         "message": {"role": "user", "content": "Run the tests."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest tests/"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "AssertionError: 5 != 4", "is_error": True}]}},
    ]
    return _write_session(tmp_path, name, lines)


def _store_with(tmp_path, sessions):
    store = Store(str(tmp_path / "store"))
    for i, path in enumerate(sessions):
        doc = convert(path, task_id=f"task-{i}").doc
        analyze(doc, store)
    return store


def test_groups_by_tool_and_error_signature(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/calc.py")
    b = _edit_failure_session(tmp_path, "b.jsonl", "sess-b", "/app/other.py",
                              extra_input={"replace_all": True})
    c = _bash_failure_session(tmp_path, "c.jsonl", "sess-c")
    store = _store_with(tmp_path, [a, b, c])

    groups = argument_shapes.argument_shapes(store)
    assert len(groups) == 2  # (Edit, "String...not found...") and (Bash, "AssertionError...")

    edit_group = next(g for g in groups if g.key[0] == "Edit")
    assert edit_group.total_failing_calls == 2
    assert len(edit_group.shapes) == 2  # two distinct shapes, one each
    assert all(s["count"] == 1 and s["share"] == 0.5 for s in edit_group.shapes)

    bash_group = next(g for g in groups if g.key[0] == "Bash")
    assert bash_group.total_failing_calls == 1


def test_shape_never_leaks_retained_values_only_keys_and_types(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/secret_path.py")
    store = _store_with(tmp_path, [a])
    groups = argument_shapes.argument_shapes(store)
    shape = groups[0].shapes[0]
    flat = json.dumps(shape)
    assert "/app/secret_path.py" not in flat
    assert "foo" not in flat and "bar" not in flat
    # But the KEYS and TYPES are present.
    keys = {k for k, _t in shape["keys"]}
    assert keys == {"file_path", "old_string", "new_string"}
    types = {t for _k, t in shape["keys"]}
    assert types == {"str"}


def test_same_shape_across_two_calls_merges_into_one_entry(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    b = _edit_failure_session(tmp_path, "b.jsonl", "sess-b", "/app/y.py")
    store = _store_with(tmp_path, [a, b])
    groups = argument_shapes.argument_shapes(store)
    assert len(groups) == 1
    assert groups[0].total_failing_calls == 2
    assert len(groups[0].shapes) == 1  # same key set + types (file_path differs in VALUE only)
    assert groups[0].shapes[0]["count"] == 2
    assert groups[0].shapes[0]["share"] == 1.0


def test_call_with_no_retained_tool_input_is_skipped(tmp_path):
    """A Bash call's tool_input is {"command": "..."} — always present, so
    this test uses min_group_size to confirm an empty store yields nothing,
    not an error."""
    store = Store(str(tmp_path / "store"))
    assert argument_shapes.argument_shapes(store) == []


def test_to_dict_shape(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    store = _store_with(tmp_path, [a])
    d = argument_shapes.argument_shapes(store)[0].to_dict()
    assert set(d) == {"key", "total_failing_calls", "shapes"}


def test_cli_argument_shapes_subcommand(tmp_path, capsys):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    _store_with(tmp_path, [a])
    from agr.cli import main
    rc = main(["--store", str(tmp_path / "store"), "argument-shapes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Edit" in out
    assert "file_path:str" in out


# --- GR-3: run-scoped shapes and per-event link ------------------------------

from agr.schema import DerivedEvent  # noqa: E402


def _run_events(store):
    from agr import read
    r = read.list_runs(store)[0]
    raw = store.read_derived(r["run_id"], r["capture_id"], "events.json") or []
    return [DerivedEvent(**e) for e in raw]


def test_argument_shapes_for_events_is_the_same_rule_as_the_store_wide_view(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    b = _bash_failure_session(tmp_path, "b.jsonl", "sess-b")
    store = _store_with(tmp_path, [a, b])
    events = _run_events(store)  # the first run only
    run_groups = argument_shapes.argument_shapes_for_events(events)
    store_groups = argument_shapes.argument_shapes(store)
    assert run_groups  # this run has one failing call
    for g in run_groups:
        assert g.total_failing_calls == 1  # one failure in this run
        matching = next(sg for sg in store_groups if sg.key == g.key)
        assert matching.total_failing_calls >= g.total_failing_calls


def test_argument_shape_link_identifies_the_failing_call_shape(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/secret_path.py")
    store = _store_with(tmp_path, [a])
    events = _run_events(store)
    call = next(e for e in events if e.event_type == "tool_call")
    link = argument_shapes.argument_shape_link_for_event(events, call.event_id)
    assert link is not None
    assert link["tool"] == "Edit"
    assert link["shape_index"] == 0
    assert link["count"] == 1 and link["total_failing_calls"] == 1
    assert {k for k, _t in link["shape_keys"]} == {"file_path", "old_string", "new_string"}
    # A structure-only link: no retained value leaks into it.
    assert "/app/secret_path.py" not in json.dumps(link)
    assert "foo" not in json.dumps(link) and "bar" not in json.dumps(link)


def test_argument_shape_link_is_resolvable_from_either_side_of_the_pair(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    store = _store_with(tmp_path, [a])
    events = _run_events(store)
    result = next(e for e in events if e.event_type == "tool_result")
    assert argument_shapes.argument_shape_link_for_event(events, result.event_id) is not None


def test_argument_shape_link_is_none_for_a_non_failing_event(tmp_path):
    a = _edit_failure_session(tmp_path, "a.jsonl", "sess-a", "/app/x.py")
    store = _store_with(tmp_path, [a])
    events = _run_events(store)
    user = next(e for e in events if e.event_type != "tool_call" and e.event_type != "tool_result")
    assert argument_shapes.argument_shape_link_for_event(events, user.event_id) is None
    assert argument_shapes.argument_shape_link_for_event(events, "evt_missing") is None
