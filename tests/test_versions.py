"""Matched version comparison — §4.16 construction rules and §12.3 statistics.

These tests are the guard rails on the claim the surface makes. A comparison is
allowed to say "this version is worse" only when the slice is exact, the axis is
isolated, and the arithmetic is task-clustered; every test here pins one of the
ways that claim can go wrong:

- runs that do not match are excluded *with the key that excluded them*;
- an unresolved version field blocks the ``Matched`` label outright;
- a second changed axis downgrades the result to a configuration comparison;
- repeated runs of one task cannot outvote a task with a single run; and
- a thin slice is labelled insufficient rather than given a direction.

Pure stdlib: the store is built by running the real pipeline over mutated copies
of the shipped ATIF fixtures, so the comparison reads genuine derived records.
"""

import os

import pytest

from agr import demo, versions
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
TASK_FIXTURES = demo.DEMO_TASKS
BASE_FIELDS = {**demo.BASELINE, **demo.SHARED_KEYS}
CAND_FIELDS = {**demo.CANDIDATE, **demo.SHARED_KEYS}


def _load(name):
    return demo.load_fixture(FIXTURES, name)


def _variant(doc, **kw):
    return demo.variant(doc, **kw)


def _make_pass(doc):
    demo.set_outcome(doc, "passed")


def _make_fail(doc):
    demo.set_outcome(doc, "failed")


def _slice(tmp_path, **kw):
    """The demo generator, pointed at a temporary store (see :mod:`agr.demo`)."""
    return demo.build_slice(Store(str(tmp_path / "store")), FIXTURES, **kw)


def _compare(store, axis="evaluation_harness", **kw):
    return versions.compare_versions(store, {"sweep_id": "sweep_141"},
                                     {"sweep_id": "sweep_142"}, axis, **kw)


# --- construction: matching, exclusions, labels ------------------------------


def test_matched_slice_pairs_every_task(tmp_path):
    result = _compare(_slice(tmp_path))
    assert result["label"] == "matched"
    assert result["match_status"] == "valid"
    assert result["observed_change_axes"] == ["evaluation_harness"]
    assert result["report"]["exact_matched_tasks"] == len(TASK_FIXTURES)
    assert result["report"]["matched_run_pairs"] == len(TASK_FIXTURES)
    assert result["exclusions"] == []
    assert len(result["included_pair_ids"]) == len(TASK_FIXTURES)


def test_every_excluded_run_names_the_key_that_excluded_it(tmp_path):
    """§4.16.1: exclusions are grouped by reason, never a bare "no match"."""
    store = _slice(tmp_path)
    # A candidate-only task version, and a baseline-only task.
    analyze(_variant(_load("chess_best_move.atif.json"), suffix="__c_v4",
                     run_fields={**CAND_FIELDS, "task_version": "chess-best-move@4"}), store)
    analyze(_variant(_load("clean_pass.atif.json"), suffix="__b", run_fields=BASE_FIELDS), store)

    result = _compare(store)
    reasons = {(g["side"], g["reason"]): g for g in result["report"]["exclusions_by_reason"]}
    assert reasons[("candidate", "task_version_mismatch")]["count"] == 1
    assert reasons[("baseline", "missing_counterpart")]["count"] == 1
    # The excluded run is named, so the intersection can be audited.
    assert reasons[("baseline", "missing_counterpart")]["run_ids"] == ["greeting_file__clean_pass__b"]
    # The matched slice itself is unaffected by the runs it excluded.
    assert result["report"]["exact_matched_tasks"] == len(TASK_FIXTURES)
    assert result["caveats"], "a partial slice must say it is partial"


def test_unresolved_version_field_blocks_the_matched_label(tmp_path):
    """The spec's hard block: an unresolved version key is not "equal by absence"."""
    store = Store(str(tmp_path / "store"))
    for name in TASK_FIXTURES:
        doc = _load(name)
        base = {**BASE_FIELDS}
        cand = {**CAND_FIELDS}
        analyze(_variant(doc, suffix="__b", run_fields=base), store)
        d = _variant(doc, suffix="__c", run_fields=cand)
        d["run"].pop("verifier_version")
        analyze(d, store)

    result = _compare(store)
    assert "verifier_version" in result["unresolved_keys"]
    assert result["match_status"] == "invalid"
    assert result["label"] == "unmatched"
    assert "unresolved_version_field:verifier_version" in result["blocked_reasons"]


