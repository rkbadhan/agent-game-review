"""Reviewer evaluation harness — metric correctness + baseline (§15.3, §20 M0).

Two layers of test. The first constructs predictions and gold by hand so each
metric is pinned in isolation (a missed critical drops recall, an extra card
drops precision, a duplicate card is redundant, an over-high ceiling overclaims,
a manufactured card on a clean pass miscalibrates). The second runs the *real*
deterministic baseline over the fixtures through the harness — the M0 accept
criterion "Precision@3 and Recall@3 can be computed" exercised on live output.
"""

import json
import os

from agr import gold
from agr import reviewer_eval as rev
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
GOLD = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "gold")


def _store(tmp_path):
    store = Store(str(tmp_path / "store"))
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".atif.json"):
            with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
                analyze(json.load(fh), store)
    return store


def _gold_traj(run_id, moments, no_moment=False):
    ann = gold.GoldAnnotation(annotator="t", moments=moments, no_decisive_moment=no_moment)
    return gold.GoldTrajectory(run_id=run_id, task_id="t", annotations=[ann])


def _moment(mid, steps, critical=False, ceiling="dependency_linked", checks=(), evidence=None):
    return gold.GoldMoment(
        moment_id=mid, anchor_type="omission", anchor_step_ids=list(steps),
        affected_checks=list(checks), critical=critical, attribution_ceiling=ceiling,
        evidence_span_step_ids=list(evidence if evidence is not None else steps),
    )


def _pred(mid, steps, ceiling="dependency_linked", checks=(), evidence=None):
    return rev.PredictedMoment(
        moment_id=mid, anchor_step_ids=list(steps), affected_checks=list(checks),
        attribution_ceiling=ceiling,
        evidence_span_step_ids=list(evidence if evidence is not None else steps),
    )


# --- metric correctness on constructed cases ---------------------------------


def test_perfect_prediction_scores_one():
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])
    ev = rev.evaluate_run([_pred("p", ["s8"])], traj)
    m = ev.to_dict()
    assert m["precision_at_3"] == 1.0
    assert m["recall_at_3"] == 1.0
    assert m["missed_critical_rate"] == 0.0
    assert m["evidence_span_precision"] == 1.0
    assert m["evidence_span_recall"] == 1.0


def test_missed_critical_moment_drops_recall():
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])
    ev = rev.evaluate_run([_pred("p", ["s2"])], traj)  # anchored elsewhere → no match
    m = ev.to_dict()
    assert m["recall_at_3"] == 0.0
    assert m["missed_critical_rate"] == 1.0


def test_spurious_extra_prediction_drops_precision():
    traj = _gold_traj("r", [_moment("g", ["s8"])])
    ev = rev.evaluate_run([_pred("p1", ["s8"]), _pred("p2", ["s2"])], traj)
    m = ev.to_dict()
    assert m["recall_at_3"] == 1.0
    assert m["precision_at_3"] == 0.5   # one of two cards matched


def test_two_cards_on_one_moment_are_redundant():
    traj = _gold_traj("r", [_moment("g", ["s8"])])
    ev = rev.evaluate_run([_pred("p1", ["s8"]), _pred("p2", ["s8"])], traj)
    m = ev.to_dict()
    assert m["recall_at_3"] == 1.0        # the moment is found
    assert m["redundant_card_rate"] == 0.5  # but one card is a duplicate
    assert ev.n_matched == 1 and ev.n_redundant == 1


def test_attribution_overclaim_is_flagged():
    traj = _gold_traj("r", [_moment("g", ["s8"], ceiling="dependency_linked")])
    ev = rev.evaluate_run([_pred("p", ["s8"], ceiling="counterfactually_supported")], traj)
    assert ev.to_dict()["attribution_overclaim_rate"] == 1.0


def test_within_ceiling_prediction_does_not_overclaim():
    traj = _gold_traj("r", [_moment("g", ["s8"], ceiling="dependency_linked")])
    ev = rev.evaluate_run([_pred("p", ["s8"], ceiling="hypothesized")], traj)
    assert ev.to_dict()["attribution_overclaim_rate"] == 0.0


def test_no_moment_calibration():
    traj = _gold_traj("r", [], no_moment=True)
    assert rev.evaluate_run([], traj).calibrated is True
    assert rev.evaluate_run([_pred("p", ["s3"])], traj).calibrated is False


def test_check_agreement_breaks_anchor_ties():
    # Two gold moments share the anchor; affected-check overlap decides the match.
    golds = [_moment("g_checkless", ["s5"]), _moment("g_c2", ["s5"], checks=["C2"])]
    traj = _gold_traj("r", golds)
    ev = rev.evaluate_run([_pred("p", ["s5"], checks=["C2"])], traj)
    matched = [x for x in ev.matches if x["status"] == "matched"]
    assert matched and matched[0]["gold"] == "g_c2"


