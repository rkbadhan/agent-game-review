"""Harness rules must constrain live proposals and old offline demo reviews."""

import copy
import pytest

from agr import demo, read, version
from agr.detectors import DetectorContext, RepeatedActionNoNewInfo
from agr.model_packet import build_packet
from agr.model_reviewer import ScriptedReviewer
from agr.review_protocol import review_protocol
from agr.reviewer import ReviewerContext, run_reviewer, validate_facts
from agr.schema import Candidate, CapabilityProfile, DerivedEvent, VerifierCheck
from agr.store import Store


DOC = {"source_type": "harbor", "run": {"agent": "tau3_llm_agent"},
       "capture_completeness": "complete",
       "capabilities": {"messages": "complete", "tool_calls": "complete"}}


def event(seq, actor, kind, content, **payload):
    return DerivedEvent(event_id=f"evt_{seq:03d}", run_id="r", source_capture_id="c",
                        sequence=seq, source_step_ids=[str(seq)], event_type=kind,
                        actor=actor, payload={"content": content, **payload})


def context(events, doc=DOC):
    return ReviewerContext(
        run_id="r", source_capture_id="c", events=events, candidates=[], slices=[],
        checks=[VerifierCheck(check_id="C1", run_id="r", source_capture_id="c",
                              name="required update", status="failed", source="native_structured")],
        profile=CapabilityProfile(run_id="r", source_capture_id="c",
                                  capabilities={"messages": "complete", "tool_calls": "complete",
                                                "tool_results": "complete"}),
        protocol=review_protocol(doc, events))


def omission(anchors, facts=()):
    return {"candidate_id": "sem_1", "kind": "omission", "polarity": "negative",
            "anchor_event_ids": anchors, "affected_checks": ["C1"],
            "structured_facts": [{"type": "requirement_status", "check_id": "C1",
                                  "status": "failed"}, *facts],
            "taxonomy_verdict": "incomplete_execution",
            "better_action": "Execute the update after confirmation."}


def test_confirmation_and_stop_does_not_create_an_agent_opportunity():
    events = [event(1, "main_agent", "model_output", "May I proceed?"),
              event(2, "user", "environment_observation", "Yes, proceed. ###STOP###"),
              event(3, "harness", "run_completed", "Trial closed")]
    ctx = context(events)
    payload = omission(["evt_001", "evt_002", "evt_003"])
    telemetry = {}
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}), telemetry)
    assert moment.gate_results["fact_validation"] == "passed"
    assert moment.gate_results["protocol"] == "no_agent_turn_after_user_termination"
    assert not moment.selected and moment.better_action is None
    assert moment.taxonomy_verdict is None
    assert telemetry["rejections"] == {"protocol": 1}
    packet, _ = build_packet(ctx)
    assert packet["harness_protocol"]["user_termination_event_ids"] == ["evt_002"]


@pytest.mark.parametrize("change", ["other_adapter", "other_agent", "partial", "tool_text", "agent_text", "later_turn"])
def test_stop_rule_does_not_apply_to_unrelated_or_incomplete_evidence(change):
    doc = copy.deepcopy(DOC)
    events = [event(1, "user", "environment_observation", "Yes. ###STOP###"),
              event(2, "harness", "run_completed", "Trial closed")]
    if change == "other_adapter":
        doc["source_type"] = "claude"
    elif change == "other_agent":
        doc["run"]["agent"] = "some_other_agent"
    elif change == "partial":
        doc["capabilities"]["messages"] = "partial"
    elif change == "tool_text":
        events[0].actor = "tool"
    elif change == "agent_text":
        events[0].actor = "main_agent"
    else:
        events.insert(1, event(2, "main_agent", "tool_call", "update", tool="update"))
        events[-1].sequence = 3
    assert review_protocol(doc, events)["user_termination_event_ids"] == []


