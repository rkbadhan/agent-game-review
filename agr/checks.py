"""Atomic verifier checks (spec §6.6, Stage A extraction).

The MVP path extracts structured checks that the verifier already emits. Code
inspection and output interpretation (also in the spec) are later work; this
core reads the ``verifier.checks[]`` array the adapter provides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from . import version
from ._util import action_signature, is_state_changing_action_related_to
from .schema import CHECK_SOURCES, CHECK_STATUSES, CHECK_TIMINGS, RunSource, VerifierCheck

if TYPE_CHECKING:
    from .schema import DerivedEvent, TaskContract


class CheckExtractionError(ValueError):
    pass


def extract_checks(doc: dict, run_source: RunSource) -> list[VerifierCheck]:
    verifier = doc.get("verifier") or {}
    raw_checks = verifier.get("checks", [])
    checks: list[VerifierCheck] = []
    for i, raw in enumerate(raw_checks):
        status = raw.get("status", "unknown")
        if status not in CHECK_STATUSES:
            raise CheckExtractionError(f"invalid check status {status!r}")
        source = raw.get("source", "native_structured")
        if source not in CHECK_SOURCES:
            raise CheckExtractionError(f"invalid check source {source!r}")
        timing = raw.get("timing")
        if timing is not None and timing not in CHECK_TIMINGS:
            raise CheckExtractionError(f"invalid check timing {timing!r}")
        checks.append(
            VerifierCheck(
                check_id=raw.get("check_id", f"C{i + 1}"),
                run_id=run_source.run_id,
                source_capture_id=run_source.source_capture_id,
                name=raw.get("name", ""),
                status=status,
                source=source,
                contract_item_ids=list(raw.get("contract_item_ids", [])),
                expected=raw.get("expected"),
                observed=raw.get("observed"),
                source_pointers=list(raw.get("source_pointers", [])),
                derivation_version=version.CHECK_DERIVATION_VERSION,
                timing=timing,
                # AGR-02: carried through only when the source (currently
                # agr.verifier_synth) declared them; a native/structured
                # external-verifier check declares neither and stays untracked
                # by reconciliation — it is already one atomic, authoritative
                # check.
                scope=raw.get("scope"),
                sequence=raw.get("sequence"),
            )
        )
    return checks


# AGR-02: a relevant subsequent mutation invalidates a passing check as
# evidence of the run's FINAL state.
STALE_REASON_RELEVANT_MUTATION = "relevant_mutation_after_check"


def reconcile_checks(checks: list["VerifierCheck"], events: list["DerivedEvent"]) -> list["VerifierCheck"]:
    """Reconcile multiple observations of the same scope (AGR-02).

    Nothing is deleted, reordered, or re-scored — every historical
    observation keeps its own ``status``/``expected``/``observed``. This sets
    only ``superseded_by`` and ``stale_reason`` (mutating the given check
    objects in place, and returning the same list) so :attr:`VerifierCheck.
    effective_status` can report the CURRENT view without losing the record
    of what was actually observed and when.

    Two independent effects:

    * Same-scope chaining — within a group of checks sharing the identical
      ``scope`` string (exact match only, never substring/containment: a
      narrower check's scope differs from a broader one's, so neither can
      ever supersede the other), every check but the last (ordered by
      ``sequence``) gets ``superseded_by`` pointing at the next one. A check
      with no scope, or a scope no other check shares, is untouched.
    * Mutation staleness — a ``passed``, ``during_run`` check that is NOT
      superseded (nothing re-verified its scope) gets ``stale_reason`` set
      when a state-changing action relevant to its own command happened
      after its result, resolved through the SAME relatedness test recovery
      classification uses (``is_state_changing_action_related_to``), so
      "relevant" means one thing across the codebase.
    """
    event_by_id = {e.event_id: e for e in events}

    groups: dict[str, list[VerifierCheck]] = {}
    for c in checks:
        if c.scope is not None:
            groups.setdefault(c.scope, []).append(c)
    for group in groups.values():
        ordered = sorted(group, key=lambda c: c.sequence if c.sequence is not None else 0)
        for earlier, later in zip(ordered, ordered[1:]):
            earlier.superseded_by = later.check_id

    for c in checks:
        if c.status != "passed" or c.timing != "during_run" or c.superseded_by is not None:
            continue
        if len(c.source_pointers) < 2:
            continue
        call_event = event_by_id.get(c.source_pointers[0])
        result_event = event_by_id.get(c.source_pointers[1])
        if call_event is None or result_event is None:
            continue
        target_sig = action_signature(call_event)
        later_events = [e for e in events if e.sequence > result_event.sequence]
        if any(is_state_changing_action_related_to(e, target_sig) for e in later_events):
            c.stale_reason = STALE_REASON_RELEVANT_MUTATION
    return checks


# AGR-02 (review 82cc113): a declared, required item is one the task author
# actually stated (or an environment precondition) — never a
# ``verifier_enforced`` item (synthesized FROM a check that already covers
# it, by definition) or a ``reference_assumption`` (not author-required; see
# agr.contract._coverage_warnings' own "reference_only_assumption" split).
_DECLARED_SOURCE_TYPES = ("stated_requirement", "environment_precondition")


def _in_session_only(current: list[tuple[VerifierCheck, str]]) -> bool:
    """True when every current check is an in-session synthesized observation.

    A native/structured external verifier check (``source ==
    "native_structured"``) was authored to cover the task's own requirements,
    so its PASSED carries that authority already. An in-session check
    (``agr.verifier_synth``) is synthesized from whatever test invocation the
    agent happened to run — it can be one narrow smoke test with no relation
    to the rest of a broader declared instruction.
    """
    return bool(current) and all(
        c.source == "output_interpretation" and c.timing == "during_run" for c, _ in current
    )


def _uncovered_required_items(contract: "TaskContract") -> list[str]:
    return [
        i.id for i in contract.items
        if i.importance == "required" and i.source_type in _DECLARED_SOURCE_TYPES
        and not i.mapped_checks
    ]


def outcome(checks: list[VerifierCheck], contract: Optional["TaskContract"] = None) -> dict:
    """Roll checks up into a run outcome summary.

    A run with no checks carries no verifier evidence. That is an explicit
    ingestion state (spec §7.2), reported as UNVERIFIED — never a vacuous
    PASSED, which would rank an unexamined run as a clean pass in triage.

    The same honesty applies per-check (F1 follow-up): a check that ended
    ``unknown``/``skipped``/``error`` recorded no verdict, so a run whose
    checks include no failure but not universal passes is UNDETERMINED —
    never PASSED with ``passed < total``.

    AGR-02: the rollup counts each check's :attr:`VerifierCheck.
    effective_status`, not its raw ``status`` — a superseded observation
    (an earlier, same-scope attempt a later one reconciled) is excluded, and
    a mutation-stale pass demotes to ``unknown``. Call
    :func:`reconcile_checks` first so those fields are populated; on
    unreconciled checks (``scope``/``superseded_by``/``stale_reason`` all
    ``None``) ``effective_status`` reduces to ``status`` and this behaves
    exactly as before. Nothing in ``checks`` is discarded here — the full
    historical list, and which entries were excluded/demoted and why, is
    still available from the input list and reported below.

    AGR-02 (review 82cc113): when ``contract`` is given and every current
    check is in-session-only (:func:`_in_session_only`), an otherwise-PASSED
    rollup demotes to UNDETERMINED if any author-``required`` declared item
    has no mapped check at all — a passing smoke test the agent happened to
    run does not establish that the rest of the declared instruction was
    exercised. This never turns a pass into a FAILED: missing coverage is
    unknown, not a failure. Omitting ``contract`` (the default) preserves the
    original, pre-review behaviour exactly.
    """
    current = [(c, c.effective_status) for c in checks]
    current = [(c, s) for c, s in current if s is not None]
    total = len(current)
    passed = sum(1 for _, s in current if s == "passed")
    failed = [c.check_id for c, s in current if s == "failed"]
    undetermined = [c.check_id for c, s in current if s in ("unknown", "skipped", "error")]
    coverage_gaps: list[str] = []
    if total == 0:
        status = "UNVERIFIED"
    elif failed:
        status = "FAILED"
    elif passed == total:
        status = "PASSED"
        if contract is not None and _in_session_only(current):
            coverage_gaps = _uncovered_required_items(contract)
            if coverage_gaps:
                status = "UNDETERMINED"
    else:
        status = "UNDETERMINED"
    return {
        "status": status,
        "passed": passed,
        "total": total,
        "failed_checks": failed,
        "undetermined_checks": undetermined,
        "total_observations": len(checks),
        "superseded_checks": [c.check_id for c in checks if c.superseded_by is not None],
        "stale_checks": [c.check_id for c in checks if c.stale_reason is not None],
        "coverage_gaps": coverage_gaps,
    }
