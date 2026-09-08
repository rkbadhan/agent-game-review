"""Runner harness for scheduled ``claude -p`` sweeps (item 26, 2026-09-08).

Runs a fixed task set (prompt + fresh workdir + verifier script) through
``claude -p ... --output-format stream-json --verbose``, verifies the
result, and ingests it under a ``configuration_id`` — so a store accumulates
comparable runs across scheduled invocations (a cron job, a systemd timer)
instead of one-off manual ingestion.

This module is pure orchestration. Subprocess invocation is isolated behind
the ``claude_runner``/``verifier_runner`` callables so tests exercise the
REAL control flow (task-set parsing, workdir isolation, sidecar
construction, per-task error handling, ingestion) against a FAKE runner —
never a live ``claude -p`` process, which would spend real API tokens on
every test run. Wiring an actual scheduled sweep means calling
:func:`run_sweep` with the default runners (real subprocesses) from a cron
job or systemd timer; this module never schedules itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

RUNNER_VERSION = "runner-0.1"


@dataclass
class Task:
    """One task in the set: a prompt, a verifier script, and an optional
    shell setup run in the fresh workdir before the prompt."""

    task_id: str
    prompt: str
    verifier: str
    setup: list[str] = field(default_factory=list)
    timeout_s: int = 1800


@dataclass
class TaskSet:
    """A named, versioned collection of tasks run under ONE configuration —
    the whole point of item 26 is that repeated sweeps of the SAME task set
    accumulate comparable runs in the store."""

    configuration_id: str
    tasks: list[Task] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "TaskSet":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "configuration_id" not in data or "tasks" not in data:
            raise ValueError(
                "task set must be a JSON object with 'configuration_id' and 'tasks'"
            )
        tasks = [Task(**t) for t in data["tasks"]]
        return cls(configuration_id=str(data["configuration_id"]), tasks=tasks)


@dataclass
class TaskRunResult:
    """The outcome of running ONE task end to end. Never raises out of
    :func:`run_task` — a crashed task becomes a result with ``error`` set,
    so one bad task never aborts the rest of a scheduled sweep."""

    task_id: str
    run_id: Optional[str] = None
    ingested: bool = False
    verifier_status: Optional[str] = None
    error: Optional[str] = None
    workdir: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "run_id": self.run_id, "ingested": self.ingested,
            "verifier_status": self.verifier_status, "error": self.error, "workdir": self.workdir,
        }


# The subprocess boundary: swap these out in tests for a fake that returns a
# canned CompletedProcess instead of shelling out to a real `claude` binary.
ClaudeRunner = Callable[[str, Path, int], "subprocess.CompletedProcess[str]"]
VerifierRunner = Callable[[str, Path, int], "subprocess.CompletedProcess[str]"]


def default_claude_runner(prompt: str, workdir: Path, timeout_s: int) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"],
        cwd=str(workdir), capture_output=True, text=True, timeout=timeout_s,
    )


def default_verifier_runner(verifier_path: str, workdir: Path, timeout_s: int) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [verifier_path], cwd=str(workdir), capture_output=True, text=True, timeout=timeout_s,
    )


def _verifier_sidecar_from_result(proc: "subprocess.CompletedProcess[str]") -> dict:
    """A verifier script's exit code IS its native structured signal (0 =
    passed, nonzero = failed) — no output parsing, no guessing at intent."""
    status = "passed" if proc.returncode == 0 else "failed"
    return {
        "raw_output": (proc.stdout or "") + (proc.stderr or ""),
        "checks": [{
            "check_id": "verifier_script",
            "name": "verifier script exit code",
            "status": status,
            "source": "native_structured",
            "timing": "post_run",
        }],
    }


def run_task(
    task: Task,
    store: Any,
    configuration_id: str,
    *,
    claude_runner: Optional[ClaudeRunner] = None,
    verifier_runner: Optional[VerifierRunner] = None,
    keep_workdir: bool = False,
) -> TaskRunResult:
    """Run ONE task end to end: fresh workdir, ``claude -p`` capture,
    verifier script, sidecar, ingest under ``configuration_id``.

    ``claude_runner``/``verifier_runner`` default to the real subprocess
    runners, looked up by module-level NAME at call time (not bound as
    parameter defaults) so a caller — a test, a CLI wrapper — can swap the
    module-level default without every ``run_task`` call site needing to
    pass it explicitly.
    """
    claude_runner = claude_runner if claude_runner is not None else default_claude_runner
    verifier_runner = verifier_runner if verifier_runner is not None else default_verifier_runner
    workdir = Path(tempfile.mkdtemp(prefix=f"agr-runner-{task.task_id}-"))
    try:
        for cmd in task.setup:
            subprocess.run(cmd, shell=True, cwd=str(workdir), timeout=task.timeout_s, check=True)

        proc = claude_runner(task.prompt, workdir, task.timeout_s)
        session_path = workdir / "session.stream.jsonl"
        session_path.write_text(proc.stdout or "", encoding="utf-8")

        verifier_proc = verifier_runner(task.verifier, workdir, task.timeout_s)
        sidecar = _verifier_sidecar_from_result(verifier_proc)

        from .adapter import get_adapter
        from .cli import _ingest_doc

        adapter = get_adapter("claude")
        result = adapter.convert(
            str(session_path), task_id=task.task_id, verifier=sidecar,
            configuration_id=configuration_id,
        )
        rc = _ingest_doc(result.doc, store)
        run_id = (result.doc.get("run") or {}).get("logical_run_id")
        return TaskRunResult(
            task_id=task.task_id, run_id=run_id, ingested=(rc == 0),
            verifier_status=sidecar["checks"][0]["status"], workdir=str(workdir),
        )
    except Exception as exc:  # noqa: BLE001 — a scheduled sweep must survive one task's crash
        return TaskRunResult(
            task_id=task.task_id, error=f"{type(exc).__name__}: {exc}", workdir=str(workdir),
        )
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def run_sweep(task_set: TaskSet, store: Any, **kwargs: Any) -> list[TaskRunResult]:
    """Run every task in the set, ingesting each into ``store`` under the
    set's ``configuration_id``. One sweep's worth of a scheduled run."""
    return [run_task(t, store, task_set.configuration_id, **kwargs) for t in task_set.tasks]
