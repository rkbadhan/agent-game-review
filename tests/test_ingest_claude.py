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
    its cost/usage metadata is preserved. Item 11 (2026-09-07): the stream's
    own ``subtype`` (here error_during_execution) is the termination reason —
    more specific than a generic "report_error" fallback."""
    sid = "b2345678-90ab-cdef-1234-567890abcdef"
    doc = convert(_write_session(tmp_path, _stream_lines(sid))).doc
    last = doc["steps"][-1]
    assert last["kind"] == "run_failed"
    assert last["termination_reason"] == "error_during_execution"
    assert last["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert last["total_cost_usd"] == 0.01
    # Item 11: cost/usage and the stream's own metadata also land on the RUN
    # record, not just the step — never lost to the derived-event allowlist.
    assert doc["run"]["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert doc["run"]["total_cost_usd"] == 0.01
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


def test_tool_result_without_is_error_is_ok(tmp_path):
    """Item 2 (2026-09-07): the Messages API populates ``is_error`` only on
    failure, so its absence IS the success signal — documented API semantics,
    not an inference. A result with no explicit error flag is ``status: ok``,
    never ``unknown`` (that reading previously made every successful tool call
    in a real transcript invisible to ignored_tool_failure's "resolved before
    submission" check, since nothing ever counted as resolved)."""
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
    assert result["status"] == "ok"


def test_tool_result_is_error_false_is_ok(tmp_path):
    lines = [
        {"type": "user", "sessionId": "sess-u2",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed",
             "is_error": False}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    result = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert result["status"] == "ok"


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


# --- items 7-11 (2026-09-07): Claude adapter correctness ---------------------

def test_isSidechain_entries_are_dropped_with_a_warning_not_merged(tmp_path):
    """Item 7: a Task-subagent turn (isSidechain: true) is dropped, never
    merged into main_agent's own trajectory — this core's linear event
    timeline has no parent-linkage for it."""
    lines = [
        {"type": "user", "sessionId": "sess-side-1",
         "message": {"role": "user", "content": "Explore the repo."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Task",
                      "input": {"subagent_type": "Explore", "prompt": "find the auth module"}}]}},
        {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "I am the subagent, searching..."}]}},
        {"type": "user", "isSidechain": True,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "sub1", "content": "found it"}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "Explore finished"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    contents = [s.get("content", "") for s in doc["steps"]]
    assert not any("I am the subagent" in c for c in contents)
    assert not any("found it" in c for c in contents)
    actors = {s["actor"] for s in doc["steps"]}
    assert "main_agent" in actors
    # The sidechain text never became a main_agent model_output.
    model_outputs = [s for s in doc["steps"] if s["kind"] == "model_output"]
    assert not any("I am the subagent" in s.get("content", "") for s in model_outputs)


