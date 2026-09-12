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
AGR_VERSION = "0.2.0"

DERIVATION_VERSION = "event-map-0.1"
# 0.2 (AGR-02): checks carry scope/sequence/superseded_by/stale_reason and an
# effective_status distinct from the raw historical status — reconciled by
# agr.checks.reconcile_checks before agr.checks.outcome rolls them up.
# 0.3 (PR #56 review): reconcile_checks' mutation-staleness test no longer
# marks a passed check stale after a documentation/text-note edit (it shares
# agr._util.is_state_changing_action_related_to with AGR-04's recovery
# credit fix) — a docs/README/.md edit cannot invalidate a check as evidence
# of final state.
# 0.4 (review 82cc113): synthesized in-session checks (agr.verifier_synth) no
# longer let a passing text summary mask a Go package build failure, a Cargo
# compile error alongside a passing crate, or a tool-level failure signal
# (non-zero exit/error status) the text parse alone couldn't see — each now
# demotes the check to "error"/adds the missed failure instead of reporting a
# clean pass. The shared mutation-relatedness helper also no longer excludes
# a functional edit by bare extension (.txt/.md/.rst) — only by conventional
# repo-meta PATH marker — so a test fixture or dependency manifest edit is no
# longer wrongly treated as staleness-irrelevant.
# 0.5 (PR #57 review): fixes a regression 0.4 introduced. A genuine failing
# test run's own exit code is non-zero WHENEVER any test fails — that is the
# ordinary shape of a real failure, not evidence of a separate tool-level
# crash. Checking the tool-level failure signal before the parsed failed
# count turned every ordinary failing run with a captured exit code into
# "error" instead of "failed", which flipped the run outcome to UNDETERMINED
# and skipped the AGR-08 detectors (they select on effective_status ==
# "failed"). The tool-level signal now only overrides a PASSING parse it
# contradicts, never a parse that already found real failures.
CHECK_DERIVATION_VERSION = "check-extract-0.5"
SLICE_DERIVATION_VERSION = "evidence-slice-0.1"
# 0.2 (AGR-08): UnresolvedRequirementAtSubmission and
# TerminalFailureWithFailingChecks each emit ONE aggregate candidate per
# eligible run/capture carrying every failing check, not one candidate per
# check — a run with N failing checks previously produced N near-identical
# terminal-statement candidates that crowded out other findings.
# 0.3 (review 82cc113): total_checks on each aggregate's structured facts is
# now the CURRENT (reconciled, effective_status is not None) check count,
# never len(ctx.checks) — a superseded historical observation no longer
# inflates the denominator a selected card renders as "N of total checks".
# 0.4 (item 33, 2026-09-11 audit): IgnoredToolFailure no longer emits a
# candidate for an UNRECOVERED episode RecoveryEpisode.expected_probe marks
# as an answered existence/state check followed by the agent taking the
# intended branch — an audited sample measured 1/5 precision on exactly this
# shape. The raw failure fact is untouched; only this detector's reading of
# it changed.
DETECTOR_VERSION = "detectors-0.4"
CONTRACT_BUILDER_VERSION = "contract-builder-0.1"
# 1.1 (AGR-02, review 82cc113): reads each mapped check's effective_status
# (the current, reconciled view) rather than its raw immutable status — a
# check a later same-scope check has superseded no longer speaks for its
# item, so an item is no longer stuck evidenced_violated by a first attempt
# the run's own reconciliation has already moved past.
CONTRACT_OBSERVATION_VERSION = "contract-status-1.1"
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
# 0.3 (AGR-08, review 82cc113): an in-session check (agr.verifier_synth,
# source == "output_interpretation", timing == "during_run") is now
# recognised as agent-observed by construction — its status_basis is
# "in_session_observation" and agent_observed_failure follows the check's own
# status directly, instead of requiring the check-id/status text pattern that
# can only ever match a literal echoed id like "C1 FAILED" (never a
# synthesized id like insession_pytest_1, so it always reported no observed
# failure for one). Rendered wording no longer calls an in-session check "the
# run's final verifier", a post-run-only concept.
REVIEWER_VERSION = "reviewer-det-0.3"

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
# 0.6 (review 82cc113): three independent AGR-04 fixes.
# * A source-code edit unrelated by path/target to a failed NETWORK PROBE
#   (curl/wget/ping/...) no longer credits strategy_changed unconditionally
#   just because it touches a "functional" path — it now needs the same
#   target-overlap evidence a shell mutation already required, since there is
#   no legitimate "fix lives in a differently-named file" story for a raw
#   connectivity failure. (Editing any other, non-probe command's related
#   source file is unaffected and still credits unconditionally.)
# * A mutation's result now needs OBSERVED success (is_tool_success), not
#   merely the absence of an observed failure — the previous check fell back
#   to the tool_call event itself when no result was captured, which can
#   never satisfy is_tool_failure either and so silently credited a change
#   from evidence that was never actually observed.
# * strategy_changed is frozen at the moment a PLAUSIBLE resolution is found
#   (the scan keeps running afterward, still looking for a strict match) —
#   activity after that point can no longer attach change credit to a
#   resolution that had already closed before it happened.
# 0.7 (review 82cc113, AGR-05): usage_completeness now also checks PER-TURN
# cost coverage inside the window, not just whether the window closed on an
# observed event — a window can contain one measured turn and a sibling turn
# with no recorded usage at all, which previously still read as "complete".
# Any turn (a tool_call, its own model_output lead-in, and its paired result)
# with no cost recorded anywhere in it, while at least one other turn in the
# same window has some, now demotes the window to "partial". A window whose
# turns are ALL cost-instrumented (including a measured zero) is unaffected.
# 0.8 (review 82cc113, AGR-07): episodes now carry error_signature_basis —
# recovery.py previously called the string-only error_signature() wrapper and
# discarded which tier (traceback_exception/diagnostic_line/fallback_last_
# nonempty) actually selected the line, so an opaque fallback signature (bare
# "---"/"}"/"===", no real diagnostic anywhere) was indistinguishable from a
# confident traceback-derived one downstream (fleet grouping, the UI).
# 0.9 (item 33, 2026-09-11 audit): episodes now carry expected_probe — an
# UNRECOVERED episode whose failed call was a read-only existence/state probe
# (test/[/ls/stat/find/which/type), whose own failure reads as the target
# being ABSENT rather than some other problem, and whose window shows a
# LATER action addressing that same target. Additive only: classification,
# evidence, and the window are unchanged — the raw non-zero result is never
# suppressed, only labelled as also answering an expected check.
# 0.10 (PR #69 review, 2026-09-12): five fixes to 0.9's expected_probe.
# * A failing call with empty/whitespace content no longer crashes
#   classification for the whole capture (an unguarded executable lookup
#   raised IndexError before the probe-shape guard ever ran).
# * Probe-target/follow-up-target comparison now reuses `_util.target_tokens`
#   / `MIN_RELATED_TOKEN_LEN`, the same "no trivial 1-2 character token"
#   floor `is_state_changing_action_related_to` already needs — a bare
#   substring check previously let a 1-character target match an unrelated
#   command purely by coincidence.
# * A follow-up action must itself have SUCCEEDED to count as the intended
#   branch — a target-matching mkdir/touch that itself failed (e.g.
#   permission denied) leaves the target just as absent as before, and was
#   previously credited the same as a successful one.
# * `ls`/`stat`/`find`'s absence diagnostic is now also read from a Claude
#   Code capture's separately-recorded stdout/stderr (`tool_use_result`),
#   not only the rendered `content` text that adapter deliberately keeps
#   distinct — the previous text-only read never fired the exemption on
#   exactly the capture shape the audit's false positives came from.
# * A compound command (`test -f x || mkdir y`) no longer pulls the CHAINED
#   command's own executable/args into the probe's target set — only the
#   probe's own invocation, truncated at the first shell chain operator, is
#   considered.
RECOVERY_VERSION = "recovery-0.10"

# The corpus manifest (AGR-01): reproducible provenance over a Harbor eval
# corpus root — source locations/checksums, logical run ids, capture ids, and
# the counting-stage definitions (discovered trials, ingested captures,
# logical runs, active captures) every later stage's counts must reconcile
# against. See agr/corpus_manifest.py.
CORPUS_MANIFEST_VERSION = "corpus-manifest-0.1"
