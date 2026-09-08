"""Review 2026-09-07 (of 049e168) — follow-up acceptance probes.

The prior revision closed the earlier review's findings but this review found
four new high-priority issues in it. These probes pin each fix's acceptance
criterion against the real adapter / detector / reviewer / packet boundary.

R1 — final-only JSON imports collapsed separate executions and ignored
      explicit options (hard-coded identity, dropped run_id/task_id/
      instruction/verifier/sweep_id/configuration_id, ignored source
      session_id).
R2 — preserved tool inputs were invisible to the reviewer and did not
      distinguish actions; a repeated mutation was flaggable on identical
      acknowledgement text alone.
R3 — repetition (detector and validator) paired parallel results by position
      instead of by tool-use id.
R4 — the observation gate set ``observation_basis = tool_evidence`` whenever
      facts validated and references resolved, without verifying the evidence
      was a tool event establishing the proposed negative behaviour.
"""

import json

from agr import read
from agr._util import action_signature
from agr.detectors import DetectorContext, RepeatedActionNoNewInfo
from agr.ingest_claude import convert
from agr.model_packet import build_packet, resolve_expansion
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.reviewer import ReviewerContext, run_reviewer
from agr.schema import Candidate, CapabilityProfile, DerivedEvent
from agr.store import Store


# --- helpers ----------------------------------------------------------------

def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _final_result(*, session_id=None, result="All checks passed.",
                  is_error=False, usage=None, cost=None):
    obj = {"type": "result", "subtype": "success", "is_error": is_error,
           "result": result}
    if session_id is not None:
        obj["session_id"] = session_id
    if usage is not None:
        obj["usage"] = usage
    if cost is not None:
        obj["total_cost_usd"] = cost
    return obj


def _event(eid, etype="tool_call", seq=None, payload=None):
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        payload=dict(payload or {}),
    )


def _det_ctx(evs):
    prof = CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete",
                      "tool_results": "complete", "filesystem": "checkpoint_only"})
    return DetectorContext(run_id="r", capture_id="c", events=evs, checks=[],
                           doc={}, profile=prof)


def _ctx(candidates, events, checks=(), profile=None):
    return ReviewerContext(
        run_id="r", source_capture_id="c", candidates=list(candidates),
        slices=[], checks=list(checks), events=list(events), profile=profile,
        declared_artifacts=[], task_instruction=None)


# === R1: final-only identity & option handling ==============================

def test_two_final_exports_with_different_sessions_yield_two_run_rows(tmp_path):
    """Two independent final-result exports (different source session ids) are
    TWO run rows, never two capture revisions of one collapsed execution."""
    store = Store(str(tmp_path / "store"))
    a = convert(_write(tmp_path, "a.json", json.dumps(_final_result(session_id="sess-A-1"))))
    b = convert(_write(tmp_path, "b.json", json.dumps(_final_result(session_id="sess-B-2"))))
    analyze(a.doc, store)
    analyze(b.doc, store)
    rows = read.list_runs(store)
    run_ids = {row["run_id"] for row in rows}
    assert len(rows) == 2
    assert "sess-A-1" in str(run_ids) and "sess-B-2" in str(run_ids)
    assert a.doc["run"]["logical_run_id"] != b.doc["run"]["logical_run_id"]
    assert a.doc["run"]["task_id"] != b.doc["run"]["task_id"]


def test_final_only_explicit_options_survive(tmp_path):
    """Explicit --run-id / --task-id / --instruction / --sweep-id /
    --configuration_id are honoured, and the source session id is preserved
    rather than dropped."""
    p = _write(tmp_path, "final.json",
               json.dumps(_final_result(session_id="sess-X-9")))
    res = convert(p, task_id="fix-tests", instruction="Fix the failing tests",
                  run_id="my-run-42", sweep_id="sweep-7",
                  configuration_id="cfg-3")
    run = res.doc["run"]
    assert run["logical_run_id"] == "my-run-42"          # explicit override honored
    assert run["task_id"] == "fix-tests"
    assert run["source_session_id"] == "sess-X-9"        # source identity preserved
    assert run["sweep_id"] == "sweep-7"
    assert run["configuration_id"] == "cfg-3"
    assert res.doc["task"]["instruction"] == "Fix the failing tests"


