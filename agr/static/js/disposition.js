"use strict";

// --- bottom bar (queue nav + disposition) ------------------------------------
const DISPOSITIONS = [["diagnosis_accepted","Diagnosis accepted"],["corrected","Corrected"],
  ["task_or_verifier_issue","Task/verifier issue"],["needs_followup","Needs follow-up"],["no_action","No action"]];
function renderBottomBar(main) {
  const rv = state.review, wf = rv.workflow || {};
  const bar = el("div", "bottom-bar");
  const q = state.queue;
  if (q && q.run_ids && q.run_ids.includes(state.runId)) {
    const pos = q.run_ids.indexOf(state.runId) + 1;
    bar.append(el("span", "queue-position", "Run " + pos + " of " + q.run_ids.length + " in this queue"));
  } else bar.append(el("span", "queue-position", ""));

  const traceBtn = el("button", "button subtle", "Open full trace");
  traceBtn.addEventListener("click", () => openTrace(null));
  bar.append(traceBtn);

  const dispWrap = el("div", "disp-wrap");
  const dispBtn = el("button", "button");
  const dispLabel = wf.review_progress === "handled" && wf.disposition ? vocab("disposition", wf.disposition).label + " ✓" : "Set disposition";
  dispBtn.append(document.createTextNode(dispLabel)); dispBtn.append(el("span", "shortcut", "D"));
  // F4: disposition writes to state.runId at click time (writeWorkflow below)
  // — disabled while a newer run/reviewer selection is still loading, same
  // reasoning as the moment-level annotation actions in moments.js.
  if (state.loading) { dispBtn.disabled = true; dispBtn.title = "Loading the selected run — try again once it finishes."; }
  dispBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleDisposition(); });
  const menu = el("div", "disp-menu" + (state.dispOpen ? " open" : "")); menu.id = "disp-menu";
  for (const [val, label] of DISPOSITIONS) {
    const b = el("button", null, label);
    b.addEventListener("click", () => setDisposition(val));
    menu.append(b);
  }
  dispWrap.append(dispBtn, menu); bar.append(dispWrap);

  const nextBtn = el("button", "button moss");
  nextBtn.append(document.createTextNode("Next unhandled → ")); nextBtn.append(el("span", "shortcut", "N"));
  nextBtn.addEventListener("click", gotoNextUnhandled);
  bar.append(nextBtn);
  main.append(bar);
}
function toggleDisposition() { state.dispOpen = !state.dispOpen; const m = $("#disp-menu"); if (m) m.classList.toggle("open", state.dispOpen); }
async function setDisposition(disposition) {
  state.dispOpen = false;
  track("run_disposition_set", { run_id: state.runId, disposition });
  await writeWorkflow({ disposition, progress: "handled" }, null, "Run marked handled");
}
async function writeWorkflow(fields, btn, okMsg) {
  const wf = (state.review && state.review.workflow) || { workflow_version: 0 };
  try {
    await apiPost("/runs/" + encodeURIComponent(state.runId) + "/workflow",
      Object.assign({ base_version: wf.workflow_version, actor: state.reviewer }, fields));
    await refreshRun(); if (okMsg) toast(okMsg);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.detail && e.data.detail.current) {
      state.review.workflow = e.data.detail.current; render(); toast("Updated elsewhere — reloaded");
    } else toast("Write failed");
  }
}
async function gotoNextUnhandled() {
  const params = new URLSearchParams();
  for (const f of state.filters) params.append("filter", f);
  params.set("sort", state.sort);
  try {
    const res = await api("/runs/" + encodeURIComponent(state.runId) + "/next?" + params.toString());
    if (res.next_unhandled) { track("next_unhandled_opened", { run_id: res.next_unhandled });
      selectRun(res.next_unhandled); toast("Opened next unhandled run"); }
    else toast("Queue clear — nothing unhandled");
  } catch (e) { toast("Failed"); }
}

