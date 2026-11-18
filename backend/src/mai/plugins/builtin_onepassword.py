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
import fcntl
import json
import os
import re
import yaml
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
        "name instead of being surfaced. Bootstrap, SSH, model-provider "
        "and plugin operator refs are protected and can never be "
        "overwritten; agent-created exports must use their reserved "
        "MAI_AGENT_<SERVER8>[_<RUN8>]_ namespace. secret.* operations "
        "are the EXCLUSIVE credential route: never the op CLI, provider "
        "HTTP APIs, environment dumps, or .env/file searches."
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
        "never returns field values. secret.* capabilities are the "
        "EXCLUSIVE credential route: all credential operations must "
        "route through them — never the op CLI, provider HTTP APIs, "
        "environment variable dumps, or .env/credential-file searches."
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
        "agent-visible answer and can be exported to credentials.env. "
        "secret.* capabilities are the EXCLUSIVE credential route: all "
        "credential operations must route through them — never the op "
        "CLI, provider HTTP APIs, environment variable dumps, or "
        ".env/credential-file searches. Agent callers may export only "
        "into their reserved MAI_AGENT_<SERVER8>[_<RUN8>]_ namespace; "
        "bootstrap, SSH, model-provider and plugin operator refs are "
        "protected and can never be overwritten."
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
        "is redacted in the agent-visible answer. secret.* capabilities "
        "are the EXCLUSIVE credential route: all credential operations "
        "must route through them — never the op CLI, provider HTTP "
        "APIs, environment variable dumps, or .env/credential-file "
        "searches. Agent callers may export only into their reserved "
        "MAI_AGENT_<SERVER8>[_<RUN8>]_ namespace; bootstrap, SSH, "
        "model-provider and plugin operator refs are protected and can "
        "never be overwritten."
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


#: Bootstrap/system variable names that can NEVER be an export target
#: (D6/C6): the plugin's own service-account lane, the controller's
#: database/env wiring, and process-level paths.
_RESERVED_BOOTSTRAP_REFS: Final[frozenset[str]] = frozenset(
    {
        "OP_SERVICE_ACCOUNT_TOKEN",
        "MAI_DATABASE_URL",
        "MAI_TEST_DATABASE_URL",
        "MAI_DATA_ROOT",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "PYTHONPATH",
    }
)

#: The reserved agent-created export namespace (server/run-owned).
_AGENT_EXPORT_PREFIX: Final[str] = "MAI_AGENT_"

#: ``MAI_AGENT_<SERVER8>_<RUN8>_<NAME>`` — run-owned agent export.
_AGENT_SERVER_RUN_RE: Final[re.Pattern[str]] = re.compile(
    r"^MAI_AGENT_([0-9A-F]{8})_([0-9A-F]{8})_[A-Z0-9_]+$"
)

#: ``MAI_AGENT_<SERVER8>_<NAME>`` — server-owned agent export.
_AGENT_SERVER_RE: Final[re.Pattern[str]] = re.compile(
    r"^MAI_AGENT_([0-9A-F]{8})_[A-Z0-9_]+$"
)

#: Exclusive lock for the ``credentials.env`` no-overwrite check-then-act
#: window (agent / ``MAI_AGENT_`` exports; fcntl, the repo's lock
#: discipline).
_EXPORT_LOCK_SUFFIX: Final[str] = ".lock"


def _is_agent_caller(context: object) -> bool:
    """Whether the dispatch context is an AGENT run (D6/C6).

    Trusted non-agent callers (operator tools, bootstrap provisioning)
    keep the full ``export_credential_ref`` compatibility contract; agent
    runs are confined to their reserved server/run-owned namespace.
    """
    principal = str(getattr(context, "principal", "") or "").lower()
    return principal.startswith(("agent", "session"))


