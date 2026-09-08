"""Fleet read-model over recovery episodes across ALL runs (item 30, 2026-09-08).

A "fleet view" answers: across every run in the store, which failures repeat,
how often, and how much they cost — grouped by whatever dimensions the
caller asks for (the failed call's ``tool``, its ``error_signature``, or
both). This is a pure re-read of already-persisted derived records
(``recoveries.json`` per run, item 29's enrichment); nothing new is computed
or inferred about the episodes themselves — only aggregated and sorted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import read
from .store import Store

# Accepted --group-by tokens -> the RecoveryEpisode field each one reads.
# "error"/"error_signature" are the same dimension under two spellings.
GROUP_DIMENSIONS = {"tool": "tool", "error": "error_signature", "error_signature": "error_signature"}

DEFAULT_GROUP_BY = ("tool", "error_signature")

# How many sample episodes each group carries for drilling into a run.
_MAX_ANCHORS = 3


def _resolve_dims(group_by: Optional[list[str]]) -> list[str]:
    requested = group_by if group_by else list(DEFAULT_GROUP_BY)
    dims: list[str] = []
    for token in requested:
        dim = GROUP_DIMENSIONS.get(token)
        if dim is None:
            raise ValueError(
                f"unknown --group-by dimension {token!r}; choose from "
                f"{sorted(set(GROUP_DIMENSIONS))}"
            )
        if dim not in dims:
            dims.append(dim)
    return dims


@dataclass
class EpisodeGroup:
    """One (tool, error_signature, ...) bucket of recovery episodes across
    every run in the store. Every field is a plain aggregate — a sum, a
    count, an average — over the ``RecoveryEpisode`` rows in the group;
    nothing here reclassifies or reinterprets a single episode.
    """

    key: tuple
    group_by: list[str]
    count: int
    run_ids: list[str] = field(default_factory=list)
    unrecovered_count: int = 0
    avg_turns_to_resolve: Optional[float] = None
    total_tokens: int = 0
    total_wall_ms: int = 0
    example_anchors: list[dict] = field(default_factory=list)

    @property
    def distinct_runs(self) -> int:
        return len(set(self.run_ids))

    @property
    def repeat_rate(self) -> float:
        """Episodes per distinct run this group appears in. >1 means the
        SAME error/tool combination recurs more than once within a run, not
        merely across different runs."""
        runs = self.distinct_runs
        return (self.count / runs) if runs else 0.0

    @property
    def unrecovered_share(self) -> float:
        return (self.unrecovered_count / self.count) if self.count else 0.0

    def to_dict(self) -> dict:
        return {
            "key": list(self.key),
            "group_by": self.group_by,
            "count": self.count,
            "runs": self.distinct_runs,
            "repeat_rate": round(self.repeat_rate, 3),
            "unrecovered_count": self.unrecovered_count,
            "unrecovered_share": round(self.unrecovered_share, 3),
            "avg_turns_to_resolve": (
                round(self.avg_turns_to_resolve, 2) if self.avg_turns_to_resolve is not None else None
            ),
            "total_tokens": self.total_tokens,
            "total_wall_ms": self.total_wall_ms,
            "example_anchors": self.example_anchors,
        }


def fleet_episodes(store: Store, group_by: Optional[list[str]] = None) -> list[EpisodeGroup]:
    """Group every run's persisted recovery episodes by ``group_by``
    (default: tool + error_signature together), sorted by group size
    (largest — the most repeated failure — first).
    """
    dims = _resolve_dims(group_by)
    runs = read.list_runs(store)
    buckets: dict[tuple, list[dict]] = {}
    for r in runs:
        run_id = r["run_id"]
        capture_id = r.get("capture_id")
        if not capture_id or not store.has_derived(run_id, capture_id, "recoveries.json"):
            continue
        episodes = store.read_derived(run_id, capture_id, "recoveries.json") or []
        for ep in episodes:
            key = tuple(ep.get(d) for d in dims)
            buckets.setdefault(key, []).append({**ep, "run_id": run_id, "source_capture_id": capture_id})

    groups: list[EpisodeGroup] = []
    for key, eps in buckets.items():
        turns = [e["turns_to_resolve"] for e in eps if e.get("turns_to_resolve") is not None]
        anchors = [
            {
                "run_id": e["run_id"],
                "source_capture_id": e["source_capture_id"],
                "episode_id": e.get("episode_id"),
                "failure_event_id": e.get("failure_event_id"),
                "classification": e.get("classification"),
            }
            for e in eps[:_MAX_ANCHORS]
        ]
        groups.append(EpisodeGroup(
            key=key,
            group_by=dims,
            count=len(eps),
            run_ids=[e["run_id"] for e in eps],
            unrecovered_count=sum(1 for e in eps if e.get("classification") == "unrecovered_failure"),
            avg_turns_to_resolve=(sum(turns) / len(turns)) if turns else None,
            total_tokens=sum(e.get("tokens") or 0 for e in eps),
            total_wall_ms=sum(e.get("wall_ms") or 0 for e in eps),
            example_anchors=anchors,
        ))
    groups.sort(key=lambda g: g.count, reverse=True)
    return groups
