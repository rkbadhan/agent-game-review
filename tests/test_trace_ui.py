"""Trace navigation keeps source identity while condensing tool exchanges."""
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from agr.pipeline import analyze
from agr.store import Store
from test_ui import FIXTURES, _launch, _page, _serving


@pytest.fixture(scope="module")
def trace_server(tmp_path_factory):
    doc = json.loads((Path(FIXTURES) / "chess_best_move.atif.json").read_text(encoding="utf-8"))
    doc["run"]["logical_run_id"] = "trace_readability"
    doc["task"] = {"instruction": "Inspect both commands", "requirements": [], "artifacts": []}
    doc["verifier"] = {"checks": []}
    doc["steps"] = [
        {"step_id": "task", "kind": "task_received", "actor": "harness", "content": "Inspect both commands"},
        {"step_id": "call-a", "kind": "tool_call", "actor": "main_agent", "tool": "Bash",
         "tool_use_id": "parallel-a", "content": json.dumps({"keystrokes": "printf alpha\n", "duration": 1})},
        {"step_id": "echo", "kind": "model_output", "actor": "main_agent", "content": "Executed Bash call_echoOnly"},
        {"step_id": "call-b", "kind": "tool_call", "actor": "main_agent", "tool": "Bash",
         "tool_use_id": "parallel-b", "content": "printf beta"},
        {"step_id": "result-b", "kind": "tool_result", "actor": "tool", "tool": "Bash", "exit_code": 0,
         "tool_use_id": "parallel-b", "content": json.dumps({"returncode": 0,
             "output": "beta response\n" + "x" * 650 + "\nNeedle beyond preview"})},
        {"step_id": "result-a", "kind": "tool_result", "actor": "tool", "tool": "Bash", "exit_code": 7,
         "tool_use_id": "parallel-a", "content": "alpha failed\n<img src=x onerror=alert(1)>"},
        {"step_id": "orphan", "kind": "tool_result", "actor": "tool", "tool": "Bash",
         "tool_use_id": "missing-call", "content": "orphan output"},
        {"step_id": "message", "kind": "model_output", "actor": "main_agent",
         "content": "Executed Bash call_echoOnly but inspection still needed"},
        {"step_id": "end", "kind": "run_completed", "actor": "harness", "content": "Finished"},
    ]
    root = str(tmp_path_factory.mktemp("trace-readability"))
    store = Store(root)
    analyze(doc, store)
    # A second run exercises the no-moment fallback independently of the failure.
    doc["run"]["logical_run_id"] = "trace_without_moments"
    doc["steps"][5]["exit_code"] = 0
    analyze(doc, store)
    with _serving(root) as base:
        yield base


