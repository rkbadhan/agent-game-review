"use strict";

// --- review chapters (§4.4) --------------------------------------------------
const CHAPTERS = [
  ["outcome", "Outcome"], ["opportunities", "Opportunities"], ["moments", "Key moments"],
  ["strongest", "Strongest behaviour"], ["signature", "Ability signature"],
  ["audit", "Task & verifier audit"], ["lesson", "Eval lesson"],
];
function strongestMoments() { return currentMoments().filter(m => m.polarity === "positive"); }
function hasCorrection() { return ((state.review && state.review.feedback) || []).some(f => f.kind === "quick_relabel"); }
function lessonMoment() {
  // A lesson is supported only when a reviewed (model-enriched) moment recommends
  // one; the deterministic core recommends none, so this returns null and the
  // chapter shows the canonical §4.11 empty state.
  return currentMoments().find(x => x.eval_lesson_recommended && x.enrichment_source) || null;
}
function persistedLesson(m) {
  const id = "lesson_" + (m && m.moment_id);
  return ((state.review && state.review.lessons) || []).find(l => l.lesson_id === id) || null;
}
function evalLesson() {
  const m = lessonMoment();
  // `lesson` is the persisted record once a human has accepted it; until then the
  // chapter shows the proposed body projected from the moment.
  return m ? { moment: m, lesson: persistedLesson(m) } : null;
}
function chapterMeta() {
  const rv = state.review, moments = currentMoments();
  return {
    outcome: { available: true },
    opportunities: { available: (rv.opportunity_rows || []).length > 0,
      reason: "This run reached no opportunity window this review could measure." },
    moments: { available: moments.length > 0, corrected: hasCorrection(),
      reason: rv.review_mode === "not_reviewable" ? "The captured evidence is insufficient for a trustworthy review."
        : rv.review_mode === "model_enriched" ? "No decisive moment was selected." : "No candidate moment was flagged deterministically." },
    strongest: { available: strongestMoments().length > 0,
      reason: "This review supported no single behaviour worth preserving." },
    signature: { available: (rv.signature || []).length > 0,
      reason: "No ability-signature rows were derived for this run." },
    audit: { available: (rv.audit || []).length > 0,
      reason: "No task/verifier audit was derived for this run." },
    lesson: { available: !!evalLesson(),
      reason: "No Eval Lesson generated." },
  };
}
function setChapter(id) { state.view = "review"; state.chapter = id; state.viewed.add(id);
  track("review_chapter_viewed", { run_id: state.runId, chapter: id }); render();
  $("#main").scrollTo({ top: 0, behavior: "smooth" }); }
