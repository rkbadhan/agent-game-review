"""Claude Code session adapter (spec §5.3 interoperability strategy).

Converts a Claude Code session file (JSONL, one JSON object per line under
``~/.claude/projects/<project>/<session-id>.jsonl``) into the minimal
ATIF-shaped JSON contract that ``agr.ingest`` imports. Like the pi adapter,
this is a pure format mapping: no analysis, no model calls, and nothing is
invented that the source did not capture.

What Claude Code sessions capture natively (and this adapter preserves):

- assistant messages with model, and typed content blocks: ``text``,
  ``thinking``, and ``tool_use`` (name + input, linked by tool-use id);
- user messages, including embedded ``tool_result`` blocks (with
  ``is_error``) that are linked back to the ``tool_use`` that issued them —
  so the timeline is tool-linked, not merely sequential;
- session metadata (id, timestamp) and summary lines.

What Claude sessions do NOT capture — task identity, requirements, and the
verifier result — must be supplied by the caller, never guessed:

- ``--task-id`` / ``--instruction`` (default: first user message text), and
- ``--verifier sidecar.json`` carrying ``raw_output`` and atomic ``checks``.

Session execution status vs task outcome: a Claude session has no observable
submission signal, so a session end is ``run_completed`` — never an agent
``final_submission`` — and without a verifier sidecar the run ingests with NO
verifier evidence (UNVERIFIED). That is an explicit, honest limited view: the
review of behaviour is useful without a task-level PASS/FAIL, and it never
claims one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapter import AdapterResult

# Provenance stamp written into every document this adapter emits (and recorded
# on the immutable capture). Bump when the mapping changes materially.
CLAUDE_ADAPTER_VERSION = "claude-adapter-0.1"

# Claude session-entry types that carry no trajectory content for analysis.
_SKIP_ENTRY_TYPES = {
    "summary",     # conversation summary lines, not agent behaviour
    "system",      # harness/system bookkeeping
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
    """Convert one Claude Code session JSONL file into an ATIF-shaped document.

    Pure function: reads the file, returns the document. Warnings describe
    every place the mapping had to make an explicit, conservative choice.
    """
    path = Path(session_path)
    entries = _read_jsonl(path)

    warnings: list[str] = []
    steps: list[dict] = []
    seq = 0

    def add(kind: str, actor: str, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        payload.setdefault("provenance", "observed")
        steps.append({"step_id": f"s{seq}", "kind": kind, "actor": actor, **payload})

    first_user_text: str | None = None
    model: str | None = None
    started_at: str | None = None
    saw_agent_step = False
    pending_calls: dict[str, dict] = {}  # tool-use id -> tool_call step payload

    for entry in entries:
        if not isinstance(entry, dict):
            warnings.append("non-object session line skipped (recorded, not dropped silently)")
            continue
        etype = entry.get("type")
        if etype in _SKIP_ENTRY_TYPES:
            continue

        if etype not in ("user", "assistant"):
            warnings.append(f"unmapped Claude entry type {etype!r}; step skipped, not dropped silently")
            continue

        msg = entry.get("message") or {}
        role = msg.get("role")
        ts = entry.get("timestamp")
        if ts and started_at is None:
            started_at = ts

        if role == "user":
            content = msg.get("content")
            tool_results = []
            if isinstance(content, list):
                tool_results = [b for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
            text = _text_of(content)
            if tool_results:
                # Tool results ride in as user-role messages; they are harness
                # output, never the agent speaking or the task statement.
                for tr in tool_results:
                    tid = tr.get("tool_use_id")
                    payload: dict[str, Any] = {
                        "tool": (pending_calls.get(tid) or {}).get("tool", "tool"),
                        "content": _text_of(tr.get("content")),
                    }
                    if tr.get("is_error"):
                        payload["exit_code"] = 1
                    else:
                        payload["exit_code"] = 0
                    add("tool_result", "tool", **payload)
                    pending_calls.pop(tid, None)
                if text:
                    add("environment_observation", "user", content=text)
                continue
            if first_user_text is None:
                first_user_text = text
                add("task_received", "harness", content=text)
            else:
                add("environment_observation", "user", content=text)

        elif role == "assistant":
            saw_agent_step = True
            model = msg.get("model") or model
            for block in msg.get("content") or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    add("model_output", "main_agent", content=block.get("text", ""))
                elif btype == "thinking":
                    add("model_output", "main_agent",
                        content=f"[thinking] {block.get('thinking', '')}")
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    args = block.get("input") or {}
                    call = {"tool": name}
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
                    add("tool_call", "main_agent", **call)
                    if block.get("id"):
                        pending_calls[block["id"]] = call
            if msg.get("stop_reason") == "error" or entry.get("isApiErrorMessage"):
                add("error_observed", "harness", content="model error response")

        else:
            warnings.append(f"unmapped Claude message role {role!r}; step skipped, not dropped silently")

    if pending_calls:
        warnings.append(f"{len(pending_calls)} tool call(s) never observed a result")

    if not steps:
        raise ValueError(f"{path.name}: session produced no trajectory steps")

    # Session execution status vs task outcome (explicit, never conflated): a
    # session close is a completed SESSION, not a submission — and with no
    # verifier the run is UNVERIFIED. A session with zero assistant turns means
    # the agent never acted: a protocol failure, not a completion.
    if saw_agent_step:
        add("run_completed", "harness", provenance="synthetic",
            content="[Claude session closed — no explicit submission signal observed]")
    else:
        add("run_failed", "harness", provenance="synthetic",
            content="[Claude session ended without any assistant step — the agent never acted]",
            termination_reason="agent_protocol_failure")
        warnings.append("session has zero assistant steps (agent never acted)")

    # --- run metadata -------------------------------------------------------
    header_id = entries[0].get("sessionId") or path.stem
    resolved_run_id = run_id or f"claude__{(task_id or 'session')}__{str(header_id)[:8]}"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": task_id or f"claude-session-{str(header_id)[:8]}",
        "model": model or "unresolved",
        "agent": "claude-code",
        "harness_version": "claude-code-session",
        "started_at": started_at or "",
    }
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id

    # --- capabilities: only what Claude sessions genuinely capture ----------
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "complete",
        "filesystem": "partial",     # observed only through tool I/O, never state-captured
        "process_state": "partial",  # tool exit codes where recorded, not process tables
        "compaction_boundary": "unavailable",
        "pre_post_compaction_context": "unavailable",
        "verifier_code": "complete" if verifier else "unavailable",
    }

    resolved_instruction = instruction if instruction is not None else (first_user_text or "")
    if instruction is None and first_user_text:
        warnings.append("task instruction taken from first user message; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": "claude-adapter-0.1",
        "source_type": "claude_session",
        "adapter_version": CLAUDE_ADAPTER_VERSION,
        "capture_completeness": "complete" if verifier else "partial",
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
