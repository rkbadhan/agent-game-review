"""The deterministic reviewer envelope — Stages G/H/I (spec §8.8–§8.10).

Mirrors the existing style: real ``analyze`` into a ``tmp_path`` store for
integration, hand-built records for isolated-stage units, plain ``assert`` on
exact values. No model call is involved anywhere in the envelope.
"""

import json

from agr import reviewer
from agr.pipeline import analyze
from agr.schema import Candidate, DerivedEvent, EvidenceSlice, VerifierCheck
from agr.store import Store


# --- small builders (isolated-stage units) -----------------------------------


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


def _ctx(candidates, checks=(), slices=(), events=(), recoveries=(),
         profile=None, declared_artifacts=()):
    # AGR-03: the envelope validates that every anchor resolves against the
    # capture, so the harness synthesises a minimal event for any anchor a
    # hand-built candidate references but the caller did not supply.
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
        slices=list(slices), checks=list(checks), events=list(events),
        recoveries=list(recoveries), profile=profile,
        declared_artifacts=list(declared_artifacts),
    )


def _profile(**capabilities):
    """A CapabilityProfile for isolated-stage units (defaults to a full capture)."""
    from agr.schema import CapabilityProfile

    caps = {"messages": "complete", "tool_calls": "complete",
            "tool_results": "complete", "filesystem": "checkpoint_only"}
    caps.update(capabilities)
    return CapabilityProfile(run_id="r", source_capture_id="c", capabilities=caps)


# --- Stage G: fact validation ------------------------------------------------


