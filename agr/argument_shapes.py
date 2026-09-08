"""Argument-shape distribution per failing-call signature (item 31, 2026-09-08).

For a given (tool, error_signature) group, what does the model's structured
``tool_input`` typically look like when the call fails? A "shape" here means
the KEY SET and the value TYPE at each key — never the retained values
themselves, so this is a distribution over argument STRUCTURE, not a leak of
retained content. Feeds the fleet view (item 30): a repeated failure can be
inspected for "does it correlate with a particular argument shape" (e.g.
every failing Edit in this group is missing a ``file_path`` key, or always
carries a ``list``-typed value where a ``str`` is expected) without opening
every individual run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from . import read
from ._util import is_tool_failure, paired_call
from .error_signature import error_signature as _compute_error_signature
from .schema import DerivedEvent
from .store import Store


def _value_type(val: Any) -> str:
    """A coarse, stable type label — bool is checked before int (bool is a
    subclass of int in Python and is never what "int" means here)."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, list):
        return "list"
    if isinstance(val, dict):
        return "dict"
    return type(val).__name__


def _shape_key(tool_input: dict) -> tuple:
    """A hashable (key, type) signature for one call's structured input,
    order-independent so {"a":1,"b":"x"} and {"b":"x","a":1} are one shape."""
    return tuple(sorted((str(k), _value_type(v)) for k, v in tool_input.items()))


@dataclass
class ArgumentShapeGroup:
    """The distribution of ``tool_input`` shapes among a (tool,
    error_signature) group's FAILING calls, across every run in the store."""

    key: tuple
    total_failing_calls: int
    shapes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": list(self.key),
            "total_failing_calls": self.total_failing_calls,
            "shapes": self.shapes,
        }


def argument_shapes(store: Store, min_group_size: int = 1) -> list[ArgumentShapeGroup]:
    """For every (tool, error_signature) group with at least one failing
    ``tool_call`` whose ``tool_input`` was retained, the distribution of
    argument shapes across those failing calls, sorted by group size.

    Reuses the SAME failure/pairing rules as recovery classification
    (``is_tool_failure`` excludes permission denials; ``paired_call`` links a
    result to the call it answers by tool-use id, adjacency only for id-less
    captures) — this is not a second, competing notion of "failing call".
    """
    groups: dict[tuple, Counter] = {}
    for r in read.list_runs(store):
        run_id, capture_id = r["run_id"], r.get("capture_id")
        if not capture_id or not store.has_derived(run_id, capture_id, "events.json"):
            continue
        raw_events = store.read_derived(run_id, capture_id, "events.json") or []
        events = [DerivedEvent(**e) for e in raw_events]
        for idx, ev in enumerate(events):
            if ev.event_type != "tool_result" or not is_tool_failure(ev):
                continue
            call = paired_call(events, idx)
            if call is None:
                continue
            tool_input = call.payload.get("tool_input")
            if not isinstance(tool_input, dict) or not tool_input:
                continue
            key = (call.payload.get("tool"), _compute_error_signature(ev.payload.get("content") or ""))
            groups.setdefault(key, Counter())[_shape_key(tool_input)] += 1

    out: list[ArgumentShapeGroup] = []
    for key, counter in groups.items():
        total = sum(counter.values())
        if total < min_group_size:
            continue
        shapes = [
            {"keys": [list(pair) for pair in shape], "count": count, "share": round(count / total, 3)}
            for shape, count in counter.most_common()
        ]
        out.append(ArgumentShapeGroup(key=key, total_failing_calls=total, shapes=shapes))
    out.sort(key=lambda g: g.total_failing_calls, reverse=True)
    return out