function moveChapter(dir) {
  const ids = CHAPTERS.map(c => c[0]); let i = ids.indexOf(state.chapter);
  i = Math.max(0, Math.min(ids.length - 1, i + dir)); setChapter(ids[i]);
}
function renderChapter(main) {
  state.viewed.add(state.chapter);
  ({ outcome: renderOutcomeChapter, opportunities: renderOpportunitiesChapter,
     moments: renderMomentsChapter, strongest: renderStrongestChapter,
     signature: renderSignatureChapter, audit: renderAuditChapter,
     lesson: renderLessonChapter }[state.chapter] || renderMomentsChapter)(main);
}
function renderChapterFooter(main) {
  const ids = CHAPTERS.map(c => c[0]), i = ids.indexOf(state.chapter);
  const foot = el("div", "chapter-foot");
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

// Chapter 1 — Outcome (§4.5): what passed/failed/warned/could-not-be-checked,
// each check mapped to its contract item, plus what limits the review.
function renderOutcomeChapter(main) {
  const rv = state.review, f = state.forensic, o = rv.outcome || {}, checks = rv.checks || [];
  const contract = rv.contract || {}, items = contract.items || [];
  const itemById = {}; items.forEach(it => (itemById[it.id] = it));

  const card = el("div", "card");
  const outcome = el("div", "outcome");
  const oc = el("div");
  oc.append(el("p", "eyebrow", "What happened"));
  const failed = checks.filter(c => c.status === "failed");
  const undetermined = checks.filter(c => ["unknown", "skipped", "error"].includes(c.status));
  // F1 follow-up: explicit outcome semantics — a run with no checks was never
  // verified, and checks that ended unknown/skipped/error recorded no verdict,
  // so neither can be headed "All checks passed."
  if (!checks.length) {
    oc.append(el("h2", null, "No verifier checks were recorded."));
    oc.append(el("p", null, "This run is UNVERIFIED — the capture carries no atomic check evidence, so nothing here should be read as a pass."));
  } else if (failed.length) {
    oc.append(el("h2", null, failed.length + " check(s) failed"));
    oc.append(el("p", null, "Failed: " + failed.map(c => c.check_id + " — " + c.name).join("; ")));
  } else if (undetermined.length) {
    oc.append(el("h2", null, "Check outcomes undetermined."));
    oc.append(el("p", null, "No check failed, but " + undetermined.map(c => c.check_id + " (" + c.status + ")").join(", ")
      + " recorded no verdict — the run is UNDETERMINED, not passed."));
  } else {
    oc.append(el("h2", null, "All checks passed."));
    oc.append(el("p", null, "No deterministic warnings were detected."));
  }
  outcome.append(oc);
  const rq = el("div", "req-summary");
  rq.append(el("div", "req-count " + statusClass(o.status), (o.passed ?? "?") + "/" + (o.total ?? "?")));
  const rc = el("div", "req-copy");
  rc.append(el("strong", null, "Requirements evidenced"), el("span", null, "Objective task progress, not a success estimate"));
  rq.append(rc); outcome.append(rq);
  card.append(outcome);
  const vm = vocab("review_mode", rv.review_mode);
  const modeNote = el("div", "review-mode-note" + (rv.review_mode === "not_reviewable" ? " not-reviewable" : ""));
  if (rv.review_mode === "not_reviewable") {
    const missing = rv.missing_capabilities || [];
    modeNote.append(vm.label + " — the captured evidence is insufficient for a trustworthy review"
      + (missing.length ? ". Missing capabilities: " + missing.join(", ") + "."
        : "; affected detectors are marked Not evaluated."));
  } else {
    modeNote.append(vm.label + (rv.review_mode === "deterministic_only"
      ? " — Situation, action, and consequence are restated from validated facts. Likely impact and better action are added only by the model reviewer."
      : " — deterministic baseline plus model-assisted interpretation."));
  }
  card.append(modeNote);
  main.append(card);

  // Atomic checks, each expandable to its verifier evidence + contract mapping.
  const csec = el("div", "card card-pad");
  csec.append(el("p", "eyebrow", "Atomic checks"));
  csec.append(el("p", "chapter-lede", "What each verifier check observed, and which task-contract item it covers."));
  const list = el("div", "check-list");
  for (const c of checks) {
    const d = el("details", "check-row"); const sum = el("summary", "check-sum");
    sum.append(el("span", "badge " + badgeClass(c.status), (c.status || "?").toUpperCase()));
    sum.append(el("span", "check-name", c.name));
    sum.append(el("span", "check-id mono", c.check_id));
    d.append(sum);
    const body = el("div", "check-body");
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

// Chapter 4 — Strongest behaviour (§4.10): a concise preservation summary of one
// behaviour worth keeping, when the review supports one.
function renderStrongestChapter(main) {
  const rv = state.review, f = state.forensic, positives = strongestMoments();
  if (!positives.length) {
    chapterEmpty(main, "No preserved-strength behaviour", chapterMeta().strongest.reason);
    return chapterClose(main);
  }
  const m = positives[0];
  const idx = currentMoments().indexOf(m);
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Strongest behaviour"));
  card.append(el("h2", "empty-title", m.summary));
  card.append(kvBlock("Opportunity", situationText(m)));
  card.append(kvBlock("Chosen behaviour", actionText(m, f)));
  card.append(kvBlock("Immediate result", consequenceText(m)));
  card.append(kvBlock("Outcome supported", (rv.outcome || {}).status
    ? "Contributed to outcome " + (rv.outcome.status) + " (" + (rv.outcome.passed ?? "?") + "/" + (rv.outcome.total ?? "?") + " checks)."
    : "See the linked moment for the supported outcome."));
  card.append(el("div", "preserve-note", "Preserve during a harness change: keep whatever produced this behaviour intact when the scaffold, tools, or prompts are revised."));
  const open = el("button", "button", "Open in Key moments →");
  open.addEventListener("click", () => { if (idx >= 0) state.momentIdx = idx; setChapter("moments"); });
  const actions = el("div", "lesson-actions"); actions.append(open); card.append(actions);
  main.append(card);
  chapterClose(main);
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

// Chapter 6 — Task & Verifier Audit (§11): one categorical, evidence-backed
// finding per dimension qualifying what this run's result can legitimately
// imply. Categorical values only — never scores — and a human concern
// correction (§4.13) replaces the shown assessment while the generated value
// stays visible.
function auditBadge(assessment) {
  const cls = assessment === "supported_concern" ? "neg"
    : assessment === "possible_concern" ? "limited"
    : assessment === "no_concern_detected" ? "pos" : "none";
  return el("span", "stat-badge " + cls, (assessment || "?").replace(/_/g, " "));
}
function renderAuditChapter(main) {
  const rv = state.review, f = state.forensic, rows = rv.audit || [];
  if (!rows.length) {
    chapterEmpty(main, "No task/verifier audit", chapterMeta().audit.reason);
    return chapterClose(main);
  }
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Task & verifier audit"));
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
  card.append(t); main.append(card);
  chapterClose(main);
}

// Chapter 7 — Eval Lesson (§4.11): shown only when a reviewed finding supports a
// concrete improvement; otherwise the canonical empty state. The lesson body is
// projected from the recommending moment; a human drives the §6.10 lifecycle and
// (§13.2) the improvement-experiment proposal, all persisted server-side.
function renderLessonChapter(main) {
  const ctx = evalLesson();
  if (!ctx) {
    const empty = el("div", "card card-pad chapter-empty");
    empty.append(el("p", "eyebrow", "Eval lesson"));
    empty.append(el("h2", "empty-title", "No Eval Lesson generated."));
    empty.append(el("p", "chapter-lede", "This deterministic review found no supported risk or accepted diagnosis."));
    main.append(empty);
    return chapterClose(main);
  }
  const m = ctx.moment, lesson = ctx.lesson;
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

  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Eval lesson"));
  card.append(el("h2", "empty-title", body.observed_behaviour || m.summary));
  card.append(kvBlock("Accepted behaviour", situationText(m)));
  card.append(kvBlock("Smallest better action", body.better_local_action || "—"));
  card.append(kvBlock("Intervention locus", iv.layer || "—"));
  card.append(kvBlock("Intervention proposal", iv.proposal || "—"));
  card.append(kvBlock("Where it should generalize", body.generalization_boundary || "—"));
  card.append(kvBlock("Where it may not generalize", body.generalization_exclusion || "—"));
  card.append(kvBlock("Possible side effects", (body.possible_side_effects || []).join(", ") || "—"));
  card.append(kvBlock("Proposed regression slice", body.regression_slice || "—"));

  const approval = el("div", "kv-block");
  approval.append(el("span", "kv-k", "Approval"));
  const av = el("span", "kv-v");
  av.append(el("span", "lesson-status", "Status · " + status)); approval.append(av);
  card.append(approval);

  const done = status === "rejected" || status === "superseded";
  const actions = el("div", "lesson-actions");
  actions.append(lessonButton("Approve for test", "primary",
    status === "proposed", () => approveLessonForTest(m)));
  actions.append(lessonButton("Edit lesson", "", !done,
    () => renderLessonEditor(main, m, lesson)));
  actions.append(lessonButton("Reject", "",
    status === "proposed" || status === "approved_for_test", () => rejectLesson(m)));
  actions.append(lessonButton("Create regression-eval proposal", "",
    status === "approved_for_test" && !(lesson && lesson.experiment_proposal),
    () => proposeExperiment(lesson)));
  card.append(actions);

  if (lesson && lesson.experiment_proposal) card.append(experimentProposalCard(lesson));
  main.append(card);
  chapterClose(main);
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
async function renderLessonEditor(main, moment, lesson) {
  // The editor needs a persisted lesson to version-edit; create one on first edit,
  // then reopen the editor against the refreshed state rather than bouncing the
  // reviewer back to the chapter.
  if (!lesson) {
    await lessonGuard(async () => { await ensureLesson(moment); });
    const fresh = persistedLesson(moment);
    return fresh ? renderLessonEditor(main, moment, fresh) : render();
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
  main.innerHTML = ""; main.append(card); chapterClose(main);
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

