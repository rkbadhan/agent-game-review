"""Eval Lessons and improvement experiments (spec §3.8, §4.11, §6.10, §13).

An Eval Lesson is the durable output of an *accepted* review diagnosis: the
smallest reusable improvement a reviewed finding supports (spec §3.8). It exists
only when a model-enriched moment recommends one (``eval_lesson_recommended``,
spec §4.11) — the deterministic core authors no verdict and therefore proposes no
lesson.

Like the human review workflow (:mod:`agr.workflow`), a lesson is *mutable*,
versioned human review state written as a derived record beside the immutable
source — never mutating it. One record file per capture holds an append-only list
of lessons (a capture can accept several moments):

- ``lessons.json`` — the run's Eval Lessons, each carrying its §6.10 body, a
  ``proposed | approved_for_test | validated | rejected | superseded`` status
  with an append-only ``revisions`` log, and — once an approved lesson is turned
  into an experiment — a §13.2 ``experiment_proposal``.

Status transitions are optimistic on ``lesson_version`` (spec §4.17): a stale
write is refused so a concurrent editor never silently clobbers.

Two normative constraints the spec makes on this module:

- **Approval never edits a prompt, tool, scaffold, harness, task, or verifier**
  (spec §4.11). Approving advances the lesson to ``approved_for_test`` — nothing
  else. Executing the intervention is post-MVP (spec §20) and lives nowhere here.
- **The proposal generator does not invent numeric thresholds as facts**
  (spec §13.2). A generated experiment proposal carries measures and guardrails
  but no success numbers; the experiment owner sets those.

Pure stdlib — no third-party imports, no model calls. The lesson body is a
deterministic projection of an already-enriched moment; the human drives the
lifecycle.
"""

from __future__ import annotations

import time
from typing import Optional

from . import taxonomy, version
from .store import Store

LESSONS_RECORD = "lessons.json"

# §6.10 — the lesson promotion lifecycle. Exactly these tokens, in this vocabulary.
LESSON_STATUSES = ("proposed", "approved_for_test", "validated", "rejected", "superseded")

# Which transitions a human may make. ``rejected`` and ``superseded`` are terminal.
# ``validated`` is reachable only from ``approved_for_test`` and, in this package,
# is a human-set status with no automated holdout behind it — the controlled
# experiment that would earn it is post-MVP (spec §20), the same way replay
# evidence is gated off in :mod:`agr.workflow`.
LESSON_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"approved_for_test", "rejected"},
    "approved_for_test": {"validated", "rejected", "superseded"},
    "validated": {"superseded"},
    "rejected": set(),
    "superseded": set(),
}

# The lesson-body fields a human may set via "Edit lesson" (spec §4.11). These are
# a reviewer's proposal and carry no controlled vocabulary — except the systemic
# intervention *layer*, which names where a fix would land and so is constrained
# to the root-cause loci (:mod:`agr.taxonomy`), exactly as the model's own
# ``root_cause_candidates`` are filtered in :func:`agr.reviewer.run_reviewer`.
EDITABLE_FIELDS = (
    "observed_behaviour", "better_local_action", "systemic_intervention",
    "generalization_boundary", "generalization_exclusion", "possible_side_effects",
    "regression_slice",
)

# §13.2 evaluation-design defaults. The design shape is fixed; the numbers are not.
_EVAL_DESIGN = {"matched_baseline": True, "repeat_seeds": 3, "private_holdout": True}


class LessonError(ValueError):
    """A malformed or disallowed lesson write (unknown status, illegal transition,
    a moment that recommends no lesson, an out-of-taxonomy intervention layer)."""


class VersionConflict(Exception):
    """An optimistic lesson write lost to a concurrent edit (spec §4.17).

    Carries the current persisted lesson so the caller (and the HTTP 409) can show
    the newer state without discarding the user's unsaved edit — the same contract
    as :class:`agr.workflow.VersionConflict`.
    """

    def __init__(self, expected: int, actual: int, current: dict):
        super().__init__(f"stale lesson write: base_version {expected} != current {actual}")
        self.expected = expected
        self.actual = actual
        self.current = current


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_capture(store: Store, run_id: str) -> str:
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        raise LessonError(f"run {run_id!r} has no captures in the store")
    return capture_id


