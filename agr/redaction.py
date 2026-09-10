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
    # The full PEM block, header through footer — matched and removed FIRST so
    # no narrower pattern below can pick off a coincidental match inside the
    # base64 body and leave a fragment of real key material sitting next to a
    # spurious marker. ``[\s\S]*?`` (not ``.``) so the body's newlines are
    # covered without needing DOTALL, and the match is non-greedy so multiple
    # keys in one text are redacted individually rather than as one span.
    #
    # Second alternative: a TRUNCATED key that never got its footer (a capture
    # cut off mid-transfer) — the header immediately followed by a
    # base64-shaped run (20+ contiguous base64-alphabet characters, i.e. no
    # whitespace breaking it into words) proving this is real key material and
    # not a bare mention of the header in prose (a doc explaining the PEM
    # format, a log line echoing it with no key ever having been present):
    # ordinary sentences have spaces breaking them into short words, so they
    # cannot satisfy the 20+-char unbroken run this branch requires.
    #
    # Once that shape is confirmed, everything else in the text is swept to
    # end-of-string (``[\s\S]*\Z``) rather than requiring every remaining line
    # to individually be 20+ chars ending exactly where the capture stopped.
    # A truncated capture is rarely alone at the tail of its string: upstream
    # truncation appends its own marker (e.g. ``" …[truncated]"`` /
    # ``"\n[truncated]"``) after cutting the body, and the cut itself can land
    # mid-line, leaving a final line shorter than 20 chars. Both used to make
    # this alternative fail to match at all — under-redacting is the one
    # failure mode this module cannot afford, so once the header+body shape is
    # confirmed the rest of the string is discarded wholesale.
    #
    # The separator after the header also accepts a literal ``\n`` escape
    # (backslash + "n", not a real newline) alongside a real line break: a key
    # captured inside structured tool input travels through ``json.dumps``
    # before redaction runs, which serializes its embedded newlines as that
    # two-character escape — a real newline never appears in the text redact()
    # actually sees for that case.
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\r?\n|\\n)[A-Za-z0-9+/=]{20,}[\s\S]*\Z"
    )),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
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


def redact_value(obj: object) -> tuple[object, RedactionResult]:
    """Traversal redaction: redact EVERY string in a nested structure (AGR-06).

    Packets carry strings in many shapes — check names and expected/observed
    values, structured facts, revision and error payloads — and a keyed pass
    alone cannot guarantee none is missed. This walks dicts, lists, and tuples
    and redacts every string VALUE it finds.

    F1 follow-up: dictionary KEYS are redacted too. Fixed schema field names
    ("moments", "check_id", …) are never secret-shaped, so passing them
    through the same pattern pass changes nothing — but nested expected/observed
    values can be arbitrary data maps whose keys are content (a synthetic
    ``sk-…`` key survived the old keys-untouched traversal). Keys that ARE
    secret-shaped are redacted and accounted exactly like values.

    Review 2026-09-07: two DISTINCT secret-shaped keys both mapped to the same
    ``[REDACTED:<reason>]`` marker, so one map entry silently overwrote the
    other — an evidence-preservation bug introduced by key rewriting. Redacted
    keys now get stable unique discriminators (``[REDACTED:<reason>#2]``,
    …) so every entry of a content-bearing map survives redaction.
    Returns ``(new_obj, combined_map)``.
    """
    merged: dict[str, int] = {}
    markers = 0

    def walk(o: object) -> object:
        nonlocal markers
        if isinstance(o, str):
            r = redact(o)
            for entry in r.removed:
                merged[entry["reason"]] = merged.get(entry["reason"], 0) + entry["count"]
            markers += r.injection_markers
            return r.text
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(k, str):
                    r = redact(k)
                    for entry in r.removed:
                        merged[entry["reason"]] = merged.get(entry["reason"], 0) + entry["count"]
                    markers += r.injection_markers
                    k = r.text
                key = k
                if isinstance(key, str) and key in out:
                    # Collision: distinct source keys redacted to the same
                    # marker (or genuinely duplicated keys). Discriminate
                    # rather than overwrite — no map entry is silently lost.
                    n = 2
                    candidate = f"{key}#{n}"
                    while candidate in out:
                        n += 1
                        candidate = f"{key}#{n}"
                    key = candidate
                out[key] = walk(v)
            return out
        if isinstance(o, (list, tuple)):
            out = [walk(v) for v in o]
            return type(o)(out) if isinstance(o, tuple) else out
        return o

    new_obj = walk(obj)
    combined = RedactionResult(
        text="",
        removed=[{"reason": k, "count": merged[k]} for k in sorted(merged)],
        injection_markers=markers,
    )
    return new_obj, combined