def test_search_pairs_full_output_and_echoes(trace_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = _page(browser)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(trace_server + "/?run=trace_readability&chapter=moments")
            page.locator(".run-header").wait_for()
            page.locator(".trace-chip").click()
            expect(page.locator("#trace-body .step")).to_have_count(6)
            # The failure moment points at the result, represented by its paired row.
            expect(page.locator(".step.active")).to_have_attribute("data-step-id", "call-a")
            assert "evidence=result-a" in page.url
            a = page.locator('.step[data-step-id="call-a"]')
            expect(a).to_have_attribute("data-paired-step-id", "result-a")
            expect(a.locator(".trace-preview")).to_have_text("printf alpha")
            expect(a.locator(".trace-result")).to_have_text("Exit 7 · alpha failed")
            expect(page.locator('.step[data-step-id="call-b"] .trace-result')).to_have_text("Exit 0 · Needle beyond preview")
            expect(page.locator('.step[data-step-id="message"]')).to_be_visible()
            expect(page.locator("#trace-body img")).to_have_count(0)
            # Search includes the paired result's full text, beyond its excerpt.
            search = page.get_by_role("searchbox", name="Search trace")
            search.fill("needle beyond preview")
            expect(page.locator("#trace-body .step")).to_have_count(1)
            expect(page.locator("#trace-body .step")).to_have_attribute("data-step-id", "call-b")
            page.locator("#trace-body .step").click()
            page.locator(".pane-active").get_by_role("button", name="Show full text").click()
            expect(page.locator(".pane-active")).to_contain_text("Needle beyond preview")
            search.fill("nothing matches")
            expect(page.locator(".trace-rows .empty")).to_contain_text("No trace entries match")
            search.press("Escape")
            expect(search).to_have_value("")
            expect(page.locator("#trace-drawer")).to_have_class("drawer open")
            expect(page.locator("#trace-body .step")).to_have_count(6)
            page.get_by_role("checkbox", name="Show execution echoes").check()
            expect(page.locator("#trace-body .step")).to_have_count(7)
            page.get_by_role("checkbox", name="Show execution echoes").uncheck()
            search.fill("call_echoOnly")
            # Searching can recover hidden echoes without changing the toggle.
            expect(page.locator('.step[data-step-id="echo"]')).to_be_visible()
            search.fill("result-a")
            expect(page.locator("#trace-body .step")).to_have_count(1)
            expect(page.locator("#trace-body .step")).to_have_attribute("data-step-id", "call-a")
            assert not errors
        finally:
            browser.close()


def test_result_and_hidden_echo_links_keep_source_identity(trace_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = _page(browser)
            page.goto(trace_server + "/?run=trace_readability&trace=1&evidence=result-a")
            page.locator("#trace-drawer.open").wait_for()
            expect(page.locator(".step.active")).to_have_attribute("data-step-id", "call-a")
            expect(page.locator(".step.active")).to_have_attribute("aria-current", "step")
            expect(page.locator(".pane-active")).to_contain_text("direction: result")
            expect(page.locator(".pane-active")).to_contain_text("Matching request")
            assert "evidence=result-a" in page.url
            page.reload()
            page.locator("#trace-drawer.open").wait_for()
            expect(page.locator(".step.active")).to_have_attribute("data-paired-step-id", "result-a")
            assert "evidence=result-a" in page.url
            page.goto(trace_server + "/?run=trace_readability&trace=1&evidence=echo")
            page.locator("#trace-drawer.open").wait_for()
            expect(page.locator('.step.active')).to_have_attribute("data-step-id", "echo")
            expect(page.locator(".pane-active")).to_contain_text("Executed Bash call_echoOnly")
            page.goto(trace_server + "/?run=trace_readability&trace=1&evidence=missing")
            page.locator("#trace-drawer.open").wait_for()
            expect(page.locator(".step.active")).to_have_attribute("data-step-id", "call-a")
            expect(page.locator("#toast")).to_contain_text("not in this capture")
            page.goto(trace_server + "/?run=trace_without_moments")
            page.locator(".run-header").wait_for()
            page.locator(".trace-chip").click()
            expect(page.locator(".step.active")).to_have_attribute("data-step-id", "orphan")
        finally:
            browser.close()

def test_result_preview_ignores_terminal_echoes_and_notices(trace_server):
    cases = [
        ("New Terminal Output:\nroot@host:/work# cat <<'EOF'\n> echoed body\n> EOF\n55\nroot@host:/work#", "55"),
        ("(.venv) runner@ci:/work$ printf ready\nready\n(.venv) runner@ci:/work$", "ready"),
        ("[runner@ci workspace]$ run-build\nBuild completed successfully\n[runner@ci workspace]$", "Build completed successfully"),
        ("PS C:\\work> run-build\nartifact ready\nPS C:\\work>", "artifact ready"),
        ("C:\\work> run-build\nartifact ready\nC:\\work>", "artifact ready"),
        ("New Terminal Output:\n$ cat <<EOF\n> command body\n> EOF\nready\n$", "ready"),
        ("heading without a newlinerunner@ci:/work# cat <<EOF\n> echoed command\n> EOF\nrunner@ci:/work#", "heading without a newline"),
        ("runner@ci:/work$ cat result.txt\n42runner@ci:/work$", "42"),
        ("runner@ci:/work$ cat result.txt\n(42)runner@ci:/work$", "(42)"),
        ("runner@ci:/work$ cat result.txt\n> answer runner@ci:/work$", "> answer"),
        ("runner@ci:/old$ cat result.txt\nHumanEval: examplesrunner@ci:/new$", "HumanEval: examples"),
        ("Enter password (will not be echoed):runner@ci:/work$", "Enter password (will not be echoed):"),
        ("abc123:/work# run-build\nready\nabc123:/work#", "ready"),
        ("\x1b[32mrunner@ci:/work$\x1b[0m run-build\n\x1b[31mValueError: invalid config\x1b[0m\nrunner@ci:/work$", "ValueError: invalid config"),
        ("runner@ci:/work$ run-build\nTraceback (most recent call last):\n  File 'build.py', line 4\nRuntimeError: build broke\nrunner@ci:/work$", "RuntimeError: build broke"),
        ("runner@ci:/work$ missing-tool\nbash: missing-tool: command not found\nrunner@ci:/work$", "bash: missing-tool: command not found"),
        ("ls: cannot access '/missing': No such file or directory", "ls: cannot access '/missing': No such file or directory"),
        ("compiler starting\nfatal: invalid source\nBuild completed", "fatal: invalid source"),
        ("runner@ci:/work$ compare-data\nmismatch in record 12\nexit\nasciinema: recording finished\nasciinema: asciicast saved to /logs/session.cast\nrunner@ci:/work$", "mismatch in record 12"),
        ("Current Terminal Screen:\nno server running on /tmp/session", "no server running on /tmp/session"),
        ("Successfully published artifact\nWARNING: running as root\n[notice] A new version is available", "Successfully published artifact"),
        ("starting checks\n4 passed in 0.10s\nWARNING: cache disabled", "4 passed in 0.10s"),
        ("first output\nlast output\n[stdout]\nWARNING: cache disabled", "last output"),
        ("WARNING: cache disabled\n[notice] update available", "No output beyond terminal echoes or notices"),
        ("WARNING: dependency failed to load\n[notice] update available", "WARNING: dependency failed to load"),
        ("root@host:/work# echo\nroot@host:/work#", "No output beyond terminal echoes or notices"),
        ("> actual output, without terminal framing", "> actual output, without terminal framing"),
        ("", "No captured content"),
    ]
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = _page(browser)
            page.goto(trace_server + "/?run=trace_readability")
            page.locator(".run-header").wait_for()
            for content, expected in cases:
                step = {"event_type": "tool_result", "content": {"content": content},
                        "availability": {"state": "complete", "capability": "tool_results"}}
                actual = page.evaluate("step => tracePreview(step)", step)
                assert actual == expected, content
            # Command previews still show the first line, never their output heuristic.
            assert page.evaluate("""() => tracePreview({event_type:'tool_call',
                content:{content:'echo first\\necho second'}})""") == "echo first"
            # Missing capability content stays gated, including during search.
            unavailable = {"step_id": "env", "panel": "environment", "content": {"content": "hidden process data"},
                           "availability": {"state": "unavailable", "capability": "process_state"}}
            assert page.evaluate("step => tracePreview(step)", unavailable) == "Not captured"
            assert "hidden process data" not in page.evaluate("step => traceSearchText(step)", unavailable)
        finally:
            browser.close()


def test_captured_lifecycle_text_is_searchable_and_inspectable(trace_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = _page(browser)
            page.goto(trace_server + "/?run=trace_readability&trace=1&evidence=end")
            page.locator("#trace-drawer.open").wait_for()
            expect(page.locator('.step.active .trace-preview')).to_have_text("Finished")
            expect(page.locator('.trace-tab.active')).to_have_text("Run events")
            expect(page.locator('.pane-active')).to_contain_text("content: Finished")
            expect(page.locator('.pane-active')).not_to_contain_text("Not captured")
            page.get_by_role("searchbox", name="Search trace").fill("Finished")
            expect(page.locator('#trace-body .step')).to_have_count(1)
            expect(page.locator('#trace-body .step')).to_have_attribute("data-step-id", "end")
        finally:
            browser.close()


def test_json_result_previews_decode_output_and_errors(trace_server):
    cases = [
        (json.dumps({"returncode": 0, "output": "first line\nanswer 42\n"}), "answer 42"),
        (json.dumps({"output": "bash: 7z: command not found\n", "exception_info": None}, indent=2), "bash: 7z: command not found"),
        (json.dumps({"output": "", "exception_info": "action was not executed"}), "Error: action was not executed"),
        (json.dumps({"output": "partial output", "exception_info": {"exception_type": "TimeoutError", "message": "deadline exceeded"}}), "TimeoutError: deadline exceeded"),
        (json.dumps({"output": "Successfully built artifact", "error": "upload failed"}), "Error: upload failed"),
        (json.dumps({"stdout": "ready\n", "stderr": "WARNING: cache disabled"}), "ready"),
        (json.dumps({"output": "", "exception_info": None}), "No captured content"),
        ({"output": "native object output\n", "exception_info": None}, "native object output"),
        ({"output": "", "error": False}, "No captured content"),
        (json.dumps({"answer": 42}), json.dumps({"answer": 42})),
        ('{"output": "truncated', '{"output": "truncated'),
    ]
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = _page(browser)
            page.goto(trace_server + "/?run=trace_readability")
            page.locator(".run-header").wait_for()
            for content, expected in cases:
                step = {"event_type": "tool_result", "content": {"content": content},
                        "availability": {"state": "complete", "capability": "tool_results"}}
                actual = page.evaluate("step => ({preview:tracePreview(step), original:step.content.content})", step)
                assert actual["preview"] == expected, content
                assert actual["original"] == content
        finally:
            browser.close()
