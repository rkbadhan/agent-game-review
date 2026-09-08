"""Runner harness for scheduled claude -p sweeps (item 26, 2026-09-08).

Exercises the REAL control flow (task-set parsing, workdir isolation,
sidecar construction, ingestion, per-task error handling) against FAKE
claude_runner/verifier_runner callables — never a live `claude -p` process
or a real verifier script, which would cost real API tokens / require an
actual binary on every test run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agr import runner
from agr.store import Store


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _stream_json_lines(session_id, model="m"):
    """A minimal claude -p --output-format stream-json capture the real
    Claude adapter converts successfully."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": session_id, "model": model},
        {"type": "user", "session_id": session_id, "message": {"role": "user", "content": "Do the task."}},
        {"type": "assistant", "session_id": session_id,
         "message": {"role": "assistant", "model": model,
         "content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": session_id, "result": "ok"},
    ]
    return "\n".join(json.dumps(l) for l in lines)


def _fake_claude_runner(stdout):
    def runner_fn(prompt, workdir, timeout_s):
        return _fake_completed(returncode=0, stdout=stdout)
    return runner_fn


def _fake_verifier_runner(returncode, stdout="", stderr=""):
    def runner_fn(verifier_path, workdir, timeout_s):
        return _fake_completed(returncode=returncode, stdout=stdout, stderr=stderr)
    return runner_fn


def test_task_set_loads_from_json(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "configuration_id": "cfg-a",
        "tasks": [{"task_id": "t1", "prompt": "fix the bug", "verifier": "./verify.sh"}],
    }), encoding="utf-8")
    ts = runner.TaskSet.load(path)
    assert ts.configuration_id == "cfg-a"
    assert len(ts.tasks) == 1
    assert ts.tasks[0].task_id == "t1"
    assert ts.tasks[0].timeout_s == 1800  # default


def test_task_set_missing_required_field_raises(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        runner.TaskSet.load(path)


def test_run_task_ingests_a_passing_task(tmp_path):
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-1")),
        verifier_runner=_fake_verifier_runner(0),
    )
    assert result.error is None
    assert result.ingested is True
    assert result.verifier_status == "passed"
    assert result.run_id is not None
    # Really landed in the store under the right configuration.
    from agr import read
    rows = read.list_runs(store)
    assert len(rows) == 1
    assert rows[0]["run_id"] == result.run_id


def test_run_task_records_a_failing_verifier(tmp_path):
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-2")),
        verifier_runner=_fake_verifier_runner(1, stderr="assertion failed"),
    )
    assert result.verifier_status == "failed"
    assert result.ingested is True


def test_run_task_workdir_is_cleaned_up_by_default(tmp_path):
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-3")),
        verifier_runner=_fake_verifier_runner(0),
    )
    assert not Path(result.workdir).exists()


def test_run_task_keep_workdir_preserves_it(tmp_path):
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-4")),
        verifier_runner=_fake_verifier_runner(0),
        keep_workdir=True,
    )
    assert Path(result.workdir).exists()
    assert (Path(result.workdir) / "session.stream.jsonl").exists()


def test_run_task_survives_a_crashing_claude_runner(tmp_path):
    """A scheduled sweep must survive one task's crash — never propagate
    the exception out of run_task."""
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")

    def crashing_runner(prompt, workdir, timeout_s):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1800)

    result = runner.run_task(task, store, "cfg-a", claude_runner=crashing_runner)
    assert result.error is not None
    assert result.ingested is False
    assert result.run_id is None


def test_run_task_timeout_preserves_workdir_and_partial_output(tmp_path):
    """A timeout is exactly when a human needs the evidence: the workdir must
    survive, and any output subprocess captured before the deadline must be
    written out rather than discarded with the rest of the crash."""
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh")

    def timing_out_runner(prompt, workdir, timeout_s):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1800, output="partial output before deadline")

    result = runner.run_task(task, store, "cfg-a", claude_runner=timing_out_runner)
    assert result.error is not None
    assert Path(result.workdir).exists()
    session_path = Path(result.workdir) / "session.stream.jsonl"
    assert session_path.exists()
    assert session_path.read_text(encoding="utf-8") == "partial output before deadline"


def test_run_task_setup_failure_preserves_workdir(tmp_path):
    """A failed setup command leaves diagnostic output in the workdir (e.g.
    stderr captured by the shell); it must not be deleted out from under it."""
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh", setup=["exit 1"])
    result = runner.run_task(task, store, "cfg-a")
    assert result.error is not None
    assert Path(result.workdir).exists()


def test_run_task_stores_the_actual_task_prompt_as_the_instruction(tmp_path):
    """The runner always knows the exact prompt it sent — it must not rely on
    the transcript echoing it back (claude -p's stream-json output does not
    reliably include a user event for the initiating prompt)."""
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="fix the flaky retry logic", verifier="./verify.sh")
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-instr")),
        verifier_runner=_fake_verifier_runner(0),
    )
    assert result.error is None
    capture_id = store.latest_capture_id(result.run_id)
    doc = store.read_source(result.run_id, capture_id)
    assert doc["task"]["instruction"] == "fix the flaky retry logic"


