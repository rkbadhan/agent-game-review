"use strict";

// A one-line, plain-language verdict for the run header. Mirrors the Overview
// "What happened" logic so the two never disagree, and — like Overview — it only
// restates validated check facts (never invents impact or intent). Returns
// { tone, headline, detail } or null when there is nothing faithful to say.
function verdictSummary(rv) {
  if (!rv) return null;
  const checks = rv.checks || [];
  const failed = checks.filter(c => c.status === "failed");
  const undetermined = checks.filter(c => ["unknown", "skipped", "error"].includes(c.status));
  if (rv.review_mode === "not_reviewable")
    return { tone: "warn", headline: "Not reviewable —",
      detail: "the captured evidence is insufficient for a trustworthy review." };
  if (!checks.length)
    return { tone: "warn", headline: "Unverified —",
      detail: "no atomic check evidence was captured, so nothing here should be read as a pass." };
  if (failed.length)
    return { tone: "fail", headline: "Failed —",
      detail: failed.length + " of " + checks.length + " checks failed: "
        + failed.map(c => c.check_id + " (" + c.name + ")").join("; ") + "." };
  if (undetermined.length)
    return { tone: "warn", headline: "Undetermined —",
      detail: "no check failed, but " + undetermined.map(c => c.check_id + " (" + c.status + ")").join(", ")
        + " recorded no verdict." };
  return { tone: "pass", headline: "Passed —",
    detail: "all " + checks.length + " requirement " + (checks.length === 1 ? "check" : "checks") + " evidenced." };
}

