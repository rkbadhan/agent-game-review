"""Recorded harness rules used by both live and offline review validation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import version
from ._util import action_signature, paired_result
from .schema import Candidate, DerivedEvent

if TYPE_CHECKING:
    from .reviewer import ReviewerContext


AGENT_ACTORS = {"agent", "main_agent", "assistant"}
AGENT_ACTIONS = {"model_output", "tool_call", "final_submission"}
# tau2-bench v1.0.1, UserSimulator.is_stop: substring matching for all three.
# https://github.com/sierra-research/tau2-bench/blob/v1.0.1/src/tau2/user/user_simulator.py#L183-L196
USER_TERMINATION_TOKENS = ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")


# Conservative recognition of explicit permission/confirmation requests in the
# recorded final message. This does not classify arbitrary explanation prose.
_CONFIRMATION_REQUEST = re.compile(
    r"\b(?:may|can|could|shall|should)\s+i\s+(?:please\s+)?(?:proceed|go\s+ahead)\b"
    r"|\b(?:please|kindly)\s+confirm\b"
    r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?confirm\b"
    r"|\bwould\s+you\s+like(?:\s+me)?\s+to\s+(?:proceed|go\s+ahead|execute|process|submit|apply|cancel|update)\b"
    r"|\b(?:do|can)\s+i\s+have\s+your\s+(?:confirmation|approval|permission|consent)\b",
    re.IGNORECASE,
)


def terminal_confirmation_event_ids(events: list[DerivedEvent], stop_ids: list[str]) -> list[str]:
    """Identify explicit final confirmation requests with no subsequent action.

    A finding based only on this turn cannot establish an execution opportunity
    following the user's response. Independent earlier actions remain reviewable.
    """
    by_id = {e.event_id: e for e in events}
    requests = []
    for stop_id in stop_ids:
        stop = by_id.get(stop_id)
        if stop is None:
            continue
        last = max((e for e in events if e.sequence < stop.sequence
                    and e.actor in AGENT_ACTORS and e.event_type in AGENT_ACTIONS
                    and e.payload.get("provenance") != "synthetic"),
                   key=lambda e: e.sequence, default=None)
        if (last is not None and last.event_type == "model_output"
                and _CONFIRMATION_REQUEST.search(re.sub(r"[*_`]+", "", last.text()))):
            requests.append(last.event_id)
    return requests


def completion_confirmation_pairs(events: list[DerivedEvent]) -> list[dict]:
    """Recognize Terminus's captured confirmation gate, not arbitrary repeats."""
    pairs = []
    calls = [(i, e) for i, e in enumerate(events) if e.event_type == "tool_call"
             and action_signature(e)[0] == "mark_task_complete"]
    for (index, first), (_, second) in zip(calls, calls[1:]):
        result = paired_result(events, index)
        if result is None or result.sequence >= second.sequence:
            continue
        text = result.text().lower()
        if ("are you sure you want to mark the task as complete?" in text
                and "task_complete" in text and "again" in text):
            pairs.append({"request_event_id": first.event_id,
                          "prompt_event_id": result.event_id,
                          "confirmation_event_id": second.event_id})
    return pairs


def review_protocol(doc: dict[str, Any], events: list[DerivedEvent]) -> dict:
    """Scope rules to source metadata and support them with recorded events.

    A STOP token in unrelated text or another adapter never establishes tau3
    termination. A captured later agent action contradicts that interpretation.
    """
    run = doc.get("run") or {}
    tau3 = (doc.get("source_type") == "harbor"
            and str(run.get("agent", "")).replace("-", "_") == "tau3_llm_agent"
            and doc.get("capture_completeness") == "complete"
            and all((doc.get("capabilities") or {}).get(k) == "complete"
                    for k in ("messages", "tool_calls")))
    stops = []
    if tau3:
        for event in events:
            if (event.actor == "user" and event.event_type == "environment_observation"
                    and any(token in event.text() for token in USER_TERMINATION_TOKENS)
                    and not any(e.sequence > event.sequence and e.actor in AGENT_ACTORS
                                and e.event_type in AGENT_ACTIONS for e in events)):
                stops.append(event.event_id)
    return {"version": version.REVIEW_PROTOCOL_VERSION, "user_termination_event_ids": stops,
            "terminal_confirmation_event_ids": terminal_confirmation_event_ids(events, stops),
            "completion_confirmations": completion_confirmation_pairs(events),
            "limits": ["Termination evidence does not establish that earlier agent actions were correct."]}


