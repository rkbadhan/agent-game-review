"""Shared span-tree flattener tests (docs/span-adapters.md).

These pin the flattener's contract against small, hand-built normalized span
trees — no source format involved yet, that's what test_ingest_otel.py /
test_ingest_langfuse.py / test_ingest_langsmith.py cover. Every one of these
trees is a toy agent run: a root agent span, a model call, one or two tool
calls, sometimes a subagent, sometimes a deliberately flaky tool that is
retried.
"""

from __future__ import annotations

import pytest

from agr.span_tree import NormalizedSpan, flatten_span_tree


def _span(span_id, parent_id, start, kind, **kw) -> NormalizedSpan:
    return NormalizedSpan(span_id=span_id, parent_id=parent_id, start_time=start, kind=kind, **kw)


def test_empty_span_list_raises():
    with pytest.raises(ValueError, match="no spans"):
        flatten_span_tree([])


def test_normalized_span_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown kind"):
        NormalizedSpan(span_id="a", parent_id=None, start_time=0, kind="bogus")


def test_normalized_span_rejects_unknown_status():
    with pytest.raises(ValueError, match="unknown status"):
        NormalizedSpan(span_id="a", parent_id=None, start_time=0, kind="agent", status="bogus")


def _basic_run(*, root_status="ok", root_output_captured=True):
    """root(agent) -> model -> tool(ls, ok) -> model -> tool(write, ok)."""
    return [
        _span("root", None, 0, "agent", name="toy-agent", status=root_status,
              input_captured=True, inputs="list the files then write a summary",
              output_captured=root_output_captured, outputs="done: wrote summary.txt"),
        _span("m1", "root", 1, "model", name="gpt-x", status="ok",
              output_captured=True, outputs="I'll list files first.",
              usage={"input_tokens": 10, "output_tokens": 5}),
        _span("t1", "root", 2, "tool", name="list_files", status="ok",
              input_captured=True, inputs={"path": "."},
              output_captured=True, outputs="a.txt\nb.txt"),
        _span("m2", "root", 3, "model", name="gpt-x", status="ok",
              output_captured=True, outputs="Now I'll write the summary."),
        _span("t2", "root", 4, "tool", name="write_file", status="ok",
              input_captured=True, inputs={"path": "summary.txt", "content": "..."},
              output_captured=True, outputs="ok"),
    ]


def test_orders_by_start_time_and_maps_kinds():
    result = flatten_span_tree(_basic_run())
    kinds = [s["kind"] for s in result.steps]
    assert kinds == [
        "task_received", "model_output", "tool_call", "tool_result",
        "model_output", "tool_call", "tool_result", "run_completed",
    ]


def test_root_input_becomes_task_received_content():
    result = flatten_span_tree(_basic_run())
    task_step = result.steps[0]
    assert task_step["kind"] == "task_received"
    assert "list the files" in task_step["content"]
    assert result.meta["instruction"] == "list the files then write a summary"


def test_root_output_becomes_terminal_content_and_run_completed():
    result = flatten_span_tree(_basic_run())
    terminal = result.steps[-1]
    assert terminal["kind"] == "run_completed"
    assert "wrote summary.txt" in terminal["content"]
    assert terminal["provenance"] == "synthetic"


def test_root_error_status_selects_run_failed():
    spans = _basic_run(root_status="error")
    # give the root span an error type so it's unambiguous
    spans[0].error_type = "ValueError"
    result = flatten_span_tree(spans)
    assert result.steps[-1]["kind"] == "run_failed"


def test_root_timeout_error_type_selects_run_timed_out():
    spans = _basic_run(root_status="error")
    spans[0].error_type = "AgentTimeoutError"
    result = flatten_span_tree(spans)
    assert result.steps[-1]["kind"] == "run_timed_out"


def test_never_synthesizes_final_submission():
    """Design doc: never a synthesised final_submission unless a submission is
    directly observed — span trees carry no such signal, so it must never
    appear."""
    result = flatten_span_tree(_basic_run())
    assert "final_submission" not in [s["kind"] for s in result.steps]


