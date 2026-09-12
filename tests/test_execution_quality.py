"""Deterministic execution-quality boundaries and observability contracts."""

from __future__ import annotations

import pytest

from agr.detectors import (
    ContextTokenBloat,
    DetectorContext,
    ExcessLatency,
    RepeatedActionNoNewInfo,
)
from agr.execution_quality import (
    execution_quality_summary,
    generation_token_counts,
    generation_usage_capability,
    generation_wall_ms,
    normalized_generation_usage,
    reconcile_generation_capabilities,
)
from agr.schema import CapabilityProfile, DerivedEvent


def _event(
    event_id: str,
    sequence: int,
    event_type: str,
    *,
    payload: dict | None = None,
    cost: dict | None = None,
) -> DerivedEvent:
    return DerivedEvent(
        event_id=event_id,
        run_id="run-1",
        source_capture_id="capture-1",
        sequence=sequence,
        source_step_ids=[f"step-{sequence}"],
        event_type=event_type,
        actor="main_agent" if event_type != "tool_result" else "tool",
        payload=payload or {},
        cost=cost or {},
    )


def _context(events: list[DerivedEvent], capabilities: dict, doc: dict | None = None):
    profile = CapabilityProfile(
        run_id="run-1", source_capture_id="capture-1", capabilities=capabilities
    )
    return DetectorContext(
        run_id="run-1",
        capture_id="capture-1",
        events=events,
        checks=[],
        doc=doc or {},
        profile=profile,
    )


def _generation(event_id: str, tokens: int, wall_ms: int = 1_000, sequence: int = 1):
    return _event(
        event_id,
        sequence,
        "model_output",
        payload={"start_time": 1_000.0, "end_time": 1_000.0 + wall_ms / 1_000},
        cost={"input_tokens": tokens, "output_tokens": 50, "total_tokens": tokens + 50},
    )


@pytest.mark.parametrize(
    ("tokens", "violations"),
    [(99_999, 0), (100_000, 1), (100_001, 1)],
)
def test_context_token_boundary(tokens, violations):
    events = [_generation("gen-1", tokens)]
    result = ContextTokenBloat().run(
        _context(events, {"generation_usage": "complete"})
    )
    assert result.evaluated is True
    assert sum(len(c.structured_facts) for c in result.candidates) == violations


def test_context_requires_complete_usage_and_aggregates_findings():
    events = [_generation("gen-1", 100_000, sequence=1), _generation("gen-2", 180_000, sequence=2)]
    unavailable = ContextTokenBloat().run(
        _context(events, {"generation_usage": "partial"})
    )
    assert unavailable.evaluated is False
    assert unavailable.unmet_capabilities == ["generation_usage"]

    result = ContextTokenBloat().run(
        _context(events, {"generation_usage": "complete"})
    )
    assert len(result.candidates) == 1
    assert len(result.candidates[0].structured_facts) == 2
    assert result.candidates[0].severity == "high"


@pytest.mark.parametrize(
    ("wall_ms", "violations"),
    [(59_999, 0), (60_000, 1), (60_001, 1)],
)
def test_generation_latency_boundary(wall_ms, violations):
    events = [_generation("gen-1", 10, wall_ms)]
    result = ExcessLatency().run(
        _context(events, {"generation_timestamps": "complete"})
    )
    assert result.evaluated is True
    assert sum(len(c.structured_facts) for c in result.candidates) == violations


def test_invalid_generation_time_is_unavailable_not_zero():
    assert generation_wall_ms(10, 9) is None
    caps = reconcile_generation_capabilities(
        {"generation_timestamps": "complete"},
        [{"kind": "model_output", "start_time": 10, "end_time": 9}],
    )
    assert caps["generation_timestamps"] == "partial"


def test_declared_complete_usage_is_downgraded_when_missing():
    caps = reconcile_generation_capabilities(
        {"generation_usage": "complete"},
        [{"kind": "model_output", "cost": {"output_tokens": 12}}],
    )
    assert caps["generation_usage"] == "partial"


