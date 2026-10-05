"""Evidence-integrity audit (evaluation strategy §2).

Pure-stdlib: synthetic reviews exercise every classification branch and the
rejected-proposal scope, and a real fixture exercises the store-backed path
(including a genuinely rejected proposal). No browser, no model, no network.
"""

import json
import os

from agr import integrity, read
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _synthetic_review():
    return {
        "run_id": "r",
        "served_reviewer_key": "model:test",
        "review_status": "moments_found",
        "review_moments": [
            {
                "moment_id": "m1",
                "selected": True,
                "validated_facts": [
                    {"type": "requirement_status", "check_id": "C1", "validation": "failed"},
                    {"type": "absence", "declared_artifact": "/x", "validation": "unrecomputable"},
                    {"type": "event_support", "event_id": "evt_1", "validation": "passed"},
                ],
                # A bad QUOTE with otherwise valid prose: prose_references is
                # resolved, so this is a quote mismatch only (PR #97 review).
                "gate_results": {
                    "references": {"status": "dangling",
                                   "dangling": {"anchor_event_ids": ["evt_zzz"]}},
                    "quote_authenticity": "dangling",
                    "explanation_support": "dangling_references",
                    "prose_references": "resolved",
                },
                "rendered_statement": "A claim.",
                "consequence": "A cause.",
                "better_action": "Do less.",
            },
            {
                "moment_id": "m2",
                "selected": True,
                "validated_facts": [
                    {"type": "event_support", "event_id": "evt_2", "validation": "passed"},
                ],
                "gate_results": {"quote_authenticity": "authentic",
                                 "explanation_support": "interpretation_only"},
                "rendered_statement": "Another claim.",
            },
            {
                # An invalid identifier in PROSE (distinct upstream signal), with
                # no quote problem.
                "moment_id": "m4",
                "selected": True,
                "gate_results": {"quote_authenticity": "authentic",
                                 "explanation_support": "dangling_references",
                                 "prose_references": "dangling"},
            },
            {
                # A proposal the gates DROPPED for a failed fact — must not vanish.
                "moment_id": "m3",
                "selected": False,
                "validated_facts": [
                    {"type": "requirement_status", "check_id": "C2", "validation": "failed"},
                ],
                "gate_results": {"references": {"status": "dangling",
                                                "dangling": {"anchor_event_ids": ["evt_yyy"]}}},
            },
        ],
        "review_counts": {
            "rejections": {"information_cutoff": 2, "assumptions_unvalidated": 1},
            "alternatives": {"proposed": 3, "attached": 1,
                             "dropped": {"information_cutoff": 2}},
        },
    }


def test_displayed_claims_are_classified():
    report = integrity.audit_review(_synthetic_review())

    assert report["displayed_moments"] == 3
    assert report["scope"] == "displayed_claims"
    assert report["facts"] == {
        "checked": 4, "passed": 2, "contradicted": 1, "unrecomputable": 1,
        "contradicted_examples": [{"moment_id": "m1", "fact": "requirement_status:C1"}],
        "unrecomputable_examples": [{"moment_id": "m1", "fact": "absence:/x"}],
    }
    assert report["references"]["dangling"] == 1
    assert report["quotes"] == {"mismatched": 1, "authentic": 2, "none": 0,
                                "examples": [{"moment_id": "m1", "status": "dangling"}]}
    assert report["explanation"] == {"evidence_linked": 0, "interpretation_only": 1,
                                     "invalid_prose_references": 1,
                                     "unknown_prose_references": 0}

    # The three-way split the evaluation strategy requires, kept distinct.
    assert report["classification"] == {
        "contradicted_facts": 1,
        "unsupported_factual_assertions": 1,
        "unsupported_interpretations": 1,
    }

    # Coverage: facts actually recomputed (2 passed + 1 contradicted) over that
    # plus 1 unrecomputable claim plus 4 unchecked prose fields.
    assert report["coverage"]["checkable_units"] == 3
    assert report["coverage"]["unchecked_claim_units"] == 1
    assert report["coverage"]["unchecked_narrative_units"] == 4
    assert report["coverage"]["ratio"] == 3 / 8


def test_rejected_proposals_keep_their_own_gate_outcomes():
    """PR #97 review: a reviewer drops a moment FOR a failed fact / dangling
    reference, so those must be reported under the rejected scope rather than
    reading as 'zero contradicted facts'."""
    report = integrity.audit_review(_synthetic_review())

    assert report["rejected_moments"] == 1
    assert report["rejected"]["count"] == 1
    assert report["rejected"]["facts"]["contradicted"] == 1
    assert report["rejected"]["references"]["dangling"] == 1
    assert report["rejected_proposals"]["total"] == 3
    assert report["dropped_alternatives"]["total"] == 2


def test_coverage_does_not_count_unrecomputable_facts_as_checkable():
    """PR #97 review: an unrecomputable fact cannot inflate coverage."""
    review = {"review_moments": [{
        "moment_id": "m", "selected": True,
        "validated_facts": [{"type": "absence", "declared_artifact": "/x",
                             "validation": "unrecomputable"}],
    }]}
    report = integrity.audit_review(review)
    assert report["coverage"]["checkable_units"] == 0
    assert report["coverage"]["unchecked_claim_units"] == 1
    assert report["coverage"]["ratio"] == 0.0


