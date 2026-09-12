"""OTel GenAI adapter tests (docs/span-adapters.md).

Pins the OTLP JSON field mapping into agr.span_tree.NormalizedSpan and the
resulting document over a toy agent run fixture: read a config file, run a
flaky validator (fails once, framework-retried, then succeeds), write the
fix, and have a nested "reviewer" subagent double-check the change.

Two fixture variants exercise the two ends of the "content catch"
(docs/span-adapters.md): otel_toy_run.json has opt-in content capture
enabled; otel_toy_run_no_content.json is the identical trace with no
captured arguments/results/messages, only names/timings/usage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agr.adapter import get_adapter
from agr.ingest_otel import OTEL_ADAPTER, OTEL_ADAPTER_VERSION, convert

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "otel"
FULL = FIXTURE_DIR / "otel_toy_run.json"
NO_CONTENT = FIXTURE_DIR / "otel_toy_run_no_content.json"


def test_registered_after_claude():
    from agr.adapter import adapter_names
    names = adapter_names()
    assert names.index("otel") > names.index("claude")
    adapter = get_adapter("otel")
    assert adapter is OTEL_ADAPTER
    assert adapter.version == OTEL_ADAPTER_VERSION


def test_full_content_fixture_maps_kinds_and_order():
    res = convert(FULL, task_id="config-fixer-task")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    assert kinds[-1] == "run_completed"
    assert "final_submission" not in kinds
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert doc["source_type"] == "otel"
    assert doc["adapter_version"] == OTEL_ADAPTER_VERSION


def test_task_instruction_and_final_output_from_root_span():
    res = convert(FULL, task_id="t")
    doc = res.doc
    assert "fix the port field" in doc["task"]["instruction"]
    terminal = doc["steps"][-1]
    assert "reviewer subagent confirmed" in terminal["content"]


def test_tool_name_from_gen_ai_tool_name():
    res = convert(FULL, task_id="t")
    tool_calls = [s for s in res.doc["steps"] if s["kind"] == "tool_call"]
    tools = [c["tool"] for c in tool_calls]
    assert "read_config" in tools
    assert "write_config" in tools
    assert "validate_config" in tools


def test_flaky_tool_retry_recognised_as_one_retry_step():
    """validate_config fails once (ConnectionError) then the framework retries
    it immediately -- known ground truth in the fixture: exactly one retry,
    and validate_config appears as ONE tool_call/tool_result pair plus ONE
    retry step, never two independent pairs."""
    res = convert(FULL, task_id="t")
    steps = res.doc["steps"]
    validate_related = [s for s in steps if s.get("tool") == "validate_config"]
    kinds = [s["kind"] for s in validate_related]
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("retry") == 1
    retry_step = [s for s in validate_related if s["kind"] == "retry"][0]
    assert retry_step["status"] == "ok"
    assert res.meta["retry_count"] == 1
    assert any("retry" in w and "recognised" in w for w in res.warnings)


def test_subagent_grouping_reviewer_not_merged_into_main_agent():
    res = convert(FULL, task_id="t")
    steps = res.doc["steps"]
    diff_call = [s for s in steps if s.get("tool") == "diff_check"]
    assert diff_call and diff_call[0]["actor"] == "subagent:reviewer"
    main_calls = [s for s in steps if s["kind"] == "tool_call" and s["actor"] == "main_agent"]
    assert {c["tool"] for c in main_calls} == {"read_config", "validate_config", "write_config"}
    assert "reviewer" in res.meta["subagent_names"]


def test_usage_tokens_carried_as_step_cost():
    res = convert(FULL, task_id="t")
    model_steps = [s for s in res.doc["steps"] if s["kind"] == "model_output"]
    with_cost = [s for s in model_steps if "cost" in s]
    assert with_cost
    assert with_cost[0]["cost"]["input_tokens"] > 0


def test_full_content_declares_complete_capabilities():
    res = convert(FULL, task_id="t")
    caps = res.doc["capabilities"]
    assert caps["tool_calls"] == "complete"
    assert caps["tool_results"] == "complete"
    assert caps["messages"] == "complete"


def test_session_id_from_conversation_id():
    res = convert(FULL, task_id="t")
    assert res.doc["run"]["source_session_id"] == "conv-toy-001"


def test_configuration_id_defaulted_from_model_and_tools_when_absent():
    res = convert(FULL, task_id="t")
    assert res.doc["run"]["configuration_id"].startswith("otel-config-")
    assert any("configuration_id defaulted" in w for w in res.warnings)


def test_explicit_configuration_id_is_never_overridden():
    res = convert(FULL, task_id="t", configuration_id="pinned-config")
    assert res.doc["run"]["configuration_id"] == "pinned-config"
    assert not any("configuration_id defaulted" in w for w in res.warnings)


# --- the content catch: no opt-in content captured ---------------------------

def test_no_content_fixture_declares_partial_or_unavailable():
    res = convert(NO_CONTENT, task_id="t")
    caps = res.doc["capabilities"]
    assert caps["tool_calls"] == "unavailable"
    assert caps["tool_results"] == "unavailable"
    assert caps["messages"] == "unavailable"
    assert any("tool_calls declared" in w for w in res.warnings)
    assert any("tool_results declared" in w for w in res.warnings)
    assert any("messages declared" in w for w in res.warnings)


def test_no_content_fixture_still_maps_names_timings_and_usage():
    """Without opt-in content: no arguments/results, but tool names, timings,
    and token usage are still present -- exactly the "enough for the fleet
    table" case the design doc describes."""
    res = convert(NO_CONTENT, task_id="t")
    tool_calls = [s for s in res.doc["steps"] if s["kind"] == "tool_call"]
    assert {c["tool"] for c in tool_calls} == {
        "read_config", "validate_config", "write_config", "diff_check"}
    assert all("content" not in c for c in tool_calls)
    model_steps = [s for s in res.doc["steps"] if s["kind"] == "model_output"]
    assert any("cost" in s for s in model_steps)
    assert all("content" not in s for s in model_steps)