def test_assignment_prefers_unmatched_gold():
    # Two predictions both overlap two co-anchored gold moments. A prediction
    # must take a *free* moment before being charged as redundant — otherwise
    # greedy scoring would match one moment twice and miss the other.
    golds = [_moment("g1", ["s5"]), _moment("g2", ["s5"])]
    traj = _gold_traj("r", golds)
    ev = rev.evaluate_run([_pred("p1", ["s5"]), _pred("p2", ["s5"])], traj)
    assert ev.n_matched == 2
    assert ev.n_redundant == 0
    assert ev.to_dict()["recall_at_3"] == 1.0


def test_genuine_redundancy_is_still_flagged():
    # Three cards, one moment: the two beyond the match are genuinely redundant.
    traj = _gold_traj("r", [_moment("g", ["s5"])])
    ev = rev.evaluate_run([_pred("p1", ["s5"]), _pred("p2", ["s5"]), _pred("p3", ["s5"])], traj)
    assert ev.n_matched == 1 and ev.n_redundant == 2


def test_threshold_requires_tighter_overlap():
    # Prediction shares one of the moment's two anchor steps → Jaccard 0.5.
    traj = _gold_traj("r", [_moment("g", ["s5", "s6"])])
    assert rev.evaluate_run([_pred("p", ["s5"])], traj, threshold=0.0).n_matched == 1
    assert rev.evaluate_run([_pred("p", ["s5"])], traj, threshold=0.6).n_matched == 0


def test_no_moment_run_reports_null_selection_metrics():
    d = rev.evaluate_run([], _gold_traj("r", [], no_moment=True)).to_dict()
    assert d["calibrated"] is True
    assert d["precision_at_3"] is None and d["recall_at_3"] is None
    assert d["missed_critical_rate"] is None


def test_undefined_denominator_is_null_not_zero():
    # No critical gold moment → missed-critical rate is undefined, not 0.0.
    ev = rev.evaluate_run([_pred("p", ["s8"])], _gold_traj("r", [_moment("g", ["s8"])]))
    assert ev.to_dict()["missed_critical_rate"] is None


def test_unmappable_anchor_stays_visible():
    # An anchor event id with no source-step mapping is kept as-is (not dropped),
    # so the prediction remains a visible, non-matching card.
    review = {"moments": [{"moment_id": "m", "anchor_event_ids": ["evt_x"],
                           "polarity": "negative", "affected_checks": []}]}
    forensic = {"steps": [{"step_id": "s1", "event_ids": ["evt_001"]}]}
    (pred,) = rev.moments_from_review(review, forensic)
    assert pred.anchor_step_ids == ["evt_x"]
    # It cannot match a gold moment anchored on real source steps.
    ev = rev.evaluate_run([pred], _gold_traj("r", [_moment("g", ["s1"])]))
    assert ev.n_matched == 0


def test_skipped_runs_are_recorded():
    gs = gold.GoldSet([_gold_traj("a", [_moment("g", ["s8"])]),
                       _gold_traj("b", [_moment("g", ["s8"])])])
    se = rev.evaluate_set({"a": [_pred("p", ["s8"])]}, gs)
    assert se.skipped_runs == ["b"]
    assert se.metrics()["n_skipped_runs"] == 1


# --- set-level aggregation ---------------------------------------------------


def test_set_metrics_are_micro_averaged():
    good = _gold_traj("a", [_moment("g", ["s8"])])
    missed = _gold_traj("b", [_moment("g", ["s8"], critical=True)])
    preds = {"a": [_pred("p", ["s8"])], "b": [_pred("p", ["s1"])]}
    gs = gold.GoldSet([good, missed])
    summary = rev.evaluate_set(preds, gs).metrics()
    assert summary["n_decisive_runs"] == 2
    assert summary["recall_at_3"] == 0.5     # 1 of 2 gold moments matched
    assert summary["missed_critical_rate"] == 1.0


# --- end-to-end baseline over the real fixtures ------------------------------


def test_reviewer_envelope_recovers_every_decisive_moment(tmp_path):
    store = _store(tmp_path)
    gs = gold.load_gold_set(GOLD)
    summary = rev.evaluate_store(store, gs).metrics()
    # The harness now scores the reviewer envelope's selected cards (§8.8–§8.10).
    # De-duplication removes the redundant cards that dragged the raw-detector
    # baseline to Precision@3 = 0.625 / redundant = 0.375, while every decisive
    # gold moment is still recovered — the measurable win a model Stage F must beat.
    # (AGR-05: the fetch_task gold annotation was reconciled — its negative
    # repetition moment claimed outputs were identical when they differ; see
    # the dated addendum in the gold file. Formal re-publication is AGR-07.)
    assert summary["recall_at_3"] == 1.0
    assert summary["redundant_card_rate"] == 0.0
    assert summary["precision_at_3"] == 1.0


def test_baseline_never_overclaims_attribution(tmp_path):
    store = _store(tmp_path)
    gs = gold.load_gold_set(GOLD)
    summary = rev.evaluate_store(store, gs).metrics()
    # The deterministic core caps at dependency_linked, so it can never exceed a
    # gold ceiling of the same level — the guardrail M4 acceptance depends on.
    assert summary["attribution_overclaim_rate"] == 0.0


