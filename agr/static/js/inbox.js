"use strict";

// --- runs inbox --------------------------------------------------------------
// U2: outcome (failed/passed/undetermined) and review status (unreviewed/
// in_progress/handled) are both explicit axes now, not just reachable via a
// sort order — mirrors agr/queue.py's FILTER_CHIPS one-for-one.
//
// Runs §item "simpler controls": a flat wall of ten chips reads as noise.
// Grouped into the two axes a reader actually reasons about — Outcome, Review
// status — with the remaining, less-frequently-needed chips (behavioural
// flags, not states) tucked behind a "More filters" disclosure. FILTER_CHIPS
// stays the flat list every chip resolves to (validated against
// agr/queue.py's FILTER_CHIPS one-for-one) since state.js's URL parsing and
// the queue API both need the flat set, not the grouping.
//
// UNDETERMINED (a verifier ran but reached no clean verdict) and UNVERIFIED (no
// verifier evidence at all) are two different situations and now get their own
// chips (agr/queue.py's outcome_bucket). "Ground truth" / "Analysis only"
// filter on the §6.2 verifier-evidence capability, not on the status string, so
// a run that carries a verifier can still be evaluated even if it is not clean.
/** @type {[string, [string, string][]][]} */
const FILTER_GROUPS = [
  ["Outcome", [["failed", "Failed"], ["passed", "Passed"],
    ["undetermined", "Undetermined"], ["unverified", "Unverified"],
    ["operational_error", "Operational error"]]],
  ["Review status", [["unreviewed", "Unreviewed"], ["in_progress", "In progress"], ["handled", "Reviewed"]]],
];
/** @type {[string, string][]} */
const MORE_FILTER_CHIPS = [["needs_attention", "Needs attention"], ["recovered", "Recovered"],
  ["plausible_recovery", "Plausible recovery"], ["verifier_concern", "Verifier concern"],
  ["ground_truth", "Ground truth"], ["analysis_only", "Analysis only"]];
const MORE_FILTER_DESCRIPTIONS = {
  needs_attention: "Explicitly flagged for manual review.",
  recovered: "Marked as recovered during the run.",
  plausible_recovery: "Shows a possible, unconfirmed recovery.",
  verifier_concern: "Has a verifier result worth inspecting.",
  ground_truth: "A verifier or gold check ran — the task outcome can be judged.",
  analysis_only: "No verifier evidence — only execution findings are supported.",
};
const FILTER_TIPS = {
  operational_error: "The harness broke (rate limit, simulator or judge crash, runner failure) — no valid verdict, and not an agent failure.",
  undetermined: "A verifier ran but reached no clean verdict.",
  unverified: "No verifier evidence at all — task success is not established.",
  ground_truth: "Runs carrying verifier evidence.",
  analysis_only: "Runs without verifier evidence; execution findings only.",
};
const FILTER_CHIPS = FILTER_GROUPS.flatMap(([, chips]) => chips).concat(MORE_FILTER_CHIPS);
const SORT_OPTIONS = [["triage","Triage priority"],["outcome","Outcome"],["review_progress","Review progress"],["cost","Cost"],["duration","Duration"],["recently_updated","Recently updated"]];
const TRIAGE_TIP = "Workflow convenience, not a severity or model-quality score.";
// §4.3.5 fast/deep entry — where each run opens. "Overview" (the default) opens
// the summary chapter so a run reads in one pass; "First key moment" is the fast
// path for repeat triage. The stored value stays "outcome" for the Overview
// choice, for compatibility with existing browser preferences.
const ENTRY_OPTIONS = [["outcome","Overview"],["first_moment","First key moment"]];
function entryPreference() { return state.entryPref === "outcome" ? "outcome" : "first_moment"; }
function setEntryPreference(v) {
  state.entryPref = v === "outcome" ? "outcome" : "first_moment";
  try { localStorage.setItem("agr-entry-pref", state.entryPref); } catch (_) {}
}
// Position of the current run within the active frozen queue, for the §4.2 header.
// One-based for display; null when the run is not part of the current queue view.
function activeQueueIds() {
  if (state.focusedQueue) return state.focusedQueue.run_ids;
  const ids = (state.queue && state.queue.run_ids) || [];
  const patternIds = state.patternRunIds ? ids.filter(id => state.patternRunIds?.has(id)) : ids;
  return state.importRunIds ? patternIds.filter(id => state.importRunIds?.has(id)) : patternIds;
}
function queuePosition() {
  const ids = activeQueueIds();
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
  // neutral, not the red a failure implies. OPERATIONAL_ERROR is a warning:
  // the harness broke, so it is not an agent failure.
  return s==="PASSED"?"passed":(s==="WARNING"||s==="OPERATIONAL_ERROR")?"warning":(s==="UNDETERMINED"||s==="UNVERIFIED")?"undetermined":"failed"; }