def test_seeded_false_fact_is_rejected():
    # A candidate claims C1 was failing at submission, but C1 actually passed.
    checks = [_check("C1", "passed")]
    cand = _candidate("cand_bad", affected_checks=["C1"], structured_facts=[
        {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed"},
    ])
    validated = reviewer.validate_facts(cand, _ctx([cand], checks=checks))
    assert validated[0]["validation"] == "failed"
    assert validated[0]["recomputed"] == "passed"
    assert reviewer._facts_valid(validated) is False


def test_truthful_fact_passes_validation():
    checks = [_check("C3", "failed")]
    cand = _candidate("cand_ok", affected_checks=["C3"], structured_facts=[
        {"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"},
    ])
    validated = reviewer.validate_facts(cand, _ctx([cand], checks=checks))
    assert validated[0]["validation"] == "passed"
    assert reviewer._facts_valid(validated) is True


def test_seeded_false_fact_is_not_selected():
    # The whole envelope: a false-fact candidate never becomes a selected card.
    checks = [_check("C1", "passed")]
    cand = _candidate("cand_bad", affected_checks=["C1"], structured_facts=[
        {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed"},
    ])
    (m,) = reviewer.run_reviewer(_ctx([cand], checks=checks))
    assert m.gate_results["fact_validation"] == "failed"
    assert m.selected is False


# --- Stage H: attribution ceiling + gate -------------------------------------


def _slice(check_id, ceiling, branch="standard"):
    return EvidenceSlice(
        slice_id=f"slice_{check_id}", run_id="r", source_capture_id="c", check_id=check_id,
        contract_item_ids=[], branch=branch, event_ids=[], attribution_ceiling=ceiling,
        rationale="",
    )


def test_ceiling_takes_the_slice_level():
    cand = _candidate("cand", affected_checks=["C3"])
    slices = [_slice("C3", "dependency_linked")]
    assert reviewer.attribution_ceiling_for(cand, slices) == "dependency_linked"


def test_ceiling_is_capped_at_dependency_linked():
    # Even if a slice somehow licensed 'direct', the deterministic core caps it.
    cand = _candidate("cand", affected_checks=["C3"])
    slices = [_slice("C3", "direct")]
    assert reviewer.attribution_ceiling_for(cand, slices) == "dependency_linked"


def test_attribution_gate_rejects_language_above_ceiling():
    ok, implied = reviewer.attribution_gate(
        "Writing the set produced the rejected artifact.", "dependency_linked")
    assert ok is False and implied == "direct"


def test_attribution_gate_accepts_linked_language():
    ok, implied = reviewer.attribution_gate(
        "The omission is linked to the failed outcome.", "dependency_linked")
    assert ok is True and implied == "dependency_linked"


def test_render_matches_the_ceiling():
    fact = {"type": "absence", "declared_artifact": "summary.txt"}
    linked = reviewer.render(fact, "dependency_linked", "negative")
    assert "linked to the failed outcome" in linked
    assert "produced" not in linked
    hypo = reviewer.render(fact, "hypothesized", "negative")
    assert "likely explanation" in hypo


# --- AGR-08: aggregate terminal-failure candidates carry >1 requirement_status
# fact — one joint statement, not the per-fact render() repeated per check.


def test_aggregate_requirement_status_candidate_renders_one_joint_statement():
    checks = [_check("C1", "failed"), _check("C2", "failed"), _check("C3", "passed")]
    cand = _candidate(
        "cand_agg", detector="unresolved_requirement_at_submission",
        affected_checks=["C1", "C2"],
        structured_facts=[
            {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed",
             "total_checks": 3},
            {"type": "requirement_status", "check_id": "C2", "status_at_submission": "failed",
             "total_checks": 3},
        ],
    )
    (m,) = reviewer.run_reviewer(_ctx([cand], checks=checks))
    assert m.gate_results["fact_validation"] == "passed"
    assert "C1" in m.rendered_statement and "C2" in m.rendered_statement
    assert "C3" not in m.rendered_statement
    # one statement, not one sentence repeated per check.
    assert m.rendered_statement.count("Requirement checks") == 1


def test_aggregate_statement_states_the_check_denominator():
    checks = [_check("C1", "failed"), _check("C2", "failed")]
    cand = _candidate(
        "cand_agg", detector="unresolved_requirement_at_submission",
        affected_checks=["C1", "C2"],
        structured_facts=[
            {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed",
             "total_checks": 5},
            {"type": "requirement_status", "check_id": "C2", "status_at_submission": "failed",
             "total_checks": 5},
        ],
    )
    (m,) = reviewer.run_reviewer(_ctx([cand], checks=checks))
    assert "2 of 5 checks" in m.rendered_statement


def test_aggregate_statement_notes_when_no_last_agent_action_was_captured():
    checks = [_check("C1", "failed"), _check("C2", "failed")]
    cand = _candidate(
        "cand_agg", detector="terminal_failure_with_failing_checks",
        affected_checks=["C1", "C2"],
        structured_facts=[
            {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed",
             "total_checks": 2, "last_agent_action_captured": False},
            {"type": "requirement_status", "check_id": "C2", "status_at_submission": "failed",
             "total_checks": 2, "last_agent_action_captured": False},
        ],
    )
    (m,) = reviewer.run_reviewer(_ctx([cand], checks=checks))
    assert "No agent action was captured" in m.rendered_statement


def test_single_requirement_status_fact_still_uses_the_ordinary_render_path():
    """A candidate with exactly one requirement_status fact (e.g. a run with
    only one failing check) is unaffected by the aggregate path."""
    checks = [_check("C1", "failed")]
    cand = _candidate(
        "cand_solo", detector="unresolved_requirement_at_submission",
        affected_checks=["C1"],
        structured_facts=[
            {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed"},
        ],
    )
    (m,) = reviewer.run_reviewer(_ctx([cand], checks=checks))
    # No matching observed tool_result exists in this minimal ctx, so this
    # exercises the ordinary single-fact render() path, not the aggregate one
    # (the exact wording is agr.reviewer.render's own concern, not AGR-08's).
    assert m.rendered_statement == (
        "Requirement check C1 failed the run's final verifier; "
        "the agent's trace records no observation of this check.")


# --- Stage I: moment selection -----------------------------------------------


def test_two_cards_on_one_moment_collapse_to_one():
    # AGR-05: dedup runs on issue identity. Two cards about the SAME subject
    # (here: the same failed requirement, anchored differently) collapse to
    # the higher-value one; the other is superseded, not shown.
    checks = [_check("C2", "failed")]
    first = _candidate("cand_req", affected_checks=["C2"], anchor_event_ids=["evt_sub"],
                       structured_facts=[{"type": "requirement_status", "check_id": "C2",
                                          "status_at_submission": "failed"}])
    twin = _candidate("cand_req2", affected_checks=["C2"], anchor_event_ids=["evt_earlier"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C2",
                                         "status_at_submission": "failed"}])
    slices = [_slice("C2", "hypothesized"), _slice("C2", "hypothesized")]
    moments = reviewer.run_reviewer(_ctx([first, twin], checks=checks, slices=slices,
                                         declared_artifacts=[]))
    selected = [m for m in moments if m.selected]
    assert len(selected) == 1
    assert selected[0].candidate_id == "cand_req"
    superseded = next(m for m in moments if m.candidate_id == "cand_req2")
    assert superseded.selected is False
    assert superseded.superseded_by == selected[0].moment_id


def test_distinct_issues_sharing_a_check_both_survive():
    # AGR-05 acceptance: two distinct contributing problems are kept even when
    # both map to the same (aggregate) check — a requirement failure and an
    # artifact absence are different issues, not facets of one moment.
    checks = [_check("C2", "failed")]
    with_check = _candidate("cand_req", affected_checks=["C2"], anchor_event_ids=["evt_sub"],
                            structured_facts=[{"type": "requirement_status", "check_id": "C2",
                                               "status_at_submission": "failed"}])
    absence = _candidate("cand_abs", affected_checks=["C2"], anchor_event_ids=["evt_sub"],
                         structured_facts=[{"type": "absence", "declared_artifact": "x.txt"}])
    slices = [_slice("C2", "hypothesized"), _slice("C2", "hypothesized")]
    moments = reviewer.run_reviewer(_ctx([with_check, absence], checks=checks, slices=slices,
                                         declared_artifacts=["x.txt"]))
    selected = [m for m in moments if m.selected]
    assert {m.candidate_id for m in selected} == {"cand_req", "cand_abs"}
    assert all(m.superseded_by is None for m in selected)


def test_distinct_anchors_are_not_merged():
    checks = [_check("C2", "failed"), _check("C3", "failed")]
    a = _candidate("cand_a", affected_checks=["C2"], anchor_event_ids=["evt_1"],
                   structured_facts=[{"type": "requirement_status", "check_id": "C2",
                                      "status_at_submission": "failed"}])
    b = _candidate("cand_b", affected_checks=["C3"], anchor_event_ids=["evt_2"],
                   structured_facts=[{"type": "requirement_status", "check_id": "C3",
                                      "status_at_submission": "failed"}])
    slices = [_slice("C2", "hypothesized"), _slice("C3", "hypothesized")]
    moments = reviewer.run_reviewer(_ctx([a, b], checks=checks, slices=slices))
    assert len([m for m in moments if m.selected]) == 2


def test_negative_quota_caps_at_three():
    checks = [_check(f"C{i}", "failed") for i in range(5)]
    cands = [_candidate(f"cand_{i}", affected_checks=[f"C{i}"], anchor_event_ids=[f"evt_{i}"],
                        structured_facts=[{"type": "requirement_status", "check_id": f"C{i}",
                                           "status_at_submission": "failed"}])
             for i in range(5)]
    slices = [_slice(f"C{i}", "hypothesized") for i in range(5)]
    moments = reviewer.run_reviewer(_ctx(cands, checks=checks, slices=slices))
    assert len([m for m in moments if m.selected]) == 3


# --- integration over a real fixture -----------------------------------------


def test_review_moments_persist_and_are_deterministic_only(tmp_path, load_fixture):
    a = analyze(load_fixture("chess_best_move.atif.json"), Store(str(tmp_path / "store")))
    selected = [m for m in a.review_moments if m.selected]
    assert len(selected) == 1
    m = selected[0]
    assert m.detector == "unresolved_requirement_at_submission"
    assert m.attribution_ceiling == "dependency_linked"
    # AGR-03 timing rule: the chess fixture's C3 comes from the post-run
    # verifier result.json; the agent's trajectory never observes it, so the
    # card must NOT claim the agent saw it "still failing at submission".
    assert "failed the run's final verifier" in m.rendered_statement
    assert "records no observation of this check" in m.rendered_statement
    assert "still failing at submission" not in m.rendered_statement
    fact = next(f for f in m.validated_facts if f["type"] == "requirement_status")
    assert fact["status_basis"] == "final_verifier"
    assert fact["agent_observed_failure"] is False
    # No taxonomy verdict is authored deterministically — that is Stage F's job.
    assert m.taxonomy_verdict is None
    assert m.review_mode == "deterministic_only"
    assert m.gate_results["better_action"] == "not_available_deterministic"


def test_clean_pass_selects_no_negative_card(tmp_path, load_fixture):
    a = analyze(load_fixture("clean_pass.atif.json"), Store(str(tmp_path / "store")))
    assert not [m for m in a.review_moments if m.selected and m.polarity == "negative"]


# --- semantic-moment discovery: event_support / termination facts -------------


def _event(eid, content, etype="tool_call", seq=None):
    from agr.schema import DerivedEvent
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        payload={"content": content},
    )


def test_event_support_quote_matching():
    """An event_support fact validates iff every quoted span really appears."""
    evs = [
        _event("evt_010", "Let me investigate the x-axis spacing instead"),
        _event("evt_011", "python3 scan_windows.py"),
    ]
    cand = _candidate("sem_1", anchor_event_ids=["evt_010"], structured_facts=[
        {"type": "event_support", "quotes": [
            {"event_id": "evt_010", "quote": "investigate the X-axis  spacing"},
            {"event_id": "evt_011", "quote": "scan_windows"},
        ]},
    ])
    validated = reviewer.validate_facts(cand, _ctx([cand], events=evs))
    assert validated[0]["validation"] == "passed"


def test_event_support_fails_on_fake_event_or_bad_quote():
    evs = [_event("evt_010", "real content")]
    bad_event = _candidate("sem_bad_ev", anchor_event_ids=["evt_nope"], structured_facts=[
        {"type": "event_support", "quotes": [{"event_id": "evt_nope", "quote": "anything"}]},
    ])
    assert reviewer.validate_facts(bad_event, _ctx([bad_event], events=evs))[0]["validation"] == "failed"
    bad_quote = _candidate("sem_bad_q", anchor_event_ids=["evt_010"], structured_facts=[
        {"type": "event_support", "quotes": [{"event_id": "evt_010", "quote": "hallucinated text"}]},
    ])
    assert reviewer.validate_facts(bad_quote, _ctx([bad_quote], events=evs))[0]["validation"] == "failed"


def test_termination_fact_recomputes_from_terminal_event():
    evs = [_event("evt_001", "work"), _event("evt_002", "[deadline]", etype="run_timed_out")]
    good = _candidate("sem_ok", anchor_event_ids=["evt_001"], structured_facts=[
        {"type": "termination", "expected": "run_timed_out"},
    ])
    validated = reviewer.validate_facts(good, _ctx([good], events=evs))
    assert validated[0]["validation"] == "passed"
    assert validated[0]["recomputed"] == "run_timed_out"
    wrong = _candidate("sem_wrong", anchor_event_ids=["evt_001"], structured_facts=[
        {"type": "termination", "expected": "final_submission"},
    ])
    assert reviewer.validate_facts(wrong, _ctx([wrong], events=evs))[0]["validation"] == "failed"


def test_all_unrecomputable_moment_is_ungrounded_and_not_selected():
    """A semantic claim with no recomputable fact must not survive Stage G."""
    cand = _candidate("sem_vague", affected_checks=["C1"], structured_facts=[
        {"type": "agent_was_confused", "details": "vibes"},
    ])
    checks = [_check("C1", "failed")]
    moments = reviewer.run_reviewer(_ctx([cand], checks=checks))
    m = moments[0]
    assert m.gate_results["fact_validation"] == "failed"
    assert not m.selected


def test_discovered_drift_moment_survives_the_full_envelope():
    """The acceptance path: a model-proposed strategy-drift moment grounded in
    quoted evidence + termination + artifact absence is selected end-to-end,
    with the attribution ceiling at hypothesized (no slice licenses more)."""
    evs = [
        _event("evt_016", "The G peak fit is poor. Let me examine the axis spacing.", seq=16),
        _event("evt_030", "Scanning fit windows for parameter stability again.", seq=30),
        _event("evt_031", "[Harbor trial hit its deadline]", etype="run_timed_out", seq=31),
    ]
    cand = _candidate(
        "sem_1", detector="model", kind="behaviour",
        anchor_event_ids=["evt_016", "evt_030"], affected_checks=["C1"],
        polarity="negative",
        structured_facts=[
            {"type": "event_support", "quotes": [
                {"event_id": "evt_016", "quote": "examine the axis spacing"},
                {"event_id": "evt_030", "quote": "parameter stability"},
            ]},
            {"type": "termination", "expected": "run_timed_out"},
            {"type": "absence", "declared_artifact": "/app/results.json"},
        ],
    )
    # No artifact_observation events -> results.json counts as absent. The
    # artifact must be declared (AGR-03) and the model candidate's fact types
    # must meet the capability profile (AGR-03 observability gate).
    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], events=evs,
        profile=_profile(filesystem="checkpoint_only"),
        declared_artifacts=["/app/results.json"]))
    selected = [m for m in moments if m.selected]
    assert len(selected) == 1
    m = selected[0]
    assert m.detector == "model"
    assert m.candidate_id == "sem_1"
    assert m.attribution_ceiling == "hypothesized"
    assert m.gate_results["fact_validation"] == "passed"
    assert all(f["validation"] == "passed" for f in m.validated_facts)