def test_unresolved_environment_key_is_a_caveat_not_a_block(tmp_path):
    """Environment/parameters degrade the claim; they do not invalidate it."""
    base = {k: v for k, v in BASE_FIELDS.items() if k != "environment_image_digest"}
    cand = {k: v for k, v in CAND_FIELDS.items() if k != "environment_image_digest"}
    store = Store(str(tmp_path / "store"))
    for name in TASK_FIXTURES:
        doc = _load(name)
        analyze(_variant(doc, suffix="__b", run_fields=base), store)
        analyze(_variant(doc, suffix="__c", run_fields=cand), store)

    result = _compare(store)
    assert result["unresolved_keys"] == ["environment_image_digest"]
    assert result["match_status"] == "valid"
    assert result["label"] == "matched"
    assert "environment_image_digest" not in result["match_keys_used"]
    assert any("environment_image_digest" in c for c in result["caveats"])


def test_second_changed_axis_downgrades_to_configuration_comparison(tmp_path):
    """§4.16.1: more than the declared axis moved, so nothing is isolated."""
    result = _compare(_slice(tmp_path, candidate_extra={"model": "demo-model-b"}))
    assert set(result["observed_change_axes"]) == {"evaluation_harness", "model"}
    assert result["label"] == "configuration_comparison"
    assert result["match_status"] == "valid"  # still a valid slice, weaker claim


def test_declaring_the_whole_configuration_never_earns_matched(tmp_path):
    result = _compare(_slice(tmp_path), axis="complete_configuration")
    assert result["label"] == "configuration_comparison"
    assert "does not isolate" not in result["interpretation"]["limit"]
    assert "no single component is isolated" in result["interpretation"]["limit"]


def test_empty_intersection_is_invalid(tmp_path):
    """No exactly matched task means there is no comparison to report."""
    store = Store(str(tmp_path / "store"))
    analyze(_variant(_load("chess_best_move.atif.json"), suffix="__b", run_fields=BASE_FIELDS), store)
    analyze(_variant(_load("clean_pass.atif.json"), suffix="__c", run_fields=CAND_FIELDS), store)
    result = _compare(store)
    assert result["match_status"] == "invalid"
    assert "no_exactly_matched_task" in result["blocked_reasons"]
    assert result["report"]["matched_run_pairs"] == 0


def test_unknown_axis_and_empty_side_are_refused(tmp_path):
    store = _slice(tmp_path)
    with pytest.raises(versions.ComparisonError):
        _compare(store, axis="vibes")
    with pytest.raises(versions.ComparisonError):
        versions.compare_versions(store, {"sweep_id": "sweep_141"},
                                  {"sweep_id": "nope"}, "evaluation_harness")


# --- statistics: task clustering, uncertainty, labels ------------------------


def _regress(doc, name):
    """Candidate regresses on every task except the recovery one."""
    if name != "tool_failure_recovery.atif.json":
        _make_fail(doc)


def test_a_seeded_regression_is_detected_on_the_matched_slice(tmp_path):
    """Milestone 5 accept: a seeded regression shows up as a regression."""
    store = _slice(tmp_path, baseline_mutate=lambda d, n: _make_pass(d),
                   candidate_mutate=_regress)
    result = _compare(store)
    pass_rate = result["pass_rate"]
    assert pass_rate["baseline"]["numerator"] == 5
    assert pass_rate["candidate"]["numerator"] == 1
    assert pass_rate["change"] == "regressed"
    low, high = pass_rate["statistics"]["interval"]
    assert high < 0, "an interval excluding zero is what licenses the claim"
    assert result["interpretation"]["summary"].startswith("On this matched slice")


def test_repeated_runs_of_one_task_cannot_outvote_a_single_run_task(tmp_path):
    """§12.3 Simpson's-paradox guard: the estimator is clustered by task.

    Three runs per task, and only *one* task regresses. Pooling runs would let
    that task's three regressing pairs swing the estimate; clustering gives every
    task one vote, so the paired mean stays at -1/5 and the interval covers zero.
    """
    def regress_one_task(doc, name):
        _make_pass(doc)
        if name == "stuck_retry.atif.json":
            _make_fail(doc)

    store = _slice(tmp_path, repeats=3, baseline_mutate=lambda d, n: _make_pass(d),
                   candidate_mutate=regress_one_task)
    result = _compare(store)
    assert result["report"]["repeated_run_strata"] == len(TASK_FIXTURES)
    assert result["report"]["matched_run_pairs"] == 3 * len(TASK_FIXTURES)
    stats = result["pass_rate"]["statistics"]
    assert stats["n_tasks"] == len(TASK_FIXTURES)      # one observation per task
    assert stats["difference"] == pytest.approx(-0.2)  # not -0.6
    assert result["pass_rate"]["change"] == "within_uncertainty"


