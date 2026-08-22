"""Product instrumentation for the review workflow (spec §4.21).

Twenty named events describing how a *reviewer* moves through the product,
plus the derived product measures they support. These evaluate usability and
trust — they are explicitly **not** agent-quality scores, and nothing here feeds
a review, a lesson, or a metric about a model.

The one hard constraint the spec sets is "instrument the review workflow without
capturing unredacted trace content in analytics events", so this module is built
as an allowlist rather than a logger:

- only the twenty named events may be recorded;
- only declared property keys may accompany them, each with a declared type; and
- string values must look like identifiers or enum tokens — bounded length, no
  whitespace — which is what keeps a prompt, a tool result, an artifact body, or
  a reviewer's free-text note from arriving here by accident.

A rejected event raises rather than being silently dropped, so a mis-instrumented
call site is a test failure and not a quiet leak. Events append to
``<store>/analytics/events.jsonl``; they are ordinary derived records beside the
immutable source, never part of a run's evidence.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .store import Store

# --- the event vocabulary (§4.21 "minimum events") ---------------------------

EVENTS = (
    "sweep_opened",
    "queue_view_created",
    "queue_filter_changed",
    "queue_sort_changed",
    "run_opened",
    "review_chapter_viewed",
    "moment_viewed",
    "evidence_opened",
    "claim_evidence_opened",
    "full_trace_opened",
    "feedback_submitted",
    "quick_relabel_submitted",
    "correction_saved",
    "run_disposition_set",
    "next_unhandled_opened",
    "lesson_created",
    "lesson_approved",
    "lesson_rejected",
    "experiment_proposed",
    "experiment_approved",
    "comparison_definition_saved",
    "comparison_exclusion_opened",
    "replay_requested",
)

# Property keys an event may carry, and the type each must be. Identifiers and
# enum tokens only: there is deliberately no ``note``, ``content``, ``summary``,
# ``statement``, or ``query`` key, because trace content and reviewer prose have
# no analytics use and every leak of them starts with a key like that.
PROPERTY_TYPES: dict[str, type | tuple[type, ...]] = {
    "session_id": str,
    "sweep_id": str,
    "queue_view_id": str,
    "run_id": str,
    "task_id": str,
    "moment_id": str,
    "lesson_id": str,
    "experiment_id": str,
    "comparison_id": str,
    "step_id": str,
    "chapter": str,
    "sort": str,
    "filters": list,
    "kind": str,
    "disposition": str,
    "review_mode": str,
    "detector": str,
    "taxonomy_version": str,
    "exclusion_reason": str,
    "corrected_fields": list,
    "evidence_kind": str,
    "count": int,
    "position": int,
    "fast_path": bool,
}

# Identifier-shaped strings only: bounded, single-token. Long or whitespace-
# bearing values are the shape free text arrives in, so they are refused.
MAX_STRING = 96


class InstrumentationError(ValueError):
    """A disallowed analytics event: unknown name, key, type, or unsafe value."""


def _check_scalar(key: str, value: Any) -> None:
    expected = PROPERTY_TYPES[key]
    if isinstance(value, bool) and expected is not bool:
        raise InstrumentationError(f"{key} must be {expected.__name__}, got bool")
    if not isinstance(value, expected):
        name = getattr(expected, "__name__", str(expected))
        raise InstrumentationError(f"{key} must be {name}, got {type(value).__name__}")
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            raise InstrumentationError(f"{key} exceeds {MAX_STRING} characters; "
                                       f"analytics events carry identifiers, not content")
        if value.strip() != value or any(c.isspace() for c in value):
            raise InstrumentationError(f"{key} contains whitespace; analytics events "
                                       f"carry identifiers and enum tokens, not free text")


def validate(event: str, properties: Optional[dict] = None) -> dict:
    """Validate one event against the allowlist, returning its clean properties."""
    if event not in EVENTS:
        raise InstrumentationError(f"unknown analytics event {event!r}")
    props = dict(properties or {})
    unknown = sorted(set(props) - set(PROPERTY_TYPES))
    if unknown:
        raise InstrumentationError(f"undeclared analytics propert{'y' if len(unknown) == 1 else 'ies'} {unknown}")
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, list):
            if PROPERTY_TYPES[key] is not list:
                raise InstrumentationError(f"{key} must not be a list")
            for item in value:
                if not isinstance(item, str):
                    raise InstrumentationError(f"{key} may only contain strings")
                _check_scalar("kind", item)  # same identifier shape as any token
        else:
            _check_scalar(key, value)
    return {k: v for k, v in props.items() if v is not None}


def record(store: Store, event: str, *, session_id: str,
           properties: Optional[dict] = None, actor: Optional[str] = None,
           at: Optional[str] = None) -> dict:
    """Append one validated event to the analytics log."""
    props = validate(event, properties)
    _check_scalar("session_id", session_id)
    entry = {
        "event": event,
        "session_id": session_id,
        "actor": actor,
        "at": at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "properties": props,
    }
    store.append_event(entry)
    return entry


def record_batch(store: Store, events: list[dict], actor: Optional[str] = None) -> int:
    """Record a batch of events, validating every one before writing any.

    All-or-nothing: a single bad event in a batch rejects the batch, so a broken
    client cannot half-write a session's history.
    """
    validated = []
    for raw in events:
        if not raw.get("session_id"):
            raise InstrumentationError("every analytics event needs a session_id")
        validate(raw.get("event"), raw.get("properties"))
        validated.append(raw)
    for raw in validated:
        record(store, raw["event"], session_id=raw["session_id"],
               properties=raw.get("properties"), actor=raw.get("actor") or actor,
               at=raw.get("at"))
    return len(validated)


# --- derived product measures (§4.21) ----------------------------------------


def _parse(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _first_gap(events: list[dict], start_event: str, end_event: str,
               key: Optional[str] = None) -> list[float]:
    """Seconds from each ``start_event`` to the next ``end_event`` in that scope.

    Scoped by session, and optionally by a property (e.g. ``run_id``) so a
    reviewer working two runs at once does not produce a phantom interval.
    """
    gaps: list[float] = []
    pending: dict[tuple, float] = {}
    for e in events:
        scope = (e.get("session_id"), e.get("properties", {}).get(key) if key else None)
        at = _parse(e.get("at"))
        if at is None:
            continue
        if e["event"] == start_event:
            pending[scope] = at
        elif e["event"] == end_event and scope in pending:
            gaps.append(at - pending.pop(scope))
    return gaps


def product_measures(store: Store) -> dict:
    """The §4.21 derived measures, each with the counts behind it.

    A measure with no observations reports ``None`` rather than ``0`` — an
    unmeasured workflow is not a workflow that scored zero. Measures that need a
    capability this package does not have yet (the fast path of §4.3.5, the Eval
    Lesson lifecycle of §4.11, replay of §4.14) are listed with
    ``"not_yet_instrumented"`` so the gap is visible rather than absent.
    """
    events = store.read_events()
    by_event: dict[str, list[dict]] = {}
    for e in events:
        by_event.setdefault(e.get("event"), []).append(e)

    sessions = {e.get("session_id") for e in events} - {None}
    dispositions = by_event.get("run_disposition_set", [])
    moment_views = by_event.get("moment_viewed", [])
    evidence_opens = by_event.get("evidence_opened", [])
    relabels = by_event.get("quick_relabel_submitted", [])
    corrections = by_event.get("correction_saved", [])
    feedback = by_event.get("feedback_submitted", [])
    runs_opened = by_event.get("run_opened", [])

    handled_per_session = [
        sum(1 for e in dispositions if e.get("session_id") == s) for s in sessions] or []
    dispositioned_runs = {e.get("properties", {}).get("run_id") for e in dispositions} - {None}
    opened_runs = {e.get("properties", {}).get("run_id") for e in runs_opened} - {None}
    enriched = sum(1 for e in runs_opened
                   if e.get("properties", {}).get("review_mode") == "model_enriched")
    lessons_created = by_event.get("lesson_created", [])
    lessons_approved = by_event.get("lesson_approved", [])
    experiments_approved = by_event.get("experiment_approved", [])

    return {
        "sessions": len(sessions),
        "events": len(events),
        "median_seconds_sweep_open_to_first_disposition":
            _median(_first_gap(events, "sweep_opened", "run_disposition_set")),
        "median_seconds_run_open_to_first_moment":
            _median(_first_gap(events, "run_opened", "moment_viewed", key="run_id")),
        "handled_runs_per_session": _median([float(n) for n in handled_per_session]),
        "moment_views_opening_evidence": _rate(len(evidence_opens), len(moment_views)),
        "quick_relabel_rate": _rate(len(relabels), len(feedback) + len(relabels) + len(corrections)),
        "full_correction_rate": _rate(len(corrections), len(feedback) + len(relabels) + len(corrections)),
        "in_progress_runs_later_dispositioned":
            _rate(len(dispositioned_runs & opened_runs), len(opened_runs)),
        "full_trace_fallback_rate": _rate(len(by_event.get("full_trace_opened", [])), len(runs_opened)),
        "model_enriched_open_rate": _rate(enriched, len(runs_opened)),
        # §4.3.5 — share of run opens that took the fast path (opened directly on
        # the first key moment via the workspace entry preference). ``fast_path``
        # is a bool the client stamps on ``run_opened``.
        "fast_path_open_rate": _rate(
            sum(1 for e in runs_opened if e.get("properties", {}).get("fast_path") is True),
            len(runs_opened)),
        # §13/§19.2 — the Eval Lesson funnel: how many created lessons a human
        # approved for test, and how many approved lessons produced a human-approved
        # experiment (the Milestone 5 accept criterion).
        "lesson_approval_rate": _rate(len(lessons_approved), len(lessons_created)),
        "approved_lesson_experiment_rate":
            _rate(len(experiments_approved), len(lessons_approved)),
        "counts": {name: len(by_event.get(name, [])) for name in EVENTS},
        # Named so the absence is legible: these need product surfaces that do
        # not exist yet, so no measure is reported for them.
        "not_yet_instrumented": [
            "reviewer agreement by detector or taxonomy version — needs §14.2 adjudication",
        ],
    }
