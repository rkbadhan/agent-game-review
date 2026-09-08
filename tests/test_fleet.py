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
    group's unrecovered_share must be 1.0. AGR-06: repeat_rate is
    affected_runs_with_more_than_one_episode / affected_runs — a SINGLETON
    affected run (one episode, one run) is 0.0, not 1.0 (the old, wrong
    "episodes per run" formula read a group appearing once in one run as a
    100% repeat)."""
    store = _store(tmp_path, "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store)
    assert len(groups) == 1
    g = groups[0]
    assert g.unrecovered_share == 1.0
    assert g.unrecovered_count == 1
    assert g.repeat_rate == 0.0


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


# --- AGR-06: repeat_rate is affected_runs_with_>1_episode / affected_runs --


def test_repeat_rate_two_episodes_in_one_affected_run_is_100_percent():
    g = fleet.EpisodeGroup(key=("shell", "X"), group_by=["tool", "error_signature"],
                           count=2, run_ids=["r1", "r1"])
    assert g.repeat_rate == 1.0


def test_repeat_rate_singleton_affected_runs_is_0_percent():
    """Acceptance: a group whose episodes never repeat within any one run —
    three affected runs, one episode each — is 0%, not the old formula's
    3 episodes / 3 runs = 1.0 misread as a 100% repeat."""
    g = fleet.EpisodeGroup(key=("shell", "X"), group_by=["tool", "error_signature"],
                           count=3, run_ids=["r1", "r2", "r3"])
    assert g.repeat_rate == 0.0


def test_repeat_rate_mixed_affected_runs():
    # r1 repeats (2 episodes); r2 and r3 are singletons -> 1 of 3 affected runs.
    g = fleet.EpisodeGroup(key=("shell", "X"), group_by=["tool", "error_signature"],
                           count=4, run_ids=["r1", "r1", "r2", "r3"])
    assert g.repeat_rate == pytest.approx(1 / 3)


def test_repeat_rate_empty_group_is_unavailable_not_zero():
    g = fleet.EpisodeGroup(key=("shell", "X"), group_by=["tool", "error_signature"],
                           count=0, run_ids=[])
    assert g.repeat_rate is None
    assert g.to_dict()["repeat_rate"] is None


def test_repeat_rate_stays_within_zero_one_bounds():
    import random
    rng = random.Random(0)
    for _ in range(50):
        run_ids: list[str] = []
        for i in range(rng.randint(1, 6)):
            run_ids += [f"r{i}"] * rng.randint(1, 5)
        g = fleet.EpisodeGroup(key=("t", "e"), group_by=["tool", "error_signature"],
                               count=len(run_ids), run_ids=run_ids)
        assert 0.0 <= g.repeat_rate <= 1.0


def test_group_by_tool_only_merging_distinct_runs_has_zero_repeat_rate(tmp_path):
    """Mixed-tool-sharing group (three DIFFERENT runs merged by tool alone)
    must not corrupt the denominator into reading as a repeat."""
    store = _store(tmp_path, "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
                   "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store, group_by=["tool"])
    assert groups[0].distinct_runs == 3
    assert groups[0].repeat_rate == 0.0


# --- AGR-06: group/fleet usage is a UNION of underlying records, not a sum --


def test_usage_union_yields_300_not_the_naive_500_sum():
    """Acceptance: two episodes' windows are 300 and 200 tokens; the second
    is a full subset of the first (they share event e2's 200 tokens) — the
    union is 300, never the naive 300+200=500."""
    eps = [
        {"run_id": "r1", "usage_records": {"e1": 100, "e2": 200}},  # 300 total
        {"run_id": "r1", "usage_records": {"e2": 200}},              # entirely shared with e1's episode
    ]
    total, overlapping = fleet._usage_union(eps)
    assert total == 300
    assert overlapping == 1


def test_usage_union_across_different_runs_never_collides_on_a_bare_event_id():
    """Event ids are unique only within one run's own capture — the union
    keys on (run_id, event_id), so the identical id string in two different
    runs must not be treated as the same event."""
    eps = [
        {"run_id": "r1", "usage_records": {"evt_001": 50}},
        {"run_id": "r2", "usage_records": {"evt_001": 75}},
    ]
    total, overlapping = fleet._usage_union(eps)
    assert total == 125
    assert overlapping == 0


def test_usage_union_with_no_overlap_equals_the_plain_sum():
    eps = [
        {"run_id": "r1", "usage_records": {"e1": 100}},
        {"run_id": "r1", "usage_records": {"e2": 50}},
    ]
    total, overlapping = fleet._usage_union(eps)
    assert total == 150
    assert overlapping == 0


def test_fleet_usage_summary_unions_across_groups_not_just_within_one(tmp_path):
    """The fleet-wide headline must not double-count an event shared by two
    episodes that land in DIFFERENT groups (different tool/signature)."""
    store = Store(str(tmp_path / "store"))
    summary = fleet.fleet_usage_summary(store)
    assert summary.total_tokens == 0
    assert summary.episode_count == 0
    assert summary.affected_runs == 0


def test_fleet_usage_summary_matches_real_episodes(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json", "ignored_failure.atif.json")
    summary = fleet.fleet_usage_summary(store)
    assert summary.episode_count == 2
    assert summary.affected_runs == 2
    # No shared events across two independent runs' episodes.
    assert summary.overlapping_usage_events == 0


# --- AGR-06: a capture revision does not multiply an episode's group -------


def test_reingesting_the_same_run_does_not_double_count_episodes(tmp_path):
    """Re-ingesting the identical source (idempotent capture) — and, more to
    the point, only the LATEST capture revision ever feeding fleet_episodes
    — must not multiply a run's episodes into its group."""
    with open(os.path.join(FIXTURES, "ignored_failure.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    store = Store(str(tmp_path / "store"))
    analyze(doc, store)
    analyze(doc, store)  # identical source: idempotent, no new capture
    groups = fleet.fleet_episodes(store)
    assert len(groups) == 1
    assert groups[0].count == 1
    assert groups[0].distinct_runs == 1


def test_confirmed_plausible_unrecovered_counts_partition_the_group(tmp_path):
    """AGR-04: every episode lands in exactly one of confirmed/plausible/
    unrecovered — the three explicit counts always sum to the group's total,
    with none silently absorbed into another."""
    store = _store(tmp_path, "stuck_retry.atif.json", "tool_failure_recovery.atif.json",
                   "ignored_failure.atif.json")
    groups = fleet.fleet_episodes(store)
    for g in groups:
        assert g.confirmed_count + g.plausible_count + g.unrecovered_count == g.count
    # tool_failure_recovery.atif.json's episode is a confirmed good_recovery.
    recovered = next(g for g in groups if g.confirmed_count == 1)
    assert recovered.plausible_count == 0 and recovered.unrecovered_count == 0
    # ignored_failure.atif.json's episode is unrecovered.
    unrecovered = next(g for g in groups if g.unrecovered_count == 1)
    assert unrecovered.confirmed_count == 0 and unrecovered.plausible_count == 0


def test_to_dict_shape(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json")
    g = fleet.fleet_episodes(store)[0]
    d = g.to_dict()
    assert set(d) == {
        "key", "group_by", "count", "runs", "repeat_rate", "unrecovered_count",
        "unrecovered_share",
        # AGR-04: confirmed/plausible recovery are explicit, separate counts —
        # never silently folded into "unrecovered" or each other.
        "confirmed_count", "confirmed_share", "plausible_count", "plausible_share",
        "avg_turns_to_resolve", "total_tokens", "total_wall_ms",
        # AGR-06: total_tokens is a union, not a plain sum — overlapping_
        # usage_events and usage_note say so explicitly.
        "overlapping_usage_events", "usage_note",
        # AGR-05/06 (review 82cc113): how many/whether this group's episodes
        # ever had usage instrumented at all — total_tokens alone cannot tell
        # a genuine zero apart from unmeasured usage.
        "usage_unavailable_count",
        # Follow-up (review of commit 5782f1b): how many episodes were only
        # PARTIALLY instrumented (distinct from fully unavailable).
        "usage_partial_count", "usage_availability",
        # AGR-07 (review 82cc113): whether this group's episodes selected
        # error_signature via the opaque fallback tier.
        "fallback_basis_count", "all_fallback_basis",
        "example_anchors",
    }


# --- AGR-05/06 (review 82cc113): usage availability, not a bare zero --------


def _doc_with_run_id_and_cost(run_id, cost=None):
    """A copy of tool_failure_recovery.atif.json under a different run id,
    optionally stamping ``cost`` onto its resolving step (s9, "python
    solve.py" succeeding) — the fixture otherwise carries no cost data at
    all, which is exactly what makes its own episode usage_completeness ==
    "unavailable". NOTE: the recovery window has TWO turns (s6/s7 "pip
    install" and s8/s9 "python solve.py") — costing only s9 leaves s7's
    turn uncovered, so this alone produces "partial", not "complete". Use
    :func:`_doc_with_full_window_cost` for a genuinely fully-measured
    episode."""
    with open(os.path.join(FIXTURES, "tool_failure_recovery.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["run"]["logical_run_id"] = run_id
    if cost is not None:
        for step in doc["steps"]:
            if step["step_id"] == "s9":
                step["cost"] = cost
    return doc


def _doc_with_full_window_cost(run_id, cost):
    """Like :func:`_doc_with_run_id_and_cost`, but costs BOTH of the
    recovery window's turns (s7 and s9) — genuinely "complete" coverage,
    not just "some cost present somewhere in the window"."""
    with open(os.path.join(FIXTURES, "tool_failure_recovery.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["run"]["logical_run_id"] = run_id
    for step in doc["steps"]:
        if step["step_id"] in ("s7", "s9"):
            step["cost"] = cost
    return doc


def test_group_usage_unavailable_reads_as_such_not_a_bare_zero(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json")
    g = fleet.fleet_episodes(store)[0]
    assert g.usage_unavailable_count == 1 == g.count
    assert g.usage_availability == "unavailable"
    assert g.total_tokens == 0


def test_group_usage_partial_when_only_some_episodes_are_measured(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_doc_with_run_id_and_cost("run_a"), store)  # no cost: unavailable
    analyze(_doc_with_full_window_cost(
        "run_b", {"input_tokens": 40, "output_tokens": 10}), store)  # fully measured
    groups = fleet.fleet_episodes(store)
    assert len(groups) == 1  # same tool + error_signature: one group
    g = groups[0]
    assert g.count == 2
    assert g.usage_unavailable_count == 1
    assert g.usage_availability == "partial"
    assert g.total_tokens == 100  # run_b's cost stamped on both of its window's turns


def test_group_usage_measured_when_every_episode_has_some_usage(tmp_path):
    """Genuinely "measured" requires FULL window coverage — costing only one
    of the window's two turns (as the other tests in this section do, on
    purpose, to get "partial") would not qualify; use
    _doc_with_full_window_cost so both turns are covered."""
    store = Store(str(tmp_path / "store"))
    analyze(_doc_with_full_window_cost("run_a", {"input_tokens": 40, "output_tokens": 10}), store)
    analyze(_doc_with_full_window_cost("run_b", {"input_tokens": 5, "output_tokens": 0}), store)
    g = fleet.fleet_episodes(store)[0]
    assert g.usage_unavailable_count == 0
    assert g.usage_partial_count == 0
    assert g.usage_availability == "measured"


def test_fleet_usage_summary_reports_unavailable_episode_count(tmp_path):
    store = _store(tmp_path, "tool_failure_recovery.atif.json")
    summary = fleet.fleet_usage_summary(store)
    assert summary.usage_unavailable_episode_count == 1
    assert "never had usage instrumented" in summary.to_dict()["usage_note"]


# --- Follow-up (review of commit 5782f1b): partial usage lost its own -------
# qualifier in fleet aggregation — a group/fleet made entirely of
# individually-"partial" episodes (some but not all of an episode's OWN
# window instrumented) previously read as plain "measured", since only
# usage_unavailable_count (the fully-unmeasured tier) was ever counted.


def _doc_with_partial_window(run_id):
    """tool_failure_recovery.atif.json's recovery window has two turns
    (s6/s7 "pip install", s8/s9 "python solve.py") — stamping cost on only
    the FIRST turn's result (s7) leaves the second uncovered, which is
    exactly usage_completeness == "partial" (some but not all of the
    window measured), never "unavailable" (nothing measured) or "complete"."""
    with open(os.path.join(FIXTURES, "tool_failure_recovery.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["run"]["logical_run_id"] = run_id
    for step in doc["steps"]:
        if step["step_id"] == "s7":
            step["cost"] = {"input_tokens": 12, "output_tokens": 3}
    return doc


def test_group_usage_availability_is_partial_when_every_episode_is_individually_partial(tmp_path):
    """A group whose episodes are all individually "partial" must not read
    as "measured" just because none of them is fully "unavailable" — the
    old usage_availability only ever looked at usage_unavailable_count."""
    store = Store(str(tmp_path / "store"))
    analyze(_doc_with_partial_window("run_a"), store)
    g = fleet.fleet_episodes(store)[0]
    assert g.usage_unavailable_count == 0
    assert g.usage_partial_count == 1 == g.count
    assert g.usage_availability == "partial"
    assert g.to_dict()["usage_availability"] == "partial"
    assert "only part of their window instrumented" in g.to_dict()["usage_note"]


def test_group_usage_availability_partial_from_mixed_unavailable_and_partial_episodes(tmp_path):
    """A mix of an "unavailable" episode and a "partial" one is still
    "partial" overall (some measurement exists, but not fully), not
    "unavailable" (that requires EVERY episode to be unavailable)."""
    store = Store(str(tmp_path / "store"))
    analyze(_doc_with_run_id_and_cost("run_a"), store)  # unavailable
    analyze(_doc_with_partial_window("run_b"), store)   # partial
    g = fleet.fleet_episodes(store)[0]
    assert g.count == 2
    assert g.usage_unavailable_count == 1
    assert g.usage_partial_count == 1
    assert g.usage_availability == "partial"


def test_fleet_usage_summary_reports_partial_episode_count(tmp_path):
    store = _store(tmp_path)
    analyze(_doc_with_partial_window("run_a"), store)
    summary = fleet.fleet_usage_summary(store)
    assert summary.usage_partial_episode_count == 1
    assert summary.usage_unavailable_episode_count == 0
    assert summary.usage_availability == "partial"
    d = summary.to_dict()
    assert d["usage_availability"] == "partial"
    assert "only part of their window instrumented" in d["usage_note"]
