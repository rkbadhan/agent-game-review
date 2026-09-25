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

# GR-1: per-tag reviewing guidance — what evidence justifies each tag. The model
# reviewer renders this map into its system prompt as GENERAL guidance, replacing
# the old run-specific "DRIFT TEST" block. Every BEHAVIOUR_TAGS entry must have a
# line here (a test enforces coverage), so the prompt cannot drift from the
# vocabulary it is allowed to emit.
BEHAVIOUR_TAG_GUIDANCE = {
    # negative
    "misread_instruction": (
        "an action contradicts a clear instruction in the task; cite the instruction "
        "text and the contradicting action"),
    "missed_constraint": (
        "an explicit constraint (limit, format, scope) that was available to the agent "
        "is violated, with an observed consequence"),
    "lost_requirement": (
        "a requirement stated in the task or the agent's own plan is never addressed "
        "again anywhere in the run"),
    "poor_decomposition": (
        "the work skips a needed intermediate step, or bundles changes that had to be "
        "staged separately, with a supported consequence"),
    "premature_commitment": (
        "the agent commits to an approach early and keeps it after contradicting "
        "evidence appears"),
    "missing_prerequisite": (
        "an action depends on state, config, or an artifact that was never created or "
        "verified, and fails or misbehaves as a result"),
    "poor_sequencing": (
        "an ordering choice makes a later action invalidate or duplicate earlier work, "
        "with an observed effect"),
    "failed_to_replan": (
        "the plan stops matching observed reality and is not revised before the run ends"),
    "wrong_tool": (
        "a tool is used where another available one plainly fit the need, and the "
        "mismatch has a supported consequence"),
    "invalid_arguments": (
        "a tool call's arguments are malformed, out of range, or contradict the tool's "
        "own schema or prior state"),
    "poor_query": (
        "a search or query is so broad, narrow, or malformed that it cannot surface "
        "the needed information"),
    "misread_tool_output": (
        "the agent's next action contradicts what a tool result plainly reported"),
    "repeated_unchanged_action": (
        "the same failing action is retried without any change that could plausibly "
        "alter the result"),
    "ignored_error": (
        "a tool or process error is observed and never handled, retried, or acknowledged"),
    "wrong_artifact_or_path": (
        "the artifact read or written is not the one the task or plan requires; cite "
        "the claimed path and the real one"),
    "incomplete_execution": (
        "the run ends with part of the required work demonstrably undone"),
    "unmonitored_process": (
        "a long-running or background process is started and its result or liveness "
        "is never checked"),
    "destructive_action": (
        "an action removes, truncates, or overwrites state needed later, with a "
        "demonstrated consequence"),
    "inefficient_execution": (
        "the required outcome is reached with avoidable cost or latency, shown by a "
        "comparable baseline or replay"),
    "skipped_verification": (
        "a required check was never run before the agent treated the work as complete "
        "or submitted"),
    "insufficient_verification": (
        "a check ran but could not establish the requirement because it was too weak, "
        "partial, or off-target"),
    "unsupported_claim": (
        "the agent asserted an outcome that the evidence available to it did not establish"),
    "premature_submission": (
        "the agent submitted while a known requirement was still failing or unresolved"),
    "stopped_enumeration": (
        "the agent stopped listing or searching before the needed set was covered, and "
        "the gap matters"),
    # positive
    "good_decomposition": (
        "the plan splits the task into steps the run then executes in a workable order, "
        "and the steps advance stated requirements"),
    "efficient_tool_selection": (
        "the chosen tool or query produced the needed evidence with little wasted work, "
        "compared with what was available"),
    "evidence_driven_decision": (
        "a decision follows directly from an observed result; quote that result verbatim"),
    "good_recovery": (
        "a failure occurred, the agent changed approach, and the change produced a "
        "confirmed recovery"),
    "strong_final_verification": (
        "a final check exercises the actual requirement and its observed result is recorded"),
    "preserved_requirements": (
        "the agent changed approach without dropping a requirement it had already established"),
    "effective_replan": (
        "the plan changed in response to contradicting evidence and the new plan "
        "advanced the task"),
}

# ---------------------------------------------------------------------------
# GR-3 — the four categories from the idea, as a deterministic VIEW over tags.
#
# The model reviewer emits behaviour TAGS, never categories: the system derives
# the category from the tags (and from a detector's own tags), so a category can
# never be claimed without a tag — and the evidence behind that tag — actually
# existing. ``detection`` records the routes that can produce a finding for the
# category: a deterministic detector, model guidance, or the argument-shape
# statistics. Coverage is REPORTED from what was really found, never forced:
# abstention stays valid and a category with no supported moment stays empty.
IDEA_CATEGORIES = {
    "mistake_in_planning": {
        "label": "Mistake in planning",
        "tags": ("poor_decomposition", "failed_to_replan", "premature_commitment"),
        "detection": ({"source": "model"},),
    },
    "bad_query_in_tool_call": {
        "label": "Bad query in a tool call",
        "tags": ("poor_query", "invalid_arguments"),
        "detection": ({"source": "model"}, {"source": "argument_shapes"}),
    },
    "claiming_victory_before_verifying": {
        "label": "Claiming victory before verifying",
        "tags": ("premature_submission", "skipped_verification"),
        # Only the MODEL may assign these tags. The submission detector surfaces
        # the situation (a submission with a failing check) but does NOT establish
        # that the agent claimed success or even knew the check was failing — an
        # honest "I could not finish" submission fails the same check. So the
        # detector is recorded as ``surfaced_by`` (a raw input to the category),
        # never as a mechanical tag route.
        "detection": ({"source": "model"},),
        "surfaced_by": ({"source": "detector", "name": "unresolved_requirement_at_submission"},),
    },
    "good_recovery_from_failed_plan": {
        "label": "Good recovery from a failed plan",
        "tags": ("good_recovery", "effective_replan"),
        "detection": ({"source": "detector", "name": "successful_recovery_via_strategy_change"},),
    },
}

