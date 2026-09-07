"""Command-line interface for the deterministic core.

    python -m agr ingest-harbor ~/harbor/jobs/<job-dir> --store .agr-store   # eval-framework path
    python -m agr ingest archive/synthetic/fixtures/chess_best_move.atif.json --store .agr-store
    python -m agr show   chess_best_move__seed42            --store .agr-store
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import workflow
from .adapter import adapter_names
from .pipeline import analyze
from .store import Store
from .versions import CHANGE_AXES as _CHANGE_AXES


def _configure_utf8_output() -> None:
    """Make stdout/stderr encode UTF-8 so glyphs never crash the CLI.

    The review renders non-ASCII glyphs (``→ · ✓ ✗ —``). On a console whose
    encoder is cp1252 (the Windows default) writing them raises
    ``UnicodeEncodeError`` mid-output. Forcing UTF-8 here is the in-process
    equivalent of ``PYTHONIOENCODING=utf-8``; ``errors="replace"`` is a last
    resort so an exotic environment degrades a glyph to ``?`` rather than
    crashing. Runs before any output is produced.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except (ValueError, OSError):
                pass
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            try:
                setattr(sys, name, io.TextIOWrapper(
                    buffer, encoding="utf-8", errors="replace", line_buffering=True))
            except (ValueError, OSError):
                pass


