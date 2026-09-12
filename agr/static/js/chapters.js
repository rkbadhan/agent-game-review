"use strict";

// --- review chapters (T1) -----------------------------------------------------
//   Primary in-run navigation is Overview · Key moments · Checks (plus a Trace
//   nav item that opens the full-trace drawer rather than swapping the main
//   stage — see render.js). Opportunities and the Task Ability Signature stay
//   reachable but move under a secondary "More analysis" disclosure so they no
//   longer compete with the primary three. Strongest behaviour is folded into
//   Key moments as a teaser + jump-to-moment, and the Eval Lesson is attached to
//   its recommending moment card instead of being a chapter of its own.
const CHAPTERS = [
  ["overview", "Overview"], ["moments", "Key moments"], ["checks", "Checks"],
];
// Secondary chapters, reachable from the "More analysis" disclosure — same
// setChapter/chapterMeta machinery, just not in the primary tab row.
const MORE_ANALYSIS_CHAPTERS = [["opportunities", "Opportunities"], ["signature", "Ability signature"]];
const ALL_CHAPTER_IDS = CHAPTERS.map(c => c[0]).concat(MORE_ANALYSIS_CHAPTERS.map(c => c[0]));
// Old chapter ids from before the T1 navigation simplification. Kept so a
// previously shared link still opens the content it pointed at.
const CHAPTER_ALIASES = { outcome: "overview", audit: "checks", strongest: "moments", lesson: "moments" };
function normalizeChapter(id) { return CHAPTER_ALIASES[id] || id; }
function strongestMoments() { return currentMoments().filter(m => m.polarity === "positive"); }
// The single moment Overview leads with: the most decisive negative finding
// when one exists, else the strongest positive one. Null when the review has
// no moments at all (deterministic-only with nothing flagged).
function mainFinding() {
  const moments = currentMoments();
  return moments.find(m => m.polarity !== "positive") || strongestMoments()[0] || null;
}
function hasCorrection() { return ((state.review && state.review.feedback) || []).some(f => f.kind === "quick_relabel"); }
function lessonMoment() {
  // A lesson is supported only when a reviewed (model-enriched) moment recommends
  // one; the deterministic core recommends none, so no moment card shows the
  // inline Eval lesson section in that case (§4.11).
  return currentMoments().find(x => x.eval_lesson_recommended && x.enrichment_source) || null;
}
function persistedLesson(m) {
  const id = "lesson_" + (m && m.moment_id);
  return ((state.review && state.review.lessons) || []).find(l => l.lesson_id === id) || null;
}
function chapterMeta() {
  const rv = state.review, moments = currentMoments();
  return {
    overview: { available: true },
    moments: { available: moments.length > 0, corrected: hasCorrection(),
      reason: rv.review_mode === "not_reviewable" ? "The captured evidence is insufficient for a trustworthy review."
        : rv.review_mode === "model_enriched" ? "No decisive moment was selected." : "No candidate moment was flagged deterministically." },
    checks: { available: true },
    opportunities: { available: (rv.opportunity_rows || []).length > 0,
      reason: "This run reached no opportunity window this review could measure." },
    signature: { available: (rv.signature || []).length > 0,
      reason: "No ability-signature rows were derived for this run." },
  };
}
function setChapter(id) { state.view = "review"; state.chapter = id; state.viewed.add(id);
  track("review_chapter_viewed", { run_id: state.runId, chapter: id }); render();
  $("#main").scrollTo({ top: 0, behavior: "smooth" }); }
function moveChapter(dir) {
  const ids = CHAPTERS.map(c => c[0]); let i = ids.indexOf(state.chapter);
  // From a "More analysis" chapter (not in this primary list), both directions
  // used to land on index 0 regardless of `dir`. Treat "not found" as sitting
  // just before the list (so `]`/next enters at the first chapter) or just
  // after it (so `[`/prev enters at the last one), matching the key pressed.
  if (i < 0) i = dir > 0 ? -1 : ids.length;
  i = Math.max(0, Math.min(ids.length - 1, i + dir)); setChapter(ids[i]);
}
function renderChapter(main) {
  state.viewed.add(state.chapter);
  ({ overview: renderOverviewChapter, opportunities: renderOpportunitiesChapter,
     moments: renderMomentsChapter, checks: renderChecksChapter,
     signature: renderSignatureChapter }[state.chapter] || renderMomentsChapter)(main);
}
function renderChapterFooter(main) {
  const ids = CHAPTERS.map(c => c[0]), i = ids.indexOf(state.chapter);
  const foot = el("div", "chapter-foot");
  // "More analysis" chapters (Opportunities, Ability signature) sit outside the
  // primary Overview → Key moments → Checks sequence, so they get a way back
  // instead of a broken position in that cycle.
  if (i < 0) {
    const back = el("button", "button subtle", "‹ Back to Overview");
    back.addEventListener("click", () => setChapter("overview"));
    foot.append(back);
    foot.append(el("span", "chapter-progress", "More analysis"));
    main.append(foot);
    return;
  }
  const prev = el("button", "button subtle" + (i <= 0 ? " ghost" : ""), "‹ Previous chapter");
  if (i > 0) prev.addEventListener("click", () => moveChapter(-1)); else prev.disabled = true;
  foot.append(prev);
  foot.append(el("span", "chapter-progress", "Chapter " + (i + 1) + " of " + ids.length + " · " + CHAPTERS[i][1]));
  const next = el("button", "button" + (i >= ids.length - 1 ? " ghost" : ""), "Next chapter ›");
  if (i < ids.length - 1) next.addEventListener("click", () => moveChapter(1)); else next.disabled = true;
  foot.append(next);
  main.append(foot);
}
function chapterClose(main) { renderChapterFooter(main); renderBottomBar(main); }
function chapterEmpty(main, title, reason) {
  const card = el("div", "card card-pad chapter-empty");
  card.append(el("p", "eyebrow", "Not available"));
  card.append(el("h2", "empty-title", title));
  card.append(el("p", "chapter-lede", reason));
  main.append(card);
}
function durationOf(run) {
  if (!run.started_at || !run.finished_at) return null;
  const a = Date.parse(run.started_at), b = Date.parse(run.finished_at);
  return isNaN(a) || isNaN(b) ? null : Math.max(0, (b - a) / 1000);
}

