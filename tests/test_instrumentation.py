"""Product instrumentation (§4.21): the allowlist, and the measures it supports.

The spec's constraint is that the analytics log must not capture unredacted trace
content. That is a guarantee about what *cannot* be written, so most of these
tests are refusals: an unknown event, an undeclared property, a value shaped like
prose or a pasted artifact. The rest check that the derived product measures are
computed from the log and report "not observed" rather than zero when a workflow
was never exercised.
"""

import pytest

from agr import instrumentation
from agr.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "store"))


# --- the allowlist -----------------------------------------------------------


def test_records_a_declared_event_with_declared_properties(tmp_path):
    store = _store(tmp_path)
    entry = instrumentation.record(store, "run_opened", session_id="sess_a",
                                   properties={"run_id": "chess__seed42",
                                               "review_mode": "deterministic_only"})
    assert entry["event"] == "run_opened"
    assert entry["properties"]["run_id"] == "chess__seed42"
    assert store.read_events() == [entry]


def test_unknown_event_name_is_refused(tmp_path):
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record(_store(tmp_path), "reviewer_thought", session_id="sess_a")


def test_undeclared_property_is_refused(tmp_path):
    """There is no `note`/`content` key, which is how prose would arrive."""
    with pytest.raises(instrumentation.InstrumentationError) as exc:
        instrumentation.record(_store(tmp_path), "feedback_submitted", session_id="sess_a",
                               properties={"note": "the agent gave up early"})
    assert "undeclared" in str(exc.value)


def test_free_text_shaped_values_are_refused(tmp_path):
    """Whitespace and length are the two shapes trace content arrives in."""
    store = _store(tmp_path)
    with pytest.raises(instrumentation.InstrumentationError) as prose:
        instrumentation.record(store, "moment_viewed", session_id="sess_a",
                               properties={"moment_id": "C3 still failing at submission"})
    assert "free text" in str(prose.value)
    with pytest.raises(instrumentation.InstrumentationError) as long:
        instrumentation.record(store, "moment_viewed", session_id="sess_a",
                               properties={"moment_id": "x" * (instrumentation.MAX_STRING + 1)})
    assert "characters" in str(long.value)
    assert store.read_events() == [], "a refused event must not be written"


def test_wrong_type_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record(store, "queue_view_created", session_id="sess_a",
                               properties={"count": "many"})
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record(store, "queue_view_created", session_id="sess_a",
                               properties={"count": True})  # bool is not an int here


def test_list_properties_hold_tokens_only(tmp_path):
    store = _store(tmp_path)
    instrumentation.record(store, "queue_filter_changed", session_id="sess_a",
                           properties={"filters": ["failed", "unreviewed"]})
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record(store, "correction_saved", session_id="sess_a",
                               properties={"corrected_fields": ["the consequence was wrong"]})


def test_a_batch_is_all_or_nothing(tmp_path):
    """A broken client cannot half-write a session's history."""
    store = _store(tmp_path)
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record_batch(store, [
            {"event": "run_opened", "session_id": "sess_a", "properties": {"run_id": "r1"}},
            {"event": "not_an_event", "session_id": "sess_a"},
        ])
    assert store.read_events() == []


def test_batch_requires_a_session(tmp_path):
    with pytest.raises(instrumentation.InstrumentationError):
        instrumentation.record_batch(_store(tmp_path), [{"event": "sweep_opened"}])


def test_the_event_vocabulary_matches_the_spec_exactly():
    """§4.21's minimum events, verbatim and in order, so neither list can drift."""
    assert instrumentation.EVENTS == (
        "sweep_opened", "queue_view_created", "queue_filter_changed", "queue_sort_changed",
        "run_opened", "review_chapter_viewed", "moment_viewed", "evidence_opened",
        "claim_evidence_opened", "full_trace_opened", "feedback_submitted",
        "quick_relabel_submitted", "correction_saved", "run_disposition_set",
        "next_unhandled_opened", "lesson_created", "lesson_approved", "lesson_rejected",
        "experiment_proposed", "experiment_approved",
        "comparison_definition_saved", "comparison_exclusion_opened", "replay_requested")


# --- derived measures --------------------------------------------------------


def _session(store, session, events):
    for event, props, at in events:
        instrumentation.record(store, event, session_id=session, properties=props, at=at)


