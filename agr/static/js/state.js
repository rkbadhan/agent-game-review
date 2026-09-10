"use strict";

// --- state -------------------------------------------------------------------
const state = { runId: null, view: "review", chapter: "moments", forensic: null, review: null,
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
  sweep: null, queue: null, filters: new Set(), sort: "triage", reviewer: "RK", dispOpen: false,
  // U2 Runs workspace: the search box's live text, and the reading position on
  // that surface — restored when a run is opened and then left again ("Back to
  // review" / the "Runs" nav button), rather than dropping the reader back at
  // the top of a freshly rendered table.
  runsSearch: "", runsScroll: 0,
  compare: null, sibling: null, traceOpen: false, traceStep: null,
  // §4.3.5 fast/deep entry preference — where each run opens, remembered per
  // browser like the panel widths. Default is Overview: a first-time reader
  // should land on the synthesis (what happened + main finding) and understand
  // the run in one pass, rather than dropping into moment 1. A reviewer doing
  // fast repeat triage can switch to "First key moment"; that choice is kept.
  entryPref: (localStorage.getItem("agr-entry-pref") === "first_moment" ? "first_moment" : "outcome"),
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
  fleet: { groupBy: "tool,error_signature", episodes: null, pending: false,
    error: null, loadToken: 0, usageSummary: null } };

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
  if (state.view === "versions") return "versions:" + (state.versions.savedId || "");
  if (state.view === "fleet" || state.view === "runs") return state.view;
  return state.runId ? "run:" + state.runId : "runs";
}
function _commitUrl(query) {
  const url = query ? location.pathname + "?" + query : location.pathname;
  const key = _majorDestinationKey();
  if (state.__inPopstate || state.__booting || _lastMajorKey === undefined || key === _lastMajorKey) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
  _lastMajorKey = key;
}
function syncUrl() {
  const p = new URLSearchParams();
  if (state.view === "runs") {
    p.set("view", "runs");
    if (state.runsSearch) p.set("q", state.runsSearch);
    for (const f of state.filters) p.append("filter", f);
    if (state.sort !== "triage") p.set("sort", state.sort);
    _commitUrl(p.toString());
    return;
  }
  if (state.view === "fleet") {
    p.set("view", "fleet");
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
    else if (v.result) { p.set("baseline", v.baseline); p.set("candidate", v.candidate); p.set("axis", v.axis); }
    _commitUrl(p.toString());
    return;
  }
  if (state.runId) p.set("run", state.runId);
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
  state.sort = SORT_OPTIONS.some(([v]) => v === sort) ? sort : "triage";
  state.filters = new Set(p.getAll("filter").filter(f => FILTER_CHIPS.some(([k]) => k === f)));
  state.runsSearch = p.get("q") || "";
  const left = p.get("left"), right = p.get("right");
  state.compare = (left && right) ? { left, right } : null;
  const v = state.versions;
  if (p.get("axis")) v.axis = p.get("axis");
  if (p.get("baseline")) v.baseline = p.get("baseline");
  if (p.get("candidate")) v.candidate = p.get("candidate");
  return { run: p.get("run"), view: p.get("view"), chapter: p.get("chapter"),
    moment: p.get("moment"), evidence: p.get("evidence"), trace: p.get("trace") === "1",
    left, right, comparison: p.get("comparison"), groupBy: p.get("group_by"), q: p.get("q") };
}
