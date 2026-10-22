"""Built-in ``onepassword`` native plugin: ``secret.list``/``secret.get``/``secret.store``.

One plugin (entry point name ``onepassword`` in the ``mai.capabilities``
group) owning three capability identifiers through the plural
``CAPABILITIES`` declaration (C3). The handler is the trusted executor
(C03 rule 7): it reaches the 1Password CLI exclusively through the
:class:`mai.credentials.onepassword.OnePasswordResolver` lane selected by
the ``s1-onepassword`` card — the service-account token is resolved from
the dispatcher's credential seam (``call.credentials``, C03 rule 6) into
an in-memory single-key source, never from a process-environment scan.

Configuration (C2/C4 contract):

- ``<MAI_DATA_ROOT>/plugins/onepassword/config.yaml`` — optional stored
  plugin config (``PluginConfigStore``). ``default_vault`` supplies the
  vault when a call omits one; when the config exists, its
  ``allowed_vaults`` allowlist is enforced strictly (``vault_not_allowed``
  on violation). Without a stored config the vault is a per-call
  requirement. A stored ``enabled: false`` refuses execution
  (``plugin_disabled``) — the worker composes the same check at
  discovery; this is the dispatch-time recheck.
- ``export_credential_ref`` (optional on ``secret.get``/``secret.store``)
  persists the secret value into ``<data_root>/credentials.env`` (0600)
  under an environment-variable name — the only supported way to hand
  secret material to later effects. The raw value in the executor output
  is declared via ``redact_values`` so the dispatcher scrubs it before
  the :class:`~mai.capabilities.dispatch.DispatchOutcome` becomes durable
  audit surface (C2); no digest is emitted for human-chosen material
  (L3: no offline brute-force oracle).

Value-free discipline matches the resolver lane: failure details name
references, vaults and classifications — never token or secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.credentials.env import (
    DATA_ROOT_CREDENTIALS_FILENAME,
    EnvResolver,
    SecretValue,
    upsert_env_credentials,
)
from mai.credentials.onepassword import OnePasswordError, OnePasswordResolver
from mai.plugins.config import PluginConfigStore, parse_plugin_config_text

__all__ = [
    "CAPABILITIES",
    "CONFIG_SCHEMA",
    "CREDENTIAL_REF_RE",
    "CREDENTIAL_REFERENCES",
    "PLUGIN_METADATA",
    "PLUGIN_NAME",
    "SECRET_GET_CAPABILITY",
    "SECRET_LIST_CAPABILITY",
    "SECRET_STORE_CAPABILITY",
    "execute",
]

#: Entry-point (plugin) name — the configuration/disable key.
PLUGIN_NAME: Final[str] = "onepassword"

#: Credential reference names the handler needs (names only, C03 rule 2;
#: values resolve at effect time through the dispatcher's seam).
CREDENTIAL_REFERENCES: Final[tuple[str, ...]] = ("OP_SERVICE_ACCOUNT_TOKEN",)

#: Plugin presentation metadata (the plugins-window surface, C5/C6).
PLUGIN_METADATA: Final[dict[str, str]] = {
    "title": "1Password Vault",
    "description": (
        "Read, list, and store service secrets in 1Password vaults via "
        "Service Account token."
    ),
}

#: Credential-reference shape for ``export_credential_ref`` (C2), frozen
#: by the canonical profile schema. Kept local: the schema module is the
#: shape owner and outside this card's allowlist (the repo-wide local-copy
#: convention; a divergence would be a review finding).
CREDENTIAL_REF_RE: Final[str] = r"^[A-Z][A-Z0-9_]{0,127}$"
_CREDENTIAL_REF: Final[re.Pattern[str]] = re.compile(CREDENTIAL_REF_RE)

#: One plugin, one configuration form (C3): every descriptor declares
#: this exact schema object.
CONFIG_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["default_vault", "allowed_vaults"],
    "properties": {
        "default_vault": {"type": "string", "minLength": 1, "maxLength": 64},
        "allowed_vaults": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    },
}

_DESCRIPTOR_VERSION: Final[str] = "1.0.0"

#: Shared vault field: every capability targets one vault; omitted means
#: the configured ``default_vault``.
_VAULT_PROPERTY: Final[dict[str, object]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "description": (
        "Target 1Password vault name or ID; omit to use the configured "
        "default_vault."
    ),
}

_EXPORT_PROPERTY: Final[dict[str, object]] = {
    "type": "string",
    "pattern": "^[A-Z][A-Z0-9_]{0,127}$",
    "description": (
        "Optional environment-variable name; when set, the secret value "
        "is persisted into the controller credentials.env under this "
        "name instead of being surfaced."
    ),
}


def _descriptor(
    identifier: str,
    description: str,
    properties: dict[str, object],
    *,
    required: list[str],
    effect_class: str,
    resource_scope: list[str],
    required_grants: list[str],
) -> dict[str, object]:
    """One frozen C03 rule 1 descriptor document (shared envelope)."""
    return {
        "identifier": identifier,
        "version": _DESCRIPTOR_VERSION,
        "config_schema": CONFIG_SCHEMA,
        "input_schema": {
            "type": "object",
            "description": description,
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "output_schema": {"type": "object"},
        "execution_environments": ["controller"],
        "effect_class": effect_class,
        "resource_scope": resource_scope,
        "required_grants": required_grants,
    }


SECRET_LIST_CAPABILITY: Final[dict[str, object]] = _descriptor(
    "secret.list",
    (
        "List 1Password vault item metadata (ids, titles, categories) "
        "with case-insensitive filtering and a bounded pagination page; "
        "never returns field values."
    ),
    {
        "vault": _VAULT_PROPERTY,
        "query": {
            "type": "string",
            "maxLength": 128,
            "description": (
                "Case-insensitive substring filter applied to item "
                "titles and ids."
            ),
        },
        "category": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "Optional 1Password item category filter (for example "
                "Login, Password, API Credential)."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": (
                "Maximum number of item summaries to return per page "
                "(default 25, max 100)."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Zero-based pagination offset into the sorted, matching "
                "item list."
            ),
        },
    },
    required=[],
    effect_class="read",
    resource_scope=[],
    required_grants=["secret.list"],
)

SECRET_GET_CAPABILITY: Final[dict[str, object]] = _descriptor(
    "secret.get",
    (
        "Read one field of one 1Password vault item through the "
        "configured Service Account token; the value is redacted in the "
        "agent-visible answer and can be exported to credentials.env."
    ),
    {
        "vault": _VAULT_PROPERTY,
        "item": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Title or ID of the vault item whose field should be "
                "read."
            ),
        },
        "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Field name inside the item to read (defaults to "
                "'password')."
            ),
        },
        "export_credential_ref": _EXPORT_PROPERTY,
    },
    required=["item"],
    effect_class="read",
    resource_scope=[],
    required_grants=["secret.get"],
)

SECRET_STORE_CAPABILITY: Final[dict[str, object]] = _descriptor(
    "secret.store",
    (
        "Create or update one 1Password vault item field with a "
        "provided or generated secret, verified by read-back; the value "
        "is redacted in the agent-visible answer."
    ),
    {
        "vault": _VAULT_PROPERTY,
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Title of the item to create or update; an existing "
                "item with this title is updated idempotently."
            ),
        },
        "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Field name inside the item to write (defaults to "
                "'password')."
            ),
        },
        "value": {
            "type": "string",
            "maxLength": 10240,
            "description": (
                "Plaintext secret value to store; required unless "
                "'generate' is true."
            ),
        },
        "generate": {
            "type": "boolean",
            "description": (
                "Generate a cryptographically random value instead of "
                "storing the provided plaintext."
            ),
        },
        "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 256,
            "description": (
                "Character length of the generated value (default 32; "
                "ignored when 'generate' is false)."
            ),
        },
        "category": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "1Password item category to create (defaults to "
                "'password')."
            ),
        },
        "username": {
            "type": "string",
            "maxLength": 256,
            "description": (
                "Optional username to store alongside the secret "
                "field."
            ),
        },
        "url": {
            "type": "string",
            "maxLength": 2048,
            "description": "Optional primary URL to attach to the item.",
        },
        "notes": {
            "type": "string",
            "maxLength": 10240,
            "description": "Optional plaintext note to attach to the item.",
        },
        "export_credential_ref": _EXPORT_PROPERTY,
    },
    required=["title"],
    effect_class="mutate",
    resource_scope=["secret.vault"],
    required_grants=["secret.store"],
)

#: Declaration order is the descriptor order (``descriptor_names``);
#: every entry shares CONFIG_SCHEMA (enforced by the runtime manifest).
CAPABILITIES: Final[tuple[dict[str, object], ...]] = (
    SECRET_LIST_CAPABILITY,
    SECRET_GET_CAPABILITY,
    SECRET_STORE_CAPABILITY,
)


class _SingleKeyCredentialSource(EnvResolver):
    """In-memory one-key stand-in for the ``.env`` resolver lane.

    The dispatcher hands the handler already-resolved credential
    material (C03 rule 6); the 1Password lane wants an
    :class:`EnvResolver` bound to one reference name. This subclass
    serves exactly that one key from memory — no file is opened and the
    token never touches disk here. An empty value reproduces the
    resolver's ``empty_value`` rejection, which the 1Password lane
    classifies as ``token_missing``.
    """

    def __init__(self, name: str, value: str) -> None:  # noqa: D107 - plain holder
        self._path = Path(f"<in-memory:{name}>")
        self._entries = {name: value}


def _ok(document: dict[str, object]) -> ExecutorResult:
    return ExecutorResult(
        ok=True,
        output=json.dumps({"status": "ok", **document}, sort_keys=True),
    )


def _failure(code: str, detail: str) -> ExecutorResult:
    return ExecutorResult(
        ok=False,
        error_code=code,
        output=json.dumps(
            {"status": "error", "code": code, "detail": detail},
            sort_keys=True,
        ),
    )


def _load_plugin_config() -> tuple[Path | None, Mapping[str, object] | None, bool]:
    """Best-effort stored config: ``(data_root, config, enabled)``.

    No ``MAI_DATA_ROOT`` or no stored document is a normal state (the
    plugin then requires an explicit vault per call); only a stored
    document can gate vaults or disable the plugin here. Failures are
    value-free and degrade to "unconfigured".
    """
    raw_root = os.environ.get("MAI_DATA_ROOT", "").strip()
    if not raw_root:
        return None, None, True
    root = Path(raw_root).expanduser()
    try:
        store = PluginConfigStore(root)
        stored = store.read_config(PLUGIN_NAME)
        document = parse_plugin_config_text(stored.text)
    except Exception:
        return root, None, True
    return root, document.config, bool(document.enabled)


def _resolve_target_vault(
    payload: Mapping[str, object],
) -> tuple[Path | None, str | None, ExecutorResult | None]:
    """Dispatch-time config recheck: enabled gate, vault default, allowlist.

    Returns ``(data_root, vault, refusal)``; ``refusal`` is set for every
    structured rejection (including ``plugin_disabled`` and
    ``vault_not_allowed``).
    """
    data_root, config, enabled = _load_plugin_config()
    if not enabled:
        return (
            data_root,
            None,
            _failure(
                "plugin_disabled",
                f"plugin {PLUGIN_NAME!r} is disabled in the stored "
                "configuration",
            ),
        )
    raw_vault = payload.get("vault")
    if raw_vault is not None and (
        not isinstance(raw_vault, str) or not raw_vault.strip()
    ):
        return data_root, None, _failure(
            "invalid_request", "vault must be a non-empty string when provided"
        )
    vault = raw_vault.strip() if isinstance(raw_vault, str) else None
    default_vault = config.get("default_vault") if config else None
    allowed = config.get("allowed_vaults") if config else None
    if vault is None:
        if isinstance(default_vault, str) and default_vault:
            vault = default_vault
        else:
            return data_root, None, _failure(
                "vault_required",
                "no vault provided and the plugin has no configured "
                "default_vault",
            )
    if isinstance(allowed, list):
        allowed_names = {item for item in allowed if isinstance(item, str)}
        if allowed_names and vault not in allowed_names:
            return data_root, None, _failure(
                "vault_not_allowed",
                f"vault {vault!r} is not in the plugin's allowed_vaults "
                "allowlist",
            )
    return data_root, vault, None


def _build_resolver(call: CapabilityCall) -> OnePasswordResolver:
    """Bind the dispatcher-resolved service-account token into the lane.

    A missing token yields the lane's ``token_missing`` classification on
    use — the plugin stays constructible (no probe at build time, matching
    the resolver's own construction contract).
    """
    token = call.credentials.get(CREDENTIAL_REFERENCES[0])
    token_value = token.value if isinstance(token, SecretValue) else ""
    return OnePasswordResolver(
        credential_ref=CREDENTIAL_REFERENCES[0],
        resolver=_SingleKeyCredentialSource(CREDENTIAL_REFERENCES[0], token_value),
        token_env=None,
    )


async def _maybe_export(
    value: str,
    payload: Mapping[str, object],
    data_root: Path | None,
) -> ExecutorResult | None:
    """Persist ``export_credential_ref`` into ``credentials.env`` (C2)."""
    raw = payload.get("export_credential_ref")
    if raw is None:
        return None
    if not isinstance(raw, str) or _CREDENTIAL_REF.fullmatch(raw) is None:
        return _failure(
            "invalid_credential_ref",
            f"export_credential_ref must match {CREDENTIAL_REF_RE} "
            "(an environment-variable name)",
        )
    if data_root is None:
        return _failure(
            "credential_export_unavailable",
            "MAI_DATA_ROOT is not configured; export_credential_ref "
            "cannot be persisted",
        )
    try:
        await asyncio.to_thread(
            upsert_env_credentials,
            data_root / DATA_ROOT_CREDENTIALS_FILENAME,
            {raw: value},
        )
    except Exception as error:  # value-free: classification only
        return _failure(
            "credential_export_failed",
            f"persisting credential reference {raw!r} failed: "
            f"{type(error).__name__}",
        )
    return None


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when provided")
    return value


def _optional_bool(
    payload: Mapping[str, object], key: str, default: bool
) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean when provided")
    return value


def _optional_int(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer when provided")
    return value


async def _list_items(
    resolver: OnePasswordResolver,
    payload: Mapping[str, object],
    vault: str,
) -> ExecutorResult:
    page = await asyncio.to_thread(
        resolver.list_items,
        vault,
        query=_optional_str(payload, "query"),
        category=_optional_str(payload, "category"),
        limit=_optional_int(payload, "limit", 25),
        offset=_optional_int(payload, "offset", 0),
    )
    return _ok(
        {
            "vault": vault,
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "vault": item.vault,
                    "updated_at": item.updated_at,
                }
                for item in page.items
            ],
            "limit": page.limit,
            "offset": page.offset,
            "total": page.total,
            "has_more": page.has_more,
        }
    )


async def _get_item(
    resolver: OnePasswordResolver,
    payload: Mapping[str, object],
    vault: str,
    data_root: Path | None,
) -> ExecutorResult:
    item = _required_str(payload, "item")
    field = _optional_str(payload, "field") or "password"
    reference = f"op://{vault}/{item}/{field}"
    resolved = await asyncio.to_thread(resolver.resolve, reference)
    value = resolved.value
    refusal = await _maybe_export(value, payload, data_root)
    if refusal is not None:
        return refusal
    document: dict[str, object] = {
        "status": "ok",
        "vault": vault,
        "item": item,
        "field": field,
        "reference": reference,
        # The dispatcher scrubs this value via redact_values (C2); it
        # exists in the document to make the scrub observable.
        "value": value,
    }
    export = payload.get("export_credential_ref")
    if isinstance(export, str) and export:
        document["exported_credential_ref"] = export
    return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True), redact_values=(value,))


async def _store_item(
    resolver: OnePasswordResolver,
    payload: Mapping[str, object],
    vault: str,
    data_root: Path | None,
) -> ExecutorResult:
    title = _required_str(payload, "title")
    generate = _optional_bool(payload, "generate", False)
    receipt = await asyncio.to_thread(
        resolver.store_secret,
        vault,
        title=title,
        field=_optional_str(payload, "field") or "password",
        value=_optional_str(payload, "value"),
        generate=generate,
        length=_optional_int(payload, "length", 32),
        category=_optional_str(payload, "category") or "password",
        username=_optional_str(payload, "username"),
        url=_optional_str(payload, "url"),
        notes=_optional_str(payload, "notes"),
    )
    secret_value = receipt.value.value
    refusal = await _maybe_export(secret_value, payload, data_root)
    if refusal is not None:
        return refusal
    document: dict[str, object] = {
        "status": "ok",
        "vault": receipt.vault,
        "item_id": receipt.item_id,
        "title": receipt.title,
        "field": receipt.field,
        "reference": receipt.reference,
        "generated": receipt.generated,
        # The dispatcher scrubs this value via redact_values (C2).
        "value": secret_value,
    }
    # L3: a digest is a brute-force oracle for human-chosen material, so
    # it is emitted only for generated secrets (read-back verified).
    if receipt.generated and receipt.sha256:
        document["sha256"] = receipt.sha256
    export = payload.get("export_credential_ref")
    if isinstance(export, str) and export:
        document["exported_credential_ref"] = export
    return ExecutorResult(
        ok=True,
        output=json.dumps(document, sort_keys=True),
        redact_values=(secret_value,),
    )


async def execute(call: CapabilityCall) -> ExecutorResult:
    """The trusted handler: config gate → token lane → one op invocation."""
    capability = call.descriptor.identifier
    payload = call.payload if isinstance(call.payload, Mapping) else {}
    data_root, vault, refusal = _resolve_target_vault(payload)
    if refusal is not None:
        return refusal
    assert vault is not None  # _resolve_target_vault contract
    resolver = _build_resolver(call)
    try:
        if capability == "secret.list":
            return await _list_items(resolver, payload, vault)
        if capability == "secret.get":
            return await _get_item(resolver, payload, vault, data_root)
        if capability == "secret.store":
            return await _store_item(resolver, payload, vault, data_root)
    except OnePasswordError as error:
        # Value-free by the resolver lane's contract (scrubbed stderr).
        return _failure(error.code.value, error.detail)
    except ValueError as error:
        return _failure("invalid_request", str(error))
    return _failure("invalid_request", f"unsupported capability {capability!r}")
