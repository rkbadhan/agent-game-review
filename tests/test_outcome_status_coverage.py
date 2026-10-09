"""Operational errors, diagnostic evidence, and the guard against unhandled statuses.

Three things pinned here:

- an attempt whose harness broke (``operational_error``) is its own outcome — not
  an agent failure, not "no verdict" — and is left out of success denominators;
- diagnostic evidence (``state_diff`` entries with tool-to-state provenance) rides on
  one authoritative check and can never change the run outcome; and
- every outcome status the backend can emit is classified explicitly by each
  surface that branches on it, so adding a status cannot silently land in the
  "failed" or "clean pass" bucket again.
"""

import os
import re

import pytest

from agr import demo, queue, versions
from agr.checks import CheckExtractionError, extract_checks, outcome
from agr.schema import (
    CHECK_STATUSES, OUTCOME_STATUSES, RunSource, VerifierCheck, diagnostic_entry,
)
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
STATIC_JS = os.path.join(os.path.dirname(__file__), "..", "agr", "static", "js")


def _check(check_id, status, **kw):
    return VerifierCheck(check_id=check_id, run_id="r", source_capture_id="c", name=check_id,
                         status=status, source="native_structured", **kw)


# --- the new outcome ----------------------------------------------------------


def test_operational_error_check_rolls_up_to_its_own_outcome():
    result = outcome([_check("C1", "passed"), _check("C2", "operational_error")])
    assert result["status"] == "OPERATIONAL_ERROR"
    assert result["operational_error_checks"] == ["C2"]
    assert result["failed_checks"] == []


def test_operational_error_outranks_a_failed_check():
    """A real failure seen next to a harness error is not a clean measurement."""
    result = outcome([_check("C1", "failed"), _check("C2", "operational_error")])
    assert result["status"] == "OPERATIONAL_ERROR"
    assert result["failed_checks"] == ["C1"]


def test_operational_error_is_a_valid_check_status():
    assert "operational_error" in CHECK_STATUSES


# --- diagnostic evidence never changes the outcome ----------------------------


def _diag():
    return [
        diagnostic_entry("tickets.*.status", "changed", observed="open", expected="closed",
                         last_writer_event_id="E7", provenance_version="state-provenance-0.1"),
        diagnostic_entry("tickets.*.note", "missing", expected="refund issued",
                         writer_status="no_writer"),
    ]


@pytest.mark.parametrize("status", ["passed", "failed", "operational_error", "unknown"])
def test_adding_or_removing_diagnostic_evidence_never_changes_the_outcome(status):
    bare = [_check("C1", status)]
    with_diag = [_check("C1", status, diagnostic_evidence={"state_diff": _diag()})]
    assert outcome(bare) == outcome(with_diag)
    # Many diff entries are still ONE check: counts are checks, not entries.
    assert outcome(with_diag)["total"] == 1


def test_state_diff_passes_through_check_extraction_and_validates():
    doc = {"verifier": {"checks": [{"check_id": "C1", "status": "failed", "diagnostic_evidence": {
        "kind": "env_state_diff",
        "state_diff": [
            {"path": "tickets.*.status", "change_type": "changed", "observed": "open",
             "expected": "closed", "last_writer_event_id": "E7"},
            {"path": "tickets.*.note", "change_type": "missing", "writer_status": "no_writer"},
        ]}}]}}
    (check,) = extract_checks(doc, _run_source())
    diff = check.diagnostic_evidence["state_diff"]
    assert [d["writer_status"] for d in diff] == ["written", "no_writer"]
    assert diff[0]["last_writer_event_id"] == "E7"
    assert "last_writer_event_id" not in diff[1]
    assert check.diagnostic_evidence["kind"] == "env_state_diff"
    assert check.to_dict()["diagnostic_evidence"] == check.diagnostic_evidence


def test_tau3_shaped_diagnostic_evidence_passes_through_unchanged():
    evidence = {"kind": "tau3_reward_info", "reward_basis": ["DB", "ACTION"],
                "db_check": {"db_match": False, "db_reward": 0.0},
                "action_checks": [{"action": {"name": "cancel"}, "action_match": False}]}
    doc = {"verifier": {"checks": [{"check_id": "C1", "status": "failed",
                                    "diagnostic_evidence": evidence}]}}
    (check,) = extract_checks(doc, _run_source())
    assert check.diagnostic_evidence == evidence


@pytest.mark.parametrize("entry", [
    {"change_type": "changed"},                                   # no path
    {"path": "a", "writer_status": "written"},                    # written without a writer
    {"path": "a", "writer_status": "no_writer", "last_writer_event_id": "E1"},
    {"path": "a", "writer_status": "sometimes"},                  # unknown vocabulary
])
def test_malformed_state_diff_entries_are_rejected(entry):
    doc = {"verifier": {"checks": [{"check_id": "C1", "status": "failed",
                                    "diagnostic_evidence": {"state_diff": [entry]}}]}}
    with pytest.raises(CheckExtractionError):
        extract_checks(doc, _run_source())


@pytest.mark.parametrize("evidence", [["not", "a", "dict"], {"state_diff": "nope"}])
def test_malformed_diagnostic_evidence_container_is_rejected(evidence):
    doc = {"verifier": {"checks": [{"check_id": "C1", "status": "failed",
                                    "diagnostic_evidence": evidence}]}}
    with pytest.raises(CheckExtractionError):
        extract_checks(doc, _run_source())


def test_diagnostic_entry_keeps_an_explicit_null_observed_value():
    entry = diagnostic_entry("a.b", "changed", observed=None, expected="x", writer_status="unknown")
    assert "observed" in entry and entry["observed"] is None


