"use strict";

// --- full-trace drawer (forensic synchronized panels) ------------------------
const PANELS = [["agent_message","Agent messages"],["tool_io","Tool I/O"],["environment","Environment"],["artifact","Artifacts"]];
// Which pane a reader is looking at overrides the step's own synchronized
// ("lit") one — set by clicking a tab, cleared whenever a different step is
// selected so the tabs go back to following that step's own panel. null
// means "follow the lit panel", same as no override at all.
let traceActiveTab = null;
let traceQuery = "";
let traceShowEchoes = false;

function traceInitialStep(f, stepId) {
  if (stepId && f.steps.some(s => s.step_id === stepId)) return stepId;
  const moment = currentMoment();
  const anchors = moment ? [...(moment.anchor_event_ids || []),
    ...(moment.facts || []).flatMap(fact => (fact.quotes || []).map(q => q.event_id))] : [];
  const focus = anchors.includes(state.evidenceFocus) ? state.evidenceFocus : anchors[0];
  const anchored = f.steps.find(s => (s.event_ids || []).includes(focus));
  if (anchored) return anchored.step_id;
  // With no review anchor, start near the recorded outcome rather than the prompt.
  const last = [...f.steps].reverse().find(s =>
    ["tool_result", "error_observed", "final_submission"].includes(s.event_type));
  return (last || f.steps[0] || {}).step_id || null;
}
function openTrace(stepId) {
  if (state.loading || !state.forensic || isWorkspaceView(state.view)) return;
  track("full_trace_opened", { run_id: state.runId, step_id: stepId || undefined });
  const f = state.forensic;
  const target = traceInitialStep(f, stepId);
  if (stepId && target !== stepId) toast("That source step is not in this capture — showing the current review position.");
  traceQuery = ""; traceShowEchoes = false;
  state.pendingStep = target; state.traceOpen = true; state.traceStep = target;
  traceActiveTab = null;
  renderTrace();
  const d = $("#trace-drawer"); d.classList.add("open"); d.setAttribute("aria-hidden", "false");
  syncUrl();
}
function closeTrace() { const d = $("#trace-drawer"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
  state.traceOpen = false; state.traceStep = null; syncUrl(); }
document.querySelectorAll("[data-close-trace]").forEach(b => b.addEventListener("click", closeTrace));
function renderTrace() {
  const f = state.forensic, body = $("#trace-body"); body.textContent = "";
  // §4.12 "a capability panel states which evidence types were complete,
  // partial, ... or unavailable" — moved behind a disclosure (§4.9/§4.12) so
  // it stays reachable without occupying permanent space above every trace a
  // reader opens; unavailable/partial capabilities relevant to THIS run
  // still surface inline wherever they actually limit a panel (see the
  // per-panel "evidence <state>" note in selectStep, and the not-reviewable
  // notice on Overview).
  const capsWrap = el("details", "caps-disclosure disclosure");
  const anyLimited = Object.values(f.capability_badge || {}).some(info => info.state !== "complete");
  capsWrap.append(el("summary", null, "Capture capabilities"
    + (anyLimited ? " (some limited)" : "")));
  const caps = el("div", "caps");
  for (const [name, info] of Object.entries(f.capability_badge || {})) {
    const c = el("div", "cap"); c.append(el("span", "dot " + info.state), el("span", null, name), el("span", "lvl", info.level || info.state)); caps.append(c);
  }
  capsWrap.append(caps);
  body.append(capsWrap);
  const entries = traceEntries(f);
  const controls = el("div", "trace-controls");
  const label = el("label", "trace-search-label", "Search trace");
  const search = el("input", "trace-search"); search.type = "search"; search.value = traceQuery;
  search.placeholder = "Commands, output, paths, or step IDs";
  label.append(search); controls.append(label);
  const echoLabel = el("label", "trace-echo-label");
  const echoes = el("input"); echoes.type = "checkbox"; echoes.checked = traceShowEchoes;
  echoLabel.append(echoes, document.createTextNode("Show execution echoes")); controls.append(echoLabel);
  body.append(controls);
  const grid = el("div", "forensic");
  const fs = el("div", "fsteps");
  fs.append(el("div", "th", "Timeline · " + f.steps.length + " source steps"));
  const count = el("div", "trace-count"); count.setAttribute("role", "status");
  fs.append(count);
  const rows = el("div", "trace-rows"); fs.append(rows);
  const renderRows = () => {
    rows.textContent = "";
    const query = traceQuery.trim().toLowerCase();
    const visible = entries.filter(entry => query ? entry.search.includes(query)
      : traceShowEchoes || !entry.echo || entry.step.step_id === state.traceStep);
    count.textContent = visible.length + " of " + entries.length + " entries"
      + (!query && !traceShowEchoes ? " · " + (entries.length - visible.length) + " echoes hidden" : "");
    for (const entry of visible) rows.append(renderTraceRow(entry));
    if (!visible.length) rows.append(el("p", "empty", "No trace entries match. Clear the search to see the timeline."));
    syncTraceSelection();
  };
  search.addEventListener("input", () => { traceQuery = search.value; renderRows(); });
  search.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    event.preventDefault(); event.stopPropagation();
    if (search.value) { search.value = ""; traceQuery = ""; renderRows(); }
    else closeTrace();
  });
  echoes.addEventListener("change", () => { traceShowEchoes = echoes.checked; renderRows(); });
  renderRows();
  grid.append(fs);
  // One inspector: a tab strip (Agent messages / Tool I/O / Environment /
  // Artifacts / Verifier) over a single visible pane, instead of laying all
  // five out as small 2x2(+1) boxes most of which say nothing about the
  // selected step. Every .pane stays in the DOM exactly as before (data-panel,
  // .lit tracking the step's own synchronized panel, the same selectStep()
  // logic) — .pane-active is a second, purely visual class that the tab
  // strip controls, defaulting to whichever pane is .lit.
  const inspector = el("div", "trace-inspector");
  const tabs = el("div", "trace-tabs"); tabs.setAttribute("role", "tablist");
  const runPanels = PANELS.concat(f.steps.some(s => s.panel === "timeline") ? [["timeline", "Run events"]] : []);
  const allTabs = runPanels.concat([["verifier", "Verifier"]]);
  for (const [key, label] of allTabs) {
    const av = key === "verifier" ? null : panelCapabilityState(f, key);
    const btn = el("button", "trace-tab"); btn.type = "button"; btn.dataset.panel = key;
    btn.setAttribute("role", "tab");
    btn.append(document.createTextNode(label));
    if (av) btn.append(el("span", "dot " + av));
    if (av === "unavailable") {
      btn.disabled = true;
      btn.title = label + " — unavailable for this run's capture.";
    }
    btn.addEventListener("click", () => { traceActiveTab = key; syncPaneVisibility(); });
    tabs.append(btn);
  }
  inspector.append(tabs);
  const panels = el("div", "panels");
  for (const [key, label] of runPanels) { const p = el("div", "pane"); p.dataset.panel = key;
    const ph = el("div", "ph"); ph.append(el("span", null, label)); const av = panelCapabilityState(f, key); if (av) ph.append(el("span", "dot " + av)); p.append(ph);
    const pb = el("div", "pb"); pb.append(el("div", "dim", "Select a step.")); p.append(pb); panels.append(p); }
  const vp = el("div", "pane wide"); vp.dataset.panel = "verifier";
  vp.append((() => { const h = el("div", "ph"); h.append(el("span", null, "Verifier")); return h; })());
  const vb = el("div", "pb"); const vt = el("table"); vt.append(rowEls("tr", ["Check", "Status", "Name"], "th"));
  for (const c of f.verifier || []) { const tr = el("tr"); tr.append(td(c.check_id, "mono")); tr.append(tdBadge(c.status)); tr.append(td(c.name)); vt.append(tr); }
  vb.append(vt); vp.append(vb); panels.append(vp);
  inspector.append(panels);
  grid.append(inspector);
  body.append(grid);
  if (state.pendingStep || state.traceStep) { const t = state.pendingStep || state.traceStep; state.pendingStep = null; selectStep(t);
    const row = body.querySelector(".step.active"); if (row) row.scrollIntoView({ block: "nearest" }); }
  else syncPaneVisibility();
}
// Group only captured, reciprocal call/result pairs. Source IDs are retained
// on the row, so a shared link to a result still selects that exact result.
function traceEntries(f) {
  /** @type {Map<string, any>} */
  const byId = new Map(f.steps.map(s => [s.step_id, s]));
  const resultCalls = new Map();
  for (const s of f.steps) {
    const result = byId.get(s.paired_step_id);
    if (s.event_type === "tool_call" && result && result.event_type === "tool_result"
        && result.paired_step_id === s.step_id) resultCalls.set(result.step_id, s);
  }
  return f.steps.filter(s => !resultCalls.has(s.step_id)).map(step => {
    const paired = byId.get(step.paired_step_id);
    const result = paired && resultCalls.get(paired.step_id) === step ? paired : null;
    const echo = step.event_type === "model_output"
      && /^Executed \S+ call_\S+$/.test(String((step.content || {}).content || "").trim())
      && !(step.execution_findings || []).length;
    return { step, result, echo, search: [step, result].filter(Boolean).map(traceSearchText).join(" ").toLowerCase() };
  });
}

