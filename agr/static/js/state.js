"use strict";

// --- state -------------------------------------------------------------------
// P0-5: read-only demo mode (set from /healthz during boot) hides write
// controls (Agree, disposition, lessons); the server enforces the same
// restriction independently, so this is a UI convenience, not the boundary.
// Storage can throw (a private window, blocked site data); a preference that
// cannot be read is simply not set, and must not stop `state` from existing.
function readEntryPreference() {
  try { return localStorage.getItem("agr-entry-pref") === "first_moment" ? "first_moment" : "outcome"; }
  catch (_) { return "outcome"; }
}
/** @type {AppState} */
const state = {
  // Settings are user defaults; review selections and jobs belong to one project.
  ai: { settings: null, draft: null, error: null, selected: new Set(), plan: null, job: null, jobs: [],
    force: false, token: 0, requestId: null, filter: "", pollToken: 0, watching: new Set() },
  readOnly: false, runId: null, view: "review", chapter: "moments", forensic: null, review: null,
  // F4: bumped on every run or reviewer selection so an out-of-order response
  // to an EARLIER selection can tell it is stale (its captured token no
  // longer matches state.loadToken) and discard itself instead of overwriting
  // what the reader is now looking at. `loading` is true from the moment a
  // selection starts until its (non-stale) response is applied — annotation
  // actions read it to stay disabled while the data underneath them could
  // still belong to a run/reviewer other than the one being written to.
  loadToken: 0, loading: false,
  momentIdx: 0, showAllContract: false, pendingStep: null, viewed: new Set(),
  // T2: which moment cards a reader expanded to the full five-part detail, by
  // moment_id — survives a re-render (e.g. after Agree) but not a run change.
  expandedMoments: new Set(),
  // Same idea for the inline "Eval lesson available" disclosure — without this,
  // approving/editing a lesson triggers a refetch + re-render that would
  // silently re-collapse the section a reviewer is actively working in.
  expandedLessons: new Set(),
  // T3: which anchor event the evidence panel is currently focused on. Reset
  // per run; renderEvidencePanel falls back to the current moment's first
  // anchor whenever this id is not one of that moment's own anchors, so a
  // newly selected finding shows its own source without extra clicks.
  evidenceFocus: null,
  sweep: null, queue: null, queueError: null, filters: new Set(), sort: "triage", reviewer: "Local reviewer", dispOpen: false,
  projectId: null, projects: [], serverReadOnly: false, captureId: null, importRunIds: null,
  workspace: { source: null, files: [], preview: null, job: null, busy: false, error: null, uploadProgress: null, selected: new Set(), connections: [], connectionId: null, investigations: [], detail: null, token: 0 },
  // UX audit finding #2: bumped on every loadInbox() call, the same way F4's
  // state.loadToken guards a run selection — a filter/sort change (or the
  // initial boot load) starting a NEW /queue fetch while an earlier one is
  // still in flight must not let that earlier, now-stale response land on
  // top of it. Without this, whichever request happened to resolve LAST won
  // the final state.queue regardless of which click was actually last, which
  // could pair a since-cleared filter with a stale filtered (possibly
  // 0-row) result — a state that looks stuck rather than merely slow, since
  // nothing about it self-corrects until some other render is triggered.
  queueLoadToken: 0,
  // U2 Runs workspace: the search box's live text, and the reading position on
  // that surface — restored when a run is opened and then left again ("Back to
  // review" / the "Runs" nav button), rather than dropping the reader back at
  // the top of a freshly rendered table. runsShown is the pagination-follow-up
  // half of that restore (runs.js): how many rows were actually on screen
  // when the run was opened, so a return can reveal enough of the paged
  // table for runsScroll to land somewhere meaningful again.
  runsSearch: "", runsScroll: 0, runsShown: 0, runsGrouping: "none", runsSelected: new Set(), runsAnchorId: null,
  focusedQueue: null, focusSearch: "", reviewStarting: false,
  inspector: { runId: null, data: null, loading: false, error: null, token: 0 },
  compare: null, sibling: null, traceOpen: false, traceStep: null,
  // §4.3.5 fast/deep entry preference — where each run opens, remembered per
  // browser like the panel widths. Default is Overview: a first-time reader
  // should land on the synthesis (what happened + main finding) and understand
  // the run in one pass, rather than dropping into moment 1. A reviewer doing
  // fast repeat triage can switch to "First key moment"; that choice is kept.
  entryPref: readEntryPreference(),
  // §4.16 version comparison: a surface of its own, so its state does not ride
  // on the selected run the way the per-run reviewer diff does.
  versions: { configurations: null, baseline: null, candidate: null,
    axis: "evaluation_harness", keys: ["task_id", "task_version", "verifier_version",
      "environment_image_digest", "task_parameters", "seed"],
    result: null, pending: false, savedId: null },
  // Item 30/32: the fleet view over recovery episodes across every run — a
  // surface of its own, like Compare versions, not tied to a selected run.
  // U3: `loadToken` guards a grouping change the same way run.js's does for a
  // run switch — an episodes fetch started for a groupBy the reader has since
  // changed away from must not land on top of the newer choice.
  // `total`/`hasMore` back the "Load more" pager: total is the server's full
  // group count (X-Total-Count), independent of how many pages have been
  // fetched into `episodes` so far. `loadingMore` is distinct from `pending`
  // so an in-flight next-page fetch shows "Loading…" on the button itself
  // rather than blanking the rows already on screen.
  fleet: { groupBy: "tool,error_signature", episodes: null, pending: false,
    error: null, loadToken: 0, usageSummary: null, executionQuality: null,
    total: null, hasMore: false, loadingMore: false } };