def test_earlier_omission_with_a_real_turn_remains_reviewable():
    ctx = context([event(1, "user", "environment_observation", "Yes, update it."),
                   event(2, "main_agent", "model_output", "Anything else?"),
                   event(3, "user", "environment_observation", "No. ###STOP###"),
                   event(4, "harness", "run_completed", "Trial closed")])
    window = {"type": "action_opportunity", "after_event_id": "evt_001",
              "before_event_id": "evt_003"}
    payload = omission(["evt_002"], [window])
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.selected and moment.gate_results["protocol"] == "passed"
    assert moment.validated_facts[-1]["recomputed"] == ["evt_002"]
    # Removing the scoped window must not let a generic final failure imply
    # that a turn was available after the final user message.
    payload["structured_facts"].pop()
    (unscoped,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert not unscoped.selected


def test_an_opportunity_cannot_start_at_the_stop_or_borrow_an_unanchored_turn():
    ctx = context([event(1, "user", "environment_observation", "Yes."),
                   event(2, "main_agent", "model_output", "Anything else?"),
                   event(3, "user", "environment_observation", "No. ###STOP###"),
                   event(4, "harness", "run_completed", "Trial closed")])
    window = {"type": "action_opportunity", "after_event_id": "evt_003",
              "before_event_id": "evt_004"}
    payload = omission(["evt_003", "evt_004"], [window])
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert not moment.selected and moment.gate_results["fact_validation"] == "failed"
    window["after_event_id"] = "evt_001"
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["fact_validation"] == "passed"
    assert not moment.selected  # the earlier turn is not the cited decision


@pytest.mark.parametrize("confirmation", [True, False])
def test_required_completion_confirmation_is_distinct_from_a_genuine_repeat(confirmation):
    output = ("Are you sure you want to mark the task as complete? "
              'If so, include "task_complete": true in your JSON response again.'
              if confirmation else "Already complete")
    events = [event(1, "main_agent", "tool_call", "{}", tool="mark_task_complete"),
              event(2, "tool", "tool_result", output),
              event(3, "main_agent", "tool_call", "{}", tool="mark_task_complete"),
              event(4, "tool", "tool_result", output)]
    ctx = context(events, {})
    # Even identical result text cannot turn a required confirmation into a
    # no-new-information claim; both detector and model facts share this rule.
    dctx = DetectorContext(run_id="r", capture_id="c", events=events, checks=ctx.checks,
                           doc={}, profile=ctx.profile)
    result = RepeatedActionNoNewInfo().run(dctx)
    assert bool(result.candidates) is not confirmation
    candidate = Candidate(candidate_id="sem_repeat", run_id="r", source_capture_id="c",
                          detector="model", kind="behaviour", anchor_event_ids=["evt_001", "evt_003"],
                          structured_facts=[{"type": "repetition", "events": ["evt_001", "evt_003"]}])
    (fact,) = validate_facts(candidate, ctx)
    assert (fact["validation"] == "passed") is not confirmation


@pytest.fixture(scope="module")
def rebuilt_demo(tmp_path_factory):
    store = Store(str(tmp_path_factory.mktemp("protocol-demo")))
    definition = demo.build_real_demo_store(store, include_comparison=False)
    return store, definition


@pytest.mark.parametrize("task", [19, 29, 35, 58, 76])
def test_baked_tau_reviews_are_revalidated_and_explain_the_closed_turn(rebuilt_demo, task):
    store, _ = rebuilt_demo
    row = next(r for r in read.list_runs(store) if f"tau3-retail-{task}__" in r["run_id"]
               and r["outcome"]["status"] == "FAILED")
    review = read.get_review(store, row["run_id"])
    assert review["harness_protocol"]["user_termination_event_ids"]
    assert not any(m["kind"] == "omission" and m["polarity"] == "negative" for m in review["moments"])
    assert any(m["gate_results"].get("protocol") == "no_agent_turn_after_user_termination"
               for m in review["review_moments"])
    # Revalidation preserves the immutable source and its provenance.
    source = store.read_source(row["run_id"], row["capture_id"])
    assert "contract_confirmation" not in source["task"]


@pytest.mark.parametrize("suffix", ["crack-7z-hash__ec926", "polyglot-c-py__f03"])
def test_baked_completion_false_positives_do_not_survive_current_validation(rebuilt_demo, suffix):
    store, _ = rebuilt_demo
    row = next(r for r in read.list_runs(store) if suffix in r["run_id"])
    review = read.get_review(store, row["run_id"])
    assert review["moments"] == []
    assert review["harness_protocol"]["completion_confirmations"]
    assert review["review_status"] == "all_proposals_rejected"
    assert all(m["reviewer_version"] == version.REVIEWER_VERSION for m in review["review_moments"])
    assert all(m["better_action"] is None for m in review["review_moments"])


def test_revalidation_records_its_version_and_retains_original_model_provenance(rebuilt_demo):
    store, definition = rebuilt_demo
    assert definition["model_reviews"] == 23 and definition["stale_reviews"] == 0
    row = next(r for r in read.list_runs(store)
               if store.has_derived(r["run_id"], r["capture_id"], "review_meta.json"))
    meta = store.read_derived(row["run_id"], row["capture_id"], "review_meta.json")
    for record in meta.values():
        assert record["origin"] == "precomputed" and record["reviewed_at"]
        assert record["revalidated_by"] == version.REVIEWER_VERSION


def test_omission_cannot_hide_the_stop_by_citing_only_the_previous_agent_message():
    ctx = context([event(1, "main_agent", "model_output", "May I proceed?"),
                   event(2, "user", "environment_observation", "Yes. ###STOP###"),
                   event(3, "harness", "run_completed", "Trial closed")])
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [omission(["evt_001"])]}))
    assert not moment.selected and moment.better_action is None


