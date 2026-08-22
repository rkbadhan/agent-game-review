"use strict";

// --- render root -------------------------------------------------------------
function render() {
  const main = $("#main"); main.textContent = "";
  // The version comparison is a surface, not a run view: it renders with no run
  // selected, so it is dispatched before the "pick a run" guard.
  if (state.view === "versions") { renderVersionsSurface(main); syncUrl(); return; }
  if (!state.review) { main.append(el("div", "empty", "Select a run to begin.")); return; }
  const rv = state.review, run = rv.run || {};
  $("#crumb-task").textContent = run.task_id || state.runId;

  // header — the §4.2 shared run-review shell. The eyebrow carries sweep identity
  // and queue position when entered from a sweep; the subtitle carries model +
  // harness versions, step count, duration, and cost; then the outcome pill. The
  // review-mode / workflow / capture-completeness strip renders directly beneath.
  const header = el("div", "run-header");
  const left = el("div");
  const eyebrow = el("p", "eyebrow");
  eyebrow.append(document.createTextNode("Task review"));
  const sweepLabel = shellSweepLabel(run);
  if (sweepLabel) eyebrow.append(el("span", "eyebrow-sep", "·"), document.createTextNode(sweepLabel));
  const pos = queuePosition();
  if (pos) eyebrow.append(el("span", "eyebrow-sep", "·"), document.createTextNode("Run " + pos.index + " of " + pos.total));
  left.append(eyebrow);
  left.append(el("h1", null, run.task_id || state.runId));
  const steps = (state.forensic && state.forensic.steps) ? state.forensic.steps.length : null;
  const subBits = [run.model, run.harness_version ? "harness " + run.harness_version : null,
    steps != null ? steps + " steps" : null, fmtDuration(durationOf(run)),
    fmtCost(costOf(run))].filter(Boolean).join(" · ");
  const sub = el("div", "run-subtitle");
  if (subBits) sub.append(document.createTextNode(subBits + " · "));
  sub.append(el("span", "mono", state.runId));
  left.append(sub);
  header.append(left);
  const o = rv.outcome || {}, sc = statusClass(o.status);
  const hs = el("div", "header-status " + sc);
  hs.append(el("span", "status-dot " + sc), el("span", null, (o.status||"?") + " · " + (o.passed??"?") + "/" + (o.total??"?") + " checks"));
  header.append(hs);
  main.append(header);
  main.append(renderShellMeta(rv));

  if (rv.watermark) main.append(el("div", "watermark", "⚠ " + rv.watermark));
  // Not-reviewable routing (§4.15): the capture is too incomplete for a
  // trustworthy review, so name the missing evidence up front rather than
  // presenting empty chapters as if a review had run.
  if (rv.review_mode === "not_reviewable") main.append(renderNotReviewable(rv));

  // review outline rail (§4.2 / §4.4) — the chapter list drives the main stage.
  // The compare view (§4.16) is not a chapter, so the outline hides there.
  if (state.view === "review") state.viewed.add(state.chapter);
  const meta = chapterMeta();
  const nav = el("div", "review-nav");
  if (state.view !== "compare") {
  const outline = el("div", "outline"); outline.setAttribute("role", "tablist");
  outline.setAttribute("aria-label", "Review chapters");
  for (const [id, label] of CHAPTERS) {
    const m = meta[id];
    const current = state.view === "review" && state.chapter === id;
    let cls = "ochip";
    if (current) cls += " current";
    else if (!m.available) cls += " unavailable";
    else if (m.corrected) cls += " corrected";
    else if (state.viewed.has(id)) cls += " done";
    const b = el("button", cls);
    const mark = !m.available ? "–" : m.corrected ? "~" : (state.viewed.has(id) && !current ? "✓" : "•");
    b.append(el("span", "ochip-mark", mark), el("span", "ochip-label", label));
    b.title = m.available ? label : label + " · Not available — " + m.reason;
    b.setAttribute("aria-current", current ? "step" : "false");
    b.addEventListener("click", () => setChapter(id));
    outline.append(b);
  }
  nav.append(outline);
  } // end outline (hidden in compare view)
  const util = el("div", "review-util");
  const srcBtn = el("button", "seg" + (state.view === "source" ? " active" : ""), "Source");
  srcBtn.addEventListener("click", () => { state.view = "source"; render(); });
  util.append(srcBtn);
  // §4.16 compare surface: enabled only when two or more reviews of this
  // capture exist. With just the deterministic baseline it shows a disabled
  // affordance with an honest tooltip — the same pattern as the §4.11 lesson.
  const avail = (rv.available_reviews || []);
  const cmpBtn = el("button", "seg" + (state.view === "compare" ? " active" : ""), "Compare");
  if (avail.length >= 2) {
    cmpBtn.title = "Side-by-side comparison of two reviews (§4.16)";
    cmpBtn.addEventListener("click", () => { state.view = "compare"; render(); });
  } else {
    cmpBtn.disabled = true;
    cmpBtn.title = "Compare needs a second review of this run (e.g. a model-reviewer pass).";
  }
  util.append(cmpBtn);
  const traceBtn = el("button", "button subtle", "Full trace");
  traceBtn.append(el("span", "shortcut", "T"));
  traceBtn.addEventListener("click", () => openTrace(null));
  util.append(traceBtn);
  nav.append(util);
  main.append(nav);

  if (state.view === "source") renderSource(main);
  else if (state.view === "compare") renderCompare(main);
  else renderChapter(main);

  renderEvidencePanel();
  syncUrl();
}