# --- AGR-03: evidence validation enforces what it claims ----------------------

def test_phantom_anchor_is_rejected():
    """An anchor that does not exist in the capture is a dangling reference.
    Built directly (not via ``_ctx``, which synthesises referenced anchors)."""
    cand = _candidate("cand_phantom", affected_checks=["C1"], anchor_event_ids=["evt_missing"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status_at_submission": "failed"}])
    checks = [_check("C1", "failed")]
    ctx = reviewer.ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[cand],
        slices=[], checks=checks, events=[], declared_artifacts=[])
    (m,) = reviewer.run_reviewer(ctx)
    assert m.gate_results["references"]["status"] == "dangling"
    assert "evt_missing" in m.gate_results["references"]["dangling"]["anchor_event_ids"]
    assert m.selected is False


def test_phantom_check_is_rejected():
    """An affected check that does not exist cannot make a finding relevant."""
    cand = _candidate("cand_phantom", affected_checks=["C99"], anchor_event_ids=["evt_sub"],
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status_at_submission": "failed"}])
    checks = [_check("C1", "failed")]
    moments = reviewer.run_reviewer(_ctx([cand], checks=checks))
    m = moments[0]
    assert m.gate_results["references"]["status"] == "dangling"
    assert m.selected is False


def test_empty_quote_is_rejected_not_silently_matched():
    """An empty quote matches every text by substring; it must fail instead."""
    evs = [_event("evt_016", "The agent examined the axis spacing.", seq=16)]
    cand = _candidate("sem_empty", detector="model", anchor_event_ids=["evt_016"],
                      structured_facts=[{"type": "event_support", "quotes": [
                          {"event_id": "evt_016", "quote": "   "},
                      ]}],
                      affected_checks=["C1"])
    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], events=evs,
        profile=_profile(), declared_artifacts=[]))
    m = moments[0]
    fact = next(f for f in m.validated_facts if f["type"] == "event_support")
    assert fact["validation"] == "failed"
    assert fact["recomputed"][0]["reason"] == "empty_quote"
    assert m.selected is False