// --- guided workspace --------------------------------------------------------
const MOMENT_ICONS = {};
function momentTypeLabel(m) { return m.taxonomy_verdict ? m.taxonomy_verdict : (m.polarity === "positive" ? "Strength" : "Concern"); }
function momentTagClass(m) { return m.polarity === "positive" ? "strength" : "concern"; }
function fmtVal(v) { return v == null ? "—" : (Array.isArray(v) ? v.join(", ") : String(v)); }

// Map every event id to the source step that emitted it, and stamp each moment
// with its timeline sequence + step index. Shared by the timeline and evidence.
function indexEvents(f, moments) {
  const seqOfEvent = {}, stepOfEvent = {};
  f.steps.forEach((s, i) => (s.event_ids || []).forEach(ev => { seqOfEvent[ev] = s.sequence; stepOfEvent[ev] = i; }));
  const maxSeq = Math.max(1, ...f.steps.map(s => s.sequence || 0));
  (moments || []).forEach(m => { const a = (m.anchor_event_ids || [])[0];
    m._seq = a != null ? seqOfEvent[a] : null; m._stepIdx = a != null ? stepOfEvent[a] : null; });
  return { seqOfEvent, stepOfEvent, maxSeq };
}

// Chapter 3 — Key moments (§4.7–4.9): unified timeline + current moment card.
function renderMomentsChapter(main) {
  const rv = state.review, f = state.forensic, moments = currentMoments();
  if (!moments.length) {
    chapterEmpty(main, "No key moments", chapterMeta().moments.reason);
    return chapterClose(main);
  }
  // Strongest behaviour (T1 / formerly its own chapter, §4.10): a compact
  // teaser above the timeline so the one behaviour worth preserving stays
  // findable without a dedicated tab — "View" just selects that moment in the
  // sequence below, where its full Situation/Action/Consequence/Impact detail
  // already renders like any other moment card.
  const positives = strongestMoments();
  if (positives.length && positives[0] !== currentMoment()) {
    const idx = currentMoments().indexOf(positives[0]);
    const teaser = el("div", "strongest-teaser");
    teaser.append(el("span", "tag strength", "Strongest behaviour"));
    teaser.append(el("span", "strongest-teaser-text", positives[0].summary));
    const view = el("button", "button subtle", "View");
    view.addEventListener("click", () => { if (idx >= 0) selectMoment(idx); });
    teaser.append(view);
    main.append(teaser);
  }

  const { seqOfEvent, maxSeq } = indexEvents(f, moments);
  const card = el("div", "card");
  const tl = el("div", "timeline-wrap");
  const sh = el("div", "section-heading");
  const shl = el("div"); shl.append(el("h3", null, "Review timeline"));
  sh.append(shl); sh.append(el("p", null, "One event axis · " + f.steps.length + " source steps"));
  tl.append(sh);
  tl.append(renderTimeline(rv, moments, seqOfEvent, maxSeq));
  card.append(tl);
  main.append(card);

  state.momentIdx = Math.max(0, Math.min(state.momentIdx, moments.length - 1));
  main.append(renderMomentCard(rv, f, currentMoment()));
  chapterClose(main);
}