def test_no_content_fixture_tool_result_status_is_unknown_not_ok():
    """Design doc: 'no error and no captured output is unknown, not passed.'"""
    res = convert(NO_CONTENT, task_id="t")
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result" and s.get("tool") == "read_config"]
    assert results[0]["status"] == "unknown"


def test_no_content_fixture_error_status_still_recognised():
    """A span's explicit ERROR status survives even without captured output —
    the flaky tool's first attempt is still an observed error."""
    res = convert(NO_CONTENT, task_id="t")
    validate_steps = [s for s in res.doc["steps"] if s.get("tool") == "validate_config"]
    results = [s for s in validate_steps if s["kind"] == "tool_result"]
    assert results[0]["status"] == "error"


def test_no_content_fixture_instruction_is_empty_and_warned():
    res = convert(NO_CONTENT, task_id="t")
    assert res.doc["task"]["instruction"] == ""
    assert any("no captured input" in w for w in res.warnings)


# --- identity, verifier, and honesty defaults ---------------------------------

def test_no_verifier_ingests_unverified_with_warning():
    res = convert(FULL, task_id="t")
    assert "verifier" not in res.doc
    assert any("no verifier evidence" in w for w in res.warnings)


def test_explicit_verifier_is_carried_through():
    verifier = {"raw_output": "1 check", "checks": [
        {"check_id": "c1", "status": "passed", "source": "native_structured"}]}
    res = convert(FULL, task_id="t", verifier=verifier)
    assert res.doc["verifier"] == verifier
    assert res.doc["capture_completeness"] == "complete"


def test_task_id_defaulted_from_trace_id_when_absent():
    res = convert(FULL)
    assert res.doc["run"]["task_id"].startswith("otel-trace-")
    assert any("task_id derived from the trace id" in w for w in res.warnings)


def test_run_id_derived_from_trace_id_and_stable():
    res1 = convert(FULL, task_id="t")
    res2 = convert(FULL, task_id="t")
    assert res1.doc["run"]["logical_run_id"] == res2.doc["run"]["logical_run_id"]


def test_explicit_run_id_overrides_default():
    res = convert(FULL, task_id="t", run_id="my-run-id")
    assert res.doc["run"]["logical_run_id"] == "my-run-id"


def test_rejects_non_otlp_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"not_otlp": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="not an OTLP JSON export"):
        convert(bad)


def test_rejects_empty_span_list(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text('{"resourceSpans": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="no spans"):
        convert(empty)


def test_adapter_object_matches_convert_function():
    a = OTEL_ADAPTER.convert(FULL, task_id="t")
    b = convert(FULL, task_id="t")
    assert a.doc["run"]["task_id"] == b.doc["run"]["task_id"]
    assert len(a.doc["steps"]) == len(b.doc["steps"])
