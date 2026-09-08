"""Fleet read-model over recovery episodes across ALL runs (item 30, 2026-09-08).

A "fleet view" answers: across every run in the store, which failures repeat,
how often, and how much they cost — grouped by whatever dimensions the
caller asks for (the failed call's ``tool``, its ``error_signature``, or
both). This is a pure re-read of already-persisted derived records
(``recoveries.json`` per run, item 29's enrichment); nothing new is computed
or inferred about the episodes themselves — only aggregated and sorted.
"""

from __future__ import annotations

from collections import Counter
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
    # AGR-04: confirmed (good_recovery / retry_succeeded_without_strategy_
    # change) and plausible (plausibly_resolved) episodes are counted
    # separately from each other and from unrecovered_count — count ==
    # confirmed_count + plausible_count + unrecovered_count always. Nothing
    # here silently folds a plausible resolution into either "confirmed" or
    # "unresolved".
    confirmed_count: int = 0
    plausible_count: int = 0
    avg_turns_to_resolve: Optional[float] = None
    # AGR-06: the UNION of underlying usage records (event_id -> tokens)
    # across every episode in this group, keyed by (run_id, event_id) so an
    # event two episodes' windows both cover (e.g. an unresolved episode's
    # window running into the next failure's) is counted once, not once per
    # episode. total_tokens is this union's sum, never a plain sum of each
    # episode's episode_window_tokens — see overlapping_usage_events.
    total_tokens: int = 0
    # How many distinct (run, event) pairs were covered by MORE than one
    # episode in this group. >0 means total_tokens is NOT the same number a
    # naive sum of episode_window_tokens would give — the group's usage is
    # non-additive with respect to its own episode list, by construction
    # (shared events are real, not a bug to eliminate).
    overlapping_usage_events: int = 0
    total_wall_ms: int = 0
    example_anchors: list[dict] = field(default_factory=list)

    @property
    def distinct_runs(self) -> int:
        return len(set(self.run_ids))

    @property
    def repeat_rate(self) -> Optional[float]:
        """Share of this group's AFFECTED RUNS where the failure recurred
        MORE THAN ONCE within that same run (AGR-06) — bounded [0, 1] by
        construction (a count of runs divided by a count that includes
        them). ``None`` (render as "unavailable") when the group has no
        affected runs at all — an empty denominator, not a rate of zero.

        This is deliberately NOT ``count / distinct_runs`` (episodes per
        run): that is an occurrence count that can exceed 1 and reads as a
        percentage only by coincidence. Two episodes in one affected run
        gives 1/1 = 100%; that same run alongside three singleton-episode
        runs gives 1/4 = 25%, not the old formula's 4 episodes / 4 runs = 1.0
        misread as "100% repeat".
        """
        if not self.run_ids:
            return None
        per_run = Counter(self.run_ids)
        affected = len(per_run)
        if not affected:
            return None
        repeated = sum(1 for n in per_run.values() if n > 1)
        return repeated / affected

    @property
    def unrecovered_share(self) -> float:
        return (self.unrecovered_count / self.count) if self.count else 0.0

    @property
    def confirmed_share(self) -> float:
        return (self.confirmed_count / self.count) if self.count else 0.0

    @property
    def plausible_share(self) -> float:
        return (self.plausible_count / self.count) if self.count else 0.0

    def to_dict(self) -> dict:
        rate = self.repeat_rate
        return {
            "key": list(self.key),
            "group_by": self.group_by,
            "count": self.count,
            "runs": self.distinct_runs,
            # None (JSON null) when unavailable — the caller renders that as
            # "unavailable", never a misleading 0%.
            "repeat_rate": round(rate, 3) if rate is not None else None,
            "unrecovered_count": self.unrecovered_count,
            "unrecovered_share": round(self.unrecovered_share, 3),
            "confirmed_count": self.confirmed_count,
            "confirmed_share": round(self.confirmed_share, 3),
            "plausible_count": self.plausible_count,
            "plausible_share": round(self.plausible_share, 3),
            "avg_turns_to_resolve": (
                round(self.avg_turns_to_resolve, 2) if self.avg_turns_to_resolve is not None else None
            ),
            # AGR-06: usage is associated with these episodes' windows, not
            # avoidable waste or a projected saving — this is a measured
            # count of tokens the underlying events recorded, nothing more.
            "total_tokens": self.total_tokens,
            "overlapping_usage_events": self.overlapping_usage_events,
            "usage_note": (
                "total_tokens is the union of underlying usage records across this "
                "group's episodes; overlapping_usage_events > 0 means some events were "
                "covered by more than one episode and are counted once, not per episode."
            ),
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
            confirmed_count=sum(
                1 for e in eps
                if e.get("classification") in ("good_recovery", "retry_succeeded_without_strategy_change")),
            plausible_count=sum(1 for e in eps if e.get("classification") == "plausibly_resolved"),
            avg_turns_to_resolve=(sum(turns) / len(turns)) if turns else None,
            # AGR-06: the union of usage_records across this group's
            # episodes, not a plain sum of episode_window_tokens — two
            # episodes whose windows share events (e.g. an unresolved
            # episode's window running into the next failure's) would
            # otherwise double-count those events' tokens.
            **dict(zip(("total_tokens", "overlapping_usage_events"), _usage_union(eps))),
            total_wall_ms=sum(e.get("wall_ms") or 0 for e in eps),
            example_anchors=anchors,
        ))
    groups.sort(key=lambda g: g.count, reverse=True)
    return groups


