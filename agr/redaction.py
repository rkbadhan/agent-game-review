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

# Lookbehind for "not in the middle of a word". ``structured_input`` feeds the
# redactor ``json.dumps`` output, where a newline/tab/CR is the two characters
# backslash + n/t/r — so the char before a token on its own line is a LETTER. A
# plain ``(?<![A-Za-z0-9])`` would then miss every token that starts a line of a
# multi-line tool input; the second alternative accepts that escape.
_TOKEN_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[ntr]))"
# Same idea for the start of an identifier-like NAME. ``.`` and ``-`` are also
# excluded so a name can only start at the beginning of a run (this keeps the
# scan linear on long dotted/hyphenated text); ``--api-key`` flags are allowed.
_NAME_START = r"(?:(?<![A-Za-z0-9_.\-])|(?<=--)|(?<=\\[ntr]))"

# Credential-assignment support (see the ``credential_assignment`` rules below).
#
# NAME: an identifier whose last segment is a sensitive word. ``key`` alone is
# far too common (``key: value``, ``sort_key``, ``primary_key``), so a ``…key``
# name must carry a credential-flavoured prefix (``api_key``, ``accessKey``), or
# be an UPPER_SNAKE env-var name (``OPENAI_API_KEY``, ``STRIPE_KEY``) other than
# the obvious non-secrets (``SORT_KEY``, ``PUBLIC_KEY``, …). camelCase names
# (``accessToken``, ``clientSecret``) are covered too. ``pwd`` is deliberately
# absent: ``PWD=/home/…`` is the working directory in every ``env`` dump.
_CRED_NAME = (
    r"(?:(?i:(?:[a-z0-9]+[_\-.]){0,6}(?:password|passwd|passphrase|secret|token"
    r"|(?:api|access|secret|private|auth|signing|encryption|master|client|service|session)[_\-]?key"
    r"|apikey))"
    r"|[a-z][A-Za-z0-9]*(?:Token|Secret|Password|Passwd|Passphrase|ApiKey|AccessKey|SecretKey|PrivateKey)"
    r"|(?!(?:SORT|PRIMARY|FOREIGN|PARTITION|CACHE|PUBLIC|SSH|GPG|PGP|HASH|SHARD|RANGE|INDEX|GROUP"
    r"|ORDER|JSON|DICT|MAP|ROW)_)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_KEY)"
)
_CRED_KEY_ONLY = re.compile(_CRED_NAME)

