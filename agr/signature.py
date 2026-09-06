"""Task Ability Signature (spec §4.3).

Assembles the per-run signature table from mechanically supported evidence:
verifier checks, opportunities, and recovery episodes. Every row is grounded in
deterministic facts.

The cardinal rule (spec §4.3): an ability with no qualifying opportunity is
"Not measured" and must never be rendered as a positive or negative judgment.
``Not reviewed`` (analysis not run) is a separate state used only by
model-enriched signatures and does not appear in this deterministic core.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Result / interpretation vocabularies kept aligned with the spec example.
RESULT_SUCCESS = "Successful"
RESULT_FAILED = "Failed"
RESULT_NOT_OBSERVED = "Not observed"

INTERP_POSITIVE = "Positive evidence"
INTERP_NEGATIVE = "Negative evidence in this run"
INTERP_NOT_MEASURED = "Not measured"


@dataclass
class SignatureRow:
    ability: str
    observed_behaviour: str
    result: str
    interpretation: str
    measured: bool
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ability": self.ability,
            "observed_behaviour": self.observed_behaviour,
            "result": self.result,
            "interpretation": self.interpretation,
            "measured": self.measured,
            "evidence": self.evidence,
        }


def _requirement_rows(checks) -> list[SignatureRow]:
    rows: list[SignatureRow] = []
    for c in checks:
        if c.status == "passed":
            result, interp = RESULT_SUCCESS, INTERP_POSITIVE
        elif c.status == "failed":
            result, interp = RESULT_FAILED, INTERP_NEGATIVE
        else:
            continue  # skipped/unknown checks are not signature evidence
        observed = c.name
        if c.status == "failed" and c.expected is not None:
            observed = f"expected {c.expected}, observed {c.observed}"
        rows.append(SignatureRow(
            ability=c.name,
            observed_behaviour=observed,
            result=result,
            interpretation=interp,
            measured=True,
            evidence=[c.check_id] + list(c.contract_item_ids),
        ))
    return rows


def _recovery_row(analysis) -> SignatureRow:
    from .recovery import GOOD_RECOVERY, UNCHANGED_RETRY

    has_failure = any(o.trigger == "tool_failure" for o in analysis.opportunities)
    if not has_failure:
        return SignatureRow(
            ability="Recover from tool failure",
            observed_behaviour="No qualifying failure occurred",
            result=RESULT_NOT_OBSERVED,
            interpretation=INTERP_NOT_MEASURED,
            measured=False,
        )
    good = any(ep.classification == GOOD_RECOVERY for ep in analysis.recoveries)
    unchanged = any(ep.classification == UNCHANGED_RETRY for ep in analysis.recoveries)
    if good:
        observed = "Strategy change resolved the failure"
        result, interp = RESULT_SUCCESS, INTERP_POSITIVE
    elif unchanged:
        # AGR-05: a successful unchanged retry IS a recovery — a strategy
        # change is not a universal requirement for sensible recovery. The
        # wording stays honest about what resolved it.
        observed = "Same action retried unchanged and succeeded — resolved without a strategy change"
        result, interp = RESULT_SUCCESS, INTERP_POSITIVE
    else:
        observed = "Failure not resolved"
        result, interp = RESULT_FAILED, INTERP_NEGATIVE
    return SignatureRow(
        ability="Recover from tool failure",
        observed_behaviour=observed,
        result=result,
        interpretation=interp,
        measured=True,
        evidence=[ep.failure_event_id for ep in analysis.recoveries],
    )


def _verification_row(analysis) -> SignatureRow:
    verify_opp = next((o for o in analysis.opportunities if o.trigger == "required_artifact_exists"), None)
    if verify_opp is None:
        return SignatureRow(
            ability="Verify before submission",
            observed_behaviour="No artifact existed to verify before submission",
            result=RESULT_NOT_OBSERVED,
            interpretation=INTERP_NOT_MEASURED,
            measured=False,
        )
    # A verification action = a verifier_check event, or a tool call re-reading
    # the artifact, occurring within the opportunity window.
    ids = [e.event_id for e in analysis.events]
    start, end = ids.index(verify_opp.start_event_id), ids.index(verify_opp.end_event_id)
    window = analysis.events[start:end + 1]
    verified = any(
        e.event_type == "verifier_check"
        or (e.event_type in ("tool_call", "tool_result") and "check" in e.text().lower())
        for e in window
    )
    any_failed = any(c.status == "failed" for c in analysis.checks)
    if verified:
        return SignatureRow(
            ability="Verify before submission",
            observed_behaviour="Verification action taken before submission",
            result=RESULT_SUCCESS,
            interpretation=INTERP_POSITIVE,
            measured=True,
            evidence=[verify_opp.start_event_id, verify_opp.end_event_id],
        )
    # AGR-05: verification is scored independently of the final outcome. A
    # passing run with no observed verification action provides NO positive
    # evidence of verification — success is not verification.
    return SignatureRow(
        ability="Verify before submission",
        observed_behaviour="No verification action before submission",
        result=RESULT_NOT_OBSERVED,
        interpretation="No positive evidence — a passing outcome is not verification evidence"
        if not any_failed else INTERP_NEGATIVE,
        measured=True,
        evidence=[verify_opp.start_event_id, verify_opp.end_event_id],
    )


def build_signature(analysis) -> list[SignatureRow]:
    rows = _requirement_rows(analysis.checks)
    rows.append(_recovery_row(analysis))
    rows.append(_verification_row(analysis))
    return rows