def test_baseline_calibrates_the_clean_pass(tmp_path):
    store = _store(tmp_path)
    gs = gold.load_gold_set(GOLD)
    result = rev.evaluate_store(store, gs)
    clean = next(r for r in result.runs if r.run_id == "greeting_file__clean_pass")
    assert clean.no_moment_case and clean.calibrated is True
    assert result.metrics()["no_moment_calibration"] == 1.0


def test_moments_from_review_maps_anchors_to_source_steps(tmp_path):
    store = _store(tmp_path)
    preds = rev.predicted_from_store(store, "chess_best_move__seed42")
    (p,) = preds
    # The chess submission-omission candidate anchors at the final_submission
    # source step s8, not a derived event id.
    assert p.anchor_step_ids == ["s8"]
    assert p.attribution_ceiling == "dependency_linked"


# --- AGR-07: polarity-aware semantics, abstentions, fabrication ---------------


def _pred_p(mid, steps, polarity, ceiling="dependency_linked", checks=()):
    return rev.PredictedMoment(
        moment_id=mid, anchor_step_ids=list(steps), polarity=polarity,
        affected_checks=list(checks), attribution_ceiling=ceiling,
        evidence_span_step_ids=list(steps),
    )


def test_positive_prediction_for_negative_gold_cannot_score():
    """Acceptance (AGR-07): a positive prediction overlapping a negative gold
    moment cannot receive semantic precision or recall credit."""
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])  # negative gold
    ev = rev.evaluate_run([_pred_p("p", ["s8"], "positive")], traj)
    d = ev.to_dict()
    assert d["precision_at_3"] == 0.0
    assert d["recall_at_3"] == 0.0
    assert d["missed_critical_rate"] == 1.0  # the negative gold is still missed
    assert d["matches"][0]["status"] == "polarity_mismatch"


def test_polarity_agreement_still_matches():
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])
    ev = rev.evaluate_run([_pred_p("p", ["s8"], "negative")], traj)
    d = ev.to_dict()
    assert d["precision_at_3"] == 1.0 and d["recall_at_3"] == 1.0
    assert d["matches"][0]["status"] == "matched"


def test_quality_dimensions_scored_separately():
    """AGR-07: mechanism specificity and claim support are their own metrics,
    never folded into the match decision."""
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True, checks=["C1"],
                                    ceiling="hypothesized")])
    # Localises correctly, names the check, stays under the gold ceiling
    # (hypothesized is the strongest language this evidence licenses).
    ev = rev.evaluate_run([_pred_p("p", ["s8"], "negative", ceiling="hypothesized",
                                   checks=["C1"])], traj)
    match = ev.to_dict()["matches"][0]
    assert match["status"] == "matched"
    assert match["mechanism_specificity"] == "check_agreement"
    assert match["claim_support"] == "within_ceiling"
    m = ev.to_dict()
    assert m["mechanism_agreement_rate"] == 1.0
    assert m["claim_support_rate"] == 1.0
    assert m["attribution_overclaim_rate"] == 0.0


def test_topk_cut_respects_selection_ranking():
    """AGR-07: the top-k cut uses the reviewer's intended ranking, not an
    incidental timeline order — rank 1 survives over an earlier-but-lower card."""
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])
    early = _pred_p("p_early", ["s2"], "negative")   # timeline-first, rank 1
    late = _pred_p("p_ranked", ["s8"], "negative")   # rank 0 — reviewer's top pick
    late.selection_rank = 0
    early.selection_rank = 1
    ev = rev.evaluate_run([early, late], traj, k=1)
    assert ev.to_dict()["recall_at_3"] == 1.0  # the ranked-top card was the cut


def test_abstained_review_stays_in_the_denominator():
    """AGR-07: an incomplete review (model failure) is an explicit abstention —
    in the denominator with zero credit, never silently skipped."""
    traj = _gold_traj("r", [_moment("g", ["s8"], critical=True)])
    ev = rev.evaluate_run([], traj, abstained=True)
    d = ev.to_dict()
    assert d["abstained"] is True
    assert d["recall_at_3"] == 0.0
    assert d["missed_critical_rate"] == 1.0
    # The fabrication metric does NOT fire — the reviewer did not fabricate;
    # it failed. Calibration is not judged on a broken review.
    assert "n_fabricated" not in d or not d.get("no_moment_case")


def test_fabricated_findings_on_clean_pass_counted():
    traj = _gold_traj("r", [_moment("g", ["s1"])], no_moment=True)
    traj.annotations[0].no_decisive_moment = True
    traj.annotations[0].no_moment_rationale = "clean pass"
    ev = rev.evaluate_run([_pred_p("p", ["s2"], "negative"),
                           _pred_p("p2", ["s3"], "negative")], traj)
    d = ev.to_dict()
    assert d["no_moment_case"] is True
    assert d["calibrated"] is False
    assert d["n_fabricated"] == 2
    summary = rev.SetEval(runs=[ev]).metrics()
    assert summary["n_fabricated_on_clean_pass"] == 2
    assert summary["fabrication_on_clean_pass_rate"] == 1.0
