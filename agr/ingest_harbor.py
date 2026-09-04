"""Harbor adapter — Terminal-Bench 2.0 / Harbor runs → ATIF (spec §5.3).

Harbor (https://www.harborframework.com) is the official harness for
Terminal-Bench 2.0. A ``harbor run`` writes one directory per trial:

    <trial_dir>/
        config.json          trial configuration
        result.json          TrialResult: reward, exception, timings, ...
        agent/
            trajectory.json   the agent trajectory in ATIF-v1.7
            recording.cast    asciinema recording (not read here)
        verifier/             verifier outputs

This adapter converts one such trial into the minimal ATIF-shaped document
``agr`` ingests (``docs/atif-schema.json``). Two honest mappings:

* **Trajectory** — Harbor's ATIF-v1.7 groups a whole turn (reasoning + message
  + several tool calls + their observation) into one ``step``. ``agr``'s event
  timeline wants finer-grained *kinded* steps, so each Harbor turn is fanned out
  into ordered ``model_output`` / ``tool_call`` / ``tool_result`` steps. Nothing
  is invented; the fan-out only re-shapes what the turn already contains.
* **Verifier** — Terminal-Bench's pass/fail lives in ``result.json`` as a
  numeric ``reward``. When the caller does not pass an explicit ``--verifier``
  sidecar, this adapter synthesises one from that reward (and any recorded
  exception), so a Harbor trial ingests with real verifier evidence out of the
  box. An explicit sidecar always wins; a trial with neither ingests honestly as
  UNVERIFIED, never a vacuous pass.

This is a pure format mapping: no analysis, no model calls, nothing invented
that the source did not capture. Every conservative choice is recorded as a
warning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapter import AdapterResult

# Provenance stamp written into every document this adapter emits (and recorded
# on the immutable capture). Bump when the mapping changes materially.
# 0.3: final_submission is no longer synthesised unconditionally — a harness-
#      terminated trial (exception_info present, e.g. AgentTimeoutError) or a
#      trajectory with zero agent steps gets only run_finished. The old 0.2
#      behaviour made the unresolved_requirement_at_submission detector anchor
#      on events the agent never authored (timeout/crash), misattributing
#      harness outcomes to the agent.
# 0.4: full event semantics. final_submission is emitted ONLY when a submission
#      is directly observed in the trajectory (mini-swe-agent's terminal
#      COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT echo, terminus-2's
#      mark_task_complete call). Otherwise the harness termination is recorded
#      as run_completed / run_timed_out / run_failed. Every adapter-written
#      step carries provenance="synthetic"; source-fanned-out steps carry
#      provenance="observed". A normal-looking end without an observed
#      submission is run_completed, never an agent submission.
HARBOR_ADAPTER_VERSION = "harbor-adapter-0.5"

# Harbor ATIF top-level source labels (Trajectory.steps[].source).
_HARBOR_SOURCES = {"user", "agent", "system"}


def _text_of(content: Any) -> str:
    """Flatten an ATIF message (string or ``ContentPart`` list) to plain text.

    ATIF-v1.6+ messages and observations may be multimodal: a list of parts,
    each ``{"type": "text"|"image"|..., ...}``. Images carry no textual
    trajectory content, so they are marked but not inlined.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype in (None, "text"):
                    parts.append(part.get("text", ""))
                elif ptype == "image" or ptype == "image_url":
                    parts.append("[image]")
                else:
                    # Unknown part type: keep any text field, else mark it.
                    parts.append(part.get("text") or f"[{ptype}]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return str(content)


def _hoist_argument(function_name: str, arguments: Any) -> dict[str, Any]:
    """Pick the analysis-relevant text out of a tool call's arguments.

    Deterministic evidence slicing matches on ``content``, so the most telling
    argument (a command, a path) is hoisted into ``content``; everything else is
    preserved as a compact JSON blob. Mirrors the pi adapter's choice so the two
    harnesses produce comparable steps.
    """
    call: dict[str, Any] = {"tool": function_name or "tool"}
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "path", "file_path", "filename"):
            val = arguments.get(key)
            if isinstance(val, str) and val:
                call["content"] = val
                if key in ("path", "file_path", "filename"):
                    call["path"] = val
                break
        else:
            call["content"] = json.dumps(arguments, ensure_ascii=False)[:2000]
    elif isinstance(arguments, str):
        call["content"] = arguments[:2000]
    else:
        call["content"] = json.dumps(arguments, ensure_ascii=False)[:2000]
    return call