# --- lesson body -------------------------------------------------------------


def default_lesson(moment: dict, *, reviewer_key: Optional[str] = None) -> dict:
    """Project a recommending moment into the §6.10 Eval Lesson body.

    Deterministic: it restates the moment's accepted diagnosis (observed
    behaviour, better local action, and the top root-cause candidate as the
    systemic intervention). The generalization boundary/exclusion, side effects,
    and regression slice are the reviewer's to author (spec §4.11) and start
    empty — they are filled through "Edit lesson", not invented here.
    """
    top = (moment.get("root_cause_candidates") or [{}])[0]
    behaviour = (moment.get("behaviour_tags") or [None])[0] or moment.get("taxonomy_verdict")
    return {
        "lesson_id": "lesson_" + str(moment.get("moment_id")),
        "source_moments": [moment.get("moment_id")],
        "observed_behaviour": moment.get("summary") or moment.get("rendered_statement") or "",
        "evidence_event_ids": list(moment.get("anchor_event_ids") or []),
        "better_local_action": moment.get("better_action") or "",
        "systemic_intervention": {
            "layer": top.get("locus"),
            "proposal": top.get("rationale") or "",
        },
        "generalization_boundary": "",
        "generalization_exclusion": "",
        "possible_side_effects": [],
        "regression_slice": "",
        "status": "proposed",
        # Carried for the §13.2 experiment observation so the proposal generator
        # needs no second read of the review.
        "behaviour": behaviour,
        "affected_checks": list(moment.get("affected_checks") or []),
        "reviewer_key": reviewer_key,
        "experiment_proposal": None,
        "lesson_version": 0,
        "created_at": None,
        "updated_at": None,
        "revisions": [],
        "lesson_writer_version": version.MODEL_REVIEWER_VERSION,
    }


# --- reads -------------------------------------------------------------------


def read_lessons(store: Store, run_id: str) -> list[dict]:
    """Every Eval Lesson recorded for the run, in creation order."""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        return []
    if store.has_derived(run_id, capture_id, LESSONS_RECORD):
        return store.read_derived(run_id, capture_id, LESSONS_RECORD)
    return []


def _find(lessons: list[dict], lesson_id: str) -> Optional[dict]:
    return next((ln for ln in lessons if ln.get("lesson_id") == lesson_id), None)


# --- writes ------------------------------------------------------------------


