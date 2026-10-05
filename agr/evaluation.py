"""Human audit sampling and scoped evaluation reporting (EV-3 / EV-4).

This module prepares reviewer packets and summarizes supplied human judgments.
It never manufactures judgments. The sample key must be kept by the evaluation
owner; share ``blind/`` first, then release ``revealed/`` only after blind-phase
annotations have been sealed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import read

RUBRIC = {
    "evidence_support": "Does the cited evidence support the explanation? (1-5)",
    "cause_consequence": "Are cause and consequence supported by the trace? (1-5)",
    "alternative_quality": "Is the suggested alternative reasonable using information available then? (1-5)",
    "abstention_quality": "Are positive moments and abstentions justified? (1-5 or NA)",
    "usefulness": "Does the review help a reader understand the run? (1-5)",
    "unsupported_factual_assertions": "Count unsupported factual assertions.",
    "unsupported_interpretations": "Count unsupported interpretations.",
    "assessed_interpretations": "Count all interpretations assessed.",
    "assessed_assertions": "Count all factual assertions assessed.",
    "moment_precision_numerator": "Number of reviewed moments judged relevant and supported.",
    "moment_precision_denominator": "Number of reviewed moments assessed.",
    "notes": "Brief evidence-based rationale; leave empty when none.",
}
RUBRIC_GUIDANCE = {
    "scale": "For 1-5 questions: 1=unsupported or misleading, 3=partly supported/useful with material gaps, 5=strongly supported and useful; use 2/4 between anchors.",
    "abstention_quality": "Use NA when there is no positive moment or abstention decision to assess.",
    "unsupported_factual_assertions": "Count factual claims not supported by the trace or verified checks; do not count interpretations here.",
    "unsupported_interpretations": "Count causal or evaluative conclusions that exceed the trace evidence.",
    "assessed_interpretations": "Count all interpretations assessed, including supported ones.",
    "moment_precision": "Count a moment in the numerator only when its location and stated interpretation are both relevant and supported.",
    "double_review": "Review independently before discussing; disagreements remain visible until separate adjudication.",
}
BLIND_ANNOTATION_FIELDS = {
    "observed_outcome": "Reviewer assessment from source evidence: success, failure, or unclear.",
    "no_decisive_moment": "Whether the source supports no decisive moment.",
    "independent_moments": "List source-step ids, polarity, criticality, and rationale for independently identified moments.",
    "task_verifier_concerns": "Concerns about whether the task outcome is adequately verified.",
    "notes": "Source-grounded notes recorded before AGR's findings are revealed.",
}


def _anon(case_id: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}\0{case_id}".encode("utf-8")).hexdigest()[:12]


def create_audit_pack(store, output_dir: str, *, sample_size: int,
                      double_review_fraction: float = 0.25, seed: int = 1,
                      reviewer_key: str | None = None) -> dict:
    """Select a reproducible sample, stratified by observed outcome/status."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if not 0 <= double_review_fraction <= 1:
        raise ValueError("double_review_fraction must be between 0 and 1")
    rows = read.list_runs(store)
    eligible = []
    for row in rows:
        rid = row["run_id"]
        try:
            review = read.get_review(store, rid, reviewer_key=reviewer_key)
            source = read.get_source(store, rid)["source"]
        except (KeyError, OSError, ValueError):
            continue
        if not review.get("review_status_success"):
            continue
        eligible.append((rid, review, source, row))
    if not eligible:
        raise ValueError("no successfully reviewed runs are available for audit")
    rng = random.Random(seed)
    strata: dict[str, list] = defaultdict(list)
    for item in eligible:
        review, source = item[1], item[2]
        outcome = (source.get("verifier") or {}).get("status", "unverified")
        review_kind = "no_moment" if not review.get("moments") else "moments_found"
        recovery = "recovery" if review.get("recoveries") else "no_recovery"
        bucket = f"{outcome}:{review.get('review_status', 'unknown')}:{review_kind}:{recovery}"
        strata[bucket].append(item)
    for members in strata.values():
        rng.shuffle(members)
    keys = sorted(strata)
    target_n = min(sample_size, len(eligible))
    quotas = {key: target_n * len(strata[key]) / len(eligible) for key in keys}
    allocations = {key: int(quotas[key]) for key in keys}
    remainder = target_n - sum(allocations.values())
    for key in sorted(keys, key=lambda k: (-(quotas[k] - allocations[k]), k)):
        if remainder == 0:
            break
        if allocations[key] < len(strata[key]):
            allocations[key] += 1
            remainder -= 1
    selected = []
    for key in keys:
        selected.extend(strata[key][:allocations[key]])
    rng.shuffle(selected)
    double_count = min(len(selected), round(len(selected) * double_review_fraction))
    double_ids = {rid for rid, *_ in selected[:double_count]}
    seed_key = str(seed)
    blind_cases, revealed_cases, mapping = [], [], {}
    for rid, review, source, _row in selected:
        blind_id = _anon(rid, seed_key)
        mapping[blind_id] = rid
        blind_cases.append({"case_id": blind_id, "source": source})
        revealed_cases.append({
            "case_id": blind_id,
            "source": source,
            "agr_review": {
                key: review.get(key) for key in (
                    "review_status", "moments", "review_moments", "evidence_slices",
                    "coach_summary", "opportunities", "recoveries", "review_counts",
                ) if key in review
            },
            "double_review": rid in double_ids,
        })
    os.makedirs(os.path.join(output_dir, "blind"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "revealed"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "key"), exist_ok=True)
    metadata = {
        "audit_pack_version": "0.1", "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed, "eligible_count": len(eligible), "selected_count": len(selected),
        "strata": {k: {"population": len(members), "selected": allocations[k]} for k, members in strata.items()},
        "double_review_count": double_count, "reviewer_key": reviewer_key,
        "rubric": RUBRIC,
        "rubric_guidance": RUBRIC_GUIDANCE,
        "blind_annotation_fields": BLIND_ANNOTATION_FIELDS,
        "instructions": "Complete blind annotations first. Seal them before sharing revealed/cases.json.",
    }
    _write(os.path.join(output_dir, "blind", "cases.json"), {"cases": blind_cases})
    _write(os.path.join(output_dir, "revealed", "cases.json"), {"cases": revealed_cases})
    _write(os.path.join(output_dir, "key", "case-map.json"), mapping)
    _write(os.path.join(output_dir, "manifest.json"), metadata)
    _write(os.path.join(output_dir, "blind", "annotations-template.json"), {
        "instructions": "Complete from blind/cases.json before seeing revealed/cases.json.",
        "annotations": [{"case_id": c["case_id"], "reviewer_id": "",
                          "observed_outcome": None, "no_decisive_moment": None,
                          "independent_moments": [], "task_verifier_concerns": [], "notes": ""}
                         for c in blind_cases],
    })
    _write(os.path.join(output_dir, "revealed", "annotations-template.json"), {
        "instructions": "Use only after blind annotations are sealed; assess AGR's complete review.",
        "annotations": [{"case_id": c["case_id"], "reviewer_id": "",
                          **{k: None for k in RUBRIC}}
                         for c in blind_cases],
    })
    return metadata