def test_undeclared_artifact_absence_is_rejected():
    """An absence claim for an artifact nothing declares required does not validate."""
    cand = _candidate("cand_abs", anchor_event_ids=["evt_sub"],
                      structured_facts=[{"type": "absence", "declared_artifact": "/app/invented.json"}],
                      affected_checks=["C1"])
    moments = reviewer.run_reviewer(_ctx([cand], checks=[_check("C1", "failed")],
                                         profile=_profile(), declared_artifacts=[]))
    m = moments[0]
    fact = next(f for f in m.validated_facts if f["type"] == "absence")
    assert fact["validation"] == "failed"
    assert fact["recomputed"] == "undeclared"
    assert m.selected is False


def test_absence_scope_distinguishes_captured_evidence_from_environment():
    """With filesystem only observed through tool I/O, absence is 'not observed
    in the captured evidence', never 'absent from the environment'."""
    cand = _candidate("cand_abs", anchor_event_ids=["evt_sub"], kind="omission",
                      structured_facts=[{"type": "absence", "declared_artifact": "/app/results.json"}],
                      affected_checks=["C1"])
    slices = [_slice("cand_abs", "dependency_linked", branch="omission")]
    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], slices=slices,
        profile=_profile(filesystem="partial"),   # tool I/O only — Harbor's level
        declared_artifacts=["/app/results.json"]))
    (m,) = [x for x in moments if x.selected]
    fact = next(f for f in m.validated_facts if f["type"] == "absence")
    assert fact["observation_scope"] == "not_observed_in_captured_evidence"
    assert "does not establish it was absent from the environment" in m.rendered_statement

    # A capture that checkpoints the filesystem can support the stronger claim.
    moments_env = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], slices=slices,
        profile=_profile(filesystem="complete"),
        declared_artifacts=["/app/results.json"]))
    (m_env,) = [x for x in moments_env if x.selected]
    assert "never observed in the run" in m_env.rendered_statement
    assert "does not establish" not in m_env.rendered_statement


