"""Reviewer gold-set schema, loader, validator, and disagreement report (§15.2).

Pure stdlib. These tests pin the M0 accept criteria: "experts can consistently
use the schema" (the validator rejects out-of-vocabulary and ungrounded labels)
and "disagreements are adjudicable" (double-labelled trajectories carry an
adjudication and surface disagreement rather than erasing it).
"""

import copy
import json
import os

import pytest

from agr import gold
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
GOLD = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "gold")


def _load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _source_steps(tmp_path):
    """Build a store from every fixture and map run_id -> source step ids."""
    store = Store(str(tmp_path / "store"))
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".atif.json"):
            analyze(_load_fixture(name), store)
    steps = {}
    for t in gold.load_gold_set(GOLD).trajectories:
        cid = store.latest_capture_id(t.run_id)
        doc = store.read_source(t.run_id, cid)
        steps[t.run_id] = {s["step_id"] for s in doc["steps"]}
    return steps


# --- loading + validation ----------------------------------------------------


def test_load_gold_set_reads_every_trajectory():
    gs = gold.load_gold_set(GOLD)
    assert set(gs.run_ids()) == {
        "chess_best_move__seed42", "build_task__ignored_failure",
        "solve_task__recovered", "fetch_task__unchanged_retry",
        "greeting_report__seed7", "greeting_file__clean_pass",
    }


def test_gold_set_validates_against_sources(tmp_path):
    # Every anchor / evidence step id names a real step in the run's source.
    gold.load_gold_set(GOLD).validate(_source_steps(tmp_path))


def test_gold_set_schema_only_validation():
    # Schema-only validation (no source map) still passes.
    gold.load_gold_set(GOLD).validate()


def test_sampling_covers_required_categories():
    gs = gold.load_gold_set(GOLD)
    kinds = set()
    no_moment = False
    for t in gs.trajectories:
        adj = t.adjudicated_annotation()
        if adj.no_decisive_moment:
            no_moment = True
        kinds.update(m.anchor_type for m in adj.moments)
    # omission, external, recovery, decision, and a no-moment calibration case.
    assert {"omission", "external", "recovery", "decision"} <= kinds
    assert no_moment


# --- validator rejects out-of-schema records ---------------------------------