# Values that are not credentials: literals, type words, language keywords, and
# the name words themselves ("token: token").
_CRED_VALUE_STOPWORDS = frozenset({
    "none", "null", "nil", "true", "false", "undefined", "required", "optional",
    "string", "str", "int", "bool", "boolean", "number", "bytes", "any", "empty",
    "password", "passwd", "secret", "secretstr", "token", "value", "redacted",
    "masked", "hidden", "await", "async", "new", "lambda", "not", "self", "this",
    "cls", "env", "request", "input",
})
_CRED_ATTRIBUTE_CHAIN = re.compile(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+")
_CRED_SNAKE_IDENT = re.compile(r"[A-Za-z]+(?:_[A-Za-z]+)+")
_CRED_SENSITIVE_WORD = re.compile(r"(?i)password|passwd|secret|token|key|pass|env")
_CRED_PLACEHOLDER = re.compile(r"<.*>|\[[A-Za-z_ ]+\]|\$\{?\w+\}?|\{\{.*\}\}", re.S)
_CRED_PROSE_TAIL = re.compile(r"[ \t]+[A-Za-z]{2,}\b(?!\s*[:=])")
_CRED_TRAILING_PUNCT = ")]},;."
_MARKER_PREFIX = "[REDACTED:"
_LONE_MARKER = re.compile(r"\[REDACTED:[^\]]+\]")


def _credential_name(keep: str) -> str:
    return keep.rstrip("\\\"'` \t:=").lower()


def _bare_value_ok(value: str, keep: str, tail: str) -> bool:
    """Whether a BARE (unquoted) assigned value should be treated as a secret.

    Declines code that merely *references* a credential — ``os.environ[...]``,
    ``get_password()``, ``self.token``, ``new_password``, ``${TOKEN}``,
    ``<your-token>`` — type words, small numeric settings (``token: 4096``),
    existing ``[REDACTED:…]`` markers, and prose (``Password: Enter your
    password below``: a ``:`` followed by a plain word and then more words).
    A value that is a plausible literal (``hunter2``, ``abc123``) is redacted:
    under-redacting is the failure this module cannot afford.
    """
    if len(value) < 4 or value[0] in "$<{%([@`":
        return False
    if any(c in value for c in "([<>") or value.lower() in _CRED_VALUE_STOPWORDS:
        return False
    if _CRED_ATTRIBUTE_CHAIN.fullmatch(value):
        return False
    if _CRED_SNAKE_IDENT.fullmatch(value) and _CRED_SENSITIVE_WORD.search(value):
        return False
    name = _credential_name(keep)
    if value.isdigit() and len(value) <= 5 and name.endswith("token"):
        return False
    if keep.rstrip().endswith(":") and value.isalpha() and _CRED_PROSE_TAIL.match(tail):
        return False
    return True


def _quoted_value_ok(value: str) -> bool:
    """Same question for a QUOTED value: any non-trivial string literal is a
    credential, except placeholders (``'<pad>'``, ``"[UNK]"``, ``"${TOKEN}"``),
    stopwords, and text that already carries a redaction marker."""
    if len(value) < 3 or _MARKER_PREFIX in value:
        return False
    v = value.strip()
    return not (_CRED_PLACEHOLDER.fullmatch(v) or v.lower() in _CRED_VALUE_STOPWORDS)


def _assignment_rule(prefix: str, bare: str, lead: str = "") -> "re.Pattern[str]":
    """ONE pattern for ``<prefix><value>`` where the value is ``"…"``, ``'…'``,
    JSON-escaped ``\\"…\\"`` or ``bare`` (a regex). A single pattern means the
    (comparatively costly) prefix scan runs once per text, not once per value
    shape. Quoted values are unbounded (AWS session tokens are routinely 700+
    chars) and tolerate the other quote kind and backslash escapes; ``lead`` is
    an optional lookahead on a quoted value. Groups: ``keep`` (the prefix),
    ``qd``/``qs``/``qe`` (opening quote) with ``vd``/``vs``/``ve`` (value), and
    ``vb`` (bare value)."""
    return re.compile(
        r"(?P<keep>" + prefix + r")(?:"
        r"(?P<qd>\")(?P<vd>" + lead + r"(?:[^\"\\\n]|\\.)+)(?=\")"
        r"|(?P<qs>')(?P<vs>" + lead + r"(?:[^'\\\n]|\\.)+)(?=')"
        r"|(?P<qe>\\\")(?P<ve>" + lead + r"(?:(?!\\\")[^\n])+)(?=\\\")"
        r"|(?P<vb>" + bare + r"))"
    )


# A shell-command window: up to 200 chars that stay inside one simple command
# (no pipe, ``&&``/``;`` chain or JSON-escaped newline between the command word
# and its flag), so ``curl … | sh && docker run -u 1000:1000`` is not one match.
_CMD_WINDOW = r"(?:[^\n|&;\\]|\\(?![ntr])){0,200}?"
_CMD_START = r"(?:\b|(?<=\\[ntr]))"
_CURL_USER = _CMD_START + r"curl\b" + _CMD_WINDOW + r"[ \t](?:-u|--user)(?:[ \t]+|=)"
_MYSQL_P = _CMD_START + r"mysql(?:dump|admin)?\b" + _CMD_WINDOW + r"[ \t]-p"
_PASSWORD_FLAG = (
    r"(?:(?<![\w-])|(?<=\\[ntr]))--(?:password|passwd|token|secret|api-?key|access-?token)[ \t]+"
)
_NOT_MARKER = r"(?!\[REDACTED:)"
_BARE_VALUE = r"[^\s\"'\\=`](?:[^\s\"'\\]|\\(?![\"'nrt]))*"
_BARE_VALUE_FLAG = r"[^\s\"'\\=`-](?:[^\s\"'\\]|\\(?![\"'nrt]))*"

_CRED_PREFIX = (
    _NAME_START + _CRED_NAME + r"(?:\\?[\"'])?\s*[:=]\s*"
)
_BASIC_AUTH = re.compile(
    r"(?i)(?P<keep>(?:\b|(?<=\\[ntr]))authorization(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?basic\s+)"
    r"(?!(?-i:[a-z]+)(?![A-Za-z0-9+/=_\-]))[A-Za-z0-9+/_\-]{8,}(?:=|%3[dD]){0,2}"
)

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
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S])*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\r?\n|\\n)[A-Za-z0-9+/=]{20,}[\s\S]*\Z"
    )),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    # Vendor keys that share the ``sk-`` prefix. Each is matched before the
    # generic OpenAI form below, and every ``sk-`` rule is anchored against a
    # preceding alphanumeric so ordinary hyphenated words ("disk-usage-…",
    # "risk-assessment-…") are not mistaken for a key now that hyphens are
    # allowed inside the body.
    ("langfuse_key", re.compile(_TOKEN_START + r"sk-lf-[A-Za-z0-9_\-]{8,}")),
    ("openrouter_key", re.compile(_TOKEN_START + r"sk-or-(?:v\d+-)?[A-Za-z0-9_\-]{16,}")),
    # Current OpenAI formats (``sk-proj-…``, ``sk-svcacct-…``, ``sk-admin-…``)
    # carry hyphens and underscores in the body, so they need the word-boundary
    # guard ("task-proj-…" contains "sk-proj-"). The legacy plain-alphanumeric
    # form is anchored too: an ID like "task-3f2a1b4c5d6e7f8091a2b3c4-attempt-2"
    # contains "sk-" plus 20+ alphanumerics. Any ``-``/``_`` tail is consumed.
    ("openai_key", re.compile(
        _TOKEN_START + r"sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"
        r"|[A-Za-z0-9]{20,}(?:[_\-][A-Za-z0-9]+)*)"
    )),
    # The lookaheads require a digit/capital in the body so a prose mention
    # ("github_pat_scope_documentation…", "AIzaSomethingSomething…") is not
    # mistaken for a key; real ones are random mixed-case alphanumerics.
    ("github_token", re.compile(
        r"gh[pousr]_[A-Za-z0-9]{20,}"
        r"|" + _TOKEN_START + r"github_pat_(?=[A-Za-z0-9_]*[0-9A-Z])[A-Za-z0-9_]{20,}"
    )),
    ("slack_token", re.compile(
        _TOKEN_START + r"(?:xox[abeprs]-|xapp-\d-)[A-Za-z0-9\-]{10,}"
    )),
    ("google_api_key", re.compile(
        _TOKEN_START + r"AIza(?=[0-9A-Za-z_\-]*[0-9])[0-9A-Za-z_\-]{35,}"
    )),
    ("huggingface_token", re.compile(_TOKEN_START + r"hf_[A-Za-z0-9]{30,}")),
    ("aws_key", re.compile(r"A(?:KIA|SIA)[0-9A-Z]{16}")),
    ("gitlab_token", re.compile(_TOKEN_START + r"glpat-[A-Za-z0-9_\-]{20,}")),
    # Stripe secret/restricted keys, live and test.
    ("stripe_key", re.compile(_TOKEN_START + r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("npm_token", re.compile(_TOKEN_START + r"npm_[A-Za-z0-9]{36}")),
    ("slack_webhook", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,}"
    )),
    # A JWT: base64url header and payload (both start ``eyJ`` = ``{"``) plus an
    # optional signature segment.
    ("jwt", re.compile(
        _TOKEN_START + r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]*)?"
    )),
    ("bearer_token", re.compile(r"(?i)(?:\b|(?<=\\[ntr]))bearer\s+[A-Za-z0-9._\-]{16,}")),
    # ``Authorization: Basic <base64>``. Anchored on the header name: a bare
    # "basic <word>" is ordinary prose, and so is a single lowercase word after
    # it ("Authorization: basic authentication"). The scheme stays so the
    # marker reads naturally; only the credential (with any URL-encoded ``=``
    # padding) is removed.
    ("basic_auth", _BASIC_AUTH),
    # Credentials embedded in a URL (``postgres://user:pass@host``). Placed
    # before the email rule so the userinfo is removed deliberately rather than
    # by the email pattern happening to match ``pass@host``. Requires a colon and
    # a non-empty password in the userinfo, so ``ssh://git@github.com`` is left
    # alone; quotes and whitespace end the run so minified JSON cannot be
    # swallowed.
    ("url_credentials", re.compile(
        r"(?P<keep>(?:(?<![A-Za-z0-9+.\-])|(?<=\\[ntr]))[A-Za-z][A-Za-z0-9+.\-]*://)"
        + _NOT_MARKER + r"[^\s/?#:@\"'`<>\\]*:[^\s/?#\"'`<>\\]+(?=@)"
    )),
    # Command-line credentials. ``curl -u user:pass`` / ``--user user:pass``
    # and ``mysql -pPASS`` only mean "credential" for those tools (``docker run
    # -u 1000:1000`` is a uid:gid), so each is anchored on the command word and
    # scans a bounded window (keeps the cost linear on long text).
    ("cli_credentials", _assignment_rule(
        _CURL_USER, _NOT_MARKER + r"[^\s:\"'`]+:[^\s\"'`]+", lead=r"(?=[^\"'\\\n]*:)")),
    ("cli_credentials", _assignment_rule(
        _MYSQL_P, _NOT_MARKER + r"(?=[^\s\-`'\"])[^\s\"'`)]+")),
    # ``--password hunter2`` (space form; the ``=`` form is the assignment rule).
    ("credential_assignment", _assignment_rule(_PASSWORD_FLAG, _BARE_VALUE_FLAG)),
    # ``NAME = value`` where NAME ends in password/secret/token/…key (env-dump
    # and config shape). Only the VALUE is removed; the name stays as context.
    # Quoted values (``"…"``, ``'…'``, JSON-escaped ``\"…\"`` for JSON inside a
    # JSON string) and bare values are all handled by one pattern; each shape
    # is filtered by ``_quoted_value_ok`` / ``_bare_value_ok`` so code
    # references, placeholders and prose are left alone. A name must END in the
    # sensitive word, so ``max_tokens=4096`` and ``password_policy: strict`` do
    # not match.
    ("credential_assignment", _assignment_rule(_CRED_PREFIX, _BARE_VALUE)),
    # Anchored at the start of the local-part run (not ``\b``) so the scan is
    # linear on long runs of ``[\w.+-]`` that contain no ``@``.
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+\b")),
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


