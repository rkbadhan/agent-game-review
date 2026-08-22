"""Opportunity detection (spec §3.2, §6.7).

An opportunity is an interval in which the agent could realistically
demonstrate a behaviour. Opportunities are the denominator for behavioural
analytics and the required anchor for omission findings.
"""

from __future__ import annotations

from ._util import is_tool_failure
from .schema import DerivedEvent, Opportunity


def _next_stop(events: list[DerivedEvent], start_idx: int) -> DerivedEvent:
    """First strategy_change / tool_result / submission after start, else last event."""
    for j in range(start_idx + 1, len(events)):
        if events[j].event_type in ("strategy_change", "tool_result", "final_submission", "run_finished"):
            return events[j]
    return events[-1]


def detect_opportunities(events: list[DerivedEvent], run_id: str, capture_id: str) -> list[Opportunity]:
    opps: list[Opportunity] = []

    # Tool-failure -> opportunity to recover.
    for idx, ev in enumerate(events):
        if is_tool_failure(ev):
            end = _next_stop(events, idx)
            opps.append(
                Opportunity(
                    opportunity_id=f"opp_recover_{ev.event_id}",
                    run_id=run_id,
                    source_capture_id=capture_id,
                    ability="tool_error_recovery",
                    trigger="tool_failure",
                    start_event_id=ev.event_id,
                    end_event_id=end.event_id,
                    feasible_actions=["change_strategy", "retry_with_fix"],
                    evidence=[ev.event_id],
                )
            )

    # Artifact present before submission -> opportunity to verify.
    artifact_evt = next((e for e in events if e.event_type == "artifact_observation"), None)
    submission = next((e for e in events if e.event_type == "final_submission"), None)
    if artifact_evt is not None and submission is not None:
        opps.append(
            Opportunity(
                opportunity_id="opp_verify_before_submission",
                run_id=run_id,
                source_capture_id=capture_id,
                ability="verification_discipline",
                trigger="required_artifact_exists",
                start_event_id=artifact_evt.event_id,
                end_event_id=submission.event_id,
                feasible_actions=["inspect_artifact", "run_completeness_check"],
                evidence=[artifact_evt.event_id],
            )
        )
    return opps
