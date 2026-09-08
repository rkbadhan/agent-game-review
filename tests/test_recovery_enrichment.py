"""Recovery episode enrichment (item 29, 2026-09-08): tool, error_signature,
turns_to_resolve, tokens, wall_ms, nth_occurrence_in_run, resolved_by.

Every field is a deterministic summary of records classify_recoveries()
already computes — these tests pin exactly what each one means.
"""

from __future__ import annotations

from agr.recovery import GOOD_RECOVERY, PLAUSIBLE_RECOVERY, UNRECOVERED, classify_recoveries
from agr.schema import DerivedEvent


def _ev(eid, etype, tool=None, content="", ts=None, cost=None, tool_use_id=None, exit_code=None):
    payload = {}
    if tool:
        payload["tool"] = tool
    if content:
        payload["content"] = content
    if ts:
        payload["timestamp"] = ts
    if tool_use_id:
        payload["tool_use_id"] = tool_use_id
    if exit_code is not None:
        payload["exit_code"] = exit_code
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=int(eid.split("_")[1]),
        source_step_ids=[eid], event_type=etype, actor="main_agent", payload=payload,
        cost=cost or {},
    )


def test_exact_rerun_recovery_is_fully_enriched():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/",
            ts="2026-09-08T10:00:00Z"),
        _ev("evt_2", "tool_result", tool="shell", content="AssertionError: 5 != 4",
            ts="2026-09-08T10:00:01Z", cost={"usage": {"input_tokens": 100, "output_tokens": 20}}, exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="cat calc.py",
            ts="2026-09-08T10:00:05Z"),
        _ev("evt_4", "tool_result", tool="shell", content="def add(a, b): return a + b + 1",
            ts="2026-09-08T10:00:06Z", exit_code=0),
        _ev("evt_5", "tool_call", tool="shell", content="pytest tests/",
            ts="2026-09-08T10:00:10Z"),
        _ev("evt_6", "tool_result", tool="shell", content="3 passed",
            ts="2026-09-08T10:00:12Z", cost={"usage": {"input_tokens": 50, "output_tokens": 10}}, exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    ep = eps[0]
    assert ep.tool == "shell"
    assert ep.error_signature == "AssertionError: <N> != <N>"
    # Two tool_calls happened between the failure and the resolving retry
    # (cat calc.py, then the exact pytest rerun).
    assert ep.turns_to_resolve == 2
    assert ep.tokens == 100 + 20 + 50 + 10
    assert ep.wall_ms == 11000  # 10:00:01 -> 10:00:12
    assert ep.resolved_by == "shell"


def test_unrecovered_failure_has_no_resolution_derived_fields():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="AssertionError: 1 != 2", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    ep = eps[0]
    assert ep.classification == UNRECOVERED
    assert ep.turns_to_resolve is None
    assert ep.wall_ms is None
    assert ep.resolved_by is None
    assert ep.tool == "shell"
    assert ep.error_signature == "AssertionError: <N> != <N>"


def test_nth_occurrence_in_run_counts_per_signature_in_order():
    """Two failures with the SAME normalised signature in one run: the
    second is nth_occurrence_in_run == 2, a distinct one stays at 1."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/a.py"),
        _ev("evt_2", "tool_result", tool="shell", content="AssertionError: 5 != 4", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/b.py"),
        _ev("evt_4", "tool_result", tool="shell", content="AssertionError: 7 != 8", exit_code=1),
        _ev("evt_5", "tool_call", tool="shell", content="curl https://api.example.com"),
        _ev("evt_6", "tool_result", tool="shell", content="connection refused", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 3
    # The first two share a normalised signature ("AssertionError: <N> != <N>").
    assert eps[0].error_signature == eps[1].error_signature
    assert eps[0].nth_occurrence_in_run == 1
    assert eps[1].nth_occurrence_in_run == 2
    # The third is a genuinely different signature — resets to 1.
    assert eps[2].error_signature != eps[0].error_signature
    assert eps[2].nth_occurrence_in_run == 1


def test_plausible_recovery_still_gets_resolved_by_and_turns():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/ -k fast"),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    ep = eps[0]
    assert ep.classification == PLAUSIBLE_RECOVERY
    assert ep.resolved_by == "shell"
    assert ep.turns_to_resolve == 1


def test_no_timestamps_means_wall_ms_is_none_not_zero():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].wall_ms is None


def test_tokens_default_to_zero_when_no_cost_anywhere():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].tokens == 0
