"""Harbor adapter tests (spec §5.3).

The adapter is a pure format mapping: a Harbor (Terminal-Bench 2.0) trial
directory -> ATIF-shaped doc. These tests pin the two mappings that need
judgement — the ATIF-v1.7 turn fan-out and the reward -> verifier synthesis —
so a Harbor format change or an adapter edit surfaces as a failure, not a
silently shifted review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agr.ingest_harbor import convert

FIXTURE = Path(__file__).resolve().parent.parent / "archive" / "synthetic" / "fixtures" / "harbor_trial"


def _trial(tmp_path, steps, *, reward=1.0, exception=None, schema="ATIF-v1.7", agent=None):
    """Write a minimal Harbor trial dir (agent/trajectory.json + result.json)."""
    traj = {
        "schema_version": schema,
        "trajectory_id": "traj-x",
        "agent": agent if agent is not None else {"name": "pi-coding-agent", "model_name": "gpt-oss"},
        "steps": steps,
    }
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    result = {"reward": reward, "exception": exception}
    (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return str(tmp_path)


def test_fixture_converts_and_maps_turn_fanout():
    res = convert(FIXTURE, task_id="fix-failing-test")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    # One Harbor agent turn with a tool_call + observation fans out into ordered
    # model_output / tool_call / tool_result steps.
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    # Fixture's final agent action carries no submission signal, so the trial
    # ends run_completed (harness-side) — never a synthesised submission.
    assert kinds[-1] == "run_completed"
    assert "final_submission" not in kinds
    # reasoning_content is preserved as a tagged model_output, not dropped.
    assert any(s.get("content", "").startswith("[thinking]") for s in doc["steps"])
    # run metadata comes from the trajectory's agent block.
    assert doc["run"]["model"] == "gpt-oss"
    assert doc["run"]["agent"].startswith("pi-coding-agent")
    assert doc["source_type"] == "harbor"
    # Bare version stored so the core renders "ATIF-v1.7", not "ATIF-vATIF-v1.7";
    # the verbatim source token is retained in harness_version.
    assert doc["atif_version"] == "1.7"
    assert doc["run"]["harness_version"] == "harbor/ATIF-v1.7"


def test_tool_result_names_its_tool_from_the_call():
    res = convert(FIXTURE, task_id="t")
    # The first tool_result should be named after the bash call it answers,
    # resolved via source_call_id -> function_name.
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert results and results[0]["tool"] == "bash"
    # The extra.exit_code on the failing pytest is carried onto the result.
    assert results[0]["exit_code"] == 1


def test_mini_swe_agent_returncode_lifted_to_exit_code_and_status(tmp_path):
    """mini-swe-agent's own harness (not Harbor) encodes each result's exit
    status as JSON inside ``content`` — ``{"returncode": N, "output": ...}`` —
    since Harbor's ``extra.exit_code`` slot never carries it for this agent.
    Item 1: decode it so a nonzero returncode becomes a real tool_result
    failure, not an eternal 'unknown' that recovery/detectors never see."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "false"}},
        ], "observation": {"results": [
            {"source_call_id": "c1",
             "content": json.dumps({"returncode": 1, "output": "boom"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert results[0]["exit_code"] == 1
    assert results[0]["status"] == "error"


def test_mini_swe_agent_zero_returncode_is_ok(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "true"}},
        ], "observation": {"results": [
            {"source_call_id": "c1",
             "content": json.dumps({"returncode": 0, "output": "ok"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert results[0]["exit_code"] == 0
    assert results[0]["status"] == "ok"


def test_extra_exit_code_still_wins_over_content_returncode(tmp_path):
    """A source that (unusually) records both ``extra.exit_code`` and a JSON
    ``returncode`` inside content keeps trusting the dedicated exit-code slot —
    the content-decoding path is a fallback, not a competing source of truth."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "true"}},
        ], "observation": {"results": [
            {"source_call_id": "c1", "extra": {"exit_code": 0},
             "content": json.dumps({"returncode": 1, "output": "stale"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert results[0]["exit_code"] == 0


def test_non_json_content_leaves_no_exit_code(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "echo hi"}},
        ], "observation": {"results": [
            {"source_call_id": "c1", "content": "hi\n"},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert "exit_code" not in results[0]
    assert "status" not in results[0]


# --- item 20 (2026-09-08): terminus-2 has no exit code anywhere --------------

def test_terminus2_process_state_declared_unavailable(tmp_path):
    """Terminus-2 observations are raw terminal screen text with no exit code
    anywhere — process_state must be 'unavailable', not the default
    'partial' (which implies some process evidence was captured through tool
    I/O)."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash_command",
             "arguments": {"keystrokes": "ls -la\n", "duration": 0.1}},
        ], "observation": {"results": [
            {"source_call_id": "c1", "content": "New Terminal Output:\nroot@x:/app# ls -la\ntotal 8\n"},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, agent={"name": "terminus-2", "version": "2.0.0"}), task_id="t")
    assert res.doc["capabilities"]["process_state"] == "unavailable"
    assert any("terminus-2" in w for w in res.warnings)


def test_non_terminus2_agent_keeps_partial_process_state(tmp_path):
    res = convert(_trial(tmp_path, [
        {"step_id": 1, "source": "user", "message": "do it"},
    ], agent={"name": "mini-swe-agent"}), task_id="t")
    assert res.doc["capabilities"]["process_state"] == "partial"


def test_terminus2_keystrokes_hoisted_into_content(tmp_path):
    """Terminus-2's tool call carries the actual shell text under
    'keystrokes', not a structured 'command' argument — without hoisting it,
    the real command was buried inside a JSON blob and every deterministic
    matcher (recovery's objective identity, evidence slicing) saw
    '{"keystrokes":...' instead of the command."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash_command",
             "arguments": {"keystrokes": "pytest tests/\n", "duration": 0.2}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, agent={"name": "terminus-2"}), task_id="t")
    call = next(s for s in res.doc["steps"] if s["kind"] == "tool_call")
    assert call["content"] == "pytest tests/"


def test_terminus2_shell_error_marker_is_a_labelled_heuristic_not_exit_code(tmp_path):
    """The optional heuristic recognises a closed set of shell error strings
    but NEVER sets exit_code/status — those stay absent, honouring the
    'unavailable' capability declaration. The signal is separate and
    labelled."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash_command",
             "arguments": {"keystrokes": "nonexistentcmd\n"}},
        ], "observation": {"results": [
            {"source_call_id": "c1",
             "content": "New Terminal Output:\nroot@x:/app# nonexistentcmd\nbash: nonexistentcmd: command not found\n"},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, agent={"name": "terminus-2"}), task_id="t")
    result = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "exit_code" not in result
    assert "status" not in result
    assert result["heuristic_status"] == "error"
    assert result["heuristic_status_source"] == "shell_error_marker"


def test_terminus2_clean_output_has_no_heuristic_status(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash_command",
             "arguments": {"keystrokes": "ls\n"}},
        ], "observation": {"results": [
            {"source_call_id": "c1", "content": "New Terminal Output:\nroot@x:/app# ls\nfile.txt\n"},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, agent={"name": "terminus-2"}), task_id="t")
    result = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "heuristic_status" not in result


# --- item 27 (2026-09-08): per-turn metrics -> step cost, deduped ------------

def test_turn_metrics_attach_to_first_step_of_the_turn_only(tmp_path):
    """A turn's metrics is a TURN-level aggregate — it must land on only the
    first step this turn fans out into (reasoning/message/first tool_call),
    never once per tool_call in a multi-call turn."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "working",
         "reasoning_content": "let's check two things",
         "tool_calls": [
             {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "ls"}},
             {"tool_call_id": "c2", "function_name": "bash", "arguments": {"command": "pwd"}},
         ],
         "metrics": {"prompt_tokens": 820, "completion_tokens": 64, "cost_usd": 0.01}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    non_task_steps = [s for s in res.doc["steps"] if s["kind"] != "task_received"]
    # thinking, message, call c1, call c2 — cost only on the first (thinking).
    assert non_task_steps[0]["cost"] == {"prompt_tokens": 820, "completion_tokens": 64, "cost_usd": 0.01}
    assert non_task_steps[0]["generation_event"] is True
    assert res.doc["capabilities"]["generation_usage"] == "complete"
    assert "cost" not in non_task_steps[1]
    assert "cost" not in non_task_steps[2]
    assert "cost" not in non_task_steps[3]


def test_reward_one_synthesises_a_passed_verifier(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    check = res.doc["verifier"]["checks"][0]
    assert check["status"] == "passed"
    assert check["source"] == "native_structured"
    # AGR-04: the reward is post-run verifier evidence — labelled as such.
    assert check["timing"] == "post_run"
    assert res.doc["capture_completeness"] == "complete"
    # Seeing a reward sidecar does not make the verifier's CODE visible.
    assert res.doc["capabilities"]["verifier_code"] == "partial"
    assert res.doc["capabilities"]["verifier_results"] == "complete"


def test_reward_zero_synthesises_a_failed_verifier(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "failed"


def test_partial_reward_is_failed_with_warning(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=0.5), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "failed"
    assert any("partial reward" in w for w in res.warnings)


def test_exception_without_reward_is_error(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=None, exception={"type": "Timeout"}), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "error"


def test_harness_killed_run_gets_no_final_submission(tmp_path):
    """A timeout-terminated trial records run_timed_out — never a submission.

    The adapter must not invent an agent-authored submission (adapter 0.4):
    the synthesised final_submission made the unresolved_requirement_at_submission
    detector anchor on events the agent never authored (real-world bug: raman-fitting
    rev2 timed out at 900 s and was reviewed as 'submitted while failing').
    """
    steps = [
        {"step_id": 1, "source": "user", "message": "fit the peaks"},
        {"step_id": 2, "source": "agent", "message": "working", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "ls"}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, reward=0.0,
                         exception={"exception_type": "AgentTimeoutError",
                                    "exception_message": "timed out after 900.0 seconds"}),
                  task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert "final_submission" not in kinds
    assert kinds[-1] == "run_timed_out"
    assert res.doc["steps"][-1]["provenance"] == "synthetic"


def test_non_timeout_exception_records_run_failed(tmp_path):
    """Other exceptions (e.g. NonZeroAgentExitCodeError) are run_failed."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "working", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "make"}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, reward=0.0,
                         exception={"exception_type": "NonZeroAgentExitCodeError",
                                    "exception_message": "exit code 1"}),
                  task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert kinds[-1] == "run_failed"
    assert "final_submission" not in kinds


def test_zero_agent_step_crash_is_run_failed_not_completed(tmp_path):
    """Harbor can record a trial as completed even when the agent crashed
    before acting (mini-swe-agent RepeatedFormatError: zero agent turns).
    With no agent steps there is no submission to attribute anything to, and
    the trial end is a protocol failure (run_failed with termination_reason),
    never an agent submission and never a completion — a crash is not a
    completed run."""
    steps = [
        {"step_id": 1, "source": "user", "message": "set up nginx"},
        {"step_id": 2, "source": "system", "message": "agent exited with format error"},
    ]
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert "final_submission" not in kinds
    assert kinds[-1] == "run_failed"
    assert res.doc["steps"][-1]["provenance"] == "synthetic"
    assert res.doc["steps"][-1]["termination_reason"] == "agent_protocol_failure"


def test_completed_run_still_gets_final_submission(tmp_path):
    """A directly observed submission signal (mini-swe-agent's terminal echo)
    yields a final_submission with provenance=observed."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "done", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash",
             "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert kinds[-1] == "final_submission"
    sub = res.doc["steps"][-1]
    assert sub["provenance"] == "observed"
    assert sub["actor"] == "main_agent"


def test_submission_echo_non_execution_response_is_tagged(tmp_path):
    """AGR-03: mini-swe-agent intercepts its own submission echo and never
    executes it, reporting that honestly as returncode -1 / exception_info
    "action was not executed" — real Harbor captures carry no source_call_id
    on this result at all, so the single call this turn made is the exact
    (not guessed) pairing. The tagged result must still keep its raw fields
    and the run must still get its final_submission."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "done", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash",
             "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}},
        ], "observation": {"results": [
            {"content": json.dumps({"returncode": -1, "output": "",
                                     "exception_info": "action was not executed"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert kinds[-1] == "final_submission"
    result_step = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert result_step["submission_control_response"] is True
    # raw fields are preserved, not erased by the tag.
    assert result_step["exit_code"] == -1
    assert result_step["status"] == "error"


def test_unrelated_negative_returncode_is_not_tagged(tmp_path):
    """Only the submission call's own non-execution reply is tagged — an
    unrelated command that happens to return -1 is left as a real failure."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "trying", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "some-tool"}},
        ], "observation": {"results": [
            {"content": json.dumps({"returncode": -1, "output": "segfault"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    result_step = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "submission_control_response" not in result_step
    assert result_step["exit_code"] == -1
    assert result_step["status"] == "error"


def test_submission_call_with_ordinary_success_is_not_tagged(tmp_path):
    """The submission call itself succeeding normally (returncode 0) is not
    a non-execution response — only the specific sentinel message is tagged."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "done", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash",
             "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}},
        ], "observation": {"results": [
            {"content": json.dumps({"returncode": 0, "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    result_step = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "submission_control_response" not in result_step


def test_ambiguous_multi_call_turn_is_not_tagged(tmp_path):
    """No source_call_id and more than one call this turn: which call the
    result answers is genuinely ambiguous, so tagging must not guess."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "done", "tool_calls": [
            {"function_name": "bash", "arguments": {"command": "ls"}},
            {"function_name": "bash", "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}},
        ], "observation": {"results": [
            {"content": json.dumps({"returncode": -1, "output": "",
                                     "exception_info": "action was not executed"})},
        ]}},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    result_step = next(s for s in res.doc["steps"] if s["kind"] == "tool_result")
    assert "submission_control_response" not in result_step


def test_mark_task_complete_is_an_observed_submission(tmp_path):
    """terminus-2 signals completion via a mark_task_complete tool call."""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "done", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "mark_task_complete", "arguments": {}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert kinds[-1] == "final_submission"
    assert res.doc["steps"][-1]["provenance"] == "observed"


def test_normal_end_without_submission_signal_is_run_completed(tmp_path):
    """A normally-ended run whose final action is ordinary work is
    run_completed — the adapter must never promote it to a submission.
    (Iteration exhaustion without a harness exception lands here too.)"""
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},
        {"step_id": 2, "source": "agent", "message": "still working", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "make test"}},
        ]},
    ]
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t")
    kinds = [s["kind"] for s in res.doc["steps"]]
    assert kinds[-1] == "run_completed"
    assert "final_submission" not in kinds
    # fan-out steps are provenance=observed
    assert res.doc["steps"][0]["provenance"] == "observed"
    assert res.doc["steps"][1]["provenance"] == "observed"


def test_explicit_verifier_overrides_result_reward(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    explicit = {"raw_output": "manual", "checks": [{"check_id": "C1", "status": "passed"}]}
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t", verifier=explicit)
    # The caller's sidecar wins over the trial's failing reward.
    assert res.doc["verifier"]["checks"][0]["check_id"] == "C1"


def test_missing_result_json_ingests_unverified(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    traj = {"schema_version": "ATIF-v1.7", "agent": {"model_name": "gpt-oss"}, "steps": steps}
    (agent_dir / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    res = convert(str(tmp_path), task_id="t")  # no result.json written
    assert "verifier" not in res.doc
    assert res.doc["capture_completeness"] == "partial"
    assert any("UNVERIFIED" in w for w in res.warnings)


def test_multimodal_message_flattens_to_text(tmp_path):
    steps = [
        {"step_id": 1, "source": "user",
         "message": [{"type": "text", "text": "look at this"}, {"type": "image"}]},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    task = next(s for s in res.doc["steps"] if s["kind"] == "task_received")
    assert "look at this" in task["content"] and "[image]" in task["content"]


def test_unmapped_source_warns_not_drops_silently(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "hi"},
        {"step_id": 2, "source": "oracle", "message": "???"},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    assert any("oracle" in w for w in res.warnings)


def test_accepts_direct_trajectory_json_path():
    res = convert(FIXTURE / "agent" / "trajectory.json", task_id="t")
    # result.json is found one level up from agent/, so the verifier still lands.
    assert res.doc["verifier"]["checks"][0]["status"] == "passed"


def test_converted_doc_ingests_end_to_end():
    """The adapter output must satisfy the real ingestion contract (§7.2)."""
    from agr.ingest import _validate

    res = convert(FIXTURE, task_id="fix-failing-test")
    _validate(res.doc)  # raises on any contract violation


def test_registry_lists_and_resolves_harbor():
    from agr.adapter import Adapter, adapter_names, get_adapter

    assert "harbor" in adapter_names()
    harbor = get_adapter("harbor")
    assert harbor.name == "harbor"
    assert isinstance(harbor, Adapter)


def test_converted_doc_stamps_adapter_version():
    """The doc carries the adapter's provenance stamp for ingestion to record."""
    from agr.ingest_harbor import HARBOR_ADAPTER_VERSION

    res = convert(FIXTURE, task_id="t")
    assert res.doc["adapter_version"] == HARBOR_ADAPTER_VERSION


def test_nested_verifier_result_reward_shape(tmp_path):
    """Current Harbor writes the reward nested under verifier_result.rewards;
    both shapes must synthesise the same verifier."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=None))
    (trial_dir / "result.json").write_text(json.dumps({
        "trial_name": "my-task__agent__attempt-1",
        "verifier_result": {"rewards": {"reward": 1.0}, "exception_info": None},
    }), encoding="utf-8")
    res = convert(str(trial_dir), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "passed"


def test_task_id_prefers_what_harbor_recorded(tmp_path):
    """With no --task-id, the trial's own task_name is used (source-supplied),
    with a warning that the contract should be confirmed."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=1.0))
    result_file = trial_dir / "result.json"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    data["task_name"] = "fix-failing-test"
    result_file.write_text(json.dumps(data), encoding="utf-8")
    res = convert(str(trial_dir))
    assert res.doc["run"]["task_id"] == "fix-failing-test"
    assert any("task_id taken from result.json" in w for w in res.warnings)


def test_flat_layout_does_not_steal_an_unrelated_result(tmp_path):
    """A trajectory.json given directly in a flat layout must not reach upward
    past its own directory and adopt some other trial's result.json."""
    outer = tmp_path / "other_trial"
    outer.mkdir()
    (outer / "result.json").write_text(json.dumps({"reward": 1.0}), encoding="utf-8")
    inner = tmp_path / "my_trial"
    inner.mkdir()
    traj = {"schema_version": "ATIF-v1.7", "agent": {}, "steps":
            [{"step_id": 1, "source": "user", "message": "do it"}]}
    (inner / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    res = convert(inner / "trajectory.json", task_id="t")
    # No result beside this trajectory: ingests UNVERIFIED, not a borrowed pass.
    assert "verifier" not in res.doc
    assert any("UNVERIFIED" in w for w in res.warnings)


def test_iter_trials_resolves_job_directories(tmp_path):
    """A harbor run job directory (one subdir per trial) fans out; a single
    trial directory resolves to itself."""
    from agr.ingest_harbor import iter_trials

    def _mk_trial(parent: Path, name: str) -> None:
        d = parent / name
        (d / "agent").mkdir(parents=True)
        (d / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
        (d / "result.json").write_text("{}", encoding="utf-8")

    job = tmp_path / "job"
    _mk_trial(job, "b_trial")
    _mk_trial(job, "a_trial")
    trials = iter_trials(job)
    assert [t.name for t in trials] == ["a_trial", "b_trial"]

    single = tmp_path / "solo"
    _mk_trial(tmp_path, "solo")
    assert iter_trials(single) == [single]

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no Harbor trial"):
        iter_trials(empty)


def test_iter_trials_resolves_a_directory_of_job_directories(tmp_path):
    """A committed corpus root (a directory of harbor job dirs) fans out two
    levels: root -> job -> trial. This is what makes `ingest-harbor corpus/`
    one command for a published evaluation corpus."""
    from agr.ingest_harbor import iter_trials

    def _mk_trial(parent: Path, name: str) -> None:
        d = parent / name
        (d / "agent").mkdir(parents=True)
        (d / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
        (d / "result.json").write_text("{}", encoding="utf-8")

    root = tmp_path / "corpus"
    for job_name in ("batch1-foo", "batch2-bar"):
        _mk_trial(root / job_name, f"{job_name}__task__1")
    # A non-job entry (loose log, writeoff without trajectory) is skipped.
    (root / "notes.log").write_text("log line", encoding="utf-8")
    wo = root / "_writeoffs" / "dbg" / "trial__x__1"
    wo.mkdir(parents=True)
    (wo / "result.json").write_text("{}", encoding="utf-8")  # no trajectory

    trials = iter_trials(root)
    assert [t.name for t in trials] == ["batch1-foo__task__1", "batch2-bar__task__1"]


def test_job_root_with_rollup_result_is_not_mistaken_for_a_trial(tmp_path):
    """A real `harbor run` job dir carries a top-level result.json beside the
    trial subdirs; discovery must fan out to the trials, not stop at the root."""
    from agr.ingest_harbor import iter_trials

    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text("{}", encoding="utf-8")  # roll-up result
    for name in ("t1", "t2"):
        d = job / f"{name}__agent__1"
        (d / "agent").mkdir(parents=True)
        (d / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
    # An errored trial with a result but no trajectory is not reviewable.
    err = job / "errored__agent__1"
    err.mkdir()
    (err / "result.json").write_text("{}", encoding="utf-8")

    assert [t.name for t in iter_trials(job)] == ["t1__agent__1", "t2__agent__1"]


def test_bom_encoded_trial_files_are_tolerated(tmp_path):
    """Logs written by Windows tooling often carry a UTF-8 BOM; the adapter
    must read them, not reject them as invalid JSON."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=None))
    for rel in ("result.json", "agent/trajectory.json"):
        p = trial_dir / rel
        p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    res = convert(str(trial_dir), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "unknown"


def test_cli_ingest_harbor_batches_a_job_directory(tmp_path):
    """The first-class verb ingests every trial in one command."""
    from agr.cli import main

    def _mk_trial(parent: Path, name: str) -> None:
        d = parent / name
        (d / "agent").mkdir(parents=True)
        traj = {"schema_version": "ATIF-v1.7", "trajectory_id": f"tr-{name}",
                "agent": {"name": "pi-coding-agent", "model_name": "gpt-oss"},
                "steps": [{"step_id": 1, "source": "user", "message": "do it"}]}
        (d / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
        (d / "result.json").write_text(
            json.dumps({"reward": 1.0, "task_name": name}), encoding="utf-8")

    job = tmp_path / "job"
    _mk_trial(job, "alpha")
    _mk_trial(job, "beta")
    store = tmp_path / "store"

    rc = main(["--store", str(store), "ingest-harbor", str(job)])
    assert rc == 0
    from agr.store import Store

    ingested_store = Store(str(store))
    # One capture registered per trial, keyed by the derived logical run id.
    # No trial UUID in these minimal results, so identity is the adapter-0.6
    # hash of the complete task+session identity (AGR-02) — distinct per trial,
    # never a truncated prefix.
    import hashlib

    def _hash_id(task, session):
        return f"harbor__{task}__{hashlib.sha256(f'{task}|{session}'.encode()).hexdigest()[:32]}"

    assert ingested_store.read_index(_hash_id("alpha", "tr-alpha"))
    assert ingested_store.read_index(_hash_id("beta", "tr-beta"))


# --- AGR-02: execution identity (adapter 0.6) --------------------------------

def _trial_with_session(tmp_path, session_id, *, trial_uuid=None, task="terminal-bench/nginx-request-logging"):
    """A Harbor trial whose session id is task-prefixed, as mini-swe-agent writes."""
    steps = [{"step_id": 1, "source": "user", "message": "log requests"}]
    tmp_path.mkdir(parents=True, exist_ok=True)
    d = Path(_trial(tmp_path, steps, reward=1.0))
    traj = json.loads((d / "agent" / "trajectory.json").read_text(encoding="utf-8"))
    # Real mini-swe-agent trajectories carry a session_id and no trajectory_id;
    # identity rests on the session the harness recorded.
    traj["session_id"] = session_id
    traj.pop("trajectory_id", None)
    (d / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    result["task_name"] = task
    if trial_uuid:
        result["id"] = trial_uuid
        result["trial_name"] = session_id.split("__agent")[0]
    (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return d


def test_distinct_attempts_sharing_a_session_prefix_get_distinct_run_ids(tmp_path):
    """The reproduced AGR-02 defect: three attempts of one task whose session
    ids are task-prefixed ('nginx-request-logging__<SHORT>__agent') all
    truncated to the same first 12 chars and merged into one logical run.
    Distinct executions must get distinct ids (pass/fail/pass outcomes)."""
    sessions = ["nginx-request-logging__vTNAJM8__agent",
                "nginx-request-logging__h6dA2go__agent",
                "nginx-request-logging__rwPp8Q4__agent"]
    ids = set()
    for i, session in enumerate(sessions):
        d = _trial_with_session(tmp_path / f"t{i}", session,
                                trial_uuid=f"00000000-0000-4000-8000-{i:012d}")
        res = convert(str(d))
        ids.add(res.doc["run"]["logical_run_id"])
        assert res.doc["run"]["trial_uuid"] == f"00000000-0000-4000-8000-{i:012d}"
        assert res.doc["run"]["trial_name"].startswith("nginx-request-logging__")
    assert len(ids) == 3


def test_run_id_prefers_the_full_harbor_trial_uuid(tmp_path):
    d = _trial_with_session(tmp_path, "nginx-request-logging__vTNAJM8__agent",
                            trial_uuid="96cfb6ac-cbf9-4858-b9b3-8916b3f86827")
    res = convert(str(d))
    run_id = res.doc["run"]["logical_run_id"]
    assert run_id == "harbor__terminal-bench/nginx-request-logging__96cfb6ac-cbf9-4858-b9b3-8916b3f86827"


def test_without_a_uuid_the_run_id_hashes_the_complete_identity(tmp_path):
    """No UUID: the id derives from the FULL task+session identity — stable
    across re-ingests and machines, never a 12-char prefix that collides."""
    a = _trial_with_session(tmp_path / "a", "nginx-request-logging__vTNAJM8__agent")
    b = _trial_with_session(tmp_path / "b", "nginx-request-logging__h6dA2go__agent")
    import hashlib

    expected = "harbor__terminal-bench/nginx-request-logging__" + hashlib.sha256(
        "terminal-bench/nginx-request-logging|nginx-request-logging__vTNAJM8__agent".encode()
    ).hexdigest()[:32]
    ra, rb = convert(str(a)), convert(str(b))
    assert ra.doc["run"]["logical_run_id"] == expected
    assert ra.doc["run"]["logical_run_id"] != rb.doc["run"]["logical_run_id"]
    # The weaker identity basis is visible as a warning, not silent.
    assert any("hash of the full task+session identity" in w for w in ra.warnings)


def test_hash_identity_is_stable_across_locations(tmp_path):
    """Re-ingesting the same trial from a different directory keeps the id —
    identity lives in the task+session, not the filesystem path."""
    a = _trial_with_session(tmp_path / "first", "nginx-request-logging__vTNAJM8__agent")
    b = _trial_with_session(tmp_path / "elsewhere" / "copy", "nginx-request-logging__vTNAJM8__agent")
    assert convert(str(a)).doc["run"]["logical_run_id"] == convert(str(b)).doc["run"]["logical_run_id"]


def test_reingest_of_the_same_trial_is_idempotent(tmp_path):
    """Same trial, same store: the second ingest is a no-op on the source."""
    from agr.store import Store
    from agr.pipeline import analyze

    d = _trial_with_session(tmp_path / "t", "nginx-request-logging__vTNAJM8__agent",
                            trial_uuid="96cfb6ac-cbf9-4858-b9b3-8916b3f86827")
    store = Store(str(tmp_path / "store"))
    doc = convert(str(d)).doc
    first = analyze(doc, store)
    second = analyze(doc, store)
    assert second.idempotent
    assert second.run_source.capture_revision == first.run_source.capture_revision
    assert second.run_source.source_capture_id == first.run_source.source_capture_id


def test_iter_trials_detailed_accounts_for_every_directory(tmp_path):
    """Discovery reports excluded directories WITH a reason, so a batch run can
    account for every discovered trial as ingested or excluded (AGR-02)."""
    job = tmp_path / "job"
    _trial_with_session(job / "good", "nginx-request-logging__vTNAJM8__agent")
    errored = job / "errored"
    errored.mkdir()
    (errored / "result.json").write_text(json.dumps({"reward": None}), encoding="utf-8")

    from agr.ingest_harbor import iter_trials_detailed

    detail = iter_trials_detailed(job)
    by_name = {p.name: reason for p, reason in detail}
    assert set(by_name) == {"good", "errored"}
    assert by_name["good"] is None
    assert by_name["errored"] and "no reviewable trajectory" in by_name["errored"]
    # iter_trials keeps its reviewed-only contract.
    from agr.ingest_harbor import iter_trials

    assert [p.name for p in iter_trials(job)] == ["good"]


# --- AGR-04: preserve the information needed for diagnosis --------------------

def _write_ctrf(trial, tests, summary=None):
    ctrf_dir = Path(trial) / "verifier"
    ctrf_dir.mkdir(exist_ok=True)
    payload = {"results": {"tool": {"name": "pytest"}, "summary": summary or {"passed": len(tests)},
                           "tests": tests}}
    (ctrf_dir / "ctrf.json").write_text(json.dumps(payload), encoding="utf-8")


def test_ctrf_tests_become_atomic_checks_with_the_aggregate_reward(tmp_path):
    """The acceptance case: eight CTRF tests, one failing (including
    test_outputs.py::test_log_file_format), each its own check; the aggregate
    reward stays the run outcome as an explicitly labelled aggregate check."""
    tests = [{"name": f"test_outputs.py::test_{i}", "status": "passed",
              "file_path": "test_outputs.py"} for i in range(7)]
    tests.append({"name": "test_outputs.py::test_log_file_format", "status": "failed",
                  "file_path": "test_outputs.py"})
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=0.0)
    _write_ctrf(trial, tests)
    res = convert(trial, task_id="t")
    checks = res.doc["verifier"]["checks"]
    ids = [c["check_id"] for c in checks]
    assert len(checks) == 9  # 8 CTRF tests + the aggregate reward
    assert "test_outputs.py::test_log_file_format" in ids
    failing = [c for c in checks if c["status"] == "failed"
               and c["check_id"] != "terminal_bench_reward"]
    assert [c["check_id"] for c in failing] == ["test_outputs.py::test_log_file_format"]
    # The aggregate reward survives as the run outcome, named as an aggregate.
    agg = next(c for c in checks if c["check_id"] == "terminal_bench_reward")
    assert agg["status"] == "failed" and "aggregate" in agg["name"]


def test_ctrf_checks_are_labelled_post_run_with_source_pointers(tmp_path):
    """Post-run verifier evidence is labelled so it is never confused with
    information the agent had during execution."""
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=1.0)
    _write_ctrf(trial, [{"name": "test_outputs.py::test_x", "status": "passed",
                         "file_path": "test_outputs.py"}])
    res = convert(trial, task_id="t")
    for c in res.doc["verifier"]["checks"]:
        assert c["timing"] == "post_run"
    ctrf_check = next(c for c in res.doc["verifier"]["checks"]
                      if c["check_id"] == "test_outputs.py::test_x")
    assert "verifier/ctrf.json" in ctrf_check["source_pointers"]
    assert "verifier/test_outputs.py" in ctrf_check["source_pointers"]


def test_unknown_ctrf_status_is_preserved_not_guessed(tmp_path):
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=1.0)
    _write_ctrf(trial, [{"name": "test_a", "status": "skipped"},
                        {"name": "test_b", "status": "WEIRD"}])
    res = convert(trial, task_id="t")
    by_id = {c["check_id"]: c for c in res.doc["verifier"]["checks"]}
    assert by_id["test_a"]["status"] == "skipped"
    assert by_id["test_b"]["status"] == "unknown"
    assert any("WEIRD" in w for w in res.warnings)


def test_verifier_log_excerpt_is_attached_capped_and_sourced(tmp_path):
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=1.0)
    _write_ctrf(trial, [{"name": "test_a", "status": "failed"}])
    vdir = Path(trial) / "verifier"
    (vdir / "test-stdout.txt").write_text("E   assert 404 == 200\n" * 10, encoding="utf-8")
    res = convert(trial, task_id="t")
    excerpt = res.doc["verifier"]["log_excerpts"][0]
    assert excerpt["source"] == "verifier/test-stdout.txt"
    assert excerpt["timing"] == "post_run"
    assert "assert 404 == 200" in excerpt["content"]
    # Over the cap -> truncated, never silently omitted or unbounded.
    (vdir / "test-stdout.txt").write_text("x" * 20_000, encoding="utf-8")
    res2 = convert(trial, task_id="t")
    assert res2.doc["verifier"]["log_excerpts"][0]["content"].endswith("[truncated]")
    assert any("truncated" in w for w in res2.warnings)


def test_missing_or_broken_ctrf_falls_back_to_the_aggregate_reward(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    assert [c["check_id"] for c in res.doc["verifier"]["checks"]] == ["terminal_bench_reward"]
    broken = _trial(tmp_path, steps, reward=1.0)
    vdir = Path(broken) / "verifier"
    vdir.mkdir(exist_ok=True)
    (vdir / "ctrf.json").write_text("{not json", encoding="utf-8")
    res2 = convert(broken, task_id="t")
    assert [c["check_id"] for c in res2.doc["verifier"]["checks"]] == ["terminal_bench_reward"]
    assert any("ctrf" in w.lower() for w in res2.warnings)


def test_explicit_verifier_wins_over_ctrf(tmp_path):
    explicit = {"checks": [{"check_id": "C1", "name": "C1", "status": "failed",
                            "source": "native_structured"}]}
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=1.0)
    _write_ctrf(trial, [{"name": "test_a", "status": "passed"}])
    res = convert(trial, task_id="t", verifier=explicit)
    assert [c["check_id"] for c in res.doc["verifier"]["checks"]] == ["C1"]


def test_full_task_instruction_is_kept_not_an_excerpt(tmp_path):
    """The complete instruction reaches doc.task.instruction and the reviewer's
    packet field — the plan's 220-character-excerpt defect stays dead."""
    full = "Please solve this issue: " + ("Configure detailed request logging. " * 130)
    assert len(full) > 4_700  # nginx-scale instruction
    steps = [{"step_id": 1, "source": "user", "message": full}]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    assert res.doc["task"]["instruction"] == full
    assert len(res.doc["task"]["instruction"]) == len(full)


def test_step_timestamps_are_preserved_when_the_source_has_them(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "do it"},          # no ts
        {"step_id": 2, "source": "agent", "message": "working",
         "timestamp": "2026-08-24T13:30:38.435732+00:00"},
    ]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    stamped = [s for s in res.doc["steps"] if s["kind"] == "model_output"]
    assert stamped and all(s.get("timestamp") == "2026-08-24T13:30:38.435732+00:00"
                           for s in stamped)
    # Never synthesised for steps the source left timestampless.
    received = next(s for s in res.doc["steps"] if s["kind"] == "task_received")
    assert "timestamp" not in received


def test_task_checksum_and_ref_recorded_when_source_supports_them(tmp_path):
    trial = _trial(tmp_path, [{"step_id": 1, "source": "user", "message": "do it"}], reward=1.0)
    result_path = Path(trial) / "result.json"
    data = json.loads(result_path.read_text(encoding="utf-8"))
    data["task_checksum"] = "913305d8"
    data["task_id"] = {"org": "terminal-bench", "name": "nginx-request-logging", "ref": "v2.1"}
    result_path.write_text(json.dumps(data), encoding="utf-8")
    res = convert(trial, task_id="t")
    assert res.doc["run"]["task_checksum"] == "913305d8"
    assert res.doc["run"]["task_org"] == "terminal-bench"
    assert res.doc["run"]["task_name"] == "nginx-request-logging"
    assert res.doc["run"]["task_ref"] == "v2.1"


# --- AGR-03 real-corpus regression -------------------------------------------

_EVAL_RUNS = Path(__file__).resolve().parent.parent / "eval-runs"


def test_published_corpus_has_exactly_seven_submission_control_responses():
    """Pins the review's own count: "the seven inspected cases" of
    mini-swe-agent's submission echo coming back as returncode -1 / "action
    was not executed" in the published eval-runs/ corpus. A change to the
    pairing or sentinel-matching logic that stops recognising one of these
    (or starts over-matching unrelated failures) should fail this test."""
    from agr.ingest_harbor import iter_trials

    tagged = 0
    for trial in iter_trials(str(_EVAL_RUNS)):
        try:
            res = convert(str(trial))
        except ValueError:
            continue
        for step in res.doc["steps"]:
            if step.get("submission_control_response"):
                tagged += 1
    assert tagged == 7


def test_tagged_submission_response_never_opens_a_recovery_episode():
    """End-to-end: a real trial carrying the tagged response must not produce
    an UNRECOVERED episode (and therefore no ignored_tool_failure finding)
    anchored on the submission round-trip."""
    from agr.pipeline import analyze
    from agr.recovery import UNRECOVERED
    from agr.store import Store
    import tempfile

    trial = _EVAL_RUNS / "swpC-nginx-request-logging-a3" / "nginx-request-logging__rwPp8Q4"
    res = convert(str(trial))
    submission_result = next(s for s in res.doc["steps"]
                             if s["kind"] == "tool_result" and s.get("submission_control_response"))
    assert submission_result is not None
    with tempfile.TemporaryDirectory() as tmp:
        analysis = analyze(res.doc, Store(tmp))
    failure_step_ids = {ep.failure_event_id for ep in analysis.recoveries if ep.classification == UNRECOVERED}
    anchored_events = {e.event_id for e in analysis.events
                       if submission_result["step_id"] in e.source_step_ids}
    assert not (failure_step_ids & anchored_events)
    ignored_tool_failure_anchors = {
        a for result in analysis.detector_results if result.detector == "ignored_tool_failure"
        for cand in result.candidates for a in cand.anchor_event_ids
    }
    assert not (ignored_tool_failure_anchors & anchored_events)


def test_packet_carries_the_full_instruction_and_check_timing(tmp_path):
    """The reviewer's typed input packet: dedicated task_instruction field,
    atomic checks carrying their post-run timing labels."""
    from agr.model_packet import build_packet
    from agr.reviewer import ReviewerContext
    full = "Please solve this issue: " + ("Configure detailed request logging. " * 130)
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[], slices=[], checks=[],
        events=[], task_instruction=full)
    packet, _red = build_packet(ctx)
    assert packet["task_instruction"] == full