def create_lesson(
    store: Store,
    run_id: str,
    *,
    moment: dict,
    actor: Optional[str],
    mutation_id: str,
    reviewer_key: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """Create the Eval Lesson for an accepted moment (spec §4.11, §13.1).

    Refuses a moment that does not recommend a lesson — a lesson exists only when
    a reviewed finding supports one (spec §4.11). Idempotent per moment: a repeated
    call (same ``moment_id``) returns the existing lesson rather than a duplicate,
    which is what §4.17 requires of a retried write.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)
    if not moment.get("eval_lesson_recommended"):
        raise LessonError(
            "this moment recommends no Eval Lesson; a lesson exists only when a "
            "reviewed finding supports one (§4.11)")

    lessons = read_lessons(store, run_id)
    lesson_id = "lesson_" + str(moment.get("moment_id"))
    existing = _find(lessons, lesson_id)
    if existing is not None:
        return existing  # idempotent: one lesson per accepted moment

    lesson = default_lesson(moment, reviewer_key=reviewer_key)
    lesson["created_at"] = now
    lesson["updated_at"] = now
    lesson["revisions"].append({
        "version": 0, "actor": actor, "at": now,
        "prior_status": None, "status": "proposed",
        "mutation_id": mutation_id,
        "lesson_writer_version": version.MODEL_REVIEWER_VERSION,
    })
    lessons.append(lesson)
    store.write_derived(run_id, capture_id, LESSONS_RECORD, lessons)
    return lesson


def _validate_edits(edits: dict) -> dict:
    """Validate an "Edit lesson" payload (spec §4.11). Free-text proposals pass
    through; the intervention layer is held to the root-cause taxonomy. Malformed
    shapes (a string where an object or list belongs) are refused with
    :class:`LessonError` rather than crashing the write path."""
    unknown = sorted(set(edits) - set(EDITABLE_FIELDS))
    if unknown:
        raise LessonError(f"unknown lesson field(s) {unknown}")
    clean = dict(edits)
    for field, value in clean.items():
        if field in ("possible_side_effects", "systemic_intervention"):
            continue
        if value is not None and not isinstance(value, str):
            raise LessonError(f"lesson field {field!r} must be a string")
    intervention = clean.get("systemic_intervention")
    if intervention is not None:
        if not isinstance(intervention, dict):
            raise LessonError(
                "systemic_intervention must be an object {layer, proposal}")
        layer = intervention.get("layer")
        if layer is not None and layer not in taxonomy.ROOT_CAUSE_LOCI:
            raise LessonError(f"intervention layer {layer!r} is outside the taxonomy")
        proposal = intervention.get("proposal")
        if proposal is not None and not isinstance(proposal, str):
            raise LessonError("intervention proposal must be a string")
    if "possible_side_effects" in clean:
        effects = clean["possible_side_effects"] or []
        if (not isinstance(effects, (list, tuple))
                or not all(isinstance(e, str) for e in effects)):
            raise LessonError("possible_side_effects must be a list of strings")
        clean["possible_side_effects"] = list(effects)
    return clean


def _bump(lesson: dict, *, actor: Optional[str], now: str,
          edited_fields=(), prior_status: Optional[str] = None) -> None:
    """Advance the optimistic version and append to the revisions log.

    Every mutation of a persisted lesson goes through here — status transitions,
    reviewer edits, and experiment-proposal writes alike — so the append-only
    revision history is a complete audit trail (spec §4.17) and a concurrent
    writer can always detect that the lesson moved under it."""
    lesson["lesson_version"] += 1
    lesson["updated_at"] = now
    lesson["revisions"].append({
        "version": lesson["lesson_version"], "actor": actor, "at": now,
        "prior_status": prior_status if prior_status is not None else lesson["status"],
        "status": lesson["status"],
        "edited_fields": sorted(edited_fields),
        "lesson_writer_version": version.MODEL_REVIEWER_VERSION,
    })


def set_lesson_status(
    store: Store,
    run_id: str,
    lesson_id: str,
    *,
    base_version: int,
    actor: Optional[str],
    status: Optional[str] = None,
    edits: Optional[dict] = None,
    now: Optional[str] = None,
) -> dict:
    """Advance a lesson's status and/or apply edits (spec §4.11, §6.10).

    ``status`` moves the lesson along the §6.10 lifecycle; an illegal jump is
    refused. ``edits`` applies the reviewer's authored body fields. At least one
    of the two must be given. The write is optimistic on ``lesson_version``:
    ``base_version`` must equal the current version or :class:`VersionConflict` is
    raised carrying the live lesson.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)
    if status is None and not edits:
        raise LessonError("set_lesson_status needs a status transition or edits")
    if status is not None and status not in LESSON_STATUSES:
        raise LessonError(f"unknown lesson status {status!r}")

    lessons = read_lessons(store, run_id)
    lesson = _find(lessons, lesson_id)
    if lesson is None:
        raise LessonError(f"lesson {lesson_id!r} not found on run {run_id!r}")
    if base_version != lesson["lesson_version"]:
        raise VersionConflict(base_version, lesson["lesson_version"], lesson)

    prior_status = lesson["status"]
    if status is not None and status != prior_status:
        if status not in LESSON_TRANSITIONS.get(prior_status, set()):
            raise LessonError(
                f"cannot move a lesson from {prior_status!r} to {status!r}")
        lesson["status"] = status

    if edits:
        lesson.update(_validate_edits(edits))

    _bump(lesson, actor=actor, now=now, edited_fields=edits or (),
          prior_status=prior_status)
    store.write_derived(run_id, capture_id, LESSONS_RECORD, lessons)
    return lesson


# --- §13.2 experiment proposal -----------------------------------------------


