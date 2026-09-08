"use strict";

// --- runs inbox --------------------------------------------------------------
const FILTER_CHIPS = [["failed","Failed"],["needs_attention","Needs attention"],["recovered","Recovered"],["plausible_recovery","Plausible recovery"],["verifier_concern","Verifier concern"],["unreviewed","Unreviewed"]];
const SORT_OPTIONS = [["triage","Triage priority"],["outcome","Outcome"],["review_progress","Review progress"],["cost","Cost"],["duration","Duration"],["recently_updated","Recently updated"]];
const TRIAGE_TIP = "Workflow convenience, not a severity or model-quality score.";
// §4.3.5 fast/deep entry — where each run opens. "First key moment" is the fast
// path for repeat triage; "Outcome" opens the summary chapter.
const ENTRY_OPTIONS = [["first_moment","First key moment"],["outcome","Outcome"]];
function entryPreference() { return state.entryPref === "outcome" ? "outcome" : "first_moment"; }
function setEntryPreference(v) {
  state.entryPref = v === "outcome" ? "outcome" : "first_moment";
  localStorage.setItem("agr-entry-pref", state.entryPref);
}
// Position of the current run within the active frozen queue, for the §4.2 header.
// One-based for display; null when the run is not part of the current queue view.
function queuePosition() {
  const ids = (state.queue && state.queue.run_ids) || [];
  const i = ids.indexOf(state.runId);
  return i >= 0 ? { index: i + 1, total: ids.length } : null;
}

function fmtDuration(sec) { if (sec == null) return null; sec = Math.round(sec);
  if (sec < 60) return sec + "s"; const m = Math.floor(sec/60), s = sec%60;
  if (m < 60) return m + "m " + String(s).padStart(2,"0") + "s"; return Math.floor(m/60) + "h " + String(m%60).padStart(2,"0") + "m"; }
function relTime(iso) { if (!iso) return null; const t = Date.parse(iso); if (isNaN(t)) return null;
  const s = Math.max(0, (Date.now()-t)/1000); if (s<90) return "just now"; if (s<5400) return Math.round(s/60)+"m ago";
  if (s<129600) return Math.round(s/3600)+"h ago"; return Math.round(s/86400)+"d ago"; }
function fmtCost(c) { return c == null ? null : "$" + Number(c).toFixed(2); }
function statusClass(s) { s = (s||"").toUpperCase();
  // F1 follow-up: UNDETERMINED/UNVERIFIED are honest "no verdict" states —
  // neutral, not the red a failure implies.
  return s==="PASSED"?"passed":s==="WARNING"?"warning":(s==="UNDETERMINED"||s==="UNVERIFIED")?"undetermined":"failed"; }