def test_single_task_slice_is_insufficient_rather_than_confident(tmp_path):
    """§12.3: one paired observation is reported without inventing variance."""
    store = _slice(tmp_path, tasks=("chess_best_move.atif.json",),
                   candidate_mutate=lambda d, n: _make_pass(d))
    result = _compare(store)
    stats = result["pass_rate"]["statistics"]
    assert stats["n_tasks"] == 1
    assert stats["interval"] is None
    assert stats["estimable"] is False
    assert result["pass_rate"]["change"] == "insufficient_evidence"


def test_two_task_slice_shows_its_numbers_without_claiming_a_direction(tmp_path):
    """§12.2: sparse slices are gated by a declared default, not by silence.

    Two tasks moving the same way produce an interval that excludes zero. The
    row still reports both sides; it is only barred from saying "regressed".
    """
    store = _slice(tmp_path, tasks=TASK_FIXTURES[:2],
                   baseline_mutate=lambda d, n: _make_pass(d),
                   candidate_mutate=lambda d, n: _make_fail(d))
    result = _compare(store)
    stats = result["pass_rate"]["statistics"]
    assert stats["n_tasks"] == 2 and stats["estimable"] is True
    assert stats["interval"][1] < 0, "the arithmetic still excludes zero"
    assert result["pass_rate"]["change"] == "insufficient_evidence"
    assert result["pass_rate"]["baseline"]["numerator"] == 2
    assert result["pass_rate"]["candidate"]["numerator"] == 0


def test_every_metric_carries_its_numerator_denominator_and_counts(tmp_path):
    """§6.12: a stored result is never just a rendered percentage."""
    result = _compare(_slice(tmp_path))
    rows = [result["pass_rate"]] + result["behaviours"] + result["failure_modes"]
    for row in rows:
        for side in ("baseline", "candidate"):
            assert "numerator" in row[side] and "denominator" in row[side]
        assert "task_count" in row and "run_pairs" in row
        assert row["statistics"]["method"] == versions.STATISTICS_VERSION
    assert result["statistics_version"] == versions.STATISTICS_VERSION
    assert result["match_key_version"] == versions.MATCH_KEY_VERSION
    assert result["review_coverage"]["baseline"]["runs"] == len(TASK_FIXTURES)


def test_behaviour_rows_are_opportunity_gated_with_unevaluated_counted_apart(tmp_path):
    """§12.1 denominators are eligible opportunities, not verifier checks."""
    result = _compare(_slice(tmp_path))
    labels = {row["label"] for row in result["behaviours"]}
    assert labels == {"Recover from tool failure", "Verify before submission"}
    for row in result["behaviours"]:
        assert row["denominator_unit"] == "runs_with_eligible_opportunity"
        # An opportunity that occurred but was not evaluated is visible, not
        # silently folded into the denominator as a failure.
        assert "eligible_not_evaluated" in row["baseline"]


def test_failure_modes_are_counted_from_detectors_not_selected_cards(tmp_path):
    """Card selection (§8.10) ranks what a reviewer *sees*; it must not become a
    measured version difference. Both sides run the same detectors here, so every
    failure-mode row must be flat even though the candidate's outcome differs."""
    store = _slice(tmp_path, candidate_mutate=lambda d, n: _make_pass(d))
    result = _compare(store)
    artifact_rows = [r for r in result["failure_modes"]
                     if r["metric_id"].endswith("required_artifact_absent")]
    assert artifact_rows, "the fixture slice should observe this detector"
    (row,) = artifact_rows
    assert row["baseline"]["numerator"] == row["candidate"]["numerator"]
    assert row["change"] != "new_failure_mode"


def test_resource_rows_state_what_the_capture_lacks(tmp_path):
    result = _compare(_slice(tmp_path))
    rows = {r["metric_id"]: r for r in result["resources"]}
    assert rows["cost"]["change"] == "not_captured"
    assert rows["tokens"]["captured"] is False
    assert rows["duration_s"]["captured"] is True


# --- saved definitions -------------------------------------------------------


def test_saved_definition_round_trips_and_revisions(tmp_path):
    store = _slice(tmp_path)
    first = versions.save_comparison(store, {"sweep_id": "sweep_141"},
                                     {"sweep_id": "sweep_142"}, "evaluation_harness",
                                     name="harness 1.8 → 1.9")
    assert first["definition_revision"] == 1
    loaded = versions.load_comparison(store, first["comparison_id"])
    assert loaded["name"] == "harness 1.8 → 1.9"
    assert loaded["label"] == "matched"
    assert loaded["report"]["matched_run_pairs"] == len(TASK_FIXTURES)

    again = versions.save_comparison(store, {"sweep_id": "sweep_141"},
                                     {"sweep_id": "sweep_142"}, "evaluation_harness")
    assert again["comparison_id"] == first["comparison_id"]  # stable, shareable
    assert again["definition_revision"] == 2
    assert [d["comparison_id"] for d in versions.list_saved_comparisons(store)] == \
        [first["comparison_id"]]


