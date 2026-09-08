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

Item 4 (2026-09-07) — two-tier linkage: R5's exact-rerun requirement is kept
as the STRICT tier (``good_recovery`` / ``retry_succeeded_without_strategy_
change``), but a narrowed rerun or an adjacent command that shares the failed
call's tool, executable, and at least one target token is no longer silently
"no evidence" — it is recorded as ``plausibly_resolved``, a WEAKER tier with
its attribution ceiling dropped one notch, tried only as a fallback when no
exact rerun ever succeeds.

Item 5 (2026-09-07) — mechanical strategy_change: no adapter besides the pi
one ever emits a literal ``strategy_change`` event, so ``good_recovery`` was
unreachable on Harbor/Claude data even given a genuine strategy change. A
state-changing action (Edit/Write, or a shell call to rm/mv/install/...) on a
DIFFERENT objective, seen before the failed operation's resolving attempt, is
mechanically what "the agent changed something" looks like in a trajectory —
it now sets ``strategy_changed`` exactly like a literal event would.

Item 29 (2026-09-08) — fleet-view enrichment: each episode also carries
``tool``, ``error_signature``, ``turns_to_resolve``, ``tokens``, ``wall_ms``,
``nth_occurrence_in_run``, and ``resolved_by`` — every one a deterministic
summary of records this module already computes, added so a fleet read-model
(item 30) can group and count episodes across many runs without re-deriving
any of it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import version
from .error_signature import error_signature_with_basis as _compute_error_signature_with_basis
from ._util import (
    action_signature,
    is_state_changing_action_related_to,
    is_tool_failure,
    is_tool_success,
    paired_call_index,
    paired_result,
    preceding_call_signature,
)
from .schema import DerivedEvent, RecoveryEpisode

GOOD_RECOVERY = "good_recovery"
UNCHANGED_RETRY = "retry_succeeded_without_strategy_change"
PLAUSIBLE_RECOVERY = "plausibly_resolved"
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


def _plausible_operation_match(success_sig: tuple | None, failed_sig: tuple | None) -> bool:
    """Item 4's WEAKER link: same tool, same executable, overlapping targets.

    Tried only when the strict tier (:func:`_links_to_failed_operation`)
    fails to find any exact rerun. Same tool and executable as the failed
    call, and at least one shared non-flag token in the remainder of the
    command — a narrowed rerun (``pytest tests/ -k fast`` after a failed
    ``pytest tests/``) or an adjacent command on the same target
    (``python -m compileall tests/`` after a failed ``python -m pytest
    tests/``) both qualify. This is deliberately looser than R5's strict
    identity and is never promoted to ``good_recovery`` / ``retry_succeeded_
    without_strategy_change`` — the caller records it as ``plausibly_
    resolved`` with a lowered attribution ceiling.
    """
    if failed_sig is None or success_sig is None:
        return False
    tool_a, tool_b = failed_sig[0], success_sig[0]
    if tool_a != tool_b:
        return False
    tokens_a = str(failed_sig[1] or "").split()
    tokens_b = str(success_sig[1] or "").split()
    if not tokens_a or not tokens_b or tokens_a[0] != tokens_b[0]:
        return False  # no executable, or a different one
    targets_a = {t for t in tokens_a[1:] if not t.startswith("-")}
    targets_b = {t for t in tokens_b[1:] if not t.startswith("-")}
    return bool(targets_a & targets_b)


# --- item 29 (2026-09-08): fleet-view enrichment helpers ---------------------

_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
)


