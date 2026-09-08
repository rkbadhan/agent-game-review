"""Check reconciliation and outcome rollup (AGR-02).

Synthesized in-session checks can retain an obsolete failure (a later
same-scope pass should reconcile it), accept a stale pass (a relevant
mutation after a passing check should invalidate it as evidence of final
state), or let a narrower check silently stand in for a broader one (must
never happen — scope matches exactly, so they never touch). These tests pin
``agr.checks.reconcile_checks`` and the resulting ``outcome()`` rollup
directly, independent of any particular adapter.
"""

from __future__ import annotations

from agr.checks import STALE_REASON_RELEVANT_MUTATION, outcome, reconcile_checks
from agr.schema import DerivedEvent, VerifierCheck


def _check(check_id, status, *, scope=None, sequence=None, timing="during_run",
           source_pointers=()):
    return VerifierCheck(
        check_id=check_id, run_id="r", source_capture_id="c", name=check_id,
        status=status, source="output_interpretation", timing=timing,
        scope=scope, sequence=sequence, source_pointers=list(source_pointers),
    )


def _event(event_id, sequence, event_type, payload, actor="main_agent"):
    return DerivedEvent(
        event_id=event_id, run_id="r", source_capture_id="c", sequence=sequence,
        source_step_ids=[event_id], event_type=event_type, actor=actor, payload=payload,
    )


# --- same-scope reconciliation ------------------------------------------------


def test_same_scope_later_pass_supersedes_earlier_failure():
    earlier = _check("k1", "failed", scope="pytest tests/", sequence=0)
    later = _check("k2", "passed", scope="pytest tests/", sequence=5)
    reconcile_checks([earlier, later], [])
    assert earlier.superseded_by == "k2"
    assert earlier.effective_status is None
    assert later.superseded_by is None
    assert later.effective_status == "passed"


def test_same_scope_later_failure_supersedes_earlier_pass():
    """A stale, accepted pass followed by a genuine later failure of the
    identical command must not keep counting as a pass."""
    earlier = _check("k1", "passed", scope="cargo test", sequence=0)
    later = _check("k2", "failed", scope="cargo test", sequence=3)
    reconcile_checks([earlier, later], [])
    assert earlier.superseded_by == "k2"
    result = outcome([earlier, later])
    assert result["status"] == "FAILED"
    assert result["total"] == 1


def test_outcome_reflects_only_the_reconciled_current_check():
    earlier = _check("k1", "failed", scope="pytest tests/", sequence=0)
    later = _check("k2", "passed", scope="pytest tests/", sequence=5)
    reconcile_checks([earlier, later], [])
    result = outcome([earlier, later])
    assert result["status"] == "PASSED"
    assert result["passed"] == 1
    assert result["total"] == 1
    assert result["failed_checks"] == []
    assert result["total_observations"] == 2
    assert result["superseded_checks"] == ["k1"]


def test_three_observations_of_the_same_scope_chain_in_order():
    a = _check("k1", "failed", scope="go test ./...", sequence=0)
    b = _check("k2", "failed", scope="go test ./...", sequence=1)
    c = _check("k3", "passed", scope="go test ./...", sequence=2)
    reconcile_checks([a, b, c], [])
    assert a.superseded_by == "k2"
    assert b.superseded_by == "k3"
    assert c.superseded_by is None
    result = outcome([a, b, c])
    assert result["status"] == "PASSED"
    assert result["total"] == 1


def test_different_scope_never_supersedes_each_other():
    """A narrower passing check must never reconcile a broader failure — they
    have different scope strings and reconciliation matches exactly."""
    broad = _check("k1", "failed", scope="pytest tests/", sequence=0)
    narrow = _check("k2", "passed", scope="pytest tests/test_a.py", sequence=1)
    reconcile_checks([broad, narrow], [])
    assert broad.superseded_by is None
    assert narrow.superseded_by is None
    result = outcome([broad, narrow])
    assert result["status"] == "FAILED"
    assert result["total"] == 2