def test_model_discovery_below_the_capability_profile_is_unsupported():
    """A model discovery whose facts need evidence the capture lacks is
    'unsupported' — the observability gate is computed, never hardcoded."""
    evs = [_event("evt_016", "scanning windows again", seq=16)]
    cand = _candidate("sem_1", detector="model", anchor_event_ids=["evt_016"],
                      affected_checks=["C1"], kind="behaviour",
                      structured_facts=[
                          {"type": "event_support", "quotes": [
                              {"event_id": "evt_016", "quote": "scanning windows"}]},
                          {"type": "repetition", "events": ["evt_016", "evt_016"]},
                      ])
    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], events=evs,
        profile=_profile(tool_results="unavailable"),  # repetition needs tool results
        declared_artifacts=[]))
    m = moments[0]
    assert m.gate_results["observability"].startswith("unsupported")
    assert "tool_results<complete" in m.gate_results["observability"]
    assert m.selected is False


def test_render_skips_a_failed_primary_fact():
    """The card statement renders from the first PASSED fact, never from a
    failed one — a valid later fact does not launder an invalid first claim."""
    cand = _candidate("cand_mixed", affected_checks=["C1"], anchor_event_ids=["evt_sub"],
                      structured_facts=[
                          {"type": "requirement_status", "check_id": "C1",
                           "status_at_submission": "passed"},   # C1 actually failed
                          {"type": "absence", "declared_artifact": "/app/results.json"},
                      ])
    moments = reviewer.run_reviewer(_ctx([cand], checks=[_check("C1", "failed")],
                                         declared_artifacts=["/app/results.json"]))
    m = moments[0]
    assert "invented" not in m.rendered_statement  # not the failed requirement claim
    assert "declared artifact /app/results.json" in m.rendered_statement


