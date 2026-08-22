"""Task Ability Signature (spec §4.3) and distributed/external slices (§8.4)."""

from agr.pipeline import analyze
from agr.signature import INTERP_NOT_MEASURED, RESULT_NOT_OBSERVED
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
    assert row.result == "Failed"
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
                assert row.result in {"Successful", "Failed"}
