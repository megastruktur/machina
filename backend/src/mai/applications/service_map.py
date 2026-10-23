"""Service Map & Category Taxonomy (campaign ``mechanicus-ui`` card 02).

The Service Map is the operator-facing catalogue layer over the C09
application adoption truth: per-service ``category`` (a machine-readable
taxonomy slug), free-text ``description`` and an operational runbook
(``instructions``: install / update / uninstall / backup / verify), plus
the category taxonomy itself (8 built-in categories + operator-defined
custom categories). The store is a confined file store rooted at
``<profiles_root>/service_map/`` (the same explicit data root convention
as every other C02 store):

- ``categories.json`` — operator-added/edited custom categories
  (``machine_id: ^[a-z0-9_-]{1,64}$``, ``label``, ``description``),
  merged with :data:`DEFAULT_SERVICE_CATEGORIES` (``built_in: bool``).
- ``servers/<server_id>.json`` — per-server map metadata keyed by
  ``application_id`` (canonical UUID string), one entry per mapped
  service.

Writes are atomic (temp file + ``os.replace`` + directory fsync, the
documents-store discipline): a crash mid-write leaves the previous record
intact. Reads validate strictly and fail honestly (a malformed record is
a surfaced error, never a silent skip), mirroring the canonical store
producers.

The module also owns the ``app.upsert_map_entry`` capability
(:func:`upsert_map_descriptor` + :class:`ServiceMapUpsertCapability`):
the agent-facing seam that adds or updates one service entry in a
server's map. The executor follows the reviewed dispatcher convention —
one :class:`~mai.capabilities.dispatch.CapabilityCall` in, one honest
:class:`~mai.capabilities.dispatch.ExecutorResult` out; the context's
``server_id`` is the ONLY target vocabulary (the payload cannot retarget
a call), and every grant/approval/policy gate upstream of the executor
is the reviewed producers', never re-implemented here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "CAPABILITY_ID",
    "CATEGORY_MACHINE_ID_RE",
    "DEFAULT_SERVICE_CATEGORIES",
    "INSTRUCTION_KEYS",
    "MAP_ORIGINS",
    "ServiceCategory",
    "CatalogRunbook",
    "ServiceInstructions",
    "ServiceMapEntry",
    "ServiceMapError",
    "ServiceMapErrorCode",
    "ServiceMapStore",
    "ServiceMapUpsertCapability",
    "classify_service",
    "entry_digest",
    "upsert_map_descriptor",
]

#: The capability identifier (the executor vocabulary frozen here).
CAPABILITY_ID: Final[str] = "app.upsert_map_entry"

#: Category ``machine_id`` shape: lowercase slugs, 1–64 chars (the spec
#: grammar ``^[a-z0-9_-]{1,64}$``).
CATEGORY_MACHINE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{1,64}$")

#: The five runbook instruction slots (frozen vocabulary).
INSTRUCTION_KEYS: Final[tuple[str, ...]] = (
    "install",
    "update",
    "uninstall",
    "backup",
    "verify",
)

#: Entry origins. ``compose``/``systemd_unit``/``docker_container`` mirror
#: the C09 discovery vocabulary; ``manual`` is the chat/agent-added
#: service with no live target reference.
MAP_ORIGINS: Final[frozenset[str]] = frozenset(
    {"compose", "systemd_unit", "docker_container", "manual"}
)

#: Bounds (explicitly bounded like every other transport surface).
MAX_NAME_CHARS: Final[int] = 200
MAX_DESCRIPTION_CHARS: Final[int] = 2000
MAX_INSTRUCTION_CHARS: Final[int] = 8000
MAX_LABEL_CHARS: Final[int] = 100
MAX_CATEGORY_DESCRIPTION_CHARS: Final[int] = 1000
MAX_ENTRIES_PER_SERVER: Final[int] = 500
MAX_MATCHED_ENTRIES: Final[int] = 8


class ServiceMapErrorCode(StrEnum):
    """Stable machine-readable store/capability rejection codes."""

    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    MALFORMED_RECORD = "malformed_record"


class ServiceMapError(ValueError):
    """One structured, value-free service-map rejection."""

    def __init__(self, code: ServiceMapErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ServiceInstructions:
    """One service's operational runbook (five frozen slots)."""

    install: str = ""
    update: str = ""
    uninstall: str = ""
    backup: str = ""
    verify: str = ""

    def to_json(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in INSTRUCTION_KEYS}

    @classmethod
    def of(cls, value: Mapping[str, object] | None) -> "ServiceInstructions":
        """Type one validated mapping (unknown keys rejected)."""
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "instructions must be an object",
            )
        unknown = set(value) - set(INSTRUCTION_KEYS)
        if unknown:
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                f"instructions has unknown keys: {sorted(unknown)}",
            )
        parsed: dict[str, str] = {}
        for key in INSTRUCTION_KEYS:
            raw = value.get(key, "")
            if not isinstance(raw, str):
                raise ServiceMapError(
                    ServiceMapErrorCode.INVALID_REQUEST,
                    f"instructions.{key} must be a string",
                )
            if len(raw) > MAX_INSTRUCTION_CHARS:
                raise ServiceMapError(
                    ServiceMapErrorCode.INVALID_REQUEST,
                    f"instructions.{key} exceeds the bounded instruction size",
                )
            parsed[key] = raw
        return cls(**parsed)


