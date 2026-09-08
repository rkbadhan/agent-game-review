"""Action identity for non-command tools (item 3, 2026-09-07).

Before this change, ``action_signature`` only widened its identity with the
call's structured ``tool_input`` for the four mutation tools (Edit,
MultiEdit, Write, NotebookEdit). Read, Grep, and Task hoisted only ONE field
of their input into ``content`` — Read's ``file_path`` alone (dropping
``offset``/``limit``), Grep's whichever of ``path``/``pattern`` the adapter's
key-priority list reached first (dropping the other), and Task had no
path/command at all. Two calls that differed only in the dropped field
looked like the identical action to recovery linkage and repetition
detection alike.
"""

from __future__ import annotations

from agr._util import action_signature
from agr.schema import DerivedEvent


def _call(tool: str, content: str, tool_input: dict) -> DerivedEvent:
    return DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_call", actor="main_agent",
        payload={"tool": tool, "content": content, "tool_input": tool_input},
    )


def test_read_same_path_different_range_is_a_different_action():
    a = _call("Read", "/app/big.py", {"file_path": "/app/big.py", "offset": 1, "limit": 50})
    b = _call("Read", "/app/big.py", {"file_path": "/app/big.py", "offset": 500, "limit": 50})
    assert action_signature(a) != action_signature(b)


def test_read_same_path_same_range_is_the_same_action():
    a = _call("Read", "/app/big.py", {"file_path": "/app/big.py", "offset": 1, "limit": 50})
    b = _call("Read", "/app/big.py", {"file_path": "/app/big.py", "offset": 1, "limit": 50})
    assert action_signature(a) == action_signature(b)


def test_grep_same_path_different_pattern_is_a_different_action():
    """Regression: the adapter's hoist priority puts ``path`` ahead of
    ``pattern`` in ``content``, so two Greps of the same path with different
    patterns previously shared one signature."""
    a = _call("Grep", "/app/src", {"pattern": "TODO", "path": "/app/src"})
    b = _call("Grep", "/app/src", {"pattern": "FIXME", "path": "/app/src"})
    assert action_signature(a) != action_signature(b)


def test_task_different_prompt_is_a_different_action():
    a = _call("Task", "", {"subagent_type": "Explore", "prompt": "find the auth module"})
    b = _call("Task", "", {"subagent_type": "Explore", "prompt": "find the logging module"})
    assert action_signature(a) != action_signature(b)


def test_edit_same_path_different_replacement_is_a_different_action():
    a = _call("Edit", "/app/calc.py", {"file_path": "/app/calc.py",
                                        "old_string": "a + b + 1", "new_string": "a + b"})
    b = _call("Edit", "/app/calc.py", {"file_path": "/app/calc.py",
                                        "old_string": "a - b", "new_string": "a + b"})
    assert action_signature(a) != action_signature(b)


def test_bash_identity_unaffected_by_non_command_widening():
    """Command tools are untouched: identity still reduces to (tool, content)."""
    a = _call("Bash", "pytest tests/", {"command": "pytest tests/"})
    b = _call("Bash", "pytest tests/", {"command": "pytest tests/"})
    assert action_signature(a) == action_signature(b)


# --- item 23 (2026-09-08): a permission denial is not a tool failure --------

def test_permission_denied_result_is_not_a_tool_failure():
    from agr._util import is_tool_failure
    from agr.schema import DerivedEvent
    ev = DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_result", actor="tool",
        payload={"tool": "Bash", "content": "permission denied", "status": "error",
                 "permission_denied": True},
    )
    assert is_tool_failure(ev) is False


def test_ordinary_error_without_permission_denied_flag_is_still_a_failure():
    from agr._util import is_tool_failure
    from agr.schema import DerivedEvent
    ev = DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_result", actor="tool",
        payload={"tool": "Bash", "content": "boom", "status": "error"},
    )
    assert is_tool_failure(ev) is True


# --- AGR-03: a submission control response is not a tool failure ------------

def test_submission_control_response_is_not_a_tool_failure():
    from agr._util import is_tool_failure
    from agr.schema import DerivedEvent
    ev = DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_result", actor="tool",
        payload={"tool": "bash", "content": "action was not executed", "status": "error",
                 "exit_code": -1, "submission_control_response": True},
    )
    assert is_tool_failure(ev) is False


# --- AGR-02/04 (review 82cc113): mutation relatedness ------------------------


def _mutation(path: str) -> DerivedEvent:
    return DerivedEvent(
        event_id="e1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_call", actor="main_agent",
        payload={"tool": "Edit", "content": path},
    )


def test_docs_edit_is_not_functionally_related():
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "pytest tests/", ())
    assert is_state_changing_action_related_to(_mutation("docs/notes.md"), failed) is False
    assert is_state_changing_action_related_to(_mutation("README.md"), failed) is False


def test_txt_fixture_edit_is_functionally_related_not_excluded_by_extension():
    """Review 82cc113: excluding every .txt/.md/.rst edit by extension alone
    wrongly excluded a test fixture and a dependency manifest — both are
    genuinely functional inputs a test run reads. Only conventionally
    repo-meta PATHS (docs/, README, ...) are excluded, never a bare
    extension."""
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "pytest tests/", ())
    assert is_state_changing_action_related_to(_mutation("tests/fixtures/input.txt"), failed) is True
    assert is_state_changing_action_related_to(_mutation("requirements.txt"), failed) is True


def test_source_edit_unrelated_by_path_still_credits_for_a_non_network_failure():
    """The primary legitimate case this module exists for: a fix in a
    differently-named source file, for an ordinary command (not a network
    probe) — must keep crediting unconditionally."""
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "pytest tests/", ())
    assert is_state_changing_action_related_to(_mutation("/app/calculator.py"), failed) is True


def test_source_edit_unrelated_to_a_network_probe_does_not_credit():
    """Review 82cc113: a source-code edit's extension does not establish a
    dependency on an unrelated network request — a failed curl/wget/ping
    holds a mutation to the SAME target-overlap test a shell mutation needs,
    unlike an ordinary build/test command."""
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "curl https://api.example.com/health", ())
    assert is_state_changing_action_related_to(_mutation("/app/calculator.py"), failed) is False


def test_source_edit_related_to_a_network_probes_own_target_still_credits():
    """The network-probe gate requires target overlap, it does not ban
    crediting mutations for network failures outright — a config file whose
    path contains the failed target's own host token still relates."""
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "curl api.example.com", ())
    assert is_state_changing_action_related_to(_mutation("/etc/hosts/api.example.com"), failed) is True


def test_network_probe_with_no_resolvable_target_still_credits_any_source_edit():
    from agr._util import is_state_changing_action_related_to
    failed = ("shell", "ping", ())
    assert is_state_changing_action_related_to(_mutation("/app/calculator.py"), failed) is True


def test_error_result_without_submission_flag_is_still_a_failure():
    from agr._util import is_tool_failure
    from agr.schema import DerivedEvent
    ev = DerivedEvent(
        event_id="evt_1", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_result", actor="tool",
        payload={"tool": "bash", "content": "action was not executed", "status": "error",
                 "exit_code": -1},
    )
    assert is_tool_failure(ev) is True
