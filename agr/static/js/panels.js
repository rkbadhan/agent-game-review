"use strict";

// --- resizable side panels ---------------------------------------------------
//   The inbox and the evidence panel are both reference material a reviewer
//   reads next to the main stage, and how much of each they want depends on the
//   run and the screen. Widths are dragged (or nudged with the arrow keys, since
//   a drag handle nobody can reach by keyboard is not a control), clamped so
//   neither panel can swallow the reading column, and remembered per browser.
const PANEL_LIMITS = { queue: [220, 520, 296], evidence: [260, 620, 358] };
function panelWidth(name) {
  const [min, max, fallback] = PANEL_LIMITS[name];
  const stored = Number(localStorage.getItem("agr-" + name + "-w"));
  return stored ? Math.min(max, Math.max(min, stored)) : fallback;
}
function setPanelWidth(name, px, persist) {
  const [min, max] = PANEL_LIMITS[name];
  const width = Math.min(max, Math.max(min, Math.round(px)));
  document.documentElement.style.setProperty("--" + name + "-w", width + "px");
  if (persist) localStorage.setItem("agr-" + name + "-w", String(width));
  return width;
}
function initResizer(id, name, measure) {
  const handle = $(id);
  const drag = e => setPanelWidth(name, measure(e), false);
  handle.addEventListener("pointerdown", e => {
    e.preventDefault();
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    const stop = () => {
      handle.classList.remove("dragging");
      setPanelWidth(name, panelWidthNow(name), true);
      handle.removeEventListener("pointermove", drag);
      handle.removeEventListener("pointerup", stop);
    };
    handle.addEventListener("pointermove", drag);
    handle.addEventListener("pointerup", stop);
  });
  handle.addEventListener("keydown", e => {
    const step = e.shiftKey ? 48 : 16;
    if (e.key === "ArrowLeft") setPanelWidth(name, panelWidthNow(name) + (name === "queue" ? -step : step), true);
    else if (e.key === "ArrowRight") setPanelWidth(name, panelWidthNow(name) + (name === "queue" ? step : -step), true);
    else return;
    e.preventDefault();
  });
  handle.addEventListener("dblclick", () => {
    localStorage.removeItem("agr-" + name + "-w");
    setPanelWidth(name, PANEL_LIMITS[name][2], false);
  });
}
function panelWidthNow(name) {
  return parseInt(getComputedStyle(document.documentElement)
    .getPropertyValue("--" + name + "-w"), 10) || PANEL_LIMITS[name][2];
}
setPanelWidth("queue", panelWidth("queue"), false);
setPanelWidth("evidence", panelWidth("evidence"), false);
initResizer("#queue-resizer", "queue", e => e.clientX);
initResizer("#evidence-resizer", "evidence", e => window.innerWidth - e.clientX);

