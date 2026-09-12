"""Normalized execution telemetry and run-level efficiency summaries.

This module is the single accounting boundary for execution-quality detectors.
Provider adapters normalize into ATIF steps; detectors consume only the derived
event records returned here and never read provider-specific payloads.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from .schema import DerivedEvent, DetectorResult

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
    return {
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
                "unique_signatures": len(
                    {tuple(fact["signature"]) for fact in redundant_facts}
                ),
                "max_calls_between": max(
                    (fact.get("calls_between", 0) for fact in redundant_facts),
                    default=None,
                ),
            },
        },
        "findings": findings,
    }
