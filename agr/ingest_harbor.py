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
* **tau3-bench** — its verifier writes ``verifier/result.json`` with a full tau2
  ``reward_info`` breakdown (``db_check``, ``action_checks``, ``nl_assertions``,
  ``env_assertions``, ``communicate_checks``, ``reward_basis``,
  ``reward_breakdown``) behind one aggregate reward. Harbor's own ``result.json``
  keeps only the reward, so this adapter reads the sidecar and attaches the
  breakdown as ``diagnostic_evidence`` on the ONE aggregate check — never as
  separate checks, which would inflate every failed-check count. The tau3 agent
  also writes no ATIF ``agent/trajectory.json``, so the adapter synthesises one
  from ``agent/tau3_runtime_state.json`` (see ``_tau3_trajectory``).

This is a pure format mapping: no analysis, no model calls, nothing invented
that the source did not capture. Every conservative choice is recorded as a
warning.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .adapter import AdapterResult
from .execution_quality import generation_usage_capability
from .schema import CHECK_STATUSES

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
# 0.5: execution identity repair (AGR-02). A trial's logical run id is now
#      derived from the full Harbor trial UUID when result.json supplies one,
#      else from a hash of the COMPLETE namespaced execution identity (task id
#      + full session id) — never from the first 12 characters of a
#      task-prefixed session string, which collided across attempts of the
#      same task ("nginx-request-logging__vTNAJM8__agent" and its siblings all
#      truncated to "nginx-reque") and merged distinct executions into one
#      logical run whose capture revisions silently overwrote each other in
#      latest-capture reads. Distinct executions now always get distinct run
#      ids; capture revisions are reserved for re-recordings/reprocessing of
#      the SAME execution. The trial uuid/name are preserved on the run for
#      lineage. Bump requires re-ingest: run ids change for every trial whose
#      session id shares a 12-char prefix with a sibling's.
# 0.8: mini-swe-agent observations encode each result's exit status as a JSON
#      object (``{"returncode": <int>, "output": ...}``) inside the ATIF
#      ``content`` field, because mini-swe-agent's own harness — not Harbor —
#      writes it that way. Nothing in Harbor's ``extra.exit_code`` slot ever
#      carried it, so every mini-swe-agent trial ingested with every tool
#      result at ``status: unknown`` and no failures for recovery/detectors to
#      see. This is a real, source-supplied exit code that was merely
#      JSON-encoded inside a text field — lifting it is decoding what the
#      source already recorded, not inference: it is stamped provenance
#      "observed" like the rest of the fanned-out step.
# 0.9 (AGR-03): the submission echo's own tool_result is tagged
#      submission_control_response when it is mini-swe-agent's harness
#      declining to execute the control command it just intercepted
#      (returncode -1, exception_info "action was not executed") — paired
#      strictly with the call being the submission action itself, so this
#      never suppresses an unrelated failure that happens to share the exit
#      code or message. Downstream (agr/_util.py:is_tool_failure) excludes it
#      from failure/recovery classification, the same way permission_denied
#      already is.
# 0.12: a tau3 trial without a Harbor result.json UUID now takes its synthesised
#      trajectory id from the trial directory name instead of the runtime
#      state's task_id (shared by every attempt of a task) or a constant, so
#      attempts no longer hash to one run id. Likewise its task id, absent a
#      caller/result.json name, now comes from the trial directory's task
#      segment, else the state's domain + task_id (``tau3-<domain>-<id>``), so
#      attempts of one task group under one task id instead of each getting a
#      ``harbor-trial-<dir>`` id. Bump requires re-ingest: task and run ids
#      change for such tau3 trials.
# 0.13: a tau3 verifier status of missing_runtime_log, invalid_runtime_log or
#      tau2_runtime_log_error now ingests as ``operational_error`` instead of
#      ``error``. All three are verifier/harness-side (the runtime log was never
#      written, was not valid JSON, or the tau2 evaluator raised), so the run
#      rolls up to OPERATIONAL_ERROR and leaves success denominators rather
#      than landing in UNDETERMINED. ``agent_error`` is unchanged: it is the
#      agent's failure and stays reward-decided. Bump requires re-ingest: the
#      check status and run outcome change for such tau3 trials.
HARBOR_ADAPTER_VERSION = "harbor-adapter-0.13"

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


