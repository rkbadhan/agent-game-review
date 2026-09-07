"""F1/F7 follow-up probes (review 2026-09-06): invented fact fields and nested
references must not become validated prose.

The probes reproduce validator vulnerabilities through the real
``validate_facts`` / ``render`` / ``run_reviewer`` boundary using scripted model
proposals. They establish that the validator now rejects or accurately scopes
each case, not how often a live model errs.
"""

from agr import reviewer
from agr.recovery import GOOD_RECOVERY, UNRECOVERED
from agr.schema import Candidate, DerivedEvent, RecoveryEpisode, VerifierCheck


def _check(check_id, status, expected=None, observed=None):
    return VerifierCheck(
        check_id=check_id, run_id="r", source_capture_id="c", name=check_id,
        status=status, source="native_structured", expected=expected, observed=observed,
    )


def _candidate(cid, **kw):
    kw.setdefault("kind", "omission")
    kw.setdefault("anchor_event_ids", ["evt_sub"])
    return Candidate(candidate_id=cid, run_id="r", source_capture_id="c",
                     detector=kw.pop("detector", "det"), **kw)


def _event(eid, content, etype="tool_call", seq=None):
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        payload={"content": content},
    )


def _ctx(candidates, checks=(), events=(), recoveries=()):
    supplied = {e.event_id for e in events}
    for cand in candidates:
        for aid in cand.anchor_event_ids:
            if aid not in supplied:
                events = list(events) + [DerivedEvent(
                    event_id=aid, run_id="r", source_capture_id="c",
                    sequence=len(supplied) + 1, source_step_ids=[aid],
                    event_type="tool_call", actor="agent")]
                supplied.add(aid)
    return reviewer.ReviewerContext(
        run_id="r", source_capture_id="c", candidates=list(candidates),
        slices=[], checks=list(checks), events=list(events),
        recoveries=list(recoveries), profile=None,
        declared_artifacts=[], task_instruction=None,
    )


def _recovery(failure_event_id, resolution_event_id, classification):
    return RecoveryEpisode(
        episode_id="ep_1", run_id="r", source_capture_id="c",
        classification=classification, failure_event_id=failure_event_id,
        resolution_event_id=resolution_event_id,
        strategy_changed=True, changed_action=True,
    )


# --- F1 probe 1: state_transition --------------------------------------------


def test_state_transition_cannot_invent_a_resolution_event():
    """A fact naming a failure and an invented resolution event is rejected:
    mere event existence is not a validated transition — the claim must match
    the episode's actual failure→resolution relationship."""
    evs = [
        _event("evt_001", "run pytest", etype="tool_call", seq=1),
        _event("evt_002", "task received", etype="task_received", seq=2),
    ]
    cand = _candidate("sem_st", detector="model", kind="behaviour",
                      anchor_event_ids=["evt_001"],
                      structured_facts=[{"type": "state_transition",
                                         "failure_event": "evt_001",
                                         "resolution_event": "evt_DOES_NOT_EXIST"}])
    validated = reviewer.validate_facts(cand, _ctx(
        [cand], events=evs,
        recoveries=[_recovery("evt_001", None, UNRECOVERED)]))
    assert validated[0]["validation"] == "failed"

    # Even a real event fails when the episode resolved elsewhere or not at all.
    cand2 = _candidate("sem_st2", detector="model", kind="behaviour",
                       anchor_event_ids=["evt_001"],
                       structured_facts=[{"type": "state_transition",
                                          "failure_event": "evt_001",
                                          "resolution_event": "evt_002"}])
    validated2 = reviewer.validate_facts(cand2, _ctx(
        [cand2], events=evs,
        recoveries=[_recovery("evt_001", None, UNRECOVERED)]))
    assert validated2[0]["validation"] == "failed"

    # A truthful claim matching the episode passes and is canonicalised from it.
    cand3 = _candidate("sem_st3", detector="model", kind="recovery", polarity="positive",
                       anchor_event_ids=["evt_001"],
                       structured_facts=[{"type": "state_transition",
                                          "failure_event": "evt_001",
                                          "resolution_event": "evt_009"}])
    ep = _recovery("evt_001", "evt_009", GOOD_RECOVERY)
    validated3 = reviewer.validate_facts(cand3, _ctx([cand3], events=evs, recoveries=[ep]))
    assert validated3[0]["validation"] == "passed"
    assert validated3[0]["resolution_event"] == "evt_009"


def test_state_transition_without_an_episode_is_not_validated_by_existence():
    """The old fallback validated a state_transition for ANY existing failure
    event with no recovery episode at all; that path is closed."""
    evs = [_event("evt_001", "run pytest", etype="tool_call", seq=1)]
    cand = _candidate("sem_st", detector="model", kind="behaviour",
                      anchor_event_ids=["evt_001"],
                      structured_facts=[{"type": "state_transition",
                                         "failure_event": "evt_001"}])
    validated = reviewer.validate_facts(cand, _ctx([cand], events=evs))
    assert validated[0]["validation"] == "failed"
    assert validated[0]["recomputed"] == "no_recovery_episode"


