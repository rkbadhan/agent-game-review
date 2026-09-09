"use strict";

// --- full-trace drawer (forensic synchronized panels) ------------------------
const PANELS = [["agent_message","Agent messages"],["tool_io","Tool I/O"],["environment","Environment"],["artifact","Artifacts"]];
function openTrace(stepId) {
  track("full_trace_opened", { run_id: state.runId, step_id: stepId || undefined });
  state.pendingStep = stepId; state.traceOpen = true; state.traceStep = stepId || null;
  renderTrace();
  const d = $("#trace-drawer"); d.classList.add("open"); d.setAttribute("aria-hidden", "false");
  syncUrl();
}
function closeTrace() { const d = $("#trace-drawer"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
  state.traceOpen = false; state.traceStep = null; syncUrl(); }
document.querySelectorAll("[data-close-trace]").forEach(b => b.addEventListener("click", closeTrace));
function renderTrace() {
  const f = state.forensic, body = $("#trace-body"); body.textContent = "";
  const caps = el("div", "caps");
  for (const [name, info] of Object.entries(f.capability_badge || {})) {
    const c = el("div", "cap"); c.append(el("span", "dot " + info.state), el("span", null, name), el("span", "lvl", info.level || info.state)); caps.append(c);
  }
  body.append(caps);
  const grid = el("div", "forensic");
  const fs = el("div", "fsteps"); fs.append(el("div", "th", "Timeline · " + f.steps.length + " source steps"));
  for (const s of f.steps) {
    const row = el("button", "step"); row.dataset.stepId = s.step_id;
    row.append(el("span", "seq", s.sequence != null ? String(s.sequence) : "·"));
    const k = el("div", "k"); const line = el("div"); line.append(el("span", "kind", s.kind || "?"), document.createTextNode(" "), el("span", "who", s.actor || "")); k.append(line);
    k.append(el("div", "who", s.step_id + (s.event_ids && s.event_ids.length ? " → " + s.event_ids.join(", ") : ""))); row.append(k);
    row.addEventListener("click", () => selectStep(s.step_id)); fs.append(row);
  }
  grid.append(fs);
  const panels = el("div", "panels");
  for (const [key, label] of PANELS) { const p = el("div", "pane"); p.dataset.panel = key;
    const ph = el("div", "ph"); ph.append(el("span", null, label)); const av = panelCapabilityState(f, key); if (av) ph.append(el("span", "dot " + av)); p.append(ph);
    p.append(el("div", "pb", (() => { const d = el("div", "dim", "Select a step."); return d; })())); panels.append(p); }
  const vp = el("div", "pane wide"); vp.dataset.panel = "verifier";
  vp.append((() => { const h = el("div", "ph"); h.append(el("span", null, "Verifier")); return h; })());
  const vb = el("div", "pb"); const vt = el("table"); vt.append(rowEls("tr", ["Check", "Status", "Name"], "th"));
  for (const c of f.verifier || []) { const tr = el("tr"); tr.append(td(c.check_id, "mono")); tr.append(tdBadge(c.status)); tr.append(td(c.name)); vt.append(tr); }
  vb.append(vt); vp.append(vb); panels.append(vp); grid.append(panels);
  body.append(grid);
  if (state.pendingStep) { const t = state.pendingStep; state.pendingStep = null; selectStep(t);
    const row = body.querySelector('.step[data-step-id="' + t + '"]'); if (row) row.scrollIntoView({ block: "center" }); }
}
function panelCapabilityState(f, key) { const map = { agent_message: "messages", tool_io: "tool_calls", environment: "process_state", artifact: "filesystem" };
  const cap = map[key]; const info = cap && f.capability_badge ? f.capability_badge[cap] : null; return info ? info.state : null; }
function selectStep(stepId) {
  const f = state.forensic; const step = f.steps.find(s => s.step_id === stepId); if (!step) return;
  state.traceStep = stepId; syncUrl();
  for (const n of document.querySelectorAll("#trace-body .step")) n.classList.toggle("active", n.dataset.stepId === stepId);
  for (const p of document.querySelectorAll("#trace-body .panels .pane")) {
    if (p.dataset.panel === "verifier") continue;
    const isTarget = p.dataset.panel === step.panel; p.classList.toggle("lit", isTarget);
    const pb = $(".pb", p); pb.textContent = "";
    if (isTarget) { pb.classList.add("mono"); pb.append(renderPanelContent(step));
      const av = step.availability || {}; if (av.state && av.state !== "complete") pb.append(el("div", "dim", "evidence " + av.state + (av.level ? " (" + av.capability + ": " + av.level + ")" : ""))); }
    else { pb.classList.remove("mono"); pb.append(el("span", "dim", "—")); }
  }
}
function renderPanelContent(step) { return renderStepContent(step); }

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
  if (av.state === "unavailable") {
    frag.append(el("div", "content-state unavailable", "Not captured"));
    frag.append(el("p", "dim", "This evidence type (" + (av.capability || "capability") + ") is unavailable for this run's capture."));
    return frag;
  }
  const keys = Object.keys(c);
  if (!keys.length) {
    frag.append(el("div", "content-state unavailable", "Not captured"));
    frag.append(el("p", "dim", "This step recorded no content for its capture level."));
    return frag;
  }
  if (av.state === "partial") frag.append(el("div", "content-state partial", "Partial capture"));
  const order = ["direction", "tool", "path", "artifact_path", "exit_code", "content", "data", "summary"];
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
    wrap.append(el("span", "content-flag truncated", "truncated for display · " + text.length + " chars"));
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

