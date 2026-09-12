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


def test_ignored_tool_failure_silent_on_expected_probe(tmp_path, load_fixture):
    """Item 33 (2026-09-11 audit): a ``test -f`` that came back non-zero
    because the file was absent, immediately followed by the agent creating
    it, is not an ignored failure — the check's own answer was acted on."""
    a = _analyze(tmp_path, load_fixture, "expected_probe_then_create.atif.json")
    assert _by_name(a)["ignored_tool_failure"].candidates == []


def test_ignored_tool_failure_still_fires_on_unactioned_probe(tmp_path, load_fixture):
    """The same probe shape, but nothing ever creates the missing file —
    a genuinely ignored failure, which must still be flagged."""
    a = _analyze(tmp_path, load_fixture, "probe_without_followup.atif.json")
    d = _by_name(a)["ignored_tool_failure"]
    assert len(d.candidates) == 1
    assert d.candidates[0].anchor_event_ids[0] == "evt_003"  # the failing tool_result


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


# --- item 6 (2026-09-07): terminal-state moment for non-submitted runs -------

def _terminal_ctx(terminal_kind, checks, *, with_submission=False, tool_results="complete"):
    from agr.detectors import DetectorContext
    from agr.schema import CapabilityProfile, DerivedEvent

    events = [
        DerivedEvent(event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
                     source_step_ids=["s1"], event_type="tool_call", actor="main_agent",
                     payload={"tool": "shell", "content": "pytest tests/"}),
        DerivedEvent(event_id="evt_2", run_id="r", source_capture_id="c", sequence=2,
                     source_step_ids=["s2"], event_type="tool_result", actor="tool",
                     payload={"tool": "shell", "content": "E: failed", "exit_code": 1}),
    ]
    if with_submission:
        events.append(DerivedEvent(
            event_id="evt_3", run_id="r", source_capture_id="c", sequence=3,
            source_step_ids=["s3"], event_type="final_submission", actor="main_agent",
            payload={"content": "done"}))
    events.append(DerivedEvent(
        event_id="evt_4", run_id="r", source_capture_id="c", sequence=4,
        source_step_ids=["s4"], event_type=terminal_kind, actor="harness",
        payload={"content": "the harness ended the run"}))
    prof = CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete", "tool_results": tool_results})
    return DetectorContext(run_id="r", capture_id="c", events=events, checks=checks, doc={}, profile=prof)


def _failed_check():
    from agr.schema import VerifierCheck
    return VerifierCheck(check_id="tests_pass", run_id="r", source_capture_id="c",
                         name="tests pass", status="failed", source="native_structured")


def test_terminal_failure_with_failing_checks_fires_on_run_timed_out():
    """Item 6 acceptance: a run that timed out (no observed submission) with a
    failing check produces a candidate anchored on the terminal event and the
    last agent action — never an empty review for a plainly failed run."""
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_timed_out", [_failed_check()])
    cands = TerminalFailureWithFailingChecks().run(ctx).candidates
    assert len(cands) == 1
    assert cands[0].anchor_event_ids[0] == "evt_4"  # the terminal event
    assert "evt_1" in cands[0].anchor_event_ids     # the last agent action
    assert cands[0].affected_checks == ["tests_pass"]
    assert cands[0].structured_facts[0]["terminal_event_type"] == "run_timed_out"


def test_terminal_failure_with_failing_checks_fires_on_run_failed():
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_failed", [_failed_check()])
    cands = TerminalFailureWithFailingChecks().run(ctx).candidates
    assert len(cands) == 1


def test_terminal_failure_detector_silent_when_submission_observed():
    """A submitted run is UnresolvedRequirementAtSubmission's territory —
    never double-counted by this detector."""
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_failed", [_failed_check()], with_submission=True)
    assert TerminalFailureWithFailingChecks().run(ctx).candidates == []


def test_terminal_failure_detector_silent_on_run_completed():
    """A normal harness-side completion (no submission observed, but not a
    failure/timeout either) is out of this detector's scope."""
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_completed", [_failed_check()])
    assert TerminalFailureWithFailingChecks().run(ctx).candidates == []


def test_terminal_failure_detector_silent_when_checks_pass():
    from agr.detectors import TerminalFailureWithFailingChecks
    from agr.schema import VerifierCheck
    passed = VerifierCheck(check_id="tests_pass", run_id="r", source_capture_id="c",
                           name="tests pass", status="passed", source="native_structured")
    ctx = _terminal_ctx("run_timed_out", [passed])
    assert TerminalFailureWithFailingChecks().run(ctx).candidates == []


# --- AGR-08: one aggregate candidate per run, not one per failing check -----


def _three_checks(*, one_passing=True):
    from agr.schema import VerifierCheck
    checks = [
        VerifierCheck(check_id="C1", run_id="r", source_capture_id="c", name="c1",
                      status="failed", source="native_structured"),
        VerifierCheck(check_id="C2", run_id="r", source_capture_id="c", name="c2",
                      status="failed", source="native_structured"),
        VerifierCheck(check_id="C3", run_id="r", source_capture_id="c", name="c3",
                      status="failed", source="native_structured"),
    ]
    if one_passing:
        checks.append(VerifierCheck(check_id="C4", run_id="r", source_capture_id="c", name="c4",
                                    status="passed", source="native_structured"))
    return checks