@dataclass(frozen=True, slots=True)
class ServiceCategory:
    """One taxonomy category (built-in or operator-defined)."""

    machine_id: str
    label: str
    description: str
    built_in: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "label": self.label,
            "description": self.description,
            "built_in": self.built_in,
        }


#: The eight built-in categories (frozen order; the taxonomy default).
DEFAULT_SERVICE_CATEGORIES: Final[tuple[ServiceCategory, ...]] = (
    ServiceCategory(
        "games",
        "Games",
        "Dedicated multiplayer game servers, SteamCMD workloads, "
        "Enshrouded, Valheim, Palworld",
        built_in=True,
    ),
    ServiceCategory(
        "ai",
        "AI & Models",
        "LLM inference, embeddings, vector databases, LiteLLM, Ollama, "
        "vLLM, Hindsight",
        built_in=True,
    ),
    ServiceCategory(
        "agent",
        "Autonomous Agents",
        "Agent runtimes, orchestrators, Hermes, bots, MCP bridges",
        built_in=True,
    ),
    ServiceCategory(
        "system",
        "System & OS",
        "Core systemd units, SSH, kernel/network daemons, UFW, storage "
        "mounts",
        built_in=True,
    ),
    ServiceCategory(
        "dev",
        "Development & CI",
        "Git forges, Forgejo, Gitea, CI runners, build caches, "
        "code-server, registries",
        built_in=True,
    ),
    ServiceCategory(
        "infrastructure",
        "Infrastructure & Proxy",
        "Reverse proxies, Caddy, Traefik, Tailscale, DNS, Docker engine, "
        "Postgres, Redis",
        built_in=True,
    ),
    ServiceCategory(
        "media",
        "Media & Content",
        "Media servers, Jellyfin, Plex, arr-stack, asset pipelines, "
        "streaming",
        built_in=True,
    ),
    ServiceCategory(
        "observability",
        "Observability & Backup",
        "Monitoring, Prometheus, Grafana, Loki, restic/borg backups, "
        "health probes",
        built_in=True,
    ),
)

#: Deterministic name→category keyword table (first match wins, frozen
#: category order; unknown names classify as ``system``). Substring match
#: on the lowercased name/description — deliberately simple and
#: deterministic (a hint for the operator, never an enforced label).
_CATEGORY_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "games",
        (
            "enshrouded", "valheim", "palworld", "steamcmd", "minecraft",
            "terraria", "factorio", "game server", "gameserver",
        ),
    ),
    (
        "ai",
        (
            "litellm", "ollama", "vllm", "hindsight", "llm", "embed",
            "vector", "whisper", "inference",
        ),
    ),
    (
        "agent",
        ("hermes", "agent", "mcp", "orchestrat", "automation", "bot"),
    ),
    (
        "dev",
        (
            "forgejo", "gitea", "ci ", "ci-", "runner", "build", "git",
            "code-server", "registry", "woodpecker", "drone",
        ),
    ),
    (
        "infrastructure",
        (
            "caddy", "traefik", "tailscale", "dns", "docker", "postgres",
            "redis", "nginx", "proxy", "vault", "minio", "s3",
        ),
    ),
    (
        "media",
        (
            "jellyfin", "plex", "sonarr", "radarr", "navidrome", "stream",
            "ffmpeg", "immich", "audiobookshelf", "media",
        ),
    ),
    (
        "observability",
        (
            "prometheus", "grafana", "loki", "restic", "borg", "backup",
            "monitor", "health", "probe", "uptime", "alert",
        ),
    ),
)

#: Deterministic name→category keyword table (first match wins, frozen
#: category order; unknown names classify as ``system``). Substring match
#: on the lowercased name/description — deliberately simple and
#: deterministic (a hint for the operator, never an enforced label).


def classify_service(name: str, description: str = "") -> str:
    """The deterministic ``suggested_category`` for one observed service.

    First keyword hit in the frozen category order wins; no hit (or an
    empty name) classifies as ``system``. A hint for the adoption
    decision, never an enforced label.
    """
    haystack = f"{name} {description}".lower()
    for machine_id, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return machine_id
    return "system"


