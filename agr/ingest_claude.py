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
status is preserved AS tool status: an error flag is not an OS exit code.

A ``tool_result`` content block's status follows the Messages API's own
semantics (2026-09-07, item 2): ``is_error`` is populated only on failure —
its ABSENCE means success, not "unknown". This is documented Anthropic API
behaviour, not an inference this adapter makes, so a result block with no
``is_error`` field is recorded as ``status: ok``.

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

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .adapter import AdapterResult

# Provenance stamp written into every document this adapter emits (and recorded
# on the immutable capture). Bump when the mapping changes materially.
CLAUDE_ADAPTER_VERSION = "claude-adapter-0.6"

# Item 10 (2026-09-07): harness-injected wrapper tags that ride inside a
# user-role message's TEXT content — a system-reminder, a slash command's
# name/stdout echo. These are harness bookkeeping, never part of what the
# user actually said or the task's own instruction, so they are stripped
# before the text reaches deterministic token matching or the instruction.
_META_BLOCK_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|local-command-stdout)>"
    r".*?</\1>",
    re.DOTALL,
)


def _strip_meta_blocks(text: str) -> str:
    return _META_BLOCK_RE.sub("", text).strip()


def _default_configuration_id(init_entry: dict) -> str:
    """Item 24 (2026-09-08): a deterministic configuration_id default from
    the stream's own init record, used only when the caller supplies none.

    Hashes model, available tools, MCP servers, and permission mode — the
    things that make two runs "the same configuration" for the §4.16
    comparison feature. ``cwd`` is deliberately excluded: it is per-task,
    never part of what "configuration" means.
    """
    mcp_servers = init_entry.get("mcp_servers") or []
    material = {
        "model": init_entry.get("model"),
        "tools": sorted(str(t) for t in (init_entry.get("tools") or [])),
        "mcp_servers": sorted(
            str(s.get("name")) if isinstance(s, dict) else str(s) for s in mcp_servers
        ),
        "permission_mode": init_entry.get("permissionMode") or init_entry.get("permission_mode"),
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return f"claude-config-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

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
    a lowercase ``session_id`` key (saved transcripts use ``sessionId``) and/or
    the stream's own ``system``/``subtype: init`` record.

    Item 9 (2026-09-07): matching on ANY ``system``-typed entry was too broad
    — a SAVED transcript can also carry a ``type: system`` record (a
    ``compact_boundary``, item 8), which made every compacted saved session
    misdetect as a stream export. Only the init record's specific
    ``subtype`` is a real stream signal.
    """
    for e in entries:
        if not isinstance(e, dict):
            continue
        if "session_id" in e:
            return True
        if e.get("type") == "system" and e.get("subtype") == "init":
            return True
    return False


class _StepBuilder:
    """Shared message-walker state: steps, sequence, and tool-use linkage."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.seq = 0
        # Item 27 (2026-09-08): the CURRENT entry's timestamp, stamped onto
        # every step derived from it — set once per entry by the caller,
        # never guessed for a step whose own entry carried none.
        self.current_timestamp: str | None = None

    def add(self, kind: str, actor: str, **payload: Any) -> None:
        self.seq += 1
        payload.setdefault("provenance", "observed")
        step = {"step_id": f"s{self.seq}", "kind": kind, "actor": actor, **payload}
        if self.current_timestamp is not None and "timestamp" not in step:
            step["timestamp"] = self.current_timestamp
        self.steps.append(step)


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
        if entry.get("isMeta"):
            # Item 10: harness-injected meta content (an isMeta-wrapped
            # system-reminder turn) carries no task-relevant trajectory
            # content — skipped, not folded into the conversation.
            continue
        # Item 7/22: a Task-subagent turn. Saved transcripts flag it with
        # isSidechain: true; claude -p --output-format stream-json instead
        # carries parent_tool_use_id on the message (no isSidechain at all) —
        # missing this second form let stream-json subagent turns through
        # unflagged, silently misattributed to main_agent. Linking either
        # form to the specific Task call that spawned it would need a
        # parent-linkage this core's linear event timeline does not support
        # — guessing from timestamp/UUID proximity risks misattributing a
        # subagent's tool calls to the wrong Task call. The conservative,
        # explicit choice for both: drop it with a warning, never merge it
        # into the main agent's own trajectory.
        parent_tool_use_id = entry.get("parent_tool_use_id") or (entry.get("message") or {}).get(
            "parent_tool_use_id")
        if entry.get("isSidechain") or parent_tool_use_id:
            state["sidechain_dropped"] = state.get("sidechain_dropped", 0) + 1
            continue
        # Stream exports carry the session id on every record (lowercase key);
        # saved transcripts carry it as ``sessionId``. First one wins; the
        # FULL id is preserved — never truncated (R3).
        if not state.get("stream_session_id"):
            sid = entry.get("session_id") or entry.get("sessionId")
            if sid:
                state["stream_session_id"] = str(sid)
        if etype == "result":
            builder.current_timestamp = entry.get("timestamp")
            _consume_result_entry(entry, builder, state, pending_calls)
            continue
        if etype == "system":
            builder.current_timestamp = entry.get("timestamp")
            if entry.get("subtype") == "compact_boundary":
                # Item 8: a real, structural event — the context window was
                # compacted — gets its own context_compaction step instead of
                # being swallowed by the generic "harness bookkeeping" skip.
                compact_meta = entry.get("compactMetadata") or {}
                builder.add("context_compaction", "harness",
                            content="[context compaction boundary]",
                            compactMetadata=compact_meta)
                state["pending_compaction_step"] = builder.steps[-1]
                state["saw_compaction_boundary"] = True
            elif entry.get("subtype") == "init":
                # Item 24: the stream's own init record — model, available
                # tools, MCP servers, permission mode, and cwd — is real
                # run-level configuration the source captured but this
                # adapter previously discarded along with the rest of
                # "harness bookkeeping".
                state["init_entry"] = entry
            # Harness init/bookkeeping otherwise: carry the session id if
            # that is all it holds.
            continue
        if etype not in ("user", "assistant"):
            builder.warnings.append(
                f"unmapped Claude entry type {etype!r}; step skipped, not dropped silently")
            continue

        msg = entry.get("message") or {}
        role = msg.get("role")
        ts = entry.get("timestamp")
        builder.current_timestamp = ts  # item 27: stamped onto every step this entry produces
        if ts and state["started_at"] is None:
            state["started_at"] = ts
        state["ended_on_model_error"] = False  # a later message supersedes an earlier error

        if role == "user":
            content = msg.get("content")
            tool_results = []
            if isinstance(content, list):
                tool_results = [b for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
            text = _strip_meta_blocks(_text_of(content))
            if tool_results:
                # Tool results ride in as user-role messages; they are harness
                # output, never the agent speaking or the task statement. Each
                # result keeps its tool-use id: the link to THE call it answers
                # (correct for parallel calls — R2). Item 2 (2026-09-07): the
                # Messages API populates ``is_error`` only on failure, so its
                # absence IS the success signal, not an unknown outcome — this
                # is documented API semantics, not an inference.
                # Item 11: ``toolUseResult`` rides beside ``message`` on this
                # SAME entry, carrying the RAW structured result (Bash's
                # stdout/stderr/interrupted) the rendered content text loses.
                # Only trustworthy 1:1 when the entry carries a single result.
                raw_result = entry.get("toolUseResult")
                for tr in tool_results:
                    tid = tr.get("tool_use_id")
                    status = "error" if tr.get("is_error") is True else "ok"
                    payload: dict[str, Any] = {
                        "tool": (pending_calls.get(tid) or {}).get("tool", "tool"),
                        "content": _result_text(tr.get("content")),
                    }
                    if tid:
                        payload["tool_use_id"] = tid
                    if isinstance(raw_result, dict) and len(tool_results) == 1:
                        extra = {k: raw_result[k] for k in ("stdout", "stderr", "interrupted")
                                if k in raw_result}
                        if extra:
                            payload["tool_use_result"] = extra
                        # A cancelled/killed call is not a clean success even
                        # without an explicit is_error flag — the interrupted
                        # flag this same lift just captured says so directly.
                        if raw_result.get("interrupted") is True:
                            status = "error"
                    payload["status"] = status
                    builder.add("tool_result", "tool", **payload)
                    if tid:
                        # Item 23: kept so a permission denial recorded on
                        # the terminal result (below) can mark the SPECIFIC
                        # step it corresponds to, after the fact.
                        state.setdefault("tool_result_steps_by_tid", {})[tid] = builder.steps[-1]
                    pending_calls.pop(tid, None)
                if text:
                    builder.add("environment_observation", "user", content=text)
                continue
            if entry.get("isCompactSummary"):
                # Item 8/10: a compaction summary is harness-synthesised
                # continuity text, never the agent's or user's own words, and
                # NEVER the task instruction — even when no compact_boundary
                # is in view (a partial capture can start mid-session, right
                # after one). It attaches to the compaction event it follows
                # when there is one; otherwise it is still recorded, just not
                # as an instruction.
                pending_compaction = state.pop("pending_compaction_step", None)
                if pending_compaction is not None:
                    pending_compaction["compaction_summary"] = text
                else:
                    builder.add("environment_observation", "user", content=text)
                continue
            if state["first_user_text"] is None:
                # Review finding #1 (2026-09-07): a first turn that strips to
                # "" (an unflagged harness wrapper like a bare command-name
                # echo) must NOT claim the instruction slot — it would go
                # unnoticed as first_user_text=="" (not None), the real
                # instruction that follows would demote to an
                # environment_observation, and the "no real user message"
                # warning below checks `is None`, which is already false.
                # Wait for a message that actually has text.
                if text:
                    state["first_user_text"] = text
                    builder.add("task_received", "harness", content=text)
            elif text:
                # Finding #5: an all-meta turn that strips to "" carries
                # nothing to record — no content-free padding step.
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
            # Item 27 (2026-09-08): message.usage is a MESSAGE-level
            # aggregate cost — attribute it to only the FIRST step derived
            # from this message (deduped by message.id) so a multi-block
            # message (thinking + text + several tool_use) never counts its
            # own token cost once per block it fans out into.
            msg_id = msg.get("id")
            usage = msg.get("usage")
            seen_msg_ids = state.setdefault("cost_message_ids_seen", set())
            pending_cost = usage if (usage and msg_id and msg_id not in seen_msg_ids) else None
            if pending_cost is not None:
                seen_msg_ids.add(msg_id)

            for block in msg.get("content") or []:
                step_cost = pending_cost
                if step_cost is not None:
                    pending_cost = None
                cost_kwarg: dict[str, Any] = {"cost": {"usage": step_cost}} if step_cost else {}
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    builder.add("model_output", "main_agent", content=block.get("text", ""), **cost_kwarg)
                elif btype == "thinking":
                    builder.add("model_output", "main_agent",
                                content=f"[thinking] {block.get('thinking', '')}", **cost_kwarg)
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
                    call.update(cost_kwarg)
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
    if state["first_user_text"] is None:
        # Item 10: every real user message was meta/compact-summary content
        # (or there simply was none) — surfaced explicitly rather than
        # silently ingesting an empty task instruction.
        builder.warnings.append(
            "no real user message found (only meta/compact-summary content, or none at "
            "all) — the task instruction is empty")
    if state.get("sidechain_dropped"):
        builder.warnings.append(
            f"{state['sidechain_dropped']} isSidechain (Task-subagent) entr"
            f"{'y' if state['sidechain_dropped'] == 1 else 'ies'} dropped — this core's linear "
            f"event timeline has no parent-linkage for subagent turns; they are never merged "
            f"into the main agent's own trajectory")
    # Item 23: mark the SPECIFIC tool_result a permission denial corresponds
    # to, so a denied call is never treated as a tool failure the agent
    # should be evaluated on recovering from — a governance block is not a
    # competence failure. Correlated by tool_use_id only; never guessed by
    # tool name alone (ambiguous with repeated calls of the same tool).
    denials = (state.get("result_entry") or {}).get("permission_denials") or []
    if denials:
        tid_map = state.get("tool_result_steps_by_tid", {})
        correlated = 0
        for d in denials:
            if not isinstance(d, dict):
                continue
            tid = d.get("tool_use_id") or d.get("id")
            step = tid_map.get(tid) if tid else None
            if step is not None:
                step["permission_denied"] = True
                correlated += 1
        if correlated < len(denials):
            builder.warnings.append(
                f"{len(denials) - correlated} of {len(denials)} permission denial(s) could not "
                f"be correlated to a specific tool_result by id — recorded in run metadata only")
    state["pending_count"] = len(pending_calls)
    return state


def _consume_result_entry(entry: dict, builder: _StepBuilder, state: dict,
                          pending_calls: dict) -> None:
    """A terminal ``result`` record from a ``claude -p`` stream (R3).

    Honored, not skipped: an ``is_error`` result ends the run in error;
    otherwise it is the ONE explicit completion signal — the only basis for a
    ``run_completed``. The full entry is kept on ``state["result_entry"]`` so
    ``convert`` can pull its cost/usage/num_turns/duration/permission-denial
    fields onto the RUN record (item 11) — those never survive as step
    payload keys the derived-event allowlist doesn't carry.
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
    # Item 11: the stream's own ``subtype`` (e.g. error_max_turns,
    # error_during_execution) is a real, source-supplied termination reason —
    # more specific than the generic "report_error" fallback.
    subtype = entry.get("subtype")
    if entry.get("is_error"):
        builder.add("run_failed", "harness", provenance="synthetic",
                    content=result_text or "[claude -p result reported an error]",
                    termination_reason=subtype or "report_error", **meta)
    else:
        if subtype and subtype != "success":
            meta["termination_reason"] = subtype
        builder.add("run_completed", "harness", provenance="synthetic",
                    content=result_text or "[claude -p reported completion]", **meta)


def _final_result_document(
    data: dict,
    path_name: str,
    *,
    task_id: str | None = None,
    instruction: str | None = None,
    run_id: str | None = None,
    verifier: dict | None = None,
    sweep_id: str | None = None,
    configuration_id: str | None = None,
) -> AdapterResult:
    """A final-result-only export: a useful LIMITED view, honestly labelled.

    The result text is preserved verbatim as an observed model output; the
    document states everywhere that NO trajectory was captured — it never
    resembles a fully captured completed execution (R3).

    Review 2026-09-07 R1 (this revision): identity and option handling now
    mirror the stream/transcript path instead of hard-coding one collapsed
    run. The source ``session_id`` is preserved; explicit ``--run-id`` /
    ``--task-id`` overrides are honoured; and a conservative documented
    fallback (file stem) is used when the source supplied neither. Two
    independent final exports therefore yield two run rows, never two
    capture revisions of one invented execution. A supplied verifier affects
    verifier OUTCOME (the checks are ingested) while the trajectory
    capabilities stay limited — missing evidence remains missing.
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

    # --- identity: source session id, explicit overrides, conservative fallback ---
    session_id = data.get("session_id") or data.get("sessionId")
    session_id = str(session_id) if session_id else None
    fallback_id = Path(path_name).stem
    resolved_session = session_id or fallback_id
    resolved_run_id = run_id or f"claude__{(task_id or 'session')}__{resolved_session}"
    resolved_task_id = task_id or f"claude-session-{resolved_session}"

    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": resolved_task_id,
        "model": data.get("model") or "unresolved",
        "agent": "claude-code",
        "harness_version": "claude-code-final-result",
        "started_at": data.get("started_at") or "",
    }
    if session_id:
        run["source_session_id"] = session_id
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id
    usage = data.get("usage") or {}
    if usage:
        run["usage"] = usage
    if data.get("total_cost_usd") is not None:
        run["total_cost_usd"] = data.get("total_cost_usd")
    # A final-result object IS a terminal result, so session completion is
    # observed — but that says nothing about task-level correctness.
    run["session_completion"] = "observed"

    resolved_instruction = instruction if instruction is not None else ""
    if instruction is None:
        warnings.append(
            "final-result-only export carries no task instruction (no trajectory "
            "was captured to take it from); supply --instruction for an explicit contract")
    if task_id:
        warnings.append(
            "a Claude session can span multiple tasks (resumed conversations); the "
            "supplied --task-id is treated as the task-execution boundary")

    # Trajectory capabilities stay limited even when a verifier is supplied —
    # a sidecar of check results is not captured trajectory evidence.
    capabilities = {
        "messages": "final_only",
        "tool_calls": "unavailable",
        "tool_results": "unavailable",
        "filesystem": "unavailable",
        "process_state": "unavailable",
        "compaction_boundary": "unavailable",
        "verifier_code": "unavailable",
    }
    doc: dict[str, Any] = {
        "atif_version": "claude-adapter-0.6",
        "source_type": "claude_final_result",
        "adapter_version": CLAUDE_ADAPTER_VERSION,
        # Always partial: no trajectory was captured, verifier or not.
        "capture_completeness": "partial",
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
            "view (§7.2) — behaviour review does not require a task-level pass/fail")
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
        return _final_result_document(
            single, path.name,
            task_id=task_id, instruction=instruction, run_id=run_id,
            verifier=verifier, sweep_id=sweep_id, configuration_id=configuration_id,
        )

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
    # Item 11: the stream's terminal result record carries run-level metadata
    # (cost, usage, turn/duration counters, permission denials) that belongs
    # on the run's own identity, not buried in a step payload key the
    # derived-event allowlist doesn't carry.
    if terminal_entry is not None:
        usage = terminal_entry.get("usage") or {}
        if usage:
            run["usage"] = usage
        if terminal_entry.get("total_cost_usd") is not None:
            run["total_cost_usd"] = terminal_entry["total_cost_usd"]
        for key in ("num_turns", "duration_ms", "duration_api_ms"):
            if terminal_entry.get(key) is not None:
                run[key] = terminal_entry[key]
        if terminal_entry.get("permission_denials"):
            run["permission_denials"] = terminal_entry["permission_denials"]

    # Item 24: the stream's own init record — model, available tools, MCP
    # servers, permission mode, and cwd — is real run-level configuration the
    # source captured but was previously discarded with "harness bookkeeping".
    init_entry = state.get("init_entry")
    if init_entry is not None:
        if init_entry.get("model") and run.get("model") in (None, "unresolved"):
            run["model"] = init_entry["model"]
        if init_entry.get("tools"):
            run["tools"] = init_entry["tools"]
        if init_entry.get("mcp_servers"):
            run["mcp_servers"] = init_entry["mcp_servers"]
        perm_mode = init_entry.get("permissionMode") or init_entry.get("permission_mode")
        if perm_mode:
            run["permission_mode"] = perm_mode
        if init_entry.get("cwd"):
            run["cwd"] = init_entry["cwd"]
        if not configuration_id:
            # A configuration is "what agent+model+tool setup produced this
            # run" — cwd is per-task, not per-configuration, so it is
            # deliberately excluded from the hash.
            run["configuration_id"] = _default_configuration_id(init_entry)
            warnings.append(
                "configuration_id defaulted from a hash of the stream's init record (model, "
                "tools, mcp_servers, permission mode); pass --configuration-id for an explicit one")

    # --- capabilities: exactly what this capture carries ---------------------
    pending = state.get("pending_count", 0)
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "partial" if pending else "complete",
        "filesystem": "partial",     # observed only through tool I/O, never state-captured
        "process_state": "partial",  # tool status/exit codes where recorded, not process tables
        # Item 8: a compact_boundary event is observed evidence of the
        # compaction structure itself — "complete" only when one actually
        # appeared in this capture, never a blanket claim otherwise.
        "compaction_boundary": "complete" if state.get("saw_compaction_boundary") else "unavailable",
        "verifier_code": "complete" if verifier else "unavailable",
    }
    # Session-completion status is run metadata, not a capability level: it
    # says whether the SOURCE recorded an explicit terminal result.
    run["session_completion"] = "observed" if terminal_entry is not None else "unobserved"

    resolved_instruction = instruction if instruction is not None else (state.get("first_user_text") or "")
    if instruction is None and state.get("first_user_text"):
        warnings.append("task instruction taken from first user message; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": "claude-adapter-0.6",
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