def test_terminal_failure_with_failing_checks_emits_one_aggregate_candidate():
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_timed_out", _three_checks())
    cands = TerminalFailureWithFailingChecks().run(ctx).candidates
    assert len(cands) == 1
    cand = cands[0]
    assert set(cand.affected_checks) == {"C1", "C2", "C3"}
    assert "C4" not in cand.affected_checks  # the passing check is not carried
    assert len(cand.structured_facts) == 3
    assert {f["check_id"] for f in cand.structured_facts} == {"C1", "C2", "C3"}
    assert all(f["total_checks"] == 4 for f in cand.structured_facts)


def test_total_checks_uses_the_reconciled_denominator_not_every_observation(tmp_path):
    """AGR-08 (review 82cc113): a fixture with four HISTORICAL observations
    but three CURRENT (reconciled) checks must report total_checks == 3, not
    the raw len(ctx.checks) == 4 — an earlier same-scope observation a later
    one superseded no longer speaks for the run's outcome (agr.checks.
    reconcile_checks) and must not inflate this denominator either."""
    from agr.detectors import TerminalFailureWithFailingChecks
    from agr.schema import VerifierCheck
    checks = [
        # Superseded by C1b: excluded from the CURRENT view (effective_status
        # is None), so it must not count toward total_checks.
        VerifierCheck(check_id="C1a", run_id="r", source_capture_id="c", name="c1",
                      status="failed", source="output_interpretation", timing="during_run",
                      scope="pytest tests/", sequence=0, superseded_by="C1b"),
        VerifierCheck(check_id="C1b", run_id="r", source_capture_id="c", name="c1",
                      status="failed", source="output_interpretation", timing="during_run",
                      scope="pytest tests/", sequence=1),
        VerifierCheck(check_id="C2", run_id="r", source_capture_id="c", name="c2",
                      status="failed", source="native_structured"),
        VerifierCheck(check_id="C3", run_id="r", source_capture_id="c", name="c3",
                      status="passed", source="native_structured"),
    ]
    ctx = _terminal_ctx("run_timed_out", checks)
    cands = TerminalFailureWithFailingChecks().run(ctx).candidates
    assert len(cands) == 1
    cand = cands[0]
    # C1a is superseded: not a current failure, so not in affected_checks.
    assert set(cand.affected_checks) == {"C1b", "C2"}
    assert all(f["total_checks"] == 3 for f in cand.structured_facts)


def test_terminal_failure_no_last_agent_action_is_recorded_explicitly():
    """When the trace has no main_agent event at all, the limitation is
    carried on the fact rather than silently anchoring only on the terminal
    event with no comment."""
    from agr.detectors import DetectorContext, TerminalFailureWithFailingChecks
    from agr.schema import CapabilityProfile, DerivedEvent

    events = [DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="run_timed_out", actor="harness",
        payload={"content": "the harness ended the run"})]
    prof = CapabilityProfile(run_id="r", source_capture_id="c",
                             capabilities={"messages": "complete"})
    ctx = DetectorContext(run_id="r", capture_id="c", events=events,
                          checks=[_failed_check()], doc={}, profile=prof)
    cands = TerminalFailureWithFailingChecks().run(ctx).candidates
    assert len(cands) == 1
    assert cands[0].anchor_event_ids == ["evt_1"]
    assert cands[0].structured_facts[0]["last_agent_action_captured"] is False


def test_unresolved_requirement_at_submission_emits_one_aggregate_candidate():
    from agr.detectors import UnresolvedRequirementAtSubmission
    ctx = _terminal_ctx("run_completed", _three_checks(), with_submission=True)
    cands = UnresolvedRequirementAtSubmission().run(ctx).candidates
    assert len(cands) == 1
    cand = cands[0]
    assert set(cand.affected_checks) == {"C1", "C2", "C3"}
    assert len(cand.structured_facts) == 3
    assert cand.anchor_event_ids == ["evt_3"]  # the observed submission


def test_unresolved_requirement_at_submission_silent_when_all_pass():
    from agr.detectors import UnresolvedRequirementAtSubmission
    from agr.schema import VerifierCheck
    passed = [VerifierCheck(check_id="C1", run_id="r", source_capture_id="c", name="c1",
                            status="passed", source="native_structured")]
    ctx = _terminal_ctx("run_completed", passed, with_submission=True)
    assert UnresolvedRequirementAtSubmission().run(ctx).candidates == []


def test_terminal_failure_detector_fires_with_partial_tool_results():
    """Review finding #2: _run reads no tool_result event at all, so
    tool_results:partial — the NORMAL capability level for a run the harness
    killed mid-tool-call (a pending call with no observed result) — must not
    gate this detector off. Before the fix, required_capabilities included
    tool_results:complete and this returned evaluated=False on exactly the
    runs the detector exists to cover."""
    from agr.detectors import TerminalFailureWithFailingChecks
    ctx = _terminal_ctx("run_timed_out", [_failed_check()], tool_results="partial")
    result = TerminalFailureWithFailingChecks().run(ctx)
    assert result.evaluated is True
    assert len(result.candidates) == 1


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
