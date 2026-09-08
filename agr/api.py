"""FastAPI read API over the deterministic store (spec §17 stack).

A thin HTTP transport over :mod:`agr.read`. All behaviour lives in the stdlib
read model; this module only maps it onto routes and turns
:class:`agr.read.RunNotFound` into ``404``. FastAPI is an *optional* dependency
so the deterministic core stays dependency-free — import this module (or call
:func:`create_app`) only when the ``api`` extra is installed::

    pip install .[api]
    python -m agr serve --store .agr-store

Read endpoints are side-effect free; the two ``POST`` routes record human review
workflow beside the immutable source (never mutating it) via :mod:`agr.workflow`::

    GET  /                          evidence-browser SPA (§4.5 forensic view)
    GET  /healthz                   liveness
    GET  /sweep                     implicit-sweep summary (§4.3.1)
    GET  /queue                     triage queue view (§4.3.2)
    GET  /runs                      run summaries
    GET  /fleet/episodes            recovery episodes grouped across every run (item 30)
    GET  /fleet/argument-shapes     argument-shape distribution per failing-call signature (item 31)
    GET  /runs/{run_id}             full deterministic review + workflow/feedback
    GET  /runs/{run_id}/forensic    synchronized forensic view (§4.5)
    GET  /runs/{run_id}/source      raw immutable source + hash verification
    GET  /runs/{run_id}/reviews     reviewer keys that have scored this capture
    GET  /runs/{run_id}/compare     diff of two reviews of one run (reviewer vs reviewer)
    GET  /runs/{run_id}/divergence  aligned against a passing sibling on the same task (item 21)
    GET  /runs/{run_id}/next        next unhandled run in a queue view (§4.2)
    POST /events                    review-workflow analytics events (§4.21)
    GET  /metrics                   derived product measures over that log (§4.21)
    GET  /configurations            pinned configurations to compare (§4.16.1)
    GET  /comparisons               saved matched-comparison definitions (§4.16.1)
    GET  /comparisons/preview       match report + result for an unsaved definition
    GET  /comparisons/{id}          a saved comparison, recomputed from its definition
    POST /comparisons               freeze a comparison definition (§4.16.1)
    POST /runs/{run_id}/workflow    set disposition / assignment / progress (§4.3.4)
    POST /runs/{run_id}/feedback    Tier-1/2/3 moment feedback + corrections (§4.13)
    POST /runs/{run_id}/lessons     create an Eval Lesson from an accepted moment (§4.11)
    POST /runs/{run_id}/lessons/{id}            advance status / edit the lesson (§6.10)
    POST /runs/{run_id}/lessons/{id}/experiment propose or approve an experiment (§13.2)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import argument_shapes, divergence, fleet, instrumentation, lessons, queue, read, version, versions, workflow
from .store import Store

_STATIC = Path(__file__).parent / "static"


def create_app(store_root: str = ".agr-store"):
    """Build the FastAPI application bound to a store directory.

    Imported lazily so the rest of the package never requires FastAPI. Raises a
    clear :class:`RuntimeError` if the optional ``api`` extra is not installed.
    """
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via serve
        raise RuntimeError(
            "The read API requires FastAPI. Install the optional extra:\n"
            "    pip install .[api]"
        ) from exc

    def _selector(raw: str) -> dict:
        """Parse a ``field=value`` side selector from the query string."""
        field, _, value = raw.partition("=")
        if not field or not value:
            raise HTTPException(status_code=422,
                                detail=f"selector must be field=value, got {raw!r}")
        return {field: value}

    store = Store(store_root)
    app = FastAPI(
        title="Agent Game Review — Read API",
        version=version.READ_MODEL_VERSION,
        summary="Read-only forensic view over the deterministic review store.",
    )

    def _run_or_404(fn, run_id: str):
        try:
            return fn(store, run_id)
        except read.RunNotFound:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    def _ensure_run(run_id: str) -> None:
        if store.latest_capture_id(run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    # The single-page app's CSS and per-phase JS modules live under /static so
    # index.html can stay a slim shell. Mounted before the routes below so the
    # JSON API paths keep precedence for anything not matching a static file.
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        """The evidence-browser single-page app; fetches the JSON API below."""
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "read_model_version": version.READ_MODEL_VERSION}

    @app.get("/sweep")
    def sweep() -> dict:
        return queue.sweep_summary(store)

    @app.get("/queue")
    def queue_view(
        filter: list[str] = Query(default=[]),
        sort: str = "triage",
        grouping: str = "none",
    ) -> dict:
        try:
            return queue.queue_view(store, filters=filter, sort=sort, grouping=grouping)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/runs")
    def runs() -> list[dict]:
        return read.list_runs(store)

    @app.get("/fleet/episodes")
    def fleet_episodes(group_by: str = "tool,error_signature") -> list[dict]:
        """Item 30: recovery episodes grouped across EVERY run in the store —
        which failures repeat, how often, and how much they cost."""
        dims = [d.strip() for d in group_by.split(",") if d.strip()] or None
        try:
            groups = fleet.fleet_episodes(store, group_by=dims)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return [g.to_dict() for g in groups]

    @app.get("/fleet/usage-summary")
    def fleet_usage_summary() -> dict:
        """AGR-06: the whole-fleet usage headline — the union of every
        recovery episode's usage records, independent of any --group-by
        choice. Never a sum of per-group totals, which could double-count an
        event covered by episodes in two different groups."""
        return fleet.fleet_usage_summary(store).to_dict()

    @app.get("/fleet/argument-shapes")
    def fleet_argument_shapes(min_group_size: int = 1) -> list[dict]:
        """Item 31: the tool_input KEY SET and value TYPE distribution among
        failing calls per (tool, error_signature) group — never the retained
        values themselves."""
        groups = argument_shapes.argument_shapes(store, min_group_size=min_group_size)
        return [g.to_dict() for g in groups]

    # NOTE: the bare /runs/{run_id} route is defined LAST (with :path) so its
    # greedy path capture never shadows the /runs/{run_id}/... sub-routes.

    @app.get("/runs/{run_id:path}/audit")
    def audit(run_id: str) -> list[dict]:
        """§11 Task & Verifier Audit rows (categorical, evidence-backed)."""
        return _run_or_404(read.get_audit, run_id)

    @app.get("/runs/{run_id:path}/forensic")
    def forensic(run_id: str) -> dict:
        return _run_or_404(read.get_forensic, run_id)

    @app.get("/runs/{run_id:path}/source")
    def source(run_id: str) -> dict:
        return _run_or_404(read.get_source, run_id)

    @app.get("/runs/{run_id:path}/reviews")
    def reviews(run_id: str) -> dict:
        """Reviewer keys that have scored this run's latest capture."""
        keys = _run_or_404(read.list_reviews, run_id)
        default = keys[0] if keys else None
        for k in keys:
            if k != "deterministic":
                default = k
                break
        return {"reviews": keys, "default_compare": [default, "deterministic"] if len(keys) >= 2 else None}

    @app.get("/runs/{run_id:path}/compare")
    def compare(run_id: str, left: str = Query(...), right: str = Query(...),
                threshold: float = Query(default=0.0)) -> dict:
        """Diff two reviews of the same capture — reviewer vs reviewer, not §4.16.

        The §4.16 matched *version* comparison is ``/comparisons`` below; this is
        the §15.3-family reviewer diff generalised from prediction-vs-gold.
        """
        try:
            return read.get_comparison(store, run_id, left, right, threshold=threshold)
        except read.RunNotFound:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"review {str(exc)!r} not found")

    @app.get("/runs/{run_id:path}/divergence")
    def divergence_endpoint(run_id: str, sibling: Optional[str] = None) -> dict:
        """Item 21: this run's tool_call timeline aligned against a PASSING
        sibling on the same task — the first point their behaviour diverged,
        not just that the outcome differs. ``sibling`` overrides the
        automatically found one. 404 when no sibling is found/given."""
        _ensure_run(run_id)
        report = divergence.divergence_report(store, run_id, sibling_run_id=sibling)
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=f"no passing sibling found for run {run_id!r} on the same task",
            )
        return report

    @app.get("/runs/{run_id:path}/next")
    def next_run(
        run_id: str,
        filter: list[str] = Query(default=[]),
        sort: str = "triage",
        grouping: str = "none",
    ) -> dict:
        try:
            nxt = queue.next_unhandled(
                store, run_id, filters=filter, sort=sort, grouping=grouping)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"run_id": run_id, "next_unhandled": nxt}

    # --- product instrumentation (§4.21) -------------------------------------

    @app.post("/events")
    def record_events(payload: dict = Body(...)) -> dict:
        """Record a batch of review-workflow analytics events (§4.21).

        Allowlisted: an unknown event name, an undeclared property, or a value
        shaped like free text is rejected for the whole batch, so trace content
        cannot reach the analytics log through a mis-instrumented call site.
        """
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise HTTPException(status_code=422, detail="events[] is required")
        try:
            written = instrumentation.record_batch(store, events, actor=payload.get("actor"))
        except instrumentation.InstrumentationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"recorded": written}

    @app.get("/metrics")
    def metrics() -> dict:
        """Derived product measures over the analytics log (§4.21)."""
        return instrumentation.product_measures(store)

    # --- matched version comparison (§4.16 / §6.12 / §12.3) ------------------

    @app.get("/configurations")
    def configurations() -> dict:
        """Pinned configurations a comparison side can be selected by (§4.16.1).

        ``default_pair`` names the two that share the most tasks, so a store
        holding more than two configurations opens on a comparable slice rather
        than on whichever two sort first.
        """
        configurations = versions.list_configurations(store)
        left, right = versions.default_pair(configurations)
        return {"configurations": configurations,
                "default_pair": [left["label"], right["label"]] if left else None}

    @app.get("/comparisons")
    def comparisons() -> dict:
        """Saved comparison definitions, each addressable by its comparison_id."""
        return {"comparisons": versions.list_saved_comparisons(store)}

    @app.get("/comparisons/preview")
    def comparison_preview(
        baseline: str = Query(..., description="field=value selector, e.g. sweep_id=sweep_141"),
        candidate: str = Query(...),
        axis: str = Query(...),
        key: list[str] = Query(default=list(versions.DEFAULT_MATCH_KEYS)),
    ) -> dict:
        """The match report + result for an unsaved definition (§4.16.1 steps 4–5)."""
        try:
            return versions.compare_versions(store, _selector(baseline), _selector(candidate),
                                             axis, match_keys=key)
        except versions.ComparisonError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/comparisons")
    def save_comparison(payload: dict = Body(...)) -> dict:
        """Freeze a comparison definition (§4.16.1 step 6)."""
        for field in ("baseline", "candidate", "axis"):
            if not payload.get(field):
                raise HTTPException(status_code=422, detail=f"{field} is required")
        try:
            return versions.save_comparison(
                store, payload["baseline"], payload["candidate"], payload["axis"],
                match_keys=payload.get("match_keys", versions.DEFAULT_MATCH_KEYS),
                name=payload.get("name"))
        except versions.ComparisonError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/comparisons/{comparison_id}")
    def comparison(comparison_id: str) -> dict:
        try:
            return versions.load_comparison(store, comparison_id)
        except versions.ComparisonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/runs/{run_id:path}/workflow")
    def set_workflow(run_id: str, payload: dict = Body(...)) -> dict:
        """Set disposition / assignment / progress (§4.3.4). Optimistic on ``base_version``."""
        _ensure_run(run_id)
        if "base_version" not in payload:
            raise HTTPException(status_code=422, detail="base_version is required")
        try:
            return workflow.set_workflow(
                store, run_id,
                actor=payload.get("actor"),
                base_version=payload["base_version"],
                progress=payload.get("progress"),
                disposition=payload.get("disposition"),
                assignee=payload.get("assignee"),
                reviewer=payload.get("reviewer"),
                note=payload.get("note"),
            )
        except workflow.VersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "version_conflict", "expected": exc.expected,
                        "actual": exc.actual, "current": exc.current},
            )
        except workflow.WorkflowError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/runs/{run_id:path}/feedback")
    def add_feedback(run_id: str, payload: dict = Body(...)) -> dict:
        """Record Tier-1/2/3 moment feedback (§4.13). Idempotent on ``mutation_id``.

        A Tier-3 ``structured_correction`` additionally carries ``correction``
        (the proposed values), ``generated`` (what they replace, for the
        side-by-side), and ``evidence_event_ids`` — required when the correction
        restates a factual claim.
        """
        _ensure_run(run_id)
        for field in ("mutation_id", "moment_id", "kind"):
            if not payload.get(field):
                raise HTTPException(status_code=422, detail=f"{field} is required")
        try:
            record, wf = workflow.add_feedback(
                store, run_id,
                actor=payload.get("actor"),
                mutation_id=payload["mutation_id"],
                moment_id=payload["moment_id"],
                kind=payload["kind"],
                replacement_group=payload.get("replacement_group"),
                note=payload.get("note"),
                correction=payload.get("correction"),
                generated=payload.get("generated"),
                evidence_event_ids=payload.get("evidence_event_ids"),
            )
        except workflow.WorkflowError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"feedback": record, "workflow": wf}

    # --- Eval Lessons + improvement experiments (§4.11, §6.10, §13) ----------

    def _lesson_conflict(exc: lessons.VersionConflict):
        return HTTPException(
            status_code=409,
            detail={"error": "version_conflict", "expected": exc.expected,
                    "actual": exc.actual, "current": exc.current})

    @app.post("/runs/{run_id:path}/lessons")
    def create_lesson(run_id: str, payload: dict = Body(...)) -> dict:
        """Create the Eval Lesson for an accepted moment (§4.11). Idempotent per moment."""
        _ensure_run(run_id)
        for field in ("mutation_id", "moment_id"):
            if not payload.get(field):
                raise HTTPException(status_code=422, detail=f"{field} is required")
        review = _run_or_404(read.get_review, run_id)
        moment = next((m for m in review["moments"]
                       if m.get("moment_id") == payload["moment_id"]), None)
        if moment is None:
            raise HTTPException(status_code=404,
                                detail=f"moment {payload['moment_id']!r} not found")
        try:
            lesson = lessons.create_lesson(
                store, run_id, moment=moment, actor=payload.get("actor"),
                mutation_id=payload["mutation_id"],
                reviewer_key=review.get("reviewer_key"))
        except lessons.LessonError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"lesson": lesson}

    @app.post("/runs/{run_id:path}/lessons/{lesson_id}")
    def update_lesson(run_id: str, lesson_id: str, payload: dict = Body(...)) -> dict:
        """Advance a lesson's status and/or apply edits (§4.11, §6.10). Optimistic on base_version."""
        _ensure_run(run_id)
        if "base_version" not in payload:
            raise HTTPException(status_code=422, detail="base_version is required")
        try:
            lesson = lessons.set_lesson_status(
                store, run_id, lesson_id, base_version=payload["base_version"],
                actor=payload.get("actor"), status=payload.get("status"),
                edits=payload.get("edits"))
        except lessons.VersionConflict as exc:
            raise _lesson_conflict(exc)
        except lessons.LessonError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"lesson": lesson}

    @app.post("/runs/{run_id:path}/lessons/{lesson_id}/experiment")
    def lesson_experiment(run_id: str, lesson_id: str, payload: dict = Body(default={})) -> dict:
        """Generate (``action`` omitted) or approve (``action=approve``) the §13.2 proposal."""
        _ensure_run(run_id)
        action = (payload or {}).get("action", "propose")
        fn = lessons.approve_experiment if action == "approve" else lessons.propose_experiment
        try:
            lesson = fn(store, run_id, lesson_id, actor=(payload or {}).get("actor"))
        except lessons.LessonError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"lesson": lesson}

    @app.get("/runs/{run_id:path}")
    def review(run_id: str, reviewer: Optional[str] = None) -> dict:
        """Full review for a run. Defined last: :path is greedy.

        ``?reviewer=<key>`` serves a specific reviewer's snapshot (AGR-07 UX:
        the browser can flip between the deterministic baseline and a model
        pass without leaving the page); the default is the most-enriched
        available review.
        """
        try:
            return read.get_review(store, run_id, reviewer_key=reviewer)
        except read.RunNotFound:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    return app
