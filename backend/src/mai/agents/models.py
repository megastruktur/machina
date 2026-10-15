"""Provider/model connection configuration and client resolution (C06 rule 1).

``06-model-config`` owns the *shape* of a model connection (endpoint) and a
model profile, plus the single place where a validated selection becomes a
PydanticAI model bound to the configured OpenAI-compatible endpoint.

Normative anchors:

- contracts.md **C06 rule 1**: provider endpoint config and model selection are
  separate; secret refs resolve at call time; model payloads use only
  allowlisted settings compatible with the endpoint; a deliberate capability
  failure produces a useful structured error (C01 envelope vocabulary).
- contracts.md **C03 rule 6**: credentials resolve from ``.env`` through the
  mandatory first resolver (``mai.credentials.env.EnvResolver``); a connection
  stores only the credential *reference* (the canonical profile schema shape
  ``[A-Z][A-Z0-9_]{0,127}``). An intentionally unauthenticated local endpoint
  needs no fake credential: the adapter's documented non-empty placeholder key
  is used and the ambient ``OPENAI_API_KEY`` environment variable is never
  consulted (names resolve only from the configured source).
- IDEA.md §8 "OpenAI-compatible connections": a *connection* (base URL,
  credential reference, timeouts, optional headers) is separated from a *model
  profile* (model identifier, output limits, sampling/reasoning settings).
  OpenAI compatibility does not imply universal support for tool calls,
  streaming, structured outputs or reasoning parameters, so the connection
  declares which optional capabilities the endpoint supports and bind-time
  validation refuses settings the endpoint does not declare.

Secret hygiene: no dataclass in this module ever holds resolved credential
material; ``repr`` paths are value-free by construction and pinned by tests
(``backend/tests/test_models.py``). The resolved material exists only inside
the constructed OpenAI SDK client at call time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from mai.credentials.env import EnvFileError, EnvResolver

__all__ = [
    "API_PLACEHOLDER_KEY",
    "CAPABILITY_FIELDS",
    "CONNECTION_FIELDS",
    "MAX_BASE_URL_CHARS",
    "MAX_HEADER_VALUE_CHARS",
    "MAX_HEADERS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "PROFILE_FIELDS",
    "REASONING_EFFORTS",
    "SCHEMA_VERSION",
    "EndpointCapabilities",
    "ModelConfigError",
    "ModelConfigErrorCode",
    "ModelConfigFieldError",
    "ModelConnection",
    "ModelProfile",
    "ModelSelection",
    "ProviderFailureKind",
    "build_model",
    "classify_provider_error",
    "main",
    "model_settings",
    "parse_connection",
    "parse_profile",
    "validate_selection",
]

SCHEMA_VERSION = 1

CONNECTION_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "base_url",
        "credential_ref",
        "timeout_seconds",
        "headers",
        "capabilities",
    }
)
CAPABILITY_FIELDS = frozenset({"tool_calls", "reasoning"})
PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "connection",
        "model",
        "temperature",
        "top_p",
        "seed",
        "max_tokens",
        "reasoning_effort",
        "context_limit",
        "output_limit",
    }
)

#: Sampling/reasoning settings MAI allowlists for model profiles (C06 rule 1:
#: allowlisted settings only). Reasoning effort values follow the OpenAI
#: chat-completions ``reasoning_effort`` parameter vocabulary.
REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})

#: Connection name / profile name / profile→connection reference: lowercase
#: slug, stable across stores and API routes.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Credential-reference shape, frozen by the canonical profile schema
#: (``mai.contracts.server`` ``_CREDENTIAL_REF_RE``, C03 rule 6). Kept local:
#: the schema module is the shape owner and is outside this card's allowlist;
#: a divergence would be a review finding, not a silent import of a private
#: symbol (same pattern as ``mai.credentials.env``).
_CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

#: Model identifier: OpenAI-style names, gateway-prefixed names
#: (``openai/gpt-4o-mini``) and local-server names (``qwen3-8b``) all fit; no
#: whitespace or control characters.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

#: Custom connection headers: RFC-token-ish names only. Header names are
#: normalized (``-`` → ``_``) before the credential-token check, so
#: ``X-Api-Key`` is classified as credential material just like ``api_key``.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")

#: Headers that always carry credential material — rejected outright, a
#: connection configures *authentication* only through ``credential_ref``.
_FORBIDDEN_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie"}
)

#: Substrings that mark an unknown field/header as inline credential material.
#: Any unknown key carrying one is rejected with the specific
#: ``inline_credential_forbidden`` code instead of generic ``unknown_field``
#: (same vocabulary as the canonical profile schema).
_CREDENTIAL_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "passphrase",
    "api_key",
    "access_key",
    "credential",
)

MAX_BASE_URL_CHARS = 2048
MAX_HEADERS = 8
MAX_HEADER_VALUE_CHARS = 256
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 600.0

#: Bounds for the profile settings actually forwarded to the endpoint.
_MAX_TEMPERATURE = 2.0
_MAX_TOP_P = 1.0
_MAX_SEED = 2**31 - 1
_MAX_TOKENS = 1_000_000
_MAX_LIMIT = 2**31 - 1

#: Non-empty placeholder the OpenAI SDK requires even for unauthenticated
#: local endpoints; PydanticAI's own ``OpenAIProvider`` inserts exactly this
#: value when a base URL is configured without a key. Using it explicitly
#: (instead of ``api_key=None``) guarantees the ambient ``OPENAI_API_KEY``
#: environment variable can never leak into a connection that intentionally
#: declares no credential reference (C03 rule 6).
API_PLACEHOLDER_KEY = "api-key-not-set"


class ModelConfigErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_DOCUMENT = "invalid_document"
    UNPARSABLE_DOCUMENT = "unparsable_document"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_FIELD = "invalid_field"
    INLINE_CREDENTIAL_FORBIDDEN = "inline_credential_forbidden"
    #: A known option that the selected endpoint does not declare support for
    #: (C06 rule 1: settings must be compatible with the endpoint; the failure
    #: is deliberate and classified, never silently dropped).
    UNSUPPORTED_OPTION = "unsupported_option"
    #: A profile names a different connection than the one it is bound to.
    INVALID_SELECTION = "invalid_selection"
    #: A connection declares a credential reference but resolution was
    #: requested without a configured credential source.
    MISSING_RESOLVER = "missing_resolver"


@dataclass(frozen=True, slots=True)
class ModelConfigFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: ModelConfigErrorCode
    field: str
    detail: str


class ModelConfigError(ValueError):
    """All rejections for one connection/profile/selection document.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never document values — so the exception is safe to log, store and return
    to a browser (C01 rule 3: no secret material in any error body).
    """

    def __init__(self, errors: Sequence[ModelConfigFieldError]) -> None:
        self.errors: tuple[ModelConfigFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"model config rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


@dataclass(frozen=True, slots=True)
class EndpointCapabilities:
    """Optional endpoint features the connection declares (C06 rule 1).

    Everything defaults to *false*: an OpenAI-compatible endpoint is assumed
    plain chat-only until the administrator declares more, and bind-time
    validation refuses settings that require undeclared capabilities.
    """

    tool_calls: bool = False
    reasoning: bool = False


@dataclass(frozen=True, slots=True)
class ModelConnection:
    """Validated endpoint connection; ``credential_ref`` is a reference."""

    schema_version: int
    name: str
    base_url: str
    credential_ref: str | None
    timeout_seconds: float | None
    headers: tuple[tuple[str, str], ...]
    capabilities: EndpointCapabilities


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Validated model profile: identifier, limits and allowlisted settings.

    ``connection`` names the endpoint this profile is meant to run against;
    :func:`validate_selection` enforces the pairing explicitly so a stored
    profile can never be silently resolved against an unrelated connection.
    """

    schema_version: int
    name: str
    connection: str
    model: str
    temperature: float | None
    top_p: float | None
    seed: int | None
    max_tokens: int | None
    reasoning_effort: str | None
    context_limit: int | None
    output_limit: int | None


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """A validated (connection, profile) pair — the unit handed to
    :func:`build_model`."""

    connection: ModelConnection
    profile: ModelProfile