// --- §4.1 shareable review location -----------------------------------------
//   The exact review position lives in the query string, so a reviewer can share
//   or reload the spot they are standing on:
//
//     ?run=chess_best_move__seed42&chapter=moments&moment=mom_003&trace=1
//      &evidence=s7&view=compare&left=deterministic&right=model:a&filter=failed&sort=triage
//
//   Every navigation rewrites the query string; most of them (a chapter switch,
//   a filter toggle, a moment or evidence selection) use history.replaceState so
//   the Back button is not spammed with one entry per click. U1 asks for more
//   than that, though: Back/Forward must also move between the app's actual
//   *destinations* — Runs, Patterns, Compare versions, and a given run's
//   investigation shell. `_commitUrl` below tells the two apart by comparing
//   the "major" destination key across renders: only a CHANGE in destination
//   pushes a new history entry; everything else inside the same destination
//   still replaces. A `popstate` (state.__inPopstate, set by boot.js's
//   listener) always replaces — the browser already moved history for us, so
//   committing must not push on top of that.
let _lastMajorKey;
function _majorDestinationKey() {
  const prefix = (state.projectId || "existing") + ":";
  if (state.view === "add-runs") return prefix + "add-runs:" + (state.workspace.source || "") + ":" + ((state.workspace.preview || {}).id || "");
  if (typeof isEngineerView === "function" && isEngineerView(state.view)) return prefix + state.view + ":" + (((state.view === "ai-review" ? state.ai.job : state.workspace.job) || {}).id || "");
  if (state.view === "versions") return prefix + "versions:" + (state.versions.savedId || "");
  if (state.view === "fleet" || state.view === "runs") return prefix + state.view;
  return prefix + (state.runId ? "run:" + state.runId + ":" + state.view : "runs");
}
function _commitUrl(query) {
  const url = query ? location.pathname + "?" + query : location.pathname;
  const key = _majorDestinationKey();
  if (state.__inPopstate || state.__booting || _lastMajorKey === undefined || key === _lastMajorKey) history.replaceState(history.state, "", url);
  else history.pushState({ agrPrevious: _lastMajorKey }, "", url);
  if (key !== _lastMajorKey) state.navigationVersion = (state.navigationVersion || 0) + 1;
  _lastMajorKey = key;
}
function syncUrl() {
  const p = new URLSearchParams();
  if (state.projectId && state.projectId !== "existing") p.set("project", state.projectId);
  if (typeof isEngineerView === "function" && isEngineerView(state.view)) {
    p.set("view", state.view);
    if (state.view === "add-runs") {
      if (state.workspace.source) p.set("source", state.workspace.source);
      if (state.workspace.connectionId && state.workspace.source === "langfuse") p.set("connection", state.workspace.connectionId);
      if (state.workspace.preview) p.set("preview", state.workspace.preview.id);
    }
    if (state.view === "import" && state.workspace.job) p.set("import", state.workspace.job.id);
    if (state.view === "ai-review" && state.ai.job) p.set("review_job", state.ai.job.id);
    _commitUrl(p.toString()); return;
  }
  if (state.runsGrouping !== "none") p.set("run_group", state.runsGrouping);
  if (state.view === "runs" && state.inspector.runId) p.set("inspect", state.inspector.runId);
  if (state.focusedQueue && !isWorkspaceView(state.view)) p.set("review_queue", state.focusedQueue.id);
  if (state.patternSelector && state.view !== "fleet") p.set("test_pattern", JSON.stringify(state.patternSelector));
  if (state.view === "runs") {
    p.set("view", "runs");
    if (state.importRunIds && state.workspace.job) p.set("import", state.workspace.job.id);
    if (state.runsSearch) p.set("q", state.runsSearch);
    if (state.ai.filter) p.set("ai_status", state.ai.filter);
    for (const f of state.filters) p.append("filter", f);
    if (state.sort !== "triage") p.set("sort", state.sort);
    _commitUrl(p.toString());
    return;
  }
  if (state.view === "fleet") {
    p.set("view", "fleet");
    if (state.fleet.mode === "tests") {
      p.set("pattern_type", "tests");
      p.set("test_group_by", state.fleet.testGroupBy || "task,test,signature");
    }
    if (state.fleet.groupBy !== "tool,error_signature") p.set("group_by", state.fleet.groupBy);
    _commitUrl(p.toString());
    return;
  }
  if (state.view === "versions") {
    // The version comparison is not about one run, so its link carries the
    // definition instead: a saved id, or the sides and axis being constructed.
    p.set("view", "versions");
    const v = state.versions;
    if (v.savedId) p.set("comparison", v.savedId);
    else if (v.result) { if (v.baseline) p.set("baseline", v.baseline); if (v.candidate) p.set("candidate", v.candidate); p.set("axis", v.axis); }
    _commitUrl(p.toString());
    return;
  }
  if (state.runsSearch) p.set("q", state.runsSearch);
  if (state.ai.filter) p.set("ai_status", state.ai.filter);
  if (state.runId) p.set("run", state.runId);
  if (state.importRunIds && state.workspace.job) p.set("import", state.workspace.job.id);
  if (state.captureId) p.set("capture", state.captureId);
  if (state.view === "review") p.set("chapter", state.chapter);
  else p.set("view", state.view);
  if (state.view === "review" && state.chapter === "moments") {
    const m = currentMoment();
    if (m && m.moment_id) p.set("moment", m.moment_id);
  }
  if (state.view === "compare" && state.compare && state.compare.left && state.compare.right) {
    p.set("left", state.compare.left);
    p.set("right", state.compare.right);
  }
  if (state.traceOpen) {
    p.set("trace", "1");
    if (state.traceStep) p.set("evidence", state.traceStep);
  }
  for (const f of state.filters) p.append("filter", f);
  if (state.sort !== "triage") p.set("sort", state.sort);
  if (state.runId && state.review && !state.loading) {
    try { localStorage.setItem("agr-last-" + (state.projectId || "existing"), p.toString()); } catch (_) {}
  }
  _commitUrl(p.toString());
}
// The queue-view half of the URL is applied before the first fetch, so a shared
// link reproduces the same queue; unknown values are dropped rather than sent on
// to the API. The run-level half is applied in `selectRun` once the review loads.
//
// U1: this now runs a second time per session, on every `popstate` (see
// boot.js), not just once at boot — so it fully REPLACES state.sort/filters/
// search from the URL rather than only adding to them. A one-time additive
// parse would leak filters forward across a Back navigation that dropped them.
function readUrl() {
  const p = new URLSearchParams(location.search);
  const sort = p.get("sort");
  state.sort = SORT_OPTIONS.some(([v]) => v === sort) ? sort || "triage" : "triage";
  state.filters = new Set(p.getAll("filter").filter(f => FILTER_CHIPS.some(([k]) => k === f)));
  state.runsSearch = p.get("q") || "";
  const grouping = p.get("run_group") || "", aiStatus = p.get("ai_status") || "";
  state.runsGrouping = ["task", "session"].includes(grouping) ? grouping : "none";
  state.ai.filter = ["none", "active", "reviewed", "failed"].includes(aiStatus) ? aiStatus : "";
  let testPattern = null;
  try {
    const parsed = JSON.parse(p.get("test_pattern") || "null");
    if (parsed && Array.isArray(parsed.key) && Array.isArray(parsed.group_by)
        && parsed.group_by.every(d => ["task", "test", "signature"].includes(d))
        && Array.isArray(parsed.task_cohorts)) testPattern = parsed;
  } catch (_) {}
  const left = p.get("left"), right = p.get("right");
  state.compare = (left && right) ? { left, right } : null;
  const v = state.versions;
  v.axis = p.get("axis") || v.axis;
  if (p.get("baseline")) v.baseline = p.get("baseline");
  if (p.get("candidate")) v.candidate = p.get("candidate");
  return { inspector: p.get("inspect"), frozenId: p.get("review_queue"), source: p.get("source"), connection: p.get("connection"), previewId: p.get("preview"), project: p.get("project"), capture: p.get("capture"), importId: p.get("import"), reviewJobId: p.get("review_job"), run: p.get("run"), view: p.get("view"), chapter: p.get("chapter"),
    moment: p.get("moment"), evidence: p.get("evidence"), trace: p.get("trace") === "1",
    testPattern, patternType: p.get("pattern_type"), testGroupBy: p.get("test_group_by"), left, right, comparison: p.get("comparison"), groupBy: p.get("group_by"), q: p.get("q") };
}