def _tokens_from_cost(cost: dict) -> int:
    """Total token count from a step's ``cost`` dict, whichever adapter shape
    it carries (Claude's ``{"usage": {...}}`` or Harbor's flat metrics dict).

    AGR-05 contract this function assumes and every caller relies on:

    * ``cost`` is an already-normalized PER-STEP/PER-TURN measurement — never
      a running cumulative total. Summing this across many events (as
      episode/window accounting does) is only correct because each step's
      own usage is independent; an adapter whose source reports a
      cumulative counter must difference consecutive values into a per-step
      delta BEFORE stamping it onto ``DerivedEvent.cost``, never here (this
      function has no notion of "the previous step" to difference against).
      Neither adapter today (Claude, Harbor) reports a cumulative counter —
      both already hand this function one turn's own usage — so no adapter
      currently needs that differencing step; a future one that does must
      add it at ingestion, not by changing this summation.
    * The keys summed are additive components of ONE measurement, never
      overlapping subsets of each other: Anthropic's contract keeps
      ``cache_creation_input_tokens``/``cache_read_input_tokens`` separate
      from ``input_tokens`` (added here, not double-counted); Harbor's
      OpenAI-style ``prompt_tokens`` already includes any cached portion, so
      that subset (``cached_tokens`` / ``prompt_tokens_details.
      cached_tokens``) is deliberately NOT in ``_TOKEN_KEYS`` — adding it
      here would double-count tokens ``prompt_tokens`` already counts.
    """
    if not cost:
        return 0
    usage = cost.get("usage") if isinstance(cost.get("usage"), dict) else cost
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in _TOKEN_KEYS:
        val = usage.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            total += val
    return int(total)


# AGR-05 (review 82cc113): a turn's cost can land on its leading model_output
# (reasoning/message before a tool_call, item 27's convention — Claude/Harbor
# both attribute it there), on the tool_call itself, or on the call's own
# paired result (a shape some captures/fixtures use) — coverage checks every
# one of those without assuming which single step actually carries it.
_TURN_EVENT_TYPES = ("model_output", "tool_call")


def _window_turn_coverage(events: list[DerivedEvent], start_idx: int, end_idx: int) -> tuple[int, int]:
    """(covered, total) turns among the tool_call events in [start_idx, end_idx].

    One turn = a tool_call, its own contiguous same-actor model_output
    lead-in (the backward walk initiating_attempt_tokens already uses), and
    its paired result. ``total`` is 0 when the window contains no tool_call
    at all (e.g. a trailing text-only turn) — coverage is then undetermined
    rather than guessed.
    """
    covered = total = 0
    for i in range(start_idx, end_idx + 1):
        e = events[i]
        if e.event_type != "tool_call":
            continue
        total += 1
        has_cost = bool(e.cost)
        j = i - 1
        while not has_cost and j >= 0 and events[j].event_type in _TURN_EVENT_TYPES \
                and events[j].actor == e.actor:
            has_cost = bool(events[j].cost)
            j -= 1
        if not has_cost:
            result = paired_result(events, i)
            has_cost = bool(result and result.cost)
        if has_cost:
            covered += 1
    return covered, total


