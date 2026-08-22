"""Human review workflow — dispositions and lightweight feedback (spec §4.3.4, §4.13, §4.17).

The deterministic pipeline and the read layer never mutate anything a human
records. Human review state is *mutable* and versioned, but it is written as
derived records next to the immutable source — exactly like the task-contract
confirmation (:mod:`agr.contract` / ``cmd_confirm``): source bytes and generated
moments are never rewritten; a correction is a new record, not an edit.

Two records live in each run's latest capture dir:

- ``workflow.json`` — the run's review-progress + disposition state (§4.3.4),
  with an append-only ``revisions`` log (actor, timestamp, prior state).
- ``feedback.json`` — an append-only list of Tier-1/Tier-2 feedback records
  (§4.13), each stamped with a ``mutation_id`` so a retried write is idempotent.

Writes to the mutable ``workflow.json`` carry an optimistic ``base_version``
(§4.17): a stale write is refused so a concurrent editor never silently clobbers
and the caller keeps its unsaved edit. Append-only feedback needs no version — a
repeated ``mutation_id`` is a no-op, which is the idempotency §4.17 requires.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import time
from typing import Optional

from . import taxonomy, version
from .schema import ATTRIBUTION_LEVELS, AUDIT_DIMENSIONS, CONCERN_ASSESSMENTS
from .store import Store

WORKFLOW_RECORD = "workflow.json"
FEEDBACK_RECORD = "feedback.json"

# §4.3.4 — the two independent human-workflow dimensions.
REVIEW_PROGRESS = ("unreviewed", "in_progress", "handled")
DISPOSITIONS = (
    "diagnosis_accepted",
    "corrected",
    "task_or_verifier_issue",
    "needs_followup",
    "no_action",
)

# §4.13 — Tier-1 quick feedback, the Tier-2 quick relabel, and the Tier-3 editor.
FEEDBACK_KINDS = ("agree", "not_decisive", "flag_task_verifier", "quick_relabel",
                  "structured_correction")

# §4.13 Tier 2 — user-facing group labels mapped to controlled taxonomy values
# (:mod:`agr.taxonomy`). ``other`` carries no token; it escalates to the Tier-3
# structured editor (a later phase). Every mapped value is asserted to be a real
# behaviour tag at import time so the map can never drift from the taxonomy.
QUICK_RELABEL_GROUPS = {
    "lost_requirement": "lost_requirement",
    "skipped_verification": "skipped_verification",
    "poor_tool_or_query": "poor_query",
    "premature_completion": "premature_submission",
    "failed_recovery": "failed_to_replan",
    "other": None,
}
assert all(
    tok is None or tok in taxonomy.BEHAVIOUR_TAGS
    for tok in QUICK_RELABEL_GROUPS.values()
), "QUICK_RELABEL_GROUPS references a token outside the controlled taxonomy"


# --- §4.13 Tier 3 — full structured correction -------------------------------
#
# The expert editor's nine fields. Each is validated against the controlled
# taxonomy before it is stored: a correction that cannot be expressed in the
# active vocabulary is refused rather than written as free text, so corrections
# stay machine-consumable by the reviewer evaluation set (§15) later.

CORRECTION_FIELDS = (
    "behaviour_tags", "consequence", "root_cause_candidates", "attribution_ceiling",
    "decisive", "better_action", "opportunity_window", "linked_items",
    "task_verifier_concern",
)

# §11.2 assessment values live in :mod:`agr.schema` (``CONCERN_ASSESSMENTS``),
# shared with the deterministic Task & Verifier Audit stage so a human concern
# and an audit finding can never drift into different vocabularies.

# Corrections that restate *what happened* — as opposed to how it is interpreted —
# must cite evidence (§4.13 "requires evidence for a corrected factual claim").
# Interpretive fields (tags, ranking, better action, decisiveness) are a reviewer's
# judgement and are accepted on their own authority.
FACTUAL_CORRECTION_FIELDS = ("consequence", "opportunity_window", "linked_items")

# A human may not hand themselves replay evidence. `counterfactually_supported`
# requires a controlled branch that changed the outcome (§3.5); this package
# generates none, so the level is unreachable by assertion — the same ceiling the
# attribution gate (§8.9) applies to the model reviewer.
UNREACHABLE_ATTRIBUTION = "counterfactually_supported"


class WorkflowError(ValueError):
    """A malformed or disallowed workflow write (bad enum, handled sans disposition)."""


class VersionConflict(Exception):
    """An optimistic write lost to a concurrent edit (§4.17).

    Carries the current persisted workflow so a caller (and the HTTP layer's 409)
    can show the newer state without discarding the user's unsaved edit.
    """

    def __init__(self, expected: int, actual: int, current: dict):
        super().__init__(f"stale workflow write: base_version {expected} != current {actual}")
        self.expected = expected
        self.actual = actual
        self.current = current


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_workflow() -> dict:
    """The workflow state of a run no human has touched yet."""
    return {
        "workflow_version": 0,
        "review_progress": "unreviewed",
        "disposition": None,
        "assignee": None,
        "reviewer": None,
        "note": None,
        "handled_at": None,
        "updated_at": None,
        "revisions": [],
    }


# --- reads -------------------------------------------------------------------


def read_workflow(store: Store, run_id: str) -> dict:
    """The run's current workflow state, or a default when none is recorded."""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        return default_workflow()
    if store.has_derived(run_id, capture_id, WORKFLOW_RECORD):
        return store.read_derived(run_id, capture_id, WORKFLOW_RECORD)
    return default_workflow()


def read_feedback(store: Store, run_id: str) -> list[dict]:
    """Every Tier-1/Tier-2 feedback record for the run, in submission order."""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        return []
    if store.has_derived(run_id, capture_id, FEEDBACK_RECORD):
        return store.read_derived(run_id, capture_id, FEEDBACK_RECORD)
    return []


# --- writes ------------------------------------------------------------------


def _require_capture(store: Store, run_id: str) -> str:
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        raise WorkflowError(f"run {run_id!r} has no captures in the store")
    return capture_id


def _touch_in_progress(wf: dict) -> bool:
    """Move an untouched review to ``in_progress`` (§4.3.4 "opening or editing").

    Returns whether a transition happened so the caller can log a revision. Never
    downgrades ``handled`` — merely editing a handled run keeps it handled.
    """
    if wf["review_progress"] == "unreviewed":
        wf["review_progress"] = "in_progress"
        return True
    return False


def set_workflow(
    store: Store,
    run_id: str,
    *,
    actor: Optional[str],
    base_version: int,
    progress: Optional[str] = None,
    disposition: Optional[str] = None,
    assignee: Optional[str] = None,
    reviewer: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """Set disposition / assignment / progress on a run's review (§4.3.4).

    Enforces the workflow rules the spec makes normative:

    - ``handled`` requires a disposition (given now or already recorded);
    - merely opening/editing a run moves ``unreviewed`` → ``in_progress`` but
      never marks it ``handled``; and
    - reopening a handled run preserves the earlier disposition and appends a new
      revision rather than clearing it.

    The write is optimistic: ``base_version`` must equal the current
    ``workflow_version`` or :class:`VersionConflict` is raised with the live state.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)
    wf = read_workflow(store, run_id)

    if base_version != wf["workflow_version"]:
        raise VersionConflict(base_version, wf["workflow_version"], wf)

    if progress is not None and progress not in REVIEW_PROGRESS:
        raise WorkflowError(f"unknown review_progress {progress!r}")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise WorkflowError(f"unknown disposition {disposition!r}")

    prior = {
        "review_progress": wf["review_progress"],
        "disposition": wf["disposition"],
        "assignee": wf["assignee"],
        "reviewer": wf["reviewer"],
    }

    if disposition is not None:
        wf["disposition"] = disposition
    if assignee is not None:
        wf["assignee"] = assignee
    if reviewer is not None:
        wf["reviewer"] = reviewer
    if note is not None:
        wf["note"] = note

    target = progress
    if target is None:
        # No explicit progress: editing at least opens the review.
        _touch_in_progress(wf)
    elif target == "handled":
        if wf["disposition"] is None:
            raise WorkflowError("cannot mark a run 'handled' without a disposition")
        wf["review_progress"] = "handled"
        wf["handled_at"] = now
    else:
        wf["review_progress"] = target

    wf["workflow_version"] += 1
    wf["updated_at"] = now
    wf["revisions"].append({
        "version": wf["workflow_version"],
        "actor": actor,
        "at": now,
        "prior": prior,
        "workflow_writer_version": version.READ_MODEL_VERSION,
    })
    store.write_derived(run_id, capture_id, WORKFLOW_RECORD, wf)
    return wf


def _validate_correction(correction: dict, evidence_event_ids: list[str]) -> dict:
    """Validate a Tier-3 correction against the controlled taxonomy (§4.13).

    Returns the accepted correction (only the fields actually corrected). Raises
    :class:`WorkflowError` on an unknown field, a token outside the active
    taxonomy, an attribution level no evidence in this system can support, or a
    corrected *factual* claim with no evidence cited.
    """
    if not isinstance(correction, dict) or not correction:
        raise WorkflowError("a structured correction must set at least one field")
    unknown = sorted(set(correction) - set(CORRECTION_FIELDS))
    if unknown:
        raise WorkflowError(f"unknown correction field(s) {unknown}")

    clean: dict = {}
    for field, value in correction.items():
        if field == "behaviour_tags":
            tags = list(value or [])
            bad = sorted(set(tags) - taxonomy.BEHAVIOUR_TAGS)
            if bad:
                raise WorkflowError(f"behaviour tag(s) outside the taxonomy: {bad}")
            clean[field] = tags
        elif field == "consequence":
            if value not in taxonomy.CONSEQUENCES:
                raise WorkflowError(f"consequence {value!r} is outside the taxonomy")
            clean[field] = value
        elif field == "root_cause_candidates":
            ranked = list(value or [])
            for candidate in ranked:
                if candidate.get("locus") not in taxonomy.ROOT_CAUSE_LOCI:
                    raise WorkflowError(
                        f"root-cause locus {candidate.get('locus')!r} is outside the taxonomy")
            clean[field] = ranked
        elif field == "attribution_ceiling":
            if value not in ATTRIBUTION_LEVELS:
                raise WorkflowError(f"attribution level {value!r} is outside the vocabulary")
            if value == UNREACHABLE_ATTRIBUTION:
                raise WorkflowError(
                    "attribution 'counterfactually_supported' requires replay evidence, "
                    "which this system does not generate; it cannot be set by hand")
            clean[field] = value
        elif field == "decisive":
            if not isinstance(value, bool):
                raise WorkflowError("decisive must be true or false")
            clean[field] = value
        elif field == "opportunity_window":
            window = dict(value or {})
            if not window.get("start_event_id") or not window.get("end_event_id"):
                raise WorkflowError("opportunity_window needs start_event_id and end_event_id")
            clean[field] = window
        elif field == "linked_items":
            links = dict(value or {})
            bad = sorted(set(links) - {"contract_item_ids", "check_ids"})
            if bad:
                raise WorkflowError(f"linked_items accepts contract_item_ids/check_ids, got {bad}")
            clean[field] = {k: list(v or []) for k, v in links.items()}
        elif field == "task_verifier_concern":
            concern = dict(value or {})
            if concern.get("assessment") not in CONCERN_ASSESSMENTS:
                raise WorkflowError(
                    f"task/verifier concern needs an assessment in {list(CONCERN_ASSESSMENTS)}")
            dimension = concern.get("dimension")
            if dimension is not None and dimension not in AUDIT_DIMENSIONS:
                raise WorkflowError(
                    f"task/verifier concern dimension {dimension!r} is outside the "
                    f"§11 audit dimensions")
            clean[field] = concern
        else:  # better_action — a human's proposal, no controlled vocabulary
            clean[field] = value

    corrected_facts = [f for f in FACTUAL_CORRECTION_FIELDS if f in clean]
    if corrected_facts and not evidence_event_ids:
        raise WorkflowError(
            f"correcting {corrected_facts} restates what happened and requires "
            f"evidence_event_ids")
    return clean


def add_feedback(
    store: Store,
    run_id: str,
    *,
    actor: Optional[str],
    mutation_id: str,
    moment_id: str,
    kind: str,
    replacement_group: Optional[str] = None,
    note: Optional[str] = None,
    correction: Optional[dict] = None,
    generated: Optional[dict] = None,
    evidence_event_ids: Optional[list[str]] = None,
    now: Optional[str] = None,
) -> tuple[dict, dict]:
    """Append a Tier-1/Tier-2 feedback record (§4.13) and return ``(record, workflow)``.

    Idempotent: a repeated ``mutation_id`` returns the already-stored record and
    does not append a duplicate (§4.17 "all writes are idempotent or use an
    explicit mutation identifier"). Submitting feedback is an edit, so it also
    opens an untouched review (``unreviewed`` → ``in_progress``) without touching
    the generated moment. Feedback never raises the attribution level (§4.7.2):
    ``agree`` records positive feedback only.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)

    if kind not in FEEDBACK_KINDS:
        raise WorkflowError(f"unknown feedback kind {kind!r}")

    replacement_label = None
    if kind == "quick_relabel":
        if replacement_group not in QUICK_RELABEL_GROUPS:
            raise WorkflowError(
                f"quick_relabel needs a replacement_group in {sorted(QUICK_RELABEL_GROUPS)}"
            )
        replacement_label = QUICK_RELABEL_GROUPS[replacement_group]

    evidence_event_ids = list(evidence_event_ids or [])
    clean_correction = None
    if kind == "structured_correction":
        clean_correction = _validate_correction(correction or {}, evidence_event_ids)

    feedback = read_feedback(store, run_id)
    for existing in feedback:
        if existing.get("mutation_id") == mutation_id:
            return existing, read_workflow(store, run_id)  # idempotent replay

    # The feedback references the workflow revision it was recorded against, then
    # the automatic "editing opens the review" transition is applied and stamped.
    wf = read_workflow(store, run_id)
    record = {
        "mutation_id": mutation_id,
        "moment_id": moment_id,
        "kind": kind,
        "replacement_group": replacement_group if kind == "quick_relabel" else None,
        "replacement_label": replacement_label,
        "note": note,
        "actor": actor,
        "at": now,
        "review_revision": wf["workflow_version"],
        "feedback_writer_version": version.READ_MODEL_VERSION,
    }
    if kind == "structured_correction":
        # A correction is a new annotation revision, never an edit of an earlier
        # one: it records what it replaces (``generated``, shown side by side in
        # the editor) and which correction it supersedes, so the whole chain of
        # human judgement stays inspectable.
        prior = [f for f in feedback
                 if f.get("kind") == "structured_correction" and f.get("moment_id") == moment_id]
        record.update({
            "correction": clean_correction,
            "corrected_fields": sorted(clean_correction),
            "generated": generated or {},
            "evidence_event_ids": evidence_event_ids,
            "correction_revision": len(prior) + 1,
            "supersedes": prior[-1]["mutation_id"] if prior else None,
            "taxonomy_version": version.TAXONOMY_VERSION,
            # §4.13: eligible for the reviewer evaluation set only after
            # adjudication, which no workflow in this package performs yet.
            "adjudication_status": "unadjudicated",
        })
    feedback.append(record)
    store.write_derived(run_id, capture_id, FEEDBACK_RECORD, feedback)

    if _touch_in_progress(wf):
        wf["workflow_version"] += 1
        wf["updated_at"] = now
        wf["revisions"].append({
            "version": wf["workflow_version"],
            "actor": actor,
            "at": now,
            "prior": {"review_progress": "unreviewed"},
            "triggered_by": "feedback",
            "workflow_writer_version": version.READ_MODEL_VERSION,
        })
        store.write_derived(run_id, capture_id, WORKFLOW_RECORD, wf)

    return record, wf