def test_final_only_verifier_sets_outcome_but_trajectory_stays_limited(tmp_path):
    """A supplied verifier affects verifier OUTCOME (the checks are ingested)
    while missing trajectory evidence remains missing: tool_calls stay
    unavailable and no behavioural review moment is produced."""
    p = _write(tmp_path, "final.json",
               json.dumps(_final_result(session_id="sess-V-1")))
    res = convert(p, verifier={
        "raw_output": "C1 PASSED",
        "checks": [{"check_id": "C1", "name": "tests pass", "status": "passed",
                    "source": "native_structured"}],
    })
    store = Store(str(tmp_path / "store"))
    a = analyze(res.doc, store)
    view = read.get_review(store, a.run_source.run_id)
    assert view["outcome"]["status"] == "PASSED"        # verifier outcome applied
    # Trajectory coverage stays limited even with a verifier sidecar.
    assert res.doc["capabilities"]["tool_calls"] == "unavailable"
    assert res.doc["capabilities"]["tool_results"] == "unavailable"
    assert res.doc["capture_completeness"] == "partial"
    # No tool evidence was captured, so no behavioural moment is selected.
    assert not [m for m in view["review_moments"] if m.get("selected")]


def test_final_only_without_session_falls_back_conservatively(tmp_path):
    """A final export with no source session id falls back to the file stem —
    documented and conservative, and two different files still differ."""
    a = convert(_write(tmp_path, "one.json", json.dumps(_final_result())))
    b = convert(_write(tmp_path, "two.json", json.dumps(_final_result())))
    assert a.doc["run"]["logical_run_id"] != b.doc["run"]["logical_run_id"]
    assert "source_session_id" not in a.doc["run"]


# === R2: structured tool inputs distinguish actions ========================

def test_different_edits_to_same_path_have_distinct_action_signatures():
    """Two successful Edit calls target app.py with different replacements.
    Both return the same generic success text, but their action identities
    now include the structured input — they are distinct actions."""
    e1 = _event("e1", payload={"tool": "Edit", "content": "app.py",
                               "tool_input": {"file_path": "app.py",
                                              "old_string": "return False",
                                              "new_string": "return True"}})
    e2 = _event("e2", payload={"tool": "Edit", "content": "app.py",
                               "tool_input": {"file_path": "app.py",
                                              "old_string": "print(1)",
                                              "new_string": "print(2)"}})
    assert action_signature(e1) != action_signature(e2)
    # The path alone is no longer the whole identity…
    same_path = _event("e3", payload={"tool": "Edit", "content": "app.py",
                                      "tool_input": {"file_path": "app.py",
                                                     "old_string": "return False",
                                                     "new_string": "return True"}})
    assert action_signature(e1) == action_signature(same_path)


