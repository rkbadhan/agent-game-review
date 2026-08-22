"""The five MVP detectors + omission slicing (spec §8.5, §8.4)."""

from agr.pipeline import analyze
from agr.store import Store


def _by_name(analysis):
    return {r.detector: r for r in analysis.detector_results}


def _analyze(tmp_path, load_fixture, name):
    return analyze(load_fixture(name), Store(str(tmp_path / "store")))


def test_ignored_tool_failure_fires_when_unresolved(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "ignored_failure.atif.json")
    d = _by_name(a)["ignored_tool_failure"]
    assert d.evaluated is True
    assert len(d.candidates) == 1
    assert d.candidates[0].anchor_event_ids[0] == "evt_003"  # the failing tool_result


def test_ignored_tool_failure_silent_when_recovered(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    assert _by_name(a)["ignored_tool_failure"].candidates == []


def test_successful_recovery_detector_is_positive(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    d = _by_name(a)["successful_recovery_via_strategy_change"]
    assert len(d.candidates) == 1
    assert d.candidates[0].polarity == "positive"
    assert d.candidates[0].kind == "recovery"


def test_no_false_recovery_on_unchanged_retry(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "stuck_retry.atif.json")
    assert _by_name(a)["successful_recovery_via_strategy_change"].candidates == []


def test_required_artifact_absent_omission(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "ignored_failure.atif.json")
    d = _by_name(a)["required_artifact_absent"]
    assert len(d.candidates) == 1
    cand = d.candidates[0]
    assert cand.kind == "omission"
    fact = cand.structured_facts[0]
    assert fact["type"] == "absence"
    assert fact["declared_artifact"] == "/app/bin"

    # An omission-branch evidence slice is produced with a real opportunity window.
    omission_slices = [s for s in a.evidence_slices if s.branch == "omission"]
    assert omission_slices
    assert omission_slices[0].attribution_ceiling == "dependency_linked"  # filesystem complete


def test_repeated_action_detector_quiet_on_distinct_actions(tmp_path, load_fixture):
    # chess fixture has no back-to-back identical tool calls.
    a = _analyze(tmp_path, load_fixture, "chess_best_move.atif.json")
    assert _by_name(a)["repeated_action_no_new_info"].candidates == []


def test_artifact_present_suppresses_absence(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    assert _by_name(a)["required_artifact_absent"].candidates == []


def test_opportunities_detected(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    abilities = {o.ability for o in a.opportunities}
    assert "tool_error_recovery" in abilities
    assert "verification_discipline" in abilities