def _usage_union(eps: list[dict]) -> tuple[int, int]:
    """``(total_tokens, overlapping_event_count)`` — the union of every
    episode's ``usage_records``, keyed by ``(run_id, event_id)`` since event
    ids repeat across different runs' captures. ``overlapping_event_count``
    is how many distinct events were covered by more than one episode —
    when it is nonzero, ``total_tokens`` is NOT the same as summing each
    episode's own ``episode_window_tokens`` (AGR-06: group usage is
    non-additive with respect to its episode list whenever windows share
    events, by construction — this is a real fact about overlapping
    windows, not something to hide by summing anyway).
    """
    tokens_by_key: dict[tuple, int] = {}
    occurrences: dict[tuple, int] = {}
    for e in eps:
        run_id = e.get("run_id")
        for event_id, tok in (e.get("usage_records") or {}).items():
            key = (run_id, event_id)
            tokens_by_key[key] = tok
            occurrences[key] = occurrences.get(key, 0) + 1
    overlapping = sum(1 for c in occurrences.values() if c > 1)
    return sum(tokens_by_key.values()), overlapping


@dataclass
class FleetUsageSummary:
    """The fleet-wide usage headline — the union across EVERY episode in the
    store, regardless of grouping (AGR-06: summing each group's own
    total_tokens would double-count any event two DIFFERENT groups' episodes
    both cover, e.g. two failures in the same run with different tools/
    signatures whose windows overlap).
    """

    total_tokens: int
    episode_count: int
    affected_runs: int
    overlapping_usage_events: int
    usage_note: str = (
        "total_tokens is the union of underlying usage records across every recovery "
        "episode in the store; it is associated with episode windows, not avoidable "
        "waste or a projected saving. overlapping_usage_events > 0 means some events "
        "were covered by more than one episode (in the same or different groups) and "
        "are counted once here, not once per episode."
    )

    def to_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "episode_count": self.episode_count,
            "affected_runs": self.affected_runs,
            "overlapping_usage_events": self.overlapping_usage_events,
            "usage_note": self.usage_note,
        }


def fleet_usage_summary(store: Store) -> FleetUsageSummary:
    """The whole-fleet usage headline, independent of any --group-by choice.

    Reads every run's persisted episodes directly (not via :func:`fleet_
    episodes`, which only ever sees one grouping's buckets) so the union is
    computed across the COMPLETE episode list once, not reassembled from
    per-group unions that could themselves double-count a shared event.
    """
    runs = read.list_runs(store)
    all_eps: list[dict] = []
    for r in runs:
        run_id = r["run_id"]
        capture_id = r.get("capture_id")
        if not capture_id or not store.has_derived(run_id, capture_id, "recoveries.json"):
            continue
        episodes = store.read_derived(run_id, capture_id, "recoveries.json") or []
        for ep in episodes:
            all_eps.append({**ep, "run_id": run_id})
    total_tokens, overlapping = _usage_union(all_eps)
    return FleetUsageSummary(
        total_tokens=total_tokens,
        episode_count=len(all_eps),
        affected_runs=len({e["run_id"] for e in all_eps}),
        overlapping_usage_events=overlapping,
    )