def test_measures_are_computed_from_the_log(tmp_path):
    store = _store(tmp_path)
    _session(store, "sess_a", [
        ("sweep_opened", {}, "2026-07-20T10:00:00Z"),
        ("run_opened", {"run_id": "r1", "review_mode": "model_enriched"}, "2026-07-20T10:00:30Z"),
        ("moment_viewed", {"run_id": "r1", "moment_id": "m1"}, "2026-07-20T10:01:00Z"),
        ("evidence_opened", {"run_id": "r1", "moment_id": "m1"}, "2026-07-20T10:01:10Z"),
        ("run_disposition_set", {"run_id": "r1", "disposition": "corrected"}, "2026-07-20T10:02:00Z"),
    ])
    m = instrumentation.product_measures(store)
    assert m["sessions"] == 1 and m["events"] == 5
    assert m["median_seconds_sweep_open_to_first_disposition"] == 120
    assert m["median_seconds_run_open_to_first_moment"] == 30
    assert m["moment_views_opening_evidence"] == 1.0
    assert m["in_progress_runs_later_dispositioned"] == 1.0
    assert m["model_enriched_open_rate"] == 1.0
    assert m["counts"]["moment_viewed"] == 1


def test_an_unexercised_workflow_is_not_observed_rather_than_zero(tmp_path):
    store = _store(tmp_path)
    instrumentation.record(store, "sweep_opened", session_id="sess_a")
    m = instrumentation.product_measures(store)
    assert m["median_seconds_sweep_open_to_first_disposition"] is None
    assert m["moment_views_opening_evidence"] is None
    assert m["counts"]["run_opened"] == 0


def test_correction_rates_separate_the_two_tiers(tmp_path):
    store = _store(tmp_path)
    _session(store, "sess_a", [
        ("feedback_submitted", {"kind": "agree"}, "2026-07-20T10:00:00Z"),
        ("feedback_submitted", {"kind": "agree"}, "2026-07-20T10:00:05Z"),
        ("quick_relabel_submitted", {"kind": "lost_requirement"}, "2026-07-20T10:00:10Z"),
        ("correction_saved", {"corrected_fields": ["consequence"]}, "2026-07-20T10:00:20Z"),
    ])
    m = instrumentation.product_measures(store)
    assert m["quick_relabel_rate"] == 0.25
    assert m["full_correction_rate"] == 0.25


def test_gaps_are_scoped_per_run_so_parallel_work_is_not_conflated(tmp_path):
    store = _store(tmp_path)
    _session(store, "sess_a", [
        ("run_opened", {"run_id": "r1"}, "2026-07-20T10:00:00Z"),
        ("run_opened", {"run_id": "r2"}, "2026-07-20T10:00:10Z"),
        ("moment_viewed", {"run_id": "r2", "moment_id": "m9"}, "2026-07-20T10:00:20Z"),
        ("moment_viewed", {"run_id": "r1", "moment_id": "m1"}, "2026-07-20T10:00:40Z"),
    ])
    m = instrumentation.product_measures(store)
    # r2 took 10s and r1 took 40s; conflating them would report a single 20s gap.
    assert m["median_seconds_run_open_to_first_moment"] == 25


def test_fast_path_open_rate_tracks_the_entry_preference(tmp_path):
    store = _store(tmp_path)
    _session(store, "sess_a", [
        ("run_opened", {"run_id": "r1", "fast_path": True}, "2026-07-20T10:00:00Z"),
        ("run_opened", {"run_id": "r2", "fast_path": False}, "2026-07-20T10:00:10Z"),
    ])
    m = instrumentation.product_measures(store)
    # One of two opens took the §4.3.5 fast path (opened on the first key moment).
    assert m["fast_path_open_rate"] == 0.5


def test_uninstrumentable_measures_are_named_not_omitted(tmp_path):
    m = instrumentation.product_measures(_store(tmp_path))
    assert any("adjudication" in note for note in m["not_yet_instrumented"])
    # The fast path (§4.3.5) and the Eval Lesson funnel (§4.11) are now
    # instrumented, so both are reported measures rather than named gaps.
    assert not any("fast path" in note for note in m["not_yet_instrumented"])
    assert not any("lesson" in note for note in m["not_yet_instrumented"])
    assert "fast_path_open_rate" in m
    assert "lesson_approval_rate" in m and "approved_lesson_experiment_rate" in m
