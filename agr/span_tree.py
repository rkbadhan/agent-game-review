"""Shared span-tree -> trajectory flattener (spec §5.3, docs/span-adapters.md).

OTel GenAI spans, a Langfuse export, and a LangSmith run tree are not per-turn
logs like Harbor's or pi's — they are **span trees**: nested, timestamped
records with parent ids and overlapping durations. This module is the one
place that shape gets turned into an ordered ATIF ``steps`` list; each source
adapter (``agr/ingest_otel.py``, ``agr/ingest_langfuse.py``,
``agr/ingest_langsmith.py``) normalizes its raw export into :class:`NormalizedSpan`
objects and delegates to :func:`flatten_span_tree` here. Everything below is a
pure, stateless re-shaping of what the normalized spans already contain — it
never invents content, and every place it must make a conservative choice
(uncaptured content, an ambiguous root, an unrecognised span kind) is recorded
as a warning, per ``docs/adapters.md``.

Design mirrors ``agr/ingest_harbor.py``'s fan-out and termination-selection
patterns; read that module's docstring first if this one is unclear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# Provenance stamp for this module's mapping logic. Source adapters that
# delegate here should mention it in their own version bump notes when the
# flattening behaviour changes materially, since it affects every span-tree
# source, not just one.
SPAN_TREE_VERSION = "span-tree-0.2"

# The small, normalized span-kind vocabulary this module understands. A source
# adapter's field map is responsible for reducing its own operation names
# (OTel's ``gen_ai.operation.name``, a Langfuse observation ``type``, a
# LangSmith ``run_type``) onto these before calling :func:`flatten_span_tree` —
# the flattener itself knows nothing about any one source's vocabulary.
#
#   "agent"  — an agent loop / sub-agent invocation (structural only: no step
#              of its own, but it establishes the actor context for its
#              descendants — see _resolve_actor).
#   "model"  — a model call (chat/completion/generation) -> model_output.
#   "tool"   — a tool/function/retriever invocation -> tool_call + tool_result.
#   "chain"  — a generic grouping span with no stronger category (a Langfuse
#              "span"/"chain"/"event", an OTel span with no recognised
#              gen_ai.operation.name) -> environment_observation when it
#              carries captured content, else skipped with a warning.
#   "other"  — anything else; same handling as "chain".
_SPAN_KINDS = {"agent", "model", "tool", "chain", "other"}

_STATUSES = {"ok", "error", "unknown"}


@dataclass
class NormalizedSpan:
    """One span, reduced to the shape the flattener needs.

    This is the contract every span-tree source adapter targets. Fields it
    cannot fill in stay at their honest default (``None`` / ``False``) rather
    than a guessed value — the flattener turns exactly those gaps into
    capability declarations and warnings, never into invented content.

    * ``span_id`` / ``parent_id`` — the tree structure. ``parent_id=None``
      (or an id not present in the span set) marks a root candidate.
    * ``start_time`` — anything totally orderable (epoch seconds, an OTel
      nanosecond timestamp, a monotonically increasing index). Only used for
      ordering, never rendered.
    * ``kind`` — one of :data:`_SPAN_KINDS` above; the source's own field map
      decides this, the flattener only consumes it.
    * ``name`` — a human-readable operation label: the tool name, the agent
      name, the model name, or the span's own operation name as a fallback.
    * ``status`` — ``"ok"`` / ``"error"`` / ``"unknown"``. ``"unknown"`` is the
      honest default when the source recorded neither an explicit success nor
      an explicit error (many OTel spans default to UNSET, not OK) — it must
      never be upgraded to ``"ok"`` by the flattener.
    * ``input_captured`` / ``output_captured`` — whether the source actually
      captured (or attempted to capture) this span's input/output content,
      independent of whether ``inputs``/``outputs`` end up empty. This is what
      drives the honest capability declarations (the "content catch" in
      docs/span-adapters.md): a tool span with a name and a clean status but
      ``output_captured=False`` produces a ``tool_result`` with
      ``status: "unknown"``, never a guessed ``"ok"``.
    * ``inputs`` / ``outputs`` — the captured payload (any JSON-ish value:
      string, dict, list); only read when the matching ``*_captured`` flag is
      True.
    * ``usage`` — ``{"input_tokens": int, "output_tokens": int, ...}`` when the
      source recorded token counts for this span, else ``None``.
    * ``cost_usd`` — a computed/observed dollar cost for this span, if any.
    * ``session_id`` — the source's session/conversation/thread grouping key
      (``gen_ai.conversation.id``, Langfuse ``sessionId``, a LangSmith
      trace/thread id), when the source carries one.
    * ``call_order`` — an explicit ordering hint for spans that share the same
      (or overlapping) ``start_time`` — e.g. the index of a tool call inside
      the model output that spawned it. ``None`` means "no better order than
      start_time is known"; see *parallel tool spans* in the design doc.
    * ``attributes`` — anything else the source captured that a specific
      adapter wants to carry through (e.g. an explicit ``exit_code``, a
      decoded JSON-error field) without widening this dataclass. The
      flattener reads exactly one key from it, ``"exit_code"``; everything
      else is source-adapter-specific and opaque here.
    """

    span_id: str
    parent_id: Optional[str]
    start_time: float
    kind: str
    name: str = ""
    status: str = "unknown"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    input_captured: bool = False
    output_captured: bool = False
    inputs: Any = None
    outputs: Any = None
    usage: Optional[dict] = None
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    call_order: Optional[int] = None
    attributes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _SPAN_KINDS:
            raise ValueError(
                f"NormalizedSpan {self.span_id!r}: unknown kind {self.kind!r}; "
                f"must be one of {sorted(_SPAN_KINDS)} — the source adapter's "
                f"field map must reduce its own vocabulary to these first"
            )
        if self.status not in _STATUSES:
            raise ValueError(
                f"NormalizedSpan {self.span_id!r}: unknown status {self.status!r}; "
                f"must be one of {sorted(_STATUSES)}"
            )


@dataclass
class FlattenResult:
    """What :func:`flatten_span_tree` returns.

    ``steps`` is the ordered ATIF-shaped step list. ``capabilities`` declares
    only what this span tree genuinely carries (everything else is left for
    ``apply_capability_defaults`` to default to ``unavailable``, exactly as
    every other adapter already relies on). ``meta`` carries the facts a
    source adapter needs to finish building the ``run``/``task`` blocks
    (resolved model/agent names, session id, instruction/final-output text)
    without re-deriving them from the spans itself.
    """

    steps: list[dict]
    capabilities: dict[str, str]
    warnings: list[str]
    meta: dict[str, Any]


def _stringify(value: Any) -> str:
    """Flatten a captured input/output payload to display/matching text.

    Spans carry arbitrary JSON-ish content (a plain string, a GenAI messages
    array, a dict of tool arguments) — never assumed to be one shape.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]