# ---------------------------------------------------------------------------
# Document validation
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _looks_like_credential(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(token in normalized for token in _CREDENTIAL_TOKENS)


def _key_error(key: str, path: str) -> ModelConfigFieldError:
    """Classify one out-of-place key: credential material vs plain unknown."""
    field = f"{path}.{key}" if path else key
    if _looks_like_credential(key):
        return ModelConfigFieldError(
            ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
            field,
            "inline credential fields are forbidden; credentials resolve by "
            "reference (C03 rule 6)",
        )
    return ModelConfigFieldError(
        ModelConfigErrorCode.UNKNOWN_FIELD,
        field,
        "unknown field (unknown fields are rejected, never ignored)",
    )


def _top_level_keys(
    document: Mapping[object, object],
    known: frozenset[str],
    errors: list[ModelConfigFieldError],
) -> None:
    for key in document:
        if not isinstance(key, str) or key not in known:
            label = key if isinstance(key, str) else repr(key)
            errors.append(_key_error(label, ""))


def _required_fields(
    document: Mapping[object, object],
    required: Sequence[str],
    errors: list[ModelConfigFieldError],
) -> None:
    for name in required:
        if name not in document:
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.MISSING_FIELD,
                    name,
                    "required field is missing",
                )
            )


def _schema_version(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> None:
    value = document.get("schema_version")
    if value is None:
        return  # already reported as missing
    if not _is_int(value) or value != SCHEMA_VERSION:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.SCHEMA_VERSION_UNSUPPORTED,
                "schema_version",
                f"only schema version {SCHEMA_VERSION} is supported",
            )
        )