function loadInbox() {
  const promise = loadInboxRecords();
  state.inboxPromise = promise;
  promise.finally(() => {
    if (state.inboxPromise !== promise) return;
    state.inboxPromise = null;
    if (state.view === "runs") { refreshWorklistActions(); const inspector = $("#trace-inspector-host"); if (inspector) renderTraceInspector(inspector); }
  }).catch(() => {});
  return promise;
}
function ensureInbox() {
  return state.queue ? Promise.resolve() : state.inboxPromise || loadInbox();
}
async function loadInboxRecords() {
  // UX audit finding #2: capture the token before the request so a NEWER
  // loadInbox() call (a second filter click, a sort change, a "Show all
  // runs" reset) started while this one is still in flight can supersede it.
  // See state.queueLoadToken's comment (state.js) for what this prevents.
  const token = ++state.queueLoadToken;
  const params = new URLSearchParams();
  for (const f of state.filters) params.append("filter", f);
  params.set("sort", state.sort);
  let sweep, queue, focusedRuns;
  try { [sweep, queue, focusedRuns] = await Promise.all([api("/sweep"), api("/queue?" + params.toString()), state.focusedQueue ? api("/runs") : Promise.resolve(null)]); }
  catch (error) { if (token !== state.queueLoadToken) return; error.agrQueueToken = token; state.queueError = error.message; throw error; }
  if (token !== state.queueLoadToken) return;  // superseded by a newer load
  state.sweep = sweep; state.queue = queue; state.queueError = null;
  document.querySelectorAll(".queue-error-notice").forEach(notice => notice.remove());
  if (state.focusedQueue && focusedRuns) { const rows = new Map(focusedRuns.map(r => [r.run_id, r])); state.focusedQueue.runs = state.focusedQueue.run_ids.map(id => rows.get(id)).filter(Boolean); }
  renderSweep();
  if (!isWorkspaceView(state.view)) { renderQueueControls(); renderRunList(); }
  if (state.view === "runs") paintWorklistQueue();
  syncUrl();
  track("queue_view_created", { queue_view_id: state.queue.queue_view_id, sort: state.sort,
    filters: [...state.filters], count: (state.queue.run_ids || []).length });
}
// Shared by the sidebar's own summary (below) and the Runs page's stat line
// (runs.js) — one place computing the sweep identity and its detail string,
// so the two surfaces can never state it differently.
function sweepIdentityText(s) {
  const hv = s.harness_versions || [];
  const harness = hv.length === 1 ? "Harness " + hv[0] : hv.length > 1 ? hv.length + " harnesses" : null;
  // The sweep identity, stated as honestly as the runs allow (§4.3.1): a real id
  // when they share one, "N sweeps" when they span several, "Implicit sweep" when
  // the source declared none.
  const sweep = s.sweep_id === "mixed" ? (s.sweep_ids || []).length + " sweeps"
    : s.sweep_id && s.sweep_id !== "implicit" ? "Sweep " + s.sweep_id : "Implicit sweep";
  const rel = relTime(s.latest_finished_at);
  const detail = [sweep, harness].filter(Boolean).join(" · ") + (rel ? " · finished " + rel : "");
  return { sweep, detail };
}
function renderSweep() {
  const s = state.sweep, host = $("#sweep-summary"); host.textContent = "";
  if (!s) return;
  const { sweep, detail } = sweepIdentityText(s);
  const titleRow = el("div", "queue-title-row");
  titleRow.append(el("h2", null, "All runs"));
  titleRow.append(el("span", "count-pill", String(s.total_runs)));
  host.append(titleRow);
  host.append(el("div", "sweep-name", detail));
  $("#crumb-sweep").textContent = (typeof activeProject === "function" && activeProject() ? activeProject().name : sweep);
  const bo = s.by_outcome || {};
  const stats = el("div", "sweep-stats");
  const stat = (cls, n, lbl) => { const d = el("div", "sweep-stat " + cls); d.append(el("strong", null, String(n)), el("span", null, lbl)); return d; };
  stats.append(stat("failed", (bo.FAILED||0)+(bo.ERROR||0), "failed"), stat("warning", bo.WARNING||0, "warning"), stat("passed", bo.PASSED||0, "passed"), stat("undetermined", bo.OPERATIONAL_ERROR||0, "operational error"));
  host.append(stats);
  const handled = el("div", "handled");
  handled.append(el("span", null, s.handled + " of " + s.triage_eligible + " handled"));
  const track = el("div", "handled-track"); const bar = el("i");
  bar.style.width = (s.triage_eligible ? (s.handled/s.triage_eligible*100) : 0) + "%"; track.append(bar);
  handled.append(track); host.append(handled);
}
// Shared by the investigation sidebar's controls below and the U2 full-width
// Runs table (runs.js) — one filter/sort definition so the two surfaces can
// never silently disagree about what a chip or sort option means.
// `count`: this chip's global match count (queue.py's filter_counts — every
// run in the store, independent of whichever OTHER filters are currently
// active; see its own comment for why). `undefined` before the first /queue
// response lands, never rendered then. A chip whose count is exactly 0 gets
// the `empty` class (app.css fades it) — the whole point being that a reader
// can tell "nothing here" apart from "1,628 runs here" without clicking
// either one, which is exactly the click a real corpus with one dominant
// bucket (e.g. everything UNVERIFIED) used to invite for no reason.
function _fchip(key, label, onChange, count) {
  const on = state.filters.has(key);
  // is-empty, not empty: the bare .empty class is a generic "no content"
  // placeholder style (padding:26px 14px, for panels like "No runs in this
  // sweep") — sharing that name with this chip's own zero-count fade made
  // the placeholder's padding win the cascade and blow the chip up into a
  // huge circle whenever its count was 0.
  const c = el("button", "fchip" + (on ? " on" : "") + (count === 0 ? " is-empty" : ""));
  c.append(el("span", "fchip-label", label));
  // Only a non-zero count earns a badge — the .is-empty fade above already
  // says "nothing here" on its own, and a row of five "0" badges next to
  // the one chip that actually matters is exactly the clutter a compact
  // control bar can't afford.
  if (count) c.append(el("span", "fchip-count", count.toLocaleString()));
  c.setAttribute("aria-pressed", on ? "true" : "false");
  c.dataset.filter = key;
  const tip = [FILTER_TIPS[key], count != null ? count.toLocaleString() + (count === 1 ? " run matches." : " runs match.") : null]
    .filter(Boolean).join(" ");
  if (tip) c.title = tip;
  // UX audit finding #2: re-read state.filters INSIDE the handler rather
  // than closing over the `on` computed when this button was built. onChange
  // triggers an async /queue reload (loadInbox) and the chip only gets its
  // next "on"/"off" class from the render that eventually follows it — a
  // second click on this SAME button before that response lands (a slow
  // /queue query, or just a fast second click) would otherwise still see the
  // stale pre-click `on`, so two quick clicks both ran the "add" branch
  // instead of "add, then remove": the filter never toggled off, and the
  // chip visibly never changed either, which is exactly the "chip stays on,
  // nothing happens" symptom this closes.
  c.addEventListener("click", () => {
    const currentlyOn = state.filters.has(key);
    currentlyOn ? state.filters.delete(key) : state.filters.add(key);
    track("queue_filter_changed", { kind: key, filters: [...state.filters] });
    onChange();
  });
  return c;
}
function _signalFilterOption(key, label, onChange, count) {
  const on = state.filters.has(key);
  const option = el("button", "signal-filter-option" + (on ? " on" : "") + (count === 0 ? " is-empty" : ""));
  option.type = "button";
  option.setAttribute("aria-pressed", on ? "true" : "false");
  option.dataset.filter = key;
  const mark = el("span", "signal-filter-mark"); if (on) mark.append(icon("check"));
  mark.setAttribute("aria-hidden", "true");
  const copy = el("span", "signal-filter-copy");
  const labelRow = el("strong");
  labelRow.append(document.createTextNode(label));
  if (count) labelRow.append(el("span", "fchip-count", count.toLocaleString()));
  copy.append(labelRow);
  copy.append(el("span", null, MORE_FILTER_DESCRIPTIONS[key]));
  option.append(mark, copy);
  // Same stale-closure fix as _fchip above — re-read state.filters at click
  // time rather than the `on` this option was built with.
  option.addEventListener("click", () => {
    const currentlyOn = state.filters.has(key);
    currentlyOn ? state.filters.delete(key) : state.filters.add(key);
    track("queue_filter_changed", { kind: key, filters: [...state.filters] });
    onChange();
  });
  return option;
}
// Runs §item "simpler controls": Outcome and Review status as two labelled
// groups (what a reader actually filters BY), with the remaining behavioural
// flags behind one compact Signals menu — never a ten-chip wall with no
// structure. Its selected-count badge keeps active, hidden filters visible
// without forcing the menu open over the queue whenever the view re-renders.
function buildFilterChipsRow(onChange) {
  // Global per-chip counts (queue.py's filter_counts) — absent before the
  // first /queue response lands, in which case every chip below renders
  // with no badge at all rather than a misleading "0".
  const counts = (state.queue && state.queue.filter_counts) || null;
  const wrap = el("div", "filter-groups");
  for (const [label, chipDefs] of FILTER_GROUPS) {
    const group = el("div", "filter-group");
    group.append(el("span", "filter-group-label", label));
    const row = el("div", "filters");
    for (const [key, chipLabel] of chipDefs)
      row.append(_fchip(key, chipLabel, onChange, counts ? counts[key] : null));
    group.append(row);
    wrap.append(group);
  }
  const activeMore = MORE_FILTER_CHIPS.filter(([key]) => state.filters.has(key));
  const more = el("details", "filter-group more-filters" + (activeMore.length ? " active" : ""));
  const summary = el("summary");
  summary.setAttribute("aria-label", "More filters"
    + (activeMore.length ? "; " + activeMore.length + " selected" : ""));
  const filterIcon = el("span", "more-filter-icon");
  filterIcon.setAttribute("aria-hidden", "true");
  summary.append(filterIcon, el("span", "more-filter-label", "More filters"));
  if (activeMore.length) summary.append(el("span", "more-filter-count", String(activeMore.length)));
  more.append(summary);
  const panel = el("div", "more-filter-panel");
  const panelHead = el("div", "more-filter-panel-head");
  const panelCopy = el("div");
  panelCopy.append(el("strong", null, "More filters"));
  panelCopy.append(el("span", null, "Narrow runs by signals from the evaluation."));
  panelHead.append(panelCopy);
  if (activeMore.length) {
    const reset = el("button", "more-filter-reset", "Clear");
    reset.type = "button";
    reset.addEventListener("click", () => {
      for (const [key] of MORE_FILTER_CHIPS) state.filters.delete(key);
      track("queue_filter_changed", { kind: "clear_signals", filters: [...state.filters] });
      onChange();
    });
    panelHead.append(reset);
  }
  panel.append(panelHead);
  panel.append(el("div", "more-filter-section-label", "Run signals"));
  const moreRow = el("div", "signal-filter-options");
  for (const [key, chipLabel] of MORE_FILTER_CHIPS) {
    moreRow.append(_signalFilterOption(key, chipLabel, onChange, counts ? counts[key] : null));
  }
  panel.append(moreRow);
  more.append(panel);
  more.addEventListener("keydown", e => {
    if (e.key === "Escape") { more.open = false; summary.focus(); }
  });
  wrap.append(more);
  return wrap;
}
function buildSortRow(onChange) {
  const sortRow = el("label", "sort-row"); sortRow.append(el("span", null, "Sort"));
  const sel = el("select");
  for (const [val, label] of SORT_OPTIONS) { const o = el("option", null, label); o.value = val; if (val === state.sort) o.selected = true; sel.append(o); }
  sel.title = state.sort === "triage" ? TRIAGE_TIP : "";
  sel.addEventListener("change", () => { state.sort = sel.value;
    track("queue_sort_changed", { sort: state.sort }); onChange(); });
  sortRow.append(sel);
  return sortRow;
}
function renderQueueControls() {
  const host = $("#queue-controls"); host.textContent = "";
  const refresh = () => loadInbox().catch(showInboxError);
  if (state.focusedQueue) {
    host.append(el("p", "subline", "Frozen review order. Return to Runs to change filters."));
    const definition = state.focusedQueue.definition || {}, parts = [];
    if (definition.search) parts.push("Search: " + definition.search);
    for (const key of definition.filters || []) { const chip = FILTER_CHIPS.find(([known]) => known === key); if (chip) parts.push(chip[1]); }
    if (definition.ai) parts.push("AI: " + definition.ai);
    const order = SORT_OPTIONS.find(([known]) => known === definition.sort); if (order) parts.push(order[1]);
    if (parts.length) host.append(el("p", "focused-definition", parts.join(" · ")));
    return;
  }
  host.append(buildFilterChipsRow(refresh));
  host.append(buildSortRow(refresh));
  // §4.3.5 workspace preference: where a run opens. Changing it never reorders the
  // queue, so it only rebinds state — no inbox reload.
  const entryRow = el("label", "sort-row"); entryRow.append(el("span", null, "Open at"));
  const esel = el("select");
  for (const [val, label] of ENTRY_OPTIONS) { const o = el("option", null, label); o.value = val; if (val === state.entryPref) o.selected = true; esel.append(o); }
  esel.title = "Where each run opens. First key moment is the fast path for repeat triage; Overview opens the summary.";
  esel.addEventListener("change", () => setEntryPreference(esel.value));
  entryRow.append(esel); host.append(entryRow);
}
// Coordinator review 2, item 3: a dense row (status dot + task name,
// ellipsis + checks tally right-aligned) instead of a ~110px four-line card —
// the queue lists every run in the sweep and needs to scan like a list, not a
// stack of cards. Concern/review-mode meta and the review state's own label
// move into the row's `title` tooltip; each also stays in the DOM as
// `.sr-only` text (clipped, not display:none/visibility:hidden), so a test or
// a screen reader that reads the row's own text still finds them (see
// app.css's .sr-only comment) even though a sighted reader now sees only the
// small state icon at the row's end.
//
// A single muted identity line sits under the name: a Langfuse project (or
// any source that names traces after the workflow) gives every run the same
// display name, so without the short session id, step count and captured
// time each row read "process_case" and nothing told two of them apart.
function runListIdentity(r) {
  const bits = [], short = shortRunId(r.run_id);
  if (short) bits.push(short);
  if (r.steps != null) bits.push(r.steps + (r.steps === 1 ? " step" : " steps"));
  const when = relTime(r.finished_at || r.started_at);
  if (when) bits.push(when);
  return bits.join(" · ");
}
// The compact tally only when the run has checks or tests to tally — an
// unverified capture with no task evaluation has nothing to count, and a bare
// "0/0" there read as "zero of zero passed" rather than "not evaluated".
function runListTally(o) {
  const tally = o.test_results || o;
  return tally.total ? outcomeCounts(o, true) : "";
}
function renderRunList() {
  const list = $("#run-list"), runs = state.focusedQueue ? focusedRows().filter(r => runsMatchesSearch(r, state.focusSearch) || (r.main_finding || "").toLowerCase().includes(state.focusSearch.trim().toLowerCase()))
    : ((state.queue && state.queue.runs) || []).filter(r => !state.importRunIds || state.importRunIds.has(r.run_id));
  const readingPosition = list.scrollTop;
  list.textContent = "";
  const count = $("#queue-count");
  if (count) count.textContent = (state.focusedQueue && state.focusSearch ? runs.length + " of " + state.focusedQueue.run_ids.length : runs.length) + (runs.length === 1 ? " run" : " runs");
  if (!runs.length) {
    delete list.dataset.revealed;
    list.append(el("div", "empty", state.focusedQueue && state.focusSearch ? "No runs match Find. Clear the search to see this queue." : "No runs match this queue.")); return;
  }
  function rowButton(r) {
    const btn = el("button", "run" + (r.run_id === state.runId ? " active" : ""));
    btn.dataset.runId = r.run_id;
    const sc = statusClass(r.outcome && r.outcome.status);
    const o = r.outcome || {};
    const line = el("div", "run-line");
    line.append(el("span", "status-dot " + sc));
    const text = el("span", "run-text");
    text.append(el("span", "run-name", runDisplayName(r)));
    const identity = state.focusedQueue ? r.input_excerpt || runRowFindingText(r) || runListIdentity(r) : runListIdentity(r);
    if (identity) text.append(el("span", "run-identity", identity));
    line.append(text);
    line.append(el("span", "run-checks " + sc, runListTally(o)));
    // The status WORD ("PASSED"/"FAILED"/…) is now conveyed by color (the dot,
    // .run-checks) rather than spelled out next to the ratio — kept as text
    // too (clipped, not hidden) so a reader querying the row's own text for
    // it, sighted or not, still finds it.
    const statusText = (o.status||"?").toUpperCase() + " · " + (runListTally(o) || outcomeCounts(o));
    line.append(el("span", "run-status sr-only " + sc, statusText));
    const wf = r.workflow || {};
    const handled = wf.review_progress === "handled";
    const stCls = handled ? "handled" : wf.review_progress === "in_progress" ? "progress" : "";
    const rs = el("span", "review-state " + stCls);
    const stateMark = el("span", "state-mark"); if (handled) stateMark.append(icon("check")); else if (stCls === "progress") stateMark.textContent = "•";
    rs.append(stateMark);
    const rsLabel = el("span", "sr-only",
      handled && wf.disposition ? vocab("disposition", wf.disposition).label : vocab("progress", wf.review_progress || "unreviewed").label);
    if (handled && wf.reviewer) rsLabel.append(document.createTextNode(" · " + wf.reviewer));
    rs.append(rsLabel);
    line.append(rs);
    btn.append(line);

    const c = r.counts || {}, bits = [];
    if (c.strength) bits.push(c.strength + " strong");
    if (c.recovery) bits.push(c.recovery + " recovery");
    if (c.concern) bits.push(c.concern + " concern");
    const cost = fmtCost(r.cost); if (cost) bits.push(cost);
    if (r.contract && r.contract.watermarked) bits.push("provisional");
    bits.push(vocab("review_mode", r.review_mode).label.replace(" review", ""));
    btn.title = runDisplayName(r) + "\n" + statusText + " — " + bits.join(" · ") + "\n" + r.run_id;
    btn.append(el("span", "sr-only run-meta", bits.join(" · ")));

    btn.addEventListener("click", () => selectRun(r.run_id));
    return btn;
  }
  const pages = state.focusPages || (state.focusPages = new Map());
  function renderWindow(host, members, key) {
    const readingPosition = list.scrollTop;
    host.textContent = "";
    let page = pages.get(key) || { start: 0, count: 50 };
    const selected = members.findIndex(run => run.run_id === state.runId);
    if (selected >= 0 && (selected < page.start || selected >= page.start + page.count)) page = { start: Math.floor(selected / 50) * 50, count: 50 };
    if (page.start >= members.length) page = { start: 0, count: 50 };
    pages.set(key, page);
    if (page.start > 0) {
      const earlier = workspaceButton("Show earlier runs", () => { pages.set(key, { start: Math.max(0, page.start - 50), count: page.count + 50 }); renderWindow(host, members, key); });
      earlier.classList.add("queue-page"); host.append(earlier);
    }
    for (const run of members.slice(page.start, page.start + page.count)) host.append(rowButton(run));
    if (page.start + page.count < members.length) {
      const more = workspaceButton("Show more runs", () => { pages.set(key, { start: page.start, count: page.count + 50 }); renderWindow(host, members, key); });
      more.classList.add("queue-page"); host.append(more);
    }
    if (readingPosition) list.scrollTop = readingPosition;
  }
  if (!state.focusedQueue) for (const run of runs) list.append(rowButton(run));
  else if (state.runsGrouping === "none") renderWindow(list, runs, state.focusedQueue.id + ":all");
  else {
    const byId = new Map(runs.map(run => [run.run_id, run]));
    const groups = state.focusedQueue.groups ? state.focusedQueue.groups.map(group => ({ key: group.key, label: group.label,
      runs: group.run_ids.map(id => byId.get(id)).filter(Boolean) })).filter(group => group.runs.length) : worklistGroups(runs);
    const reveal = state.focusedQueue.id + ":" + state.runId;
    for (const group of groups) {
      const details = el("details", "focused-group");
      details.open = !groupIsClosed(group.key) || (state.focusGroupRevealed !== reveal && group.runs.some(run => run.run_id === state.runId));
      details.append(el("summary", null, group.label + " · " + group.runs.length));
      const body = el("div"); details.append(body); list.append(details);
      const key = state.focusedQueue.id + ":" + group.key;
      if (details.open) renderWindow(body, group.runs, key);
      details.addEventListener("toggle", () => {
        if (!details.isConnected) return;
        saveGroupState(group.key, !details.open);
        if (details.open && !body.childNodes.length) renderWindow(body, group.runs, key);
        else if (!details.open) body.textContent = "";
      });
    }
  }
  if (state.focusedQueue) state.focusGroupRevealed = state.focusedQueue.id + ":" + state.runId;
  // Keep the open run in view when it changes (stepper, keyboard, a shared
  // link), but leave the list where the reader scrolled it on any other
  // re-render.
  // Filtering the active run out invalidates its reveal, so clearing the
  // filter brings it back into view even though the selected id is unchanged.
  const activeRow = list.querySelector(".run.active");
  if (!activeRow) delete list.dataset.revealed;
  if (activeRow && list.dataset.revealed !== state.runId) {
    list.dataset.revealed = state.runId;
    activeRow.scrollIntoView({ block: "nearest" });
  } else list.scrollTop = readingPosition;
}
function showInboxError(error) {
  if (error.agrQueueToken && error.agrQueueToken !== state.queueLoadToken) return;
  state.queueError = error.message;
  if (state.view === "runs" || !state.queue) { state.view = "runs"; render(); }
  else {
    const previous = $("#queue-load-error"); if (previous) previous.remove();
    const notice = queueErrorNotice(); notice.id = "queue-load-error"; $("#queue").insertBefore(notice, $("#run-list"));
  }
}