def _hoist_path(inputs: Any) -> Optional[str]:
    """Pull a path-like argument out of captured tool inputs, if present.

    Mirrors the other adapters' hoisting of the most analysis-relevant
    argument (``ingest_harbor.py:_hoist_argument``); kept narrow on purpose.
    """
    if isinstance(inputs, dict):
        for key in ("path", "file_path", "filename"):
            val = inputs.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _sort_key(span: NormalizedSpan) -> tuple:
    # start_time is the primary order; call_order (when the source captured
    # the model's own tool-call sequence) breaks ties for spans that overlap
    # or share a start_time — e.g. parallel tool calls fired from one model
    # turn. span_id is the final, deterministic tiebreak.
    return (span.start_time, span.call_order if span.call_order is not None else 0, span.span_id)


def _find_roots(spans: list[NormalizedSpan]) -> list[NormalizedSpan]:
    ids = {s.span_id for s in spans}
    return [s for s in spans if s.parent_id is None or s.parent_id not in ids]


def _resolve_actor(
    span: NormalizedSpan,
    by_id: dict[str, NormalizedSpan],
    root_id: str,
) -> str:
    """The span-tree analogue of isSidechain / parent_tool_use_id pairing.

    Walks up the parent chain to the nearest ancestor of kind "agent" (never
    including the span itself). No such ancestor, or the nearest one IS the
    root, means this span belongs to the main agent loop. A nearer, non-root
    agent ancestor means this span was produced inside a subagent invocation —
    labelled by that subagent's own name so distinct subagents stay
    distinguishable, and NEVER merged into the main agent's own trajectory
    (the design doc's explicit call-out; unlike the Claude adapter, which
    drops sidechains entirely for lack of parent-linkage, span trees carry the
    linkage natively, so the content is kept, just correctly attributed).
    """
    seen: set[str] = set()
    node_id = span.parent_id
    while node_id is not None and node_id in by_id and node_id not in seen:
        seen.add(node_id)
        node = by_id[node_id]
        if node.kind == "agent":
            if node.span_id == root_id:
                return "main_agent"
            return f"subagent:{node.name or node.span_id}"
        node_id = node.parent_id
    return "main_agent"