def test_saved_comparison_recomputes_against_the_current_store(tmp_path):
    """The definition is the promise; the numbers are recomputed, never cached."""
    store = _slice(tmp_path)
    saved = versions.save_comparison(store, {"sweep_id": "sweep_141"},
                                     {"sweep_id": "sweep_142"}, "evaluation_harness")
    before = versions.load_comparison(store, saved["comparison_id"])
    doc = _load("clean_pass.atif.json")
    analyze(_variant(doc, suffix="__b", run_fields=BASE_FIELDS), store)
    analyze(_variant(doc, suffix="__c", run_fields=CAND_FIELDS), store)
    after = versions.load_comparison(store, saved["comparison_id"])
    assert after["report"]["matched_run_pairs"] == before["report"]["matched_run_pairs"] + 1


def test_unknown_comparison_id_raises(tmp_path):
    with pytest.raises(versions.ComparisonError):
        versions.load_comparison(_slice(tmp_path), "comparison_missing")


def test_list_configurations_offers_a_reproducible_selector(tmp_path):
    configs = {c["label"]: c for c in versions.list_configurations(_slice(tmp_path))}
    assert set(configs) == {"sweep_141", "sweep_142"}
    assert configs["sweep_141"]["selector"] == {"sweep_id": "sweep_141"}
    assert configs["sweep_141"]["run_count"] == len(TASK_FIXTURES)
    assert configs["sweep_141"]["task_count"] == len(TASK_FIXTURES)


# --- the demo slice ----------------------------------------------------------


def test_demo_store_builds_a_slice_the_surface_can_compare(tmp_path):
    """`agr demo-store` must produce what it promises: a matched slice with real
    exclusions, so the §4.16 surface is reachable from a fresh checkout."""
    store = Store(str(tmp_path / "store"))
    definition = demo.build_demo_store(store, FIXTURES)
    result = versions.compare_versions(store, definition["baseline"],
                                       definition["candidate"], definition["axis"])
    assert result["label"] == "matched"
    assert result["report"]["matched_run_pairs"] == len(demo.DEMO_TASKS)
    # Both exclusion paths are demonstrated, not just a clean intersection.
    assert {g["reason"] for g in result["report"]["exclusions_by_reason"]} == {
        "missing_counterpart", "task_version_mismatch"}
    assert result["pass_rate"]["change"] == "improved"


def test_demo_runs_are_marked_synthetic(tmp_path):
    """The fabricated candidate outcomes must be visible as fabricated."""
    from agr import read
    store = Store(str(tmp_path / "store"))
    demo.build_demo_store(store, FIXTURES)
    sources = {s["run_id"]: read.get_review(store, s["run_id"])["run"]
               for s in read.list_runs(store)}
    assert sources, "the demo store should hold runs"
    assert all(run["source_type"] == "synthetic_demo" for run in sources.values())


def test_default_pair_ignores_a_configuration_that_shares_no_slice(tmp_path):
    """A store often holds more than two configurations — the shipped fixtures
    plus a demo slice, say. The default must be the pair that shares a task
    slice, not whichever two sort first (which compares unrelated runs and
    labels the result a configuration comparison: true, and useless)."""
    store = Store(str(tmp_path / "store"))
    import glob
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.atif.json"))):
        analyze(_load(os.path.basename(path)), store)   # every fixture, as shipped
    demo.build_demo_store(store, FIXTURES)              # plus a demo slice

    configurations = versions.list_configurations(store)
    assert {c["label"] for c in configurations} == {"harbor-0.9", "sweep_141", "sweep_142"}
    # The shipped fixtures share *more* tasks with the demo baseline than the two
    # demo sides share with each other, so ranking by slice size picks the wrong
    # pair. Isolation wins: the demo sides differ on one axis, the fixtures differ
    # on harness *and* environment (they declare no digest at all).
    left, right = versions.default_pair(configurations)
    assert (left["label"], right["label"]) == ("sweep_141", "sweep_142")


def test_default_pair_is_none_when_nothing_is_comparable(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_variant(_load("chess_best_move.atif.json"), suffix="__b",
                     run_fields=BASE_FIELDS), store)
    analyze(_variant(_load("clean_pass.atif.json"), suffix="__c",
                     run_fields=CAND_FIELDS), store)
    assert versions.default_pair(versions.list_configurations(store)) == (None, None)
