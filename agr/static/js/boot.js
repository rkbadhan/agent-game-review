"use strict";

// --- boot --------------------------------------------------------------------
// A shared link (§4.1) reopens its run at the encoded position; a link to a run
// this store does not hold falls back to the queue with an explanation rather
// than an empty workspace.
async function boot() {
  const want = readUrl();
  await loadInbox();
  // T1: first use opens straight into the review — no blocking terminology
  // modal. The same orientation content stays reachable on demand from Help
  // ("What do these terms mean?") and from Glossary.
  track("sweep_opened", { sweep_id: (state.sweep || {}).sweep_id,
    count: (state.sweep || {}).total_runs });
  const first = state.queue && state.queue.run_ids && state.queue.run_ids[0];
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
    catch (e) { toast("That run is not in this store — opening the queue instead."); }
  }
  if (first) await selectRun(first);
}
boot().catch(showInboxError);

// --- small helpers (Review/Source tables) ------------------------------------
function rowEls(tag, cells, cellTag) { const tr = el(tag); for (const c of cells) tr.append(el(cellTag, null, c)); return tr; }
function td(text, cls) { return el("td", cls || null, text == null ? "" : String(text)); }
function tdBadge(status) { const cls = status === "passed" ? "pass" : status === "failed" ? "fail" : "warn";
  const cell = el("td"); cell.append(el("span", "badge " + cls, (status || "?").toUpperCase())); return cell; }
function fmt(v) { return v == null ? "" : (Array.isArray(v) ? v.join(", ") : String(v)); }