@pytest.mark.parametrize("kind", ["omission", "behaviour", "recovery", "external", None])
@pytest.mark.parametrize("anchors", [["evt_002", "evt_003", "evt_004"], ["evt_003", "evt_004"]])
def test_an_earlier_window_cannot_launder_post_termination_blame(kind, anchors):
    ctx = context([event(1, "user", "environment_observation", "Here is my choice."),
                   event(2, "main_agent", "model_output", "May I proceed?"),
                   event(3, "user", "environment_observation", "Yes. ###STOP###"),
                   event(4, "harness", "run_completed", "Closed")])
    payload = omission(anchors, [{"type": "action_opportunity", "after_event_id": "evt_001",
                                  "before_event_id": "evt_003"}])
    payload["kind"] = kind
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["fact_validation"] == "passed"
    assert moment.gate_results["protocol"] == "no_agent_turn_after_user_termination"
    assert not moment.selected and moment.better_action is None


@pytest.mark.parametrize("name", ["tau3_llm_agent", "tau3-llm-agent"])
@pytest.mark.parametrize("ending", ["Yes. ###STOP###.", "###STOP### bye",
                                     "###TRANSFER### please", "###OUT-OF-SCOPE###."])
def test_tau_names_and_simulator_termination_tokens(name, ending):
    doc = copy.deepcopy(DOC)
    doc["run"]["agent"] = name
    ctx = context([event(1, "user", "environment_observation", ending),
                   event(2, "harness", "run_completed", "Closed")], doc)
    assert ctx.protocol["user_termination_event_ids"] == ["evt_001"]
    payload = omission(["evt_001", "evt_002"])
    payload.pop("kind")
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert not moment.selected


def test_action_window_is_supporting_metadata_not_a_headline_or_sole_fact():
    ctx = context([event(1, "user", "environment_observation", "Yes, update it."),
                   event(2, "main_agent", "model_output", "Anything else?"),
                   event(3, "user", "environment_observation", "No. ###STOP###")])
    window = {"type": "action_opportunity", "after_event_id": "evt_001", "before_event_id": "evt_003"}
    payload = omission(["evt_002"])
    payload["structured_facts"].insert(0, window)
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.selected and moment.rendered_statement.startswith("Requirement check C1")
    payload["structured_facts"] = [window]
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert not moment.selected and moment.gate_results["fact_validation"] == "failed"