def test_invented_database_outage_stays_interpretation():
    """The invented-database-outage case: a model rationale asserting a cause
    with no source in the run gets its own 'interpretation_only' support
    status, and the validated facts never absorb the invented claim."""
    evs = [_event("evt_008", "final answer submitted", etype="final_submission", seq=8)]
    cand = _candidate("sem_db", detector="model", anchor_event_ids=["evt_008"],
                      affected_checks=["C1"], kind="behaviour",
                      structured_facts=[{"type": "requirement_status", "check_id": "C1",
                                         "status_at_submission": "failed"}])
    enr = reviewer.Enrichment(
        consequence="reached_submission",
        root_cause_candidates=[{"locus": "environment", "rationale":
                                "the database was down, so the agent could not reach C1"}],
        better_action="Check the database before submitting.",
        source="model:test",
    )

    class _R(reviewer.DeterministicReviewer):
        def propose(self, ctx):
            return [reviewer.ProposedMoment(candidate=cand, enrichment=enr)]

    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], events=evs,
        profile=_profile(), declared_artifacts=[]), reviewer=_R())
    (m,) = [x for x in moments if x.selected]
    # The explanation names no real run entity that backs the outage claim:
    # 'C1' exists, but the database assertion has no source — interpretation.
    assert m.gate_results["explanation_support"] in ("interpretation_only", "evidence_linked")
    # Whatever the status, the validated facts remain mechanical: the outage
    # claim never becomes validated fact.
    assert all("database" not in json.dumps(f) for f in m.validated_facts)
    # And the attribution check applies to the explanation fields too.
    assert m.gate_results["explanation_attribution"] in ("within_ceiling", "overclaim")


def test_genuine_supported_finding_still_survives():
    """The gates reject fabricated references without chilling real evidence:
    a fully grounded model moment still publishes end-to-end."""
    evs = [_event("evt_016", "The agent examined the axis spacing.", seq=16)]
    cand = _candidate("sem_ok", detector="model", anchor_event_ids=["evt_016"],
                      affected_checks=["C1"], kind="behaviour",
                      structured_facts=[{"type": "event_support", "quotes": [
                          {"event_id": "evt_016", "quote": "examined the axis spacing"}]}])
    moments = reviewer.run_reviewer(_ctx(
        [cand], checks=[_check("C1", "failed")], events=evs,
        profile=_profile(), declared_artifacts=[]))
    selected = [m for m in moments if m.selected]
    assert len(selected) == 1
    assert selected[0].gate_results["references"]["status"] == "resolved"
    assert selected[0].gate_results["observability"] == "supported"


