"""Redaction and untrusted-content isolation (spec §7.4, §16.1).

Every string that reaches a model-assisted component is trace-derived and
therefore **untrusted data** — task instructions, agent messages, tool output,
repo text, web pages. Two things happen to it here, *before* any model call
(spec line 834: "Redaction and untrusted-content isolation occur before any
model-facing processing"):

1. **Redaction** — obvious secrets and PII are removed and replaced with a typed
   marker, and a *redaction map* records what was removed and why. If redaction
   removes evidence a claim would need, the claim becomes unavailable rather than
   guessed (spec §7.4) — that is enforced downstream by fact validation (Stage G),
   which recomputes from the immutable source, not from the redacted text.

2. **Isolation** — content is wrapped as *data*, never instructions. The real
   boundary is structural: the packet builder puts every trace string inside a
   JSON data field and the system prompt declares those fields untrusted (spec
   §21: "No tools, typed packets, strict instruction/data boundary"). This module
   additionally *detects* embedded-instruction markers for telemetry — it records
   them, it never acts on them, and it deliberately does **not** rewrite content to
   "sanitise" injections (that would corrupt evidence). Isolation is what makes an
   injection inert; detection is only observability.

The point is not that a model can never be fooled by injected text — it is that a
fooled reviewer still cannot change the review, because its facts are recomputed
against the source and it is given no tools and no instruction authority.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import version

# Secret / PII patterns. Conservative and typed — each carries the reason that is
# recorded in the redaction map and shown in the marker. Ordered most-specific
# first so a token isn't misclassified as a generic high-entropy blob.
_SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]

# Markers that *look like* an attempt to give the reviewer instructions. Recorded
# for telemetry only; never acted on and never removed (see module docstring).
_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore (?:all |the )?(?:previous|prior|above) instructions"
    r"|disregard (?:the |all )?(?:above|previous)"
    r"|system\s*:|you are now|new instructions|mark (?:this|the) run"
    r"|assistant\s*:|override|jailbreak)"
)


@dataclass
class RedactionResult:
    """Redacted text plus the map of what was removed (spec §7.4)."""

    text: str
    removed: list[dict] = field(default_factory=list)          # [{reason, count}]
    injection_markers: int = 0                                 # observability only
    redaction_version: str = version.REDACTION_VERSION

    def to_dict(self) -> dict:
        return {
            "removed": list(self.removed),
            "injection_markers": self.injection_markers,
            "redaction_version": self.redaction_version,
        }


def redact(text: str) -> RedactionResult:
    """Remove secrets/PII, replacing each with a typed ``[REDACTED:<reason>]`` marker.

    Records a count per reason in the redaction map, and (separately) counts
    embedded-instruction markers without altering them.
    """
    if not text:
        return RedactionResult(text=text or "")
    counts: dict[str, int] = {}
    out = text
    for reason, pattern in _SECRET_PATTERNS:
        out, n = pattern.subn(f"[REDACTED:{reason}]", out)
        if n:
            counts[reason] = counts.get(reason, 0) + n
    removed = [{"reason": r, "count": counts[r]} for r in sorted(counts)]
    markers = len(_INJECTION_MARKERS.findall(text))
    return RedactionResult(text=out, removed=removed, injection_markers=markers)


def redact_all(sections: dict[str, str]) -> tuple[dict[str, str], RedactionResult]:
    """Redact a set of named text sections, merging their redaction maps.

    Returns the redacted sections plus one combined :class:`RedactionResult`
    (its ``text`` field is unused for the combined form).
    """
    out: dict[str, str] = {}
    merged: dict[str, int] = {}
    markers = 0
    for name, value in sections.items():
        r = redact(value or "")
        out[name] = r.text
        for entry in r.removed:
            merged[entry["reason"]] = merged.get(entry["reason"], 0) + entry["count"]
        markers += r.injection_markers
    combined = RedactionResult(
        text="",
        removed=[{"reason": k, "count": merged[k]} for k in sorted(merged)],
        injection_markers=markers,
    )
    return out, combined
