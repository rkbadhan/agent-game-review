"""Deterministic analysis: events, checks, evidence slices, detectors."""

import json
import os

from agr.pipeline import analyze
from agr.store import Store

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures", "chess_best_move.atif.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def analyze_fixture(tmp_path):
    store = Store(str(tmp_path / "store"))
    return analyze(load_fixture(), store)


def test_outcome_matches_spec_card(tmp_path):
    a = analyze_fixture(tmp_path)
    assert a.outcome["status"] == "FAILED"
    assert a.outcome["passed"] == 5
    assert a.outcome["total"] == 6
    assert a.outcome["failed_checks"] == ["C3"]


def test_every_derived_event_traces_to_source_steps(tmp_path):
    doc = load_fixture()
    step_ids = {s["step_id"] for s in doc["steps"]}
    a = analyze_fixture(tmp_path)

    assert len(a.events) == len(doc["steps"])
    for event in a.events:
        assert event.source_step_ids, f"{event.event_id} has no source step"
        assert all(sid in step_ids for sid in event.source_step_ids)
    # Submission and result events are present for downstream stages.
    types = {e.event_type for e in a.events}
    assert "final_submission" in types
    assert "tool_result" in types
    assert "artifact_observation" in types


def test_failed_check_slice_is_dependency_linked(tmp_path):
    a = analyze_fixture(tmp_path)
    assert len(a.evidence_slices) == 1
    s = a.evidence_slices[0]
    assert s.check_id == "C3"
    assert s.contract_item_ids == ["R3"]
    # The missing move appears in a tool result (s4 -> evt_004) and the observed
    # set in the artifact (s7 -> evt_007); a dependency path exists.
    assert s.attribution_ceiling == "dependency_linked"
    assert "evt_004" in s.event_ids
    assert "evt_007" in s.event_ids


def test_deterministic_core_never_exceeds_dependency_linked(tmp_path):
    a = analyze_fixture(tmp_path)
    for s in a.evidence_slices:
        assert s.attribution_ceiling in {"hypothesized", "dependency_linked"}


def test_unresolved_requirement_detector_fires(tmp_path):
    a = analyze_fixture(tmp_path)
    by_name = {r.detector: r for r in a.detector_results}

    d = by_name["unresolved_requirement_at_submission"]
    assert d.evaluated is True
    assert len(d.candidates) == 1
    cand = d.candidates[0]
    assert cand.affected_checks == ["C3"]
    assert cand.affected_contract_items == ["R3"]
    assert cand.anchor_event_ids == ["evt_008"]  # final_submission


def test_capability_gated_detector_reports_not_evaluated(tmp_path):
    a = analyze_fixture(tmp_path)
    by_name = {r.detector: r for r in a.detector_results}

    d = by_name["compaction_requirement_loss"]
    assert d.evaluated is False  # pre_post_compaction_context is only "partial"
    assert "pre_post_compaction_context" in d.unmet_capabilities
    assert d.candidates == []


def test_analysis_persists_derived_records(tmp_path):
    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture(), store)
    rs = a.run_source
    for name in ("events.json", "checks.json", "outcome.json",
                 "evidence_slices.json", "detector_results.json", "audit.json"):
        assert store.read_derived(rs.run_id, rs.source_capture_id, name) is not None
