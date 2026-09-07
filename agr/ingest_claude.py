"""Claude Code session adapter (spec §5.3 interoperability strategy).

Converts a Claude Code capture into the minimal ATIF-shaped JSON contract that
``agr.ingest`` imports. Like the pi adapter, this is a pure format mapping: no
analysis, no model calls, and nothing is invented that the source did not
capture.

Supported input formats (review 2026-09-07, R3) — detected explicitly, never
partially normalized into each other:

- **Saved transcript** (JSONL under ``~/.claude/projects/<project>/<id>.jsonl``):
  user/assistant message entries with typed content blocks;
- **``claude -p --output-format stream-json``** (newline-delimited stream):
  the same message entries plus ``system`` init records and a terminal
  ``result`` record;
- **``claude -p --output-format json``** (a single final-result object): a
  useful LIMITED view only — the result text with an explicit
  no-trajectory warning, never a fake full execution.

What Claude captures natively (and this adapter preserves — review 2026-09-07,
R2): assistant messages with model and typed content blocks (``text``,
``thinking``, ``tool_use``), the COMPLETE structured tool input (an ``Edit``'s
``old_string``/``new_string``, a ``Write``'s content — display excerpts stay
separate from retained evidence), tool-use ids, and the result→call link by id
(correct for parallel calls; sequential adjacency is never guessed). Tool
status is preserved AS tool status: an error flag is not an OS exit code, and
a result without an explicit ``is_error`` is recorded as unknown — never
silently turned into success.

What Claude sessions do NOT capture — task identity, requirements, and the
verifier result — must be supplied by the caller, never guessed:
``--task-id`` / ``--instruction`` (default: first user message text), and
``--verifier sidecar.json`` carrying ``raw_output`` and atomic ``checks``.

Session execution status vs task outcome: a transcript end does NOT by itself
establish session completion (the file may be interrupted, partial, or still
active — review 2026-09-07, R3). Completion is asserted ONLY when the source
records an explicit terminal ``result``; otherwise the capture says so
explicitly (``session_completion: unobserved``) and reports no completion
event. Without a verifier the run ingests UNVERIFIED — an explicit, honest
limited view that never claims a task-level PASS/FAIL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapter import AdapterResult

# Provenance stamp written into every document this adapter emits (and recorded
# on the immutable capture). Bump when the mapping changes materially.
CLAUDE_ADAPTER_VERSION = "claude-adapter-0.2"

# Claude session-entry types that carry no trajectory content for analysis.
_SKIP_ENTRY_TYPES = {
    "summary",     # conversation summary lines, not agent behaviour
    "file-history-snapshot",
    "queue-operation",
}


def _read_jsonl(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{lineno}: invalid JSON ({exc})") from exc
    if not entries:
        raise ValueError(f"{path.name}: empty session file")
    return entries


def _text_of(content: Any) -> str:
    """Flatten Claude message content (string or typed blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _result_text(content: Any) -> str:
    """Tool-result payload text: a string, or a list of typed blocks."""
    if isinstance(content, str):
        return content
    return _text_of(content)


def _is_final_result_object(data: Any) -> bool:
    """A ``claude -p --output-format json`` export: one object, terminal result."""
    return isinstance(data, dict) and "result" in data and "message" not in data


def _looks_like_stream(entries: list[dict]) -> bool:
    """A ``claude -p --output-format stream-json`` export: stream records carry
    lowercase ``session_id`` and/or ``system``/``result`` entry types, which
    saved transcripts never do."""
    for e in entries:
        if not isinstance(e, dict):
            continue
        if "session_id" in e or e.get("type") in ("system", "result"):
            return True
    return False


class _StepBuilder:
    """Shared message-walker state: steps, sequence, and tool-use linkage."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.seq = 0

    def add(self, kind: str, actor: str, **payload: Any) -> None:
        self.seq += 1
        payload.setdefault("provenance", "observed")
        self.steps.append({"step_id": f"s{self.seq}", "kind": kind, "actor": actor, **payload})


def _build_steps(entries: list[dict], builder: _StepBuilder) -> dict:
    """Walk user/assistant message entries into ATIF steps.

    Returns counters used for capability/honesty decisions: pending tool calls,
    whether the agent ever acted, the resolved model, first user text, the
    first timestamp, and whether the last meaningful entry was a model error.
    """
    pending_calls: dict[str, dict] = {}  # tool-use id -> call step
    state: dict[str, Any] = {
        "first_user_text": None, "model": None, "started_at": None,
        "saw_agent_step": False, "ended_on_model_error": False,
    }

    for entry in entries:
        if not isinstance(entry, dict):
            builder.warnings.append("non-object session line skipped (recorded, not dropped silently)")
            continue
        etype = entry.get("type")
        if etype in _SKIP_ENTRY_TYPES:
            continue
        # Stream exports carry the session id on every record (lowercase key);
        # saved transcripts carry it as ``sessionId``. First one wins; the
        # FULL id is preserved — never truncated (R3).
        if not state.get("stream_session_id"):
            sid = entry.get("session_id") or entry.get("sessionId")
            if sid:
                state["stream_session_id"] = str(sid)
        if etype == "result":
            _consume_result_entry(entry, builder, state, pending_calls)
            continue
        if etype == "system":
            # Harness init/bookkeeping: carry the session id if that is all it holds.
            continue
        if etype not in ("user", "assistant"):
            builder.warnings.append(
                f"unmapped Claude entry type {etype!r}; step skipped, not dropped silently")
            continue

        msg = entry.get("message") or {}
        role = msg.get("role")
        ts = entry.get("timestamp")
        if ts and state["started_at"] is None:
            state["started_at"] = ts
        state["ended_on_model_error"] = False  # a later message supersedes an earlier error

        if role == "user":
            content = msg.get("content")
            tool_results = []
            if isinstance(content, list):
                tool_results = [b for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
            text = _text_of(content)
            if tool_results:
                # Tool results ride in as user-role messages; they are harness
                # output, never the agent speaking or the task statement. Each
                # result keeps its tool-use id: the link to THE call it answers
                # (correct for parallel calls — R2). Tool status is tool status:
                # an explicit error flag is recorded as such; NO flag means
                # unknown — never a synthesized success.
                for tr in tool_results:
                    tid = tr.get("tool_use_id")
                    payload: dict[str, Any] = {
                        "tool": (pending_calls.get(tid) or {}).get("tool", "tool"),
                        "content": _result_text(tr.get("content")),
                    }
                    if tid:
                        payload["tool_use_id"] = tid
                    if tr.get("is_error") is True:
                        payload["status"] = "error"
                    elif tr.get("is_error") is False:
                        payload["status"] = "ok"
                    builder.add("tool_result", "tool", **payload)
                    pending_calls.pop(tid, None)
                if text:
                    builder.add("environment_observation", "user", content=text)
                continue
            if state["first_user_text"] is None:
                state["first_user_text"] = text
                builder.add("task_received", "harness", content=text)
            else:
                builder.add("environment_observation", "user", content=text)

        elif role == "assistant":
            state["saw_agent_step"] = True
            state["ended_on_model_error"] = False
            model = msg.get("model") or state["model"]
            if model:
                state["model"] = model
            if entry.get("isApiErrorMessage"):
                state["ended_on_model_error"] = True
                builder.add("error_observed", "harness",
                            content="model error response (API error entry)")
                continue
            for block in msg.get("content") or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    builder.add("model_output", "main_agent", content=block.get("text", ""))
                elif btype == "thinking":
                    builder.add("model_output", "main_agent",
                                content=f"[thinking] {block.get('thinking', '')}")
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    args = block.get("input") or {}
                    call: dict[str, Any] = {"tool": name}
                    # R2: the COMPLETE structured tool input is retained evidence —
                    # an Edit's old/new strings, a Write's content — kept beside,
                    # never instead of, the display excerpt below.
                    call["tool_input"] = args
                    if bid := block.get("id"):
                        call["tool_use_id"] = bid
                    # Hoist the most analysis-relevant argument into content so
                    # deterministic token matching (evidence slicing) can see it.
                    for key in ("command", "file_path", "path", "pattern", "url"):
                        if isinstance(args.get(key), str):
                            call["content"] = args[key]
                            if key in ("file_path", "path"):
                                call["path"] = args[key]
                            break
                    else:
                        call["content"] = json.dumps(args, ensure_ascii=False)[:2000]
                    builder.add("tool_call", "main_agent", **call)
                    if bid:
                        pending_calls[bid] = call

        else:
            builder.warnings.append(
                f"unmapped Claude message role {role!r}; step skipped, not dropped silently")

    if pending_calls:
        builder.warnings.append(
            f"{len(pending_calls)} tool call(s) never observed a result — tool-result "
            f"coverage is partial, and no completion is claimed for them")
    state["pending_count"] = len(pending_calls)
    return state


def _consume_result_entry(entry: dict, builder: _StepBuilder, state: dict,
                          pending_calls: dict) -> None:
    """A terminal ``result`` record from a ``claude -p`` stream (R3).

    Honored, not skipped: an ``is_error`` result ends the run in error;
    otherwise it is the ONE explicit completion signal — the only basis for a
    ``run_completed``. Cost/usage metadata is preserved in run metadata, never
    silently dropped.
    """
    state["result_entry"] = entry
    if entry.get("session_id") and not state.get("stream_session_id"):
        state["stream_session_id"] = entry.get("session_id")
    result_text = str(entry.get("result") or "")
    usage = entry.get("usage") or {}
    cost = entry.get("total_cost_usd")
    meta: dict[str, Any] = {}
    if usage:
        meta["usage"] = usage
    if cost is not None:
        meta["total_cost_usd"] = cost
    if entry.get("is_error"):
        builder.add("run_failed", "harness", provenance="synthetic",
                    content=result_text or "[claude -p result reported an error]",
                    termination_reason="report_error", **meta)
    else:
        builder.add("run_completed", "harness", provenance="synthetic",
                    content=result_text or "[claude -p reported completion]", **meta)


def _final_result_document(data: dict, path_name: str) -> AdapterResult:
    """A final-result-only export: a useful LIMITED view, honestly labelled.

    The result text is preserved verbatim as an observed model output; the
    document states everywhere that NO trajectory was captured — it never
    resembles a fully captured completed execution (R3).
    """
    warnings = [
        f"{path_name}: final-result-only export — no trajectory steps were captured, "
        f"so no behavioural review is possible; only the final result text is available",
    ]
    steps = [{
        "step_id": "s1", "kind": "model_output", "actor": "main_agent",
        "provenance": "observed", "content": str(data.get("result") or ""),
    }]
    if data.get("is_error"):
        warnings.append("the final result reports is_error: true — the invocation failed")
    run: dict[str, Any] = {
        "logical_run_id": "claude__final_result_only",
        "task_id": "claude-final-result-only",
        "model": "unresolved",
        "agent": "claude-code",
        "harness_version": "claude-code-final-result",
        "started_at": "",
    }
    usage = data.get("usage") or {}
    if usage:
        run["usage"] = usage
    if data.get("total_cost_usd") is not None:
        run["total_cost_usd"] = data.get("total_cost_usd")
    run["session_completion"] = "observed"
    doc: dict[str, Any] = {
        "atif_version": "claude-adapter-0.2",
        "source_type": "claude_final_result",
        "adapter_version": CLAUDE_ADAPTER_VERSION,
        "capture_completeness": "partial",
        "run": run,
        "capabilities": {
            "messages": "final_only",
            "tool_calls": "unavailable",
            "tool_results": "unavailable",
            "filesystem": "unavailable",
            "process_state": "unavailable",
            "compaction_boundary": "unavailable",
            "verifier_code": "unavailable",
        },
        "task": {"instruction": "", "artifacts": [], "requirements": []},
        "steps": steps,
    }
    return AdapterResult(doc=doc, warnings=warnings, meta={"entry_count": 1})


def convert(
    session_path: str | Path,
    *,
    task_id: str | None = None,
    instruction: str | None = None,
    run_id: str | None = None,
    verifier: dict | None = None,
    sweep_id: str | None = None,
    configuration_id: str | None = None,
) -> AdapterResult:
    """Convert one Claude Code capture into an ATIF-shaped document.

    Pure function: reads the file, detects the input format (saved transcript,
    ``claude -p`` stream export, or final-result object), and returns the
    document. Warnings describe every place the mapping had to make an
    explicit, conservative choice.
    """
    path = Path(session_path)

    # Format detection (R3): a single JSON object that is a terminal result is
    # the ``--output-format json`` case; anything else parses as JSONL.
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.strip()
    single: Any = None
    if stripped.startswith("{"):
        try:
            candidate = json.loads(stripped)
            if isinstance(candidate, dict):
                single = candidate
        except json.JSONDecodeError:
            single = None
    if single is not None and _is_final_result_object(single):
        return _final_result_document(single, path.name)

    entries = _read_jsonl(path)
    is_stream = _looks_like_stream(entries)

    builder = _StepBuilder()
    state = _build_steps(entries, builder)
    warnings = builder.warnings
    steps = builder.steps

    # --- identity: full source session id, never truncated (R3) --------------
    session_id = entries[0].get("sessionId") if entries else None
    session_id = str(session_id) if session_id else state.get("stream_session_id")
    if is_stream and not session_id:
        warnings.append(
            "stream export carries no session id anywhere — identity falls back to "
            "the file name (collisions are possible; the source did not supply one)")
    fallback_id = path.stem

    # --- completion is observed, never assumed (R3) --------------------------
    # A transcript end is NOT a completion signal: saved sessions and streams
    # without a terminal result record report session_completion unobserved and
    # emit NO completion event. A session that ends on an unanswered tool call
    # or a model API error is exactly the interrupted case this covers.
    terminal_entry = state.get("result_entry")
    if terminal_entry is None:
        if state.get("ended_on_model_error"):
            warnings.append(
                "the last recorded entry is a model API error and no completion signal "
                "follows — session completion is NOT observed (the capture may be "
                "interrupted or the invocation may have failed)")
        else:
            warnings.append(
                "end of transcript without a terminal result record — session "
                "completion is NOT observed (the file may be interrupted, partial, or "
                "still active); no run_completed/run_failed is synthesized")

    if not steps:
        raise ValueError(f"{path.name}: session produced no trajectory steps")

    # A session with zero assistant turns means the agent never acted: a
    # protocol failure (observed — the capture contains no agent work at all),
    # not a completion. This remains the one synthetic terminal event that is
    # asserted without a source completion signal, because its negation is
    # what the capture shows.
    if not state.get("saw_agent_step") and terminal_entry is None \
            and not state.get("ended_on_model_error"):
        builder.add("run_failed", "harness", provenance="synthetic",
                    content="[Claude session ended without any assistant step — the agent never acted]",
                    termination_reason="agent_protocol_failure")
        warnings.append("session has zero assistant steps (agent never acted)")

    # --- run metadata --------------------------------------------------------
    resolved_session = session_id or fallback_id
    resolved_run_id = run_id or f"claude__{(task_id or 'session')}__{resolved_session}"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": task_id or f"claude-session-{resolved_session}",
        "model": state.get("model") or "unresolved",
        "agent": "claude-code",
        "harness_version": "claude-code-session",
        "started_at": state.get("started_at") or "",
    }
    if session_id:
        run["source_session_id"] = session_id
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id
    # A resumed conversation can contain multiple tasks: full session identity
    # is preserved, and the caller's --task-id remains the task boundary.
    if task_id:
        warnings.append(
            "a Claude session can span multiple tasks (resumed conversations); the "
            "supplied --task-id is treated as the task-execution boundary")

    # --- capabilities: exactly what this capture carries ---------------------
    pending = state.get("pending_count", 0)
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "partial" if pending else "complete",
        "filesystem": "partial",     # observed only through tool I/O, never state-captured
        "process_state": "partial",  # tool status/exit codes where recorded, not process tables
        "compaction_boundary": "unavailable",
        "verifier_code": "complete" if verifier else "unavailable",
    }
    # Session-completion status is run metadata, not a capability level: it
    # says whether the SOURCE recorded an explicit terminal result.
    run["session_completion"] = "observed" if terminal_entry is not None else "unobserved"

    resolved_instruction = instruction if instruction is not None else (state.get("first_user_text") or "")
    if instruction is None and state.get("first_user_text"):
        warnings.append("task instruction taken from first user message; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": "claude-adapter-0.2",
        "source_type": "claude_stream" if is_stream else "claude_session",
        "adapter_version": CLAUDE_ADAPTER_VERSION,
        "capture_completeness": "complete" if (verifier and terminal_entry is not None) else "partial",
        "run": run,
        "capabilities": capabilities,
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": steps,
    }
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append(
            "no verifier supplied: run ingests UNVERIFIED with an honest limited "
            "view (§7.2) — behaviour review does not require a task-level pass/fail"
        )

    return AdapterResult(doc=doc, warnings=warnings, meta={"entry_count": len(entries)})


class ClaudeAdapter:
    """The Claude-session adapter, exposed through the shared ``Adapter``
    protocol (spec §5.3). A thin object over the pure ``convert`` function so
    the CLI can dispatch to it by name via the registry."""

    name = "claude"
    version = CLAUDE_ADAPTER_VERSION

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


CLAUDE_ADAPTER = ClaudeAdapter()
