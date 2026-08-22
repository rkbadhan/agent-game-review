"""Shared event helpers for the deterministic analysis stages."""

from __future__ import annotations

from typing import Optional

from .schema import DerivedEvent


def exit_code(event: DerivedEvent) -> Optional[int]:
    ec = event.payload.get("exit_code")
    return ec if isinstance(ec, int) else None


def is_tool_failure(event: DerivedEvent) -> bool:
    if event.event_type == "error_observed":
        return True
    if event.event_type == "tool_result":
        ec = exit_code(event)
        return ec is not None and ec != 0
    return False


def is_tool_success(event: DerivedEvent) -> bool:
    return event.event_type == "tool_result" and exit_code(event) == 0


def action_signature(event: DerivedEvent) -> tuple:
    """Identity of a tool call for 'same vs changed action' comparison."""
    p = event.payload
    return (p.get("tool"), p.get("content") or p.get("data") or p.get("path") or "")


def preceding_call_signature(events: list[DerivedEvent], idx: int) -> Optional[tuple]:
    """Signature of the nearest tool_call before ``events[idx]``."""
    for j in range(idx - 1, -1, -1):
        if events[j].event_type == "tool_call":
            return action_signature(events[j])
    return None