def flatten_span_tree(spans: list[NormalizedSpan]) -> FlattenResult:
    """Flatten a normalized span tree into an ordered ATIF ``steps`` list.

    Pure function: no I/O, no invented content. See the module docstring and
    ``docs/span-adapters.md`` ("The shared flattener") for the responsibilities
    this implements.
    """
    if not spans:
        raise ValueError("flatten_span_tree: no spans given")

    warnings: list[str] = []
    by_id = {s.span_id: s for s in spans}

    roots = _find_roots(spans)
    if not roots:
        # Defensive only: every span's parent_id pointed at another span in
        # the set, i.e. a cycle. Fall back to the earliest-starting span so a
        # malformed export still produces a document, honestly flagged.
        warnings.append(
            "no root span found (every span's parent_id resolves to another span in "
            "the set — a cycle); the earliest-starting span was used as the root"
        )
        root = min(spans, key=_sort_key)
    else:
        root = min(roots, key=_sort_key)
        extra_roots = [r for r in roots if r.span_id != root.span_id]
        if extra_roots:
            warnings.append(
                f"{len(extra_roots)} additional top-level span(s) besides the root "
                f"({root.span_id!r}) were found; only the earliest-starting one is "
                f"treated as the run's task/termination source, the rest are "
                f"flattened as ordinary steps in time order"
            )

    ordered = sorted(spans, key=_sort_key)

    steps: list[dict] = []
    seq = 0

    def add(kind: str, actor: str, span: NormalizedSpan, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        payload.setdefault("provenance", "observed")
        step = {"step_id": f"sp{seq}", "kind": kind, "actor": actor, **payload}
        if span.timestamp is not None:
            step["timestamp"] = span.timestamp
            step["start_time"] = span.timestamp
        if span.end_timestamp is not None:
            step["end_time"] = span.end_timestamp
        steps.append(step)

    # --- task_received: the root span's captured input -----------------------
    if root.input_captured:
        instruction_text = _stringify(root.inputs)
        add("task_received", "harness", root, content=instruction_text)
    else:
        instruction_text = ""
        add("task_received", "harness", root,
            content="[root span input was not captured by this source]")
        warnings.append(
            f"root span {root.span_id!r} carries no captured input — task "
            f"instruction is empty; confirm the contract (docs/adapters.md)"
        )

    # --- body: every non-root, non-agent-kind span, in time order ------------
    # Agent-kind spans (root included) never get a step of their own — they
    # are pure structural context (see _resolve_actor); only their tool/model
    # descendants produce trajectory steps.
    tool_input_flags: list[bool] = []
    tool_output_flags: list[bool] = []
    model_output_flags: list[bool] = []
    model_usage_flags: list[bool] = []
    model_timestamp_flags: list[bool] = []
    saw_body_span = False
    retry_count = 0
    unmapped_count = 0
    last_op: Optional[NormalizedSpan] = None  # most recently emitted tool/model span
    subagent_names: set[str] = set()
    session_id: Optional[str] = root.session_id
    model_name: Optional[str] = None

    for span in ordered:
        if span.span_id == root.span_id:
            continue
        if session_id is None and span.session_id is not None:
            session_id = span.session_id

        if span.kind == "agent":
            # Structural only — establishes actor context for descendants,
            # never a step. Its name is recorded so subagent grouping is
            # visible in run metadata even though no step names it directly.
            subagent_names.add(span.name or span.span_id)
            continue

        saw_body_span = True
        actor = _resolve_actor(span, by_id, root.span_id)

        # Framework-level retry recognition (design doc "Framework-level
        # retries"): a sibling span with the SAME parent, kind, and name as
        # the tool/model span most recently processed, where that prior span
        # ended in error, is the framework repeating a failed operation —
        # not a fresh action the agent chose to take again. Deliberately
        # narrow: it keys off immediate time-adjacency (the previous
        # tool/model span processed, not any earlier one under the same
        # parent) so an agent's own deliberate
        # repeat call to the same tool later in the run — after something
        # else happened in between, or after a clean success — is never
        # misclassified as a retry.
        is_retry = (
            last_op is not None
            and last_op.parent_id == span.parent_id
            and last_op.kind == span.kind
            and last_op.name == span.name
            and last_op.status == "error"
        )

        if span.kind == "tool":
            tool_input_flags.append(span.input_captured)
            tool_output_flags.append(span.output_captured)
            tool_name = span.name or "tool"
            # Design doc rule: no error AND no captured output is "unknown",
            # never a guessed "passed" — a clean-looking span with nothing
            # actually captured is not evidence of success.
            if span.status == "error":
                result_status = "error"
            elif span.status == "ok" and span.output_captured:
                result_status = "ok"
            else:
                result_status = "unknown"

            if is_retry:
                retry_count += 1
                payload: dict[str, Any] = {"tool": tool_name, "status": result_status}
                if span.output_captured:
                    payload["content"] = _stringify(span.outputs)
                elif span.input_captured:
                    payload["content"] = _stringify(span.inputs)
                exit_code = span.attributes.get("exit_code")
                if exit_code is not None:
                    payload["exit_code"] = exit_code
                add("retry", actor, span, **payload)
            else:
                call_payload: dict[str, Any] = {"tool": tool_name}
                if span.input_captured:
                    call_payload["content"] = _stringify(span.inputs)
                    hoisted_path = _hoist_path(span.inputs)
                    if hoisted_path:
                        call_payload["path"] = hoisted_path
                if span.usage or span.cost_usd is not None:
                    call_payload["cost"] = _cost_dict(span)
                add("tool_call", actor, span, **call_payload)

                result_payload: dict[str, Any] = {"tool": tool_name, "status": result_status}
                if span.output_captured:
                    result_payload["content"] = _stringify(span.outputs)
                exit_code = span.attributes.get("exit_code")
                if exit_code is not None:
                    result_payload["exit_code"] = exit_code
                if span.error_message and span.status == "error" and not span.output_captured:
                    # Even without a captured result payload, an explicit
                    # error message (e.g. OTel's error.type / a span status
                    # description) is genuinely source-supplied evidence —
                    # kept, not invented.
                    result_payload["content"] = span.error_message
                add("tool_result", "tool", span, **result_payload)
            last_op = span

        elif span.kind == "model":
            model_output_flags.append(span.output_captured)
            model_usage_flags.append(
                isinstance(span.usage, dict)
                and isinstance(span.usage.get("input_tokens"), (int, float))
                and not isinstance(span.usage.get("input_tokens"), bool)
            )
            model_timestamp_flags.append(
                span.timestamp is not None and span.end_timestamp is not None
            )
            if span.name:
                model_name = span.name

            if is_retry:
                retry_count += 1
                payload = {"status": "error" if span.status == "error" else
                           ("ok" if span.output_captured else "unknown")}
                payload.update({"generation_event": True, "generation_id": span.span_id,
                                "model": span.name or None})
                if span.output_captured:
                    payload["content"] = _stringify(span.outputs)
                if span.usage or span.cost_usd is not None:
                    payload["cost"] = _cost_dict(span)
                add("retry", actor, span, **payload)
            else:
                payload = {"generation_event": True, "generation_id": span.span_id,
                           "model": span.name or None}
                if span.output_captured:
                    payload["content"] = _stringify(span.outputs)
                if span.usage or span.cost_usd is not None:
                    payload["cost"] = _cost_dict(span)
                add("model_output", actor, span, **payload)
            last_op = span

        else:  # "chain" / "other" — generic grouping spans
            has_content = span.input_captured or span.output_captured
            if has_content:
                text = _stringify(span.outputs) if span.output_captured else _stringify(span.inputs)
                add("environment_observation", actor, span, content=text)
            else:
                unmapped_count += 1
                warnings.append(
                    f"span {span.span_id!r} ({span.name or span.kind!r}, kind={span.kind!r}) "
                    f"carried no captured input/output — skipped, not represented as a step"
                )
            # Generic spans never participate in retry-adjacency matching —
            # only tool/model repeats count as a framework retry.

    # --- termination: the root span's status selects the terminal event ------
    # No agent activity at all (zero non-root, non-agent spans) is a protocol
    # failure, not a completion, exactly as the Harbor/pi/Claude adapters
    # already treat a trajectory with zero agent steps.
    final_output_text: Optional[str] = None
    if not saw_body_span:
        add("run_failed", "harness", root, provenance="synthetic",
            content="[span tree closed — no agent activity beyond the root span was observed]",
            termination_reason="agent_protocol_failure")
        warnings.append("span tree has no tool/model spans beneath the root (agent never acted)")
    else:
        if root.output_captured:
            final_output_text = _stringify(root.outputs)
        if root.status == "error":
            error_type = (root.error_type or "").lower()
            if "timeout" in error_type or "deadline" in error_type:
                add("run_timed_out", "harness", root, provenance="synthetic",
                    content=final_output_text or f"[root span reported a timeout: {root.error_type}]")
            else:
                add("run_failed", "harness", root, provenance="synthetic",
                    content=final_output_text or f"[root span reported an error: {root.error_type or root.error_message or 'unknown'}]")
        else:
            if root.status == "unknown":
                warnings.append(
                    f"root span {root.span_id!r} status was not explicitly set (neither ok "
                    f"nor error); treated as run_completed based on the absence of an error "
                    f"signal — this reflects EXECUTION completion, not a confirmed task success"
                )
            add("run_completed", "harness", root, provenance="synthetic",
                content=final_output_text or "[span tree closed normally — no error status observed on the root span]")

    # --- capabilities: only what this span tree genuinely carries ------------
    def _capability_from_flags(flags: list[bool]) -> Optional[str]:
        if not flags:
            return None
        if all(flags):
            return "complete"
        if any(flags):
            return "partial"
        return "unavailable"

    capabilities: dict[str, str] = {}
    tool_calls_level = _capability_from_flags(tool_input_flags)
    if tool_calls_level is not None:
        capabilities["tool_calls"] = tool_calls_level
        if tool_calls_level != "complete":
            captured = sum(tool_input_flags)
            warnings.append(
                f"tool_calls declared {tool_calls_level!r}: {captured}/{len(tool_input_flags)} "
                f"tool span(s) captured their arguments; the rest carry only name/timing "
                f"(the OTel/Langfuse/LangSmith content opt-in was not enabled for them)"
            )
    tool_results_level = _capability_from_flags(tool_output_flags)
    if tool_results_level is not None:
        capabilities["tool_results"] = tool_results_level
        if tool_results_level != "complete":
            captured = sum(tool_output_flags)
            warnings.append(
                f"tool_results declared {tool_results_level!r}: {captured}/{len(tool_output_flags)} "
                f"tool span(s) captured their result payload — a clean status with no captured "
                f"output is reported as 'unknown', never a guessed pass"
            )

    messages_flags = model_output_flags
    if messages_flags:
        messages_level = _capability_from_flags(messages_flags)
    elif root.output_captured:
        messages_level = "final_only"
    else:
        messages_level = None
    if messages_level is not None:
        capabilities["messages"] = messages_level
        if messages_level not in ("complete",):
            warnings.append(
                f"messages declared {messages_level!r}: model span output content was not "
                f"fully captured by this source"
            )

    generation_usage_level = _capability_from_flags(model_usage_flags)
    if generation_usage_level is not None:
        capabilities["generation_usage"] = generation_usage_level
    generation_timestamps_level = _capability_from_flags(model_timestamp_flags)
    if generation_timestamps_level is not None:
        capabilities["generation_timestamps"] = generation_timestamps_level

    if retry_count:
        warnings.append(
            f"{retry_count} framework-level retry span(s) recognised (a sibling span repeating "
            f"an immediately-preceding failed operation under the same parent) — mapped to "
            f"'retry' steps, not double-counted as fresh actions"
        )

    meta: dict[str, Any] = {
        "root_span_id": root.span_id,
        "root_name": root.name,
        "session_id": session_id,
        "model": model_name,
        "instruction": instruction_text,
        "final_output": final_output_text,
        "subagent_names": sorted(subagent_names),
        "retry_count": retry_count,
        "unmapped_span_count": unmapped_count,
        "saw_agent_activity": saw_body_span,
    }

    return FlattenResult(steps=steps, capabilities=capabilities, warnings=warnings, meta=meta)


def _cost_dict(span: NormalizedSpan) -> dict:
    cost: dict[str, Any] = dict(span.usage) if span.usage else {}
    if span.cost_usd is not None:
        cost["cost_usd"] = span.cost_usd
    return cost
