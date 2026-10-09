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


def cmd_synthesize_verifier(args) -> int:
    """Extract in-session pytest/npm test/cargo test/go test invocations and
    their results into a --verifier sidecar (item 25, 2026-09-08).

    Converts the source with the named adapter (no ingest — this is a pure
    preprocessing step) and writes exactly the sidecar shape --verifier
    already accepts, so an otherwise UNVERIFIED session ingests PROVISIONAL
    with real atomic checks: `agr ingest-from --adapter ... --verifier
    <output>`. Recognises only a closed, versioned set of test-runner
    invocations and output shapes — an unrecognised one produces no check,
    never a guessed pass/fail.
    """
    from .adapter import get_adapter
    from .verifier_synth import synthesize_verifier

    adapter = get_adapter(args.adapter)
    result = adapter.convert(args.path, task_id=args.task_id)
    for w in result.warnings:
        print(f"  adapter warning: {w}")
    verifier = synthesize_verifier(result.doc)
    if verifier is None:
        print("no recognised test invocation with a parseable result found; "
              "no sidecar written", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(verifier, fh, indent=2)
    print(f"wrote {len(verifier['checks'])} check(s) to {args.output!r}")
    for check in verifier["checks"]:
        print(f"  {check['check_id']}: {check['status']} — {check['name']}")
    return 0


def cmd_run_sweep(args) -> int:
    """Run a task set through claude -p and ingest every result (item 26,
    2026-09-08). Meant to be invoked by a scheduler (cron, a systemd timer)
    so the store accumulates comparable runs under one configuration_id
    across repeated sweeps — this command itself does not schedule anything.
    """
    from . import runner

    try:
        task_set = runner.TaskSet.load(args.task_set)
    except (ValueError, OSError, KeyError) as exc:
        print(f"failed to load task set {args.task_set!r}: {exc}", file=sys.stderr)
        return 1
    store = Store(args.store)
    results = runner.run_sweep(task_set, store, keep_workdir=args.keep_workdir)
    failed = 0
    for r in results:
        if r.error:
            failed += 1
            # A timeout still sets error, but its partial transcript may
            # have been ingested as its own stored, reviewable run — say so
            # rather than leaving that run_id undiscoverable from this output.
            extra = f" (partial capture ingested as run_id={r.run_id})" if r.run_id else ""
            print(f"  {r.task_id}: ERROR — {r.error}{extra}", file=sys.stderr)
        else:
            print(f"  {r.task_id}: ingested={r.ingested} verifier={r.verifier_status} run_id={r.run_id}")
    print(f"{len(results) - failed}/{len(results)} task(s) ingested successfully")
    return 1 if failed else 0


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


def cmd_ingest_langfuse_api(args) -> int:
    """Fetch one Langfuse trace by id from the live API, then ingest it.

    A thin composition of two already-separate, already-tested seams:
    ``agr.langfuse_api.fetch_trace`` (the only network access in this whole
    path) and the Langfuse adapter's ``convert()`` (which accepts the
    fetched flat dict directly — no intermediate file, unlike ``ingest-from``
    which always reads a path). Everything after that point — warnings,
    ``_ingest_doc``'s printed summary — is identical to ``ingest-from
    --adapter langfuse``, so the two paths stay behaviourally in sync.

    Credentials/host resolve inside ``fetch_trace`` (``--host`` here, else
    ``$LANGFUSE_HOST``/``$LANGFUSE_PUBLIC_KEY``/``$LANGFUSE_SECRET_KEY``) —
    there is deliberately no ``--public-key``/``--secret-key`` flag, so a
    secret can never end up in shell history or a process listing; only the
    host, which is not sensitive, is ever a CLI argument. The secret key
    itself is never printed by anything on this path.
    """
    from .ingest_langfuse import LANGFUSE_ADAPTER
    from .langfuse_api import LangfuseAPIError, fetch_trace

    try:
        trace = fetch_trace(args.trace_id, host=args.host)
    except (ValueError, LangfuseAPIError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    result = LANGFUSE_ADAPTER.convert(
        trace,
        task_id=args.task_id,
        instruction=args.instruction,
        run_id=args.run_id,
        sweep_id=args.sweep_id,
        configuration_id=args.configuration_id,
    )
    for w in result.warnings:
        print(f"  adapter warning: {w}")
    store = Store(args.store)
    return _ingest_doc(result.doc, store)


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


def _format_diagnostic_evidence(ev) -> list[str]:
    """Human-readable lines for a check's ``diagnostic_evidence``.

    Diagnostic, never a verdict: this is why an aggregate check reached its
    status. tau3-bench attaches the tau2 reward breakdown here — which DB
    field mismatched, which actions failed — behind the one pass/fail check.
    """
    if not isinstance(ev, dict):
        return []
    lines: list[str] = []
    basis = ev.get("reward_basis")
    if basis:
        lines.append("reward basis: " + ", ".join(str(b) for b in basis))
    db = ev.get("db_check")
    if isinstance(db, dict) and "db_match" in db:
        lines.append("database: " + ("match" if db.get("db_match") else "MISMATCH"))
    actions = ev.get("action_checks")
    if isinstance(actions, list) and actions:
        failed = [a for a in actions if isinstance(a, dict) and a.get("action_match") is False]
        lines.append(f"actions: {len(actions)} checked, {len(failed)} failed")
        for a in failed[:5]:
            act = a.get("action") if isinstance(a.get("action"), dict) else {}
            name = act.get("name") or "action"
            kind = a.get("tool_type")
            lines.append("  - " + name + (f" ({kind})" if kind else ""))
    nls = ev.get("nl_assertions")
    if isinstance(nls, list) and nls:
        unmet = [x for x in nls if isinstance(x, dict) and x.get("met") is False]
        lines.append(f"nl assertions: {len(nls)} checked, {len(unmet)} unmet")
        for x in unmet[:3]:
            lines.append("  - " + str(x.get("justification") or x.get("assertion") or "unmet"))
    comms = ev.get("communicate_checks")
    if isinstance(comms, list) and comms:
        unmet = [x for x in comms if isinstance(x, dict) and x.get("met") is False]
        lines.append(f"communicate checks: {len(comms)} checked, {len(unmet)} unmet")
    diff = ev.get("state_diff")
    if isinstance(diff, list) and diff:
        lines.append(f"state diff: {len(diff)} path(s)")
        for d in [d for d in diff if isinstance(d, dict)][:5]:
            lines.append(f"  - {d.get('path')} ({d.get('change_type') or 'changed'}; "
                         f"writer: {d.get('writer_status') or 'unknown'})")
    return lines


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
        for detail in _format_diagnostic_evidence(c.diagnostic_evidence):
            print(f"      {detail}")
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


def cmd_audit_integrity(args) -> int:
    """Report the evidence-integrity audit for one served review.

    Consolidates the grounding checks the reviewer already ran (references,
    quotes, fact recomputation, rejected proposals) and reports their coverage.
    A mechanical pass is not a claim that the review is true — the report says
    so explicitly (see ``agr.integrity``).
    """
    from . import integrity

    store = Store(args.store)
    if store.latest_capture_id(args.run_id) is None:
        print(f"run {args.run_id!r} not found in store {args.store!r}", file=sys.stderr)
        return 1
    report = integrity.audit_run(store, args.run_id, getattr(args, "reviewer", None))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(integrity.format_text(report))
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


def cmd_task(args) -> int:
    """Per-task, multi-attempt view: every attempt of one task side by side.

    Answers "where do failing attempts split from passing ones" at a glance:
    each attempt's outcome plus the verifier breakdown behind it (DB match,
    failed actions with their read/write type, unmet assertions). Attempts are
    ordered passing-first so a failing row's breakdown sits next to the passing
    rows it differs from. Attempts are bucketed with the same outcome buckets
    the rest of AGR uses (pass / fail / undetermined / unverified /
    operational_error): an operational error or an undetermined run is never
    counted as a plain failure.
    """
    from . import read
    from .queue import outcome_bucket

    store = Store(args.store)
    summaries = [s for s in read.list_runs(store) if s.get("task_id") == args.task_id]
    if not summaries:
        print(f"no runs for task {args.task_id!r} in store {args.store!r}", file=sys.stderr)
        return 1

    # pass first, then the softer non-passes, failures last, so a failing row's
    # breakdown sits next to the passing rows it differs from.
    _order = {"pass": 0, "operational_error": 1, "undetermined": 2,
              "unverified": 3, "fail": 4}

    def _bucket(s) -> str:
        return outcome_bucket((s["outcome"] or {}).get("status"))

    summaries.sort(key=lambda s: (_order.get(_bucket(s), 9), s["run_id"]))
    buckets: dict[str, int] = {}
    for s in summaries:
        buckets[_bucket(s)] = buckets.get(_bucket(s), 0) + 1
    labels = {"pass": "passed", "fail": "failed", "undetermined": "undetermined",
              "unverified": "unverified", "operational_error": "operational errors"}
    # Every bucket present is shown, in a fixed order; an unknown bucket prints
    # its raw name rather than being silently dropped from the header.
    shown = [k for k in _order if buckets.get(k)]
    shown += [k for k in buckets if k not in _order]
    parts = [f"{buckets[k]} {labels.get(k, k)}" for k in shown]
    print(f"{args.task_id}  ·  {len(summaries)} attempts · " + " · ".join(parts))
    print()
    for s in summaries:
        o = s["outcome"] or {}
        rid = s["run_id"]
        short = rid.rsplit("__", 1)[-1][:12]
        status = o.get("status") or "?"
        counts = f"{o.get('passed')}/{o.get('total')}" if o.get("passed") is not None else "?/?"
        print(f"  [{status} {counts}]  {short}")
        review = read.get_review(store, rid)
        for c in review.get("checks", []):
            for detail in _format_diagnostic_evidence(c.get("diagnostic_evidence")):
                print(f"        {detail}")
    return 0


def cmd_episodes(args) -> int:
    """Fleet view over recovery episodes across every run (item 30, 2026-09-08).

    Groups every run's persisted recovery episodes by tool and/or error
    signature, so a repeated failure across many runs is visible as ONE row
    instead of scattered across per-run reviews.
    """
    from . import fleet
    store = Store(args.store)
    group_by = [d.strip() for d in args.group_by.split(",") if d.strip()] if args.group_by else None
    try:
        groups = fleet.fleet_episodes(store, group_by=group_by)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not groups:
        print(f"no recovery episodes in store {args.store!r}", file=sys.stderr)
        return 1
    for g in groups:
        key = " / ".join(str(k) for k in g.key)
        turns = f"{g.avg_turns_to_resolve:.1f}" if g.avg_turns_to_resolve is not None else "?"
        print(f"{key}  count={g.count} runs={g.distinct_runs} repeat_rate={g.repeat_rate:.2f} "
              f"unrecovered={g.unrecovered_share:.0%} avg_turns={turns} "
              f"tokens={g.total_tokens} wall_ms={g.total_wall_ms}")
    return 0


def cmd_argument_shapes(args) -> int:
    """Argument-shape distribution per failing-call signature (item 31, 2026-09-08).

    For every (tool, error_signature) group, the KEY SET and value TYPE the
    model sent on failing calls — never the retained values themselves.
    """
    from . import argument_shapes
    store = Store(args.store)
    groups = argument_shapes.argument_shapes(store, min_group_size=args.min_group_size)
    if not groups:
        print(f"no failing calls with retained tool_input in store {args.store!r}", file=sys.stderr)
        return 1
    for g in groups:
        print(f"{g.key[0]} / {g.key[1]}  ({g.total_failing_calls} failing calls, "
              f"{len(g.shapes)} distinct shape(s))")
        for s in g.shapes:
            keys = ", ".join(f"{k}:{t}" for k, t in s["keys"])
            print(f"    {s['share']:.0%} ({s['count']}x)  {{{keys}}}")
    return 0


def cmd_backfill_execution_quality(args) -> int:
    """Provision execution_quality.json for captures ingested before it existed.

    One-time (idempotent) migration: a whole pre-feature store would otherwise
    recompute its execution-quality summary on every read.
    """
    from .execution_quality import backfill_execution_quality
    store = Store(args.store)
    written = backfill_execution_quality(store)
    if written:
        print(f"wrote execution_quality.json for {written} capture(s) in {args.store!r}")
    else:
        print(f"every capture in {args.store!r} already has execution_quality.json")
    return 0


def _selector_arg(raw: str) -> dict:
    field, _, value = raw.partition("=")
    if not field or not value:
        raise SystemExit(f"selector must be field=value, got {raw!r}")
    return {field: value}


def _build_demo(store: Store, args) -> dict:
    """Build the real demo when its dataset is present, else the synthetic slice.

    GR-4: the real demo (real Terminal-Bench runs + their pre-computed model
    reviews) is the default because it is what ``agr demo`` is meant to open on.
    ``--synthetic`` forces the old behavior. Either way the synthetic comparison
    slice is included so the Compare surface still has matched data.
    """
    from . import demo
    if not getattr(args, "synthetic", False):
        real_dir = demo.find_real_demo_dir(getattr(args, "real_dir", None))
        if real_dir is not None:
            definition = demo.build_real_demo_store(store, real_dir)
            print(f"Built the real demo store in {args.store!r} (real Terminal-Bench runs)")
            print(f"  real runs     {definition['real_runs']}")
            print(f"  model reviews {definition['model_reviews']} (pre-computed; no API key)")
            if definition.get("stale_reviews"):
                print(f"  WARNING      {definition['stale_reviews']} review(s) skipped: "
                      "source hash does not match the run (re-bake with agr bake-reviews)",
                      file=sys.stderr)
            if definition.get("landing_run"):
                print(f"  opens on      {definition['landing_run']}")
            print(f"  comparison    {definition['axis']}")
            return definition
    fixtures_dir = demo.find_fixtures_dir(getattr(args, "fixtures", None))
    definition = demo.build_demo_store(store, fixtures_dir)
    print(f"Built a synthetic demo slice in {args.store!r} (source_type=synthetic_demo)")
    print(f"  baseline   {definition['baseline']}")
    print(f"  candidate  {definition['candidate']}")
    print(f"  axis       {definition['axis']}")
    return definition


def cmd_demo_store(args) -> int:
    """Build the demo store (GR-4: real runs + pre-computed reviews, else synthetic)."""
    store = Store(args.store)
    _build_demo(store, args)
    print()
    print("  python3 -m agr serve --store " + args.store)
    print("  then: Compare versions (V) -> Preview match")
    return 0


def cmd_demo(args) -> int:
    """Build the demo store and start the server — the five-minute path.

    One command from clone to browsing the demo:

        pip install .[api]
        agr demo

    Builds the real Terminal-Bench demo (with pre-computed model reviews, so no
    API key is needed), then starts the read API server so you can open the
    evidence-browser SPA in your browser. All contracts are auto-confirmed so no
    watermarks appear.
    """
    # Default to a dedicated demo store so the working store is not polluted.
    # User can override via the global --store argument.
    if not args.store_explicit and args.store == ".agr-store":
        args.store = ".agr-demo"
    store = Store(args.store)
    definition = _build_demo(store, args)
    landing = definition.get("landing_run")
    if landing:
        print(f"  deep link  http://{args.host}:{args.port}/?run={landing}")
    print()
    _start_server(args)
    return 0


def cmd_bake_reviews(args) -> int:
    """GR-4: export real runs + their pre-computed model reviews into a dataset.

    Run after scoring the corpus with the model reviewer, e.g.
    ``agr review --all --provider … --model …``. The committed ``runs/`` and
    ``reviews/`` let ``agr demo`` reproduce the reviews with no credential.
    """
    from . import demo
    store = Store(args.store)
    try:
        result = demo.bake_reviews(store, args.out, model=args.model)
    except ValueError as exc:
        print(f"cannot bake reviews: {exc}", file=sys.stderr)
        return 2
    print(f"Baked {result['runs']} review(s) into {result['out_dir']!r} "
          f"(reviewer model {result['model']})")
    print("  runs/    real trajectory sources")
    print("  reviews/ pre-computed reviews (moments + reviewer model + date)")
    return 0 if result["runs"] else 1


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


_LEGACY_COMMANDS = ("eval", "benchmark", "audit-pack", "publish-evaluation")


def cmd_legacy_moved(args) -> int:
    """Stub for evaluation commands that moved out of the core CLI."""
    print(f"`agr {args.command}` moved out of the core CLI. Run it as:\n"
          f"    python -m legacy {args.command} ...\n"
          "from a repository checkout, in the repository root (or with PYTHONPATH set to it);\n"
          "it is not part of the installed package (see legacy/README.md).", file=sys.stderr)
    return 2


def _review_one(store: Store, run_id: str, reviewer):
    """Run the model-enriched pipeline over a run's latest capture (raises on failure)."""
    with store.index_lock(run_id):
        capture_id = store.latest_capture_id(run_id)
        if capture_id is None:
            raise KeyError(f"run {run_id!r} not found in store")
        doc = store.read_source(run_id, capture_id)
    return analyze(doc, store, reviewer=reviewer, expected_capture_id=capture_id)


def _review_all(store: Store, reviewer, settings: dict, force: bool = False) -> int:
    """Review every run in the store with one reviewer; per-run failures don't stop the sweep."""
    from . import read
    run_ids = [entry["run_id"] for entry in read.list_runs(store)]
    if not run_ids:
        print(f"no runs in store {store.root!r} — ingest something first", file=sys.stderr)
        return 1
    done = skipped = failed = 0
    for run_id in run_ids:
        if not force and _already_enriched(store, run_id, reviewer.reviewer_key):
            print(f"  · {run_id} — skipped (already model-enriched; use --force to redo)")
            skipped += 1
            continue
        try:
            analysis = _review_one(store, run_id, reviewer)
        except Exception as exc:  # noqa: BLE001 - one bad run must not kill the batch
            print(f"  ✗ {run_id} — model reviewer failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        # P0-3: analyze() falls back to the deterministic baseline instead of
        # raising when the model reviewer errors (AGR-06) — a run whose
        # Analysis carries review_error never ran the model, so it counts as
        # failed, not as a served review.
        if analysis.review_error is not None:
            print(f"  ✗ {run_id} — model reviewer failed: "
                  f"{analysis.review_error.get('message', analysis.review_error)}", file=sys.stderr)
            failed += 1
            continue
        # GR-1: an early stop (budget, timeout, missing chunks) is not a served
        # review either — count it as failed and name the reason.
        if analysis.review_incomplete is not None:
            print(f"  ✗ {run_id} — model reviewer stopped early: "
                  f"{analysis.review_incomplete.get('reason', analysis.review_incomplete)}", file=sys.stderr)
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


def _already_enriched(store: Store, run_id: str, reviewer_key=None) -> bool:
    """Does the run's latest capture already carry a model-enriched review slot?"""
    capture_id = store.latest_capture_id(run_id)
    if capture_id is None:
        return False
    keys = store.list_reviews(run_id, capture_id)
    from .review_identity import matching_review_key
    return bool(matching_review_key(store, run_id, capture_id, reviewer_key)) if reviewer_key else any(key.startswith("model") for key in keys)




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
    from .userconfig import effective_review_config

    # A bare usage error is checked before setting up a reviewer — no point
    # demanding credentials/SDK for a command that has nothing to review.
    if not getattr(args, "all", False) and not args.run_id:
        print("nothing to review: give a run_id or use --all for the whole store", file=sys.stderr)
        return 1

    # Settings resolve --flag > $AGR_REVIEW_MODEL / $OPENAI_BASE_URL > saved
    # `agr config` file > the provider's built-in default.
    settings = effective_review_config(provider=args.provider, model=args.model, base_url=args.base_url,
        cost_budget_usd=getattr(args, "cost_budget", None),
        time_budget_s=getattr(args, "time_budget", None),
        request_timeout_s=getattr(args, "request_timeout", None))
    try:
        reviewer = make_reviewer(
            settings["provider"], settings["model"], settings["base_url"],
            cost_budget_usd=settings["cost_budget_usd"],
            time_budget_s=settings["time_budget_s"],
            request_timeout_s=settings["request_timeout_s"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:  # the provider SDK (optional extra) is not installed
        print(f"cannot run model reviewer: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - auth/network error while constructing the client
        print(f"model reviewer failed: {exc}", file=sys.stderr)
        print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
              file=sys.stderr)
        return 4

    if getattr(args, "all", False):
        return _review_all(store, reviewer, settings, force=getattr(args, "force", False))

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
    # P0-3: a caught model failure inside analyze() serves the deterministic
    # fallback instead of raising (AGR-06) — treat that the same as any other
    # review failure rather than reporting success.
    if analysis.review_error is not None:
        print(f"model reviewer failed: "
              f"{analysis.review_error.get('message', analysis.review_error)}", file=sys.stderr)
        print("check credentials (ANTHROPIC_API_KEY / OPENAI_API_KEY) and connectivity",
              file=sys.stderr)
        return 4
    # GR-1: stopped early on the review's own budget/timeout — the run was not
    # actually reviewed, so it does not report success.
    if analysis.review_incomplete is not None:
        print(f"model reviewer stopped early: "
              f"{analysis.review_incomplete.get('reason', analysis.review_incomplete)}"
              " — the deterministic baseline is served", file=sys.stderr)
        return 4
    _print_show(analysis)
    return 0


def cmd_config(args) -> int:
    """Inspect, save, or explicitly test shared local reviewer settings."""
    from .userconfig import clear_config, config_path, effective_review_config, save_config
    from .model_reviewer import make_reviewer, test_connection
    if args.clear:
        print("cleared saved reviewer settings" if clear_config() else "no saved reviewer settings to clear")
        return 0
    changes = {"provider": args.provider, "model": args.model, "base_url": args.base_url,
               "cost_budget_usd": args.cost_budget, "time_budget_s": args.time_budget,
               "request_timeout_s": args.request_timeout}
    effective = effective_review_config(**changes)
    print(f"reviewer settings ({config_path()}):")
    for field in ("provider", "model", "base_url", "cost_budget_usd", "time_budget_s", "request_timeout_s"):
        print(f"  {field}: {effective[field]} ({effective['origins'][field]})")
    if args.test:
        try:
            reviewer = make_reviewer(**{k: effective[k] for k in changes})
            test_connection(reviewer)
        except RuntimeError as exc:
            from .model_reviewer import ModelOutputError, ProviderRequestError
            if isinstance(exc, (ModelOutputError, ProviderRequestError)):
                print(f"FAILED: {exc}", file=sys.stderr)
                return 4
            print(f"MISSING SDK: {exc}", file=sys.stderr)
            return 3
        except Exception:
            print("FAILED: check the API key, endpoint, model ID, and JSON response support.", file=sys.stderr)
            return 4
        print("OK — the endpoint returned the expected JSON response. No trace was sent.")
    # Combined --test/save does not activate settings that failed the test.
    if any(value is not None for value in changes.values()):
        save_config(**changes)
        print(f"saved reviewer settings to {config_path()}")
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
    # P0-5: --read-only takes the flag; otherwise create_app falls back to the
    # AGR_READ_ONLY env var, so a container platform can set it without a
    # code-level flag.
    read_only = True if getattr(args, "read_only", False) else None
    from .workspace_api import loopback
    allowed_hosts = list(getattr(args, "allowed_host", None) or [])
    if args.host not in ("0.0.0.0", "::"):
        allowed_hosts.append(args.host)
    app = create_app(args.store, read_only=read_only,
                     allowed_hosts=allowed_hosts,
                     session_token=os.environ.get("AGR_SESSION_TOKEN"),
                     require_session=not loopback(args.host))
    mode = " (read-only)" if getattr(app.state, "agr_read_only", False) else ""
    print(f"serving read API for store {args.store!r} at http://{args.host}:{args.port}{mode}")
    if not app.state.agr_read_only and (not loopback(args.host) or getattr(args, "allowed_host", None)):
        browser_host = (getattr(args, "allowed_host", None) or [args.host])[0]
        if browser_host in ("0.0.0.0", "::"):
            browser_host = "127.0.0.1"
        if ":" in browser_host:
            browser_host = "[" + browser_host + "]"
        print(f"Open http://{browser_host}:{args.port}/#agr-session={app.state.workspace_session_token}")
        print("Use this session fragment on your HTTPS proxy URL; keep the link private.")
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

    psv = sub.add_parser(
        "synthesize-verifier",
        help="extract in-session pytest/npm/cargo/go test results into a --verifier sidecar")
    psv.add_argument("--adapter", required=True, choices=adapter_names(),
                     help="adapter to convert the source with (no ingest — pure preprocessing)")
    psv.add_argument("path", help="path to the harness log")
    psv.add_argument("--task-id", help="benchmark task id (passed through to the adapter)")
    psv.add_argument("--output", required=True, help="path to write the verifier sidecar .json")
    psv.set_defaults(func=cmd_synthesize_verifier)

    prs = sub.add_parser(
        "run-sweep",
        help="run a task set through claude -p and ingest every result")
    prs.add_argument("task_set", help="path to a task-set JSON file (configuration_id + tasks)")
    prs.add_argument("--keep-workdir", action="store_true",
                     help="do not delete each task's working directory afterward (debugging)")
    prs.set_defaults(func=cmd_run_sweep)

    pp = sub.add_parser("ingest-pi", help="ingest a pi session (.jsonl) via the pi adapter")
    pp.add_argument("path", help="path to a pi session .jsonl file")
    _add_adapter_args(pp)
    pp.set_defaults(func=cmd_ingest_pi)

    pla = sub.add_parser(
        "ingest-langfuse-api",
        help="fetch one Langfuse trace by id from the live API and ingest it")
    pla.add_argument("--trace-id", required=True, help="Langfuse trace id to fetch")
    pla.add_argument("--host", default=None,
                     help="Langfuse host, e.g. https://cloud.langfuse.com (default: "
                          "$LANGFUSE_HOST, else http://localhost:3000). Credentials are "
                          "never a CLI argument: set $LANGFUSE_PUBLIC_KEY / "
                          "$LANGFUSE_SECRET_KEY.")
    pla.add_argument("--task-id", help="benchmark task id (default: derived from the trace id)")
    pla.add_argument("--instruction", help="task instruction (default: the trace's captured input)")
    pla.add_argument("--run-id", help="logical run id (default: derived from the trace id)")
    pla.add_argument("--sweep-id", help="sweep identity for cross-run surfaces")
    pla.add_argument("--configuration-id", help="pinned configuration identity")
    pla.set_defaults(func=cmd_ingest_langfuse_api)

    ps = sub.add_parser("show", help="show the deterministic review for a run")
    ps.add_argument("run_id", help="logical run id")
    ps.set_defaults(func=cmd_show)

    pai = sub.add_parser("audit-integrity",
                         help="report what the grounding checks covered for one review")
    pai.add_argument("run_id", help="logical run id")
    pai.add_argument("--reviewer", help="reviewer key to audit (default: the served one)")
    pai.add_argument("--json", action="store_true", help="print the full report as JSON")
    pai.set_defaults(func=cmd_audit_integrity)

    pc = sub.add_parser("confirm", help="record a human confirmation of the task contract")
    pc.add_argument("run_id", help="logical run id")
    pc.add_argument("--all", action="store_true", help="confirm every contract item")
    pc.add_argument("--decisions", help="path to a JSON confirmation record")
    pc.add_argument("--by", help="name of the confirming reviewer")
    pc.set_defaults(func=cmd_confirm)

    pr = sub.add_parser("runs", help="list every run in the store")
    pr.set_defaults(func=cmd_runs)

    pt = sub.add_parser("task",
                        help="per-task attempts view: every attempt of one task with its verifier breakdown")
    pt.add_argument("task_id")
    pt.set_defaults(func=cmd_task)

    pe = sub.add_parser("episodes",
                        help="fleet view: group recovery episodes across every run")
    pe.add_argument("--group-by", default="tool,error_signature",
                    help="comma-separated grouping dimensions: tool, error (alias for "
                         "error_signature), error_signature (default: tool,error_signature)")
    pe.set_defaults(func=cmd_episodes)

    pas = sub.add_parser("argument-shapes",
                         help="argument-shape distribution per failing-call signature")
    pas.add_argument("--min-group-size", type=int, default=1,
                     help="only show groups with at least this many failing calls (default: 1)")
    pas.set_defaults(func=cmd_argument_shapes)

    pbeq = sub.add_parser(
        "backfill-execution-quality",
        help="write execution_quality.json for captures ingested before the detectors existed",
    )
    pbeq.set_defaults(func=cmd_backfill_execution_quality)

    pd = sub.add_parser("disposition", help="record a human review disposition on a run")
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
                        help="derived product measures over the analytics log")
    pm.set_defaults(func=cmd_metrics)

    pds = sub.add_parser("demo-store",
                         help="build the demo store (real runs + pre-computed reviews; --synthetic for the old slice)")
    pds.add_argument("--fixtures", default=None,
                     help="ATIF fixtures directory (default: built-in package data)")
    pds.add_argument("--real-dir", default=None,
                     help="real demo dataset dir (default: built-in package data)")
    pds.add_argument("--synthetic", action="store_true",
                     help="force the synthetic two-configuration slice instead of the real demo")
    pds.set_defaults(func=cmd_demo_store)

    pdemo = sub.add_parser("demo",
                           help="[five-minute path] build demo store and serve the evidence browser")
    pdemo.add_argument("--host", default=os.environ.get("AGR_HOST", "127.0.0.1"),
                       help="bind host (default: $AGR_HOST, else 127.0.0.1; use 0.0.0.0 in a container)")
    pdemo.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                       help="bind port (default: $PORT, else 8000)")
    pdemo.add_argument("--fixtures", default=None,
                       help="ATIF fixtures directory (default: built-in package data)")
    pdemo.add_argument("--real-dir", default=None,
                       help="real demo dataset dir (default: built-in package data)")
    pdemo.add_argument("--synthetic", action="store_true",
                       help="force the synthetic two-configuration slice instead of the real demo")
    pdemo.set_defaults(func=cmd_demo)

    pbr = sub.add_parser("bake-reviews",
                         help="GR-4: export real runs + pre-computed model reviews into a demo dataset")
    pbr.add_argument("--out", required=True,
                     help="output directory (writes runs/ and reviews/)")
    pbr.add_argument("--model", default=None,
                     help="reviewer-model substring to export (default: kimi-k3)")
    pbr.set_defaults(func=cmd_bake_reviews)

    pcf = sub.add_parser("configurations",
                         help="list the pinned configurations available to compare")
    pcf.set_defaults(func=cmd_configurations)

    pcv = sub.add_parser("compare-versions",
                         help="matched comparison of two configurations on one task slice")
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

    for name in _LEGACY_COMMANDS:
        pl = sub.add_parser(name, help="moved to `python -m legacy " + name + "` (see legacy/README.md)",
                            add_help=False)
        pl.set_defaults(func=cmd_legacy_moved)


    prv = sub.add_parser(
        "review", help="run the model reviewer over a run (needs a 'model-*' extra + key)")
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
    prv.add_argument("--cost-budget", type=float, default=None, metavar="USD",
                     help="estimated-cost budget target: a round that would START past "
                          "it is skipped (status: incomplete). Checked before each round, "
                          "so one in-flight round can finish over it. Default $0.15, "
                          "0 disables (also $AGR_REVIEW_COST_BUDGET_USD)")
    prv.add_argument("--time-budget", type=float, default=None, metavar="SECONDS",
                     help="elapsed-time budget target: a round that would START past it "
                          "is skipped (status: incomplete). Default 90, 0 disables "
                          "(also $AGR_REVIEW_TIME_BUDGET_S)")
    prv.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS",
                     help="timeout for each provider request (default: saved setting or 60)")
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
    pcfg.add_argument("--cost-budget", type=float, default=None, metavar="USD",
                      help="save the estimated per-review cost target (0 disables)")
    pcfg.add_argument("--time-budget", type=float, default=None, metavar="SECONDS",
                      help="save the elapsed-time target (0 disables)")
    pcfg.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS",
                      help="save the timeout for each provider request")
    pcfg.set_defaults(func=cmd_config)

    pv = sub.add_parser("serve", help="serve the read API over HTTP (needs the 'api' extra)")
    pv.add_argument("--host", default=os.environ.get("AGR_HOST", "127.0.0.1"),
                    help="bind host (default: $AGR_HOST, else 127.0.0.1; use 0.0.0.0 in a container)")
    pv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                    help="bind port (default: $PORT, else 8000)")
    pv.add_argument("--allowed-host", action="append", default=[],
                    help="allow an exact browser/proxy host name (repeatable); remote access requires the printed session link")
    pv.add_argument("--read-only", action="store_true",
                    help="reject every non-GET/HEAD request with 403 (also: $AGR_READ_ONLY=1); "
                         "for a hosted public demo that must never be written to")
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
    # The moved evaluation commands are stubs that only print a pointer, so they
    # tolerate their old flags; every other command stays strict.
    args, extra = parser.parse_known_args(argv)
    if extra and args.func is not cmd_legacy_moved:
        parser.error("unrecognized arguments: " + " ".join(extra))
    args.store_explicit = args.store is not None
    if args.store is None:
        args.store = ".agr-store"
    _load_env(getattr(args, "env_file", None))
    from .userconfig import ConfigError
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