def _chess_dict():
    with open(os.path.join(GOLD, "chess_best_move__seed42.gold.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _one_bad(mutate):
    d = _chess_dict()
    mutate(d)
    return gold.GoldSet([gold.GoldTrajectory.from_dict(d)])


def test_validate_rejects_unknown_behaviour_tag():
    gs = _one_bad(lambda d: d["annotations"][0]["moments"][0]["behaviour_tags"].append("nonsense_tag"))
    with pytest.raises(gold.GoldValidationError, match="behaviour tag"):
        gs.validate()


def test_validate_rejects_bad_attribution_level():
    gs = _one_bad(lambda d: d["annotations"][0]["moments"][0].__setitem__(
        "attribution_ceiling", "definitely_certain"))
    with pytest.raises(gold.GoldValidationError, match="attribution_ceiling"):
        gs.validate()


def test_validate_rejects_unknown_root_cause_locus():
    gs = _one_bad(lambda d: d["annotations"][0]["moments"][0]["root_cause_candidates"][0].__setitem__(
        "locus", "cosmic_rays"))
    with pytest.raises(gold.GoldValidationError, match="locus"):
        gs.validate()


def test_validate_rejects_step_id_absent_from_source(tmp_path):
    gs = _one_bad(lambda d: d["annotations"][0]["moments"][0]["anchor_step_ids"].append("s999"))
    with pytest.raises(gold.GoldValidationError, match="not present in the source"):
        gs.validate(_source_steps(tmp_path))


def test_validate_rejects_no_moment_with_moments():
    d = _chess_dict()
    d["annotations"][0]["no_decisive_moment"] = True  # but moments are present
    gs = gold.GoldSet([gold.GoldTrajectory.from_dict(d)])
    with pytest.raises(gold.GoldValidationError, match="no_decisive_moment"):
        gs.validate()


# --- annotation provenance (AGR-01) ------------------------------------------


def test_legacy_records_default_to_human_unfrozen_unbatched():
    # 0.1-era fixtures predate label_source/label_batch/frozen entirely.
    gs = gold.load_gold_set(GOLD)
    for t in gs.trajectories:
        assert t.label_source == "human"
        assert t.label_batch is None
        assert t.frozen is False
        # Not independent gold yet: nothing here has been frozen.
        assert not t.is_independent_human_gold()


def test_unfrozen_run_ids_reports_everything_by_default():
    gs = gold.load_gold_set(GOLD)
    assert set(gs.unfrozen_run_ids()) == set(gs.run_ids())
    assert gs.independent_human_gold().run_ids() == []


def test_frozen_human_trajectory_is_independent_gold():
    d = _chess_dict()
    d["frozen"] = True
    traj = gold.GoldTrajectory.from_dict(d)
    assert traj.is_independent_human_gold()
    gs = gold.GoldSet([traj])
    assert gs.unfrozen_run_ids() == []
    assert gs.independent_human_gold().run_ids() == [traj.run_id]


def test_frozen_model_draft_is_not_independent_gold():
    d = _chess_dict()
    d["frozen"] = True
    d["label_source"] = "model_draft"
    traj = gold.GoldTrajectory.from_dict(d)
    assert not traj.is_independent_human_gold()
    assert gold.GoldSet([traj]).independent_human_gold().run_ids() == []


def test_validate_rejects_unknown_label_source():
    d = _chess_dict()
    d["label_source"] = "vibes"
    gs = gold.GoldSet([gold.GoldTrajectory.from_dict(d)])
    with pytest.raises(gold.GoldValidationError, match="label_source"):
        gs.validate()


def test_round_trip_preserves_provenance_fields():
    d = _chess_dict()
    d["frozen"] = True
    d["label_batch"] = "2026-09-batch-1"
    traj = gold.GoldTrajectory.from_dict(d)
    again = gold.GoldTrajectory.from_dict(copy.deepcopy(traj.to_dict()))
    assert again.frozen is True
    assert again.label_batch == "2026-09-batch-1"
    assert again.label_source == "human"


# --- adjudication ------------------------------------------------------------


def test_single_annotation_is_its_own_adjudication():
    gs = gold.load_gold_set(GOLD)
    solo = gs.by_run("solve_task__recovered")
    assert not solo.double_labelled
    assert solo.adjudicated_annotation().annotator == "annotator_a"


def test_double_labelled_without_adjudication_is_an_error():
    d = _chess_dict()
    d.pop("adjudicated")  # two annotations, no resolved truth
    traj = gold.GoldTrajectory.from_dict(d)
    with pytest.raises(gold.GoldValidationError, match="adjudicated"):
        traj.adjudicated_annotation()
    with pytest.raises(gold.GoldValidationError, match="adjudicated"):
        gold.GoldSet([traj]).validate()


# --- disagreement report -----------------------------------------------------


def test_disagreement_report_surfaces_seeded_disagreement():
    gs = gold.load_gold_set(GOLD)
    report = gold.disagreement_report(gs.by_run("chess_best_move__seed42"))
    assert report["double_labelled"] is True
    assert report["agreement"] is False
    # annotator_b raised an extra low-value moment that a did not.
    assert report["b_only_moments"] == ["m_premature_commitment"]
    # and they disagree on the top root-cause locus of the shared moment.
    shared = report["matched_moments"][0]
    fields = {d["field"] for d in shared["disagreements"]}
    assert "top_root_cause_locus" in fields


def test_disagreement_report_on_single_annotator_is_trivial():
    gs = gold.load_gold_set(GOLD)
    report = gold.disagreement_report(gs.by_run("solve_task__recovered"))
    assert report["double_labelled"] is False


def test_round_trip_preserves_records():
    gs = gold.load_gold_set(GOLD)
    for t in gs.trajectories:
        again = gold.GoldTrajectory.from_dict(copy.deepcopy(t.to_dict()))
        assert again.run_id == t.run_id
        assert len(again.annotations) == len(t.annotations)
