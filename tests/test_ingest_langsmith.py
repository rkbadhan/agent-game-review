"""LangSmith adapter tests (docs/span-adapters.md).

Same toy "config-fixer" agent run as the OTel/Langfuse fixtures, expressed as
LangSmith runs + feedback. validate_config fails once with LangSmith's own
native `error` string and is retried by the framework; a nested "reviewer"
chain run has no subagent marker, which is the one honest scope reduction
this adapter documents (see ingest_langsmith.py's module docstring) and this
file pins directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agr.adapter import get_adapter
from agr.ingest_langsmith import LANGSMITH_ADAPTER, LANGSMITH_ADAPTER_VERSION, convert

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "langsmith" / "langsmith_toy_run.json"


def test_registered_after_langfuse():
    from agr.adapter import adapter_names
    names = adapter_names()
    assert names.index("langsmith") > names.index("langfuse")
    adapter = get_adapter("langsmith")
    assert adapter is LANGSMITH_ADAPTER
    assert adapter.version == LANGSMITH_ADAPTER_VERSION


def test_maps_kinds_and_order():
    res = convert(FIXTURE, task_id="t")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert "final_submission" not in kinds
    assert doc["source_type"] == "langsmith"


def test_root_run_inputs_outputs_become_instruction_and_terminal():
    res = convert(FIXTURE, task_id="t")
    doc = res.doc
    assert "fix the port field" in doc["task"]["instruction"]
    terminal = doc["steps"][-1]
    assert terminal["kind"] == "run_completed"
    assert "reviewer subagent confirmed" in terminal["content"]


def test_explicit_error_string_marks_the_run_as_error():
    res = convert(FIXTURE, task_id="t")
    validate_steps = [s for s in res.doc["steps"] if s.get("tool") == "validate_config"]
    first_result = [s for s in validate_steps if s["kind"] == "tool_result"][0]
    assert first_result["status"] == "error"
    assert "connection refused" in first_result["content"]


def test_flaky_tool_retry_recognised():
    res = convert(FIXTURE, task_id="t")
    validate_steps = [s for s in res.doc["steps"] if s.get("tool") == "validate_config"]
    kinds = [s["kind"] for s in validate_steps]
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("retry") == 1
    assert res.meta["retry_count"] == 1


def test_no_error_and_captured_output_is_ok():
    res = convert(FIXTURE, task_id="t")
    read_result = [s for s in res.doc["steps"]
                   if s["kind"] == "tool_result" and s.get("tool") == "read_config"][0]
    assert read_result["status"] == "ok"


def test_nested_chain_run_has_no_subagent_marker_and_is_attributed_to_main_agent():
    """The documented LangSmith scope reduction: no explicit agent/subagent
    signal exists in run_type, so diff_check (nested under the 'reviewer'
    chain run) is attributed to main_agent, not a subagent, and a warning
    says so."""
    res = convert(FIXTURE, task_id="t")
    diff_call = [s for s in res.doc["steps"] if s.get("tool") == "diff_check"]
    assert diff_call and diff_call[0]["actor"] == "main_agent"
    assert any("no explicit agent/subagent marker" in w for w in res.warnings)


def test_usage_metadata_carried_as_cost():
    res = convert(FIXTURE, task_id="t")
    model_steps = [s for s in res.doc["steps"] if s["kind"] == "model_output"]
    with_cost = [s for s in model_steps if "cost" in s]
    assert with_cost
    assert with_cost[0]["cost"]["input_tokens"] > 0


def test_session_id_prefers_thread_id_else_session_id():
    res = convert(FIXTURE, task_id="t")
    assert res.doc["run"]["source_session_id"] == "proj-toy-001"


# --- feedback -> verifier.checks ----------------------------------------------

def test_feedback_synthesizes_verifier_when_no_explicit_one_given():
    res = convert(FIXTURE, task_id="t")
    assert "verifier" in res.doc
    checks = res.doc["verifier"]["checks"]
    assert len(checks) == 2
    assert all(c["source"] == "instrumented_assertion" for c in checks)


def test_boolean_like_feedback_score_maps_to_passed():
    res = convert(FIXTURE, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["correctness"]["status"] == "passed"


def test_ambiguous_numeric_feedback_score_maps_to_unknown_with_warning():
    res = convert(FIXTURE, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["helpfulness"]["status"] == "unknown"
    assert any("no clear pass/fail mapping" in w for w in res.warnings)


def test_explicit_verifier_overrides_feedback():
    verifier = {"raw_output": "explicit", "checks": [
        {"check_id": "c1", "status": "passed", "source": "native_structured"}]}
    res = convert(FIXTURE, task_id="t", verifier=verifier)
    assert res.doc["verifier"] == verifier


def test_no_feedback_and_no_verifier_ingests_unverified(tmp_path):
    export = json.loads(FIXTURE.read_text())
    export["feedback"] = []
    p = tmp_path / "no_feedback.json"
    p.write_text(json.dumps(export), encoding="utf-8")
    res = convert(p, task_id="t")
    assert "verifier" not in res.doc
    assert any("no verifier evidence" in w for w in res.warnings)


# --- input formats and structural edge cases -----------------------------------

def test_bare_list_of_runs_is_accepted_with_no_feedback(tmp_path):
    export = json.loads(FIXTURE.read_text())
    p = tmp_path / "bare_runs.json"
    p.write_text(json.dumps(export["runs"]), encoding="utf-8")
    res = convert(p, task_id="t")
    assert "verifier" not in res.doc
    assert len(res.doc["steps"]) > 1


def test_rejects_malformed_export(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"not_runs": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a LangSmith run export"):
        convert(bad)


def test_rejects_empty_run_list(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text('{"runs": [], "feedback": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="no runs"):
        convert(empty)


def test_task_id_defaulted_from_root_run_id():
    res = convert(FIXTURE)
    assert res.doc["run"]["task_id"].startswith("langsmith-trace-")
    assert any("task_id derived from the root run id" in w for w in res.warnings)


def test_adapter_object_matches_convert_function():
    a = LANGSMITH_ADAPTER.convert(FIXTURE, task_id="t")
    b = convert(FIXTURE, task_id="t")
    assert a.doc["run"]["task_id"] == b.doc["run"]["task_id"]
    assert len(a.doc["steps"]) == len(b.doc["steps"])
