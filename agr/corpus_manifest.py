"""Corpus manifest — reproducible provenance and counting-stage definitions (AGR-01).

The review and the proposed tasks used different moment counts because nothing
pinned what a "run" was being counted *of*. This module is the single place
that resolves a Harbor eval corpus root (e.g. ``eval-runs/``) into:

* source locations and checksums for every discovered trial,
* the logical run id and capture identity Harbor ingestion would assign it,
* eligibility/exclusion with a reason, and
* the reviewed commit and derivation versions the manifest was built against.

It deliberately does not re-implement discovery or identity: trial discovery
and exclusion reuse :func:`agr.ingest_harbor.iter_trials_detailed`, and run/
capture identity reuse :func:`agr.ingest.ingest` against a throwaway store —
the same functions ``agr ingest-harbor`` runs in production. A manifest can
therefore never drift from what a real batch ingest does; it stops one stage
short of that (no detectors, no reviewer) so it stays a provenance/counting
report rather than a review.

Counting-stage vocabulary (the terms every later stage's counts must
reconcile against — see :data:`COUNTING_STAGES` for the one-line definitions
callers can surface verbatim):

``discovered_trials``
    Every directory :func:`agr.ingest_harbor.iter_trials_detailed` found while
    walking the corpus root, whether or not it turned out to be reviewable.
``eligible_trials`` / ``excluded_trials``
    The discovered trials split by whether they carried a reviewable
    trajectory and converted without error. Every excluded trial carries a
    reason; nothing disappears silently.
``ingested_captures``
    Capture registrations this build actually created (a fresh source hash +
    adapter version pair). Re-ingesting the same trial is idempotent and does
    not add to this count.
``logical_runs``
    Distinct ``run.logical_run_id`` values among the eligible trials — the
    execution identity a capture revision never changes.
``active_captures``
    One per logical run: the capture the run's index currently ends on
    (``Store.latest_capture_id``). Equal to ``logical_runs`` here because this
    manifest ingests each trial exactly once into a fresh store; a store that
    has absorbed re-ingested revisions can have fewer active captures than
    total ingested captures.
``generated_candidates`` / ``validated_candidates`` / ``selected_moments`` /
``displayed_moments``
    Review-time counts, not corpus counts: how many detector candidates a run
    produced, how many survived fact validation, how many the reviewer
    selected, and how many a UI actually renders. This module only names them
    so every later stage (AGR-08, AGR-15, AGR-17) reports its counts under the
    same labels instead of each inventing its own; computing them requires
    running the reviewer, which is out of scope here.

Pure stdlib. The only non-deterministic field in the output is
``generated_at``; everything else is a function of the corpus contents and the
two commit references passed in.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import version
from .adapter import get_adapter
from .ingest import ingest
from .ingest_harbor import iter_trials_detailed
from .store import Store

MANIFEST_VERSION = version.CORPUS_MANIFEST_VERSION

# One-line definitions, kept next to the code that implements them rather than
# only in a doc that can drift. Surfaced verbatim in every manifest's
# ``counting_definitions`` so a reader never has to cross-reference this
# docstring to know what a number means.
COUNTING_STAGES = {
    "discovered_trials": "Every directory found while walking the corpus root, "
        "whether or not it turned out to be a reviewable trial.",
    "eligible_trials": "Discovered trials that carried a reviewable trajectory "
        "and converted to an ATIF document without error.",
    "excluded_trials": "Discovered trials that were not ingested, each with an "
        "explicit reason (no reviewable trajectory, empty job directory, "
        "conversion error).",
    "ingested_captures": "Capture registrations this build newly created "
        "(fresh source hash + adapter version). Re-ingesting the same trial "
        "is idempotent and does not add to this count.",
    "logical_runs": "Distinct run.logical_run_id values among the eligible "
        "trials — the execution identity a capture revision never changes.",
    "active_captures": "One per logical run: the capture each run's index "
        "currently ends on (the capture later stages read by default).",
    "generated_candidates": "Detector candidates produced for a run, before "
        "fact validation — a review-time count, not a corpus count.",
    "validated_candidates": "Generated candidates that survived fact "
        "validation against the retained evidence.",
    "selected_moments": "Validated candidates the reviewer selected/"
        "deduplicated into the final card set.",
    "displayed_moments": "Selected moments an actual UI or report rendered — "
        "may be fewer than selected when a surface caps what it shows.",
}


def _sha256_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _load_ledger(path: Path) -> dict[str, list[dict]]:
    """``job_name -> ledger rows``, so a trial's provenance can carry its
    recorded phase/agent/model/outcomes/tokens alongside the ingested identity.
    """
    by_job: dict[str, list[dict]] = {}
    if not path.exists():
        return by_job
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_job.setdefault(row.get("job_name"), []).append(row)
    return by_job


def _git_commit(cwd: Optional[str] = None) -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                              capture_output=True, text=True, timeout=5, check=True)
        return out.stdout.strip() or None
    except Exception:
        return None


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _trial_record(trial_path: Path, reason: Optional[str], source_root: Path,
                   ledger_by_job: dict[str, list[dict]], store: Store) -> dict:
    job_name = trial_path.parent.name
    config = _read_json(trial_path / "config.json") or {}
    traj_path = trial_path / "agent" / "trajectory.json"
    if not traj_path.exists():
        traj_path = trial_path / "trajectory.json"

    record: dict = {
        "job": job_name,
        "trial_dir": _relative(trial_path, source_root),
        "eligible": reason is None,
        "exclusion_reason": reason,
        "configured_task": (config.get("task") or {}).get("name"),
        "configured_agent": (config.get("agent") or {}).get("name"),
        "configured_model": (config.get("agent") or {}).get("model_name"),
        "job_id": config.get("job_id"),
        "file_checksums": {
            "trajectory.json": _sha256_file(traj_path),
            "result.json": _sha256_file(trial_path / "result.json"),
        },
        "ledger": ledger_by_job.get(job_name, []),
    }
    if reason is not None:
        return record

    adapter = get_adapter("harbor")
    try:
        converted = adapter.convert(str(trial_path))
    except ValueError as exc:
        record["eligible"] = False
        record["exclusion_reason"] = f"conversion_error: {exc}"
        return record

    record["adapter_warnings"] = list(converted.warnings)
    result = ingest(converted.doc, store)
    rs = result.run_source
    record.update({
        "run_id": rs.run_id,
        "task_id": rs.task_id,
        "model": rs.model,
        "agent": rs.agent,
        "capture_id": rs.source_capture_id,
        "capture_revision": rs.capture_revision,
        "source_hash": rs.source_hash,
        "capture_completeness": rs.capture_completeness,
        "adapter_version": rs.adapter_version,
        "sweep_id": rs.sweep_id,
        "configuration_id": rs.configuration_id,
        "idempotent_capture": result.idempotent,
    })
    return record


_DERIVATION_VERSION_FIELDS = (
    "AGR_VERSION", "DERIVATION_VERSION", "CHECK_DERIVATION_VERSION",
    "SLICE_DERIVATION_VERSION", "DETECTOR_VERSION", "CONTRACT_BUILDER_VERSION",
    "CONTRACT_OBSERVATION_VERSION", "PHASE_SEGMENTATION_VERSION",
    "TAXONOMY_VERSION", "READ_MODEL_VERSION", "GOLD_SCHEMA_VERSION",
    "REVIEWER_EVAL_VERSION", "REVIEWER_VERSION", "MODEL_REVIEWER_VERSION",
    "REDACTION_VERSION", "AUDIT_VERSION", "ERROR_SIGNATURE_VERSION",
    "RECOVERY_VERSION", "CORPUS_MANIFEST_VERSION",
)


def build_manifest(source_root: str, *, reviewed_commit: str,
                    generated_at_commit: Optional[str] = None,
                    ledger_path: Optional[str] = None) -> dict:
    """Build a reproducible corpus manifest over a Harbor eval corpus root.

    ``reviewed_commit`` names the commit the pinned review was performed
    against (provenance, not a checkout this function performs).
    ``generated_at_commit`` defaults to the current repository's ``HEAD`` —
    the commit whose adapter/ingest logic actually produced this manifest,
    which can be later than ``reviewed_commit``.
    """
    root = Path(source_root)
    discovered = iter_trials_detailed(str(root))
    ledger_by_job = _load_ledger(Path(ledger_path) if ledger_path else root / "ledger.jsonl")

    with tempfile.TemporaryDirectory(prefix="agr-corpus-manifest-") as tmp:
        store = Store(tmp)
        trials = [_trial_record(path, reason, root, ledger_by_job, store)
                  for path, reason in discovered]

    eligible = [t for t in trials if t["eligible"]]
    excluded = [t for t in trials if not t["eligible"]]
    run_ids = sorted({t["run_id"] for t in eligible if t.get("run_id")})
    new_captures = sum(1 for t in eligible if not t.get("idempotent_capture"))

    exclusions_by_reason: dict[str, int] = {}
    for t in excluded:
        exclusions_by_reason[t["exclusion_reason"]] = exclusions_by_reason.get(t["exclusion_reason"], 0) + 1

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generated_at_commit": generated_at_commit or _git_commit(),
        "reviewed_commit": reviewed_commit,
        "source_root": str(root),
        "derivation_versions": {name: getattr(version, name) for name in _DERIVATION_VERSION_FIELDS},
        "counting_definitions": COUNTING_STAGES,
        "counts": {
            "discovered_trials": len(discovered),
            "eligible_trials": len(eligible),
            "excluded_trials": len(excluded),
            "logical_runs": len(run_ids),
            "ingested_captures": new_captures,
            "active_captures": len(run_ids),
        },
        "exclusions_by_reason": exclusions_by_reason,
        "logical_run_ids": run_ids,
        "trials": trials,
    }


def write_manifest(manifest: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
