"""Typed records for the deterministic core.

These mirror the data model in spec §6. They are deliberately plain
dataclasses (stdlib only) so the core has no third-party dependencies and is
trivially serialisable to the immutable JSON store.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# --- Controlled vocabularies (subset used by the deterministic core) ---------

# Core derived-event vocabulary (spec §6.3).
EVENT_TYPES = {
    "task_received",
    "model_output",
    "plan_declared",
    "tool_call",
    "tool_result",
    "environment_observation",
    "artifact_observation",
    "process_started",
    "process_observed",
    "error_observed",
    "retry",
    "strategy_change",
    "context_compaction",
    "final_submission",
    "verifier_check",
    "run_completed",
    "run_timed_out",
    "run_failed",
    "run_finished",
}

# Adapter capability levels (spec §6.2), ordered weakest -> strongest.
CAPABILITY_LEVELS = ("unavailable", "final_only", "checkpoint_only", "partial", "complete")
CAPABILITY_RANK = {level: i for i, level in enumerate(CAPABILITY_LEVELS)}

# Verifier check status and source (spec §6.6).
CHECK_STATUSES = {"passed", "failed", "skipped", "error", "unknown"}
CHECK_SOURCES = {
    "native_structured",
    "instrumented_assertion",
    "code_inspection",
    "output_interpretation",
}

# When the check's evidence was collected relative to the agent's execution
# (AGR-04): post-run verifier output is not information the agent had.
CHECK_TIMINGS = {"during_run", "post_run"}

# Capture completeness (spec §6.1 / §7.3).
CAPTURE_COMPLETENESS = {"incomplete", "partial", "complete", "corrected"}

# Attribution ceiling a deterministic slice may confer (spec §3.5). The
# deterministic core never emits anything above ``dependency_linked``;
# ``counterfactually_supported`` requires replay evidence that this package
# does not produce.
ATTRIBUTION_LEVELS = ("hypothesized", "dependency_linked", "direct", "counterfactually_supported")

# Step provenance (spec §6.3): how a step came to exist. ``observed`` steps
# record something the source actually captured; ``derived`` steps are computed
# from observed ones; ``synthetic`` steps are adapter-written bookkeeping that
# no source ever contained (e.g. a harness terminal marker).
PROVENANCE_LEVELS = {"observed", "derived", "synthetic"}

# --- Task contract vocabularies (spec §3.1, §6.4, §6.5) ----------------------

# Where a contract item comes from (spec §3.1). Disagreement between sources is
# preserved rather than silently resolved.
CONTRACT_SOURCE_TYPES = {
    "stated_requirement",        # explicitly present in the task instruction
    "verifier_enforced",         # checked by verifier code / structured output
    "inferred_assumption",       # plausible but not explicit; needs confirmation
    "environment_precondition",  # required by the environment / tool interface
    "reference_assumption",      # present only in an oracle/reference solution
}

# Contract lifecycle (spec §6.4). Reviews built on ``draft`` or ``provisional``
# contracts are watermarked; only ``human_confirmed`` clears the watermark.
CONTRACT_STATUSES = {"draft", "provisional", "human_confirmed", "superseded"}
WATERMARKED_STATUSES = {"draft", "provisional"}

# Per-item human decision. Items start ``unconfirmed`` and require an explicit
# human decision before a contract can reach ``human_confirmed``.
ITEM_HUMAN_STATUSES = {"unconfirmed", "confirmed", "rejected", "edited"}

# Item importance — governs which items must be confirmed to finalise.
IMPORTANCE_LEVELS = {"required", "optional", "informational"}

# Whether an item asserts a thing must be present or must be absent. Two items
# on the same target with opposing polarity are a contradiction (spec §8.2).
CONTRACT_POLARITIES = {"require", "forbid"}

# Contract/check cross-check warnings (spec §8.2). These are surfaced, never
# silently resolved.
CONTRACT_WARNING_TYPES = {
    "verifier_only_requirement",         # verifier enforces something un-declared
    "prompt_only_unverified_requirement",  # stated requirement with no check
    "reference_only_assumption",         # assumption present only in the oracle
    "uncovered_artifact_requirement",    # declared artifact no check references
    "contradiction",                     # two items disagree on the same target
}

# Per-run contract observation status (spec §6.5). ``unknown`` is required when
# observability or semantic interpretation is insufficient.
CONTRACT_OBSERVATION_STATUSES = {
    "not_observed",
    "in_progress",
    "evidenced_satisfied",
    "at_risk",
    "evidenced_violated",
    "unknown",
}

# --- Task & Verifier Audit vocabularies (spec §11) ----------------------------

# The ten categorical audit dimensions (spec §11.1), in display order. Findings
# are categorical, evidence-backed qualifications of what a run result can
# legitimately imply — never 0-1 scores, and never an override of the result.
AUDIT_DIMENSIONS = (
    "instruction_clarity",
    "contract_verifier_coverage",
    "hidden_verifier_requirements",
    "prompt_only_unverified_requirements",
    "reference_solution_assumptions",
    "verifier_stability",
    "exploitability",
    "environment_realism",
    "capability_contamination",
    "product_relevance",
)

# Assessment values for every audit finding (spec §11.2). A dimension whose
# supporting inputs are missing reports ``insufficient_evidence`` — surfaced as
# "cannot say", never silently as "no concern".
CONCERN_ASSESSMENTS = ("supported_concern", "possible_concern",
                       "no_concern_detected", "insufficient_evidence")


def _clean(d: dict) -> dict:
    """Drop ``None`` values for tidier stored JSON."""
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class RunSource:
    """Immutable identity of one captured representation of a run (spec §6.1).

    ``run_id`` is the logical execution identity; ``source_capture_id`` is the
    immutable identity of one capture of that execution. A fuller or corrected
    capture of the same execution is a new capture (new id, incremented
    revision, ``supersedes_source_capture_id`` set), never a new logical run.
    """

    run_id: str
    source_capture_id: str
    capture_revision: int
    source_hash: str
    source_type: str
    source_schema: str
    capture_completeness: str
    task_id: str
    supersedes_source_capture_id: Optional[str] = None
    model: Optional[str] = None
    agent: Optional[str] = None
    harness_version: Optional[str] = None
    task_version: Optional[str] = None
    verifier_version: Optional[str] = None
    seed: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    adapter_version: str = ""
    # Sweep and configuration identity (§6.1, §7.1) plus the remaining §4.16
    # match keys. Carried through when the source declares them; ``None`` means
    # the source did not declare it, which the matched comparison reports as an
    # unresolved key rather than treating as "equal by absence".
    sweep_id: Optional[str] = None
    configuration_id: Optional[str] = None
    environment_image_digest: Optional[str] = None
    task_parameters: Optional[dict] = None
    # Captured agent execution cost/usage (§4.3.3), e.g. Claude's
    # ``total_cost_usd``/``usage``. ``None`` means the source never captured
    # it — never fabricated as 0, which would be indistinguishable from a
    # genuinely free run and would sort as the cheapest. Distinct from the
    # model reviewer's OWN per-round cost (agr/model_reviewer.py telemetry,
    # written to review_telemetry.json): that is AGR's review spend, not the
    # agent-under-test's, and the two are never added together.
    cost: Optional[float] = None
    tokens: Optional[dict] = None
    # Runs §item: the working directory the harness ran in, when the source
    # captured one (currently only the Claude Code adapter's stream init
    # record). Shown as a compact repository/location hint on the Runs
    # surface; ``None`` when the source never declared it — never guessed
    # from task_id or any other field.
    cwd: Optional[str] = None

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class CapabilityProfile:
    """What the source captured completely / partially / not at all (spec §6.2)."""

    run_id: str
    source_capture_id: str
    capabilities: dict[str, str]

    def to_dict(self) -> dict:
        return asdict(self)

    def rank(self, capability: str) -> int:
        return CAPABILITY_RANK.get(self.capabilities.get(capability, "unavailable"), 0)

    def meets(self, capability: str, minimum: str) -> bool:
        return self.rank(capability) >= CAPABILITY_RANK[minimum]


@dataclass
class DerivedEvent:
    """A normalised analysis event (spec §6.3).

    Every derived event points back to the source step(s) it came from via
    ``source_step_ids`` — the Milestone 1 traceability requirement (spec §20).
    """

    event_id: str
    run_id: str
    source_capture_id: str
    sequence: int
    source_step_ids: list[str]
    event_type: str
    actor: str
    parent_event_ids: list[str] = field(default_factory=list)
    phase_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    observations: list[Any] = field(default_factory=list)
    cost: dict[str, Any] = field(default_factory=dict)
    derivation_version: str = ""

    def to_dict(self) -> dict:
        return _clean(asdict(self))

    def text(self) -> str:
        """Best-effort textual content for deterministic token matching."""
        parts: list[str] = []
        for key in ("content", "data", "path", "artifact_path", "tool", "summary"):
            val = self.payload.get(key)
            if isinstance(val, str):
                parts.append(val)
        return " ".join(parts)


@dataclass
class Phase:
    """A contiguous structural segment of the run timeline (spec §8, Stage C1).

    Phase segmentation is deterministic and always runs (§8): it groups derived
    events by their *structural role* (intake / planning / execution /
    submission), splitting execution at each retry or strategy change into
    attempts. The labels are structural, not semantic — naming a phase
    "Installing the engine" is model-driven interpretation (C2/M4), out of the
    deterministic core. Every event carries its ``phase_id`` for synchronisation.
    """

    phase_id: str
    run_id: str
    source_capture_id: str
    kind: str  # intake | planning | execution | submission
    label: str
    event_ids: list[str] = field(default_factory=list)
    attempt: Optional[int] = None
    derivation_version: str = ""

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class VerifierCheck:
    """An atomic verifier check connected to contract items (spec §6.6)."""

    check_id: str
    run_id: str
    source_capture_id: str
    name: str
    status: str
    source: str
    contract_item_ids: list[str] = field(default_factory=list)
    expected: Optional[list] = None
    observed: Optional[list] = None
    source_pointers: list[str] = field(default_factory=list)
    derivation_version: str = ""
    # AGR-04 (pre-AGR-01 numbering): when the evidence was collected —
    # post-run verifier output is labelled so it is never presented as
    # information the agent had.
    timing: Optional[str] = None
    # AGR-02: reconciliation over multiple observations of the SAME scope
    # (agr.checks.reconcile_checks). ``scope`` is the normalized identity of
    # what was tested (e.g. the exact command text); ``None`` for checks a
    # reconciliation pass does not track (native/structured external-verifier
    # checks, which are already one atomic authoritative check each).
    scope: Optional[str] = None
    # Execution order among during_run checks, for "later observation" to be
    # well-defined; ``None`` when the source recorded no order (native checks).
    sequence: Optional[int] = None
    # Set on an EARLIER check when a LATER check of the identical scope
    # reconciles it — never the reverse, and never across different scopes (a
    # narrower check's scope string differs from a broader one's, so neither
    # can ever supersede the other). The historical record is never deleted or
    # mutated; this only says which check currently speaks for its scope.
    superseded_by: Optional[str] = None
    # Set on a ``passed`` check when a relevant mutation occurred after it
    # with no later same-scope re-verification — the pass can no longer be
    # trusted as evidence of the run's FINAL state, even though it was a real,
    # correctly-observed result at the time.
    stale_reason: Optional[str] = None

    @property
    def effective_status(self) -> Optional[str]:
        """This check's contribution to the CURRENT (reconciled) outcome view.

        ``None`` means excluded — a later observation of the identical scope
        speaks for it instead. A stale pass no longer counts as a pass (see
        ``stale_reason``) but is not silently dropped either: it demotes to
        ``"unknown"`` so an unresolved final state stays visible rather than
        reading as a clean pass. ``status`` itself is never changed — it is
        the immutable historical fact; this is the derived, current-view read.
        """
        if self.superseded_by is not None:
            return None
        if self.stale_reason is not None and self.status == "passed":
            return "unknown"
        return self.status

    def to_dict(self) -> dict:
        d = _clean(asdict(self))
        d["effective_status"] = self.effective_status
        return d


@dataclass
class EvidenceSlice:
    """Smallest relevant set connecting a check to events (spec §3.3 / §8.4).

    An evidence slice establishes *relevance*, not causality. Its
    ``attribution_ceiling`` is the strongest attribution language the slice can
    license; the deterministic core caps this at ``dependency_linked``.
    """

    slice_id: str
    run_id: str
    source_capture_id: str
    check_id: str
    contract_item_ids: list[str]
    branch: str  # standard | omission | distributed | external
    event_ids: list[str]
    attribution_ceiling: str
    rationale: str
    derivation_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Opportunity:
    """An interval in which the agent could demonstrate a behaviour (spec §6.7).

    Opportunity is the denominator for behavioural analytics and the anchor for
    omission findings — an omission is only valid inside a feasible window.
    """

    opportunity_id: str
    run_id: str
    source_capture_id: str
    ability: str
    trigger: str
    start_event_id: str
    end_event_id: str
    feasible_actions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecoveryEpisode:
    """Classification of behaviour following a qualifying failure (spec §8.6).

    ``good_recovery`` requires a qualifying failure, a meaningful strategy or
    action change, and eventual success. A success after an *unchanged* retry
    is recorded separately as ``retry_succeeded_without_strategy_change`` and is
    never promoted to good recovery.

    Item 4 (2026-09-07): ``plausibly_resolved`` is a THIRD, weaker tier for a
    success that shares the failed call's tool and executable and at least
    one overlapping target token, but not its complete command tail — a
    narrowed rerun or an adjacent command on the same target, which the
    strict objective-identity check (``_operation_key``) deliberately never
    links. It is evidence worth keeping, not nothing, but weaker than an
    exact-rerun link — ``attribution_ceiling`` records that: ``hypothesized``
    for a plausible link, the core's usual ``dependency_linked`` otherwise.
    """

    episode_id: str
    run_id: str
    source_capture_id: str
    classification: str  # good_recovery | retry_succeeded_without_strategy_change |
                          # plausibly_resolved | unrecovered_failure
    failure_event_id: str
    resolution_event_id: Optional[str]
    strategy_changed: bool
    changed_action: bool
    evidence_event_ids: list[str] = field(default_factory=list)
    attribution_ceiling: str = "dependency_linked"
    # Item 29 (2026-09-08) fleet-view enrichment — every field below is
    # deterministic and derived from records this module already computes;
    # none of it is a new judgement call, only a summary of the episode.
    tool: Optional[str] = None                   # the failed call's tool (action_signature[0])
    error_signature: Optional[str] = None         # agr.error_signature.error_signature(failure text)
    # AGR-07 (review 82cc113): which tier selected error_signature — one of
    # agr.error_signature._select_diagnostic's "traceback_exception" /
    # "diagnostic_line" / "fallback_last_nonempty". error_signature_with_
    # basis() always computes this (never None); it
    # was previously discarded because recovery.py called the string-only
    # error_signature() wrapper instead — a fallback-basis signature (no real
    # diagnostic found, e.g. bare "---") is then indistinguishable from a
    # confident traceback-derived one downstream (fleet grouping, the UI).
    error_signature_basis: Optional[str] = None
    # The same diagnostic line error_signature is derived from, kept UNMASKED
    # (real numbers/paths/strings, not <N>/<PATH>/<STR> placeholders) — useful
    # for a per-run finding headline, where error_signature's normalisation is
    # exactly what makes cross-run grouping possible but throws away the exact
    # figures a reader of ONE run wants to see (e.g. "43,320 tokens", not
    # "<N> tokens"). Same basis tiering as error_signature_basis.
    failure_diagnostic: Optional[str] = None
    turns_to_resolve: Optional[int] = None        # tool_call count from failure through resolution; None if unresolved
    # AGR-05: usage summed strictly AFTER the failure result through the
    # selected resolution (or the observed terminal event, for an unrecovered
    # episode) — by event POSITION over every event in that span, including
    # model_output, never the evidence_event_ids list above (built for
    # display/traceability; it omits model_output entirely). A later,
    # unrelated change scanned only while still looking for a strict match
    # after an earlier plausible one was already found can never inflate this
    # — the window ends at the resolution actually used.
    episode_window_tokens: int = 0
    # The FAILING attempt's own cost (its result, plus its paired call's, when
    # either carries one) — kept separate from the window above, which starts
    # strictly after it: the initiating attempt is not part of the cost of
    # recovering FROM the failure.
    initiating_attempt_tokens: int = 0
    # "complete" when the window closes on an actually observed event (a
    # resolution, or the run's own terminal event); "partial" when the
    # capture ran out mid-episode with neither ever observed; "unavailable"
    # when not one event inside the window ever carried a cost record at all
    # (the source never instrumented usage there) — a measured zero (some
    # events had cost data and it summed to zero) must never be confused
    # with usage that was simply never captured.
    usage_completeness: str = "complete"
    # AGR-06: event_id -> token count for every cost-carrying event in this
    # episode's window. Lets a fleet-level aggregate (agr.fleet) compute
    # usage as a UNION of underlying records across episodes/groups instead
    # of summing episode_window_tokens directly — two episodes whose windows
    # share events (e.g. an unresolved episode's window running into the
    # next failure's) would otherwise double-count the shared events'
    # tokens. Keys are unique only within this episode's OWN run_id+capture;
    # a cross-run merge must key on (run_id, event_id).
    usage_records: dict[str, int] = field(default_factory=dict)
    wall_ms: Optional[int] = None                 # wall-clock ms failure->resolution, when both carry a timestamp
    nth_occurrence_in_run: int = 1                # 1-indexed count of this error_signature within THIS run
    resolved_by: Optional[str] = None             # the resolving call's tool name; None if unresolved
    # Item 33 (2026-09-11 audit): True only for an UNRECOVERED episode whose
    # failed call was a read-only existence/state probe (test/[/ls/stat/
    # find/which/type), whose own failure reads as the target being ABSENT
    # rather than some other problem, and whose window shows a LATER action
    # addressing that same target — "the agent asked, got 'not there', and
    # took the intended branch". Distinguishing this from a genuine mistake
    # is a judgement ON TOP of the raw failure, never a replacement for it:
    # ``classification`` still reads ``unrecovered_failure`` and the episode
    # keeps its true evidence/window — only a detector deciding whether this
    # is ALSO a behavioural finding worth surfacing should read this flag.
    expected_probe: bool = False
    derivation_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    """A structured, deterministic behavioural candidate (spec §8.5).

    Detectors emit structured facts, not prose. Interpretation and causal
    judgement are deliberately absent — those belong to the model reviewer,
    which is not part of the deterministic core.
    """

    candidate_id: str
    run_id: str
    source_capture_id: str
    detector: str
    kind: str  # behaviour | omission | recovery | external
    anchor_event_ids: list[str]
    polarity: str = "negative"  # negative | positive
    severity: Optional[str] = None  # info | warning | high
    behaviour: Optional[str] = None
    consequence: Optional[str] = None
    micro_abilities: list[str] = field(default_factory=list)
    evidence_status: str = "complete"
    affected_checks: list[str] = field(default_factory=list)
    affected_contract_items: list[str] = field(default_factory=list)
    structured_facts: list[dict] = field(default_factory=list)
    detector_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewMoment:
    """A reviewed, validated, attribution-gated moment (spec §8.8–§8.10).

    The output of the deterministic reviewer envelope built ahead of the model
    reviewer (Milestone 4). It carries a candidate whose structured facts have
    been *re-validated* against the source-derived records (Stage G), a
    ``rendered_statement`` whose causal language does not exceed
    ``attribution_ceiling`` (Stage H), and the moment-selection outcome — whether
    the card was ``selected`` and, if de-duplicated, which higher-value moment
    ``superseded`` it (Stage I).

    The taxonomy *enrichment* fields (``taxonomy_verdict``, ``behaviour_tags``,
    ``phase``, ``consequence``, ``micro_abilities``, ``root_cause_candidates``,
    ``better_action``, ``instructional_value``, ``eval_lesson_recommended``) are
    the *model* reviewer's output (Stage F, spec §8.7). They stay ``None`` / empty
    in ``deterministic_only`` mode — the deterministic envelope never authors a
    verdict — and are populated behind the same view once a model reviewer runs.
    ``enrichment_source`` labels who wrote them so the UI can mark interpretations
    and better actions as model-generated (spec §8.8 "visibly labeled").
    """

    moment_id: str
    run_id: str
    source_capture_id: str
    candidate_id: str
    detector: str
    kind: str  # behaviour | omission | recovery | external
    polarity: str  # negative | positive
    anchor_event_ids: list[str]
    affected_checks: list[str] = field(default_factory=list)
    affected_contract_items: list[str] = field(default_factory=list)
    validated_facts: list[dict] = field(default_factory=list)
    attribution_ceiling: str = "hypothesized"
    rendered_statement: str = ""
    gate_results: dict = field(default_factory=dict)
    sequence: Optional[int] = None
    phase_id: Optional[str] = None
    selected: bool = False
    selection_rank: Optional[int] = None
    superseded_by: Optional[str] = None
    # --- model reviewer enrichment (Stage F, §8.7); None/empty when deterministic ---
    taxonomy_verdict: Optional[str] = None
    behaviour_tags: list[str] = field(default_factory=list)
    phase: Optional[str] = None
    consequence: Optional[str] = None
    micro_abilities: list[str] = field(default_factory=list)
    root_cause_candidates: list[dict] = field(default_factory=list)
    better_action: Optional[str] = None
    instructional_value: Optional[str] = None
    eval_lesson_recommended: bool = False
    enrichment_source: Optional[str] = None  # e.g. "model:claude-opus-4-8"; None = deterministic
    taxonomy_version: Optional[str] = None
    review_mode: str = "deterministic_only"
    reviewer_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContractItem:
    """One requirement in a task contract (spec §3.1, §6.4).

    A contract item is not a rewritten prompt line and not a copy of a verifier
    assertion — it is a human-confirmable statement of what the task appears to
    require, tagged with where the evidence for it comes from (``source_type``)
    and which atomic verifier checks are believed to cover it (``mapped_checks``).
    ``polarity`` distinguishes "must be present" from "must be absent" so that
    contradictory requirements can be detected mechanically.
    """

    id: str
    description: str
    source_type: str
    source_pointers: list[str] = field(default_factory=list)
    importance: str = "required"
    verification_mode: Optional[str] = None
    mapped_checks: list[str] = field(default_factory=list)
    human_status: str = "unconfirmed"
    target: Optional[str] = None
    polarity: str = "require"

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class ContractWarning:
    """A surfaced disagreement between contract sources (spec §8.2).

    The builder never silently resolves a mismatch; it records it here so a
    human can adjudicate during confirmation.
    """

    warning_type: str
    message: str
    item_ids: list[str] = field(default_factory=list)
    check_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskContract:
    """A versioned, human-confirmable task contract (spec §6.4).

    ``contract_version`` increments each time a human decision produces a new
    contract; the prior version is superseded, never mutated in place. A review
    built on a ``draft`` or ``provisional`` contract is watermarked.
    """

    task_id: str
    contract_version: int
    status: str
    items: list[ContractItem] = field(default_factory=list)
    warnings: list[ContractWarning] = field(default_factory=list)
    task_version: Optional[str] = None
    supersedes_contract_version: Optional[int] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    builder_version: str = ""

    @property
    def watermarked(self) -> bool:
        return self.status in WATERMARKED_STATUSES

    def item(self, item_id: str) -> Optional[ContractItem]:
        return next((i for i in self.items if i.id == item_id), None)

    def to_dict(self) -> dict:
        d = {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "contract_version": self.contract_version,
            "status": self.status,
            "supersedes_contract_version": self.supersedes_contract_version,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "builder_version": self.builder_version,
            "items": [i.to_dict() for i in self.items],
            "warnings": [w.to_dict() for w in self.warnings],
        }
        return _clean(d)


@dataclass
class ContractObservation:
    """Evidence-backed satisfaction of one contract item in one run (spec §6.5).

    Contract satisfaction is an *observation*, not assumed ground truth. It is
    derived deterministically from the status of the item's mapped verifier
    checks; ``unknown`` is used when no check or an inconclusive check covers it.
    """

    run_id: str
    contract_item_id: str
    status: str
    at_event_id: Optional[str] = None
    evidence: list[str] = field(default_factory=list)
    derivation: str = ""
    derivation_version: str = ""

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class AuditFinding:
    """One categorical, evidence-backed task/verifier audit finding (spec §11).

    Each finding qualifies *what a run result can legitimately imply* along one
    audit dimension — it never overrides the run result itself (spec §11). The
    assessment is one of ``CONCERN_ASSESSMENTS``; every concern carries exact
    supporting evidence as ids pointing into the derived records (events,
    verifier checks, contract items), so the claim is mechanically checkable.
    """

    dimension: str
    assessment: str
    statement: str
    run_id: Optional[str] = None
    source_capture_id: Optional[str] = None
    evidence_event_ids: list[str] = field(default_factory=list)
    evidence_check_ids: list[str] = field(default_factory=list)
    evidence_item_ids: list[str] = field(default_factory=list)
    audit_version: str = ""

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class DetectorResult:
    """Outcome of running one detector (spec §6.2 'not evaluated' rule).

    A detector whose required observability is missing reports
    ``evaluated=False`` — surfaced as "not evaluated", never as "no problem
    found".
    """

    detector: str
    evaluated: bool
    candidates: list[Candidate] = field(default_factory=list)
    unmet_capabilities: list[str] = field(default_factory=list)
    # AGR-05: True when the detector is a registered placeholder — distinct
    # from a capability-gated skip and from an evaluated no-issue run.
    placeholder: bool = False

    def to_dict(self) -> dict:
        return {
            "detector": self.detector,
            "evaluated": self.evaluated,
            "candidates": [c.to_dict() for c in self.candidates],
            "unmet_capabilities": self.unmet_capabilities,
            "placeholder": self.placeholder,
        }
