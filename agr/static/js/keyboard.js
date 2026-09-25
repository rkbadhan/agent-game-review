"use strict";

// --- help + keyboard ---------------------------------------------------------
// T1 primary navigation: Runs · Patterns · Compare versions.
$("#runs-button").addEventListener("click", goToRuns);
// One help dialog, two tabs (Shortcuts / Glossary) — #help-button/"?" opens
// on Shortcuts, #glossary-button/"G" opens on Glossary; each switches the
// tab even if the dialog is already open on the other one.
function openHelp(tab) {
  const shortcuts = tab !== "glossary";
  $("#help-tab-shortcuts").classList.toggle("active", shortcuts);
  $("#help-tab-glossary").classList.toggle("active", !shortcuts);
  $("#help-tab-shortcuts").setAttribute("aria-selected", shortcuts ? "true" : "false");
  $("#help-tab-glossary").setAttribute("aria-selected", shortcuts ? "false" : "true");
  $("#help-panel-shortcuts").hidden = !shortcuts;
  $("#help-panel-glossary").hidden = shortcuts;
  if (!shortcuts) buildGlossary();
  openModal("#help-modal");
}
$("#help-tab-shortcuts").addEventListener("click", () => openHelp("shortcuts"));
$("#help-tab-glossary").addEventListener("click", () => openHelp("glossary"));
$("#help-button").addEventListener("click", () => openHelp("shortcuts"));
$("#versions-button").addEventListener("click", () => { state.view = "versions"; render(); });
$("#fleet-button").addEventListener("click", () => { state.view = "fleet"; render(); });
// §4.19 glossary + first-session guidance. The terminology overlay no longer
// blocks first use (T1) — it's reachable on demand from Help instead.
$("#glossary-button").addEventListener("click", () => openHelp("glossary"));
$("#open-intro").addEventListener("click", () => { closeModals(); openModal("#intro-modal"); });
$("#intro-glossary").addEventListener("click", () => { dismissIntro(); openHelp("glossary"); });
$("#intro-dismiss").addEventListener("click", dismissIntro);
// The sidebar (brand, primary nav, and — while investigating a
// run — the run queue) is a persistent column on desktop and an off-canvas
// drawer on mobile, opened by the topbar's #queue-toggle and closed by its
// own ×, an outside click on the shade, Escape, or choosing a destination.
function openSidebarDrawer() { $("#sidebar").classList.add("open"); $("#sidebar-shade").classList.add("show"); }
function closeSidebarDrawer() { $("#sidebar").classList.remove("open"); $("#sidebar-shade").classList.remove("show"); }
$("#queue-toggle").addEventListener("click", openSidebarDrawer);
$("#sidebar-close").addEventListener("click", closeSidebarDrawer);
$("#sidebar-shade").addEventListener("click", closeSidebarDrawer);
for (const id of ["#runs-button", "#fleet-button", "#versions-button", "#glossary-button", "#help-button"])
  $(id).addEventListener("click", closeSidebarDrawer);
// Desktop-only collapse to a ~56px icon rail — remembered per browser like
// the panel widths, and never left in a state that could strand a reader if
// localStorage throws (a private window, blocked storage, …).
(function initSidebarCollapse() {
  let collapsed = false;
  try { collapsed = localStorage.getItem("agr-sidebar-collapsed") === "1"; } catch (e) {}
  const apply = () => {
    $("#sidebar").classList.toggle("collapsed", collapsed);
    $("#workspace").classList.toggle("sidebar-collapsed", collapsed);
    $("#sidebar-collapse").setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  };
  apply();
  $("#sidebar-collapse").addEventListener("click", () => {
    collapsed = !collapsed;
    apply();
    try { localStorage.setItem("agr-sidebar-collapsed", collapsed ? "1" : "0"); } catch (e) {}
  });
})();
// The run view-bar's popovers (More analysis, Compare) are mutually exclusive
// and dismiss on an outside click — native <details> do neither, so two could
// sit open and overlap. This runs before the summary's default toggle, so the
// one being opened is not yet in [open] and only the others close.
document.addEventListener("click", e => {
  document.querySelectorAll(".review-nav details[open]").forEach(d => {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});
// UX audit finding #3: the Runs "More filters" popover (inbox.js's
// buildFilterChipsRow, used by both the sidebar and the Runs page) had the
// same missing outside-click dismiss as the view-bar popovers above — but
// wasn't covered by that handler, which is scoped to `.review-nav` and this
// popover lives in the filter row instead (two places: the sidebar's
// #queue-controls and the Runs page's .runs-controls-row). Left open, it sits
// absolute-positioned OVER the table beneath it (app.css's
// `.runs-controls-row .more-filter-panel`), which then blocks clicks on the
// rows underneath — closing it here, the same way, means a reader dismisses
// it with the same click that would otherwise land on a blocked row.
document.addEventListener("click", e => {
  document.querySelectorAll("details.more-filters[open]").forEach(d => {
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
    closeTrace(); closeModals(); closeSidebarDrawer(); state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open");
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
  else if (k === "g") openHelp("glossary");
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
  else if (e.key === "?") openHelp("shortcuts");
});
