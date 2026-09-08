"""Fleet read-model over recovery episodes across ALL runs (item 30, 2026-09-08).

Builds a store from three synthetic fixtures, each producing exactly one
recovery episode with a distinct (tool, error_signature), and checks the
grouping/aggregation is a faithful re-read of persisted episodes.
"""

from __future__ import annotations

import json
import os

import pytest

from agr import fleet
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _store(tmp_path, *names):
    store = Store(str(tmp_path / "store"))
    for name in names:
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            analyze(json.load(fh), store)
    return store


def test_groups_by_default_tool_and_error_signature(tmp_path):
    store = _store(tmp_path, "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
                   "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store)
    # Three distinct failures (TimeoutError, ModuleNotFoundError, compilation
    # failed) never collapse into one group just because they share a tool.
    assert len(groups) == 3
    assert all(g.group_by == ["tool", "error_signature"] for g in groups)
    assert all(g.key[0] == "shell" for g in groups)
    assert all(g.count == 1 for g in groups)
    assert all(g.distinct_runs == 1 for g in groups)


def test_group_by_tool_only_merges_distinct_errors_of_the_same_tool(tmp_path):
    store = _store(tmp_path, "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
                   "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store, group_by=["tool"])
    assert len(groups) == 1
    assert groups[0].key == ("shell",)
    assert groups[0].count == 3
    assert groups[0].distinct_runs == 3


def test_error_alias_resolves_to_error_signature(tmp_path):
    store = _store(tmp_path, "stuck_retry.atif.json")
    groups = fleet.fleet_episodes(store, group_by=["error"])
    assert groups[0].group_by == ["error_signature"]


def test_unrecovered_share_and_repeat_rate(tmp_path):
    """ignored_failure.atif.json's one episode is unrecovered_failure — its
    group's unrecovered_share must be 1.0, and repeat_rate 1.0 for a group
    appearing once in one run."""
    store = _store(tmp_path, "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store)
    assert len(groups) == 1
    g = groups[0]
    assert g.unrecovered_share == 1.0
    assert g.unrecovered_count == 1
    assert g.repeat_rate == 1.0


def test_example_anchors_point_back_to_the_run(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json")
    groups = fleet.fleet_episodes(store)
    anchor = groups[0].example_anchors[0]
    assert anchor["run_id"]
    assert anchor["source_capture_id"]
    assert anchor["failure_event_id"]
    assert anchor["classification"] == "good_recovery"


def test_groups_sorted_largest_first(tmp_path):
    store = _store(tmp_path, "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
                   "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store, group_by=["tool"])
    counts = [g.count for g in groups]
    assert counts == sorted(counts, reverse=True)


def test_empty_store_produces_no_groups(tmp_path):
    store = Store(str(tmp_path / "store"))
    assert fleet.fleet_episodes(store) == []


def test_unknown_group_by_dimension_raises():
    with pytest.raises(ValueError):
        fleet._resolve_dims(["not_a_real_dimension"])


def test_cli_episodes_subcommand(tmp_path, capsys):
    _store(tmp_path, "tool_failure_recovery.atif.json")
    from agr.cli import main
    rc = main(["--store", str(tmp_path / "store"), "episodes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shell" in out
    assert "count=1" in out


def test_cli_episodes_unknown_group_by_errors_cleanly(tmp_path, capsys):
    _store(tmp_path, "tool_failure_recovery.atif.json")
    from agr.cli import main
    rc = main(["--store", str(tmp_path / "store"), "episodes", "--group-by", "bogus"])
    assert rc == 1


def test_to_dict_shape(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json")
    g = fleet.fleet_episodes(store)[0]
    d = g.to_dict()
    assert set(d) == {
        "key", "group_by", "count", "runs", "repeat_rate", "unrecovered_count",
        "unrecovered_share", "avg_turns_to_resolve", "total_tokens", "total_wall_ms",
        "example_anchors",
    }
