"""GR-2 — grounded alternatives: kinds, the information cutoff, and status honesty.

Offline (``ScriptedReviewer``), same builders as ``test_model_reviewer.py``.
The three kinds are kept apart, a suggestion that leans on later evidence is
dropped (counted), and only a linked experiment that met the validation rule may
carry a "Validated by …" label.
"""

from agr import reviewer
from agr.model_reviewer import ScriptedReviewer
from agr.reviewer import ReviewerContext, run_reviewer
from agr.schema import (
    Alternative,
    Candidate,
    CapabilityProfile,
    DerivedEvent,
    EvidenceSlice,
    RecoveryEpisode,
    VerifierCheck,
    alternative_label,
)


# --- builders ----------------------------------------------------------------


def _check(cid, status):
    return VerifierCheck(check_id=cid, run_id="r", source_capture_id="c", name=cid,
                         status=status, source="native_structured")


def _cand(cid, **kw):
    kw.setdefault("kind", "omission")
    kw.setdefault("anchor_event_ids", ["evt_sub"])
    return Candidate(candidate_id=cid, run_id="r", source_capture_id="c",
                     detector=kw.pop("detector", "det"), **kw)


def _slice(cid, ceiling):
    return EvidenceSlice(slice_id=f"s_{cid}", run_id="r", source_capture_id="c",
                         check_id=cid, contract_item_ids=[], branch="standard",
                         event_ids=[], attribution_ceiling=ceiling, rationale="")


def _ev(eid, seq):
    return DerivedEvent(event_id=eid, run_id="r", source_capture_id="c",
                        sequence=seq, source_step_ids=[eid],
                        event_type="tool_call", actor="agent")


def _ctx(candidates, events, checks=(), slices=(), recoveries=(), **kw):
    # A full-capability profile by default, so model candidates pass the
    # computed observability gate (same default as test_model_reviewer._ctx).
    kw.setdefault("profile", CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete",
                      "tool_results": "complete", "filesystem": "checkpoint_only"}))
    return ReviewerContext(
        run_id="r", source_capture_id="c", candidates=list(candidates),
        slices=list(slices), checks=list(checks), events=list(events),
        recoveries=list(recoveries), declared_artifacts=[], **kw,
    )


_FAIL_C3 = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]


def _moment(**enr):
    return {
        "candidate_id": "cand_C3", "kind": "omission", "polarity": "negative",
        "anchor_event_ids": ["evt_sub"], "affected_checks": ["C3"],
        "structured_facts": _FAIL_C3, **enr,
    }


def _base_events():
    # evt_early (1) < evt_dec (2) < evt_late (3)
    return [_ev("evt_sub", 1), _ev("evt_early", 1), _ev("evt_dec", 2), _ev("evt_late", 3)]


def _run(payload, *, ctx_extra=None, telemetry=None):
    ctx = _ctx([_cand("cand_C3", affected_checks=["C3"])], _base_events(),
               checks=[_check("C3", "failed")], slices=[_slice("C3", "dependency_linked")],
               **(ctx_extra or {}))
    return run_reviewer(ctx, ScriptedReviewer(payload), telemetry=telemetry)


# --- label mapping -----------------------------------------------------------


def test_alternative_label_maps_every_status():
    assert alternative_label("suggested") == "Suggested alternative"
    assert alternative_label("observed") == "Alternative observed in a comparable passing run"
    assert alternative_label("validated", {"status": "validated_by_replay"}) == "Validated by replay"
    assert alternative_label(
        "validated", {"status": "validated_by_comparable_experiment"}
    ) == "Validated by comparable experiment"
    # A proposed / failed / inconclusive experiment never wears a "Validated" label.
    assert "Validated" not in alternative_label("validated", {"status": "proposed"})
    assert "failed" in alternative_label("validated", {"status": "failed"})
    assert "inconclusive" in alternative_label("validated", {"status": "inconclusive"})


# --- suggested alternative + information cutoff ------------------------------


def test_suggested_alternative_attaches_when_cutoff_holds():
    payload = {"moments": [_moment(alternatives=[{
        "kind": "suggested",
        "proposal": "read only the first 100 lines before parsing",
        "replaces_decision": {"event_id": "evt_dec", "description": "read the whole file"},
        "information_available": ["evt_early"],
    }])]}
    (m,) = [x for x in _run(payload) if x.selected]
    (alt,) = m.alternatives
    assert isinstance(alt, Alternative)
    assert alt.kind == "suggested"
    assert alt.label == "Suggested alternative"
    assert alt.rejected is False
    assert alt.replaces_decision == {"event_id": "evt_dec", "description": "read the whole file"}
    assert [i["event_id"] for i in alt.information_available] == ["evt_early"]


