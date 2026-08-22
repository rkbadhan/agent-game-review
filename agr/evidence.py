"""Evidence and dependency slicing (spec §3.3, §8.4).

An evidence slice connects a verifier check to the smallest relevant set of
events. It establishes *relevance*, not causality (spec principle #3). The
deterministic core therefore caps the attribution ceiling at
``dependency_linked`` and never emits ``counterfactually_supported`` — that
requires replay evidence this package does not generate.
"""

from __future__ import annotations

import re

from . import version
from .schema import DerivedEvent, EvidenceSlice, Opportunity, VerifierCheck

_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text))


def _values(check: VerifierCheck) -> tuple[set[str], set[str], set[str]]:
    expected = {str(v) for v in (check.expected or [])}
    observed = {str(v) for v in (check.observed or [])}
    missing = expected - observed
    return expected, observed, missing


def slice_for_check(check: VerifierCheck, events: list[DerivedEvent]) -> EvidenceSlice:
    """Build a relevance slice for a single check.

    Standard branch: the expected/observed values appeared in the trace. We
    collect events whose content mentions any expected or observed value, plus
    the final submission. When both an ``artifact_observation`` carrying the
    observed set and a prior ``tool_result`` carrying a *missing* expected
    value exist, a dependency path is present and the ceiling is
    ``dependency_linked``; otherwise it stays ``hypothesized``.
    """
    expected, observed, missing = _values(check)
    relevant_values = expected | observed

    matched: list[DerivedEvent] = []
    saw_tool_result_with_missing = False
    saw_artifact_observation = False

    for event in events:
        toks = _tokens(event.text())
        if relevant_values & toks or event.event_type == "final_submission":
            matched.append(event)
            if event.event_type == "tool_result" and (missing & toks):
                saw_tool_result_with_missing = True
            if event.event_type == "artifact_observation" and (observed & toks):
                saw_artifact_observation = True

    if check.status == "failed" and saw_tool_result_with_missing and saw_artifact_observation:
        ceiling = "dependency_linked"
        rationale = (
            "A required value appeared in a tool result but is absent from the "
            "observed artifact; a state/evidence path links the two to the failed check."
        )
    else:
        ceiling = "hypothesized"
        rationale = "Relevant events were identified but no dependency path was mechanically established."

    return EvidenceSlice(
        slice_id=f"slice_{check.check_id}",
        run_id=check.run_id,
        source_capture_id=check.source_capture_id,
        check_id=check.check_id,
        contract_item_ids=list(check.contract_item_ids),
        branch="standard",
        event_ids=[e.event_id for e in matched],
        attribution_ceiling=ceiling,
        rationale=rationale,
        derivation_version=version.SLICE_DERIVATION_VERSION,
    )


def slice_omission(
    run_id: str,
    capture_id: str,
    slice_key: str,
    contract_item_ids: list[str],
    opportunity: Opportunity,
    closing_event_id: str,
    observability_supported: bool,
) -> EvidenceSlice:
    """Omission-branch slice (spec §8.4 omission branch).

    An omission is established only inside a feasible opportunity window and
    only when observability supports the absence claim. With both, a dependency
    path from the missed window to the outcome exists; without adequate
    observability the ceiling drops to ``hypothesized``.
    """
    ceiling = "dependency_linked" if observability_supported else "hypothesized"
    event_ids = list(dict.fromkeys([opportunity.start_event_id, opportunity.end_event_id, closing_event_id]))
    if observability_supported:
        rationale = (
            "The required behaviour is absent across a feasible opportunity window that "
            "the run bypassed at submission; observability supports the absence claim."
        )
    else:
        rationale = (
            "The required behaviour was not observed, but observability is insufficient to "
            "assert the absence mechanically."
        )
    return EvidenceSlice(
        slice_id=f"slice_omission_{slice_key}",
        run_id=run_id,
        source_capture_id=capture_id,
        check_id=slice_key,
        contract_item_ids=list(contract_item_ids),
        branch="omission",
        event_ids=event_ids,
        attribution_ceiling=ceiling,
        rationale=rationale,
        derivation_version=version.SLICE_DERIVATION_VERSION,
    )


def slice_distributed(
    run_id: str,
    capture_id: str,
    slice_key: str,
    event_ids: list[str],
    pattern: str,
) -> EvidenceSlice:
    """Distributed-branch slice (spec §8.4 distributed branch).

    Used when no single action is decisive: the slice anchors an interval and
    names the pattern (e.g. repeated local choices, progressive requirement
    loss, accumulated resource exhaustion). Attribution is capped at
    ``dependency_linked`` because a pattern establishes relevance, not cause.
    """
    return EvidenceSlice(
        slice_id=f"slice_distributed_{slice_key}",
        run_id=run_id,
        source_capture_id=capture_id,
        check_id=slice_key,
        contract_item_ids=[],
        branch="distributed",
        event_ids=list(dict.fromkeys(event_ids)),
        attribution_ceiling="dependency_linked",
        rationale=f"No single action is decisive; the interval shows a distributed pattern: {pattern}.",
        derivation_version=version.SLICE_DERIVATION_VERSION,
    )


def slice_external(
    run_id: str,
    capture_id: str,
    check: VerifierCheck,
    external_event_id: str,
    locus: str,
) -> EvidenceSlice:
    """External-branch slice (spec §8.4 external branch).

    Used when the outcome may be better explained by a tool, environment,
    harness, or verifier event rather than an agent decision. The deterministic
    core cannot prove this without a counterfactual, so the ceiling stays
    ``hypothesized`` and the language remains explanatory.
    """
    return EvidenceSlice(
        slice_id=f"slice_external_{check.check_id}",
        run_id=run_id,
        source_capture_id=capture_id,
        check_id=check.check_id,
        contract_item_ids=list(check.contract_item_ids),
        branch="external",
        event_ids=[external_event_id],
        attribution_ceiling="hypothesized",
        rationale=(
            f"The failed check may be better explained by a {locus} event on the path "
            f"than by an agent decision; this is explanatory, not established cause."
        ),
        derivation_version=version.SLICE_DERIVATION_VERSION,
    )