def _experiment_proposal(lesson: dict) -> dict:
    """Build the §13.2 improvement-experiment proposal from an approved lesson.

    A pure projection. ``observation`` reports only what this single run proves —
    the accepted behaviour and the failures it touched; the cross-run opportunity
    denominator is left ``None`` because aggregating it across runs is §12.4, which
    this package does not compute yet (an unmeasured count is not zero). No numeric
    success threshold is emitted — the experiment owner sets those (spec §13.2).
    """
    intervention = lesson.get("systemic_intervention") or {}
    return {
        "experiment_id": "experiment_" + str(lesson.get("lesson_id")),
        "lesson_id": lesson.get("lesson_id"),
        "observation": {
            "behaviour": lesson.get("behaviour"),
            "opportunities": None,  # cross-run aggregation is §12.4 (not built)
            "observed": len(lesson.get("source_moments") or []),
            "affected_failures": len(lesson.get("affected_checks") or []),
            "scope": "single_run",
        },
        "hypothesis": intervention.get("proposal") or "",
        "proposed_change": {
            "layer": intervention.get("layer"),
            "description": intervention.get("proposal") or "",
        },
        "evaluation_design": {
            "visible_slice": lesson.get("regression_slice") or "",
            **_EVAL_DESIGN,
        },
        "primary_measure": lesson.get("behaviour"),
        "guardrails": ["pass_rate", "median_tokens"],
        # No numeric thresholds: the proposal generator does not invent them (§13.2).
        "status": "proposed",
        "approved_by": None,
        "approved_at": None,
    }


def propose_experiment(
    store: Store,
    run_id: str,
    lesson_id: str,
    *,
    actor: Optional[str],
    now: Optional[str] = None,
) -> dict:
    """Generate a §13.2 experiment proposal for an ``approved_for_test`` lesson.

    Only an approved lesson may seed an experiment — a proposed or rejected lesson
    is not an accepted diagnosis. Idempotent: it does not regenerate over an
    already-approved proposal.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)
    lessons = read_lessons(store, run_id)
    lesson = _find(lessons, lesson_id)
    if lesson is None:
        raise LessonError(f"lesson {lesson_id!r} not found on run {run_id!r}")
    if lesson["status"] != "approved_for_test":
        raise LessonError(
            f"only an approved_for_test lesson can seed an experiment; "
            f"lesson {lesson_id!r} is {lesson['status']!r}")

    existing = lesson.get("experiment_proposal")
    if existing and existing.get("status") == "approved":
        return lesson  # idempotent: never overwrite a human-approved proposal
    lesson["experiment_proposal"] = _experiment_proposal(lesson)
    _bump(lesson, actor=actor, now=now, edited_fields=["experiment_proposal"])
    store.write_derived(run_id, capture_id, LESSONS_RECORD, lessons)
    return lesson


def approve_experiment(
    store: Store,
    run_id: str,
    lesson_id: str,
    *,
    actor: Optional[str],
    now: Optional[str] = None,
) -> dict:
    """Record human approval of a generated experiment proposal (spec §13.2).

    This is the Milestone 5 accept criterion — "one accepted lesson produces a
    human-approved experiment proposal" — made provable from storage: it advances
    the proposal's own ``proposed → approved`` flag, stamped with the approver.
    Idempotent: an already-approved proposal keeps its first approver and
    timestamp rather than being re-stamped by a repeated click.
    """
    now = now or _now()
    capture_id = _require_capture(store, run_id)
    lessons = read_lessons(store, run_id)
    lesson = _find(lessons, lesson_id)
    if lesson is None:
        raise LessonError(f"lesson {lesson_id!r} not found on run {run_id!r}")
    proposal = lesson.get("experiment_proposal")
    if not proposal:
        raise LessonError(
            f"lesson {lesson_id!r} has no experiment proposal to approve")
    if proposal.get("status") == "approved":
        return lesson  # idempotent: keep the first approver and timestamp
    proposal["status"] = "approved"
    proposal["approved_by"] = actor
    proposal["approved_at"] = now
    _bump(lesson, actor=actor, now=now, edited_fields=["experiment_proposal"])
    store.write_derived(run_id, capture_id, LESSONS_RECORD, lessons)
    return lesson
