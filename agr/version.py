"""Version stamps for every deterministic derivation.

The spec requires that reprocessing with a new derivation, detector, taxonomy,
or validator version creates new derived records without mutating the source
(spec §7.3). Every derived record embeds the version that produced it so that
provenance is explicit and reprocessing is auditable.
"""

# Adapter provenance is per-source, not global: each adapter stamps its own
# version into the document it emits (see agr/ingest_harbor.py and
# agr/ingest_pi.py). This default applies only when a raw ATIF document is
# ingested directly with no adapter in the path (``agr ingest``, fixtures).
RAW_ATIF_IMPORT_VERSION = "atif-import-0.1"

# The package version, stamped into eval manifests so a result record is
# auditable against the release that produced it (never null).
AGR_VERSION = "0.1.0"

DERIVATION_VERSION = "event-map-0.1"
CHECK_DERIVATION_VERSION = "check-extract-0.1"
SLICE_DERIVATION_VERSION = "evidence-slice-0.1"
DETECTOR_VERSION = "detectors-0.1"
CONTRACT_BUILDER_VERSION = "contract-builder-0.1"
CONTRACT_OBSERVATION_VERSION = "contract-status-1.0"
PHASE_SEGMENTATION_VERSION = "phase-segment-0.1"
TAXONOMY_VERSION = "0.1"

# The read layer (§4.5 forensic view, Milestone 1 evidence browser) is a
# projection over persisted records — it derives nothing new, but it is
# versioned so a UI can pin the shape of the views it consumes.
READ_MODEL_VERSION = "read-model-0.2"

# The reviewer gold set (§15.1) and the evaluation harness (§15.3). The spec
# sequences both ahead of the model reviewer (M4, principle #11); versioning
# them means a gold record or an eval report is auditable against the schema /
# metric definitions that produced it, just like every deterministic derivation.
GOLD_SCHEMA_VERSION = "gold-0.1"
# 0.2: semantic metric names corrected (affected_check_overlap_rate,
# attribution_ceiling_respected_rate) — derivations under different names are
# not comparable.
REVIEWER_EVAL_VERSION = "reviewer-eval-0.2"

# The deterministic reviewer envelope (spec §8.8 fact validation, §8.9 attribution
# gate, §8.10 moment selection). This is Milestone 4's safety envelope built
# *ahead* of the model reviewer (Stage F): it re-validates every structured fact,
# assigns attribution language no stronger than the evidence slice licenses, and
# selects/de-duplicates the final cards — all deterministically, with no model
# call. Every ReviewMoment embeds this version. Stage F plugs in behind it.
REVIEWER_VERSION = "reviewer-det-0.2"

# Stage F — the model reviewer (spec §8.7). The reviewer runs behind the
# deterministic envelope: its structured facts are recomputed (Stage G), its
# attribution language capped (Stage H), and its cards selected (Stage I). The
# version stamps model-enriched review moments so a verdict is auditable against
# the prompt/schema that produced it.
MODEL_REVIEWER_VERSION = "reviewer-model-0.2"

# Redaction and untrusted-content isolation applied before any model-facing text
# (spec §7.4, §16.1). The redaction map records what was removed and why, stamped
# with this version so the model-visible representation is reproducible.
# 0.2: dictionary keys are redacted too (data-map keys can be content).
REDACTION_VERSION = "redaction-0.2"

# Task & Verifier Audit (spec §11). The audit is deterministic: it maps
# already-derived signals — task-contract items and warnings, verifier-check
# outcomes, opportunities, and recovery episodes — onto the ten categorical
# dimensions. Every AuditFinding embeds this version so an assessment is
# auditable against the mapping that produced it, like every other derivation.
AUDIT_VERSION = "audit-0.1"