def _parse_returncode(content: Any) -> int | None:
    """Decode a mini-swe-agent exit status out of a tool result's ``content``.

    mini-swe-agent's own harness serializes each command result as
    ``{"returncode": <int>, "output": <str>}`` — sometimes as a JSON string
    (the common case: Harbor's ATIF ``content`` field is text), occasionally
    already parsed into a dict. Recognised only when ``returncode`` is
    genuinely an int (bool is a subclass of int and is excluded — it is never
    what this field means); anything else returns ``None`` so the caller falls
    through to "no exit code recorded", never a guessed one.
    """
    obj: Any = None
    if isinstance(content, dict):
        obj = content
    elif isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                obj = parsed
    if obj is None:
        return None
    rc = obj.get("returncode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        return None
    return rc


def _is_submission_call(function_name: Any, arguments: Any) -> bool:
    """Is this call the agent's own submission-protocol action?

    The literal control actions the submission protocol uses: mini-swe-agent's
    ``echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` or terminus-2's
    ``mark_task_complete``. A single definition shared by
    :func:`_observed_submission` (decides whether the run gets a
    ``final_submission`` event) and the submission-control-response tagging in
    :func:`convert` (AGR-03), so the two can never drift apart.
    """
    fname = str(function_name or "").lower()
    if fname == "mark_task_complete":
        return True
    args_text = json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""
    return "complete_task_and_submit_final_output" in args_text.lower()


# AGR-03: mini-swe-agent's own harness intercepts its submission echo as a
# control signal and deliberately never executes it, then reports that fact
# honestly through the same channel a real command failure would use. Matched
# as an exact, closed sentinel — never a substring of arbitrary output — and
# only ever consulted together with _is_submission_call on the SAME call, so
# an unrelated command that happens to return this exact message is untouched.
_SUBMISSION_NON_EXECUTION_MESSAGE = "action was not executed"


def _is_submission_non_execution_response(content: Any) -> bool:
    """Is this the harness's "I did not run your submission command" reply?

    Decodes the same ``{"returncode": ..., "exception_info": ...}`` shape
    :func:`_parse_returncode` reads (mini-swe-agent's own harness serialises a
    result this way), and recognises it only by the literal
    ``exception_info`` sentinel above — never by ``returncode`` alone (a
    genuine failure is also often ``-1``) and never by the message alone
    outside that structured shape.
    """
    obj: Any = None
    if isinstance(content, dict):
        obj = content
    elif isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return False
    exc = obj.get("exception_info")
    return isinstance(exc, str) and exc.strip().lower() == _SUBMISSION_NON_EXECUTION_MESSAGE


def _hoist_argument(function_name: str, arguments: Any) -> dict[str, Any]:
    """Pick the analysis-relevant text out of a tool call's arguments.

    Deterministic evidence slicing matches on ``content``, so the most telling
    argument (a command, a path) is hoisted into ``content``; everything else is
    preserved as a compact JSON blob. Mirrors the pi adapter's choice so the two
    harnesses produce comparable steps.

    Item 20 (2026-09-08): terminus-2's own tool call carries the actual shell
    text under ``keystrokes`` (a terminal-emulation harness, not a structured
    ``command`` argument) — without it in this priority list, the real
    command was buried inside the JSON-blob fallback and every deterministic
    matcher keyed on ``content`` (recovery's objective identity, evidence
    slicing) saw ``{"keystrokes":...`` instead of the command.
    """
    call: dict[str, Any] = {"tool": function_name or "tool"}
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "keystrokes", "path", "file_path", "filename"):
            val = arguments.get(key)
            if isinstance(val, str) and val:
                call["content"] = val.rstrip("\n") if key == "keystrokes" else val
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


# Item 20 (2026-09-08): a closed, UNAMBIGUOUS set of shell error messages a
# failed command's own shell prints verbatim — never a semantic read of
# arbitrary output. Used only for the OPTIONAL, clearly-labelled heuristic
# below; it never sets exit_code/status, so it can never be mistaken for
# genuinely captured process state.
_SHELL_ERROR_MARKERS = (
    "command not found",
    "No such file or directory",
    "Permission denied",
    "syntax error near unexpected token",
    "is not recognized as an internal or external command",
)


def _terminus2_heuristic_status(content: str) -> str | None:
    """A lower-confidence, clearly-labelled status source for terminus-2.

    Terminus-2 observations are raw terminal screen text with no exit code
    anywhere (``process_state`` is declared ``unavailable`` for it — see
    ``convert``). This mechanically recognises a closed set of shell error
    strings the shell itself would have printed verbatim on a failed command;
    returns ``None`` (no signal) for everything else, including any output
    that merely mentions a word like "error" — that would be interpreting
    content, not recognising a fixed marker.
    """
    return "error" if any(marker in content for marker in _SHELL_ERROR_MARKERS) else None


def _trial_markers(p: Path) -> bool:
    """Does this directory look like one Harbor *reviewable* trial?

    Requires a trajectory — a bare ``result.json`` proves nothing is reviewable
    (an errored trial, or a job directory's roll-up result, which also carries a
    top-level ``result.json`` and must not be mistaken for a trial).

    tau3-bench writes no ATIF ``trajectory.json``; its conversation lives in
    ``agent/tau3_runtime_state.json``, which the adapter synthesises a
    trajectory from. Accepting it here is what lets a tau3 job be batch-ingested.
    """
    return (
        (p / "agent" / "trajectory.json").exists()
        or (p / "trajectory.json").exists()
        or (p / "agent" / "tau3_runtime_state.json").exists()
        or (p / "tau3_runtime_state.json").exists()
    )


def iter_trials_detailed(source: str | Path) -> list[tuple[Path, str | None]]:
    """Like :func:`iter_trials`, but every candidate directory is accounted for.

    Returns ``(path, exclusion_reason)`` pairs: ``reason is None`` for trials
    that will be ingested, a human-readable reason for directories that were
    found but skipped (errored trials, bare job roll-ups, unrelated dirs).
    The reason is None only where a trial is actually reviewable, so a batch
    run can report "every discovered trial ingested or excluded with a
    reason" (AGR-02) instead of silently disappearing directories.
    """
    p = Path(source)
    if p.is_file():
        return [(p.parent if p.name == "result.json" else p, None)]
    if p.is_dir():
        if _trial_markers(p):
            return [(p, None)]
        accounted: list[tuple[Path, str | None]] = []
        for child in sorted(p.iterdir()):
            if not child.is_dir():
                continue
            if _trial_markers(child):
                accounted.append((child, None))
            else:
                accounted.append((child, "no reviewable trajectory (agent/trajectory.json or "
                                          "agent/tau3_runtime_state.json missing — errored trial "
                                          "or job roll-up)"))
        if any(reason is None for _, reason in accounted):
            return accounted
        # A directory *of* job directories (e.g. a committed corpus root):
        # descend one more level and collect each job's trials.
        nested: list[tuple[Path, str | None]] = []
        for job in sorted(p.iterdir()):
            if not job.is_dir():
                continue
            job_children = sorted(job.iterdir())
            if not job_children:
                nested.append((job, "empty job directory"))
                continue
            for trial in job_children:
                if not trial.is_dir():
                    continue
                if _trial_markers(trial):
                    nested.append((trial, None))
                else:
                    nested.append((trial, "no reviewable trajectory (agent/trajectory.json or "
                                              "agent/tau3_runtime_state.json missing — errored "
                                              "trial or job roll-up)"))
        if any(reason is None for _, reason in nested):
            # A job whose trials were collected is not itself an exclusion —
            # drop level-1 reasons for directories that turned out to be jobs.
            included_parents = {trial.parent for trial, reason in nested if reason is None}
            nested.extend((job, reason) for job, reason in accounted
                          if reason is not None and job not in included_parents)
            return nested
        if nested:
            return nested
        raise ValueError(
            f"no Harbor trial with a reviewable trajectory found under {p}: "
            f"expected trial directories containing agent/trajectory.json (or "
            f"agent/tau3_runtime_state.json for tau3-bench). Note: oracle runs "
            f"record no trajectory."
        )
    raise ValueError(f"no such Harbor source: {p}")


