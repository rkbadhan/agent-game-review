"""Sweep summary + triage queue projection (spec §4.3.1–§4.3.2, §4.2).

Pure stdlib. Asserts the implicit-sweep summary counts, the filter chips, the
triage-priority ordering, the stable queue-view id, and that ``Next unhandled``
walks the frozen queue order.
"""

import json
import os

import pytest

from agr import queue, workflow
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")

# Every fixture in the repo; a mix of PASSED / FAILED and deterministic reviews.
ALL_FIXTURES = (
    "chess_best_move.atif.json", "clean_pass.atif.json", "contract_mismatch.atif.json",
    "ignored_failure.atif.json", "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
)


def _store(tmp_path, *names):
    store = Store(str(tmp_path / "store"))
    for name in (names or ALL_FIXTURES):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            analyze(json.load(fh), store)
    return store


def test_sweep_summary_counts(tmp_path):
    store = _store(tmp_path)
    s = queue.sweep_summary(store)
    assert s["total_runs"] == len(ALL_FIXTURES)
    assert sum(s["by_outcome"].values()) == len(ALL_FIXTURES)
    assert s["handled"] == 0
    # Every run is deterministic-only in the fixtures, so no clean pass is sampled;
    # triage-eligible excludes unsampled clean passes.
    assert s["triage_eligible"] == s["total_runs"] - s["by_outcome"]["PASSED"]
    # Implicit-sweep header meta is honest: harness(es) present + latest completion.
    assert s["harness_versions"] == ["harbor-0.9"]
    assert s["latest_finished_at"] is not None
    # The fixtures declare no sweep_id, so the identity is "implicit" (not faked).
    assert s["sweep_id"] == "implicit" and s["sweep_ids"] == []
    # No cost in the ATIF fixtures, so cost aggregates stay None (never fabricated).
    assert s["total_cost"] is None and s["median_cost"] is None


def _store_with_sweeps(tmp_path, assignments):
    """Analyze fixtures after stamping each with a sweep_id (name -> sweep_id)."""
    store = Store(str(tmp_path / "store"))
    for name, sweep_id in assignments.items():
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            doc = json.load(fh)
        if sweep_id is not None:
            doc["run"]["sweep_id"] = sweep_id
        analyze(doc, store)
    return store


def test_sweep_identity_reports_a_shared_real_id(tmp_path):
    store = _store_with_sweeps(tmp_path, {
        "chess_best_move.atif.json": "sweep_141",
        "clean_pass.atif.json": "sweep_141"})
    s = queue.sweep_summary(store)
    assert s["sweep_id"] == "sweep_141"
    assert s["sweep_ids"] == ["sweep_141"]


def test_sweep_identity_names_a_store_spanning_several_sweeps(tmp_path):
    store = _store_with_sweeps(tmp_path, {
        "chess_best_move.atif.json": "sweep_141",
        "clean_pass.atif.json": "sweep_142",
        "contract_mismatch.atif.json": None})  # a run with no sweep_id
    s = queue.sweep_summary(store)
    assert s["sweep_id"] == "mixed"
    assert s["sweep_ids"] == ["sweep_141", "sweep_142"]


def test_failed_filter_chip(tmp_path):
    store = _store(tmp_path)
    view = queue.queue_view(store, filters=["failed"], sort="triage")
    runs_by_id = {r["run_id"]: r for r in view["runs"]}
    assert view["run_ids"]  # non-empty
    assert all(runs_by_id[r]["outcome"]["status"] in ("FAILED", "ERROR") for r in view["run_ids"])


def test_unreviewed_filter_reflects_workflow(tmp_path):
    store = _store(tmp_path)
    rid = queue.queue_view(store, filters=["failed"])["run_ids"][0]
    workflow.set_workflow(store, rid, actor="a", base_version=0,
                          disposition="no_action", progress="handled")
    remaining = queue.queue_view(store, filters=["unreviewed"])["run_ids"]
    assert rid not in remaining


def test_triage_priority_orders_failed_with_moment_first(tmp_path):
    store = _store(tmp_path)
    view = queue.queue_view(store, sort="triage")
    ranks = [queue._triage_rank(r) for r in view["runs"]]
    assert ranks == sorted(ranks)  # non-decreasing tiers, i.e. urgent first


def test_queue_view_id_is_stable_and_definition_sensitive(tmp_path):
    store = _store(tmp_path)
    a = queue.queue_view(store, filters=["failed"], sort="triage")["queue_view_id"]
    b = queue.queue_view(store, filters=["failed"], sort="triage")["queue_view_id"]
    c = queue.queue_view(store, filters=["failed"], sort="outcome")["queue_view_id"]
    assert a == b and a != c


def test_grouping_partitions_all_runs(tmp_path):
    store = _store(tmp_path)
    view = queue.queue_view(store, grouping="outcome")
    grouped = sum(g["count"] for g in view["groups"])
    assert grouped == len(view["run_ids"]) == len(ALL_FIXTURES)


