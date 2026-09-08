"""Synthesized verifier from in-session test invocations (item 25, 2026-09-08)."""

from __future__ import annotations

from agr.verifier_synth import synthesize_verifier


def _doc(steps):
    return {
        "atif_version": "1.0", "source_type": "synthetic", "capture_completeness": "partial",
        "run": {"logical_run_id": "r1", "task_id": "t1"},
        "steps": steps,
    }


def _call_result(cid, command, output):
    return [
        {"step_id": f"{cid}c", "kind": "tool_call", "actor": "main_agent", "tool": "shell",
         "content": command, "tool_use_id": cid},
        {"step_id": f"{cid}r", "kind": "tool_result", "actor": "tool", "tool": "shell",
         "content": output, "tool_use_id": cid},
    ]


def test_pytest_all_passed():
    steps = _call_result("c1", "pytest tests/", "===== 3 passed in 0.12s =====")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier is not None
    assert len(verifier["checks"]) == 1
    check = verifier["checks"][0]
    assert check["status"] == "passed"
    assert check["source"] == "output_interpretation"
    assert check["timing"] == "during_run"


def test_pytest_some_failed():
    steps = _call_result("c1", "python -m pytest tests/", "1 failed, 2 passed in 0.30s")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_npm_test_jest_style():
    steps = _call_result("c1", "npm test", "Tests:       1 failed, 4 passed, 5 total")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_npm_test_mocha_style_all_passing():
    steps = _call_result("c1", "npm run test", "  12 passing (45ms)")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "passed"


def test_cargo_test_passed():
    steps = _call_result("c1", "cargo test", "test result: ok. 7 passed; 0 failed; 0 ignored")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "passed"


def test_cargo_test_failed():
    steps = _call_result("c1", "cargo test", "test result: FAILED. 5 passed; 2 failed; 0 ignored")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_go_test_verbose_output():
    output = "--- PASS: TestAdd (0.00s)\n--- FAIL: TestSub (0.00s)\nFAIL\nexit status 1"
    steps = _call_result("c1", "go test ./...", output)
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_go_test_quiet_output_falls_back_to_summary_line():
    steps = _call_result("c1", "go test ./...", "ok  \tmypackage\t0.005s")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "passed"


# --- AGR-02: mixed/multi-package Go and Cargo output -------------------------


def test_go_test_multi_package_one_failing_is_not_masked_by_a_passing_sibling():
    """go test ./... against several packages prints one summary line PER
    PACKAGE. Before AGR-02, checking only "does an ok line exist anywhere"
    (re.search, first-match) reported an all-pass result even with a FAIL
    line for a different package sitting right below it."""
    output = "ok  \tpkg/a\t0.004s\nFAIL\tpkg/b\t0.010s\nok  \tpkg/c\t0.002s\n"
    steps = _call_result("c1", "go test ./...", output)
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_go_test_build_failed_package_counts_as_failing():
    output = "ok  \tpkg/a\t0.004s\nFAIL\tpkg/b [build failed]\n"
    steps = _call_result("c1", "go test ./...", output)
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_go_test_all_packages_passing_is_still_passed():
    output = "ok  \tpkg/a\t0.004s\nok  \tpkg/b\t0.002s\n"
    steps = _call_result("c1", "go test ./...", output)
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "passed"


def test_cargo_workspace_second_crate_failure_is_not_dropped():
    """cargo test --workspace prints one "test result: ..." line PER CRATE.
    Before AGR-02, re.search matched only the first — a failing second crate
    right below a passing first crate's summary was silently ignored."""
    output = ("test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured\n\n"
              "test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured\n")
    steps = _call_result("c1", "cargo test --workspace", output)
    verifier = synthesize_verifier(_doc(steps))
    check = verifier["checks"][0]
    assert check["status"] == "failed"


