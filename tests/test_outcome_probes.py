"""F3 follow-up probes (review 2026-09-06): unknown outcomes and verifier
timing must never become false statements.

Covers the three paths the review reproduced — the outcome roll-up, the
agent-observed timing parse, and the honest wording those records support —
plus the ordering rule (a failed observation followed by a later pass).
"""

from agr.checks import outcome
from agr.reviewer import _agent_observed_check_statuses
from agr.schema import DerivedEvent, VerifierCheck


def _check(check_id, status):
    return VerifierCheck(
        check_id=check_id, run_id="r", source_capture_id="c", name=check_id,
        status=status, source="native_structured",
    )


def _event(eid, content, etype="tool_result", seq=None):
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        payload={"content": content},
    )


# --- outcome roll-up ----------------------------------------------------------


def test_unknown_skipped_error_checks_are_never_passed():
    """A sole check with no recorded verdict is UNDETERMINED — the old roll-up
    returned PASSED with passed=0/total=1."""
    for status in ("unknown", "skipped", "error"):
        o = outcome([_check("C1", status)])
        assert o["status"] == "UNDETERMINED", status
        assert o["passed"] == 0 and o["total"] == 1
        assert o["undetermined_checks"] == ["C1"]


def test_outcome_distinguishes_the_four_verdict_families():
    no_checks = outcome([])
    assert no_checks["status"] == "UNVERIFIED"

    clean = outcome([_check("C1", "passed"), _check("C2", "passed")])
    assert clean["status"] == "PASSED"

    failed = outcome([_check("C1", "passed"), _check("C2", "failed")])
    assert failed["status"] == "FAILED"
    assert failed["failed_checks"] == ["C2"]

    # One recorded pass plus one non-verdict: no failure, but not a clean pass.
    mixed = outcome([_check("C1", "passed"), _check("C2", "error")])
    assert mixed["status"] == "UNDETERMINED"


# --- agent-observed timing ------------------------------------------------------


def test_observed_pass_is_not_an_observed_failure():
    """A captured 'C1 PASSED' is a pass observation: a later verifier failure
    must not be phrased as something the agent saw failing."""
    events = [
        _event("evt_1", "running checks... C1 PASSED", seq=1),
        _event("evt_2", "final answer submitted", etype="final_submission", seq=2),
    ]
    statuses = _agent_observed_check_statuses(events, "final_submission")
    assert statuses.get("C1") == "PASSED"


def test_observed_failure_then_later_pass_resolves_in_order():
    """The final relevant observation wins: a check seen failing and later seen
    passing is not 'still failing at submission'."""
    events = [
        _event("evt_1", "C1 FAILED — expected 3 rows, got 0", seq=1),
        _event("evt_2", "rerun: C1 PASSED", seq=2),
        _event("evt_3", "final answer submitted", etype="final_submission", seq=3),
    ]
    statuses = _agent_observed_check_statuses(events, "final_submission")
    assert statuses.get("C1") == "PASSED"

    # And the reverse order leaves the failure as the last observation.
    events[1] = _event("evt_2", "rerun: C1 FAILED", seq=2)
    statuses = _agent_observed_check_statuses(events, "final_submission")
    assert statuses.get("C1") == "FAILED"


def test_bare_check_id_mention_observes_nothing():
    """A check id appearing in output is not evidence about its outcome — only
    an id paired with a status word is an observation."""
    events = [
        _event("evt_1", "collecting coverage for C1 and C2", seq=1),
        _event("evt_2", "final answer submitted", etype="final_submission", seq=2),
    ]
    statuses = _agent_observed_check_statuses(events, "final_submission")
    assert statuses == {}


def test_nothing_after_the_terminal_event_counts():
    """Verifier output computed after the run is never agent-visible."""
    events = [
        _event("evt_1", "final answer submitted", etype="final_submission", seq=1),
        _event("evt_2", "C1 FAILED", seq=2),  # post-run verifier record
    ]
    statuses = _agent_observed_check_statuses(events, "final_submission")
    assert statuses == {}
