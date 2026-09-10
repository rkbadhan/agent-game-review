"use strict";

// --- boot + history routing ---------------------------------------------------
// A shared link (§4.1) reopens its run at the encoded position; a link to a run
// this store does not hold falls back to the queue with an explanation rather
// than an empty workspace. U1: Browser Back/Forward across the major
// destinations (Runs / Patterns / Compare versions / a run's investigation
// shell) is driven by the same dispatch this initial boot uses — see the
// `popstate` listener below and the pushState/replaceState choice in
// state.js's syncUrl().
async function dispatchLocation(want, isBoot) {
  // A shared "Runs" link opens the workspace with its search/filter/sort intact.
  if (want.view === "runs") {
    state.view = "runs";
    if (want.q != null) state.runsSearch = want.q;
    render();
    return;
  }
  // A shared fleet link opens the surface directly at its group-by dimension.
  if (want.view === "fleet") {
    state.view = "fleet";
    if (want.groupBy) state.fleet.groupBy = want.groupBy;
    render();
    return;
  }
  // A shared version-comparison link opens its surface directly: a saved id
  // reloads the frozen definition, an unsaved one rebuilds from its sides.
  if (want.view === "versions") {
    state.view = "versions";
    // Configurations first: both restore paths resolve their sides through them.
    try { state.versions.configurations = (await api("/configurations")).configurations || []; }
    catch (e) { /* renderVersions reports the failure */ }
    render();
    if (want.comparison) await openSavedComparison(want.comparison);
    else if (state.versions.baseline && state.versions.candidate) await previewComparison();
    return;
  }
  if (want.run) {
    try { return await selectRun(want.run, want); }
    catch (e) { toast("That run is not in this store — opening the runs workspace instead."); }
  }
  // Only the initial boot auto-opens the first run when nothing else was asked
  // for — a `popstate` landing on a bare URL (the state before anything was
  // ever pushed) returns to the Runs workspace instead, since by then the
  // reader has already seen the queue and is navigating away from something.
  const first = isBoot && state.queue && state.queue.run_ids && state.queue.run_ids[0];
  if (first) return selectRun(first);
  state.view = "runs"; render();
}
async function boot() {
  const want = readUrl();
  // `loadInbox()` below calls `syncUrl()` on its own (so a filter/sort change
  // elsewhere stays reflected in the URL without a full render), but at this
  // point in boot the destination is not resolved yet — state.view/runId are
  // still their transient defaults, so that early call would compute the
  // WRONG major-destination key ("runs") and lock it in as the history
  // baseline. `dispatchLocation` below then resolves the real destination
  // (typically a run) and would see that as a *change*, pushing a spurious
  // history entry on every fresh page load — so the first Back press would
  // land back on the Runs workspace instead of leaving the app. Guarding the
  // whole boot sequence the same way `popstate` does (state.__inPopstate)
  // keeps every commit during boot a replaceState, and `_lastMajorKey` ends
  // up correctly seeded from the destination boot actually resolved to.
  state.__booting = true;
  try {
    await loadInbox();
    // T1: first use opens straight into the review — no blocking terminology
    // modal. The same orientation content stays reachable on demand from Help
    // ("What do these terms mean?") and from Glossary.
    track("sweep_opened", { sweep_id: (state.sweep || {}).sweep_id,
      count: (state.sweep || {}).total_runs });
    await dispatchLocation(want, true);
  } finally { state.__booting = false; }
}
boot().catch(showInboxError);
window.addEventListener("popstate", () => {
  state.__inPopstate = true;
  dispatchLocation(readUrl(), false)
    .catch(() => toast("Could not restore that page."))
    .finally(() => { state.__inPopstate = false; });
});

// --- small helpers (Review/Source tables) ------------------------------------
function rowEls(tag, cells, cellTag) { const tr = el(tag); for (const c of cells) tr.append(el(cellTag, null, c)); return tr; }
function td(text, cls) { return el("td", cls || null, text == null ? "" : String(text)); }
function tdBadge(status) { const cls = status === "passed" ? "pass" : status === "failed" ? "fail" : "warn";
  const cell = el("td"); cell.append(el("span", "badge " + cls, (status || "?").toUpperCase())); return cell; }
function fmt(v) { return v == null ? "" : (Array.isArray(v) ? v.join(", ") : String(v)); }