def test_run_task_survives_a_failed_setup_command(tmp_path):
    store = Store(str(tmp_path / "store"))
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh",
                       setup=["exit 1"])
    result = runner.run_task(
        task, store, "cfg-a",
        claude_runner=_fake_claude_runner(_stream_json_lines("sess-5")),
        verifier_runner=_fake_verifier_runner(0),
    )
    assert result.error is not None
    assert result.ingested is False


def test_setup_commands_run_before_the_prompt(tmp_path):
    store = Store(str(tmp_path / "store"))
    marker = tmp_path / "marker.txt"
    task = runner.Task(task_id="t1", prompt="do it", verifier="./verify.sh",
                       setup=[f"echo hello > {marker}"])
    seen = {}

    def claude_runner(prompt, workdir, timeout_s):
        seen["marker_exists_at_prompt_time"] = marker.exists()
        return _fake_completed(returncode=0, stdout=_stream_json_lines("sess-6"))

    runner.run_task(task, store, "cfg-a", claude_runner=claude_runner,
                    verifier_runner=_fake_verifier_runner(0))
    assert seen["marker_exists_at_prompt_time"] is True


def test_run_sweep_runs_every_task_and_uses_the_sets_configuration_id(tmp_path):
    store = Store(str(tmp_path / "store"))
    ts = runner.TaskSet(configuration_id="cfg-sweep", tasks=[
        runner.Task(task_id="t1", prompt="p1", verifier="./v.sh"),
        runner.Task(task_id="t2", prompt="p2", verifier="./v.sh"),
    ])
    call_count = {"n": 0}

    def claude_runner(prompt, workdir, timeout_s):
        call_count["n"] += 1
        return _fake_completed(returncode=0, stdout=_stream_json_lines(f"sess-{call_count['n']}"))

    results = runner.run_sweep(ts, store, claude_runner=claude_runner,
                              verifier_runner=_fake_verifier_runner(0))
    assert len(results) == 2
    assert all(r.ingested for r in results)
    from agr import read
    rows = read.list_runs(store)
    assert len(rows) == 2
    for r in results:
        rs = store.read_derived(r.run_id, store.latest_capture_id(r.run_id), "run_source.json")
        assert rs["configuration_id"] == "cfg-sweep"


def test_cli_run_sweep_subcommand(tmp_path, monkeypatch):
    """The CLI wrapper never invokes a real `claude` binary or verifier
    script in tests — it monkeypatches the MODULE-LEVEL default runners
    (looked up by name at call time, not bound as parameter defaults) so
    `agr run-sweep` exercises the real CLI/argparse wiring end to end."""
    from agr.cli import main

    def fake_claude(prompt, workdir, timeout_s):
        return _fake_completed(returncode=0, stdout=_stream_json_lines("sess-cli"))

    def fake_verifier(verifier_path, workdir, timeout_s):
        return _fake_completed(returncode=0)

    monkeypatch.setattr(runner, "default_claude_runner", fake_claude)
    monkeypatch.setattr(runner, "default_verifier_runner", fake_verifier)

    task_set_path = tmp_path / "tasks.json"
    task_set_path.write_text(json.dumps({
        "configuration_id": "cfg-cli",
        "tasks": [{"task_id": "t1", "prompt": "do it", "verifier": "./v.sh"}],
    }), encoding="utf-8")

    rc = main(["--store", str(tmp_path / "store"), "run-sweep", str(task_set_path)])
    assert rc == 0
    from agr import read
    from agr.store import Store as _Store
    store = _Store(str(tmp_path / "store"))
    rows = read.list_runs(store)
    assert len(rows) == 1


def test_cli_run_sweep_reports_nonzero_on_a_bad_task_set(tmp_path):
    from agr.cli import main
    task_set_path = tmp_path / "tasks.json"
    task_set_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    rc = main(["--store", str(tmp_path / "store"), "run-sweep", str(task_set_path)])
    assert rc == 1


def test_one_crashing_task_does_not_abort_the_rest_of_the_sweep(tmp_path):
    store = Store(str(tmp_path / "store"))
    ts = runner.TaskSet(configuration_id="cfg-sweep", tasks=[
        runner.Task(task_id="t-bad", prompt="p1", verifier="./v.sh"),
        runner.Task(task_id="t-good", prompt="p2", verifier="./v.sh"),
    ])
    call_count = {"n": 0}

    def flaky_runner(prompt, workdir, timeout_s):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return _fake_completed(returncode=0, stdout=_stream_json_lines("sess-ok"))

    results = runner.run_sweep(ts, store, claude_runner=flaky_runner,
                              verifier_runner=_fake_verifier_runner(0))
    assert len(results) == 2
    assert results[0].error is not None and not results[0].ingested
    assert results[1].error is None and results[1].ingested
