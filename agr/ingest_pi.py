"""Pi session adapter (spec §5.3 interoperability strategy).

Converts a pi session file (JSONL, see pi ``docs/session-format.md``) into the
minimal ATIF-shaped JSON contract that ``agr.ingest`` imports. This is a pure
format mapping: no analysis, no model calls, and nothing is invented that the
source did not capture.

What pi captures natively (and this adapter preserves):

- tool calls and results, linked by ``toolCallId``, with ``isError``;
- bash executions with ``exitCode`` / ``cancelled`` / ``truncated``;
- assistant messages with provider / model / usage / stopReason;
- compaction entries with ``tokensBefore`` and, on newer harnesses, the
  retained post-compaction tail (``retainedTail``) — which upgrades
  ``pre_post_compaction_context`` from the partial level other sources reach;
- model / thinking-level changes and per-message token & cost usage.

What pi does *not* capture — task identity, requirements, and the verifier
result — must be supplied by the caller (the benchmark runner or the human
running the eval). They are explicit adapter inputs, never guessed:

- ``--task-id`` / ``--instruction`` (default: first user message text), and
- ``--verifier sidecar.json`` carrying ``raw_output`` and atomic ``checks``.
  Without it the run ingests with *no verifier evidence*: the pipeline already
  treats missing checks as an explicit ingestion state, not a pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .adapter import AdapterResult

# Provenance stamp written into every document this adapter emits (and recorded
# on the immutable capture). Bump when the mapping changes materially.
PI_ADAPTER_VERSION = "pi-adapter-0.2"

# Pi session-entry types that carry no trajectory content for analysis.
_SKIP_ENTRY_TYPES = {
    "session",            # header: handled separately
    "label",              # human bookmarks, not agent behaviour
    "session_info",       # display name only
    "custom",             # extension state, never in LLM context
    "thinking_level_change",
    "model_change",       # recorded in run metadata, not a trajectory step
}

# The pi adapter returns the shared adapter result type. The alias is kept for
# backwards compatibility; adapter-specific extras (the entry count) live in
# ``AdapterResult.meta``.
PiAdapterResult = AdapterResult


def _read_jsonl(path: Path) -> list[dict]:
    entries: list[dict] = []
    # utf-8-sig: tolerate a byte-order mark (logs written by Windows tooling).
    with path.open("r", encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{lineno}: invalid JSON ({exc})") from exc
    if not entries or entries[0].get("type") != "session":
        raise ValueError(f"{path.name}: not a pi session file (missing session header)")
    return entries


def _active_branch(entries: Iterable[dict]) -> list[dict]:
    """Reduce the entry tree to the active branch (leaf -> root walk).

    Pi sessions are trees (branching via /tree, /fork). The trajectory that
    produced the final state is the path from the current leaf to the root.
    Branch summaries on that path are kept as context; abandoned branches are
    not part of this run's trajectory.
    """
    by_id: dict[str, dict] = {}
    leaf: dict | None = None
    for e in entries:
        if e.get("type") == "session":
            continue
        eid = e.get("id")
        if not eid:
            continue
        by_id[eid] = e
        leaf = e  # file order is append order; last entry is the leaf
    path: list[dict] = []
    seen: set[str] = set()
    node = leaf
    while node is not None:
        nid = node["id"]
        if nid in seen:  # defensive: a cycle would loop forever
            break
        seen.add(nid)
        path.append(node)
        node = by_id.get(node.get("parentId"))
    path.reverse()
    return path


def _text_of(content: Any) -> str:
    """Flatten pi message content (string or typed blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
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
) -> PiAdapterResult:
    """Convert one pi session JSONL file into an ATIF-shaped document.

    Pure function: reads the file, returns the document. Warnings describe
    every place the mapping had to make an explicit, conservative choice.
    """
    path = Path(session_path)
    entries = _read_jsonl(path)
    header = entries[0]
    branch = _active_branch(entries)

    warnings: list[str] = []
    steps: list[dict] = []
    seq = 0

    def add(kind: str, actor: str, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        steps.append({"step_id": f"s{seq}", "kind": kind, "actor": actor, **payload})

    first_user_text: str | None = None
    model: str | None = None
    provider: str | None = None
    saw_compaction_retained = False
    pending_calls: dict[str, dict] = {}  # toolCallId -> tool_call step payload

    for entry in branch:
        etype = entry.get("type")
        if etype in _SKIP_ENTRY_TYPES:
            continue

        if etype == "compaction":
            add(
                "context_compaction",
                "harness",
                summary=entry.get("summary", ""),
                tokens_before=entry.get("tokensBefore"),
            )
            if entry.get("retainedTail"):
                saw_compaction_retained = True
            else:
                warnings.append(
                    "compaction without retainedTail: pre/post-compaction visibility is partial"
                )
            continue

        if etype == "branch_summary":
            add("strategy_change", "harness", summary=entry.get("summary", ""))
            continue

        if etype not in ("message", "custom_message"):
            warnings.append(f"unmapped pi entry type {etype!r}; step skipped, not dropped silently")
            continue

        if etype == "custom_message":
            # Extension-injected context that the model did see.
            add("environment_observation", "harness", content=_text_of(entry.get("content")))
            continue

        msg = entry.get("message") or {}
        role = msg.get("role")

        if role == "user":
            text = _text_of(msg.get("content"))
            if first_user_text is None:
                first_user_text = text
                add("task_received", "harness", content=text)
            else:
                add("environment_observation", "user", content=text)

        elif role == "assistant":
            provider = msg.get("provider") or provider
            model = msg.get("model") or model
            for block in msg.get("content") or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    add("model_output", "main_agent", content=block.get("text", ""))
                elif btype == "thinking":
                    add("model_output", "main_agent", content=f"[thinking] {block.get('thinking', '')}")
                elif btype == "toolCall":
                    name = block.get("name", "tool")
                    args = block.get("arguments") or {}
                    call = {"tool": name}
                    # Hoist the most analysis-relevant argument into content so
                    # deterministic token matching (evidence slicing) can see it.
                    for key in ("command", "path"):
                        if isinstance(args.get(key), str):
                            call["content"] = args[key]
                            if key == "path":
                                call["path"] = args[key]
                            break
                    else:
                        call["content"] = json.dumps(args, ensure_ascii=False)[:2000]
                    add("tool_call", "main_agent", **call)
                    if block.get("id"):
                        pending_calls[block["id"]] = call
                # other block types (e.g. images) carry no textual trajectory content
            if msg.get("stopReason") == "error":
                add("error_observed", "harness", content=msg.get("errorMessage", "model error"))

        elif role == "toolResult":
            payload: dict[str, Any] = {
                "tool": msg.get("toolName", "tool"),
                "content": _text_of(msg.get("content")),
            }
            if msg.get("isError"):
                payload["exit_code"] = 1
            else:
                payload["exit_code"] = 0
            add("tool_result", "tool", **payload)
            pending_calls.pop(msg.get("toolCallId"), None)

        elif role == "bashExecution":
            # Bash executions are recorded as one atomic entry (command +
            # output together), so the call and its result are always emitted
            # as an adjacent pair; unlike id-linked tool calls they can never
            # go unmatched, so they deliberately do not join pending_calls.
            command = msg.get("command", "")
            add("tool_call", "main_agent", tool="bash", content=command)
            payload = {"tool": "bash", "content": msg.get("output", "")}
            ec = msg.get("exitCode")
            payload["exit_code"] = ec if isinstance(ec, int) else (1 if msg.get("cancelled") else 0)
            if msg.get("cancelled"):
                payload["content"] = "[cancelled] " + payload["content"]
            add("tool_result", "tool", **payload)

        elif role in ("compactionSummary", "branchSummary"):
            continue  # reconstructed context, already represented by their entries
        else:
            warnings.append(f"unmapped pi message role {role!r}; step skipped, not dropped silently")

    if pending_calls:
        warnings.append(f"{len(pending_calls)} tool call(s) never observed a result")

    if not steps:
        raise ValueError(f"{path.name}: session produced no trajectory steps")

    add("final_submission", "main_agent", content="[pi session ended — final assistant state]")
    add("run_finished", "harness", content="pi session closed")

    # --- run metadata -------------------------------------------------------
    header_id = header.get("id", path.stem)
    ts = header.get("timestamp", "")
    resolved_run_id = run_id or f"pi__{(task_id or 'session')}__{header_id[:8]}"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": task_id or f"pi-session-{header_id[:8]}",
        "model": model or "unresolved",
        "agent": "pi-coding-agent",
        "harness_version": f"pi-session-v{header.get('version', '?')}",
        "started_at": ts,
    }
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id

    # --- capabilities: only what pi genuinely captured ----------------------
    has_compaction = any(s["kind"] == "context_compaction" for s in steps)
    capabilities = {
        "messages": "complete",
        "tool_calls": "complete",
        "tool_results": "complete",
        "filesystem": "partial",       # observed only through tool I/O, never state-captured
        "process_state": "partial",    # bash exit codes, not process tables
        "compaction_boundary": "complete" if has_compaction else "unavailable",
        "pre_post_compaction_context": (
            "complete" if saw_compaction_retained else ("partial" if has_compaction else "unavailable")
        ),
        "verifier_code": "complete" if verifier else "unavailable",
    }

    resolved_instruction = instruction if instruction is not None else (first_user_text or "")
    if instruction is None and first_user_text:
        warnings.append("task instruction taken from first user message; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": "pi-adapter-0.1",
        "source_type": "pi_session",
        "adapter_version": PI_ADAPTER_VERSION,
        "capture_completeness": "complete" if verifier else "partial",
        "run": run,
        "capabilities": capabilities,
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": steps,
    }
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append("no verifier supplied: run ingests with no verifier evidence (§7.2)")

    return AdapterResult(doc=doc, warnings=warnings, meta={"entry_count": len(branch)})


def load_verifier(path: str | Path) -> dict:
    """Load a verifier sidecar: ``{"raw_output": ..., "checks": [...]}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or "checks" not in data:
        raise ValueError("verifier sidecar must be a JSON object with a 'checks' list")
    return data


class PiAdapter:
    """The pi-session adapter, exposed through the shared ``Adapter`` protocol
    (spec §5.3). A thin object over the pure ``convert`` function so the CLI can
    dispatch to it by name via the registry."""

    name = "pi"
    version = PI_ADAPTER_VERSION

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


PI_ADAPTER = PiAdapter()