// Overview chapter (T1 / formerly "Outcome", §4.5): a single evidence-backed
// finding headline, kept separate from the task outcome and from whether any
// recovery happened, plus what materially limits the review. The atomic check
// table, contract mapping, and requirement warnings live in Checks; a full
// walkthrough (action, consequence, likely impact, better action, evidence)
// lives on the moment card itself in Key moments.
//
// U5: previously this chapter opened with a full-sentence, always-generic
// "What happened" verdict ("Passed — all 6 checks evidenced.") as its biggest
// element, with the actual decisive finding relegated to a second card below
// it — so the one specific, evidence-backed fact about the run competed with,
// rather than led, the page. The two cards are now one: the finding headline
// (or, when none was selected, the outcome narrative itself) is the single
// most prominent thing on the page; task outcome and review-mode context sit
// right beneath it, stated once, not restated in a second card.
function renderOverviewChapter(main) {
  const rv = state.review, f = state.forensic, o = rv.outcome || {};
  // F3: reads the SAME reconciled narrative as the header verdict
  // (outcomeNarrative, ui-utils.js) instead of re-deriving one from raw
  // check.status — that duplication was how a superseded/stale check or
  // unknown requirement coverage could read "All checks passed." here while
  // the header (or the outcome pill right below) correctly said otherwise.
  const narrative = outcomeNarrative(rv) || {
    tone: "warn", headline: "Unverified —",
    detail: "no atomic check evidence was captured, so nothing here should be read as a pass.",
  };
  const mf = mainFinding();

  const card = el("div", "card card-pad finding-card");
  if (mf) {
    // U4/U5: the single most prominent element on the page (.main-finding-title)
    // — a reader's eye should land on the specific, evidence-backed finding,
    // not the task identifier in the header or a generic pass/fail sentence.
    // A headline reads at a sentence/clause boundary (leadFinding, shared with
    // the Runs table row) rather than the full statement — an aggregate
    // finding naming every failing check can run to several lines, which
    // defeats "headline"; the untruncated statement is always one click away
    // via "Open in Key moments →" below.
    const headline = el("h2", "main-finding-title " + (mf.polarity === "positive" ? "pass" : "fail"),
      leadFinding(mf.summary, 220));
    if (leadFinding(mf.summary, 220) !== mf.summary) headline.title = mf.summary;
    card.append(headline);
    if (mf.polarity === "positive")
      card.append(el("p", "finding-sub", "Strongest behaviour observed in this run."));
    // One important content rule (spec): keep tool failure, recovery, and the
    // final task outcome separate — a Recovery line only ever states what the
    // evidence for THIS moment supports, never the run's eventual result.
    const rec = recoveryText(mf);
    if (rec) {
      const r = el("p", "finding-line finding-recovery " + rec.cls);
      r.append(el("strong", null, "Recovery: "), document.createTextNode(rec.text));
      card.append(r);
    }
  } else {
    const tone = narrative.tone === "pass" ? "pass" : narrative.tone === "fail" ? "fail" : "warn";
    card.append(el("h2", "main-finding-title " + tone, narrative.headline.replace(/\s*—$/, "")));
    card.append(el("p", "finding-sub", rv.review_mode === "not_reviewable"
      ? "No finding is available — the captured evidence is insufficient for a trustworthy review."
      : "No decisive finding was selected for this run."));
  }
  // Task outcome, stated once here (never repeated as its own card below it) —
  // the requirement tally folded inline rather than a competing colour block.
  const outLine = el("p", "finding-line finding-outcome");
  outLine.append(el("strong", null, "Task outcome: "));
  outLine.append(el("span", "run-verdict-key " + narrative.tone, narrative.headline.replace(/\s*—$/, "")));
  outLine.append(document.createTextNode(" (" + (o.passed ?? "?") + "/" + (o.total ?? "?") + " checks) — " + narrative.detail));
  card.append(outLine);

  const vm = vocab("review_mode", rv.review_mode);
  const modeNote = el("p", "finding-line finding-mode" + (rv.review_mode === "not_reviewable" ? " not-reviewable" : ""));
  if (rv.review_mode === "not_reviewable") {
    const missing = rv.missing_capabilities || [];
    modeNote.append(vm.label + " — the captured evidence is insufficient for a trustworthy review"
      + (missing.length ? ". Missing capabilities: " + missing.join(", ") + "."
        : "; affected detectors are marked Not evaluated."));
  } else {
    modeNote.append(vm.label + (rv.review_mode === "deterministic_only"
      ? " — situation, action, and consequence are restated from validated facts. Likely impact and better action are added only by the model reviewer."
      : " — deterministic baseline plus model-assisted interpretation."));
  }
  card.append(modeNote);

  if (mf) {
    // U4/U5: "how to inspect its evidence" from the initial view — one click,
    // and an obvious action rather than a second, easy-to-miss button.
    const actions = el("div", "chapter-foot finding-actions");
    const idx = currentMoments().indexOf(mf);
    const viewEvidence = el("button", "button primary", evidenceActionLabel(mf));
    viewEvidence.addEventListener("click", () => {
      if (idx >= 0) state.momentIdx = idx;
      state.view = "review"; state.chapter = "moments"; state.viewed.add("moments");
      render();
      focusEvidence();
    });
    actions.append(viewEvidence);
    const open = el("button", "button subtle", "Open in Key moments →");
    open.addEventListener("click", () => { if (idx >= 0) state.momentIdx = idx; setChapter("moments"); });
    actions.append(open);
    card.append(actions);
  }
  main.append(card);

  main.append(renderExecutionQuality(rv, f));

  main.append(renderFinalState(rv, f));

  // What limits the review (§4.5): capture completeness + disabled detectors.
  const lsec = el("div", "card card-pad");
  lsec.append(el("p", "eyebrow", "What limits the review"));
  const limits = el("div");
  const cap = rv.capture || {}, comp = cap.capture_completeness;
  if (comp && comp !== "complete") {
    const r = el("div", "limit-item"); r.append(el("span", "chip", comp));
    r.append(document.createTextNode("Capture is not complete — some evidence types may be partial or unavailable."));
    limits.append(r);
  }
  const disabled = (rv.detector_results || []).filter(d => d.evaluated === false);
  for (const d of disabled) {
    const r = el("div", "limit-item");
    if (d.placeholder) {
      // AGR-05: a registered placeholder is distinct from a capability-gated
      // skip — it has evaluated nothing and could not have, either.
      r.append(el("span", "chip", "not implemented"));
      r.append(document.createTextNode(d.detector + " — registered placeholder, no evidence family implemented yet"));
    } else {
      r.append(el("span", "chip", "not evaluated"));
      r.append(document.createTextNode(d.detector + " — missing capability: " + (d.unmet_capabilities || []).join(", ")));
    }
    limits.append(r);
  }
  // AGR-06: explicit review states — a model failure is never rendered as
  // "no decisive moment", and a valid empty review is named as such.
  if ((rv.review_errors || []).length) {
    for (const e of rv.review_errors) {
      const r = el("div", "limit-item"); r.append(el("span", "chip", "model review failed"));
      r.append(document.createTextNode((e.reviewer_key || "model") + " — " + (e.error_type || "error") +
        "; the deterministic baseline is shown. " + (e.message || "")));
      limits.append(r);
    }
  } else if (rv.review_status === "empty") {
    const r = el("div", "limit-item"); r.append(el("span", "chip", "review complete"));
    r.append(document.createTextNode("The model review returned no moments for this run."));
    limits.append(r);
  } else if (rv.review_status === "no_selection") {
    const r = el("div", "limit-item"); r.append(el("span", "chip", "no grounded moments"));
    r.append(document.createTextNode("Moments were proposed but none passed evidence validation; none are shown."));
    limits.append(r);
  }
  if (rv.watermark) {
    const r = el("div", "limit-item"); r.append(el("span", "chip", "provisional"));
    r.append(document.createTextNode(rv.watermark)); limits.append(r);
  }
  if (!limits.childNodes.length)
    limits.append(el("p", "chapter-lede", "Capture is complete, all detectors ran, and the contract is confirmed. Nothing limits this review."));
  lsec.append(limits); main.append(lsec);

  chapterClose(main);
}