def test_cargo_compile_error_produces_an_explicit_error_check_not_silence():
    """A pure compile failure never prints a "test result: ..." line at all
    — before AGR-02 this produced NO check, silently losing the observation
    that the agent DID run cargo test and it DID fail."""
    output = "error[E0433]: failed to resolve: use of undeclared crate\nerror: could not compile `demo` (bin \"demo\") due to previous error"
    steps = _call_result("c1", "cargo test", output)
    verifier = synthesize_verifier(_doc(steps))
    assert verifier is not None
    check = verifier["checks"][0]
    assert check["status"] == "error"


def test_synthesized_checks_carry_scope_and_sequence():
    steps = _call_result("c1", "pytest tests/test_a.py", "1 passed")
    verifier = synthesize_verifier(_doc(steps))
    check = verifier["checks"][0]
    assert check["scope"] == "pytest tests/test_a.py"
    assert check["sequence"] == 0


def test_rspec():
    steps = _call_result("c1", "bundle exec rspec", "10 examples, 2 failures")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier["checks"][0]["status"] == "failed"


def test_multiple_invocations_each_get_their_own_check():
    steps = (_call_result("c1", "pytest tests/", "3 passed")
             + _call_result("c2", "npm test", "5 passing"))
    verifier = synthesize_verifier(_doc(steps))
    assert len(verifier["checks"]) == 2
    assert verifier["checks"][0]["check_id"] != verifier["checks"][1]["check_id"]


def test_unrecognised_command_produces_no_check():
    steps = _call_result("c1", "echo hello", "hello")
    assert synthesize_verifier(_doc(steps)) is None


def test_recognised_command_with_unparseable_output_produces_no_check():
    """A pytest invocation whose output doesn't match any known pattern
    (e.g. truncated, or a custom reporter) is not guessed at — no check,
    not a fabricated 'unknown' one."""
    steps = _call_result("c1", "pytest tests/", "some custom reporter output with no numbers")
    assert synthesize_verifier(_doc(steps)) is None


def test_command_with_no_paired_result_produces_no_check():
    steps = [
        {"step_id": "c1", "kind": "tool_call", "actor": "main_agent", "tool": "shell",
         "content": "pytest tests/", "tool_use_id": "c1"},
    ]
    assert synthesize_verifier(_doc(steps)) is None


def test_flags_and_targets_do_not_prevent_a_match():
    steps = _call_result("c1", "pytest -k test_add tests/test_calc.py -v", "1 passed in 0.01s")
    verifier = synthesize_verifier(_doc(steps))
    assert verifier is not None
    assert verifier["checks"][0]["status"] == "passed"


def test_no_steps_at_all_returns_none():
    assert synthesize_verifier(_doc([])) is None


def test_cli_synthesize_verifier_subcommand(tmp_path):
    import json as _json
    from agr.cli import main

    session = tmp_path / "session.jsonl"
    lines = [
        {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "Fix it."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest tests/"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed in 0.10s"}]}},
    ]
    session.write_text("\n".join(_json.dumps(l) for l in lines), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    rc = main(["synthesize-verifier", "--adapter", "claude", str(session), "--output", str(output)])
    assert rc == 0
    sidecar = _json.loads(output.read_text())
    assert sidecar["checks"][0]["status"] == "passed"


def test_cli_synthesize_verifier_no_match_exits_nonzero(tmp_path):
    import json as _json
    from agr.cli import main

    session = tmp_path / "session.jsonl"
    lines = [
        {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "Look around."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}]}},
    ]
    session.write_text("\n".join(_json.dumps(l) for l in lines), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    rc = main(["synthesize-verifier", "--adapter", "claude", str(session), "--output", str(output)])
    assert rc == 1
    assert not output.exists()


def test_sidecar_shape_matches_load_verifier_contract():
    """The output must be exactly what agr.ingest_pi.load_verifier /
    ingest accepts: {"raw_output": str, "checks": [...]}."""
    steps = _call_result("c1", "pytest tests/", "3 passed")
    verifier = synthesize_verifier(_doc(steps))
    assert isinstance(verifier["raw_output"], str)
    assert isinstance(verifier["checks"], list)
    check = verifier["checks"][0]
    assert set(check) >= {"check_id", "name", "status", "source", "timing", "source_pointers"}
