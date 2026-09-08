"""Sibling divergence view (item 21, 2026-09-08).

Two runs of the SAME task share an identical prefix of tool_calls, then
diverge on one action; the failing run then fails its check, the sibling
passes. Checks the report finds the sibling, aligns by (phase_kind, tool,
content), and reports the true first point of divergence — not just "the
outcome differs".
"""

from __future__ import annotations

from agr import divergence
from agr.pipeline import analyze
from agr.store import Store


def _doc(run_id, task_id, steps, *, passed, sweep_id=None, configuration_id=None):
    run = {
        "logical_run_id": run_id,
        "task_id": task_id,
        "model": "m",
        "agent": "a",
        "harness_version": "h",
    }
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id
    return {
        "atif_version": "1.0",
        "source_type": "synthetic",
        "capture_completeness": "complete",
        "run": run,
        "capabilities": {"messages": "complete", "tool_calls": "complete", "tool_results": "complete"},
        "task": {"instruction": "fix the bug", "artifacts": [], "requirements": []},
        "steps": steps,
        "verifier": {
            "raw_output": "",
            "checks": [{"check_id": "c1", "name": "tests pass",
                       "status": "passed" if passed else "failed", "source": "native_structured"}],
        },
    }


def _shared_prefix():
    return [
        {"step_id": "s1", "kind": "task_received", "actor": "harness", "content": "fix the bug"},
        {"step_id": "s2", "kind": "tool_call", "actor": "main_agent", "tool": "shell", "content": "ls"},
        {"step_id": "s3", "kind": "tool_result", "actor": "tool", "tool": "shell", "content": "file.py", "exit_code": 0},
        {"step_id": "s4", "kind": "tool_call", "actor": "main_agent", "tool": "shell", "content": "cat file.py"},
        {"step_id": "s5", "kind": "tool_result", "actor": "tool", "tool": "shell", "content": "def add(a,b): return a+b+1", "exit_code": 0},
    ]


