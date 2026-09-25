"use strict";

// A one-line, plain-language verdict for the run header. Delegates to
// outcomeNarrative (ui-utils.js) — the single place that reads the backend's
// reconciled rv.outcome.status — so the header can never disagree with
// Overview's "What happened" headline, which reads the same function.

// --- render root -------------------------------------------------------------
// U1: which top-level surfaces are global workspaces (Runs / Patterns /
// Compare versions) rather than a run's investigation shell. Workspaces get
// the full content width and no persistent evidence panel; opening a run
// switches into the investigation shell below.
function isWorkspaceView(view) { return view === "runs" || view === "fleet" || view === "versions"; }

// The top bar's run stepper (‹ 5/27 ›) — empty when there is
// no active queue position (no run loaded, or a run opened outside any
// queue, e.g. a direct link).
function renderTopbarStepper() {
  const host = $("#topbar-stepper"); host.textContent = "";
  const pos = queuePosition();
  if (!pos) return;
  const ids = state.queue.run_ids;
  const stepper = el("span", "run-stepper");
  const prev = el("button", "step-arrow"); prev.append(icon("chevron-left"));
  prev.title = "Previous run in this queue";
  if (pos.index > 1) prev.addEventListener("click", () => selectRun(ids[pos.index - 2]));
  else prev.disabled = true;
  const next = el("button", "step-arrow"); next.append(icon("chevron-right"));
  next.title = "Next run in this queue";
  if (pos.index < pos.total) next.addEventListener("click", () => selectRun(ids[pos.index]));
  else next.disabled = true;
  const label = el("span", "step-label");
  label.append(el("span", "step-label-word", "Run "), document.createTextNode(pos.index + " of " + pos.total));
  stepper.append(prev, label, next);
  host.append(stepper);
}

