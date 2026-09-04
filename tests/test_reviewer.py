"""The deterministic reviewer envelope — Stages G/H/I (spec §8.8–§8.10).

Mirrors the existing style: real ``analyze`` into a ``tmp_path`` store for
integration, hand-built records for isolated-stage units, plain ``assert`` on
exact values. No model call is involved anywhere in the envelope.
"""

from agr import reviewer
from agr.pipeline import analyze
from agr.schema import Candidate, EvidenceSlice, VerifierCheck
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


def _ctx(candidates, checks=(), slices=(), events=(), recoveries=()):
    return reviewer.ReviewerContext(
        run_id="r", source_capture_id="c", candidates=list(candidates),
        slices=list(slices), checks=list(checks), events=list(events),
        recoveries=list(recoveries),
    )


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


# --- Stage I: moment selection -----------------------------------------------


def test_two_cards_on_one_moment_collapse_to_one():
    # Two candidates anchored at the same event: the higher-value one (touching a
    # failed check) is kept; the other is superseded, not shown.
    checks = [_check("C2", "failed")]
    with_check = _candidate("cand_req", affected_checks=["C2"], anchor_event_ids=["evt_sub"],
                            structured_facts=[{"type": "requirement_status", "check_id": "C2",
                                               "status_at_submission": "failed"}])
    absence = _candidate("cand_abs", anchor_event_ids=["evt_sub"],
                         structured_facts=[{"type": "absence", "declared_artifact": "x.txt"}])
    slices = [_slice("C2", "hypothesized"),
              _slice("cand_abs", "dependency_linked", branch="omission")]
    moments = reviewer.run_reviewer(_ctx([with_check, absence], checks=checks, slices=slices))
    selected = [m for m in moments if m.selected]
    assert len(selected) == 1
    assert selected[0].candidate_id == "cand_req"
    superseded = next(m for m in moments if m.candidate_id == "cand_abs")
    assert superseded.selected is False
    assert superseded.superseded_by == selected[0].moment_id


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
    assert "check C3 was still failing at submission" in m.rendered_statement
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
    # No artifact_observation events -> results.json counts as absent.
    moments = reviewer.run_reviewer(_ctx([cand], checks=[_check("C1", "failed")], events=evs))
    selected = [m for m in moments if m.selected]
    assert len(selected) == 1
    m = selected[0]
    assert m.detector == "model"
    assert m.candidate_id == "sem_1"
    assert m.attribution_ceiling == "hypothesized"
    assert m.gate_results["fact_validation"] == "passed"
    assert all(f["validation"] == "passed" for f in m.validated_facts)
