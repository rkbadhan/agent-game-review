"""Evidence-integrity audit — what the deterministic checks did and did not cover.

The guided review is model-written and grounding-checked: structured facts are
recomputed, quoted spans are matched against their named event, and identifiers
a moment cites must exist. Those checks already run inside the reviewer
(``agr.reviewer.run_reviewer`` and its ``gate_results``); this module
*consolidates* their outcome for one served review into a single report and —
the part that matters — states how much of the review they actually cover.

The honest rule this module exists to enforce:

    zero mechanical failures is NOT zero fabricated facts.

A fact can recompute and a quote can match while the sentence built on top of
them still overstates. Free-form prose is therefore reported as
``unchecked_narrative_units``, never as verified. The report names what each
check establishes and what it does not.

Two scopes, always kept apart (PR #97 review):

* ``displayed`` — the moments a reader sees. ``facts`` / ``references`` /
  ``quotes`` / ``explanation`` / ``coverage`` / ``classification`` describe only
  these, because those are the claims a reader is asked to trust.
* ``rejected`` — proposals the gates dropped. A reviewer rejects a moment *for*
  a failed fact or a dangling reference, so a report that counted only displayed
  moments could read "zero contradicted facts" right after discarding several
  for exactly that reason. The rejected proposals' own gate outcomes are reported
  separately here.

Problem classification (mechanical signal, not a substitute for the human audit
of complete reviews — evaluation strategy EV-3):

* **contradicted fact** — a structured fact whose recomputation failed.
* **unsupported factual assertion** — a structured fact that could not be
  recomputed, so no mechanical check supports it.
* **unsupported interpretation** — an explanation whose prose goes beyond its
  quoted spans (``explanation_support == "interpretation_only"``).

Pure stdlib: no third-party dependency, no model call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - duck-typed, like the rest of the core
    from .store import Store

# Fact validation statuses written by ``agr.reviewer.validate_facts``.
_FACT_PASSED = "passed"
_FACT_FAILED = "failed"
_FACT_UNRECOMPUTABLE = "unrecomputable"

# ``agr.reviewer._quote_authenticity`` returns one of these three.
_QUOTE_DANGLING = "dangling"
_QUOTE_AUTHENTIC = "authentic"
_QUOTE_NONE = "none"

# ``agr.reviewer._explanation_support`` returns one of these.
_EXPLANATION_DANGLING_REFS = "dangling_references"
_EXPLANATION_EVIDENCE_LINKED = "evidence_linked"
_EXPLANATION_INTERPRETATION_ONLY = "interpretation_only"

# ``agr.reviewer._prose_reference_validity`` returns one of these. Older stored
# reviews predate the field and fall back to the conflated explanation_support.
_PROSE_REFERENCES_DANGLING = "dangling"
_PROSE_REFERENCES_RESOLVED = "resolved"

# Free-form prose fields a moment can carry. These are narrative: the grounding
# layer does not verify their truth, so they are counted as UNCHECKED, never as
# passing anything.
_PROSE_FIELDS = ("rendered_statement", "consequence", "better_action")

# Cap on example rows kept per dimension, so one pathological review cannot
# produce an unbounded report.
_MAX_EXAMPLES = 10

_FALLBACK_SOURCE = "served_moments_fallback_no_envelope"

_COVERAGE_DEFINITION = (
    "checkable units are structured facts the grounding layer actually "
    "recomputed (passed or contradicted); unchecked claim units are facts it "
    "could not recompute (or that were never checked) and narrative prose "
    "fields (rendered statement, consequence, suggested action). The ratio "
    "states what share of these assertion-like units the automated checks can "
    "test — it is not a claim about every sentence a review displays."
)

_LIMITS = [
    "Event-reference validation establishes that a cited event exists, not that it supports the interpretation.",
    "Exact-quote matching establishes that a quote occurs in its event, not that the surrounding claim is true.",
    "Structured-fact recomputation establishes a supported field or calculation, not that every statement is grounded.",
    "Narrative prose is reported as unchecked; zero mechanical failures does not mean zero fabricated facts.",
    "Rejected proposals are reported separately from displayed claims; a rejected moment's gate failure never becomes a displayed claim's coverage.",
    "Reviews stored before the distinct prose_references signal report an unknown prose-reference result where a failed quote and a failed prose id are indistinguishable; it is never counted as a verified zero.",
    "This is a mechanical signal. Unsupported facts and interpretations require the human audit of complete reviews.",
]


def _review_moments(review: dict) -> tuple[list[dict], str]:
    """Every reviewer moment in the review, and where the rows came from.

    The envelope (``review_moments``) is the only view that carries
    ``gate_results`` / ``validated_facts``. ``read.get_review`` turns a *missing*
    envelope into ``[]``, an empty list indistinguishable from a valid empty
    review — so the legacy detector-candidate projection (``moments``) is used
    only when there are no envelope rows but the read model still served moments,
    and the report says so rather than claiming the review was checked clean.
    """
    envelope = review.get("review_moments")
    if envelope:
        return list(envelope), "review_moments"
    legacy = review.get("moments")
    if legacy:
        return list(legacy), _FALLBACK_SOURCE
    return [], "review_moments_empty"


def _fact_label(fact: dict) -> str:
    ftype = fact.get("type") or "?"
    subject = (fact.get("check_id") or fact.get("declared_artifact")
               or fact.get("event_id") or fact.get("signature") or "")
    if isinstance(subject, (list, tuple)):
        subject = ",".join(str(s) for s in subject)
    return f"{ftype}:{subject}" if subject else str(ftype)


def _tally(moments: list[dict]) -> dict:
    """Tally the mechanical checks for one scope (displayed or rejected)."""
    facts = {
        "checked": 0, "passed": 0, "contradicted": 0, "unrecomputable": 0,
        "contradicted_examples": [], "unrecomputable_examples": [],
    }
    references = {"dangling": 0, "examples": []}
    quotes = {"mismatched": 0, "authentic": 0, "none": 0, "examples": []}
    explanation = {"evidence_linked": 0, "interpretation_only": 0,
                   "invalid_prose_references": 0, "unknown_prose_references": 0}
    unchecked_claims = 0
    unchecked_narrative = 0

    for m in moments:
        gate = m.get("gate_results") or {}
        mid = m.get("moment_id") or m.get("candidate_id")

        for fact in (m.get("validated_facts") or m.get("facts") or []):
            status = fact.get("validation")
            if status is None:
                # A structured claim present but not mechanically checked in this
                # view (legacy fallback). Count it as unchecked — never as
                # passing, and never dropped, so coverage cannot look better than
                # it is by simply ignoring unverifiable claims.
                unchecked_claims += 1
                continue
            facts["checked"] += 1
            if status == _FACT_PASSED:
                facts["passed"] += 1
            elif status == _FACT_FAILED:
                facts["contradicted"] += 1
                if len(facts["contradicted_examples"]) < _MAX_EXAMPLES:
                    facts["contradicted_examples"].append({"moment_id": mid, "fact": _fact_label(fact)})
            elif status == _FACT_UNRECOMPUTABLE:
                # A fact no check could recompute is NOT coverage — it is an
                # unchecked claim (PR #97 review).
                facts["unrecomputable"] += 1
                unchecked_claims += 1
                if len(facts["unrecomputable_examples"]) < _MAX_EXAMPLES:
                    facts["unrecomputable_examples"].append({"moment_id": mid, "fact": _fact_label(fact)})

        refs = gate.get("references")
        if isinstance(refs, dict) and refs.get("status") == "dangling":
            references["dangling"] += 1
            if len(references["examples"]) < _MAX_EXAMPLES:
                references["examples"].append({"moment_id": mid, "dangling": refs.get("dangling")})

        # Quote authenticity is its own dimension (``dangling`` = a quoted span
        # did not occur in its event). An invalid identifier in PROSE is a
        # separate signal (``explanation_support == dangling_references``) and is
        # never folded into the quote count (PR #97 review).
        quote_status = gate.get("quote_authenticity")
        if quote_status == _QUOTE_DANGLING:
            quotes["mismatched"] += 1
            if len(quotes["examples"]) < _MAX_EXAMPLES:
                quotes["examples"].append({"moment_id": mid, "status": "dangling"})
        elif quote_status == _QUOTE_AUTHENTIC:
            quotes["authentic"] += 1
        elif quote_status == _QUOTE_NONE:
            quotes["none"] += 1

        explanation_status = gate.get("explanation_support")
        if explanation_status == _EXPLANATION_EVIDENCE_LINKED:
            explanation["evidence_linked"] += 1
        elif explanation_status == _EXPLANATION_INTERPRETATION_ONLY:
            explanation["interpretation_only"] += 1
        # Invalid prose references get their own count. Prefer the distinct
        # upstream signal. Older reviews that predate it carry only the
        # conflated ``dangling_references`` status: when the quote was NOT the
        # cause, the prose id must be; when the quote WAS dangling, the prose-id
        # result is genuinely UNKNOWABLE (it could be quote-only, prose-only or
        # both), so it is reported as unknown — never as a verified zero
        # (PR #97 review).
        prose_refs = gate.get("prose_references")
        if prose_refs is not None:
            if prose_refs == _PROSE_REFERENCES_DANGLING:
                explanation["invalid_prose_references"] += 1
        elif explanation_status == _EXPLANATION_DANGLING_REFS:
            if quote_status == _QUOTE_DANGLING:
                explanation["unknown_prose_references"] += 1
            else:
                explanation["invalid_prose_references"] += 1

        for field in _PROSE_FIELDS:
            text = m.get(field)
            if isinstance(text, str) and text.strip():
                unchecked_narrative += 1

    checkable = facts["passed"] + facts["contradicted"]
    denominator = checkable + unchecked_claims + unchecked_narrative
    coverage = {
        "checkable_units": checkable,
        "unchecked_claim_units": unchecked_claims,
        "unchecked_narrative_units": unchecked_narrative,
        "ratio": (checkable / denominator) if denominator else None,
        "definition": _COVERAGE_DEFINITION,
    }
    return {
        "facts": facts,
        "references": references,
        "quotes": quotes,
        "explanation": explanation,
        "coverage": coverage,
    }


def audit_review(review: dict) -> dict:
    """Consolidate the mechanical checks for one served review (pure function).

    ``review`` is the dict from :func:`agr.read.get_review`. Returns a report
    dict; see the module docstring for the meaning of each field.
    """
    all_moments, source = _review_moments(review)
    if source == _FALLBACK_SOURCE:
        # Legacy projection: no envelope, so nothing was mechanically checked and
        # every row is treated as displayed (they carry no ``selected`` flag).
        displayed, rejected = list(all_moments), []
    else:
        displayed = [m for m in all_moments if m.get("selected")]
        rejected = [m for m in all_moments if not m.get("selected")]

    shown = _tally(displayed)
    dropped = _tally(rejected)
    interpretations = shown["explanation"]["interpretation_only"]

    counts = review.get("review_counts") or {}
    rejections = counts.get("rejections") or {}
    dropped_alternatives = ((counts.get("alternatives") or {}).get("dropped")) or {}

    return {
        "run_id": review.get("run_id"),
        "reviewer_key": review.get("served_reviewer_key") or review.get("reviewer_key"),
        "review_status": review.get("review_status"),
        "source": source,
        "scope": "displayed_claims",
        "displayed_moments": len(displayed),
        "rejected_moments": len(rejected),
        # Displayed-claim checks. These are the claims a reader is asked to trust.
        "facts": shown["facts"],
        "references": shown["references"],
        "quotes": shown["quotes"],
        "explanation": shown["explanation"],
        "coverage": shown["coverage"],
        "classification": {
            "contradicted_facts": shown["facts"]["contradicted"],
            "unsupported_factual_assertions": shown["facts"]["unrecomputable"],
            "unsupported_interpretations": interpretations,
        },
        # Proposals the gates dropped, with their OWN gate outcomes, so discarding
        # a moment for a failed fact cannot show up as "zero contradicted facts".
        "rejected": {
            "count": len(rejected),
            "facts": dropped["facts"],
            "references": dropped["references"],
            "quotes": dropped["quotes"],
            "explanation": dropped["explanation"],
        },
        "rejected_proposals": {"by_reason": dict(rejections), "total": sum(rejections.values())},
        "dropped_alternatives": {"by_reason": dict(dropped_alternatives),
                                 "total": sum(dropped_alternatives.values())},
        "limits": list(_LIMITS),
    }


def audit_run(store: "Store", run_id: str, reviewer_key: Optional[str] = None) -> dict:
    """Audit the served review for one run in ``store`` (independent capture read)."""
    from . import read

    report = audit_review(read.get_review(store, run_id, reviewer_key))
    # ``get_review`` keys the served review by capture, not by run id, so the
    # caller's run id is authoritative here.
    report["run_id"] = run_id
    return report


def format_text(report: dict) -> str:
    """Render a report as a short, readable block for the CLI."""
    lines = []
    lines.append(f"Evidence-integrity audit — {report.get('run_id')}")
    lines.append(f"  reviewer            {report.get('reviewer_key')}")
    lines.append(f"  review status       {report.get('review_status')}")
    lines.append(f"  displayed moments   {report.get('displayed_moments')}")
    facts = report.get("facts", {})
    lines.append(f"  facts recomputed    {facts.get('passed', 0)}/{facts.get('checked', 0)} passed"
                 f" · {facts.get('contradicted', 0)} contradicted"
                 f" · {facts.get('unrecomputable', 0)} unrecomputable")
    refs = report.get("references", {})
    lines.append(f"  dangling references {refs.get('dangling', 0)}")
    quotes = report.get("quotes", {})
    expl = report.get("explanation", {})
    lines.append(f"  quotes              {quotes.get('mismatched', 0)} mismatched"
                 f" · {quotes.get('authentic', 0)} authentic"
                 f" · {quotes.get('none', 0)} none")
    unknown_refs = expl.get("unknown_prose_references", 0)
    lines.append(f"  explanations        {expl.get('evidence_linked', 0)} evidence-linked"
                 f" · {expl.get('interpretation_only', 0)} interpretation-only"
                 f" · {expl.get('invalid_prose_references', 0)} invalid prose references"
                 + (f" · {unknown_refs} unknown prose references" if unknown_refs else ""))
    cov = report.get("coverage", {})
    ratio = cov.get("ratio")
    ratio_s = "n/a" if ratio is None else f"{ratio:.0%}"
    lines.append(f"  checking coverage   {ratio_s} "
                 f"({cov.get('checkable_units', 0)} checkable / "
                 f"{cov.get('unchecked_claim_units', 0)} unchecked claims / "
                 f"{cov.get('unchecked_narrative_units', 0)} unchecked narrative units)")
    rejected = report.get("rejected", {})
    if rejected.get("count"):
        rfacts = rejected.get("facts", {})
        rrefs = rejected.get("references", {})
        lines.append(f"  rejected moments    {rejected['count']}"
                     f" (facts: {rfacts.get('contradicted', 0)} contradicted,"
                     f" {rfacts.get('unrecomputable', 0)} unrecomputable;"
                     f" references: {rrefs.get('dangling', 0)} dangling)")
    if report.get("rejected_proposals", {}).get("total"):
        by = ", ".join(f"{k}={v}" for k, v in sorted(report["rejected_proposals"].get("by_reason", {}).items()))
        lines.append(f"  rejected proposals  {report['rejected_proposals']['total']} ({by})")
    cls = report.get("classification", {})
    lines.append(f"  classification      contradicted facts {cls.get('contradicted_facts', 0)}"
                 f" · unsupported factual assertions {cls.get('unsupported_factual_assertions', 0)}"
                 f" · unsupported interpretations {cls.get('unsupported_interpretations', 0)}")
    if report.get("source") == _FALLBACK_SOURCE:
        lines.append("  NOTE: no reviewer envelope for this run — no mechanical checks were recorded.")
    lines.append("  limits:")
    for limit in report.get("limits", []):
        lines.append(f"    - {limit}")
    return "\n".join(lines)
