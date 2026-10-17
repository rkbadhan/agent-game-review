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
  $(shortcuts ? "#help-tab-shortcuts" : "#help-tab-glossary").focus();
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
const sidebarMobile = window.matchMedia("(max-width: 820px)");
function syncSidebarDrawer() {
  const sidebar = $("#sidebar"), open = sidebarMobile.matches && sidebar.classList.contains("open");
  sidebar.inert = sidebarMobile.matches && !open;
  $("#queue-toggle").setAttribute("aria-expanded", String(open));
  $("#queue-toggle").setAttribute("aria-controls", "sidebar");
  for (const selector of [".main-col", "#evidence-panel", "#queue-resizer", "#evidence-resizer"])
    $(selector).inert = open;
  if (open) { sidebar.setAttribute("role", "dialog"); sidebar.setAttribute("aria-modal", "true"); sidebar.setAttribute("aria-label", "Navigation"); }
  else { sidebar.removeAttribute("role"); sidebar.removeAttribute("aria-modal"); sidebar.removeAttribute("aria-label"); }
}
function openSidebarDrawer() {
  if (!sidebarMobile.matches) return;
  $("#sidebar").classList.add("open"); $("#sidebar-shade").classList.add("show");
  syncSidebarDrawer(); $("#sidebar-close").focus();
}
function closeSidebarDrawer(restoreFocus = true) {
  const wasOpen = $("#sidebar").classList.contains("open");
  $("#sidebar").classList.remove("open"); $("#sidebar-shade").classList.remove("show");
  syncSidebarDrawer();
  if (wasOpen && restoreFocus && sidebarMobile.matches) $("#queue-toggle").focus();
}
$("#queue-toggle").addEventListener("click", openSidebarDrawer);
$("#sidebar-close").addEventListener("click", () => closeSidebarDrawer());
$("#sidebar-shade").addEventListener("click", () => closeSidebarDrawer());
// Close before the destination handler runs, including all project navigation.
$("#sidebar").addEventListener("click", e => {
  if (!sidebarMobile.matches || !e.target.closest(".sidebar-nav-item, #add-runs-button, #glossary-button, #help-button, #reviewer-avatar, .run")) return;
  closeSidebarDrawer(false);
  requestAnimationFrame(() => { if (!document.querySelector(".modal-shade.open")) focusWorkspace("#main h1, #main h2", true); });
}, true);
$("#project-switcher").addEventListener("change", () => closeSidebarDrawer(false));
sidebarMobile.addEventListener("change", () => closeSidebarDrawer(false));
syncSidebarDrawer();
document.addEventListener("keydown", e => {
  if (!sidebarMobile.matches || !$("#sidebar").classList.contains("open")) return;
  if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeSidebarDrawer(); return; }
  if (e.key !== "Tab") { e.stopImmediatePropagation(); return; }
  e.stopImmediatePropagation();
  const items = [...$("#sidebar").querySelectorAll("button, a[href], input, select, textarea, [tabindex]")]
    .filter(n => !n.disabled && n.tabIndex >= 0 && n.getClientRects().length);
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
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
async function goToRuns() {
  cancelPendingRunLoad("runs");
  state.loadToken++; state.loading = false;
  const frozen = state.focusedQueue, back = frozen && frozen.return_to;
  if (frozen) restoreFocusedDefinition(frozen);
  if (back) {
    state.runsAnchorId = back.anchor || state.runId;
    state.runsShown = back.shown || 0; state.runsScroll = back.scroll || 0;
  }
  state.focusedQueue = null;
  state.runId = null; state.review = null; state.forensic = null;
  state.view = "runs"; closeTrace();
  $("#crumb-task").textContent = "Runs";
  if (back && back.inspector) {
    state.inspector.runId = back.inspector;
    render(); await openRunInspector(back.inspector, true);
  } else render();
}

document.addEventListener("click", e => { if (!e.target.closest(".disp-wrap")) { state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open"); } });
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (runPickerDialog.open) closeRunPicker(); else openRunPicker();
    return;
  }
  if (runPickerDialog.open) return;
  // AltGr (Ctrl+Alt) and Option can produce bracket characters on non-US
  // layouts. Keep those character shortcuts, but ignore command chords.
  if (((e.ctrlKey || e.metaKey) && !e.altKey) || e.target.isContentEditable) return;
  if (document.querySelector(".modal-shade.open")) return;
  if (e.target.matches("input, select, textarea")) return;
  const k = e.key.toLowerCase();
  if (e.altKey && !e.ctrlKey && !e.metaKey && ["ArrowDown", "ArrowUp"].includes(e.key) && !isWorkspaceView(state.view)) {
    e.preventDefault(); const ids = activeQueueIds(), index = ids.indexOf(state.runId), next = index + (e.key === "ArrowDown" ? 1 : -1);
    if (next >= 0 && next < ids.length) selectRun(ids[next]); return;
  }
  if (e.key === "Escape" && state.view === "runs" && state.inspector.runId) { closeRunInspector(); return; }
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