@dataclass(frozen=True, slots=True)
class ServiceMapEntry:
    """One per-server service map record (the confined-store shape)."""

    application_id: str
    server_id: str
    name: str
    origin: str
    compose_project: str | None
    working_dir: str | None
    unit_name: str | None
    category: str
    description: str
    instructions: ServiceInstructions
    updated_at: str
    revision: int
    #: Server-specific prompt instructions injected into the Agent's
    #: ``@service`` context alongside the global description/runbook.
    custom_instructions: str = ""
    #: Whether the service is currently installed on the host (a catalog
    #: runbook staged-but-not-installed carries ``False``).
    installed: bool = True

    def to_json(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "server_id": self.server_id,
            "name": self.name,
            "origin": self.origin,
            "compose_project": self.compose_project,
            "working_dir": self.working_dir,
            "unit_name": self.unit_name,
            "category": self.category,
            "description": self.description,
            "instructions": self.instructions.to_json(),
            "updated_at": self.updated_at,
            "revision": self.revision,
            "custom_instructions": self.custom_instructions,
            "installed": self.installed,
        }


@dataclass(frozen=True, slots=True)
class CatalogRunbook:
    """One global Runbook Catalog record (the ``catalog.json`` shape).

    A runbook is the server-agnostic half of a service: identity, taxonomy
    category, description and the operational runbook. Per-server state
    (installed flag, custom instructions) stays on the
    :class:`ServiceMapEntry`; the catalog never carries it.
    """

    runbook_id: str
    name: str
    category: str
    description: str
    instructions: ServiceInstructions
    updated_at: str
    revision: int

    def to_json(self) -> dict[str, object]:
        return {
            "runbook_id": self.runbook_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "instructions": self.instructions.to_json(),
            "updated_at": self.updated_at,
            "revision": self.revision,
        }


