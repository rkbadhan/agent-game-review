"use strict";

// --- select + refresh --------------------------------------------------------
// `restore` carries a shared link's run-level position (§4.1): which surface,
// chapter, moment, and trace step to land on. Anything the loaded review no
// longer contains is reported rather than silently ignored.
async function selectRun(runId, restore) {
  // F4: this selection invalidates any load already in flight (an earlier
  // selectRun/refreshRun/reviewer switch whose response has not landed yet).
  // Capture the token now; after the await, a mismatch means a NEWER
  // selection has since started and this response must be discarded rather
  // than overwrite what the reader has already moved on to.
  const token = ++state.loadToken;
  state.focusSearch = "";
  // F4: snapshot the run currently on screen so a failed load can put it back
  // exactly as it was. Without this, a failure left state.runId pointing at
  // the run that failed to load while state.review/state.forensic (and every
  // other field below) stayed on the previous run — annotations read
  // state.runId, so they re-enabled (via `loading`) and submitted against the
  // wrong run while a different run's review sat on screen.
  const prev = state.pendingSelection && state.loading ? state.pendingSelection.previous : {
    captureId: state.captureId, readOnly: state.readOnly,
    runId: state.runId, review: state.review, forensic: state.forensic,
    momentIdx: state.momentIdx, dispOpen: state.dispOpen, view: state.view,
    chapter: state.chapter, viewed: state.viewed,
    expandedMoments: state.expandedMoments, expandedLessons: state.expandedLessons,
    evidenceFocus: state.evidenceFocus, reviewerKey: state.reviewerKey,
    compare: state.compare,
  };
  state.pendingSelection = { token, previous: prev };
  state.loading = true;
  state.captureId = restore && restore.capture || null;
  state.readOnly = state.serverReadOnly || !!(activeProject() || {}).sample || !!(activeProject() || {}).archived || !!state.captureId;
  state.runId = runId; state.momentIdx = 0; state.dispOpen = false;
  state.view = "review"; state.chapter = "moments"; state.viewed = new Set();
  state.expandedMoments = new Set(); state.expandedLessons = new Set(); state.evidenceFocus = null;
  // AGR-07 UX: the reviewer flip resets on run change — the default view is
  // the most-enriched review of the run being opened.
  state.reviewerKey = null;
  closeTrace();  // a drawer left open belongs to the run being left behind
  state.compare = restore && restore.left && restore.right
    ? { left: restore.left, right: restore.right } : null;
  // Replace the previous findings immediately; keep navigation and disabled
  // review actions available while the selected run is fetched.
  render();
  let forensic, review;
  try {
    [forensic, review] = await Promise.all([
      api("/runs/" + encodeURIComponent(runId) + "/forensic"),
      api(reviewUrl(runId, state.reviewerKey)),
    ]);
  } catch (e) {
    if (token !== state.loadToken || state.runId !== runId || isWorkspaceView(state.view)) return;
    if (token === state.loadToken && state.runId === runId && !isWorkspaceView(state.view)) {
      // Put the previous run back so state.runId matches state.review again —
      // writes stay gated to whatever is actually on screen.
      Object.assign(state, prev);
      state.loading = false; state.pendingSelection = null;
      render();
      toast("Could not load that run — showing the previous one again.");
    }
    throw e;
  }
  if (token !== state.loadToken || state.runId !== runId || isWorkspaceView(state.view)) return;  // superseded by a newer selection
  state.forensic = forensic; state.review = review;
  state.loading = false; state.pendingSelection = null;
  // Landing chapter: a shared link's position (§4.1) wins; otherwise the §4.3.5
  // workspace preference decides between Overview and the first key moment.
  // `normalizeChapter` translates a chapter id from before the T1 navigation
  // simplification (e.g. `outcome`, `audit`) so an old shared link still opens
  // the content it pointed at.
  let restoreChapter = null;
  if (restore && restore.chapter) {
    const norm = normalizeChapter(restore.chapter);
    if (ALL_CHAPTER_IDS.includes(norm)) restoreChapter = norm;
  }
  if (!restoreChapter && restore && restore.moment) restoreChapter = "moments";
  const fastPath = applyEntryPreference(restoreChapter);
  if (restore) {
    if (restore.view === "source" || restore.view === "compare" || restore.view === "sibling")
      state.view = restore.view;
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
  if (window.innerWidth <= 820) closeSidebarDrawer();
  $("#main").scrollTo({ top: 0, behavior: "instant" });
  if (window.innerWidth <= 900) focusWorkspace("#main h1", false);
  // Opening work must not delay showing its evidence.
  if (!state.readOnly && (review.workflow || {}).review_progress === "unreviewed")
    markRunInProgress(runId, review.workflow, state.projectId, token);
}

function markRunInProgress(runId, workflow, project, token) {
  const key = project + ":" + runId;
  const opens = state.workflowOpens || (state.workflowOpens = new Map());
  let opening = opens.get(key);
  if (!opening) {
    opening = apiPost("/runs/" + encodeURIComponent(runId) + "/workflow", {
      actor: state.reviewer, base_version: workflow.workflow_version, progress: "in_progress"
    });
    opens.set(key, opening);
    opening.finally(() => { if (opens.get(key) === opening) opens.delete(key); }).catch(() => {});
  }
  const current = () => project === state.projectId && token === state.loadToken && runId === state.runId
    && !state.readOnly && !isWorkspaceView(state.view);
  const apply = saved => {
    if (!current()) return;
    if (saved.workflow_version >= ((state.review.workflow || {}).workflow_version || 0)) state.review.workflow = saved;
    const chip = $(".shell-chip.wf"); if (chip) paintWorkflowChip(chip, state.review.workflow);
    if (saved.review_progress === "handled") {
      state.dispOpen = false;
      const actions = $("#review-action-bar"); if (actions) { actions.remove(); renderBottomBar(); }
    }
    // Update queue counts without rebuilding the review or losing an edit/focus.
    loadInbox().catch(() => { if (current()) toast("Review opened; could not refresh the run list."); });
  };
  opening.then(apply).catch(error => {
    if (!current()) return;
    if (error.status === 409 && error.data?.detail?.current) {
      apply(error.data.detail.current);
      toast("Review changed elsewhere — check the current disposition before saving again.");
    } else toast("Opened the run, but could not update human review state.");
  });
}
// Apply the §4.3.5 entry preference (or a shared link's explicit chapter). Sets
// state.chapter / momentIdx and returns whether this open used the fast path —
// the recommended jump straight to the first key moment — for §4.21 metrics.
function applyEntryPreference(explicitChapter) {
  const rv = state.review || {};
  if (explicitChapter) { state.chapter = explicitChapter; return false; }
  // Not-reviewable routing (§4.15): no moment is selectable, so open at Overview.
  if (rv.review_mode === "not_reviewable") { state.chapter = "overview"; return false; }
  if (entryPreference() === "outcome") { state.chapter = "overview"; return false; }
  // Fast path: open the first key moment when one exists; otherwise fall back to
  // Overview and say why, exactly as §4.3.5 requires.
  if (currentMoments().length) { state.chapter = "moments"; state.momentIdx = 0; return true; }
  state.chapter = "overview";
  toast("No key moment was selected for this run — opening at Overview.");
  return false;
}
async function refreshRun() {
  // F4: same staleness guard as selectRun — a refresh in flight when the
  // reader switches run/reviewer must not land on top of the new selection.
  const token = state.loadToken, runId = state.runId, reviewerKey = state.reviewerKey;
  const review = await api(reviewUrl(runId, reviewerKey));
  if (token !== state.loadToken || state.runId !== runId || isWorkspaceView(state.view)) return;
  state.review = review;
  await loadInbox().catch(() => {});
  if (token !== state.loadToken || state.runId !== runId || isWorkspaceView(state.view)) return;
  render();
}
// AGR-07 UX: which reviewer's snapshot to serve. ``null`` = the most-enriched
// available review (a model pass if present, else the deterministic baseline).
function reviewUrl(runId, reviewerKey) {
  const base = "/runs/" + encodeURIComponent(runId);
  return reviewerKey ? base + "?reviewer=" + encodeURIComponent(reviewerKey) : base;
}
function feedbackFor(momentId) { return ((state.review && state.review.feedback) || []).filter(f => f.moment_id === momentId); }
function currentMoments() { return (state.review && state.review.moments) || []; }
function currentMoment() { return currentMoments()[state.momentIdx] || null; }
