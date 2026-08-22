"""Reviewer gold set — annotation schema, loader, and disagreement report (§15).

The spec sequences the reviewer's *gold dataset* and its *evaluation harness*
ahead of the model reviewer itself (§15, §20 Milestone 0, principle #11): you
cannot accept the reviewer ("Precision@3 and missed-critical targets met on
held-out labelled traces", §20 M4) without a yardstick that already exists and
already runs. This module is the schema half of that yardstick; the scoring half
is :mod:`agr.reviewer_eval`.

A gold record is *human annotation* grounded in the immutable source. Moments
anchor on **source step ids** (``s1``…``sN``) rather than derived event ids, so a
label survives a derivation-version bump and compares against any reviewer in one
coordinate space. Every controlled vocabulary here is copied verbatim from the
spec (§3.4 anchor types, §9.1 behaviour axes, §11 root-cause loci) and reused
from :mod:`agr.schema` (attribution levels) — the schema does not re-invent them,
and :meth:`GoldSet.validate` rejects anything outside them so that "experts can
consistently use the schema" (§20 M0 accept) is mechanically enforced.

Honesty note: the labels shipped under ``gold/`` are *synthetic reference
annotations authored for the synthetic fixtures*, not expert-adjudicated
production data. The schema, protocol, and metrics are the deliverable; the
labels demonstrate them. This mirrors the README's honesty about "Harbor ATIF"
being a synthetic contract.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from . import version
from .schema import ATTRIBUTION_LEVELS, _clean
# Controlled vocabularies (spec §3.4, §9.1, §11) live in one place so the gold
# schema and the model reviewer validate against the same sets.
from .taxonomy import (  # re-exported for callers importing these from agr.gold
    BEHAVIOUR_TAGS,
    MOMENT_ANCHOR_TYPES,
    MOMENT_POLARITIES,
    NEGATIVE_BEHAVIOUR_TAGS,
    POSITIVE_BEHAVIOUR_TAGS,
    ROOT_CAUSE_LOCI,
)


class GoldValidationError(ValueError):
    """Raised when a gold record violates the annotation schema (spec §15.2)."""


# --- anchor overlap ----------------------------------------------------------


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


# --- schema records ----------------------------------------------------------


@dataclass
class RootCauseCandidate:
    """A ranked structured root-cause hypothesis for a moment (spec §11).

    ``rank`` is 1-based (1 = most likely). ``locus`` is drawn from the §11
    vocabulary; ``rationale`` states the evidence in words.
    """

    locus: str
    rank: int
    rationale: str
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return _clean({
            "locus": self.locus, "rank": self.rank,
            "rationale": self.rationale, "detail": self.detail,
        })

    @staticmethod
    def from_dict(d: dict) -> "RootCauseCandidate":
        return RootCauseCandidate(
            locus=d["locus"], rank=int(d["rank"]),
            rationale=d.get("rationale", ""), detail=d.get("detail"),
        )


@dataclass
class GoldMoment:
    """One human-labelled decisive moment (spec §15.2 annotation protocol).

    Anchored on immutable source step ids so the label is stable across
    derivation versions and comparable against any reviewer. ``critical`` marks a
    moment whose omission by a reviewer is a *missed-critical* error (spec §15.3);
    ``attribution_ceiling`` is the strongest attribution language the evidence
    licenses — a reviewer that exceeds it overclaims causality (spec §20 M4:
    "causal wording never exceeds attribution evidence").
    """

    moment_id: str
    anchor_type: str
    anchor_step_ids: list[str]
    polarity: str = "negative"
    behaviour_tags: list[str] = field(default_factory=list)
    affected_checks: list[str] = field(default_factory=list)
    affected_requirements: list[str] = field(default_factory=list)
    evidence_span_step_ids: list[str] = field(default_factory=list)
    attribution_ceiling: str = "dependency_linked"
    opportunity_ability: Optional[str] = None
    opportunity_window_step_ids: list[str] = field(default_factory=list)
    root_cause_candidates: list[RootCauseCandidate] = field(default_factory=list)
    acceptable_better_actions: list[str] = field(default_factory=list)
    critical: bool = False

    @property
    def anchor_set(self) -> set[str]:
        return set(self.anchor_step_ids)

    def to_dict(self) -> dict:
        return _clean({
            "moment_id": self.moment_id,
            "anchor_type": self.anchor_type,
            "anchor_step_ids": self.anchor_step_ids,
            "polarity": self.polarity,
            "behaviour_tags": self.behaviour_tags,
            "affected_checks": self.affected_checks,
            "affected_requirements": self.affected_requirements,
            "evidence_span_step_ids": self.evidence_span_step_ids,
            "attribution_ceiling": self.attribution_ceiling,
            "opportunity_ability": self.opportunity_ability,
            "opportunity_window_step_ids": self.opportunity_window_step_ids or None,
            "root_cause_candidates": [r.to_dict() for r in self.root_cause_candidates] or None,
            "acceptable_better_actions": self.acceptable_better_actions or None,
            "critical": self.critical,
        })

    @staticmethod
    def from_dict(d: dict) -> "GoldMoment":
        return GoldMoment(
            moment_id=d["moment_id"],
            anchor_type=d["anchor_type"],
            anchor_step_ids=list(d.get("anchor_step_ids", [])),
            polarity=d.get("polarity", "negative"),
            behaviour_tags=list(d.get("behaviour_tags", [])),
            affected_checks=list(d.get("affected_checks", [])),
            affected_requirements=list(d.get("affected_requirements", [])),
            evidence_span_step_ids=list(d.get("evidence_span_step_ids", [])),
            attribution_ceiling=d.get("attribution_ceiling", "dependency_linked"),
            opportunity_ability=d.get("opportunity_ability"),
            opportunity_window_step_ids=list(d.get("opportunity_window_step_ids", [])),
            root_cause_candidates=[RootCauseCandidate.from_dict(r)
                                   for r in d.get("root_cause_candidates", [])],
            acceptable_better_actions=list(d.get("acceptable_better_actions", [])),
            critical=bool(d.get("critical", False)),
        )


@dataclass
class GoldAnnotation:
    """One annotator's labels for one trajectory (spec §15.2).

    ``no_decisive_moment`` records the honest judgement that no defensible
    decisive moment exists (spec §15.1 sampling requirement); when set, ``moments``
    is expected to be empty and ``no_moment_rationale`` explains why.
    """

    annotator: str
    moments: list[GoldMoment] = field(default_factory=list)
    no_decisive_moment: bool = False
    no_moment_rationale: Optional[str] = None
    task_verifier_concerns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _clean({
            "annotator": self.annotator,
            "moments": [m.to_dict() for m in self.moments],
            "no_decisive_moment": self.no_decisive_moment,
            "no_moment_rationale": self.no_moment_rationale,
            "task_verifier_concerns": self.task_verifier_concerns or None,
        })

    @staticmethod
    def from_dict(d: dict) -> "GoldAnnotation":
        return GoldAnnotation(
            annotator=d["annotator"],
            moments=[GoldMoment.from_dict(m) for m in d.get("moments", [])],
            no_decisive_moment=bool(d.get("no_decisive_moment", False)),
            no_moment_rationale=d.get("no_moment_rationale"),
            task_verifier_concerns=list(d.get("task_verifier_concerns", [])),
        )


@dataclass
class GoldTrajectory:
    """Gold labels for one logical run (spec §15.2).

    Carries one or more annotations. More than one means the trajectory was
    *double-labelled* (spec §15.2: "a subset is independently double-labeled and
    adjudicated"). ``adjudicated`` is the resolved truth used for scoring; when a
    trajectory has a single annotation it is the adjudicated truth by default, so
    disagreement is *reported, not erased*.
    """

    run_id: str
    task_id: str
    annotations: list[GoldAnnotation] = field(default_factory=list)
    adjudicated: Optional[GoldAnnotation] = None
    schema_version: str = version.GOLD_SCHEMA_VERSION

    @property
    def double_labelled(self) -> bool:
        return len(self.annotations) > 1

    def adjudicated_annotation(self) -> GoldAnnotation:
        """The single annotation scoring treats as truth.

        The explicit ``adjudicated`` record if present; otherwise the sole
        annotation. A double-labelled trajectory with no adjudication is an
        error — the harness must not silently pick one annotator over another.
        """
        if self.adjudicated is not None:
            return self.adjudicated
        if len(self.annotations) == 1:
            return self.annotations[0]
        raise GoldValidationError(
            f"{self.run_id}: double-labelled trajectory needs an 'adjudicated' record")

    def to_dict(self) -> dict:
        return _clean({
            "run_id": self.run_id,
            "task_id": self.task_id,
            "schema_version": self.schema_version,
            "annotations": [a.to_dict() for a in self.annotations],
            "adjudicated": self.adjudicated.to_dict() if self.adjudicated else None,
        })

    @staticmethod
    def from_dict(d: dict) -> "GoldTrajectory":
        adj = d.get("adjudicated")
        return GoldTrajectory(
            run_id=d["run_id"],
            task_id=d.get("task_id", ""),
            annotations=[GoldAnnotation.from_dict(a) for a in d.get("annotations", [])],
            adjudicated=GoldAnnotation.from_dict(adj) if adj else None,
            schema_version=d.get("schema_version", version.GOLD_SCHEMA_VERSION),
        )


# --- validation --------------------------------------------------------------


def _validate_moment(run_id: str, m: GoldMoment, source_steps: Optional[set[str]]) -> None:
    where = f"{run_id}/{m.moment_id}"
    if m.anchor_type not in MOMENT_ANCHOR_TYPES:
        raise GoldValidationError(f"{where}: unknown anchor_type {m.anchor_type!r}")
    if m.polarity not in MOMENT_POLARITIES:
        raise GoldValidationError(f"{where}: unknown polarity {m.polarity!r}")
    if not m.anchor_step_ids:
        raise GoldValidationError(f"{where}: a decisive moment needs at least one anchor step")
    if m.attribution_ceiling not in ATTRIBUTION_LEVELS:
        raise GoldValidationError(
            f"{where}: unknown attribution_ceiling {m.attribution_ceiling!r}")
    for tag in m.behaviour_tags:
        if tag not in BEHAVIOUR_TAGS:
            raise GoldValidationError(f"{where}: unknown behaviour tag {tag!r}")
    for rc in m.root_cause_candidates:
        if rc.locus not in ROOT_CAUSE_LOCI:
            raise GoldValidationError(f"{where}: unknown root-cause locus {rc.locus!r}")
    if source_steps is not None:
        for sid in set(m.anchor_step_ids) | set(m.evidence_span_step_ids) | set(
                m.opportunity_window_step_ids):
            if sid not in source_steps:
                raise GoldValidationError(
                    f"{where}: step id {sid!r} is not present in the source of {run_id}")


def _validate_annotation(run_id: str, a: GoldAnnotation, source_steps: Optional[set[str]]) -> None:
    if a.no_decisive_moment and a.moments:
        raise GoldValidationError(
            f"{run_id}/{a.annotator}: no_decisive_moment is set but moments were labelled")
    seen: set[str] = set()
    for m in a.moments:
        if m.moment_id in seen:
            raise GoldValidationError(f"{run_id}: duplicate moment id {m.moment_id!r}")
        seen.add(m.moment_id)
        _validate_moment(run_id, m, source_steps)


@dataclass
class GoldSet:
    """An indexed, validated collection of gold trajectories (spec §15.1)."""

    trajectories: list[GoldTrajectory] = field(default_factory=list)

    def by_run(self, run_id: str) -> Optional[GoldTrajectory]:
        return next((t for t in self.trajectories if t.run_id == run_id), None)

    def run_ids(self) -> list[str]:
        return [t.run_id for t in self.trajectories]

    def validate(self, source_steps: Optional[dict[str, set[str]]] = None) -> "GoldSet":
        """Validate every record against the schema; return ``self`` for chaining.

        When ``source_steps`` (run_id -> set of source step ids) is supplied,
        every anchor / evidence / opportunity step id must name a real step in
        that run's immutable source — so gold cannot reference evidence the
        source never captured.
        """
        seen: set[str] = set()
        for t in self.trajectories:
            if t.run_id in seen:
                raise GoldValidationError(f"duplicate gold trajectory for run {t.run_id!r}")
            seen.add(t.run_id)
            if not t.annotations:
                raise GoldValidationError(f"{t.run_id}: gold trajectory has no annotations")
            steps = source_steps.get(t.run_id) if source_steps else None
            for a in t.annotations:
                _validate_annotation(t.run_id, a, steps)
            if t.adjudicated is not None:
                _validate_annotation(t.run_id, t.adjudicated, steps)
            elif t.double_labelled:
                raise GoldValidationError(
                    f"{t.run_id}: double-labelled trajectory needs an 'adjudicated' record")
        return self


def load_gold_set(directory: str) -> GoldSet:
    """Load every ``*.gold.json`` under ``directory`` into a :class:`GoldSet`.

    Does not validate against sources (the loader has no store); call
    :meth:`GoldSet.validate` with a ``source_steps`` map for that check.
    """
    trajectories: list[GoldTrajectory] = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".gold.json"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                trajectories.append(GoldTrajectory.from_dict(json.load(fh)))
    return GoldSet(trajectories=trajectories)


# --- disagreement report (spec §15.2) ----------------------------------------


def disagreement_report(trajectory: GoldTrajectory, threshold: float = 0.0) -> dict:
    """Report inter-annotator disagreement for a double-labelled trajectory.

    Compares the first two annotators pairwise (the meaning of "double-labelled"
    in §15.2): which moments both raised, which only one raised, and where they
    disagree on attribution ceiling or top root-cause locus on a shared moment.
    Disagreement is *reported, not erased* (spec §15.2 / design commitment).
    """
    if len(trajectory.annotations) < 2:
        return {"run_id": trajectory.run_id, "double_labelled": False}

    a, b = trajectory.annotations[0], trajectory.annotations[1]
    matched: list[dict] = []
    b_matched: set[str] = set()
    for ma in a.moments:
        partner = next((mb for mb in b.moments
                        if mb.moment_id not in b_matched
                        and anchor_overlap(ma.anchor_set, mb.anchor_set, threshold)), None)
        if partner is None:
            continue
        b_matched.add(partner.moment_id)
        entry = {"a_moment": ma.moment_id, "b_moment": partner.moment_id, "disagreements": []}
        if ma.attribution_ceiling != partner.attribution_ceiling:
            entry["disagreements"].append({
                "field": "attribution_ceiling",
                "a": ma.attribution_ceiling, "b": partner.attribution_ceiling,
            })
        la, lb = _top_locus(ma), _top_locus(partner)
        if la != lb:
            entry["disagreements"].append({"field": "top_root_cause_locus", "a": la, "b": lb})
        matched.append(entry)

    a_only = [m.moment_id for m in a.moments
              if not any(anchor_overlap(m.anchor_set, mb.anchor_set, threshold) for mb in b.moments)]
    b_only = [m.moment_id for m in b.moments if m.moment_id not in b_matched]

    return {
        "run_id": trajectory.run_id,
        "double_labelled": True,
        "annotators": [a.annotator, b.annotator],
        "no_decisive_moment_disagreement": a.no_decisive_moment != b.no_decisive_moment,
        "matched_moments": matched,
        "a_only_moments": a_only,
        "b_only_moments": b_only,
        "agreement": not (a_only or b_only or any(e["disagreements"] for e in matched)
                          or a.no_decisive_moment != b.no_decisive_moment),
    }


def _top_locus(m: GoldMoment) -> Optional[str]:
    if not m.root_cause_candidates:
        return None
    return min(m.root_cause_candidates, key=lambda r: r.rank).locus
