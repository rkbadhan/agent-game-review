"""Synthesize a ``--verifier`` sidecar from in-session test invocations
(item 25, 2026-09-08).

Many sessions already run ``pytest``/``npm test``/``cargo test``/``go test``
DURING the run — real, atomic evidence the agent itself observed, not
invented. This extracts those invocations and their pass/fail results from
an already-converted ATIF document and writes exactly the ``--verifier``
sidecar shape ``ingest``/every adapter already accepts (``{"raw_output":
..., "checks": [...]}``), turning an otherwise UNVERIFIED run into
PROVISIONAL with real atomic checks — no external verifier run required.

Deliberately narrow: a closed, versioned set of test-runner invocations
(matched on the executable/subcommand only, never on flags or targets) and a
closed set of well-known output-format patterns per runner family. An
invocation or output shape this doesn't recognise produces NO check for it —
never a guessed pass/fail from prose.
"""

from __future__ import annotations

import re
from typing import Optional

from ._util import paired_result
from .events import derive_events
from .schema import RunSource

# --- item 25: recognised test-runner invocations, matched on the command's
# executable/subcommand only (never flags or targets), so `pytest -k foo
# tests/` and `npm test -- --watch` both match their runner.
_RUNNER_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("pytest", re.compile(r"^(python3?\s+-m\s+)?pytest\b")),
    ("npm_test", re.compile(r"^npm\s+(test|run\s+test)\b")),
    ("yarn_test", re.compile(r"^yarn\s+test\b")),
    ("cargo_test", re.compile(r"^cargo\s+test\b")),
    ("go_test", re.compile(r"^go\s+test\b")),
    ("rspec", re.compile(r"^(bundle\s+exec\s+)?rspec\b")),
]


def _match_runner(command: str) -> Optional[str]:
    text = command.strip()
    for name, pattern in _RUNNER_PATTERNS:
        if pattern.match(text):
            return name
    return None


def _parse_pytest(output: str) -> Optional[tuple[int, int]]:
    m_pass = re.search(r"(\d+)\s+passed", output)
    m_fail = re.search(r"(\d+)\s+failed", output)
    m_err = re.search(r"(\d+)\s+error", output)
    if not (m_pass or m_fail or m_err):
        return None
    passed = int(m_pass.group(1)) if m_pass else 0
    failed = (int(m_fail.group(1)) if m_fail else 0) + (int(m_err.group(1)) if m_err else 0)
    return passed, failed


def _parse_npm_jest(output: str) -> Optional[tuple[int, int]]:
    m = re.search(r"Tests:\s+(?:(\d+)\s+failed,\s*)?(?:\d+\s+skipped,\s*)?(\d+)\s+passed", output)
    if m:
        failed = int(m.group(1)) if m.group(1) else 0
        return int(m.group(2)), failed
    m_pass = re.search(r"(\d+)\s+passing", output)
    m_fail = re.search(r"(\d+)\s+failing", output)
    if m_pass or m_fail:
        return (int(m_pass.group(1)) if m_pass else 0, int(m_fail.group(1)) if m_fail else 0)
    return None


def _parse_cargo(output: str) -> Optional[tuple[int, int]]:
    m = re.search(r"test result:\s*(?:ok|FAILED)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed", output)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_go_test(output: str) -> Optional[tuple[int, int]]:
    passed = len(re.findall(r"^--- PASS:", output, re.MULTILINE))
    failed = len(re.findall(r"^--- FAIL:", output, re.MULTILINE))
    if passed or failed:
        return passed, failed
    # No per-test lines (go test without -v): fall back to the one overall
    # summary line `go test` always prints.
    if re.search(r"^ok\s+\S+", output, re.MULTILINE):
        return 1, 0
    if re.search(r"^FAIL\b", output, re.MULTILINE):
        return 0, 1
    return None


def _parse_rspec(output: str) -> Optional[tuple[int, int]]:
    m = re.search(r"(\d+)\s+examples?,\s*(\d+)\s+failures?", output)
    if not m:
        return None
    total, failed = int(m.group(1)), int(m.group(2))
    return total - failed, failed


_PARSERS = {
    "pytest": _parse_pytest,
    "npm_test": _parse_npm_jest,
    "yarn_test": _parse_npm_jest,
    "cargo_test": _parse_cargo,
    "go_test": _parse_go_test,
    "rspec": _parse_rspec,
}


def synthesize_verifier(doc: dict) -> Optional[dict]:
    """Extract in-session test invocations and results from an already-
    converted ATIF document and build a ``--verifier`` sidecar.

    Returns ``None`` when no recognised test invocation with a parseable
    result was found — the caller keeps the run UNVERIFIED rather than
    writing an empty, misleading sidecar.
    """
    run = doc.get("run") or {}
    rs = RunSource(
        run_id=str(run.get("logical_run_id") or "synth"),
        source_capture_id="synth",
        capture_revision=1,
        source_hash="",
        source_type=str(doc.get("source_type") or "unknown"),
        source_schema=str(doc.get("atif_version") or ""),
        capture_completeness=str(doc.get("capture_completeness") or "partial"),
        task_id=str(run.get("task_id") or ""),
    )
    events = derive_events(doc, rs)

    checks: list[dict] = []
    raw_chunks: list[str] = []
    for idx, ev in enumerate(events):
        if ev.event_type != "tool_call":
            continue
        command = ev.payload.get("content") or ""
        runner = _match_runner(command)
        if runner is None:
            continue
        result = paired_result(events, idx)
        if result is None:
            continue
        output = result.payload.get("content") or ""
        parsed = _PARSERS[runner](output)
        if parsed is None:
            continue
        passed, failed = parsed
        status = "passed" if failed == 0 and passed > 0 else "failed" if failed > 0 else "unknown"
        check_id = f"insession_{runner}_{len(checks) + 1}"
        checks.append({
            "check_id": check_id,
            "name": f"{runner}: {passed} passed, {failed} failed (in-session)",
            "status": status,
            "source": "output_interpretation",
            "timing": "during_run",
            "source_pointers": [ev.event_id, result.event_id],
        })
        raw_chunks.append(f"[{ev.event_id} -> {result.event_id}] {command}\n{output}")

    if not checks:
        return None
    return {"raw_output": "\n\n".join(raw_chunks), "checks": checks}