def action_opportunity(fact: dict, ctx: ReviewerContext) -> list[str]:
    """An omission needs an actual agent turn after its enabling observation."""
    by_id = {e.event_id: e for e in ctx.events}
    start = by_id.get(fact.get("after_event_id"))
    end = by_id.get(fact.get("before_event_id"))
    if start is None or end is None or start.sequence >= end.sequence:
        return []
    stops = set(ctx.protocol.get("user_termination_event_ids", []))
    if start.event_id in stops:
        return []
    stop_sequences = [by_id[eid].sequence for eid in stops if eid in by_id]
    cutoff = min([end.sequence, *[s for s in stop_sequences if s > start.sequence]])
    return [e.event_id for e in ctx.events if start.sequence < e.sequence < cutoff
            and e.actor in AGENT_ACTORS and e.event_type in AGENT_ACTIONS
            and e.payload.get("provenance") != "synthetic"]


def negative_finding_protocol_gate(candidate: Candidate, ctx: ReviewerContext) -> str:
    """Gate every negative finding, regardless of the model's chosen kind.

    A pre-termination window cannot justify decisions anchored at/after the
    ending message. Earlier actions are reviewable directly; an omission needs
    its own observed opportunity. All decision anchors precede termination.
    """
    if candidate.polarity != "negative":
        return "passed"
    by_id = {e.event_id: e for e in ctx.events}
    stop_sequences = [by_id[eid].sequence
                      for eid in ctx.protocol.get("user_termination_event_ids", [])
                      if eid in by_id]
    if not stop_sequences:
        return "passed"
    stop_seq = min(stop_sequences)
    refs = set(candidate.anchor_event_ids)
    for fact in candidate.structured_facts:
        refs.update(q.get("event_id") for q in fact.get("quotes", []))
        refs.update(fact.get("events", []))
        refs.update(fact.get(key) for key in
                    ("event_id", "failure_event", "resolution_event") if fact.get(key))
    if not refs or any(eid in by_id and by_id[eid].sequence >= stop_seq for eid in refs):
        return "no_agent_turn_after_user_termination"
    if candidate.detector == "model":
        cited_actions = {eid for eid in refs if eid in by_id
                         and by_id[eid].actor in AGENT_ACTORS
                         and by_id[eid].event_type in AGENT_ACTIONS
                         and by_id[eid].payload.get("provenance") != "synthetic"}
        final_requests = set(ctx.protocol.get("terminal_confirmation_event_ids", []))
        if cited_actions and cited_actions <= final_requests:
            return "terminal_confirmation_has_no_action_opportunity"
    if candidate.kind != "omission":
        if candidate.detector != "model":
            return "passed"
        # A recorded action is its own opportunity. Do not require a missing-
        # action window for criticism of something the agent actually did.
        if any(eid in by_id and by_id[eid].actor in AGENT_ACTORS
               and by_id[eid].event_type in AGENT_ACTIONS
               and by_id[eid].payload.get("provenance") != "synthetic"
               for eid in candidate.anchor_event_ids):
            return "passed"
        return "no_agent_turn_after_user_termination"
    for fact in candidate.structured_facts:
        if fact.get("type") == "action_opportunity":
            turns = action_opportunity(fact, ctx)
            if set(turns) & set(candidate.anchor_event_ids):
                return "passed"
    return "no_agent_turn_after_user_termination"