def test_repetition_retains_a_genuine_third_call_and_uses_cached_protocol(monkeypatch):
    import agr.reviewer as reviewer
    output = ('Are you sure you want to mark the task as complete? '
              'If so, include "task_complete": true in your JSON response again.')
    # First two calls predate the confirmation protocol. Only the last two
    # are a captured request/confirmation edge. All results remain equivalent.
    events = [event(i, "main_agent" if i % 2 else "tool",
                    "tool_call" if i % 2 else "tool_result", "{}" if i % 2 else output,
                    **({"tool": "mark_task_complete"} if i % 2 else {})) for i in range(1, 7)]
    ctx = context(events, {})
    ctx.protocol["completion_confirmations"] = [{"request_event_id": "evt_003",
        "confirmation_event_id": "evt_005", "prompt_event_id": "evt_004"}]
    monkeypatch.setattr(reviewer, "completion_confirmation_pairs",
                        lambda _: pytest.fail("must use the context's cached protocol"))
    candidate = Candidate(candidate_id="sem_repeat", run_id="r", source_capture_id="c",
        detector="model", kind="behaviour", anchor_event_ids=["evt_001", "evt_003", "evt_005"],
        structured_facts=[{"type": "repetition", "events": ["evt_001", "evt_003", "evt_005"]}])
    (fact,) = validate_facts(candidate, ctx)
    assert fact["validation"] == "passed" and fact["events"] == ["evt_001", "evt_003"]
    assert len(fact["recomputed"]) == 2
    events[1].payload["content"] = "Different state"
    (fact,) = validate_facts(candidate, ctx)
    assert fact["validation"] == "failed"  # no equivalent unprotected pair


def test_raw_explanation_inputs_survive_replay_and_keep_authenticity_and_overclaim():
    ctx = context([event(1, "main_agent", "model_output", "Observed evidence")], {})
    payload = {"candidate_id": "raw", "polarity": "positive", "anchor_event_ids": ["evt_001"],
        "structured_facts": [{"type": "event_support", "quotes": [
            {"event_id": "evt_001", "quote": "Observed evidence"}]}],
        "quotes": [{"event_id": "evt_001", "quote": "Observed evidence"}],
        "consequence": "This caused the final outcome."}
    (original,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert original.consequence is None  # filtered vocabulary must not erase raw prose
    assert original.raw_enrichment["consequence"] == payload["consequence"]
    (replayed,) = demo._revalidate_baked_review(
        {"reviewer_key": "model:scripted", "moments": [original.to_dict()]}, ctx)
    for name in ("quote_authenticity", "explanation_attribution"):
        assert replayed["gate_results"][name] == original.gate_results[name]
    assert replayed["gate_results"]["quote_authenticity"] == "authentic"
    assert replayed["gate_results"]["explanation_attribution"] == "overclaim"


@pytest.mark.parametrize("task, candidate", [(35, "cand_ignored_tool_failure_evt_007"), (19, "sem_1")])
def test_legacy_demo_preserves_overclaim_with_explicit_revalidation_limits(rebuilt_demo, task, candidate):
    store, _ = rebuilt_demo
    row = next(r for r in read.list_runs(store) if f"tau3-retail-{task}__" in r["run_id"]
               and r["outcome"]["status"] == "FAILED")
    moment = next(m for m in read.get_review(store, row["run_id"])["review_moments"]
                  if m["candidate_id"] == candidate)
    gates = moment["gate_results"]
    assert gates["explanation_attribution"] == "overclaim"
    assert gates["quote_authenticity"] == "not_revalidated"
    assert gates["historical_explanation_diagnostics"]["quote_authenticity"] == "authentic"
    assert gates["explanation_revalidation"] == "unavailable_legacy_inputs"
    assert moment["raw_enrichment"] is None


@pytest.mark.parametrize("task,candidate,selected", [
    (104, "sem_1", True), (104, "sem_2", True),
    (110, "sem_1", True), (98, "sem_1", True),
    (98, "sem_2", False), (35, "sem_1", False),
])
def test_baked_earlier_actions_need_no_omission_window(rebuilt_demo, task, candidate, selected):
    store, _ = rebuilt_demo
    row = next(r for r in read.list_runs(store) if f"tau3-retail-{task}__" in r["run_id"]
               and r["outcome"]["status"] == "FAILED")
    review = read.get_review(store, row["run_id"])
    moment = next(m for m in review["review_moments"] if m["candidate_id"] == candidate)
    assert moment["kind"] == "behaviour"
    assert not any(f["type"] == "action_opportunity" for f in moment["validated_facts"])
    assert moment["gate_results"]["protocol"] == "passed"
    assert moment["selected"] is selected
    events = store.read_derived(row["run_id"], row["capture_id"], "events.json")
    by_id = {e["event_id"]: e for e in events}
    stop_seq = min(by_id[eid]["sequence"] for eid in review["harness_protocol"]["user_termination_event_ids"])
    assert all(by_id[eid]["sequence"] < stop_seq for eid in moment["anchor_event_ids"])


def test_demo_opens_on_the_polyglot_regression_story(rebuilt_demo):
    _, definition = rebuilt_demo
    assert definition["landing_run"] == "harbor__terminal-bench/polyglot-c-py__6c3b4b8b-991"


@pytest.mark.parametrize("kind", ["behaviour", "external", "recovery", None])
def test_earlier_recorded_action_is_reviewable_without_a_window(kind):
    ctx = context([event(1, "user", "environment_observation", "Only update order A."),
                   event(2, "main_agent", "tool_call", "Updated order B", tool="update"),
                   event(3, "user", "environment_observation", "###STOP###")])
    payload = omission(["evt_002"], [{"type": "event_support", "quotes": [
        {"event_id": "evt_002", "quote": "Updated order B"}]}])
    payload["kind"] = kind
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["protocol"] == "passed" and moment.selected


@pytest.mark.parametrize("actor,kind,synthetic", [
    ("user", "environment_observation", False), ("tool", "tool_result", False),
    ("harness", "model_output", False), ("main_agent", "model_output", True),
])
def test_negative_behavior_needs_an_actual_agent_action_anchor(actor, kind, synthetic):
    ctx = context([event(1, actor, kind, "Recorded text", **({"provenance": "synthetic"} if synthetic else {})),
                   event(2, "user", "environment_observation", "###STOP###")])
    payload = omission(["evt_001"])
    payload["kind"] = "behaviour"
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["protocol"] == "no_agent_turn_after_user_termination"
    assert not moment.selected


@pytest.mark.parametrize("kind", ["omission", "behaviour", "external", "recovery", None])
@pytest.mark.parametrize("window_end", [None, "evt_003", "evt_004"])
def test_final_confirmation_alone_cannot_support_execution_blame(kind, window_end):
    ctx = context([event(1, "user", "environment_observation", "I prefer option A."),
                   event(2, "main_agent", "model_output", "May I proceed?"),
                   event(3, "user", "environment_observation", "Yes, proceed. ###STOP###"),
                   event(4, "harness", "run_completed", "Closed")])
    facts = [{"type": "action_opportunity", "after_event_id": "evt_001",
              "before_event_id": window_end}] if window_end else []
    payload = omission(["evt_002"], facts)
    payload["kind"] = kind
    payload["alternatives"] = [{"kind": "suggested", "replacement_for_event_id": "evt_002",
                                "description": "Execute the update after confirmation."}]
    telemetry = {}
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}), telemetry)
    assert moment.gate_results["fact_validation"] == "passed"
    assert moment.gate_results["protocol"] == "terminal_confirmation_has_no_action_opportunity"
    assert not moment.selected and moment.better_action is None
    assert moment.taxonomy_verdict is None and moment.alternatives == []
    assert telemetry["rejections"] == {"protocol": 1}
    packet, _ = build_packet(ctx)
    assert packet["harness_protocol"]["terminal_confirmation_event_ids"] == ["evt_002"]