function renderExecutionQuality(rv, forensic) {
  const eq = rv.execution_quality || {}, efficiency = eq.efficiency || {};
  const card = el("div", "card card-pad execution-quality");
  card.append(el("p", "eyebrow", "Execution quality · independent of outcome"));
  const labels = {
    context_bloat: "Token efficiency", latency: "Latency", redundant_work: "Redundant work",
  };
  const table = el("table", "sig-table");
  table.append(rowEls("tr", ["Dimension", "Status", "Evidence"], "th"));
  for (const key of Object.keys(labels)) {
    const metric = efficiency[key] || {
      evaluated: false, status: "unevaluated", unmet_capabilities: ["not captured"],
    };
    const tr = el("tr"); tr.append(td(labels[key]));
    const status = metric.status || (metric.evaluated ? "healthy" : "unevaluated");
    const statusCell = el("td");
    statusCell.append(el("span", "chip eq-" + status,
      status === "issue" ? "⚠ issue" : status === "healthy" ? "✓ healthy" : "— not evaluated"));
    tr.append(statusCell);
    const evidence = el("td");
    if (!metric.evaluated) {
      evidence.append(document.createTextNode("Missing: " + (metric.unmet_capabilities || []).join(", ")));
    } else if (key === "context_bloat" && metric.violations) {
      evidence.append(document.createTextNode(metric.violations + " generation(s) · peak "
        + fmtCompact(metric.peak_input_tokens) + " input tokens"));
    } else if (key === "latency" && metric.violations) {
      evidence.append(document.createTextNode(metric.violations + " slow generation(s) · worst "
        + fmtDuration(metric.max_generation_ms / 1000)));
    } else if (key === "redundant_work" && metric.violations) {
      evidence.append(document.createTextNode(metric.violations + " repeated action(s) · maximum separation "
        + metric.max_calls_between + " calls"));
    } else {
      evidence.append(document.createTextNode("Evaluated; no violation detected."));
    }
    tr.append(evidence); table.append(tr);
  }
  card.append(table);
  const findings = eq.findings || [];
  if (findings.length) {
    const titles = {
      context_token_bloat: "Context bloat",
      excess_latency: "Slow generations",
      repeated_action_no_new_info: "Redundant work",
    };
    const list = el("div", "eq-findings");
    for (const finding of findings) {
      const facts = finding.structured_facts || [];
      const item = el("details", "eq-finding");
      const summary = el("summary");
      summary.append(el("strong", null, titles[finding.detector] || finding.detector));
      summary.append(document.createTextNode(" · " + facts.length + " event-level violation(s)"));
      item.append(summary);
      for (const fact of facts) {
        let text = fact.event_id || (fact.events || []).join(" → ");
        if (fact.type === "token_usage")
          text += " · " + fmtCompact(fact.input_tokens) + " input tokens · " + fact.excess_ratio + "× threshold";
        else if (fact.type === "generation_latency")
          text += " · " + fmtDuration(fact.wall_ms / 1000) + " · threshold " + fmtDuration(fact.threshold_ms / 1000);
        else if (fact.type === "repetition")
          text += " · " + fact.calls_between + " call(s) between · equivalent captured outputs";
        item.append(el("div", "eq-fact mono", text));
      }
      const inspect = el("button", "button subtle", "Inspect evidence");
      inspect.addEventListener("click", () => {
        const eventId = (finding.anchor_event_ids || [])[0];
        const step = (forensic.steps || []).find(s => (s.event_ids || []).includes(eventId));
        openTrace(step ? step.step_id : null);
      });
      item.append(inspect); list.append(item);
    }
    card.append(list);
  }
  return card;
}

// Final environment & artifacts (§4.5): the closing observed state of the run.
// Restated, never inferred — a declared artifact the capture never observed stays
// a visible row, and the capability levels that govern artifact/process evidence
// are shown so an empty section reads as "not captured", not "nothing was there".
function renderFinalState(rv, f) {
  const fs = rv.final_state || {}, artifacts = fs.artifacts || [], av = fs.availability || {};
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Final environment & artifacts"));
  card.append(el("p", "chapter-lede", "What the run left behind, as the capture observed it at the end — before any judgment about whether it was right."));

  if (artifacts.length) {
    const t = el("table", "final-table");
    t.append(rowEls("tr", ["Artifact", "Final state", "Observed value", "Evidence"], "th"));
    for (const a of artifacts) {
      const tr = el("tr");
      const pathTd = el("td", "final-path");
      pathTd.append(document.createTextNode(a.path));
      if (!a.declared) { const c = el("span", "chip", "undeclared");
        c.title = "Observed in the run but not declared by the task."; pathTd.append(c); }
      tr.append(pathTd);

      const observed = a.state === "observed";
      const stTd = el("td");
      const badge = el("span", "stat-badge " + (observed ? "measured" : a.declared ? "neg" : "none"),
        observed ? "Observed" : "Never observed");
      badge.title = observed
        ? "The capture recorded this artifact's state during the run."
        : "Declared by the task; no artifact observation for it appears in the capture.";
      stTd.append(badge);
      if (a.observed_after_submission)
        stTd.append(el("div", "final-note", "observed after submission"));
      tr.append(stTd);

      const vTd = el("td");
      if (a.content != null) vTd.append(el("span", "final-value", a.content + (a.content_truncated ? " …" : "")));
      else if (a.last_tool_event_id) vTd.append(el("span", "sig-reason", "no value captured — a tool call named this path"));
      else vTd.append(el("span", "sig-reason", "no value captured"));
      tr.append(vTd);

      tr.append(evidenceTd([a.observed_at_event_id || a.last_tool_event_id], f));
      t.append(tr);
    }
    card.append(t);
  } else {
    card.append(el("p", "chapter-lede", "This task declared no artifacts, and none were observed."));
  }

  const block = el("div", "final-block");
  block.append(el("p", "eyebrow", "Environment at the end of the run"));
  const rows = fs.environment || [];
  for (const r of rows) {
    block.append(kvLine((r.event_type || "observation").replace(/_/g, " "),
      r.detail || "(no captured detail)"));
  }
  if (fs.last_exit) {
    const e = fs.last_exit;
    block.append(kvLine("Last process exit",
      (e.tool ? e.tool + " " : "") + "exited " + e.exit_code));
  }
  if (!rows.length && !fs.last_exit) {
    const ps = av.process_state || {};
    block.append(el("p", "chapter-lede", ps.state === "unavailable"
      ? "No environment or process state was captured for this run — process-state capture is unavailable, so this section is empty by capture, not by observation."
      : "The capture recorded no closing environment observation for this run."));
  }
  const levels = [["filesystem", "Artifact capture"], ["process_state", "Process state"]]
    .map(([key, label]) => { const c = av[key] || {}; return label + ": " + (c.level || "unknown"); });
  block.append(el("p", "final-note", levels.join(" · ")));
  card.append(block);
  return card;
}