def iter_trials(source: str | Path) -> list[Path]:
    """Resolve a Harbor path to the list of trial directories it contains.

    Accepts one trial directory, a ``trajectory.json`` file, a Harbor **job
    directory** (what ``harbor run`` writes: one subdirectory per trial), or a
    **directory of job directories** (e.g. a published corpus root). The job-dir
    cases are what make batch review of an eval sweep one command.
    Raises ``ValueError`` with the searched layout when nothing is found.
    """
    return [path for path, reason in iter_trials_detailed(source) if reason is None]


def _resolve_paths(
    source: str | Path,
) -> tuple[Path | None, Path | None, list[str], Path | None]:
    """Resolve one Harbor trial to its trajectory, result and tau3 state.

    Returns ``(trajectory.json | None, result.json | None, warnings,
    tau3_runtime_state.json | None)``. ``source`` may be a trial directory or a
    path straight to a ``trajectory.json``. The result sidecar is looked for
    only in the layout implied by what was given — ``<trial>/agent/
    trajectory.json`` implies ``<trial>/result.json``, and a flat
    ``<trial>/trajectory.json`` implies its own directory — never by reaching
    upward past the trial boundary, so a flat layout cannot pick up some other
    trial's result. Absences are reported, not guessed.

    When no ``trajectory.json`` exists but a tau3 ``tau3_runtime_state.json``
    does, the trajectory is ``None`` and the tau3 state is returned so the
    caller can synthesise one (the tau3 agent never writes ATIF).
    """
    warnings: list[str] = []
    p = Path(source)
    if p.is_dir():
        trial_dir = p
        traj = p / "agent" / "trajectory.json"
        if not traj.exists():
            traj = p / "trajectory.json"  # flatter layouts
        result = p / "result.json"
    else:
        traj = p
        if traj.parent.name == "agent":
            # <trial>/agent/trajectory.json -> <trial>/result.json
            trial_dir = traj.parent.parent
        else:
            # flat layout: the trajectory's own directory is the trial dir
            trial_dir = traj.parent
        result = trial_dir / "result.json"

    # tau3-bench writes no ATIF trajectory.json: its recorded conversation is
    # the runtime server's log. Offer that as a fallback so the trial still
    # ingests, rather than failing on a file the tau3 agent never produces.
    tau3_state = trial_dir / "agent" / "tau3_runtime_state.json"
    if not tau3_state.is_file():
        tau3_state = trial_dir / "tau3_runtime_state.json"  # flatter layouts
    if not traj.exists():
        if tau3_state.is_file():
            traj = None
        else:
            raise ValueError(f"no Harbor trajectory found at {traj}")
    if result is not None and not result.exists():
        warnings.append(
            f"no result.json beside {Path(source).name}: reward-based verifier not synthesised"
        )
        result = None
    return traj, result, warnings, (tau3_state if traj is None else None)


def _execution_run_id(task_id: str, session_id: str, result_data: dict,
                      warnings: list[str]) -> str:
    """The logical run id for ONE Harbor execution (adapter 0.6, AGR-02).

    The full Harbor trial UUID is the primary identity: it is what the harness
    itself uses to distinguish attempts. Without one, the id derives from a
    hash of the COMPLETE namespaced identity (task id + full session id) —
    stable across machines and re-ingests, and collision-free where the old
    12-character prefix merged distinct attempts ("…vTNAJM8__agent",
    "…h6dA2go__agent" and "…rwPp8Q4__agent" all began "nginx-reque").

    A hash id is warned about so its provenance stays visible: without a UUID,
    two trials of the same task that honestly report the same session id are
    indistinguishable from the source, and the hash would merge them too —
    the warning tells the reader exactly what evidence identity rests on.
    """
    uuid = result_data.get("id")
    if uuid:
        return f"harbor__{task_id}__{uuid}"
    identity = f"{task_id}|{session_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    warnings.append(
        "run id derived from a hash of the full task+session identity "
        "(no Harbor trial UUID in result.json); distinct attempts that "
        "report identical session ids cannot be told apart from this source"
    )
    return f"harbor__{task_id}__{digest}"


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
        "name": "Terminal-Bench task reward (aggregate)",
        "status": status,
        "source": "native_structured",
        "timing": "post_run",
        "source_pointers": [source_name],
    }
    return {"raw_output": json.dumps(raw, ensure_ascii=False), "checks": [check]}, warnings


# AGR-04: cap on verifier log excerpts carried into the capture. Post-run
# verifier output is diagnostic evidence, not agent-visible context; keep it
# bounded and source-referenced either way.
_VERIFIER_LOG_CAP = 16_384