@pytest.mark.parametrize("confirmation_text", [
    "May I proceed?", "Shall I go ahead with both? (yes/no)",
    "Please confirm: would you like me to proceed with this exchange?",
    "Can you confirm you'd like me to proceed with both exchanges?",
    "Would you like to proceed with the return?",
    "Please **confirm** this is the only item you want changed.",
    "Could I please proceed?", "Do I have your approval?",
])
def test_final_confirmation_request_is_recorded(confirmation_text):
    ctx = context([event(1, "main_agent", "model_output", confirmation_text),
                   event(2, "user", "environment_observation", "Yes. ###STOP###")])
    assert ctx.protocol["terminal_confirmation_event_ids"] == ["evt_001"]


@pytest.mark.parametrize("ending", ["Is there anything else I can help with?", "Anything else?",
                                     "The update has been applied."])
def test_final_conversation_closing_is_not_a_confirmation_request(ending):
    ctx = context([event(1, "main_agent", "model_output", ending),
                   event(2, "user", "environment_observation", "Thanks. ###STOP###")])
    assert ctx.protocol["terminal_confirmation_event_ids"] == []


def test_earlier_action_remains_reviewable_when_final_turn_requests_confirmation():
    ctx = context([event(1, "main_agent", "tool_call", "Updated the wrong order", tool="update"),
                   event(2, "tool", "tool_result", "Updated"),
                   event(3, "main_agent", "model_output", "May I proceed?"),
                   event(4, "user", "environment_observation", "Yes. ###STOP###")])
    payload = omission(["evt_001", "evt_003"], [{"type": "event_support", "quotes": [
        {"event_id": "evt_001", "quote": "Updated the wrong order"}]}])
    payload["kind"] = "behaviour"
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["protocol"] == "passed" and moment.selected


