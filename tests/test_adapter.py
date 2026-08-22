"""The shared adapter interface (Increment 2, spec §5.3).

Every harness plugs into one contract: an AdapterResult, the Adapter protocol,
the capability-defaulting honesty rule, and a registry the CLI dispatches
through. These tests pin that contract and prove the pi adapter satisfies it.
"""

import json

import pytest

from agr.adapter import (
    DEFAULT_CAPABILITIES,
    Adapter,
    AdapterResult,
    adapter_names,
    apply_capability_defaults,
    get_adapter,
)


def _pi_session(tmp_path) -> str:
    """A minimal but valid pi session JSONL, written to disk."""
    header = {"type": "session", "version": 3, "id": "sess-a1", "timestamp": "2026-07-28T10:00:00Z"}
    user = {"type": "message", "id": "e1", "parentId": header["id"],
            "message": {"role": "user", "content": "Write hello to /out.txt"}}
    asst = {"type": "message", "id": "e2", "parentId": "e1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                        "provider": "anthropic", "model": "claude-x", "stopReason": "endTurn"}}
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in (header, user, asst)), encoding="utf-8")
    return str(p)


def test_apply_capability_defaults_missing_is_unavailable():
    caps = apply_capability_defaults(None)
    assert caps == DEFAULT_CAPABILITIES
    assert all(level == "unavailable" for level in caps.values())


def test_apply_capability_defaults_declared_overrides_only_what_is_declared():
    caps = apply_capability_defaults({"messages": "complete"})
    assert caps["messages"] == "complete"
    # Everything undeclared stays honest.
    assert caps["filesystem"] == "unavailable"
    # The default dict is not mutated by the merge.
    assert DEFAULT_CAPABILITIES["messages"] == "unavailable"


def test_registry_lists_and_resolves_pi():
    assert "pi" in adapter_names()
    pi = get_adapter("pi")
    assert pi.name == "pi"
    assert isinstance(pi, Adapter)


def test_registry_order_is_support_priority_harbor_first():
    """Eval-framework sources come first; interactive sessions after."""
    names = adapter_names()
    assert names[0] == "harbor"
    assert "pi" in names


def test_adapters_carry_their_own_version_stamp():
    """Provenance is per-adapter, never a shared global default."""
    from agr.ingest_harbor import HARBOR_ADAPTER_VERSION
    from agr.ingest_pi import PI_ADAPTER_VERSION

    assert get_adapter("harbor").version == HARBOR_ADAPTER_VERSION
    assert get_adapter("pi").version == PI_ADAPTER_VERSION
    assert HARBOR_ADAPTER_VERSION.startswith("harbor-")
    assert PI_ADAPTER_VERSION.startswith("pi-")


def test_get_unknown_adapter_raises_with_helpful_message():
    with pytest.raises(KeyError, match="unknown adapter"):
        get_adapter("does-not-exist")


def test_pi_adapter_returns_adapter_result(tmp_path):
    from agr.ingest_pi import PI_ADAPTER, PiAdapterResult

    # PiAdapterResult is now an alias of the shared type.
    assert PiAdapterResult is AdapterResult

    result = PI_ADAPTER.convert(_pi_session(tmp_path), task_id="t1")
    assert isinstance(result, AdapterResult)
    assert result.doc["steps"]
    assert isinstance(result.warnings, list)
    # The old entry_count now lives in meta.
    assert result.meta["entry_count"] >= 1