def _parse_name(document: Mapping[object, object], field: str) -> str | None:
    value = document.get(field)
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        return None
    return value


def _parse_base_url(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> str | None:
    raw = document.get("base_url")
    if not isinstance(raw, str) or not raw.strip():
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "base_url",
                "must be a non-empty http(s) URL string",
            )
        )
        return None
    if len(raw) > MAX_BASE_URL_CHARS:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "base_url",
                f"exceeds {MAX_BASE_URL_CHARS} characters",
            )
        )
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "base_url",
                "must be an absolute http(s) URL with a host",
            )
        )
        return None
    if parts.username or parts.password:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
                "base_url",
                "userinfo credentials in the URL are forbidden; credentials "
                "resolve by reference (C03 rule 6)",
            )
        )
        return None
    if parts.query or parts.fragment:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "base_url",
                "query strings and fragments are not part of an endpoint "
                "base URL",
            )
        )
        return None
    return raw.rstrip("/")


def _parse_credential_ref(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> str | None:
    value = document.get("credential_ref")
    if value is None:
        return None
    if not isinstance(value, str) or not _CREDENTIAL_REF_RE.fullmatch(value):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "credential_ref",
                "must match [A-Z][A-Z0-9_]{0,127} (frozen by the canonical "
                "profile schema) or be null for an unauthenticated endpoint",
            )
        )
        return None
    return value


def _parse_timeout(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> float | None:
    value = document.get("timeout_seconds")
    if value is None:
        return None
    if not _is_number(value) or not (
        MIN_TIMEOUT_SECONDS <= float(value) <= MAX_TIMEOUT_SECONDS
    ):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "timeout_seconds",
                f"must be a number between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS} seconds, or null",
            )
        )
        return None
    return float(value)


def _parse_headers(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> tuple[tuple[str, str], ...]:
    raw = document.get("headers")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "headers",
                "must be a mapping of header name to literal value",
            )
        )
        return ()
    if len(raw) > MAX_HEADERS:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "headers",
                f"at most {MAX_HEADERS} custom headers are allowed",
            )
        )
    parsed: list[tuple[str, str]] = []
    for key, value in raw.items():
        field = f"headers.{key}" if isinstance(key, str) else "headers"
        if not isinstance(key, str) or not _HEADER_NAME_RE.fullmatch(key):
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.INVALID_FIELD,
                    field,
                    "header name must match [A-Za-z0-9-]{1,128}",
                )
            )
            continue
        if key.lower() in _FORBIDDEN_HEADER_NAMES:
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
                    field,
                    "authentication headers are forbidden; credentials "
                    "resolve by reference (C03 rule 6)",
                )
            )
            continue
        if _looks_like_credential(key):
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
                    field,
                    "credential-carrying headers are forbidden; credentials "
                    "resolve by reference (C03 rule 6)",
                )
            )
            continue
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_HEADER_VALUE_CHARS
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
        ):
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.INVALID_FIELD,
                    field,
                    f"header value must be a printable string of 1–"
                    f"{MAX_HEADER_VALUE_CHARS} characters",
                )
            )
            continue
        parsed.append((key, value))
    return tuple(parsed)


