"""Recovery state machine (spec §8.6).

A successful action after a failure is not automatically good recovery.
``good_recovery`` requires a qualifying failure, a meaningful strategy or
action change, new progress, and eventual success. A success reached by
repeating the *same* action unchanged is recorded as
``retry_succeeded_without_strategy_change`` — never promoted to good recovery.

AGR-05: the resolving success must be LINKED to the failed operation. The
episode closes only on a success of the same objective — same tool and same
operation identity (executable + meaningful subcommand, e.g. ``python -m pytest``
≠ ``python --version``). An unrelated successful command (failed ``pytest``,
then a successful ``pwd``) cannot close the failure episode.

F1 follow-up (review 2026-09-06): episodes are tracked per failed operation —

* an unrelated failure stays an independent episode; it is never consumed as
  part of another operation's episode;
* change evidence attaches to the RESOLVING attempt: an unrelated intervening
  call does not turn an identical successful retry into a "changed action";
* the operation key is a mechanical approximation (executable + first
  meaningful subcommand token). Narrowed test selection (``pytest`` →
  ``pytest -k fast``) links as the same objective; whether the narrowed scope
  still covers the originally failed check cannot be established from command
  text alone and is not inferred here.
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


# Flags whose next token is a VALUE, not a subcommand (mechanical approximation;
# a fuller parser would need per-tool grammar, which the capture does not carry).
_VALUE_FLAGS = {"-k", "-m", "-c", "-o", "-D", "--tb", "--cov", "--rootdir", "--filter"}


def _operation_key(call_sig: tuple | None) -> tuple | None:
    """Objective identity of a tool call: (tool, executable, subcommand).

    The subcommand is the first token after the executable that is neither a
    flag nor the value of a value-taking flag — so ``python -m pytest tests/``
    identifies as ``pytest``, distinct from ``python --version``. This is the
    objective the review's F1 probes require: the same tool alone is not the
    same objective.
    """
    if call_sig is None:
        return None
    tool, content = call_sig[0], str(call_sig[1] or "")
    tokens = content.split()
    if not tokens:
        return (tool, "", "")
    exe, sub, skip_next = tokens[0], "", False
    for t in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            skip_next = t in _VALUE_FLAGS
            continue
        sub = t
        break
    return (tool, exe, sub)


def _links_to_failed_operation(success_sig: tuple | None, failed_sig: tuple | None) -> bool:
    """Whether a successful tool result plausibly resolves the failed operation.

    Both sides are CALL signatures (the resolving success is linked through
    the call that produced it). Linkage is mechanical: the same objective —
    same tool, executable, and meaningful subcommand (F1 follow-up). A success
    on a different objective never links: a successful ``python --version``
    does not resolve a failed ``python -m pytest tests/``, and a successful
    ``pwd`` does not resolve a failed ``pytest``.
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
        pending_call_sig: tuple | None = None  # the call the next result answers

        j = idx + 1
        while j < n:
            e2 = events[j]
            if e2.event_type == "strategy_change":
                strategy_changed = True
                evidence.append(e2.event_id)
            elif e2.event_type == "tool_call":
                pending_call_sig = action_signature(e2)
                if _operation_key(pending_call_sig) == failed_key:
                    last_same_op_call = pending_call_sig
                evidence.append(e2.event_id)
            elif is_tool_failure(e2):
                # F1 follow-up: only a failure of the SAME operation belongs to
                # this episode. An unrelated failure stays unconsumed so it
                # forms its own episode and is never silently absorbed here.
                if _operation_key(preceding_call_signature(events, j)) == failed_key:
                    consumed.add(j)
                evidence.append(e2.event_id)
            elif is_tool_success(e2):
                # AGR-05: only a success LINKED to the failed operation closes
                # the episode — linked via the CALL that produced this result.
                # Unrelated successes are recorded as evidence (the run
                # continued) but resolve nothing.
                if _operation_key(pending_call_sig) == failed_key:
                    resolution = e2
                    resolution_sig = pending_call_sig
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
            # No resolving attempt: changed_action records whether the agent
            # tried a DIFFERENT approach to this same objective and still
            # failed — not whether unrelated work happened in between.
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
