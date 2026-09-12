"""OTel GenAI adapter (spec §5.3; docs/span-adapters.md).

Converts an OTLP JSON export (the JSON serialization of
``ExportTraceServiceRequest`` — ``{"resourceSpans": [...]}}``) whose spans
follow the OpenTelemetry GenAI semantic conventions into the ATIF-shaped
document ``agr`` ingests. This is the highest-leverage of the three planned
span-tree sources: Langfuse ingests OTLP natively and LangSmith accepts it, so
anything already emitting spec-compliant GenAI spans is covered once this
exists (docs/span-adapters.md, "OTel GenAI spans (build first)").

This adapter does exactly two things:

1. Reduce OTLP's own span shape (``traceId``/``spanId``/``parentSpanId``,
   typed ``attributes``, an enum ``status``) into :class:`agr.span_tree.NormalizedSpan`.
   Field mapping (docs/span-adapters.md table):

   =========================  =============================================
   ATIF target                OTel GenAI source
   =========================  =============================================
   step kind                  ``gen_ai.operation.name``: ``invoke_agent`` ->
                               agent (structural/subagent grouping),
                               ``chat``/``generate_content``/``text_completion``
                               / ``embeddings`` -> model, ``execute_tool`` ->
                               tool. Anything else (or a span the root, with
                               no gen_ai attributes at all) falls back to
                               "agent" for the trace root, "other" otherwise.
   ``tool``                   ``gen_ai.tool.name``
   subagent grouping           ``execute_tool`` nested under an
                               ``invoke_agent {gen_ai.agent.name}`` parent
                               (handled by ``agr.span_tree``'s actor
                               resolution — this adapter only supplies the
                               ``kind="agent"`` / name mapping)
   usage/cost                 ``gen_ai.usage.input_tokens`` /
                               ``gen_ai.usage.output_tokens``
   error/status                span ``status`` + ``error.type``
   session grouping             ``gen_ai.conversation.id``
   =========================  =============================================

2. Delegate the actual flattening (ordering, tool-call/result fan-out,
   subagent attribution, retry recognition, terminal-event selection,
   capability declarations) to :func:`agr.span_tree.flatten_span_tree` — see
   that module for the shared logic every span-tree source relies on.

**The content catch** (docs/span-adapters.md): OTel GenAI instrumentations
*SHOULD NOT* capture message/tool content by default and *SHOULD* offer an
opt-in. Content is read from a small set of known attribute keys per span
kind (``gen_ai.tool.call.arguments``/``gen_ai.tool.call.result`` for tools,
``gen_ai.input.messages``/``gen_ai.output.messages`` for model calls, a
generic ``input.value``/``output.value`` pair — used by several
non-GenAI-specific tracers — for the run-level root span). Absent keys are
recorded as NOT captured, never as "empty content" — that distinction is what
drives ``agr.span_tree``'s honest ``partial``/``unavailable`` capability
declarations instead of a guessed pass.

Identity, verifier evidence, and task instruction confirmation follow the
same rules as every other adapter (docs/adapters.md): supplied by the caller
or conservatively derived from the trace itself, never invented, always with
a warning when a fallback is used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Union

from .adapter import AdapterResult
from .span_tree import NormalizedSpan, flatten_span_tree

# Provenance stamp written into every document this adapter emits. Bump when
# the OTLP field mapping changes materially (spec §5.3 versioning rule: OTel
# GenAI conventions are still evolving, so this pin matters).
OTEL_ADAPTER_VERSION = "otel-adapter-0.2"

# GenAI semantic-convention operation names this adapter recognises, and the
# normalized span kind each reduces to. Anything else falls through to
# "other" (a generic/unmapped span) with a warning, never a guess.
_OPERATION_TO_KIND = {
    "invoke_agent": "agent",
    "create_agent": "agent",
    "chat": "model",
    "generate_content": "model",
    "text_completion": "model",
    "embeddings": "model",
    "execute_tool": "tool",
}

# Attribute keys read as this span kind's captured input/output, in priority
# order. A generic ``input.value``/``output.value`` pair (used by several
# non-GenAI-specific tracers, and the only reasonable place a root
# invoke_agent span's own task instruction/final output would live — GenAI
# semconv defines no dedicated attribute for "the whole run's input") is
# tried last for every kind, never instead of the GenAI-specific keys.
_INPUT_KEYS = {
    "tool": ("gen_ai.tool.call.arguments",),
    "model": ("gen_ai.input.messages",),
    "agent": (),
}
_OUTPUT_KEYS = {
    "tool": ("gen_ai.tool.call.result",),
    "model": ("gen_ai.output.messages",),
    "agent": (),
}
_GENERIC_INPUT_KEYS = ("input.value",)
_GENERIC_OUTPUT_KEYS = ("output.value",)


def _decode_attr_value(value: dict) -> Any:
    """Decode one OTLP ``AnyValue`` JSON object to a plain Python value.

    Handles the scalar cases every GenAI attribute this adapter reads
    actually uses (string/int/double/bool); ``arrayValue``/``kvlistValue`` are
    decoded structurally so nothing throws, but no field this adapter reads
    is expected to need them.
    """
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        iv = value["intValue"]
        try:
            return int(iv)
        except (TypeError, ValueError):
            return iv
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [_decode_attr_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {kv["key"]: _decode_attr_value(kv.get("value", {}))
                for kv in value["kvlistValue"].get("values", [])}
    return None


def _decode_attrs(attr_list: Any) -> dict[str, Any]:
    """OTLP JSON's ``attributes`` is a list of ``{"key": ..., "value": {...}}``
    objects, never a plain dict — decode it into one for easy lookup."""
    out: dict[str, Any] = {}
    if not isinstance(attr_list, list):
        return out
    for kv in attr_list:
        if not isinstance(kv, dict) or "key" not in kv:
            continue
        out[kv["key"]] = _decode_attr_value(kv.get("value") or {})
    return out


def _status_code(status: Any) -> str:
    """Normalize OTLP's ``Status.code`` (string enum name or its integer) to
    ``"ok"`` / ``"error"`` / ``"unknown"``.

    OTel's own default is UNSET (0), not OK — a span nobody bothered to mark
    is genuinely unknown, never promoted to a success (mirrored by
    ``agr.span_tree``'s handling of ``status="unknown"``).
    """
    if not isinstance(status, dict):
        return "unknown"
    code = status.get("code")
    if code in ("STATUS_CODE_OK", 1):
        return "ok"
    if code in ("STATUS_CODE_ERROR", 2):
        return "error"
    return "unknown"


def _ns_to_iso(ns: Any) -> Optional[str]:
    """Best-effort ``startTimeUnixNano`` (a decimal string per protobuf JSON
    mapping) -> an ISO-8601 timestamp for display. ``None`` on anything that
    doesn't parse — never a guessed time."""
    try:
        seconds = int(ns) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _extract_io(attrs: dict, kind: str) -> tuple[Any, bool, Any, bool]:
    """Read (input, input_captured, output, output_captured) for one span.

    A key's mere PRESENCE marks capture, independent of whether the resulting
    value is empty — an instrumentation that captured an empty tool result is
    different from one that never captured results at all, and only the
    latter should degrade a capability declaration.
    """
    input_keys = _INPUT_KEYS.get(kind, ()) + _GENERIC_INPUT_KEYS
    output_keys = _OUTPUT_KEYS.get(kind, ()) + _GENERIC_OUTPUT_KEYS
    input_val, input_captured = None, False
    for key in input_keys:
        if key in attrs:
            input_val, input_captured = attrs[key], True
            break
    output_val, output_captured = None, False
    for key in output_keys:
        if key in attrs:
            output_val, output_captured = attrs[key], True
            break
    return input_val, input_captured, output_val, output_captured


def _iter_raw_spans(doc: dict) -> list[dict]:
    """Flatten OTLP's resourceSpans -> scopeSpans -> spans nesting."""
    spans: list[dict] = []
    for rs in doc.get("resourceSpans") or []:
        if not isinstance(rs, dict):
            continue
        for ss in rs.get("scopeSpans") or []:
            if not isinstance(ss, dict):
                continue
            for sp in ss.get("spans") or []:
                if isinstance(sp, dict):
                    spans.append(sp)
    return spans


def _load_otlp_documents(source: Union[str, Path, list]) -> list[dict]:
    """Read one or more OTLP JSON files (see the module docstring)."""
    paths = source if isinstance(source, (list, tuple)) else [source]
    docs: list[dict] = []
    for p in paths:
        path = Path(p)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or "resourceSpans" not in data:
            raise ValueError(
                f"{path.name}: not an OTLP JSON export (expected a top-level "
                f"'resourceSpans' key)"
            )
        docs.append(data)
    return docs


def _normalize(raw_spans: list[dict], warnings: list[str]) -> list[NormalizedSpan]:
    """Reduce raw OTLP span dicts into the flattener's NormalizedSpan shape."""
    # First pass: which span ids are roots (no parent, or an empty/absent
    # parentSpanId) — needed because a rootless span with no gen_ai.operation
    # attribute at all still needs a kind, and "agent" is the only sensible
    # fallback for whatever wraps the entire trace.
    span_ids = {sp.get("spanId") for sp in raw_spans if sp.get("spanId")}
    normalized: list[NormalizedSpan] = []
    unrecognised_ops: set[str] = set()

    for sp in raw_spans:
        span_id = sp.get("spanId")
        if not span_id:
            warnings.append("OTLP span with no spanId skipped, not dropped silently")
            continue
        parent_id = sp.get("parentSpanId") or None
        if parent_id not in span_ids:
            parent_id = None  # dangling/absent parent -> a root candidate

        attrs = _decode_attrs(sp.get("attributes"))
        operation = attrs.get("gen_ai.operation.name")
        is_root = parent_id is None

        if operation in _OPERATION_TO_KIND:
            kind = _OPERATION_TO_KIND[operation]
        elif is_root:
            kind = "agent"
            if operation is not None:
                unrecognised_ops.add(str(operation))
        else:
            kind = "other"
            if operation is not None:
                unrecognised_ops.add(str(operation))

        if kind == "tool":
            name = attrs.get("gen_ai.tool.name") or sp.get("name") or "tool"
        elif kind == "model":
            name = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model") \
                or sp.get("name") or "model"
        elif kind == "agent":
            name = attrs.get("gen_ai.agent.name") or sp.get("name") or "agent"
        else:
            name = sp.get("name") or "span"

        status = _status_code(sp.get("status"))
        error_type = attrs.get("error.type")
        error_message = (sp.get("status") or {}).get("message") if isinstance(sp.get("status"), dict) else None

        input_val, input_captured, output_val, output_captured = _extract_io(attrs, kind)

        usage = None
        in_tok, out_tok = attrs.get("gen_ai.usage.input_tokens"), attrs.get("gen_ai.usage.output_tokens")
        if in_tok is not None or out_tok is not None:
            usage = {}
            if in_tok is not None:
                usage["input_tokens"] = in_tok
            if out_tok is not None:
                usage["output_tokens"] = out_tok

        try:
            start_time = int(sp.get("startTimeUnixNano") or 0)
        except (TypeError, ValueError):
            start_time = 0

        normalized.append(NormalizedSpan(
            span_id=span_id,
            parent_id=parent_id,
            start_time=start_time,
            kind=kind,
            name=str(name),
            status=status,
            error_type=str(error_type) if error_type is not None else None,
            error_message=str(error_message) if error_message else None,
            input_captured=input_captured,
            output_captured=output_captured,
            inputs=input_val,
            outputs=output_val,
            usage=usage,
            session_id=attrs.get("gen_ai.conversation.id"),
            timestamp=_ns_to_iso(sp.get("startTimeUnixNano")),
            end_timestamp=_ns_to_iso(sp.get("endTimeUnixNano")),
            attributes=attrs,
        ))

    if unrecognised_ops:
        warnings.append(
            f"unrecognised gen_ai.operation.name value(s) {sorted(unrecognised_ops)!r}; "
            f"span(s) mapped to a generic step or, at the trace root, treated as the "
            f"agent span — confirm against the GenAI semantic-convention version in use"
        )
    return normalized


def _default_configuration_id(model: Optional[str], tool_names: set[str]) -> str:
    """A deterministic fallback configuration identity from model + tool set
    (docs/span-adapters.md, "Session grouping differs" / "never invent
    identity" — this is the one thing the design doc explicitly sanctions as
    a substitute when the source names no pinned configuration)."""
    material = {"model": model, "tools": sorted(tool_names)}
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return f"otel-config-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def convert(
    source: Union[str, Path, list],
    *,
    task_id: Optional[str] = None,
    instruction: Optional[str] = None,
    run_id: Optional[str] = None,
    verifier: Optional[dict] = None,
    sweep_id: Optional[str] = None,
    configuration_id: Optional[str] = None,
) -> AdapterResult:
    """Convert one OTLP JSON export (or a list of them sharing one trace)
    into an ATIF-shaped document.

    Pure function: reads the file(s), normalizes the spans, and delegates to
    :func:`agr.span_tree.flatten_span_tree`.
    """
    docs = _load_otlp_documents(source)
    raw_spans = [sp for doc in docs for sp in _iter_raw_spans(doc)]
    if not raw_spans:
        names = ", ".join(str(Path(p).name) for p in (source if isinstance(source, (list, tuple)) else [source]))
        raise ValueError(f"{names}: OTLP export carries no spans")

    warnings: list[str] = []
    normalized = _normalize(raw_spans, warnings)
    result = flatten_span_tree(normalized)
    warnings.extend(result.warnings)

    trace_id = raw_spans[0].get("traceId") or result.meta["root_span_id"]
    derived_task_id = task_id or f"otel-trace-{trace_id[:16]}"
    if task_id is None:
        warnings.append(f"task_id derived from the trace id ({derived_task_id}); confirm the contract")
    resolved_run_id = run_id or f"otel__{derived_task_id}__{trace_id}"

    model = result.meta.get("model") or "unresolved"
    tool_names = {s["tool"] for s in result.steps if s.get("kind") == "tool_call" and s.get("tool")}

    run: dict[str, Any] = {
        "logical_run_id": resolved_run_id,
        "task_id": derived_task_id,
        "model": model,
        "agent": result.meta.get("root_name") or "otel-agent",
        "harness_version": "otel-genai",
    }
    if result.meta.get("session_id"):
        run["source_session_id"] = result.meta["session_id"]
    if sweep_id:
        run["sweep_id"] = sweep_id
    if configuration_id:
        run["configuration_id"] = configuration_id
    elif tool_names or model != "unresolved":
        run["configuration_id"] = _default_configuration_id(model, tool_names)
        warnings.append(
            "configuration_id defaulted from a hash of (model, tool set); pass "
            "--configuration-id for an explicit one"
        )

    resolved_instruction = instruction if instruction is not None else (result.meta.get("instruction") or "")
    if instruction is None and result.meta.get("instruction"):
        warnings.append("task instruction taken from the root span's captured input; confirm the contract")

    doc: dict[str, Any] = {
        "atif_version": OTEL_ADAPTER_VERSION,
        "source_type": "otel",
        "adapter_version": OTEL_ADAPTER_VERSION,
        "capture_completeness": "complete" if verifier else "partial",
        "run": run,
        "capabilities": dict(result.capabilities),
        "task": {"instruction": resolved_instruction, "artifacts": [], "requirements": []},
        "steps": result.steps,
    }
    if verifier:
        doc["verifier"] = verifier
    else:
        warnings.append(
            "no verifier supplied: run ingests with no verifier evidence (§7.2) — OTel "
            "GenAI traces carry no native pass/fail signal"
        )

    return AdapterResult(
        doc=doc,
        warnings=warnings,
        meta={"span_count": len(raw_spans), "derived_steps": len(result.steps),
              "subagent_names": result.meta.get("subagent_names", []),
              "retry_count": result.meta.get("retry_count", 0)},
    )


class OtelAdapter:
    """The OTel GenAI adapter, exposed through the shared ``Adapter``
    protocol (spec §5.3). A thin object over the pure ``convert`` function so
    the CLI can dispatch to it by name via the registry."""

    name = "otel"
    version = OTEL_ADAPTER_VERSION

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


OTEL_ADAPTER = OtelAdapter()