def test_confirmation_followed_by_an_actual_action_does_not_trigger_terminal_guard():
    ctx = context([event(1, "main_agent", "model_output", "May I proceed?"),
                   event(2, "user", "environment_observation", "Yes."),
                   event(3, "main_agent", "tool_call", "Updated the wrong order", tool="update"),
                   event(4, "tool", "tool_result", "Updated"),
                   event(5, "user", "environment_observation", "###STOP###")])
    assert ctx.protocol["terminal_confirmation_event_ids"] == []
    payload = omission(["evt_003"])
    payload["kind"] = "behaviour"
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["protocol"] == "passed" and moment.selected


@pytest.mark.parametrize("task", [19, 29, 35, 58, 76])
@pytest.mark.parametrize("kind", ["omission", "behaviour"])
def test_real_final_confirmation_cannot_borrow_a_window(rebuilt_demo, tmp_path, task, kind):
    from agr.pipeline import analyze
    store, _ = rebuilt_demo
    row = next(r for r in read.list_runs(store) if f"tau3-retail-{task}__" in r["run_id"]
               and r["outcome"]["status"] == "FAILED")
    doc = store.read_source(row["run_id"], row["capture_id"])
    ctx = analyze(doc, Store(str(tmp_path / "probe"))).review_context
    (last_id,) = ctx.protocol["terminal_confirmation_event_ids"]
    (stop_id,) = ctx.protocol["user_termination_event_ids"]
    last_seq = next(e.sequence for e in ctx.events if e.event_id == last_id)
    before_request = max((e for e in ctx.events if e.sequence < last_seq), key=lambda e: e.sequence)
    payload = omission([last_id], [{"type": "action_opportunity",
        "after_event_id": before_request.event_id, "before_event_id": stop_id}])
    payload["kind"] = kind
    payload["affected_checks"] = [ctx.checks[0].check_id]
    payload["structured_facts"][0]["check_id"] = ctx.checks[0].check_id
    (moment,) = run_reviewer(ctx, ScriptedReviewer({"moments": [payload]}))
    assert moment.gate_results["fact_validation"] == "passed"
    assert moment.gate_results["protocol"] == "terminal_confirmation_has_no_action_opportunity"
    assert not moment.selected and moment.better_action is None


@pytest.mark.parametrize("raw_inputs", [False, True])
def test_baked_final_confirmation_blame_is_rejected_on_replay(raw_inputs):
    ctx = context([event(1, "user", "environment_observation", "Option A."),
                   event(2, "main_agent", "model_output", "May I proceed?"),
                   event(3, "user", "environment_observation", "Yes. ###STOP###")])
    payload = omission(["evt_002"], [{"type": "action_opportunity", "after_event_id": "evt_001",
                                     "before_event_id": "evt_003"}])
    payload["kind"] = "behaviour"
    payload["selected"] = True
    payload["validated_facts"] = payload.pop("structured_facts")
    payload["gate_results"] = {"protocol": "passed", "fact_validation": "passed"}
    if raw_inputs:
        payload["raw_enrichment"] = {"better_action": payload["better_action"],
                                     "taxonomy_verdict": payload["taxonomy_verdict"]}
    (moment,) = demo._revalidate_baked_review({"reviewer_key": "model:offline", "moments": [payload]}, ctx)
    assert not moment["selected"] and moment["better_action"] is None
    assert moment["gate_results"]["protocol"] == "terminal_confirmation_has_no_action_opportunity"
    assert any("final confirmation request" in limit for limit in moment["limits"])