def _parse_capabilities(
    document: Mapping[object, object], errors: list[ModelConfigFieldError]
) -> EndpointCapabilities:
    raw = document.get("capabilities")
    if raw is None:
        return EndpointCapabilities()
    if not isinstance(raw, Mapping):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "capabilities",
                "must be a mapping of capability flags",
            )
        )
        return EndpointCapabilities()
    for key in raw:
        if not isinstance(key, str) or key not in CAPABILITY_FIELDS:
            label = key if isinstance(key, str) else repr(key)
            errors.append(_key_error(label, "capabilities"))
    tool_calls = raw.get("tool_calls")
    if tool_calls is not None and not isinstance(tool_calls, bool):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "capabilities.tool_calls",
                "must be a boolean",
            )
        )
        tool_calls = None
    reasoning = raw.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, bool):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "capabilities.reasoning",
                "must be a boolean",
            )
        )
        reasoning = None
    return EndpointCapabilities(
        tool_calls=bool(tool_calls), reasoning=bool(reasoning)
    )


def parse_connection(document: Mapping[str, object]) -> ModelConnection:
    """Validate one connection document into a :class:`ModelConnection`.

    Raises :class:`ModelConfigError` collecting every field rejection;
    messages are value-free.
    """
    errors: list[ModelConfigFieldError] = []
    if not isinstance(document, Mapping):
        raise ModelConfigError(
            (
                ModelConfigFieldError(
                    ModelConfigErrorCode.INVALID_DOCUMENT,
                    "",
                    "connection document must be a mapping",
                ),
            )
        )
    _top_level_keys(document, CONNECTION_FIELDS, errors)
    _required_fields(document, ("schema_version", "name", "base_url"), errors)
    _schema_version(document, errors)

    name = _parse_name(document, "name")
    if name is None and "name" in document:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "name",
                "must match [a-z0-9][a-z0-9_-]{0,63}",
            )
        )
    base_url = _parse_base_url(document, errors)
    credential_ref = _parse_credential_ref(document, errors)
    timeout_seconds = _parse_timeout(document, errors)
    headers = _parse_headers(document, errors)
    capabilities = _parse_capabilities(document, errors)
    if errors:
        raise ModelConfigError(errors)
    assert isinstance(name, str) and base_url is not None  # required above
    return ModelConnection(
        schema_version=SCHEMA_VERSION,
        name=name,
        base_url=base_url,
        credential_ref=credential_ref,
        timeout_seconds=timeout_seconds,
        headers=headers,
        capabilities=capabilities,
    )


def _parse_optional_number(
    document: Mapping[object, object],
    field: str,
    *,
    low: float,
    high: float,
    integer: bool,
    errors: list[ModelConfigFieldError],
) -> float | int | None:
    value = document.get(field)
    if value is None:
        return None
    ok = _is_int(value) if integer else _is_number(value)
    if not ok or not (low <= float(value) <= high):  # type: ignore[arg-type]
        kind = "integer" if integer else "number"
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                field,
                f"must be a {kind} between {low} and {high}, or null",
            )
        )
        return None
    return value  # type: ignore[return-value]


