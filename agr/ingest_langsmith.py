"""LangSmith adapter (spec §5.3; docs/span-adapters.md).

Converts a LangSmith run export — either the SDK's ``list_runs`` output
serialized as a bare JSON list of run dicts, or a bulk export shaped
``{"runs": [...], "feedback": [...]}`` — into the ATIF-shaped document
``agr`` ingests.

LangSmith is structurally the closest of the three span-tree sources to
Langfuse: runs carry ``run_type`` (``llm``/``tool``/``chain``/``retriever``/
``embedding``/``prompt``/``parser``), ``inputs``, ``outputs``, an ``error``
string, ``parent_run_id``, ``trace_id``, and per-run token counts — a direct
fit for :mod:`agr.span_tree`. Feedback records map to ``verifier.checks[]``
the same way Langfuse ``scores`` do (docs/span-adapters.md).

Field mapping:

    run_type            -> normalized kind
    ------------------------------------------------------------------
    llm                 -> model
    embedding           -> model (usage only)
    tool, retriever     -> tool
    chain, prompt,
    parser, other        -> chain (generic grouping)

One deliberate, honestly-labelled scope reduction from the OTel/Langfuse
adapters: LangSmith's ``run_type`` vocabulary carries no explicit
agent/subagent marker (no ``invoke_agent`` operation, no ``AGENT``
observation type) — a nested ``chain`` run is LangSmith's generic grouping
for ANY sub-workflow, not specifically a subagent invocation. Guessing which
chain runs are "really" subagents from their name or tags would be exactly
the kind of invented identity ``docs/adapters.md`` rules out, so this
adapter does not attempt subagent grouping: every tool/model run is
attributed to ``main_agent``, and a warning says so whenever the trace
contains nested chain runs, so a reviewer knows the flat attribution is a
source limitation, not a claim that the run had no substructure.

Errors are read directly from LangSmith's own ``error`` field (a plain
string, not the ambiguous ``level``-vs-content situation Langfuse's
integrations create) — a much more reliable native signal, so no JSON-error
content decoding is needed here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from .adapter import AdapterResult
from .ingest_langfuse import _CATEGORICAL_FAIL, _CATEGORICAL_PASS  # reused, not re-derived
from .schema import CHECK_STATUSES
from .span_tree import NormalizedSpan, flatten_span_tree

# Provenance stamp written into every document this adapter emits. Bump when
# the run_type mapping or the feedback->check mapping changes materially.
LANGSMITH_ADAPTER_VERSION = "langsmith-adapter-0.1"

# LangSmith run_type -> normalized span kind. See the module docstring for
# why "chain" (the only grouping type LangSmith has) maps to the flattener's
# generic "chain" kind rather than "agent".
_RUN_TYPE_TO_KIND = {
    "llm": "model",
    "embedding": "model",
    "tool": "tool",
    "retriever": "tool",
    "chain": "chain",
    "prompt": "chain",
    "parser": "chain",
}


def _parse_ts(ts: Any) -> float:
    """LangSmith timestamps are ISO-8601 strings; reduce to a sortable float.
    Mirrors ``ingest_langfuse._parse_ts``; never raises."""
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _extract_usage(run: dict) -> Optional[dict]:
    """Per-run token counts, checked in priority order across the shapes
    different LangSmith SDK/export versions have used: direct top-level
    fields (older), then ``outputs.usage_metadata`` (newer LangChain
    convention), then ``extra.metadata`` token fields."""
    in_tok = run.get("prompt_tokens")
    out_tok = run.get("completion_tokens")
    if in_tok is None and out_tok is None:
        outputs = run.get("outputs") or {}
        usage_meta = outputs.get("usage_metadata") if isinstance(outputs, dict) else None
        if isinstance(usage_meta, dict):
            in_tok = usage_meta.get("input_tokens")
            out_tok = usage_meta.get("output_tokens")
    if in_tok is None and out_tok is None:
        extra = run.get("extra") or {}
        meta = extra.get("metadata") if isinstance(extra, dict) else None
        if isinstance(meta, dict):
            in_tok = meta.get("prompt_tokens") or meta.get("input_tokens")
            out_tok = meta.get("completion_tokens") or meta.get("output_tokens")
    if in_tok is None and out_tok is None:
        return None
    usage: dict[str, Any] = {}
    if in_tok is not None:
        usage["input_tokens"] = in_tok
    if out_tok is not None:
        usage["output_tokens"] = out_tok
    return usage


def _feedback_status(fb: dict) -> str:
    """Map one LangSmith feedback record to a verifier check status.

    LangSmith feedback carries a numeric ``score`` and/or a ``value``
    (bool/string), with no declared dataType the way Langfuse scores have.
    Mirrors ``ingest_langfuse._score_status``'s narrowness: only the
    unambiguous cases (a boolean, an exact 0/1, or a small closed
    pass/fail-like string vocabulary — reused from the Langfuse adapter, not
    re-derived) map to passed/failed; anything else is "unknown" rather than
    a guessed threshold on an arbitrary scale.
    """
    score = fb.get("score")
    if isinstance(score, bool):
        return "passed" if score else "failed"
    if isinstance(score, (int, float)):
        if score == 1 or score == 1.0:
            return "passed"
        if score == 0 or score == 0.0:
            return "failed"
        return "unknown"
    value = fb.get("value")
    if isinstance(value, bool):
        return "passed" if value else "failed"
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _CATEGORICAL_PASS:
            return "passed"
        if low in _CATEGORICAL_FAIL:
            return "failed"
    return "unknown"


def _feedback_to_verifier(feedback: list, warnings: list[str]) -> Optional[dict]:
    """LangSmith ``feedback`` records -> a ``verifier.checks[]`` sidecar.

    Mirrors ``ingest_langfuse._scores_to_verifier``. Returns ``None`` when
    there is no feedback — the caller keeps the run with no verifier evidence
    rather than writing an empty, misleading sidecar.
    """
    if not feedback:
        return None
    checks: list[dict] = []
    for i, fb in enumerate(feedback):
        if not isinstance(fb, dict) or not (fb.get("key") or fb.get("id")):
            warnings.append(f"feedback record {i} has no key/id; skipped, not dropped silently")
            continue
        status = _feedback_status(fb)
        if status == "unknown":
            warnings.append(
                f"feedback {fb.get('key') or fb.get('id')!r} (score={fb.get('score')!r}, "
                f"value={fb.get('value')!r}) has no clear pass/fail mapping; recorded as "
                f"'unknown', not guessed"
            )
        check: dict[str, Any] = {
            "check_id": str(fb.get("id") or f"langsmith_feedback_{i}"),
            "name": str(fb.get("key") or f"feedback_{i}"),
            "status": status if status in CHECK_STATUSES else "unknown",
            "source": "instrumented_assertion",
            # Post-run is the conservative default: LangSmith feedback
            # typically comes from an eval run or an annotation queue after
            # the trace completed, never presented as information the agent
            # itself had.
            "timing": "post_run",
            "source_pointers": [f"feedback[{i}]"],
        }
        if fb.get("comment"):
            check["comment"] = fb["comment"]
        if fb.get("run_id"):
            check["source_pointers"].append(f"run:{fb['run_id']}")
        checks.append(check)
    if not checks:
        return None
    return {"raw_output": json.dumps({"feedback": feedback}, ensure_ascii=False), "checks": checks}


def _load_export(source: Union[str, Path]) -> tuple[list, list]:
    """Accepts a bare list of run dicts (``list_runs`` output) or
    ``{"runs": [...], "feedback": [...]}`` (a bulk export)."""
    path = Path(source)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data, []
    if isinstance(data, dict) and "runs" in data:
        return data.get("runs") or [], data.get("feedback") or []
    raise ValueError(
        f"{path.name}: not a LangSmith run export (expected a JSON list of runs, or a "
        f"{{'runs': [...], 'feedback': [...]}} object)"
    )


def _normalize(runs: list, warnings: list[str]) -> list[NormalizedSpan]:
    run_ids = {str(r.get("id")) for r in runs if isinstance(r, dict) and r.get("id")}
    normalized: list[NormalizedSpan] = []
    unmapped_types: set[str] = set()
    nested_chain_count = 0

    for r in runs:
        if not isinstance(r, dict):
            warnings.append("non-object run skipped, not dropped silently")
            continue
        run_id = r.get("id")
        if not run_id:
            warnings.append("run with no id skipped, not dropped silently")
            continue
        run_id = str(run_id)
        parent_id = r.get("parent_run_id")
        parent_id = str(parent_id) if parent_id and str(parent_id) in run_ids else None

        run_type = str(r.get("run_type") or "").strip().lower()
        kind = _RUN_TYPE_TO_KIND.get(run_type)
        if kind is None:
            kind = "other"
            if run_type:
                unmapped_types.add(run_type)
        if kind == "chain" and parent_id is not None:
            nested_chain_count += 1

        inputs = r.get("inputs")
        outputs = r.get("outputs")
        error = r.get("error")
        if error:
            status = "error"
        elif outputs is not None:
            status = "ok"
        else:
            status = "unknown"

        name = str(r.get("name") or run_id)

        thread_id = None
        extra = r.get("extra") or {}
        if isinstance(extra, dict):
            meta = extra.get("metadata") or {}
            if isinstance(meta, dict):
                thread_id = meta.get("thread_id")
        session_id = thread_id or r.get("session_id") or r.get("trace_id")

        normalized.append(NormalizedSpan(
            span_id=run_id,
            parent_id=parent_id,
            start_time=_parse_ts(r.get("start_time")),
            kind=kind,
            name=name,
            status=status,
            error_message=str(error) if error else None,
            input_captured=inputs is not None,
            output_captured=outputs is not None,
            inputs=inputs,
            outputs=outputs,
            usage=_extract_usage(r),
            session_id=str(session_id) if session_id else None,
            timestamp=str(r.get("start_time")) if r.get("start_time") else None,
        ))

    if unmapped_types:
        warnings.append(
            f"unrecognised LangSmith run_type value(s) {sorted(unmapped_types)!r}; "
            f"mapped to a generic grouping step, never dropped silently"
        )
    if nested_chain_count:
        warnings.append(
            f"{nested_chain_count} nested 'chain' run(s) found; LangSmith's run_type carries "
            f"no explicit agent/subagent marker, so every tool/model run is attributed to "
            f"main_agent rather than a guessed subagent boundary (see module docstring)"
        )
    return normalized


def convert(
    source: Union[str, Path],
    *,
    task_id: Optional[str] = None,
    instruction: Optional[str] = None,
    run_id: Optional[str] = None,
    verifier: Optional[dict] = None,
    sweep_id: Optional[str] = None,
    configuration_id: Optional[str] = None,
) -> AdapterResult:
    """Convert one LangSmith run export into an ATIF-shaped document.

    Pure function: reads the file, normalizes the run tree, and delegates to
    :func:`agr.span_tree.flatten_span_tree`.
    """
    runs, feedback = _load_export(source)
    if not runs:
        raise ValueError(f"{Path(source).name}: LangSmith export carries no runs")

    warnings: list[str] = []
    normalized = _normalize(runs, warnings)
    result = flatten_span_tree(normalized)
    warnings.extend(result.warnings)

    trace_key = result.meta["root_span_id"]
    derived_task_id = task_id or f"langsmith-trace-{trace_key[:16]}"
    if task_id is None:
        warnings.append(f"task_id derived from the root run id ({derived_task_id}); confirm the contract")
    resolved_run_id = run_id or f"langsmith__{derived_task_id}__{trace_key}"

    model = result.meta.get("model") or "unresolved"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": derived_task_id,
        "model": model,
        "agent": result.meta.get("root_name") or "langsmith-agent",
        "harness_version": "langsmith-runs",
    }
    if result.meta.get("session_id"):
        run["source_session_id"] = result.meta["session_id"]
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id

    resolved_instruction = instruction if instruction is not None else (result.meta.get("instruction") or "")
    if instruction is None and result.meta.get("instruction"):
        warnings.append("task instruction taken from the root run's captured input; confirm the contract")

    capabilities = dict(result.capabilities)

    if verifier is None:
        fb_warnings: list[str] = []
        verifier = _feedback_to_verifier(feedback, fb_warnings)
        warnings.extend(fb_warnings)
        if verifier:
            capabilities["verifier_results"] = "complete"
            warnings.append(
                f"verifier synthesised from {len(verifier['checks'])} LangSmith feedback "
                f"record(s) (source: instrumented_assertion)"
            )

    doc: dict[str, Any] = {
        "atif_version": LANGSMITH_ADAPTER_VERSION,
        "source_type": "langsmith",
        "adapter_version": LANGSMITH_ADAPTER_VERSION,
        "capture_completeness": "complete" if verifier else "partial",
        "run": run,
        "capabilities": capabilities,
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": result.steps,
    }
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append(
            "no verifier supplied and no feedback records: run ingests with no verifier "
            "evidence (§7.2)"
        )

    return AdapterResult(
        doc=doc,
        warnings=warnings,
        meta={"run_count": len(runs), "derived_steps": len(result.steps),
              "retry_count": result.meta.get("retry_count", 0)},
    )


class LangSmithAdapter:
    """The LangSmith adapter, exposed through the shared ``Adapter`` protocol
    (spec §5.3). A thin object over the pure ``convert`` function so the CLI
    can dispatch to it by name via the registry."""

    name = "langsmith"
    version = LANGSMITH_ADAPTER_VERSION

    def convert(
        self,
        source: Any,
        *,
        task_id: Optional[str] = None,
        instruction: Optional[str] = None,
        run_id: Optional[str] = None,
        verifier: Optional[dict] = None,
        sweep_id: Optional[str] = None,
        configuration_id: Optional[str] = None,
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


LANGSMITH_ADAPTER = LangSmithAdapter()
