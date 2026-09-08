"use strict";

// --- state -------------------------------------------------------------------
const state = { runId: null, view: "review", chapter: "moments", forensic: null, review: null,
  momentIdx: 0, showAllContract: false, pendingStep: null, viewed: new Set(),
  sweep: null, queue: null, filters: new Set(), sort: "triage", reviewer: "RK", dispOpen: false,
  compare: null, sibling: null, traceOpen: false, traceStep: null,
  // §4.3.5 fast/deep entry preference — where each run opens, remembered per
  // browser like the panel widths. Default is the recommended fast path.
  entryPref: (localStorage.getItem("agr-entry-pref") === "outcome" ? "outcome" : "first_moment"),
  // §4.16 version comparison: a surface of its own, so its state does not ride
  // on the selected run the way the per-run reviewer diff does.
  versions: { configurations: null, baseline: null, candidate: null,
    axis: "evaluation_harness", keys: ["task_id", "task_version", "verifier_version",
      "environment_image_digest", "task_parameters", "seed"],
    result: null, pending: false, savedId: null },
  // Item 30/32: the fleet view over recovery episodes across every run — a
  // surface of its own, like Compare versions, not tied to a selected run.
  fleet: { groupBy: "tool,error_signature", episodes: null, pending: false, usageSummary: null } };

// --- §4.1 shareable review location -----------------------------------------
//   The exact review position lives in the query string, so a reviewer can share
//   or reload the spot they are standing on:
//
//     ?run=chess_best_move__seed42&chapter=moments&moment=mom_003&trace=1
//      &evidence=s7&view=compare&left=deterministic&right=model:a&filter=failed&sort=triage
//
//   Every navigation rewrites it with history.replaceState — the URL tracks the
//   app rather than capturing the Back button, which keeps leaving the app the
//   way it does today. Reading it back happens once, at boot, in `boot()`.
function syncUrl() {
  const p = new URLSearchParams();
  if (state.view === "fleet") {
    p.set("view", "fleet");
    if (state.fleet.groupBy !== "tool,error_signature") p.set("group_by", state.fleet.groupBy);
    history.replaceState(null, "", location.pathname + "?" + p.toString());
    return;
  }
  if (state.view === "versions") {
    // The version comparison is not about one run, so its link carries the
    // definition instead: a saved id, or the sides and axis being constructed.
    p.set("view", "versions");
    const v = state.versions;
    if (v.savedId) p.set("comparison", v.savedId);
    else if (v.result) { p.set("baseline", v.baseline); p.set("candidate", v.candidate); p.set("axis", v.axis); }
    history.replaceState(null, "", location.pathname + "?" + p.toString());
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
  const q = p.toString();
  history.replaceState(null, "", q ? location.pathname + "?" + q : location.pathname);
}
// The queue-view half of the URL is applied before the first fetch, so a shared
// link reproduces the same queue; unknown values are dropped rather than sent on
// to the API. The run-level half is applied in `selectRun` once the review loads.
function readUrl() {
  const p = new URLSearchParams(location.search);
  const sort = p.get("sort");
  if (SORT_OPTIONS.some(([v]) => v === sort)) state.sort = sort;
  for (const f of p.getAll("filter")) if (FILTER_CHIPS.some(([k]) => k === f)) state.filters.add(f);
  const left = p.get("left"), right = p.get("right");
  if (left && right) state.compare = { left, right };
  const v = state.versions;
  if (p.get("axis")) v.axis = p.get("axis");
  if (p.get("baseline")) v.baseline = p.get("baseline");
  if (p.get("candidate")) v.candidate = p.get("candidate");
  return { run: p.get("run"), view: p.get("view"), chapter: p.get("chapter"),
    moment: p.get("moment"), evidence: p.get("evidence"), trace: p.get("trace") === "1",
    left, right, comparison: p.get("comparison"), groupBy: p.get("group_by") };
}