def test_no_agent_activity_is_protocol_failure_not_completion():
    spans = [_span("root", None, 0, "agent", name="toy-agent", status="ok",
                    input_captured=True, inputs="do something")]
    result = flatten_span_tree(spans)
    kinds = [s["kind"] for s in result.steps]
    assert kinds == ["task_received", "run_failed"]
    assert result.steps[-1]["termination_reason"] == "agent_protocol_failure"
    assert not result.meta["saw_agent_activity"]


def test_tool_call_and_result_carry_tool_name_and_status():
    result = flatten_span_tree(_basic_run())
    calls = [s for s in result.steps if s["kind"] == "tool_call"]
    results = [s for s in result.steps if s["kind"] == "tool_result"]
    assert [c["tool"] for c in calls] == ["list_files", "write_file"]
    assert [r["tool"] for r in results] == ["list_files", "write_file"]
    assert all(r["status"] == "ok" for r in results)


def test_model_output_carries_usage_as_cost():
    result = flatten_span_tree(_basic_run())
    model_steps = [s for s in result.steps if s["kind"] == "model_output"]
    assert model_steps[0]["cost"]["input_tokens"] == 10
    assert model_steps[0]["cost"]["output_tokens"] == 5


# --- the content catch: uncaptured tool arguments/results ---------------------

def test_uncaptured_tool_content_declares_partial_and_unknown_status():
    spans = [
        _span("root", None, 0, "agent", name="toy-agent", status="ok",
              input_captured=True, inputs="do the thing",
              output_captured=True, outputs="done"),
        _span("t1", "root", 1, "tool", name="run_query", status="ok",
              input_captured=False, output_captured=False),
    ]
    result = flatten_span_tree(spans)
    calls = [s for s in result.steps if s["kind"] == "tool_call"]
    results = [s for s in result.steps if s["kind"] == "tool_result"]
    assert calls[0]["tool"] == "run_query"
    assert "content" not in calls[0]  # no arguments captured
    # Design doc rule: clean status + no captured output => unknown, not passed.
    assert results[0]["status"] == "unknown"
    assert "content" not in results[0]
    assert result.capabilities["tool_calls"] == "unavailable"
    assert result.capabilities["tool_results"] == "unavailable"
    assert any("tool_calls declared" in w for w in result.warnings)
    assert any("tool_results declared" in w for w in result.warnings)


def test_mixed_capture_declares_partial():
    spans = [
        _span("root", None, 0, "agent", name="toy-agent", status="ok",
              input_captured=True, inputs="x", output_captured=True, outputs="y"),
        _span("t1", "root", 1, "tool", name="a", status="ok",
              input_captured=True, inputs={"q": "1"}, output_captured=True, outputs="ok"),
        _span("t2", "root", 2, "tool", name="b", status="ok",
              input_captured=False, output_captured=False),
    ]
    result = flatten_span_tree(spans)
    assert result.capabilities["tool_calls"] == "partial"
    assert result.capabilities["tool_results"] == "partial"


def test_no_tool_spans_leaves_tool_capabilities_undeclared():
    spans = [
        _span("root", None, 0, "agent", name="toy-agent", status="ok",
              input_captured=True, inputs="x", output_captured=True, outputs="y"),
        _span("m1", "root", 1, "model", status="ok", output_captured=True, outputs="hi"),
    ]
    result = flatten_span_tree(spans)
    assert "tool_calls" not in result.capabilities
    assert "tool_results" not in result.capabilities


def test_explicit_error_message_kept_even_without_captured_output():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("t1", "root", 1, "tool", name="flaky", status="error",
              error_message="connection refused", input_captured=False, output_captured=False),
    ]
    result = flatten_span_tree(spans)
    results = [s for s in result.steps if s["kind"] == "tool_result"]
    assert results[0]["status"] == "error"
    assert results[0]["content"] == "connection refused"


