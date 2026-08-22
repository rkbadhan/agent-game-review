"""Eval Lessons + improvement experiments (spec §3.8, §4.11, §6.10, §13).

Pure stdlib (no FastAPI), so these always run in CI. They assert the lesson write
model records an accepted diagnosis beside the immutable source without mutating
it, honours the §6.10 lifecycle and the §4.17 optimistic-version contract, and
turns one accepted lesson into a human-approved experiment proposal (the
Milestone 5 accept criterion) — without inventing numeric thresholds (§13.2).
"""

import pytest

from agr import lessons, read
from agr.model_packet import build_packet
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.reviewer import ReviewerContext
from agr.store import Store

RUN = "chess_best_move__seed42"


def _enriched_store(tmp_path, load_fixture, **enr):
    """A store whose chess run has a model-enriched, lesson-recommending moment.

    Runs the pipeline once deterministically to read the real candidate id/facts,
    then again behind a ``ScriptedReviewer`` that recommends an Eval Lesson — the
    only state in which the §4.11 chapter offers to create one.
    """
    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture("chess_best_move.atif.json"), store)
    ctx = ReviewerContext(
        run_id=a.run_source.run_id, source_capture_id=a.run_source.source_capture_id,
        candidates=[c for r in a.detector_results if r.evaluated for c in r.candidates],
        slices=a.evidence_slices, checks=a.checks, events=a.events,
        recoveries=a.recoveries, contract=a.contract)
    packet, _ = build_packet(ctx)
    pc = packet["deterministic_candidates"][0]
    moment = {
        "candidate_id": pc["candidate_id"], "kind": "omission", "polarity": "negative",
        "anchor_event_ids": pc["anchor_event_ids"], "affected_checks": pc["affected_checks"],
        "structured_facts": pc["structured_facts"],
        "taxonomy_verdict": "Mistake", "behaviour_tags": ["stopped_enumeration"],
        "root_cause_candidates": [{"locus": "evaluation_harness", "rank": 1,
                                   "rationale": "Add an unresolved-requirement submission gate"}],
        "better_action": "Check the complete candidate set before submission",
        "eval_lesson_recommended": True,
    }
    moment.update(enr)
    analyze(load_fixture("chess_best_move.atif.json"), store,
            reviewer=ScriptedReviewer({"moments": [moment]}))
    return store


def _moment(store):
    return read.get_review(store, RUN)["moments"][0]


# --- the recommending moment reaches the read model --------------------------


def test_enriched_moment_carries_the_lesson_flag(tmp_path, load_fixture):
    m = _moment(_enriched_store(tmp_path, load_fixture))
    assert m["eval_lesson_recommended"] is True
    assert m["enrichment_source"] == "model:scripted"


def test_deterministic_moment_recommends_no_lesson(tmp_path, load_fixture):
    store = Store(str(tmp_path / "store"))
    analyze(load_fixture("chess_best_move.atif.json"), store)
    m = read.get_review(store, RUN)["moments"][0]
    assert m.get("eval_lesson_recommended") in (False, None)


# --- lesson body + creation --------------------------------------------------


def test_default_lesson_projects_the_moment(tmp_path, load_fixture):
    m = _moment(_enriched_store(tmp_path, load_fixture))
    body = lessons.default_lesson(m)
    assert body["status"] == "proposed"
    assert body["better_local_action"].startswith("Check the complete")
    assert body["systemic_intervention"] == {
        "layer": "evaluation_harness",
        "proposal": "Add an unresolved-requirement submission gate"}
    assert body["source_moments"] == [m["moment_id"]]
    assert body["behaviour"] == "stopped_enumeration"


def test_create_refuses_a_moment_with_no_recommendation(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    m = dict(_moment(store)); m["eval_lesson_recommended"] = False
    with pytest.raises(lessons.LessonError):
        lessons.create_lesson(store, RUN, moment=m, actor="rk", mutation_id="x")


def test_create_is_idempotent_per_moment(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    m = _moment(store)
    a = lessons.create_lesson(store, RUN, moment=m, actor="rk", mutation_id="m1")
    b = lessons.create_lesson(store, RUN, moment=m, actor="rk", mutation_id="m2")
    assert a["lesson_id"] == b["lesson_id"]
    assert len(lessons.read_lessons(store, RUN)) == 1


# --- §6.10 lifecycle ---------------------------------------------------------


def _lesson(store, load_fixture=None):
    return lessons.create_lesson(store, RUN, moment=_moment(store), actor="rk", mutation_id="c")


def test_approve_advances_status_and_records_a_revision(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    out = lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                    actor="rk", status="approved_for_test")
    assert out["status"] == "approved_for_test"
    assert out["lesson_version"] == 1
    assert out["revisions"][-1]["prior_status"] == "proposed"


def test_illegal_transition_is_refused(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    # proposed cannot jump straight to validated.
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                  actor="rk", status="validated")
    # A rejected lesson is terminal.
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="rk", status="rejected")
    reread = lessons.read_lessons(store, RUN)[0]
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"],
                                  base_version=reread["lesson_version"], actor="rk",
                                  status="approved_for_test")


def test_optimistic_version_conflict(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="a", status="approved_for_test")
    with pytest.raises(lessons.VersionConflict) as exc:
        lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                  actor="b", status="rejected")
    assert exc.value.expected == 0 and exc.value.actual == 1
    assert exc.value.current["status"] == "approved_for_test"