// Chapter 2 — Task opportunities (§4.6): what the task allowed the run to reveal.
function renderOpportunitiesChapter(main) {
  const rv = state.review, f = state.forensic, rows = rv.opportunity_rows || [];
  if (!rows.length) {
    chapterEmpty(main, "No measured opportunities", chapterMeta().opportunities.reason);
    return chapterClose(main);
  }
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Task opportunities"));
  card.append(el("p", "chapter-lede", "What this run created a chance to observe — shown before any ability judgment."));
  const t = el("table", "opp-table");
  t.append(rowEls("tr", ["Opportunity", "Trigger", "Window", "Observed behaviour", "Status", "Evidence"], "th"));
  for (const r of rows) {
    const tr = el("tr");
    tr.append(td(r.ability)); tr.append(td(r.trigger)); tr.append(td(windowLabel(r, f)));
    tr.append(td(r.observed_behaviour || vocab("opportunity_status", r.status).tip));
    const stTd = el("td"); const v = vocab("opportunity_status", r.status);
    const b = el("span", "stat-badge " + oppStatusClass(r.status), v.label); b.title = v.tip; stTd.append(b); tr.append(stTd);
    tr.append(evidenceTd(r.evidence, f));
    t.append(tr);
  }
  card.append(t); main.append(card);
  chapterClose(main);
}

// Checks chapter (T1): requirement results (the atomic verifier checks, each
// mapped to its contract item, plus requirement warnings) together with the
// task/verifier audit — both are "what does this outcome actually mean",
// so they now share one chapter instead of Outcome and a separate Audit tab.
function renderChecksChapter(main) {
  const rv = state.review, f = state.forensic, checks = rv.checks || [], o = rv.outcome || {};
  const contract = rv.contract || {}, items = contract.items || [];
  const itemById = {}; items.forEach(it => (itemById[it.id] = it));

  const csec = el("div", "card card-pad");
  csec.append(el("p", "eyebrow", "Requirement results"));
  csec.append(el("p", "chapter-lede", "What each verifier check observed, and which task-contract item it covers."));
  // F3: name the reconciliation up front — how many of the rows below no
  // longer count toward rv.outcome.status, and why — instead of leaving the
  // reader to notice the per-row SUPERSEDED/STALE tags on their own.
  const supersededCount = (o.superseded_checks || []).length;
  const staleCount = (o.stale_checks || []).length;
  if (supersededCount || staleCount) {
    const bits = [];
    if (supersededCount) bits.push(supersededCount + " superseded by a later check");
    if (staleCount) bits.push(staleCount + " stale (invalidated by a later action)");
    csec.append(el("p", "check-warn", "⚠ " + bits.join("; ") + " — excluded from the current outcome below."));
  }
  const list = el("div", "check-list");
  for (const c of checks) {
    // F3: a check the backend already excluded from (superseded_by) or
    // demoted in (stale_reason) the reconciled verdict must never LOOK like a
    // live, currently-counted check — the badge follows effective_status
    // (what actually fed rv.outcome), with an explicit tag naming why it
    // differs from the raw historical status shown alongside it.
    const d = el("details", "check-row"); const sum = el("summary", "check-sum");
    const live = c.effective_status !== undefined ? c.effective_status : c.status;
    sum.append(el("span", "badge " + badgeClass(live), (live || "excluded").toUpperCase()));
    if (c.superseded_by)
      sum.append(el("span", "badge warn", "SUPERSEDED"));
    else if (c.stale_reason)
      sum.append(el("span", "badge warn", "STALE"));
    sum.append(el("span", "check-name", c.name));
    sum.append(el("span", "check-id mono", c.check_id));
    d.append(sum);
    const body = el("div", "check-body");
    if (c.superseded_by)
      body.append(el("div", "check-warn", "⚠ superseded by " + c.superseded_by
        + " — this observation (historical status: " + (c.status || "?").toUpperCase()
        + ") no longer counts toward the run's outcome."));
    else if (c.stale_reason)
      body.append(el("div", "check-warn", "⚠ stale — " + c.stale_reason
        + "; this " + (c.status || "?").toUpperCase() + " no longer counts as a current pass."));
    body.append(kvLine("Expected", fmt(c.expected) || "—"));
    body.append(kvLine("Observed", fmt(c.observed) || "—"));
    const mapped = (c.contract_item_ids || []).map(id => (itemById[id] || {}).description || id);
    body.append(kvLine("Covers contract", mapped.length ? mapped.join("; ") : "No mapped contract item"));
    for (const w of (contract.warnings || []).filter(w => (w.check_ids || []).includes(c.check_id)))
      body.append(el("div", "check-warn", "⚠ " + warnKind(w.warning_type) + " — " + w.message));
    d.append(body); list.append(d);
  }
  if (!checks.length) list.append(el("p", "chapter-lede", "This run recorded no atomic verifier checks."));
  csec.append(list); main.append(csec);

  // Requirement warnings surfaced by the contract builder (§4.5, §8.2): shown,
  // never silently resolved.
  const warns = contract.warnings || [];
  if (warns.length) {
    const wsec = el("div", "card card-pad");
    wsec.append(el("p", "eyebrow", "Requirement warnings"));
    for (const w of warns) {
      const row = el("div", "warn-row");
      row.append(el("div", "warn-kind", warnKind(w.warning_type)));
      row.append(el("div", "warn-msg", w.message));
      wsec.append(row);
    }
    main.append(wsec);
  }

  main.append(renderAuditSection(rv, f));
  chapterClose(main);
}

