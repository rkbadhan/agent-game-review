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


# --- AGR-05: recovery must link to the failed operation -----------------------

def _events():
    """failed pytest -> successful pwd -> pytest retried -> success."""
    from agr.schema import DerivedEvent

    def ev(eid, etype, tool=None, content="", seq=None, exit_code=None):
        payload = {}
        if tool:
            payload["tool"] = tool
        if content:
            payload["content"] = content
        if exit_code is not None:
            payload["exit_code"] = exit_code
        return DerivedEvent(event_id=eid, run_id="r", source_capture_id="c",
                            sequence=seq or int(eid.split("_")[1]),
                            source_step_ids=[eid], event_type=etype,
                            actor="main_agent", payload=payload)

    return [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: assertion failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="pwd"),
        ev("evt_4", "tool_result", tool="shell", content="/work", exit_code=0),
        ev("evt_5", "tool_call", tool="shell", content="pytest tests/ -k fast"),
        ev("evt_6", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]


def test_unrelated_success_does_not_close_the_failure_episode():
    """Acceptance (AGR-05): failed pytest followed by successful pwd is not
    good recovery — the episode stays open until the pytest retry succeeds."""
    from agr.recovery import classify_recoveries
    eps = classify_recoveries(_events(), "r", "c")
    assert len(eps) == 1
    ep = eps[0]
    # The pwd success resolved nothing; the pytest retry did.
    assert ep.resolution_event_id == "evt_6"
    assert ep.classification == GOOD_RECOVERY  # changed-argument retry of pytest


def test_failed_operation_never_resolved_stays_unrecovered():
    from agr.recovery import UNRECOVERED, classify_recoveries
    evs = _events()[:4]  # failed pytest, then pwd — no retry
    eps = classify_recoveries(evs, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].resolution_event_id is None