def test_isMeta_entries_are_skipped(tmp_path):
    """Item 10: an isMeta-wrapped entry carries no trajectory content."""
    lines = [
        {"type": "user", "sessionId": "sess-meta-1",
         "message": {"role": "user", "content": "Fix the bug."}},
        {"type": "user", "isMeta": True,
         "message": {"role": "user", "content": "<system-reminder>internal note</system-reminder>"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "Working on it."}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert not any("internal note" in s.get("content", "") for s in doc["steps"])
    assert doc["task"]["instruction"] == "Fix the bug."


def test_system_reminder_and_command_blocks_are_stripped_from_user_text(tmp_path):
    """Item 10: harness-injected wrapper tags inside a user message's own text
    (not isMeta-wrapped, just present in the content string) are stripped."""
    lines = [
        {"type": "user", "sessionId": "sess-strip-1",
         "message": {"role": "user", "content":
             "<system-reminder>background context</system-reminder>Please fix the bug."}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert doc["task"]["instruction"] == "Please fix the bug."
    assert "system-reminder" not in doc["task"]["instruction"]


def test_compact_summary_never_becomes_the_task_instruction(tmp_path):
    """Item 10 acceptance: a session whose earliest available user message is
    an isCompactSummary continuation never has that text used as the task
    instruction — it stays empty, with a warning, rather than a fabricated
    'instruction' that is really harness-synthesised continuity text."""
    lines = [
        {"type": "user", "isCompactSummary": True,
         "message": {"role": "user", "content": "Summary of prior work: fixed 3 bugs."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "Continuing..."}]}},
    ]
    res = convert(_write_session(tmp_path, lines))
    assert res.doc["task"]["instruction"] == ""
    assert any("no real user message" in w for w in res.warnings)


def test_compact_boundary_becomes_context_compaction_with_summary_payload(tmp_path):
    """Item 8: a system/compact_boundary entry becomes its own
    context_compaction step (not swallowed as generic bookkeeping), and the
    isCompactSummary message that follows attaches to THAT event instead of
    becoming a separate environment_observation."""
    lines = [
        {"type": "user", "sessionId": "sess-compact-1",
         "message": {"role": "user", "content": "Do a big task."}},
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 100000}},
        {"type": "user", "isCompactSummary": True,
         "message": {"role": "user", "content": "Summary: did steps 1-5."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "Continuing from the summary."}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert "context_compaction" in kinds
    compaction_step = next(s for s in doc["steps"] if s["kind"] == "context_compaction")
    assert compaction_step["compactMetadata"] == {"trigger": "auto", "preTokens": 100000}
    assert compaction_step["compaction_summary"] == "Summary: did steps 1-5."
    # The summary did NOT also appear as its own environment_observation.
    observations = [s for s in doc["steps"] if s["kind"] == "environment_observation"]
    assert not any("did steps 1-5" in s.get("content", "") for s in observations)
    assert doc["capabilities"]["compaction_boundary"] == "complete"


def test_stream_format_detection_not_fooled_by_compact_boundary(tmp_path):
    """Item 9: a SAVED transcript (sessionId, no lowercase session_id
    anywhere) that happens to contain a compact_boundary system record must
    still be detected as claude_session, not misclassified as a stream
    export just because it has a type: system entry."""
    lines = [
        {"type": "user", "sessionId": "sess-fmt-1",
         "message": {"role": "user", "content": "Do the task."}},
        {"type": "system", "subtype": "compact_boundary", "compactMetadata": {}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert doc["source_type"] == "claude_session"


def test_bash_tool_use_result_lifts_stdout_stderr_interrupted(tmp_path):
    """Item 11: the raw toolUseResult sibling field (stdout/stderr/interrupted)
    is lifted onto the tool_result step, richer than the rendered content."""
    lines = [
        {"type": "user", "sessionId": "sess-tur-1",
         "message": {"role": "user", "content": "Run the build."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "make build"}}]}},
        {"type": "user", "toolUseResult": {"stdout": "built ok", "stderr": "", "interrupted": False},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "built ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    result = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert result["tool_use_result"] == {"stdout": "built ok", "stderr": "", "interrupted": False}


def test_stream_result_num_turns_and_permission_denials_land_on_run(tmp_path):
    """Item 11: num_turns, duration_ms, and permission_denials from the
    stream's terminal result record land on run metadata."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-meta-run-1", "model": "m"},
        {"type": "user", "session_id": "sess-meta-run-1",
         "message": {"role": "user", "content": "Do it."}},
        {"type": "assistant", "session_id": "sess-meta-run-1",
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "done"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sess-meta-run-1", "result": "ok",
         "num_turns": 4, "duration_ms": 1500,
         "permission_denials": [{"tool": "Bash", "reason": "blocked"}]},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert doc["run"]["num_turns"] == 4
    assert doc["run"]["duration_ms"] == 1500
    assert doc["run"]["permission_denials"] == [{"tool": "Bash", "reason": "blocked"}]


# --- PR #54 review findings (2026-09-07) -------------------------------------

def test_unflagged_meta_first_turn_does_not_swallow_the_real_instruction(tmp_path):
    """Finding #1: a first user turn that strips to "" (an unflagged wrapper,
    e.g. a bare slash-command echo not marked isMeta) must not claim the
    instruction slot — the real instruction that follows must still become
    task_received, not demote to an environment_observation."""
    lines = [
        {"type": "user", "sessionId": "sess-f1-1",
         "message": {"role": "user", "content":
             "<command-name>/init</command-name><command-message>init</command-message>"
             "<command-args></command-args>"}},
        {"type": "user", "message": {"role": "user", "content": "Please fix the failing test."}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert doc["task"]["instruction"] == "Please fix the failing test."
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds.count("task_received") == 1
    assert doc["steps"][0]["kind"] == "task_received"
    assert doc["steps"][0]["content"] == "Please fix the failing test."


def test_all_meta_first_turn_with_no_real_message_still_warns(tmp_path):
    """The "no real user message found" warning must still fire when every
    user turn strips to empty (the fix must not accidentally silence it)."""
    lines = [
        {"type": "user", "sessionId": "sess-f1-2",
         "message": {"role": "user", "content": "<command-name>/init</command-name>"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    res = convert(_write_session(tmp_path, lines))
    assert res.doc["task"]["instruction"] == ""
    assert any("no real user message" in w for w in res.warnings)


def test_all_meta_non_first_turn_emits_no_empty_observation(tmp_path):
    """Finding #5: a non-first user turn that strips to "" must not pad the
    trajectory with a content-free environment_observation."""
    lines = [
        {"type": "user", "sessionId": "sess-f5-1",
         "message": {"role": "user", "content": "Fix the bug."}},
        {"type": "user", "message": {"role": "user", "content":
             "<command-name>/compact</command-name>"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    observations = [s for s in doc["steps"] if s["kind"] == "environment_observation"]
    assert not any(o.get("content", "") == "" for o in observations)


def test_interrupted_tool_result_is_not_recorded_as_ok(tmp_path):
    """Finding #4: toolUseResult.interrupted:true means the call did not
    complete cleanly, even with no explicit is_error flag — must not be
    status: ok (which is_tool_success() would then treat as a resolved
    success)."""
    lines = [
        {"type": "user", "sessionId": "sess-f4-1",
         "message": {"role": "user", "content": "Run the long build."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "make build"}}]}},
        {"type": "user",
         "toolUseResult": {"stdout": "partial output", "stderr": "", "interrupted": True},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "partial output"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    result = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert result["status"] == "error"
    assert result["tool_use_result"]["interrupted"] is True


# --- items 22-24 (2026-09-08): claude -p stream-json specifics ---------------

def test_parent_tool_use_id_subagent_turn_is_dropped_like_isSidechain(tmp_path):
    """Item 22: claude -p --output-format stream-json subagent turns carry
    parent_tool_use_id on the message instead of isSidechain — item 7 must
    catch both, or a stream-json subagent turn silently merges into
    main_agent."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-p22-1", "model": "m"},
        {"type": "user", "session_id": "sess-p22-1",
         "message": {"role": "user", "content": "Explore the repo."}},
        {"type": "assistant", "session_id": "sess-p22-1",
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Task",
                      "input": {"subagent_type": "Explore", "prompt": "find auth"}}]}},
        {"type": "assistant", "session_id": "sess-p22-1",
         "message": {"role": "assistant", "model": "m", "parent_tool_use_id": "t1",
         "content": [{"type": "text", "text": "I am the stream subagent, searching..."}]}},
        {"type": "user", "session_id": "sess-p22-1",
         "message": {"role": "user", "parent_tool_use_id": "t1", "content": [
             {"type": "tool_result", "tool_use_id": "sub1", "content": "found it"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    contents = [s.get("content", "") for s in doc["steps"]]
    assert not any("I am the stream subagent" in c for c in contents)
    assert not any(c == "found it" for c in contents)


def test_init_record_lifts_model_tools_mcp_permission_mode_cwd(tmp_path):
    """Item 24: the stream's own init record carries real run configuration
    that was previously discarded as generic harness bookkeeping."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-init-1",
         "model": "claude-opus-4-8", "tools": ["Bash", "Edit", "Read"],
         "mcp_servers": [{"name": "github", "status": "connected"}],
         "permissionMode": "acceptEdits", "cwd": "/home/user/project"},
        {"type": "user", "session_id": "sess-init-1",
         "message": {"role": "user", "content": "Do the task."}},
        {"type": "assistant", "session_id": "sess-init-1",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    run = doc["run"]
    assert run["tools"] == ["Bash", "Edit", "Read"]
    assert run["mcp_servers"] == [{"name": "github", "status": "connected"}]
    assert run["permission_mode"] == "acceptEdits"
    assert run["cwd"] == "/home/user/project"


def test_configuration_id_defaults_from_init_record_hash_when_not_supplied(tmp_path):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-cfg-1",
         "model": "claude-opus-4-8", "tools": ["Bash"], "permissionMode": "default"},
        {"type": "user", "session_id": "sess-cfg-1",
         "message": {"role": "user", "content": "Do it."}},
    ]
    res = convert(_write_session(tmp_path, lines))
    assert res.doc["run"]["configuration_id"].startswith("claude-config-")
    assert any("configuration_id defaulted" in w for w in res.warnings)


def test_explicit_configuration_id_overrides_init_record_default(tmp_path):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-cfg-2",
         "model": "claude-opus-4-8", "tools": ["Bash"]},
        {"type": "user", "session_id": "sess-cfg-2",
         "message": {"role": "user", "content": "Do it."}},
    ]
    res = convert(_write_session(tmp_path, lines), configuration_id="my-explicit-config")
    assert res.doc["run"]["configuration_id"] == "my-explicit-config"


def test_two_init_records_with_same_shape_hash_identically(tmp_path):
    """Same model/tools/mcp/permission-mode across two independent sessions
    -> same default configuration_id, so they group as one configuration."""
    def _lines(sid):
        return [
            {"type": "system", "subtype": "init", "session_id": sid,
             "model": "claude-opus-4-8", "tools": ["Bash", "Edit"],
             "permissionMode": "default"},
            {"type": "user", "session_id": sid, "message": {"role": "user", "content": "go"}},
        ]
    p1 = tmp_path / "a.jsonl"
    p1.write_text("\n".join(_session_line(l) for l in _lines("sess-a")), encoding="utf-8")
    p2 = tmp_path / "b.jsonl"
    p2.write_text("\n".join(_session_line(l) for l in _lines("sess-b")), encoding="utf-8")
    cfg1 = convert(p1).doc["run"]["configuration_id"]
    cfg2 = convert(p2).doc["run"]["configuration_id"]
    assert cfg1 == cfg2


def test_permission_denial_marks_the_specific_tool_result(tmp_path):
    """Item 23: a permission_denials entry correlated by tool_use_id marks
    THAT tool_result — it must never open a failure/recovery episode."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-perm-1", "model": "m"},
        {"type": "user", "session_id": "sess-perm-1",
         "message": {"role": "user", "content": "Delete the temp files."}},
        {"type": "assistant", "session_id": "sess-perm-1",
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "rm -rf /tmp/scratch"}}]}},
        {"type": "user", "session_id": "sess-perm-1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "permission denied by user", "is_error": True}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sess-perm-1", "result": "done",
         "permission_denials": [{"tool_use_id": "t1", "tool_name": "Bash"}]},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    result = next(s for s in doc["steps"] if s["kind"] == "tool_result")
    assert result["permission_denied"] is True
    assert doc["run"]["permission_denials"] == [{"tool_use_id": "t1", "tool_name": "Bash"}]


