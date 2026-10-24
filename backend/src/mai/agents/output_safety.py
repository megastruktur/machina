"""Shared output-safety projection (C2: zero secret persistence).

ONE projection for every output-to-receipt/history seam: tool output that
reaches a durable receipt (``job_steps.receipt`` / the ``step.completed``
``job_events`` row), the persisted model history, or a stream projection is
redacted BEFORE any size bound is applied, so a truncated tail can never
split a secret around the marker or leak a partial secret prefix.

The projection layers, in order:

1. **Resolved-credential value forms** — every serialized form (raw,
   JSON-escaped ASCII, JSON-escaped non-ASCII) of each resolved secret
   value is replaced. The ONE producer of those forms is
   :func:`mai.capabilities.dispatch._secret_output_forms`; this module
   reuses it so receipt/history scrubbing can never drift from the
   dispatcher's own output boundary.
2. **Secret-shaped token patterns** — synthetic credentials that were
   never resolved through the credential store (a tool that echoes an
   upstream API response) have no resolved value to scrub, so the
   projection also recognizes the token shapes itself: ``sk-``-style API
   keys, ``Bearer``/``Basic`` authorization credentials, ``password=`` /
   ``api_key=``-style key/value pairs, PEM blocks, JWTs, and URL userinfo
   credentials.
3. **Explicit size bound, applied LAST** — redaction runs first over the
   whole text; only then is the bounded projection cut. A secret fully in
   the kept region is already replaced, one fully in the dropped tail is
   gone, and neither can be split by the cut.

Nested documents and JSON-encoded string payloads (double-encoded tool
output, echoed envelopes) are traversed recursively before the bound, so
an escaped copy of a secret inside a nested string cannot survive.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

from mai.capabilities.dispatch import _secret_output_forms

__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_PROJECTION_CHARS",
    "REDACTED",
    "bound_text",
    "project_document",
    "project_text",
    "redact_document",
    "redact_text",
    "redact_values",
]

#: The replacement marker. Receipt/event consumers assert its bracketed
#: form when proving redaction happened (S1).
REDACTED: Final[str] = "[REDACTED]"

#: Explicit protection limit: the durable receipt projection is cut to at
#: most this many CHARACTERS (the seam's former bound, now applied after
#: redaction). The model-facing projection is NOT re-bounded here: the
#: dispatcher already applied its own output budget (and its
#: ``truncated``/``omitted_bytes`` receipt fields must keep describing
#: exactly that bound).
MAX_PROJECTION_CHARS: Final[int] = 32768

#: Explicit protection limit: recursive traversal depth for nested
#: documents and JSON-encoded string payloads. Deeper structures fall back
#: to whole-subtree text redaction instead of recursive expansion.
MAX_JSON_DEPTH: Final[int] = 8

#: Explicit protection limit: only texts up to this size are probed as
#: JSON documents. The dispatcher bounds executor output to its output
#: budget (≤ 64 KiB) before the seam sees it, so this guard only fails
#: closed for pathologically composed direct callers.
MAX_JSON_PROBE_CHARS: Final[int] = 262144

# ---------------------------------------------------------------------------
# pattern layer (secret-shaped tokens with no resolved value)
# ---------------------------------------------------------------------------

#: ``sk-``-style provider API keys (incl. ``sk-proj-``/``sk-ant-`` families
#: via the shared prefix charset). Unbounded tail: a > 4096-char key is
#: still matched in full.
_API_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bsk-[A-Za-z0-9_-]{20,}\b"
)

#: ``Bearer`` credentials (header- or inline-style). The scheme word and
#: its whitespace are preserved; only the token is replaced.
_BEARER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?P<prefix>bearer)\s+(?P<token>[A-Za-z0-9._~+/=-]{8,})"
)

#: ``Authorization: Basic <base64>`` header-style credentials.
_BASIC_AUTH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?P<prefix>authorization\s*:\s*basic)\s+(?P<token>[A-Za-z0-9+/=]{8,})"
)

#: key/value credentials: ``password=``, ``api_key:``, ``"secret": "..."``.
#: ``q1`` carries the JSON/Python-style closing quote of the KEY so quoted
#: keys match too; quoted values keep their quote characters so JSON
#: documents stay parseable after redaction; values never cross a line
#: boundary (multiline credentials are covered by the PEM pattern).
_KEY_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|api_?key|secret|token|access_?token"
    r"|refresh_?token|auth_?token|client_?secret|private_?key)"
    r"(?P<q1>\"?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"'&,;<>]{6,})"
)

#: PEM blocks (multiline): the whole block is one secret.
_PEM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----",
    re.DOTALL,
)

#: JWTs (three dot-separated base64url segments; unbounded segments, so an
#: oversized > 4096-char token is matched in full).
_JWT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{5,}\b"
)

#: URL userinfo credentials: the password slot of ``scheme://user:pass@``.
_URL_USERINFO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<user>[^\s@/:]+):(?P<password>[^\s@/]+)@"
)

#: Pattern layer in application order. PEM runs before the key/value layer
#: so ``private_key: -----BEGIN ...`` is replaced once, as a whole block.
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _PEM_PATTERN,
    _JWT_PATTERN,
    _API_KEY_PATTERN,
    _BEARER_PATTERN,
    _BASIC_AUTH_PATTERN,
    _KEY_VALUE_PATTERN,
    _URL_USERINFO_PATTERN,
)


def _replace_pattern_match(match: re.Match[str]) -> str:
    """Keep the identifying prefix of a match; replace only the secret."""
    groups = match.groupdict()
    if "token" in groups:
        return f"{groups['prefix']} {REDACTED}"
    if "password" in groups and "scheme" in groups:
        return f"{groups['scheme']}{groups['user']}:{REDACTED}@"
    if "value" in groups:
        q = groups.get("q1", "")
        value = groups["value"]
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            return f"{groups['key']}{q}{groups['sep']}\"{REDACTED}\""
        return f"{groups['key']}{q}{groups['sep']}{REDACTED}"
    return REDACTED


# ---------------------------------------------------------------------------
# the projection layers
# ---------------------------------------------------------------------------


def redact_values(text: str, secrets: Sequence[str]) -> str:
    """Replace every resolved-credential value form in ``text``.

    Layer 1 only: raw plus JSON-escaped forms of each resolved value,
    longest first. This is the seam's value-scrubbing primitive; the
    pattern layer (:func:`redact_text`) adds secret-SHAPED tokens on top.
    """
    for value in _secret_output_forms(list(secrets)):
        text = text.replace(value, REDACTED)
    return text


def redact_text(text: str, secrets: Sequence[str] = ()) -> str:
    """Fully redact one string: resolved value forms, then token patterns.

    Runs over the WHOLE text — callers must not truncate first, or a cut
    can split a secret around the marker (the repaired seam bug).
    """
    text = redact_values(text, secrets)
    for pattern in _PATTERNS:
        text = pattern.sub(_replace_pattern_match, text)
    return text


def redact_document(
    value: object,
    secrets: Sequence[str] = (),
    *,
    depth: int = 0,
) -> object:
    """Recursively redact a parsed document.

    Mappings, lists and tuples are traversed; strings are redacted via
    :func:`redact_text`; a string that itself parses as a JSON document
    (double-encoded payload) is unwrapped, redacted recursively, and
    re-encoded — up to :data:`MAX_JSON_DEPTH`, after which the string is
    redacted as plain text (fail-safe, never a crash).
    """
    if isinstance(value, str):
        if depth < MAX_JSON_DEPTH and len(value) <= MAX_JSON_PROBE_CHARS:
            # Redact the literal text FIRST: at this level the encoded
            # document's quotes are real characters, so key-context
            # patterns (``"password": "..."``) still fire. Then unwrap the
            # (still-valid) JSON document and recurse for anything the
            # text layer structurally missed.
            value = redact_text(value, secrets)
            stripped = value.lstrip()
            if stripped[:1] in ("{", "["):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    return json.dumps(
                        redact_document(parsed, secrets, depth=depth + 1),
                        ensure_ascii=False,
                    )
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            key: redact_document(item, secrets, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_document(item, secrets, depth=depth + 1) for item in value]
    return value


def bound_text(text: str, limit: int = MAX_PROJECTION_CHARS) -> str:
    """Cut redacted text to ``limit`` characters (explicit, honest bound).

    MUST run after redaction: every complete secret in ``text`` is already
    replaced, so the cut can neither expose nor split one.
    """
    if len(text) <= limit:
        return text
    return text[:limit]


def project_document(
    text: str,
    secrets: Sequence[str] = (),
    *,
    limit: int = MAX_PROJECTION_CHARS,
) -> object:
    """The durable receipt projection of one tool-output text.

    Redacts the raw text FIRST (value forms + token patterns), then — only
    if the redacted text still parses as a JSON document — redacts the
    parsed structure recursively (nested / JSON-encoded payloads) and
    returns the document; otherwise returns the redacted text. Any
    oversized result is bounded LAST, so a truncated tail never splits or
    leaks a partial secret prefix (S1 criterion 1).
    """
    safe_text = redact_text(text, secrets)
    if len(safe_text) <= MAX_JSON_PROBE_CHARS:
        stripped = safe_text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(safe_text)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                document = redact_document(parsed, secrets)
                if len(json.dumps(document, ensure_ascii=False)) <= limit:
                    return document
    return bound_text(safe_text, limit)


def project_text(
    text: str,
    secrets: Sequence[str] = (),
    *,
    max_chars: int | None = None,
) -> str:
    """The model-history/stream projection of one tool-output text.

    Redaction FIRST (value forms + token patterns), then — only if the
    redacted text parses as a JSON document — the same recursive
    parse-unwrap traversal as :func:`project_document`: JSON-escaped
    inner payloads (``"encoded": "{\\"password\\": ...}"``) lose their
    escaping on parse, so key-context patterns fire there too, and the
    re-serialized document (``ensure_ascii=False``) stays valid JSON.
    NO size bound by default: the model-facing text was already bounded
    by the dispatcher's output budget, and the result document's
    ``output_truncated``/``output_omitted_bytes`` fields describe exactly
    that upstream bound — re-bounding here would desynchronize them.
    A caller-supplied ``max_chars`` (explicit protection limit) applies
    :func:`bound_text` AFTER redaction, so a cut never splits a secret.
    """
    safe_text = redact_text(text, secrets)
    if len(safe_text) <= MAX_JSON_PROBE_CHARS:
        stripped = safe_text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(safe_text)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                safe_text = json.dumps(
                    redact_document(parsed, secrets), ensure_ascii=False
                )
    if max_chars is not None:
        return bound_text(safe_text, max_chars)
    return safe_text