def test_unscoped_checks_are_never_reconciled():
    """Native/structured external-verifier checks (no scope) are each already
    one atomic, authoritative check — reconciliation must leave them alone,
    even when two of them share a status."""
    a = _check("C1", "failed", scope=None, timing=None)
    b = _check("C2", "failed", scope=None, timing=None)
    reconcile_checks([a, b], [])
    assert a.superseded_by is None
    assert b.superseded_by is None


# --- mutation staleness --------------------------------------------------------


def test_passed_check_goes_stale_after_a_relevant_mutation_with_no_reverification():
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/test_a.py"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    edit = _event("e3", 3, "tool_call", {
        "tool": "Edit", "content": "/app/a.py",
        "tool_input": {"file_path": "/app/a.py", "old_string": "x", "new_string": "y"},
    })
    check = _check("k1", "passed", scope="pytest tests/test_a.py", sequence=0,
                   source_pointers=["e1", "e2"])
    reconcile_checks([check], [call, result, edit])
    assert check.stale_reason == STALE_REASON_RELEVANT_MUTATION
    assert check.effective_status == "unknown"
    out = outcome([check])
    assert out["status"] == "UNDETERMINED"
    assert out["passed"] == 0
    assert out["stale_checks"] == ["k1"]
    # the historical fact is untouched — this WAS observed to pass.
    assert check.status == "passed"


def test_passed_check_with_no_subsequent_events_stays_current():
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    check = _check("k1", "passed", scope="pytest tests/", sequence=0, source_pointers=["e1", "e2"])
    reconcile_checks([check], [call, result])
    assert check.stale_reason is None
    assert check.effective_status == "passed"
    assert outcome([check])["status"] == "PASSED"


def test_passed_check_with_only_unrelated_activity_after_stays_current():
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/test_a.py"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    unrelated = _event("e3", 3, "tool_call", {"tool": "Bash", "content": "git status"})
    check = _check("k1", "passed", scope="pytest tests/test_a.py", sequence=0,
                   source_pointers=["e1", "e2"])
    reconcile_checks([check], [call, result, unrelated])
    assert check.stale_reason is None
    assert check.effective_status == "passed"


def test_passed_check_after_a_documentation_edit_stays_current():
    """AGR-04 (PR #56 review): a documentation edit cannot be the relevant
    mutation that invalidates a check as evidence of final state — editing
    docs/README/.md text cannot change what a test or command does. Unlike a
    source-file edit (see the "relevant mutation" test above, which
    correctly still marks a check stale even via a differently-named .py
    file), this must NOT trigger staleness."""
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/test_a.py"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    doc_edit = _event("e3", 3, "tool_call", {
        "tool": "Edit", "content": "docs/notes.md",
        "tool_input": {"file_path": "docs/notes.md", "old_string": "x", "new_string": "y"},
    })
    check = _check("k1", "passed", scope="pytest tests/test_a.py", sequence=0,
                   source_pointers=["e1", "e2"])
    reconcile_checks([check], [call, result, doc_edit])
    assert check.stale_reason is None
    assert check.effective_status == "passed"


def test_re_verified_scope_is_superseded_not_marked_stale():
    """A relevant mutation followed by a genuine re-run of the SAME command
    reconciles through superseding, not staleness — the two must not stack."""
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/test_a.py"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    edit = _event("e3", 3, "tool_call", {
        "tool": "Edit", "content": "/app/a.py",
        "tool_input": {"file_path": "/app/a.py", "old_string": "x", "new_string": "y"},
    })
    earlier = _check("k1", "passed", scope="pytest tests/test_a.py", sequence=0,
                     source_pointers=["e1", "e2"])
    later = _check("k2", "passed", scope="pytest tests/test_a.py", sequence=5)
    reconcile_checks([earlier, later], [call, result, edit])
    assert earlier.superseded_by == "k2"
    assert earlier.stale_reason is None  # excluded via superseding, not double-flagged
    assert later.effective_status == "passed"
    assert outcome([earlier, later])["status"] == "PASSED"