async function loadInbox() {
  const params = new URLSearchParams();
  for (const f of state.filters) params.append("filter", f);
  params.set("sort", state.sort);
  [state.sweep, state.queue] = await Promise.all([api("/sweep"), api("/queue?" + params.toString())]);
  renderSweep(); renderQueueControls(); renderRunList(); syncUrl();
  track("queue_view_created", { queue_view_id: state.queue.queue_view_id, sort: state.sort,
    filters: [...state.filters], count: (state.queue.run_ids || []).length });
}
function renderSweep() {
  const s = state.sweep, host = $("#sweep-summary"); host.textContent = "";
  if (!s) return;
  const hv = s.harness_versions || [];
  const harness = hv.length === 1 ? "Harness " + hv[0] : hv.length > 1 ? hv.length + " harnesses" : null;
  // The sweep identity, stated as honestly as the runs allow (§4.3.1): a real id
  // when they share one, "N sweeps" when they span several, "Implicit sweep" when
  // the source declared none.
  const sweep = s.sweep_id === "mixed" ? (s.sweep_ids || []).length + " sweeps"
    : s.sweep_id && s.sweep_id !== "implicit" ? "Sweep " + s.sweep_id : "Implicit sweep";
  const titleRow = el("div", "queue-title-row");
  titleRow.append(el("h2", null, "All runs"));
  titleRow.append(el("span", "count-pill", String(s.total_runs)));
  host.append(titleRow);
  const rel = relTime(s.latest_finished_at);
  const detail = [sweep, harness].filter(Boolean).join(" · ") + (rel ? " · finished " + rel : "");
  host.append(el("div", "sweep-name", detail));
  $("#crumb-sweep").textContent = sweep;
  const bo = s.by_outcome || {};
  const stats = el("div", "sweep-stats");
  const stat = (cls, n, lbl) => { const d = el("div", "sweep-stat " + cls); d.append(el("strong", null, String(n)), el("span", null, lbl)); return d; };
  stats.append(stat("failed", (bo.FAILED||0)+(bo.ERROR||0), "failed"), stat("warning", bo.WARNING||0, "warning"), stat("passed", bo.PASSED||0, "passed"));
  host.append(stats);
  const handled = el("div", "handled");
  handled.append(el("span", null, s.handled + " of " + s.triage_eligible + " handled"));
  const track = el("div", "handled-track"); const bar = el("i");
  bar.style.width = (s.triage_eligible ? (s.handled/s.triage_eligible*100) : 0) + "%"; track.append(bar);
  handled.append(track); host.append(handled);
}
function renderQueueControls() {
  const host = $("#queue-controls"); host.textContent = "";
  const chips = el("div", "filters");
  for (const [key, label] of FILTER_CHIPS) {
    const on = state.filters.has(key);
    const c = el("button", "fchip" + (on ? " on" : ""), label);
    c.setAttribute("aria-pressed", on ? "true" : "false");
    c.dataset.filter = key;
    c.addEventListener("click", () => { on ? state.filters.delete(key) : state.filters.add(key);
      track("queue_filter_changed", { kind: key, filters: [...state.filters] });
      loadInbox().catch(showInboxError); });
    chips.append(c);
  }
  host.append(chips);
  const sortRow = el("label", "sort-row"); sortRow.append(el("span", null, "Sort"));
  const sel = el("select");
  for (const [val, label] of SORT_OPTIONS) { const o = el("option", null, label); o.value = val; if (val === state.sort) o.selected = true; sel.append(o); }
  sel.title = state.sort === "triage" ? TRIAGE_TIP : "";
  sel.addEventListener("change", () => { state.sort = sel.value;
    track("queue_sort_changed", { sort: state.sort }); loadInbox().catch(showInboxError); });
  sortRow.append(sel); host.append(sortRow);
  // §4.3.5 workspace preference: where a run opens. Changing it never reorders the
  // queue, so it only rebinds state — no inbox reload.
  const entryRow = el("label", "sort-row"); entryRow.append(el("span", null, "Open at"));
  const esel = el("select");
  for (const [val, label] of ENTRY_OPTIONS) { const o = el("option", null, label); o.value = val; if (val === state.entryPref) o.selected = true; esel.append(o); }
  esel.title = "Where each run opens (§4.3.5). First key moment is the fast path for repeat triage; Outcome opens the summary.";
  esel.addEventListener("change", () => setEntryPreference(esel.value));
  entryRow.append(esel); host.append(entryRow);
}
function renderRunList() {
  const list = $("#run-list"), runs = (state.queue && state.queue.runs) || [];
  list.textContent = "";
  if (!runs.length) { list.append(el("div", "empty", "No runs match this queue.")); return; }
  for (const r of runs) {
    const btn = el("button", "run" + (r.run_id === state.runId ? " active" : ""));
    btn.dataset.runId = r.run_id;
    const sc = statusClass(r.outcome && r.outcome.status);
    const line = el("div", "run-line");
    line.append(el("span", "status-dot " + sc));
    const o = r.outcome || {};
    line.append(el("span", "run-status " + sc, (o.status||"?").toUpperCase() + " · " + (o.passed??"?") + "/" + (o.total??"?")));
    line.append(el("span", "run-mode", vocab("review_mode", r.review_mode).label.replace(" review", "")));
    btn.append(line);
    btn.append(el("div", "run-name", r.task_id || r.run_id));
    const c = r.counts || {}, bits = [];
    if (c.strength) bits.push(c.strength + " strong");
    if (c.recovery) bits.push(c.recovery + " recovery");
    if (c.concern) bits.push(c.concern + " concern");
    if (r.steps != null) bits.push(r.steps + " steps");
    const cost = fmtCost(r.cost); if (cost) bits.push(cost);
    if (r.contract && r.contract.watermarked) bits.push("provisional");
    if (bits.length) btn.append(el("div", "run-meta", bits.join(" · ")));
    const wf = r.workflow || {};
    const handled = wf.review_progress === "handled";
    const stCls = handled ? "handled" : wf.review_progress === "in_progress" ? "progress" : "";
    const rs = el("div", "review-state " + stCls);
    rs.append(el("span", "state-mark", handled ? "✓" : stCls === "progress" ? "•" : ""));
    rs.append(document.createTextNode(handled && wf.disposition ? vocab("disposition", wf.disposition).label : vocab("progress", wf.review_progress || "unreviewed").label));
    if (handled && wf.reviewer) rs.append(el("span", null, " · " + wf.reviewer));
    btn.append(rs);
    btn.addEventListener("click", () => selectRun(r.run_id));
    list.append(btn);
  }
}
function showInboxError(e) { $("#run-list").append(el("div", "empty", "Failed to load queue: " + e.message)); }