def _verifier_from_ctrf(trial_dir: Path, result_data: dict) -> tuple[dict | None, list[str]]:
    """Import ``verifier/ctrf.json`` test results as atomic checks (AGR-04).

    CTRF (Candidate Test Report Format) is what Terminal-Bench 2.0 verifiers
    write behind the aggregate reward: one record per test with an explicit
    status, file path, and timing. Each test becomes its own check so a
    single failing test is visible next to the passes — the aggregate reward
    stays the run outcome and is retained as an explicitly aggregate check.
    Statuses the source does not state are preserved as ``unknown``, never
    guessed into pass/fail.
    """
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    if not ctrf_path.is_file():
        return None, []
    try:
        ctrf = json.loads(ctrf_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, [f"verifier/ctrf.json unreadable ({exc}); falling back to the aggregate reward"]

    results = ctrf.get("results") if isinstance(ctrf, dict) else None
    tests = results.get("tests") if isinstance(results, dict) else None
    if not isinstance(tests, list) or not tests:
        return None, ["verifier/ctrf.json carries no test results; falling back to the aggregate reward"]

    warnings: list[str] = []
    checks: list[dict] = []
    for i, test in enumerate(tests):
        if not isinstance(test, dict) or not test.get("name"):
            warnings.append(f"ctrf test {i} has no name; skipped, not dropped silently")
            continue
        raw_status = test.get("status")
        status = raw_status if raw_status in CHECK_STATUSES else "unknown"
        if raw_status is not None and raw_status not in CHECK_STATUSES:
            warnings.append(
                f"ctrf test {test['name']!r}: unmapped status {raw_status!r} preserved as 'unknown'"
            )
        pointers = ["verifier/ctrf.json"]
        if test.get("file_path"):
            pointers.append(f"verifier/{test['file_path']}")
        checks.append({
            "check_id": str(test["name"]),
            "name": str(test["name"]),
            "status": status,
            "source": "native_structured",
            "timing": "post_run",
            "source_pointers": pointers,
        })

    # The aggregate reward remains the run outcome — keep it, clearly labelled
    # as an aggregate so per-test results and the overall verdict stay apart.
    reward_verifier, reward_warnings = _verifier_from_result(result_data, "result.json")
    warnings.extend(reward_warnings)
    if reward_verifier:
        checks.extend(reward_verifier["checks"])

    verifier: dict = {
        "raw_output": json.dumps({"ctrf_summary": results.get("summary")}, ensure_ascii=False)
        if isinstance(results, dict) else "",
        "checks": checks,
    }

    # Attach the verifier's own log output, capped and source-referenced.
    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    if stdout_path.is_file():
        try:
            text = stdout_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            warnings.append(f"verifier/test-stdout.txt unreadable ({exc})")
            text = ""
        if text:
            if len(text) > _VERIFIER_LOG_CAP:
                text = text[:_VERIFIER_LOG_CAP] + "\n[truncated]"
                warnings.append("verifier/test-stdout.txt truncated to 16384 chars")
            verifier["log_excerpts"] = [{
                "source": "verifier/test-stdout.txt",
                "content": text,
                "timing": "post_run",
            }]
    return verifier, warnings


# --- tau3-bench: full tau2 reward breakdown behind one aggregate reward -------
#
# The tau3 verifier (``tests/evaluate.py``) writes ``verifier/result.json``:
#
#     {"status": "passed"|"mismatch"|"missing_runtime_log"|...,
#      "reward": 1.0,
#      "used_tau2_evaluator": true,
#      "reward_info": {"reward": ..., "db_check": {...},
#                      "action_checks": [...], "nl_assertions": [...],
#                      "env_assertions": [...], "communicate_checks": [...],
#                      "reward_basis": [...], "reward_breakdown": {...}}}
#
# Harbor's own ``result.json`` carries only ``verifier_result.rewards.reward``;
# the breakdown is dropped there. Reading the sidecar lets a review see WHY an
# attempt failed (which DB field, which action, which assertion) rather than
# only that it did.
#
# The breakdown is attached as ``diagnostic_evidence`` on the single aggregate
# check, never as separate checks: outcome(), the detectors and the failure-mode
# counts all count every failed check, so per-db-field or per-action checks
# would inflate those counts and could flip a run's outcome. A test pins that
# adding or removing breakdown entries never changes the outcome.

_TAU3_VERIFIER_STATUS = {
    "passed": "passed",
    "mismatch": "failed",
    # Verifier/harness-side: the runtime log was never written, was not valid
    # JSON, or the tau2 evaluator raised. No valid verdict on the agent, so
    # these are operational errors, not agent failures and not "unknown".
    "missing_runtime_log": "operational_error",
    "invalid_runtime_log": "operational_error",
    "tau2_runtime_log_error": "operational_error",
}

# Mapped statuses that mean "the verifier itself did not produce a verdict".
# They outrank the reward (a 0.0 reward beside a broken verifier is not a
# demonstrated agent failure) and leave no status/reward disagreement to report.
_TAU3_NO_VERDICT_STATUSES = ("error", "operational_error")

_TAU3_REWARD_INFO_KEYS = (
    "db_check",
    "action_checks",
    "nl_assertions",
    "env_assertions",
    "communicate_checks",
    "reward_basis",
    "reward_breakdown",
)


def _verifier_from_tau3(trial_dir: Path, result_data: dict) -> tuple[dict | None, list[str]]:
    """Read a tau3 ``verifier/result.json`` into ONE aggregate check.

    Returns ``None`` when the trial has no such sidecar or it does not carry the
    tau3 shape, so the caller falls through to the Terminal-Bench reward.
    """
    path = trial_dir / "verifier" / "result.json"
    if not path.is_file():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, [
            f"verifier/result.json unreadable ({exc}); falling back to the aggregate reward"
        ]
    if not isinstance(payload, dict):
        return None, []

    reward_info = payload.get("reward_info")
    # ``used_tau2_evaluator`` is present even on the error paths
    # (missing/invalid runtime log), where reward_info carries no tau2 keys.
    tau3_shaped = isinstance(reward_info, dict) and (
        "used_tau2_evaluator" in payload
        or any(key in reward_info for key in _TAU3_REWARD_INFO_KEYS)
    )
    if not tau3_shaped:
        return None, []

    warnings: list[str] = []
    reward = reward_info.get("reward")
    raw_status = payload.get("status")
    mapped = _TAU3_VERIFIER_STATUS.get(raw_status) if isinstance(raw_status, str) else None

    # An explicit error status outranks a 0.0 reward: a crashed/missing runtime
    # log is an operational error, not a demonstrated agent failure.
    if mapped in _TAU3_NO_VERDICT_STATUSES:
        status = mapped
    elif isinstance(reward, bool):
        status = "passed" if reward else "failed"
    elif isinstance(reward, (int, float)):
        status = "passed" if reward >= 1.0 else "failed"
        if 0.0 < reward < 1.0:
            warnings.append(
                f"partial tau3 reward {reward} recorded as failed (tau3 pass is reward==1.0)"
            )
    elif mapped is not None:
        status = mapped
    else:
        status = "unknown"
        warnings.append(
            f"verifier/result.json has no usable reward/status "
            f"(reward={reward!r}, status={raw_status!r}); verifier status is 'unknown'"
        )
    if mapped is None and isinstance(raw_status, str):
        # Say where the status really came from: a numeric/bool reward decides
        # it even when the status string is unknown to us.
        detail = (
            f"status taken from reward ({status})"
            if status != "unknown"
            else "preserved as 'unknown'"
        )
        warnings.append(f"unmapped tau3 verifier status {raw_status!r}; {detail}")
    # A verifier status that disagrees with the reward (e.g. status "mismatch"
    # with reward 1.0) resolves to the reward — it is what Harbor recorded — but
    # never silently: the disagreement is surfaced so a reviewer can judge it.
    # Only when the reward actually decided the status: an explicit error or
    # operational_error status outranks the reward (see above), so there is no
    # disagreement to report and warning "the reward was used" would be false.
    reward_status = None
    if isinstance(reward, bool):
        reward_status = "passed" if reward else "failed"
    elif isinstance(reward, (int, float)):
        reward_status = "passed" if reward >= 1.0 else "failed"
    if (mapped not in (None, *_TAU3_NO_VERDICT_STATUSES)
            and reward_status is not None and mapped != reward_status):
        warnings.append(
            f"tau3 verifier status {raw_status!r} maps to {mapped!r} but reward "
            f"{reward!r} implies {reward_status!r}; the reward was used"
        )

    evidence: dict[str, Any] = {"kind": "tau3_reward_info"}
    if raw_status is not None:
        evidence["verifier_status"] = raw_status
    if reward is not None:
        evidence["reward"] = reward
    for key in _TAU3_REWARD_INFO_KEYS:
        if key in reward_info:
            evidence[key] = reward_info[key]

    check = {
        "check_id": "tau3_task_reward",
        "name": "tau3 task reward (aggregate; tau2 evaluate_simulation)",
        "status": status,
        "source": "native_structured",
        "timing": "post_run",
        "source_pointers": ["verifier/result.json"],
        "diagnostic_evidence": evidence,
    }
    raw = {
        "status": raw_status,
        "reward": reward,
        "reward_basis": reward_info.get("reward_basis"),
        "reward_breakdown": reward_info.get("reward_breakdown"),
    }
    return {"raw_output": json.dumps(raw, ensure_ascii=False), "checks": [check]}, warnings


# --- tau3-bench: synthesise the ATIF trajectory it never captures -------------
#
# The tau3 agent writes no Harbor ``agent/trajectory.json``. The recorded
# conversation lives in ``agent/tau3_runtime_state.json`` (the runtime server's
# log of tau2 messages). Without this the adapter raises "no Harbor trajectory
# found" and the run cannot be ingested at all. This synthesises an ATIF-v1.7
# trajectory from that state so the existing fan-out and every detector run
# unchanged.
#
# Nothing is invented: each ATIF step is one recorded message, its tool_calls
# are that message's own, and its observation results are the consecutive tool
# messages that follow it (a tool message whose id matches none of the turn's
# call ids is kept and warned about). A warning records that the
# trajectory was DERIVED from the runtime log, not captured as ATIF.


def _tau3_trial_dir(state_path: Path) -> Path:
    """The trial directory for a ``tau3_runtime_state.json``.

    Harbor writes it as ``<trial>/agent/tau3_runtime_state.json``; a flatter
    ``<trial>/tau3_runtime_state.json`` is accepted too (see ``_trial_markers``).
    """
    parent = state_path.parent
    return parent.parent if parent.name == "agent" else parent


def _tau3_task_hint(state: Any, trial_dir: Path) -> tuple[str, str] | None:
    """A task id for a tau3 trial that neither the caller nor result.json named.

    Both candidates are source-supplied structure shared by every attempt of a
    task: the task segment of Harbor's ``<task>__<suffix>`` trial directory name
    (the same split ``_task_name`` applies to ``trial_name``), else the runtime
    state's ``domain`` + ``task_id`` in the tau3 adapter's naming,
    ``tau3-<domain>-<task_id>``. Returns ``(task_id, where it came from)``, or
    None when neither is present — never a constant shared across trials.
    """
    dir_name = trial_dir.resolve().name
    name = _task_name({"trial_name": dir_name})
    if name:
        return name, f"the trial directory name {dir_name}"
    if isinstance(state, dict):
        domain, number = state.get("domain"), state.get("task_id")
        if (isinstance(domain, str) and domain
                and (isinstance(number, int) and not isinstance(number, bool)
                     or isinstance(number, str) and number)):
            return f"tau3-{domain}-{number}", "the tau3 runtime state (domain + task_id)"
    return None


def _tau3_metrics(msg: dict) -> dict | None:
    """Map a tau2 message's usage/cost onto ATIF metrics, when present.

    tau2 records ``usage`` as ``None`` on many runs and ``cost`` as 0.0; only
    what the source actually carries is surfaced, never a fabricated 0.
    """
    metrics: dict[str, Any] = {}
    usage = msg.get("usage")
    if isinstance(usage, dict):
        for src, dst in (
            ("prompt_tokens", "prompt_tokens"),
            ("input_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("output_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = usage.get(src)
            if isinstance(value, (int, float)) and dst not in metrics:
                metrics[dst] = value
    cost = msg.get("cost")
    if isinstance(cost, (int, float)):
        metrics["cost_usd"] = cost
    return metrics or None


def _tau3_trajectory(
    state_path: Path,
    result_data: dict,
    warnings: list[str],
    instruction: str | None = None,
) -> dict:
    """Build an ATIF-v1.7 trajectory from a tau3 ``tau3_runtime_state.json``."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{state_path.name}: unreadable tau3 runtime state ({exc})") from exc
    raw_messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError(f"{state_path.name}: tau3 runtime state has no messages")

    agent_cfg = (result_data.get("config") or {}).get("agent") or {}
    model_name = agent_cfg.get("model_name")
    agent_name = agent_cfg.get("name") or "tau3-llm-agent"

    steps: list[dict] = []
    # A real task instruction beats treating the simulated user's first line as
    # the task. The tau3 runner writes `tau3-llm-agent.instruction.md` beside
    # the runtime state; the caller may also supply one. That file is the
    # instruction the AGENT was given (the task's <policy> block — what
    # _make_parity_instruction emits), not the agent's system prompt and not a
    # simulated-user turn. Prepending it as the first "user" step makes the
    # fan-out mark it task_received and keeps EVERY recorded user turn a user
    # turn (provenance "user"), never agent-authored.
    instruction_text = instruction
    if not instruction_text:
        instr_path = state_path.parent / "tau3-llm-agent.instruction.md"
        if instr_path.is_file():
            try:
                instruction_text = instr_path.read_text(encoding="utf-8-sig").strip()
            except OSError as exc:
                warnings.append(f"{instr_path.name} unreadable ({exc})")
    if instruction_text:
        steps.append({"step_id": 1, "source": "user", "message": instruction_text})

    i = 0
    n = len(raw_messages)
    while i < n:
        msg = raw_messages[i]
        if not isinstance(msg, dict):
            warnings.append("tau3 runtime message is not an object; skipped, not dropped silently")
            i += 1
            continue
        role = msg.get("role")
        # tau2 names the agent role "assistant"; ATIF calls it "agent".
        source = {"assistant": "agent", "user": "user", "system": "system"}.get(role)
        if source is None:
            # tool results are consumed by the turn that called them; an orphan
            # here means the source is malformed, so say so rather than guess.
            if role == "tool":
                warnings.append("tau3 tool message has no preceding assistant/user turn; skipped")
            else:
                warnings.append(f"tau3 message role {role!r} is not mapped; skipped")
            i += 1
            continue

        atif_calls: list[dict] = []
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            atif_calls.append({
                "tool_call_id": call.get("id"),
                "function_name": call.get("name") or "tool",
                "arguments": call.get("arguments") or {},
            })

        results: list[dict] = []
        call_ids = {c["tool_call_id"] for c in atif_calls if c["tool_call_id"] is not None}
        j = i + 1
        while j < n and isinstance(raw_messages[j], dict) and raw_messages[j].get("role") == "tool":
            tool_msg = raw_messages[j]
            # Results are the consecutive tool messages after the turn, not
            # looked up by id; keep a mismatched one but never silently.
            if tool_msg.get("id") not in call_ids:
                warnings.append(
                    f"tau3 tool message id {tool_msg.get('id')!r} matches no tool call of the "
                    f"preceding {role} turn (step {len(steps) + 1}); attached to it anyway"
                )
            result: dict[str, Any] = {
                "source_call_id": tool_msg.get("id"),
                "content": tool_msg.get("content"),
            }
            # tau2 records an explicit boolean; map it so the fan-out labels the
            # result as a failure without guessing from the text.
            if tool_msg.get("error"):
                result["extra"] = {"is_error": True}
            results.append(result)
            j += 1

        step: dict[str, Any] = {"step_id": len(steps) + 1, "source": source}
        if msg.get("timestamp"):
            step["timestamp"] = msg["timestamp"]
        if source == "agent" and model_name:
            step["model_name"] = model_name
        content = msg.get("content")
        if content is not None:
            step["message"] = _text_of(content)
        if atif_calls:
            step["tool_calls"] = atif_calls
        if results:
            step["observation"] = {"results": results}
        metrics = _tau3_metrics(msg)
        if metrics:
            step["metrics"] = metrics
        steps.append(step)
        i = j

    if not steps:
        raise ValueError(f"{state_path.name}: tau3 runtime state produced no steps")
    warnings.append(
        "trajectory synthesised from tau3_runtime_state.json (the tau3 agent writes no "
        "agent/trajectory.json); each step is one recorded tau2 message, not a captured ATIF turn"
    )
    return {
        "schema_version": "ATIF-v1.7",
        # Without a Harbor trial UUID the id seeds the run identity, so it must
        # be unique per trial: the state's task_id is shared by every attempt of
        # a task and would collapse them into one run. The trial directory name
        # is per-attempt and stable across re-ingests.
        "trajectory_id": str(result_data.get("id") or _tau3_trial_dir(state_path).resolve().name),
        "agent": {"name": agent_name, "model_name": model_name},
        "steps": steps,
        # Private: consumed (and removed) by convert(); never part of the ATIF.
        "_task_hint": _tau3_task_hint(state, _tau3_trial_dir(state_path)),
    }


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
    traj_path, result_path, warnings, tau3_state_path = _resolve_paths(source)
    result_data: dict = _read_result(result_path) if result_path is not None else {}
    tau3_task_hint: tuple[str, str] | None = None
    if traj_path is not None:
        traj = json.loads(traj_path.read_text(encoding="utf-8-sig"))
    else:
        # tau3: no captured ATIF trajectory — synthesise one from the runtime log.
        traj = _tau3_trajectory(tau3_state_path, result_data, warnings, instruction)
        tau3_task_hint = traj.pop("_task_hint", None)

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
        raise ValueError(f"{Path(source).name}: trajectory has no steps")

    steps: list[dict] = []
    seq = 0

    def add(kind: str, actor: str, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        # Source-fanned-out steps are observed; adapter-written terminal events
        # override with provenance="synthetic" at their call site.
        payload.setdefault("provenance", "observed")
        step = {"step_id": f"h{seq}", "kind": kind, "actor": actor, **payload}
        # AGR-04: preserve the source's own timestamp when the step carries one
        # (mini-swe-agent records agent turns only) — never synthesise one.
        if timestamp is not None:
            step["timestamp"] = timestamp
        steps.append(step)

    first_user_text: str | None = None
    model: str | None = agent.get("model_name")
    call_names: dict[str, str] = {}  # tool_call_id -> function_name, to name results
    call_args: dict[str, Any] = {}  # tool_call_id -> arguments, for AGR-03 submission pairing
    saw_agent_step = False
    last_agent_tool_calls: list[dict] = []

    for hstep in harbor_steps:
        if not isinstance(hstep, dict):
            warnings.append("non-object step skipped, not dropped silently")
            continue
        src = hstep.get("source")
        model = hstep.get("model_name") or model
        timestamp = hstep.get("timestamp")  # AGR-04: preserve available timing

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
            # Item 27 (2026-09-08): metrics is a TURN-level aggregate cost —
            # attribute it to only the FIRST step this turn fans out into
            # (thinking, message, or the first tool_call), never once per
            # step, so a turn with several tool_calls doesn't multiply its
            # own token cost.
            metrics = hstep.get("metrics")
            pending_cost = dict(metrics) if isinstance(metrics, dict) and metrics else None
            pending_generation = True
            generation_id = str(hstep.get("step_id") or f"harbor-generation-{seq + 1}")

            def _cost_kwarg() -> dict[str, Any]:
                nonlocal pending_cost, pending_generation
                if not pending_generation:
                    return {}
                pending_generation = False
                kw: dict[str, Any] = {
                    "generation_event": True,
                    "generation_id": generation_id,
                    "model": model,
                }
                if pending_cost is not None:
                    kw["cost"] = pending_cost
                    pending_cost = None
                return kw

            reasoning = hstep.get("reasoning_content")
            if reasoning:
                add("model_output", "main_agent", content=f"[thinking] {_text_of(reasoning)}", **_cost_kwarg())
            message = _text_of(hstep.get("message"))
            if message:
                add("model_output", "main_agent", content=message, **_cost_kwarg())
            turn_calls: list[tuple[str, Any]] = []  # this turn's (function_name, arguments), in order
            for call in hstep.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fname = call.get("function_name") or call.get("name") or "tool"
                payload = _hoist_argument(fname, call.get("arguments"))
                payload.update(_cost_kwarg())
                add("tool_call", "main_agent", **payload)
                cid = call.get("tool_call_id") or call.get("id")
                if cid:
                    call_names[cid] = fname
                    call_args[cid] = call.get("arguments")
                turn_calls.append((fname, call.get("arguments")))
            # The observation records the environment's response to this turn's
            # tool calls; each result becomes its own tool_result step.
            observation = hstep.get("observation") or {}
            for res in observation.get("results") or []:
                if not isinstance(res, dict):
                    continue
                cid = res.get("source_call_id")
                raw_content = res.get("content")
                payload = {
                    "tool": call_names.get(cid, "tool"),
                    "content": _text_of(raw_content),
                }
                # AGR-03: resolve which call this result answers, to check
                # whether the pair is the submission protocol's own
                # round-trip. Harbor's mini-swe-agent turns carry no
                # source_call_id at all (id-based match is unavailable), but
                # a turn's result answers the turn's OWN call — exact pairing
                # when the turn made exactly one call, which is mini-swe-
                # agent's normal one-action-per-turn shape. With no id and
                # more than one call this turn, which call produced this
                # result is genuinely ambiguous, so it is left unresolved
                # (never guessed) and the tagging below simply does not fire.
                if cid and cid in call_names:
                    call_fname, call_arguments = call_names[cid], call_args.get(cid)
                elif not cid and len(turn_calls) == 1:
                    call_fname, call_arguments = turn_calls[0]
                else:
                    call_fname, call_arguments = None, None
                # This specific call+response pair is the submission
                # protocol's own round-trip, not evidence of a tool failure —
                # tag it so recovery/detector classification (agr/_util.py's
                # is_tool_failure) excludes it, the same way permission_denied
                # already is. Both the call and the response must match: a
                # -1 from an unrelated command, or an unrelated message, is
                # never touched (only this exact pairing is).
                if call_fname is not None and _is_submission_call(call_fname, call_arguments) \
                        and _is_submission_non_execution_response(raw_content):
                    payload["submission_control_response"] = True
                # ATIF observations don't carry a POSIX exit code as a top-level
                # field; only record one when the source explicitly surfaced it
                # (via extra), never guess.
                extra = res.get("extra") or {}
                if isinstance(extra, dict) and "exit_code" in extra:
                    payload["exit_code"] = extra["exit_code"]
                elif isinstance(extra, dict) and extra.get("is_error"):
                    payload["exit_code"] = 1
                else:
                    # mini-swe-agent's own harness encodes the exit status
                    # inside content itself (adapter 0.8) — decode it when
                    # ``extra`` carried nothing.
                    returncode = _parse_returncode(raw_content)
                    if returncode is not None:
                        payload["exit_code"] = returncode
                        payload["status"] = "ok" if returncode == 0 else "error"
                    elif agent.get("name") == "terminus-2":
                        # Item 20: terminus-2 observations are raw terminal
                        # screen text with no exit code anywhere — never
                        # promoted to exit_code/status (process_state is
                        # declared unavailable for this agent below). This is
                        # a SEPARATE, clearly-labelled, lower-confidence
                        # signal a UI/detector may choose to use later.
                        heuristic = _terminus2_heuristic_status(payload["content"])
                        if heuristic is not None:
                            payload["heuristic_status"] = heuristic
                            payload["heuristic_status_source"] = "shell_error_marker"
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
        return any(_is_submission_call(c.get("function_name") or c.get("name"), c.get("arguments"))
                   for c in calls)

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
    traj_id = str(traj.get("trajectory_id") or traj.get("session_id") or Path(source).stem)
    # A synthesised tau3 trajectory has a per-trial id, so without a task name
    # its task id comes from the source's own per-task structure (trial
    # directory name, else runtime-state domain + task_id) before the per-trial
    # fallback — otherwise every attempt of one task would get its own task id.
    derived_task_id = task_id or _task_name(result_data)
    if task_id is None and derived_task_id:
        warnings.append(f"task_id taken from result.json ({derived_task_id}); confirm the contract")
    elif not derived_task_id and tau3_task_hint:
        derived_task_id = tau3_task_hint[0]
        warnings.append(
            f"task_id taken from {tau3_task_hint[1]} ({derived_task_id}); confirm the contract"
        )
    derived_task_id = derived_task_id or f"harbor-trial-{traj_id[:12]}"
    resolved_run_id = run_id or _execution_run_id(derived_task_id, traj_id, result_data, warnings)
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": derived_task_id,
        "model": model or "unresolved",
        "agent": agent.get("name") or "harbor-agent",
        "harness_version": f"harbor/{schema_version}",
    }
    # Execution lineage (adapter 0.6): preserve what the source recorded about
    # WHICH execution this is, so distinct attempts stay distinguishable and
    # old-capture→new-execution mappings stay auditable. A stable identifier
    # goes on the run; the truncated session prefix never decides identity.
    trial_uuid = result_data.get("id")
    trial_name = result_data.get("trial_name")
    if trial_uuid:
        run["trial_uuid"] = str(trial_uuid)
    if trial_name:
        run["trial_name"] = str(trial_name)
    # The full session/trajectory id the source recorded — lineage for old-
    # store mappings, which truncated exactly this value (AGR-02).
    run["source_session_id"] = traj_id
    agent_version = agent.get("version")
    if agent_version:
        run["agent"] = f"{run['agent']}@{agent_version}"
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id
    # AGR-04: task version/hash fields only when the source supports them —
    # the checksum/ref go on the run verbatim; nothing is invented for
    # sources that don't record them.
    if result_data.get("task_checksum"):
        run["task_checksum"] = str(result_data["task_checksum"])
    task_ref = result_data.get("task_id")
    if isinstance(task_ref, dict):
        for key in ("org", "name", "ref"):
            if task_ref.get(key):
                run[f"task_{key}"] = str(task_ref[key])

    # --- capabilities: only what Harbor genuinely captured ------------------
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "complete",
        # Harbor observes the filesystem/processes only through tool I/O, never
        # as captured state, and the asciinema recording isn't parsed here.
        "filesystem": "partial",
        "process_state": "partial",
        "generation_usage": generation_usage_capability(steps),
    }
    # Item 20 (2026-09-08): terminus-2's tool observations are raw terminal
    # screen text with no exit code anywhere — "partial" (the default above)
    # implies SOME process-state evidence was captured through tool I/O, which
    # is false for this agent. Declaring it "unavailable" makes capability-
    # gated failure detectors report "not evaluated" honestly instead of
    # silently finding nothing on a source that never captured it.
    if agent.get("name") == "terminus-2":
        capabilities["process_state"] = "unavailable"
        warnings.append(
            "agent is terminus-2: tool observations are raw terminal screen text with no "
            "exit code — process_state is 'unavailable', not 'partial'"
        )

    # --- verifier: CTRF atomic tests first; else explicit sidecar; else the
    # aggregate reward from result.json (AGR-04 priority) ---------------------
    if traj_path is not None:
        trial_dir = traj_path.parent.parent if traj_path.parent.name == "agent" else traj_path.parent
    else:
        # <trial>/agent/tau3_runtime_state.json (or flat <trial>/...) -> <trial>
        trial_dir = _tau3_trial_dir(tau3_state_path)
    synth_warnings: list[str] = []
    if verifier is None:
        verifier, ctrf_warnings = _verifier_from_ctrf(trial_dir, result_data)
        synth_warnings.extend(ctrf_warnings)
    if verifier is None:
        verifier, tau3_warnings = _verifier_from_tau3(trial_dir, result_data)
        synth_warnings.extend(tau3_warnings)
    if verifier is None and result_path is not None:
        verifier, reward_warnings = _verifier_from_result(result_data, result_path.name)
        synth_warnings.extend(reward_warnings)
    warnings.extend(synth_warnings)
    if verifier:
        # Honest capability reporting (AGR-04): the trial bundle carries the
        # verifier's *results* (reward, CTRF test records, log output) but not
        # the verifier's code. Seeing a reward sidecar does not make the code
        # visible — report the results capture as complete, the code as partial.
        capabilities["verifier_code"] = "partial"
        capabilities["verifier_results"] = "complete"
        warnings.append(
            "verifier results captured (aggregate reward"
            + (" + tau3 reward breakdown" if any(c.get("diagnostic_evidence") for c in verifier.get("checks", [])) else "")
            + (" + ctrf.json" if any(c.get("check_id") != "terminal_bench_reward" and "::" in str(c.get("check_id", "")) for c in verifier.get("checks", [])) else "")
            + "); the verifier's own code is not in the trial bundle — verifier_code is 'partial', not 'complete'"
        )

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
