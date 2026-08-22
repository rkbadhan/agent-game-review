"""Safe absence/presence predicates (spec §8.4 omission branch, §16.1).

Predicates are plain data evaluated by a whitelisted matcher — never executed
as trace-supplied code. This satisfies the security requirement that detector
and validator code never runs trace-supplied predicates directly.

A predicate is a dict, e.g.::

    {"event_type": "artifact_observation", "artifact_path": "/solution.txt"}
    {"event_type": "tool_call", "contains": "stockfish"}
"""

from __future__ import annotations

import re

from .schema import DerivedEvent

_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")

_ALLOWED_KEYS = {"event_type", "actor", "artifact_path", "tool", "contains"}


def _matches(event: DerivedEvent, predicate: dict) -> bool:
    for key in predicate:
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"unsupported predicate key {key!r}")
    if "event_type" in predicate and event.event_type != predicate["event_type"]:
        return False
    if "actor" in predicate and event.actor != predicate["actor"]:
        return False
    if "artifact_path" in predicate and event.payload.get("artifact_path") != predicate["artifact_path"]:
        return False
    if "tool" in predicate and event.payload.get("tool") != predicate["tool"]:
        return False
    if "contains" in predicate:
        if predicate["contains"] not in set(_TOKEN.findall(event.text())):
            return False
    return True


def matching(events: list[DerivedEvent], predicate: dict) -> list[DerivedEvent]:
    return [e for e in events if _matches(e, predicate)]


def absent(events: list[DerivedEvent], predicate: dict) -> bool:
    """True when zero events match — the mechanical basis of an omission."""
    return not matching(events, predicate)
