"use strict";

// --- §4.21 product instrumentation -------------------------------------------
//   Events describe how a *reviewer* moves through the product — never what a
//   run contained. The API allowlists event names and property keys and rejects
//   anything shaped like free text, so this client sends identifiers and enum
//   tokens only: no statements, notes, artifact values, or tool output. Events
//   are batched and flushed on an idle tick so instrumentation never sits in
//   front of a user action, and a failed flush is dropped rather than retried
//   forever — analytics must not become a reason a review cannot be recorded.
const SESSION_ID = "sess_" + Math.random().toString(36).slice(2, 10);
let eventQueue = [], flushTimer = null;
function track(event, properties) {
  eventQueue.push({ event, session_id: SESSION_ID, properties: properties || {} });
  if (flushTimer) return;
  flushTimer = setTimeout(flushEvents, 1200);
}
async function flushEvents() {
  flushTimer = null;
  const batch = eventQueue.splice(0, eventQueue.length);
  if (!batch.length) return;
  try { await apiPost("/events", { events: batch, actor: state.reviewer }); }
  catch (e) { /* analytics is best-effort and never blocks review work */ }
}
window.addEventListener("beforeunload", () => { if (eventQueue.length) flushEvents(); });