def test_generation_usage_normalizes_nested_cache_and_harbor_shapes():
    assert generation_token_counts({
        "usage": {
            "input_tokens": 70,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 10,
            "output_tokens": 5,
        }
    }) == (100, 5, 105)
    assert generation_token_counts({
        "prompt_tokens": 80, "completion_tokens": 7
    }) == (80, 7, 87)
    assert generation_token_counts({
        "input": 70, "cacheRead": 20, "cacheWrite": 10,
        "output": 5, "totalTokens": 105,
    }) == (100, 5, 105)


def test_marked_tool_only_generation_and_model_retry_are_accounted():
    tool_generation = _event(
        "tool-call-generation", 1, "tool_call",
        payload={"generation_event": True, "generation_id": "generation-1"},
        cost={"usage": {"input_tokens": 120_000, "output_tokens": 10}},
    )
    retry_generation = _event(
        "model-retry", 2, "retry",
        payload={
            "generation_event": True,
            "generation_id": "generation-2",
            "start_time": 100,
            "end_time": 170,
        },
        cost={"prompt_tokens": 200_000, "completion_tokens": 20},
    )
    records = normalized_generation_usage([tool_generation, retry_generation])
    assert [(record.event_id, record.input_tokens) for record in records] == [
        ("tool-call-generation", 120_000), ("model-retry", 200_000)
    ]
    assert records[1].wall_ms == 70_000
    assert generation_usage_capability([
        {"kind": "tool_call", "generation_event": True, "cost": tool_generation.cost},
        {"kind": "retry", "generation_event": True, "cost": retry_generation.cost},
    ]) == "complete"
    context = ContextTokenBloat().run(_context(
        [tool_generation, retry_generation], {"generation_usage": "complete"}
    ))
    latency = ExcessLatency().run(_context(
        [tool_generation, retry_generation], {"generation_timestamps": "complete"}
    ))
    assert len(context.candidates[0].structured_facts) == 2
    assert latency.candidates[0].structured_facts[0]["event_id"] == "model-retry"


def _call(call_id: str, sequence: int, command: str = "status"):
    return _event(
        call_id,
        sequence,
        "tool_call",
        payload={"tool": "shell", "content": command, "tool_use_id": call_id},
    )


def _result(result_id: str, sequence: int, call_id: str, output=...):
    payload = {"tool": "shell", "tool_use_id": call_id}
    if output is not ...:
        payload["content"] = output
    return _event(result_id, sequence, "tool_result", payload=payload)


def _redundancy(events, *, window=20):
    return RepeatedActionNoNewInfo().run(
        _context(
            events,
            {"tool_calls": "complete", "tool_results": "complete"},
            {"execution_quality": {"redundant_work": {"comparison_window": window}}},
        )
    )


def test_redundancy_finds_non_adjacent_repeat_and_retains_results():
    events = [
        _call("call-a", 1),
        _result("result-a", 2, "call-a", {"state": "same"}),
        _call("call-b", 3, "other"),
        _result("result-b", 4, "call-b", "different action"),
        _call("call-c", 5),
        _result("result-c", 6, "call-c", {"state": "same"}),
    ]
    result = _redundancy(events)
    assert len(result.candidates) == 1
    fact = result.candidates[0].structured_facts[0]
    assert fact["events"] == ["call-a", "call-c"]
    assert fact["result_event_ids"] == ["result-a", "result-c"]
    assert fact["calls_between"] == 1


@pytest.mark.parametrize("second_output", [..., None, {"state": "changed"}])
def test_redundancy_requires_two_captured_equivalent_outputs(second_output):
    events = [
        _call("call-a", 1),
        _result("result-a", 2, "call-a", None),
        _call("call-b", 3),
        _result("result-b", 4, "call-b", second_output),
    ]
    result = _redundancy(events)
    if second_output is None:
        # Explicitly captured JSON null is valid evidence and differs from omission.
        assert len(result.candidates) == 1
    else:
        assert result.candidates == []


