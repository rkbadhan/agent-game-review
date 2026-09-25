"""Read layer over the immutable store (spec §5, Milestone 1 evidence browser).

Pure stdlib. This module turns the persisted immutable source and the derived
records the deterministic pipeline already wrote into *read-model views*:

- :func:`list_runs`     — one summary row per logical run;
- :func:`get_review`    — the full deterministic review of a run;
- :func:`get_forensic`  — the synchronized forensic view (§4.5): every source
  step projected onto the timeline / agent-message / tool-I/O / environment /
  artifact / verifier panels, plus a capability badge stating which evidence
  types were captured completely, partially, or not at all; and
- :func:`get_source`    — the raw immutable source with a hash re-verification
  (Milestone 1 accept: "imported source bytes match hashes").

It derives *nothing new* and never writes: every value is read straight from
records `pipeline.analyze` persisted, so a GET is side-effect free. This is a
read model over an append-only store — if the source is re-ingested under a new
derivation version, the persisted derived snapshot is refreshed at ingest time
and the views reflect that snapshot. The FastAPI binding in :mod:`agr.api` is a
thin transport over these functions; nothing here imports a third-party package.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from datetime import datetime
from typing import Any, Optional

from . import lessons, taxonomy, version, workflow
from .execution_quality import (
    execution_quality_record,
    generation_token_counts,
    generation_wall_ms,
)
from .recovery import raw_failure_lead
from .schema import WATERMARKED_STATUSES
from .store import InvalidRunId, Store, source_hash


class RunNotFound(Exception):
    """Raised when a logical run has no captures in the store."""


# --- panel projection --------------------------------------------------------

# Which forensic panel (spec §4.5) a derived event primarily belongs to. The
# timeline panel always holds every event; these route an event's payload onto
# exactly one content panel so selecting a step lights up the right column.
_EVENT_PANEL = {
    "task_received": "agent_message",
    "model_output": "agent_message",
    "plan_declared": "agent_message",
    "final_submission": "agent_message",
    "tool_call": "tool_io",
    "tool_result": "tool_io",
    "environment_observation": "environment",
    "process_started": "environment",
    "process_observed": "environment",
    "error_observed": "environment",
    "retry": "environment",
    "strategy_change": "environment",
    "context_compaction": "environment",
    "artifact_observation": "artifact",
    "verifier_check": "verifier",
    "run_finished": "timeline",
    "run_completed": "timeline",
    "run_timed_out": "timeline",
    "run_failed": "timeline",
}

# The capability (spec §6.2) that governs whether a panel's evidence was
# captured. Used to mark a panel complete / partial / unavailable so that
# "unavailable state is visibly marked" (Milestone 1 accept criterion).
_PANEL_CAPABILITY = {
    "agent_message": "messages",
    "tool_io": "tool_calls",
    "environment": "process_state",
    "artifact": "filesystem",
    "verifier": "verifier_code",
}

# Capability levels that mean the evidence is fully / partially present. Every
# other level (``unavailable``, ``final_only``, ``checkpoint_only``) is coarser
# than "partial" and is surfaced verbatim to the UI alongside this flag.
_PRESENT_LEVELS = {"complete"}
_PARTIAL_LEVELS = {"partial", "checkpoint_only", "final_only"}


def _availability(level: Optional[str]) -> str:
    if level in _PRESENT_LEVELS:
        return "complete"
    if level in _PARTIAL_LEVELS:
        return "partial"
    return "unavailable"


# --- store access helpers ----------------------------------------------------


def _runs_root(store: Store) -> str:
    return os.path.join(store.root, "runs")


def _latest(store: Store, run_id: str) -> str:
    """Return the latest capture id for a run, or raise :class:`RunNotFound`."""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        raise RunNotFound(run_id)
    return capture_id


def _index_entry(store: Store, run_id: str, capture_id: str) -> dict:
    for entry in store.read_index(run_id):
        if entry["capture_id"] == capture_id:
            return entry
    raise RunNotFound(run_id)


def _read(store: Store, run_id: str, capture_id: str, name: str, default: Any = None) -> Any:
    """Read a derived record, tolerating runs persisted before it existed."""
    if store.has_derived(run_id, capture_id, name):
        return store.read_derived(run_id, capture_id, name)
    return default


def _moment_summary(candidate: dict) -> str:
    """A short *structural* description of a candidate, grounded in its facts.

    Deterministic and non-interpretive: it restates what the detector observed,
    never a verdict ("mistake") or narration. The verdict label and prose are
    the model reviewer's job (Stage F / Milestone 4); until then the guided view
    shows this structural summary as an honest stand-in.
    """
    facts = candidate.get("structured_facts") or [{}]
    f = facts[0]
    ftype = f.get("type")
    if ftype == "requirement_status":
        detail = ""
        if f.get("expected") is not None:
            detail = f" (expected {f.get('expected')}, observed {f.get('observed')})"
        return f"{f.get('check_id')} still failing at submission{detail}"
    if ftype == "repetition":
        return f"identical action repeated with no new information ({', '.join(f.get('events', []))})"
    if ftype == "absence":
        return f"declared artifact {f.get('declared_artifact')} never observed"
    if ftype == "state_transition":
        tool = f.get("tool")
        diag = f.get("failure_diagnostic") or f.get("error_signature")
        diag_usable = diag and f.get("error_signature_basis") != "fallback_last_nonempty"
        if tool and diag_usable:
            lead = f"{tool} failed: {diag}"
        else:
            # Root-cause follow-up: an opaque fallback signature is dropped
            # above as unusable, but the episode's own raw_failure_text is
            # still something to root-cause from — use it rather than
            # dead-ending on a bare "tool call failed".
            raw_lead = raw_failure_lead(f.get("raw_failure_text"))
            if tool and raw_lead:
                lead = f"{tool} failed: {raw_lead}"
            elif raw_lead:
                lead = f"tool call failed: {raw_lead}"
            elif tool:
                lead = f"{tool} call failed"
            else:
                lead = "tool call failed"
        if f.get("resolution_event"):
            return f"{lead}, recovered via strategy change"
        return f"{lead}, left unresolved before submission"
    return candidate.get("detector", "candidate")


def _review_moment_view(m: dict) -> dict:
    """Project a persisted :class:`ReviewMoment` into the guided-view moment shape.

    Keeps the keys the browser and the evaluation harness already consume (adding
    the reviewer envelope's ``attribution_ceiling`` and ``rendered_statement``).
    The ``tag`` stays a neutral ``strength``/``concern`` — a taxonomy *verdict* is
    still the model reviewer's job (Stage F); ``taxonomy_verdict`` is carried
    through so Stage F can fill it behind the same view.
    """
    polarity = m.get("polarity", "negative")
    return {
        "moment_id": m.get("moment_id"),
        "candidate_id": m.get("candidate_id"),
        "detector": m.get("detector"),
        "kind": m.get("kind"),
        "polarity": polarity,
        "tag": "strength" if polarity == "positive" else "concern",
        "anchor_event_ids": m.get("anchor_event_ids", []),
        "sequence": m.get("sequence"),
        "phase_id": m.get("phase_id"),
        "affected_checks": m.get("affected_checks", []),
        "affected_contract_items": m.get("affected_contract_items", []),
        "attribution_ceiling": m.get("attribution_ceiling"),
        # P2 (§B1): what this finding's evidence does not establish.
        "limits": m.get("limits", []),
        "summary": m.get("rendered_statement"),
        "rendered_statement": m.get("rendered_statement"),
        # Model reviewer enrichment (Stage F); None/empty on deterministic-only runs.
        "taxonomy_verdict": m.get("taxonomy_verdict"),
        "behaviour_tags": m.get("behaviour_tags", []),
        "phase": m.get("phase"),
        "consequence": m.get("consequence"),
        "micro_abilities": m.get("micro_abilities", []),
        "root_cause_candidates": m.get("root_cause_candidates", []),
        "better_action": m.get("better_action"),
        # GR-2: the three kinds of grounded alternative, each shown under its own
        # status. Persisted as an ``Alternative`` record; pass through as dicts.
        "alternatives": [
            a.to_dict() if hasattr(a, "to_dict") else a
            for a in (m.get("alternatives") or [])
        ],
        "instructional_value": m.get("instructional_value"),
        # Whether this accepted finding supports an Eval Lesson (§4.11). Carried
        # to the browser so the Eval Lesson chapter can offer to create one; the
        # deterministic core sets it False, so only model-enriched moments qualify.
        "eval_lesson_recommended": bool(m.get("eval_lesson_recommended")),
        "enrichment_source": m.get("enrichment_source"),
        "facts": m.get("validated_facts", []),
        # AGR-03: the three review statuses the UI must keep apart — evidence
        # validation (the recomputed facts above), interpretation support (is
        # the model's explanation linked to run evidence or pure
        # interpretation), and intervention validation (is the proposed better
        # action's causal wording within the evidence ceiling).
        "gate_results": m.get("gate_results", {}),
    }


def _selected_moments(review_moments: list[dict]) -> list[dict]:
    """The reviewer envelope's selected cards, in selection order (§8.10)."""
    selected = [m for m in review_moments if m.get("selected")]
    selected.sort(key=lambda m: (m.get("selection_rank") is None, m.get("selection_rank") or 0))
    return [_review_moment_view(m) for m in selected]


def _moments(events: list[dict], detector_results: list[dict]) -> list[dict]:
    """Project deterministic detector candidates into ordered key moments.

    A pure read-model projection — no new derivation. Each moment carries its
    anchor, timeline sequence, phase, polarity (as a neutral ``strength`` /
    ``concern`` tag rather than a taxonomy verdict), affected checks/items, and
    the structured facts behind it.
    """
    seq_of = {e["event_id"]: e.get("sequence") for e in events}
    phase_of = {e["event_id"]: e.get("phase_id") for e in events}
    moments: list[dict] = []
    for r in detector_results:
        if not r.get("evaluated"):
            continue
        for c in r.get("candidates", []):
            anchors = c.get("anchor_event_ids", [])
            anchor = anchors[0] if anchors else None
            polarity = c.get("polarity", "negative")
            moments.append({
                "moment_id": c.get("candidate_id"),
                "detector": c.get("detector"),
                "kind": c.get("kind"),
                "polarity": polarity,
                "tag": "strength" if polarity == "positive" else "concern",
                "anchor_event_ids": anchors,
                "sequence": seq_of.get(anchor),
                "phase_id": phase_of.get(anchor),
                "affected_checks": c.get("affected_checks", []),
                "affected_contract_items": c.get("affected_contract_items", []),
                "summary": _moment_summary(c),
                # P2 (§B1): the fallback projection states limits too.
                "limits": c.get("limits", []),
                "facts": c.get("structured_facts", []),
            })
    moments.sort(key=lambda m: (m["sequence"] is None, m["sequence"] or 0))
    return moments


def _recovery_strategy_changed(moment: dict) -> bool:
    """Whether a recovery moment's episode changed strategy — read from the
    state_transition fact the detector emitted (never inferred from prose)."""
    for f in moment.get("facts") or []:
        if f.get("type") == "state_transition" and f.get("strategy_changed"):
            return True
    return False


def _apply_moment_enrichment(moments: list[dict], derived_events: list) -> None:
    """GR-3: derive each moment's idea category, mechanical label and basis.

    A detector that emits a vocabulary tag (recovery, submission) gives the
    moment a MECHANICAL label — its support is the detector's own structured
    classification, not model prose — so a deterministic-only review still shows
    the supported-recovery label the idea asks for. The category is a pure
    function of the moment's tags (model tags ∪ detector tags); the model never
    names a category. The argument-shape link is deterministic too: it is
    attached only when the moment anchors a genuinely failing tool call.
    """
    from .argument_shapes import argument_shape_link_for_event

    for m in moments:
        detector = m.get("detector")
        det_tags = list(taxonomy.DETECTOR_BEHAVIOUR_TAGS.get(detector, ()))
        if detector == "successful_recovery_via_strategy_change" and \
                _recovery_strategy_changed(m):
            det_tags.append("effective_replan")
        if det_tags:
            m["detector_tags"] = det_tags
            if not m.get("behaviour_tags"):
                m["behaviour_tags"] = list(det_tags)
        # The basis is per CATEGORY, not per moment: a model tag for one category
        # must never inherit the mechanical basis the detector gives another. When
        # a moment carries both, the displayed category is the mechanically
        # supported one (its deterministic label is the headline) and every other
        # category stays visible with its OWN basis in ``category_bases``.
        model_tags = list(m.get("behaviour_tags") or [])
        mechanical_cats = {c: "mechanical" for c in taxonomy.category_for_tags(det_tags)}
        model_cats = {c: "model" for c in taxonomy.category_for_tags(model_tags)}
        category_bases = {**model_cats, **mechanical_cats}  # mechanical wins a tie
        categories = sorted(category_bases)
        m["category_bases"] = category_bases
        m["categories"] = categories
        displayed = (sorted(mechanical_cats) or categories or [None])[0]
        m["category"] = displayed
        m["category_label"] = (
            taxonomy.IDEA_CATEGORIES[displayed]["label"] if displayed else None
        )
        m["basis"] = category_bases.get(displayed)
        # EVERY category the moment covers is exposed with its own label and
        # basis, so the card shows exactly what the coverage count reports — a
        # secondary category is never counted but left invisible.
        m["category_details"] = [
            {"category_id": cid, "label": taxonomy.IDEA_CATEGORIES[cid]["label"],
             "basis": category_bases[cid]}
            for cid in categories
        ]
        if detector == "successful_recovery_via_strategy_change":
            m["label"] = "Supported recovery"
        elif detector in taxonomy.DETECTOR_NEUTRAL_LABELS:
            # The detector observed something real but does not establish a tag's
            # meaning — a neutral label, never a victory claim.
            m["label"] = taxonomy.DETECTOR_NEUTRAL_LABELS[detector]
        if m["basis"] is None and (det_tags or m.get("label")):
            m["basis"] = "mechanical"
        if not m.get("argument_shape_link"):
            link = None
            for eid in m.get("anchor_event_ids") or []:
                link = argument_shape_link_for_event(derived_events, eid)
                if link:
                    break
            if link:
                m["argument_shape_link"] = link


def _run_moments(store: Store, run_id: str, capture_id: str,
                 events: list[dict], detector_results: list[dict],
                 reviewer_key: Optional[str] = None) -> tuple[list[dict], Optional[list[dict]], Optional[str]]:
    """The run's key moments plus the raw envelope, shared by review + run cards.

    Prefers the deterministic reviewer envelope's selected, de-duplicated,
    attribution-gated cards (§8.8–§8.10); falls back to the raw detector-candidate
    projection for runs persisted before the envelope existed. Returns
    ``(moments, review_moments, served_key)`` where ``review_moments`` is ``None``
    on the fallback path and ``served_key`` is the reviewer key actually read
    (may differ from ``reviewer_key`` when the requested key was unavailable).

    ``reviewer_key`` selects which review snapshot to read when more than one
    reviewer has scored the capture. ``None`` picks the most-enriched
    available review: a model pass if present, else the deterministic baseline.
    """
    review_moments, served_key = _read_review(store, run_id, capture_id, reviewer_key)
    moments = (
        _selected_moments(review_moments)
        if review_moments is not None
        else _moments(events, detector_results)
    )
    if moments and events:
        from .schema import DerivedEvent
        _apply_moment_enrichment(moments, [DerivedEvent(**e) for e in events])
    return moments, review_moments, served_key


def _active_error_keys(store: Store, run_id: str, capture_id: str) -> set[str]:
    """Reviewer keys with a CURRENT (unresolved) error (F1 follow-up).

    ``review_errors.json`` holds each reviewer's active error only — pipeline
    resolves a reviewer's entry on a successful retry — so a historical
    failure never makes a healthy retry look failed.
    """
    errors = _read(store, run_id, capture_id, "review_errors.json", [])
    return {e.get("reviewer_key") for e in errors if isinstance(e, dict)}


def _incomplete_reviewer_keys(store: Store, run_id: str, capture_id: str) -> set[str]:
    """Reviewer keys whose LATEST attempt stopped early (GR-1 'incomplete').

    An incomplete attempt writes no new snapshot, so the reviewer's older
    successful slot is stale for it: the default must not serve those cards
    while the status says the deterministic baseline is shown. A later success
    becomes the latest attempt and clears this naturally.
    """
    latest: dict[str, str] = {}
    for a in _read(store, run_id, capture_id, "review_attempts.json", []):
        key = a.get("reviewer_key")
        if key:
            latest[key] = a.get("outcome")
    return {k for k, outcome in latest.items() if outcome == "incomplete"}


def _default_reviewer_key(store: Store, run_id: str, capture_id: str) -> str:
    """The most-enriched HEALTHY review: model if present, else deterministic.

    Review 2026-09-07 (R4): a reviewer slot whose review FAILED is never
    servable as the default while a healthy alternative exists — its snapshot
    is stale (or absent) while the UI wording says the deterministic baseline
    is shown. GR-1 extends the same rule to a reviewer whose latest attempt was
    ``incomplete``: it too serves no fresh snapshot. Serving order: a healthy
    model slot, then the deterministic baseline, and only a stale model slot
    when no baseline was ever written (a stale slot beats nothing at all).
    """
    stale = _active_error_keys(store, run_id, capture_id) | \
        _incomplete_reviewer_keys(store, run_id, capture_id)
    keys = store.list_reviews(run_id, capture_id)
    for k in keys:
        if k != "deterministic" and k not in stale:
            return k
    if "deterministic" in keys:
        return "deterministic"
    for k in keys:
        if k != "deterministic":
            return k  # explicit fallback: a stale slot beats nothing at all
    return "deterministic"


def _read_review(store: Store, run_id: str, capture_id: str,
                  reviewer_key: Optional[str]) -> tuple[Optional[list[dict]], Optional[str]]:
    """Read one review slot, or the default (most-enriched) when key is None.

    Returns ``(moments, served_key)`` where ``served_key`` is the reviewer key
    actually read — which may differ from ``reviewer_key`` when the requested key
    does not exist and the default was served instead. ``moments`` is ``None`` on
    the legacy fallback path (no reviews/ dir and no review_moments.json).

    Falls back to the legacy ``review_moments.json`` for captures written before
    the multi-reviewer store existed.
    """
    keys = store.list_reviews(run_id, capture_id)
    if not keys:
        # Truly legacy capture with no reviews/ dir and no review_moments.json.
        return _read(store, run_id, capture_id, "review_moments.json", None), None
    key = reviewer_key if reviewer_key in keys else _default_reviewer_key(store, run_id, capture_id)
    try:
        return store.read_review_slot(run_id, capture_id, key), key
    except KeyError:
        return _read(store, run_id, capture_id, "review_moments.json", None), None


def _duration_seconds(rs: dict) -> Optional[float]:
    """Wall-clock run duration from the ISO ``started_at``/``finished_at`` stamps."""
    start, end = rs.get("started_at"), rs.get("finished_at")
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (t1 - t0).total_seconds()
    return delta if delta >= 0 else None


def _step_count(events: list[dict]) -> int:
    """Number of distinct source steps on the derived timeline (§4.3.3 card 'steps')."""
    return len({sid for e in events for sid in e.get("source_step_ids", [])})


# Capture completeness the adapter reports as too incomplete for a trustworthy
# review (§4.15). ``partial`` is degraded-but-reviewable — its missing panels are
# marked unavailable, not the whole review withheld; ``incomplete`` is the adapter
# stating the capture cannot support a review at all.
_NOT_REVIEWABLE_COMPLETENESS = {"incomplete"}


def _review_mode(review_moments: Optional[list[dict]],
                 capture_completeness: Optional[str] = None) -> str:
    """The run's explicit review mode (§4.15).

    ``not_reviewable`` when the source capture is too incomplete for a trustworthy
    review — evidence insufficiency, which the adapter declares, takes precedence
    over enrichment (a run with too little evidence is not reviewed, enriched or
    not). Otherwise ``model_enriched`` when any selected card carries model
    enrichment, else ``deterministic_only``.
    """
    if capture_completeness in _NOT_REVIEWABLE_COMPLETENESS:
        return "not_reviewable"
    return (
        "model_enriched"
        if any(m.get("enrichment_source") for m in (review_moments or []))
        else "deterministic_only"
    )


def _missing_capabilities(capabilities: dict) -> list[str]:
    """Capture capabilities that are absent or unavailable (§4.15).

    Listed explicitly so a ``not_reviewable`` review can say exactly which evidence
    types are missing rather than failing silently — the same honesty the forensic
    view applies per panel.
    """
    return sorted(k for k, level in (capabilities or {}).items()
                  if level in (None, "unavailable"))


def _evidence_grade(moment: dict, capture_completeness: Optional[str]) -> str:
    """Deterministic evidence grade for a moment card header (§4.7.1, §4.19).

    Distinct from *attribution* (which bounds causal language): the grade states
    how well the moment's facts are grounded. It is mechanical, not a judgement —

    - **strong**: at least one fact was re-validated by the reviewer envelope
      (``validation == "passed"``) and the capture is complete;
    - **moderate**: re-validated facts but partial capture, or unvalidated
      structured facts over a complete capture; and
    - **limited**: no facts, or unvalidated facts over an incomplete capture —
      the finding leans on provisional grounding.
    """
    facts = moment.get("facts") or []
    if not facts:
        return "limited"
    complete = capture_completeness == "complete"
    revalidated = any(f.get("validation") == "passed" for f in facts)
    if revalidated:
        return "strong" if complete else "moderate"
    return "moderate" if complete else "limited"


def _watermark(contract: dict) -> Optional[str]:
    """Reconstruct the review watermark from a persisted contract record.

    Mirrors ``pipeline.Analysis.watermark`` so the read layer and the CLI agree
    on the wording of a provisional-review banner (spec §6.4).
    """
    if not contract or contract.get("status") not in WATERMARKED_STATUSES:
        return None
    return (f"PROVISIONAL REVIEW — based on a {contract['status']} task "
            f"contract (v{contract['contract_version']}); not human-confirmed.")


# --- task opportunities chapter (§4.6) ---------------------------------------

# Human-readable words for the deterministic ability/trigger slugs. The signature
# table (§4.10) already speaks these words; resolving here keeps the Opportunities
# and Ability Signature chapters describing the same behaviour identically.
_OPPORTUNITY_ABILITY = {
    "tool_error_recovery": "Recover from tool failure",
    "verification_discipline": "Verify before submission",
}
_OPPORTUNITY_TRIGGER = {
    "tool_failure": "A tool call failed",
    "required_artifact_exists": "A required artifact existed before submission",
}
# trigger -> the Task Ability Signature ability whose deterministic evaluation
# measures the behaviour the opportunity's window created a chance to observe.
_TRIGGER_SIGNATURE_ABILITY = {
    "tool_failure": "Recover from tool failure",
    "required_artifact_exists": "Verify before submission",
}


def _opportunity_rows(opportunities: list[dict], signature: list[dict],
                      completeness: Optional[str]) -> list[dict]:
    """Per-opportunity rows for the Task Opportunities chapter (spec §4.6).

    Each row states what the run created an opportunity to observe and whether
    the selected review evaluated it. Status is *derived, never invented*:

    - ``measured``      a deterministic Task Ability Signature row evaluated the
      behaviour in this window;
    - ``not_evaluable`` the opportunity occurred but capture completeness leaves
      the behaviour unobservable;
    - ``not_reviewed``  the opportunity occurred and capture is complete, but the
      selected review mode did not evaluate the behaviour.

    ``not_measured`` (the opportunity never occurred) is intentionally never a row
    here — an opportunity is only listed once its window is reached. The
    never-occurred state is carried by the signature table instead (§4.10). The
    four states are kept distinct so the UI never collapses them to a blank cell.
    """
    sig_by_ability = {r.get("ability"): r for r in signature or []}
    rows: list[dict] = []
    for opp in opportunities or []:
        trigger = opp.get("trigger")
        sig = sig_by_ability.get(_TRIGGER_SIGNATURE_ABILITY.get(trigger, ""))
        if sig is not None and sig.get("measured"):
            status = "measured"
            observed = sig.get("observed_behaviour") or ""
            evidence = list(sig.get("evidence") or []) or list(opp.get("evidence") or [])
        elif completeness not in ("complete", None):
            status, observed = "not_evaluable", ""
            evidence = list(opp.get("evidence") or [])
        else:
            status, observed = "not_reviewed", ""
            evidence = list(opp.get("evidence") or [])
        rows.append({
            "opportunity_id": opp.get("opportunity_id"),
            "ability": _OPPORTUNITY_ABILITY.get(opp.get("ability"), opp.get("ability")),
            "trigger": _OPPORTUNITY_TRIGGER.get(trigger, trigger),
            "start_event_id": opp.get("start_event_id"),
            "end_event_id": opp.get("end_event_id"),
            "observed_behaviour": observed,
            "status": status,
            "evidence": evidence,
        })
    return rows


# --- human corrections (§4.13 Tier 3) ----------------------------------------

# Moment fields a Tier-3 correction can replace, and where each one lands on the
# moment view. Anything outside this map is carried on the correction record but
# never silently overwrites a generated value.
_CORRECTION_TARGETS = {
    "behaviour_tags": "behaviour_tags",
    "consequence": "consequence",
    "root_cause_candidates": "root_cause_candidates",
    "attribution_ceiling": "attribution_ceiling",
    "better_action": "better_action",
}


def _apply_corrections(moments: list[dict], feedback: list[dict]) -> None:
    """Overlay each moment's accepted human correction (§4.13), in place.

    After a correction the UI shows the accepted interpretation *by default* while
    the generated version stays inspectable, so each corrected moment carries:

    - the corrected values, applied onto the moment itself;
    - ``generated`` — the values they replaced, kept verbatim; and
    - ``correction`` — provenance (actor, revision, evidence, adjudication).

    The generated record is never rewritten in the store; this is a read-time
    overlay of an append-only annotation, which is why the same capture can be
    re-read without a correction by ignoring the feedback log.
    """
    latest: dict[str, dict] = {}
    for record in feedback or []:
        if record.get("kind") == "structured_correction" and record.get("moment_id"):
            latest[record["moment_id"]] = record  # append-only: last wins
    for moment in moments:
        record = latest.get(moment.get("moment_id"))
        if record is None:
            continue
        correction = record.get("correction") or {}
        generated = {}
        for field, target in _CORRECTION_TARGETS.items():
            if field in correction:
                generated[target] = moment.get(target)
                moment[target] = correction[field]
        if "decisive" in correction:
            moment["human_decisive"] = correction["decisive"]
        moment["corrected"] = True
        moment["generated"] = generated
        moment["correction"] = {
            "mutation_id": record.get("mutation_id"),
            "actor": record.get("actor"),
            "at": record.get("at"),
            "correction_revision": record.get("correction_revision"),
            "corrected_fields": record.get("corrected_fields", []),
            "evidence_event_ids": record.get("evidence_event_ids", []),
            "taxonomy_version": record.get("taxonomy_version"),
            "adjudication_status": record.get("adjudication_status"),
            "opportunity_window": correction.get("opportunity_window"),
            "linked_items": correction.get("linked_items"),
            "task_verifier_concern": correction.get("task_verifier_concern"),
            # §4.13 lineage: only relationships that actually exist may be shown.
            # A correction is applied to this review; nothing downstream consumes
            # it yet, so no lesson or dataset line is claimed.
            "lineage": ["Applied to this review"],
        }


# --- final environment / artifact summary (§4.5) ------------------------------

# Event types whose payload describes the environment rather than the agent's
# own messages or tool I/O. The closing observation of each is what the Outcome
# chapter reports as the run's final environment state.
_ENVIRONMENT_EVENTS = ("environment_observation", "process_started",
                       "process_observed", "error_observed")

# Observed artifact contents are restated verbatim up to this length; longer
# values are truncated with an explicit flag rather than silently cut.
_CONTENT_LIMIT = 200


def _capability_state(capabilities: dict, name: str) -> dict:
    level = capabilities.get(name)
    return {"capability": name, "level": level, "state": _availability(level)}


def _payload_detail(event: dict) -> Optional[str]:
    """The first populated human-readable payload field of an event."""
    payload = event.get("payload") or {}
    for key in ("content", "summary", "data"):
        if payload.get(key) not in (None, ""):
            return str(payload[key])[:_CONTENT_LIMIT]
    return None


def _final_state(events: list[dict], source: dict, capabilities: dict) -> dict:
    """The run's final environment/artifact summary (spec §4.5).

    The Outcome chapter has to answer "what did the run leave behind?" before any
    interpretation. This restates the *closing observed state* and nothing else:
    every declared artifact with its last observation (or an explicit
    ``never_observed``), any artifact observed but never declared, the last
    environment observation of each kind, and the last process exit the capture
    recorded.

    Declared artifacts are read from the immutable source (``task.artifacts``) —
    the same declaration the ``required_artifact_absent`` detector consumes — so
    a missing artifact still appears here when that detector could not run for
    lack of filesystem capture. The capability levels governing artifact and
    process evidence ride along in ``availability`` so an empty section reads as
    "not captured" rather than "nothing was there".
    """
    submission = next((e for e in reversed(events)
                       if e.get("event_type") == "final_submission"), None)
    submission_seq = submission.get("sequence") if submission else None

    declared = [a.get("path") for a in (source.get("task") or {}).get("artifacts", [])
                if a.get("path")]
    last_observation: dict[str, dict] = {}
    last_tool_call: dict[str, dict] = {}
    for e in events:
        payload = e.get("payload") or {}
        if e.get("event_type") == "artifact_observation" and payload.get("artifact_path"):
            last_observation[payload["artifact_path"]] = e
        elif e.get("event_type") == "tool_call" and payload.get("path"):
            last_tool_call[payload["path"]] = e

    artifacts: list[dict] = []
    for path in list(dict.fromkeys(declared + list(last_observation))):
        observed = last_observation.get(path)
        tool_call = last_tool_call.get(path)
        content = (observed.get("payload") or {}).get("content") if observed else None
        text = None if content is None else str(content)
        seq = observed.get("sequence") if observed else None
        artifacts.append({
            "path": path,
            "declared": path in declared,
            "state": "observed" if observed else "never_observed",
            "observed_at_event_id": observed.get("event_id") if observed else None,
            "observed_at_sequence": seq,
            "observed_after_submission": bool(
                seq is not None and submission_seq is not None and seq > submission_seq),
            "content": None if text is None else text[:_CONTENT_LIMIT],
            "content_truncated": bool(text is not None and len(text) > _CONTENT_LIMIT),
            "last_tool_event_id": tool_call.get("event_id") if tool_call else None,
        })

    closing: dict[str, dict] = {}
    for e in events:
        if e.get("event_type") in _ENVIRONMENT_EVENTS:
            closing[e["event_type"]] = e
    environment = [{
        "event_id": e["event_id"],
        "sequence": e.get("sequence"),
        "event_type": event_type,
        "detail": _payload_detail(e),
    } for event_type, e in sorted(closing.items(), key=lambda kv: kv[1].get("sequence") or 0)]

    exits = [e for e in events if e.get("event_type") == "tool_result"
             and (e.get("payload") or {}).get("exit_code") is not None]
    last = exits[-1] if exits else None
    return {
        "submission_event_id": submission.get("event_id") if submission else None,
        "artifacts": artifacts,
        "environment": environment,
        "last_exit": None if last is None else {
            "event_id": last["event_id"],
            "sequence": last.get("sequence"),
            "tool": (last.get("payload") or {}).get("tool"),
            "exit_code": str((last.get("payload") or {}).get("exit_code")),
        },
        "availability": {name: _capability_state(capabilities, name)
                         for name in ("filesystem", "process_state")},
    }


# --- public read-model views -------------------------------------------------


def _verification_summary(capabilities: dict, outcome: dict) -> dict:
    """How much of a task verdict this capture actually supports.

    Verifier *evidence* — not the outcome string — decides whether a run can be
    evaluated. The §6.2 capability profile is the normative source
    (``verifier_results`` / ``verifier_code``); ``outcome.total`` is the fallback
    for captures persisted before adapters declared it. Kept separate from the
    outcome so a verdict can never be inferred from an import or review error.
    """
    capabilities = capabilities or {}
    results = capabilities.get("verifier_results")
    code = capabilities.get("verifier_code")
    checks = outcome.get("total") or 0
    return {
        "has_verifier": bool(results and results != "unavailable") or checks > 0,
        "results": results,
        "code": code,
        "checks": checks,
        # ``checks > 1`` usually means per-test results (e.g. Harbor's
        # ``ctrf.json``) rather than only the aggregate reward — a proxy, not a
        # capability, so it is named as one.
        "atomic": checks > 1,
    }


# fleet.py's three endpoints (episodes/usage-summary/execution-quality) and
# queue.py's two (sweep/queue) each call list_runs() independently — on the
# Patterns page alone the browser fires 3-4 separate HTTP requests that all
# need it, and list_runs() itself is an os.walk() of the whole store plus
# several JSON reads per run, the dominant cost of every one of those
# requests. Cache the result per Store instance (a WeakKeyDictionary, not
# id(store): a plain int id can be recycled by a later, unrelated Store once
# an earlier one is garbage-collected, which would serve that new store a
# dead store's cached rows).
#
# Invalidation has two layers: Store._write_seq changes the instant THIS
# process writes anything list_runs reads (so e.g. POST /runs/{id}/workflow
# is reflected on the very next call, in-process, exactly — no window where
# a reader could see stale workflow state) — see Store's own writes for what
# bumps it. The short TTL beneath that is the only guard against a SEPARATE
# process (an `agr review`/ingest CLI run) writing to the same on-disk store
# while `agr serve` is up: this process's _write_seq can't see that, so the
# cache is also time-bounded, at the cost of up to _LIST_RUNS_CACHE_TTL_S of
# staleness against an external writer.
_LIST_RUNS_CACHE_TTL_S = 3.0
_list_runs_cache: "weakref.WeakKeyDictionary[Store, tuple[int, float, list[dict]]]" = (
    weakref.WeakKeyDictionary()
)
_list_runs_cache_lock = threading.Lock()


def list_runs(store: Store) -> list[dict]:
    """Summarise every logical run in the store, ordered by run id.

    Each summary is assembled from persisted records only; it never recomputes
    the pipeline. Runs are keyed by their filesystem directory (the logical
    ``run_id``) so a fuller capture of the same run stays a single row.

    Cached per store (see the module-level note above) — callers get a fresh
    ``list`` on every call (so an in-place ``.sort()`` like queue.py's can
    never corrupt the cached rows for the next caller), but the same row
    ``dict`` objects; nothing here mutates a row in place after building it,
    so sharing them across callers is safe.
    """
    with _list_runs_cache_lock:
        now = time.monotonic()
        cached = _list_runs_cache.get(store)
        if cached is not None:
            write_seq, computed_at, summaries = cached
            if write_seq == store._write_seq and (now - computed_at) < _LIST_RUNS_CACHE_TTL_S:
                return list(summaries)
        # Captured BEFORE the (I/O-bound, lock-released-during-disk-reads)
        # compute below, not after: a write landing on this store WHILE
        # _list_runs_uncached is mid-walk bumps store._write_seq during the
        # call, and if that new value were the one cached, the next reader
        # would see seq match and be served this now-stale snapshot for the
        # rest of the TTL. Caching the seq from before the compute means any
        # write concurrent with it forces a recompute on the very next call.
        seq_before = store._write_seq
        summaries = _list_runs_uncached(store)
        _list_runs_cache[store] = (seq_before, now, summaries)
        return list(summaries)


def _list_runs_uncached(store: Store) -> list[dict]:
    root = _runs_root(store)
    if not os.path.isdir(root):
        return []
    summaries: list[dict] = []

    def _discover_run_ids() -> list[str]:
        # Runs may be nested under namespace dirs (the harbor adapter keys runs
        # by task id, e.g. runs/harbor__terminal-bench/<task>/...). A dir is a
        # run only when it carries a capture index; namespace dirs are descended
        # into, run dirs are pruned (their captures/ subtree is not a run).
        found: list[str] = []
        for dirpath, dirnames, _ in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            if rel == ".":
                continue
            run_id = rel.replace(os.sep, "/")
            try:
                capture_id = store.latest_capture_id(run_id)
            except InvalidRunId:
                # F1 follow-up (review of PR #64): a directory left over from
                # before the path-safety charset existed (e.g. a colon in an
                # old logical_run_id) is not a listable run, but it must not
                # abort the WHOLE listing — skip it and keep walking, in case
                # something valid is nested underneath.
                continue
            if capture_id is not None:
                found.append(run_id)
                dirnames[:] = []
        return sorted(found)

    for run_id in _discover_run_ids():
        capture_id = store.latest_capture_id(run_id)
        if capture_id is None:
            continue
        entry = _index_entry(store, run_id, capture_id)
        rs = _read(store, run_id, capture_id, "run_source.json", {})
        outcome = _read(store, run_id, capture_id, "outcome.json", {})
        capabilities = _read(store, run_id, capture_id, "capabilities.json", {}).get(
            "capabilities", {})
        contract = _read(store, run_id, capture_id, "contract.json", {})
        events = _read(store, run_id, capture_id, "events.json", [])
        detector_results = _read(store, run_id, capture_id, "detector_results.json", [])
        recoveries = _read(store, run_id, capture_id, "recoveries.json", [])
        moments, review_moments, _ = _run_moments(store, run_id, capture_id, events, detector_results)
        concern = sum(1 for m in moments if m.get("polarity") != "positive")
        strength = sum(1 for m in moments if m.get("polarity") == "positive")
        # AGR-04: confirmed (good_recovery / retry_succeeded_without_strategy_
        # change) and plausible (plausibly_resolved) recovery are counted
        # separately — a plausible resolution must never silently read as
        # confirmed just because it also carries a resolution_event_id.
        recovery_counts = {"confirmed": 0, "plausible": 0, "unresolved": 0}
        for ep in recoveries:
            cls = ep.get("classification")
            if cls in ("good_recovery", "retry_succeeded_without_strategy_change"):
                recovery_counts["confirmed"] += 1
            elif cls == "plausibly_resolved":
                recovery_counts["plausible"] += 1
            elif cls == "unrecovered_failure":
                recovery_counts["unresolved"] += 1
        recovered = recovery_counts["confirmed"] > 0 or any(
            m.get("kind") == "recovery" and m.get("polarity") == "positive" for m in moments)
        wf = workflow.read_workflow(store, run_id)
        # U2 Runs table: the same "one decisive finding" the Overview chapter
        # leads with (mirrors the browser's mainFinding()) — the first
        # non-positive (negative/neutral) moment in selection order, or the
        # first positive one when none was found, so a triage reader gets a
        # finding headline without opening the run. None when the review has
        # no moments at all. "Strongest positive" in mainFinding()'s own
        # comment is exactly this fallback, not a separate strength ranking:
        # its strongestMoments()[0] is `moments.filter(positive)[0]`, i.e. the
        # first positive moment in the same selection-ordered list — the two
        # implementations are the same computation, not an approximation of it.
        main_finding_moment = next((m for m in moments if m.get("polarity") != "positive"), None) or next(
            (m for m in moments if m.get("polarity") == "positive"), None)
        summaries.append({
            "run_id": run_id,
            "task_id": rs.get("task_id"),
            # Runs surface (item: readable session identity) — the working
            # directory the harness ran in, when the source captured one.
            # None means the source never declared it, not that this run has
            # no repository.
            "cwd": rs.get("cwd"),
            "model": rs.get("model"),
            "agent": rs.get("agent"),
            "harness_version": rs.get("harness_version"),
            # The real sweep this run was ingested under (§7.1), or None when the
            # source declared none — the honest input to the §4.3.1 sweep summary.
            "sweep_id": rs.get("sweep_id"),
            # AGR-12: the configuration this run was ingested under (item 26's
            # runner harness stamps it; other sources may leave it None) — the
            # grouping a sibling comparison must prefer before sweep_id, since
            # two runs under different configurations (prompt, tool set,
            # harness version) are not attempting the same thing.
            "configuration_id": rs.get("configuration_id"),
            "capture_id": capture_id,
            "capture_revision": entry.get("capture_revision"),
            "capture_completeness": entry.get("capture_completeness"),
            "ingested_at": entry.get("ingested_at"),
            "started_at": rs.get("started_at"),
            "finished_at": rs.get("finished_at"),
            "duration_s": _duration_seconds(rs),
            "steps": _step_count(events),
            # Cost/tokens are shown only when the capture carries them (§4.3.3);
            # absent in the current ATIF, so these stay None rather than fabricated.
            "cost": rs.get("cost"),
            "tokens": rs.get("tokens"),
            "outcome": {
                "status": outcome.get("status"),
                "passed": outcome.get("passed"),
                "total": outcome.get("total"),
            },
            # Whether a task verdict is even possible for this capture, kept
            # separate from the outcome itself (§6.2). A caller can filter or
            # annotate on this without re-deriving it from the status string.
            "verification": _verification_summary(capabilities, outcome),
            "review_mode": _review_mode(review_moments, entry.get("capture_completeness")),
            "main_finding": (main_finding_moment or {}).get("summary"),
            "main_finding_polarity": (main_finding_moment or {}).get("polarity"),
            "counts": {
                "concern": concern,
                "strength": strength,
                "moments": len(moments),
                "recovery": sum(1 for m in moments if m.get("kind") == "recovery"),
            },
            # AGR-04: "recovered" (a triage convenience) reports CONFIRMED
            # recovery only; recovery_counts breaks out plausible/unresolved
            # explicitly rather than folding them silently into "recovered".
            "recovered": bool(recovered),
            "recovery_plausible": recovery_counts["plausible"] > 0,
            "recovery_counts": recovery_counts,
            "verifier_concern": bool(contract.get("warnings")),
            "contract": {
                "version": contract.get("contract_version"),
                "status": contract.get("status"),
                "watermarked": contract.get("status") in WATERMARKED_STATUSES,
            },
            "watermark": _watermark(contract),
            "workflow": {
                "workflow_version": wf.get("workflow_version", 0),
                "review_progress": wf.get("review_progress", "unreviewed"),
                "disposition": wf.get("disposition"),
                "reviewer": wf.get("reviewer"),
                "assignee": wf.get("assignee"),
                "handled_at": wf.get("handled_at"),
            },
        })
    return summaries


def get_audit(store: Store, run_id: str, capture_id: Optional[str] = None,
              feedback: Optional[list[dict]] = None) -> list[dict]:
    """The §11 Task & Verifier Audit rows for one run's latest capture.

    Reads the persisted ``audit.json`` and overlays human concern corrections
    (§4.13 Tier-3 ``task_verifier_concern``) onto the matching dimension row:
    a correction that names a dimension replaces the shown assessment by
    default, with the generated assessment kept under ``generated`` and full
    provenance under ``correction`` — the same read-time overlay rule the
    moment corrections follow. The store record is never rewritten.

    A correction on a dimension with *no* generated row (a capture analyzed
    before the audit stage existed, or an audit that produced nothing for it)
    still renders: the row is synthesised from the human record rather than
    silently dropped. Callers that already hold the latest capture id and/or
    the feedback log may pass them in to avoid re-reading.
    """
    if capture_id is None:
        capture_id = _latest(store, run_id)
    findings = _read(store, run_id, capture_id, "audit.json", [])
    if feedback is None:
        feedback = workflow.read_feedback(store, run_id)
    latest_by_dimension: dict[str, dict] = {}
    for record in feedback or []:
        if record.get("kind") != "structured_correction":
            continue
        concern = (record.get("correction") or {}).get("task_verifier_concern") or {}
        dimension = concern.get("dimension")
        if dimension:
            latest_by_dimension[dimension] = record  # append-only: last wins
    for finding in findings:
        record = latest_by_dimension.get(finding.get("dimension"))
        if record is None:
            continue
        concern = (record.get("correction") or {}).get("task_verifier_concern") or {}
        finding["generated"] = {"assessment": finding.get("assessment")}
        finding["assessment"] = concern["assessment"]
        finding["human_statement"] = concern.get("note")
        finding["corrected"] = True
        finding["correction"] = {
            "mutation_id": record.get("mutation_id"),
            "actor": record.get("actor"),
            "at": record.get("at"),
            "adjudication_status": record.get("adjudication_status"),
            "evidence_event_ids": record.get("evidence_event_ids", []),
        }
    existing = {f.get("dimension") for f in findings}
    for dimension, record in latest_by_dimension.items():
        if dimension in existing:
            continue
        concern = (record.get("correction") or {}).get("task_verifier_concern") or {}
        findings.append({
            "dimension": dimension,
            "assessment": concern["assessment"],
            "statement": concern.get("note")
                or "Human-raised concern; the deterministic audit stage produced no "
                   "row for this capture.",
            "human_statement": concern.get("note"),
            "corrected": True,
            "correction": {
                "mutation_id": record.get("mutation_id"),
                "actor": record.get("actor"),
                "at": record.get("at"),
                "adjudication_status": record.get("adjudication_status"),
                "evidence_event_ids": record.get("evidence_event_ids", []),
            },
        })
    return findings


# GR-1: a completed review ends in exactly one of these states. The first two
# are successful outcomes; the others are explicit non-abstentions — a failed or
# unconfigured reviewer is never rendered as "no decisive moment".
REVIEW_STATUSES = (
    "moments_found",           # completed review, one or more validated moments
    "no_decisive_moment",      # completed review that reached that conclusion
    "all_proposals_rejected",  # completed review whose proposals all failed validation
    "not_configured",          # no model reviewer set up; deterministic baseline only
    "review_failed",           # provider, SDK, or parsing error
    "incomplete",              # processing stopped early (budget, timeout, missing chunks)
)
SUCCESSFUL_REVIEW_STATUSES = frozenset({"moments_found", "no_decisive_moment"})


def _review_status(review_moments: Optional[list[dict]], errors: list,
                   served_key: Optional[str] = None,
                   telemetry: Optional[dict] = None) -> str:
    """The served review's state, kept explicit (AGR-06, GR-1).

    ``errors`` is the ACTIVE error list for the reviewer whose snapshot is
    served (F1 follow-up) — historical attempts live in review_attempts.json
    and never flip a successful retry back to failed. ``served_key`` names the
    reviewer whose snapshot is served: anything other than ``"deterministic"``
    (or a legacy envelope carrying model enrichment) means a model review ran,
    which is what separates "no decisive moment" from "not configured".

    ``incomplete`` wins over the other states: processing stopped early
    (budget, timeout, missing chunks), so no completed conclusion exists.
    """
    telemetry = telemetry or {}
    t_key = telemetry.get("reviewer_key")
    # The marker belongs to the attempt that produced it: it counts for the
    # served slot, or when a model attempt stopped early and the deterministic
    # baseline is what is left to serve.
    if telemetry.get("incomplete") and (
            t_key == served_key
            or (served_key in (None, "deterministic")
                and t_key not in (None, "deterministic"))):
        return "incomplete"
    if errors:
        return "review_failed"
    model_served = (served_key not in (None, "deterministic")) or any(
        m.get("enrichment_source") for m in (review_moments or []))
    if not model_served:
        return "not_configured"
    if review_moments and any(m.get("selected") for m in review_moments):
        return "moments_found"
    if not review_moments:
        return "no_decisive_moment"
    return "all_proposals_rejected"


def _review_counts(review_moments: Optional[list[dict]], telemetry: Optional[dict],
                   served_key: Optional[str]) -> dict:
    """GR-1 counts shown beside a status — proposals, selections, rejections.

    The served reviewer's own attempt telemetry is used when it matches the
    slot; otherwise the counts come from the served moments alone. The rejection
    count is what "all proposals rejected" displays.
    """
    moments = review_moments or []
    counts = {
        "proposed": len(moments),
        "selected": sum(1 for m in moments if m.get("selected")),
    }
    telemetry = telemetry or {}
    if telemetry.get("reviewer_key") == served_key:
        if telemetry.get("proposed") is not None:
            counts["proposed"] = telemetry["proposed"]
        if telemetry.get("selected") is not None:
            counts["selected"] = telemetry["selected"]
        # GR-2: how many grounded alternatives were proposed, attached, and
        # dropped (by reason) — the same measured-accounting the moments get.
        alt = telemetry.get("alternatives") or {}
        if alt:
            counts["alternatives"] = {
                "proposed": alt.get("proposed", 0),
                "attached": alt.get("attached", 0),
                "dropped": dict(alt.get("dropped") or {}),
            }
        rejections = telemetry.get("rejections") or {}
        if rejections:
            counts["rejections"] = dict(rejections)
            counts["rejected"] = sum(rejections.values())
            return counts
    counts["rejected"] = max(0, counts["proposed"] - counts["selected"])
    return counts


def _attach_store_alternatives(store: Store, run_id: str, moments: list[dict]) -> dict:
    """Attach the store-backed OBSERVED and VALIDATED alternatives at read time (GR-2).

    Both are sourced from other records — a passing sibling run and a linked
    experiment — that can be ingested or changed *after* the review was computed.
    Deriving them here, from the whole store, means a newly found sibling or a
    recorded experiment outcome is reflected without reanalysis (PR #91 review),
    and a deterministic moment can carry them too. A suggested alternative stays
    with the persisted model review — that one is model output, not store state.

    Returns drop counts (an alternative matching no moment is counted, never
    invented onto one). Lazy imports avoid the ``read`` ↔ ``divergence`` cycle.
    """
    from . import divergence, lessons
    from .schema import alternative_label

    capture_id = _latest(store, run_id)
    events = _read(store, run_id, capture_id, "events.json", [])
    seq_of = {e.get("event_id"): e.get("sequence") for e in events}
    drops: dict[str, int] = {}

    def _attach(raw: dict, kind: str) -> None:
        mid, eid = raw.get("moment_id"), raw.get("event_id")
        target = None
        if mid:
            target = next((m for m in moments if m.get("moment_id") == mid), None)
        if target is None and eid:
            target = next(
                (m for m in moments if eid in (m.get("anchor_event_ids") or [])), None)
        if target is None and eid and seq_of.get(eid) is not None:
            # No moment anchors the divergence event itself: attach to the
            # nearest moment by timeline position. A failed run often has its
            # only moment *after* the divergence (e.g. the submission that
            # omitted the work), and that moment is exactly where the observed
            # alternative reads as context — so search both directions, never
            # dropping the alternative merely because the moment follows.
            seq = seq_of[eid]
            positioned = [m for m in moments if m.get("sequence") is not None]
            if positioned:
                target = min(positioned, key=lambda m: abs(m["sequence"] - seq))
        if target is None:
            key = f"{kind}_no_matching_moment"
            drops[key] = drops.get(key, 0) + 1
            return
        target.setdefault("alternatives", []).append({
            "kind": kind,
            "label": alternative_label(kind, raw.get("validation")),
            "proposal": raw.get("proposal") or "",
            "source": raw.get("source") or kind,
            "attribution_ceiling": raw.get("attribution_ceiling") or target.get("attribution_ceiling"),
            "limits": list(raw.get("limits") or []),
            "replaces_decision": None,
            "information_available": [],
            "assumptions": [],
            "sibling_diff": raw.get("sibling_diff"),
            "validation": raw.get("validation"),
            "rejected": False,
            "rejection_reason": None,
        })

    observed = divergence.observed_alternative(store, run_id)
    if observed:
        _attach(observed, "observed")
    validated = lessons.validated_alternative(store, run_id)
    if validated:
        _attach(validated, "validated")
    return drops


def get_review(store: Store, run_id: str, reviewer_key: Optional[str] = None) -> dict:
    """The full review of one run — the JSON form of ``agr show``.

    Bundles identity, outcome, task contract (with its provisional watermark),
    atomic checks, evidence slices, the Task Ability Signature, opportunities,
    recovery episodes, per-item contract observations, and detector results,
    each read from its persisted derived record. The §4.5 ``final_state``
    summary additionally reads the immutable source for its artifact
    declarations; like every value here it restates, never re-derives.

    ``reviewer_key`` selects which reviewer's snapshot to read when more than one
    has scored the capture. ``None`` picks the most-enriched available
    review (a model pass if present, else the deterministic baseline) so the
    default UI surface stays unchanged.
    """
    capture_id = _latest(store, run_id)
    entry = _index_entry(store, run_id, capture_id)
    contract = _read(store, run_id, capture_id, "contract.json", {})
    events = _read(store, run_id, capture_id, "events.json", [])
    detector_results = _read(store, run_id, capture_id, "detector_results.json", [])
    review_errors = _read(store, run_id, capture_id, "review_errors.json", [])
    moments, review_moments, served_key = _run_moments(
        store, run_id, capture_id, events, detector_results, reviewer_key=reviewer_key)
    chosen_key = served_key if served_key is not None else _default_reviewer_key(store, run_id, capture_id)
    # F1 follow-up: review_errors.json holds only each reviewer's ACTIVE error
    # (pipeline resolves an entry on a successful retry) — historical attempts
    # are in review_attempts.json and never flip a fixed review back to failed.
    all_active_errors = _read(store, run_id, capture_id, "review_errors.json", [])
    # Review 2026-09-07 (R4): the served review's status is not contaminated by
    # OTHER reviewers' errors. An explicitly requested reviewer reports only
    # its own state — a healthy model:B must not read as an abstention because
    # model:A failed. On the default surface, the deterministic baseline is
    # served when a model reviewer is failing, and the view says exactly that:
    # enrichment failed, deterministic baseline shown.
    if reviewer_key:
        review_errors = [e for e in all_active_errors if e.get("reviewer_key") == reviewer_key]
    elif chosen_key != "deterministic":
        review_errors = [e for e in all_active_errors if e.get("reviewer_key") == chosen_key]
    else:
        review_errors = all_active_errors
    # Errors from reviewers other than the one serving this view stay visible
    # as those reviewers' status — never merged into the served review's state.
    other_reviewer_errors = [e for e in all_active_errors
                             if e not in review_errors]
    # Attach a deterministic evidence grade for the §4.7.1 moment-card header.
    completeness = entry.get("capture_completeness")
    for m in moments:
        m["evidence_grade"] = _evidence_grade(m, completeness)
    # Overlay accepted human corrections (§4.13): the accepted interpretation is
    # what the review serves by default, with the generated values carried beside
    # it. The store keeps the generated moment untouched.
    feedback = workflow.read_feedback(store, run_id)
    _apply_corrections(moments, feedback)
    # GR-2: the store-backed alternatives (a passing sibling, a linked experiment)
    # are attached here, so they reflect the current store without reanalysis.
    alternative_drops = _attach_store_alternatives(store, run_id, moments)
    opportunities = _read(store, run_id, capture_id, "opportunities.json", [])
    signature = _read(store, run_id, capture_id, "signature.json", [])
    capabilities = _read(store, run_id, capture_id, "capabilities.json", {}).get("capabilities", {})
    source = store.read_source(run_id, capture_id)
    # GR-1: the served review ends in exactly one status; the counts beside it
    # (proposals / selections / rejections) are what "all proposals rejected"
    # displays. Both are computed once against the same served reviewer key.
    review_telemetry = _read(store, run_id, capture_id, "review_telemetry.json", {})
    review_status = _review_status(review_moments, review_errors, chosen_key, review_telemetry)
    review_counts = _review_counts(review_moments, review_telemetry, chosen_key)
    # Fold the read-time alternative drops into the counts, and report the total
    # attached on the served moments (suggested + observed + validated).
    attached = sum(len(m.get("alternatives") or []) for m in moments)
    if alternative_drops or attached:
        alt_counts = review_counts.setdefault(
            "alternatives", {"proposed": 0, "attached": 0, "dropped": {}})
        alt_counts["attached"] = attached
        for reason, n in alternative_drops.items():
            alt_counts["dropped"][reason] = alt_counts["dropped"].get(reason, 0) + n
    # GR-3: which of the idea's four categories the SERVED moments actually
    # covered, and how many moments each. Reported from what was found — an
    # uncovered category is simply absent, never padded.
    category_counts: dict[str, int] = {}
    for m in moments:
        for category_id in m.get("categories") or []:
            category_counts[category_id] = category_counts.get(category_id, 0) + 1
    review_counts["categories"] = {
        "covered": sorted(category_counts),
        "counts": category_counts,
    }
    # GR-4: the reviewer model and date behind the SERVED snapshot, when a demo
    # baked a pre-computed review. Read straight from the record — the read layer
    # invents nothing, and a live review has no meta (its provenance is the
    # reviewer_key + telemetry).
    review_meta = _read(store, run_id, capture_id, "review_meta.json", {})
    served_meta = review_meta.get(chosen_key) or {}
    return {
        "review_meta": review_meta,
        "review_model": served_meta.get("model"),
        "reviewed_at": served_meta.get("reviewed_at"),
        "read_model_version": version.READ_MODEL_VERSION,
        "run": _read(store, run_id, capture_id, "run_source.json", {}),
        "capture": {
            "capture_id": capture_id,
            "capture_revision": entry.get("capture_revision"),
            "capture_completeness": entry.get("capture_completeness"),
            "source_hash": entry.get("source_hash"),
            "supersedes_source_capture_id": entry.get("supersedes_source_capture_id"),
            "ingested_at": entry.get("ingested_at"),
        },
        "capabilities": capabilities,
        "outcome": _read(store, run_id, capture_id, "outcome.json", {}),
        # Captures ingested before the execution-quality detectors existed have
        # no persisted summary; recompute it from their own events so an old
        # store shows the same evaluated/unevaluated states as a fresh one.
        # The events/capabilities/source are already loaded above — no re-read.
        "execution_quality": execution_quality_record(
            store, run_id, capture_id,
            raw_events=events, capabilities=capabilities, source=source,
        ),
        # Closing observed state for the Outcome chapter (§4.5). Reads the
        # immutable source for the artifact declarations; derives no judgement.
        "final_state": _final_state(events, source, capabilities),
        "watermark": _watermark(contract),
        "contract": contract,
        # GR-4: a demo clears the watermark under ``demo_confirmed`` — a distinct
        # status that must never read as a human confirmation. Stated explicitly
        # so the report and the UI can label the override.
        "contract_status": contract.get("status"),
        "contract_demo_override": contract.get("status") == "demo_confirmed",
        "contract_observations": _read(store, run_id, capture_id, "contract_observations.json", []),
        "checks": _read(store, run_id, capture_id, "checks.json", []),
        "phases": _read(store, run_id, capture_id, "phases.json", []),
        "review_mode": _review_mode(review_moments, completeness),
        # Which capture capabilities are missing (§4.15) — always stated so a
        # not_reviewable review can name exactly what is absent.
        "missing_capabilities": _missing_capabilities(capabilities),
        "reviewer_key": chosen_key,
        # Review 2026-09-07 (R4): requested vs served are named separately, so a
        # fallback (or a stale-slot skip) is observable instead of silent.
        "requested_reviewer_key": reviewer_key,
        "served_reviewer_key": chosen_key,
        "available_reviews": store.list_reviews(run_id, capture_id),
        # AGR-06: explicit review states. ``review_errors`` records the served
        # reviewer's ACTIVE enrichment failure (malformed model output, provider
        # error) separately from a valid empty review; ``review_status`` names
        # the served state so the UI never renders a failure as "no decisive
        # moment". F1 follow-up: a successful retry resolves its active error,
        # and per-attempt history lives in review_attempts.json.
        "review_errors": review_errors,
        "other_reviewer_errors": other_reviewer_errors,
        "review_attempts": _read(store, run_id, capture_id, "review_attempts.json", []),
        "review_telemetry": review_telemetry,
        "review_status": review_status,
        # GR-1: only moments_found and no_decisive_moment are successful
        # outcomes; the rest are explicit non-abstentions (a failed or
        # unconfigured reviewer is never rendered as "no decisive moment").
        "review_status_success": review_status in SUCCESSFUL_REVIEW_STATUSES,
        "review_counts": review_counts,
        "moments": moments,
        "review_moments": review_moments if review_moments is not None else [],
        "evidence_slices": _read(store, run_id, capture_id, "evidence_slices.json", []),
        "signature": signature,
        "opportunities": opportunities,
        # §11 Task & Verifier Audit — categorical qualifications of what this
        # run's result can legitimately imply (with human concern overlay).
        # Reuses this call's capture id and feedback log; no second read.
        "audit": get_audit(store, run_id, capture_id=capture_id, feedback=feedback),
        # Derived §4.6 rows: each opportunity joined to its signature evaluation
        # so the chapter shows measured/not_reviewed/not_evaluable distinctly.
        "opportunity_rows": _opportunity_rows(opportunities, signature, completeness),
        "recoveries": _read(store, run_id, capture_id, "recoveries.json", []),
        "detector_results": detector_results,
        # Human review workflow (§4.3.4) + Tier-1/2/3 feedback (§4.13). Mutable,
        # versioned, and stored beside the immutable source — never mutating it.
        "workflow": workflow.read_workflow(store, run_id),
        "feedback": feedback,
        # Eval Lessons accepted from this run's moments (§4.11, §6.10). Mutable and
        # versioned like the workflow above, written beside the immutable source.
        "lessons": lessons.read_lessons(store, run_id),
    }


def get_forensic(store: Store, run_id: str) -> dict:
    """The synchronized forensic view of one run (spec §4.5, Level 4).

    Returns the capture's capability badge plus one row per *source step*.
    Selecting a step in a UI reads that row: the derived event(s) it produced,
    the single content panel its payload belongs to, and that panel's
    availability derived from the capability profile — so unavailable evidence
    is visibly marked. The verifier panel is a run-level list of atomic checks
    (verifier checks are not source steps in ATIF); a check is attached to a
    step when its ``source_pointers`` name that step.
    """
    capture_id = _latest(store, run_id)
    source = store.read_source(run_id, capture_id)
    capabilities = _read(store, run_id, capture_id, "capabilities.json", {}).get("capabilities", {})
    events = _read(store, run_id, capture_id, "events.json", [])
    checks = _read(store, run_id, capture_id, "checks.json", [])
    phases = _read(store, run_id, capture_id, "phases.json", [])
    execution_quality = execution_quality_record(
        store, run_id, capture_id,
        raw_events=events, capabilities=capabilities, source=source,
    )
    finding_by_event: dict[str, list[dict]] = {}
    for finding in execution_quality.get("findings", []):
        evidence_event_ids = list(finding.get("anchor_event_ids", []))
        for fact in finding.get("structured_facts", []):
            if fact.get("event_id"):
                evidence_event_ids.append(fact["event_id"])
            evidence_event_ids.extend(fact.get("result_event_ids", []))
        for event_id in dict.fromkeys(evidence_event_ids):
            finding_by_event.setdefault(event_id, []).append({
                "candidate_id": finding.get("candidate_id"),
                "detector": finding.get("detector"),
                "severity": finding.get("severity"),
            })

    events_by_step: dict[str, list[dict]] = {}
    for evt in events:
        for step_id in evt.get("source_step_ids", []):
            events_by_step.setdefault(step_id, []).append(evt)
    # Safe to key by event_id alone (no last-wins collision, and a paired
    # call/result can never resolve to the SAME step as itself): every event
    # this read model ever sees was built by agr.events.derive_events, which
    # assigns each event exactly one source_step_ids entry from its own
    # ATIF step — the mapping is a bijection, one event per step and one
    # step per event, by construction.
    step_of_event = {evt["event_id"]: step_id
                     for step_id, evs in events_by_step.items() for evt in evs}
    paired_event = _pair_tool_events(events)

    rows: list[dict] = []
    for step in source.get("steps", []):
        step_id = step["step_id"]
        step_events = events_by_step.get(step_id, [])
        primary = step_events[0] if step_events else None
        event_type = primary["event_type"] if primary else None
        panel = _EVENT_PANEL.get(event_type, "timeline") if event_type else "timeline"
        cap = _PANEL_CAPABILITY.get(panel)
        level = capabilities.get(cap) if cap else None
        # The paired call (for a result) or result (for a call) — same step,
        # not event, since a UI selects steps. None when this step's event has
        # no matched counterpart (an id-less capture's unresolved result, or a
        # call the capture never observed a result for) or is not a tool_call/
        # tool_result step at all.
        paired_step_id = None
        if primary is not None:
            paired_eid = paired_event.get(primary["event_id"])
            if paired_eid is not None:
                paired_step_id = step_of_event.get(paired_eid)
        rows.append({
            "step_id": step_id,
            "kind": step.get("kind"),
            "actor": step.get("actor"),
            "event_ids": [e["event_id"] for e in step_events],
            "event_type": event_type,
            "sequence": primary["sequence"] if primary else None,
            "phase_id": primary.get("phase_id") if primary else None,
            "panel": panel,
            "paired_step_id": paired_step_id,
            "content": _panel_content(step, event_type),
            "availability": {
                "capability": cap,
                "level": level,
                "state": _availability(level),
            },
            "verifier_checks": [c["check_id"] for c in checks
                                if _pointers_reference_step(c, step_id)],
            "execution_findings": [
                finding
                for event in step_events
                for finding in finding_by_event.get(event["event_id"], [])
            ],
        })

    return {
        "read_model_version": version.READ_MODEL_VERSION,
        "run_id": run_id,
        "capture_id": capture_id,
        "capability_badge": {
            cap: {"level": lvl, "state": _availability(lvl)}
            for cap, lvl in capabilities.items()
        },
        "panels": list(_PANEL_CAPABILITY.keys()) + ["timeline"],
        "phases": phases,
        "steps": rows,
        "verifier": checks,
        "execution_quality": execution_quality,
    }


def get_source(store: Store, run_id: str) -> dict:
    """The raw immutable source plus a hash re-verification.

    Recomputes the canonical source hash from the stored bytes and compares it
    to the hash recorded at ingest, satisfying the Milestone 1 accept criterion
    "imported source bytes match hashes". A mismatch means the immutable record
    was tampered with; ``verified`` is ``False`` and both hashes are returned.
    """
    capture_id = _latest(store, run_id)
    entry = _index_entry(store, run_id, capture_id)
    doc = store.read_source(run_id, capture_id)
    recorded = entry.get("source_hash")
    computed = source_hash(doc)
    return {
        "run_id": run_id,
        "capture_id": capture_id,
        "capture_revision": entry.get("capture_revision"),
        "capture_completeness": entry.get("capture_completeness"),
        "adapter_version": entry.get("adapter_version"),
        "recorded_source_hash": recorded,
        "computed_source_hash": computed,
        "verified": recorded == computed,
        "source": doc,
    }


def list_reviews(store: Store, run_id: str) -> list[str]:
    """The reviewer keys that have scored this run's latest capture.

    ``["deterministic"]`` for a run scored only by the baseline envelope; a
    ``model:<source>`` key appears alongside it once a Stage F reviewer has run.
    The reviewer-diff view uses this to decide whether a side-by-side view is
    available (two or more keys) or to show its honest empty state.
    """
    capture_id = _latest(store, run_id)
    return store.list_reviews(run_id, capture_id)


def get_comparison(store: Store, run_id: str, left_key: str, right_key: str,
                    threshold: float = 0.0) -> dict:
    """Diff two reviews of one run — reviewer vs reviewer (see :mod:`agr.compare`).

    Not the §4.16 matched *version* comparison; that is :mod:`agr.versions`.

    Reads each reviewer's snapshot (via :func:`get_review` with the reviewer_key)
    plus the shared forensic view, then delegates to
    :func:`agr.compare.compare_reviews` for anchor-overlap alignment and
    field-level diffs. Both reviews must exist for the latest capture.
    """
    from . import compare
    available = list_reviews(store, run_id)
    if left_key not in available or right_key not in available:
        raise RunNotFound(run_id)
    left = get_review(store, run_id, reviewer_key=left_key)
    right = get_review(store, run_id, reviewer_key=right_key)
    forensic = get_forensic(store, run_id)
    return compare.compare_reviews(left, right, forensic, left_key, right_key,
                                   threshold=threshold)


# --- projection details ------------------------------------------------------


def _pair_tool_events(events: list[dict]) -> dict[str, str]:
    """``event_id -> paired event_id`` for every matched tool_call/tool_result
    pair, so the forensic view can show a request and its response together
    (spec §4.9/§4.12: "the matching tool request and result together") instead
    of only whichever one a reader happened to click.

    Mirrors :func:`agr._util.paired_result` specifically (the function its own
    docstring names as "the ONE call/result index used by ... evidence
    views"), not :func:`agr._util.paired_call`: matched by ``tool_use_id``
    when BOTH sides carry one (correct for parallel calls), else the nearest
    following id-less result with no other call in between. A result whose id
    matches no captured call, or an id-less result following an id-BEARING
    call, stays unpaired rather than guessed — ``paired_call`` alone falls
    back to adjacency whenever the RESULT is id-less regardless of whether
    the call it is nearest to actually carries an id; ``paired_result`` (and
    this function) does not, since that direction is what recovery,
    repetition detection, and fact validation already rely on for a
    consistent call/result index. A capture that inconsistently tags only one
    side of a pair with an id is not expected in practice (source adapters
    tag both sides or neither), so this stays unpaired-by-design rather than
    guessed.
    """
    pairs: dict[str, str] = {}
    pending_by_id: dict[str, str] = {}
    pending_adjacent: Optional[str] = None
    for e in events:
        et = e.get("event_type")
        if et == "tool_call":
            cid = (e.get("payload") or {}).get("tool_use_id")
            if cid:
                pending_by_id[str(cid)] = e["event_id"]
                pending_adjacent = None
            else:
                pending_adjacent = e["event_id"]
        elif et == "tool_result":
            cid = (e.get("payload") or {}).get("tool_use_id")
            call_id = pending_by_id.pop(str(cid), None) if cid else None
            if call_id is None and not cid:
                call_id = pending_adjacent
            if call_id is not None:
                pairs[call_id] = e["event_id"]
                pairs[e["event_id"]] = call_id
            pending_adjacent = None
    return pairs


def _panel_content(step: dict, event_type: Optional[str]) -> dict:
    """Project a source step's fields onto its panel's display content."""
    content: dict[str, Any] = {}
    for key in ("content", "data", "path", "artifact_path", "tool", "exit_code", "summary"):
        if key in step:
            content[key] = step[key]
    if event_type in ("tool_call", "tool_result"):
        content["direction"] = "call" if event_type == "tool_call" else "result"
    if event_type == "model_output" or step.get("generation_event") is True:
        input_tokens, output_tokens, total_tokens = generation_token_counts(step.get("cost"))
        for target, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("total_tokens", total_tokens),
        ):
            if value is not None:
                content[target] = value
        for key in ("start_time", "end_time", "provider", "model"):
            if key in step:
                content[key] = step[key]
        wall_ms = generation_wall_ms(
            step.get("start_time", step.get("timestamp")), step.get("end_time")
        )
        if wall_ms is not None:
            content["wall_ms"] = wall_ms
    return content


def _pointers_reference_step(check: dict, step_id: str) -> bool:
    """True when a verifier check's source pointers name this source step."""
    return any(p == step_id or p.startswith(f"{step_id}:")
               for p in check.get("source_pointers", []))
