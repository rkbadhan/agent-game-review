"""Shared event helpers for the deterministic analysis stages."""

from __future__ import annotations

from typing import Optional

from .schema import DerivedEvent

# Tool-result status vocabulary, separate from an OS process exit code: a tool
# error flag (or a missing result) is tool status, not an exit code. Sources
# that record a real exit code keep it; sources that record only an error flag
# get ``status`` only.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"


def exit_code(event: DerivedEvent) -> Optional[int]:
    ec = event.payload.get("exit_code")
    return ec if isinstance(ec, int) else None


def tool_status(event: DerivedEvent) -> str:
    """The tool result's status: ``ok`` / ``error`` / ``unknown``.

    Exit code wins where the source recorded one; otherwise an explicit
    ``status`` field; otherwise unknown — which is deliberately neither a
    success nor a failure (an unobserved outcome is never classified).
    """
    ec = exit_code(event)
    if ec is not None:
        return STATUS_ERROR if ec != 0 else STATUS_OK
    status = event.payload.get("status")
    if status in (STATUS_OK, STATUS_ERROR):
        return status
    return STATUS_UNKNOWN


def is_tool_failure(event: DerivedEvent) -> bool:
    if event.event_type == "error_observed":
        return True
    return event.event_type == "tool_result" and tool_status(event) == STATUS_ERROR


def is_tool_success(event: DerivedEvent) -> bool:
    return event.event_type == "tool_result" and tool_status(event) == STATUS_OK


def action_signature(event: DerivedEvent) -> tuple:
    """Identity of a tool call for 'same vs changed action' comparison."""
    p = event.payload
    return (p.get("tool"), p.get("content") or p.get("data") or p.get("path") or "")


def _call_id(event: DerivedEvent) -> Optional[str]:
    cid = event.payload.get("tool_use_id")
    return str(cid) if cid else None


def paired_call(events: list[DerivedEvent], idx: int) -> Optional[DerivedEvent]:
    """The tool_call event that produced the result at ``events[idx]``.

    F1 follow-up (review 2026-09-07, R2): when the capture records tool-use
    ids, the result is matched to THE call it answers — correct for parallel
    calls, where the nearest preceding call is not necessarily the right one.
    Adjacency is the fallback, clearly used only when the source carries no
    ids (and a result with an id that matches no captured call stays
    unpaired rather than being silently re-attached to a neighbour).
    """
    ev = events[idx]
    if ev.event_type != "tool_result":
        return None
    cid = _call_id(ev)
    if cid:
        for j in range(idx - 1, -1, -1):
            if events[j].event_type == "tool_call" and _call_id(events[j]) == cid:
                return events[j]
        return None  # id present but no matching call: never guess
    for j in range(idx - 1, -1, -1):
        if events[j].event_type == "tool_call":
            return events[j]
    return None


def preceding_call_signature(events: list[DerivedEvent], idx: int) -> Optional[tuple]:
    """Signature of the call that produced the result at ``events[idx]``.

    ID-linked when the capture carries tool-use ids; nearest-preceding only
    as the adjacency fallback for id-less captures.
    """
    call = paired_call(events, idx)
    if call is not None:
        return action_signature(call)
    return None
