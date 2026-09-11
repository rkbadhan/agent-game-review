"use strict";

// --- evidence panel (right column) -------------------------------------------
// T3: the panel leads with the actual captured source — the linked tool
// input/output or agent message — instead of only metadata about it. One
// "focused" event drives that display; a claim's other anchors are still
// listed, and clicking one moves the focus to that exact event. Capture ids,
// revisions, and per-fact validation move to one Provenance section at the
// bottom instead of repeating on every item.
function stepForEvent(f, eventId) {
  const idx = f.steps.findIndex(s => (s.event_ids || []).includes(eventId));
  return idx >= 0 ? { step: f.steps[idx], idx } : { step: null, idx: -1 };
}
// The moment's fact values (expected/observed), flattened to strings — the
// only terms an excerpt may highlight as a supporting span (§4.9/T3: never a
// fuzzy match, only an exact one).
function factSpanTerms(facts) {
  const terms = [];
  for (const fct of facts || []) {
    for (const key of ["expected", "observed"]) {
      const v = fct[key]; if (v == null) continue;
      for (const item of Array.isArray(v) ? v : [v])
        if (typeof item === "string" && item.length) terms.push(item);
    }
  }
  return terms;
}
function renderEvidencePanel() {
  const host = $("#evidence-body"); host.textContent = "";
  const onMoments = state.view === "review" && state.chapter === "moments";
  if (!onMoments || !currentMoment()) {
    host.append(el("div", "panel-dim", onMoments ? "No moment selected." : "Evidence follows the Key-moments chapter’s selected moment."));
    return;
  }
  const rv = state.review, f = state.forensic, moment = currentMoment();

  // header + trust strip
  const head = el("div", "evidence-head");
  const ph = el("div", "panel-heading"); ph.append(el("h2", null, "Evidence & judgment"));
  const items = countEvidence(rv, f, moment);
  ph.append(el("span", "evidence-count", String(items)));
  head.append(ph);
  head.append(el("div", "evidence-subtitle", "Support for the selected moment, grouped by claim."));
  const trust = el("div", "trust-strip");
  const tc = (label, val, tip) => { const c = el("div", "trust-card"); c.append(el("span", null, label), el("strong", null, val)); if (tip) c.title = tip; return c; };
  const ev = vocab("evidence", moment.evidence_grade), at = vocab("attribution", moment.attribution_ceiling);
  // "Cause" is the plain label for the attribution ceiling — how firmly the
  // evidence ties the action to the outcome; the precise term and definition
  // stay on hover and in the glossary.
  trust.append(tc("Evidence", ev.label, ev.tip), tc("Cause", at.label, at.tip));
  head.append(trust);
  host.append(head);

  const content = el("div", "evidence-content");
  content.append(renderSourceGroup(rv, f, moment));
  content.append(renderChecksGroup(rv, f, moment));

  // fact vs. interpretation
  const g3 = evGroup("Fact vs. interpretation");
  const note = el("div", "judgment-note");
  note.textContent = "The validated facts above are mechanical. " + (moment.enrichment_source
    ? "Any “likely impact” is a reviewer interpretation graded " + vocab("attribution", moment.attribution_ceiling).label + "; replay has not established causality unless marked replay-supported."
    : "This is a deterministic review — no interpretation or better action was generated, so none is shown.");
  g3.append(note);
  content.append(g3);

  content.append(renderProvenanceGroup(rv, f, moment));

  host.append(content);
}

