"""GR-1: the review ends in exactly one status, and the screen shows it.

Six states, exactly two of them successful outcomes (``moments_found`` and
``no_decisive_moment``). A failed, unconfigured or early-stopped review is never
rendered as abstention. These tests pin the mapping, the served-reviewer scoping
of ``incomplete``, and the counts shown beside the status.
"""

import json
import os

from agr import read
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.read import (
    REVIEW_STATUSES,
    SUCCESSFUL_REVIEW_STATUSES,
    _review_counts,
    _review_status,
)
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
FIXTURE = "chess_best_move.atif.json"
MODEL = "model:test"


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


# --- the state model ---------------------------------------------------------


def test_status_set_is_exactly_six_and_two_successes():
    assert len(REVIEW_STATUSES) == 6
    assert set(REVIEW_STATUSES) == {
        "moments_found", "no_decisive_moment", "all_proposals_rejected",
        "not_configured", "review_failed", "incomplete",
    }
    assert SUCCESSFUL_REVIEW_STATUSES == {"moments_found", "no_decisive_moment"}
    assert SUCCESSFUL_REVIEW_STATUSES <= set(REVIEW_STATUSES)


def test_status_mapping_for_a_served_model_review():
    assert _review_status([{"selected": True}], [], MODEL) == "moments_found"
    assert _review_status([], [], MODEL) == "no_decisive_moment"
    assert _review_status([{"selected": False}], [], MODEL) == "all_proposals_rejected"


def test_deterministic_baseline_is_not_configured_not_abstention():
    # A deterministic moment is still shown, but no model review ran — that is
    # "not configured", never "no decisive moment".
    assert _review_status([{"selected": True}], [], "deterministic") == "not_configured"
    assert _review_status(None, [], None) == "not_configured"


def test_legacy_envelope_with_model_enrichment_counts_as_model_served():
    moment = {"selected": True, "enrichment_source": "model:legacy"}
    assert _review_status([moment], [], None) == "moments_found"


def test_error_wins_over_empty_and_selection():
    errors = [{"error_type": "ModelOutputError", "reviewer_key": MODEL}]
    assert _review_status([], [], MODEL, None) == "no_decisive_moment"
    assert _review_status([], errors, MODEL) == "review_failed"
    assert _review_status([{"selected": True}], errors, MODEL) == "review_failed"


def test_incomplete_wins_and_is_scoped_to_the_served_reviewer():
    telemetry = {"incomplete": True, "reason": "budget", "reviewer_key": MODEL}
    assert _review_status([], [], MODEL, telemetry) == "incomplete"
    # Even a completed-looking result is superseded by "stopped early".
    assert _review_status([{"selected": True}], [{"error_type": "E"}], MODEL, telemetry) == "incomplete"
    # Another reviewer's incomplete marker never contaminates this slot.
    other = {"incomplete": True, "reviewer_key": "model:other"}
    assert _review_status([{"selected": True}], [], MODEL, other) == "moments_found"


# --- the counts shown beside the status --------------------------------------


def test_counts_use_the_served_reviewers_own_telemetry():
    moments = [{"selected": True}, {"selected": False}]
    telemetry = {
        "reviewer_key": MODEL, "proposed": 4, "selected": 1,
        "rejections": {"fact_validation": 2, "quota": 1},
    }
    counts = _review_counts(moments, telemetry, MODEL)
    assert counts["proposed"] == 4
    assert counts["selected"] == 1
    assert counts["rejected"] == 3
    assert counts["rejections"] == {"fact_validation": 2, "quota": 1}


def test_counts_for_another_reviewer_derive_from_the_served_moments():
    moments = [{"selected": True}, {"selected": False}]
    telemetry = {"reviewer_key": "model:other", "proposed": 99, "selected": 99}
    counts = _review_counts(moments, telemetry, MODEL)
    assert counts["proposed"] == 2
    assert counts["selected"] == 1
    assert counts["rejected"] == 1


# --- integration over a real fixture -----------------------------------------


def test_deterministic_only_run_reports_not_configured(tmp_path):
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store)
    view = read.get_review(store, analysis.run_source.run_id)
    assert view["served_reviewer_key"] == "deterministic"
    assert view["review_status"] == "not_configured"
    assert view["review_status_success"] is False
    assert view["review_counts"]["proposed"] >= 1


def test_model_review_with_no_moments_reports_no_decisive_moment(tmp_path):
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=ScriptedReviewer({"moments": []}, source="model:probe"))
    view = read.get_review(store, analysis.run_source.run_id)
    assert view["served_reviewer_key"] == "model:probe"
    assert view["review_status"] == "no_decisive_moment"
    assert view["review_status_success"] is True
    assert view["moments"] == []


def test_model_review_with_all_proposals_rejected_reports_the_count(tmp_path):
    payload = {"moments": [{
        "candidate_id": "sem_1", "kind": "omission",
        "anchor_event_ids": ["evt_does_not_exist"], "structured_facts": [],
    }]}
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=ScriptedReviewer(payload, source="model:probe"))
    view = read.get_review(store, analysis.run_source.run_id)
    assert view["served_reviewer_key"] == "model:probe"
    assert view["review_status"] == "all_proposals_rejected"
    assert view["review_status_success"] is False
    assert view["review_counts"]["rejected"] >= 1
    assert not any(m.get("selected") for m in view["review_moments"])


def test_model_review_with_a_valid_moment_reports_moments_found(tmp_path):
    # The scripted payload mirrors the deterministic selected moments through
    # the model slot, so the same facts recompute and one moment is selected.
    scratch = Store(str(tmp_path / "scratch"))
    baseline = analyze(_load(FIXTURE), scratch)
    seed = read.get_review(scratch, baseline.run_source.run_id)["review_moments"]
    payload = {"moments": [{
        "candidate_id": m["candidate_id"],
        "anchor_event_ids": m["anchor_event_ids"],
        "kind": m["kind"],
        "polarity": m["polarity"],
        "affected_checks": m["affected_checks"],
        "structured_facts": [{k: v for k, v in f.items()
                              if k not in ("validation", "recomputed")}
                             for f in m.get("validated_facts", [])],
    } for m in seed if m.get("selected")]}

    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=ScriptedReviewer(payload, source="model:probe"))
    view = read.get_review(store, analysis.run_source.run_id)
    assert view["served_reviewer_key"] == "model:probe"
    assert view["review_status"] == "moments_found"
    assert view["review_status_success"] is True
    assert view["review_counts"]["selected"] >= 1


# --- the UI surfaces (static pin; the browser gate exercises them live) -------


def test_ui_shows_the_status_and_a_clear_no_model_review_label():
    js_dir = os.path.join(os.path.dirname(__file__), "..", "agr", "static", "js")
    with open(os.path.join(js_dir, "render.js"), encoding="utf-8") as fh:
        render = fh.read()
    with open(os.path.join(js_dir, "ui-utils.js"), encoding="utf-8") as fh:
        ui = fh.read()
    # The single status is rendered in the shell, and the no-model case gets its
    # own clear label instead of reading as abstention.
    assert 'vocab("review_status"' in render
    assert '"No model review"' in render
    assert 'review_status === "not_configured"' in render
    # Verifier-optional runs state task success as unverified in those words.
    assert "Task success unverified" in ui
