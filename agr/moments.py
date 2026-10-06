"""Reviewer-agnostic moment shape and step-anchor matching.

A reviewer's moment and a (legacy) gold label are compared in **source-step
coordinates**: a predicted moment is anchored on derived event ids, which
:func:`moments_from_review` maps back onto source step ids through the forensic
step-to-event routing, so two moments are "the same moment" when their anchor
step-id sets overlap (:func:`anchor_overlap`).

This is the small shared kernel that the Compare surface (:mod:`agr.compare`)
needs to align two reviews. It lives in the core so the core never imports the
gold / reviewer-evaluation tooling that was moved to ``legacy/``.

Pure stdlib — no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def anchor_overlap(a: set[str], b: set[str], threshold: float = 0.0) -> bool:
    """True when two anchor step-id sets overlap enough to be "the same moment".

    Matching is by source-step overlap so gold labels and any reviewer's moments
    compare in the same coordinate space. ``threshold`` is a Jaccard floor;
    the default (``0.0``) treats any shared step as a match, which is the right
    default for the short MVP traces (a decisive moment is typically one anchor
    step). Raise the threshold to demand tighter agreement.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    if threshold <= 0.0:
        return True
    return inter / len(a | b) >= threshold


@dataclass
class PredictedMoment:
    """One moment a reviewer surfaced, normalised into gold coordinates.

    ``attribution_ceiling`` is the strongest attribution language the *reviewer*
    attached to the card. The deterministic baseline never claims more than
    ``dependency_linked`` (the core's cap; README "relevance is not causality"),
    so that is the default.
    """

    moment_id: str
    anchor_step_ids: list[str]
    polarity: str = "negative"
    affected_checks: list[str] = field(default_factory=list)
    evidence_span_step_ids: list[str] = field(default_factory=list)
    attribution_ceiling: str = "dependency_linked"
    # AGR-07: the reviewer's intended ranking for the top-k cut. The review
    # view orders moments by selection rank; carrying it here keeps the cut on
    # the reviewer's ranking rather than any incidental timeline order.
    selection_rank: Optional[int] = None
    better_action: Optional[str] = None

    @property
    def anchor_set(self) -> set[str]:
        return set(self.anchor_step_ids)


def moments_from_review(review: dict, forensic: dict) -> list[PredictedMoment]:
    """Adapt the deterministic ``review["moments"]`` into predictions.

    Uses the forensic view's step→event routing to translate each moment's
    event-id anchors into source step ids, so predictions compare against gold in
    the same coordinate space. Moment order is preserved (the review orders
    moments by timeline), which is the order the top-k cut respects.
    """
    step_of_event: dict[str, str] = {}
    for row in forensic.get("steps", []):
        for eid in row.get("event_ids", []):
            step_of_event[eid] = row["step_id"]

    out: list[PredictedMoment] = []
    for m in review.get("moments", []):
        # Map each event-id anchor onto its source step. Keep any id we cannot
        # ground as-is, so an unmappable anchor stays a *visible* (non-matching)
        # prediction — counted against precision — rather than silently vanishing.
        steps = _dedupe(step_of_event.get(e, e) for e in m.get("anchor_event_ids", []))
        out.append(PredictedMoment(
            moment_id=m.get("moment_id", ""),
            anchor_step_ids=steps,
            polarity=m.get("polarity", "negative"),
            affected_checks=list(m.get("affected_checks", [])),
            # The deterministic reviewer cites its anchors as the evidence span.
            evidence_span_step_ids=steps,
            attribution_ceiling=m.get("attribution_ceiling", "dependency_linked"),
            # AGR-07: the reviewer's own selection ranking drives the top-k cut.
            selection_rank=m.get("selection_rank"),
            better_action=m.get("better_action"),
        ))
    # The top-k cut must respect the reviewer's intended ranking (AGR-07) —
    # rank order when ranks exist, never an incidental timeline order.
    out.sort(key=lambda p: (p.selection_rank is None, p.selection_rank or 0))
    return out


def _dedupe(it) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in it:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
