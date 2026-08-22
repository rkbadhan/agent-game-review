"use strict";

// --- evidence panel (right column) -------------------------------------------
function renderEvidencePanel() {
  const host = $("#evidence-body"); host.textContent = "";
  const onMoments = state.view === "review" && state.chapter === "moments";
  if (!onMoments || !currentMoment()) {
    host.append(el("div", "panel-dim", onMoments ? "No moment selected." : "Evidence follows the Key-moments chapter’s selected moment."));
    return;
  }
  const rv = state.review, f = state.forensic, moment = currentMoment();
  const cap = rv.capture || {};
  const capNote = "capture " + (cap.capture_id || "?") + " rev " + (cap.capture_revision ?? "?") + " · " + (cap.capture_completeness || "?");

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
  trust.append(tc("Evidence", ev.label, ev.tip), tc("Attribution", at.label, at.tip));
  head.append(trust);
  host.append(head);

  const content = el("div", "evidence-content");
  // situation & action
  const g1 = evGroup("Situation & action");
  for (const fct of moment.facts || []) {
    const parts = [fct.type]; if (fct.check_id) parts.push("check " + fct.check_id); if (fct.validation) parts.push("validation " + fct.validation);
    g1.append(evItem("fact", parts.join(" · "), "expected " + fmtVal(fct.expected) + " · observed " + fmtVal(fct.observed), capNote, null));
  }
  for (const a of moment.anchor_event_ids || []) {
    const idx = f.steps.findIndex(s => (s.event_ids || []).includes(a));
    const st = idx >= 0 ? f.steps[idx] : null; const av = st && st.availability ? st.availability.state : null;
    g1.append(evItem("event", a + (st ? " · " + (st.kind || "?") : ""), st ? "trace step " + (idx + 1) + (av && av !== "complete" ? " · evidence " + av : "") : "not captured", capNote, st ? st.step_id : null));
  }
  content.append(g1);

  // consequence & outcome
  const checks = rv.checks || [], slices = rv.evidence_slices || [];
  const g2 = evGroup("Consequence & outcome");
  let any = false;
  for (const cid of moment.affected_checks || []) {
    const chk = checks.find(c => c.check_id === cid);
    if (chk) { any = true; g2.append(evItem("check", cid + " · " + (chk.status || "?"), "expected " + fmtVal(chk.expected) + " · observed " + fmtVal(chk.observed), capNote, null)); }
    for (const sl of slices.filter(s => s.check_id === cid)) { any = true;
      g2.append(evItem("slice", sl.slice_id + " · " + vocab("attribution", sl.attribution_ceiling).label, sl.rationale, capNote, null)); }
  }
  if (!any) g2.append(evItem("outcome", (rv.outcome||{}).status || "?", "no per-check slice linked to this moment", capNote, null));
  content.append(g2);

  // interpretation boundary
  const g3 = evGroup("Interpretation boundary");
  const note = el("div", "judgment-note");
  note.textContent = "The validated facts above are mechanical. " + (moment.enrichment_source
    ? "Any “likely impact” is a reviewer interpretation graded " + vocab("attribution", moment.attribution_ceiling).label + "; replay has not established causality unless marked replay-supported."
    : "This is a deterministic review — no interpretation or better action was generated, so none is shown.");
  g3.append(note);
  content.append(g3);

  host.append(content);
}
function countEvidence(rv, f, moment) {
  let n = (moment.facts || []).length + (moment.anchor_event_ids || []).length;
  const checks = rv.checks || [], slices = rv.evidence_slices || [];
  for (const cid of moment.affected_checks || []) { if (checks.find(c => c.check_id === cid)) n++; n += slices.filter(s => s.check_id === cid).length; }
  return n;
}
function evGroup(title) { const g = el("section", "evidence-group"); g.append(el("h3", null, title)); return g; }
function evItem(kind, head, detail, capNote, stepId) {
  const it = el("article", "evidence-item");
  const l = el("div", "evidence-label"); l.append(el("span", "evidence-type", kind), el("span", null, head)); it.append(l);
  if (detail) it.append(el("p", "evidence-detail", detail));
  if (capNote) it.append(el("div", "evidence-cap", capNote));
  if (stepId) { const b = el("button", "source-link", "Open source step →");
    b.addEventListener("click", () => { track("claim_evidence_opened", { run_id: state.runId,
      step_id: stepId, evidence_kind: kind }); openTrace(stepId); }); it.append(b); }
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

