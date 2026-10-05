"""EV-1: the Who&When attribution-benchmark adapter and its scorer.

These tests pin the two conversions that make the evaluation comparable:
trace -> ATIF (one step per history entry, so a ``mistake_step`` index maps
unambiguously) and label -> gold (the decisive step as a critical negative
moment). They also pin the honesty rules: reference labels never enter the
reviewer-visible document, an unmappable record is skipped with a warning, and
the manifest declares AGR's single-prediction rule and limits before scoring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agr.benchmark import (
    AttributionPrediction,
    BenchmarkManifest,
    _resolve_agent,
    score_case,
)
from agr.benchmark_whowhen import (
    WHO_WHEN_MANIFEST,
    convert_record,
    load_cases,
)
from agr.gold import GoldSet
from agr.store import Store, validate_run_id

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "whowhen"
ALG_DIR = str(FIXTURES / "Algorithm-Generated")
HC_DIR = str(FIXTURES / "Hand-Crafted")


def _alg_record() -> dict:
    return json.loads((FIXTURES / "Algorithm-Generated" / "sample.json").read_text(encoding="utf-8"))


def _hc_record() -> dict:
    return json.loads((FIXTURES / "Hand-Crafted" / "sample.json").read_text(encoding="utf-8"))


# --- trace -> ATIF -----------------------------------------------------------


def test_algorithm_generated_history_maps_one_step_per_entry():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    steps = case.doc["steps"]
    assert [s["step_id"] for s in steps] == [f"s{i}" for i in range(1, 6)]
    assert steps[0]["kind"] == "model_output"
    assert steps[0]["actor"] == "Spreadsheet_Expert"
    assert steps[1]["kind"] == "model_output"
    assert steps[1]["actor"] == "Spreadsheet_Expert"
    # the terminal result is a tool_result whose literal exit code is read out
    assert steps[2]["kind"] == "tool_result"
    assert steps[2]["actor"] == "tool"
    assert steps[2]["exit_code"] == 0
    assert steps[3]["actor"] == "Verification_Expert"
    assert case.doc["run"]["task_id"] == "00000000-0000-0000-0000-0000000000aa"


def test_gold_anchors_on_the_decisive_step_and_is_critical():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    annotation = case.gold.adjudicated_annotation()
    assert len(annotation.moments) == 1
    moment = annotation.moments[0]
    # mistake_step=1 (0-based) -> s2 (1-based)
    assert moment.anchor_step_ids == ["s2"]
    assert moment.polarity == "negative"
    assert moment.critical is True
    assert moment.anchor_type == "decision"
    assert case.gold.label_source == "human"
    assert case.gold.frozen is True
    assert case.meta["mistake_agent"] == "Spreadsheet_Expert"


def test_hand_crafted_roles_become_speakers_and_delegation_normalizes():
    case = convert_record(_hc_record(), case_id="sample", split="hand-crafted")
    steps = {s["step_id"]: s for s in case.doc["steps"]}
    assert steps["s1"]["kind"] == "task_received"
    assert steps["s1"]["actor"] == "human"
    # "Orchestrator (-> WebSurfer)" is the Orchestrator's own turn
    assert steps["s3"]["kind"] == "model_output"
    assert steps["s3"]["actor"] == "Orchestrator"
    assert steps["s4"]["actor"] == "WebSurfer"
    # mistake_step=3 -> s4, whose actor is the labelled responsible agent
    assert case.gold.adjudicated_annotation().moments[0].anchor_step_ids == ["s4"]


def test_reference_labels_never_enter_the_document():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    blob = json.dumps(case.doc)
    assert case.meta["mistake_reason"] not in blob
    assert case.doc["task"]["instruction"] != case.meta["mistake_reason"]
    assert "mistake_agent" not in blob
    assert "mistake_reason" not in blob


def test_no_verifier_is_invented():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    assert "verifier" not in case.doc
    assert case.doc["capture_completeness"] == "partial"


def test_run_id_is_store_safe():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    assert validate_run_id(case.run_id) == case.run_id


def test_out_of_range_mistake_step_is_a_warning_not_a_guess(tmp_path):
    bad = dict(_alg_record())
    bad["mistake_step"] = "99"
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    cases, warnings = load_cases(str(tmp_path))
    assert cases == []
    assert len(warnings) == 1 and "mistake_step 99" in warnings[0]


def test_load_cases_reads_every_record():
    cases, warnings = load_cases(ALG_DIR)
    assert [c.case_id for c in cases] == ["sample"]
    assert warnings == []


def test_gold_validates_against_the_source_steps():
    cases, _ = load_cases(ALG_DIR)
    source_steps = {
        c.run_id: {s["step_id"] for s in c.doc["steps"]} for c in cases
    }
    GoldSet([c.gold for c in cases]).validate(source_steps)


# --- manifest ----------------------------------------------------------------


def test_manifest_states_rule_provenance_and_limits():
    assert isinstance(WHO_WHEN_MANIFEST, BenchmarkManifest)
    assert WHO_WHEN_MANIFEST.benchmark == "who_and_when"
    assert "negative" in WHO_WHEN_MANIFEST.single_prediction_rule.lower()
    assert WHO_WHEN_MANIFEST.license == "MIT"
    assert WHO_WHEN_MANIFEST.coverage["abstention"] is False
    assert any("abstention" in limit.lower() for limit in WHO_WHEN_MANIFEST.limits)
    assert WHO_WHEN_MANIFEST.to_dict()["manifest_version"] == "benchmark-manifest-0.1"


# --- agent resolution --------------------------------------------------------


def test_agent_resolution_walks_back_from_a_tool_result():
    rows = [
        {"step_id": "s1", "actor": "Alpha_Expert", "sequence": 1},
        {"step_id": "s2", "actor": "tool", "sequence": 2},
    ]
    anchor = [rows[1]]  # the tool result itself
    assert _resolve_agent(anchor, rows) == "Alpha_Expert"


def test_agent_resolution_prefers_the_moments_own_agent_step():
    rows = [
        {"step_id": "s1", "actor": "tool", "sequence": 1},
        {"step_id": "s2", "actor": "Beta_Expert", "sequence": 2},
    ]
    assert _resolve_agent([rows[1]], rows) == "Beta_Expert"


def test_agent_resolution_normalizes_orchestrator_delegation():
    rows = [{"step_id": "s1", "actor": "Orchestrator (-> WebSurfer)", "sequence": 1}]
    assert _resolve_agent(rows, rows) == "Orchestrator"


# --- scoring -----------------------------------------------------------------


def test_score_case_awards_step_and_agent_independently():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    hit = AttributionPrediction(case_id="sample", step_ids=["s2"], agent="Spreadsheet_Expert")
    score = score_case(hit, case)
    assert (score.step_correct, score.agent_correct, score.both_correct) == (True, True, True)

    right_step_wrong_agent = AttributionPrediction(case_id="sample", step_ids=["s2"], agent="Nope")
    score = score_case(right_step_wrong_agent, case)
    assert (score.step_correct, score.agent_correct, score.both_correct) == (True, False, False)

    wrong = AttributionPrediction(case_id="sample", step_ids=["s3"], agent="Nope")
    assert score_case(wrong, case).step_correct is False


def test_abstention_is_scored_not_skipped():
    case = convert_record(_alg_record(), case_id="sample", split="algorithm-generated")
    score = score_case(AttributionPrediction(case_id="sample", abstained=True), case)
    assert (score.step_correct, score.agent_correct, score.both_correct) == (False, False, False)
    assert score.abstained is True


# --- end to end --------------------------------------------------------------


@pytest.mark.parametrize("directory", [ALG_DIR, HC_DIR])
def test_run_benchmark_end_to_end(tmp_path, directory):
    from agr.benchmark import run_benchmark
    from agr.benchmark_whowhen import WHO_WHEN_ADAPTER

    store = Store(str(tmp_path / "store"))
    result = run_benchmark(WHO_WHEN_ADAPTER, directory, store, analyze_fn=_analyze_shim)
    report = result.report()
    assert report["summary"]["n_cases"] == 1
    assert report["manifest"]["benchmark"] == "who_and_when"
    assert report["summary"]["step_accuracy"] in (0.0, 1.0)
    # the runner still ingests + analyses the real pipeline shape
    assert report["cases"][0]["run_id"].startswith("whowhen__")


def _analyze_shim(doc, store, reviewer=None):
    """Real analyze, kept as a named seam so the import stays local."""
    from agr.pipeline import analyze
    return analyze(doc, store, reviewer=reviewer)


def test_run_benchmark_reports_undefined_rate_on_empty_set():
    from agr.benchmark import AttributionSetEval

    assert AttributionSetEval().metrics()["step_accuracy"] is None
