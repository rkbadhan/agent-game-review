"use strict";

// --- bottom bar (queue nav + disposition) ------------------------------------
const DISPOSITIONS = [["diagnosis_accepted","Diagnosis accepted"],["corrected","Corrected"],
  ["task_or_verifier_issue","Task/verifier issue"],["needs_followup","Needs follow-up"],["no_action","No action"]];
function renderBottomBar(main) {
  const rv = state.review || {}, wf = rv.workflow || {};
  const bar = el("div", "bottom-bar");
  const position = queuePosition();
  bar.append(el("span", "queue-position", position ? "Run " + position.index + " of " + position.total + " in this queue" : "Outside this queue"));
  if (state.focusedQueue) bar.append(workspaceButton("Back to Runs", goToRuns));

  const traceBtn = el("button", "button subtle", "Open full trace");
  traceBtn.disabled = state.loading;
  traceBtn.addEventListener("click", () => openTrace(null));
  bar.append(traceBtn);

  // P0-5: read-only demo mode hides the disposition control outright — the
  // server rejects the write independently (agr/api.py).
  if (!state.readOnly) {
    const dispWrap = el("div", "disp-wrap");
    const dispBtn = el("button", "button" + (wf.review_progress !== "handled" ? " primary" : ""));
    const dispLabel = !state.loading && wf.review_progress === "handled" && wf.disposition ? "Reviewed · " + vocab("disposition", wf.disposition).label + " ✓" : "Mark reviewed";
    dispBtn.append(document.createTextNode(dispLabel)); dispBtn.append(el("span", "shortcut", "D"));
    // F4: disposition writes to state.runId at click time (writeWorkflow below)
    // — disabled while a newer run/reviewer selection is still loading, same
    // reasoning as the moment-level annotation actions in moments.js.
    if (state.loading) { dispBtn.disabled = true; dispBtn.title = "Loading the selected run — try again once it finishes."; }
    dispBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleDisposition(); });
    const menu = el("div", "disp-menu" + (state.dispOpen ? " open" : "")); menu.id = "disp-menu";
    menu.append(el("p", "disposition-help", state.focusedQueue ? "Choose a disposition to mark reviewed and move to the next run." : "Choose a disposition to complete this review."));
    for (const [val, label] of DISPOSITIONS) {
      const b = el("button", null, label);
      b.addEventListener("click", () => setDisposition(val));
      menu.append(b);
    }
    dispWrap.append(dispBtn, menu); bar.append(dispWrap);
  }

  const nextBtn = el("button", "button subtle");
  nextBtn.append(document.createTextNode("Next unhandled → ")); nextBtn.append(el("span", "shortcut", "N"));
  nextBtn.disabled = state.loading;
  nextBtn.addEventListener("click", gotoNextUnhandled);
  bar.append(nextBtn);
  const host = el("div", "review-action-bar"); host.id = "review-action-bar"; host.append(bar); $(".main-col").append(host);
}
function toggleDisposition() { if (state.loading || state.readOnly || isWorkspaceView(state.view)) return; state.dispOpen = !state.dispOpen; const m = $("#disp-menu"); if (m) m.classList.toggle("open", state.dispOpen); }
async function setDisposition(disposition) {
  if (state.loading || state.readOnly || isWorkspaceView(state.view)) return;
  state.dispOpen = false;
  const runId = state.runId;
  track("run_disposition_set", { run_id: runId, disposition });
  const saved = await writeWorkflow({ disposition, progress: "handled" }, null, "Run marked reviewed");
  if (saved && state.focusedQueue && state.runId === runId && !isWorkspaceView(state.view)) await gotoNextUnhandled();
}
async function writeWorkflow(fields, btn, okMsg) {
  if (state.loading || state.readOnly || !state.review || !state.runId || isWorkspaceView(state.view)) return false;
  const runId = state.runId, project = state.projectId, token = state.loadToken;
  const key = project + ":" + runId;
  const pending = state.workflowWrites || (state.workflowWrites = new Set());
  if (pending.has(key)) return false;
  pending.add(key);
  if (btn) btn.disabled = true;
  try {
    // A quick disposition waits for the opening write, then uses its version.
    const opening = state.workflowOpens && state.workflowOpens.get(key);
    if (opening) { try { await opening; } catch (error) { if (error.status === 409) return false; } }
    if (project !== state.projectId || token !== state.loadToken || runId !== state.runId || isWorkspaceView(state.view)) return false;
    const wf = state.review.workflow || { workflow_version: 0 };
    const saved = await apiPost("/runs/" + encodeURIComponent(runId) + "/workflow",
      Object.assign({ base_version: wf.workflow_version, actor: state.reviewer }, fields));
    if (project !== state.projectId || token !== state.loadToken || runId !== state.runId || isWorkspaceView(state.view)) return false;
    state.review.workflow = saved;
    try { await refreshRun(); } catch (_) { toast("Review saved; could not reload its latest data."); return false; }
    if (okMsg && token === state.loadToken) toast(okMsg);
    return token === state.loadToken && !isWorkspaceView(state.view);
  } catch (error) {
    if (project !== state.projectId || token !== state.loadToken || isWorkspaceView(state.view)) return false;
    if (error.status === 409 && error.data && error.data.detail && error.data.detail.current) {
      state.review.workflow = error.data.detail.current; render(); toast("Updated elsewhere — reloaded. Check the disposition before saving again.");
    } else toast("Could not save the review. Try again.");
    return false;
  } finally { pending.delete(key); if (btn && btn.isConnected) btn.disabled = false; }
}
async function gotoNextUnhandled() {
  if (state.loading || state.nextPending || !state.runId || isWorkspaceView(state.view)) return;
  const project = state.projectId, token = state.loadToken, runId = state.runId;
  const ids = activeQueueIds(), remaining = ids.slice(ids.indexOf(runId) + 1);
  state.nextPending = true;
  try {
    // Keep URLs bounded even for a thousand long IDs. The server reads live
    // workflow state, but never re-sorts this explicit, frozen order.
    while (remaining.length) {
      const params = new URLSearchParams();
      do { params.append("order", remaining.shift()); }
      while (remaining.length && params.toString().length + encodeURIComponent(remaining[0]).length < 4000 && params.getAll("order").length < 32);
      const res = await api("/runs/" + encodeURIComponent(runId) + "/next?" + params.toString());
      if (project !== state.projectId || token !== state.loadToken || isWorkspaceView(state.view)) return;
      if (res.next_unhandled) {
        track("next_unhandled_opened", { run_id: res.next_unhandled });
        await selectRun(res.next_unhandled); toast("Opened next unreviewed run"); return;
      }
    }
    const live = await api("/runs");
    if (project !== state.projectId || token !== state.loadToken || isWorkspaceView(state.view)) return;
    const members = new Set(ids), pending = live.filter(row => members.has(row.run_id) && row.workflow.review_progress !== "handled");
    toast(pending.length ? "End of queue · " + pending.length + " runs still need review. Use Previous or return to Runs."
      : "Queue complete — no remaining unreviewed runs. Return to Runs to choose another view.");
  } catch (_) { if (project === state.projectId && token === state.loadToken) toast("Could not load the next run. Try again."); }
  finally { state.nextPending = false; }
}
