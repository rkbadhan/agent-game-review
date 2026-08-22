"use strict";

// --- select + refresh --------------------------------------------------------
// `restore` carries a shared link's run-level position (§4.1): which surface,
// chapter, moment, and trace step to land on. Anything the loaded review no
// longer contains is reported rather than silently ignored.
async function selectRun(runId, restore) {
  state.runId = runId; state.momentIdx = 0; state.dispOpen = false;
  state.view = "review"; state.chapter = "moments"; state.viewed = new Set();
  closeTrace();  // a drawer left open belongs to the run being left behind
  state.compare = restore && restore.left && restore.right
    ? { left: restore.left, right: restore.right } : null;
  [state.forensic, state.review] = await Promise.all([
    api("/runs/" + encodeURIComponent(runId) + "/forensic"),
    api("/runs/" + encodeURIComponent(runId)),
  ]);
  // Landing chapter: a shared link's position (§4.1) wins; otherwise the §4.3.5
  // workspace preference decides between Outcome and the first key moment.
  let restoreChapter = restore && CHAPTERS.some(c => c[0] === restore.chapter) ? restore.chapter : null;
  if (!restoreChapter && restore && restore.moment) restoreChapter = "moments";
  const fastPath = applyEntryPreference(restoreChapter);
  if (restore) {
    if (restore.view === "source" || restore.view === "compare") state.view = restore.view;
    if (restore.moment) {
      const i = currentMoments().findIndex(m => m.moment_id === restore.moment);
      if (i >= 0) state.momentIdx = i;
      else toast("That moment is not in this review — showing the first one.");
    }
  }
  track("run_opened", { run_id: runId, task_id: (state.review.run || {}).task_id,
    review_mode: state.review.review_mode, fast_path: fastPath });
  renderRunList();
  render();
  if (restore && (restore.trace || restore.evidence)) openTrace(restore.evidence || null);
  if (window.innerWidth <= 820) $("#queue").classList.remove("open");
  $("#main").scrollTo({ top: 0, behavior: "smooth" });
}
// Apply the §4.3.5 entry preference (or a shared link's explicit chapter). Sets
// state.chapter / momentIdx and returns whether this open used the fast path —
// the recommended jump straight to the first key moment — for §4.21 metrics.
function applyEntryPreference(explicitChapter) {
  const rv = state.review || {};
  if (explicitChapter) { state.chapter = explicitChapter; return false; }
  // Not-reviewable routing (§4.15): no moment is selectable, so open at Outcome.
  if (rv.review_mode === "not_reviewable") { state.chapter = "outcome"; return false; }
  if (entryPreference() === "outcome") { state.chapter = "outcome"; return false; }
  // Fast path: open the first key moment when one exists; otherwise fall back to
  // Outcome and say why, exactly as §4.3.5 requires.
  if (currentMoments().length) { state.chapter = "moments"; state.momentIdx = 0; return true; }
  state.chapter = "outcome";
  toast("No key moment was selected for this run — opening at Outcome.");
  return false;
}
async function refreshRun() {
  state.review = await api("/runs/" + encodeURIComponent(state.runId));
  await loadInbox().catch(() => {});
  render();
}
function feedbackFor(momentId) { return ((state.review && state.review.feedback) || []).filter(f => f.moment_id === momentId); }
function currentMoments() { return (state.review && state.review.moments) || []; }
function currentMoment() { return currentMoments()[state.momentIdx] || null; }
