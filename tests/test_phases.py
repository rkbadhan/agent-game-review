"""Deterministic phase segmentation (Stage C1)."""

import json
import os

from agr.phases import segment_phases
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _analyze(tmp_path, name):
    return analyze(_load(name), Store(str(tmp_path / "store")))


def test_chess_segments_into_four_structural_phases(tmp_path):
    a = _analyze(tmp_path, "chess_best_move.atif.json")
    assert [p.kind for p in a.phases] == ["intake", "planning", "execution", "submission"]
    assert [p.label for p in a.phases] == ["Task intake", "Planning", "Execution", "Submission"]


def test_every_event_belongs_to_exactly_one_phase(tmp_path):
    a = _analyze(tmp_path, "chess_best_move.atif.json")
    covered = [eid for p in a.phases for eid in p.event_ids]
    assert covered == [e.event_id for e in a.events]  # ordered, no gaps
    assert len(covered) == len(set(covered))           # no overlaps
    # Every event is stamped with its containing phase.
    by_phase = {eid: p.phase_id for p in a.phases for eid in p.event_ids}
    for e in a.events:
        assert e.phase_id == by_phase[e.event_id]


def test_execution_splits_into_attempts_at_strategy_change(tmp_path):
    a = _analyze(tmp_path, "tool_failure_recovery.atif.json")
    exec_phases = [p for p in a.phases if p.kind == "execution"]
    assert len(exec_phases) == 2
    assert exec_phases[1].attempt == 2
    assert "attempt 2" in exec_phases[1].label


def test_segmentation_is_deterministic(tmp_path):
    doc = _load("chess_best_move.atif.json")
    a = analyze(doc, Store(str(tmp_path / "s1")))
    b = analyze(doc, Store(str(tmp_path / "s2")))
    assert [p.to_dict() for p in a.phases] == [p.to_dict() for p in b.phases]


def test_run_without_planning_has_no_planning_phase(tmp_path):
    # stuck_retry goes straight to tool use with no leading model_output.
    a = _analyze(tmp_path, "stuck_retry.atif.json")
    assert "planning" not in [p.kind for p in a.phases]
    assert [p.kind for p in a.phases][0] == "intake"


def test_phases_persisted_with_derivation_version(tmp_path):
    from agr import version
    store = Store(str(tmp_path / "store"))
    a = analyze(_load("chess_best_move.atif.json"), store)
    persisted = store.read_derived(a.run_source.run_id, a.run_source.source_capture_id, "phases.json")
    assert persisted, "phases.json is persisted"
    # Every derived record embeds the version that produced it (spec §7.3).
    for ph in persisted:
        assert ph["derivation_version"] == version.PHASE_SEGMENTATION_VERSION
