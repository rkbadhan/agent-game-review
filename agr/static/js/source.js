"use strict";

// --- Source view -------------------------------------------------------------
async function renderSource(main) {
  const wrap = el("div", "section"); wrap.append(el("div", "subline", "Loading immutable source…")); main.append(wrap);
  let src; try { src = await api("/runs/" + encodeURIComponent(state.runId) + "/source"); }
  catch (e) { wrap.textContent = ""; wrap.append(el("div", "empty", "Failed: " + e.message)); return; }
  wrap.textContent = "";
  const ok = src.verified;
  wrap.append(el("span", ok ? "verify-ok" : "verify-bad", ok ? "✓ source bytes match recorded hash" : "✗ hash mismatch — source may be tampered"));
  wrap.append(el("div", "subline mono", "recorded: " + (src.recorded_source_hash || "?")));
  wrap.append(el("div", "subline mono", "computed: " + (src.computed_source_hash || "?")));
  wrap.append(el("div", "subline", "capture " + src.capture_id + " · " + src.capture_completeness + " · adapter " + (src.adapter_version || "?")));
  wrap.append(el("pre", "raw", JSON.stringify(src.source, null, 2)));
}