def test_uncorrelated_permission_denial_warns_instead_of_guessing(tmp_path):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-perm-2", "model": "m"},
        {"type": "user", "session_id": "sess-perm-2",
         "message": {"role": "user", "content": "Do it."}},
        {"type": "assistant", "session_id": "sess-perm-2",
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "session_id": "sess-perm-2",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sess-perm-2", "result": "done",
         "permission_denials": [{"tool_name": "Bash"}]},
    ]
    res = convert(_write_session(tmp_path, lines))
    result = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "permission_denied" not in result
    assert any("could not be correlated" in w for w in res.warnings)


# --- item 27 (2026-09-08): per-step cost and timestamps ----------------------

def test_every_step_carries_the_entrys_timestamp(tmp_path):
    lines = [
        {"type": "user", "sessionId": "sess-ts-1", "timestamp": "2026-09-08T10:00:00Z",
         "message": {"role": "user", "content": "Do it."}},
        {"type": "assistant", "timestamp": "2026-09-08T10:00:05Z",
         "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    assert doc["steps"][0]["timestamp"] == "2026-09-08T10:00:00Z"
    assert doc["steps"][1]["timestamp"] == "2026-09-08T10:00:05Z"


def test_message_usage_attaches_to_first_step_only_deduped_by_message_id(tmp_path):
    """A multi-block message (text + two tool_use) fans out into three
    steps, but message.usage is a MESSAGE-level aggregate — it must land on
    only the FIRST step, not once per block (which would multiply the
    apparent cost by 3x when a fleet view later sums per-step cost)."""
    lines = [
        {"type": "user", "sessionId": "sess-cost-1",
         "message": {"role": "user", "content": "Do two things."}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_1",
         "usage": {"input_tokens": 100, "output_tokens": 40},
         "content": [
             {"type": "text", "text": "I'll do both."},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
             {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pwd"}},
         ]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    non_task_steps = [s for s in doc["steps"] if s["kind"] != "task_received"]
    assert len(non_task_steps) == 3
    costs = [s.get("cost") for s in non_task_steps]
    assert costs[0] == {"usage": {"input_tokens": 100, "output_tokens": 40}}
    assert costs[1] is None
    assert costs[2] is None
    assert non_task_steps[0]["generation_event"] is True
    assert doc["capabilities"]["generation_usage"] == "complete"


def test_later_record_can_supply_usage_for_existing_message_generation(tmp_path):
    lines = [
        {"type": "user", "sessionId": "sess-cost-late",
         "message": {"role": "user", "content": "Go."}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_late",
         "content": [{"type": "thinking", "thinking": "working"}]}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_late",
         "usage": {"input_tokens": 200_000, "output_tokens": 10},
         "content": [{"type": "text", "text": "done"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    generations = [step for step in doc["steps"] if step.get("generation_event")]
    assert len(generations) == 1
    assert generations[0]["cost"] == {
        "usage": {"input_tokens": 200_000, "output_tokens": 10}
    }
    assert doc["capabilities"]["generation_usage"] == "complete"


def test_two_messages_each_get_their_own_cost_by_distinct_message_id(tmp_path):
    lines = [
        {"type": "user", "sessionId": "sess-cost-2",
         "message": {"role": "user", "content": "Go."}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_a",
         "usage": {"input_tokens": 10, "output_tokens": 5},
         "content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_b",
         "usage": {"input_tokens": 20, "output_tokens": 8},
         "content": [{"type": "text", "text": "second"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    model_outputs = [s for s in doc["steps"] if s["kind"] == "model_output"]
    assert model_outputs[0]["cost"] == {"usage": {"input_tokens": 10, "output_tokens": 5}}
    assert model_outputs[1]["cost"] == {"usage": {"input_tokens": 20, "output_tokens": 8}}


def test_step_cost_survives_into_derived_event(tmp_path):
    """The raw ATIF step's cost key must actually reach DerivedEvent.cost —
    the whole point is a fleet view aggregating cost per event/episode."""
    from agr.events import derive_events
    from agr.schema import RunSource
    lines = [
        {"type": "user", "sessionId": "sess-cost-3",
         "message": {"role": "user", "content": "Go."}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m", "id": "msg_c",
         "usage": {"input_tokens": 7, "output_tokens": 3},
         "content": [{"type": "text", "text": "ok"}]}},
    ]
    doc = convert(_write_session(tmp_path, lines)).doc
    rs = RunSource(run_id="r", source_capture_id="c", capture_revision=1, source_hash="h",
                   source_type="claude_session", source_schema="s", capture_completeness="partial",
                   task_id="t")
    events = derive_events(doc, rs)
    model_output = next(e for e in events if e.event_type == "model_output")
    assert model_output.cost == {"usage": {"input_tokens": 7, "output_tokens": 3}}