def test_cost_sort_never_ranks_uncaptured_cost_as_cheapest(tmp_path):
    """F5: a run whose cost was never captured must not tie with (or rank
    ahead of, under descending-cost order) a run that genuinely cost $0 —
    that would present 'unknown' as 'free'. Known costs sort first, highest
    to lowest; every uncaptured-cost run trails behind all of them.

    The $0 and uncaptured roles are assigned so the tie-break on run_id
    alone (both would carry the same numeric key of 0.0 without the fix)
    puts them in the WRONG order — "greeting_file__..." sorts before
    "greeting_report__..." alphabetically, so if the fix regressed to a bare
    numeric key, list_runs' own alphabetical order would silently produce
    the very order this test expects to fail on, and the test would pass
    for the wrong reason. Swapping which fixture plays which role is what
    makes this test actually exercise the guard."""
    store = Store(str(tmp_path / "store"))
    names_and_costs = [
        ("chess_best_move.atif.json", 5.0),
        ("clean_pass.atif.json", None),          # never captured
        ("contract_mismatch.atif.json", 0.0),    # genuinely free
    ]
    for name, cost in names_and_costs:
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            doc = json.load(fh)
        if cost is not None:
            doc["run"]["total_cost_usd"] = cost
        analyze(doc, store)

    order = queue.queue_view(store, sort="cost")["run_ids"]
    assert order == [
        "chess_best_move__seed42",    # $5.00, known
        "greeting_report__seed7",     # $0.00, known
        "greeting_file__clean_pass",  # uncaptured — last, not tied with $0.00
    ]


def test_duration_sort_never_ranks_uncaptured_duration_as_shortest(tmp_path):
    """Review of PR #64: the same 'absent is not zero' guard the cost sort
    got must also apply to duration — an uncaptured duration_s (missing
    started_at/finished_at) must not tie with a genuine 0-second run. Role
    assignment follows the same reasoning as the cost test above: the
    alphabetically-earlier run_id gets the uncaptured duration, so a
    regression to a bare numeric tie-break (which falls back to list_runs'
    alphabetical order) would put it first — the wrong order — rather than
    coincidentally matching what the fix produces."""
    store = Store(str(tmp_path / "store"))

    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        long_run = json.load(fh)  # started_at/finished_at ~4m12s apart
    analyze(long_run, store)

    with open(os.path.join(FIXTURES, "clean_pass.atif.json"), encoding="utf-8") as fh:
        unknown_run = json.load(fh)
    del unknown_run["run"]["finished_at"]  # never captured
    analyze(unknown_run, store)

    with open(os.path.join(FIXTURES, "contract_mismatch.atif.json"), encoding="utf-8") as fh:
        zero_run = json.load(fh)
    zero_run["run"]["finished_at"] = zero_run["run"]["started_at"]  # genuinely 0s
    analyze(zero_run, store)

    order = queue.queue_view(store, sort="duration")["run_ids"]
    assert order == [
        "chess_best_move__seed42",    # ~252s, known
        "greeting_report__seed7",     # 0s, known
        "greeting_file__clean_pass",  # uncaptured — last, not tied with 0s
    ]


def test_unknown_filter_and_sort_raise(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        queue.queue_view(store, filters=["nope"])


def test_cost_and_duration_sort_put_missing_values_last_not_tied_with_zero():
    """U2: a run with no cost/duration captured is unavailable, not free or
    instant — it must not sort as if it tied with (and, once negated for
    descending order, sorted ahead of) a run that genuinely measured zero.
    A pure unit test of the sort key against synthetic run cards — run-level
    cost/duration are not wired through any adapter yet (§4.3.3), so this
    cannot be exercised end to end via ``analyze()``."""
    def card(run_id, cost=None, duration_s=None):
        return {"run_id": run_id, "cost": cost, "duration_s": duration_s,
                "outcome": {"status": "PASSED"}, "workflow": {}}

    cards = [card("unpriced"), card("priced", cost=5.0), card("free", cost=0.0)]
    order = [r["run_id"] for r in sorted(cards, key=queue._sort_key("cost"))]
    assert order == ["priced", "free", "unpriced"]

    cards = [card("no_duration"), card("slow", duration_s=90), card("instant", duration_s=0)]
    order = [r["run_id"] for r in sorted(cards, key=queue._sort_key("duration"))]
    assert order == ["slow", "instant", "no_duration"]


def test_outcome_and_review_status_filters_are_explicit(tmp_path):
    """U2: passed/undetermined and in_progress/handled are their own filter
    chips, not only reachable through a sort order."""
    store = _store(tmp_path)
    passed = queue.queue_view(store, filters=["passed"])["runs"]
    assert passed and all((r["outcome"].get("status") or "").upper() == "PASSED" for r in passed)

    rid = queue.queue_view(store)["run_ids"][0]
    workflow.set_workflow(store, rid, actor="tester", base_version=0, progress="in_progress")
    in_progress = queue.queue_view(store, filters=["in_progress"])["run_ids"]
    assert rid in in_progress
    assert rid not in queue.queue_view(store, filters=["unreviewed"])["run_ids"]
    assert rid not in queue.queue_view(store, filters=["handled"])["run_ids"]
    with pytest.raises(ValueError):
        queue.queue_view(store, sort="nope")


def test_next_unhandled_walks_frozen_order(tmp_path):
    store = _store(tmp_path)
    order = queue.queue_view(store, sort="triage")["run_ids"]
    first, second = order[0], order[1]
    assert queue.next_unhandled(store, first, sort="triage") == second
    # Handling the second skips it.
    workflow.set_workflow(store, second, actor="a", base_version=0,
                          disposition="no_action", progress="handled")
    assert queue.next_unhandled(store, first, sort="triage") == order[2]