def _run_source():
    return RunSource(run_id="r", source_capture_id="c", capture_revision=1, source_hash="h",
                     source_type="t", source_schema="s", capture_completeness="complete",
                     task_id="task")


# --- version comparison: errors leave the denominator ------------------------


def _slice(tmp_path, *, candidate_errors=(), candidate_fails=()):
    def baseline(doc, name):
        demo.set_outcome(doc, "passed")

    def candidate(doc, name):
        if name in candidate_errors:
            demo.set_outcome(doc, "operational_error")
        elif name in candidate_fails:
            demo.set_outcome(doc, "failed")
        else:
            demo.set_outcome(doc, "passed")

    store = Store(str(tmp_path / "store"))
    demo.build_slice(store, FIXTURES, baseline_mutate=baseline, candidate_mutate=candidate)
    return store


def _compare(store):
    return versions.compare_versions(store, {"sweep_id": "sweep_141"},
                                     {"sweep_id": "sweep_142"}, "evaluation_harness")


def test_operational_error_leaves_the_pass_rate_denominator(tmp_path):
    errored = demo.DEMO_TASKS[0]
    result = _compare(_slice(tmp_path, candidate_errors=(errored,)))
    pass_rate = result["pass_rate"]
    n = len(demo.DEMO_TASKS)
    assert pass_rate["candidate"] == {"numerator": n - 1, "denominator": n - 1}
    # The task with no eligible candidate attempt drops out of the paired test.
    assert pass_rate["task_count"] == n - 1
    # And the error is visible as its own rate, not hidden.
    error_rate = result["operational_error_rate"]
    assert error_rate["candidate"] == {"numerator": 1, "denominator": n}
    assert error_rate["baseline"] == {"numerator": 0, "denominator": n}


def test_a_real_failure_stays_in_the_denominator(tmp_path):
    """The contrast case: an agent failure is a 0, an operational error is no observation."""
    failed = demo.DEMO_TASKS[0]
    result = _compare(_slice(tmp_path, candidate_fails=(failed,)))
    n = len(demo.DEMO_TASKS)
    assert result["pass_rate"]["candidate"] == {"numerator": n - 1, "denominator": n}
    assert result["pass_rate"]["task_count"] == n
    assert result["operational_error_rate"]["candidate"]["numerator"] == 0


def test_pass_observation_unit():
    assert versions._pass_observation({"outcome": {"status": "PASSED"}}) == (1, 1)
    assert versions._pass_observation({"outcome": {"status": "FAILED"}}) == (0, 1)
    assert versions._pass_observation({"outcome": {"status": "UNDETERMINED"}}) == (0, 1)
    assert versions._pass_observation({"outcome": {"status": "OPERATIONAL_ERROR"}}) == (0, 0)


# --- ingest through the real pipeline and the Runs surfaces -------------------


def test_operational_error_run_is_not_a_failure_on_any_runs_surface(tmp_path):
    store = _slice(tmp_path, candidate_errors=(demo.DEMO_TASKS[0],))
    runs = [r for r in queue.read.list_runs(store)
            if r["outcome"]["status"] == "OPERATIONAL_ERROR"]
    assert len(runs) == 1
    run = runs[0]
    assert queue.outcome_bucket(run["outcome"]["status"]) == "operational_error"
    assert queue.FILTER_CHIPS["operational_error"](run | {"workflow": {}})
    assert not queue.FILTER_CHIPS["failed"](run | {"workflow": {}})
    assert not queue.FILTER_CHIPS["passed"](run | {"workflow": {}})
    # It must not sink into the "clean unsampled pass" triage tier.
    assert queue._triage_rank(run) < 5


# --- the guard: no emitted status goes unclassified --------------------------


def test_every_outcome_status_has_an_explicit_queue_bucket():
    for status in OUTCOME_STATUSES:
        assert status in queue._STATUS_BUCKET, f"{status} falls to the default bucket"
        assert queue._STATUS_BUCKET[status] in queue.OUTCOME_BUCKETS


def test_every_outcome_status_is_counted_in_the_sweep_summary(tmp_path):
    store = _slice(tmp_path, candidate_errors=(demo.DEMO_TASKS[0],))
    by_outcome = queue.sweep_summary(store)["by_outcome"]
    assert by_outcome.get("OPERATIONAL_ERROR") == 1
    assert sum(by_outcome.values()) == 2 * len(demo.DEMO_TASKS)


def test_outcome_emits_only_registered_statuses():
    samples = [
        [], [_check("a", "passed")], [_check("a", "failed")], [_check("a", "unknown")],
        [_check("a", "operational_error")],
    ]
    for checks in samples:
        assert outcome(checks)["status"] in OUTCOME_STATUSES


def test_every_ui_script_that_branches_on_outcome_status_names_operational_error():
    """UI scripts cannot be imported here, so this is a static guard.

    A script that already special-cases UNDETERMINED/UNVERIFIED/FAILED is making
    a decision per status; if it never mentions OPERATIONAL_ERROR it silently
    renders one as a failure (the fallthrough in most of them) or as a pass.
    """
    branches_on_status = re.compile(r'"(UNDETERMINED|UNVERIFIED)"|\.status === "FAILED"|bo\.FAILED')
    missing = []
    for name in sorted(os.listdir(STATIC_JS)):
        if not name.endswith(".js"):
            continue
        with open(os.path.join(STATIC_JS, name), encoding="utf-8") as fh:
            text = fh.read()
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("//"))
        if branches_on_status.search(code) and "OPERATIONAL_ERROR" not in code \
                and "operational_error" not in code:
            missing.append(name)
    assert not missing, f"UI scripts branch on outcome status but ignore OPERATIONAL_ERROR: {missing}"
