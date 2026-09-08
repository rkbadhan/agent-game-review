"use strict";

// --- item 21: sibling divergence view ----------------------------------------
//   For a failed run, GET /runs/{run_id}/divergence aligns its tool_call
//   timeline against a PASSING sibling on the same task (difflib sequence
//   matching by phase kind + tool + content) and reports the first point
//   they diverged. This view shows the shared prefix collapsed, the
//   divergence point itself front and centre, and the rest of the aligned
//   timeline below — never a theory of failure a shared next action would
//   refute.
async function renderSiblingDivergence(main) {
  const wrap = el("div", "section");
  main.append(wrap);
  if (state.sibling && state.sibling.runId === state.runId && state.sibling.data) {
    wrap.append(renderSiblingReport(state.sibling.data));
    renderEvidencePanel();
    return;
  }
  wrap.append(el("div", "subline", "Finding a passing sibling…"));
  try {
    const data = await api("/runs/" + encodeURIComponent(state.runId) + "/divergence");
    state.sibling = { runId: state.runId, data };
    wrap.textContent = "";
    wrap.append(renderSiblingReport(data));
  } catch (e) {
    wrap.textContent = "";
    const msg = (e && e.status === 404)
      ? "No passing sibling found on the same task — this run cannot be compared this way."
      : "Failed: " + e.message;
    wrap.append(el("div", "empty", msg));
  }
  renderEvidencePanel();
}

function renderSiblingReport(report) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Sibling divergence"));
  const head = el("p", "chapter-lede");
  head.append(document.createTextNode("Aligned against the passing sibling "));
  const link = el("button", "seg", report.passing_run_id);
  link.addEventListener("click", () => { state.view = "review"; selectRun(report.passing_run_id); });
  head.append(link);
  head.append(document.createTextNode(" on the same task (" + report.task_id + ")."));
  card.append(head);

  const fd = report.first_divergence;
  if (!fd) {
    card.append(el("div", "empty",
      "The two runs' tool_call timelines are IDENTICAL — whatever separated the "
      + "outcomes happened outside the actions themselves (verifier flake, "
      + "environment nondeterminism), not in what the agent did."));
    return card;
  }

  const point = el("div", "sib-divergence-point");
  point.append(el("p", "eyebrow", "First point of divergence — action " + (fd.matched_prefix_length + 1)));
  const row = el("div", "vs-construct");
  row.append(siblingActionCell("This run (failed)", fd.failed_action, true));
  row.append(siblingActionCell("Sibling (passed)", fd.passing_action, false));
  point.append(row);
  card.append(point);

  const scroll = el("div", "table-scroll");
  const t = el("table", "vs-table");
  t.append(rowEls("tr", ["#", "Tag", "This run", "Sibling"], "th"));
  report.aligned.forEach((a, i) => {
    const tr = el("tr");
    if (i === fd.matched_prefix_length) tr.classList.add("sib-row-diverge");
    tr.append(td(String(i + 1)));
    tr.append(td(a.tag));
    tr.append(siblingActionTd(a.failed, true));
    tr.append(siblingActionTd(a.passing, false));
    t.append(tr);
  });
  scroll.append(t);
  const details = el("details", "vs-drill");
  details.append(el("summary", null, "Full aligned timeline (" + report.aligned.length + " actions)"));
  details.append(scroll);
  card.append(details);
  return card;
}

function siblingActionCell(label, action, isFailedSide) {
  const cell = el("div", "vs-field");
  cell.append(el("span", null, label));
  if (!action) {
    cell.append(el("div", "dim", "— run ended here —"));
    return cell;
  }
  const b = el("button", "button" + (isFailedSide ? " primary" : ""),
    (action.tool || "?") + ": " + (action.content || "").slice(0, 60));
  b.title = "Jump to this step in the trace";
  b.addEventListener("click", () => jumpToEventInCurrentRun(action.event_id, isFailedSide));
  cell.append(b);
  if (action.phase_kind) cell.append(el("div", "vs-counts", "phase: " + action.phase_kind));
  return cell;
}

function siblingActionTd(action, isFailedSide) {
  const cell = el("td");
  if (!action) { cell.append(el("span", "dim", "—")); return cell; }
  const b = el("button", "seg", (action.tool || "?") + ": " + (action.content || "").slice(0, 40));
  b.title = isFailedSide ? "Jump to this step in this run's trace" : "This is the sibling's action — open the sibling to trace it";
  b.addEventListener("click", () => jumpToEventInCurrentRun(action.event_id, isFailedSide));
  cell.append(b);
  return cell;
}

// The failed side's event lives in the CURRENTLY open run's own forensic
// view — jump straight to it. The passing side's event lives in the
// SIBLING run, which is not loaded here; opening it changes the run in
// view, matching how a fleet example anchor behaves.
async function jumpToEventInCurrentRun(eventId, isFailedSide) {
  if (!eventId) return;
  if (isFailedSide) {
    const f = state.forensic || {};
    const step = (f.steps || []).find(s => (s.event_ids || []).includes(eventId));
    if (step) openTrace(step.step_id);
    else toast("Could not locate that event in this run's trace.");
    return;
  }
  const sib = state.sibling && state.sibling.data;
  if (!sib) return;
  state.view = "review";
  await selectRun(sib.passing_run_id);
  const f = state.forensic || {};
  const step = (f.steps || []).find(s => (s.event_ids || []).includes(eventId));
  if (step) openTrace(step.step_id);
}
