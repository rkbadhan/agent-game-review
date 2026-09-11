"""Harbor ATIF ingestion (spec §7, Stage A of §8).

Note on ATIF: the specification names Harbor's Agent Trajectory Interchange
Format as the MVP source. There is no public ATIF library to depend on, so
this adapter defines a concrete, minimal ATIF-shaped JSON contract (see
``archive/synthetic/fixtures/`` in the repo) and imports it deterministically. The
exact ``atif_version`` is preserved on every capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import version
from .adapter import apply_capability_defaults
from .events import KIND_TO_EVENT
from .schema import CAPABILITY_LEVELS, CAPTURE_COMPLETENESS, CapabilityProfile, RunSource
from .store import InvalidRunId, Store, capture_id_for, source_hash, validate_run_id


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    run_source: RunSource
    capabilities: CapabilityProfile
    idempotent: bool


# The default capability profile and the "missing -> unavailable" rule live in
# agr/adapter.py so adapters and ingest share one definition (see
# apply_capability_defaults, used in _capability_profile below).


def _validate(doc: dict) -> None:
    """Minimal ingestion validation (spec §7.2)."""
    if not isinstance(doc, dict):
        raise IngestError("ATIF document must be a JSON object")
    if "atif_version" not in doc:
        raise IngestError("missing atif_version")
    run = doc.get("run")
    if not isinstance(run, dict) or not run.get("logical_run_id"):
        raise IngestError("missing run.logical_run_id")
    try:
        validate_run_id(run["logical_run_id"])
    except InvalidRunId as exc:
        raise IngestError(str(exc)) from exc
    if not run.get("task_id"):
        raise IngestError("missing run.task_id")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise IngestError("missing or empty steps[]")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise IngestError(f"step {i} must be a JSON object")
        sid = step.get("step_id")
        if not sid:
            raise IngestError(f"step {i} missing step_id")
        if sid in seen:
            raise IngestError(f"duplicate step_id {sid!r}")
        seen.add(sid)
        kind = step.get("kind")
        if not kind:
            raise IngestError(f"step {sid!r} missing kind")
        # The step-kind vocabulary is the set of kinds the event timeline can
        # map (see agr/events.py:KIND_TO_EVENT and docs/atif-schema.json). An
        # unknown kind fails here at the contract boundary rather than deeper in
        # event derivation, so adapter authors get an actionable message.
        if kind not in KIND_TO_EVENT:
            raise IngestError(
                f"step {sid!r} has unknown kind {kind!r}; "
                f"expected one of {sorted(KIND_TO_EVENT)}"
            )
    completeness = doc.get("capture_completeness", "complete")
    if completeness not in CAPTURE_COMPLETENESS:
        raise IngestError(f"invalid capture_completeness {completeness!r}")
    caps = doc.get("capabilities", {})
    if not isinstance(caps, dict):
        raise IngestError("capabilities must be a JSON object")
    for cap, level in caps.items():
        if level not in CAPABILITY_LEVELS:
            raise IngestError(f"invalid capability level {level!r} for {cap!r}")


def _capability_profile(doc: dict, run_id: str, capture_id: str) -> CapabilityProfile:
    caps = apply_capability_defaults(doc.get("capabilities"))
    return CapabilityProfile(run_id=run_id, source_capture_id=capture_id, capabilities=caps)


def ingest(doc: dict, store: Store, adapter_version: str | None = None) -> IngestResult:
    """Ingest one ATIF document into the immutable store.

    Idempotent: re-ingesting the same source hash with the same adapter version
    is a no-op on the source (spec §7.3). A different hash for the same logical
    run creates a new, linked capture revision.

    Adapter provenance is per-source: an explicit ``adapter_version`` argument
    wins, else the document's own ``adapter_version`` stamp (written by the
    adapter that produced it), else the raw-ATIF import default for documents
    ingested with no adapter in the path.
    """
    _validate(doc)
    resolved_adapter_version = (
        adapter_version
        or doc.get("adapter_version")
        or version.RAW_ATIF_IMPORT_VERSION
    )

    run = doc["run"]
    run_id = run["logical_run_id"]
    hash_str = source_hash(doc)
    capture_id = capture_id_for(hash_str)
    completeness = doc.get("capture_completeness", "complete")

    entry = store.register_capture(run_id, capture_id, hash_str, resolved_adapter_version, completeness)
    idempotent = bool(entry.get("idempotent"))

    # Source is immutable; write only stores it the first time.
    store.write_source(run_id, capture_id, doc)

    run_source = RunSource(
        run_id=run_id,
        source_capture_id=capture_id,
        capture_revision=entry["capture_revision"],
        source_hash=hash_str,
        # No default harness label: a document that does not declare its origin
        # is recorded as unknown, never assumed to be from any one harness.
        source_type=doc.get("source_type", "unknown"),
        source_schema=f"ATIF-v{doc['atif_version']}",
        capture_completeness=completeness,
        task_id=run["task_id"],
        supersedes_source_capture_id=entry.get("supersedes_source_capture_id"),
        model=run.get("model"),
        agent=run.get("agent"),
        harness_version=run.get("harness_version"),
        task_version=run.get("task_version"),
        verifier_version=run.get("verifier_version"),
        seed=run.get("seed"),
        started_at=run.get("started_at"),
        finished_at=run.get("finished_at"),
        adapter_version=resolved_adapter_version,
        sweep_id=run.get("sweep_id"),
        configuration_id=run.get("configuration_id"),
        environment_image_digest=run.get("environment_image_digest"),
        task_parameters=run.get("task_parameters"),
        # F5: the adapter's captured cost/usage (e.g. Claude's
        # total_cost_usd/usage) travels through unchanged; absent in the
        # source, it stays None rather than becoming a misleading 0.
        cost=run.get("total_cost_usd"),
        tokens=run.get("usage"),
        cwd=run.get("cwd"),
    )
    capabilities = _capability_profile(doc, run_id, capture_id)

    store.write_derived(run_id, capture_id, "run_source.json", run_source.to_dict())
    store.write_derived(run_id, capture_id, "capabilities.json", capabilities.to_dict())

    return IngestResult(run_source=run_source, capabilities=capabilities, idempotent=idempotent)
