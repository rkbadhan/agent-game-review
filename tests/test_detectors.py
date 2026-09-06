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


# --- AGR-05: repetition claims need output evidence; placeholders are honest --

def _repeat_events(*outputs):
    """N identical tool calls, each answered by a tool_result with the given
    output. Alternating call/result, as derivation produces them."""
    from agr.schema import DerivedEvent

    evs = []
    seq = 0
    for i, out in enumerate(outputs):
        seq += 1
        evs.append(DerivedEvent(
            event_id=f"evt_c{i}", run_id="r", source_capture_id="c", sequence=seq,
            source_step_ids=[f"s{i}"], event_type="tool_call", actor="main_agent",
            payload={"tool": "shell", "content": "check-status --poll"}))
        seq += 1
        evs.append(DerivedEvent(
            event_id=f"evt_r{i}", run_id="r", source_capture_id="c", sequence=seq,
            source_step_ids=[f"s{i}"], event_type="tool_result", actor="tool",
            payload={"tool": "shell", "content": out, "exit_code": 0}))
    return evs


def _det_ctx(evs):
    from agr.detectors import DetectorContext
    from agr.schema import CapabilityProfile
    prof = CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete",
                      "tool_results": "complete", "filesystem": "checkpoint_only"})
    return DetectorContext(run_id="r", capture_id="c", events=evs, checks=[], doc={},
                           profile=prof)


def test_identical_outputs_support_the_no_new_info_claim():
    from agr.detectors import RepeatedActionNoNewInfo
    evs = _repeat_events("status: running", "status: running")
    cands = RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates
    assert len(cands) == 1
    fact = cands[0].structured_facts[0]
    assert fact["outputs_compared"] is True and fact["outputs_equivalent"] is True
    assert fact["result_event_ids"] == ["evt_r0", "evt_r1"]


def test_changing_poll_output_is_not_no_new_information():
    """Acceptance (AGR-05): a poll whose report changed is observation with
    new information — no repetition defect is emitted."""
    from agr.detectors import RepeatedActionNoNewInfo
    evs = _repeat_events("status: running", "status: done")
    cands = RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates
    assert cands == []


def test_unobserved_outputs_cannot_support_the_claim():
    from agr.detectors import RepeatedActionNoNewInfo
    evs = _repeat_events("status: running")  # second call's result missing
    del evs[-1]
    cands = RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates
    assert cands == []


def test_placeholder_detector_is_distinct_from_evaluated_no_issue(tmp_path, load_fixture):
    """Acceptance (AGR-05): a registered placeholder reports evaluated=False
    with placeholder=True — never 'evaluated, no problem found'."""
    a = _analyze(tmp_path, load_fixture, "chess_best_move.atif.json")
    comp = next(r for r in a.detector_results if r.detector == "compaction_requirement_loss")
    assert comp.evaluated is False
    assert comp.placeholder is True
    assert comp.candidates == []
    # Contrast: an evaluated detector that found nothing reports evaluated=True.
    rec = next(r for r in a.detector_results if r.detector == "successful_recovery_via_strategy_change")
    assert rec.evaluated is True and rec.placeholder is False