// --- render root -------------------------------------------------------------
function render() {
  const main = $("#main"); main.textContent = "";
  // The version comparison and the fleet view are surfaces, not run views:
  // each renders with no run selected, so both are dispatched before the
  // "pick a run" guard.
  if (state.view === "versions") { renderVersionsSurface(main); syncUrl(); return; }
  if (state.view === "fleet") { renderFleetSurface(main); syncUrl(); return; }
  if (!state.review) { main.append(el("div", "empty", "Select a run to begin.")); syncUrl(); return; }
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
  // Queue position doubles as a stepper: walk the sweep run-by-run from the
  // header without going back to the queue list. (Bottom bar's "Next unhandled"
  // skips handled runs; this steps every run in order.)
  const pos = queuePosition();
  if (pos) {
    const ids = state.queue.run_ids;
    eyebrow.append(el("span", "eyebrow-sep", "·"));
    const stepper = el("span", "run-stepper");
    const prev = el("button", "step-arrow", "‹");
    prev.title = "Previous run in this queue";
    if (pos.index > 1) prev.addEventListener("click", () => selectRun(ids[pos.index - 2]));
    else prev.disabled = true;
    const next = el("button", "step-arrow", "›");
    next.title = "Next run in this queue";
    if (pos.index < pos.total) next.addEventListener("click", () => selectRun(ids[pos.index]));
    else next.disabled = true;
    stepper.append(prev, el("span", "step-label", "Run " + pos.index + " of " + pos.total), next);
    eyebrow.append(stepper);
  }
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
  // Persistent one-line verdict (plain language) directly under the title, so
  // the run's result reads in one glance from any chapter — not only Overview.
  // Restated from validated check facts; no interpretation is added here.
  const vsum = verdictSummary(rv);
  if (vsum) {
    const v = el("p", "run-verdict " + vsum.tone);
    v.append(el("span", "run-verdict-key", vsum.headline), document.createTextNode(" " + vsum.detail));
    left.append(v);
  }
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

  // The run's view bar (T1 / §4.2 / §4.4) has two zones on one rail. LEFT: the
  // review itself — Overview · Key moments · Checks, as tabs. RIGHT: one cluster
  // gathering every OTHER way to view this run — the full trace, the raw source,
  // "More analysis" (Opportunities / Ability signature), and Compare — so the
  // tabs read purely as "the review" and the tools sit together in one place.
  if (state.view === "review") state.viewed.add(state.chapter);
  const meta = chapterMeta();
  const nav = el("div", "review-nav");
  // Shared chapter-chip builder, used by both the tabs and the "More analysis"
  // menu, so it is defined before either is built.
  const ochip = (id, label, m, current) => {
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
    return b;
  };
  // LEFT zone: the three primary chapters as tabs. The compare view has no
  // chapter, so the tabs hide there.
  if (state.view !== "compare") {
    const outline = el("div", "outline"); outline.setAttribute("role", "tablist");
    outline.setAttribute("aria-label", "Review chapters");
    for (const [id, label] of CHAPTERS)
      outline.append(ochip(id, label, meta[id], state.view === "review" && state.chapter === id));
    nav.append(outline);
  }
  // RIGHT zone: other views & tools, all together.
  const util = el("div", "review-util");
  // Trace opens the full-trace drawer rather than swapping the main stage.
  const traceChip = el("button", "seg trace-chip", "↗ Trace");
  traceChip.title = "Open the full trace (T)";
  traceChip.addEventListener("click", () => openTrace(null));
  util.append(traceChip);
  // Raw immutable source.
  const srcBtn = el("button", "seg" + (state.view === "source" ? " active" : ""), "Source");
  srcBtn.title = "The immutable source events behind this review";
  srcBtn.addEventListener("click", () => { state.view = "source"; render(); });
  util.append(srcBtn);
  // "More analysis": Opportunities and the Task Ability Signature, kept off the
  // primary tabs but reachable here.
  const more = el("details", "more-analysis");
  if (state.view === "review" && (state.chapter === "opportunities" || state.chapter === "signature")) more.open = true;
  more.append(el("summary", null, "More analysis"));
  const moreList = el("div", "more-analysis-list");
  for (const [id, label] of MORE_ANALYSIS_CHAPTERS)
    moreList.append(ochip(id, label, meta[id], state.view === "review" && state.chapter === id));
  more.append(moreList);
  util.append(more);
  // Compare (§4.16 / item 21): one control gathers every way to put this run
  // beside another — a passing sibling, another reviewer's take, or another
  // version — instead of three differently-named entry points in three places.
  const avail = rv.available_reviews || [];
  const cmpMenu = el("details", "more-analysis compare-menu");
  const cmpSummary = el("summary", (state.view === "sibling" || state.view === "compare") ? "active" : null, "Compare");
  cmpSummary.title = "Put this run beside another — a passing run, a reviewer, or a version";
  cmpMenu.append(cmpSummary);
  const cmpList = el("div", "more-analysis-list");
  // 1. Against a passing sibling — only meaningful for a FAILED run; a passed
  //    run gets a disabled row with an honest reason.
  const sib = el("button", "compare-item" + (state.view === "sibling" ? " current" : ""), "Against a passing run");
  if ((o.status || "").toUpperCase() === "FAILED") {
    sib.title = "Align this run against a passing sibling on the same task (§item 21)";
    sib.addEventListener("click", () => { state.view = "sibling"; render(); });
  } else {
    sib.disabled = true;
    sib.title = "Needs a FAILED run to align against a passing one.";
  }
  cmpList.append(sib);
  // 2. Between two reviewers of this run — only when more than one review exists.
  if (avail.length >= 2) {
    const rev = el("button", "compare-item" + (state.view === "compare" ? " current" : ""), "Between reviewers");
    rev.title = "Side-by-side comparison of two reviews of this run (§4.16) (C)";
    rev.addEventListener("click", () => { state.view = "compare"; render(); });
    cmpList.append(rev);
  }
  // 3. Across versions — a matched task slice; a surface of its own, also on the
  //    app bar, gathered here so "compare" is one idea in one place.
  const ver = el("button", "compare-item", "Across versions →");
  ver.title = "Compare configurations on a matched task slice (V)";
  ver.addEventListener("click", () => { state.view = "versions"; render(); });
  cmpList.append(ver);
  cmpMenu.append(cmpList);
  util.append(cmpMenu);
  nav.append(util);
  main.append(nav);

  if (state.view === "source") renderSource(main);
  else if (state.view === "compare") renderCompare(main);
  else if (state.view === "sibling") renderSiblingDivergence(main);
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
  // AGR-07 UX: reviewer flip — when more than one reviewer has scored this
  // capture, the reader can switch snapshots inline instead of re-running CLI
  // commands. The active reviewer's key is marked in the tab set.
  const avail = rv.available_reviews || [];
  if (avail.length >= 2) {
    const flip = el("span", "shell-chip reviewer-flip");
    const active = rv.reviewer_key;
    for (const key of avail) {
      const label = key === "deterministic" ? "Deterministic"
        : key.startsWith("model:") ? "AI · " + key.slice(6) : key;
      const tab = el("button", "seg flip-tab" + (key === active ? " active" : ""), label);
      tab.title = "Serve this reviewer's snapshot of the run";
      tab.addEventListener("click", async () => {
        if (key === state.reviewerKey || (key === active && state.reviewerKey == null)) return;
        state.reviewerKey = key === "deterministic" && avail.includes(active) ? key : (key === active ? null : key);
        state.momentIdx = 0;
        toast("Serving " + label + " review…");
        state.review = await api(reviewUrl(state.runId, state.reviewerKey));
        render();
      });
      flip.append(tab);
    }
    // These chips switch which reviewer's snapshot is served; comparing two
    // reviews side by side lives in the unified "Compare" control by the tabs.
    wrap.append(flip);
  } else {
    // Honest affordance: the AI review has not been run — name the command.
    const hint = el("span", "shell-chip ai-hint", "AI review: not run");
    hint.title = "Run the model reviewer from the CLI:\n  agr --store <store> review \"" + (rv.reviewer_key ? state.runId : state.runId)
      + "\" --provider anthropic\n(or --provider openai). It spends tokens; every fact it asserts is still recomputed.";
    wrap.append(hint);
  }
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
