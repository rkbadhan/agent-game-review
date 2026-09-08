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

import hashlib
import json
from pathlib import Path
from typing import Any

from .adapter import AdapterResult
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
HARBOR_ADAPTER_VERSION = "harbor-adapter-0.8"

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
    """
    return (
        (p / "agent" / "trajectory.json").exists()
        or (p / "trajectory.json").exists()
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
                accounted.append((child, "no reviewable trajectory (agent/trajectory.json missing "
                                          "— errored trial or job roll-up)"))
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
                    nested.append((trial, "no reviewable trajectory (agent/trajectory.json missing "
                                              "— errored trial or job roll-up)"))
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
            f"trajectory.json). Note: oracle runs record no trajectory."
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
        step = {"step_id": f"h{seq}", "kind": kind, "actor": actor, **payload}
        # AGR-04: preserve the source's own timestamp when the step carries one
        # (mini-swe-agent records agent turns only) — never synthesise one.
        if timestamp is not None:
            step["timestamp"] = timestamp
        steps.append(step)

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

            def _cost_kwarg() -> dict[str, Any]:
                nonlocal pending_cost
                if pending_cost is None:
                    return {}
                kw = {"cost": pending_cost}
                pending_cost = None
                return kw

            reasoning = hstep.get("reasoning_content")
            if reasoning:
                add("model_output", "main_agent", content=f"[thinking] {_text_of(reasoning)}", **_cost_kwarg())
            message = _text_of(hstep.get("message"))
            if message:
                add("model_output", "main_agent", content=message, **_cost_kwarg())
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
    trial_dir = traj_path.parent.parent if traj_path.parent.name == "agent" else traj_path.parent
    synth_warnings: list[str] = []
    if verifier is None:
        verifier, ctrf_warnings = _verifier_from_ctrf(trial_dir, result_data)
        synth_warnings.extend(ctrf_warnings)
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
