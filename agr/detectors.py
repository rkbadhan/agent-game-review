"""Deterministic candidate detectors (spec §8.5).

Detectors emit structured facts, not prose, and never assign causal
interpretation. Each declares the observability it needs; when the capability
profile does not meet it, the detector reports ``evaluated=False`` and is
surfaced as "not evaluated", never as "no problem found" (spec §6.2).

The five MVP detectors (spec §8.5) are implemented, plus a capability-gated
context detector to exercise the "not evaluated" path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import version
from ._util import action_signature, is_mutation, paired_result
from .recovery import GOOD_RECOVERY, UNRECOVERED
from .schema import (
    Candidate,
    CapabilityProfile,
    DerivedEvent,
    DetectorResult,
    Opportunity,
    RecoveryEpisode,
    VerifierCheck,
)


@dataclass
class DetectorContext:
    run_id: str
    capture_id: str
    events: list[DerivedEvent]
    checks: list[VerifierCheck]
    doc: dict
    profile: CapabilityProfile
    recoveries: list[RecoveryEpisode] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)

    def submission(self) -> Optional[DerivedEvent]:
        return next((e for e in self.events if e.event_type == "final_submission"), None)


class Detector:
    name: str = ""
    required_capabilities: dict[str, str] = {}
    # AGR-05: a registered-but-not-implemented detector is distinct from an
    # evaluated detector that found no issue. Placeholders report
    # evaluated=False with placeholder=True even when capabilities would allow
    # a run — "no candidates" from a stub must never read as "no problem".
    implemented: bool = True

    def _run(self, ctx: DetectorContext) -> list[Candidate]:
        raise NotImplementedError

    def run(self, ctx: DetectorContext) -> DetectorResult:
        unmet = [c for c, m in self.required_capabilities.items() if not ctx.profile.meets(c, m)]
        if not self.implemented:
            # A stub can never evaluate, whatever the capabilities say — report
            # the placeholder status (and the unmet capabilities, for candour).
            return DetectorResult(detector=self.name, evaluated=False,
                                  unmet_capabilities=unmet, placeholder=True)
        if unmet:
            return DetectorResult(detector=self.name, evaluated=False, unmet_capabilities=unmet)
        return DetectorResult(detector=self.name, evaluated=True, candidates=self._run(ctx))

    def _candidate(self, ctx: DetectorContext, key: str, **kw) -> Candidate:
        return Candidate(
            candidate_id=f"cand_{self.name}_{key}",
            run_id=ctx.run_id,
            source_capture_id=ctx.capture_id,
            detector=self.name,
            detector_version=version.DETECTOR_VERSION,
            **kw,
        )


class UnresolvedRequirementAtSubmission(Detector):
    """A required contract item is still failing when the run submits (§8.5 #4).

    AGR-08: one candidate for the whole run carrying every failing check —
    never one candidate per check. Repeating the same terminal statement once
    per failing check crowded out other, genuinely different findings on the
    run and made "how many distinct problems does this run have" unreadable
    from the candidate count.
    """

    name = "unresolved_requirement_at_submission"
    required_capabilities = {"messages": "complete", "tool_results": "complete"}

    def _run(self, ctx):
        submit = ctx.submission()
        if submit is None:
            return []
        # AGR-02: the reconciled CURRENT view — an obsolete failure a later
        # same-scope check reconciled (or a pass since invalidated by a
        # relevant mutation) must not still flag as unresolved.
        failing = [c for c in ctx.checks if c.effective_status == "failed"]
        if not failing:
            return []
        facts = [{
            "type": "requirement_status", "check_id": c.check_id,
            "status_at_submission": "failed",
            "expected": c.expected, "observed": c.observed,
            "total_checks": len(ctx.checks),
        } for c in failing]
        return [self._candidate(
            ctx, "aggregate", kind="omission", anchor_event_ids=[submit.event_id],
            affected_checks=[c.check_id for c in failing],
            affected_contract_items=sorted({i for c in failing for i in c.contract_item_ids}),
            structured_facts=facts,
        )]


class TerminalFailureWithFailingChecks(Detector):
    """A required check still failing when the run ends WITHOUT a submission,
    because the harness terminated it (timeout or failure) — item 6 (2026-09-07).

    ``UnresolvedRequirementAtSubmission`` anchors on an observed
    ``final_submission``; a run the harness killed (a timeout, a crash) never
    has one, so a genuinely failed run with failing checks produced ZERO
    candidates from that detector — an empty review for a run that plainly
    did not finish the task. This detector covers exactly that gap: it fires
    only when the run ended ``run_timed_out``/``run_failed`` and no
    submission was ever observed (never overlapping with the submission-
    anchored detector), anchored on the terminal event itself and the last
    agent action, so the run is never silently reviewed as "nothing to say".
    """

    name = "terminal_failure_with_failing_checks"
    # Review finding #2 (2026-09-07): _run reads no tool_result event at all —
    # only the terminal event, the last main_agent event, and ctx.checks — so
    # requiring tool_results:complete gated this detector off on exactly the
    # runs it exists for. A run the harness killed mid-tool-call (the normal
    # shape of a timeout/crash) reports tool_results:partial, which made this
    # detector report evaluated=False and emit nothing on precisely those runs.
    required_capabilities = {"messages": "complete"}

    _TERMINAL_FAILURE_TYPES = {"run_timed_out", "run_failed"}

    def _run(self, ctx):
        if ctx.submission() is not None:
            return []  # a submitted run is UnresolvedRequirementAtSubmission's territory
        terminal = next(
            (e for e in ctx.events if e.event_type in self._TERMINAL_FAILURE_TYPES), None)
        if terminal is None:
            return []
        last_agent_event = next(
            (e for e in reversed(ctx.events) if e.actor == "main_agent"), None)
        has_last_agent_action = (
            last_agent_event is not None and last_agent_event.event_id != terminal.event_id)
        anchor = [terminal.event_id]
        if has_last_agent_action:
            anchor.append(last_agent_event.event_id)
        # AGR-02: the reconciled CURRENT view — see UnresolvedRequirementAtSubmission.
        failing = [c for c in ctx.checks if c.effective_status == "failed"]
        if not failing:
            return []
        facts = [{
            "type": "requirement_status", "check_id": c.check_id,
            "status_at_submission": "failed",
            "expected": c.expected, "observed": c.observed,
            "terminal_event_type": terminal.event_type,
            "total_checks": len(ctx.checks),
            # AGR-08: render that limitation explicitly rather than letting a
            # missing last-agent-action silently look like an ordinary anchor.
            "last_agent_action_captured": has_last_agent_action,
        } for c in failing]
        return [self._candidate(
            ctx, "aggregate", kind="omission", anchor_event_ids=anchor,
            affected_checks=[c.check_id for c in failing],
            affected_contract_items=sorted({i for c in failing for i in c.contract_item_ids}),
            structured_facts=facts,
        )]


class IgnoredToolFailure(Detector):
    """A tool failure left unresolved through submission (§8.5 #1)."""

    name = "ignored_tool_failure"
    required_capabilities = {"tool_results": "complete"}

    def _run(self, ctx):
        submit = ctx.submission()
        out = []
        for ep in ctx.recoveries:
            if ep.classification != UNRECOVERED:
                continue
            anchor = [ep.failure_event_id] + ([submit.event_id] if submit else [])
            out.append(self._candidate(
                ctx, ep.failure_event_id, kind="behaviour", anchor_event_ids=anchor,
                structured_facts=[{
                    "type": "state_transition", "failure_event": ep.failure_event_id,
                    "resolved_before_submission": False,
                }],
            ))
        return out


class RepeatedActionNoNewInfo(Detector):
    """Same action repeated, producing the same output again (§8.5 #2).

    AGR-05: a repeated call is an observation, not automatically a defect. The
    "no new information" claim is made only when the mechanical evidence
    supports it — both calls' tool results are captured and their outputs are
    equivalent. Differing outputs (e.g. a poll whose report changed) are
    observation with new information and emit nothing; unobserved results
    cannot support the claim and emit nothing either.
    """

    name = "repeated_action_no_new_info"
    required_capabilities = {"tool_calls": "complete", "tool_results": "complete"}

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(text.split())

    def _run(self, ctx):
        out = []
        # First walk: collect tool calls in order and remember whether a
        # strategy change separated a call from the previous one. The result
        # for each call is resolved through the ONE shared call↔result index
        # (``paired_result`` — id-based, correct for parallel calls; adjacency
        # only as a conservative id-less fallback). Detector, validator, and
        # evidence views all use this pairing (review 2026-09-07 R2/R3).
        calls: list[dict] = []
        strategy_since = False
        for i, ev in enumerate(ctx.events):
            if ev.event_type == "strategy_change":
                strategy_since = True
                continue
            if ev.event_type == "tool_call":
                calls.append({"idx": i, "call": ev,
                              "result": None, "strategy_since": strategy_since})
                strategy_since = False
        for c in calls:
            c["result"] = paired_result(ctx.events, c["idx"])
        # Second walk: consecutive identical calls with no strategy change —
        # emit only when BOTH outputs are captured and equivalent.
        for a, b in zip(calls, calls[1:]):
            if action_signature(a["call"]) != action_signature(b["call"]):
                continue
            if a["strategy_since"] or b["strategy_since"]:
                continue
            # R2: a repeated mutation (Edit/Write) cannot be flagged on
            # identical acknowledgement text alone — "ok" says nothing about
            # what the file became, so equivalent ack text does not establish
            # that the repeat did no useful work. Resulting-state evidence is
            # not captured here, so mutations never emit a no-new-info claim.
            if is_mutation(b["call"]):
                continue
            ra, rb = a["result"], b["result"]
            if ra is None or rb is None:
                continue  # outputs not captured — the claim is unsupported
            if self._norm(ra.text()) != self._norm(rb.text()):
                continue  # outputs differ — observation with new information
            out.append(self._candidate(
                ctx, b["call"].event_id, kind="behaviour",
                anchor_event_ids=[a["call"].event_id, b["call"].event_id],
                structured_facts=[{
                    "type": "repetition", "signature": list(action_signature(b["call"])),
                    "events": [a["call"].event_id, b["call"].event_id],
                    "result_event_ids": [ra.event_id, rb.event_id],
                    "outputs_compared": True,
                    "outputs_equivalent": True,
                }],
            ))
        return out


class RequiredArtifactAbsent(Detector):
    """A declared task artifact never appears in any observation (§8.5 #3, omission)."""

    name = "required_artifact_absent"
    required_capabilities = {"filesystem": "checkpoint_only"}

    def _run(self, ctx):
        declared = [a.get("path") for a in ctx.doc.get("task", {}).get("artifacts", []) if a.get("path")]
        observed = {e.payload.get("artifact_path") for e in ctx.events if e.event_type == "artifact_observation"}
        submit = ctx.submission()
        closing = submit.event_id if submit else (ctx.events[-1].event_id if ctx.events else "")
        out = []
        for path in declared:
            if path in observed:
                continue
            out.append(self._candidate(
                ctx, path.strip("/").replace("/", "_"), kind="omission",
                anchor_event_ids=[closing],
                structured_facts=[{
                    "type": "absence",
                    "predicate": {"event_type": "artifact_observation", "artifact_path": path},
                    "declared_artifact": path,
                }],
            ))
        return out


class SuccessfulRecoveryViaStrategyChange(Detector):
    """Positive: a qualifying failure recovered via a real strategy change (§8.5 #5)."""

    name = "successful_recovery_via_strategy_change"
    required_capabilities = {"tool_results": "complete"}

    def _run(self, ctx):
        out = []
        for ep in ctx.recoveries:
            if ep.classification != GOOD_RECOVERY:
                continue
            anchor = [ep.failure_event_id] + ([ep.resolution_event_id] if ep.resolution_event_id else [])
            out.append(self._candidate(
                ctx, ep.failure_event_id, kind="recovery", polarity="positive",
                anchor_event_ids=anchor,
                structured_facts=[{
                    "type": "state_transition", "failure_event": ep.failure_event_id,
                    "resolution_event": ep.resolution_event_id,
                    "strategy_changed": ep.strategy_changed, "changed_action": ep.changed_action,
                }],
            ))
        return out


class CompactionRequirementLoss(Detector):
    """Requirement lost across a compaction (context family). Capability-gated.

    AGR-05: registered as a placeholder — the compaction-context evidence
    family is not implemented yet. It reports ``evaluated=False,
    placeholder=True`` so the UI can say "not implemented" instead of the
    "evaluated, no issue found" a stub's empty candidate list would fake.
    """

    name = "compaction_requirement_loss"
    required_capabilities = {"pre_post_compaction_context": "complete"}
    implemented = False

    def _run(self, ctx):
        return []


DETECTORS: list[Detector] = [
    UnresolvedRequirementAtSubmission(),
    TerminalFailureWithFailingChecks(),
    IgnoredToolFailure(),
    RepeatedActionNoNewInfo(),
    RequiredArtifactAbsent(),
    SuccessfulRecoveryViaStrategyChange(),
    CompactionRequirementLoss(),
]


def run_detectors(ctx: DetectorContext, detectors: Optional[list[Detector]] = None) -> list[DetectorResult]:
    detectors = detectors if detectors is not None else DETECTORS
    return [d.run(ctx) for d in detectors]