def _failing_run_doc(run_id="fail-run", sweep_id=None, configuration_id=None):
    steps = _shared_prefix() + [
        {"step_id": "s6", "kind": "tool_call", "actor": "main_agent", "tool": "shell", "content": "python solve.py"},
        {"step_id": "s7", "kind": "tool_result", "actor": "tool", "tool": "shell", "content": "AssertionError", "exit_code": 1},
        {"step_id": "s8", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    return _doc(run_id, "fix-bug", steps, passed=False, sweep_id=sweep_id, configuration_id=configuration_id)


def _passing_run_doc(run_id="pass-run", sweep_id=None, configuration_id=None):
    steps = _shared_prefix() + [
        {"step_id": "s6", "kind": "tool_call", "actor": "main_agent", "tool": "shell", "content": "python fix.py"},
        {"step_id": "s7", "kind": "tool_result", "actor": "tool", "tool": "shell", "content": "3 passed", "exit_code": 0},
        {"step_id": "s8", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    return _doc(run_id, "fix-bug", steps, passed=True, sweep_id=sweep_id, configuration_id=configuration_id)


def _unrelated_task_doc(run_id="other-task"):
    steps = [
        {"step_id": "s1", "kind": "task_received", "actor": "harness", "content": "different task"},
        {"step_id": "s2", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    return _doc(run_id, "unrelated-task", steps, passed=True)


def test_finds_passing_sibling_on_the_same_task(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(), store)
    analyze(_unrelated_task_doc(), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib == "pass-run"


def test_no_sibling_returns_none(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    assert divergence.find_passing_sibling(store, "fail-run") is None


def test_unknown_run_returns_none(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    assert divergence.find_passing_sibling(store, "does-not-exist") is None


def test_prefers_same_sweep_sibling(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(sweep_id="sweep-a"), store)
    analyze(_passing_run_doc(run_id="pass-other-sweep", sweep_id="sweep-b"), store)
    analyze(_passing_run_doc(run_id="pass-same-sweep", sweep_id="sweep-a"), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib == "pass-same-sweep"


def test_prefers_same_configuration_sibling(tmp_path):
    """AGR-12: configuration_id is filtered FIRST — a passing run under a
    different configuration (a different prompt, tool set, harness version)
    is not a valid divergence baseline even though it shares the task_id."""
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(configuration_id="cfg-a"), store)
    analyze(_passing_run_doc(run_id="pass-other-cfg", configuration_id="cfg-b"), store)
    analyze(_passing_run_doc(run_id="pass-same-cfg", configuration_id="cfg-a"), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib == "pass-same-cfg"


def test_configuration_match_takes_priority_over_sweep_match(tmp_path):
    """A sibling matching configuration_id is preferred over one that only
    matches sweep_id — configuration is the harder requirement (AGR-12)."""
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(sweep_id="sweep-a", configuration_id="cfg-a"), store)
    analyze(_passing_run_doc(run_id="pass-same-sweep-diff-cfg",
                             sweep_id="sweep-a", configuration_id="cfg-b"), store)
    analyze(_passing_run_doc(run_id="pass-same-cfg-diff-sweep",
                             sweep_id="sweep-z", configuration_id="cfg-a"), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib == "pass-same-cfg-diff-sweep"


def test_falls_back_to_any_passing_sibling_when_no_configuration_declared(tmp_path):
    """A target run with no declared configuration_id falls back to the
    whole task-matched pool exactly as before (configuration filtering never
    demands a field the source never declared)."""
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(configuration_id="cfg-b"), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib == "pass-run"


def test_no_sibling_when_only_a_different_configuration_passed(tmp_path):
    """AGR-12 (PR #58 review): when the target declares a configuration_id
    and NO passing run shares it, there is no comparable sibling — the
    incompatible cross-configuration run must NOT be returned as a
    fallback. This is the actual acceptance criterion (rejecting the
    mismatched pairing), not just preferring a matched one when both exist."""
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(configuration_id="cfg-a"), store)
    analyze(_passing_run_doc(configuration_id="cfg-b"), store)
    sib = divergence.find_passing_sibling(store, "fail-run")
    assert sib is None


def test_divergence_report_finds_the_true_first_point_of_divergence(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(), store)
    report = divergence.divergence_report(store, "fail-run")
    assert report is not None
    assert report["failed_run_id"] == "fail-run"
    assert report["passing_run_id"] == "pass-run"
    assert report["task_id"] == "fix-bug"

    fd = report["first_divergence"]
    # "ls" and "cat file.py" matched (2 tool_calls) before the divergence.
    assert fd["matched_prefix_length"] == 2
    assert fd["failed_action"]["content"] == "python solve.py"
    assert fd["passing_action"]["content"] == "python fix.py"


def test_aligned_sequence_marks_the_shared_prefix_as_equal(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(), store)
    report = divergence.divergence_report(store, "fail-run")
    aligned = report["aligned"]
    equal_tags = [a["tag"] for a in aligned if a["tag"] == "equal"]
    assert len(equal_tags) == 2
    assert aligned[0]["failed"]["content"] == "ls"
    assert aligned[0]["passing"]["content"] == "ls"
    assert aligned[1]["failed"]["content"] == "cat file.py"
    # After the shared prefix, a non-equal entry carries the divergent pair.
    non_equal = next(a for a in aligned if a["tag"] != "equal")
    assert non_equal["failed"]["content"] == "python solve.py"
    assert non_equal["passing"]["content"] == "python fix.py"


def test_explicit_sibling_override(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(run_id="pass-run-2"), store)
    report = divergence.divergence_report(store, "fail-run", sibling_run_id="pass-run-2")
    assert report["passing_run_id"] == "pass-run-2"


def test_no_sibling_available_returns_none(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    assert divergence.divergence_report(store, "fail-run") is None


def test_unknown_run_report_returns_none(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_failing_run_doc(), store)
    analyze(_passing_run_doc(), store)
    assert divergence.divergence_report(store, "does-not-exist") is None


def test_different_edits_to_the_same_path_are_detected_as_divergent(tmp_path):
    """AGR-13: a failing run and a passing run both Edit the SAME file
    (same phase/tool/path), but with DIFFERENT replacements. Comparing on
    phase/tool/path alone would call this an exact match; the structured
    input_identity (old_string/new_string) must be part of the comparison
    key so the report finds the real point of divergence."""
    store = Store(str(tmp_path / "store"))
    shared = [
        {"step_id": "s1", "kind": "task_received", "actor": "harness", "content": "fix the bug"},
    ]
    fail_steps = shared + [
        {"step_id": "s2", "kind": "tool_call", "actor": "main_agent", "tool": "Edit",
         "path": "app.py", "content": "app.py",
         "tool_input": {"file_path": "app.py", "old_string": "return False", "new_string": "return None"}},
        {"step_id": "s3", "kind": "tool_result", "actor": "tool", "tool": "Edit", "content": "ok", "exit_code": 0},
        {"step_id": "s4", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    pass_steps = shared + [
        {"step_id": "s2", "kind": "tool_call", "actor": "main_agent", "tool": "Edit",
         "path": "app.py", "content": "app.py",
         "tool_input": {"file_path": "app.py", "old_string": "return False", "new_string": "return True"}},
        {"step_id": "s3", "kind": "tool_result", "actor": "tool", "tool": "Edit", "content": "ok", "exit_code": 0},
        {"step_id": "s4", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    analyze(_doc("fail-edit", "fix-bug", fail_steps, passed=False), store)
    analyze(_doc("pass-edit", "fix-bug", pass_steps, passed=True), store)
    report = divergence.divergence_report(store, "fail-edit")
    assert report is not None
    fd = report["first_divergence"]
    assert fd is not None, "different edits to the same path must be reported as a divergence"
    assert fd["matched_prefix_length"] == 0
    assert fd["failed_action"]["input_identity"] == "old_string=return False|new_string=return None"
    assert fd["passing_action"]["input_identity"] == "old_string=return False|new_string=return True"


def test_identical_timelines_have_no_divergence(tmp_path):
    """Two runs whose tool_call timelines are IDENTICAL (only the check
    outcome differs, e.g. a flaky verifier) report no divergence point."""
    store = Store(str(tmp_path / "store"))
    steps = _shared_prefix() + [
        {"step_id": "s6", "kind": "final_submission", "actor": "main_agent", "content": "done"},
    ]
    analyze(_doc("fail-identical", "fix-bug", steps, passed=False), store)
    analyze(_doc("pass-identical", "fix-bug", steps, passed=True), store)
    report = divergence.divergence_report(store, "fail-identical")
    assert report["first_divergence"] is None
    assert all(a["tag"] == "equal" for a in report["aligned"])
