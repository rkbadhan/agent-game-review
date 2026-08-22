"""The controlled behavioural taxonomy (spec §9.1).

Single source of truth for the multi-axis vocabulary the *model* reviewer
(Stage F) labels moments with: phase, behaviour (negative + positive),
consequence, root-cause locus, and micro-ability. The deterministic gold schema
(:mod:`agr.gold`) re-uses the anchor/behaviour/locus/polarity sets from here so
the two never drift.

The taxonomy is versioned (``version.TAXONOMY_VERSION``): a moment stores the
version it was labelled under, and the reviewer may never emit a tag outside the
active version (spec §8.7 "create taxonomy labels outside the active version" is a
prohibited action; §9.2 "semantic meaning is never silently redefined").

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

# Phase axis (spec §9.1). The deterministic C1 segmentation names a coarser set
# (intake/planning/execution/submission); the taxonomy phase axis is the finer
# vocabulary a reviewer assigns to a moment.
PHASES = {
    "understanding", "planning", "information_gathering", "execution",
    "recovery", "verification", "submission", "external",
}

# What kind of thing a decisive moment is (spec §3.4). A detector
# ``Candidate.kind`` of ``behaviour`` maps to the ``decision`` anchor type.
MOMENT_ANCHOR_TYPES = {"decision", "omission", "recovery", "external"}

# Behaviour axis (spec §9.1). A moment may carry several tags.
NEGATIVE_BEHAVIOUR_TAGS = {
    "misread_instruction", "missed_constraint", "lost_requirement",
    "poor_decomposition", "premature_commitment", "missing_prerequisite",
    "poor_sequencing", "failed_to_replan", "wrong_tool", "invalid_arguments",
    "poor_query", "misread_tool_output", "repeated_unchanged_action",
    "ignored_error", "wrong_artifact_or_path", "incomplete_execution",
    "unmonitored_process", "destructive_action", "inefficient_execution",
    "skipped_verification", "insufficient_verification", "unsupported_claim",
    "premature_submission", "stopped_enumeration",
}
POSITIVE_BEHAVIOUR_TAGS = {
    "good_decomposition", "efficient_tool_selection", "evidence_driven_decision",
    "good_recovery", "strong_final_verification", "preserved_requirements",
    "effective_replan",
}
BEHAVIOUR_TAGS = NEGATIVE_BEHAVIOUR_TAGS | POSITIVE_BEHAVIOUR_TAGS

# Consequence axis (spec §9.1).
CONSEQUENCES = {
    "requirement_failed", "requirement_at_risk", "incorrect_state",
    "incomplete_artifact", "excess_cost", "excess_latency",
    "recovery_succeeded", "verifier_failure", "verifier_pass", "no_material_effect",
}

# Root-cause locus (spec §11, §9.1). A single rollout normally cannot justify
# ``base_model`` above ``hypothesized``; that ceiling is enforced by the reviewer.
ROOT_CAUSE_LOCI = {
    "base_model", "agent_policy", "agent_scaffold", "evaluation_harness",
    "tool", "environment", "task_instruction", "reference_solution",
    "verifier", "indeterminate",
}

# Micro-ability evidence axis (spec §9.1).
MICRO_ABILITIES = {
    "requirement_tracking", "exhaustive_completion", "query_formulation",
    "tool_selection", "tool_error_recovery", "strategy_change",
    "state_interpretation", "artifact_management", "process_monitoring",
    "compaction_resume", "verification_discipline", "termination_judgment",
}

# Polarity of a moment (a positive moment is a strength; spec §8.10 review quota).
MOMENT_POLARITIES = {"negative", "positive"}
