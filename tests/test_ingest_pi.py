"""Pi adapter tests (spec §5.3).

The adapter is a pure format mapping: pi session JSONL -> ATIF-shaped doc.
These tests pin the mapping so a pi format change or an adapter edit surfaces
as a test failure, not a silently shifted review.
"""

from __future__ import annotations

import json

import pytest

from agr.ingest_pi import _active_branch, convert, load_verifier


def _session(entries: list[dict]) -> str:
    header = {"type": "session", "version": 3, "id": "sess-0001", "timestamp": "2026-07-28T10:00:00Z"}
    lines = [json.dumps(header)]
    prev = None
    for i, e in enumerate(entries):
        e.setdefault("id", f"e{i:04d}")
        e.setdefault("parentId", prev)
        e.setdefault("timestamp", "2026-07-28T10:00:01Z")
        prev = e["id"]
        lines.append(json.dumps(e))
    return "\n".join(lines)


def _write(tmp_path, content: str) -> str:
    p = tmp_path / "session.jsonl"
    p.write_text(content, encoding="utf-8")
    return str(p)


def _msg(role, **kw):
    return {"type": "message", "message": {"role": role, **kw}}


def test_basic_conversion_maps_tool_flow(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="Write hello to /out.txt"),
        _msg("assistant", content=[
            {"type": "thinking", "thinking": "plan first"},
            {"type": "text", "text": "Writing the file now."},
            {"type": "toolCall", "id": "c1", "name": "write", "arguments": {"path": "/out.txt", "content": "hello"}},
        ], provider="anthropic", model="claude-x", stopReason="toolUse"),
        _msg("toolResult", toolCallId="c1", toolName="write",
             content=[{"type": "text", "text": "wrote 5 bytes"}], isError=False),
    ]))
    res = convert(path, task_id="t1")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    # pi sessions carry no observable submission signal — session end is
    # run_completed (pi-adapter-0.4), never a synthesised agent submission.
    assert kinds[-1] == "run_completed"
    # tool result success -> exit_code 0 (recovery state machine reads this)
    tr = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert tr["exit_code"] == 0
    # tool call hoists the path argument for deterministic token matching
    tc = next(s for s in doc["steps"] if s["kind"] == "tool_call")
    assert tc["tool"] == "write" and tc.get("path") == "/out.txt"
    # run metadata from the assistant message
    assert doc["run"]["model"] == "claude-x"
    assert doc["run"]["agent"] == "pi-coding-agent"
    assert doc["run"]["task_id"] == "t1"
    # thinking preserved as a tagged model_output, not dropped
    assert any(s.get("content", "").startswith("[thinking]") for s in doc["steps"])


def test_tool_error_maps_to_failure_exit_code(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="run the thing"),
        _msg("assistant", content=[
            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "false"}}]),
        _msg("toolResult", toolCallId="c1", toolName="bash",
             content=[{"type": "text", "text": "exit 1"}], isError=True),
    ]))
    res = convert(path)
    tr = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert tr["exit_code"] == 1  # is_tool_failure() keys on this


def test_bash_execution_maps_with_exit_code(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="hi"),
        _msg("bashExecution", command="pytest", output="1 failed", exitCode=1, cancelled=False),
    ]))
    res = convert(path)
    steps = res.doc["steps"]
    call = next(s for s in steps if s["kind"] == "tool_call")
    result = next(s for s in steps if s["kind"] == "tool_result")
    assert call["tool"] == "bash" and call["content"] == "pytest"
    assert result["exit_code"] == 1


def test_compaction_maps_and_upgrades_capability(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="long task"),
        {"type": "compaction", "summary": "earlier work", "tokensBefore": 50000,
         "retainedTail": [{"role": "user", "content": "latest"}]},
        _msg("assistant", content=[{"type": "text", "text": "continuing"}]),
    ]))
    res = convert(path)
    doc = res.doc
    assert any(s["kind"] == "context_compaction" for s in doc["steps"])
    caps = doc["capabilities"]
    assert caps["compaction_boundary"] == "complete"
    # retainedTail -> full pre/post visibility, better than most sources reach
    assert caps["pre_post_compaction_context"] == "complete"


