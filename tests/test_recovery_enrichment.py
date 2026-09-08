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
    # AGR-05: the window is strictly AFTER the failure result (evt_2) through
    # the resolution (evt_6) — evt_2's own 100+20 is the initiating attempt's
    # cost, kept separate; only the resolving result's 50+10 is IN the window.
    assert ep.episode_window_tokens == 50 + 10
    assert ep.initiating_attempt_tokens == 100 + 20
    assert ep.usage_completeness == "complete"
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
    assert eps[0].episode_window_tokens == 0
    assert eps[0].initiating_attempt_tokens == 0


# --- AGR-05: episode usage from its defined event interval -------------------


def test_model_output_cost_within_the_window_is_counted():
    """A turn's usage/cost can land on a model_output step (every adapter's
    turn-fanout can produce this shape) — the OLD `evidence` list excluded
    model_output entirely, so a resolving turn's own reasoning/message cost
    was silently dropped from the total."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "model_output", content="[thinking] let me check the file",
            cost={"usage": {"input_tokens": 200, "output_tokens": 40}}),
        _ev("evt_4", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_5", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].episode_window_tokens == 200 + 40


def test_unrecovered_episode_window_extends_to_the_observed_terminal_event():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "model_output", content="giving up",
            cost={"usage": {"input_tokens": 30, "output_tokens": 5}}),
        _ev("evt_4", "run_completed"),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].episode_window_tokens == 30 + 5
    assert eps[0].usage_completeness == "complete"


def test_incomplete_capture_marks_the_window_partial():
    """The capture ends mid-episode: no resolution, and no terminal event was
    ever observed either — the window stops at capture end, honestly marked
    partial rather than silently treated as the whole story."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "model_output", content="still working",
            cost={"usage": {"input_tokens": 15, "output_tokens": 3}}),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].episode_window_tokens == 15 + 3
    assert eps[0].usage_completeness == "partial"


def test_plausible_resolution_window_ends_at_the_plausible_match_not_later():
    """A later, unrelated change scanned only while still looking for a
    (never-found) strict match must not inflate the window a plausible
    resolution earlier in the trace is credited with."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/ -k fast"),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed",
            cost={"usage": {"input_tokens": 10, "output_tokens": 2}}, exit_code=0),
        # Scanning continues after the plausible match, still looking for a
        # strict rerun that never comes — this later activity must not count.
        _ev("evt_5", "model_output", content="doing something else",
            cost={"usage": {"input_tokens": 999, "output_tokens": 999}}),
        _ev("evt_6", "tool_call", tool="shell", content="ls"),
        _ev("evt_7", "tool_result", tool="shell", content="file.txt", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == PLAUSIBLE_RECOVERY
    assert eps[0].episode_window_tokens == 10 + 2


def test_no_cost_anywhere_in_the_window_is_unavailable_not_a_measured_zero():
    """A window where NOTHING carried a cost record (an agent whose adapter
    never reports usage — e.g. terminus-2) must read as unavailable, never
    as a confidently-measured zero."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].episode_window_tokens == 0
    assert eps[0].usage_completeness == "unavailable"


def test_measured_zero_cost_is_complete_not_unavailable():
    """A window where cost WAS recorded and genuinely summed to zero (e.g. a
    cache-only turn) is a real measurement, not "unavailable"."""
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/",
            cost={"usage": {"input_tokens": 0, "output_tokens": 0}}),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].episode_window_tokens == 0
    assert eps[0].usage_completeness == "complete"


def test_initiating_attempt_tokens_found_on_the_turns_leading_model_output():
    """AGR-05/06 (PR #56 review, confirmed): a turn's cost is attached to
    only the FIRST of its fanned-out steps — for a turn that opens with
    reasoning/message text, that is a model_output step BEFORE the tool_call,
    not the call itself. Reading only the failing call's own cost silently
    lost this (common) shape's real cost entirely."""
    events = [
        _ev("evt_1", "model_output", content="[thinking] trying pytest",
            cost={"usage": {"input_tokens": 300, "output_tokens": 40}}),
        _ev("evt_2", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_3", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_4", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_5", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].initiating_attempt_tokens == 300 + 40
    # The leading model_output belongs to the FAILING turn, strictly before
    # the window (idx+1) — it must not also leak into episode_window_tokens.
    assert eps[0].episode_window_tokens == 0


def test_initiating_attempt_tokens_stop_at_the_previous_turns_boundary():
    """The backward walk must not cross into an EARLIER, unrelated turn's own
    model_output/tool_call run."""
    events = [
        _ev("evt_1", "model_output", content="[thinking] unrelated earlier turn",
            cost={"usage": {"input_tokens": 999, "output_tokens": 999}}),
        _ev("evt_2", "tool_result", content="earlier result", tool="shell"),
        _ev("evt_3", "model_output", content="[thinking] trying pytest",
            cost={"usage": {"input_tokens": 10, "output_tokens": 5}}),
        _ev("evt_4", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_5", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_6", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_7", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].initiating_attempt_tokens == 10 + 5


def test_initiating_attempt_tokens_come_from_the_failing_call_and_result():
    events = [
        _ev("evt_1", "tool_call", tool="shell", content="pytest tests/",
            cost={"usage": {"input_tokens": 500, "output_tokens": 60}}),
        _ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        _ev("evt_3", "tool_call", tool="shell", content="pytest tests/"),
        _ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].initiating_attempt_tokens == 500 + 60
    assert eps[0].episode_window_tokens == 0