# --- F1 probe 2: requirement_status detail -----------------------------------


def test_invented_expected_observed_values_do_not_survive_validation():
    """Invented check detail is replaced by the check record — the rendered
    statement sources expected/observed from the verifier, never the proposal."""
    check = _check("C1", "failed", expected=["g2e4"], observed=["g2e4"])
    cand = _candidate("sem_inv", detector="model", anchor_event_ids=["evt_sub"],
                      affected_checks=["C1"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status_at_submission": "failed",
                                         "expected": "production database intact",
                                         "observed": "production database deleted"}])
    validated = reviewer.validate_facts(cand, _ctx([cand], checks=[check]))
    assert validated[0]["validation"] == "passed"
    assert validated[0]["expected"] == ["g2e4"]
    assert validated[0]["observed"] == ["g2e4"]
    rendered = reviewer.render(validated[0], "hypothesized", "negative")
    assert "database" not in rendered and "g2e4" in rendered


# --- F1 probe 3: requirement_status polarity ---------------------------------


def test_passing_check_submitted_as_passed_renders_as_a_pass():
    """A passing check claimed with its true status validates — and the renderer
    states a pass, not the failure wording the old template assumed."""
    check = _check("C1", "passed")
    cand = _candidate("sem_pass", detector="model", anchor_event_ids=["evt_sub"],
                      affected_checks=["C1"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status": "passed"}])
    validated = reviewer.validate_facts(cand, _ctx([cand], checks=[check]))
    assert validated[0]["validation"] == "passed"
    assert validated[0]["status"] == "passed"
    rendered = reviewer.render(validated[0], "hypothesized", "negative")
    assert "passed the run's final verifier" in rendered
    assert "failed" not in rendered


# --- F1 probe 4: repetition semantics ----------------------------------------


def test_repetition_with_differing_outputs_is_rejected():
    """Two identical poll commands whose recorded results differ are an
    observation with new information — the validator shares the detector's
    output-equivalence semantics instead of validating signatures alone."""
    evs = [
        _event("evt_a", "check status", etype="tool_call", seq=1),
        _event("evt_ar", "3 jobs running", etype="tool_result", seq=2),
        _event("evt_b", "check status", etype="tool_call", seq=3),
        _event("evt_br", "4 jobs running", etype="tool_result", seq=4),
    ]
    cand = _candidate("sem_rep", detector="model", anchor_event_ids=["evt_b"],
                      structured_facts=[{"type": "repetition",
                                         "events": ["evt_a", "evt_b"]}])
    validated = reviewer.validate_facts(cand, _ctx([cand], events=evs))
    assert validated[0]["validation"] == "failed"

    # The equivalent-output case still passes.
    evs[3] = _event("evt_br", "3 jobs running", etype="tool_result", seq=4)
    validated2 = reviewer.validate_facts(cand, _ctx([cand], events=evs))
    assert validated2[0]["validation"] == "passed"


# --- F7: explanation support -------------------------------------------------


def test_explanation_mentioning_a_real_id_is_still_interpretation():
    """'C1: The production database was deleted…' names a real check, but
    identifier existence is reference validity, not semantic support — the
    explanation is interpretation_only, never evidence_linked."""
    enr = reviewer.Enrichment(
        consequence="service_outage",
        root_cause_candidates=[{"locus": "environment", "rationale":
                                "C1: The production database was deleted and the "
                                "customer service went offline"}],
        source="model:test",
    )
    evs = [_event("evt_sub", "final answer submitted", etype="final_submission")]
    ctx = _ctx([_candidate("x", affected_checks=["C1"])], checks=[_check("C1", "failed")],
               events=evs)
    assert reviewer._explanation_support(enr, ctx) == "interpretation_only"


def test_explanation_quotes_earn_evidence_linked_only_when_they_match():
    """Quoted spans are the only observational support for explanations —
    verified against the recorded event text like an event_support fact."""
    evs = [_event("evt_016", "The agent examined the axis spacing.", seq=16)]
    enr_quoted = reviewer.Enrichment(
        consequence="reached_submission",
        root_cause_candidates=[{"locus": "agent_decision", "rationale": "stopped early"}],
        quotes=[{"event_id": "evt_016", "quote": "examined the axis spacing"}],
        source="model:test",
    )
    enr_bad = reviewer.Enrichment(
        consequence="reached_submission",
        root_cause_candidates=[{"locus": "agent_decision", "rationale": "stopped early"}],
        quotes=[{"event_id": "evt_016", "quote": "the database was deleted"}],
        source="model:test",
    )
    ctx = _ctx([_candidate("x")], events=evs)
    # Review 2026-09-07: quote authenticity and explanation support are
    # SEPARATE dimensions — an authentic quote does not upgrade the prose.
    assert reviewer._explanation_support(enr_quoted, ctx) == "interpretation_only"
    assert reviewer._quote_authenticity(enr_quoted, ctx) == "authentic"
    assert reviewer._explanation_support(enr_bad, ctx) == "dangling_references"
    assert reviewer._quote_authenticity(enr_bad, ctx) == "dangling"


def test_authentic_quote_of_something_else_never_upgrades_invented_prose():
    """Review 2026-09-07 reproduction: adding a valid quote (here, of a task
    instruction) to an invented database/outage rationale must NOT yield
    selected, evidence_linked — matching a quote establishes quote
    authenticity, not support for the accompanying factual assertions."""
    evs = [_event("evt_001", "Deploy the service and verify the health check.", seq=1)]
    enr = reviewer.Enrichment(
        consequence="the production database was deleted, causing an outage",
        root_cause_candidates=[{"locus": "environment", "rationale":
                                "the production database was deleted by a background job"}],
        # A TRUE quote of the recorded instruction — unrelated to the claims.
        quotes=[{"event_id": "evt_001",
                 "quote": "Deploy the service and verify the health check."}],
        source="model:test",
    )
    ctx = _ctx([_candidate("x")], events=evs)
    assert reviewer._quote_authenticity(enr, ctx) == "authentic"
    assert reviewer._explanation_support(enr, ctx) == "interpretation_only"


def test_prose_that_only_restates_the_quote_is_evidence_linked():
    """The legitimate case survives: an explanation that states nothing
    beyond the quoted evidence is evidence-linked."""
    evs = [_event("evt_016", "The agent examined the axis spacing.", seq=16)]
    enr = reviewer.Enrichment(
        consequence="examined the axis spacing",
        root_cause_candidates=[],
        quotes=[{"event_id": "evt_016", "quote": "examined the axis spacing"}],
        source="model:test",
    )
    ctx = _ctx([_candidate("x")], events=evs)
    assert reviewer._explanation_support(enr, ctx) == "evidence_linked"
    assert reviewer._quote_authenticity(enr, ctx) == "authentic"


# --- observed-status vocabulary: parse → fact → render end to end ------------


def test_observed_failure_flows_through_fact_validation_and_render():
    """Review 2026-09-07: the parser's status vocabulary is normalized once —
    a real ``C1 FAILED`` observation must yield ``agent_observed_failure: true``
    through fact validation and render as 'still failing at submission',
    instead of a case mismatch recording that no observation was made."""
    from agr.reviewer import render, validate_facts
    events = [
        DerivedEvent(event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
                     source_step_ids=["evt_1"], event_type="tool_result", actor="tool",
                     payload={"content": "C1 FAILED — expected 3 rows, got 0"}),
        DerivedEvent(event_id="evt_2", run_id="r", source_capture_id="c", sequence=2,
                     source_step_ids=["evt_2"], event_type="final_submission",
                     actor="main_agent", payload={"content": "submitted"}),
    ]
    checks = [_check("C1", "failed")]
    cand = _candidate("x", affected_checks=["C1"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status_at_submission": "failed"}])
    ctx = _ctx([cand], checks=checks, events=events)
    fact = validate_facts(cand, ctx)[0]
    assert fact["validation"] == "passed"
    assert fact["agent_observed_failure"] is True
    assert fact["agent_observed_status"] == "failed"
    statement = render(fact, "dependency_linked", "negative")
    assert "still failing at submission" in statement


# --- full-envelope probes -----------------------------------------------------


def test_invented_resolution_never_publishes_through_the_envelope():
    """The same adversarial proposal through the full Stage G→I pipeline: the
    invented-resolution moment is unselected and nothing fabricated renders."""
    from agr.recovery import UNRECOVERED

    evs = [
        _event("evt_001", "run pytest", etype="tool_call", seq=1),
        _event("evt_sub", "final answer submitted", etype="final_submission", seq=9),
    ]
    cand = _candidate("sem_bad", detector="model", kind="behaviour",
                      anchor_event_ids=["evt_001"],
                      structured_facts=[{"type": "state_transition",
                                         "failure_event": "evt_001",
                                         "resolution_event": "evt_DOES_NOT_EXIST"}])
    check = _check("C1", "failed")

    class _R(reviewer.DeterministicReviewer):
        review_mode = "model_enriched"

        def propose(self, ctx):
            return [reviewer.ProposedMoment(candidate=cand, enrichment=None)]

    moments = reviewer.run_reviewer(_ctx([cand], checks=[check], events=evs,
                                         recoveries=[_recovery("evt_001", None, UNRECOVERED)]),
                                    reviewer=_R())
    assert not any(m.selected for m in moments)
    bad = next(m for m in moments if m.candidate_id == "sem_bad")
    assert bad.gate_results["fact_validation"] == "failed"
    assert "evt_DOES_NOT_EXIST" not in bad.rendered_statement
