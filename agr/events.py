"""Derived event timeline (Stage A, spec §6.3, §8.1).

Normalises ATIF steps into analysis events. Every derived event records the
source step(s) it came from so the Milestone 1 traceability requirement holds:
"every derived event points back to source steps" (spec §20).
"""

from __future__ import annotations

from typing import Any

from . import version
from .schema import EVENT_TYPES, DerivedEvent, RunSource

# ATIF step "kind" -> core event vocabulary. Unknown kinds are surfaced as an
# explicit error rather than silently dropped.
KIND_TO_EVENT = {
    "task_received": "task_received",
    "model_output": "model_output",
    "plan_declared": "plan_declared",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "environment_observation": "environment_observation",
    "artifact_observation": "artifact_observation",
    "process_started": "process_started",
    "process_observed": "process_observed",
    "error_observed": "error_observed",
    "retry": "retry",
    "strategy_change": "strategy_change",
    "context_compaction": "context_compaction",
    "final_submission": "final_submission",
    "run_completed": "run_completed",
    "run_timed_out": "run_timed_out",
    "run_failed": "run_failed",
    "run_finished": "run_finished",
}

# Keys copied verbatim from an ATIF step into the derived event payload.
# ``tool_use_id`` (R2): the call↔result link, so downstream pairing helpers
# match results to the call they answer instead of guessing by adjacency.
# ``tool_input`` (R2): the COMPLETE structured tool input the source recorded —
# display excerpts stay separate; the evidence retains the full content.
# ``status``: tool-result status (ok|error) where the source records a flag
# rather than an OS exit code.
_PAYLOAD_KEYS = ("content", "data", "path", "artifact_path", "tool", "exit_code", "summary", "provenance", "termination_reason", "tool_use_id", "tool_input", "status", "tool_use_result", "compactMetadata", "compaction_summary", "heuristic_status", "heuristic_status_source", "permission_denied", "timestamp")


class EventDerivationError(ValueError):
    pass


def derive_events(doc: dict, run_source: RunSource) -> list[DerivedEvent]:
    events: list[DerivedEvent] = []
    prev_id: str | None = None
    for i, step in enumerate(doc["steps"], start=1):
        kind = step.get("kind")
        event_type = KIND_TO_EVENT.get(kind)
        if event_type is None:
            raise EventDerivationError(f"unmapped ATIF step kind {kind!r} at step {step.get('step_id')}")
        if event_type not in EVENT_TYPES:
            raise EventDerivationError(f"derived unknown event_type {event_type!r}")

        payload: dict[str, Any] = {k: step[k] for k in _PAYLOAD_KEYS if k in step}
        event = DerivedEvent(
            event_id=f"evt_{i:03d}",
            run_id=run_source.run_id,
            source_capture_id=run_source.source_capture_id,
            sequence=i,
            source_step_ids=[step["step_id"]],
            event_type=event_type,
            actor=step.get("actor", "unknown"),
            parent_event_ids=[prev_id] if prev_id else [],
            phase_id=None,
            payload=payload,
            cost=step.get("cost", {}),
            derivation_version=version.DERIVATION_VERSION,
        )
        events.append(event)
        prev_id = event.event_id
    return events
