"""§11 Task & Verifier Audit — categorical, evidence-backed findings.

Covers the deterministic mapping onto the ten dimensions, exact-evidence
linking, persistence, the read-model overlay of human concern corrections
(§4.13), and the HTTP surface.
"""

import json
import os

import pytest

from agr import workflow
from agr.pipeline import analyze
from agr.read import get_audit, get_review
from agr.schema import AUDIT_DIMENSIONS, CONCERN_ASSESSMENTS
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
CHESS = "chess_best_move.atif.json"
MISMATCH = "contract_mismatch.atif.json"


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _analyze(name, tmp_path):
    return analyze(_load(name), Store(str(tmp_path / name)))


def _by_dimension(findings):
    return {f.dimension: f for f in findings}


def test_all_ten_dimensions_always_reported_in_order(tmp_path):
    a = _analyze(CHESS, tmp_path)
    assert [f.dimension for f in a.audit] == list(AUDIT_DIMENSIONS)
    assert all(f.assessment in CONCERN_ASSESSMENTS for f in a.audit)
    assert all(f.audit_version for f in a.audit)


def test_clean_run_yields_no_concern_where_signals_exist(tmp_path):
    a = _analyze(CHESS, tmp_path)
    by = _by_dimension(a.audit)
    # Mechanical checks ran clean -> explicit no_concern_detected, never silence.
    assert by["contract_verifier_coverage"].assessment == "no_concern_detected"
    assert by["hidden_verifier_requirements"].assessment == "no_concern_detected"
    assert by["prompt_only_unverified_requirements"].assessment == "no_concern_detected"
    assert by["verifier_stability"].assessment == "no_concern_detected"
    # No deterministic signal exists for these -> cannot-say, never a clean bill.
    assert by["instruction_clarity"].assessment == "insufficient_evidence"
    assert by["exploitability"].assessment == "insufficient_evidence"
    assert by["product_relevance"].assessment == "insufficient_evidence"
    # The audit qualifies the result; it never changes it.
    assert a.outcome["status"] == "FAILED"


def test_contract_mismatch_fires_supported_concerns_with_exact_evidence(tmp_path):
    a = _analyze(MISMATCH, tmp_path)
    by = _by_dimension(a.audit)

    hidden = by["hidden_verifier_requirements"]
    assert hidden.assessment == "supported_concern"
    assert hidden.evidence_check_ids == ["C2"]
    assert hidden.evidence_item_ids == ["V-C2"]

    prompt_only = by["prompt_only_unverified_requirements"]
    assert prompt_only.assessment == "supported_concern"
    assert prompt_only.evidence_item_ids == ["R2", "R3"]

    reference = by["reference_solution_assumptions"]
    assert reference.assessment == "supported_concern"
    assert reference.evidence_item_ids == ["A1"]

    coverage = by["contract_verifier_coverage"]
    assert coverage.assessment == "supported_concern"
    assert set(coverage.evidence_item_ids) == {"R2", "R3", "A1"}


def test_every_nontrivial_concern_carries_evidence(tmp_path):
    for name in (CHESS, MISMATCH):
        a = _analyze(name, tmp_path)
        all_events = {e.event_id for e in a.events}
        all_checks = {c.check_id for c in a.checks}
        all_items = {i.id for i in a.contract.items}
        for f in a.audit:
            if f.assessment in ("no_concern_detected", "insufficient_evidence"):
                continue  # these legitimately carry no supporting ids
            ids = set(f.evidence_event_ids) | set(f.evidence_check_ids) | set(f.evidence_item_ids)
            assert ids, f"{name}/{f.dimension}: concern without evidence"
            assert ids <= all_events | all_checks | all_items, (
                f"{name}/{f.dimension}: evidence points outside the derived records")


def test_audit_is_persisted_and_read_back(tmp_path):
    store = Store(str(tmp_path / "store"))
    with open(os.path.join(FIXTURES, MISMATCH), encoding="utf-8") as fh:
        a = analyze(json.load(fh), store)
    rs = a.run_source
    persisted = store.read_derived(rs.run_id, rs.source_capture_id, "audit.json")
    assert persisted is not None and len(persisted) == len(AUDIT_DIMENSIONS)
    rows = get_audit(store, rs.run_id)
    assert [r["dimension"] for r in rows] == list(AUDIT_DIMENSIONS)
    assert any(r["assessment"] == "supported_concern" for r in rows)