def _load(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def cmd_ingest(args) -> int:
    store = Store(args.store)
    doc = _load(args.path)
    return _ingest_doc(doc, store)


def cmd_ingest_from(args) -> int:
    """Convert a harness log to ATIF via a named adapter, then ingest it.

    The generic path (spec §5.3): adding a harness is a new adapter, not a new
    CLI verb. The adapter maps the log to the ATIF contract; the normal pipeline
    takes over from there. Nothing about the run is invented — task identity and
    the verifier result are explicit inputs or honestly absent.
    """
    from .adapter import get_adapter
    from .ingest_pi import load_verifier

    adapter = get_adapter(args.adapter)
    verifier = load_verifier(args.verifier) if args.verifier else None
    result = adapter.convert(
        args.path,
        task_id=args.task_id,
        instruction=args.instruction,
        run_id=args.run_id,
        verifier=verifier,
        sweep_id=args.sweep_id,
        configuration_id=args.configuration_id,
    )
    for w in result.warnings:
        print(f"  adapter warning: {w}")
    store = Store(args.store)
    return _ingest_doc(result.doc, store)


def cmd_ingest_harbor(args) -> int:
    """Ingest Harbor (Terminal-Bench 2.0) output — the primary eval-framework path.

    PATH may be one trial directory, a ``trajectory.json`` file, or a job
    directory as written by ``harbor run`` (one subdirectory per trial), in
    which case every trial under it is ingested. Task identity comes from
    ``--task-id``, else from what Harbor recorded on each trial, else from its
    trajectory id; the reward-based verifier is synthesised from each trial's
    ``result.json`` unless ``--verifier`` supplies an explicit sidecar.
    """
    from .adapter import get_adapter
    from .ingest_harbor import iter_trials_detailed
    from .ingest_pi import load_verifier

    try:
        discovered = iter_trials_detailed(args.path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    trials = [path for path, reason in discovered if reason is None]
    excluded = [(path, reason) for path, reason in discovered if reason is not None]
    if len(trials) > 1 and args.run_id:
        print("--run-id cannot name a single run when PATH holds multiple trials",
              file=sys.stderr)
        return 2

    adapter = get_adapter("harbor")
    verifier = load_verifier(args.verifier) if args.verifier else None
    store = Store(args.store)
    ingested = skipped = 0
    for trial, reason in excluded:
        print(f"excluded {Path(trial).name}: {reason}")
        skipped += 1
    for trial in trials:
        label = Path(trial).name
        try:
            result = adapter.convert(
                trial,
                task_id=args.task_id,
                instruction=args.instruction,
                run_id=None if len(trials) > 1 else args.run_id,
                verifier=verifier,
                sweep_id=args.sweep_id,
                configuration_id=args.configuration_id,
            )
        except ValueError as exc:
            print(f"excluded {label}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        print(f"— {label}")
        for w in result.warnings:
            print(f"  adapter warning: {w}")
        _ingest_doc(result.doc, store)
        ingested += 1
    if len(discovered) > 1:
        print(f"\n{ingested} trial(s) ingested"
              + (f", {skipped} excluded with a reason" if skipped else "")
              + f" — {ingested + skipped} of {len(discovered)} discovered accounted for")
    return 0 if ingested else 1


def cmd_ingest_pi(args) -> int:
    """Ingest a pi session JSONL via the pi adapter (thin alias of ``ingest-from
    --adapter pi``, kept for convenience and backwards compatibility)."""
    args.adapter = "pi"
    return cmd_ingest_from(args)


def _ingest_doc(doc: dict, store: Store) -> int:
    analysis = analyze(doc, store)
    rs = analysis.run_source
    tag = "idempotent (no-op on source)" if analysis.idempotent else "new capture"
    print(f"Ingested {rs.run_id}")
    print(f"  capture   : {rs.source_capture_id} (rev {rs.capture_revision}, {tag})")
    print(f"  schema    : {rs.source_schema}")
    print(f"  hash      : {rs.source_hash}")
    print(f"  outcome   : {analysis.outcome['status']} · "
          f"{analysis.outcome['passed']}/{analysis.outcome['total']} checks")
    c = analysis.contract
    print(f"  contract  : v{c.contract_version} · {c.status} · "
          f"{len(c.items)} items · {len(c.warnings)} warning(s)")
    if analysis.watermark:
        print(f"  watermark : {analysis.watermark}")
    return 0


def _print_contract(analysis) -> None:
    c = analysis.contract
    conf = f" · confirmed by {c.confirmed_by}" if c.confirmed_by else ""
    print(f"Task contract v{c.contract_version} · {c.status}{conf}")
    for item in c.items:
        mark = {"confirmed": "✓", "rejected": "✗", "edited": "~"}.get(item.human_status, "·")
        cov = f" → {', '.join(item.mapped_checks)}" if item.mapped_checks else " → (no check)"
        print(f"  [{mark}] {item.id} ({item.source_type}) {item.description}{cov}")
    if c.warnings:
        print("  Contract warnings:")
        for w in c.warnings:
            print(f"    ! {w.warning_type}: {w.message}")
    print()


def _print_show(analysis) -> None:
    rs = analysis.run_source
    o = analysis.outcome
    watermark = analysis.watermark
    if watermark:
        print(f"*** {watermark} ***")
    print(f"{o['status']} · {o['passed']}/{o['total']} checks    {rs.task_id}")
    print(f"run {rs.run_id} · capture {rs.source_capture_id} (rev {rs.capture_revision})")
    print(f"model {rs.model} · harness {rs.harness_version} · n=1")
    print()

    _print_contract(analysis)

    print("Atomic checks:")
    for c in analysis.checks:
        mark = "PASS" if c.status == "passed" else c.status.upper()
        line = f"  [{mark}] {c.check_id} {c.name}"
        if c.status == "failed" and c.expected is not None:
            line += f"  expected={c.expected} observed={c.observed}"
        print(line)
    print()

    if analysis.evidence_slices:
        print("Evidence slices:")
        for s in analysis.evidence_slices:
            print(f"  [{s.branch}] {s.check_id}: {len(s.event_ids)} events · ceiling={s.attribution_ceiling}")
            print(f"    events: {', '.join(s.event_ids)}")
            print(f"    {s.rationale}")
        print()

    print("Task Ability Signature:")
    for row in analysis.signature:
        print(f"  {row.ability}")
        print(f"    {row.observed_behaviour} · {row.result} · {row.interpretation}")
    print()

    if analysis.opportunities:
        print("Opportunities:")
        for o in analysis.opportunities:
            print(f"  {o.ability} ({o.trigger}) · {o.start_event_id}→{o.end_event_id}")
        print()

    if analysis.recoveries:
        print("Recovery episodes:")
        for ep in analysis.recoveries:
            res = ep.resolution_event_id or "unresolved"
            print(f"  {ep.classification} · fail {ep.failure_event_id} → {res}")
        print()

    selected = [m for m in analysis.review_moments if m.selected]
    if selected:
        enriched = any(m.enrichment_source for m in selected)
        mode = "model-enriched · Stage F" if enriched else "deterministic envelope · Stages G/H/I"
        print(f"Reviewed moments ({mode}):")
        for m in selected:
            tag = "strength" if m.polarity == "positive" else "concern"
            verdict = m.taxonomy_verdict or "(verdict: model reviewer / Stage F)"
            print(f"  [{tag}] ceiling={m.attribution_ceiling} · {verdict}")
            print(f"    {m.rendered_statement}")
            if m.enrichment_source:  # visibly labelled model-generated (§8.8)
                if m.behaviour_tags:
                    print(f"    tags: {', '.join(m.behaviour_tags)}  ({m.enrichment_source})")
                if m.root_cause_candidates:
                    top = m.root_cause_candidates[0]
                    print(f"    root cause: {top.get('locus')} — {top.get('rationale')}")
                if m.better_action:
                    print(f"    better action (model): {m.better_action}")
        print()

    print("Detectors:")
    for r in analysis.detector_results:
        if not r.evaluated:
            print(f"  {r.detector}: not evaluated (missing: {', '.join(r.unmet_capabilities)})")
        elif r.candidates:
            print(f"  {r.detector}: {len(r.candidates)} candidate(s)")
            for cand in r.candidates:
                tag = "+" if cand.polarity == "positive" else "-"
                extra = f" → checks {cand.affected_checks}" if cand.affected_checks else ""
                print(f"    {tag} {cand.kind} @ {', '.join(cand.anchor_event_ids)}{extra}")
        else:
            print(f"  {r.detector}: no candidate")


def cmd_show(args) -> int:
    store = Store(args.store)
    capture_id = store.latest_capture_id(args.run_id)
    if capture_id is None:
        print(f"run {args.run_id!r} not found in store {args.store!r}", file=sys.stderr)
        return 1
    doc = store.read_source(args.run_id, capture_id)
    analysis = analyze(doc, store)  # deterministic: recompute for display
    _print_show(analysis)
    return 0


def cmd_confirm(args) -> int:
    """Record a human confirmation of the task contract (spec §8.2).

    Persists a confirmation decision next to the immutable source. Re-running
    ``show`` then reflects the confirmed, non-watermarked contract.
    """
    store = Store(args.store)
    capture_id = store.latest_capture_id(args.run_id)
    if capture_id is None:
        print(f"run {args.run_id!r} not found in store {args.store!r}", file=sys.stderr)
        return 1

    if args.decisions:
        confirmation = _load(args.decisions)
    elif args.all:
        confirmation = {"confirm_all": True}
    else:
        print("provide --all or --decisions <path>", file=sys.stderr)
        return 2

    # Human confirmation is what clears the review watermark, so record who did
    # it. Fall back through the usual identity env vars; if none is available
    # and --by was not given, proceed but flag the weak audit trail.
    confirmer = args.by or confirmation.get("confirmed_by") or _default_confirmer()
    if not confirmer:
        print("warning: no confirmer identity found; pass --by <name> for a proper "
              "audit trail. Recording as 'unknown'.", file=sys.stderr)
        confirmer = "unknown"
    confirmation["confirmed_by"] = confirmer
    confirmation.setdefault("confirmed_at", _now())

    store.write_derived(args.run_id, capture_id, "contract_confirmation.json", confirmation)

    analysis = analyze(store.read_source(args.run_id, capture_id), store)
    c = analysis.contract
    print(f"Contract for {args.run_id} is now v{c.contract_version} · {c.status}")
    if analysis.watermark:
        print(analysis.watermark)
    else:
        print(f"Confirmed by {c.confirmed_by} at {c.confirmed_at}; watermark cleared.")
    return 0


def cmd_disposition(args) -> int:
    """Record a human review disposition on a run (spec §4.3.4).

    The headless parallel of the UI's disposition control: writes the mutable
    workflow record beside the immutable source (never mutating it), using the
    same optimistic ``base_version`` the HTTP layer enforces. Omit ``--base-version``
    to target the run's current version.
    """
    store = Store(args.store)
    if store.latest_capture_id(args.run_id) is None:
        print(f"run {args.run_id!r} not found in store {args.store!r}", file=sys.stderr)
        return 1

    actor = args.by or _default_confirmer() or "unknown"
    base_version = (args.base_version if args.base_version is not None
                    else workflow.read_workflow(store, args.run_id)["workflow_version"])
    try:
        wf = workflow.set_workflow(
            store, args.run_id,
            actor=actor,
            base_version=base_version,
            progress=args.progress,
            disposition=args.disposition,
            assignee=args.assign,
            note=args.note,
        )
    except workflow.VersionConflict as exc:
        print(f"stale write: run is at version {exc.actual}, not {exc.expected}; "
              "re-read and retry", file=sys.stderr)
        return 2
    except workflow.WorkflowError as exc:
        print(f"cannot set workflow: {exc}", file=sys.stderr)
        return 2

    disp = wf["disposition"] or "—"
    print(f"{args.run_id}: {wf['review_progress']} · disposition {disp} "
          f"(v{wf['workflow_version']}, by {actor})")
    return 0


def cmd_runs(args) -> int:
    """List every run in the store (the read model's summary view)."""
    from . import read
    store = Store(args.store)
    summaries = read.list_runs(store)
    if not summaries:
        print(f"no runs in store {args.store!r}", file=sys.stderr)
        return 1
    for s in summaries:
        o = s["outcome"]
        wm = " · PROVISIONAL" if s["contract"]["watermarked"] else ""
        status = o.get("status") or "?"
        passed, total = o.get("passed"), o.get("total")
        counts = f"{passed}/{total}" if passed is not None else "?/?"
        print(f"{s['run_id']}  [{status} {counts}]  {s.get('task_id') or ''}{wm}")
    return 0


def _selector_arg(raw: str) -> dict:
    field, _, value = raw.partition("=")
    if not field or not value:
        raise SystemExit(f"selector must be field=value, got {raw!r}")
    return {field: value}


def cmd_demo_store(args) -> int:
    """Build a synthetic two-configuration slice so §4.16 has something to compare.

    The shipped fixtures are one harness with no sweep, so a fresh store shows the
    comparison surface's empty state. This ingests them twice under two sweep ids,
    improving most tasks on the candidate side, and leaves one unmatched run per
    side so the match report's exclusions are real. Every run it writes is marked
    ``synthetic_demo``.
    """
    from . import demo
    fixtures_dir = demo.find_fixtures_dir(args.fixtures)
    store = Store(args.store)
    definition = demo.build_demo_store(store, fixtures_dir)
    print(f"Built a synthetic demo slice in {args.store!r} (source_type=synthetic_demo)")
    print(f"  baseline   {definition['baseline']}")
    print(f"  candidate  {definition['candidate']}")
    print(f"  axis       {definition['axis']}")
    print()
    print("  python3 -m agr serve --store " + args.store)
    print("  then: Compare versions (V) -> Preview match")
    return 0


def cmd_demo(args) -> int:
    """Build the demo store and start the server — the five-minute path.

    One command from clone to browsing the demo sweep:

        pip install .[api]
        agr demo

    Builds a synthetic two-configuration slice in the store, then starts the
    read API server so you can open the evidence-browser SPA in your browser.
    All contracts are auto-confirmed so no watermarks appear.
    """
    from . import demo
    # Default to a dedicated demo store so the working store is not polluted.
    # User can override via the global --store argument.
    if not args.store_explicit and args.store == ".agr-store":
        args.store = ".agr-demo"
    fixtures_dir = demo.find_fixtures_dir(args.fixtures)
    store = Store(args.store)
    definition = demo.build_demo_store(store, fixtures_dir)
    print(f"Demo store built in {args.store!r}")
    print(f"  baseline   {definition['baseline']['sweep_id']}")
    print(f"  candidate  {definition['candidate']['sweep_id']}")
    print(f"  axis       {definition['axis']}")
    print(f"  runs       12 (5 matched + 2 unmatched)")
    print()
    _start_server(args)
    return 0


def _start_server(args) -> None:
    """Start uvicorn with the read API app."""
    try:
        import uvicorn  # noqa: F401
        from .api import create_app
    except (ModuleNotFoundError, RuntimeError) as exc:
        print(f"cannot start server: {exc}", file=sys.stderr)
        print("install the API extras with:  pip install .[api]", file=sys.stderr)
        return
    app = create_app(args.store)
    print(f"Serving demo at http://{args.host}:{args.port}")
    print("Open your browser to view the evidence-browser SPA.")
    print("Press Ctrl+C to stop.")
    print()
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_metrics(args) -> int:
    """Derived product measures over the review-workflow analytics log (§4.21)."""
    from . import instrumentation
    store = Store(args.store)
    m = instrumentation.product_measures(store)
    if not m["events"]:
        print(f"no analytics events in store {args.store!r}", file=sys.stderr)
        return 1
    print(f"Review-workflow measures — {m['events']} events across {m['sessions']} session(s)")
    print("  (usability and trust; never an agent-quality score)")
    print()
    for key in ("median_seconds_sweep_open_to_first_disposition",
                "median_seconds_run_open_to_first_moment",
                "handled_runs_per_session",
                "moment_views_opening_evidence",
                "quick_relabel_rate",
                "full_correction_rate",
                "in_progress_runs_later_dispositioned",
                "full_trace_fallback_rate",
                "model_enriched_open_rate"):
        value = m[key]
        print(f"  {key:44s} {'not observed' if value is None else round(value, 3)}")
    print()
    print("Event counts:")
    for name, count in m["counts"].items():
        if count:
            print(f"  {name:32s} {count}")
    missing = [name for name, count in m["counts"].items() if not count]
    if missing:
        print(f"  not yet emitted: {', '.join(missing)}")
    for note in m["not_yet_instrumented"]:
        print(f"  no measure yet: {note}")
    return 0


def cmd_configurations(args) -> int:
    """List the pinned configurations a comparison side can select (§4.16.1)."""
    from . import versions
    store = Store(args.store)
    configs = versions.list_configurations(store)
    if not configs:
        print(f"no runs in store {args.store!r}", file=sys.stderr)
        return 1
    for c in configs:
        selector = ",".join(f"{k}={v}" for k, v in c["selector"].items())
        print(f"{c['label']:24s} {c['run_count']:3d} runs · {c['task_count']:3d} tasks  [{selector}]")
    return 0


def cmd_compare_versions(args) -> int:
    """Matched version comparison of two configurations (§4.16 / §6.12 / §12.3).

    Prints the result as JSON: the surface that renders it is the browser, and the
    result carries numerators, denominators, counts, and intervals that a terminal
    table would only flatten.
    """
    from . import versions
    store = Store(args.store)
    try:
        if args.save:
            definition = versions.save_comparison(
                store, _selector_arg(args.baseline), _selector_arg(args.candidate),
                args.axis, name=args.name)
            result = versions.load_comparison(store, definition["comparison_id"])
        else:
            result = versions.compare_versions(
                store, _selector_arg(args.baseline), _selector_arg(args.candidate), args.axis)
    except versions.ComparisonError as exc:
        print(f"cannot build comparison: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pairs"] else 1


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
        from .model_reviewer import make_reviewer
        try:
            reviewer = make_reviewer(provider, model_id, getattr(args, "base_url", None))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
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

        from . import version
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
                "command": "agr eval",
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
    """The checked-out commit of this checkout, when git is available.

    Recorded so an eval manifest is auditable against the exact source that
    produced it; None when the checkout is not a git repo (e.g. an installed
    wheel run outside a checkout) — absent, never invented.
    """
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def _review_one(store: Store, run_id: str, reviewer):
    """Run the model-enriched pipeline over a run's latest capture (raises on failure)."""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        raise KeyError(f"run {run_id!r} not found in store")
    doc = store.read_source(run_id, capture_id)
    return analyze(doc, store, reviewer=reviewer)


def _review_all(store: Store, reviewer, settings: dict, force: bool = False) -> int:
    """Review every run in the store with one reviewer; per-run failures don't stop the sweep."""
    from . import read
    run_ids = [entry["run_id"] for entry in read.list_runs(store)]
    if not run_ids:
        print(f"no runs in store {store.root!r} — ingest something first", file=sys.stderr)
        return 1
    done = skipped = failed = 0
    for run_id in run_ids:
        if not force and _already_enriched(store, run_id):
            print(f"  · {run_id} — skipped (already model-enriched; use --force to redo)")
            skipped += 1
            continue
        try:
            analysis = _review_one(store, run_id, reviewer)
        except Exception as exc:  # noqa: BLE001 - one bad run must not kill the batch
            print(f"  ✗ {run_id} — model reviewer failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        moments = len(getattr(analysis, "review_moments", []) or [])
        print(f"  ✓ {run_id} — {moments} moment(s) · reviewer {reviewer.reviewer_key}")
        done += 1
    print(f"\nreviewed {done} · skipped {skipped} · failed {failed} "
          f"(provider {settings['provider']}, model {getattr(reviewer, 'model', '?')})")
    if failed and not done:
        print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
              file=sys.stderr)
        return 4
    return 0


def _already_enriched(store: Store, run_id: str) -> bool:
    """Does the run's latest capture already carry a model-enriched review slot?"""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        return False
    return any(key.startswith("model") for key in store.list_reviews(run_id, capture_id))


def cmd_review(args) -> int:
    """Run the Stage F model reviewer over a run (spec §8.7).

    Recomputes the deterministic pipeline but swaps the reviewer for a model
    adapter, so the persisted review moments carry the model's taxonomy verdicts,
    behaviour tags, ranked root causes, and better actions — every fact still
    recomputed (Stage G), attribution still capped (Stage H). Requires the matching
    provider extra and credentials; both failures exit cleanly with a hint.
    """
    store = Store(args.store)
    from .model_reviewer import make_reviewer
    from .userconfig import resolve_review_settings

    # Settings resolve --flag > $AGR_REVIEW_MODEL / $OPENAI_BASE_URL > saved
    # `agr config` file > the provider's built-in default.
    settings = resolve_review_settings(args.provider, args.model, args.base_url)
    try:
        reviewer = make_reviewer(settings["provider"], settings["model"], settings["base_url"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - auth/network error while constructing the client
        print(f"model reviewer failed: {exc}", file=sys.stderr)
        print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
              file=sys.stderr)
        return 4

    if getattr(args, "all", False):
        return _review_all(store, reviewer, settings, force=getattr(args, "force", False))

    if not args.run_id:
        print("nothing to review: give a run_id or use --all for the whole store", file=sys.stderr)
        return 1
    try:
        analysis = _review_one(store, args.run_id, reviewer)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:  # the provider SDK (optional extra) is not installed
        print(f"cannot run model reviewer: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - provider/auth/network error surfaced to the user
        print(f"model reviewer failed: {exc}", file=sys.stderr)
        print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
              file=sys.stderr)
        return 4
    _print_show(analysis)
    return 0


def cmd_config(args) -> int:
    """Show / save / validate the model-reviewer settings used by `agr review`.

    One-time setup so `agr review <id>` and `agr review --all` work without
    flags: `agr config --provider openai --model <id> --base-url <url>`.
    `--test` makes one real completion call against the effective settings
    (flag > env > saved file) and reports exactly what it reached.
    """
    from .userconfig import clear_config, config_path, load_config, resolve_review_settings, save_config

    if args.clear:
        if clear_config():
            print(f"cleared saved reviewer settings ({config_path()})")
        else:
            print("no saved reviewer settings to clear")
        return 0

    saving = any(v is not None for v in (args.provider, args.model, args.base_url))
    if saving:
        if args.provider is not None and args.provider not in ("anthropic", "openai"):
            print(f"unknown provider {args.provider!r}; choose from ['anthropic', 'openai']",
                  file=sys.stderr)
            return 2
        cfg = save_config(args.provider, args.model, args.base_url)
        print(f"saved reviewer settings to {config_path()}")
    else:
        cfg = load_config()

    effective = resolve_review_settings(args.provider, args.model, args.base_url)
    print(f"\nreviewer settings (effective, flag > env > {config_path()} > provider default):")
    print(f"  provider: {effective['provider']}")
    print(f"  model:    {effective['model'] or '(provider default)'}")
    print(f"  base_url: {effective['base_url'] or '(provider default)'}")
    if saving and cfg.get("updated_at"):
        print(f"  saved:    {cfg['updated_at']}")

    if not args.test:
        return 0

    # One real completion call — validates SDK install, credentials, endpoint,
    # and model id in a single cheap round trip.
    from .model_reviewer import make_reviewer
    try:
        reviewer = make_reviewer(effective["provider"], effective["model"], effective["base_url"])
    except ValueError as exc:
        print(f"\nconfiguration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - auth/network error while constructing the client
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("hints: check the API key env (ANTHROPIC_API_KEY / OPENAI_API_KEY), "
              "the base_url, and that the model id exists at that endpoint", file=sys.stderr)
        return 4
    print(f"\ntesting {effective['provider']} · {effective['model'] or '(default model)'}")
    try:
        reviewer._complete("You are a connectivity test.",
                           '{"self_test": "reply with the JSON object {"ok": true}"}')
    except RuntimeError as exc:  # the provider SDK (optional extra) is not installed
        print(f"MISSING SDK: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - auth/network/model errors surface here
        print(f"FAILED: {exc}", file=sys.stderr)
        print("hints: check the API key env (ANTHROPIC_API_KEY / OPENAI_API_KEY), "
              "the base_url, and that the model id exists at that endpoint", file=sys.stderr)
        return 4
    print("OK — the reviewer endpoint is reachable and the model responded.")
    return 0


def cmd_serve(args) -> int:
    """Serve the read API over HTTP (requires the optional ``api`` extra)."""
    try:
        import uvicorn  # noqa: F401
        from .api import create_app
    except (ModuleNotFoundError, RuntimeError) as exc:
        print(f"cannot start server: {exc}", file=sys.stderr)
        print("install the API extras with:  pip install .[api]", file=sys.stderr)
        return 3
    app = create_app(args.store)
    print(f"serving read API for store {args.store!r} at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_confirmer() -> Optional[str]:
    """Best-effort confirmer identity from the environment (cross-platform)."""
    for var in ("AGR_CONFIRMED_BY", "USER", "USERNAME", "LOGNAME"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agr", description="Agent Game Review — deterministic core")
    # Resolved to .agr-store in main(); None lets commands tell an explicit
    # choice from the default (the eval harness isolates its scratch store).
    p.add_argument("--store", default=None, help="store directory (default: .agr-store)")
    p.add_argument("--env-file", default=None,
                   help="path to a .env file to load (default: ./.env or $AGR_DOTENV; "
                        "session/OS env vars still take precedence)")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("ingest", help="ingest an ATIF document")
    pi.add_argument("path", help="path to an ATIF .json document")
    pi.set_defaults(func=cmd_ingest)

    def _add_adapter_args(sp) -> None:
        """The identity/evidence inputs the core never invents (docs/adapters.md)."""
        sp.add_argument("--task-id", help="benchmark task id (default: derived from source)")
        sp.add_argument("--instruction", help="task instruction (default: first user message)")
        sp.add_argument("--run-id", help="logical run id (default: derived from source)")
        sp.add_argument("--verifier", help="path to a verifier sidecar .json (raw_output + checks)")
        sp.add_argument("--sweep-id", help="sweep identity for cross-run surfaces")
        sp.add_argument("--configuration-id", help="pinned configuration identity")

    ph = sub.add_parser(
        "ingest-harbor",
        help="ingest Harbor/Terminal-Bench 2.0 output — trial dir, trajectory.json, "
             "or a whole job directory of trials")
    ph.add_argument("path",
                    help="trial directory, trajectory.json, or job directory (batch)")
    _add_adapter_args(ph)
    ph.set_defaults(func=cmd_ingest_harbor)

    pf = sub.add_parser("ingest-from",
                        help="convert a harness log to ATIF via a named adapter and ingest it")
    pf.add_argument("--adapter", required=True, choices=adapter_names(),
                    help="adapter to convert the source with (registry order is support "
                         "priority: eval-framework sources first)")
    pf.add_argument("path", help="path to the harness log")
    _add_adapter_args(pf)
    pf.set_defaults(func=cmd_ingest_from)

    pp = sub.add_parser("ingest-pi", help="ingest a pi session (.jsonl) via the pi adapter")
    pp.add_argument("path", help="path to a pi session .jsonl file")
    _add_adapter_args(pp)
    pp.set_defaults(func=cmd_ingest_pi)

    ps = sub.add_parser("show", help="show the deterministic review for a run")
    ps.add_argument("run_id", help="logical run id")
    ps.set_defaults(func=cmd_show)

    pc = sub.add_parser("confirm", help="record a human confirmation of the task contract")
    pc.add_argument("run_id", help="logical run id")
    pc.add_argument("--all", action="store_true", help="confirm every contract item")
    pc.add_argument("--decisions", help="path to a JSON confirmation record")
    pc.add_argument("--by", help="name of the confirming reviewer")
    pc.set_defaults(func=cmd_confirm)

    pr = sub.add_parser("runs", help="list every run in the store")
    pr.set_defaults(func=cmd_runs)

    pd = sub.add_parser("disposition", help="record a human review disposition on a run (§4.3.4)")
    pd.add_argument("run_id", help="logical run id")
    pd.add_argument("--disposition", choices=list(workflow.DISPOSITIONS),
                    help="review disposition to set")
    pd.add_argument("--progress", choices=list(workflow.REVIEW_PROGRESS),
                    help="review progress to set ('handled' requires a disposition)")
    pd.add_argument("--assign", help="assignee to set")
    pd.add_argument("--note", help="optional note")
    pd.add_argument("--by", help="name of the reviewer (default: env identity)")
    pd.add_argument("--base-version", type=int, default=None,
                    help="optimistic version to write against (default: current)")
    pd.set_defaults(func=cmd_disposition)

    pm = sub.add_parser("metrics",
                        help="derived product measures over the analytics log (§4.21)")
    pm.set_defaults(func=cmd_metrics)

    pds = sub.add_parser("demo-store",
                         help="build a synthetic two-configuration slice to demo §4.16")
    pds.add_argument("--fixtures", default=None,
                     help="ATIF fixtures directory (default: built-in package data)")
    pds.set_defaults(func=cmd_demo_store)

    pdemo = sub.add_parser("demo",
                           help="[five-minute path] build demo store and serve the evidence browser")
    pdemo.add_argument("--host", default=os.environ.get("AGR_HOST", "127.0.0.1"),
                       help="bind host (default: $AGR_HOST, else 127.0.0.1; use 0.0.0.0 in a container)")
    pdemo.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                       help="bind port (default: $PORT, else 8000)")
    pdemo.add_argument("--fixtures", default=None,
                       help="ATIF fixtures directory (default: built-in package data)")
    pdemo.set_defaults(func=cmd_demo)

    pcf = sub.add_parser("configurations",
                         help="list the pinned configurations available to compare (§4.16.1)")
    pcf.set_defaults(func=cmd_configurations)

    pcv = sub.add_parser("compare-versions",
                         help="matched comparison of two configurations on one task slice (§4.16)")
    pcv.add_argument("--baseline", required=True, metavar="FIELD=VALUE",
                     help="baseline side selector, e.g. sweep_id=sweep_141")
    pcv.add_argument("--candidate", required=True, metavar="FIELD=VALUE",
                     help="candidate side selector, e.g. sweep_id=sweep_142")
    pcv.add_argument("--axis", required=True, choices=list(_CHANGE_AXES),
                     help="the intended changed axis; anything more makes it a "
                          "configuration comparison rather than a matched one")
    pcv.add_argument("--save", action="store_true",
                     help="freeze the definition in the store and print its comparison id")
    pcv.add_argument("--name", default=None, help="human name for a saved comparison")
    pcv.set_defaults(func=cmd_compare_versions)

    pe = sub.add_parser("eval", help="score the reviewer against the gold set "
                                     "(deterministic baseline, or --provider to compare a model)")
    pe.add_argument("--gold", default="archive/synthetic/gold", help="gold set directory (default: archived synthetic gold)")
    pe.add_argument("--fixtures", default="archive/synthetic/fixtures", help="ATIF fixtures directory (default: archived synthetic fixtures)")
    pe.add_argument("--provider", default=None, choices=["anthropic", "openai"],
                    help="also score a model reviewer and compare it to the baseline "
                         "(needs the matching 'model-*' extra + a key)")
    pe.add_argument("--model", default=None,
                    help="model id for --provider (default: $AGR_REVIEW_MODEL or the provider default)")
    pe.add_argument("--base-url", default=None,
                    help="OpenAI/Anthropic-compatible endpoint for --provider (any model)")
    pe.add_argument("--manifest", default=None, metavar="PATH",
                    help="write a reproducibility manifest (versions, gold hashes, "
                         "adjudication status, invocation) to PATH (AGR-07)")
    pe.set_defaults(func=cmd_eval)

    prv = sub.add_parser(
        "review", help="run the Stage F model reviewer over a run (needs a 'model-*' extra + key)")
    prv.add_argument("run_id", nargs="?", default=None,
                     help="logical run id (omit when --all)")
    prv.add_argument("--all", action="store_true",
                     help="review every run in the store (skips runs already model-enriched)")
    prv.add_argument("--force", action="store_true",
                     help="with --all: re-review runs that already carry a model review")
    prv.add_argument("--provider", default=None, choices=["anthropic", "openai"],
                     help="model provider (default: saved 'agr config' provider, else anthropic)")
    prv.add_argument("--model", default=None,
                     help="model id (default: $AGR_REVIEW_MODEL, else saved 'agr config' "
                          "model, else the provider's default, e.g. claude-opus-4-8)")
    prv.add_argument("--base-url", default=None,
                     help="OpenAI/Anthropic-compatible endpoint URL (use any model: "
                          "OpenRouter, Together, a local vLLM/Ollama server, …). "
                          "Falls back to OPENAI_BASE_URL for --provider openai.")
    prv.set_defaults(func=cmd_review)

    pcfg = sub.add_parser(
        "config", help="show / save / validate the reviewer settings used by 'agr review'")
    pcfg.add_argument("--provider", default=None, choices=["anthropic", "openai"],
                      help="save this provider for future 'agr review' runs")
    pcfg.add_argument("--model", default=None,
                      help="save this model id for future 'agr review' runs")
    pcfg.add_argument("--base-url", default=None,
                      help="save this endpoint URL (OpenRouter, Ollama, vLLM, …)")
    pcfg.add_argument("--test", action="store_true",
                      help="make one real completion call with the effective settings")
    pcfg.add_argument("--clear", action="store_true", help="delete the saved settings")
    pcfg.set_defaults(func=cmd_config)

    pv = sub.add_parser("serve", help="serve the read API over HTTP (needs the 'api' extra)")
    pv.add_argument("--host", default=os.environ.get("AGR_HOST", "127.0.0.1"),
                    help="bind host (default: $AGR_HOST, else 127.0.0.1; use 0.0.0.0 in a container)")
    pv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                    help="bind port (default: $PORT, else 8000)")
    pv.set_defaults(func=cmd_serve)
    return p


def _load_env(env_file: Optional[str]) -> None:
    """Load ``.env`` via python-dotenv when it's installed (ships with the model
    extras). Real session/OS env vars take precedence (``override=False``). The
    deterministic commands need no credentials, so if python-dotenv is absent this
    is silently skipped rather than made a hard dependency of the core."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(env_file or find_dotenv(usecwd=True), override=False)


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.store_explicit = args.store is not None
    if args.store is None:
        args.store = ".agr-store"
    _load_env(getattr(args, "env_file", None))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