def _trial_markers(p: Path) -> bool:
    """Does this directory look like one Harbor *reviewable* trial?

    Requires a trajectory — a bare ``result.json`` proves nothing is reviewable
    (an errored trial, or a job directory's roll-up result, which also carries a
    top-level ``result.json`` and must not be mistaken for a trial).
    """
    return (
        (p / "agent" / "trajectory.json").exists()
        or (p / "trajectory.json").exists()
    )


def iter_trials(source: str | Path) -> list[Path]:
    """Resolve a Harbor path to the list of trial directories it contains.

    Accepts one trial directory, a ``trajectory.json`` file, a Harbor **job
    directory** (what ``harbor run`` writes: one subdirectory per trial), or a
    **directory of job directories** (e.g. a published corpus root). The job-dir
    cases are what make batch review of an eval sweep one command.
    Raises ``ValueError`` with the searched layout when nothing is found.
    """
    p = Path(source)
    if p.is_file():
        return [p.parent if p.name == "result.json" else p]
    if p.is_dir():
        if _trial_markers(p):
            return [p]
        trials = sorted(child for child in p.iterdir() if child.is_dir() and _trial_markers(child))
        if trials:
            return trials
        # A directory *of* job directories (e.g. a committed corpus root):
        # descend one more level and collect each job's trials.
        nested = sorted(
            trial
            for job in p.iterdir()
            if job.is_dir()
            for trial in job.iterdir()
            if trial.is_dir() and _trial_markers(trial)
        )
        if nested:
            return nested
        raise ValueError(
            f"no Harbor trial with a reviewable trajectory found under {p}: "
            f"expected trial directories containing agent/trajectory.json (or "
            f"trajectory.json). Note: oracle runs record no trajectory."
        )
    raise ValueError(f"no such Harbor source: {p}")


def _resolve_paths(source: str | Path) -> tuple[Path, Path | None, list[str]]:
    """Resolve one Harbor trial to (trajectory.json, result.json | None).

    ``source`` may be a trial directory or a path straight to a
    ``trajectory.json``. The result sidecar is looked for only in the layout
    implied by what was given — ``<trial>/agent/trajectory.json`` implies
    ``<trial>/result.json``, and a flat ``<trial>/trajectory.json`` implies its
    own directory — never by reaching upward past the trial boundary, so a flat
    layout cannot pick up some other trial's result. Absences are reported,
    not guessed.
    """
    warnings: list[str] = []
    p = Path(source)
    if p.is_dir():
        traj = p / "agent" / "trajectory.json"
        if not traj.exists():
            traj = p / "trajectory.json"  # flatter layouts
        result = p / "result.json"
    else:
        traj = p
        if traj.parent.name == "agent":
            # <trial>/agent/trajectory.json -> <trial>/result.json
            result = traj.parent.parent / "result.json"
        else:
            # flat layout: the trajectory's own directory is the trial dir
            result = traj.parent / "result.json"
    if not traj.exists():
        raise ValueError(f"no Harbor trajectory found at {traj}")
    if result is not None and not result.exists():
        warnings.append(
            f"no result.json beside {Path(source).name}: reward-based verifier not synthesised"
        )
        result = None
    return traj, result, warnings


