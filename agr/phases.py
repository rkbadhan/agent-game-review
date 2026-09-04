"""Deterministic phase segmentation (Stage C1, spec §8).

Groups the derived event timeline into contiguous phases by *structural role*:

- ``intake``     — the task hand-off;
- ``planning``   — model reasoning before any tool is used;
- ``execution``  — tool calls, results, and observations doing the work; and
- ``submission`` — the final submission and run wrap-up.

Execution is split at each ``retry`` / ``strategy_change`` boundary so distinct
attempts become distinct phases. Segmentation is deterministic and always runs
(§8) — it is pure structure. Giving a phase a *semantic* name ("Installing the
engine", "Analysing the position") is model-driven interpretation (Stage C2 /
Milestone 4) and is deliberately not done here; labels stay structural.
"""

from __future__ import annotations

from . import version
from .schema import DerivedEvent, Phase

_INTAKE = {"task_received"}
# Terminal events. ``final_submission`` is only ever emitted for a directly
# observed submission (adapter contract since harbor-adapter-0.3/0.4); the
# run_* kinds carry harness-side termination semantics.
_SUBMISSION = {
    "final_submission", "run_finished", "verifier_check",
    "run_completed", "run_timed_out", "run_failed",
}
_EXECUTION = {
    "tool_call", "tool_result", "environment_observation", "artifact_observation",
    "process_started", "process_observed", "error_observed", "retry", "strategy_change",
}
_BOUNDARY = {"retry", "strategy_change"}

_LABELS = {
    "intake": "Task intake",
    "planning": "Planning",
    "execution": "Execution",
    "submission": "Submission",
}


def _role(event: DerivedEvent, seen_tool: bool) -> str:
    t = event.event_type
    if t in _INTAKE:
        return "intake"
    if t in _SUBMISSION:
        return "submission"
    if t in _EXECUTION:
        return "execution"
    # model_output / plan_declared / context_compaction: planning until work starts.
    return "execution" if seen_tool else "planning"


def segment_phases(events: list[DerivedEvent], run_id: str, capture_id: str) -> list[Phase]:
    """Segment events into structural phases and stamp each event's ``phase_id``."""
    phases: list[Phase] = []
    current: Phase | None = None
    seen_tool = False
    attempt = 1

    for event in events:
        role = _role(event, seen_tool)
        boundary = (
            role == "execution"
            and event.event_type in _BOUNDARY
            and current is not None
            and current.kind == "execution"
            and current.event_ids
        )
        if boundary:
            attempt += 1
        if current is None or current.kind != role or boundary:
            att = attempt if role == "execution" else None
            label = _LABELS[role]
            if role == "execution" and att and att > 1:
                label = f"{label} · attempt {att}"
            current = Phase(
                phase_id=f"ph_{len(phases) + 1:02d}",
                run_id=run_id,
                source_capture_id=capture_id,
                kind=role,
                label=label,
                event_ids=[],
                attempt=att,
                derivation_version=version.PHASE_SEGMENTATION_VERSION,
            )
            phases.append(current)
        current.event_ids.append(event.event_id)
        event.phase_id = current.phase_id
        if event.event_type in ("tool_call", "tool_result"):
            seen_tool = True

    return phases