def _extract_yaml_credential_refs(node: object, out: set[str]) -> None:
    """Collect ``credential_ref``/``credential_refs`` values from parsed
    YAML (bounded recursion over dicts and lists)."""
    if isinstance(node, Mapping):
        ref = node.get("credential_ref")
        if isinstance(ref, str):
            out.add(ref.strip())
        refs = node.get("credential_refs")
        if isinstance(refs, str):
            out.add(refs.strip())
        elif isinstance(refs, (list, tuple)):
            for item in refs:
                if isinstance(item, str):
                    out.add(item.strip())
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                _extract_yaml_credential_refs(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            if isinstance(item, (Mapping, list, tuple)):
                _extract_yaml_credential_refs(item, out)


def _safe_load_yaml(path: Path) -> object | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _configured_credential_refs(data_root: Path | None) -> frozenset[str]:
    """Operator/infrastructure refs configured under ``data_root``.

    Bounded, failure-tolerant scans of the canonical operator surfaces:
    server profiles (``servers/*/profile.yaml``, including nested
    ``ssh.credential_ref``), model connection ``credential_ref`` fields
    (``models/connections/*.json``), and plugin ``credential_refs``
    (``plugins/*.yaml`` and ``plugins/*/*.yaml``; single/double quotes,
    flow-style lists and inline comments all parse via
    ``yaml.safe_load``). Unreadable or absent surfaces simply contribute
    nothing.
    """
    if data_root is None:
        return frozenset()
    refs: set[str] = set()
    try:
        servers_dir = data_root / "servers"
        if servers_dir.is_dir():
            for profile in sorted(servers_dir.glob("*/profile.yaml")):
                document = _safe_load_yaml(profile)
                if document is not None:
                    _extract_yaml_credential_refs(document, refs)
        connections_dir = data_root / "models" / "connections"
        if connections_dir.is_dir():
            for path in sorted(connections_dir.glob("*.json")):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(document, Mapping):
                    _extract_yaml_credential_refs(document, refs)
        plugins_dir = data_root / "plugins"
        if plugins_dir.is_dir():
            yaml_paths: list[Path] = []
            for pattern in ("*.yaml", "*/*.yaml"):
                yaml_paths.extend(sorted(plugins_dir.glob(pattern)))
            for path in yaml_paths:
                document = _safe_load_yaml(path)
                if document is not None:
                    _extract_yaml_credential_refs(document, refs)
    except OSError:  # pragma: no cover - defensive: scans never crash export
        pass
    return frozenset(ref for ref in refs if ref)


def _existing_env_collision(raw: str, data_root: Path | None) -> bool:
    """Whether ``credentials.env`` already carries an entry for ``raw``.

    Ownership is not recorded in the env file, so an existing entry is
    treated as operator/other-run state: agent callers (and every
    ``MAI_AGENT_`` namespace export) must never overwrite it.
    """
    if data_root is None:
        return False
    try:
        text = (data_root / DATA_ROOT_CREDENTIALS_FILENAME).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == raw:
            return True
    return False


def _agent_scope_failure(
    raw: str, context: object
) -> ExecutorResult | None:
    """Validate a ``MAI_AGENT_`` ref against the trusted call context."""
    server8 = str(getattr(context, "server_id", "")).replace("-", "")[:8].upper()
    run8 = str(getattr(context, "run_id", "")).replace("-", "")[:8].upper()
    server_run = _AGENT_SERVER_RUN_RE.match(raw)
    if server_run is not None:
        if server_run.group(1) != server8 or server_run.group(2) != run8:
            return _failure(
                "credential_ref_scope_mismatch",
                "export_credential_ref MAI_AGENT_ namespace belongs to a "
                "different server or run and cannot be written from this "
                "context",
            )
        return None
    server_only = _AGENT_SERVER_RE.match(raw)
    if server_only is not None:
        if server_only.group(1) != server8:
            return _failure(
                "credential_ref_scope_mismatch",
                "export_credential_ref MAI_AGENT_ namespace belongs to a "
                "different server and cannot be written from this context",
            )
        return None
    return _failure(
        "credential_ref_scope_mismatch",
        "export_credential_ref must use the reserved "
        "MAI_AGENT_<SERVER8>[_<RUN8>]_ namespace for agent-created "
        "exports",
    )


def _validate_export_ref(
    payload: Mapping[str, object],
    data_root: Path | None,
    context: object,
) -> ExecutorResult | None:
    """Validate ``export_credential_ref`` BEFORE any vault/env write.

    ``None`` = the export may proceed; otherwise a structured refusal:
    ``invalid_credential_ref`` (malformed), ``credential_ref_protected``
    (bootstrap/operator-owned), ``credential_ref_scope_mismatch``
    (agent namespace of another server/run, or an unscoped agent
    export), ``credential_ref_collision`` (existing entry an agent may
    not overwrite), ``credential_export_unavailable`` (no data root).
    """
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
    if raw in _RESERVED_BOOTSTRAP_REFS:
        return _failure(
            "credential_ref_protected",
            f"export_credential_ref {raw!r} is reserved for bootstrap, "
            "SSH, or operator configuration and cannot be overwritten",
        )
    configured = _configured_credential_refs(data_root)
    if raw.upper() in {ref.upper() for ref in configured}:
        return _failure(
            "credential_ref_protected",
            f"export_credential_ref {raw!r} is reserved for bootstrap, "
            "SSH, or operator configuration and cannot be overwritten",
        )
    agent_caller = _is_agent_caller(context)
    if raw.startswith(_AGENT_EXPORT_PREFIX):
        scope_failure = _agent_scope_failure(raw, context)
        if scope_failure is not None:
            return scope_failure
    elif agent_caller:
        return _failure(
            "credential_ref_scope_mismatch",
            "agent callers must export into their reserved "
            "MAI_AGENT_<SERVER8>[_<RUN8>]_ namespace",
        )
    if agent_caller or raw.startswith(_AGENT_EXPORT_PREFIX):
        if _existing_env_collision(raw, data_root):
            return _failure(
                "credential_ref_collision",
                f"export_credential_ref {raw!r} already exists in "
                "credentials.env and was not created by this server/run",
            )
    return None


def _export_to_env_file(
    data_root: Path,
    export: str,
    value: str,
    *,
    disallow_overwrite: bool,
) -> ExecutorResult | None:
    """Write one export under the env-file lock (sync; runs in a thread).

    The exclusive ``flock`` on ``<credentials.env>.lock`` closes the
    check-then-act window: while holding it, a no-overwrite export that
    collides with an existing entry fails BEFORE
    ``upsert_env_credentials`` touches the file.
    """
    lock_path = (
        data_root / f"{DATA_ROOT_CREDENTIALS_FILENAME}{_EXPORT_LOCK_SUFFIX}"
    )
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if disallow_overwrite and _existing_env_collision(export, data_root):
            return _failure(
                "credential_ref_collision",
                f"export_credential_ref {export!r} already exists in "
                "credentials.env and was not created by this server/run",
            )
        upsert_env_credentials(
            data_root / DATA_ROOT_CREDENTIALS_FILENAME, {export: value}
        )
        return None
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


async def _perform_export(
    value: str,
    payload: Mapping[str, object],
    data_root: Path | None,
    context: object,
) -> ExecutorResult | None:
    """Persist the ALREADY-VALIDATED export into ``credentials.env``.

    The disk write runs in a thread and re-checks the no-overwrite rule
    under the env-file lock (TOCTOU backstop for concurrent runs).
    """
    raw = payload.get("export_credential_ref")
    export = raw if isinstance(raw, str) and raw else None
    if export is None:
        return None
    disallow_overwrite = _is_agent_caller(context) or raw.startswith(
        _AGENT_EXPORT_PREFIX
    )
    try:
        return await asyncio.to_thread(
            _export_to_env_file,
            data_root,
            export,
            value,
            disallow_overwrite=disallow_overwrite,
        )
    except Exception as error:  # value-free: classification only
        return _failure(
            "credential_export_failed",
            f"persisting credential reference {export!r} failed: "
            f"{type(error).__name__}",
        )


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
    context: object,
) -> ExecutorResult:
    item = _required_str(payload, "item")
    field = _optional_str(payload, "field") or "password"
    reference = f"op://{vault}/{item}/{field}"
    resolved = await asyncio.to_thread(resolver.resolve, reference)
    value = resolved.value
    refusal = await _perform_export(value, payload, data_root, context)
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
    context: object,
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
    refusal = await _perform_export(secret_value, payload, data_root, context)
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
    """The trusted handler: config gate → export-target gate → token lane
    → one op invocation. The export gate runs BEFORE any vault read or
    write, so a forbidden or malformed ``export_credential_ref`` fails
    with zero side effects (no 1Password item created, no
    ``credentials.env`` modification)."""
    capability = call.descriptor.identifier
    payload = call.payload if isinstance(call.payload, Mapping) else {}
    data_root, vault, refusal = _resolve_target_vault(payload)
    if refusal is not None:
        return refusal
    assert vault is not None  # _resolve_target_vault contract
    export_refusal = await asyncio.to_thread(
        _validate_export_ref, payload, data_root, call.context
    )
    if export_refusal is not None:
        return export_refusal
    resolver = _build_resolver(call)
    try:
        if capability == "secret.list":
            return await _list_items(resolver, payload, vault)
        if capability == "secret.get":
            return await _get_item(resolver, payload, vault, data_root, call.context)
        if capability == "secret.store":
            return await _store_item(resolver, payload, vault, data_root, call.context)
    except OnePasswordError as error:
        # Value-free by the resolver lane's contract (scrubbed stderr).
        return _failure(error.code.value, error.detail)
    except ValueError as error:
        return _failure("invalid_request", str(error))
    return _failure("invalid_request", f"unsupported capability {capability!r}")