def test_repeated_mutation_is_not_flagged_on_identical_ack_text():
    """A repeated Edit returning the same \"ok\" acknowledgement is NOT a
    no-new-information defect: identical ack text cannot establish that a
    mutation did no useful work (the file may have changed). The detector
    emits nothing for mutations without resulting-state evidence."""
    evs = [
        _event("c1", seq=1, payload={"tool": "Edit", "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "a", "new_string": "b"}}),
        _event("r1", etype="tool_result", seq=2, payload={"content": "ok", "exit_code": 0}),
        _event("c2", seq=3, payload={"tool": "Edit", "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "a", "new_string": "b"}}),
        _event("r2", etype="tool_result", seq=4, payload={"content": "ok", "exit_code": 0}),
    ]
    cands = RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates
    assert cands == []


def test_packet_evidence_carries_structured_tool_input_and_ids():
    """The reviewer packet's per-candidate evidence and the timeline digest
    carry the tool-use id, explicit status, and bounded structured tool_input
    — so the model can retrieve an Edit's old/new strings and distinguish
    parallel calls (R2)."""
    evs = [
        _event("c1", seq=1, payload={"tool": "Edit", "tool_use_id": "toolu_1",
                                     "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "return False",
                                                    "new_string": "return True"}}),
        _event("r1", etype="tool_result", seq=2,
               payload={"tool": "Edit", "tool_use_id": "toolu_1",
                        "content": "ok", "status": "ok"}),
    ]
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="det", kind="behaviour",
                     anchor_event_ids=["c1", "r1"], structured_facts=[])
    ctx = _ctx([cand], evs)
    packet, _ = build_packet(ctx)
    ev = packet["deterministic_candidates"][0]["evidence"][0]
    assert ev["tool_use_id"] == "toolu_1"
    assert "tool_input" in ev and "return False" in ev["tool_input"]
    digest = {d["event_id"]: d for d in packet["timeline_digest"]}
    assert digest["c1"]["tool_use_id"] == "toolu_1"
    assert "tool_input" in digest["c1"]
    # Tool-result status is surfaced too.
    res_ev = packet["deterministic_candidates"][0]["evidence"][-1]
    assert res_ev["event_id"] == "r1"
    assert res_ev.get("status") == "ok"


def test_expansion_returns_full_structured_tool_input():
    """The bounded expansion round returns the FULL structured tool input
    (redacted), so a finding grounded in an edit can retrieve exactly what
    changed."""
    big_new = "x" * 300
    evs = [
        _event("c1", seq=1, payload={"tool": "Edit", "tool_use_id": "toolu_1",
                                     "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "return False",
                                                    "new_string": big_new}}),
    ]
    ctx = _ctx([], evs)
    expansion = resolve_expansion(ctx, [{"event_ids": ["c1"]}])
    granted = expansion["evidence"][0]
    assert granted["tool_use_id"] == "toolu_1"
    assert "tool_input" in granted
    assert big_new in granted["tool_input"]   # full input, not a 200-char excerpt


def test_changing_tool_output_prevents_no_new_info_claim():
    """For non-mutation tools, changed output is observation with new
    information — no repetition defect (and the validator agrees)."""
    evs = [
        _event("c1", seq=1, payload={"tool": "poll", "content": "check-status"}),
        _event("r1", etype="tool_result", seq=2, payload={"content": "running 10%"}),
        _event("c2", seq=3, payload={"tool": "poll", "content": "check-status"}),
        _event("r2", etype="tool_result", seq=4, payload={"content": "running 50%"}),
    ]
    assert RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates == []
    cand = Candidate(candidate_id="sem_rep", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour",
                     anchor_event_ids=["c2"],
                     structured_facts=[{"type": "repetition", "events": ["c1", "c2"]}])
    validated = run_reviewer(_ctx([cand], evs))
    assert validated[0].gate_results["fact_validation"] == "failed"


# === R3: repetition pairs parallel results by id, not position =============

def _parallel_events():
    """Call A and poll B issued together (each with a tool-use id). A returns
    'running 10%', B returns 'running 50%'. A later B poll returns 'running 10%'.
    Every result carries its correct tool-use id."""
    return [
        _event("a", seq=1, payload={"tool": "poll", "content": "check",
                                    "tool_use_id": "a1"}),
        _event("b", seq=2, payload={"tool": "poll", "content": "check",
                                    "tool_use_id": "b1"}),
        _event("ra", etype="tool_result", seq=3,
               payload={"content": "running 10%", "tool_use_id": "a1"}),
        _event("rb1", etype="tool_result", seq=4,
               payload={"content": "running 50%", "tool_use_id": "b1"}),
        _event("b2", seq=5, payload={"tool": "poll", "content": "check",
                                     "tool_use_id": "b2"}),
        _event("rb2", etype="tool_result", seq=6,
               payload={"content": "running 10%", "tool_use_id": "b2"}),
    ]


def test_parallel_results_paired_by_id_not_position():
    """The detector must NOT attach A's output to the first B call. With
    id-based pairing, B's result is 'running 50%' and B2's is 'running 10%' —
    they differ, so no repetition concern is published. Position-based pairing
    would pair two 'running 10%' outputs and emit a false defect."""
    evs = _parallel_events()
    assert RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates == []


def test_validator_repetition_over_mispaired_parallel_calls_fails():
    """A scripted model claiming repetition over the B/B2 parallel pair fails
    fact validation: their recorded outputs differ once results are paired by
    id (the actual B observations changed)."""
    evs = _parallel_events()
    cand = Candidate(candidate_id="sem_rep", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour",
                     anchor_event_ids=["b2"],
                     structured_facts=[{"type": "repetition", "events": ["b", "b2"]}])
    moments = run_reviewer(_ctx([cand], evs))
    assert moments[0].gate_results["fact_validation"] == "failed"
    assert moments[0].selected is False


def test_reordering_parallel_results_pairs_correctly():
    """Reordered parallel results (B's result arriving before A's) still pair
    to the correct calls by id — the conclusions are identical to the ordered
    case."""
    evs = _parallel_events()
    # Swap the two result events' order.
    evs[2], evs[3] = evs[3], evs[2]
    assert RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates == []


def test_unmatched_id_result_stays_unpaired():
    """A result whose tool-use id matches no captured call stays unpaired
    rather than being silently re-attached to a neighbour — so it cannot
    support a repetition claim."""
    evs = [
        _event("c1", seq=1, payload={"tool": "poll", "content": "check",
                                     "tool_use_id": "a1"}),
        _event("c2", seq=2, payload={"tool": "poll", "content": "check",
                                     "tool_use_id": "b1"}),
        # result for b1 arrives; a1's result is missing (unmatched id 'x9')
        _event("rb1", etype="tool_result", seq=3,
               payload={"content": "running 50%", "tool_use_id": "b1"}),
        _event("rx", etype="tool_result", seq=4,
               payload={"content": "running 10%", "tool_use_id": "x9"}),
    ]
    cands = RepeatedActionNoNewInfo().run(_det_ctx(evs)).candidates
    assert cands == []


# === R4: observation gate must establish a behavioural concern =============

def test_message_quote_only_negative_finding_is_not_selected(tmp_path):
    """A trace with only a user message and an assistant response. A scripted
    model proposes a negative finding supported by an exact quote of the user
    message. The quote is authentic and references resolve, but no tool event
    establishes a negative behaviour — so observation_basis is NOT
    tool_evidence and the finding does not publish."""
    lines = [
        {"type": "user", "sessionId": "sess-q-1",
         "message": {"role": "user", "content": "Please improve performance"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "I'll look into it."}]}},
    ]
    p = _write(tmp_path, "session.jsonl",
               "\n".join(json.dumps(l) for l in lines))
    doc = convert(p).doc
    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
    run_id = a.run_source.run_id

    # Find the user-message event id to quote.
    events = store.read_derived(run_id, a.run_source.source_capture_id,
                                "events.json")
    user_ev = next(e for e in events if e["event_type"] == "task_received")
    quote = "Please improve performance"
    payload = {"moments": [{
        "candidate_id": "sem_1", "kind": "behaviour", "polarity": "negative",
        "anchor_event_ids": [user_ev["event_id"]],
        "structured_facts": [{"type": "event_support",
                              "quotes": [{"event_id": user_ev["event_id"],
                                          "quote": quote}]}],
        "consequence": "performance_degradation",
        "quotes": [{"event_id": user_ev["event_id"], "quote": quote}],
    }]}
    analyze(doc, store, reviewer=ScriptedReviewer(payload, source="model:test"))
    view = read.get_review(store, run_id, reviewer_key="model:test")
    neg = [m for m in view["review_moments"]
           if m["polarity"] == "negative" and m.get("selected")]
    assert not neg, "a message-quote-only negative finding must not be selected"
    # And the observation basis never reads as tool evidence.
    assert all(m["gate_results"].get("observation_basis") != "tool_evidence"
               for m in view["review_moments"])


def test_failed_tool_no_verifier_observation_still_selected(tmp_path):
    """R4 acceptance: the useful failed-tool/no-verifier card is RETAINED —
    a state_transition observation whose failure_event is an observed failed
    tool is backed by tool evidence and stays selectable without a verifier."""
    lines = [
        {"type": "user", "sessionId": "sess-ft-1",
         "message": {"role": "user", "content": "Fix the failing test"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "python -m pytest tests/"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "E   assert 404 == 200", "is_error": True}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "Done."}]}},
    ]
    p = _write(tmp_path, "session.jsonl",
               "\n".join(json.dumps(l) for l in lines))
    doc = convert(p).doc
    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
    view = read.get_review(store, a.run_source.run_id)
    assert view["outcome"]["status"] == "UNVERIFIED"
    obs = [m for m in view["review_moments"] if m.get("selected")
           and m["gate_results"].get("observation_basis") == "tool_evidence"]
    assert obs, "the failed-tool observation must stay selected without a verifier"
    assert obs[0]["polarity"] == "negative"
    assert "remains unknown" in obs[0]["rendered_statement"].lower()