// Task & Verifier Audit (§11), as a section within Checks: one categorical,
// evidence-backed finding per dimension qualifying what this run's result can
// legitimately imply. Categorical values only — never scores — and a human
// concern correction (§4.13) replaces the shown assessment while the
// generated value stays visible.
function auditBadge(assessment) {
  const cls = assessment === "supported_concern" ? "neg"
    : assessment === "possible_concern" ? "limited"
    : assessment === "no_concern_detected" ? "pos" : "none";
  return el("span", "stat-badge " + cls, (assessment || "?").replace(/_/g, " "));
}
function renderAuditSection(rv, f) {
  const rows = rv.audit || [];
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Task & verifier audit"));
  if (!rows.length) {
    card.append(el("p", "chapter-lede", "No task/verifier audit was derived for this run."));
    return card;
  }
  card.append(el("p", "chapter-lede",
    "What this run's result can legitimately imply about the task and its verifier. "
    + "Categorical findings with exact evidence — the audit never overrides the result."));
  const t = el("table", "audit-table");
  t.append(rowEls("tr", ["Dimension", "Assessment", "Finding", "Evidence"], "th"));
  for (const r of rows) {
    const tr = el("tr");
    tr.append(td((r.dimension || "?").replace(/_/g, " ")));
    const aTd = el("td");
    aTd.append(auditBadge(r.assessment));
    if (r.corrected) {
      const chip = el("span", "chip", "human-adjusted");
      chip.title = "Adjusted by " + ((r.correction || {}).actor || "a reviewer")
        + (r.human_statement ? ": " + r.human_statement : "");
      aTd.append(chip);
    }
    tr.append(aTd);
    const fTd = el("td");
    fTd.append(document.createTextNode(r.statement || "—"));
    if (r.corrected) {
      fTd.append(el("div", "final-note",
        "generated: " + (((r.generated || {}).assessment || "?").replace(/_/g, " "))));
      if (r.human_statement) fTd.append(el("div", "final-note", r.human_statement));
    }
    tr.append(fTd);
    tr.append(evidenceTd(
      [].concat(r.evidence_event_ids, r.evidence_check_ids, r.evidence_item_ids), f));
    t.append(tr);
  }
  card.append(t);
  return card;
}

// Chapter 5 — Task Ability Signature (§4.10): the full n=1 table, all rows, with
// no-opportunity / not-analyzed / evidence-unavailable kept distinct.
function renderSignatureChapter(main) {
  const rv = state.review, f = state.forensic, rows = rv.signature || [];
  if (!rows.length) {
    chapterEmpty(main, "No ability signature", chapterMeta().signature.reason);
    return chapterClose(main);
  }
  const card = el("div", "card card-pad");
  const head = el("p", "eyebrow"); head.append(document.createTextNode("Task Ability Signature"));
  card.append(head);
  const title = el("h2", "empty-title"); title.append(document.createTextNode((rv.run || {}).task_id || "This task"));
  title.append(el("span", "sig-n", "n = 1 · this run only")); card.append(title);
  const t = el("table", "sig-table");
  t.append(rowEls("tr", ["Ability", "Observed behaviour", "Result", "Interpretation"], "th"));
  for (const r of rows) {
    const tr = el("tr");
    tr.append(td(r.ability)); tr.append(td(r.observed_behaviour));
    const resTd = el("td"); resTd.append(sigResultBadge(r.result)); tr.append(resTd);
    const iTd = el("td");
    if (r.measured) {
      const line = el("div"); line.append(document.createTextNode(r.interpretation + " · this run")); iTd.append(line);
      iTd.append(evidenceCell(r.evidence, f));
    } else {
      iTd.append(el("span", "sig-reason", sigNoInterp(r)));
    }
    tr.append(iTd); t.append(tr);
  }
  card.append(t); main.append(card);
  chapterClose(main);
}

// Eval lesson (T1 / formerly its own chapter, §4.11): rendered inline on the
// moment card that recommends it (see moments.js `renderLessonInline`) rather
// than as a separate chapter — the finding is where the lesson belongs. The
// body is projected from the recommending moment; a human drives the §6.10
// lifecycle and (§13.2) the improvement-experiment proposal, both persisted
// server-side. `container` is appended to, not replaced, so the caller decides
// whether it sits inside a moment card or (as chapterClose does elsewhere)
// closes out a full chapter.
function renderLessonInline(container, m) {
  const lesson = persistedLesson(m);
  const rcc = (m.root_cause_candidates || [])[0] || {};
  // Prefer the persisted (possibly edited) values; fall back to the moment's
  // projected defaults before the lesson has been accepted.
  const body = lesson || {
    observed_behaviour: m.summary, better_local_action: m.better_action || "—",
    systemic_intervention: { layer: rcc.locus, proposal: rcc.rationale },
    generalization_boundary: "", generalization_exclusion: "",
    possible_side_effects: [], regression_slice: "",
  };
  const iv = body.systemic_intervention || {};
  const status = lesson ? lesson.status : "proposed";

  container.append(kvBlock("Accepted behaviour", situationText(m)));
  container.append(kvBlock("Smallest better action", body.better_local_action || "—"));
  container.append(kvBlock("Intervention locus", iv.layer || "—"));
  container.append(kvBlock("Intervention proposal", iv.proposal || "—"));
  container.append(kvBlock("Where it should generalize", body.generalization_boundary || "—"));
  container.append(kvBlock("Where it may not generalize", body.generalization_exclusion || "—"));
  container.append(kvBlock("Possible side effects", (body.possible_side_effects || []).join(", ") || "—"));
  container.append(kvBlock("Proposed regression slice", body.regression_slice || "—"));

  const approval = el("div", "kv-block");
  approval.append(el("span", "kv-k", "Approval"));
  const av = el("span", "kv-v");
  av.append(el("span", "lesson-status", "Status · " + status)); approval.append(av);
  container.append(approval);

  const done = status === "rejected" || status === "superseded";
  const actions = el("div", "lesson-actions");
  actions.append(lessonButton("Approve for test", "primary",
    status === "proposed", () => approveLessonForTest(m)));
  actions.append(lessonButton("Edit lesson", "", !done,
    () => renderLessonEditor(container, m, lesson)));
  actions.append(lessonButton("Reject", "",
    status === "proposed" || status === "approved_for_test", () => rejectLesson(m)));
  actions.append(lessonButton("Create regression-eval proposal", "",
    status === "approved_for_test" && !(lesson && lesson.experiment_proposal),
    () => proposeExperiment(lesson)));
  container.append(actions);

  if (lesson && lesson.experiment_proposal) container.append(experimentProposalCard(lesson));
}