def test_human_concern_correction_overlays_the_matching_row(tmp_path):
    store = Store(str(tmp_path / "store"))
    with open(os.path.join(FIXTURES, CHESS), encoding="utf-8") as fh:
        a = analyze(json.load(fh), store)
    run_id = a.run_source.run_id

    workflow.add_feedback(
        store, run_id,
        actor="expert", mutation_id="m1", moment_id="mom_any",
        kind="structured_correction",
        correction={"task_verifier_concern": {
            "dimension": "contract_verifier_coverage",
            "assessment": "possible_concern",
            "note": "coverage rests on one artifact check per requirement",
        }},
        evidence_event_ids=["evt_004"],
    )
    rows = get_audit(store, run_id)
    row = next(r for r in rows if r["dimension"] == "contract_verifier_coverage")
    assert row["assessment"] == "possible_concern"          # human value shown by default
    assert row["generated"]["assessment"] == "no_concern_detected"  # generated kept verbatim
    assert row["corrected"] is True
    assert row["correction"]["actor"] == "expert"
    assert row["correction"]["adjudication_status"] == "unadjudicated"
    # Untouched rows stay exactly as generated.
    other = next(r for r in rows if r["dimension"] == "verifier_stability")
    assert "generated" not in other and "corrected" not in other


def test_correction_with_unknown_dimension_is_refused(tmp_path):
    store = Store(str(tmp_path / "store"))
    with open(os.path.join(FIXTURES, CHESS), encoding="utf-8") as fh:
        a = analyze(json.load(fh), store)
    with pytest.raises(workflow.WorkflowError):
        workflow.add_feedback(
            store, a.run_source.run_id,
            actor="expert", mutation_id="m2", moment_id="mom_any",
            kind="structured_correction",
            correction={"task_verifier_concern": {
                "dimension": "not_a_real_dimension",
                "assessment": "supported_concern",
            }},
        )


def test_review_payload_and_http_surface_carry_the_audit(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from agr.api import create_app

    root = str(tmp_path / "store")
    store = Store(root)
    with open(os.path.join(FIXTURES, CHESS), encoding="utf-8") as fh:
        analyze(json.load(fh), store)

    review = get_review(store, "chess_best_move__seed42")
    assert len(review["audit"]) == len(AUDIT_DIMENSIONS)

    client = TestClient(create_app(root))
    resp = client.get("/runs/chess_best_move__seed42/audit")
    assert resp.status_code == 200
    assert len(resp.json()) == len(AUDIT_DIMENSIONS)
    missing = client.get("/runs/not_a_run/audit")
    assert missing.status_code == 404


# --- review-fix regressions ----------------------------------------------------


def test_coverage_without_required_items_reports_cannot_say(tmp_path):
    """A contract with zero required items was never assessed for coverage — the
    dimension must say so, not assert a clean bill from an empty set."""
    store = Store(str(tmp_path / "store"))
    doc = _load(MISMATCH)
    doc["task"]["requirements"] = []
    doc["task"]["artifacts"] = []
    doc["task"]["reference_solution"] = {}
    doc["verifier"]["checks"] = [{
        "check_id": "C1", "name": "Writable working directory", "status": "passed",
        "source": "native_structured", "source_pointers": ["trial.json:exit_code"],
        "contract_item_ids": ["E1"],
    }]
    a = analyze(doc, store)
    by = _by_dimension(a.audit)
    assert by["contract_verifier_coverage"].assessment == "insufficient_evidence"
    # Hidden requirements keeps a legitimate clean bill here: every check maps
    # to a declared item, so absence of a warning is real evidence.
    assert by["hidden_verifier_requirements"].assessment == "no_concern_detected"


def test_hidden_requirements_with_no_checks_cannot_say(tmp_path):
    """With zero captured checks, undeclared enforcement cannot be ruled out —
    no_concern_detected would be a clean bill derived from absence of data."""
    store = Store(str(tmp_path / "store"))
    doc = _load(MISMATCH)
    doc["verifier"]["raw_output"] = ""
    doc["verifier"]["checks"] = []
    a = analyze(doc, store)
    assert _by_dimension(a.audit)["hidden_verifier_requirements"].assessment == \
        "insufficient_evidence"


def test_correction_survives_when_no_generated_audit_exists(tmp_path):
    """A run analyzed before the audit stage existed has no audit.json, but a
    human concern on it must still render — synthesised from the correction."""
    store = Store(str(tmp_path / "store"))
    with open(os.path.join(FIXTURES, CHESS), encoding="utf-8") as fh:
        a = analyze(json.load(fh), store)
    rs = a.run_source
    os.remove(os.path.join(store._capture_dir(rs.run_id, rs.source_capture_id), "audit.json"))

    workflow.add_feedback(
        store, rs.run_id,
        actor="expert", mutation_id="m3", moment_id="mom_any",
        kind="structured_correction",
        correction={"task_verifier_concern": {
            "dimension": "verifier_stability",
            "assessment": "possible_concern",
            "note": "verifier is flaky on this task family",
        }},
    )
    rows = get_audit(store, rs.run_id)
    assert [r["dimension"] for r in rows] == ["verifier_stability"]
    row = rows[0]
    assert row["assessment"] == "possible_concern"
    assert row["corrected"] is True and row["human_statement"] == \
        "verifier is flaky on this task family"
    assert "generated" not in row  # nothing generated to preserve
    # ...and it flows through the review payload and HTTP surface too.
    assert [r["dimension"] for r in get_review(store, rs.run_id)["audit"]] == \
        ["verifier_stability"]
