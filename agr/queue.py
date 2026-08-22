"""Sweep summary + triage queue projection (spec §4.3.1–§4.3.2, §4.3.4).

A read-model projection over :func:`agr.read.list_runs`. The MVP presents the
store as one queue, but the sweep identity is stated honestly from what the runs
carry (§7.1, §4.3.1): the real ``sweep_id`` from ingest when every run shares
one, ``"mixed"`` when the store spans several, and ``"implicit"`` only when the
source declared none. The queue machinery (grouping, the visible filter chips,
the documented sorts, and the default triage-priority order) is built on top so a
reviewer works an inbox, not a flat list.

Two properties the spec makes normative are honoured here:

- **Nothing is hidden.** The active grouping, filters, and sort are echoed back in
  every response; run order is never set by an invisible relevance heuristic
  (§4.3.2).
- **Stable queue view.** A queue definition hashes to a ``queue_view_id`` so
  ``Next unhandled run`` walks a frozen order rather than recomputing an invisible
  one after every disposition (§4.3.2, §4.17).

Pure stdlib — no third-party imports.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from . import read
from .store import Store

# §4.3.2 — the primary visible filter chips, each a predicate over a run card.
FILTER_CHIPS: dict[str, Callable[[dict], bool]] = {
    "failed": lambda r: (r["outcome"].get("status") or "").upper() in ("FAILED", "ERROR"),
    "needs_attention": lambda r: (r["outcome"].get("status") or "").upper() in ("FAILED", "WARNING", "ERROR"),
    "recovered": lambda r: bool(r.get("recovered")),
    "verifier_concern": lambda r: bool(r.get("verifier_concern")),
    "unreviewed": lambda r: r["workflow"].get("review_progress") == "unreviewed",
}

# §4.3.2 — supported sort controls. Every key maps to a sort function; the default
# is ``triage`` (the documented triage-priority order, not a severity score).
SORTS = ("triage", "outcome", "review_progress", "cost", "duration", "recently_updated")

# §4.3.2 — group-by dimensions (the MVP subset over available card fields).
GROUPINGS = ("none", "outcome", "task_family", "review_progress", "disposition", "review_mode")

# Deterministic outcome order for sorting / grouping.
_OUTCOME_RANK = {"ERROR": 0, "FAILED": 1, "WARNING": 2, "PASSED": 3}
_PROGRESS_RANK = {"unreviewed": 0, "in_progress": 1, "handled": 2}


def _status(r: dict) -> str:
    return (r["outcome"].get("status") or "").upper()


def _triage_rank(r: dict) -> int:
    """The §4.3.2 default triage-priority tier (0 = most urgent).

    A workflow convenience, not a severity or model-quality score: the tooltip in
    the UI says so. Six tiers, in the spec's order.
    """
    status = _status(r)
    counts = r.get("counts") or {}
    supported_moment = (counts.get("concern") or 0) > 0
    if status in ("FAILED", "ERROR") and supported_moment:
        return 0  # failed with a supported decisive moment
    if status == "WARNING" or r.get("recovered"):
        return 1  # WARNING outcome or supported recovery
    if r.get("verifier_concern"):
        return 2  # possible task/verifier concern
    if status in ("FAILED", "ERROR"):
        return 3  # other failed runs
    if status == "PASSED" and r.get("review_mode") == "model_enriched":
        return 4  # sampled clean passes
    return 5      # unsampled clean passes


def _sort_key(sort: str) -> Callable[[dict], Any]:
    if sort == "outcome":
        return lambda r: (_OUTCOME_RANK.get(_status(r), 9), r["run_id"])
    if sort == "review_progress":
        return lambda r: (_PROGRESS_RANK.get(r["workflow"].get("review_progress"), 9), r["run_id"])
    if sort == "cost":
        return lambda r: (-(_num(r.get("cost"))), r["run_id"])
    if sort == "duration":
        return lambda r: (-(_num(r.get("duration_s"))), r["run_id"])
    if sort == "recently_updated":
        return lambda r: (r["workflow"].get("updated_at") or r.get("ingested_at") or "", r["run_id"])
    # default: triage priority, then outcome severity, then id (stable, visible).
    return lambda r: (_triage_rank(r), _OUTCOME_RANK.get(_status(r), 9), r["run_id"])


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _group_of(r: dict, grouping: str) -> str:
    if grouping == "outcome":
        return _status(r) or "UNKNOWN"
    if grouping == "task_family":
        return (r.get("task_id") or "unclassified").split("__", 1)[0]
    if grouping == "review_progress":
        return r["workflow"].get("review_progress") or "unreviewed"
    if grouping == "disposition":
        return r["workflow"].get("disposition") or "none"
    if grouping == "review_mode":
        return r.get("review_mode") or "deterministic_only"
    return "all"


def _queue_view_id(definition: dict) -> str:
    """A stable id for a frozen queue definition (grouping + filters + sort)."""
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return "queue_view_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def sweep_summary(store: Store) -> dict:
    """Aggregate the implicit sweep: outcome counts, review modes, handling progress.

    Outcome counts are deterministic; the summary never implies that a passed run
    was inspected or that review progress is an agent-quality measure (§4.3.1).
    """
    runs = read.list_runs(store)
    by_outcome = {"PASSED": 0, "FAILED": 0, "WARNING": 0, "ERROR": 0}
    by_mode = {"deterministic_only": 0, "model_enriched": 0}
    handled = triage_eligible = 0
    harnesses: set[str] = set()
    sweeps: set[str] = set()
    finished: list[str] = []
    costs: list[float] = []
    for r in runs:
        if r.get("sweep_id"):
            sweeps.add(r["sweep_id"])
        by_outcome[_status(r)] = by_outcome.get(_status(r), 0) + 1
        by_mode[r.get("review_mode", "deterministic_only")] = (
            by_mode.get(r.get("review_mode", "deterministic_only"), 0) + 1)
        # Triage-eligible = anything not a clean unsampled pass.
        if not (_status(r) == "PASSED" and r.get("review_mode") != "model_enriched"):
            triage_eligible += 1
        if r["workflow"].get("review_progress") == "handled":
            handled += 1
        if r.get("harness_version"):
            harnesses.add(r["harness_version"])
        if r.get("finished_at"):
            finished.append(r["finished_at"])
        if r.get("cost") is not None:
            costs.append(_num(r["cost"]))
    return {
        # The sweep identity, stated honestly from what the runs actually carry
        # (§7.1, §4.3.1): the real id when every run shares one, ``"implicit"`` when
        # the source declared none, ``"mixed"`` when the store spans several sweeps.
        # The distinct real ids are always listed so the grouping is inspectable.
        "sweep_id": next(iter(sweeps)) if len(sweeps) == 1 else "mixed" if sweeps else "implicit",
        "sweep_ids": sorted(sweeps),
        "total_runs": len(runs),
        # Implicit sweep: the harness(es) present and the most recent completion,
        # stated honestly rather than a fabricated sweep id/timestamp.
        "harness_versions": sorted(harnesses),
        "latest_finished_at": max(finished) if finished else None,
        "by_outcome": by_outcome,
        "by_review_mode": by_mode,
        "handled": handled,
        "triage_eligible": triage_eligible,
        "verifier_concerns": sum(1 for r in runs if r.get("verifier_concern")),
        # Cost aggregates appear only when the capture carries cost (§4.3.1).
        "total_cost": round(sum(costs), 4) if costs else None,
        "median_cost": (sorted(costs)[len(costs) // 2] if costs else None),
    }


def queue_view(
    store: Store,
    *,
    filters: Optional[list[str]] = None,
    sort: str = "triage",
    grouping: str = "none",
) -> dict:
    """Build a grouped/filtered/sorted queue view over the implicit sweep.

    The active grouping, filters, and sort are echoed in the response and folded
    into a stable ``queue_view_id`` so a shared link and ``Next unhandled run``
    reproduce the exact order (§4.3.2, §4.17). Unknown filter or sort names raise
    ``ValueError`` rather than silently changing the order.
    """
    filters = list(filters or [])
    for f in filters:
        if f not in FILTER_CHIPS:
            raise ValueError(f"unknown filter chip {f!r}; known: {sorted(FILTER_CHIPS)}")
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}; known: {SORTS}")
    if grouping not in GROUPINGS:
        raise ValueError(f"unknown grouping {grouping!r}; known: {GROUPINGS}")

    rows = read.list_runs(store)
    for f in filters:
        rows = [r for r in rows if FILTER_CHIPS[f](r)]
    rows.sort(key=_sort_key(sort))

    definition = {"filters": sorted(filters), "sort": sort, "grouping": grouping}
    groups: list[dict] = []
    if grouping == "none":
        groups.append({"key": "all", "label": "All runs", "run_ids": [r["run_id"] for r in rows]})
    else:
        buckets: dict[str, list[str]] = {}
        for r in rows:
            buckets.setdefault(_group_of(r, grouping), []).append(r["run_id"])
        for key in sorted(buckets):
            groups.append({"key": key, "label": key, "run_ids": buckets[key]})
    for g in groups:
        ids = set(g["run_ids"])
        members = [r for r in rows if r["run_id"] in ids]
        g["count"] = len(members)
        g["handled"] = sum(1 for r in members if r["workflow"].get("review_progress") == "handled")

    return {
        "read_model_version": read.version.READ_MODEL_VERSION,
        "queue_view_id": _queue_view_id(definition),
        "filters": definition["filters"],
        "sort": sort,
        "grouping": grouping,
        "available_filters": sorted(FILTER_CHIPS),
        "available_sorts": list(SORTS),
        "available_groupings": list(GROUPINGS),
        "run_ids": [r["run_id"] for r in rows],
        "groups": groups,
        "runs": rows,
    }


def next_unhandled(store: Store, run_id: Optional[str], **view_kwargs) -> Optional[str]:
    """The next not-``handled`` run after ``run_id`` in the frozen queue order.

    Walks the same order the queue view encodes so ``Next unhandled run`` respects
    the queue definition rather than recomputing an invisible one (§4.2, §4.3.5).
    """
    view = queue_view(store, **view_kwargs)
    order = view["run_ids"]
    runs_by_id = {r["run_id"]: r for r in view["runs"]}
    start = order.index(run_id) + 1 if run_id in order else 0
    for rid in order[start:]:
        if runs_by_id[rid]["workflow"].get("review_progress") != "handled":
            return rid
    return None