# --- subagent grouping ---------------------------------------------------------

def test_nested_agent_span_is_a_subagent_not_a_new_main_loop():
    spans = [
        _span("root", None, 0, "agent", name="orchestrator", status="ok",
              input_captured=True, inputs="do the big task",
              output_captured=True, outputs="done"),
        _span("t_top", "root", 1, "tool", name="delegate", status="ok",
              input_captured=True, inputs={}, output_captured=True, outputs="ok"),
        _span("sub", "root", 2, "agent", name="researcher", status="ok"),
        _span("t_sub", "sub", 3, "tool", name="search", status="ok",
              input_captured=True, inputs={"q": "x"}, output_captured=True, outputs="results"),
        _span("m_sub", "sub", 4, "model", status="ok", output_captured=True, outputs="found it"),
    ]
    result = flatten_span_tree(spans)
    by_tool = {s["tool"]: s for s in result.steps if s.get("kind") == "tool_call"}
    assert by_tool["delegate"]["actor"] == "main_agent"
    assert by_tool["search"]["actor"] == "subagent:researcher"
    sub_model = [s for s in result.steps if s["kind"] == "model_output" and s["actor"] != "main_agent"]
    assert sub_model and sub_model[0]["actor"] == "subagent:researcher"
    assert "researcher" in result.meta["subagent_names"]
    # The subagent's own steps are never dropped (unlike the Claude adapter's
    # isSidechain handling) — they're present, just correctly attributed.
    assert any(s.get("tool") == "search" for s in result.steps)


# --- framework retries ----------------------------------------------------------

def test_repeated_failed_sibling_tool_span_becomes_a_retry_step():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("t1", "root", 1, "tool", name="flaky_tool", status="error",
              input_captured=True, inputs={"q": "1"},
              output_captured=True, outputs="500 Internal Server Error"),
        _span("t2", "root", 2, "tool", name="flaky_tool", status="ok",
              input_captured=True, inputs={"q": "1"},
              output_captured=True, outputs="ok now"),
    ]
    result = flatten_span_tree(spans)
    kinds = [s["kind"] for s in result.steps]
    # First attempt is a normal call/result pair (it's the FIRST occurrence);
    # the second, which repeats the same failed operation, becomes one retry
    # step instead of a second call/result pair.
    assert kinds == ["task_received", "tool_call", "tool_result", "retry", "run_completed"]
    retry_step = result.steps[3]
    assert retry_step["tool"] == "flaky_tool"
    assert retry_step["status"] == "ok"
    assert retry_step["content"] == "ok now"
    assert result.meta["retry_count"] == 1
    assert any("retry span(s) recognised" in w for w in result.warnings)


def test_model_retry_remains_a_generation_for_usage_and_timing():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("m1", "root", 1, "model", name="gpt-x", status="error",
              output_captured=True, outputs="failed", usage={"input_tokens": 10},
              timestamp="2026-09-12T10:00:00Z", end_timestamp="2026-09-12T10:00:01Z"),
        _span("m2", "root", 2, "model", name="gpt-x", status="ok",
              output_captured=True, outputs="recovered", usage={"input_tokens": 200_000},
              timestamp="2026-09-12T10:01:00Z", end_timestamp="2026-09-12T10:06:00Z"),
    ]
    result = flatten_span_tree(spans)
    retry = next(step for step in result.steps if step["kind"] == "retry")
    assert retry["generation_event"] is True
    assert retry["cost"]["input_tokens"] == 200_000
    assert retry["start_time"] == "2026-09-12T10:01:00Z"
    assert retry["end_time"] == "2026-09-12T10:06:00Z"
    assert result.capabilities["generation_usage"] == "complete"
    assert result.capabilities["generation_timestamps"] == "complete"