# A deterministic detector that emits a vocabulary tag on its own: the tag is
# mechanically supported, so the label built from it is a MECHANICAL label (its
# ``basis``), not a model interpretation. Kept here beside the categories so the
# coverage invariant can see detector → tag → category in one place. Recovery's
# ``effective_replan`` is added conditionally by the read layer (it depends on
# the episode's ``strategy_changed``), so only the unconditional tag is mapped.
#
# A detector belongs here ONLY when it establishes the tag's meaning from its own
# structures. ``unresolved_requirement_at_submission`` deliberately does NOT: it
# proves a submission and a failing check, not that the agent claimed victory —
# so it gets a neutral label instead (:data:`DETECTOR_NEUTRAL_LABELS`).
DETECTOR_BEHAVIOUR_TAGS = {
    "successful_recovery_via_strategy_change": ("good_recovery",),
}

# A neutral, mechanically supported label for a detector whose finding is real
# but does NOT establish a taxonomy tag's meaning. It states exactly what the
# detector observed — no victory, no verified claim — so nothing is over-claimed.
DETECTOR_NEUTRAL_LABELS = {
    "unresolved_requirement_at_submission": "Requirement unresolved at submission",
}

_TAG_TO_CATEGORY = {
    tag: category_id
    for category_id, spec in IDEA_CATEGORIES.items()
    for tag in spec["tags"]
}


def category_for_tag(tag: str) -> str | None:
    """The idea category a behaviour tag belongs to, or ``None`` if the tag is
    outside the four idea categories (most of the taxonomy is — this registry is
    the *idea coverage* check, not a second full taxonomy)."""
    return _TAG_TO_CATEGORY.get(tag)


def category_for_tags(tags) -> list[str]:
    """Every idea category covered by a moment's tags, sorted and de-duplicated."""
    return sorted({category_for_tag(t) for t in tags if category_for_tag(t)})


def category_coverage() -> list[dict]:
    """The registry with, per category, whether each detection route exists.

    ``model_guided`` is true when a tag of the category has prompt guidance (the
    model can be asked for it); ``has_detector`` when a deterministic detector
    can emit it; ``has_argument_shapes`` when the shape statistics can support it.
    """
    out = []
    for category_id, spec in IDEA_CATEGORIES.items():
        sources = {d["source"] for d in spec["detection"]}
        out.append({
            "category_id": category_id,
            "label": spec["label"],
            "tags": list(spec["tags"]),
            "detection": [dict(d) for d in spec["detection"]],
            "surfaced_by": [dict(d) for d in spec.get("surfaced_by", ())],
            "model_guided": all(t in BEHAVIOUR_TAG_GUIDANCE for t in spec["tags"]),
            "has_detector": "detector" in sources,
            "has_argument_shapes": "argument_shapes" in sources,
        })
    return out


# GR-3 invariants: the registry can never drift from the vocabulary it claims to
# cover. Every tag is real and has prompt guidance; every category has at least
# one detection route; and no tag is claimed by two categories (so a moment has
# one unambiguous category per tag).
assert all(
    tag in BEHAVIOUR_TAGS
    for spec in IDEA_CATEGORIES.values() for tag in spec["tags"]
), "IDEA_CATEGORIES names a tag outside the controlled behaviour taxonomy"
assert all(
    tag in BEHAVIOUR_TAG_GUIDANCE
    for spec in IDEA_CATEGORIES.values() for tag in spec["tags"]
), "IDEA_CATEGORIES names a tag with no prompt guidance"
assert all(spec["detection"] for spec in IDEA_CATEGORIES.values()), \
    "an idea category has no detection route"
assert len(_TAG_TO_CATEGORY) == sum(
    len(spec["tags"]) for spec in IDEA_CATEGORIES.values()
), "a behaviour tag belongs to more than one idea category"
assert all(
    tag in BEHAVIOUR_TAGS
    for tags in DETECTOR_BEHAVIOUR_TAGS.values() for tag in tags
), "DETECTOR_BEHAVIOUR_TAGS maps a detector to a tag outside the taxonomy"
assert not (set(DETECTOR_BEHAVIOUR_TAGS) & set(DETECTOR_NEUTRAL_LABELS)), \
    "a detector cannot both mechanically tag and be neutral"

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