def _read_result(result_path: Path) -> dict:
    """Read a Harbor ``result.json`` (any known layout) into a dict.

    Read as ``utf-8-sig`` so a byte-order mark — common in logs written by
    Windows tooling — is tolerated rather than rejected as invalid JSON.
    """
    try:
        data = json.loads(result_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{result_path.name}: invalid JSON ({exc})") from exc
    return data if isinstance(data, dict) else {}


def _reward_and_exception(data: dict) -> tuple[Any, Any]:
    """Extract (reward, exception) from a Harbor result dict.

    Harbor has written this in two shapes: an older flat one
    (``{"reward": ..., "exception": ...}``) and the current nested one
    (``{"verifier_result": {"rewards": {"reward": ...}, "exception_info": ...}}``).
    Both are read; the caller supplies neither, so nothing is invented here.
    """
    reward = data.get("reward")
    exc = data.get("exception") or data.get("exception_info")

    verifier_result = data.get("verifier_result")
    if isinstance(verifier_result, dict):
        rewards = verifier_result.get("rewards")
        if reward is None and isinstance(rewards, dict):
            reward = rewards.get("reward")
        exc = exc or verifier_result.get("exception_info") or verifier_result.get("exception")
    return reward, exc


def _task_name(data: dict) -> str | None:
    """The task identity Harbor itself recorded on the trial, if any.

    Reads ``task_name``/``task_id`` outright, then falls back to the task
    segment of ``trial_name``, which Harbor composes as ``task__agent__attempt``
    — still source-supplied structure, never invented.
    """
    name = data.get("task_name") or data.get("task_id")
    if name:
        return str(name)
    trial = data.get("trial_name")
    if isinstance(trial, str) and "__" in trial:
        task = trial.split("__", 1)[0]
        return task or None
    return None


def _verifier_from_result(data: dict, source_name: str) -> tuple[dict, list[str]]:
    """Synthesise a verifier sidecar from a Harbor ``result.json`` reward.

    Terminal-Bench rewards are the native pass/fail signal. The mapping is
    deliberately conservative: a reward at or above 1.0 is a pass, a numeric
    reward below it is a fail, a recorded exception with no reward is an error,
    and no reward at all is ``unknown`` — never silently a pass. The numeric
    reward and any exception are surfaced in ``raw_output`` for transparency.
    """
    warnings: list[str] = []
    reward, exc = _reward_and_exception(data)

    if isinstance(reward, bool):  # bool is a subclass of int — treat as pass/fail
        status = "passed" if reward else "failed"
    elif isinstance(reward, (int, float)):
        status = "passed" if reward >= 1.0 else "failed"
        if 0.0 < reward < 1.0:
            warnings.append(
                f"partial reward {reward} recorded as failed (Terminal-Bench pass is reward>=1.0)"
            )
    elif exc:
        status = "error"
    else:
        status = "unknown"
        warnings.append(f"{source_name} has no numeric reward: verifier status is 'unknown'")

    raw = {"reward": reward}
    if exc:
        raw["exception"] = exc
    check = {
        "check_id": "terminal_bench_reward",
        "name": "Terminal-Bench task reward",
        "status": status,
        "source": "native_structured",
    }
    return {"raw_output": json.dumps(raw, ensure_ascii=False), "checks": [check]}, warnings


def convert(
    source: str | Path,
    *,
    task_id: str | None = None,
    instruction: str | None = None,
    run_id: str | None = None,
    verifier: dict | None = None,
    sweep_id: str | None = None,
    configuration_id: str | None = None,
) -> AdapterResult:
    """Convert one Harbor trial into an ATIF-shaped document.

    Pure function: reads the trial's ``trajectory.json`` (and, when no explicit
    verifier is supplied, its ``result.json``) and returns the document.
    """
    traj_path, result_path, warnings = _resolve_paths(source)
    traj = json.loads(traj_path.read_text(encoding="utf-8-sig"))
    result_data: dict = _read_result(result_path) if result_path is not None else {}

    schema_version = str(traj.get("schema_version") or traj.get("atif_version") or "ATIF-unknown")
    if schema_version == "ATIF-unknown":
        warnings.append("trajectory has no schema_version; recorded as 'ATIF-unknown'")
    # The core renders atif_version as "ATIF-v{atif_version}", so store the bare
    # version and keep the source's verbatim token in harness_version below —
    # Harbor's native "ATIF-v1.7" must not double-prefix to "ATIF-vATIF-v1.7".
    atif_version = schema_version
    for prefix in ("ATIF-v", "atif-v", "ATIF-", "v"):
        if atif_version.startswith(prefix):
            atif_version = atif_version[len(prefix):]
            break
    agent = traj.get("agent") or {}
    harbor_steps = traj.get("steps") or []
    if not isinstance(harbor_steps, list) or not harbor_steps:
        raise ValueError(f"{traj_path.name}: trajectory has no steps")

    steps: list[dict] = []
    seq = 0

    def add(kind: str, actor: str, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        # Source-fanned-out steps are observed; adapter-written terminal events
        # override with provenance="synthetic" at their call site.
        payload.setdefault("provenance", "observed")
        steps.append({"step_id": f"h{seq}", "kind": kind, "actor": actor, **payload})

    first_user_text: str | None = None
    model: str | None = agent.get("model_name")
    call_names: dict[str, str] = {}  # tool_call_id -> function_name, to name results
    saw_agent_step = False
    last_agent_tool_calls: list[dict] = []

    for hstep in harbor_steps:
        if not isinstance(hstep, dict):
            warnings.append("non-object step skipped, not dropped silently")
            continue
        src = hstep.get("source")
        model = hstep.get("model_name") or model

        if src == "user":
            text = _text_of(hstep.get("message"))
            if first_user_text is None:
                first_user_text = text
                add("task_received", "harness", content=text)
            else:
                add("environment_observation", "user", content=text)

        elif src == "system":
            add("environment_observation", "harness", content=_text_of(hstep.get("message")))

        elif src == "agent":
            saw_agent_step = True
            last_agent_tool_calls = [c for c in (hstep.get("tool_calls") or []) if isinstance(c, dict)]
            reasoning = hstep.get("reasoning_content")
            if reasoning:
                add("model_output", "main_agent", content=f"[thinking] {_text_of(reasoning)}")
            message = _text_of(hstep.get("message"))
            if message:
                add("model_output", "main_agent", content=message)
            for call in hstep.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fname = call.get("function_name") or call.get("name") or "tool"
                payload = _hoist_argument(fname, call.get("arguments"))
                add("tool_call", "main_agent", **payload)
                cid = call.get("tool_call_id") or call.get("id")
                if cid:
                    call_names[cid] = fname
            # The observation records the environment's response to this turn's
            # tool calls; each result becomes its own tool_result step.
            observation = hstep.get("observation") or {}
            for res in observation.get("results") or []:
                if not isinstance(res, dict):
                    continue
                cid = res.get("source_call_id")
                payload = {
                    "tool": call_names.get(cid, "tool"),
                    "content": _text_of(res.get("content")),
                }
                # ATIF observations don't carry a POSIX exit code; only record one
                # when the source explicitly surfaced it (via extra), never guess.
                extra = res.get("extra") or {}
                if isinstance(extra, dict) and "exit_code" in extra:
                    payload["exit_code"] = extra["exit_code"]
                elif isinstance(extra, dict) and extra.get("is_error"):
                    payload["exit_code"] = 1
                add("tool_result", "tool", **payload)
        else:
            warnings.append(
                f"unmapped Harbor step source {src!r}; step skipped, not dropped silently"
            )

    if not steps:
        raise ValueError(f"{traj_path.name}: trajectory produced no trajectory steps")

    # --- termination --------------------------------------------------------
    # Event semantics (adapter 0.4, revised 0.5): an agent submission is
    # recorded ONLY when directly observed in the trajectory — mini-swe-agent's
    # terminal COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT echo or a mark_task_complete
    # tool call as the final agent action. Everything else is harness-side:
    #   run_timed_out  deadline exception (e.g. AgentTimeoutError)
    #   run_failed     other recorded exception (e.g. NonZeroAgentExitCodeError),
    #                  or a zero-agent-step crash (Harbor scored the trial as
    #                  completed, e.g. mini-swe-agent RepeatedFormatError — but a
    #                  trial where the agent never acted is a protocol failure,
    #                  not a completion; labelling it run_completed repeats the
    #                  timeout-equals-submission ontology error at smaller scale)
    #   run_completed  harness ended the trial normally without an observed
    #                  submission (incl. iteration exhaustion with agent activity)
    # A normal-looking end is NEVER promoted to an agent submission: inventing
    # one makes downstream detectors attribute harness outcomes to the agent.
    _, exc = _reward_and_exception(result_data)
    exc_type = exc.get("exception_type") or exc.get("type") if isinstance(exc, dict) else exc

    def _observed_submission(calls: list[dict]) -> bool:
        if not calls:
            return False
        for c in calls:
            fname = (c.get("function_name") or c.get("name") or "").lower()
            args = c.get("arguments")
            args_text = json.dumps(args, ensure_ascii=False) if args is not None else ""
            if fname == "mark_task_complete":
                return True
            if "complete_task_and_submit_final_output" in args_text.lower():
                return True
        return False

    if _observed_submission(last_agent_tool_calls):
        add("final_submission", "main_agent", provenance="observed",
            content="[Observed agent submission signal in final agent action]")
    elif exc_type and "timeout" in str(exc_type).lower():
        add("run_timed_out", "harness", provenance="synthetic",
            content=f"[Harbor trial hit its deadline: {exc_type}]")
    elif exc_type:
        add("run_failed", "harness", provenance="synthetic",
            content=f"[Harbor trial ended via {exc_type}]")
    elif not saw_agent_step:
        add("run_failed", "harness", provenance="synthetic",
            content="[Harbor trial closed — trajectory contains no agent steps; the agent never acted]",
            termination_reason="agent_protocol_failure")
    else:
        add("run_completed", "harness", provenance="synthetic",
            content="[Harbor trial closed normally — no observed submission signal]")

    # --- run metadata -------------------------------------------------------
    # Task identity comes from the caller, else from what Harbor recorded on
    # the trial (result.json task_name), else the trajectory id — in that
    # honesty order. The fallbacks are source-supplied, never invented.
    traj_id = str(traj.get("trajectory_id") or traj.get("session_id") or traj_path.stem)
    derived_task_id = task_id or _task_name(result_data) or f"harbor-trial-{traj_id[:12]}"
    if task_id is None and _task_name(result_data):
        warnings.append(f"task_id taken from result.json ({derived_task_id}); confirm the contract")
    resolved_run_id = run_id or f"harbor__{derived_task_id}__{traj_id[:12]}"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": derived_task_id,
        "model": model or "unresolved",
        "agent": agent.get("name") or "harbor-agent",
        "harness_version": f"harbor/{schema_version}",
    }
    agent_version = agent.get("version")
    if agent_version:
        run["agent"] = f"{run['agent']}@{agent_version}"
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id

    # --- capabilities: only what Harbor genuinely captured ------------------
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "complete",
        # Harbor observes the filesystem/processes only through tool I/O, never
        # as captured state, and the asciinema recording isn't parsed here.
        "filesystem": "partial",
        "process_state": "partial",
    }

    # --- verifier: explicit sidecar wins; else synthesise from result.json --
    synth_warnings: list[str] = []
    if verifier is None and result_path is not None:
        verifier, synth_warnings = _verifier_from_result(result_data, result_path.name)
    warnings.extend(synth_warnings)
    if verifier:
        capabilities["verifier_code"] = "complete"

    resolved_instruction = instruction if instruction is not None else (first_user_text or "")
    if instruction is None and first_user_text:
        warnings.append("task instruction taken from first user step; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": atif_version,
        "source_type": "harbor",
        "adapter_version": HARBOR_ADAPTER_VERSION,
        "capture_completeness": "complete" if verifier else "partial",
        "run": run,
        "capabilities": capabilities,
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": steps,
    }
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append("no verifier supplied and no result.json reward: run ingests UNVERIFIED (§7.2)")

    return AdapterResult(
        doc=doc,
        warnings=warnings,
        meta={"harbor_steps": len(harbor_steps), "derived_steps": len(steps)},
    )


class HarborAdapter:
    """The Harbor / Terminal-Bench 2.0 adapter, exposed through the shared
    ``Adapter`` protocol (spec §5.3). A thin object over the pure ``convert``
    function so the CLI can dispatch to it by name via the registry."""

    name = "harbor"
    version = HARBOR_ADAPTER_VERSION

    def convert(
        self,
        source: Any,
        *,
        task_id: str | None = None,
        instruction: str | None = None,
        run_id: str | None = None,
        verifier: dict | None = None,
        sweep_id: str | None = None,
        configuration_id: str | None = None,
    ) -> AdapterResult:
        return convert(
            source,
            task_id=task_id,
            instruction=instruction,
            run_id=run_id,
            verifier=verifier,
            sweep_id=sweep_id,
            configuration_id=configuration_id,
        )


HARBOR_ADAPTER = HarborAdapter()
