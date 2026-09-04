"""HTTP-transport tests for the read API.

FastAPI (and the httpx-backed TestClient) is an optional dependency, so these
skip cleanly when the ``api`` extra is not installed — the read-model behaviour
itself is covered dependency-free in ``test_read.py``. Where installed, they
assert the routes map onto the read model and that a missing run is a 404.
"""

import json
import os

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # required by starlette's TestClient
from fastapi.testclient import TestClient  # noqa: E402

from agr.api import create_app  # noqa: E402
from agr.pipeline import analyze  # noqa: E402
from agr.store import Store  # noqa: E402
from agr.reviewer import DeterministicReviewer  # noqa: E402
from agr.model_reviewer import ScriptedReviewer  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _client(tmp_path, *names):
    store = Store(str(tmp_path / "store"))
    for name in names:
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            analyze(json.load(fh), store)
    return TestClient(create_app(str(tmp_path / "store"))), store


def test_healthz(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_the_spa(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # The SPA's fetch targets and mount points are present in the served asset.
    assert 'id="run-list"' in resp.text
    assert "Agent Game Review" in resp.text


def test_runs_endpoint(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.get("/runs")
    assert resp.status_code == 200
    (row,) = resp.json()
    assert row["run_id"] == "chess_best_move__seed42"
    assert row["outcome"]["status"] == "FAILED"


def test_review_endpoint(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.get("/runs/chess_best_move__seed42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"]["passed"] == 5
    assert body["contract"]["status"] == "human_confirmed"
    assert body["reviewer_key"] == "deterministic"
    assert body["available_reviews"] == ["deterministic"]


def test_forensic_endpoint(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.get("/runs/chess_best_move__seed42/forensic")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["steps"]) == 9
    assert "messages" in body["capability_badge"]


def test_source_endpoint_verifies_hash(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.get("/runs/chess_best_move__seed42/source")
    assert resp.status_code == 200
    assert resp.json()["verified"] is True


def test_unknown_run_is_404(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    for suffix in ("", "/forensic", "/source", "/reviews"):
        resp = client.get(f"/runs/missing_run{suffix}")
        assert resp.status_code == 404


# --- runs surface + write path (§4.3, §4.13, §4.17) --------------------------

RUN = "chess_best_move__seed42"


def test_sweep_endpoint(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json", "clean_pass.atif.json")
    body = client.get("/sweep").json()
    assert body["total_runs"] == 2
    assert body["by_outcome"]["FAILED"] >= 1


def test_queue_endpoint_filters_and_is_stable(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json", "clean_pass.atif.json")
    a = client.get("/queue?filter=failed&sort=triage").json()
    assert RUN in a["run_ids"]
    assert all("run_id" in r for r in a["runs"])
    b = client.get("/queue?filter=failed&sort=triage").json()
    assert a["queue_view_id"] == b["queue_view_id"]


def test_queue_bad_filter_is_400(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    assert client.get("/queue?filter=nope").status_code == 400


def test_review_endpoint_carries_workflow_and_feedback(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    body = client.get(f"/runs/{RUN}").json()
    assert body["workflow"]["review_progress"] == "unreviewed"
    assert body["feedback"] == []


def test_feedback_write_is_idempotent(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    payload = {"mutation_id": "m1", "moment_id": "mom", "kind": "agree", "actor": "me"}
    r1 = client.post(f"/runs/{RUN}/feedback", json=payload)
    r2 = client.post(f"/runs/{RUN}/feedback", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert len(client.get(f"/runs/{RUN}").json()["feedback"]) == 1
    # Feedback opens the review.
    assert r1.json()["workflow"]["review_progress"] == "in_progress"


def test_quick_relabel_maps_to_taxonomy(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.post(f"/runs/{RUN}/feedback", json={
        "mutation_id": "r1", "moment_id": "mom", "kind": "quick_relabel",
        "replacement_group": "premature_completion"})
    assert resp.status_code == 200
    assert resp.json()["feedback"]["replacement_label"] == "premature_submission"


def test_disposition_requires_a_value_to_mark_handled(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    v = client.get(f"/runs/{RUN}").json()["workflow"]["workflow_version"]
    resp = client.post(f"/runs/{RUN}/workflow", json={"base_version": v, "progress": "handled"})
    assert resp.status_code == 422


def test_disposition_handled_then_stale_write_conflicts(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    v = client.get(f"/runs/{RUN}").json()["workflow"]["workflow_version"]
    ok = client.post(f"/runs/{RUN}/workflow",
                     json={"base_version": v, "disposition": "corrected",
                           "progress": "handled", "reviewer": "me"})
    assert ok.status_code == 200 and ok.json()["review_progress"] == "handled"
    stale = client.post(f"/runs/{RUN}/workflow",
                        json={"base_version": 0, "disposition": "no_action"})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "version_conflict"
    assert detail["current"]["disposition"] == "corrected"


def test_next_unhandled_endpoint(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json", "clean_pass.atif.json")
    body = client.get(f"/runs/{RUN}/next?sort=triage").json()
    assert "next_unhandled" in body


def test_write_to_missing_run_is_404(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.post("/runs/missing/feedback",
                       json={"mutation_id": "z", "moment_id": "m", "kind": "agree"})
    assert resp.status_code == 404


def test_source_still_verifies_after_writes(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    v = client.get(f"/runs/{RUN}").json()["workflow"]["workflow_version"]
    client.post(f"/runs/{RUN}/workflow",
                json={"base_version": v, "disposition": "corrected", "progress": "handled"})
    client.post(f"/runs/{RUN}/feedback",
                json={"mutation_id": "m", "moment_id": "mom", "kind": "agree"})
    assert client.get(f"/runs/{RUN}/source").json()["verified"] is True


# --- §4.16 / §6.12 compare surface -----------------------------------------

def _enriched_payload(review):
    """Build a scripted-reviewer payload mirroring the deterministic moments + enrichment."""
    out = []
    for m in review["review_moments"]:
        if not m.get("selected"):
            continue
        out.append({
            "candidate_id": m["candidate_id"],
            "anchor_event_ids": m["anchor_event_ids"],
            "kind": m["kind"],
            "polarity": m["polarity"],
            "affected_checks": m["affected_checks"],
            # Stage G requires at least one validating fact — mirror the
            # deterministic facts so the enriched card stays grounded.
            "structured_facts": [f for f in m.get("validated_facts", []) if not f.get("validation") == "failed"],
            "behaviour_tags": ["failed_to_replan"],
            "better_action": "Re-check the build after fixing the import.",
            "root_cause_candidates": [{"locus": "agent_decision", "rationale": "did not retry"}],
        })
    return {"moments": out}


def test_reviews_endpoint_lists_one_for_deterministic_only(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    body = client.get(f"/runs/{RUN}/reviews").json()
    assert body["reviews"] == ["deterministic"]
    assert body["default_compare"] is None  # need >=2 for a comparison


def test_reviews_endpoint_lists_both_after_model_pass(tmp_path):
    client, store = _client(tmp_path, "chess_best_move.atif.json")
    det = client.get(f"/runs/{RUN}").json()
    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        analyze(json.load(fh), store, reviewer=ScriptedReviewer(
            _enriched_payload(det), source="model:test"))
    body = client.get(f"/runs/{RUN}/reviews").json()
    assert body["reviews"] == ["deterministic", "model:test"]
    assert body["default_compare"] == ["model:test", "deterministic"]


def test_compare_endpoint_pairs_matched_moments(tmp_path):
    client, store = _client(tmp_path, "chess_best_move.atif.json")
    det = client.get(f"/runs/{RUN}").json()
    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        analyze(json.load(fh), store, reviewer=ScriptedReviewer(
            _enriched_payload(det), source="model:test"))
    body = client.get(f"/runs/{RUN}/compare?left=deterministic&right=model:test").json()
    assert body["counts"]["matched"] >= 1
    assert body["counts"]["added"] == 0
    assert body["counts"]["removed"] == 0
    matched = [p for p in body["pairs"] if p["status"] == "matched"][0]
    assert matched["diffs"]["better_action"]["changed"] is True
    assert matched["set_diffs"]["behaviour_tags"]["added"] == ["failed_to_replan"]


def test_compare_endpoint_404_for_missing_review(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    resp = client.get(f"/runs/{RUN}/compare?left=deterministic&right=model:nope")
    assert resp.status_code == 404


def test_model_pass_does_not_destroy_baseline(tmp_path):
    """The §4.16 requirement: a model pass coexists with, not overwrites, the baseline."""
    client, store = _client(tmp_path, "chess_best_move.atif.json")
    before = client.get(f"/runs/{RUN}").json()["moments"]
    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        analyze(json.load(fh), store, reviewer=ScriptedReviewer(
            _enriched_payload(client.get(f"/runs/{RUN}").json()), source="model:test"))
    # The deterministic slot still has its moments (read via the compare endpoint,
    # which reads both slots directly rather than the default most-enriched view).
    cmp = client.get(f"/runs/{RUN}/compare?left=deterministic&right=model:test").json()
    assert cmp["counts"]["removed"] == 0  # baseline moments still present
    assert len(before) >= 1


# --- §4.16 matched version comparison ---------------------------------------

def _version_store(tmp_path):
    """Two configurations over the same three tasks, via the shared builder."""
    from test_versions import _slice  # noqa: PLC0415 - test-only helper
    store = _slice(tmp_path, tasks=("chess_best_move.atif.json",
                                    "contract_mismatch.atif.json",
                                    "tool_failure_recovery.atif.json"))
    return TestClient(create_app(str(tmp_path / "store"))), store


def test_configurations_endpoint_lists_selectable_sides(tmp_path):
    client, _ = _version_store(tmp_path)
    body = client.get("/configurations").json()["configurations"]
    assert {c["label"] for c in body} == {"sweep_141", "sweep_142"}
    assert all(c["selector"] for c in body)


def test_comparison_preview_returns_the_match_report(tmp_path):
    client, _ = _version_store(tmp_path)
    body = client.get("/comparisons/preview", params={
        "baseline": "sweep_id=sweep_141", "candidate": "sweep_id=sweep_142",
        "axis": "evaluation_harness"}).json()
    assert body["label"] == "matched"
    assert body["report"]["matched_run_pairs"] == 3
    assert body["pass_rate"]["baseline"]["denominator"] == 3


def test_comparison_preview_422s_on_a_bad_axis_or_selector(tmp_path):
    client, _ = _version_store(tmp_path)
    bad_axis = client.get("/comparisons/preview", params={
        "baseline": "sweep_id=sweep_141", "candidate": "sweep_id=sweep_142", "axis": "vibes"})
    assert bad_axis.status_code == 422
    bad_selector = client.get("/comparisons/preview", params={
        "baseline": "sweep_141", "candidate": "sweep_id=sweep_142", "axis": "model"})
    assert bad_selector.status_code == 422


def test_saved_comparison_is_addressable_by_its_id(tmp_path):
    client, _ = _version_store(tmp_path)
    saved = client.post("/comparisons", json={
        "baseline": {"sweep_id": "sweep_141"}, "candidate": {"sweep_id": "sweep_142"},
        "axis": "evaluation_harness", "name": "harness 1.8 → 1.9"}).json()
    cid = saved["comparison_id"]
    assert client.get("/comparisons").json()["comparisons"][0]["comparison_id"] == cid
    body = client.get(f"/comparisons/{cid}").json()
    assert body["name"] == "harness 1.8 → 1.9"
    assert body["saved"] is True
    assert client.get("/comparisons/comparison_nope").status_code == 404


def test_save_comparison_requires_both_sides(tmp_path):
    client, _ = _version_store(tmp_path)
    resp = client.post("/comparisons", json={"baseline": {"sweep_id": "sweep_141"}})
    assert resp.status_code == 422


# --- §4.13 Tier 3 + §4.21 instrumentation over HTTP --------------------------

def test_structured_correction_round_trips_through_the_api(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    moment_id = client.get(f"/runs/{RUN}").json()["moments"][0]["moment_id"]
    resp = client.post(f"/runs/{RUN}/feedback", json={
        "mutation_id": "corr-1", "moment_id": moment_id, "kind": "structured_correction",
        "actor": "rk", "correction": {"consequence": "incorrect_state", "decisive": False},
        "generated": {"consequence": None}, "evidence_event_ids": ["evt_008"]})
    assert resp.status_code == 200
    assert resp.json()["feedback"]["corrected_fields"] == ["consequence", "decisive"]
    moment = client.get(f"/runs/{RUN}").json()["moments"][0]
    assert moment["corrected"] is True and moment["consequence"] == "incorrect_state"


def test_structured_correction_validation_errors_are_422(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    moment_id = client.get(f"/runs/{RUN}").json()["moments"][0]["moment_id"]
    missing_evidence = client.post(f"/runs/{RUN}/feedback", json={
        "mutation_id": "corr-2", "moment_id": moment_id, "kind": "structured_correction",
        "correction": {"consequence": "incorrect_state"}})
    assert missing_evidence.status_code == 422
    assert "evidence_event_ids" in missing_evidence.json()["detail"]
    bad_token = client.post(f"/runs/{RUN}/feedback", json={
        "mutation_id": "corr-3", "moment_id": moment_id, "kind": "structured_correction",
        "correction": {"behaviour_tags": ["gave_up"]}})
    assert bad_token.status_code == 422


def test_events_endpoint_records_and_refuses(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    ok = client.post("/events", json={"events": [
        {"event": "run_opened", "session_id": "sess_a", "properties": {"run_id": RUN}},
        {"event": "moment_viewed", "session_id": "sess_a", "properties": {"run_id": RUN, "moment_id": "m1"}},
    ]})
    assert ok.json() == {"recorded": 2}
    leaky = client.post("/events", json={"events": [
        {"event": "moment_viewed", "session_id": "sess_a",
         "properties": {"moment_id": "the agent stopped after the first move"}}]})
    assert leaky.status_code == 422
    assert client.post("/events", json={"events": []}).status_code == 422


def test_metrics_endpoint_reports_measures_over_the_log(tmp_path):
    client, _ = _client(tmp_path, "chess_best_move.atif.json")
    client.post("/events", json={"events": [
        {"event": "sweep_opened", "session_id": "sess_a"},
        {"event": "run_opened", "session_id": "sess_a", "properties": {"run_id": RUN}},
    ]})
    body = client.get("/metrics").json()
    assert body["events"] == 2 and body["sessions"] == 1
    assert body["counts"]["run_opened"] == 1
    assert body["moment_views_opening_evidence"] is None  # never exercised, not zero


# --- Eval Lessons over HTTP (§4.11, §6.10, §13.2) ----------------------------


def _enriched_client(tmp_path):
    """A TestClient over a store whose chess run recommends an Eval Lesson."""
    from agr.model_packet import build_packet
    from agr.reviewer import ReviewerContext

    store = Store(str(tmp_path / "store"))
    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        source = json.load(fh)
    a = analyze(source, store)
    ctx = ReviewerContext(
        run_id=a.run_source.run_id, source_capture_id=a.run_source.source_capture_id,
        candidates=[c for r in a.detector_results if r.evaluated for c in r.candidates],
        slices=a.evidence_slices, checks=a.checks, events=a.events,
        recoveries=a.recoveries, contract=a.contract)
    pc = build_packet(ctx)[0]["deterministic_candidates"][0]
    analyze(source, store, reviewer=ScriptedReviewer({"moments": [{
        "candidate_id": pc["candidate_id"], "kind": "omission", "polarity": "negative",
        "anchor_event_ids": pc["anchor_event_ids"], "affected_checks": pc["affected_checks"],
        "structured_facts": pc["structured_facts"],
        "root_cause_candidates": [{"locus": "evaluation_harness", "rank": 1,
                                   "rationale": "Add a submission gate"}],
        "better_action": "Check the complete candidate set first",
        "eval_lesson_recommended": True}]}))
    return TestClient(create_app(str(tmp_path / "store")))


def test_lesson_lifecycle_and_experiment_over_http(tmp_path):
    client = _enriched_client(tmp_path)
    moment_id = client.get(f"/runs/{RUN}").json()["moments"][0]["moment_id"]

    created = client.post(f"/runs/{RUN}/lessons",
                          json={"mutation_id": "l1", "moment_id": moment_id, "actor": "rk"})
    assert created.status_code == 200
    lesson = created.json()["lesson"]
    assert lesson["status"] == "proposed"

    approved = client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}",
                           json={"base_version": 0, "status": "approved_for_test", "actor": "rk"})
    assert approved.json()["lesson"]["status"] == "approved_for_test"

    proposed = client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}/experiment",
                           json={"actor": "rk"})
    assert proposed.json()["lesson"]["experiment_proposal"]["status"] == "proposed"

    okd = client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}/experiment",
                      json={"action": "approve", "actor": "rk"})
    proposal = okd.json()["lesson"]["experiment_proposal"]
    assert proposal["status"] == "approved" and proposal["approved_by"] == "rk"

    # Served through the review the SPA reads.
    assert client.get(f"/runs/{RUN}").json()["lessons"][0]["experiment_proposal"]["status"] == "approved"


def test_lesson_write_is_optimistic_and_refuses_bad_input(tmp_path):
    client = _enriched_client(tmp_path)
    moment_id = client.get(f"/runs/{RUN}").json()["moments"][0]["moment_id"]
    lesson = client.post(f"/runs/{RUN}/lessons",
                         json={"mutation_id": "l1", "moment_id": moment_id}).json()["lesson"]
    # Stale base_version → 409 with the live lesson carried back.
    client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}",
                json={"base_version": 0, "status": "approved_for_test"})
    stale = client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}",
                        json={"base_version": 0, "status": "rejected"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["status"] == "approved_for_test"
    # An illegal transition (approved_for_test cannot revert to proposed) → 422.
    assert client.post(f"/runs/{RUN}/lessons/{lesson['lesson_id']}",
                       json={"base_version": 1, "status": "proposed"}).status_code == 422
