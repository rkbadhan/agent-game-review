"""Shared adapter interface (spec §5.3 interoperability).

An adapter converts one harness's session log into the ATIF document that agr
ingests (``docs/atif-schema.json``). This module is the single shape every
adapter shares:

* ``AdapterResult`` — what an adapter returns (doc + warnings + meta).
* ``Adapter`` — the protocol an adapter satisfies.
* ``apply_capability_defaults`` — the "missing capability → unavailable"
  honesty rule, defined once so it cannot drift between adapters and ingest.
* a small registry the CLI dispatches through, so adding a harness is one file
  and one registry entry, not a new CLI verb each time.

There is no universal converter: every harness names things differently, so
each needs its own adapter. What they map *onto* is this stable contract.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# Capabilities every adapter reports on. Anything a source omits defaults to
# "unavailable": missing observability is stated, never assumed present. A
# detector that depends on an unavailable capability reports "not evaluated"
# rather than guessing.
DEFAULT_CAPABILITIES = {
    "messages": "unavailable",
    "tool_calls": "unavailable",
    "tool_results": "unavailable",
    "filesystem": "unavailable",
    "process_state": "unavailable",
    "network_state": "unavailable",
    "compaction_boundary": "unavailable",
    "pre_post_compaction_context": "unavailable",
    "sidecar_state": "unavailable",
    "verifier_code": "unavailable",
}


def apply_capability_defaults(declared: dict | None) -> dict:
    """Merge a source's declared capabilities over the all-unavailable default.

    The one definition of the honesty rule: whatever a source does not declare
    stays ``unavailable``. Ingest profiles every doc through here, so an adapter
    that omits a capability gets the honest default for free.
    """
    caps = dict(DEFAULT_CAPABILITIES)
    if declared:
        caps.update(declared)
    return caps


@dataclass
class AdapterResult:
    """What every adapter returns.

    ``doc`` is the ATIF document (``docs/atif-schema.json``). ``warnings``
    records every place the mapping made a conservative choice — an unmapped
    entry skipped, an instruction guessed, a missing verifier — so imperfect
    mappings stay honest. ``meta`` carries adapter-specific extras (e.g. an
    entry count) that the pipeline does not read but a caller may want.
    """

    doc: dict
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    """One harness -> ATIF. Adapters are stateless; ``convert`` is pure.

    ``source`` is adapter-specific (a log path, a span export, ...). The keyword
    arguments are the identity and evidence the core never invents (see
    ``docs/adapters.md``): supply them, or they are honestly absent.

    ``version`` is the adapter's own provenance stamp; every document it emits
    carries it top-level so ingestion records which adapter produced the capture.
    """

    name: str
    version: str

    def convert(
        self,
        source: Any,
        *,
        task_id: str | None = None,
        instruction: str | None = None,
        run_id: str | None = None,
        verifier: dict | None = None,
        sweep_id: str | None = None,
        configuration_id: str | None = None,
    ) -> AdapterResult: ...


# Built-in adapters, resolved lazily so importing this module never drags in an
# adapter's dependencies and never risks an import cycle. Adding a harness is
# one entry here plus one file. Registry order is support priority: eval-
# framework sources first (Harbor / Terminal-Bench 2.0 is the primary path),
# then interactive-session sources (pi), then span-tree sources (otel,
# langfuse, langsmith — docs/span-adapters.md), newest last.
_BUILTIN_ADAPTERS: dict[str, tuple[str, str]] = {
    "harbor": ("agr.ingest_harbor", "HARBOR_ADAPTER"),
    "pi": ("agr.ingest_pi", "PI_ADAPTER"),
    "claude": ("agr.ingest_claude", "CLAUDE_ADAPTER"),
    "otel": ("agr.ingest_otel", "OTEL_ADAPTER"),
    "langfuse": ("agr.ingest_langfuse", "LANGFUSE_ADAPTER"),
    "langsmith": ("agr.ingest_langsmith", "LANGSMITH_ADAPTER"),
}


def adapter_names() -> list[str]:
    """Registered adapter names in support priority order (registry order).

    Used for CLI ``--adapter`` choices and help text, so the primary source is
    always listed first.
    """
    return list(_BUILTIN_ADAPTERS)


def get_adapter(name: str) -> Adapter:
    """Resolve a registered adapter by name, importing its module on demand."""
    try:
        module_name, attr = _BUILTIN_ADAPTERS[name]
    except KeyError:
        raise KeyError(
            f"unknown adapter {name!r}; available: {', '.join(adapter_names())}"
        ) from None
    module = importlib.import_module(module_name)
    return getattr(module, attr)
