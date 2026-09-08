"""AGR-02 follow-up (review of commit 5782f1b): a Claude trace containing one
passing smoke test became task-level PASSED after attaching synthesized
checks, despite unknown task coverage.

The earlier AGR-02 fix (review 82cc113) only demoted an in-session-only PASS
to UNDETERMINED when a DECLARED (stated_requirement/environment_precondition)
required item existed and was left uncovered. A Claude trace's task carries
only a free-text instruction — task.requirements is always empty — so
build_contract never produces a declared item to be "uncovered" against, and
the demotion never fired. This end-to-end probe pins the real pipeline
(Claude adapter -> verifier_synth -> pipeline.analyze) against the fix.
"""

import json
import os

from agr.ingest_claude import convert
from agr.pipeline import analyze
from agr.store import Store
from agr.verifier_synth import synthesize_verifier


def _claude_stream(instruction="Fix the bug in the parser and make sure it works."):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x",
         "tools": ["Bash"], "cwd": "/app"},
        {"type": "user", "session_id": "s1", "message": {"role": "user", "content": instruction}},
        {"type": "assistant", "session_id": "s1",
         "message": {"role": "assistant", "model": "claude-x",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest tests/test_smoke.py"}}]}},
        {"type": "user", "session_id": "s1",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "1 passed in 0.01s"}]}},
        {"type": "assistant", "session_id": "s1",
         "message": {"role": "assistant", "model": "claude-x",
         "content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s1", "result": "ok"},
    ]
    return "\n".join(json.dumps(l) for l in lines)


def test_claude_trace_smoke_test_no_longer_certifies_full_task_pass(tmp_path):
    """A Claude trace with one passing smoke test and NO declared task
    requirements — the normal shape for a free-text instruction — must not
    read as a certified task-level PASSED once a verifier sidecar is
    synthesized from that single in-session test invocation."""
    path = tmp_path / "session.stream.jsonl"
    path.write_text(_claude_stream(), encoding="utf-8")

    result = convert(str(path), task_id="parser-fix")
    doc = result.doc
    assert doc["task"]["requirements"] == []  # the real shape: nothing declared

    sidecar = synthesize_verifier(doc)
    assert sidecar is not None and len(sidecar["checks"]) == 1
    doc["verifier"] = sidecar

    store = Store(str(tmp_path / "store"))
    analysis = analyze(doc, store)
    assert analysis.outcome["status"] == "UNDETERMINED"
    assert analysis.outcome["coverage_unknown"] is True
    assert analysis.outcome["passed"] == 1
    assert analysis.outcome["total"] == 1
    assert analysis.outcome["failed_checks"] == []


def test_claude_trace_with_an_explicit_declared_requirement_still_checks_coverage_normally(tmp_path):
    """When a caller DOES supply a declared requirement (e.g. via an explicit
    contract), the original AGR-02 gap-based behaviour still governs — this
    probe is additive, not a replacement for the declared-item-gap check."""
    path = tmp_path / "session.stream.jsonl"
    path.write_text(_claude_stream(), encoding="utf-8")

    result = convert(str(path), task_id="parser-fix")
    doc = result.doc
    doc["task"]["requirements"] = [
        {"id": "R1", "description": "smoke test passes"},
        {"id": "R2", "description": "the parser handles unicode input"},
    ]

    sidecar = synthesize_verifier(doc)
    doc["verifier"] = sidecar

    store = Store(str(tmp_path / "store"))
    analysis = analyze(doc, store)
    assert analysis.outcome["status"] == "UNDETERMINED"
    assert analysis.outcome["coverage_unknown"] is False
    assert "R2" in analysis.outcome["coverage_gaps"]
