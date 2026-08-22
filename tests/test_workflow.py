"""Human review workflow — dispositions + Tier-1/2 feedback (spec §4.3.4, §4.13, §4.17).

Pure stdlib (no FastAPI), so these always run in CI. They assert the write model
records human review beside the immutable source without mutating it, and honours
the normative rules: handled requires a disposition, reopen preserves it, writes
are optimistic on ``base_version``, and feedback is idempotent on ``mutation_id``.
"""

import json
import os

import pytest

from agr import read, workflow
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _store_with(tmp_path, *names):
    store = Store(str(tmp_path / "store"))
    for name in names:
        analyze(_load(name), store)
    return store


RUN = "chess_best_move__seed42"


def test_default_workflow_is_unreviewed(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    wf = workflow.read_workflow(store, RUN)
    assert wf["review_progress"] == "unreviewed"
    assert wf["disposition"] is None
    assert wf["workflow_version"] == 0


def test_set_disposition_opens_and_records_revision(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    wf = workflow.set_workflow(store, RUN, actor="alice", base_version=0,
                               disposition="diagnosis_accepted")
    # Editing opens the review (§4.3.4) but does not mark it handled.
    assert wf["review_progress"] == "in_progress"
    assert wf["disposition"] == "diagnosis_accepted"
    assert wf["workflow_version"] == 1
    assert wf["revisions"][-1]["actor"] == "alice"
    # Persisted and re-read identically.
    assert workflow.read_workflow(store, RUN)["disposition"] == "diagnosis_accepted"


def test_handled_requires_a_disposition(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError):
        workflow.set_workflow(store, RUN, actor="a", base_version=0, progress="handled")
    # No partial write happened: still unreviewed at version 0.
    assert workflow.read_workflow(store, RUN)["workflow_version"] == 0


def test_reopen_preserves_prior_disposition(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    wf = workflow.set_workflow(store, RUN, actor="a", base_version=0,
                              disposition="corrected", progress="handled")
    assert wf["review_progress"] == "handled" and wf["handled_at"]
    reopened = workflow.set_workflow(store, RUN, actor="a", base_version=wf["workflow_version"],
                                    progress="in_progress")
    assert reopened["review_progress"] == "in_progress"
    assert reopened["disposition"] == "corrected"  # preserved, not cleared
    assert len(reopened["revisions"]) == 2


def test_optimistic_version_conflict(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    workflow.set_workflow(store, RUN, actor="a", base_version=0, disposition="no_action")
    with pytest.raises(workflow.VersionConflict) as exc:
        workflow.set_workflow(store, RUN, actor="b", base_version=0, disposition="needs_followup")
    assert exc.value.expected == 0 and exc.value.actual == 1
    assert exc.value.current["disposition"] == "no_action"  # newer state carried for recovery


def test_unknown_enums_rejected(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError):
        workflow.set_workflow(store, RUN, actor="a", base_version=0, disposition="bogus")


def test_feedback_is_idempotent_on_mutation_id(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    r1, wf = workflow.add_feedback(store, RUN, actor="a", mutation_id="m1",
                                   moment_id="mom", kind="agree")
    r2, _ = workflow.add_feedback(store, RUN, actor="a", mutation_id="m1",
                                  moment_id="mom", kind="agree")
    assert r1 == r2
    assert len(workflow.read_feedback(store, RUN)) == 1
    # Feedback is an edit → opens the review.
    assert wf["review_progress"] == "in_progress"


def test_quick_relabel_maps_to_controlled_taxonomy(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    rec, _ = workflow.add_feedback(store, RUN, actor="a", mutation_id="m2", moment_id="mom",
                                   kind="quick_relabel", replacement_group="skipped_verification")
    assert rec["replacement_label"] == "skipped_verification"
    assert rec["replacement_label"] in workflow.taxonomy.BEHAVIOUR_TAGS


def test_quick_relabel_requires_a_known_group(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError):
        workflow.add_feedback(store, RUN, actor="a", mutation_id="m3", moment_id="mom",
                              kind="quick_relabel", replacement_group="not_a_group")


def test_writes_never_mutate_the_immutable_source(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    before = read.get_source(store, RUN)
    workflow.set_workflow(store, RUN, actor="a", base_version=0,
                          disposition="corrected", progress="handled")
    workflow.add_feedback(store, RUN, actor="a", mutation_id="m", moment_id="mom", kind="agree")
    after = read.get_source(store, RUN)
    assert after["verified"] is True
    assert after["computed_source_hash"] == before["computed_source_hash"]


def test_review_view_includes_workflow_and_feedback(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    workflow.add_feedback(store, RUN, actor="a", mutation_id="m", moment_id="mom", kind="agree")
    rv = read.get_review(store, RUN)
    assert rv["workflow"]["review_progress"] == "in_progress"
    assert rv["feedback"][0]["kind"] == "agree"


def test_moments_carry_evidence_grade(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    rv = read.get_review(store, RUN)
    (m,) = rv["moments"]
    # Validated requirement_status fact over a complete capture → strong (§4.7.1).
    assert m["evidence_grade"] == "strong"
    # Grade is distinct from attribution (which caps causal language).
    assert m["attribution_ceiling"] == "dependency_linked"


def test_evidence_grade_helper_downgrades_without_validation(tmp_path):
    # A moment with no facts leans on interpretation → limited; unvalidated facts
    # over a complete capture → moderate; validated + complete → strong.
    assert read._evidence_grade({"facts": []}, "complete") == "limited"
    assert read._evidence_grade({"facts": [{"type": "x"}]}, "complete") == "moderate"
    assert read._evidence_grade({"facts": [{"type": "x"}]}, "partial") == "limited"
    assert read._evidence_grade({"facts": [{"validation": "passed"}]}, "partial") == "moderate"
    assert read._evidence_grade({"facts": [{"validation": "passed"}]}, "complete") == "strong"


def test_list_runs_carries_workflow_and_triage_signals(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    (row,) = read.list_runs(store)
    assert row["workflow"]["review_progress"] == "unreviewed"
    assert "review_mode" in row and "counts" in row
    assert set(row["counts"]) >= {"concern", "strength", "moments"}
    # Run-card metrics (§4.3.3): real step count + duration; cost absent, not faked.
    assert row["steps"] == 9
    assert row["duration_s"] == 252.0  # 10:30:00 → 10:34:12
    assert row["cost"] is None


# --- §4.13 Tier 3 — full structured correction -------------------------------


def _moment_id(store):
    return read.get_review(store, RUN)["moments"][0]["moment_id"]


def _correct(store, correction, **kw):
    return workflow.add_feedback(
        store, RUN, actor="rk", mutation_id=kw.pop("mutation_id", "c1"),
        moment_id=kw.pop("moment_id", None) or _moment_id(store),
        kind="structured_correction", correction=correction, **kw)


def test_structured_correction_is_a_new_annotation_revision(tmp_path):
    """Saving records a revision beside the generated moment, never over it."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    before = read.get_review(store, RUN)["moments"][0]
    record, _ = _correct(store, {"behaviour_tags": ["stopped_enumeration"], "decisive": True},
                         generated={"behaviour_tags": before.get("behaviour_tags", [])})

    assert record["kind"] == "structured_correction"
    assert record["correction_revision"] == 1 and record["supersedes"] is None
    assert record["corrected_fields"] == ["behaviour_tags", "decisive"]
    assert record["adjudication_status"] == "unadjudicated"
    assert record["taxonomy_version"]

    # A second correction supersedes the first rather than editing it.
    second, _ = _correct(store, {"consequence": "requirement_failed"},
                         mutation_id="c2", evidence_event_ids=["evt_008"])
    assert second["correction_revision"] == 2
    assert second["supersedes"] == record["mutation_id"]
    assert len(workflow.read_feedback(store, RUN)) == 2


def test_correction_overlays_the_accepted_view_and_keeps_the_generated_one(tmp_path):
    """§4.13: accepted interpretation by default, generated version inspectable."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    generated = read.get_review(store, RUN)["moments"][0]
    _correct(store, {"behaviour_tags": ["lost_requirement"],
                     "attribution_ceiling": "hypothesized"},
             generated={"behaviour_tags": generated.get("behaviour_tags", []),
                        "attribution_ceiling": generated.get("attribution_ceiling")})

    moment = read.get_review(store, RUN)["moments"][0]
    assert moment["corrected"] is True
    assert moment["behaviour_tags"] == ["lost_requirement"]        # accepted value
    assert moment["attribution_ceiling"] == "hypothesized"
    assert moment["generated"]["attribution_ceiling"] == generated["attribution_ceiling"]
    assert moment["correction"]["corrected_fields"] == ["attribution_ceiling", "behaviour_tags"]
    # Only lineage that exists is claimed (§4.13) — nothing downstream consumes it.
    assert moment["correction"]["lineage"] == ["Applied to this review"]


def test_correction_tokens_must_be_in_the_active_taxonomy(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    for bad in ({"behaviour_tags": ["gave_up"]},
                {"consequence": "the run failed"},
                {"root_cause_candidates": [{"locus": "vibes"}]},
                {"task_verifier_concern": {"assessment": "seems bad"}}):
        with pytest.raises(workflow.WorkflowError):
            _correct(store, bad, evidence_event_ids=["evt_008"])


def test_correcting_a_factual_claim_requires_evidence(tmp_path):
    """Interpretation is a reviewer's to give; a restatement of fact needs support."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError) as exc:
        _correct(store, {"consequence": "incorrect_state"})
    assert "evidence_event_ids" in str(exc.value)
    # The same correction is accepted once it cites its evidence.
    record, _ = _correct(store, {"consequence": "incorrect_state"},
                         evidence_event_ids=["evt_008"])
    assert record["evidence_event_ids"] == ["evt_008"]
    # An interpretive field needs none.
    assert _correct(store, {"better_action": "Write every winning move."},
                    mutation_id="c3")[0]["correction"]["better_action"]


def test_a_human_cannot_hand_themselves_replay_evidence(tmp_path):
    """`counterfactually_supported` needs a controlled branch this system cannot run."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError) as exc:
        _correct(store, {"attribution_ceiling": "counterfactually_supported"})
    assert "replay evidence" in str(exc.value)


def test_correction_rejects_unknown_fields_and_empty_edits(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(workflow.WorkflowError):
        _correct(store, {"severity": "high"})
    with pytest.raises(workflow.WorkflowError):
        _correct(store, {})


def test_correction_does_not_mutate_the_generated_record(tmp_path):
    """The store's review snapshot is untouched; the overlay is read-time only."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    capture = store.latest_capture_id(RUN)
    before = store.read_review_slot(RUN, capture, "deterministic")
    _correct(store, {"behaviour_tags": ["lost_requirement"]})
    assert store.read_review_slot(RUN, capture, "deterministic") == before
