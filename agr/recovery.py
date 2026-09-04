"""Recovery state machine (spec §8.6).

A successful action after a failure is not automatically good recovery.
``good_recovery`` requires a qualifying failure, a meaningful strategy or
action change, new progress, and eventual success. A success reached by
repeating the *same* action unchanged is recorded as
``retry_succeeded_without_strategy_change`` — never promoted to good recovery.
"""

from __future__ import annotations

from . import version
from ._util import action_signature, is_tool_failure, is_tool_success, preceding_call_signature
from .schema import DerivedEvent, RecoveryEpisode

GOOD_RECOVERY = "good_recovery"
UNCHANGED_RETRY = "retry_succeeded_without_strategy_change"
UNRECOVERED = "unrecovered_failure"

_STOP = {
    "final_submission", "run_finished", "run_completed", "run_timed_out", "run_failed",
}


def classify_recoveries(events: list[DerivedEvent], run_id: str, capture_id: str) -> list[RecoveryEpisode]:
    episodes: list[RecoveryEpisode] = []
    n = len(events)
    consumed: set[int] = set()

    for idx, ev in enumerate(events):
        if idx in consumed or not is_tool_failure(ev):
            continue

        failed_sig = preceding_call_signature(events, idx)
        strategy_changed = False
        changed_action = False
        resolution: DerivedEvent | None = None
        evidence = [ev.event_id]

        j = idx + 1
        while j < n:
            e2 = events[j]
            if e2.event_type == "strategy_change":
                strategy_changed = True
                evidence.append(e2.event_id)
            elif e2.event_type == "tool_call":
                if failed_sig is not None and action_signature(e2) != failed_sig:
                    changed_action = True
                evidence.append(e2.event_id)
            elif is_tool_failure(e2):
                consumed.add(j)  # a retry of the same episode
                evidence.append(e2.event_id)
            elif is_tool_success(e2):
                resolution = e2
                evidence.append(e2.event_id)
                break
            elif e2.event_type in _STOP:
                break
            j += 1

        if resolution is not None:
            classification = GOOD_RECOVERY if (strategy_changed or changed_action) else UNCHANGED_RETRY
        else:
            classification = UNRECOVERED

        episodes.append(
            RecoveryEpisode(
                episode_id=f"rec_{ev.event_id}",
                run_id=run_id,
                source_capture_id=capture_id,
                classification=classification,
                failure_event_id=ev.event_id,
                resolution_event_id=resolution.event_id if resolution else None,
                strategy_changed=strategy_changed,
                changed_action=changed_action,
                evidence_event_ids=evidence,
            )
        )
    return episodes