def test_suggestion_using_later_information_is_dropped_and_counted():
    payload = {"moments": [_moment(alternatives=[{
        "kind": "suggested",
        "proposal": "a move only justified by what happened next",
        "replaces_decision": {"event_id": "evt_dec"},
        # evt_late happens AFTER the decision — this is exactly the cutoff violation.
        "information_available": ["evt_late"],
    }])]}
    telemetry = {}
    (m,) = [x for x in _run(payload, telemetry=telemetry) if x.selected]
    assert m.alternatives == []
    assert telemetry["alternatives"]["dropped"]["information_cutoff"] == 1
    assert telemetry["alternatives"]["attached"] == 0


def test_suggestion_with_unrecomputable_assumption_is_dropped():
    payload = {"moments": [_moment(alternatives=[{
        "kind": "suggested",
        "proposal": "assumes a check that never failed",
        "replaces_decision": {"event_id": "evt_dec"},
        "information_available": ["evt_early"],
        "assumptions": [{"type": "requirement_status", "check_id": "C3",
                         "status_at_submission": "passed"}],
    }])]}
    telemetry = {}
    (m,) = [x for x in _run(payload, telemetry=telemetry) if x.selected]
    assert m.alternatives == []
    assert telemetry["alternatives"]["dropped"]["assumptions_unvalidated"] == 1


def test_assumption_grounded_in_later_evidence_is_dropped():
    """PR #91 review: the cutoff binds the ASSUMPTIONS too. An assumption that
    only holds against the final verifier result (not available at the decision)
    must not validate, even though it recomputes against the full context."""
    payload = {"moments": [_moment(alternatives=[{
        "kind": "suggested", "proposal": "x",
        "replaces_decision": {"event_id": "evt_dec"},
        "information_available": ["evt_early"],
        "assumptions": list(_FAIL_C3),  # needs the post-run check result
    }])]}
    telemetry = {}
    (m,) = [x for x in _run(payload, telemetry=telemetry) if x.selected]
    assert m.alternatives == []
    assert telemetry["alternatives"]["dropped"]["assumptions_unvalidated"] == 1


def test_suggestion_with_unknown_decision_is_dropped():
    payload = {"moments": [_moment(alternatives=[{
        "kind": "suggested", "proposal": "x",
        "replaces_decision": {"event_id": "evt_nope"}, "information_available": [],
    }])]}
    telemetry = {}
    (m,) = [x for x in _run(payload, telemetry=telemetry) if x.selected]
    assert m.alternatives == []
    assert telemetry["alternatives"]["dropped"]["decision_unknown"] == 1


def test_model_cannot_claim_a_reserved_kind():
    # The model may only PROPOSE; an observed/validated alternative from prose is ignored.
    payload = {"moments": [_moment(alternatives=[{
        "kind": "validated", "proposal": "trust me, an experiment proved this",
        "replaces_decision": {"event_id": "evt_dec"}, "information_available": [],
    }])]}
    telemetry = {}
    (m,) = [x for x in _run(payload, telemetry=telemetry) if x.selected]
    assert m.alternatives == []
    assert telemetry["alternatives"]["dropped"]["reserved_kind"] == 1


# --- observed + validated from the context -----------------------------------


# --- observed + validated from the STORE, attached at read time ---------------
#
# PR #91 review: both are sourced from records that can change AFTER the review
# was computed (a sibling run, a linked experiment), so they are derived on read
# rather than baked into the persisted moment. The finders themselves are tested
# in test_divergence.py / test_lessons.py; here we test the attach + labels.


def _base_review(tmp_path, load_fixture):
    from agr import read
    from agr.pipeline import analyze
    from agr.store import Store

    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture("chess_best_move.atif.json"), store)
    return store, a.run_source.run_id, read.get_review(store, a.run_source.run_id)