def test_post_run_pass_is_never_marked_stale():
    """Staleness only applies to during_run observations resolved against the
    SAME trajectory's later events — a post-run (final) verifier check has no
    "later in this trace" to invalidate it against."""
    call = _event("e1", 1, "tool_call", {"tool": "Bash", "content": "pytest tests/"})
    result = _event("e2", 2, "tool_result", {"tool": "Bash", "content": "1 passed", "status": "ok"},
                    actor="tool")
    edit = _event("e3", 3, "tool_call", {
        "tool": "Edit", "content": "/app/a.py",
        "tool_input": {"file_path": "/app/a.py", "old_string": "x", "new_string": "y"},
    })
    check = _check("C1", "passed", scope=None, timing="post_run", source_pointers=["e1", "e2"])
    reconcile_checks([check], [call, result, edit])
    assert check.stale_reason is None


# --- outcome edge cases --------------------------------------------------------


def test_no_checks_is_unverified():
    assert outcome([])["status"] == "UNVERIFIED"


def test_error_status_check_counts_as_undetermined_not_failed():
    check = _check("k1", "error", scope="cargo test", sequence=0)
    reconcile_checks([check], [])
    result = outcome([check])
    assert result["status"] == "UNDETERMINED"
    assert result["undetermined_checks"] == ["k1"]
    assert result["failed_checks"] == []


# --- AGR-02 (review 82cc113): in-session-only PASSED needs declared coverage -

def _contract(items):
    from agr.schema import ContractItem, TaskContract
    return TaskContract(task_id="t", contract_version=1, status="draft", items=[
        ContractItem(id=iid, description="", source_type=st, importance=imp,
                     mapped_checks=list(mapped))
        for iid, st, imp, mapped in items
    ])


def test_in_session_pass_with_uncovered_required_item_is_undetermined():
    """One passing smoke test the agent happened to run must not become a
    task-level PASSED when the author's declared instruction has a required
    item no check ever mapped to."""
    check = _check("k1", "passed", scope="pytest tests/test_smoke.py", sequence=0)
    contract = _contract([
        ("R1", "stated_requirement", "required", ["k1"]),
        ("R2", "stated_requirement", "required", []),  # never covered by any check
    ])
    result = outcome([check], contract)
    assert result["status"] == "UNDETERMINED"
    assert result["coverage_gaps"] == ["R2"]


def test_in_session_pass_with_full_declared_coverage_still_passes():
    check = _check("k1", "passed", scope="pytest tests/", sequence=0)
    contract = _contract([("R1", "stated_requirement", "required", ["k1"])])
    result = outcome([check], contract)
    assert result["status"] == "PASSED"
    assert result["coverage_gaps"] == []


def test_native_structured_pass_is_unaffected_by_coverage_gaps():
    """A real external verifier's PASSED already carries author authority —
    the coverage requirement is specific to in-session-synthesized evidence."""
    check = VerifierCheck(check_id="k1", run_id="r", source_capture_id="c", name="k1",
                          status="passed", source="native_structured")
    contract = _contract([
        ("R1", "stated_requirement", "required", ["k1"]),
        ("R2", "stated_requirement", "required", []),
    ])
    result = outcome([check], contract)
    assert result["status"] == "PASSED"


def test_uncovered_optional_item_does_not_block_in_session_pass():
    check = _check("k1", "passed", scope="pytest tests/", sequence=0)
    contract = _contract([
        ("R1", "stated_requirement", "required", ["k1"]),
        ("R2", "stated_requirement", "optional", []),
    ])
    result = outcome([check], contract)
    assert result["status"] == "PASSED"


def test_uncovered_verifier_enforced_item_does_not_block_in_session_pass():
    """A verifier_enforced item is synthesized FROM a check — it is covered by
    construction and must never itself count as a coverage gap."""
    check = _check("k1", "passed", scope="pytest tests/", sequence=0)
    contract = _contract([("V-k1", "verifier_enforced", "required", [])])
    result = outcome([check], contract)
    assert result["status"] == "PASSED"


def test_omitting_contract_preserves_original_behaviour():
    check = _check("k1", "passed", scope="pytest tests/", sequence=0)
    assert outcome([check])["status"] == "PASSED"