// The finding's linked source: which anchor is focused, that event's actual
// captured content (with a supporting span highlighted when exact), and a way
// to look at neighbouring steps or open the full trace at that exact point.
function renderSourceGroup(rv, f, moment) {
  const g = evGroup("What the agent did");
  const anchors = (moment.anchor_event_ids || []).filter(Boolean);
  // The anchored-source display (tabs, captured content, neighbours, "open
  // full trace") only applies when there is an anchor to focus. The facts
  // this moment cites are independent of that — an omission/absence moment
  // routinely has facts but no anchor_event_ids, and previously lost that
  // evidence entirely by returning before the fact loop below ever ran.
  if (!anchors.length) {
    g.append(el("p", "panel-dim", "This moment has no anchored source event."));
  } else {
    // Selecting an anchor (T3) moves the focus to that exact event; a focus id
    // that is not one of this moment's anchors (e.g. left over from a
    // previously selected moment) falls back to the first anchor, so a newly
    // selected finding immediately shows its own linked source.
    if (!anchors.includes(state.evidenceFocus)) state.evidenceFocus = anchors[0];
    if (anchors.length > 1) {
      const tabs = el("div", "evidence-anchor-tabs");
      for (const a of anchors) {
        const { step, idx } = stepForEvent(f, a);
        const b = el("button", "seg" + (a === state.evidenceFocus ? " active" : ""),
          step ? "Step " + (idx + 1) : a);
        b.title = step ? (step.kind || "event").replace(/_/g, " ") + " · " + a : a + " (not captured)";
        b.addEventListener("click", () => { state.evidenceFocus = a; renderEvidencePanel(); });
        tabs.append(b);
      }
      g.append(tabs);
    }
    const { step, idx } = stepForEvent(f, state.evidenceFocus);
    const terms = factSpanTerms(moment.facts);
    const item = el("article", "evidence-item evidence-source");
    const l = el("div", "evidence-label");
    l.append(el("span", "evidence-type", step ? (step.kind || "event").replace(/_/g, " ") : "event"));
    l.append(el("span", null, state.evidenceFocus + (step ? " · trace step " + (idx + 1) : "")));
    item.append(l);
    item.append(renderStepContent(step, { highlightTerms: terms }));
    // Same eligibility test `highlightSpan` uses (§ trace.js): a term too
    // short to highlight must not count as "found" here either, or this note
    // and the (absent) highlight would silently disagree with each other.
    const highlightable = terms.filter(t => t.length >= MIN_HIGHLIGHT_TERM_LENGTH);
    if (step && highlightable.length && !highlightable.some(t => stepContentHasTerm(step, t)))
      item.append(el("p", "evidence-span-note", "No exact supporting span was located in this event's captured text — shown without a highlighted match."));
    const paired = renderPairedStep(f, step);
    if (paired) item.append(paired);
    item.append(renderNeighbors(f, idx));
    if (step) {
      const b = el("button", "source-link", "Open source step →");
      b.addEventListener("click", () => { track("claim_evidence_opened", { run_id: state.runId,
        step_id: step.step_id, evidence_kind: "event" }); openTrace(step.step_id); });
      item.append(b);
    }
    g.append(item);
  }

  // Every fact this moment cites, restated beside its own claim rather than
  // as a bare list — the expected/observed values a reader should be able to
  // check against the source content above. Rendered regardless of anchors.
  for (const fct of moment.facts || []) {
    const parts = [fct.type]; if (fct.check_id) parts.push("check " + fct.check_id);
    g.append(evItem(parts.join(" · "), "expected " + fmtVal(fct.expected) + " · observed " + fmtVal(fct.observed)));
  }
  return g;
}
function stepContentHasTerm(step, term) {
  const c = (step && step.content) || {};
  return ["content", "data", "summary"].some(k => typeof c[k] === "string" && c[k].includes(term));
}
// Users can expand neighbouring events (T3) without leaving the evidence
// panel — a collapsed strip of the steps immediately around the focused one,
// each a jump to make it the new focus.
function renderNeighbors(f, idx) {
  const wrap = el("details", "evidence-neighbors");
  wrap.append(el("summary", null, "Show neighbouring events"));
  const list = el("div", "evidence-neighbor-list");
  const steps = f.steps || [];
  // idx is -1 when the focused event has no source step of its own (an
  // uncaptured id) — there is no trace position to be "near", so showing the
  // first two steps as its neighbours would be a made-up adjacency.
  if (idx < 0) {
    list.append(el("p", "panel-dim", "This event has no position in the captured trace."));
  } else {
    const from = Math.max(0, idx - 2), to = Math.min(steps.length - 1, idx + 2);
    for (let i = from; i <= to; i++) {
      if (i === idx) continue;
      const s = steps[i];
      const row = el("button", "evidence-neighbor-row",
        "Step " + (i + 1) + " · " + (s.kind || "?").replace(/_/g, " ") + (s.actor ? " · " + s.actor : ""));
      row.addEventListener("click", () => {
        const eid = (s.event_ids || [])[0];
        if (eid) { state.evidenceFocus = eid; renderEvidencePanel(); }
      });
      list.append(row);
    }
  }
  if (!list.childNodes.length) list.append(el("p", "panel-dim", "No neighbouring steps in this trace."));
  wrap.append(list);
  return wrap;
}