def _catalog_runbook_of(document: Mapping[str, object]) -> CatalogRunbook:
    """Strictly type one persisted catalog document (malformed = surfaced)."""
    try:
        runbook_id = _canonical_uuid(document["runbook_id"], "runbook_id")
        name = _validated_name(document["name"])
        category = _validated_category_id(document["category"])
        description = _validated_description(document["description"])
        instructions = ServiceInstructions.of(document.get("instructions"))
        raw_revision = document["revision"]
        if (
            not isinstance(raw_revision, int)
            or isinstance(raw_revision, bool)
            or raw_revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        updated_at = str(document["updated_at"])
    except (KeyError, TypeError, ValueError, ServiceMapError) as exc:
        raise ServiceMapError(
            ServiceMapErrorCode.MALFORMED_RECORD,
            f"catalog runbook record is malformed: {exc}",
        ) from None
    return CatalogRunbook(
        runbook_id=runbook_id,
        name=name,
        category=category,
        description=description,
        instructions=instructions,
        updated_at=updated_at,
        revision=raw_revision,
    )


def entry_digest(entry: ServiceMapEntry) -> str:
    """The canonical digest of one map entry (view ``sha256`` for manual
    entries that have no adoption manifest hash)."""
    return hashlib.sha256(
        json.dumps(entry.to_json(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _canonical_uuid(value: object, field: str) -> str:
    """Canonical UUID parse (string form, lowercase, hyphenated)."""
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ServiceMapError(
            ServiceMapErrorCode.INVALID_REQUEST,
            f"{field} must be a canonical UUID string",
        ) from None
    return str(parsed)


def _validated_category_id(value: object, field: str = "category") -> str:
    if not isinstance(value, str) or not CATEGORY_MACHINE_ID_RE.fullmatch(value):
        raise ServiceMapError(
            ServiceMapErrorCode.INVALID_REQUEST,
            f"{field} must match {CATEGORY_MACHINE_ID_RE.pattern}",
        )
    return value


def _validated_origin(value: object) -> str:
    if not isinstance(value, str) or value not in MAP_ORIGINS:
        raise ServiceMapError(
            ServiceMapErrorCode.INVALID_REQUEST,
            f"origin must be one of {sorted(MAP_ORIGINS)}",
        )
    return value


def _validated_name(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_NAME_CHARS):
        raise ServiceMapError(
            ServiceMapErrorCode.INVALID_REQUEST,
            f"name must be a string of 1..{MAX_NAME_CHARS} chars",
        )
    return value


def _validated_description(value: object, field: str = "description") -> str:
    if not isinstance(value, str) or len(value) > MAX_DESCRIPTION_CHARS:
        raise ServiceMapError(
            ServiceMapErrorCode.INVALID_REQUEST,
            f"{field} must be a string of at most "
            f"{MAX_DESCRIPTION_CHARS} chars",
        )
    return value


def _wire_ts(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory(directory: Path) -> None:
    """Persist one directory entry change (rename durability)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """One durable atomic file replace (temp + fsync + replace + dir fsync)."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _entry_of(document: Mapping[str, object], *, server_id: str) -> ServiceMapEntry:
    """Strictly type one persisted entry document (malformed = surfaced)."""
    try:
        application_id = _canonical_uuid(document["application_id"], "application_id")
        entry_server = _canonical_uuid(document["server_id"], "server_id")
        name = _validated_name(document["name"])
        origin = _validated_origin(document["origin"])
        category = _validated_category_id(document["category"])
        description = _validated_description(document["description"])
        instructions = ServiceInstructions.of(document.get("instructions"))
        raw_revision = document["revision"]
        if (
            not isinstance(raw_revision, int)
            or isinstance(raw_revision, bool)
            or raw_revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        updated_at = str(document["updated_at"])
        # Backward-compatible: records written before the catalog split have
        # no custom_instructions/installed keys and read as ""/True.
        raw_custom = document.get("custom_instructions", "")
        if not isinstance(raw_custom, str) or len(raw_custom) > MAX_INSTRUCTION_CHARS:
            raise ValueError("custom_instructions must be a bounded string")
        raw_installed = document.get("installed", True)
        if not isinstance(raw_installed, bool):
            raise ValueError("installed must be a boolean")
    except (KeyError, TypeError, ValueError, ServiceMapError) as exc:
        raise ServiceMapError(
            ServiceMapErrorCode.MALFORMED_RECORD,
            f"service map entry for server {server_id} is malformed: {exc}",
        ) from None
    if entry_server != server_id:
        raise ServiceMapError(
            ServiceMapErrorCode.MALFORMED_RECORD,
            f"service map entry for server {server_id} is stored under a "
            "different server id",
        )

    def _optional_str(field: str) -> str | None:
        value = document.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                f"service map entry for server {server_id} has a malformed "
                f"{field}",
            )
        return value

    return ServiceMapEntry(
        application_id=application_id,
        server_id=entry_server,
        name=name,
        origin=origin,
        compose_project=_optional_str("compose_project"),
        working_dir=_optional_str("working_dir"),
        unit_name=_optional_str("unit_name"),
        category=category,
        description=description,
        instructions=instructions,
        updated_at=updated_at,
        revision=raw_revision,
        custom_instructions=raw_custom,
        installed=raw_installed,
    )


class ServiceMapStore:
    """Confined service-map + category-taxonomy store (atomic JSON files).

    Constructor takes the explicit ``service_map`` directory (the wiring
    roots it at ``<profiles_root>/service_map/``) and only RESOLVES it —
    the directory tree is created lazily on the first write, so a
    read-only deployment (or an app that never maps a service) leaves
    the data root untouched. An unusable root surfaces as the
    ``OSError`` of that first write, the fail-closed composition
    contract of every other C02 store. The store touches no clock or
    network state except inside mutating calls (``updated_at``).
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve(strict=False)

    @property
    def root(self) -> Path:
        """The resolved confined root."""
        return self._root

    def _ensure_root(self) -> None:
        """Create the confined tree (first write only; the read paths
        treat an absent tree as an empty map)."""
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        (self._root / "servers").mkdir(exist_ok=True)
        os.chmod(self._root / "servers", 0o700)

    def _server_path(self, server_id: uuid.UUID) -> Path:
        return self._root / "servers" / f"{server_id}.json"

    # -- categories ---------------------------------------------------------

    def list_categories(self) -> tuple[ServiceCategory, ...]:
        """Built-ins merged with operator customs (stable default order).

        A custom record whose ``machine_id`` collides with a built-in
        overrides the label/description but keeps ``built_in=True`` (the
        built-in taxonomy line is never shadowed into looking custom);
        new customs append in file order with ``built_in=False``.
        """
        merged: dict[str, ServiceCategory] = {
            category.machine_id: category for category in DEFAULT_SERVICE_CATEGORIES
        }
        for custom in self._read_custom_categories():
            existing = merged.get(custom.machine_id)
            merged[custom.machine_id] = ServiceCategory(
                machine_id=custom.machine_id,
                label=custom.label,
                description=custom.description,
                built_in=True if existing is not None else False,
            )
        return tuple(merged.values())

    def upsert_category(
        self, machine_id: str, *, label: str, description: str = ""
    ) -> ServiceCategory:
        """Add or update one custom category (persisted to the JSON file)."""
        _validated_category_id(machine_id, "machine_id")
        if not isinstance(label, str) or not (1 <= len(label) <= MAX_LABEL_CHARS):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                f"label must be a string of 1..{MAX_LABEL_CHARS} chars",
            )
        _validated_description(description, "description")
        customs = [
            item
            for item in self._read_custom_categories()
            if item.machine_id != machine_id
        ]
        customs.append(ServiceCategory(machine_id, label, description))
        document = [item.to_json() for item in customs]
        self._ensure_root()
        _atomic_write(
            self._root / "categories.json",
            json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
        )
        return ServiceCategory(machine_id, label, description, built_in=False)

    def _read_custom_categories(self) -> list[ServiceCategory]:
        path = self._root / "categories.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                f"custom category file is unreadable: {exc}",
            ) from None
        if not isinstance(raw, list):
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                "custom category file must hold a JSON array",
            )
        customs: list[ServiceCategory] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ServiceMapError(
                    ServiceMapErrorCode.MALFORMED_RECORD,
                    "custom category records must be JSON objects",
                )
            try:
                machine_id = _validated_category_id(item["machine_id"], "machine_id")
                label = str(item["label"])
                description = str(item.get("description", ""))
            except (KeyError, ServiceMapError) as exc:
                raise ServiceMapError(
                    ServiceMapErrorCode.MALFORMED_RECORD,
                    f"custom category record is malformed: {exc}",
                ) from None
            customs.append(ServiceCategory(machine_id, label, description))
        return customs

    # -- per-server entries ---------------------------------------------------

    def list_entries(self, server_id: uuid.UUID) -> tuple[ServiceMapEntry, ...]:
        """All map entries for one server (stable name order)."""
        path = self._server_path(server_id)
        if not path.exists():
            return ()
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                f"service map for server {server_id} is unreadable: {exc}",
            ) from None
        if not isinstance(raw, list):
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                f"service map for server {server_id} must hold a JSON array",
            )
        sid = str(server_id)
        entries = [
            _entry_of(item, server_id=sid)
            for item in raw
            if isinstance(item, Mapping)
        ]
        return tuple(sorted(entries, key=lambda entry: (entry.name, entry.application_id)))

    def get_entry(
        self, server_id: uuid.UUID, application_id: uuid.UUID
    ) -> ServiceMapEntry | None:
        """One entry by identity, ``None`` when unmapped."""
        wanted = str(application_id)
        for entry in self.list_entries(server_id):
            if entry.application_id == wanted:
                return entry
        return None

    def find_entries(
        self, server_id: uuid.UUID, tags: Iterable[str]
    ) -> tuple[ServiceMapEntry, ...]:
        """Entries matching any tag (case-insensitive) by application_id,
        name, unit_name or compose_project — the chat ``@service`` chip
        resolution. Results are deduped, bounded, stable name order."""
        normalized = {
            tag.strip().lower() for tag in tags if isinstance(tag, str) and tag.strip()
        }
        if not normalized:
            return ()
        matched = [
            entry
            for entry in self.list_entries(server_id)
            if normalized
            & {
                entry.application_id.lower(),
                entry.name.lower(),
                *( [entry.unit_name.lower()] if entry.unit_name else [] ),
                *( [entry.compose_project.lower()] if entry.compose_project else [] ),
            }
        ]
        return tuple(matched[:MAX_MATCHED_ENTRIES])

    def upsert_entry(
        self,
        server_id: uuid.UUID,
        *,
        name: str,
        category: str = "system",
        origin: str = "manual",
        application_id: str | uuid.UUID | None = None,
        compose_project: str | None = None,
        working_dir: str | None = None,
        unit_name: str | None = None,
        description: str = "",
        instructions: ServiceInstructions | Mapping[str, object] | None = None,
        custom_instructions: str | None = None,
        installed: bool | None = None,
    ) -> ServiceMapEntry:
        """Add or update one service entry (revision-bumped, atomic).

        ``application_id`` omitted mints the deterministic UUID5 from
        ``(server_id, name)`` — the same service name converges on the
        same identity across upserts. The new revision is the previously
        stored revision + 1 (or 1 on create). ``custom_instructions`` and
        ``installed`` are the per-server overrides: ``None`` preserves the
        previously stored value (or the ``""``/``True`` default on create),
        so callers that do not name them never wipe server-local state.
        """
        sid = _canonical_uuid(server_id, "server_id")
        name = _validated_name(name)
        category = _validated_category_id(category)
        origin = _validated_origin(origin)
        description = _validated_description(description)
        if custom_instructions is not None:
            if (
                not isinstance(custom_instructions, str)
                or len(custom_instructions) > MAX_INSTRUCTION_CHARS
            ):
                raise ServiceMapError(
                    ServiceMapErrorCode.INVALID_REQUEST,
                    "custom_instructions must be a string of at most "
                    f"{MAX_INSTRUCTION_CHARS} chars",
                )
        if installed is not None and not isinstance(installed, bool):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "installed must be a boolean",
            )
        if isinstance(instructions, ServiceInstructions):
            parsed_instructions = instructions
        else:
            parsed_instructions = ServiceInstructions.of(instructions)
        for field, value in (
            ("compose_project", compose_project),
            ("working_dir", working_dir),
            ("unit_name", unit_name),
        ):
            if value is not None and (
                not isinstance(value, str) or not (1 <= len(value) <= 300)
            ):
                raise ServiceMapError(
                    ServiceMapErrorCode.INVALID_REQUEST,
                    f"{field} must be a string of 1..300 chars or null",
                )
        if application_id is None:
            application_id = uuid.uuid5(
                uuid.UUID(sid), f"service-map:{name.strip().lower()}"
            )
        aid = _canonical_uuid(application_id, "application_id")

        entries = [
            entry
            for entry in self.list_entries(server_id)
            if entry.application_id != aid
        ]
        if len(entries) >= MAX_ENTRIES_PER_SERVER:
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                f"a server map holds at most {MAX_ENTRIES_PER_SERVER} entries",
            )
        previous = self.get_entry(server_id, uuid.UUID(aid))
        entry = ServiceMapEntry(
            application_id=aid,
            server_id=sid,
            name=name,
            origin=origin,
            compose_project=compose_project,
            working_dir=working_dir,
            unit_name=unit_name,
            category=category,
            description=description,
            instructions=parsed_instructions,
            updated_at=_wire_ts(datetime.now(timezone.utc)),
            revision=1 if previous is None else previous.revision + 1,
            custom_instructions=(
                custom_instructions
                if custom_instructions is not None
                else ("" if previous is None else previous.custom_instructions)
            ),
            installed=(
                installed
                if installed is not None
                else (True if previous is None else previous.installed)
            ),
        )
        document = [item.to_json() for item in entries] + [entry.to_json()]
        self._ensure_root()
        _atomic_write(
            self._server_path(server_id),
            json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
        )
        return entry

    def delete_entry(
        self, server_id: uuid.UUID, application_id: str | uuid.UUID
    ) -> bool:
        """Remove one service entry by identity (atomic).

        Returns ``True`` when an existing entry was removed, ``False``
        when no entry matched ``application_id``.
        """
        _canonical_uuid(server_id, "server_id")
        aid = _canonical_uuid(application_id, "application_id")
        existing = self.list_entries(server_id)
        remaining = [entry for entry in existing if entry.application_id != aid]
        if len(remaining) == len(existing):
            return False
        document = [item.to_json() for item in remaining]
        self._ensure_root()
        _atomic_write(
            self._server_path(server_id),
            json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
        )
        return True

    # -- global runbook catalog ------------------------------------------------

    def _catalog_path(self) -> Path:
        return self._root / "catalog.json"

    def _read_catalog(self) -> list[CatalogRunbook]:
        """The persisted catalog records in file order (absent file = empty)."""
        path = self._catalog_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                f"runbook catalog file is unreadable: {exc}",
            ) from None
        if not isinstance(raw, list):
            raise ServiceMapError(
                ServiceMapErrorCode.MALFORMED_RECORD,
                "runbook catalog file must hold a JSON array",
            )
        return [
            _catalog_runbook_of(item)
            for item in raw
            if isinstance(item, Mapping)
        ]

    def _write_catalog(self, runbooks: list[CatalogRunbook]) -> None:
        self._ensure_root()
        _atomic_write(
            self._catalog_path(),
            json.dumps(
                [item.to_json() for item in runbooks], indent=2, sort_keys=True
            ).encode("utf-8"),
        )

    def _known_server_entries(self) -> tuple[ServiceMapEntry, ...]:
        """Every per-server entry across all servers (stable identity order).

        Powers the catalog's automatic seeding: a service mapped on any
        server before the catalog existed shows up as a catalog runbook
        without a migration. A server file whose stem is not a UUID is
        foreign debris and is skipped; a malformed record inside a real
        server file surfaces (the store's fail-honest discipline).
        """
        servers_dir = self._root / "servers"
        if not servers_dir.exists():
            return ()
        entries: list[ServiceMapEntry] = []
        for path in sorted(servers_dir.glob("*.json")):
            try:
                server_id = uuid.UUID(path.stem)
            except ValueError:
                continue
            if path.name != f"{server_id}.json":
                continue
            entries.extend(self.list_entries(server_id))
        return tuple(sorted(entries, key=lambda e: (e.name.lower(), e.application_id)))

    def list_catalog_runbooks(self) -> tuple[CatalogRunbook, ...]:
        """The merged global catalog (stable name order).

        ``catalog.json`` records come first in authority: a persisted
        runbook shadows any server map entry of the same name (case-
        insensitive) or id. Every remaining server map entry — services
        that existed before the catalog — is synthesized as a runbook
        (its entry id stands in as the runbook id), so existing services
        are automatically in the catalog.
        """
        persisted = self._read_catalog()
        taken_ids = {runbook.runbook_id for runbook in persisted}
        taken_names = {runbook.name.lower() for runbook in persisted}
        merged = list(persisted)
        for entry in self._known_server_entries():
            if entry.application_id in taken_ids:
                continue
            if entry.name.lower() in taken_names:
                continue
            merged.append(
                CatalogRunbook(
                    runbook_id=entry.application_id,
                    name=entry.name,
                    category=entry.category,
                    description=entry.description,
                    instructions=entry.instructions,
                    updated_at=entry.updated_at,
                    revision=entry.revision,
                )
            )
            taken_ids.add(entry.application_id)
            taken_names.add(entry.name.lower())
        return tuple(
            sorted(merged, key=lambda r: (r.name.lower(), r.runbook_id))
        )

    def get_catalog_runbook(
        self, runbook_id: str | uuid.UUID
    ) -> CatalogRunbook | None:
        """One catalog runbook by identity, ``None`` when absent."""
        wanted = _canonical_uuid(runbook_id, "runbook_id")
        for runbook in self.list_catalog_runbooks():
            if runbook.runbook_id == wanted:
                return runbook
        return None

    def upsert_catalog_runbook(
        self,
        *,
        name: str,
        category: str = "system",
        description: str = "",
        instructions: ServiceInstructions | Mapping[str, object] | None = None,
        runbook_id: str | uuid.UUID | None = None,
    ) -> CatalogRunbook:
        """Add or update one catalog runbook (revision-bumped, atomic).

        ``runbook_id`` omitted mints the deterministic UUID5 from the
        catalog namespace and the lowercased name, so re-upserting a run
        of the same name converges on one record. Server map entries are
        never touched from here — the catalog is the global half only.
        """
        name = _validated_name(name)
        category = _validated_category_id(category)
        description = _validated_description(description)
        if isinstance(instructions, ServiceInstructions):
            parsed_instructions = instructions
        else:
            parsed_instructions = ServiceInstructions.of(instructions)
        rid = (
            _canonical_uuid(runbook_id, "runbook_id")
            if runbook_id is not None
            else str(
                uuid.uuid5(uuid.UUID(int=0), f"runbook-catalog:{name.strip().lower()}")
            )
        )
        existing = self._read_catalog()
        runbooks = [item for item in existing if item.runbook_id != rid]
        previous = next((item for item in existing if item.runbook_id == rid), None)
        runbook = CatalogRunbook(
            runbook_id=rid,
            name=name,
            category=category,
            description=description,
            instructions=parsed_instructions,
            updated_at=_wire_ts(datetime.now(timezone.utc)),
            revision=1 if previous is None else previous.revision + 1,
        )
        runbooks.append(runbook)
        self._write_catalog(runbooks)
        return runbook

    def delete_catalog_runbook(self, runbook_id: str | uuid.UUID) -> bool:
        """Remove one catalog runbook (catalog.json ONLY).

        Server map entries are intentionally untouched: removing the
        global recipe never uninstalls or unmapped a service. Returns
        ``True`` when a persisted record was removed, ``False`` when the
        id named no catalog record (a synthesized server-entry projection
        is not deletable from the catalog).
        """
        rid = _canonical_uuid(runbook_id, "runbook_id")
        existing = self._read_catalog()
        remaining = [item for item in existing if item.runbook_id != rid]
        if len(remaining) == len(existing):
            return False
        self._write_catalog(remaining)
        return True


# ---------------------------------------------------------------------------
# The ``app.upsert_map_entry`` capability (agent-facing seam)
# ---------------------------------------------------------------------------


def upsert_map_descriptor() -> CapabilityDescriptor:
    """The validated ``app.upsert_map_entry`` descriptor (C03 rule 1).

    ``effect_class="mutate"`` is the load-bearing field: the capability
    writes controller-local service-map state, so the policy layer routes
    it through the mutating lane. (The card vocabulary names a
    ``risk_tier``; the frozen descriptor registry owns no such field —
    the effect class plus the ``service_map`` resource scope carry the
    routing decision, a recorded contract adaptation.) The target is
    exclusively the context's ``server_id`` — the payload names no host
    and cannot retarget the call (C03 rule 2).
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Create or update a Service Map entry for a service on the "
                    "target server, including its category, origin, description, "
                    "and operational runbook instructions."
                ),
                "required": ["name"],
                "properties": {
                    "application_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Optional canonical UUID; omitted mints the "
                            "deterministic UUID5 from (server_id, name)."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_NAME_CHARS,
                        "description": "The service display name.",
                    },
                    "category": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": CATEGORY_MACHINE_ID_RE.pattern,
                        "description": (
                            "The taxonomy machine_id (built-in or custom)."
                        ),
                    },
                    "origin": {
                        "type": "string",
                        "enum": sorted(MAP_ORIGINS),
                        "description": "The service origin vocabulary.",
                    },
                    "compose_project": {
                        "type": ["string", "null"],
                        "description": "The Compose project selector.",
                    },
                    "working_dir": {
                        "type": ["string", "null"],
                        "description": "The service working directory.",
                    },
                    "unit_name": {
                        "type": ["string", "null"],
                        "description": "The systemd unit selector.",
                    },
                    "description": {
                        "type": "string",
                        "maxLength": MAX_DESCRIPTION_CHARS,
                        "description": "Free-text service description.",
                    },
                    "custom_instructions": {
                        "type": "string",
                        "maxLength": MAX_INSTRUCTION_CHARS,
                        "description": (
                            "Server-specific operational notes injected into the "
                            "agent's service context alongside the global description "
                            "and runbook instructions."
                        ),
                    },
                    "installed": {
                        "type": "boolean",
                        "description": (
                            "Whether the service is currently installed on this "
                            "host; false marks a catalog runbook staged but not "
                            "yet installed."
                        ),
                    },
                    "instructions": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            key: {
                                "type": "string",
                                "maxLength": MAX_INSTRUCTION_CHARS,
                                "description": (
                                    f"Operational {key} instructions and verification steps for this service."
                                ),
                            }
                            for key in INSTRUCTION_KEYS
                        },
                        "description": (
                            "Operational runbook: install/update/uninstall/"
                            "backup/verify."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "application_id",
                    "server_id",
                    "name",
                    "origin",
                    "category",
                    "revision",
                    "updated_at",
                    "created",
                ],
                "properties": {
                    "application_id": {"type": "string", "format": "uuid"},
                    "server_id": {"type": "string", "format": "uuid"},
                    "name": {"type": "string"},
                    "origin": {"type": "string", "enum": sorted(MAP_ORIGINS)},
                    "category": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "updated_at": {"type": "string"},
                    "custom_instructions": {"type": "string"},
                    "installed": {"type": "boolean"},
                    "created": {"type": "boolean"},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "mutate",
            "resource_scope": ["service_map"],
            "required_grants": [CAPABILITY_ID],
        }
    )


class ServiceMapUpsertCapability:
    """``app.upsert_map_entry`` executor: one gated call → one entry.

    Reachable only through the dispatcher (C03 rule 7); performs no
    policy decision of its own — the :class:`ExecutionContext` supplies
    the only server vocabulary (the upsert always targets
    ``context.server_id``) and invalid payloads are structured
    :class:`ServiceMapError` rejections the dispatcher maps honestly.
    """

    def __init__(self, store: ServiceMapStore) -> None:
        if not isinstance(store, ServiceMapStore):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "store must be a ServiceMapStore",
            )
        self._store = store

    def __repr__(self) -> str:
        return f"<ServiceMapUpsertCapability root={self._store.root!s}>"

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated upsert (see module docstring for the shape)."""
        if not isinstance(call, CapabilityCall):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "call context must be an ExecutionContext",
            )
        payload = call.payload
        if not isinstance(payload, Mapping):
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                "payload must be a JSON object",
            )
        unknown = set(payload) - {
            "application_id",
            "name",
            "category",
            "origin",
            "compose_project",
            "working_dir",
            "unit_name",
            "description",
            "instructions",
            "custom_instructions",
            "installed",
        }
        if unknown:
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST,
                f"payload has unknown keys: {sorted(unknown)}",
            )
        if "name" not in payload:
            raise ServiceMapError(
                ServiceMapErrorCode.INVALID_REQUEST, "name is required"
            )
        application_id = payload.get("application_id")
        entry = self._store.upsert_entry(
            context.server_id,
            name=payload["name"],
            category=payload.get("category", "system"),
            origin=payload.get("origin", "manual"),
            application_id=None if application_id is None else application_id,
            compose_project=payload.get("compose_project"),
            working_dir=payload.get("working_dir"),
            unit_name=payload.get("unit_name"),
            description=payload.get("description", ""),
            instructions=payload.get("instructions"),
            custom_instructions=payload.get("custom_instructions"),
            installed=payload.get("installed"),
        )
        previous_revision = entry.revision - 1
        summary = {
            "status": "ok",
            "application_id": entry.application_id,
            "server_id": entry.server_id,
            "name": entry.name,
            "origin": entry.origin,
            "category": entry.category,
            "revision": entry.revision,
            "updated_at": entry.updated_at,
            "custom_instructions": entry.custom_instructions,
            "installed": entry.installed,
            "created": previous_revision < 1,
        }
        return ExecutorResult(
            ok=True, output=json.dumps(summary, sort_keys=True)
        )
