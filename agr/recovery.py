"""Recovery state machine (spec §8.6).

A successful action after a failure is not automatically good recovery.
``good_recovery`` requires a qualifying failure, a meaningful strategy or
action change, new progress, and eventual success. A success reached by
repeating the *same* action unchanged is recorded as
``retry_succeeded_without_strategy_change`` — never promoted to good recovery.

AGR-05: the resolving success must be LINKED to the failed operation. The
episode closes only on a success of the same tool working on the same
objective — same action signature, or (for changed-argument retries) the same
primary command token (e.g. ``pytest …`` after a failed ``pytest …``). An
unrelated successful command (failed ``pytest``, then a successful ``pwd``)
cannot close the failure episode; the episode stays open and is classified
``unrecovered_failure`` unless a linked success or a run-stop event follows.
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


def _primary_token(content: str) -> str:
    """First token of a command-like content string — the operation's objective
    stem (``pytest test_x.py -k f`` → ``pytest``). A mechanical, documented
    approximation for linking a retry to the operation that failed."""
    return content.strip().split(" ", 1)[0] if content else ""


def _links_to_failed_operation(success_sig: tuple | None, failed_sig: tuple | None) -> bool:
    """Whether a successful tool result plausibly resolves the failed operation.

    Both sides are CALL signatures (the resolving success is linked through
    the call that produced it). Linkage is mechanical: same tool, and either
    the identical action signature (an unchanged retry) or the same primary
    token (a changed-argument retry of the same operation). A success on a
    different objective never links — a successful ``pwd`` does not resolve a
    failed ``pytest``.
    """
    if failed_sig is None or success_sig is None:
        return False  # cannot establish the operations — cannot link
    if success_sig[0] != failed_sig[0]:
        return False
    if success_sig[1] == failed_sig[1]:
        return True  # unchanged retry of the same operation
    # Changed-argument retry: same operation stem only.
    return (_primary_token(str(success_sig[1])) != ""
            and _primary_token(str(success_sig[1])) == _primary_token(str(failed_sig[1])))


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
        pending_call_sig: tuple | None = None  # the call the next result answers

        j = idx + 1
        while j < n:
            e2 = events[j]
            if e2.event_type == "strategy_change":
                strategy_changed = True
                evidence.append(e2.event_id)
            elif e2.event_type == "tool_call":
                pending_call_sig = action_signature(e2)
                if failed_sig is not None and pending_call_sig != failed_sig:
                    changed_action = True
                evidence.append(e2.event_id)
            elif is_tool_failure(e2):
                consumed.add(j)  # a retry of the same episode
                evidence.append(e2.event_id)
            elif is_tool_success(e2):
                # AGR-05: only a success LINKED to the failed operation closes
                # the episode — linked via the CALL that produced this result.
                # Unrelated successes are recorded as evidence (the run
                # continued) but resolve nothing.
                if _links_to_failed_operation(pending_call_sig, failed_sig):
                    resolution = e2
                    evidence.append(e2.event_id)
                    break
                evidence.append(e2.event_id)
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