def test_redundancy_respects_strategy_boundaries_and_window():
    strategy = _event("strategy", 3, "strategy_change", payload={"content": "new plan"})
    separated = [
        _call("call-a", 1), _result("result-a", 2, "call-a", "same"), strategy,
        _call("call-b", 4), _result("result-b", 5, "call-b", "same"),
    ]
    assert _redundancy(separated).candidates == []

    outside_window = [
        _call("call-a", 1), _result("result-a", 2, "call-a", "same"),
        _call("other", 3, "other"), _result("other-result", 4, "other", "x"),
        _call("call-b", 5), _result("result-b", 6, "call-b", "same"),
    ]
    assert _redundancy(outside_window, window=1).candidates == []


def test_redundancy_never_infers_unchanged_state_from_mutation_acknowledgements():
    events = [
        _call("call-a", 1, "write /out.txt"),
        _result("result-a", 2, "call-a", "ok"),
        _call("call-b", 3, "write /out.txt"),
        _result("result-b", 4, "call-b", "ok"),
    ]
    for event in (events[0], events[2]):
        event.payload["tool"] = "Write"
    assert _redundancy(events).candidates == []


def test_usage_identity_and_independent_summary_states():
    events = [_generation("gen-1", 100_001, 60_001)]
    records = normalized_generation_usage(events)
    assert records[0].identity == ("run-1", "capture-1", "gen-1")

    context = ContextTokenBloat().run(
        _context(events, {"generation_usage": "complete"})
    )
    latency = ExcessLatency().run(
        _context(events, {"generation_timestamps": "partial"})
    )
    redundant = _redundancy([])
    summary = execution_quality_summary([context, latency, redundant], events)
    assert summary["efficiency"]["context_bloat"]["status"] == "issue"
    assert summary["efficiency"]["latency"]["status"] == "unevaluated"
    assert summary["efficiency"]["redundant_work"]["status"] == "healthy"
    assert summary["efficiency"]["context_bloat"]["peak_input_tokens"] == 100_001
    assert summary["efficiency"]["context_bloat"]["median_input_tokens"] == 100_001


def test_pipeline_persists_execution_summary_and_evidence_slice(
    tmp_path, load_fixture
):
    from agr.pipeline import analyze
    from agr.read import get_review
    from agr.store import Store

    doc = load_fixture("clean_pass.atif.json")
    doc["capabilities"].update({
        "generation_usage": "complete",
        "generation_timestamps": "complete",
    })
    generation = next(step for step in doc["steps"] if step["kind"] == "model_output")
    generation.update({
        "start_time": "2026-07-19T09:00:01Z",
        "end_time": "2026-07-19T09:01:02Z",
        "cost": {"input_tokens": 100_001, "output_tokens": 25, "total_tokens": 100_026},
    })
    store = Store(str(tmp_path / "store"))
    analysis = analyze(doc, store)

    assert analysis.outcome["status"] == "PASSED"
    assert analysis.execution_quality["efficiency"]["context_bloat"]["status"] == "issue"
    assert analysis.execution_quality["efficiency"]["latency"]["status"] == "issue"
    execution_slices = [
        item for item in analysis.evidence_slices
        if item.check_id.startswith("cand_context_token_bloat")
    ]
    assert execution_slices and "evt_002" in execution_slices[0].event_ids
    review = get_review(store, analysis.run_source.run_id)
    assert review["execution_quality"] == analysis.execution_quality