def _apply(reason: str, pattern: "re.Pattern[str]", text: str) -> tuple[str, int]:
    """Substitute one pattern, returning ``(new_text, replacements_made)``.

    A pattern with a ``keep`` group keeps that prefix (the assignment name, URL
    scheme, auth scheme) and removes the rest. Assignment patterns (see
    :func:`_assignment_rule`) carry a value group per shape; for
    ``credential_assignment`` the value is only removed if the matching filter
    accepts it (``_quoted_value_ok`` / ``_bare_value_ok``; trailing closing
    punctuation of a bare value stays outside the marker). Values that already
    carry a typed marker are left alone. The count reflects replacements
    actually made.
    """
    marker = f"[REDACTED:{reason}]"
    names = pattern.groupindex
    if "keep" not in names:
        return pattern.subn(marker, text)
    count = 0

    if "vb" not in names:
        def repl_simple(m: "re.Match[str]") -> str:
            nonlocal count
            count += 1
            return m.group("keep") + marker
        return pattern.sub(repl_simple, text), count

    filtered = reason == "credential_assignment"

    def repl(m: "re.Match[str]") -> str:
        nonlocal count
        for quote, group in (("qd", "vd"), ("qs", "vs"), ("qe", "ve")):
            value = m.group(group)
            if value is not None:
                if _MARKER_PREFIX in value or (filtered and not _quoted_value_ok(value)):
                    return m.group(0)
                count += 1
                return m.group("keep") + m.group(quote) + marker
        value = m.group("vb")
        if filtered:
            core = value.rstrip(_CRED_TRAILING_PUNCT)
            if not _bare_value_ok(core, m.group("keep"), m.string[m.end():]):
                return m.group(0)
            count += 1
            return m.group("keep") + marker + value[len(core):]
        count += 1
        return m.group("keep") + marker

    return pattern.sub(repl, text), count


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
        out, n = _apply(reason, pattern, out)
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

    def bump(reason: str, n: int = 1) -> None:
        merged[reason] = merged.get(reason, 0) + n

    def mask_leaves(o: object) -> object:
        """Mask every string (and long number) under a credential-named key."""
        if isinstance(o, str):
            if _LONE_MARKER.fullmatch(o):
                return o
            if o and o.strip().lower() not in _CRED_VALUE_STOPWORDS:
                bump("credential_assignment")
                return "[REDACTED:credential_assignment]"
            return o
        if isinstance(o, bool):
            return o
        if isinstance(o, (int, float)):
            if len(str(o)) >= 6:
                bump("credential_assignment")
                return "[REDACTED:credential_assignment]"
            return o
        if isinstance(o, dict):
            return {k: mask_leaves(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            out = [mask_leaves(v) for v in o]
            return tuple(out) if isinstance(o, tuple) else out
        return o

    def mask_under_key(key: object, value: object) -> object:
        """``{"password": "hunter2"}`` / ``{"Authorization": "Basic …"}``: the
        name and value sit in separate slots, so the text rules cannot see them
        together. A credential-named key masks its value (typed markers already
        written by the text pass are kept, so the reason is not lost)."""
        if not isinstance(key, str):
            return value
        if _CRED_KEY_ONLY.fullmatch(key):
            return mask_leaves(value)
        if key.lower() in ("authorization", "proxy-authorization") and isinstance(value, str):
            prefix = "Authorization: "
            text, n = _apply("basic_auth", _BASIC_AUTH, prefix + value)
            if n:
                bump("basic_auth", n)
                return text[len(prefix):]
        return value

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
                orig_key = k
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
                out[key] = mask_under_key(orig_key, walk(v))
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