function render() {
  const main = $("#main"); main.textContent = "";
  // A review view with no SELECTED run (e.g. a stray fallback) has nothing to
  // investigate, so it resolves to the Runs workspace before the layout mode
  // below is decided — never a bare "review" mode with an empty three-pane
  // shell. Deliberately keyed on runId, not on `state.review` being loaded
  // yet: selectRun (run.js) now renders immediately, before its fetch
  // resolves, so state.runId is set but state.review is still the old value
  // (or null, on a run's first-ever selection) for that one transient
  // render. Redirecting on `!state.review` would flip state.view to "runs"
  // right there and never flip it back once the fetch lands — the loaded
  // run would render as the Runs workspace instead of its own review. The
  // "!state.review" case below (a run selected but not yet loaded) is exactly
  // that transient window, and renders its own "Loading…" placeholder.
  if (state.view === "review" && !state.runId) state.view = "runs";
  const workspace = isWorkspaceView(state.view);
  document.body.classList.toggle("workspace-mode", workspace);
  main.classList.toggle("wide", workspace);
  // N1: the app bar's primary nav (Runs / Patterns / Compare versions) marks
  // which workspace is current — reviewing a specific run leaves all three
  // unmarked, since none of them is "current" while investigating a run.
  $("#runs-button").classList.toggle("current", state.view === "runs");
  $("#fleet-button").classList.toggle("current", state.view === "fleet");
  $("#versions-button").classList.toggle("current", state.view === "versions");
  // The investigation sidebar's filter chips/run list live outside #main (so
  // they survive #main being wiped below) and are otherwise refreshed lazily
  // by loadInbox() — reconciling them here on every render, regardless of how
  // this view was reached, is what keeps a workspace's OWN filter chips
  // (rendered by runs.js, inside #main) from ever coexisting in the DOM with
  // the sidebar's — two same-class controls open to the same click would be
  // ambiguous for both a human and a test's selector.
  if (workspace) {
    $("#queue-controls").textContent = ""; $("#run-list").textContent = "";
    $("#crumb-task").textContent = state.view === "runs" ? "Runs"
      : state.view === "fleet" ? "Patterns" : "Compare versions";
    $("#topbar-stepper").textContent = "";
  }
  else { renderQueueControls(); renderRunList(); }
  // The runs table, version comparison, and fleet view are surfaces, not run
  // views: each renders with no run selected, so all three are dispatched
  // before the "pick a run" guard.
  if (state.view === "runs") { renderRunsSurface(main); syncUrl(); return; }
  if (state.view === "versions") { renderVersionsSurface(main); syncUrl(); return; }
  if (state.view === "fleet") { renderFleetSurface(main); syncUrl(); return; }
  if (!state.review) {
    main.append(el("div", "empty", state.runId && state.loading ? "Loading…" : "Select a run to begin."));
    syncUrl(); return;
  }
  const rv = state.review, run = rv.run || {};
  $("#crumb-task").textContent = run.task_id || state.runId;

  // header — the §4.2 shared run-review shell, compacted to three lines
  // (target: ≤140px tall at 1440px, ≤220px at 390px): a title line
  // (task name + inline outcome badge), one meta line (model · harness ·
  // steps · a short id with a copy icon — the full id lives in its tooltip,
  // never wrapped mono across several lines), then the plain-language
  // verdict. The review-mode / workflow / capture-completeness strip
  // (.shell-meta) renders directly beneath as the header's controls row.
  // The run stepper lives in the top bar (breadcrumb left, stepper right),
  // not inline here — queue position doubles as a stepper either way: walk
  // the sweep run-by-run without going back to the queue list. (Bottom bar's
  // "Next unhandled" skips handled runs; this steps every run in order.)
  renderTopbarStepper();
  const header = el("div", "run-header");
  const left = el("div");
  const titleRow = el("div", "run-title-row");
  titleRow.append(el("h1", null, run.task_id || state.runId));
  const o = rv.outcome || {}, sc = statusClass(o.status);
  const hs = el("span", "header-status " + sc);
  hs.append(el("span", "status-dot " + sc), el("span", null, (o.status||"?") + " · " + (o.passed??"?") + "/" + (o.total??"?") + " checks"));
  titleRow.append(hs);
  left.append(titleRow);
  const steps = (state.forensic && state.forensic.steps) ? state.forensic.steps.length : null;
  const subBits = [run.model, run.harness_version ? "harness " + run.harness_version : null,
    steps != null ? steps + " steps" : null, fmtDuration(durationOf(run)),
    fmtCost(costOf(run))].filter(Boolean).join(" · ");
  const sub = el("div", "run-subtitle");
  if (subBits) sub.append(document.createTextNode(subBits + " · "));
  const shortId = shortRunId(state.runId, 10) || state.runId;
  const idSpan = el("span", "mono run-id-short", shortId);
  idSpan.title = state.runId;
  sub.append(idSpan);
  sub.append(copyButton(state.runId, "Copy full run id"));
  left.append(sub);
  // Persistent one-line verdict (plain language) directly under the title, so
  // the run's result reads in one glance from any chapter — not only Overview.
  // Restated from validated check facts; no interpretation is added here.
  const vsum = outcomeNarrative(rv);
  if (vsum) {
    const v = el("p", "run-verdict " + vsum.tone);
    v.append(el("span", "run-verdict-key", vsum.headline), document.createTextNode(" " + vsum.detail));
    left.append(v);
  }
  header.append(left);
  main.append(header);
  main.append(renderShellMeta(rv));

  if (rv.watermark) { const w = el("div", "watermark callout callout-warn"); w.append(icon("alert-triangle", "watermark-icon"), document.createTextNode(" " + rv.watermark)); main.append(w); }
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
    const markSpan = el("span", "ochip-mark");
    if (!m.available) markSpan.textContent = "–";
    else if (m.corrected) markSpan.textContent = "~";
    else if (state.viewed.has(id) && !current) markSpan.append(icon("check"));
    else markSpan.textContent = "•";
    b.append(markSpan, el("span", "ochip-label", label));
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
  const traceChip = el("button", "seg trace-chip");
  traceChip.append(icon("external-link"), document.createTextNode(" Trace"));
  traceChip.title = "Open the full trace (T)";
  traceChip.addEventListener("click", () => openTrace(null));
  util.append(traceChip);
  // Raw immutable source.
  const srcBtn = el("button", "seg" + (state.view === "source" ? " active" : ""), "Source");
  srcBtn.title = "The immutable source events behind this review";
  srcBtn.addEventListener("click", () => { state.view = "source"; render(); });
  util.append(srcBtn);
  // "More analysis" (Opportunities / Ability signature) and "Compare" (against
  // a passing sibling, another reviewer, or another version) used to be two
  // separate dropdown triggers here — at 390px both text buttons together
  // wrapped onto their own line, and a reader had to know which of two
  // similar-looking buttons held which item. One "⋯" menu now gathers both
  // groups; every item below keeps its own class and click handler, just
  // under one trigger and one list. The trigger is marked active while one of
  // its destinations is showing; the list itself stays closed so it never
  // covers the page it just opened.
  const avail = rv.available_reviews || [];
  const toolsActive = (state.view === "review" && (state.chapter === "opportunities" || state.chapter === "signature"))
    || state.view === "sibling" || state.view === "compare";
  const tools = el("details", "more-analysis tools-menu");
  const toolsSummary = el("summary", toolsActive ? "active" : null);
  toolsSummary.append(icon("more-horizontal"), el("span", "sr-only", "More analysis and compare"));
  toolsSummary.title = "More analysis & compare — Opportunities, Ability signature, and putting this run beside another";
  toolsSummary.setAttribute("aria-label", "More analysis and compare");
  tools.append(toolsSummary);
  const toolsList = el("div", "more-analysis-list");
  toolsList.append(el("div", "more-analysis-section-label", "More analysis"));
  for (const [id, label] of MORE_ANALYSIS_CHAPTERS)
    toolsList.append(ochip(id, label, meta[id], state.view === "review" && state.chapter === id));
  toolsList.append(el("div", "more-analysis-divider"));
  toolsList.append(el("div", "more-analysis-section-label", "Compare"));
  // 1. Against a passing sibling — only meaningful for a FAILED run; a passed
  //    run gets a disabled row with an honest reason.
  const sib = el("button", "compare-item" + (state.view === "sibling" ? " current" : ""), "Against a passing run");
  if ((o.status || "").toUpperCase() === "FAILED") {
    sib.title = "Align this run against a passing sibling on the same task";
    sib.addEventListener("click", () => { state.view = "sibling"; render(); });
  } else {
    sib.disabled = true;
    sib.title = "Needs a FAILED run to align against a passing one.";
  }
  toolsList.append(sib);
  // 2. Between two reviewers of this run — only when more than one review exists.
  if (avail.length >= 2) {
    const rev = el("button", "compare-item" + (state.view === "compare" ? " current" : ""), "Between reviewers");
    rev.title = "Side-by-side comparison of two reviews of this run (C)";
    rev.addEventListener("click", () => { state.view = "compare"; render(); });
    toolsList.append(rev);
  }
  // 3. Across versions — a matched task slice; a surface of its own, also in
  //    the sidebar nav, gathered here so "compare" is one idea in one place.
  const ver = el("button", "compare-item", "Across versions →");
  ver.title = "Compare configurations on a matched task slice (V)";
  ver.addEventListener("click", () => { state.view = "versions"; render(); });
  toolsList.append(ver);
  tools.append(toolsList);
  util.append(tools);
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
  modeChip.title = mv.tip;
  // GR-1: the review's single status, stated up front. Only moments_found and
  // no_decisive_moment are successful outcomes; the rest are explicit, and a
  // failed/unconfigured reviewer is never shown as "no decisive moment".
  // A successful status (e.g. "Moments found" alongside "AI-enriched
  // review") is redundant with the mode chip it always accompanies, so it
  // folds into that chip's tooltip instead of its own pill; a genuine
  // warning (a failed/unconfigured reviewer) still gets its own visible chip
  // — that is signal, not redundancy.
  wrap.append(modeChip);
  if (rv.review_status) {
    const sv = vocab("review_status", rv.review_status);
    if (rv.review_status_success) {
      modeChip.title = mv.tip + " · " + sv.label + (sv.tip ? ": " + sv.tip : "");
    } else {
      const statusChip = el("span", "shell-chip warn", sv.label);
      statusChip.title = sv.tip;
      wrap.append(statusChip);
    }
  }
  // GR-4: a pre-computed demo review names its reviewer model and date, so a
  // reader sees the provenance of an offline review (and it is not mistaken for
  // a live model call, which the demo never makes).
  if (rv.review_model) {
    const when = rv.reviewed_at ? " · " + rv.reviewed_at.slice(0, 10) : "";
    const provenance = el("span", "shell-chip mode-det", "AI " + rv.review_model + when);
    provenance.title = "Reviewer model and date of this pre-computed review";
    wrap.append(provenance);
  }
  // GR-4: a demo contract clears the watermark under a demo-specific status, so
  // it must never read as a human confirmation. Say so explicitly.
  if (rv.contract_demo_override) {
    const chip = el("span", "shell-chip warn", "Demo override — contract not human-confirmed");
    chip.title = "This demo cleared the contract watermark without a human confirmation";
    wrap.append(chip);
  }
  // AGR-07 UX: reviewer flip — when more than one reviewer has scored this
  // capture, the reader can switch snapshots inline instead of re-running CLI
  // commands. The active reviewer's key is marked in the tab set.
  const avail = rv.available_reviews || [];
  if (avail.length >= 2) {
    const flip = el("span", "shell-chip reviewer-flip");
    const active = rv.reviewer_key;
    for (const key of avail) {
      // M2: a full model id ("accounts/fireworks/models/kimi-k3")
      // wraps a segmented tab onto two lines on a narrow screen — the last
      // path segment is enough to tell reviewers apart; the full id is
      // still one hover (or the header's provenance chip) away.
      const fullModel = key.startsWith("model:") ? key.slice(6) : null;
      const label = key === "deterministic" ? "Deterministic"
        : fullModel ? "AI · " + shortModelName(fullModel) : key;
      const tab = el("button", "seg flip-tab" + (key === active ? " active" : ""), label);
      tab.title = fullModel ? "Serve this reviewer's snapshot of the run (" + fullModel + ")"
        : "Serve this reviewer's snapshot of the run";
      tab.addEventListener("click", async () => {
        if (key === state.reviewerKey || (key === active && state.reviewerKey == null)) return;
        state.reviewerKey = key === "deterministic" && avail.includes(active) ? key : (key === active ? null : key);
        state.momentIdx = 0;
        toast("Serving " + label + " review…");
        // F4: shares selectRun's staleness guard — a reviewer switch races
        // the same state.review a run switch does, so it uses the same
        // token and disables writes the same way while its fetch is in flight.
        const token = ++state.loadToken, runId = state.runId, reviewerKey = state.reviewerKey;
        state.loading = true;
        render();
        let review;
        try {
          review = await api(reviewUrl(runId, reviewerKey));
        } catch (e) {
          if (token === state.loadToken) { state.loading = false; render(); }
          throw e;
        }
        if (token !== state.loadToken) return;  // superseded by a newer selection
        state.review = review;
        state.loading = false;
        render();
      });
      flip.append(tab);
    }
    // These chips switch which reviewer's snapshot is served; comparing two
    // reviews side by side lives in the unified "Compare" control by the tabs.
    wrap.append(flip);
  } else {
    // GR-1: no model snapshot is served. "No model review" is the clear,
    // first-class label when nothing is configured; a failed or early-stopped
    // attempt is named by the limits panel and the status chip above, and the
    // CLI hint still says how to retry. Either way the deterministic review
    // shows — an absence of a model call is never presented as abstention.
    const none = rv.review_status === "not_configured";
    const hint = el("span", "shell-chip ai-hint", none ? "No model review" : "AI review: not served");
    hint.title = (none ? "No model reviewer has scored this run."
      : "The model review is not being served — see the review limits.")
      + "\nRun the model reviewer from the CLI:\n  agr --store <store> review \"" + state.runId
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
  const card = el("div", "notice not-reviewable callout callout-warn");
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
