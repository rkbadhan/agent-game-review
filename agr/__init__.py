"""Agent Game Review — deterministic core.

This package implements the deterministic (no-LLM) portion of the Agent Game
Review specification: source ingestion, immutable capture records, capability
profiling, derived event timelines, atomic verifier checks, evidence slices,
and deterministic candidate detectors.

Everything here corresponds to Stages A, C1, D, and E of the analysis pipeline
(spec §8) plus the Milestone 1 source/evidence foundation (spec §20). No model
calls are made. Model-assisted stages (contract confirmation, local phase
analysis, global reviewer, lesson generation) are intentionally not in this
package.
"""

from .version import (
    RAW_ATIF_IMPORT_VERSION,
    DERIVATION_VERSION,
    CHECK_DERIVATION_VERSION,
    SLICE_DERIVATION_VERSION,
    CONTRACT_BUILDER_VERSION,
    CONTRACT_OBSERVATION_VERSION,
    TAXONOMY_VERSION,
)

__all__ = [
    "RAW_ATIF_IMPORT_VERSION",
    "DERIVATION_VERSION",
    "CHECK_DERIVATION_VERSION",
    "SLICE_DERIVATION_VERSION",
    "CONTRACT_BUILDER_VERSION",
    "CONTRACT_OBSERVATION_VERSION",
    "TAXONOMY_VERSION",
]