function traceContentUnavailable(step) {
  const av = (step && step.availability) || {};
  // Timeline records have no capability axis. Their captured lifecycle text
  // remains available even though the API gives the absent axis an unavailable badge.
  return av.state === "unavailable" && step.panel !== "timeline";
}

function traceSearchText(step) {
  return JSON.stringify([step.step_id, step.event_ids, step.kind, step.actor,
    traceContentUnavailable(step) ? null : step.content]);
}

// Preview selection only: these patterns never change captured tool status.
// Prompts may follow output without a newline, or start mid-prompt in a screen capture.
const TRACE_TERMINAL_PROMPT = /(?:(?:\([^)]*\)\s+)?[\w.-]+@[\w.-]+(?::[^\r\n]*?)?[#$](?:\s|$)|^(?:\([^)]*\)\s*)?(?:\[[^\]\r\n]+\]|[\w.-]+:(?:~|\/)[^\r\n]*?)[#$](?:\s|$)|^PS\s+[^>]+>(?:\s|$)|^[A-Za-z]:\\[^>]*>(?:\s|$))/;
const TRACE_RESULT_NOISE = /^(?:New Terminal Output:|Current Terminal Screen:|\[(?:stdout|stderr|metadata|notice)\]|WARNING:|asciinema:|Script (?:started|done)(?: on|,))/i;

function traceResultPreview(lines) {
  const prompts = lines.map(line => TRACE_TERMINAL_PROMPT.exec(line));
  // A no-newline answer can look like part of the username ("42runner@ci").
  // Reuse identities seen in the same result to recover that output prefix.
  const identities = [...new Set(prompts.filter(Boolean).map(prompt =>
    (prompt[0].match(/^[\w.-]+@[\w.-]+/) || [""])[0]).filter(Boolean))]
    .sort((a, b) => a.length - b.length);
  const terminal = prompts.some(Boolean)
    || lines.some(line => /^(?:New Terminal Output:|Current Terminal Screen:)$/.test(line));
  const output = lines.map((line, index) => {
    const prompt = prompts[index];
    if (!prompt) return terminal && /^(?:[>$#](?:\s|$))/.test(line) ? "" : line;
    const identity = (prompt[0].match(/^[\w.-]+@[\w.-]+/) || [""])[0];
    const known = identities.find(candidate => identity.endsWith(candidate));
    const prefixEnd = prompt.index + (known ? identity.length - known.length : 0);
    return line.slice(0, prefixEnd).trimEnd();
  }).filter(Boolean);
  const substantive = output.filter(line => !TRACE_RESULT_NOISE.test(line));
  // Prefer an exception's message to its traceback header, then other generic
  // failures, then completion signals. Fall back to the last substantive line.
  const exception = output.find(line => /(?:^|\s)[\w.]*(?:Error|Exception)(?::|$)/.test(line));
  const failure = output.find(line => /\b(?:error|fatal|traceback|mismatch|failed|failure)\b|command not found|no (?:such )?(?:file|directory|process|server)\b|not (?:found|running)|connection refused|segmentation fault/i.test(line));
  const success = [...substantive].reverse().find(line => /\b(?:success(?:ful(?:ly)?)?|passed|completed|succeeded)\b/i.test(line));
  return exception || failure || success || substantive[substantive.length - 1]
    || (lines.length ? "No output beyond terminal echoes or notices" : "No captured content");
}

function traceResultText(text) {
  let result = text;
  if (typeof text === "string") {
    try { result = JSON.parse(text); } catch (_) { return text; }
  }
  if (!result || typeof result !== "object" || Array.isArray(result)
      || !["output", "stdout", "stderr", "exception_info", "error", "exception"].some(key => Object.hasOwn(result, key))) return text;
  const outputs = [result.output, result.stdout, result.stderr]
    .filter(value => value != null && value !== "")
    .map(value => typeof value === "string" ? value : JSON.stringify(value));
  const errors = [result.exception_info, result.error, result.exception]
    .filter(value => value && (typeof value !== "object" || Object.keys(value).length))
    .map(value => {
      if (typeof value !== "object") return "Error: " + String(value);
      const type = value.exception_type || value.type || value.name || "Error";
      const message = value.exception_message || value.message || value.traceback || JSON.stringify(value);
      return type + ": " + message;
    });
  return outputs.concat(errors).join("\n");
}

function tracePreview(step) {
  if (traceContentUnavailable(step)) return "Not captured";
  const c = step.content || {};
  let text = c.content ?? c.data ?? c.summary ?? c.path ?? c.artifact_path ?? "";
  // Older Harbor captures store the command as a JSON string. Decode only
  // recognized argument fields; never execute captured commands or markup.
  if (step.event_type === "tool_call" && typeof text === "string") {
    try {
      const args = JSON.parse(text);
      if (args && typeof args === "object")
        text = [args.command, args.cmd, args.keystrokes, args.file_path, args.path]
          .find(value => typeof value === "string" && value.trim()) || text;
    } catch (_) { /* Plain command text. */ }
  }
  if (step.event_type === "tool_result") text = traceResultText(text);
  const raw = typeof text === "object" ? JSON.stringify(text) : String(text);
  const lines = raw.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return truncateText(step.event_type === "tool_result" ? traceResultPreview(lines)
    : lines[0] || "No captured content", 180);
}

function renderTraceRow(entry) {
  const s = entry.step, result = entry.result;
  const row = el("button", "step"); row.type = "button"; row.dataset.stepId = s.step_id;
  if (result) row.dataset.pairedStepId = result.step_id;
  if ([s, result].some(step => step && (step.execution_findings || []).length)) row.classList.add("execution-flag");
  row.append(el("span", "seq", s.sequence != null ? String(s.sequence) : "·"));
  const k = el("div", "k");
  const c = s.content || {}, detail = [];
  if (c.input_tokens != null) detail.push(fmtCompact(c.input_tokens) + " input tokens");
  if (c.wall_ms != null) detail.push(fmtDuration(c.wall_ms / 1000));
  if (c.exit_code != null) detail.push("exit " + c.exit_code);
  const line = el("div");
  line.append(el("span", "kind", c.tool || humanizeToken(s.kind || "step")),
    el("span", "who", " · " + (s.actor || "") + (detail.length ? " · " + detail.join(" · ") : "")));
  k.append(line, el("div", "trace-preview", tracePreview(s)));
  if (result) {
    const output = result.content || {};
    k.append(el("div", "trace-result", (output.exit_code != null ? "Exit " + output.exit_code + " · " : "Result · ") + tracePreview(result)));
  }
  k.append(el("div", "who", [s, result].filter(Boolean).map(step => step.step_id
    + ((step.event_ids || []).length ? " → " + step.event_ids.join(", ") : "")).join(" · ")
    + (row.classList.contains("execution-flag") ? " · ⚠ execution" : "")));
  row.append(k); row.addEventListener("click", () => selectStep(s.step_id));
  return row;
}

function syncTraceSelection() {
  for (const row of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#trace-body .step"))) {
    const active = row.dataset.stepId === state.traceStep || row.dataset.pairedStepId === state.traceStep;
    row.classList.toggle("active", active);
    if (active) row.setAttribute("aria-current", "step"); else row.removeAttribute("aria-current");
  }
}

function panelCapabilityState(f, key) { const map = { agent_message: "messages", tool_io: "tool_calls", environment: "process_state", artifact: "filesystem" };
  const cap = map[key]; const info = cap && f.capability_badge ? f.capability_badge[cap] : null; return info ? info.state : null; }
// The tab strip's own visual layer over the pane the step selection already
// computed: whichever pane is .lit, unless a reader clicked a different tab
// (traceActiveTab) to look at a panel type the step itself didn't light up.
function syncPaneVisibility() {
  const lit = /** @type {HTMLElement | null} */ (document.querySelector("#trace-body .panels .pane.lit"));
  const activeKey = traceActiveTab || (lit && lit.dataset.panel) || "agent_message";
  for (const p of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#trace-body .panels .pane")))
    p.classList.toggle("pane-active", p.dataset.panel === activeKey);
  for (const t of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#trace-body .trace-tab")))
    t.classList.toggle("active", t.dataset.panel === activeKey);
}
function selectStep(stepId) {
  const f = state.forensic; const step = f.steps.find(s => s.step_id === stepId); if (!step) return;
  state.traceStep = stepId; syncUrl();
  traceActiveTab = null;  // a new step goes back to following its own synchronized panel
  syncTraceSelection();
  for (const p of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#trace-body .panels .pane"))) {
    if (p.dataset.panel === "verifier") continue;
    const isTarget = p.dataset.panel === step.panel; p.classList.toggle("lit", isTarget);
    // §4.9/§4.12 "collapse empty panels": a panel that has nothing to say
    // about the selected step shrinks to its header instead of a body that
    // only ever reads "—" — four mostly-empty columns is not "synchronized",
    // it is noise around the one panel that actually lit up.
    p.classList.toggle("empty", !isTarget);
    const pb = $(".pb", p); pb.textContent = "";
    if (isTarget) {
      pb.classList.add("mono");
      pb.append(renderPanelContent(step));
      const av = step.availability || {};
      if (av.capability && av.state && av.state !== "complete")
        pb.append(el("div", "dim", "evidence " + av.state + (av.level ? " (" + av.capability + ": " + av.level + ")" : "")));
      // The matching tool request/result, shown together rather than
      // requiring a second click on a different step.
      const paired = renderPairedStep(f, step);
      if (paired) pb.append(paired);
    } else { pb.classList.remove("mono"); }
  }
  syncPaneVisibility();
}
function renderPanelContent(step) { return renderStepContent(step); }

// Evidence §4.9/§4.12: "the matching tool request and result together" — a
// tool_call's paired tool_result (or a tool_result's paired tool_call), shown
// right beside the focused step instead of requiring a reader to search the
// timeline for the other half of the same exchange. `step.paired_step_id`
// comes from agr.read.get_forensic's own id-based-with-adjacency-fallback
// pairing (mirrors agr._util.paired_call/paired_result); null when the
// capture recorded no matching counterpart.
function renderPairedStep(f, step) {
  if (!step || !step.paired_step_id) return null;
  const idx = f.steps.findIndex(s => s.step_id === step.paired_step_id);
  if (idx < 0) return null;
  const paired = f.steps[idx];
  const wrap = el("div", "evidence-paired");
  const label = step.event_type === "tool_call" ? "Matching result" : "Matching request";
  wrap.append(el("div", "evidence-paired-label", label + " · trace step " + (idx + 1)));
  wrap.append(renderStepContent(paired));
  return wrap;
}

// --- T3: shared step-content rendering (full trace + evidence panel) --------
// One formatter for "what did this source step actually contain" so the
// evidence panel and the full-trace drawer never describe the same event
// differently. `opts.highlightTerms` marks an exact supporting span when one
// of the moment's fact values (expected/observed) appears verbatim in the
// text — never a fuzzy or partial match, so a highlight is never shown for a
// coincidental overlap.
const EXCERPT_LIMIT = 600;
function renderStepContent(step, opts) {
  opts = opts || {};
  const c = (step && step.content) || {};
  const av = (step && step.availability) || {};
  const frag = document.createDocumentFragment();
  const generationTelemetry = ["input_tokens", "output_tokens", "total_tokens", "wall_ms", "start_time", "end_time", "provider", "model"]
    .some(key => c[key] != null);
  if (traceContentUnavailable(step) && !generationTelemetry) {
    frag.append(el("div", "content-state unavailable", "Not captured"));
    frag.append(el("p", "dim", "This evidence type (" + (av.capability || "capability") + ") is unavailable for this run's capture."));
    return frag;
  }
  if (traceContentUnavailable(step) && generationTelemetry) {
    frag.append(el("div", "content-state unavailable", "Message content not captured"));
    frag.append(el("p", "dim", "Generation usage/timing metadata below was captured independently."));
  }
  const keys = Object.keys(c);
  if (!keys.length) {
    frag.append(el("div", "content-state unavailable", "Not captured"));
    frag.append(el("p", "dim", "This step recorded no content for its capture level."));
    return frag;
  }
  if (av.state === "partial") frag.append(el("div", "content-state partial", "Partial capture"));
  const order = ["direction", "tool", "path", "artifact_path", "exit_code", "input_tokens", "output_tokens", "total_tokens", "wall_ms", "start_time", "end_time", "provider", "model", "content", "data", "summary"];
  const seen = new Set();
  const textKeys = new Set(["content", "data", "summary"]);
  for (const key of order.concat(keys.filter(k => !order.includes(k)))) {
    if (c[key] == null || seen.has(key)) continue;
    seen.add(key);
    const line = el("div", "content-line");
    line.append(el("span", "kv", key + ": "));
    if (textKeys.has(key)) line.append(renderExcerpt(String(c[key]), opts.highlightTerms));
    else line.append(document.createTextNode(String(c[key])));
    frag.append(line);
  }
  return frag;
}
// A text excerpt: truncated (for display only, never altering the underlying
// value) past EXCERPT_LIMIT with an explicit "show full text" expansion, and
// an exact supporting span highlighted via <mark> when one is found.
function renderExcerpt(text, highlightTerms) {
  const wrap = el("span", "content-excerpt");
  const over = text.length > EXCERPT_LIMIT;
  const short = over ? text.slice(0, EXCERPT_LIMIT) : text;
  const body = el("span");
  body.append(highlightSpan(short, highlightTerms));
  wrap.append(body);
  if (over) {
    wrap.append(el("span", "content-flag truncated", "Showing " + EXCERPT_LIMIT + " of " + text.length + " characters"));
    const toggle = el("button", "content-expand", "Show full text");
    toggle.addEventListener("click", () => {
      body.textContent = "";
      body.append(highlightSpan(text, highlightTerms));
      toggle.remove();
      wrap.querySelector(".content-flag").textContent = "shown in full · " + text.length + " chars";
    });
    wrap.append(toggle);
  }
  return wrap;
}
// Highlights the single longest exact term from `terms` that occurs verbatim
// in `text`. No match → the plain text, unmarked — a coincidental partial
// overlap is never presented as a located supporting span. A minimum length
// is required: a short value like "0" or "ok" occurs constantly by chance in
// unrelated text, and marking that as "the" supporting span would be exactly
// the false confidence this feature exists to avoid.
const MIN_HIGHLIGHT_TERM_LENGTH = 4;
function highlightSpan(text, terms) {
  const frag = document.createDocumentFragment();
  const hit = (terms || []).map(String)
    .filter(t => t.length >= MIN_HIGHLIGHT_TERM_LENGTH && text.includes(t))
    .sort((a, b) => b.length - a.length)[0];
  if (!hit) { frag.append(document.createTextNode(text)); return frag; }
  const idx = text.indexOf(hit);
  frag.append(document.createTextNode(text.slice(0, idx)));
  frag.append(el("mark", null, hit));
  frag.append(document.createTextNode(text.slice(idx + hit.length)));
  return frag;
}