def parse_profile(document: Mapping[str, object]) -> ModelProfile:
    """Validate one model-profile document into a :class:`ModelProfile`.

    Settings outside the MAI allowlist are rejected as unknown fields
    (credential-shaped keys with the specific
    ``inline_credential_forbidden`` code); endpoint *capability* gates are
    enforced later by :func:`validate_selection`, which knows the connection.
    """
    errors: list[ModelConfigFieldError] = []
    if not isinstance(document, Mapping):
        raise ModelConfigError(
            (
                ModelConfigFieldError(
                    ModelConfigErrorCode.INVALID_DOCUMENT,
                    "",
                    "model profile document must be a mapping",
                ),
            )
        )
    _top_level_keys(document, PROFILE_FIELDS, errors)
    _required_fields(
        document, ("schema_version", "name", "connection", "model"), errors
    )
    _schema_version(document, errors)

    name = _parse_name(document, "name")
    if name is None and "name" in document:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "name",
                "must match [a-z0-9][a-z0-9_-]{0,63}",
            )
        )
    connection = _parse_name(document, "connection")
    if connection is None and "connection" in document:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "connection",
                "must name a connection matching [a-z0-9][a-z0-9_-]{0,63}",
            )
        )

    model_raw = document.get("model")
    model: str | None = None
    if not isinstance(model_raw, str) or not _MODEL_ID_RE.fullmatch(model_raw):
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_FIELD,
                "model",
                "must be a model identifier matching "
                "[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
            )
        )
    else:
        model = model_raw

    temperature = _parse_optional_number(
        document, "temperature", low=0.0, high=_MAX_TEMPERATURE, integer=False,
        errors=errors,
    )
    top_p = _parse_optional_number(
        document, "top_p", low=0.0, high=_MAX_TOP_P, integer=False, errors=errors
    )
    seed = _parse_optional_number(
        document, "seed", low=0, high=_MAX_SEED, integer=True, errors=errors
    )
    max_tokens = _parse_optional_number(
        document, "max_tokens", low=1, high=_MAX_TOKENS, integer=True,
        errors=errors,
    )
    context_limit = _parse_optional_number(
        document, "context_limit", low=1, high=_MAX_LIMIT, integer=True,
        errors=errors,
    )
    output_limit = _parse_optional_number(
        document, "output_limit", low=1, high=_MAX_LIMIT, integer=True,
        errors=errors,
    )

    reasoning_effort_raw = document.get("reasoning_effort")
    reasoning_effort: str | None = None
    if reasoning_effort_raw is not None:
        if (
            not isinstance(reasoning_effort_raw, str)
            or reasoning_effort_raw not in REASONING_EFFORTS
        ):
            errors.append(
                ModelConfigFieldError(
                    ModelConfigErrorCode.INVALID_FIELD,
                    "reasoning_effort",
                    f"must be one of {sorted(REASONING_EFFORTS)}, or null",
                )
            )
        else:
            reasoning_effort = reasoning_effort_raw

    if errors:
        raise ModelConfigError(errors)
    assert name is not None and connection is not None and model is not None
    return ModelProfile(
        schema_version=SCHEMA_VERSION,
        name=name,
        connection=connection,
        model=model,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        context_limit=context_limit,
        output_limit=output_limit,
    )


# ---------------------------------------------------------------------------
# Selection binding and client resolution
# ---------------------------------------------------------------------------


def validate_selection(
    connection: ModelConnection, profile: ModelProfile
) -> ModelSelection:
    """Bind a profile to its explicitly named connection (C06 rule 1).

    Classified deliberate failures:

    - ``invalid_selection`` — the profile names a different connection;
    - ``unsupported_option`` — a profile setting requires an endpoint
      capability the connection does not declare (e.g. ``reasoning_effort``
      without ``capabilities.reasoning``).
    """
    errors: list[ModelConfigFieldError] = []
    if profile.connection != connection.name:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.INVALID_SELECTION,
                "connection",
                f"profile names connection {profile.connection!r} but was "
                f"bound to {connection.name!r}",
            )
        )
    if profile.reasoning_effort is not None and not connection.capabilities.reasoning:
        errors.append(
            ModelConfigFieldError(
                ModelConfigErrorCode.UNSUPPORTED_OPTION,
                "reasoning_effort",
                "requires endpoint capability 'reasoning', which connection "
                f"{connection.name!r} does not declare",
            )
        )
    if errors:
        raise ModelConfigError(errors)
    return ModelSelection(connection=connection, profile=profile)


def model_settings(profile: ModelProfile) -> OpenAIChatModelSettings:
    """The PydanticAI settings payload for a profile (allowlisted keys only).

    ``None`` fields are omitted: nothing is invented on the wire beyond what
    the profile explicitly selects.
    """
    settings: OpenAIChatModelSettings = OpenAIChatModelSettings()
    if profile.temperature is not None:
        settings["temperature"] = profile.temperature
    if profile.top_p is not None:
        settings["top_p"] = profile.top_p
    if profile.seed is not None:
        settings["seed"] = profile.seed
    if profile.max_tokens is not None:
        settings["max_tokens"] = profile.max_tokens
    if profile.reasoning_effort is not None:
        settings["openai_reasoning_effort"] = profile.reasoning_effort
    return settings


