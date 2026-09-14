"""Normalized execution telemetry and run-level efficiency summaries.

This module is the single accounting boundary for execution-quality detectors.
Provider adapters normalize into ATIF steps; detectors consume only the derived
event records returned here and never read provider-specific payloads.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Optional

from .schema import DerivedEvent, DetectorResult

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .store import Store

_CACHE_INPUT_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class ExecutionQualityConfig:
    context_enabled: bool = True
    input_tokens_threshold: int = 100_000
    latency_enabled: bool = True
    generation_ms_threshold: int = 60_000
    redundancy_enabled: bool = True
    redundancy_window: int = 20

    @classmethod
    def from_doc(cls, doc: dict) -> "ExecutionQualityConfig":
        raw = doc.get("execution_quality") or {}
        context = raw.get("context_bloat") or {}
        latency = raw.get("latency") or {}
        redundant = raw.get("redundant_work") or {}
        config = cls(
            context_enabled=bool(context.get("enabled", True)),
            input_tokens_threshold=int(context.get("input_tokens_threshold", 100_000)),
            latency_enabled=bool(latency.get("enabled", True)),
            generation_ms_threshold=int(latency.get("generation_ms_threshold", 60_000)),
            redundancy_enabled=bool(redundant.get("enabled", True)),
            redundancy_window=int(redundant.get("comparison_window", 20)),
        )
        if config.input_tokens_threshold < 0 or config.generation_ms_threshold < 0:
            raise ValueError("execution-quality thresholds must be non-negative")
        if config.redundancy_window < 1:
            raise ValueError("redundant-work comparison_window must be positive")
        return config


@dataclass(frozen=True)
class GenerationUsage:
    run_id: str
    source_capture_id: str
    event_id: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    start_time: Any
    end_time: Any
    wall_ms: Optional[int]
    provider: Optional[str]
    model: Optional[str]

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.run_id, self.source_capture_id, self.event_id


def _number(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _time_value(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def generation_wall_ms(start_time: Any, end_time: Any) -> Optional[int]:
    start, end = _time_value(start_time), _time_value(end_time)
    if start is None or end is None or end < start:
        return None
    return round((end - start) * 1000)


def generation_token_counts(cost: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Normalize Claude/Harbor/OpenAI-style per-generation usage.

    Anthropic reports uncached, cache-created, and cache-read input tokens as
    additive fields. OpenAI-style ``prompt_tokens`` already includes cached
    tokens, so cache detail is deliberately not added to that shape.
    """
    if not isinstance(cost, dict):
        return None, None, None
    usage = cost.get("usage") if isinstance(cost.get("usage"), dict) else cost
    direct_input = _number(usage.get("input_tokens"))
    prompt_input = _number(usage.get("prompt_tokens"))
    if direct_input is not None:
        cache_tokens = sum(_number(usage.get(key)) or 0 for key in _CACHE_INPUT_KEYS)
        input_tokens = direct_input + cache_tokens
    else:
        pi_input = _number(usage.get("input"))
        if pi_input is not None:
            input_tokens = pi_input + sum(
                _number(usage.get(key)) or 0 for key in ("cacheRead", "cacheWrite")
            )
        else:
            input_tokens = prompt_input
    output_tokens = _number(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _number(usage.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _number(usage.get("output"))
    total_tokens = _number(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _number(usage.get("totalTokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    return input_tokens, output_tokens, total_tokens


def _generation_events(events: Iterable[DerivedEvent]) -> list[DerivedEvent]:
    materialized = list(events)
    marked = [event for event in materialized if event.payload.get("generation_event") is True]
    return marked or [event for event in materialized if event.event_type == "model_output"]


def generation_usage_capability(steps: Iterable[dict]) -> str:
    """Coverage level for adapter-normalized generation usage records."""
    materialized = list(steps)
    marked = [step for step in materialized if step.get("generation_event") is True]
    generations = marked or [step for step in materialized if step.get("kind") == "model_output"]
    if not generations:
        return "unavailable"
    present = [generation_token_counts(step.get("cost"))[0] is not None
               for step in generations]
    return "complete" if all(present) else "partial" if any(present) else "unavailable"


def normalized_generation_usage(events: Iterable[DerivedEvent]) -> list[GenerationUsage]:
    """Return one authoritative usage record per model-generation event."""
    records: list[GenerationUsage] = []
    seen: set[tuple[str, str, str]] = set()
    for event in _generation_events(events):
        input_tokens, output_tokens, total_tokens = generation_token_counts(event.cost)
        start_time = event.payload.get("start_time", event.payload.get("timestamp"))
        end_time = event.payload.get("end_time")
        record = GenerationUsage(
            run_id=event.run_id,
            source_capture_id=event.source_capture_id,
            event_id=event.event_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            start_time=start_time,
            end_time=end_time,
            wall_ms=generation_wall_ms(start_time, end_time),
            provider=event.payload.get("provider"),
            model=event.payload.get("model"),
        )
        if record.identity in seen:
            raise ValueError(f"duplicate generation usage identity {record.identity!r}")
        seen.add(record.identity)
        records.append(record)
    return records


def reconcile_generation_capabilities(capabilities: dict[str, str], steps: list[dict]) -> dict[str, str]:
    """Downgrade contradictory completeness declarations at the ingest boundary."""
    caps = dict(capabilities)
    marked = [step for step in steps if step.get("generation_event") is True]
    generations = marked or [step for step in steps if step.get("kind") == "model_output"]
    if caps.get("generation_usage") == "complete" and any(
        generation_token_counts(step.get("cost"))[0] is None
        for step in generations
    ):
        caps["generation_usage"] = "partial"
    if caps.get("generation_timestamps") == "complete" and any(
        generation_wall_ms(
            step.get("start_time", step.get("timestamp")), step.get("end_time")
        ) is None
        for step in generations
    ):
        caps["generation_timestamps"] = "partial"
    return caps


def nearest_rank(values: Iterable[int], percentile: float) -> Optional[int]:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def execution_quality_summary(
    results: Iterable[DetectorResult], events: Iterable[DerivedEvent] = ()
) -> dict:
    """Project detector output into the independent run-level efficiency axis."""
    by_name = {result.detector: result for result in results}

    def result(name: str) -> DetectorResult:
        return by_name[name]

    context = result("context_token_bloat")
    latency = result("excess_latency")
    redundant = result("repeated_action_no_new_info")
    context_facts = [fact for c in context.candidates for fact in c.structured_facts]
    latency_facts = [fact for c in latency.candidates for fact in c.structured_facts]
    redundant_facts = [fact for c in redundant.candidates for fact in c.structured_facts]
    generations = normalized_generation_usage(events)
    observed_input_tokens = [
        record.input_tokens for record in generations if record.input_tokens is not None
    ]
    observed_latencies = [
        record.wall_ms for record in generations if record.wall_ms is not None
    ]

    def state(detector_result: DetectorResult) -> str:
        if not detector_result.evaluated:
            return "unevaluated"
        return "issue" if detector_result.candidates else "healthy"

    findings = [
        candidate.to_dict()
        for detector_result in (context, latency, redundant)
        for candidate in detector_result.candidates
    ]
    return with_efficiency_limits({
        "efficiency": {
            "context_bloat": {
                "evaluated": context.evaluated,
                "status": state(context),
                "unmet_capabilities": context.unmet_capabilities,
                "violations": len(context_facts),
                "peak_input_tokens": max(
                    observed_input_tokens, default=None
                ),
                "total_input_tokens": sum(observed_input_tokens) if observed_input_tokens else None,
                "median_input_tokens": (
                    round(statistics.median(observed_input_tokens))
                    if observed_input_tokens else None
                ),
                "p95_input_tokens": nearest_rank(observed_input_tokens, .95),
                "total_affected_input_tokens": sum(
                    fact["input_tokens"] for fact in context_facts
                ),
            },
            "latency": {
                "evaluated": latency.evaluated,
                "status": state(latency),
                "unmet_capabilities": latency.unmet_capabilities,
                "violations": len(latency_facts),
                "max_generation_ms": max(
                    observed_latencies, default=None
                ),
                "median_generation_ms": (
                    round(statistics.median(observed_latencies))
                    if observed_latencies else None
                ),
                "p95_generation_ms": nearest_rank(observed_latencies, .95),
            },
            "redundant_work": {
                "evaluated": redundant.evaluated,
                "status": state(redundant),
                "unmet_capabilities": redundant.unmet_capabilities,
                "violations": len(redundant_facts),
                # A signature is ``(tool, content_key, input_identity)`` and either
                # element may be a structured argument (an ATIF ``data`` object),
                # so it is canonicalized to JSON before de-duplicating rather than
                # put straight into a set — a dict is not hashable and would take
                # the whole summary down.
                "unique_signatures": len({
                    json.dumps(fact["signature"], sort_keys=True, default=str)
                    for fact in redundant_facts
                }),
                "max_calls_between": max(
                    (fact.get("calls_between", 0) for fact in redundant_facts),
                    default=None,
                ),
            },
        },
        "findings": findings,
    })


def _empty_execution_quality() -> dict:
    return {"efficiency": {}, "findings": []}


def execution_quality_from_records(
    raw_events: Iterable[Any],
    capabilities: Optional[dict],
    source: Any = None,
    *,
    run_id: Optional[str] = None,
    capture_id: Optional[str] = None,
) -> dict:
    """Run the three execution detectors over already-loaded records.

    The pure core of the derive-on-read fallback: it takes the events (raw
    ``events.json`` dicts or :class:`DerivedEvent` instances), the capability
    map, and the immutable source document, and does no store I/O. A caller
    that has already loaded any of these (e.g. the forensic read) passes them
    in rather than paying for a second read and re-parse.

    Returns an empty summary when there are no events to evaluate; every
    dimension then reports unevaluated rather than a fabricated pass.
    """
    # Imported lazily: detectors.py imports this module.
    from .detectors import (
        ContextTokenBloat,
        DetectorContext,
        ExcessLatency,
        RepeatedActionNoNewInfo,
    )
    from .schema import CapabilityProfile

    events = [
        evt if isinstance(evt, DerivedEvent) else DerivedEvent(**evt)
        for evt in raw_events
    ]
    if not events:
        return _empty_execution_quality()
    if run_id is None:
        run_id = events[0].run_id
        capture_id = events[0].source_capture_id
    profile = CapabilityProfile(
        run_id=run_id or "",
        source_capture_id=capture_id or "",
        capabilities=dict(capabilities or {}),
    )
    # The thresholds are the capture's own when the immutable source declares
    # them; otherwise the module defaults apply, exactly as at ingest time.
    doc = {"execution_quality": source.get("execution_quality", {})} \
        if isinstance(source, dict) else {}
    ctx = DetectorContext(
        run_id=run_id or "",
        capture_id=capture_id or "",
        events=events,
        checks=[],
        doc=doc,
        profile=profile,
    )
    results = [detector().run(ctx) for detector in (
        ContextTokenBloat, ExcessLatency, RepeatedActionNoNewInfo,
    )]
    return execution_quality_summary(results, events)


def derive_execution_quality(
    store: "Store",
    run_id: str,
    capture_id: str,
    *,
    raw_events: Optional[list] = None,
    capabilities: Optional[dict] = None,
    source: Any = None,
) -> dict:
    """Recompute a run's execution-quality summary from persisted records.

    A capture ingested before the execution-quality detectors existed has no
    ``execution_quality.json``. Rather than silently dropping that run (which
    made the whole fleet card read "No results"), run the three deterministic
    detectors against the already-persisted events/capabilities here. This is
    the same accounting boundary the pipeline uses — only the invocation point
    differs — so an old store and a freshly ingested one agree.

    Callers that have already loaded ``events`` / ``capabilities`` / ``source``
    (the forensic read) pass them via the keyword arguments; the rest are read
    here. Every optional read is guarded — a capture can legitimately have
    ``events.json`` without ``capabilities.json``, and a missing file must
    report unevaluated, never raise and take the page down.

    Returns an empty summary when the capture has no persisted events at all
    (there is nothing to evaluate): those dimensions are reported unevaluated.
    """
    if raw_events is None:
        if not store.has_derived(run_id, capture_id, "events.json"):
            return _empty_execution_quality()
        raw_events = store.read_derived(run_id, capture_id, "events.json") or []
    if capabilities is None:
        capabilities = {}
        if store.has_derived(run_id, capture_id, "capabilities.json"):
            capabilities = (
                store.read_derived(run_id, capture_id, "capabilities.json") or {}
            ).get("capabilities", {})
    if source is None:
        try:
            source = store.read_source(run_id, capture_id)
        except (OSError, ValueError):
            source = {}
    return execution_quality_from_records(
        raw_events, capabilities, source, run_id=run_id, capture_id=capture_id,
    )


def execution_quality_record(
    store: "Store",
    run_id: str,
    capture_id: str,
    *,
    raw_events: Optional[list] = None,
    capabilities: Optional[dict] = None,
    source: Any = None,
) -> dict:
    """The persisted execution-quality summary, or a derived one for old captures.

    Provisioning for a whole old store is done once by
    :func:`backfill_execution_quality`; this fallback keeps a single un-migrated
    capture correct in the meantime.
    """
    if store.has_derived(run_id, capture_id, "execution_quality.json"):
        record = store.read_derived(run_id, capture_id, "execution_quality.json") \
            or _empty_execution_quality()
    else:
        record = derive_execution_quality(
            store, run_id, capture_id,
            raw_events=raw_events, capabilities=capabilities, source=source,
        )
    return with_efficiency_limits(record)


def efficiency_limits(value: dict) -> list[str]:
    """What one execution-quality dimension does NOT establish (§B1).

    A deterministic restatement of the dimension's own evidence status — never
    a judgement about whether the run *should* have spent less. Kept per
    dimension so a reader sees, beside the number, that a violation is a
    candidate for review rather than proven waste.
    """
    if not value.get("evaluated"):
        caps = ", ".join(sorted(value.get("unmet_capabilities") or []))
        return ["No opportunity to evaluate — the capture lacked "
                + (caps or "the required telemetry")
                + "; a coverage gap, not a clean result."]
    limits = [
        "Execution quality is independent of the task outcome; a violation here does "
        "not by itself establish a task failure.",
        "Measured usage is associated with the run, not a validated avoidable cost or "
        "projected saving.",
    ]
    if value.get("violations"):
        limits.append("The threshold defines a boundary, not a target; exceeding it is a "
                      "candidate for review, not proof of waste.")
    return limits


def with_efficiency_limits(record: dict) -> dict:
    """Return ``record`` with a ``limits`` list on every efficiency dimension.

    A read-time projection so it applies uniformly to captures persisted before
    this existed and to freshly derived ones — no backfill required.
    """
    out = dict(record or {})
    efficiency = out.get("efficiency")
    if isinstance(efficiency, dict):
        out["efficiency"] = {
            dimension: {**value, "limits": efficiency_limits(value)}
            for dimension, value in efficiency.items()
        }
    return out


def backfill_execution_quality(store: "Store") -> int:
    """Persist an execution-quality summary for every capture missing one.

    The derive-on-read fallback is correct but recomputes on every request (the
    redundancy detector walks the event list per pair), so a large pre-feature
    store should be provisioned once instead. Idempotent: captures that already
    have the record are left untouched. Returns the number written.
    """
    from . import read

    written = 0
    for run in read.list_runs(store):
        run_id = run["run_id"]
        # list_runs deliberately collapses capture revisions into one logical
        # run. The migration must expand that run's immutable capture index so
        # an older revision is not silently left unprovisioned.
        for entry in store.read_index(run_id):
            capture_id = entry.get("capture_id")
            if not capture_id or store.has_derived(
                run_id, capture_id, "execution_quality.json"
            ):
                continue
            record = derive_execution_quality(store, run_id, capture_id)
            store.write_derived(run_id, capture_id, "execution_quality.json", record)
            written += 1
    return written
