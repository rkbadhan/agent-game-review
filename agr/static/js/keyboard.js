"use strict";

// --- help + keyboard ---------------------------------------------------------
// T1 primary navigation: Runs · Patterns · Compare versions.
$("#runs-button").addEventListener("click", goToRuns);
$("#help-button").addEventListener("click", () => openModal("#help-modal"));
$("#versions-button").addEventListener("click", () => { state.view = "versions"; render(); });
$("#fleet-button").addEventListener("click", () => { state.view = "fleet"; render(); });
// §4.19 glossary + first-session guidance. The terminology overlay no longer
// blocks first use (T1) — it's reachable on demand from Help instead.
$("#glossary-button").addEventListener("click", openGlossary);
$("#open-intro").addEventListener("click", () => { closeModals(); openModal("#intro-modal"); });
$("#intro-glossary").addEventListener("click", () => { dismissIntro(); openGlossary(); });
$("#intro-dismiss").addEventListener("click", dismissIntro);
$("#queue-toggle").addEventListener("click", () => $("#queue").classList.toggle("open"));
// Narrow-screen overflow menu: each item forwards to its real appbar button so
// there is a single set of handlers. Selecting one closes the menu.
document.querySelectorAll("#appbar-menu [data-proxy]").forEach(item => {
  item.addEventListener("click", () => {
    const target = $("#" + item.dataset.proxy);
    if (target) target.click();
    $("#appbar-menu").removeAttribute("open");
  });
});
// Dismiss the overflow menu on an outside click, like a normal popover.
document.addEventListener("click", e => {
  const menu = $("#appbar-menu");
  if (menu && menu.open && !e.target.closest("#appbar-menu")) menu.removeAttribute("open");
});
// The run view-bar's popovers (More analysis, Compare) are mutually exclusive
// and dismiss on an outside click — native <details> do neither, so two could
// sit open and overlap. This runs before the summary's default toggle, so the
// one being opened is not yet in [open] and only the others close.
document.addEventListener("click", e => {
  document.querySelectorAll(".review-nav details[open]").forEach(d => {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});
// "Runs" (U1): the global triage workspace, distinct from a run's investigation
// shell — full width, no evidence panel (render.js's isWorkspaceView), reached
// from the app bar regardless of what was open before.
function goToRuns() {
  state.runId = null; state.review = null; state.forensic = null;
  state.view = "runs"; closeTrace();
  $("#crumb-task").textContent = "Runs";
  render();
}
document.addEventListener("click", e => { if (!e.target.closest(".disp-wrap")) { state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open"); } });
document.addEventListener("keydown", e => {
  if (e.target.matches("input, select, textarea")) return;
  const k = e.key.toLowerCase();
  const onMoments = state.view === "review" && state.chapter === "moments";
  if (e.key === "Escape") {
    closeTrace(); closeModals(); state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open");
    if (state.view === "compare" || state.view === "sibling") { state.view = "review"; render(); }
    // U1: leaving a global workspace (Patterns / Compare versions) returns to
    // the run being investigated when one is loaded, else to the Runs
    // workspace — never the empty "review" mode with nothing to show.
    else if (state.view === "versions" || state.view === "fleet") {
      state.view = state.runId ? "review" : "runs"; render();
    }
  }
  else if (k === "v") { state.view = state.view === "versions" ? (state.runId ? "review" : "runs") : "versions"; render(); }
  else if (k === "f") { state.view = state.view === "fleet" ? (state.runId ? "review" : "runs") : "fleet"; render(); }
  else if (k === "g") openGlossary();
  else if (!state.review) return;
  else if (e.key === "[") moveChapter(-1);
  else if (e.key === "]") moveChapter(1);
  else if (k === "j") { if (onMoments) moveMoment(1); }
  else if (k === "k") { if (onMoments) moveMoment(-1); }
  else if (k === "e") { if (onMoments && currentMoment()) focusEvidence(); }
  else if (k === "c") {
    if (state.view === "compare") { state.view = "review"; render(); }
    else if ((state.review.available_reviews || []).length >= 2) { state.view = "compare"; render(); }
    else toast("Compare reviewer outputs needs a second review of this run");
  }
  // F4 follow-up (review of PR #64): the bottom-bar button already disables
  // itself while a run/reviewer selection is loading (disposition.js) — the
  // shortcut must honor the same guard instead of opening the menu around it.
  // F4 follow-up (review of PR #64): the bottom-bar button already disables
  // itself while a run/reviewer selection is loading (disposition.js) — the
  // shortcut must honor the same guard instead of opening the menu around it.
  else if (k === "d") { if (!state.loading) toggleDisposition(); }
  else if (k === "n") gotoNextUnhandled();
  else if (k === "t") openTrace(null);
  else if (e.key === "?") openModal("#help-modal");
});