// --- §4.2 shell details ------------------------------------------------------
// Sweep identity for the run header, from the run's own declared sweep. Honest:
// omitted when the source declared none rather than invented.
function shellSweepLabel(run) { return run && run.sweep_id ? "Sweep " + run.sweep_id : null; }
// Cost is shown only when the capture carries a numeric cost (§4.2/§4.3.3). The
// current ATIF declares none, so this stays absent rather than fabricated.
function costOf(run) { return typeof (run && run.cost) === "number" ? run.cost : null; }

// The review-mode / workflow / capture-completeness strip beneath the header
// (§4.2): what kind of review this is, where it stands, who owns it, and whether
// the capture was complete — each stated before the reader reads the review.
function renderShellMeta(rv) {
  const wrap = el("div", "shell-meta");
  const mode = rv.review_mode || "deterministic_only";
  const mv = vocab("review_mode", mode);
  const modeCls = mode === "not_reviewable" ? "warn" : mode === "model_enriched" ? "enriched" : "det";
  const modeChip = el("span", "shell-chip mode-" + modeCls, mv.label);
  modeChip.title = mv.tip; wrap.append(modeChip);
  const wf = rv.workflow || {};
  const handled = wf.review_progress === "handled";
  const wfVocab = handled && wf.disposition ? vocab("disposition", wf.disposition)
    : vocab("progress", wf.review_progress || "unreviewed");
  const wfChip = el("span", "shell-chip wf" + (handled ? " on" : ""), wfVocab.label);
  wfChip.title = wfVocab.tip; wrap.append(wfChip);
  const who = [wf.reviewer ? "Reviewer " + wf.reviewer : null,
    wf.assignee ? "Assigned " + wf.assignee : null].filter(Boolean).join(" · ");
  if (who) wrap.append(el("span", "shell-who", who));
  const comp = (rv.capture || {}).capture_completeness;
  if (comp && comp !== "complete") {
    const warn = el("span", "shell-chip warn", "Capture: " + comp);
    warn.title = "The source capture is " + comp + " — some evidence may be missing.";
    wrap.append(warn);
  }
  return wrap;
}

// The §4.15 not-reviewable notice: the review mode plus exactly which capture
// capabilities were missing, so the reason is legible rather than a blank stage.
function renderNotReviewable(rv) {
  const card = el("div", "notice not-reviewable");
  card.append(el("p", "eyebrow", "Not reviewable"));
  card.append(el("p", "notice-lede",
    "The captured evidence is insufficient for a trustworthy review. Outcome and the raw "
    + "source stay available; guided moments are withheld rather than generated."));
  const missing = rv.missing_capabilities || [];
  if (missing.length) {
    const row = el("div", "missing-caps");
    row.append(el("span", "missing-caps-label", "Missing capture"));
    for (const c of missing) row.append(el("span", "cap-pill", c));
    card.append(row);
  }
  return card;
}
