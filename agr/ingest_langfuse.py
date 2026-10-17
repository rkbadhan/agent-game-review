"""Langfuse adapter (spec §5.3; docs/span-adapters.md).

Converts a Langfuse trace into the ATIF-shaped document ``agr`` ingests.
Two source shapes are accepted (see ``_load_export``/``_normalize``):

1. The blob-storage/UI export shape this adapter originally targeted:
   ``{"trace": {...}, "observations": [...], "scores": [...]}`` — three
   top-level siblings.
2. The REAL live-API shape (``GET /api/public/traces/{id}``, and what
   :func:`agr.langfuse_api.fetch_trace` returns): a FLAT
   ``TraceWithFullDetails`` object — the trace's own fields (``id``,
   ``timestamp``, ``name``, ``input``, ``output``, ...) sit at the TOP
   level, with ``observations``/``scores`` nested inside as siblings of
   ``id``. There is no ``"trace"`` wrapper key at all. (0.2: this shape was
   not accepted before — a raw API response silently parsed with an EMPTY
   observation list, because the loader looked for a top-level ``"trace"``
   key that this shape never has. See the version note below.)

Langfuse's ten observation types map almost one-to-one onto
:mod:`agr.span_tree`'s normalized kinds (docs/span-adapters.md table):

    event, span, chain        -> generic grouping ("chain")
    generation                -> model span ("model")
    agent                     -> agent span ("agent")
    tool, retriever           -> tool span ("tool")
    evaluator, guardrail      -> check-like signal; represented as a generic
                                  grouping step (their real evidentiary value
                                  is the trace's own ``scores``, below)
    embedding                 -> model span, usage only ("model")

Two things this adapter gets right that a naive mapping would not:

1. **Errors are not just the ``level`` attribute.** Langfuse's own ``level``
   (``DEBUG``/``DEFAULT``/``WARNING``/``ERROR``) plus ``statusMessage`` is the
   first signal, but many integrations never set it — a tool that returned an
   error payload can still show ``DEFAULT`` at the span layer. So every
   observation's ``output`` is ALSO inspected for the same JSON-error shapes
   Harbor already decodes (``{"returncode": ...}`` and friends) — reusing
   ``agr.ingest_harbor._parse_returncode`` directly rather than re-deriving a
   second, divergent copy of that decoder. A decoded non-zero/zero return
   code always wins over an ambiguous or absent ``level``.
2. **Scores retain their evaluator-defined meaning.** Original values, types,
   subjects and provenance are kept in ``source_scores``. Only an explicit
   verifier sidecar establishes whole-task checks; metric polarity, thresholds
   and coverage are never guessed.


The actual span-tree flattening (ordering, tool-call/result fan-out, subagent
attribution, retry recognition, terminal-event selection, capability
declarations) is delegated to :func:`agr.span_tree.flatten_span_tree`; see
that module for the shared logic every span-tree source relies on.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from .adapter import AdapterResult
from .ingest_harbor import _parse_returncode  # reused, not re-derived (see module docstring)
from .span_tree import NormalizedSpan, flatten_span_tree

# Provenance stamp written into every document this adapter emits. Bump when
# the observation-type mapping or the score->check mapping changes materially.
# 0.2: two bugs fixed against the AUTHORITATIVE Langfuse API schema (this
#      adapter had only ever been exercised against the hand-written export
#      fixture, never a real API response):
#      - The loader required a top-level "trace" key. A raw
#        ``GET /api/public/traces/{id}`` response (``TraceWithFullDetails``)
#        is FLAT -- the trace's own fields are at the top level and
#        ``observations``/``scores`` are nested siblings of ``id``, with no
#        "trace" wrapper at all. That shape parsed "successfully" with a
#        SILENTLY EMPTY observation list (every real trace ingested as a bare
#        root span, no tool calls, no model output). Both shapes are now
#        accepted; see the module docstring.
#      - ``_score_status`` read a CATEGORICAL score's human label off
#        ``value``. Per Langfuse's ``ScoreV1`` schema, a categorical score's
#        ``value`` is a NUMERIC category-index mapping; the label a human
#        or evaluator actually wrote ("pass"/"fail"/...) is in
#        ``stringValue``. Every categorical score therefore always fell
#        through to "unknown" -- fixed to read ``stringValue`` (falling back
#        to ``value`` only when it happens to already be a string, e.g. a
#        non-conformant source).
#      Re-ingest is recommended for any store populated from adapter 0.1:
#      previously-ingested Langfuse runs may have zero observations and/or
#      categorical checks stuck at "unknown" that a re-run now resolves.
# 0.4: preserve trace display identity and keep scores as metrics; explicit verifier required.
LANGFUSE_ADAPTER_VERSION = "langfuse-adapter-0.5"

# Langfuse observation ``type`` -> normalized span kind (case-insensitive:
# the SDKs and the API have varied in casing across versions).
_TYPE_TO_KIND = {
    "event": "chain",
    "span": "chain",
    "chain": "chain",
    "generation": "model",
    "agent": "agent",
    "tool": "tool",
    "retriever": "tool",
    "evaluator": "chain",
    "guardrail": "chain",
    "embedding": "model",
}

# Shared categorical labels retained for the LangSmith feedback adapter.
_CATEGORICAL_PASS = {"true", "pass", "passed", "correct", "good"}
_CATEGORICAL_FAIL = {"false", "fail", "failed", "incorrect", "bad"}


def _parse_ts(ts: Any) -> float:
    """Langfuse timestamps are ISO-8601 strings; reduce to a sortable float.

    Returns ``0.0`` (never raises) on anything unparseable — ordering
    degrades gracefully rather than the whole conversion failing on one bad
    timestamp.
    """
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


def _resolve_status(level: Optional[str], output: Any) -> tuple[str, Optional[int]]:
    """The two-signal error resolution this adapter's docstring describes.

    Returns ``(status, decoded_exit_code)``. ``level == "ERROR"`` always wins.
    Otherwise, the output is checked for a decodable ``{"returncode": ...}``
    shape (Harbor's decoder, reused) — this can DOWNGRADE an apparently clean
    ``DEFAULT``/``DEBUG`` level to ``"error"``, which is exactly the gotcha
    this adapter exists to catch. With neither signal, ``DEFAULT``/``DEBUG``
    (an explicit non-error level) is ``"ok"``; ``WARNING`` and a genuinely
    absent level are ``"unknown"`` — a flagged concern, or no signal at all,
    is never promoted to a confirmed pass.
    """
    level_norm = (level or "").strip().upper()
    decoded_rc = _parse_returncode(output)
    if level_norm == "ERROR":
        return "error", decoded_rc
    if decoded_rc is not None:
        return ("ok" if decoded_rc == 0 else "error"), decoded_rc
    if level_norm in ("DEFAULT", "DEBUG"):
        return "ok", None
    return "unknown", None  # WARNING, or no level recorded at all


def _is_flat_trace_shape(data: dict) -> bool:
    """Is this dict a flat ``TraceWithFullDetails`` (the real API shape)?

    Recognised by ``id`` plus at least one of ``observations``/``scores`` —
    the two collections the API nests as siblings of ``id`` (see module
    docstring). A trace with genuinely zero observations AND zero scores
    would not be recognised by this check alone, but such a trace carries
    nothing this adapter could act on anyway.
    """
    return bool(data.get("id")) and ("observations" in data or "scores" in data)


def _load_export(source: Union[str, Path, dict]) -> dict:
    """Load a Langfuse export, or accept one already parsed.

    ``source`` is either a path to a JSON file, or a dict already in one of
    the two accepted shapes — the latter is what lets :func:`convert` take
    :func:`agr.langfuse_api.fetch_trace`'s return value directly, with no
    intermediate file round-trip.
    """
    if isinstance(source, dict):
        data: Any = source
        label = "<in-memory trace>"
    else:
        path = Path(source)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        label = path.name
    if not isinstance(data, dict) or not ("trace" in data or _is_flat_trace_shape(data)):
        raise ValueError(
            f"{label}: not a Langfuse trace export (expected a top-level 'trace' key — the "
            f"blob-storage/UI export shape — or a flat TraceWithFullDetails object with "
            f"'id' plus 'observations'/'scores' — the live API shape)"
        )
    return data


def _normalize(export: dict, warnings: list[str]) -> tuple[list[NormalizedSpan], str]:
    """Build the normalized span tree: the trace itself as the root span, and
    every observation as a child (or grandchild, via ``parentObservationId``)
    of it.

    ``export`` may be either accepted shape (see module docstring): the
    wrapped ``{"trace": {...}, ...}`` export, or a flat
    ``TraceWithFullDetails`` where the dict itself IS the trace. ``export.get
    ("trace") or export`` resolves both in one line — the wrapped shape's
    "trace" key wins when present, and a flat shape (which has no "trace"
    key, so this returns ``None``) falls back to treating the whole dict as
    the trace. ``observations``/``scores`` need no such branching: both
    shapes carry them as top-level keys of ``export`` (wrapped: siblings of
    "trace"; flat: siblings of "id", which is also all of ``export``).
    """
    trace = export.get("trace") or export
    trace_id = str(trace.get("id") or "trace")
    observations = export.get("observations") or []

    trace_input = trace.get("input")
    trace_output = trace.get("output")
    trace_level = str(trace.get("level") or "").strip().upper()
    root = NormalizedSpan(
        span_id=trace_id,
        parent_id=None,
        start_time=_parse_ts(trace.get("timestamp") or trace.get("createdAt")),
        kind="agent",
        name=str(trace.get("name") or "langfuse-trace"),
        status="error" if trace_level == "ERROR" else "unknown",
        error_message=trace.get("statusMessage") if trace_level == "ERROR" else None,
        input_captured=trace_input is not None,
        output_captured=trace_output is not None,
        inputs=trace_input,
        outputs=trace_output,
        session_id=trace.get("sessionId"),
        timestamp=str(trace.get("timestamp")) if trace.get("timestamp") else None,
        end_timestamp=str(trace.get("endTime")) if trace.get("endTime") else None,
    )

    spans = [root]
    unmapped_types: set[str] = set()
    for obs in observations:
        if not isinstance(obs, dict):
            warnings.append("non-object observation skipped, not dropped silently")
            continue
        obs_id = obs.get("id")
        if not obs_id:
            warnings.append("observation with no id skipped, not dropped silently")
            continue
        otype = str(obs.get("type") or "").strip().lower()
        kind = _TYPE_TO_KIND.get(otype)
        if kind is None:
            kind = "other"
            if otype:
                unmapped_types.add(otype)
        parent_id = obs.get("parentObservationId") or trace_id

        obs_input = obs.get("input")
        obs_output = obs.get("output")
        status, decoded_rc = _resolve_status(obs.get("level"), obs_output)

        usage = None
        usage_src = obs.get("usage") or obs.get("usageDetails")
        if isinstance(usage_src, dict):
            in_tok = usage_src.get("input") if "input" in usage_src else usage_src.get("promptTokens")
            out_tok = usage_src.get("output") if "output" in usage_src else usage_src.get("completionTokens")
            if in_tok is not None or out_tok is not None:
                usage = {}
                if in_tok is not None:
                    usage["input_tokens"] = in_tok
                if out_tok is not None:
                    usage["output_tokens"] = out_tok

        name = str(obs.get("name") or obs_id)
        model_name = obs.get("model")
        if kind == "model" and model_name:
            name = str(model_name)

        attrs: dict[str, Any] = {}
        if decoded_rc is not None:
            attrs["exit_code"] = decoded_rc

        spans.append(NormalizedSpan(
            span_id=str(obs_id),
            parent_id=str(parent_id),
            start_time=_parse_ts(obs.get("startTime")),
            kind=kind,
            name=name,
            status=status,
            error_message=obs.get("statusMessage") if status == "error" else None,
            input_captured=obs_input is not None,
            output_captured=obs_output is not None,
            inputs=obs_input,
            outputs=obs_output,
            usage=usage,
            session_id=trace.get("sessionId"),
            timestamp=str(obs.get("startTime")) if obs.get("startTime") else None,
            end_timestamp=str(obs.get("endTime")) if obs.get("endTime") else None,
            attributes=attrs,
        ))

    if unmapped_types:
        warnings.append(
            f"unrecognised Langfuse observation type(s) {sorted(unmapped_types)!r}; "
            f"mapped to a generic grouping step, never dropped silently"
        )
    return spans, trace_id


def run_id_for_trace(trace_id: str) -> str:
    """The logical run id :func:`convert` assigns a trace when no overrides are given."""
    return f"langfuse__langfuse-trace-{trace_id[:16]}__{trace_id}"


def convert(
    source: Union[str, Path, dict],
    *,
    task_id: Optional[str] = None,
    instruction: Optional[str] = None,
    run_id: Optional[str] = None,
    verifier: Optional[dict] = None,
    sweep_id: Optional[str] = None,
    configuration_id: Optional[str] = None,
) -> AdapterResult:
    """Convert one Langfuse trace into an ATIF-shaped document.

    Pure function: no network access happens here (that's
    :func:`agr.langfuse_api.fetch_trace`'s job, deliberately kept separate).
    ``source`` is a path to a Langfuse export file, OR an already-parsed
    dict in either accepted shape (see module docstring) — the latter lets a
    live-fetched trace be converted with no intermediate file. Normalizes
    trace + observations and delegates to
    :func:`agr.span_tree.flatten_span_tree`.
    """
    export = _load_export(source)
    warnings: list[str] = []
    normalized, trace_id = _normalize(export, warnings)
    result = flatten_span_tree(normalized)
    warnings.extend(result.warnings)

    derived_task_id = task_id or f"langfuse-trace-{trace_id[:16]}"
    if task_id is None:
        warnings.append(f"task_id derived from the trace id ({derived_task_id}); confirm the contract")
    resolved_run_id = run_id or f"langfuse__{derived_task_id}__{trace_id}"

    model = result.meta.get("model") or "unresolved"
    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": derived_task_id,
        "model": model,
        "agent": result.meta.get("root_name") or "langfuse-agent",
        "harness_version": "langfuse-export",
    }
    if result.meta.get("session_id"):
        run["source_session_id"] = result.meta["session_id"]
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id

    resolved_instruction = instruction if instruction is not None else (result.meta.get("instruction") or "")
    if instruction is None and result.meta.get("instruction"):
        warnings.append("task instruction taken from the trace's captured input; confirm the contract")

    capabilities = dict(result.capabilities)

    # Scores have evaluator-defined polarity and scope. Retain them as metrics;
    # only an explicit verifier can establish a whole-task pass/fail contract.
    scores = sorted(export.get("scores") or [], key=lambda score: json.dumps(score, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    summary = export.get("scores_fetch") or {"status": "available" if scores else "none"}
    run["evaluation_summary"] = {"score_status": summary["status"], "score_count": len(scores), "checks_available": bool((verifier or {}).get("checks"))}
    trace = export.get("trace") or export
    run["display_name"] = str(trace.get("name") or derived_task_id)
    source_context = export.get("source_context")
    context = {k: v for k, v in source_context.items() if k in ("host", "project_id") and isinstance(v, str)} if isinstance(source_context, dict) else {}
    # The source document is hashed to identify a capture, so only what identifies the trace may go in it. The search
    # window (`fetch_limits`) differs on every search and a project label can be renamed; neither describes the trace,
    # and either would turn an unchanged re-import into a new capture.
    context.update(trace_id=trace_id, retrieval="bounded" if export.get("fetch_limits") else "export")
    for key in ("environment", "tags", "release", "version", "sessionId", "userId"):
        if key in trace:
            context[key] = trace[key]
    if task_id is not None:
        context["task_id_explicit"] = True
    run["source_context"] = context
    if trace.get("timestamp"):
        run["started_at"] = trace["timestamp"]
    if scores and verifier is None:
        warnings.append("Langfuse scores are retained as metrics. Provide explicit evaluation checks to define task success; score polarity and observation scope are not inferred.")

    doc: dict[str, Any] = {
        "atif_version": LANGFUSE_ADAPTER_VERSION,
        "source_type": "langfuse",
        "adapter_version": LANGFUSE_ADAPTER_VERSION,
        "capture_completeness": "partial" if export.get("fetch_limits") or any(v == "partial" for v in result.capabilities.values()) else "complete",
        "run": run,
        "capabilities": capabilities,
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": result.steps,
    }
    # Retain root I/O separately from observations and explicit task overrides.
    # Previews redact these immutable values before returning them.
    doc["source_io"] = {key: trace[key] for key in ("input", "output") if key in trace}
    doc["source_scores"] = scores
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append(
            "no explicit verifier supplied: run ingests with no "
            "verifier evidence (§7.2)"
        )

    return AdapterResult(
        doc=doc,
        warnings=warnings,
        meta={"observation_count": len(export.get("observations") or []),
              "derived_steps": len(result.steps),
              "subagent_names": result.meta.get("subagent_names", []),
              "retry_count": result.meta.get("retry_count", 0)},
    )


class LangfuseAdapter:
    """The Langfuse adapter, exposed through the shared ``Adapter`` protocol
    (spec §5.3). A thin object over the pure ``convert`` function so the CLI
    can dispatch to it by name via the registry."""

    name = "langfuse"
    version = LANGFUSE_ADAPTER_VERSION

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


LANGFUSE_ADAPTER = LangfuseAdapter()
