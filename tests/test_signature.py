"""Task Ability Signature (spec §4.3) and distributed/external slices (§8.4)."""

from dataclasses import dataclass, field

import pytest

from agr.pipeline import analyze
from agr.schema import VerifierCheck
from agr.signature import (
    INTERP_NOT_MEASURED,
    INTERP_PLAUSIBLE,
    RESULT_NOT_OBSERVED,
    RESULT_PLAUSIBLE,
    RESULT_SUCCESS,
    _recovery_row,
    _requirement_rows,
)
from agr.store import Store


def _analyze(tmp_path, load_fixture, name):
    return analyze(load_fixture(name), Store(str(tmp_path / "store")))


def _row(analysis, ability):
    return next(r for r in analysis.signature if r.ability == ability)


def test_recovery_row_not_measured_without_opportunity(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "chess_best_move.atif.json")
    row = _row(a, "Recover from tool failure")
    # No tool failure occurred -> must stay Not measured, never a judgment.
    assert row.measured is False
    assert row.result == RESULT_NOT_OBSERVED
    assert row.interpretation == INTERP_NOT_MEASURED


def test_recovery_row_positive_on_good_recovery(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    row = _row(a, "Recover from tool failure")
    assert row.measured is True
    assert row.result == "Successful"
    assert row.interpretation == "Positive evidence"


def test_verification_row_negative_when_unverified_and_failed(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "chess_best_move.atif.json")
    row = _row(a, "Verify before submission")
    assert row.measured is True
    # AGR-05: the row scores the verification BEHAVIOUR, independent of the
    # final outcome — no verification action was observed, whatever the run
    # returned. With failing checks the absence is still negative evidence.
    assert row.result == RESULT_NOT_OBSERVED
    assert row.interpretation == "Negative evidence in this run"


def test_requirement_rows_track_check_outcomes(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "chess_best_move.atif.json")
    results = {r.ability: r.result for r in a.signature}
    assert results["All winning moves included"] == "Failed"
    assert results["Artifact exists at /solution.txt"] == "Successful"


def test_external_slice_emitted_for_unrecovered_failure(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "ignored_failure.atif.json")
    external = [s for s in a.evidence_slices if s.branch == "external"]
    assert external
    # External attribution is explanatory only — never above hypothesized.
    assert all(s.attribution_ceiling == "hypothesized" for s in external)


def test_signature_never_conflates_not_measured_with_result(tmp_path, load_fixture):
    for name in ("chess_best_move.atif.json", "tool_failure_recovery.atif.json",
                 "stuck_retry.atif.json", "ignored_failure.atif.json"):
        a = _analyze(tmp_path, load_fixture, name)
        for row in a.signature:
            if row.interpretation == INTERP_NOT_MEASURED:
                assert row.measured is False
                assert row.result == RESULT_NOT_OBSERVED
            else:
                # AGR-05: a measured row may also report NOT_OBSERVED when the
                # qualifying opportunity existed but the behaviour never did
                # (e.g. no verification action) — that is an observation about
                # the behaviour, not a verdict laundered from the outcome.
                assert row.result in {"Successful", "Failed", RESULT_NOT_OBSERVED, RESULT_PLAUSIBLE}


# --- AGR-05: honest recovery representation; verification scored on its own ---

def test_recovery_row_positive_on_successful_unchanged_retry(tmp_path, load_fixture):
    """Acceptance (AGR-05): a strategy change is not a universal requirement —
    a successful unchanged retry is a recovery, worded honestly."""
    a = _analyze(tmp_path, load_fixture, "stuck_retry.atif.json")
    row = _row(a, "Recover from tool failure")
    assert row.measured is True
    assert row.result == "Successful"
    assert row.interpretation == "Positive evidence"
    assert "unchanged" in row.observed_behaviour.lower()


# --- AGR-04: plausible recovery is its own category, never "Successful" -----


@dataclass
class _FakeOpportunity:
    trigger: str


@dataclass
class _FakeEpisode:
    classification: str
    failure_event_id: str = "evt_fake"


@dataclass
class _FakeAnalysis:
    opportunities: list = field(default_factory=list)
    recoveries: list = field(default_factory=list)


def test_plausible_recovery_row_is_its_own_category_not_successful():
    from agr.recovery import PLAUSIBLE_RECOVERY

    fake = _FakeAnalysis(
        opportunities=[_FakeOpportunity(trigger="tool_failure")],
        recoveries=[_FakeEpisode(classification=PLAUSIBLE_RECOVERY)],
    )
    row = _recovery_row(fake)
    assert row.measured is True
    assert row.result == RESULT_PLAUSIBLE
    assert row.result != "Successful"
    assert row.interpretation == INTERP_PLAUSIBLE
    assert "plausible" in row.observed_behaviour.lower()


def test_confirmed_recovery_still_outranks_plausible_when_both_present():
    from agr.recovery import GOOD_RECOVERY, PLAUSIBLE_RECOVERY

    fake = _FakeAnalysis(
        opportunities=[_FakeOpportunity(trigger="tool_failure")],
        recoveries=[_FakeEpisode(classification=PLAUSIBLE_RECOVERY),
                   _FakeEpisode(classification=GOOD_RECOVERY)],
    )
    row = _recovery_row(fake)
    assert row.result == "Successful"


def test_verification_row_gets_no_credit_on_a_pass_without_verification(tmp_path, load_fixture):
    """Acceptance (AGR-05): a passing run without an observed verification
    action provides no positive evidence of verification."""
    a = _analyze(tmp_path, load_fixture, "stuck_retry.atif.json")
    row = _row(a, "Verify before submission")
    assert row.measured is True
    assert row.result == RESULT_NOT_OBSERVED
    assert "no positive evidence" in row.interpretation.lower()


# --- verification credit (review 2026-10-02 #6) --------------------------------


def _clean_pass_with_step_before_submission(load_fixture, command, result="done", tool="shell"):
    """clean_pass with one extra call/result between the artifact observation and submission."""
    doc = load_fixture("clean_pass.atif.json")
    at = next(i for i, st in enumerate(doc["steps"]) if st["kind"] == "final_submission")
    doc["steps"][at:at] = [
        {"step_id": "x1", "kind": "tool_call", "actor": "main_agent", "tool": tool, "content": command},
        {"step_id": "x2", "kind": "tool_result", "actor": "tool", "tool": tool, "exit_code": 0, "content": result},
    ]
    return doc


@pytest.mark.parametrize("command, result", [
    ("git checkout -b feature", "Switched to a new branch 'feature'"),
    ("git checkout main", "Already on 'main'"),
    ("ls checkpoints/", "checkpoint_1.pt"),
    ("echo ready", "please check back later"),
    ("pip install pytest", "Successfully installed pytest"),
    ("which pytest", "/usr/bin/pytest"),
    ("grep -r pytest .", "setup.cfg:pytest"),
    ("echo 'run pytest later'", "run pytest later"),
    ("cat > test_solution.py << EOF", ""),
    ("vim test.py", ""),
    ("cat test.py", "def test_x(): pass"),
    ("chmod +x check.sh", ""),
    ("cat pytest.ini", "[pytest]"),
    ("cat diff.patch", "--- a"),
    ("git add tests/test_x.py", ""),
    ("python3 generate_tests.py", "wrote 4 files"),
])
def test_unrelated_command_mentioning_check_earns_no_verification_credit(
        tmp_path, load_fixture, command, result):
    a = analyze(_clean_pass_with_step_before_submission(load_fixture, command, result),
                Store(str(tmp_path / "store")))
    row = _row(a, "Verify before submission")
    assert row.result == RESULT_NOT_OBSERVED
    assert row.interpretation != "Positive evidence"


@pytest.mark.parametrize("command", [
    "pytest -q",
    "cd /app && python3 -m unittest discover",
    ".venv/bin/pytest tests/",
    "npm run test",
    "go test ./...",
    "./check.sh",
    "diff /out.txt expected.txt",
    "uv run pytest -q",
    "PYTHONPATH=. pytest",
    "bash /tests/run-tests.sh",
    "./run_tests.sh",
    "python tests/test_outputs.py",
    "make -C build test",
    "./gradlew test",
    "bun test",
    "[ -f /out.txt ]",
    "cat notes.txt | grep -c done && pytest -q",
])
def test_running_a_test_or_compare_command_earns_verification_credit(tmp_path, load_fixture, command):
    a = analyze(_clean_pass_with_step_before_submission(load_fixture, command),
                Store(str(tmp_path / "store")))
    row = _row(a, "Verify before submission")
    assert row.result == "Successful"
    assert row.interpretation == "Positive evidence"
    # The row cites the verification action itself, not just the window edges.
    verifying = next(e for e in a.events if e.source_step_ids == ["x1"])
    assert verifying.event_id in row.evidence


def test_a_tool_result_alone_never_counts_as_verification(tmp_path, load_fixture):
    a = analyze(_clean_pass_with_step_before_submission(load_fixture, "cat notes.txt", "pytest passed, all checks ok"),
                Store(str(tmp_path / "store")))
    assert _row(a, "Verify before submission").result == RESULT_NOT_OBSERVED


@pytest.mark.parametrize("tool, credited", [
    ("run_tests", True), ("runTests", True), ("verify_answer", True),
    ("write_test_file", False), ("create_test", False), ("lint_fix", False),
    ("check_status", False), ("git_checkout", False),
])
def test_only_a_tool_named_for_running_verification_earns_credit(tmp_path, load_fixture, tool, credited):
    a = analyze(_clean_pass_with_step_before_submission(load_fixture, "answer.txt", tool=tool),
                Store(str(tmp_path / "store")))
    assert (_row(a, "Verify before submission").result == "Successful") is credited


# --- reconciled check status (review 2026-10-02 #7) ----------------------------


def _check(check_id, status, **kw):
    return VerifierCheck(check_id=check_id, run_id="r", source_capture_id="c",
                         name="test suite", status=status, source="in_session", **kw)


def test_signature_ignores_a_check_superseded_by_a_later_pass():
    rows = _requirement_rows([
        _check("C1", "failed", superseded_by="C2"),
        _check("C2", "passed"),
    ])
    assert [(r.evidence[0], r.result) for r in rows] == [("C2", RESULT_SUCCESS)]


def test_signature_awards_no_success_to_a_pass_made_stale_by_a_later_mutation():
    rows = _requirement_rows([_check("C1", "passed", stale_reason="relevant_mutation_after_check")])
    assert rows == []


def test_signature_follows_reconcile_checks_end_to_end():
    from agr.checks import reconcile_checks
    checks = [_check("C1", "failed", scope="pytest", sequence=1),
              _check("C2", "passed", scope="pytest", sequence=2)]
    reconcile_checks(checks, [])
    rows = _requirement_rows(checks)
    assert [(r.evidence[0], r.result) for r in rows] == [("C2", RESULT_SUCCESS)]