def _write(path: str, value: Any) -> None:
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _cluster_interval(rows: list[dict], value_key: str, cluster_key: str = "task_id",
                      *, seed: int = 17, samples: int = 2000) -> list[float] | None:
    """Percentile bootstrap, resampling task clusters rather than run rows."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if isinstance(value, bool):
            grouped[str(row.get(cluster_key) or row.get("run_id"))].append(float(value))
    if not grouped:
        return None
    clusters = list(grouped.values())
    if len(clusters) == 1:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        picked = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        vals = [v for group in picked for v in group]
        estimates.append(sum(vals) / len(vals))
    estimates.sort()
    return [round(estimates[int(.025 * (samples - 1))], 4),
            round(estimates[int(.975 * (samples - 1))], 4)]


def _cluster_ratio_interval(rows: list[dict], numerator: str, denominator: str,
                            cluster_key: str = "task_id", *, seed: int = 19,
                            samples: int = 2000) -> list[float] | None:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        n, d = row.get(numerator), row.get(denominator)
        if isinstance(n, (int, float)) and isinstance(d, (int, float)):
            grouped[str(row.get(cluster_key) or row.get("run_id"))].append((int(n), int(d)))
    if len(grouped) < 2:
        return None
    clusters = list(grouped.values())
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        picked = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        n_total = sum(n for cluster in picked for n, _ in cluster)
        d_total = sum(d for cluster in picked for _, d in cluster)
        if d_total:
            estimates.append(n_total / d_total)
    if not estimates:
        return None
    estimates.sort()
    return [round(estimates[int(.025 * (len(estimates) - 1))], 4),
            round(estimates[int(.975 * (len(estimates) - 1))], 4)]


def _benchmark_claim_eligibility(report: dict) -> tuple[str, str | None]:
    manifest = report.get("run_manifest")
    selection = manifest.get("selection") if isinstance(manifest, dict) else None
    frozen_inputs = bool(
        isinstance(manifest, dict)
        and manifest.get("run_manifest_version")
        and isinstance(selection, dict)
        and selection.get("records_sha256")
    )
    if not frozen_inputs:
        return "exploratory_unfrozen", "no frozen run manifest"
    reviewer = manifest.get("reviewer") or {}
    if reviewer.get("kind") == "model":
        protocol = manifest.get("evaluation_protocol") or {}
        required = ("training_cutoff", "partition_policy")
        missing = [field for field in required
                   if not isinstance(protocol.get(field), str) or not protocol[field].strip()]
        if missing:
            return "exploratory_protocol_incomplete", "missing required protocol field(s): " + ", ".join(missing)
        unknown_markers = {"unknown", "not known", "not assessed", "unavailable", "n/a", "na", "none"}
        if protocol["training_cutoff"].strip().lower() in unknown_markers:
            return "exploratory_contamination_unassessed", "model training cutoff is unknown; contamination cannot be assessed"
        if protocol["partition_policy"].strip().lower() in unknown_markers:
            return "exploratory_partition_unassessed", "task partition policy is unknown"
    return "publishable", None


def build_evaluation_report(benchmark_reports: list[dict], audits: list[dict], *,
                            integrity_reports: list[dict] | None = None,
                            thresholds: dict[str, float] | None = None) -> dict:
    """Combine benchmark and human-audit evidence into scoped, denominator-first claims."""
    thresholds = thresholds or {
        "moment_precision": .8, "unsupported_factual_assertions": 0,
        "unsupported_interpretation_rate": .1,
    }
    submitted = [a for report in audits for a in report.get("annotations", [])]
    required_counts = ("unsupported_factual_assertions", "unsupported_interpretations",
                       "assessed_assertions", "assessed_interpretations",
                       "moment_precision_numerator", "moment_precision_denominator")
    rows = []
    seen_reviewers = set()
    for row in submitted:
        complete = bool(row.get("reviewer_id")) and all(
            isinstance(row.get(key), int) and not isinstance(row.get(key), bool)
            and row[key] >= 0 for key in required_counts
        )
        if not complete:
            continue
        if row["unsupported_factual_assertions"] > row["assessed_assertions"]:
            raise ValueError(f"{row.get('case_id')}: unsupported factual count exceeds assessed assertions")
        if row["unsupported_interpretations"] > row["assessed_interpretations"]:
            raise ValueError(f"{row.get('case_id')}: unsupported interpretation count exceeds assessed interpretations")
        if row["moment_precision_numerator"] > row["moment_precision_denominator"]:
            raise ValueError(f"{row.get('case_id')}: moment precision numerator exceeds denominator")
        key = (row.get("case_id"), row.get("reviewer_id"))
        if key in seen_reviewers:
            raise ValueError(f"duplicate audit annotation for case/reviewer {key!r}")
        seen_reviewers.add(key)
        rows.append(row)
    by_case: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_case[str(row.get("case_id"))].append(row)
    disagreement = []
    for case_id, annotations in by_case.items():
        if len(annotations) > 1:
            fields = ("evidence_support", "cause_consequence", "alternative_quality",
                      "abstention_quality", "usefulness", "unsupported_factual_assertions",
                      "unsupported_interpretations")
            compared = [(a.get(f), b.get(f)) for i, a in enumerate(annotations)
                        for b in annotations[i + 1:] for f in fields
                        if a.get(f) is not None and b.get(f) is not None]
            if compared:
                disagreement.append({"case_id": case_id, "items_compared": len(compared),
                                     "exact_disagreement_rate": round(sum(x != y for x, y in compared) / len(compared), 4)})
    facts = sum(int(a.get("unsupported_factual_assertions") or 0) for a in rows)
    interpretations = sum(int(a.get("unsupported_interpretations") or 0) for a in rows)
    assessed = sum(int(a.get("assessed_assertions") or 0) for a in rows)
    p_num = sum(int(a.get("moment_precision_numerator") or 0) for a in rows)
    p_den = sum(int(a.get("moment_precision_denominator") or 0) for a in rows)
    interp_den = sum(int(a.get("assessed_interpretations") or 0) for a in rows)
    rating_keys = ("evidence_support", "cause_consequence", "alternative_quality",
                   "abstention_quality", "usefulness")
    ratings = {}
    for key in rating_keys:
        values = [a[key] for a in rows if isinstance(a.get(key), int)
                  and not isinstance(a.get(key), bool) and 1 <= a[key] <= 5]
        ratings[key] = {"mean": round(sum(values) / len(values), 3) if values else None,
                        "n_ratings": len(values)}
    human = {
        "n_reviews": len({(a.get("case_id"), a.get("reviewer_id")) for a in rows}),
        "n_submitted_reviews": len(submitted),
        "n_incomplete_reviews": len(submitted) - len(rows),
        "n_cases": len(by_case), "n_assertions_assessed": assessed,
        "rubric_ratings": ratings,
        "unsupported_factual_assertions": facts,
        "unsupported_factual_assertion_rate": round(facts / assessed, 4) if assessed else None,
        "unsupported_factual_assertion_rate_cluster_bootstrap_95": _cluster_ratio_interval(
            rows, "unsupported_factual_assertions", "assessed_assertions", "case_id"),
        "unsupported_interpretations": interpretations,
        "unsupported_interpretation_rate": round(interpretations / interp_den, 4) if interp_den else None,
        "unsupported_interpretation_rate_cluster_bootstrap_95": _cluster_ratio_interval(
            rows, "unsupported_interpretations", "assessed_interpretations", "case_id"),
        "moment_precision": round(p_num / p_den, 4) if p_den else None,
        "moment_precision_numerator": p_num, "moment_precision_denominator": p_den,
        "moment_precision_cluster_bootstrap_95": _cluster_ratio_interval(
            rows, "moment_precision_numerator", "moment_precision_denominator", "case_id"),
        "double_review_disagreement": disagreement,
    }
    human["thresholds"] = {
        "moment_precision": {"target": thresholds["moment_precision"], "observed": human["moment_precision"],
                             "met": human["moment_precision"] is not None and human["moment_precision"] >= thresholds["moment_precision"]},
        "unsupported_factual_assertions": {"target": thresholds["unsupported_factual_assertions"], "observed": facts,
                                           "met": bool(human["n_reviews"] and assessed)
                                           and facts <= thresholds["unsupported_factual_assertions"]},
        "unsupported_interpretation_rate": {"target": thresholds["unsupported_interpretation_rate"], "observed": human["unsupported_interpretation_rate"],
                                            "met": human["unsupported_interpretation_rate"] is not None and human["unsupported_interpretation_rate"] <= thresholds["unsupported_interpretation_rate"]},
    }
    benchmarks = []
    for report in benchmark_reports:
        summary = report.get("summary", {})
        cases = report.get("cases", [])
        if (report.get("manifest") or {}).get("benchmark") == "trail":
            tm = summary.get("trail_span_metrics", {})
            claim_status, claim_reason = _benchmark_claim_eligibility(report)
            benchmarks.append({"manifest": report.get("manifest"),
                               "run_manifest": report.get("run_manifest"), "summary": summary,
                               "claim_status": claim_status, "claim_reason": claim_reason,
                               "metrics": {
                                   key: {"numerator": tm.get(num), "denominator": tm.get(den),
                                         "rate": tm.get(key), "cluster_bootstrap_95": _cluster_ratio_interval(
                                             cases, case_num, case_den)}
                                   for key, num, den, case_num, case_den in (
                                       ("span_precision", "true_positives", "predicted_spans", "true_positives", "predicted_count"),
                                       ("span_recall", "true_positives", "gold_spans", "true_positives", "gold_count"),
                                       ("high_impact_recall", "high_impact_numerator", "high_impact_denominator", "critical_true_positives", "critical_gold_count"),
                                   )
                               } | {"span_f1": {"rate": tm.get("span_f1"),
                                                  "cluster_bootstrap_95": None}},
                               "failure_count": {"review_failed": summary.get("review_failed_count", 0),
                                                 "incomplete": summary.get("incomplete_count", 0),
                                                 "all_proposals_rejected": summary.get("all_proposals_rejected_count", 0)}})
            continue
        metrics = {}
        for name, key in (("step_accuracy", "step_correct"), ("agent_accuracy", "agent_correct"),
                          ("both_accuracy", "both_correct")):
            num = sum(r.get(key) is True for r in cases)
            den = len(cases)
            metrics[name] = {"numerator": num, "denominator": den,
                             "rate": round(num / den, 4) if den else None,
                             "cluster_bootstrap_95": _cluster_interval(cases, key)}
        claim_status, claim_reason = _benchmark_claim_eligibility(report)
        benchmarks.append({"manifest": report.get("manifest"),
                           "run_manifest": report.get("run_manifest"), "summary": summary,
                           "claim_status": claim_status, "claim_reason": claim_reason,
                           "metrics": metrics,
                           "failure_count": {"review_failed": summary.get("review_failed_count", 0),
                                             "incomplete": summary.get("incomplete_count", 0),
                                             "all_proposals_rejected": summary.get("all_proposals_rejected_count", 0)}})
    integrity_reports = integrity_reports or []
    integrity_totals = {
        "n_reports": len(integrity_reports),
        "displayed_moments": sum(int(r.get("displayed_moments") or 0) for r in integrity_reports),
        "dangling_references": sum(int((r.get("references") or {}).get("dangling") or 0) for r in integrity_reports),
        "mismatched_quotes": sum(int((r.get("quotes") or {}).get("mismatched") or 0) for r in integrity_reports),
        "checking_coverage_checkable_units": sum(int((r.get("coverage") or {}).get("checkable_units") or 0) for r in integrity_reports),
        "checking_coverage_unchecked_claim_units": sum(int((r.get("coverage") or {}).get("unchecked_claim_units") or 0) for r in integrity_reports),
        "checking_coverage_unchecked_narrative_units": sum(int((r.get("coverage") or {}).get("unchecked_narrative_units") or 0) for r in integrity_reports),
        "classification": {
            key: sum(int((r.get("classification") or {}).get(key) or 0) for r in integrity_reports)
            for key in ("contradicted_facts", "unsupported_factual_assertions", "unsupported_interpretations")
        },
        "note": "Mechanical checks establish reference/quote/fact integrity only, not semantic truth.",
    }
    benchmark_claims = []
    exploratory_benchmarks = []
    for item in benchmarks:
        if item["claim_status"] != "publishable":
            exploratory_benchmarks.append({
                "benchmark": (item.get("manifest") or {}).get("benchmark"),
                "claim_status": item["claim_status"],
                "reason": item["claim_reason"],
                "note": "metrics are exploratory and excluded from benchmark claims",
            })
            continue
        manifest = item.get("manifest") or {}
        name = manifest.get("benchmark") or "benchmark"
        metrics = item.get("metrics", {})
        if name == "trail":
            recall = metrics.get("span_recall", {})
            precision = metrics.get("span_precision", {})
            benchmark_claims.append(
                f"TRAIL: AGR matched {recall.get('numerator', 0)} of "
                f"{recall.get('denominator', 0)} labeled error spans; span precision "
                f"was {precision.get('numerator', 0)} of {precision.get('denominator', 0)} predictions."
            )
        else:
            step = metrics.get("step_accuracy", {})
            benchmark_claims.append(
                f"{name}: AGR matched the reference decisive step in "
                f"{step.get('numerator', 0)} of {step.get('denominator', 0)} evaluated cases."
            )
    return {
        "evaluation_report_version": "0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Only the attached frozen benchmark cases and human-audited reviews are covered; unfrozen benchmark metrics are exploratory and excluded from claims.",
        "benchmark_evidence": benchmarks,
        "human_audit": human,
        "integrity_audits": integrity_totals,
        "thresholds": thresholds,
        "claims": {
            "benchmark": benchmark_claims,
            "exploratory_benchmarks": exploratory_benchmarks,
            "human": (f"Human reviewers observed {facts} unsupported factual assertions "
                      f"across {human['n_reviews']} audited reviews containing "
                      f"{assessed} assessed assertions; this claim covers only the attached sample."),
        },
    }
