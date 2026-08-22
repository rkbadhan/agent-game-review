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


def test_unknown_filter_and_sort_raise(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        queue.queue_view(store, filters=["nope"])
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
