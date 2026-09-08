"use strict";

// --- help + keyboard ---------------------------------------------------------
$("#help-button").addEventListener("click", () => openModal("#help-modal"));
$("#versions-button").addEventListener("click", () => { state.view = "versions"; render(); });
$("#fleet-button").addEventListener("click", () => { state.view = "fleet"; render(); });
// §4.19 glossary + first-session guidance.
$("#glossary-button").addEventListener("click", openGlossary);
$("#intro-glossary").addEventListener("click", () => { dismissIntro(); openGlossary(); });
$("#intro-dismiss").addEventListener("click", dismissIntro);
$("#queue-toggle").addEventListener("click", () => $("#queue").classList.toggle("open"));
document.addEventListener("click", e => { if (!e.target.closest(".disp-wrap")) { state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open"); } });
document.addEventListener("keydown", e => {
  if (e.target.matches("input, select, textarea")) return;
  const k = e.key.toLowerCase();
  const onMoments = state.view === "review" && state.chapter === "moments";
  if (e.key === "Escape") {
    closeTrace(); closeModals(); state.dispOpen = false; const m = $("#disp-menu"); if (m) m.classList.remove("open");
    if (state.view === "compare" || state.view === "versions" || state.view === "fleet"
        || state.view === "sibling") {
      state.view = "review"; render();
    }
  }
  else if (k === "v") { state.view = state.view === "versions" ? "review" : "versions"; render(); }
  else if (k === "f") { state.view = state.view === "fleet" ? "review" : "fleet"; render(); }
  else if (k === "g") openGlossary();
  else if (!state.review) return;
  else if (e.key === "[") moveChapter(-1);
  else if (e.key === "]") moveChapter(1);
  else if (k === "j") { if (onMoments) moveMoment(1); }
  else if (k === "k") { if (onMoments) moveMoment(-1); }
  else if (k === "e") { if (onMoments && currentMoment()) focusEvidence(); }
  else if (k === "c") {
    if (state.view === "compare") { state.view = "review"; render(); }
    else if ((state.review.available_reviews || []).length >= 2) { state.view = "compare"; render(); }
    else toast("Compare needs a second review of this run");
  }
  else if (k === "d") toggleDisposition();
  else if (k === "n") gotoNextUnhandled();
  else if (k === "t") openTrace(null);
  else if (e.key === "?") openModal("#help-modal");
});