def _parse_timestamp(ts: object) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _wall_ms(start_event: DerivedEvent, end_event: DerivedEvent) -> Optional[int]:
    """Wall-clock milliseconds between two events, when both carry a
    parseable timestamp — ``None`` otherwise, never guessed."""
    start = _parse_timestamp(start_event.payload.get("timestamp"))
    end = _parse_timestamp(end_event.payload.get("timestamp"))
    if start is None or end is None:
        return None
    delta_ms = (end - start).total_seconds() * 1000
    return int(delta_ms) if delta_ms >= 0 else None


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
        plausible_resolution: DerivedEvent | None = None
        plausible_sig: tuple | None = None
        last_same_op_call: tuple | None = None  # last attempt at this objective
        evidence = [ev.event_id]
        # Item 29: total tool_call attempts (any objective) seen since the
        # failure, captured at the moment a resolution (strict or plausible)
        # is found — this is turns_to_resolve.
        tool_calls_since_failure = 0
        turns_at_strict_resolution: int | None = None
        turns_at_plausible_resolution: int | None = None
        # AGR-05: indices, not just events — episode_window_tokens sums by
        # POSITION over `events` (every event type, including model_output,
        # which `evidence` below excludes — that list is for display/
        # traceability, never an accounting ledger). resolution_idx/
        # plausible_idx/stop_idx let the window end at the SAME point the
        # classification actually resolved at, so a later, irrelevant change
        # (scanned only because the loop kept looking for a strict match
        # after already finding a plausible one) can never inflate the
        # window a resolution earlier in the trace is credited with.
        resolution_idx: int | None = None
        plausible_idx: int | None = None
        stop_idx: int | None = None
        # AGR-04 (review 82cc113): a plausible resolution does not stop the
        # scan (it keeps looking for a strict match), so strategy_changed can
        # keep accumulating credit from activity AFTER the plausible
        # resolution already closed — a later, unrelated edit must not attach
        # to an earlier resolution's episode. Frozen the moment plausible_idx
        # is set; used instead of the live value if the plausible branch ends
        # up being the one actually taken.
        strategy_changed_at_plausible: bool | None = None

        j = idx + 1
        while j < n:
            e2 = events[j]
            if e2.event_type == "strategy_change":
                strategy_changed = True
                evidence.append(e2.event_id)
            elif e2.event_type == "tool_call":
                tool_calls_since_failure += 1
                call_sig = action_signature(e2)
                if _operation_key(call_sig) == failed_key:
                    last_same_op_call = call_sig
                elif (not strategy_changed and is_state_changing_action_related_to(e2, failed_sig)
                      and is_tool_success(paired_result(events, j))):
                    # Item 5, refined by review finding #3: a state-changing
                    # action on a DIFFERENT but RELATED objective before the
                    # failed operation's resolving attempt is what "the agent
                    # changed something" mechanically looks like — the same
                    # signal a literal strategy_change event carries. An
                    # unrelated mutation (a stray mkdir before an unrelated
                    # retry) must not credit an unchanged retry as recovery.
                    #
                    # AGR-04 (review 82cc113): requires an OBSERVED successful
                    # result, not merely the absence of an observed failure.
                    # ``paired_result`` returns ``None`` when the call's
                    # result was never captured (a partial/interrupted
                    # capture) or has no known id-linked match; the previous
                    # ``not is_tool_failure(paired_result(...) or e2)`` fell
                    # back to the CALL event itself in that case, which is
                    # never a tool_result and so never satisfies
                    # is_tool_failure either — silently crediting a change
                    # from evidence that was never actually observed. A
                    # missing result now leaves the change unconfirmed:
                    # ``is_tool_success(None)`` is False, same as any other
                    # non-tool_result event.
                    strategy_changed = True
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
                    resolution_idx = j
                    turns_at_strict_resolution = tool_calls_since_failure
                    evidence.append(e2.event_id)
                    break
                # Item 4: no exact match — remember the FIRST plausible
                # (same tool+executable, overlapping target) success as a
                # fallback, but keep scanning for a strict one to prefer.
                if plausible_resolution is None and _plausible_operation_match(result_sig, failed_sig):
                    plausible_resolution = e2
                    plausible_sig = result_sig
                    plausible_idx = j
                    strategy_changed_at_plausible = strategy_changed
                    turns_at_plausible_resolution = tool_calls_since_failure
                evidence.append(e2.event_id)
            elif e2.event_type in _STOP:
                stop_idx = j
                break
            j += 1

        attribution_ceiling = "dependency_linked"
        turns_to_resolve: int | None = None
        resolved_by: str | None = None
        # AGR-05: the window's own end index, distinct from wherever the scan
        # happened to stop — see the resolution_idx/plausible_idx/stop_idx
        # comment above. usage_completeness stays "complete" whenever the
        # window closes on an actual observed event (a resolution or a
        # terminal event); only running out of capture with neither ever
        # observed is "partial".
        window_end_idx: int
        usage_completeness = "complete"
        if resolution is not None:
            # F1 follow-up: change evidence attaches to the RESOLVING attempt —
            # an unrelated intervening call (a successful pwd, a curl) does not
            # make an identical successful retry a "changed action".
            changed_action = resolution_sig != failed_sig
            classification = GOOD_RECOVERY if (strategy_changed or changed_action) else UNCHANGED_RETRY
            turns_to_resolve = turns_at_strict_resolution
            resolved_by = resolution_sig[0] if resolution_sig else None
            window_end_idx = resolution_idx
        elif plausible_resolution is not None:
            # Item 4: no exact rerun ever succeeded, but a narrowed/adjacent
            # command on an overlapping target did. Recorded as a distinct,
            # weaker tier — never promoted to good_recovery/unchanged_retry —
            # with the attribution ceiling dropped one notch.
            resolution = plausible_resolution
            changed_action = plausible_sig != failed_sig
            classification = PLAUSIBLE_RECOVERY
            attribution_ceiling = "hypothesized"
            turns_to_resolve = turns_at_plausible_resolution
            resolved_by = plausible_sig[0] if plausible_sig else None
            # AGR-04 (review 82cc113): the scan keeps looking for a strict
            # match after finding a plausible one, so the live strategy_changed
            # can pick up credit from activity that happened AFTER this
            # resolution already closed — frozen at plausible_idx, same as
            # window_end_idx below, for the same reason.
            strategy_changed = strategy_changed_at_plausible
            # The window ends where THIS resolution actually was, never
            # wherever the scan eventually stopped while still looking for a
            # (never-found) strict match — a later, unrelated change cannot
            # explain an earlier resolution's window.
            window_end_idx = plausible_idx
        else:
            # No resolving attempt: with conservative objective identity (R5),
            # changed_action records whether the agent re-attempted the exact
            # failed command and it failed again — a different or narrowed
            # command is a different objective and establishes nothing about
            # this one.
            changed_action = last_same_op_call is not None and last_same_op_call != failed_sig
            classification = UNRECOVERED
            if stop_idx is not None:
                window_end_idx = stop_idx  # the run's own observed terminal event
            else:
                # Scan ran out of captured events without ever seeing a
                # terminal event — an incomplete capture, mid-episode. Stop at
                # capture end and say so, rather than silently treating a
                # truncated window as the whole story.
                window_end_idx = n - 1
                usage_completeness = "partial"

        # Item 29: a deterministic summary of this episode for the fleet view
        # — every field below is derived from records this function already
        # computed, none of it a new judgement call.
        tool = failed_sig[0] if failed_sig else None
        # ev.text() folds "tool" and other payload fields in after "content"
        # (space-joined) — harmless for token matching, but it breaks a
        # JSON-transported result's own parsing (mini-swe-agent's own
        # {"returncode":...,"output":...}) by appending trailing garbage
        # after the closing brace. The raw content field alone is what
        # error_signature's JSON-transport unwrap needs to parse cleanly.
        failure_text = ev.payload.get("content")
        if not isinstance(failure_text, str) or not failure_text:
            failure_text = ev.text()
        # AGR-07 (review 82cc113): the basis (which tier actually selected
        # the line — a real diagnostic vs. an opaque fallback like "---")
        # travels alongside the signature itself, not discarded here.
        sig, sig_basis = _compute_error_signature_with_basis(failure_text)
        # AGR-05: episode_window_tokens sums EVERY event strictly after the
        # failure result through window_end_idx (inclusive), by POSITION —
        # never the `evidence` list, which is built for display/traceability
        # (it deliberately omits model_output entirely, and skips irrelevant
        # intervening tool_calls' significance) and would silently under-count
        # a turn whose usage/cost landed on a model_output step, which every
        # adapter's turn-fanout can produce. The initiating (failed) attempt's
        # own cost is kept separate — it is not part of what recovering FROM
        # the failure cost, and this window starts strictly after it.
        window_start_idx = idx + 1
        window_events = (events[window_start_idx:window_end_idx + 1]
                         if window_end_idx >= window_start_idx else [])
        episode_window_tokens = sum(_tokens_from_cost(e.cost) for e in window_events)
        # AGR-06: per-event token records for a fleet-level UNION across
        # episodes (agr.fleet) — only cost-carrying events, so an empty dict
        # cleanly means "nothing measured" rather than padding it with zeros.
        usage_records = {e.event_id: _tokens_from_cost(e.cost) for e in window_events if e.cost}
        if window_events and not any(e.cost for e in window_events):
            # AGR-05: not one event in the window ever carried a cost record —
            # the source never instrumented usage here at all. A measured
            # zero (some events had cost data, it just summed to zero) is
            # valid and stays "complete"/"partial"; this is the OTHER case —
            # no measurement exists, so 0 must not read as a known zero.
            usage_completeness = "unavailable"
        elif window_events:
            # AGR-05 (review 82cc113): observing the window's own end (a real
            # resolution or terminal event) proves the WINDOW closed, not
            # that every turn inside it was cost-instrumented — a window can
            # contain one measured turn and a sibling turn with no recorded
            # usage at all. A turn with no cost anywhere in it while at least
            # one other turn in the same window has some is a genuine
            # coverage gap, not an "unavailable" (nothing measured at all) or
            # "complete" (nothing missing) window.
            covered_turns, total_turns = _window_turn_coverage(events, window_start_idx, window_end_idx)
            if total_turns and covered_turns < total_turns:
                usage_completeness = "partial"
        # AGR-05/06 (PR #56 review, confirmed): a turn's cost is attached to
        # only the FIRST of its fanned-out steps (item 27) — for a turn that
        # opens with reasoning/message text, that is a model_output step
        # BEFORE the tool_call, not the call itself. Reading only the failing
        # call's own cost silently lost the turn's real cost in exactly that
        # (common) shape. Walk back through the call's own contiguous,
        # same-actor model_output/tool_call run — the rest of its turn's
        # fan-out — to find it, mirroring how the harbor/claude adapters
        # attribute a turn's cost in the first place.
        failing_call_idx = paired_call_index(events, idx)
        initiating_attempt_tokens = _tokens_from_cost(ev.cost)
        if failing_call_idx is not None:
            turn_start = failing_call_idx
            call_actor = events[failing_call_idx].actor
            while (turn_start > 0 and events[turn_start - 1].event_type in ("model_output", "tool_call")
                  and events[turn_start - 1].actor == call_actor):
                turn_start -= 1
            initiating_attempt_tokens += sum(
                _tokens_from_cost(events[i].cost) for i in range(turn_start, failing_call_idx + 1))
        wall_ms = _wall_ms(ev, resolution) if resolution is not None else None

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
                attribution_ceiling=attribution_ceiling,
                tool=tool,
                error_signature=sig,
                error_signature_basis=sig_basis,
                turns_to_resolve=turns_to_resolve,
                episode_window_tokens=episode_window_tokens,
                initiating_attempt_tokens=initiating_attempt_tokens,
                usage_completeness=usage_completeness,
                usage_records=usage_records,
                wall_ms=wall_ms,
                resolved_by=resolved_by,
                derivation_version=version.RECOVERY_VERSION,
            )
        )

    # Item 29: nth_occurrence_in_run — a 1-indexed count of how many times
    # THIS error_signature has been seen so far within this run, in event
    # order. Requires every episode's signature to already be computed, so
    # it is a final pass over the run's own episodes, not a per-episode
    # computation.
    signature_counts: dict[str, int] = {}
    for ep in episodes:
        key = ep.error_signature or ""
        signature_counts[key] = signature_counts.get(key, 0) + 1
        ep.nth_occurrence_in_run = signature_counts[key]

    return episodes
