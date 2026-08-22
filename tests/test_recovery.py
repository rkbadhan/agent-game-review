"""Recovery state machine (spec §8.6).

The load-bearing distinction: an unchanged retry that succeeds is NOT good
recovery.
"""

from agr.pipeline import analyze
from agr.recovery import GOOD_RECOVERY, UNCHANGED_RETRY, UNRECOVERED
from agr.store import Store


def _analyze(tmp_path, load_fixture, name):
    return analyze(load_fixture(name), Store(str(tmp_path / "store")))


def test_strategy_change_before_success_is_good_recovery(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "tool_failure_recovery.atif.json")
    assert len(a.recoveries) == 1
    ep = a.recoveries[0]
    assert ep.classification == GOOD_RECOVERY
    assert ep.strategy_changed is True
    assert ep.failure_event_id == "evt_004"
    assert ep.resolution_event_id is not None


def test_unchanged_retry_is_not_good_recovery(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "stuck_retry.atif.json")
    assert len(a.recoveries) == 1
    ep = a.recoveries[0]
    assert ep.classification == UNCHANGED_RETRY
    assert ep.classification != GOOD_RECOVERY
    assert ep.strategy_changed is False
    assert ep.changed_action is False


def test_unresolved_failure_is_unrecovered(tmp_path, load_fixture):
    a = _analyze(tmp_path, load_fixture, "ignored_failure.atif.json")
    assert len(a.recoveries) == 1
    assert a.recoveries[0].classification == UNRECOVERED
    assert a.recoveries[0].resolution_event_id is None