def test_forensic_view_keeps_generation_metadata_when_message_text_is_unavailable(
    tmp_path, load_fixture
):
    from agr.pipeline import analyze
    from agr.read import get_forensic
    from agr.store import Store

    doc = load_fixture("clean_pass.atif.json")
    doc["run"]["logical_run_id"] += "__metadata-only"
    doc["capabilities"].update({
        "messages": "unavailable",
        "generation_usage": "complete",
        "generation_timestamps": "complete",
    })
    generation = next(step for step in doc["steps"] if step["kind"] == "model_output")
    generation.pop("content", None)
    generation.update({
        "start_time": 100,
        "end_time": 170,
        "cost": {"usage": {"input_tokens": 99_000, "cache_read_input_tokens": 2_000}},
    })
    store = Store(str(tmp_path / "store"))
    analysis = analyze(doc, store)
    forensic = get_forensic(store, analysis.run_source.run_id)
    step = next(item for item in forensic["steps"] if item["event_type"] == "model_output")
    assert step["availability"]["state"] == "unavailable"
    assert step["content"]["input_tokens"] == 101_000
    assert step["content"]["wall_ms"] == 70_000


def test_reviewer_recomputes_execution_facts_and_rejects_tampering():
    from agr.reviewer import ReviewerContext, validate_facts
    from agr.schema import Candidate

    event = _generation("gen-1", 120_000, 65_000)
    candidate = Candidate(
        candidate_id="candidate",
        run_id="run-1",
        source_capture_id="capture-1",
        detector="context_token_bloat",
        kind="behaviour",
        anchor_event_ids=["gen-1"],
        structured_facts=[{
            "type": "token_usage",
            "event_id": "gen-1",
            "input_tokens": 120_000,
            "threshold": 100_000,
        }, {
            "type": "generation_latency",
            "event_id": "gen-1",
            "wall_ms": 65_000,
            "threshold_ms": 60_000,
        }],
    )
    ctx = ReviewerContext(
        run_id="run-1", source_capture_id="capture-1", candidates=[candidate],
        slices=[], checks=[], events=[event],
    )
    assert [fact["validation"] for fact in validate_facts(candidate, ctx)] == [
        "passed", "passed"
    ]
    candidate.structured_facts[0]["input_tokens"] = 999_999
    assert validate_facts(candidate, ctx)[0]["validation"] == "failed"


def test_fleet_matrix_excludes_unevaluated_runs_from_dimension_denominator(
    tmp_path, load_fixture
):
    import copy

    from agr.fleet import fleet_execution_quality
    from agr.pipeline import analyze
    from agr.store import Store

    store = Store(str(tmp_path / "store"))
    for suffix, tokens, usage_level in (
        ("issue", 100_001, "complete"),
        ("healthy", 50_000, "complete"),
        ("unknown", 0, "unavailable"),
    ):
        doc = copy.deepcopy(load_fixture("clean_pass.atif.json"))
        doc["run"]["logical_run_id"] += "__" + suffix
        doc["capabilities"]["generation_usage"] = usage_level
        generation = next(step for step in doc["steps"] if step["kind"] == "model_output")
        if usage_level == "complete":
            generation["cost"] = {"input_tokens": tokens}
        analyze(doc, store)

    metric = fleet_execution_quality(store)["by_outcome"]["pass"]["dimensions"]["context_bloat"]
    assert metric == {
        "eligible_runs": 2,
        "affected_runs": 1,
        "affected_percent": 50.0,
    }


def test_version_metrics_do_not_turn_unevaluated_execution_into_zero():
    from agr.versions import _cost_metrics

    run = {
        "run": {},
        "execution_quality": {
            "efficiency": {
                "context_bloat": {"evaluated": False, "median_input_tokens": 0},
                "latency": {"evaluated": False, "p95_generation_ms": 0},
                "redundant_work": {"evaluated": False, "violations": 0},
            }
        },
    }
    rows = _cost_metrics([{"pair_id": "pair-1", "_baseline": run, "_candidate": run}])
    execution_rows = {row["metric_id"]: row for row in rows}
    for metric_id in ("median_input_tokens", "p95_generation_ms", "redundant_actions"):
        assert execution_rows[metric_id]["captured"] is False
        assert execution_rows[metric_id]["change"] == "not_captured"