def test_invalid_prose_reference_is_not_a_quote_mismatch():
    """A bad identifier in prose is its own dimension, not a quote mismatch."""
    review = {"review_moments": [{
        "moment_id": "m", "selected": True,
        "gate_results": {"quote_authenticity": "authentic",
                         "explanation_support": "dangling_references",
                         "prose_references": "dangling"},
    }]}
    report = integrity.audit_review(review)
    assert report["quotes"]["mismatched"] == 0
    assert report["explanation"]["invalid_prose_references"] == 1


def test_bad_quote_with_valid_prose_is_not_a_prose_reference():
    """PR #97 review: a bad quote with valid prose counts as a quote mismatch
    only, never also as an invalid prose reference."""
    gate = {"quote_authenticity": "dangling",
            "explanation_support": "dangling_references",
            "prose_references": "resolved"}
    report = integrity.audit_review({"review_moments": [
        {"moment_id": "m", "selected": True, "gate_results": gate}]})
    assert report["quotes"]["mismatched"] == 1
    assert report["explanation"]["invalid_prose_references"] == 0
    assert report["explanation"]["unknown_prose_references"] == 0


def test_bad_quote_and_bad_prose_id_are_both_counted_with_the_new_signal():
    """The distinct signal keeps both failures visible at once."""
    gate = {"quote_authenticity": "dangling",
            "explanation_support": "dangling_references",
            "prose_references": "dangling"}
    report = integrity.audit_review({"review_moments": [
        {"moment_id": "m", "selected": True, "gate_results": gate}]})
    assert report["quotes"]["mismatched"] == 1
    assert report["explanation"]["invalid_prose_references"] == 1
    assert report["explanation"]["unknown_prose_references"] == 0


def test_legacy_bad_quote_and_prose_id_reports_unknown_not_zero():
    """PR #97 review follow-up: an older review with both failures is ambiguous,
    so the prose-reference result is unknown — never a verified zero."""
    gate = {"quote_authenticity": "dangling",
            "explanation_support": "dangling_references"}  # no prose_references
    report = integrity.audit_review({"review_moments": [
        {"moment_id": "m", "selected": True, "gate_results": gate}]})
    assert report["quotes"]["mismatched"] == 1
    assert report["explanation"]["invalid_prose_references"] == 0
    assert report["explanation"]["unknown_prose_references"] == 1


def test_fallback_envelope_is_reachable_when_read_returns_empty_list():
    """PR #97 review: ``read.get_review`` turns a missing envelope into ``[]``, so
    the fallback must trigger on an empty (falsy) envelope with served moments."""
    review = {"review_moments": [],
              "moments": [{"moment_id": "m", "facts": [{"type": "absence"}]}]}
    report = integrity.audit_review(review)
    assert report["source"] == "served_moments_fallback_no_envelope"
    assert report["displayed_moments"] == 1
    assert report["rejected_moments"] == 0
    assert report["facts"]["checked"] == 0
    assert report["coverage"]["unchecked_claim_units"] == 1
    assert report["coverage"]["ratio"] == 0.0


def test_audit_run_over_a_real_fixture(tmp_path):
    store = Store(str(tmp_path / "store"))
    analyze(_load("tool_failure_recovery.atif.json"), store)

    report = integrity.audit_run(store, "solve_task__recovered")
    assert report["run_id"] == "solve_task__recovered"
    assert report["source"] == "review_moments"
    assert report["displayed_moments"] >= 1
    assert report["facts"]["checked"] >= 1
    assert report["classification"]["contradicted_facts"] == 0
    assert report["references"]["dangling"] == 0
    ratio = report["coverage"]["ratio"]
    assert ratio is not None and 0.0 <= ratio <= 1.0

    text = integrity.format_text(report)
    assert "Evidence-integrity audit" in text
    assert "checking coverage" in text


def test_audit_run_reports_a_real_rejected_proposal(tmp_path):
    """End-to-end: a scripted model proposal with a non-existent check is dropped
    by Stage G, and the audit reports it under the rejected scope."""
    store = Store(str(tmp_path / "store"))
    analyze(_load("tool_failure_recovery.atif.json"), store)
    payload = {"moments": [{
        "candidate_id": "sem_bad", "anchor_event_ids": ["evt_004"],
        "kind": "behaviour", "polarity": "negative",
        "structured_facts": [{"type": "requirement_status", "check_id": "NOPE",
                              "status_at_submission": "failed"}],
        "behaviour_tags": ["poor_query"],
    }]}
    analyze(_load("tool_failure_recovery.atif.json"), store,
            reviewer=ScriptedReviewer(payload, source="model:test"))

    report = integrity.audit_run(store, "solve_task__recovered")
    assert report["rejected_moments"] >= 1
    assert report["rejected"]["facts"]["contradicted"] >= 1
    # The dropped proposal is not counted among the displayed claims.
    assert report["classification"]["contradicted_facts"] == 0