def test_deliberate_repeat_call_after_success_is_not_a_retry():
    """The SAME tool called twice, but the first call succeeded — this is the
    agent choosing to call it again, not a framework-retried failure, and
    must never be collapsed into a retry step."""
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("t1", "root", 1, "tool", name="list_files", status="ok",
              input_captured=True, inputs={"path": "a"},
              output_captured=True, outputs="a.txt"),
        _span("t2", "root", 2, "tool", name="list_files", status="ok",
              input_captured=True, inputs={"path": "b"},
              output_captured=True, outputs="b.txt"),
    ]
    result = flatten_span_tree(spans)
    kinds = [s["kind"] for s in result.steps]
    assert "retry" not in kinds
    assert kinds.count("tool_call") == 2
    assert result.meta["retry_count"] == 0


def test_unrelated_span_between_failure_and_repeat_breaks_retry_adjacency():
    """Retry recognition is deliberately narrow: only the IMMEDIATELY prior
    span counts. An unrelated action in between means the later same-named
    call is not classified as a retry of the earlier failure."""
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("t1", "root", 1, "tool", name="flaky_tool", status="error",
              input_captured=True, inputs={}, output_captured=True, outputs="boom"),
        _span("t_other", "root", 2, "tool", name="other_tool", status="ok",
              input_captured=True, inputs={}, output_captured=True, outputs="ok"),
        _span("t2", "root", 3, "tool", name="flaky_tool", status="ok",
              input_captured=True, inputs={}, output_captured=True, outputs="ok now"),
    ]
    result = flatten_span_tree(spans)
    assert result.meta["retry_count"] == 0
    assert [s["kind"] for s in result.steps].count("tool_call") == 3


# --- generic / unmapped grouping spans -----------------------------------------

def test_chain_span_with_content_becomes_environment_observation():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("c1", "root", 1, "chain", name="retrieval-step",
              input_captured=False, output_captured=True, outputs="retrieved 3 docs"),
    ]
    result = flatten_span_tree(spans)
    obs = [s for s in result.steps if s["kind"] == "environment_observation"]
    assert obs and obs[0]["content"] == "retrieved 3 docs"


def test_content_free_chain_span_is_skipped_with_a_warning():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("c1", "root", 1, "chain", name="wrapper"),
    ]
    result = flatten_span_tree(spans)
    assert not any(s["kind"] == "environment_observation" for s in result.steps)
    assert result.meta["unmapped_span_count"] == 1
    assert any("carried no captured input/output" in w for w in result.warnings)


# --- root input not captured ----------------------------------------------------

def test_root_input_not_captured_is_warned_and_empty():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=False,
              output_captured=True, outputs="done"),
        _span("m1", "root", 1, "model", status="ok", output_captured=True, outputs="hi"),
    ]
    result = flatten_span_tree(spans)
    assert result.meta["instruction"] == ""
    assert any("carries no captured input" in w for w in result.warnings)


def test_multiple_roots_warns_and_uses_earliest():
    spans = [
        _span("root2", None, 5, "agent", status="ok", input_captured=True, inputs="second",
              output_captured=True, outputs="done2"),
        _span("root1", None, 0, "agent", status="ok", input_captured=True, inputs="first",
              output_captured=True, outputs="done1"),
        _span("m1", "root1", 1, "model", status="ok", output_captured=True, outputs="hi"),
    ]
    result = flatten_span_tree(spans)
    assert result.meta["root_span_id"] == "root1"
    assert any("additional top-level span" in w for w in result.warnings)


def test_call_order_breaks_ties_for_overlapping_spans():
    spans = [
        _span("root", None, 0, "agent", status="ok", input_captured=True, inputs="x",
              output_captured=True, outputs="done"),
        _span("t_b", "root", 1, "tool", name="second", status="ok", call_order=2,
              input_captured=True, inputs={}, output_captured=True, outputs="ok"),
        _span("t_a", "root", 1, "tool", name="first", status="ok", call_order=1,
              input_captured=True, inputs={}, output_captured=True, outputs="ok"),
    ]
    result = flatten_span_tree(spans)
    calls = [s["tool"] for s in result.steps if s["kind"] == "tool_call"]
    assert calls == ["first", "second"]