def test_observed_alternative_attaches_at_read_time(tmp_path, load_fixture, monkeypatch):
    from agr import read

    store, run_id, rev0 = _base_review(tmp_path, load_fixture)
    m0 = rev0["moments"][0]
    anchor = m0["anchor_event_ids"][0]
    monkeypatch.setattr("agr.divergence.observed_alternative", lambda s, r: {
        "event_id": anchor, "proposal": "the passing sibling ran the tests first",
        "source": "sibling:run_pass",
        "sibling_diff": {"model": {"failed_run": "m", "passing_run": "m2"}},
    })
    rev = read.get_review(store, run_id)
    m = next(m for m in rev["moments"] if m["moment_id"] == m0["moment_id"])
    (alt,) = [a for a in m["alternatives"] if a["kind"] == "observed"]
    assert alt["label"] == "Alternative observed in a comparable passing run"
    assert alt["sibling_diff"]["model"]["passing_run"] == "m2"


def test_validated_alternative_label_follows_its_status(tmp_path, load_fixture, monkeypatch):
    from agr import read

    store, run_id, rev0 = _base_review(tmp_path, load_fixture)
    m0 = rev0["moments"][0]
    validated = {
        "moment_id": m0["moment_id"], "proposal": "cap the read size",
        "source": "experiment:lesson_1",
        "validation": {"status": "validated_by_comparable_experiment",
                       "stated_benefit": "fewer tokens", "outcome_checks": ["C3 passed"],
                       "comparison_limits": ["single task family"]},
    }
    monkeypatch.setattr("agr.lessons.validated_alternative", lambda s, r: validated)
    rev = read.get_review(store, run_id)
    m = next(m for m in rev["moments"] if m["moment_id"] == m0["moment_id"])
    (alt,) = [a for a in m["alternatives"] if a["kind"] == "validated"]
    assert alt["label"] == "Validated by comparable experiment"
    assert alt["validation"]["outcome_checks"] == ["C3 passed"]


def test_failed_experiment_stays_visible_at_read_time(tmp_path, load_fixture, monkeypatch):
    from agr import read

    store, run_id, rev0 = _base_review(tmp_path, load_fixture)
    m0 = rev0["moments"][0]
    monkeypatch.setattr("agr.lessons.validated_alternative", lambda s, r: {
        "moment_id": m0["moment_id"], "proposal": "cap the read size",
        "source": "experiment:lesson_2",
        "validation": {"status": "failed", "stated_benefit": "x",
                       "outcome_checks": [], "comparison_limits": []},
    })
    rev = read.get_review(store, run_id)
    m = next(m for m in rev["moments"] if m["moment_id"] == m0["moment_id"])
    (alt,) = [a for a in m["alternatives"] if a["kind"] == "validated"]
    assert alt["label"] == "Alternative — linked experiment failed"


def test_store_alternative_with_no_moment_is_dropped_not_invented(tmp_path, load_fixture, monkeypatch):
    from agr import read

    store, run_id, _ = _base_review(tmp_path, load_fixture)
    monkeypatch.setattr("agr.divergence.observed_alternative", lambda s, r: {
        "event_id": "evt_nonexistent", "proposal": "x", "source": "sibling:r2"})
    rev = read.get_review(store, run_id)
    assert all(not (m.get("alternatives")) for m in rev["moments"])
    assert rev["review_counts"]["alternatives"]["dropped"]["observed_no_matching_moment"] == 1


# --- PR #91 review: the cutoff gates checks/recoveries by their EVIDENCE ------


def test_cutoff_gates_a_check_by_the_event_that_revealed_its_result():
    """A synthesized in-session check's ``sequence`` is the TOOL CALL's index, but
    its status comes from the later tool RESULT. The cutoff must gate on the
    result, not the call — otherwise a suggestion replacing the call validates a
    fact using a result the agent had not seen."""
    events = [_ev("evt_call", 1), _ev("evt_result", 5)]
    check = VerifierCheck(
        check_id="C9", run_id="r", source_capture_id="c", name="C9", status="failed",
        source="output_interpretation", timing="during_run",
        source_pointers=["evt_call", "evt_result"], sequence=1)
    ctx = _ctx([_cand("cand_C3", affected_checks=["C3"])], events,
               checks=[check], slices=[_slice("C3", "dependency_linked")])
    # Decision at the call (seq 1): the result (seq 5) is not yet observed.
    assert reviewer._cutoff_context(ctx, 1).checks == []
    # Decision at or after the result: the check is available.
    assert len(reviewer._cutoff_context(ctx, 5).checks) == 1


