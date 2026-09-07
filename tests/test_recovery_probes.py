"""F4 follow-up probes (review 2026-09-06): recovery linkage must track the
failed operation — different objectives are not recoveries, independent
failures stay independent, and change evidence attaches to the resolving
attempt, not every intervening call.
"""

from agr.recovery import (
    GOOD_RECOVERY,
    UNCHANGED_RETRY,
    UNRECOVERED,
    classify_recoveries,
)
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


def test_same_executable_different_subcommand_is_not_a_recovery():
    """Probe 1: ``python -m pytest tests/`` fails, then ``python --version``
    succeeds — the same tool and even the same executable, but a different
    objective. It must not classify as good_recovery."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="python -m pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="python --version"),
        ev("evt_4", "tool_result", tool="shell", content="Python 3.12.3", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].resolution_event_id is None


def test_unrelated_failure_stays_an_independent_episode():
    """Probe 2: pytest fails, an unrelated curl fails, pytest succeeds — the
    unresolved curl failure must survive as its own episode, never be consumed
    as part of the pytest episode."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="curl https://api.example.com"),
        ev("evt_4", "tool_result", tool="shell", content="connection refused", exit_code=7),
        ev("evt_5", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_6", "tool_result", tool="shell", content="3 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    by_failure = {ep.failure_event_id: ep for ep in eps}
    assert len(eps) == 2
    # The pytest retry resolves the pytest episode (unchanged retry: the
    # successful retry itself is not changed).
    assert by_failure["evt_2"].classification == UNCHANGED_RETRY
    # The curl failure keeps its own unresolved episode.
    assert by_failure["evt_4"].classification == UNRECOVERED
    assert by_failure["evt_4"].resolution_event_id is None


def test_intervening_unrelated_call_does_not_make_a_retry_changed():
    """Probe 3: pytest fails, an unrelated pwd succeeds, an identical pytest
    retry succeeds — the resolving attempt is unchanged, so this is
    retry_succeeded_without_strategy_change, not good_recovery."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="pwd"),
        ev("evt_4", "tool_result", tool="shell", content="/work", exit_code=0),
        ev("evt_5", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_6", "tool_result", tool="shell", content="3 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    ep = eps[0]
    assert ep.classification == UNCHANGED_RETRY
    assert ep.classification != GOOD_RECOVERY
    assert ep.changed_action is False
    assert ep.resolution_event_id == "evt_6"


def test_narrowed_rerun_cannot_resolve_the_failed_objective():
    """Review 2026-09-07 (R5) acceptance: a narrowed rerun that succeeds does
    not establish recovery — running a subset that may exclude the failing
    test proves nothing about the originally failed check. The episode stays
    unrecovered (the success of the narrowed command is recorded as evidence
    the run continued, nothing more)."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="pytest tests/ -k fast"),
        ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].resolution_event_id is None


def test_different_module_success_cannot_resolve_pytest_failure():
    """Review 2026-09-07 (R5) acceptance: the module/subcommand identity is
    preserved — a successful ``python -m compileall tests/`` does not resolve
    a failed ``python -m pytest tests/``."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="python -m pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="python -m compileall tests/"),
        ev("evt_4", "tool_result", tool="shell", content="compiling ok", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].resolution_event_id is None


def test_exact_rerun_still_resolves_as_unchanged_retry():
    """The conservative identity keeps the legitimate case working: an exact
    rerun of the failed command that succeeds is
    retry_succeeded_without_strategy_change (never good_recovery)."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="python -m pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="python -m pytest tests/"),
        ev("evt_4", "tool_result", tool="shell", content="3 passed", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNCHANGED_RETRY
    assert eps[0].resolution_event_id == "evt_4"


def test_changed_resolving_attempt_is_still_good_recovery():
    """A genuine strategy change (an explicit strategy_change event) before a
    success of the SAME objective remains good_recovery."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_2b", "strategy_change", content="switch to targeted module rerun", seq=3),
        ev("evt_3", "tool_call", tool="shell", content="pytest tests/", seq=4),
        ev("evt_4", "tool_result", tool="shell", content="1 passed", exit_code=0, seq=5),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == GOOD_RECOVERY
    assert eps[0].strategy_changed is True


def test_parallel_call_success_cannot_resolve_the_other_calls_failure():
    """Review 2026-09-07 (R2+R5): with tool-use ids recorded, a result is
    paired with THE call it answers — a parallel curl success cannot close a
    pytest failure episode, and the pytest retry resolves it by its own id."""
    def call(eid, cid, content):
        e = ev(eid, "tool_call", tool="shell", content=content)
        e.payload["tool_use_id"] = cid
        return e

    def result(eid, cid, content, code):
        e = ev(eid, "tool_result", tool="shell", content=content, exit_code=code)
        e.payload["tool_use_id"] = cid
        return e

    events = [
        call("evt_1", "toolu_a", "python -m pytest tests/"),
        call("evt_2", "toolu_b", "curl https://api.example.com/health"),
        result("evt_3", "toolu_a", "E: failed", 1),          # pytest result...
        result("evt_4", "toolu_b", "200 OK", 0),              # ...arrives after curl's
        call("evt_5", "toolu_c", "curl https://api.example.com/health"),
        result("evt_6", "toolu_c", "200 OK", 0),              # curl retried, succeeded
    ]
    eps = classify_recoveries(events, "r", "c")
    by_failure = {ep.failure_event_id: ep for ep in eps}
    # The curl result's success pairs with the curl call (id-linked), not with
    # the pytest call adjacency would suggest: pytest stays unresolved.
    assert by_failure["evt_3"].classification == UNRECOVERED
    assert by_failure["evt_3"].resolution_event_id is None


def test_changed_attempt_without_success_records_changed_action():
    """An unrecovered failure after the agent re-attempted the exact failed
    command records the repeated attempt. Review 2026-09-07 (R5): a narrowed
    or different command is a different objective — it establishes nothing
    about this episode, so changed_action stays False."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="pytest tests/"),
        ev("evt_2", "tool_result", tool="shell", content="E: failed", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="pytest tests/ -k fast"),
        ev("evt_4", "tool_result", tool="shell", content="E: still failed", exit_code=1),
        ev("evt_5", "tool_call", tool="shell", content="final answer"),
        ev("evt_6", "tool_result", tool="shell", content="submitted", exit_code=0,
           ),  # noqa: EDT001
        ev("evt_7", "final_submission", content="done"),
    ]
    eps = classify_recoveries(events, "r", "c")
    # Two episodes: the failed pytest objective, and the narrowed rerun's own
    # failure — a different objective forms its own episode (F1 follow-up).
    by_failure = {ep.failure_event_id: ep for ep in eps}
    assert set(by_failure) == {"evt_2", "evt_4"}
    for ep in by_failure.values():
        assert ep.classification == UNRECOVERED
        assert ep.resolution_event_id is None
    # A narrowed command establishes nothing about the original objective.
    assert by_failure["evt_2"].changed_action is False