function lessonButton(label, extra, enabled, onClick) {
  const b = el("button", "button" + (extra ? " " + extra : ""), label);
  if (!enabled) b.disabled = true; else b.addEventListener("click", onClick);
  return b;
}

// §13.2 improvement-experiment proposal, shown once generated. Its own human
// "Approve proposal" action advances proposed → approved — the M5 accept criterion.
function experimentProposalCard(lesson) {
  const p = lesson.experiment_proposal, obs = p.observation || {}, ed = p.evaluation_design || {};
  const wrap = el("div", "lesson-experiment");
  wrap.append(el("p", "eyebrow", "Improvement experiment · " + p.status));
  wrap.append(kvBlock("Hypothesis", p.hypothesis || "—"));
  wrap.append(kvBlock("Observation", (obs.behaviour || "—") +
    " · observed " + (obs.observed != null ? obs.observed : "—") +
    " · affected failures " + (obs.affected_failures != null ? obs.affected_failures : "—") +
    (obs.opportunities == null ? " · opportunities not aggregated (single run)" : "")));
  const pc = p.proposed_change || {};
  wrap.append(kvBlock("Proposed change", (pc.layer || "—") + " — " + (pc.description || "—")));
  wrap.append(kvBlock("Evaluation design", "slice " + (ed.visible_slice || "—") +
    " · matched baseline " + ed.matched_baseline + " · " + ed.repeat_seeds + " seeds" +
    (ed.private_holdout ? " · private holdout" : "")));
  wrap.append(kvBlock("Primary measure", p.primary_measure || "—"));
  wrap.append(kvBlock("Guardrails", (p.guardrails || []).join(", ") || "—"));
  wrap.append(el("p", "chapter-lede", "Numeric success thresholds are set by the experiment owner — the generator does not invent them."));
  if (p.status !== "approved") {
    const actions = el("div", "lesson-actions");
    actions.append(lessonButton("Approve proposal", "primary", true, () => approveExperiment(lesson)));
    wrap.append(actions);
  } else {
    wrap.append(el("p", "kv-v", "Approved by " + (p.approved_by || "reviewer")));
  }
  return wrap;
}

// --- lesson lifecycle actions (POST + refresh, matching disposition.js) -------

async function ensureLesson(moment) {
  const existing = persistedLesson(moment);
  if (existing) return existing;
  const r = await apiPost("/runs/" + encodeURIComponent(state.runId) + "/lessons",
    { mutation_id: uid(), moment_id: moment.moment_id, actor: state.reviewer });
  track("lesson_created", { run_id: state.runId, moment_id: moment.moment_id,
    lesson_id: r.lesson.lesson_id });
  return r.lesson;
}
function lessonPath(lesson, suffix) {
  return "/runs/" + encodeURIComponent(state.runId) + "/lessons/" + encodeURIComponent(lesson.lesson_id) + (suffix || "");
}
async function lessonGuard(fn, okMsg) {
  try { await fn(); await refreshRun(); if (okMsg) toast(okMsg); }
  catch (e) {
    if (e.status === 409) { await refreshRun(); toast("Updated elsewhere — reloaded"); }
    else toast("Could not save lesson");
  }
}
async function approveLessonForTest(moment) {
  await lessonGuard(async () => {
    const lesson = await ensureLesson(moment);
    await apiPost(lessonPath(lesson), { base_version: lesson.lesson_version,
      status: "approved_for_test", actor: state.reviewer });
    track("lesson_approved", { run_id: state.runId, lesson_id: lesson.lesson_id });
  }, "Lesson approved for test");
}
async function rejectLesson(moment) {
  await lessonGuard(async () => {
    const lesson = await ensureLesson(moment);
    await apiPost(lessonPath(lesson), { base_version: lesson.lesson_version,
      status: "rejected", actor: state.reviewer });
    track("lesson_rejected", { run_id: state.runId, lesson_id: lesson.lesson_id });
  }, "Lesson rejected");
}
async function proposeExperiment(lesson) {
  await lessonGuard(async () => {
    await apiPost(lessonPath(lesson, "/experiment"), { actor: state.reviewer });
    track("experiment_proposed", { run_id: state.runId, lesson_id: lesson.lesson_id });
  }, "Experiment proposal generated");
}
async function approveExperiment(lesson) {
  await lessonGuard(async () => {
    await apiPost(lessonPath(lesson, "/experiment"), { action: "approve", actor: state.reviewer });
    track("experiment_approved", { run_id: state.runId, lesson_id: lesson.lesson_id });
  }, "Experiment proposal approved");
}

