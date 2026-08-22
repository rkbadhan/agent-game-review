"use strict";

const UNRESOLVED_CONTRACT = new Set(["at_risk","evidenced_violated","unknown","not_observed","in_progress"]);
function renderTimeline(rv, moments, seqOfEvent, maxSeq) {
  const pct = seq => (maxSeq > 1 ? ((seq-1)/(maxSeq-1))*100 : 0);
  const wrap = el("div");

  // phase lane
  const plane = el("div", "rlane");
  const curPhase = currentMoment() ? currentMoment().phase_id : null;
  for (const ph of rv.phases || []) {
    const seqs = (ph.event_ids || []).map(e => seqOfEvent[e]).filter(x => x != null);
    if (!seqs.length) continue;
    const a = Math.min(...seqs), b = Math.max(...seqs);
    const band = el("div", "phase-band" + (ph.phase_id === curPhase ? " active" : ""));
    band.style.left = pct(a) + "%"; band.style.width = Math.max(9, pct(b) - pct(a)) + "%"; band.title = ph.label;
    band.append(el("span", "phase-band-label", ph.label));
    band.addEventListener("click", () => { const i = moments.findIndex(m => m.phase_id === ph.phase_id); if (i >= 0) selectMoment(i); });
    plane.append(band);
  }
  wrap.append(laneRow("Phase", plane));

  // progress lane
  const glane = el("div", "rlane");
  const progAt = groupBySeq(rv.contract_observations || [], ob => seqOfEvent[ob.at_event_id], ob => state.showAllContract || UNRESOLVED_CONTRACT.has(ob.status));
  for (const [seq, obs] of progAt) {
    const cluster = clusterAt(pct(seq));
    for (const ob of obs) {
      const cls = ob.status === "evidenced_violated" ? "bad" : ob.status === "evidenced_satisfied" ? "ok" : "warn";
      const chip = el("span", "prog-mark " + cls, ob.contract_item_id);
      chip.title = ob.contract_item_id + " · " + vocab("contract_status", ob.status).label + " @ " + ob.at_event_id;
      cluster.append(chip);
    }
    glane.append(cluster);
  }
  if (!progAt.size) glane.append(el("span", "lane-empty", state.showAllContract ? "no contract items" : "no unresolved items"));
  wrap.append(laneRow("Task progress", glane));

  // moment lane
  const mlane = el("div", "rlane");
  const momAt = groupBySeq(moments.map((m, i) => ({ m, i })), x => x.m._seq, () => true);
  for (const [seq, entries] of momAt) {
    const cluster = clusterAt(pct(seq));
    for (const { m, i } of entries) {
      const mk = el("button", "moment-mark " + momentTagClass(m) + (i === state.momentIdx ? " active" : ""), String(i + 1));
      mk.title = momentTypeLabel(m) + " · " + m.summary;
      mk.setAttribute("aria-label", "Moment " + (i + 1) + ": " + momentTypeLabel(m));
      mk.addEventListener("click", () => selectMoment(i));
      cluster.append(mk);
    }
    mlane.append(cluster);
  }
  wrap.append(laneRow("Key moments", mlane));

  const toggle = el("label", "sort-row"); toggle.style.marginTop = "6px";
  const cb = el("input"); cb.type = "checkbox"; cb.checked = !!state.showAllContract;
  cb.addEventListener("change", () => { state.showAllContract = cb.checked; render(); });
  toggle.append(cb, document.createTextNode(" Show all contract items"));
  wrap.append(toggle);
  return wrap;
}
function laneRow(label, lane) { const row = el("div", "lane-row"); row.append(el("div", "lane-label", label));
  const track = el("div", "lane-track"); track.append(lane); row.append(track); return row; }
function groupBySeq(items, seqOf, keep) { const by = new Map();
  for (const it of items) { if (!keep(it)) continue; const s = seqOf(it); if (s == null) continue; if (!by.has(s)) by.set(s, []); by.get(s).push(it); } return by; }
function clusterAt(p) { const c = el("div", "lane-cluster"); c.style.left = p + "%";
  c.style.transform = p >= 85 ? "translateX(-100%)" : p <= 15 ? "translateX(0)" : "translateX(-50%)"; return c; }
