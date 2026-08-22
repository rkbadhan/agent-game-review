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
from ._util import action_signature
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

    def _run(self, ctx: DetectorContext) -> list[Candidate]:
        raise NotImplementedError

    def run(self, ctx: DetectorContext) -> DetectorResult:
        unmet = [c for c, m in self.required_capabilities.items() if not ctx.profile.meets(c, m)]
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
    """A required contract item is still failing when the run submits (§8.5 #4)."""

    name = "unresolved_requirement_at_submission"
    required_capabilities = {"messages": "complete", "tool_results": "complete"}

    def _run(self, ctx):
        submit = ctx.submission()
        if submit is None:
            return []
        out = []
        for check in ctx.checks:
            if check.status != "failed":
                continue
            out.append(self._candidate(
                ctx, check.check_id, kind="omission", anchor_event_ids=[submit.event_id],
                affected_checks=[check.check_id], affected_contract_items=list(check.contract_item_ids),
                structured_facts=[{
                    "type": "requirement_status", "check_id": check.check_id,
                    "status_at_submission": "failed",
                    "expected": check.expected, "observed": check.observed,
                }],
            ))
        return out


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
    """Same action repeated with no strategy change in between (§8.5 #2)."""

    name = "repeated_action_no_new_info"
    required_capabilities = {"tool_calls": "complete"}

    def _run(self, ctx):
        out = []
        last_sig = None
        last_call: DerivedEvent | None = None
        strategy_since = False
        for ev in ctx.events:
            if ev.event_type == "strategy_change":
                strategy_since = True
                continue
            if ev.event_type != "tool_call":
                continue
            sig = action_signature(ev)
            if sig == last_sig and not strategy_since:
                out.append(self._candidate(
                    ctx, ev.event_id, kind="behaviour",
                    anchor_event_ids=[last_call.event_id, ev.event_id],
                    structured_facts=[{
                        "type": "repetition", "signature": list(sig),
                        "events": [last_call.event_id, ev.event_id],
                    }],
                ))
            last_sig, last_call, strategy_since = sig, ev, False
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
    """Requirement lost across a compaction (context family). Capability-gated."""

    name = "compaction_requirement_loss"
    required_capabilities = {"pre_post_compaction_context": "complete"}

    def _run(self, ctx):
        return []


DETECTORS: list[Detector] = [
    UnresolvedRequirementAtSubmission(),
    IgnoredToolFailure(),
    RepeatedActionNoNewInfo(),
    RequiredArtifactAbsent(),
    SuccessfulRecoveryViaStrategyChange(),
    CompactionRequirementLoss(),
]


def run_detectors(ctx: DetectorContext, detectors: Optional[list[Detector]] = None) -> list[DetectorResult]:
    detectors = detectors if detectors is not None else DETECTORS
    return [d.run(ctx) for d in detectors]