def build_model(
    selection: ModelSelection, *, resolver: EnvResolver | None = None
) -> "ResolvedModel":
    """Resolve a validated selection into a PydanticAI model (C06 rule 1).

    Credential material is resolved at call time through the configured
    ``.env`` source only (C03 rule 6). A connection declaring
    ``credential_ref`` without a resolver is a classified
    ``missing_resolver`` failure; a connection without one uses the adapter's
    non-empty placeholder key and never consults ambient credential
    environment variables.

    Raises :class:`ModelConfigError` (``missing_resolver``) or
    :class:`mai.credentials.env.EnvFileError` (structured, value-free
    credential-resolution failure).
    """
    connection, profile = selection.connection, selection.profile
    if connection.credential_ref is not None:
        if resolver is None:
            raise ModelConfigError(
                (
                    ModelConfigFieldError(
                        ModelConfigErrorCode.MISSING_RESOLVER,
                        "credential_ref",
                        f"connection {connection.name!r} declares credential "
                        f"reference {connection.credential_ref!r} but no .env "
                        "source was configured (C03 rule 6)",
                    ),
                )
            )
        api_key = resolver.resolve(connection.credential_ref).value
    else:
        api_key = API_PLACEHOLDER_KEY
    client_kwargs: dict[str, Any] = {
        "base_url": connection.base_url,
        "api_key": api_key,
        "default_headers": dict(connection.headers) or None,
    }
    # Only forward a declared timeout: an explicit ``timeout=None`` would
    # disable the SDK's default request timeouts entirely (connect=5s,
    # read/write/pool=600s), leaving a hung endpoint unbounded — the
    # reachability class would then be unreachable on the default path.
    if connection.timeout_seconds is not None:
        client_kwargs["timeout"] = connection.timeout_seconds
    client = AsyncOpenAI(**client_kwargs)
    provider = OpenAIProvider(openai_client=client)
    model = OpenAIChatModel(
        profile.model,
        provider=provider,
        settings=model_settings(profile),
    )
    return ResolvedModel(
        connection=connection, profile=profile, provider=provider, model=model
    )


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A validated selection bound to a live PydanticAI model.

    The credential material lives only inside ``provider``'s OpenAI client;
    the custom ``repr`` is value-free and carries configuration (names,
    endpoint, reference) only.
    """

    connection: ModelConnection
    profile: ModelProfile
    provider: OpenAIProvider
    model: OpenAIChatModel

    def __repr__(self) -> str:
        connection = self.connection
        credential = (
            f"reference:{connection.credential_ref}"
            if connection.credential_ref is not None
            else "none"
        )
        return (
            f"<ResolvedModel connection={connection.name!r} "
            f"profile={self.profile.name!r} model={self.profile.model!r} "
            f"endpoint={connection.base_url!r} credential={credential}>"
        )


# ---------------------------------------------------------------------------
# Provider failure classification (C06 rule 1 structured errors)
# ---------------------------------------------------------------------------


class ProviderFailureKind(StrEnum):
    """Distinguished provider failure classes (acceptance criterion 2)."""

    #: Endpoint not reachable: connection refused, DNS failure or timeout.
    #: The OpenAI SDK reports these as ``APIConnectionError`` subclasses
    #: (timeouts included); the PydanticAI run loop wraps them as
    #: ``ModelAPIError`` — both classify here.
    REACHABILITY = "reachability"
    #: The endpoint rejected the credential.
    AUTH = "auth"
    #: The endpoint rejected the request payload — including an unsupported
    #: setting the schema could not know about.
    REJECTED_REQUEST = "rejected_request"
    #: Any other structured API-level failure (rate limits, server errors).
    PROVIDER = "provider"
    #: Anything outside the SDK's structured error surface.
    UNKNOWN = "unknown"


def classify_provider_error(error: BaseException) -> ProviderFailureKind:
    """Map a provider failure onto a stable failure class.

    Two surfaces exist and both are handled:

    - direct OpenAI SDK exceptions (``openai.*Error``) — raised when the
      client is driven without the PydanticAI run loop;
    - PydanticAI-wrapped errors: ``Agent.run`` re-raises SDK ``APIStatusError``
      as :class:`pydantic_ai.exceptions.ModelHTTPError` (status code +
      provider body preserved) and transport failures as
      :class:`pydantic_ai.exceptions.ModelAPIError`.

    Order matters: specific classes are classified before the generic
    ``APIStatusError`` catch-all.
    """
    if isinstance(error, ModelHTTPError):
        if error.status_code in (401, 403):
            return ProviderFailureKind.AUTH
        if error.status_code in (400, 404, 422):
            return ProviderFailureKind.REJECTED_REQUEST
        return ProviderFailureKind.PROVIDER
    if isinstance(error, (APIConnectionError, ModelAPIError)):
        return ProviderFailureKind.REACHABILITY
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return ProviderFailureKind.AUTH
    if isinstance(error, (BadRequestError, UnprocessableEntityError)):
        return ProviderFailureKind.REJECTED_REQUEST
    if isinstance(error, APIStatusError):
        return ProviderFailureKind.PROVIDER
    return ProviderFailureKind.UNKNOWN


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


def _load_document(path: str) -> dict[str, object]:
    with open(path, "rb") as handle:
        return json.load(handle)


def _validation_failure(error: ModelConfigError) -> dict[str, object]:
    first = error.errors[0]
    return {
        "kind": "error",
        "code": first.code.value,
        "fields": error.fields,
        "detail": str(error),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.agents.models",
        description=(
            "Validate model connection/profile documents or resolve a "
            "selection against a configured .env source. Structured output "
            "only; credential values are never printed (resolution success "
            "is proven by the value's SHA-256 digest)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    connection_parser = sub.add_parser(
        "validate-connection", help="validate a connection JSON document"
    )
    connection_parser.add_argument("document")
    profile_parser = sub.add_parser(
        "validate-profile", help="validate a model profile JSON document"
    )
    profile_parser.add_argument("document")
    resolve_parser = sub.add_parser(
        "resolve",
        help="bind a profile to its connection and resolve the credential "
        "reference",
    )
    resolve_parser.add_argument("connection")
    resolve_parser.add_argument("profile")
    resolve_parser.add_argument(
        "--env", help="configured .env credential source (C03 rule 6)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success, 1 structured rejection, 2 usage error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-connection":
            document: Any = _load_document(args.document)
            parsed = parse_connection(document)
            _emit(
                {
                    "kind": "ok",
                    "connection": parsed.name,
                    "endpoint": parsed.base_url,
                    "credential_ref": parsed.credential_ref,
                    "capabilities": {
                        "tool_calls": parsed.capabilities.tool_calls,
                        "reasoning": parsed.capabilities.reasoning,
                    },
                }
            )
            return 0
        if args.command == "validate-profile":
            document = _load_document(args.document)
            parsed = parse_profile(document)
            _emit({"kind": "ok", "profile": parsed.name, "model": parsed.model})
            return 0
        # resolve
        connection = parse_connection(_load_document(args.connection))
        profile = parse_profile(_load_document(args.profile))
        selection = validate_selection(connection, profile)
        resolver = EnvResolver(args.env) if args.env else None
        resolved = build_model(selection, resolver=resolver)
    except json.JSONDecodeError:
        _emit({"kind": "error", "code": "unparsable_document"})
        return 1
    except OSError:
        _emit({"kind": "error", "code": "document_unreadable"})
        return 1
    except EnvFileError as error:
        _emit({"kind": "error", "code": error.code.value, "detail": error.detail})
        return 1
    except ModelConfigError as error:
        _emit(_validation_failure(error))
        return 1
    assert isinstance(resolved, ResolvedModel)
    settings = model_settings(resolved.profile)
    payload: dict[str, object] = {
        "kind": "resolved",
        "connection": resolved.connection.name,
        "profile": resolved.profile.name,
        "model": resolved.profile.model,
        "endpoint": resolved.connection.base_url,
        "credential_ref": resolved.connection.credential_ref,
        "settings": dict(settings),
    }
    if resolved.connection.credential_ref is not None:
        digest = hashlib.sha256(
            resolved.provider.client.api_key.encode("utf-8")
        ).hexdigest()
        payload["credential_sha256"] = digest
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
