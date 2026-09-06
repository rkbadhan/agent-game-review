"""Read layer over the store: summaries, review, forensic view, source verify.

These tests are pure stdlib (no FastAPI) so they always run in CI. They build a
store by analysing fixtures, then assert the read model reflects the persisted
records and honours the Milestone 1 accept criteria: hashes verify, every
derived event traces to a source step, and unavailable evidence is marked.
"""

import json
import os

import pytest

from agr import read
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


# --- list_runs ---------------------------------------------------------------


def test_list_runs_empty_store(tmp_path):
    store = Store(str(tmp_path / "store"))
    assert read.list_runs(store) == []


def test_list_runs_summarises_each_run(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json", "contract_mismatch.atif.json")
    summaries = {s["run_id"]: s for s in read.list_runs(store)}

    assert set(summaries) == {"chess_best_move__seed42", "greeting_report__seed7"}

    chess = summaries["chess_best_move__seed42"]
    assert chess["task_id"] == "chess-best-move"
    assert chess["model"] == "demo-model-a"
    assert chess["outcome"] == {"status": "FAILED", "passed": 5, "total": 6}
    # A clean, human-confirmed contract is not watermarked.
    assert chess["contract"]["status"] == "human_confirmed"
    assert chess["contract"]["watermarked"] is False
    assert chess["watermark"] is None


def test_list_runs_flags_provisional_contract(tmp_path):
    store = _store_with(tmp_path, "contract_mismatch.atif.json")
    (summary,) = read.list_runs(store)
    assert summary["contract"]["watermarked"] is True
    assert "PROVISIONAL REVIEW" in summary["watermark"]


# --- get_review --------------------------------------------------------------


def test_get_review_bundles_the_deterministic_review(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    review = read.get_review(store, "chess_best_move__seed42")

    assert review["run"]["task_id"] == "chess-best-move"
    assert review["outcome"]["status"] == "FAILED"
    assert review["watermark"] is None
    assert {c["check_id"] for c in review["checks"]} == {"C1", "C2", "C3", "C4", "C5", "C6"}
    # The single failed-check slice is carried through.
    assert len(review["evidence_slices"]) == 1
    assert review["evidence_slices"][0]["check_id"] == "C3"
    assert review["signature"], "Task Ability Signature should be present"
    assert review["capabilities"]["filesystem"] == "checkpoint_only"


def test_get_review_unknown_run_raises(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    with pytest.raises(read.RunNotFound):
        read.get_review(store, "does_not_exist")


# --- task opportunities chapter (§4.6) --------------------------------------


def test_opportunity_rows_join_signature_for_status(tmp_path):
    """Each opportunity is projected into a §4.6 row whose status is derived from
    the deterministic Task Ability Signature — the two chapters must agree."""
    store = _store_with(tmp_path, "tool_failure_recovery.atif.json")
    review = read.get_review(store, "solve_task__recovered")
    rows = {r["ability"]: r for r in review["opportunity_rows"]}
    # Both windows were reached and deterministically evaluated → "measured", with
    # the observed behaviour lifted verbatim from the signature (no invention).
    assert rows["Recover from tool failure"]["status"] == "measured"
    assert rows["Recover from tool failure"]["observed_behaviour"] == "Strategy change resolved the failure"
    assert rows["Recover from tool failure"]["trigger"] == "A tool call failed"
    assert rows["Verify before submission"]["status"] == "measured"
    # The window carries its bounding events so the UI can resolve trace steps.
    assert rows["Recover from tool failure"]["start_event_id"]
    assert rows["Recover from tool failure"]["end_event_id"]


def test_opportunity_rows_never_invent_not_measured(tmp_path):
    """``not_measured`` (never occurred) is a signature state, not an opportunity
    row — every listed opportunity actually reached its window."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["opportunity_rows"], "the verify window should produce a row"
    assert all(r["status"] != "not_measured" for r in review["opportunity_rows"])


# --- final environment / artifact summary (§4.5) -----------------------------


def test_final_state_reports_the_observed_artifact(tmp_path):
    """The closing artifact state is restated from the capture, with the anchor
    events a reviewer needs to open the source step behind it."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    final = read.get_review(store, "chess_best_move__seed42")["final_state"]
    (artifact,) = final["artifacts"]
    assert artifact["path"] == "/solution.txt"
    assert artifact["declared"] is True
    assert artifact["state"] == "observed"
    assert artifact["content"] == "g2e4"
    assert artifact["observed_at_event_id"] == "evt_007"
    assert artifact["last_tool_event_id"] == "evt_006"
    # Observed before the agent submitted, and the run's last process exit is kept.
    assert artifact["observed_after_submission"] is False
    assert final["submission_event_id"] == "evt_008"
    assert final["last_exit"]["exit_code"] == "0"


def test_final_state_keeps_a_declared_artifact_that_was_never_observed(tmp_path):
    """A declared artifact with no observation stays a visible row (§4.5) rather
    than dropping out of the summary."""
    store = _store_with(tmp_path, "contract_mismatch.atif.json")
    final = read.get_review(store, "greeting_report__seed7")["final_state"]
    rows = {a["path"]: a for a in final["artifacts"]}
    assert rows["/out.txt"]["state"] == "observed"
    assert rows["/summary.txt"]["state"] == "never_observed"
    assert rows["/summary.txt"]["declared"] is True
    assert rows["/summary.txt"]["content"] is None
    assert rows["/summary.txt"]["last_tool_event_id"] is None


def test_final_state_carries_the_capabilities_that_govern_it(tmp_path):
    """An empty environment section must be readable as "not captured": the
    capability levels behind artifact and process evidence ride along."""
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    final = read.get_review(store, "chess_best_move__seed42")["final_state"]
    assert final["environment"] == []
    assert final["availability"]["process_state"] == {
        "capability": "process_state", "level": "unavailable", "state": "unavailable"}
    assert final["availability"]["filesystem"] == {
        "capability": "filesystem", "level": "checkpoint_only", "state": "partial"}


def test_final_state_reports_the_closing_environment_observations():
    """One row per environment event type, each the *last* one observed, ordered
    by sequence — and an artifact observed after submission is marked as such."""
    events = [
        {"event_id": "evt_001", "sequence": 1, "event_type": "environment_observation",
         "payload": {"content": "workdir clean"}},
        {"event_id": "evt_002", "sequence": 2, "event_type": "error_observed",
         "payload": {"summary": "disk pressure"}},
        {"event_id": "evt_003", "sequence": 3, "event_type": "environment_observation",
         "payload": {"content": "workdir has /out.txt"}},
        {"event_id": "evt_004", "sequence": 4, "event_type": "final_submission", "payload": {}},
        {"event_id": "evt_005", "sequence": 5, "event_type": "artifact_observation",
         "payload": {"artifact_path": "/out.txt", "content": "done"}},
    ]
    source = {"task": {"artifacts": [{"path": "/out.txt"}]}}
    final = read._final_state(events, source, {"filesystem": "complete", "process_state": "partial"})

    assert [(r["event_type"], r["event_id"]) for r in final["environment"]] == [
        ("error_observed", "evt_002"), ("environment_observation", "evt_003")]
    assert final["environment"][1]["detail"] == "workdir has /out.txt"
    assert final["artifacts"][0]["observed_after_submission"] is True
    assert final["last_exit"] is None


def test_final_state_marks_an_undeclared_artifact_and_truncates_long_content():
    """An artifact the task never declared is listed but flagged; an oversized
    observed value is cut with an explicit flag rather than silently."""
    long_value = "x" * (read._CONTENT_LIMIT + 40)
    events = [{"event_id": "evt_001", "sequence": 1, "event_type": "artifact_observation",
               "payload": {"artifact_path": "/scratch.txt", "content": long_value}}]
    final = read._final_state(events, {"task": {}}, {})
    (artifact,) = final["artifacts"]
    assert artifact["declared"] is False
    assert artifact["content_truncated"] is True
    assert len(artifact["content"]) == read._CONTENT_LIMIT
    # No capability profile at all still reports honestly, never "complete".
    assert final["availability"]["filesystem"]["state"] == "unavailable"


# --- guided view: phases + key moments -------------------------------------


def test_review_includes_phases(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    review = read.get_review(store, "chess_best_move__seed42")
    assert [p["label"] for p in review["phases"]] == [
        "Task intake", "Planning", "Execution", "Submission"]


def test_moments_project_the_submission_omission(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    review = read.get_review(store, "chess_best_move__seed42")
    moments = review["moments"]
    assert len(moments) == 1
    m = moments[0]
    assert m["detector"] == "unresolved_requirement_at_submission"
    assert m["polarity"] == "negative" and m["tag"] == "concern"
    assert m["affected_checks"] == ["C3"]
    assert m["sequence"] == 8              # anchored at final_submission
    assert m["phase_id"] == "ph_04"        # the submission phase
    # The reviewer envelope renders the card from a controlled template grounded
    # in the recomputed fact — a status statement, never an authored verdict.
    # AGR-03 timing rule: this fixture's C3 comes from the post-run verifier
    # result, which the agent never observed, so the card must not claim the
    # agent saw it failing at submission.
    assert "check C3 failed the run's final verifier" in m["summary"]
    assert "records no observation of this check" in m["summary"]
    assert "mistake" not in m["summary"].lower()
    # The envelope carries the attribution ceiling the evidence slice licenses.
    assert m["attribution_ceiling"] == "dependency_linked"


def test_positive_recovery_is_a_strength_moment(tmp_path):
    store = _store_with(tmp_path, "tool_failure_recovery.atif.json")
    review = read.get_review(store, "solve_task__recovered")
    strengths = [m for m in review["moments"] if m["tag"] == "strength"]
    assert strengths and strengths[0]["polarity"] == "positive"
    assert strengths[0]["kind"] == "recovery"


def test_passing_run_has_no_unresolved_requirement_moment(tmp_path):
    # A run whose checks all pass still surfaces behavioural moments (e.g. a
    # strength), but never a submission-omission moment tied to a failed check.
    store = _store_with(tmp_path, "tool_failure_recovery.atif.json")
    review = read.get_review(store, "solve_task__recovered")
    assert review["outcome"]["status"] == "PASSED"
    assert not [m for m in review["moments"]
                if m["detector"] == "unresolved_requirement_at_submission"]


def test_moments_are_ordered_by_timeline(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json", "stuck_retry.atif.json")
    review = read.get_review(store, "fetch_task__unchanged_retry")
    seqs = [m["sequence"] for m in review["moments"]]
    assert seqs == sorted(seqs)


def test_forensic_steps_carry_phase_ids(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    forensic = read.get_forensic(store, "chess_best_move__seed42")
    assert forensic["phases"], "forensic view exposes phases"
    for row in forensic["steps"]:
        assert row["phase_id"], f"{row['step_id']} has no phase"


# --- get_forensic ------------------------------------------------------------


def test_forensic_has_one_row_per_source_step_tracing_back(tmp_path):
    doc = _load("chess_best_move.atif.json")
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    forensic = read.get_forensic(store, "chess_best_move__seed42")

    source_step_ids = [s["step_id"] for s in doc["steps"]]
    assert [row["step_id"] for row in forensic["steps"]] == source_step_ids
    # Every step traces forward to at least one derived event (M1 traceability).
    for row in forensic["steps"]:
        assert row["event_ids"], f"{row['step_id']} has no derived event"


def test_forensic_routes_steps_onto_panels(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    forensic = read.get_forensic(store, "chess_best_move__seed42")
    panel_of = {row["step_id"]: row["panel"] for row in forensic["steps"]}

    assert panel_of["s2"] == "agent_message"      # model_output
    assert panel_of["s3"] == "tool_io"            # tool_call
    assert panel_of["s6"] == "tool_io"            # write_file tool_call
    assert panel_of["s7"] == "artifact"           # artifact_observation

    # Tool call vs result direction is projected onto the content.
    s4 = next(r for r in forensic["steps"] if r["step_id"] == "s4")
    assert s4["content"]["direction"] == "result"
    assert s4["content"]["exit_code"] == 0


def test_forensic_marks_unavailable_evidence(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    forensic = read.get_forensic(store, "chess_best_move__seed42")
    badge = forensic["capability_badge"]

    # Fully captured message evidence reads as complete; filesystem is only a
    # checkpoint (partial); process state was never captured (unavailable).
    assert badge["messages"]["state"] == "complete"
    assert badge["filesystem"] == {"level": "checkpoint_only", "state": "partial"}
    assert badge["process_state"]["state"] == "unavailable"

    # The artifact step inherits the filesystem capability's partial state.
    artifact_row = next(r for r in forensic["steps"] if r["panel"] == "artifact")
    assert artifact_row["availability"] == {
        "capability": "filesystem", "level": "checkpoint_only", "state": "partial"}


def test_forensic_exposes_verifier_panel(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    forensic = read.get_forensic(store, "chess_best_move__seed42")
    assert {c["check_id"] for c in forensic["verifier"]} == {
        "C1", "C2", "C3", "C4", "C5", "C6"}


# --- get_source --------------------------------------------------------------


def test_get_source_verifies_hash(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    src = read.get_source(store, "chess_best_move__seed42")
    assert src["verified"] is True
    assert src["recorded_source_hash"] == src["computed_source_hash"]
    assert src["source"]["run"]["task_id"] == "chess-best-move"


def test_get_source_detects_tampering(tmp_path):
    store = _store_with(tmp_path, "chess_best_move.atif.json")
    run_id = "chess_best_move__seed42"
    capture_id = store.latest_capture_id(run_id)
    # Tamper with the immutable source bytes directly on disk.
    path = os.path.join(store._capture_dir(run_id, capture_id), "source.json")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["steps"][0]["content"] = "TAMPERED"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)

    src = read.get_source(store, run_id)
    assert src["verified"] is False
    assert src["recorded_source_hash"] != src["computed_source_hash"]


# --- not_reviewable review mode (§4.15) --------------------------------------


def _store_incomplete(tmp_path, **caps):
    """A store whose chess run declares an ``incomplete`` capture, optionally with
    some capabilities forced unavailable, so the §4.15 not_reviewable state fires."""
    doc = _load("chess_best_move.atif.json")
    doc["capture_completeness"] = "incomplete"
    doc.setdefault("capabilities", {}).update(caps)
    store = Store(str(tmp_path / "store"))
    analyze(doc, store)
    return store


def test_incomplete_capture_is_not_reviewable(tmp_path):
    store = _store_incomplete(tmp_path, tool_calls="unavailable", verifier_code="unavailable")
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["review_mode"] == "not_reviewable"
    # §4.15: the missing capabilities are named explicitly, never failing silently.
    missing = review["missing_capabilities"]
    assert "tool_calls" in missing and "verifier_code" in missing
    # And the run summary reports the same state, so the queue can badge it.
    (row,) = read.list_runs(store)
    assert row["review_mode"] == "not_reviewable"


def test_partial_capture_is_still_reviewable(tmp_path):
    # ``partial`` is degraded-but-reviewable — only ``incomplete`` withholds review.
    doc = _load("chess_best_move.atif.json")
    doc["capture_completeness"] = "partial"
    store = Store(str(tmp_path / "store"))
    analyze(doc, store)
    assert read.get_review(store, "chess_best_move__seed42")["review_mode"] == "deterministic_only"


def test_review_mode_helper_precedence(tmp_path):
    # Evidence insufficiency wins over enrichment: an incomplete capture is
    # not_reviewable even if a card carries model enrichment.
    enriched = [{"enrichment_source": "model:x", "selected": True}]
    assert read._review_mode(enriched, "incomplete") == "not_reviewable"
    assert read._review_mode(enriched, "complete") == "model_enriched"
    assert read._review_mode([], "partial") == "deterministic_only"
