"""Reviewer evaluation harness — scoring half of the M0 yardstick (spec §15.3).

The spec builds the reviewer's evaluation set *before* the reviewer (§15, §20 M0,
principle #11). :mod:`agr.gold` is the schema and ground-truth half; this module
is the scoring half. It is **reviewer-agnostic**: it scores a list of
:class:`PredictedMoment` against a :class:`~agr.gold.GoldTrajectory`, regardless
of where the predictions came from. Today the only reviewer that exists is the
deterministic detector layer, so its projected moments
(``read.get_review(...)["moments"]``) are the *baseline* — which lets the harness
run end-to-end now, before any model call. When the M4 model reviewer lands, its
structured moments feed the same functions unchanged and must beat that baseline.

Predictions and gold both live in **source-step coordinates**: a predicted moment
is anchored on derived event ids, which :func:`moments_from_review` maps back onto
source step ids via the forensic step↔event routing, so a moment matches a gold
moment when their anchor step-id sets overlap (spec §15.3 evidence spans).

Metrics implemented (the moment-selection + attribution subset of §15.3):

- Precision@3 / Recall@3 against adjudicated decisive moments;
- redundant-card rate (several cards for one moment);
- missed-critical-moment rate;
- no-moment calibration (a clean pass must not manufacture a card);
- evidence-span precision / recall on matched moments; and
- attribution-overclaim rate (a card claiming more causal certainty than the gold
  ceiling licenses — the guardrail M4 acceptance hinges on, §20 M4).

Aggregation is micro-averaged across runs. No score is displayed or compared
across tasks (spec §8.10). Pure stdlib — no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import version
from .gold import GoldMoment, GoldSet, GoldTrajectory, anchor_overlap
from .schema import ATTRIBUTION_LEVELS

_ATTR_RANK = {level: i for i, level in enumerate(ATTRIBUTION_LEVELS)}


# --- the reviewer-agnostic prediction shape ----------------------------------


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


# --- per-run evaluation ------------------------------------------------------


@dataclass
class RunEval:
    """Raw, aggregation-ready counts for one run's evaluation.

    Rates are derived from these counts by :class:`SetEval` so that a set-level
    metric is a micro-average, not a mean-of-means.
    """

    run_id: str
    no_moment_case: bool = False
    calibrated: Optional[bool] = None      # no-moment runs only
    abstained: bool = False                # AGR-07: review failed / no decision
    n_pred_topk: int = 0
    n_matched: int = 0
    n_gold: int = 0
    n_gold_critical: int = 0
    n_missed_critical: int = 0
    n_redundant: int = 0
    ev_inter: int = 0
    ev_pred: int = 0
    ev_gold: int = 0
    n_attr_overclaim: int = 0
    # AGR-07: per-match quality dimensions, scored separately (spec §15.3).
    n_mech_agree: int = 0                  # matched pairs with mechanism specificity
    n_claim_supported: int = 0             # matched pairs whose claims fit the ceiling
    matches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        # A no-moment run is scored by calibration and explicit fabrication
        # counts, not moment selection — the selection metrics are undefined
        # (no gold moments to match), so report them as null rather than a
        # flattering 0/0 = 1.0.
        if self.no_moment_case:
            return {
                "run_id": self.run_id,
                "no_moment_case": True,
                "abstained": self.abstained,
                "calibrated": self.calibrated,
                "n_fabricated": self.n_pred_topk,
                "precision_at_3": None,
                "recall_at_3": None,
                "redundant_card_rate": None,
                "missed_critical_rate": None,
                "evidence_span_precision": None,
                "evidence_span_recall": None,
                "attribution_overclaim_rate": None,
                "matches": self.matches,
            }
        if self.abstained:
            # AGR-07: an incomplete review (enrichment failure, provider error)
            # is an explicit abstention — it stays in the denominator with zero
            # credit, never silently skipped and never scored as a clean sheet.
            return {
                "run_id": self.run_id,
                "abstained": True,
                "precision_at_3": 0.0 if self.n_pred_topk == 0 else _ratio(self.n_matched, self.n_pred_topk),
                "recall_at_3": _ratio(self.n_matched, self.n_gold) if self.n_gold else None,
                "redundant_card_rate": None,
                "missed_critical_rate": _ratio(self.n_missed_critical, self.n_gold_critical)
                if self.n_gold_critical else None,
                "evidence_span_precision": None,
                "evidence_span_recall": None,
                "attribution_overclaim_rate": None,
                "matches": [],
            }
        return {
            "run_id": self.run_id,
            "no_moment_case": False,
            "abstained": False,
            "precision_at_3": _ratio(self.n_matched, self.n_pred_topk),
            "recall_at_3": _ratio(self.n_matched, self.n_gold),
            "redundant_card_rate": _ratio(self.n_redundant, self.n_pred_topk),
            "missed_critical_rate": _ratio(self.n_missed_critical, self.n_gold_critical),
            "evidence_span_precision": _ratio(self.ev_inter, self.ev_pred),
            "evidence_span_recall": _ratio(self.ev_inter, self.ev_gold),
            "attribution_overclaim_rate": _ratio(self.n_attr_overclaim, self.n_matched),
            "mechanism_agreement_rate": _ratio(self.n_mech_agree, self.n_matched),
            "claim_support_rate": _ratio(self.n_claim_supported, self.n_matched),
            "matches": self.matches,
        }


def _best_gold(pred: PredictedMoment, golds: list[GoldMoment], threshold: float,
               matched_ids: set[str]):
    """The gold moment a prediction best overlaps, or None.

    Overlap gates the match; ties break on affected-check agreement then Jaccard,
    so a prediction lands on the gold moment it most specifically concerns. An
    *already-matched* gold moment is only chosen when no unmatched overlapping
    moment remains — so a prediction that overlaps both a taken and a free moment
    takes the free one (which greedy scoring alone would miss), and is flagged
    redundant only when every moment it could explain is already accounted for.
    """
    scored = []
    for g in golds:
        if not anchor_overlap(pred.anchor_set, g.anchor_set, threshold):
            continue
        checks = len(set(pred.affected_checks) & set(g.affected_checks))
        inter = len(pred.anchor_set & g.anchor_set)
        union = len(pred.anchor_set | g.anchor_set) or 1
        # Sort key: unmatched first, then higher check overlap, then higher
        # Jaccard. ``in matched_ids`` is False (0) for unmatched, sorting ahead.
        scored.append(((g.moment_id in matched_ids), -checks, -(inter / union), g))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[0][3]


def evaluate_run(predicted: list[PredictedMoment], gold: GoldTrajectory,
                 k: int = 3, threshold: float = 0.0, abstained: bool = False) -> RunEval:
    """Score one run's predictions against its adjudicated gold annotation.

    ``abstained`` marks an INCOMPLETE review (AGR-06 enrichment failure,
    provider error): the run stays in the denominator with zero credit —
    an error is never silently equivalent to a clean result (AGR-07).
    """
    adjudicated = gold.adjudicated_annotation()

    if adjudicated.no_decisive_moment:
        # No defensible decisive moment: a calibrated reviewer surfaces no card.
        return RunEval(run_id=gold.run_id, no_moment_case=True,
                       abstained=abstained,
                       calibrated=(len(predicted) == 0 and not abstained),
                       n_pred_topk=len(predicted[:k]))

    golds = list(adjudicated.moments)
    # AGR-07: the top-k cut uses the reviewer's intended selection ranking —
    # never an incidental caller order. Ranks absent (None) sort last.
    predicted = sorted(predicted, key=lambda p: (p.selection_rank is None,
                                                 p.selection_rank if p.selection_rank is not None else 0))
    topk = predicted[:k]
    ev = RunEval(
        run_id=gold.run_id,
        abstained=abstained,
        n_pred_topk=len(topk),
        n_gold=len(golds),
        n_gold_critical=sum(1 for g in golds if g.critical),
    )
    matched_gold_ids: set[str] = set()
    for pred in topk:
        g = _best_gold(pred, golds, threshold, matched_gold_ids)
        if g is None:
            ev.matches.append({"prediction": pred.moment_id, "gold": None, "status": "false_positive"})
            continue
        if g.moment_id in matched_gold_ids:
            ev.n_redundant += 1
            ev.matches.append({"prediction": pred.moment_id, "gold": g.moment_id, "status": "redundant"})
            continue
        if pred.polarity != g.polarity:
            # AGR-07: polarity disagreement is NOT a semantic match — a positive
            # prediction for a negative gold moment (or vice versa) cannot earn
            # precision or recall credit, however well it localises. The gold
            # moment stays unmatched (a real miss).
            ev.matches.append({"prediction": pred.moment_id, "gold": g.moment_id,
                               "status": "polarity_mismatch",
                               "pred_polarity": pred.polarity, "gold_polarity": g.polarity})
            continue
        matched_gold_ids.add(g.moment_id)
        ev.n_matched += 1
        # Evidence-span overlap on the matched pair.
        p_span, g_span = set(pred.evidence_span_step_ids), set(g.evidence_span_step_ids)
        ev.ev_inter += len(p_span & g_span)
        ev.ev_pred += len(p_span)
        ev.ev_gold += len(g_span)
        # Attribution overclaim: reviewer claims more than the gold ceiling.
        overclaim = _ATTR_RANK.get(pred.attribution_ceiling, 0) > _ATTR_RANK.get(g.attribution_ceiling, 0)
        if overclaim:
            ev.n_attr_overclaim += 1
        # AGR-07: the remaining semantic dimensions are scored separately,
        # never folded into the match decision.
        checks_agree = bool(set(pred.affected_checks) & set(g.affected_checks))
        ev.n_mech_agree += 1 if checks_agree else 0
        ev.n_claim_supported += 0 if overclaim else 1
        ev.matches.append({
            "prediction": pred.moment_id, "gold": g.moment_id, "status": "matched",
            "mechanism_specificity": "check_agreement" if checks_agree else "anchor_only",
            "claim_support": "within_ceiling" if not overclaim else "overclaimed",
            "better_action_assessed": bool(pred.better_action),
        })

    ev.n_missed_critical = sum(1 for g in golds if g.critical and g.moment_id not in matched_gold_ids)
    return ev


# --- set-level aggregation ---------------------------------------------------


@dataclass
class SetEval:
    """Micro-averaged metrics over a set of :class:`RunEval` (spec §15.3).

    ``skipped_runs`` names gold trajectories that had no prediction supplied (for
    :func:`evaluate_store`, a run absent from the store) — so a shrunken
    denominator is visible rather than silent.
    """

    runs: list[RunEval] = field(default_factory=list)
    skipped_runs: list[str] = field(default_factory=list)

    def metrics(self) -> dict:
        decisive = [r for r in self.runs if not r.no_moment_case]
        no_moment = [r for r in self.runs if r.no_moment_case]
        s = lambda attr, rows=decisive: sum(getattr(r, attr) for r in rows)  # noqa: E731
        calibrated = sum(1 for r in no_moment if r.calibrated)
        fabricated = sum(r.n_pred_topk for r in no_moment)
        abstentions = sum(1 for r in self.runs if r.abstained)
        return {
            "reviewer_eval_version": version.REVIEWER_EVAL_VERSION,
            "n_runs": len(self.runs),
            "n_decisive_runs": len(decisive),
            "n_no_moment_runs": len(no_moment),
            "n_skipped_runs": len(self.skipped_runs),
            # AGR-07: errors and abstentions stay in the denominator — a failed
            # review scores zero credit and is counted here explicitly.
            "n_abstained_runs": abstentions,
            "abstention_rate": _ratio(abstentions, len(self.runs)),
            # A metric with a zero denominator is *undefined* and reported as null
            # (e.g. missed-critical when no gold moment is critical) rather than a
            # 0.0 / 1.0 that reads as a real score.
            "precision_at_3": _ratio(s("n_matched"), s("n_pred_topk")),
            "recall_at_3": _ratio(s("n_matched"), s("n_gold")),
            "redundant_card_rate": _ratio(s("n_redundant"), s("n_pred_topk")),
            "missed_critical_rate": _ratio(s("n_missed_critical"), s("n_gold_critical")),
            "no_moment_calibration": _ratio(calibrated, len(no_moment)),
            # AGR-07: fabricated findings on clean passes are counted and named.
            "n_fabricated_on_clean_pass": fabricated,
            "fabrication_on_clean_pass_rate": _ratio(
                sum(1 for r in no_moment if r.n_pred_topk > 0), len(no_moment)),
            "evidence_span_precision": _ratio(s("ev_inter"), s("ev_pred")),
            "evidence_span_recall": _ratio(s("ev_inter"), s("ev_gold")),
            "attribution_overclaim_rate": _ratio(s("n_attr_overclaim"), s("n_matched")),
            # AGR-07: semantic quality scored separately per matched pair.
            "mechanism_agreement_rate": _ratio(s("n_mech_agree"), s("n_matched")),
            "claim_support_rate": _ratio(s("n_claim_supported"), s("n_matched")),
        }

    def report(self) -> dict:
        return {"summary": self.metrics(), "runs": [r.to_dict() for r in self.runs],
                "skipped_runs": self.skipped_runs}


def evaluate_set(predictions: dict[str, list[PredictedMoment]], gold_set: GoldSet,
                 k: int = 3, threshold: float = 0.0) -> SetEval:
    """Evaluate every gold trajectory for which predictions were supplied.

    A trajectory mapped to ``None`` is an ABSTENTION (AGR-07): the review is
    incomplete — it stays in the denominator with zero credit. Trajectories
    with no entry at all are recorded in ``skipped_runs`` — never silently
    dropped from the denominator either.
    """
    runs: list[RunEval] = []
    skipped: list[str] = []
    for traj in gold_set.trajectories:
        if traj.run_id not in predictions:
            skipped.append(traj.run_id)
            continue
        preds = predictions[traj.run_id]
        if preds is None:
            runs.append(evaluate_run([], traj, k=k, threshold=threshold, abstained=True))
            continue
        runs.append(evaluate_run(preds, traj, k=k, threshold=threshold))
    return SetEval(runs=runs, skipped_runs=skipped)


def _ratio(num: int, den: int, empty=None):
    """``num/den`` rounded, or ``empty`` (``None`` = undefined) when ``den`` is 0."""
    if den == 0:
        return empty
    return round(num / den, 4)


# --- store convenience (deterministic baseline) ------------------------------


def predicted_from_store(store, run_id: str) -> list[PredictedMoment]:
    """The deterministic baseline reviewer's moments for one run, as predictions.

    Reads the persisted review + forensic views (no recomputation) and adapts
    them. This is the reviewer the harness scores until the M4 model reviewer
    exists; the model reviewer will produce :class:`PredictedMoment`s the same way.
    """
    from . import read
    review = read.get_review(store, run_id)
    forensic = read.get_forensic(store, run_id)
    return moments_from_review(review, forensic)


def evaluate_store(store, gold_set: GoldSet, k: int = 3, threshold: float = 0.0) -> SetEval:
    """Score the deterministic baseline over every gold run present in the store.

    A run whose served review is in a FAILED state (AGR-06 enrichment error) is
    scored as an explicit abstention — in the denominator, zero credit — never
    silently skipped and never conflated with a clean result (AGR-07).
    """
    from .read import RunNotFound
    from . import read
    predictions: dict[str, list[PredictedMoment] | None] = {}
    for traj in gold_set.trajectories:
        try:
            view = read.get_review(store, traj.run_id)
            if view.get("review_status") == "failed":
                predictions[traj.run_id] = None  # abstention: incomplete review
                continue
            predictions[traj.run_id] = predicted_from_store(store, traj.run_id)
        except RunNotFound:
            continue
    return evaluate_set(predictions, gold_set, k=k, threshold=threshold)
