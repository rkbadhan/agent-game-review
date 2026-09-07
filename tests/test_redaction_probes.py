"""F6 follow-up probe (review 2026-09-06): redaction must cover dictionary
KEYS, not only string values — nested expected/observed values can be arbitrary
data maps whose keys are content.

The probe uses a synthetic ``sk-`` key (32 'A' characters); no real credential
is used or exposed.
"""

from agr.redaction import redact_value

_SECRET = "sk-" + "A" * 32


def test_secret_shaped_dict_key_is_redacted():
    obj = {"expected": {_SECRET: "database intact"}, "check_id": "C1"}
    out, result = redact_value(obj)
    exported = str(out)
    assert _SECRET not in exported
    assert "[REDACTED:openai_key]" in exported
    # The redaction accounting records the key removal too.
    reasons = {r["reason"]: r["count"] for r in result.removed}
    assert reasons.get("openai_key", 0) >= 1


def test_schema_field_names_pass_through_unchanged():
    """Fixed schema field names are never secret-shaped, so key redaction must
    leave them intact — the packet's structure survives traversal."""
    obj = {"moments": [{"check_id": "C1", "structured_facts": [], "anchor_event_ids": ["evt_1"]}]}
    out, _ = redact_value(obj)
    assert out == obj


def test_secret_in_value_is_still_redacted():
    obj = {"observed": f"token {_SECRET} deleted"}
    out, _ = redact_value(obj)
    assert _SECRET not in str(out)