// Check-related evidence (T3): the check's name, expected value, observed
// value, and status together, not split across a summary line and a detail.
function renderChecksGroup(rv, f, moment) {
  const g = evGroup("What resulted");
  const checks = rv.checks || [], slices = rv.evidence_slices || [];
  let any = false;
  for (const cid of moment.affected_checks || []) {
    const chk = checks.find(c => c.check_id === cid);
    if (chk) {
      any = true;
      const it = el("article", "evidence-item evidence-check");
      const l = el("div", "evidence-label");
      l.append(el("span", "evidence-type", "check"), el("span", null, chk.name || cid));
      it.append(l);
      const statusRow = el("div", "check-status-row");
      statusRow.append(el("span", "badge " + badgeClass(chk.status), (chk.status || "?").toUpperCase()),
        el("span", "check-id mono", cid));
      it.append(statusRow);
      it.append(kvLine("Expected", fmt(chk.expected) || "—"));
      it.append(kvLine("Observed", fmt(chk.observed) || "—"));
      g.append(it);
    }
    for (const sl of slices.filter(s => s.check_id === cid)) {
      any = true;
      g.append(evItem(sl.slice_id + " · " + vocab("attribution", sl.attribution_ceiling).label, sl.rationale));
    }
  }
  if (!any) g.append(evItem("outcome", ((rv.outcome || {}).status || "?") + " — no per-check slice linked to this moment"));
  return g;
}

// Provenance (T3): capture id/revision/completeness and per-fact validation,
// stated once here instead of repeated above every excerpt.
function renderProvenanceGroup(rv, f, moment) {
  const cap = rv.capture || {};
  const wrap = el("details", "evidence-provenance");
  wrap.append(el("summary", null, "Where this came from"));
  const body = el("div", "provenance-body");
  body.append(kvLine("Capture", (cap.capture_id || "?") + " · rev " + (cap.capture_revision ?? "?")));
  body.append(kvLine("Completeness", cap.capture_completeness || "?"));
  (moment.facts || []).forEach((fct, i) => {
    if (fct.validation) body.append(kvLine("Fact " + (i + 1) + " validation", fct.validation));
  });
  wrap.append(body);
  return wrap;
}
function countEvidence(rv, f, moment) {
  let n = (moment.facts || []).length + (moment.anchor_event_ids || []).length;
  const checks = rv.checks || [], slices = rv.evidence_slices || [];
  for (const cid of moment.affected_checks || []) { if (checks.find(c => c.check_id === cid)) n++; n += slices.filter(s => s.check_id === cid).length; }
  return n;
}
function evGroup(title) { const g = el("section", "evidence-group"); g.append(el("h3", null, title)); return g; }
function evItem(head, detail) {
  const it = el("article", "evidence-item");
  const l = el("div", "evidence-label"); l.append(el("span", null, head)); it.append(l);
  if (detail) it.append(el("p", "evidence-detail", detail));
  return it;
}
function focusEvidence() {
  const m = currentMoment();
  track("evidence_opened", { run_id: state.runId, moment_id: m && m.moment_id });
  const panel = $("#evidence-panel");
  if (window.innerWidth <= 1180) panel.classList.add("open");
  panel.scrollTo({ top: 0, behavior: "smooth" });
  toast("Evidence aligned to the selected moment");
}
$("#evidence-tab").addEventListener("click", () => $("#evidence-panel").classList.toggle("open"));

