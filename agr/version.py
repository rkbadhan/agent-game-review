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
# 0.2 (AGR-02): checks carry scope/sequence/superseded_by/stale_reason and an
# effective_status distinct from the raw historical status — reconciled by
# agr.checks.reconcile_checks before agr.checks.outcome rolls them up.
# 0.3 (PR #56 review): reconcile_checks' mutation-staleness test no longer
# marks a passed check stale after a documentation/text-note edit (it shares
# agr._util.is_state_changing_action_related_to with AGR-04's recovery
# credit fix) — a docs/README/.md edit cannot invalidate a check as evidence
# of final state.
CHECK_DERIVATION_VERSION = "check-extract-0.3"
SLICE_DERIVATION_VERSION = "evidence-slice-0.1"
# 0.2 (AGR-08): UnresolvedRequirementAtSubmission and
# TerminalFailureWithFailingChecks each emit ONE aggregate candidate per
# eligible run/capture carrying every failing check, not one candidate per
# check — a run with N failing checks previously produced N near-identical
# terminal-statement candidates that crowded out other findings.
DETECTOR_VERSION = "detectors-0.2"
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
# 0.2 (AGR-01): trajectories carry annotation provenance — label_source
# (human vs. model_draft, so a draft can never silently stand in for
# independent human gold) and label_batch/frozen (a batch is usable for
# tuning only once frozen). Existing 0.1 records still load: the new fields
# default to human/unbatched/unfrozen.
GOLD_SCHEMA_VERSION = "gold-0.2"
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

# Error signature normaliser (item 28, 2026-09-08 fleet-view gaps). A
# deterministic, stdlib-only grouping key for otherwise-noisy error text —
# line numbers, addresses, paths, timestamps, UUIDs, and quoted literals are
# stripped so the fleet view can group "the same error" across many runs.
# 0.2 (AGR-07): diagnostic selection now prefers structured exception info,
# then a marker-recognised diagnostic line anywhere in the text (not just the
# first line — junk preamble like an /etc/os-release dump or a progress
# banner no longer wins over a real diagnostic later in the output), then a
# documented last-nonempty-line fallback. A truncated traceback (nothing
# after its header) no longer collapses to the generic header text itself.
ERROR_SIGNATURE_VERSION = "error-sig-0.2"

# The recovery state machine (spec §8.6, items 4/5/29). RecoveryEpisode
# previously carried no derivation_version at all — unlike every other
# derived record — so a classification/enrichment change wasn't auditable
# against the logic that produced it.
# 0.2 (AGR-04): a state-changing action only credits strategy_changed when
# its own result did not fail — a failed Edit/rm changed nothing the agent
# could have built the eventual success on, and previously still earned
# "the agent changed something" credit.
# 0.3 (AGR-05): usage is now episode_window_tokens/initiating_attempt_tokens/
# usage_completeness — summed by event POSITION over the defined window
# (strictly after the failure through the selected resolution or observed
# terminal event, including model_output), replacing the old `tokens` field
# that summed the display-only `evidence_event_ids` list instead (which
# excluded model_output entirely and conflated the initiating attempt's own
# cost into the total).
# 0.4 (AGR-06): episodes now also carry usage_records (event_id -> tokens
# for every cost-carrying event in the window), so agr.fleet can compute
# group/fleet usage as a UNION of underlying records instead of summing
# episode_window_tokens directly — two episodes whose windows share events
# would otherwise double-count them.
# 0.5 (PR #56 review): a documentation/text-note edit (docs/, README, .md/
# .rst/.txt) no longer credits strategy_changed — it cannot be the
# functional fix for a failing command, unlike a same-or-different-named
# SOURCE file edit, which still credits unconditionally (see
# agr._util.is_state_changing_action_related_to). initiating_attempt_tokens
# now also finds a turn's cost on its LEADING model_output step when the
# failing call's own turn opened with reasoning/message text (item 27's
# per-turn cost attribution) — previously read as 0 whenever that was the
# case (confirmed on the real corpus: 11/22 episodes, now 0/22).
RECOVERY_VERSION = "recovery-0.5"

# The corpus manifest (AGR-01): reproducible provenance over a Harbor eval
# corpus root — source locations/checksums, logical run ids, capture ids, and
# the counting-stage definitions (discovered trials, ingested captures,
# logical runs, active captures) every later stage's counts must reconcile
# against. See agr/corpus_manifest.py.
CORPUS_MANIFEST_VERSION = "corpus-manifest-0.1"
