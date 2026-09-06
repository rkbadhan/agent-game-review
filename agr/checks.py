"""Atomic verifier checks (spec §6.6, Stage A extraction).

The MVP path extracts structured checks that the verifier already emits. Code
inspection and output interpretation (also in the spec) are later work; this
core reads the ``verifier.checks[]`` array the adapter provides.
"""

from __future__ import annotations

from . import version
from .schema import CHECK_SOURCES, CHECK_STATUSES, CHECK_TIMINGS, RunSource, VerifierCheck


class CheckExtractionError(ValueError):
    pass


def extract_checks(doc: dict, run_source: RunSource) -> list[VerifierCheck]:
    verifier = doc.get("verifier") or {}
    raw_checks = verifier.get("checks", [])
    checks: list[VerifierCheck] = []
    for i, raw in enumerate(raw_checks):
        status = raw.get("status", "unknown")
        if status not in CHECK_STATUSES:
            raise CheckExtractionError(f"invalid check status {status!r}")
        source = raw.get("source", "native_structured")
        if source not in CHECK_SOURCES:
            raise CheckExtractionError(f"invalid check source {source!r}")
        timing = raw.get("timing")
        if timing is not None and timing not in CHECK_TIMINGS:
            raise CheckExtractionError(f"invalid check timing {timing!r}")
        checks.append(
            VerifierCheck(
                check_id=raw.get("check_id", f"C{i + 1}"),
                run_id=run_source.run_id,
                source_capture_id=run_source.source_capture_id,
                name=raw.get("name", ""),
                status=status,
                source=source,
                contract_item_ids=list(raw.get("contract_item_ids", [])),
                expected=raw.get("expected"),
                observed=raw.get("observed"),
                source_pointers=list(raw.get("source_pointers", [])),
                derivation_version=version.CHECK_DERIVATION_VERSION,
                timing=timing,
            )
        )
    return checks


def outcome(checks: list[VerifierCheck]) -> dict:
    """Roll checks up into a run outcome summary.

    A run with no checks carries no verifier evidence. That is an explicit
    ingestion state (spec §7.2), reported as UNVERIFIED — never a vacuous
    PASSED, which would rank an unexamined run as a clean pass in triage.
    """
    total = len(checks)
    passed = sum(1 for c in checks if c.status == "passed")
    failed = [c.check_id for c in checks if c.status == "failed"]
    if total == 0:
        status = "UNVERIFIED"
    else:
        status = "PASSED" if not failed else "FAILED"
    return {
        "status": status,
        "passed": passed,
        "total": total,
        "failed_checks": failed,
    }