def test_cutoff_excludes_a_check_with_unknown_observation_time():
    events = [_ev("evt_call", 1)]
    check = VerifierCheck(
        check_id="C9", run_id="r", source_capture_id="c", name="C9", status="failed",
        source="output_interpretation", timing="during_run",
        source_pointers=["evt_missing"], sequence=1)
    ctx = _ctx([_cand("cand_C3", affected_checks=["C3"])], events,
               checks=[check], slices=[_slice("C3", "dependency_linked")])
    assert reviewer._cutoff_context(ctx, 9).checks == []


def _recovery(failure, resolution, classification="good_recovery"):
    return RecoveryEpisode(
        episode_id="ep", run_id="r", source_capture_id="c", classification=classification,
        failure_event_id=failure, resolution_event_id=resolution,
        strategy_changed=True, changed_action=True)


def test_cutoff_gates_a_recovery_by_the_resolution_it_uses():
    events = [_ev("evt_fail", 1), _ev("evt_resolve", 6)]
    ctx = _ctx([_cand("cand_C3", affected_checks=["C3"])], events,
               checks=[_check("C3", "failed")], slices=[_slice("C3", "dependency_linked")],
               recoveries=[_recovery("evt_fail", "evt_resolve")])
    # Decision at the failure (seq 1): the resolution (seq 6) is not yet observed.
    assert reviewer._cutoff_context(ctx, 1).recoveries == []
    # Decision at/after the resolution: the episode is available.
    assert len(reviewer._cutoff_context(ctx, 6).recoveries) == 1


def test_cutoff_keeps_an_unresolved_failure_once_its_failure_is_known():
    events = [_ev("evt_fail", 1), _ev("evt_later", 4)]
    ctx = _ctx([_cand("cand_C3", affected_checks=["C3"])], events,
               checks=[_check("C3", "failed")], slices=[_slice("C3", "dependency_linked")],
               recoveries=[_recovery("evt_fail", None, "unrecovered_failure")])
    assert len(reviewer._cutoff_context(ctx, 1).recoveries) == 1


# --- deterministic baseline --------------------------------------------------


def test_deterministic_review_carries_no_alternatives():
    moments = run_reviewer(_ctx([_cand("cand_C3", affected_checks=["C3"])], _base_events(),
                                checks=[_check("C3", "failed")],
                                slices=[_slice("C3", "dependency_linked")]))
    assert all(m.alternatives == [] for m in moments)


# --- served review (integration through the store) ---------------------------


def test_alternatives_are_served_and_counted(tmp_path, load_fixture):
    """The grounded alternative survives to the read model, and its propose /
    attach / drop counts are served beside the review (WS1 acceptance)."""
    from agr import read
    from agr.pipeline import analyze
    from agr.store import Store

    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture("chess_best_move.atif.json"), store)
    run_id = a.run_source.run_id
    m0 = next(m for m in read.get_review(store, run_id)["review_moments"] if m.get("selected"))
    anchor = m0["anchor_event_ids"][0]
    facts = [{k: v for k, v in f.items() if k not in ("validation", "recomputed")}
             for f in m0["validated_facts"]]
    payload = {"moments": [{
        "candidate_id": m0["candidate_id"], "anchor_event_ids": m0["anchor_event_ids"],
        "kind": m0["kind"], "polarity": m0["polarity"], "affected_checks": m0["affected_checks"],
        "structured_facts": facts,
        "better_action": "Check the complete candidate set before submission",
        "alternatives": [
            {"kind": "suggested", "proposal": "Enumerate every winning move before submitting.",
             "replaces_decision": {"event_id": anchor, "description": "submitted without enumerating"},
             "information_available": [anchor]},
            {"kind": "suggested", "proposal": "a suggestion that needs later evidence",
             "replaces_decision": {"event_id": anchor},
             "information_available": ["evt_does_not_exist"]},
        ],
    }]}
    analyze(load_fixture("chess_best_move.atif.json"), store,
            reviewer=ScriptedReviewer(payload, source="model:test"))

    review = read.get_review(store, run_id)
    served = [a for m in review["moments"] for a in m.get("alternatives", [])]
    assert [a["label"] for a in served] == ["Suggested alternative"]
    counts = review["review_counts"]["alternatives"]
    assert counts["proposed"] == 2
    assert counts["attached"] == 1
    assert counts["dropped"]["information_cutoff"] == 1
