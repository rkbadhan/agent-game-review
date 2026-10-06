import json

import pytest

from agr import read
from legacy.benchmark_trail import TrailAdapter
from legacy.evaluation import build_evaluation_report, create_audit_pack


def _trail_record():
    trace = {
        "resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "root", "name": "agent",
             "startTimeUnixNano": "1", "endTimeUnixNano": "4",
             "attributes": [
                 {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                 {"key": "input.value", "value": {"stringValue": "inspect error"}},
             ], "status": {"code": 1}},
            {"traceId": "t1", "spanId": "mapped-error", "parentSpanId": "root",
             "name": "terminal", "startTimeUnixNano": "2", "endTimeUnixNano": "3",
             "attributes": [
                 {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                 {"key": "gen_ai.tool.name", "value": {"stringValue": "terminal"}},
                 {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": "{}"}},
                 {"key": "gen_ai.tool.call.result", "value": {"stringValue": "failed"}},
             ], "status": {"code": 2}},
        ]}]}]
    }
    return {"id": "case1", "trace": trace, "labels": [
        {"span_id": "mapped-error", "impact": "High"},
        {"span_id": "unmapped-error", "impact": "Low"},
    ]}


def test_trail_unmapped_labels_stay_in_recall_denominator(tmp_path, monkeypatch):
    data = tmp_path / "trail"
    data.mkdir()
    (data / "case.json").write_text(json.dumps(_trail_record()), encoding="utf-8")
    cases, warnings = TrailAdapter().load_cases(str(data))
    assert len(cases) == 1
    assert len(warnings) == 1
    case = cases[0]
    mapped_step = next(step for step, span in case.meta["step_span_ids"].items()
                       if span == "mapped-error")

    monkeypatch.setattr(read, "get_review", lambda *_a, **_k: {
        "review_status": "moments_found",
        "moments": [{"polarity": "negative", "anchor_event_ids": ["evt-1"]}],
    })
    monkeypatch.setattr(read, "get_forensic", lambda *_a, **_k: {
        "steps": [{"step_id": mapped_step, "event_ids": ["evt-1"]}],
    })
    score = TrailAdapter().score_case(object(), case)
    assert score.true_positives == 1
    assert score.gold_count == 2
    assert score.to_dict()["span_recall"] == 0.5


@pytest.mark.parametrize("status", ["review_failed", "incomplete", "all_proposals_rejected"])
def test_trail_failed_review_gets_no_prediction_credit(tmp_path, monkeypatch, status):
    data = tmp_path / "trail"
    data.mkdir()
    (data / "case.json").write_text(json.dumps(_trail_record()), encoding="utf-8")
    case = TrailAdapter().load_cases(str(data))[0][0]
    monkeypatch.setattr(read, "get_review", lambda *_a, **_k: {
        "review_status": status,
        "moments": [{"polarity": "negative", "anchor_event_ids": ["evt-1"]}],
    })
    score = TrailAdapter().score_case(object(), case)
    assert score.true_positives == 0
    assert score.predicted_count == 0
    assert score.gold_count == 2
    assert score.review_status == status
    assert not score.abstained


def test_unfrozen_benchmark_results_are_exploratory_only():
    report = build_evaluation_report([{
        "manifest": {"benchmark": "who_and_when"},
        "run_manifest": None,
        "summary": {},
        "cases": [{"task_id": "t1", "step_correct": True}],
    }], [])
    assert report["benchmark_evidence"][0]["claim_status"] == "exploratory_unfrozen"
    assert report["claims"]["benchmark"] == []
    assert report["claims"]["exploratory_benchmarks"]


def test_model_result_requires_training_cutoff_and_partition_policy():
    report = {
        "manifest": {"benchmark": "who_and_when"},
        "run_manifest": {
            "run_manifest_version": "benchmark-run-0.2",
            "reviewer": {"kind": "model"},
            "selection": {"records_sha256": "abc"},
            "evaluation_protocol": {},
        },
        "summary": {},
        "cases": [{"task_id": "t1", "step_correct": True}],
    }
    unfrozen = build_evaluation_report([report], [])
    assert unfrozen["benchmark_evidence"][0]["claim_status"] == "exploratory_protocol_incomplete"
    report["run_manifest"]["evaluation_protocol"] = {
        "training_cutoff": "unknown", "partition_policy": "group by task_id",
    }
    unknown_cutoff = build_evaluation_report([report], [])
    assert unknown_cutoff["benchmark_evidence"][0]["claim_status"] == "exploratory_contamination_unassessed"
    assert unknown_cutoff["claims"]["benchmark"] == []
    report["run_manifest"]["evaluation_protocol"]["training_cutoff"] = "2025-02-01"
    frozen = build_evaluation_report([report], [])
    assert frozen["benchmark_evidence"][0]["claim_status"] == "publishable"


def test_audit_pack_has_separate_blind_and_revealed_forms(tmp_path, monkeypatch):
    store = object()
    monkeypatch.setattr(read, "list_runs", lambda _store: [{"run_id": "run-1"}])
    monkeypatch.setattr(read, "get_review", lambda *_a, **_k: {
        "review_status_success": True, "review_status": "moments_found",
        "moments": [], "review_moments": [],
    })
    monkeypatch.setattr(read, "get_source", lambda *_a, **_k: {
        "source": {"run": {"task_id": "task-1"}, "steps": [], "verifier": {"status": "pass"}},
    })
    output = tmp_path / "audit"
    create_audit_pack(store, str(output), sample_size=1, seed=4)
    blind_form = json.loads((output / "blind" / "annotations-template.json").read_text())
    revealed_form = json.loads((output / "revealed" / "annotations-template.json").read_text())
    assert "independent_moments" in blind_form["annotations"][0]
    assert "evidence_support" not in blind_form["annotations"][0]
    assert "evidence_support" in revealed_form["annotations"][0]
    assert "moment_precision_numerator" in revealed_form["annotations"][0]