def test_compaction_without_retained_tail_is_partial(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="long task"),
        {"type": "compaction", "summary": "earlier work", "tokensBefore": 50000},
        _msg("assistant", content=[{"type": "text", "text": "continuing"}]),
    ]))
    res = convert(path)
    assert res.doc["capabilities"]["pre_post_compaction_context"] == "partial"
    assert any("retainedTail" in w for w in res.warnings)


def test_no_verifier_is_explicit_not_silent(tmp_path):
    path = _write(tmp_path, _session([_msg("user", content="do it")]))
    res = convert(path)
    assert "verifier" not in res.doc
    assert res.doc["capture_completeness"] == "partial"
    assert res.doc["capabilities"]["verifier_code"] == "unavailable"
    assert any("no verifier" in w for w in res.warnings)


def test_verifier_sidecar_attaches(tmp_path):
    path = _write(tmp_path, _session([_msg("user", content="do it")]))
    vpath = tmp_path / "verifier.json"
    vpath.write_text(json.dumps({
        "raw_output": "C1 pass",
        "checks": [{"check_id": "C1", "name": "artifact exists", "status": "passed"}],
    }), encoding="utf-8")
    res = convert(path, verifier=load_verifier(vpath))
    assert res.doc["verifier"]["checks"][0]["check_id"] == "C1"
    assert res.doc["capture_completeness"] == "complete"


def test_first_user_message_is_instruction_with_warning(tmp_path):
    path = _write(tmp_path, _session([_msg("user", content="build the widget")]))
    res = convert(path)
    assert res.doc["task"]["instruction"] == "build the widget"
    assert any("first user message" in w for w in res.warnings)


def test_active_branch_excludes_abandoned_branches(tmp_path):
    # Root -> A -> B (leaf of branch 1); root -> C (leaf of branch 2, current).
    entries = [
        {"type": "message", "id": "root", "parentId": None,
         "message": {"role": "user", "content": "task"}},
        {"type": "message", "id": "a", "parentId": "root",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "branch1"}]}},
        {"type": "message", "id": "c", "parentId": "root",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "branch2-current"}]}},
    ]
    branch = _active_branch([{"type": "session", "version": 3}] + entries)
    ids = [e["id"] for e in branch]
    assert ids == ["root", "c"]  # abandoned branch-1 message is not in this run's trajectory


def test_unmapped_role_warns_not_drops_silently(tmp_path):
    path = _write(tmp_path, _session([
        _msg("user", content="hi"),
        _msg("mysteryRole", content="???"),
    ]))
    res = convert(path)
    assert any("mysteryRole" in w for w in res.warnings)


def test_rejects_non_session_file(tmp_path):
    p = tmp_path / "not.jsonl"
    p.write_text('{"type":"message"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a pi session"):
        convert(str(p))


def test_converted_doc_ingests_end_to_end(tmp_path):
    """The adapter output must satisfy the real ingestion contract (§7.2)."""
    from agr.ingest import _validate
    path = _write(tmp_path, _session([
        _msg("user", content="Write hello to /out.txt"),
        _msg("assistant", content=[
            {"type": "toolCall", "id": "c1", "name": "write", "arguments": {"path": "/out.txt"}}]),
        _msg("toolResult", toolCallId="c1", toolName="write",
             content=[{"type": "text", "text": "ok"}], isError=False),
    ]))
    res = convert(path, task_id="t1", verifier={
        "raw_output": "C1 pass",
        "checks": [{"check_id": "C1", "name": "artifact exists", "status": "passed"}],
    })
    _validate(res.doc)  # raises on any contract violation
