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


def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    # the failed result carries its error STATUS (a tool flag, not an
    # synthesized OS exit code — review 2026-09-07 R3)
    failed_result = doc["steps"][call_idx + 1]
    assert failed_result["status"] == "error"
    assert "exit_code" not in failed_result

    # The passing retry appears later, linked, status ok.
    retry_idx = max(i for i, (k, _) in enumerate(kinds) if k == "tool_call")
    assert doc["steps"][retry_idx + 1]["status"] == "ok"
    assert "1 passed" in doc["steps"][retry_idx + 1]["content"]

    # No invented identifiers and nothing dropped silently.
    assert not any("never observed a result" in w for w in res.warnings)


def test_edit_and_write_tool_inputs_are_retained_in_full(tmp_path):
    """Review 2026-09-07 (R2) acceptance: an Edit's old/new strings and a
    Write's content survive conversion as retained evidence — display excerpts
    stay separate, but the model can inspect the change that was made."""
    lines = [
        {"type": "user", "sessionId": "sess-edit-1",
         "message": {"role": "user", "content": "Fix the test."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [{"type": "tool_use", "id": "toolu_e1", "name": "Edit",
                      "input": {"file_path": "app.py", "old_string": "return 404",
                                 "new_string": "return 200"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_e1", "content": "edited"}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [{"type": "tool_use", "id": "toolu_w1", "name": "Write",
                      "input": {"file_path": "new.py", "content": "print('hello')"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_w1", "content": "written"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    calls = [s for s in doc["steps"] if s["kind"] == "tool_call"]
    edit = next(c for c in calls if c["tool"] == "Edit")
    assert edit["tool_input"] == {"file_path": "app.py", "old_string": "return 404",
                                  "new_string": "return 200"}
    write = next(c for c in calls if c["tool"] == "Write")
    assert write["tool_input"]["content"] == "print('hello')"
    # Every call carries its tool-use id; every result links back by id.
    results = [s for s in doc["steps"] if s["kind"] == "tool_result"]
    assert {c["tool_use_id"] for c in calls} == {r["tool_use_id"] for r in results}


def test_parallel_tool_results_pair_by_id_not_adjacency(tmp_path):
    """Review 2026-09-07 (R2) acceptance: two parallel calls whose results
    arrive out of order keep their own linkage — the pytest failure is linked
    to the pytest call, the curl success to the curl call, by tool-use id."""
    lines = [
        {"type": "user", "sessionId": "sess-par-1",
         "message": {"role": "user", "content": "Run the tests and check the API."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [
             {"type": "tool_use", "id": "toolu_a", "name": "Bash",
              "input": {"command": "python -m pytest tests/"}},
             {"type": "tool_use", "id": "toolu_b", "name": "Bash",
              "input": {"command": "curl https://api.example.com/health"}},
         ]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_b", "content": "200 OK",
              "is_error": False},
             {"type": "tool_result", "tool_use_id": "toolu_a", "content": "E: failed",
              "is_error": True},
         ]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    steps = doc["steps"]
    pytest_call = next(s for s in steps if s["kind"] == "tool_call"
                       and "pytest" in s.get("content", ""))
    curl_call = next(s for s in steps if s["kind"] == "tool_call"
                     and "curl" in s.get("content", ""))
    pytest_result = next(s for s in steps if s["kind"] == "tool_result"
                         and s["tool_use_id"] == "toolu_a")
    curl_result = next(s for s in steps if s["kind"] == "tool_result"
                       and s["tool_use_id"] == "toolu_b")
    assert pytest_result["tool"] == pytest_call["tool"] and pytest_result["status"] == "error"
    assert curl_result["status"] == "ok"
    # The tool name on each result comes from ITS call (id lookup), not from
    # whichever call happened to precede it.
    assert "curl" in curl_call["content"] and pytest_result["content"] == "E: failed"


def test_session_status_is_never_a_task_outcome(tmp_path):
    """Review 2026-09-07 (R3): the end of a transcript does NOT establish
    completion — a saved session with no terminal result reports
    ``session_completion: unobserved`` and synthesizes NO completion event;
    without a verifier the run is UNVERIFIED, not passed."""
    path = _write_session(tmp_path, _sample_session())
    res = convert(path)
    doc = res.doc
    assert not any(s["kind"] in ("run_completed", "run_failed", "final_submission")
                   for s in doc["steps"])
    assert doc["run"]["session_completion"] == "unobserved"
    assert any("completion is NOT observed" in w for w in res.warnings)

    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
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


# --- review 2026-09-07 (R3): the claude -p input family ----------------------


def _stream_lines(session_id="b2345678-90ab-cdef-1234-567890abcdef"):
    return [
        {"type": "system", "subtype": "init", "session_id": session_id,
         "model": "claude-opus-4-8"},
        {"type": "assistant", "session_id": session_id,
         "message": {"role": "assistant", "model": "claude-opus-4-8", "content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Bash",
              "input": {"command": "python -m pytest tests/"}}]}},
        {"type": "user", "session_id": session_id,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": "E: failed", "is_error": True}]}},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "session_id": session_id, "result": "command failed",
         "usage": {"input_tokens": 10, "output_tokens": 5},
         "total_cost_usd": 0.01},
    ]


def test_stream_with_error_result_is_an_errored_run_not_completed(tmp_path):
    """A terminal result with is_error:true ends the run in error — the result
    record is honored, not skipped in favour of a synthetic completion, and
    its cost/usage metadata is preserved."""
    sid = "b2345678-90ab-cdef-1234-567890abcdef"
    doc = convert(_write_session(tmp_path, _stream_lines(sid))).doc
    last = doc["steps"][-1]
    assert last["kind"] == "run_failed"
    assert last["termination_reason"] == "report_error"
    assert last["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert last["total_cost_usd"] == 0.01
    # Full session identity from the stream's lowercase session_id field.
    assert doc["run"]["source_session_id"] == sid
    assert sid in doc["run"]["logical_run_id"]
    assert doc["run"]["session_completion"] == "observed"


def test_stream_session_id_used_for_identity(tmp_path):
    """A stream's ``session_id`` is used — the file stem fallback was the bug
    (R3): the source-supplied identity must survive."""
    sid = "cc-1234-abcd-full-identity"
    lines = _stream_lines(sid)[:3]  # no terminal result: interrupted stream
    res = convert(_write_session(tmp_path, lines))
    assert res.doc["run"]["source_session_id"] == sid
    assert sid in res.doc["run"]["logical_run_id"]
    assert res.doc["run"]["session_completion"] == "unobserved"


def test_full_session_ids_never_collide_on_a_prefix(tmp_path):
    """R3 acceptance: two distinct session ids sharing an 8-char prefix stay
    two distinct identities — truncation is gone."""
    a = convert(_write_session(_mkdir(tmp_path / "one"), _stream_lines("abc12345aaaaaaaa-aa")))
    b = convert(_write_session(_mkdir(tmp_path / "two"), _stream_lines("abc12345bbbbbbbb-bb")))
    assert a.doc["run"]["logical_run_id"] != b.doc["run"]["logical_run_id"]
    assert a.doc["run"]["task_id"] != b.doc["run"]["task_id"]


def test_pending_tool_call_means_partial_tool_result_coverage(tmp_path):
    """A saved session ending in an unanswered tool call warns AND downgrades
    tool-result coverage — it never declares results complete (R3)."""
    lines = [
        {"type": "user", "sessionId": "sess-pend-1",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest"}}]}},
    ]
    res = convert(_write_session(tmp_path, lines))
    assert any("never observed a result" in w for w in res.warnings)
    assert res.doc["capabilities"]["tool_results"] == "partial"


def test_api_error_ending_does_not_claim_completion(tmp_path):
    """An assistant API-error entry is recorded as an observed error; with no
    completion signal after it, no run_completed is appended (R3)."""
    lines = [
        {"type": "user", "sessionId": "sess-err-1",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "isApiErrorMessage": True,
         "message": {"role": "assistant", "model": "m", "content": []}},
    ]
    res = convert(_write_session(tmp_path, lines))
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert "error_observed" in kinds
    assert "run_completed" not in kinds


def test_tool_result_without_is_error_is_unknown_not_success(tmp_path):
    """R3: a result with no explicit error flag is tool status UNKNOWN —
    never a synthesized ``exit_code: 0`` success."""
    lines = [
        {"type": "user", "sessionId": "sess-u1",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    result = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert "exit_code" not in result
    assert "status" not in result  # unknown: neither success nor failure claimed


def test_final_result_only_object_gets_a_limited_view(tmp_path):
    """R3 acceptance: a final-result-only export produces a useful limited
    view that explicitly says NO trajectory was captured — it never resembles
    a fully captured completed execution."""
    p = tmp_path / "final.json"
    p.write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "All checks passed.",
        "usage": {"input_tokens": 4}, "total_cost_usd": 0.02,
    }), encoding="utf-8")
    res = convert(p)
    doc = res.doc
    assert doc["source_type"] == "claude_final_result"
    assert doc["capabilities"]["messages"] == "final_only"
    assert doc["capabilities"]["tool_calls"] == "unavailable"
    assert [s["kind"] for s in doc["steps"]] == ["model_output"]
    assert doc["steps"][0]["content"] == "All checks passed."
    assert doc["run"]["total_cost_usd"] == 0.02
    assert any("no trajectory" in w for w in res.warnings)
