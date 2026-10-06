"""Legacy evaluation commands: ``python -m legacy <command>``.

These commands used to live in ``agr``'s CLI. They score the reviewer against a
gold set (``eval``), against external attribution benchmarks (``benchmark``), and
prepare/publish evaluation evidence (``audit-pack``, ``publish-evaluation``).
They are kept runnable but are not part of the core path; see ``legacy/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from pathlib import Path

from agr.cli import _configure_utf8_output, _load, _load_env
from agr.pipeline import analyze
from agr.store import Store


_EVAL_KEYS = ("precision_at_3", "recall_at_3", "redundant_card_rate",
              "missed_critical_rate", "no_moment_calibration",
              "evidence_span_precision", "evidence_span_recall",
              "attribution_overclaim_rate",
              # F1 follow-up: the semantic structural proxies print too —
              # named for what they measure, not what they imply.
              "affected_check_overlap_rate",
              "attribution_ceiling_respected_rate")

# For the M4 acceptance check: which direction is an improvement, and which
# metrics the model must not regress on to "beat" the deterministic baseline.
_HIGHER_BETTER = {"precision_at_3", "recall_at_3", "no_moment_calibration",
                  "evidence_span_precision", "evidence_span_recall",
                  "affected_check_overlap_rate",
                  "attribution_ceiling_respected_rate"}
_ACCEPTANCE = ("precision_at_3", "recall_at_3", "missed_critical_rate",
               "attribution_overclaim_rate")


def _fmt_metric(v) -> str:
    return "n/a" if v is None else f"{v:.4f}"


def _print_eval_single(result) -> None:
    report = result.report()
    summary = report["summary"]
    print("Reviewer evaluation — deterministic baseline vs gold set")
    print(f"  harness {summary['reviewer_eval_version']} · "
          f"{summary['n_runs']} runs "
          f"({summary['n_decisive_runs']} decisive, {summary['n_no_moment_runs']} no-moment)")
    print()
    print("Per run:")
    for r in report["runs"]:
        if r["no_moment_case"]:
            verdict = "calibrated" if r["calibrated"] else "MISCALIBRATED (emitted a card)"
            print(f"  {r['run_id']}  no-moment · {verdict}")
        else:
            fmt = lambda v: "n/a" if v is None else v  # noqa: E731
            print(f"  {r['run_id']}  P@3={fmt(r['precision_at_3'])} R@3={fmt(r['recall_at_3'])} "
                  f"redundant={fmt(r['redundant_card_rate'])} "
                  f"missed_critical={fmt(r['missed_critical_rate'])} "
                  f"overclaim={fmt(r['attribution_overclaim_rate'])}")
    print()
    print("Aggregate (micro-averaged; no score compared across tasks):")
    for key in _EVAL_KEYS:
        val = summary[key]
        print(f"  {key:28s} {'n/a (undefined)' if val is None else val}")
    if result.skipped_runs:
        print(f"  skipped (no prediction)      {', '.join(result.skipped_runs)}")


def _print_eval_comparison(baseline, model, provider: str, model_id: str) -> None:
    b, m = baseline.metrics(), model.metrics()
    b_runs = {r["run_id"]: r for r in baseline.report()["runs"]}
    m_runs = {r["run_id"]: r for r in model.report()["runs"]}

    print(f"Reviewer evaluation — deterministic baseline vs model reviewer "
          f"({provider} · {model_id})")
    print(f"  harness {b['reviewer_eval_version']} · {b['n_runs']} runs "
          f"({b['n_decisive_runs']} decisive, {b['n_no_moment_runs']} no-moment)")
    print()
    print("Per run (P@3 / R@3, baseline → model):")
    for run_id in sorted(b_runs):
        br, mr = b_runs[run_id], m_runs.get(run_id, {})
        if br["no_moment_case"]:
            bc = "calibrated" if br["calibrated"] else "MISCALIBRATED"
            mc = "calibrated" if mr.get("calibrated") else "MISCALIBRATED"
            print(f"  {run_id}  no-moment · {bc} → {mc}")
        else:
            print(f"  {run_id}  {_fmt_metric(br['precision_at_3'])}/{_fmt_metric(br['recall_at_3'])}"
                  f" → {_fmt_metric(mr.get('precision_at_3'))}/{_fmt_metric(mr.get('recall_at_3'))}")
    print()
    print("Aggregate (micro-averaged; no score compared across tasks):")
    print(f"  {'metric':28s} {'baseline':>10} {'model':>10} {'Δ':>10}")
    for key in _EVAL_KEYS:
        bv, mv = b[key], m[key]
        if bv is None or mv is None:
            print(f"  {key:28s} {_fmt_metric(bv):>10} {_fmt_metric(mv):>10} {'n/a':>10}")
            continue
        delta = mv - bv
        better = (delta > 0) if key in _HIGHER_BETTER else (delta < 0)
        worse = (delta < 0) if key in _HIGHER_BETTER else (delta > 0)
        mark = "  ↑ better" if better else ("  ↓ worse" if worse else "  = same")
        print(f"  {key:28s} {bv:>10.4f} {mv:>10.4f} {delta:>+10.4f}{mark}")
    print()
    print("M4 acceptance — the model reviewer must beat the deterministic baseline:")
    regressed = False
    for key in _ACCEPTANCE:
        bv, mv = b[key], m[key]
        if bv is None or mv is None:
            print(f"  [=] {key}: baseline {_fmt_metric(bv)} → model {_fmt_metric(mv)} (n/a)")
            continue
        ok = mv >= bv if key in _HIGHER_BETTER else mv <= bv
        strictly = mv > bv if key in _HIGHER_BETTER else mv < bv
        box = "=" if (ok and not strictly) else ("✓" if ok else "✗")
        guard = " (guardrail)" if key == "attribution_overclaim_rate" else ""
        print(f"  [{box}] {key}{guard}: baseline {_fmt_metric(bv)} → model {_fmt_metric(mv)}")
        if not ok:
            regressed = True
    if regressed:
        worst = [k for k in _ACCEPTANCE
                 if (m[k] is not None and b[k] is not None
                     and (m[k] < b[k] if k in _HIGHER_BETTER else m[k] > b[k]))]
        print(f"  → VERDICT: FAIL — model regresses on {', '.join(worst)}")
    else:
        print("  → VERDICT: PASS — model matches or beats the baseline on every gate")


def cmd_eval(args) -> int:
    """Score the deterministic baseline reviewer against the gold set (spec §15.3).

    Ingests every fixture the gold set references, validates the gold labels
    against those immutable sources, then runs the reviewer evaluation harness and
    prints per-run + aggregate metrics plus a disagreement report for each
    double-labelled trajectory. With ``--provider`` it *also* scores a Stage F model
    reviewer over the same runs and prints a baseline→model comparison plus the M4
    acceptance verdict (the model must not regress on precision/recall/missed-critical
    and must not overclaim attribution). Without it, only the deterministic baseline
    is scored.
    """
    import glob

    from . import reviewer_eval as rev
    from .gold import disagreement_report, load_gold_set

    gold_set = load_gold_set(args.gold)
    if not gold_set.trajectories:
        print(f"no gold trajectories under {args.gold!r}", file=sys.stderr)
        return 1

    # The eval harness needs a store as scratch space for the runs it scores.
    # Using the user's working store would pollute it with fixture runs, so the
    # eval store is isolated by default; --store is honoured only when passed
    # explicitly (e.g. to keep scored runs around for inspection).
    import tempfile

    if args.store_explicit:
        store = Store(args.store)
    else:
        store = Store(tempfile.mkdtemp(prefix="agr-eval-"))
    paths = sorted(glob.glob(os.path.join(args.fixtures, "*.atif.json")))
    for path in paths:
        analyze(_load(path), store)

    # Validate gold against the immutable sources: every anchor / evidence step
    # id must name a real step in the run it labels.
    source_steps: dict[str, set[str]] = {}
    for traj in gold_set.trajectories:
        capture_id = store.latest_capture_id(traj.run_id)
        if capture_id is None:
            continue
        doc = store.read_source(traj.run_id, capture_id)
        source_steps[traj.run_id] = {s["step_id"] for s in doc.get("steps", [])}
    gold_set.validate(source_steps)

    baseline = rev.evaluate_store(store, gold_set)

    provider = getattr(args, "provider", None)
    if not provider:
        _print_eval_single(baseline)
    else:
        model_id = args.model or os.environ.get("AGR_REVIEW_MODEL")
        from agr.model_reviewer import make_reviewer
        try:
            reviewer = make_reviewer(provider, model_id, getattr(args, "base_url", None))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except RuntimeError as exc:  # the provider SDK (optional extra) is not installed
            print(f"cannot run model reviewer: {exc}", file=sys.stderr)
            return 3
        # Re-score the same runs with the model reviewer (overwrites the persisted
        # review moments with the model-enriched ones), then compare.
        try:
            for path in paths:
                analyze(_load(path), store, reviewer=reviewer)
        except RuntimeError as exc:  # provider SDK (optional extra) not installed
            print(f"cannot run model reviewer: {exc}", file=sys.stderr)
            return 3
        except Exception as exc:  # noqa: BLE001 - provider/auth/network error
            print(f"model reviewer failed: {exc}", file=sys.stderr)
            print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
                  file=sys.stderr)
            return 4
        model = rev.evaluate_store(store, gold_set)
        _print_eval_comparison(baseline, model, provider, reviewer.model)

    disagreements = [disagreement_report(t) for t in gold_set.trajectories if t.double_labelled]
    if disagreements:
        print()
        print("Inter-annotator disagreement (double-labelled trajectories):")
        for d in disagreements:
            state = "agree" if d["agreement"] else "disagree"
            print(f"  {d['run_id']} · {', '.join(d['annotators'])} · {state}")
            for m in d["matched_moments"]:
                for dis in m["disagreements"]:
                    print(f"    ~ {m['a_moment']}: {dis['field']} {dis['a']!r} vs {dis['b']!r}")
            for mid in d["a_only_moments"]:
                print(f"    - only {d['annotators'][0]}: {mid}")
            for mid in d["b_only_moments"]:
                print(f"    - only {d['annotators'][1]}: {mid}")

    # AGR-07: reproducibility manifest — versions, gold status, invocation, and
    # the full report, so another person can regenerate the same numbers.
    if getattr(args, "manifest", None):
        import hashlib
        import platform

        from agr import version
        gold_files = sorted(glob.glob(os.path.join(args.gold, "*.gold.json")))
        fixture_files = sorted(glob.glob(os.path.join(args.fixtures, "*.atif.json")))
        # F1 follow-up: the manifest carries the full derivation — the reviewed
        # source commit, fixture hashes, the scored report(s), and the package
        # version — so results are reproducible and auditable.
        report = baseline.report()
        if provider:
            report = {"baseline": report, "model": model.report()}
        manifest = {
            "manifest_version": 2,
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_commit": _git_sha(),
            "invocation": {
                "command": "python -m legacy eval",
                "fixtures": args.fixtures,
                "gold_dir": args.gold,
                "k": 3,
                "provider": provider or "deterministic-only",
                "model": (args.model or os.environ.get("AGR_REVIEW_MODEL")) if provider else None,
            },
            "versions": {
                "python": platform.python_version(),
                "agr_package": version.AGR_VERSION,
                "reviewer_eval": version.REVIEWER_EVAL_VERSION,
                "detector": version.DETECTOR_VERSION,
                "reviewer": version.REVIEWER_VERSION,
                "redaction": version.REDACTION_VERSION,
            },
            "gold_set": {
                "files": [
                    {"path": os.path.basename(f),
                     "sha256": hashlib.sha256(open(f, "rb").read()).hexdigest()}
                    for f in gold_files
                ],
                "adjudication_status": {
                    t.run_id: "double_labelled" if t.double_labelled else "single_labelled"
                    for t in gold_set.trajectories
                },
            },
            "fixtures": [
                {"path": os.path.basename(f),
                 "sha256": hashlib.sha256(open(f, "rb").read()).hexdigest()}
                for f in fixture_files
            ],
            "report": report,
        }
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        print(f"\nmanifest written to {args.manifest}")
    return 0


def _git_sha() -> Optional[str]:
    """The AGR source commit, when this CLI runs from a git checkout.

    Review 2026-09-07: the commit is resolved against THIS PACKAGE's location,
    not the caller's working directory — an installed CLI invoked inside a
    different Git project must not report that project's commit as AGR's code
    version. None when the package does not live in a git repo (e.g. an
    installed wheel) — absent, never invented.
    """
    try:
        import subprocess
        from pathlib import Path
        pkg_root = Path(__file__).resolve().parent
        out = subprocess.run(["git", "-C", str(pkg_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def _benchmark_adapter(name: str):
    """Resolve a registered benchmark adapter by name."""
    from .benchmark_whowhen import WHO_WHEN_ADAPTER
    from .benchmark_trail import TRAIL_ADAPTER
    return {WHO_WHEN_ADAPTER.name: WHO_WHEN_ADAPTER,
            TRAIL_ADAPTER.name: TRAIL_ADAPTER}.get(name)


def _fmt_rate(rate) -> str:
    return f"{rate:.3f}" if rate is not None else "n/a"


def _print_benchmark(result) -> None:
    """Print the EV-1 attribution result with its declared rule and limits."""
    m = result.metrics()
    manifest = result.manifest
    print(f"Attribution benchmark — {manifest.benchmark} ({manifest.version})")
    print(f"  source            {manifest.source_url}  ·  {manifest.license}")
    print(f"  single-prediction {manifest.single_prediction_rule}")
    print(f"  cases             {m['n_cases']} scored  ·  {m['n_warnings']} warning(s)")
    for label, rate_key, count_key in (() if "trail_span_metrics" in m else (
        ("step accuracy", "step_accuracy", "step_correct_count"),
        ("agent accuracy", "agent_accuracy", "agent_correct_count"),
        ("both accuracy", "both_accuracy", "both_correct_count"),
    )):
        print(f"  {label:<17} {_fmt_rate(m[rate_key])}  ({m[count_key]}/{m['n_cases']})")
    if "trail_span_metrics" in m:
        t = m["trail_span_metrics"]
        print(f"  span precision    {t['span_precision']:.3f} ({t['true_positives']}/{t['predicted_spans']})")
        print(f"  span recall       {t['span_recall']:.3f} ({t['true_positives']}/{t['gold_spans']})")
        print(f"  span F1           {t['span_f1']:.3f}")
        print(f"  high-impact recall { _fmt_rate(t['high_impact_recall'])} "
              f"({t['high_impact_numerator']}/{t['high_impact_denominator']})")
    if "trail_span_metrics" not in m:
        print(f"  {'abstention rate':<17} {_fmt_rate(m['abstention_rate'])}")
    print()
    print(manifest.scoring_protocol)
    if manifest.limits:
        print()
        print("Limits (carry these with any number above):")
        for limit in manifest.limits:
            print(f"  - {limit}")


def cmd_benchmark(args) -> int:
    """Score AGR against an external failure-attribution benchmark (EV-1).

    Converts a benchmark's records into ingestable ATIF runs plus reference
    labels, runs AGR's reviewer over them, and scores the declared
    single-prediction rule against the benchmark's own protocol. The manifest
    (version, provenance, rule, limits) is part of every result.
    """
    import tempfile

    from . import benchmark as bench

    adapter = _benchmark_adapter(args.benchmark)
    if adapter is None:
        print(f"unknown benchmark {args.benchmark!r}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.data):
        print(f"--data is not a directory: {args.data!r}", file=sys.stderr)
        return 2

    if args.store_explicit:
        store = Store(args.store)
    else:
        store = Store(tempfile.mkdtemp(prefix="agr-benchmark-"))

    reviewer = None
    reviewer_factory = None
    if args.provider:
        model_id = args.model or os.environ.get("AGR_REVIEW_MODEL")
        from agr.model_reviewer import make_reviewer
        try:
            reviewer = make_reviewer(args.provider, model_id, args.base_url)
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 3
        reviewer_factory = lambda: reviewer  # noqa: E731 - one reviewer, reused per case

    run_manifest = None
    if args.manifest:
        if args.out and os.path.normcase(os.path.abspath(args.manifest)) == os.path.normcase(
            os.path.abspath(args.out)
        ):
            print("--manifest and --out must use different paths", file=sys.stderr)
            return 2
        if os.path.exists(args.manifest):
            print(f"run manifest already exists: {args.manifest!r}", file=sys.stderr)
            return 2
        try:
            protocol = _load(args.protocol) if args.protocol else {}
            run_manifest = bench.build_run_manifest(
                adapter,
                args.data,
                limit=args.limit,
                provider=args.provider,
                model=getattr(reviewer, "model", None) if reviewer else None,
                base_url=args.base_url,
                protocol=protocol,
            )
            # Exclusive creation keeps a frozen record from being silently
            # replaced by a later run with different inputs or settings.
            with open(args.manifest, "x", encoding="utf-8") as fh:
                json.dump(run_manifest, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except (OSError, ValueError) as exc:
            print(f"could not freeze benchmark run: {exc}", file=sys.stderr)
            return 2

    result = bench.run_benchmark(
        adapter, args.data, store, limit=args.limit,
        reviewer_factory=reviewer_factory,
        reviewer_key=getattr(reviewer, "reviewer_key", None) if reviewer else None,
        run_manifest=run_manifest,
    )
    _print_benchmark(result)
    report = result.report()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"\nwrote report to {args.out}")
    if args.manifest:
        print(f"froze run manifest to {args.manifest}")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_audit_pack(args) -> int:
    """Create the reproducible EV-3 sample, blind/revealed packets and rubric."""
    from .evaluation import create_audit_pack
    try:
        metadata = create_audit_pack(
            Store(args.store), args.out, sample_size=args.sample_size,
            double_review_fraction=args.double_review_fraction, seed=args.seed,
            reviewer_key=args.reviewer,
        )
    except (OSError, ValueError) as exc:
        print(f"could not create audit pack: {exc}", file=sys.stderr)
        return 2
    print(f"created audit pack: {metadata['selected_count']} of {metadata['eligible_count']} reviews")
    print(f"double-reviewed cases: {metadata['double_review_count']}")
    print(f"blind phase: {os.path.join(args.out, 'blind', 'cases.json')}")
    print(f"blind annotation form: {os.path.join(args.out, 'blind', 'annotations-template.json')}")
    print(f"revealed phase and form: {os.path.join(args.out, 'revealed')}")
    print(f"sealed key: {os.path.join(args.out, 'key', 'case-map.json')}")
    return 0


def cmd_publish_evaluation(args) -> int:
    """Combine frozen benchmark, human audit, and integrity evidence (EV-4)."""
    from .evaluation import build_evaluation_report
    try:
        benchmark_reports = [_load(p) for p in args.benchmark_report]
        audits = [_load(p) for p in args.audit]
        integrity_reports = [_load(p) for p in args.integrity]
        if args.thresholds:
            thresholds = _load(args.thresholds)
        else:
            thresholds = None
        report = build_evaluation_report(
            benchmark_reports, audits, integrity_reports=integrity_reports,
            thresholds=thresholds,
        )
        with open(args.out, "x", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"could not publish evaluation report: {exc}", file=sys.stderr)
        return 2
    print(f"wrote scoped evaluation report to {args.out}")
    print(f"human-audited reviews: {report['human_audit']['n_reviews']} across {report['human_audit']['n_cases']} cases")
    print("claims are limited to the attached benchmark cases and audited sample")
    return 0


# Defaults resolve against this checkout, so `eval` works from any working directory
# (the package itself must still be importable: run from the repo root or set PYTHONPATH).
_LEGACY_DIR = Path(__file__).resolve().parent
_DEFAULT_GOLD = str(_LEGACY_DIR / "gold" / "synthetic")
_DEFAULT_FIXTURES = str(_LEGACY_DIR.parent / "archive" / "synthetic" / "fixtures")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m legacy",
        description="Legacy Agent Game Review evaluation commands (gold set, benchmarks, audit)")
    p.add_argument("--store", default=None, help="store directory (default: .agr-store)")
    p.add_argument("--env-file", default=None,
                   help="path to a .env file to load (default: ./.env or $AGR_DOTENV)")
    sub = p.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("eval", help="score the reviewer against the gold set "
                                     "(deterministic baseline, or --provider to compare a model)")
    pe.add_argument("--gold", default=_DEFAULT_GOLD, help="gold set directory (default: the synthetic reference gold in this checkout)")
    pe.add_argument("--fixtures", default=_DEFAULT_FIXTURES, help="ATIF fixtures directory (default: archive/synthetic/fixtures in this checkout)")
    pe.add_argument("--provider", default=None, choices=["anthropic", "openai"],
                    help="also score a model reviewer and compare it to the baseline "
                         "(needs the matching 'model-*' extra + a key)")
    pe.add_argument("--model", default=None,
                    help="model id for --provider (default: $AGR_REVIEW_MODEL or the provider default)")
    pe.add_argument("--base-url", default=None,
                    help="OpenAI/Anthropic-compatible endpoint for --provider (any model)")
    pe.add_argument("--manifest", default=None, metavar="PATH",
                    help="write a reproducibility manifest (versions, gold hashes, "
                         "adjudication status, invocation) to PATH")
    pe.set_defaults(func=cmd_eval)

    pb = sub.add_parser(
        "benchmark",
        help="score AGR against an external failure-attribution benchmark (EV-1)")
    pb.add_argument("benchmark", choices=["who-and-when", "trail"],
                    help="which benchmark to run")
    pb.add_argument("--data", required=True,
                    help="directory of benchmark records (the split, e.g. "
                         "Who&When split or locally supplied TRAIL JSON/JSONL export)")
    pb.add_argument("--limit", type=int, default=None,
                    help="score only the first N records (quick check)")
    pb.add_argument("--provider", default=None, choices=["anthropic", "openai"],
                    help="also run the model reviewer instead of the deterministic baseline "
                         "(needs the matching 'model-*' extra + a key)")
    pb.add_argument("--model", default=None,
                    help="model id for --provider (default: $AGR_REVIEW_MODEL or the provider default)")
    pb.add_argument("--base-url", default=None,
                    help="OpenAI/Anthropic-compatible endpoint for --provider")
    pb.add_argument("--out", default=None, metavar="PATH",
                    help="write the full JSON report (manifest + per-case scores) to PATH")
    pb.add_argument("--manifest", default=None, metavar="PATH",
                    help="freeze selected record hashes and reviewer settings before scoring")
    pb.add_argument("--protocol", default=None, metavar="JSON",
                    help="predeclared JSON with configuration, thresholds, and partition_policy; model runs also require training_cutoff")
    pb.add_argument("--json", action="store_true",
                    help="print the full JSON report")
    pb.set_defaults(func=cmd_benchmark)

    pap = sub.add_parser("audit-pack", help="prepare a reproducible EV-3 human-audit sample")
    pap.add_argument("--out", required=True, help="new output directory for blind/revealed packets")
    pap.add_argument("--sample-size", type=int, default=30)
    pap.add_argument("--double-review-fraction", type=float, default=0.25)
    pap.add_argument("--seed", type=int, default=1)
    pap.add_argument("--reviewer", default=None, help="reviewer key to audit (default: served review)")
    pap.set_defaults(func=cmd_audit_pack)

    ppe = sub.add_parser("publish-evaluation", help="publish scoped EV-4 results from supplied evidence")
    ppe.add_argument("--benchmark-report", action="append", default=[], metavar="JSON",
                     help="benchmark result report; repeat for each benchmark")
    ppe.add_argument("--audit", action="append", default=[], metavar="JSON",
                     help="completed human annotations JSON; repeat as needed")
    ppe.add_argument("--integrity", action="append", default=[], metavar="JSON",
                     help="agr audit-integrity JSON report; repeat for each audited run")
    ppe.add_argument("--thresholds", help="predeclared threshold JSON (optional)")
    ppe.add_argument("--out", required=True, help="new output JSON path")
    ppe.set_defaults(func=cmd_publish_evaluation)
    return p


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.store_explicit = args.store is not None
    if args.store is None:
        args.store = ".agr-store"
    _load_env(getattr(args, "env_file", None))
    return args.func(args)
