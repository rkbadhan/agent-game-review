"""The Claude Code session adapter (repair order 7, review 2026-09-06).

Covers the review's completion criteria: JSONL sessions produce faithful
tool-linked timelines, session execution status is never conflated with a task
outcome, and verifier-optional sessions get an honest limited view (UNVERIFIED,
not a borrowed pass).
"""

import json

import pytest

from agr.adapter import adapter_names, get_adapter
from agr.ingest_claude import CLAUDE_ADAPTER, convert
from agr.pipeline import analyze
from agr.store import Store


def _session_line(obj) -> str:
    return json.dumps(obj)


def _write_session(tmp_path, lines) -> object:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(_session_line(l) for l in lines), encoding="utf-8")
    return p


def _sample_session():
    return [
        {"type": "user", "sessionId": "abc12345",
         "message": {"role": "user", "content": "Fix the failing test in tests/."}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [
             {"type": "thinking", "thinking": "run the tests first"},
             {"type": "text", "text": "I'll run the test suite."},
             {"type": "tool_use", "id": "toolu_1", "name": "Bash",
              "input": {"command": "python -m pytest tests/"}},
         ]}},
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": "E   assert 404 == 200", "is_error": True},
         ]}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [
             {"type": "tool_use", "id": "toolu_2", "name": "Edit",
              "input": {"file_path": "tests/test_x.py", "old": "404", "new": "200"}},
         ]}},
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_2",
              "content": "edited", "is_error": False},
         ]}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [
             {"type": "text", "text": "Fixed."},
             {"type": "tool_use", "id": "toolu_3", "name": "Bash",
              "input": {"command": "python -m pytest tests/"}},
         ]}},
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_3",
              "content": "1 passed", "is_error": False},
         ]}},
        {"type": "summary", "summary": "the agent fixed a test"},
    ]


def test_registered_as_an_adapter():
    assert "claude" in adapter_names()
    assert get_adapter("claude") is CLAUDE_ADAPTER


def test_tool_linked_timeline_is_faithful(tmp_path):
    """Calls and results are linked by tool-use id and land in order: the
    failure, the fix, and the passing retry are all visible as adjacent
    call/result pairs."""
    path = _write_session(tmp_path, _sample_session())
    res = convert(path)
    doc = res.doc
    kinds = [(s["kind"], s.get("content", "")) for s in doc["steps"]]
    assert kinds[0] == ("task_received", "Fix the failing test in tests/.")

    # call → failed result, in order.
    call_idx = next(i for i, (k, _) in enumerate(kinds) if k == "tool_call")
    assert kinds[call_idx] == ("tool_call", "python -m pytest tests/")
    assert kinds[call_idx + 1][0] == "tool_result"
    assert kinds[call_idx + 1][1] == "E   assert 404 == 200"
    # the failed result carries its error status
    failed_result = doc["steps"][call_idx + 1]
    assert failed_result["exit_code"] == 1

    # The passing retry appears later, linked, exit 0.
    retry_idx = max(i for i, (k, _) in enumerate(kinds) if k == "tool_call")
    assert doc["steps"][retry_idx + 1]["exit_code"] == 0
    assert "1 passed" in doc["steps"][retry_idx + 1]["content"]

    # No invented identifiers and nothing dropped silently.
    assert not any("never observed a result" in w for w in res.warnings)


def test_session_status_is_never_a_task_outcome(tmp_path):
    """Session close is run_completed with synthetic provenance — never a
    final_submission; without a verifier the run is UNVERIFIED, not passed."""
    path = _write_session(tmp_path, _sample_session())
    res = convert(path)
    last = res.doc["steps"][-1]
    assert last["kind"] == "run_completed"
    assert last["provenance"] == "synthetic"
    assert not any(s["kind"] == "final_submission" for s in res.doc["steps"])

    store = Store(str(tmp_path / "store"))
    a = analyze(res.doc, store)
    assert a.outcome["status"] == "UNVERIFIED"
    assert a.outcome["total"] == 0


def test_verifier_optional_honest_limited_view(tmp_path, load_fixture):
    """A verifier sidecar can be supplied; without one the run still reviews —
    as UNVERIFIED with partial capture, never a borrowed pass."""
    doc = convert(_write_session(tmp_path, _sample_session())).doc
    store = Store(str(tmp_path / "store"))

    a = analyze(doc, store)
    assert a.outcome["status"] == "UNVERIFIED"

    (tmp_path / "s2").mkdir()
    with_verifier = convert(_write_session(tmp_path / "s2", _sample_session()),
                            verifier={"raw_output": "ok",
                                      "checks": [{"check_id": "C1", "name": "test passes",
                                                  "status": "passed", "source": "native_structured"}]})
    store2 = Store(str(tmp_path / "store2"))
    a2 = analyze(with_verifier.doc, store2)
    assert a2.outcome["status"] == "PASSED"
    assert a2.outcome["passed"] == 1 and a2.outcome["total"] == 1


def test_empty_session_is_a_protocol_failure_not_a_completion(tmp_path):
    path = _write_session(tmp_path, [
        {"type": "user", "sessionId": "abc12345",
         "message": {"role": "user", "content": "hello?"}},
    ])
    res = convert(path)
    last = res.doc["steps"][-1]
    assert last["kind"] == "run_failed"
    assert last.get("termination_reason") == "agent_protocol_failure"


def test_unparsable_session_raises(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        convert(p)
