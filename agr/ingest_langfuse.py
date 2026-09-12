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
2. **``scores`` are the first external verifier signal any adapter carries.**
   A Langfuse trace's ``scores`` (from a Langfuse-run evaluator, a human
   annotation queue, or the SDK's own ``score()`` call) map into
   ``verifier.checks[]`` with ``source: instrumented_assertion`` — stronger
   evidence than the reward-only Harbor path, and exactly the shape
   ``agr.verifier_synth`` already targets for in-session test results.

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
from .schema import CHECK_STATUSES
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
LANGFUSE_ADAPTER_VERSION = "langfuse-adapter-0.2"

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

# A small, closed set of dataType-appropriate strings/values a Langfuse score
# maps onto passed/failed. Anything outside this set is "unknown" — never a
# guessed threshold on an arbitrary numeric scale (see _score_status).
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


def _score_status(score: dict) -> str:
    """Map one Langfuse score to a verifier check status.

    Deliberately narrow: a boolean score maps directly; a categorical score
    maps only through the small closed vocabulary above; a numeric score maps
    only at its two unambiguous endpoints (0 or 1) — Langfuse numeric scores
    are on an arbitrary, evaluator-defined scale, so anywhere in between has
    no universal pass/fail meaning and is reported "unknown" rather than a
    guessed threshold.

    BOOLEAN vs. CATEGORICAL read DIFFERENT fields for their label, per
    Langfuse's ``ScoreV1`` schema — this split is not cosmetic:

    * BOOLEAN: ``value`` is the primary encoding (0 or 1); ``stringValue``
      ("True"/"False") is a convenience mirror of the same fact. ``value``
      is preferred, ``stringValue`` is the fallback.
    * CATEGORICAL: ``value`` is a NUMERIC category-INDEX the UI uses for
      sorting/charting, not a pass/fail signal at all — the human-readable
      label an evaluator or annotator actually chose ("pass", "correct", …)
      lives in ``stringValue``. Reading ``value`` here (a natural but wrong
      guess) means every categorical score silently falls through to
      "unknown" forever. ``value`` is consulted only as a last resort, and
      only when it happens to already be a string (a non-conformant source).
    """
    dtype = str(score.get("dataType") or "").strip().upper()
    value = score.get("value")
    string_value = score.get("stringValue")
    if dtype == "BOOLEAN":
        if isinstance(value, bool):
            return "passed" if value else "failed"
        if isinstance(value, (int, float)):
            return "passed" if value >= 1 else "failed"
        for candidate in (string_value, value):
            if isinstance(candidate, str):
                low = candidate.strip().lower()
                if low in ("true", "1"):
                    return "passed"
                if low in ("false", "0"):
                    return "failed"
        return "unknown"
    if dtype == "CATEGORICAL":
        label = string_value if isinstance(string_value, str) else value if isinstance(value, str) else None
        if isinstance(label, str):
            low = label.strip().lower()
            if low in _CATEGORICAL_PASS:
                return "passed"
            if low in _CATEGORICAL_FAIL:
                return "failed"
        return "unknown"
    if dtype == "NUMERIC" and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1 or value == 1.0:
            return "passed"
        if value == 0 or value == 0.0:
            return "failed"
        return "unknown"
    return "unknown"


def _scores_to_verifier(scores: list, warnings: list[str]) -> Optional[dict]:
    """Langfuse ``scores`` -> a ``verifier.checks[]`` sidecar.

    Every score becomes its own atomic check, ``source:
    instrumented_assertion`` (the score is external verifier evidence, not
    something the deterministic core inferred from prose). Returns ``None``
    when there are no scores — the caller keeps the run with no verifier
    evidence rather than writing an empty, misleading sidecar.
    """
    if not scores:
        return None
    checks: list[dict] = []
    for i, score in enumerate(scores):
        if not isinstance(score, dict) or not (score.get("name") or score.get("id")):
            warnings.append(f"score {i} has no name/id; skipped, not dropped silently")
            continue
        status = _score_status(score)
        if status == "unknown":
            warnings.append(
                f"score {score.get('name') or score.get('id')!r} (dataType="
                f"{score.get('dataType')!r}, value={score.get('value')!r}) has no clear "
                f"pass/fail mapping; recorded as 'unknown', not guessed"
            )
        check: dict[str, Any] = {
            "check_id": str(score.get("id") or f"langfuse_score_{i}"),
            "name": str(score.get("name") or f"score_{i}"),
            "status": status if status in CHECK_STATUSES else "unknown",
            "source": "instrumented_assertion",
            # Post-run is the conservative default: most Langfuse scores come
            # from an evaluator or human queue that runs AFTER the trace
            # completes, so this is never presented as information the agent
            # had during the run.
            "timing": "post_run",
            "source_pointers": [f"scores[{i}]"],
        }
        if score.get("comment"):
            check["comment"] = score["comment"]
        checks.append(check)
    if not checks:
        return None
    raw = json.dumps({"scores": scores}, ensure_ascii=False)
    return {"raw_output": raw, "checks": checks}


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
            attributes=attrs,
        ))

    if unmapped_types:
        warnings.append(
            f"unrecognised Langfuse observation type(s) {sorted(unmapped_types)!r}; "
            f"mapped to a generic grouping step, never dropped silently"
        )
    return spans, trace_id


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

    # --- verifier: explicit sidecar wins; else the trace's own scores --------
    if verifier is None:
        score_warnings: list[str] = []
        verifier = _scores_to_verifier(export.get("scores") or [], score_warnings)
        warnings.extend(score_warnings)
        if verifier:
            capabilities["verifier_results"] = "complete"
            warnings.append(
                f"verifier synthesised from {len(verifier['checks'])} Langfuse score(s) "
                f"(source: instrumented_assertion)"
            )

    doc: dict[str, Any] = {
        "atif_version": LANGFUSE_ADAPTER_VERSION,
        "source_type": "langfuse",
        "adapter_version": LANGFUSE_ADAPTER_VERSION,
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
            "no verifier supplied and no scores on the trace: run ingests with no "
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