def test_edit_applies_reviewer_fields_and_guards_the_layer(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    out = lessons.set_lesson_status(
        store, RUN, ln["lesson_id"], base_version=0, actor="rk",
        edits={"generalization_boundary": "exhaustive-requirement tasks",
               "possible_side_effects": ["additional tokens"],
               "regression_slice": "exhaustive_requirements_v1"})
    assert out["generalization_boundary"].startswith("exhaustive")
    assert out["possible_side_effects"] == ["additional tokens"]
    assert out["status"] == "proposed"  # editing does not advance status
    # An intervention layer outside the taxonomy is refused.
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"],
                                  base_version=out["lesson_version"], actor="rk",
                                  edits={"systemic_intervention": {"layer": "vibes"}})


def test_edit_refuses_malformed_shapes(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    # A string where the intervention object belongs must not crash the write path.
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                  actor="rk", edits={"systemic_intervention": "vibes"})
    # A bare string must not explode into a list of characters.
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                  actor="rk", edits={"possible_side_effects": "tokens"})
    # Free-text fields reject non-strings.
    with pytest.raises(lessons.LessonError):
        lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                                  actor="rk", edits={"regression_slice": ["v1"]})
    # None of the refused writes touched the lesson.
    assert lessons.read_lessons(store, RUN)[0]["lesson_version"] == 0


# --- §13.2 experiment proposal + the M5 accept criterion ---------------------


def test_experiment_needs_an_approved_lesson(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)  # still proposed
    with pytest.raises(lessons.LessonError):
        lessons.propose_experiment(store, RUN, ln["lesson_id"], actor="rk")


def test_proposal_carries_no_fabricated_thresholds(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="rk", status="approved_for_test")
    out = lessons.propose_experiment(store, RUN, ln["lesson_id"], actor="rk")
    p = out["experiment_proposal"]
    assert p["status"] == "proposed"
    assert p["proposed_change"]["layer"] == "evaluation_harness"
    assert p["primary_measure"] == "stopped_enumeration"
    # Honest denominators: cross-run opportunity aggregation is §12.4, not built.
    assert p["observation"]["opportunities"] is None
    assert p["observation"]["scope"] == "single_run"
    # No numeric success threshold anywhere in the proposal (§13.2).
    flat = repr(p)
    assert "threshold" not in flat.lower()


def test_experiment_writes_are_versioned_and_audited(tmp_path, load_fixture):
    """Proposal generation and human approval mutate the lesson like any other
    write: the optimistic version advances and the revisions log records it."""
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="rk", status="approved_for_test")
    out = lessons.propose_experiment(store, RUN, ln["lesson_id"], actor="rk")
    assert out["lesson_version"] == 2
    assert out["revisions"][-1]["edited_fields"] == ["experiment_proposal"]
    out = lessons.approve_experiment(store, RUN, ln["lesson_id"], actor="rk")
    assert out["lesson_version"] == 3
    assert out["revisions"][-1]["edited_fields"] == ["experiment_proposal"]
    assert out["revisions"][-1]["actor"] == "rk"


def test_approve_experiment_is_idempotent(tmp_path, load_fixture):
    """A repeated approval keeps the first approver and timestamp — a retried
    click never re-stamps a human decision."""
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="rk", status="approved_for_test")
    lessons.propose_experiment(store, RUN, ln["lesson_id"], actor="rk")
    first = lessons.approve_experiment(store, RUN, ln["lesson_id"], actor="rk",
                                       now="2025-01-01T00:00:00Z")
    second = lessons.approve_experiment(store, RUN, ln["lesson_id"], actor="other",
                                        now="2025-06-01T00:00:00Z")
    p = second["experiment_proposal"]
    assert p["approved_by"] == "rk"
    assert p["approved_at"] == "2025-01-01T00:00:00Z"
    assert second["lesson_version"] == first["lesson_version"]


def test_one_accepted_lesson_produces_a_human_approved_experiment(tmp_path, load_fixture):
    """The Milestone 5 accept criterion, provable from storage."""
    store = _enriched_store(tmp_path, load_fixture)
    ln = _lesson(store)
    lessons.set_lesson_status(store, RUN, ln["lesson_id"], base_version=0,
                              actor="rk", status="approved_for_test")
    lessons.propose_experiment(store, RUN, ln["lesson_id"], actor="rk")
    lessons.approve_experiment(store, RUN, ln["lesson_id"], actor="rk")

    persisted = lessons.read_lessons(store, RUN)[0]
    assert persisted["status"] == "approved_for_test"
    proposal = persisted["experiment_proposal"]
    assert proposal["status"] == "approved"
    assert proposal["approved_by"] == "rk" and proposal["approved_at"]
    # And it is served through the read model the UI consumes.
    assert read.get_review(store, RUN)["lessons"][0]["experiment_proposal"]["status"] == "approved"


def test_lessons_are_written_beside_the_immutable_source(tmp_path, load_fixture):
    store = _enriched_store(tmp_path, load_fixture)
    before = read.get_source(store, RUN)
    _lesson(store)
    after = read.get_source(store, RUN)
    assert after["verified"] is True
    assert after["computed_source_hash"] == before["computed_source_hash"]
