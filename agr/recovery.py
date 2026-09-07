"""Recovery state machine (spec §8.6).

A successful action after a failure is not automatically good recovery.
``good_recovery`` requires a qualifying failure, a meaningful strategy or
action change, new progress, and eventual success. A success reached by
repeating the *same* action unchanged is recorded as
``retry_succeeded_without_strategy_change`` — never promoted to good recovery.

AGR-05: the resolving success must be LINKED to the failed operation. The
episode closes only on a success of the same objective.

Review 2026-09-07 (R5) — conservative objective identity: the previous parser
treated ``-m`` as a value-taking flag and discarded its module name, and
ignored scope modifiers like ``-k``, so ``python -m pytest`` /
``python -m compileall`` / ``pytest tests/ -k fast`` all keyed identically to
the failed ``python -m pytest tests/`` and their successes were credited as
recovery. Whether a narrowed or different-module rerun still covers the
originally failed check cannot be established from command text alone — so
until a richer relationship is available the parser is CONSERVATIVE: the
operation key is the tool, the executable, and the complete remaining command
tail. A success on a different or narrowed command supports "later command
succeeded", never "the failed objective was resolved". Only an exact rerun of
the failed command links to the failed operation.

Review 2026-09-07 (R2): results are paired to their calls by tool-use id when
the capture records one; adjacency is the fallback for id-less captures, so a
parallel call's success can no longer resolve another call's failure.
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


def _operation_key(call_sig: tuple | None) -> tuple | None:
    """Objective identity of a tool call: (tool, executable, command tail).

    Conservative by design (review 2026-09-07 R5): every token after the
    executable is part of the identity — module selectors (``-m pytest``),
    scope filters (``-k fast``), and targets (``tests/``) all change the key.
    A changed or narrowed command therefore never shares the failed
    objective's key, and only an exact rerun can resolve its episode.
    """
    if call_sig is None:
        return None
    tool, content = call_sig[0], str(call_sig[1] or "")
    tokens = content.split()
    if not tokens:
        return (tool, "", ())
    return (tool, tokens[0], tuple(tokens[1:]))


def _links_to_failed_operation(success_sig: tuple | None, failed_sig: tuple | None) -> bool:
    """Whether a successful tool result plausibly resolves the failed operation.

    Both sides are CALL signatures (the resolving success is linked through
    the call that produced it). Linkage is mechanical and conservative: the
    same objective — same tool, executable, and full command tail (R5). A
    success on a different objective never links: a successful
    ``python --version`` does not resolve a failed ``python -m pytest tests/``,
    a successful ``python -m compileall tests/`` does not resolve a failed
    ``python -m pytest tests/``, and a narrowed ``pytest tests/ -k fast``
    does not resolve a failed ``pytest tests/``.
    """
    if failed_sig is None or success_sig is None:
        return False  # cannot establish the operations — cannot link
    return _operation_key(success_sig) == _operation_key(failed_sig)


def classify_recoveries(events: list[DerivedEvent], run_id: str, capture_id: str) -> list[RecoveryEpisode]:
    episodes: list[RecoveryEpisode] = []
    n = len(events)
    consumed: set[int] = set()

    for idx, ev in enumerate(events):
        if idx in consumed or not is_tool_failure(ev):
            continue

        failed_sig = preceding_call_signature(events, idx)
        failed_key = _operation_key(failed_sig)
        strategy_changed = False
        resolution: DerivedEvent | None = None
        resolution_sig: tuple | None = None
        last_same_op_call: tuple | None = None  # last attempt at this objective
        evidence = [ev.event_id]

        j = idx + 1
        while j < n:
            e2 = events[j]
            if e2.event_type == "strategy_change":
                strategy_changed = True
                evidence.append(e2.event_id)
            elif e2.event_type == "tool_call":
                call_sig = action_signature(e2)
                if _operation_key(call_sig) == failed_key:
                    last_same_op_call = call_sig
                evidence.append(e2.event_id)
            elif is_tool_failure(e2):
                # F1 follow-up: only a failure of the SAME operation belongs to
                # this episode. An unrelated failure stays unconsumed so it
                # forms its own episode and is never silently absorbed here.
                if _operation_key(preceding_call_signature(events, j)) == failed_key:
                    consumed.add(j)
                evidence.append(e2.event_id)
            elif is_tool_success(e2):
                # AGR-05 + R2: only a success LINKED to the failed operation
                # closes the episode — paired to the call that produced this
                # result by tool-use id (adjacency only for id-less captures).
                # Unrelated successes are recorded as evidence (the run
                # continued) but resolve nothing.
                result_sig = preceding_call_signature(events, j)
                if _operation_key(result_sig) == failed_key:
                    resolution = e2
                    resolution_sig = result_sig
                    evidence.append(e2.event_id)
                    break
                evidence.append(e2.event_id)
            elif e2.event_type in _STOP:
                break
            j += 1

        if resolution is not None:
            # F1 follow-up: change evidence attaches to the RESOLVING attempt —
            # an unrelated intervening call (a successful pwd, a curl) does not
            # make an identical successful retry a "changed action".
            changed_action = resolution_sig != failed_sig
            classification = GOOD_RECOVERY if (strategy_changed or changed_action) else UNCHANGED_RETRY
        else:
            # No resolving attempt: with conservative objective identity (R5),
            # changed_action records whether the agent re-attempted the exact
            # failed command and it failed again — a different or narrowed
            # command is a different objective and establishes nothing about
            # this one.
            changed_action = last_same_op_call is not None and last_same_op_call != failed_sig
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