// "Edit lesson" (§4.11) — an inline editor for the reviewer-authored body fields.
// Interpretive/free-text proposals only; the systemic-intervention layer stays a
// controlled locus, validated server-side.
const LESSON_EDIT_FIELDS = [
  ["observed_behaviour", "Observed behaviour", "text"],
  ["better_local_action", "Smallest better action", "text"],
  ["intervention_layer", "Intervention locus", "text"],
  ["intervention_proposal", "Intervention proposal", "text"],
  ["generalization_boundary", "Where it should generalize", "text"],
  ["generalization_exclusion", "Where it may not generalize", "text"],
  ["possible_side_effects", "Possible side effects (comma-separated)", "list"],
  ["regression_slice", "Proposed regression slice", "text"],
];
async function renderLessonEditor(container, moment, lesson) {
  // The editor needs a persisted lesson to version-edit; create one on first edit,
  // then reopen the editor against the refreshed state rather than bouncing the
  // reviewer back to the chapter.
  if (!lesson) {
    await lessonGuard(async () => { await ensureLesson(moment); });
    // `lessonGuard` already called `refreshRun()` → `render()` above, which
    // rebuilt the moment card and detached the `container` this call was
    // given — writing into it now would be invisible. Re-open the editor
    // against the freshly rendered container for this same moment instead.
    const fresh = persistedLesson(moment);
    const live = document.querySelector(
      '.moment-lesson-body[data-moment-id="' + CSS.escape(moment.moment_id) + '"]');
    return (fresh && live) ? renderLessonEditor(live, moment, fresh) : render();
  }
  const iv = lesson.systemic_intervention || {};
  const seed = {
    observed_behaviour: lesson.observed_behaviour || "",
    better_local_action: lesson.better_local_action || "",
    intervention_layer: iv.layer || "", intervention_proposal: iv.proposal || "",
    generalization_boundary: lesson.generalization_boundary || "",
    generalization_exclusion: lesson.generalization_exclusion || "",
    possible_side_effects: (lesson.possible_side_effects || []).join(", "),
    regression_slice: lesson.regression_slice || "",
  };
  const card = el("div", "card card-pad lesson-editor");
  card.append(el("p", "eyebrow", "Edit lesson"));
  const inputs = {};
  for (const [id, label] of LESSON_EDIT_FIELDS) {
    card.append(el("span", "kv-k", label));
    const input = el("input", "corr-input"); input.value = seed[id]; inputs[id] = input;
    card.append(input);
  }
  const err = el("p", "corr-error"); err.id = "lesson-edit-error"; card.append(err);
  const actions = el("div", "lesson-actions");
  const save = el("button", "button primary", "Save lesson");
  save.addEventListener("click", () => saveLessonEdits(lesson, inputs, err));
  const cancel = el("button", "button", "Cancel");
  cancel.addEventListener("click", render);
  actions.append(save, cancel); card.append(actions);
  container.innerHTML = ""; container.append(card);
}
async function saveLessonEdits(lesson, inputs, err) {
  const edits = {};
  const ba = inputs.better_local_action.value.trim();
  if (ba !== (lesson.better_local_action || "")) edits.better_local_action = ba;
  const layer = inputs.intervention_layer.value.trim();
  const proposal = inputs.intervention_proposal.value.trim();
  const iv = lesson.systemic_intervention || {};
  if (layer !== (iv.layer || "") || proposal !== (iv.proposal || ""))
    edits.systemic_intervention = { layer: layer || null, proposal };
  for (const f of ["observed_behaviour", "generalization_boundary", "generalization_exclusion", "regression_slice"]) {
    const v = inputs[f].value.trim();
    if (v !== (lesson[f] || "")) edits[f] = v;
  }
  const effects = inputs.possible_side_effects.value.split(/\s*,\s*/).map(s => s.trim()).filter(Boolean);
  if (effects.join(", ") !== (lesson.possible_side_effects || []).join(", "))
    edits.possible_side_effects = effects;
  if (!Object.keys(edits).length) { toast("Nothing changed"); render(); return; }
  try {
    await apiPost(lessonPath(lesson), { base_version: lesson.lesson_version,
      actor: state.reviewer, edits });
    await refreshRun(); toast("Lesson updated");
  } catch (e) {
    if (e.status === 409) { await refreshRun(); toast("Updated elsewhere — reloaded"); return; }
    const detail = e.data && e.data.detail ? e.data.detail : "Save failed";
    err.textContent = typeof detail === "string" ? detail : "Save failed";
  }
}

// --- chapter helpers ---------------------------------------------------------
function kvLine(k, v) { const r = el("div", "kv-line"); r.append(el("span", "kv-k", k), el("span", "kv-v", v)); return r; }
function kvBlock(k, v) { const r = el("div", "kv-block"); r.append(el("span", "kv-k", k), el("span", "kv-v", v)); return r; }
function badgeClass(status) { return status === "passed" ? "pass" : status === "failed" ? "fail" : "warn"; }
function warnKind(t) { return (t || "warning").replace(/_/g, " "); }
function windowLabel(r, f) {
  const a = f.steps.findIndex(s => (s.event_ids || []).includes(r.start_event_id));
  const b = f.steps.findIndex(s => (s.event_ids || []).includes(r.end_event_id));
  if (a >= 0 && b >= 0) return a === b ? "Step " + (a + 1) : "Steps " + (a + 1) + "–" + (b + 1);
  return (r.start_event_id || "?") + "–" + (r.end_event_id || "?");
}
function oppStatusClass(s) { return s === "measured" ? "measured" : s === "not_evaluable" ? "limited" : "none"; }
function sigResultBadge(result) {
  const cls = result === "Successful" ? "pos" : result === "Failed" ? "neg" : "none";
  return el("span", "stat-badge " + cls, result || "—");
}
function sigNoInterp(r) {
  // Deterministic core emits only "Not measured" (no opportunity). Model-enriched
  // signatures may add not_reviewed / not_evaluable; keep all three distinct.
  if (r.status === "not_reviewed") return "Not analyzed";
  if (r.status === "not_evaluable") return "Evidence unavailable";
  return "No opportunity occurred";
}
// Returns an inline <span> container of evidence links/static ids. Callers that
// need a real table cell wrap it via evidenceTd(); never append this directly
// to a <tr>, and never nest it inside another <td> (it is not a cell itself).
function evidenceCell(evidence, f) {
  const wrap = el("span", "ev-cell mono");
  const ids = (evidence || []).filter(Boolean);
  if (!ids.length) { wrap.append(el("span", "ev-static", "—")); return wrap; }
  for (const id of ids) {
    const idx = f.steps.findIndex(s => (s.event_ids || []).includes(id));
    if (idx >= 0) {
      const b = el("button", "ev-link", id); b.title = "Open source step " + (idx + 1);
      b.addEventListener("click", () => openTrace(f.steps[idx].step_id)); wrap.append(b);
    } else wrap.append(el("span", "ev-static", id));
  }
  return wrap;
}
// Wrap an evidenceCell in a real <td> for table rows.
function evidenceTd(evidence, f) { const c = el("td", "mono"); c.append(evidenceCell(evidence, f)); return c; }

