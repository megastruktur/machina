"""Explicit application adoption: canonical manifest + SQL projection.

Card ``08-adoption``; consumer of the integrated producers
``mai.applications.discover`` (observed records + adoption-time reference
vocabulary) and the C02 storage pattern established by ``mai.profiles.files``
/ ``mai.profiles.projection``. Contracts.md:

- **C09 rule 1** — adoption is an EXPLICIT admin action. The input is an
  :class:`AdoptionRequest` the caller (API card, operator CLI) builds from an
  explicit decision; nothing in this module accepts a
  :class:`~mai.applications.discover.DiscoveryInventory` blob and nothing
  mints grants or enables execution. The flow runs one way only: adoption
  records FEED discovery's ``managed`` tagging via
  :meth:`ApplicationAdoption.managed_references`; discovery output never
  mutates anything (acceptance 2).
- **C02 rules 2–3** — canonical path scheme
  ``servers/<server_id>/applications/<application_id>/manifest.yaml`` comes
  verbatim from :func:`canonical_manifest_relative_path`, which accepts only
  :class:`uuid.UUID` identities, so traversal/absolute user paths are
  unrepresentable by construction. Symlinked path components are rejected at
  run time (``path_escape``); reads open with ``O_NOFOLLOW``.
- **C02 rule 4** — canonical version triple (integer content ``revision``
  starting at 1, ``schema_revision`` = :data:`MANIFEST_SCHEMA_VERSION`,
  SHA-256 content hash) lives in the sidecar
  ``servers/<server_id>/applications/<application_id>/manifest.meta.json``.
  The Postgres projection (:meth:`ApplicationAdoption.read`) stores the same
  triple plus the projected manifest fields — never a second copy of the
  document text.
- **C02 rule 5 / crash windows** — the frozen journal phases
  ``prepared -> file_committed -> reconciled``. Atomic replace is temp file +
  fsync + ``os.replace`` + directory fsync; the manifest is replaced BEFORE
  its metadata (W2: the file is authoritative; an interrupted pair is
  reported as ``hash_mismatch``/``metadata_missing``, never silently
  reconciled). Projection write and journal advance share ONE transaction
  (W3). Revocation journals the tombstone BEFORE the file is removed and
  purges the projection only after file absence confirms (W4); a manifest
  resurrected under a live tombstone is a surfaced ``tombstone_conflict``.
  :meth:`ApplicationAdoption.recover` is the worker routine for the W1/W2
  windows: it reconciles from the file's ACTUAL hash/revision, never from an
  intent's claim ("no invented successful sync").
- **C09 rule 6** — external Compose projects stay at their ORIGINAL target
  paths. The manifest's ``external_paths`` section records those paths as
  REFERENCED METADATA (paths only, never contents — the discovery safe view
  carries through); this module has no code path that opens, copies, moves
  or deletes anything under them. Drift observation/reconciliation belongs
  to the maintenance cards, not here.
- **C09 rule 2 boundary** — recipe fields (deploy semantics, health checks,
  update/removal plans) are a separate frozen producer (``08-recipe-schema``);
  the adoption manifest deliberately carries none of them.
- **C01 rules 1/3/6** — opaque UUID identities only; every rejection is a
  value-free :class:`AdoptionError` with a stable code (no manifest content,
  no paths, no secrets in messages); a stale caller revision conflicts
  (``stale_revision``) instead of silently clobbering.

Duplicate/stale/ambiguous adoption handling (acceptance 2):

- a live record for the same ``application_id`` with
  ``expected_revision = 0`` is a duplicate create: ``already_adopted``;
- a caller whose ``expected_revision`` differs from the live revision gets
  ``stale_revision`` (update requires the observed revision);
- a live projection row OR canonical manifest for the same
  ``application_id`` under a DIFFERENT server is refused with
  ``cross_server_conflict`` — adoption never relocates an identity across
  servers (relocation would reset the revision chain and orphan the
  original server's canonical pair and managed references); the projection
  upsert is server-safe as a storage backstop (a conflicting row is never
  re-owned, a refused reconcile aborts and stays surfaced);
- selectors (compose project / normalized working dir / unit name) that
  overlap ANOTHER live adoption on the same server are refused with
  ``reference_conflict`` — this is the ambiguity resolution the discovery
  card deferred to adoption: two managed records must never claim one
  identity, otherwise :func:`mai.applications.discover.classify_managed`
  would resolve the overlap by arbitrary id order.

Origin scope: adoption supports exactly the origins discovery can tag
``managed`` — ``compose`` (project name / working dir selectors) and
``systemd_unit`` (unit name selector). Standalone containers have no frozen
selector (discovery keeps them ``discovered``), so adopting them is refused
with ``unsupported_origin`` instead of creating a record that could never be
matched again.

Single-writer discipline: per-server mutation serialization is a caller
invariant (C05 rule 6, engine locks); the journal's partial unique index
(one live intent per ``(server_id, application_id)``) and the projection
overlap check are storage backstops, not a distributed lock.

POSIX/Postgres assumptions mirror the profile store: ``O_NOFOLLOW`` and
directory fsync (Windows is a frozen non-goal); PostgreSQL via
:func:`mai.database.create_app_engine` is the only SQL dialect. No module
state, no clock in the file half (projection timestamps are SQL-owned), no
environment reads: results derive from explicit arguments, filesystem state
and the database only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Sequence

import yaml
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaml.nodes import MappingNode, Node, SequenceNode

from mai.applications.discover import ApplicationOrigin, ManagedReference

__all__ = [
    "ABSENT_REVISION",
    "APPLICATIONS_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_META_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "OP_DELETE",
    "OP_UPSERT",
    "PHASE_FILE_COMMITTED",
    "PHASE_PREPARED",
    "PHASE_RECONCILED",
    "AdoptedApplication",
    "AdoptionError",
    "AdoptionErrorCode",
    "AdoptionManifest",
    "AdoptionRequest",
    "AdoptionResult",
    "ApplicationAdoption",
    "ApplicationManifestStore",
    "JournalIntent",
    "RecoveryOutcome",
    "StoredManifest",
    "canonical_manifest_relative_path",
    "canonical_meta_relative_path",
    "parse_manifest_text",
    "render_manifest",
]

# ---------------------------------------------------------------------------
# Frozen constants (C02 rule 2 canonical scheme + rule 4 version triple)
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = "manifest.yaml"
MANIFEST_META_FILENAME = "manifest.meta.json"
APPLICATIONS_DIR = "applications"
MANIFEST_SCHEMA_VERSION = 1
META_FORMAT = 1

#: Byte cap for one canonical manifest, enforced before parsing (same
#: resource-exhaustion boundary as the profile store; an adoption manifest is
#: a small admin-controlled document).
MAX_DOCUMENT_BYTES = 64 * 1024

#: Revision convention for "no canonical manifest stored" (create baseline).
ABSENT_REVISION = 0

#: Journal operations (C02 rule 5 frozen shape).
OP_UPSERT = "upsert"
OP_DELETE = "delete"

#: Frozen journal phases (C02 rule 5).
PHASE_PREPARED = "prepared"
PHASE_FILE_COMMITTED = "file_committed"
PHASE_RECONCILED = "reconciled"

_LIVE_PHASES = (PHASE_PREPARED, PHASE_FILE_COMMITTED)

_NAME_MAX = 200
_SELECTOR_MAX = 512
_PATH_MAX = 1024
_PATH_LIST_MAX = 64

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "application_id",
        "server_id",
        "name",
        "origin",
        "reference",
        "external_paths",
    }
)
_REFERENCE_FIELDS = frozenset({"compose_project", "working_dir", "unit_name"})
_EXTERNAL_FIELDS = frozenset({"working_dir", "config_files", "environment_files"})

_HEX_DIGITS = frozenset("0123456789abcdef")


class AdoptionErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    # request validation
    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_APPLICATION_ID = "invalid_application_id"
    INVALID_REVISION = "invalid_revision"
    INVALID_NAME = "invalid_name"
    UNSUPPORTED_ORIGIN = "unsupported_origin"
    INVALID_REFERENCE = "invalid_reference"
    INVALID_EXTERNAL_PATH = "invalid_external_path"
    # canonical document
    UNPARSABLE_DOCUMENT = "unparsable_document"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DOCUMENT_ALIAS_FORBIDDEN = "document_alias_forbidden"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_FIELD = "invalid_field"
    SERVER_ID_MISMATCH = "server_id_mismatch"
    APPLICATION_ID_MISMATCH = "application_id_mismatch"
    # canonical store
    PATH_ESCAPE = "path_escape"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    ORPHAN_MANIFEST = "orphan_manifest"
    HASH_MISMATCH = "hash_mismatch"
    # adoption decisions
    ALREADY_ADOPTED = "already_adopted"
    REFERENCE_CONFLICT = "reference_conflict"
    CROSS_SERVER_CONFLICT = "cross_server_conflict"
    NOT_ADOPTED = "not_adopted"
    # journal
    INTENT_NOT_FOUND = "intent_not_found"
    INTENT_PENDING = "intent_pending"
    INTENT_MISMATCH = "intent_mismatch"
    INTENT_PHASE = "intent_phase"
    PAYLOAD_MISMATCH = "payload_mismatch"
    FILE_DIVERGED = "file_diverged"
    TOMBSTONE_CONFLICT = "tombstone_conflict"


class AdoptionError(ValueError):
    """One structured, value-free adoption rejection.

    ``detail`` never contains manifest field values (names, paths, selector
    values): transcripts and API bodies stay non-disclosing.
    """

    def __init__(self, code: AdoptionErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Request / manifest records (explicit decision -> canonical document)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdoptionRequest:
    """One explicit adoption decision (C09 rule 1).

    Built by the caller from an admin's decision — typically after inspecting
    a :class:`~mai.applications.discover.DiscoveryInventory` — never derived
    from one automatically. ``expected_revision`` follows C01 rule 6:
    ``0`` creates, otherwise it must equal the live record's revision
    (a stale caller conflicts).
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    name: str
    origin: ApplicationOrigin
    compose_project: str | None = None
    working_dir: str | None = None
    unit_name: str | None = None
    external_working_dir: str | None = None
    external_config_files: tuple[str, ...] = ()
    external_environment_files: tuple[str, ...] = ()
    expected_revision: int = ABSENT_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, uuid.UUID):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_SERVER_ID,
                "server_id must be a uuid.UUID instance",
            )
        if not isinstance(self.application_id, uuid.UUID):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_APPLICATION_ID,
                "application_id must be a uuid.UUID instance",
            )
        _validated_name(self.name)
        if not isinstance(self.origin, ApplicationOrigin):
            raise AdoptionError(
                AdoptionErrorCode.UNSUPPORTED_ORIGIN,
                "origin must be an ApplicationOrigin value",
            )
        if self.origin is ApplicationOrigin.DOCKER_CONTAINER:
            raise AdoptionError(
                AdoptionErrorCode.UNSUPPORTED_ORIGIN,
                "standalone containers have no frozen selector and cannot be "
                "adopted; only compose projects and systemd units can become "
                "managed records",
            )
        selectors = 0
        for field in ("compose_project", "working_dir", "unit_name"):
            value = getattr(self, field)
            if value is not None:
                _validated_selector(field, value)
                selectors += 1
        if selectors == 0:
            raise AdoptionError(
                AdoptionErrorCode.INVALID_REFERENCE,
                "an adoption request needs at least one reference selector "
                "(compose_project, working_dir or unit_name)",
            )
        _validated_path_value("external_working_dir", self.external_working_dir)
        for field, values in (
            ("external_config_files", self.external_config_files),
            ("external_environment_files", self.external_environment_files),
        ):
            if not isinstance(values, tuple):
                raise AdoptionError(
                    AdoptionErrorCode.INVALID_EXTERNAL_PATH,
                    f"{field} must be a tuple of strings",
                )
            if len(values) > _PATH_LIST_MAX:
                raise AdoptionError(
                    AdoptionErrorCode.INVALID_EXTERNAL_PATH,
                    f"{field} exceeds the entry limit",
                )
            for value in values:
                _validated_path_value(field, value)
        if (
            not isinstance(self.expected_revision, int)
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 0
        ):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_REVISION,
                "expected_revision must be a non-negative int",
            )


@dataclass(frozen=True, slots=True)
class AdoptionManifest:
    """One parsed canonical adoption manifest (strict shape)."""

    schema_version: int
    server_id: uuid.UUID
    application_id: uuid.UUID
    name: str
    origin: ApplicationOrigin
    compose_project: str | None
    working_dir: str | None
    unit_name: str | None
    external_working_dir: str | None
    external_config_files: tuple[str, ...]
    external_environment_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredManifest:
    """Canonical manifest as stored: text plus the C02 rule-4 version triple."""

    server_id: uuid.UUID
    application_id: uuid.UUID
    text: str
    revision: int
    schema_revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AdoptedApplication:
    """Projected adoption record (C02 rule 4) with the W2 staleness flag.

    ``stale`` is True while a live (non-reconciled) journal intent exists for
    the application: consumers must treat the record as not yet reconciled.
    """

    application_id: uuid.UUID
    server_id: uuid.UUID
    name: str
    origin: ApplicationOrigin
    compose_project: str | None
    working_dir: str | None
    unit_name: str | None
    external_working_dir: str | None
    external_config_files: tuple[str, ...]
    external_environment_files: tuple[str, ...]
    revision: int
    schema_revision: int
    sha256: str
    adopted_at: datetime
    reconciled_at: datetime
    stale: bool


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    """Outcome of one ``adopt``/``revoke`` call (idempotent pipeline)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    application_id: uuid.UUID
    op: str
    #: ``applied`` (fresh full pipeline), ``resumed`` (completed a
    #: pre-existing live intent) or ``already_reconciled`` (retry of a done
    #: intent; no effect).
    outcome: str
    revision: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class JournalIntent:
    """One durable write intent (C02 rule 5 journal row)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    application_id: uuid.UUID
    op: str
    phase: str
    expected_revision: int
    base_sha256: str | None
    target_revision: int | None
    target_sha256: str | None
    schema_revision: int | None
    payload: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Per-intent result of one recovery sweep (value-free)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    application_id: uuid.UUID
    op: str
    #: ``advanced_reconciled`` | ``reexecuted_reconciled`` | ``conflict`` |
    #: ``error``.
    status: str
    code: str | None
    detail: str
    revision: int | None


# ---------------------------------------------------------------------------
# Request/value validation (value-free; no effect before success)
# ---------------------------------------------------------------------------


def _validated_name(name: object) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= _NAME_MAX:
        raise AdoptionError(
            AdoptionErrorCode.INVALID_NAME,
            f"name must be a string of 1 to {_NAME_MAX} printable characters",
        )
    if not name.isprintable():
        raise AdoptionError(
            AdoptionErrorCode.INVALID_NAME,
            "name must not contain control characters",
        )
    return name


def _validated_selector(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise AdoptionError(
            AdoptionErrorCode.INVALID_REFERENCE,
            f"{field} must be None or a non-empty printable string",
        )
    if len(value) > _SELECTOR_MAX:
        raise AdoptionError(
            AdoptionErrorCode.INVALID_REFERENCE,
            f"{field} exceeds the length limit",
        )
    return value


def _validated_path_value(field: str, value: object) -> None:
    """External deployment paths are recorded references (C09 rule 6).

    They are NEVER opened, resolved or joined by the controller — the bounds
    are record hygiene only (printable, bounded), so exotic-but-real
    target-side paths (spaces, unicode) stay recordable verbatim.
    """
    if value is None:
        return
    if not isinstance(value, str) or not value or not value.isprintable():
        raise AdoptionError(
            AdoptionErrorCode.INVALID_EXTERNAL_PATH,
            f"{field} entries must be non-empty printable strings",
        )
    if len(value) > _PATH_MAX:
        raise AdoptionError(
            AdoptionErrorCode.INVALID_EXTERNAL_PATH,
            f"{field} entry exceeds the length limit",
        )


def _validated_server_id(server_id: object) -> uuid.UUID:
    if not isinstance(server_id, uuid.UUID):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_SERVER_ID,
            "server_id must be a uuid.UUID instance",
        )
    return server_id


def _validated_application_id(application_id: object) -> uuid.UUID:
    if not isinstance(application_id, uuid.UUID):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_APPLICATION_ID,
            "application_id must be a uuid.UUID instance",
        )
    return application_id


def _validated_expected_revision(expected_revision: object) -> int:
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_REVISION,
            "expected_revision must be a non-negative int",
        )
    return expected_revision


def _validated_intent_id(intent_id: object) -> uuid.UUID:
    if not isinstance(intent_id, uuid.UUID):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_APPLICATION_ID,
            "intent_id must be a uuid.UUID instance",
        )
    return intent_id


def _is_canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


# ---------------------------------------------------------------------------
# Canonical document: render + strict parse (pure)
# ---------------------------------------------------------------------------


def render_manifest(request: AdoptionRequest) -> str:
    """Render the canonical manifest text for one adoption request.

    Deterministic (sorted keys, block style): the same request always
    produces the same bytes, so the stored hash is stable across re-adoption
    of identical data. External paths are recorded verbatim (C09 rule 6);
    only paths, never file contents.
    """
    reference: dict[str, str] = {}
    for field in ("compose_project", "working_dir", "unit_name"):
        value = getattr(request, field)
        if value is not None:
            reference[field] = value
    external: dict[str, object] = {}
    if request.external_working_dir is not None:
        external["working_dir"] = request.external_working_dir
    external["config_files"] = list(request.external_config_files)
    external["environment_files"] = list(request.external_environment_files)
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "application_id": str(request.application_id),
        "server_id": str(request.server_id),
        "name": request.name,
        "origin": request.origin.value,
        "reference": reference,
        "external_paths": external,
    }
    return yaml.safe_dump(
        document, sort_keys=True, default_flow_style=False, allow_unicode=True
    )


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate and unhashable mapping keys.

    PyYAML would otherwise silently keep the last of duplicate keys —
    unacceptable for identity-bearing canonical files (same pattern as the
    profile schema producer).
    """

    def construct_mapping(self, node: MappingNode, deep: bool = False):  # type: ignore[override]
        seen: list[object] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                raise yaml.YAMLError("unhashable mapping key") from None
            if duplicate:
                raise yaml.YAMLError("duplicate mapping key")
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def parse_manifest_text(text: str) -> AdoptionManifest:
    """Strictly parse and validate one canonical manifest document.

    Unknown fields are rejected, never ignored (windowed ADR §8 rule 2
    applied to canonical data); the embedded identities must be well-formed
    UUIDs; the origin vocabulary is restricted to the adoptable origins
    (standalone containers are refused — module docstring). Rejections are
    value-free :class:`AdoptionError`s.
    """
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError:
        raise AdoptionError(
            AdoptionErrorCode.UNPARSABLE_DOCUMENT,
            "manifest is not a valid strict YAML document",
        ) from None
    if not isinstance(document, dict):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_FIELD,
            "manifest must be a mapping",
        )
    for key in document:
        if not isinstance(key, str) or key not in _MANIFEST_FIELDS:
            raise AdoptionError(
                AdoptionErrorCode.UNKNOWN_FIELD,
                "unknown manifest field (unknown fields are rejected, never "
                "ignored)",
            )
    for key in _MANIFEST_FIELDS:
        if key not in document:
            raise AdoptionError(
                AdoptionErrorCode.MISSING_FIELD,
                f"missing required manifest field {key!r}",
            )
    if document["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise AdoptionError(
            AdoptionErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}",
        )
    try:
        server_id = uuid.UUID(str(document["server_id"]))
        application_id = uuid.UUID(str(document["application_id"]))
    except ValueError:
        raise AdoptionError(
            AdoptionErrorCode.INVALID_FIELD,
            "manifest identities must be canonical UUID strings",
        ) from None
    name = _validated_name(document["name"])
    try:
        origin = ApplicationOrigin(str(document["origin"]))
    except ValueError:
        raise AdoptionError(
            AdoptionErrorCode.INVALID_FIELD,
            "manifest origin is not a known discovery origin",
        ) from None
    if origin is ApplicationOrigin.DOCKER_CONTAINER:
        raise AdoptionError(
            AdoptionErrorCode.UNSUPPORTED_ORIGIN,
            "standalone-container manifests are not adoptable records",
        )
    reference = document["reference"]
    if not isinstance(reference, dict):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_FIELD,
            "manifest reference must be a mapping",
        )
    for key in reference:
        if not isinstance(key, str) or key not in _REFERENCE_FIELDS:
            raise AdoptionError(
                AdoptionErrorCode.UNKNOWN_FIELD,
                "unknown reference selector",
            )
    selectors: dict[str, str | None] = {}
    for field in _REFERENCE_FIELDS:
        value = reference.get(field)
        if value is None:
            selectors[field] = None
            continue
        selectors[field] = _validated_selector(field, value)
    if all(value is None for value in selectors.values()):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_REFERENCE,
            "a manifest needs at least one reference selector",
        )
    external = document["external_paths"]
    if not isinstance(external, dict):
        raise AdoptionError(
            AdoptionErrorCode.INVALID_FIELD,
            "manifest external_paths must be a mapping",
        )
    for key in external:
        if not isinstance(key, str) or key not in _EXTERNAL_FIELDS:
            raise AdoptionError(
                AdoptionErrorCode.UNKNOWN_FIELD,
                "unknown external_paths field",
            )
    external_working_dir = external.get("working_dir")
    _validated_path_value("external_paths.working_dir", external_working_dir)
    file_lists: dict[str, tuple[str, ...]] = {}
    for field in ("config_files", "environment_files"):
        raw = external.get(field, [])
        if not isinstance(raw, list) or len(raw) > _PATH_LIST_MAX:
            raise AdoptionError(
                AdoptionErrorCode.INVALID_FIELD,
                f"external_paths.{field} must be a bounded list of strings",
            )
        for entry in raw:
            _validated_path_value(f"external_paths.{field}", entry)
        file_lists[field] = tuple(str(entry) for entry in raw)
    return AdoptionManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        server_id=server_id,
        application_id=application_id,
        name=name,
        origin=origin,
        compose_project=selectors["compose_project"],
        working_dir=selectors["working_dir"],
        unit_name=selectors["unit_name"],
        external_working_dir=(
            str(external_working_dir) if external_working_dir is not None else None
        ),
        external_config_files=file_lists["config_files"],
        external_environment_files=file_lists["environment_files"],
    )


def canonical_manifest_relative_path(
    server_id: uuid.UUID, application_id: uuid.UUID
) -> str:
    """Canonical manifest path (C02 rule 2) below the approved data root.

    Accepts only :class:`uuid.UUID` identities: the path is derived from the
    opaque IDs alone, so traversal/absolute-path injection has no
    representation.
    """
    if not isinstance(server_id, uuid.UUID):
        raise TypeError("server_id must be a uuid.UUID (C02 rule 3)")
    if not isinstance(application_id, uuid.UUID):
        raise TypeError("application_id must be a uuid.UUID (C02 rule 3)")
    return (
        f"servers/{server_id}/{APPLICATIONS_DIR}/{application_id}/{MANIFEST_FILENAME}"
    )


def canonical_meta_relative_path(
    server_id: uuid.UUID, application_id: uuid.UUID
) -> str:
    """Sidecar metadata path (C02 rule 4) below the approved data root."""
    if not isinstance(server_id, uuid.UUID):
        raise TypeError("server_id must be a uuid.UUID (C02 rule 3)")
    if not isinstance(application_id, uuid.UUID):
        raise TypeError("application_id must be a uuid.UUID (C02 rule 3)")
    return (
        f"servers/{server_id}/{APPLICATIONS_DIR}/{application_id}"
        f"/{MANIFEST_META_FILENAME}"
    )


# ---------------------------------------------------------------------------
# Confined atomic canonical manifest storage (C02 rules 2–4)
# ---------------------------------------------------------------------------


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (rename/unlink durability)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _contains_alias(root: Node) -> bool:
    """Detect YAML alias references before any construction (billion-laughs
    hardening; node-identity reuse proves an alias — profile store pattern)."""
    seen: set[int] = set()
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            return True
        seen.add(id(node))
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                stack.append(key_node)
                stack.append(value_node)
        elif isinstance(node, SequenceNode):
            stack.extend(node.value)
    return False


def _checked_document(text: str) -> bytes:
    """Byte cap + alias/depth hardening before any parse or filesystem effect."""
    data = text.encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise AdoptionError(
            AdoptionErrorCode.DOCUMENT_TOO_LARGE,
            f"manifest is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return data
    except RecursionError:
        raise AdoptionError(
            AdoptionErrorCode.DOCUMENT_TOO_DEEP,
            "manifest nesting exceeds the supported depth",
        ) from None
    if node is not None and _contains_alias(node):
        raise AdoptionError(
            AdoptionErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
            "manifest contains YAML alias nodes; anchor/alias expansion is "
            "not permitted in canonical files",
        )
    return data


class ApplicationManifestStore:
    """Confined, revision-checked, atomic canonical manifest storage (C02).

    Structural mirror of :class:`mai.profiles.files.ProfileFileStore` for the
    application manifest path scheme: same confinement (symlink-component
    rejection, ``O_NOFOLLOW`` reads, restrictive modes), same pairing rules
    for the revision sidecar, same atomic temp+rename+fsync replace. The
    store never validates semantics beyond the document's own identity
    binding — adoption decisions belong to :class:`ApplicationAdoption`.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        """Resolved data root (C02 rule 1: explicitly configured entry)."""
        return self._root

    def servers_storing(self, application_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """Server ids whose canonical tree holds a manifest for this id.

        Input for the cross-server duplicate guard: existence only, through
        the confined path derivation (symlinked components are rejected by
        ``_confined``) — the candidate manifests are never read or parsed
        here, identity is the directory layout alone.
        """
        aid = _validated_application_id(application_id)
        servers = self._root / "servers"
        if not servers.is_dir():
            return ()
        found: list[uuid.UUID] = []
        for entry in sorted(servers.iterdir(), key=lambda p: p.name):
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                other = uuid.UUID(entry.name)
            except ValueError:
                continue
            if self._confined(canonical_manifest_relative_path(other, aid)).is_file():
                found.append(other)
        return tuple(found)

    # -- public operations -------------------------------------------------

    def read(self, server_id: uuid.UUID, application_id: uuid.UUID) -> StoredManifest:
        """Return the stored manifest, hash-verified against its metadata."""
        server_id = _validated_server_id(server_id)
        application_id = _validated_application_id(application_id)
        target = self._confined(canonical_manifest_relative_path(server_id, application_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id, application_id))
        if not target.exists():
            raise AdoptionError(
                AdoptionErrorCode.NOT_FOUND,
                "no canonical manifest stored for this application id",
            )
        meta = self._load_meta(server_id, application_id, meta_path)
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise AdoptionError(
                AdoptionErrorCode.NOT_FOUND,
                "no canonical manifest stored for this application id",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise AdoptionError(
                AdoptionErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery must "
                "reconcile from the file hash",
            )
        return StoredManifest(
            server_id=server_id,
            application_id=application_id,
            text=data.decode("utf-8"),
            revision=meta["revision"],
            schema_revision=meta["schema_revision"],
            sha256=digest,
        )

    def write(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        text: str,
        *,
        expected_revision: int,
    ) -> StoredManifest:
        """Validate then atomically replace the canonical manifest.

        ``expected_revision`` is the revision the caller last observed (``0``
        to create); on success the revision is ``expected + 1``. A
        structurally invalid manifest raises before any filesystem effect.
        """
        server_id = _validated_server_id(server_id)
        application_id = _validated_application_id(application_id)
        _validated_expected_revision(expected_revision)
        data = _checked_document(text)
        manifest = parse_manifest_text(text)
        if manifest.server_id != server_id:
            raise AdoptionError(
                AdoptionErrorCode.SERVER_ID_MISMATCH,
                "embedded server_id does not match the storage identity",
            )
        if manifest.application_id != application_id:
            raise AdoptionError(
                AdoptionErrorCode.APPLICATION_ID_MISMATCH,
                "embedded application_id does not match the storage identity",
            )
        target = self._confined(canonical_manifest_relative_path(server_id, application_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id, application_id))
        current = self._current_revision(
            server_id, application_id, target, meta_path, tolerate_orphan=False
        )
        if current != expected_revision:
            raise AdoptionError(
                AdoptionErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_application_dir(target.parent)
        self._remove_stale_temps(target.parent)
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target, data)
        # C02 W2: canonical first, metadata second — the file is
        # authoritative; an interrupted pair is reported on read, never
        # silently reconciled here.
        self._atomic_write(
            meta_path, self._meta_bytes(server_id, application_id, revision, digest)
        )
        return StoredManifest(
            server_id=server_id,
            application_id=application_id,
            text=text,
            revision=revision,
            schema_revision=MANIFEST_SCHEMA_VERSION,
            sha256=digest,
        )

    def delete(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        expected_revision: int,
    ) -> None:
        """Revision-checked removal: canonical file first, metadata second.

        Completing a half-deleted state (metadata without canonical file) is
        done by repeating the delete with the metadata's revision.
        """
        server_id = _validated_server_id(server_id)
        application_id = _validated_application_id(application_id)
        _validated_expected_revision(expected_revision)
        target = self._confined(canonical_manifest_relative_path(server_id, application_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id, application_id))
        current = self._current_revision(
            server_id, application_id, target, meta_path, tolerate_orphan=True
        )
        if current == ABSENT_REVISION:
            raise AdoptionError(
                AdoptionErrorCode.NOT_FOUND,
                "nothing stored for this application id",
            )
        if current != expected_revision:
            raise AdoptionError(
                AdoptionErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._remove_stale_temps(target.parent)
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        _fsync_directory(target.parent)

    # -- confinement and state helpers ------------------------------------

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural (UUIDs + fixed names)."""
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise AdoptionError(
                    AdoptionErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise AdoptionError(
                AdoptionErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    def _load_meta(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        meta_path: Path,
    ) -> dict[str, object]:
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            raise AdoptionError(
                AdoptionErrorCode.METADATA_MISSING,
                "canonical manifest exists without revision metadata; "
                "recovery must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise AdoptionError(
                AdoptionErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        if (
            not isinstance(meta, dict)
            or meta.get("format") != META_FORMAT
            or meta.get("server_id") != str(server_id)
            or meta.get("application_id") != str(application_id)
            or not _is_positive_int(meta.get("revision"))
            or not _is_positive_int(meta.get("schema_revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
        ):
            raise AdoptionError(
                AdoptionErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        return meta

    def _current_revision(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        target: Path,
        meta_path: Path,
        *,
        tolerate_orphan: bool,
    ) -> int:
        """Pairing-aware current revision; ``ABSENT_REVISION`` when both absent."""
        has_file = target.exists()
        has_meta = meta_path.exists()
        if not has_file and not has_meta:
            return ABSENT_REVISION
        if has_file and not has_meta:
            raise AdoptionError(
                AdoptionErrorCode.METADATA_MISSING,
                "canonical manifest exists without revision metadata; "
                "recovery must reconcile before further writes",
            )
        meta = self._load_meta(server_id, application_id, meta_path)
        if not has_file and not tolerate_orphan:
            raise AdoptionError(
                AdoptionErrorCode.ORPHAN_MANIFEST,
                "revision metadata exists without a canonical manifest; "
                "complete the delete before writing",
            )
        return int(meta["revision"])

    @staticmethod
    def _ensure_application_dir(directory: Path) -> None:
        # directory = <root>/servers/<sid>/applications/<aid>: every level is
        # part of the canonical scheme and stays owner-only (C02 rule 1).
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for level in (
            directory.parent.parent.parent,
            directory.parent.parent,
            directory.parent,
            directory,
        ):
            os.chmod(level, 0o700)

    @staticmethod
    def _meta_bytes(
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        revision: int,
        digest: str,
    ) -> bytes:
        payload = {
            "format": META_FORMAT,
            "server_id": str(server_id),
            "application_id": str(application_id),
            "revision": revision,
            "schema_revision": MANIFEST_SCHEMA_VERSION,
            "sha256": digest,
        }
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` via temp file + fsync + atomic replace."""
        directory = path.parent
        fd, temp_name = tempfile.mkstemp(
            dir=directory, prefix=f"{path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(directory)

    @staticmethod
    def _remove_stale_temps(directory: Path) -> None:
        """Best-effort cleanup of this store's crash-orphaned temp files."""
        if not directory.is_dir():
            return
        prefixes = (f"{MANIFEST_FILENAME}.", f"{MANIFEST_META_FILENAME}.")
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# ---------------------------------------------------------------------------
# SQL statements (schema owned by migrations/versions/0006_applications.py)
# ---------------------------------------------------------------------------

_INSERT_INTENT = text(
    """
    INSERT INTO application_projection_journal (
        intent_id, server_id, application_id, op, phase, expected_revision,
        base_sha256, target_revision, target_sha256, schema_revision, payload
    ) VALUES (
        :intent_id, :server_id, :application_id, :op, :phase,
        :expected_revision, :base_sha256, :target_revision, :target_sha256,
        :schema_revision, :payload
    )
    """
)

_SELECT_INTENT = text(
    """
    SELECT intent_id, server_id, application_id, op, phase, expected_revision,
           base_sha256, target_revision, target_sha256, schema_revision,
           payload, created_at, updated_at
      FROM application_projection_journal
     WHERE intent_id = :intent_id
    """
)

_SELECT_LIVE_INTENTS_ALL = text(
    """
    SELECT intent_id, server_id, application_id, op, phase, expected_revision,
           base_sha256, target_revision, target_sha256, schema_revision,
           payload, created_at, updated_at
      FROM application_projection_journal
     WHERE phase <> :reconciled
     ORDER BY created_at, intent_id
    """
)

_SELECT_LIVE_INTENTS_SERVER = text(
    """
    SELECT intent_id, server_id, application_id, op, phase, expected_revision,
           base_sha256, target_revision, target_sha256, schema_revision,
           payload, created_at, updated_at
      FROM application_projection_journal
     WHERE phase <> :reconciled AND server_id = :server_id
     ORDER BY created_at, intent_id
    """
)

_SELECT_OWN_ROW = text(
    """
    SELECT p.application_id, p.server_id, p.name, p.origin, p.compose_project,
           p.working_dir, p.unit_name, p.external_working_dir,
           p.external_config_files, p.external_environment_files, p.revision,
           p.schema_revision, p.sha256, p.adopted_at, p.reconciled_at,
           (j.intent_id IS NOT NULL) AS stale
      FROM application_projection p
      LEFT JOIN application_projection_journal j
             ON j.server_id = p.server_id
            AND j.application_id = p.application_id
            AND j.phase <> :reconciled
     WHERE p.server_id = :server_id AND p.application_id = :application_id
    """
)

_SELECT_SERVER_ROWS = text(
    """
    SELECT p.application_id, p.server_id, p.name, p.origin, p.compose_project,
           p.working_dir, p.unit_name, p.external_working_dir,
           p.external_config_files, p.external_environment_files, p.revision,
           p.schema_revision, p.sha256, p.adopted_at, p.reconciled_at,
           (j.intent_id IS NOT NULL) AS stale
      FROM application_projection p
      LEFT JOIN application_projection_journal j
             ON j.server_id = p.server_id
            AND j.application_id = p.application_id
            AND j.phase <> :reconciled
     WHERE p.server_id = :server_id
     ORDER BY p.application_id
    """
)

#: Selector-overlap refusal (module docstring): rtrim('/','-style trailing
#: slashes) mirrors the discovery-side normalization exactly.
_SELECT_CONFLICTING_ROWS = text(
    """
    SELECT application_id, compose_project, working_dir, unit_name
      FROM application_projection
     WHERE server_id = :server_id
       AND application_id <> :application_id
       AND (
           (compose_project IS NOT NULL AND compose_project = :compose_project)
        OR (unit_name IS NOT NULL AND unit_name = :unit_name)
        OR (working_dir IS NOT NULL AND CAST(:working_dir AS text) IS NOT NULL
            AND rtrim(working_dir, '/') = rtrim(CAST(:working_dir AS text), '/'))
       )
     LIMIT 1
    """
)

#: Cross-server duplicate guard: a live projection row for this
#: ``application_id`` already belongs to ANOTHER server. Adoption must
#: refuse — the upsert must never silently relocate the row (which would
#: reset the revision chain and orphan the original server's canonical
#: pair and managed references).
_SELECT_FOREIGN_SERVER = text(
    """
    SELECT server_id
      FROM application_projection
     WHERE application_id = :application_id
       AND server_id <> :server_id
     LIMIT 1
    """
)

#: W1 handoff: the payload dies here — every later phase is CHECK-forced to
#: hold no document text.
_ADVANCE_FILE_COMMITTED = text(
    """
    UPDATE application_projection_journal
       SET phase = :file_committed, payload = NULL, updated_at = now()
     WHERE intent_id = :intent_id AND phase = :prepared
    """
)

_ADVANCE_RECONCILED = text(
    """
    UPDATE application_projection_journal
       SET phase = :reconciled, updated_at = now()
     WHERE intent_id = :intent_id AND phase = :file_committed
    """
)

#: W3 core: projection-row write and journal advance share ONE transaction.
#: ``adopted_at`` keeps its first-insert value; ``reconciled_at`` advances.
#: Storage backstop for the cross-server duplicate guard: the conflicting
#: row is never re-owned (``server_id`` stays out of the SET list and the
#: update only applies when the live row ALREADY belongs to this server);
#: anything else skips the update, ``RETURNING`` yields nothing and
#: :meth:`_reconcile_upsert_from_file` surfaces the refusal.
_UPSERT_PROJECTION = text(
    """
    INSERT INTO application_projection (
        application_id, server_id, name, origin, compose_project, working_dir,
        unit_name, external_working_dir, external_config_files,
        external_environment_files, revision, schema_revision, sha256
    ) VALUES (
        :application_id, :server_id, :name, :origin, :compose_project,
        :working_dir, :unit_name, :external_working_dir,
        :external_config_files, :external_environment_files, :revision,
        :schema_revision, :sha256
    )
    ON CONFLICT (application_id) DO UPDATE
       SET name = EXCLUDED.name,
           origin = EXCLUDED.origin,
           compose_project = EXCLUDED.compose_project,
           working_dir = EXCLUDED.working_dir,
           unit_name = EXCLUDED.unit_name,
           external_working_dir = EXCLUDED.external_working_dir,
           external_config_files = EXCLUDED.external_config_files,
           external_environment_files = EXCLUDED.external_environment_files,
           revision = EXCLUDED.revision,
           schema_revision = EXCLUDED.schema_revision,
           sha256 = EXCLUDED.sha256,
           reconciled_at = now()
     WHERE application_projection.server_id = EXCLUDED.server_id
    RETURNING server_id
    """
)

#: Server-scoped purge (W4): a tombstone may only remove its OWN server's
#: row; the application identity alone is not ownership.
_DELETE_PROJECTION = text(
    """
    DELETE FROM application_projection
     WHERE application_id = :application_id AND server_id = :server_id
    """
)


def _intent_of(row: Sequence[object] | object) -> JournalIntent:
    """Materialize one journal row (mapping) into the frozen dataclass."""
    return JournalIntent(
        intent_id=row["intent_id"],  # type: ignore[index]
        server_id=row["server_id"],  # type: ignore[index]
        application_id=row["application_id"],  # type: ignore[index]
        op=row["op"],  # type: ignore[index]
        phase=row["phase"],  # type: ignore[index]
        expected_revision=row["expected_revision"],  # type: ignore[index]
        base_sha256=row["base_sha256"],  # type: ignore[index]
        target_revision=row["target_revision"],  # type: ignore[index]
        target_sha256=row["target_sha256"],  # type: ignore[index]
        schema_revision=row["schema_revision"],  # type: ignore[index]
        payload=row["payload"],  # type: ignore[index]
        created_at=row["created_at"],  # type: ignore[index]
        updated_at=row["updated_at"],  # type: ignore[index]
    )


def _normalized_dir(path: str) -> str:
    """Trailing-slash normalization; must stay identical to discovery's
    ``classify_managed`` matching (``mai.applications.discover``)."""
    return path.rstrip("/")


def _selectors_conflict(
    row_compose: str | None,
    row_dir: str | None,
    row_unit: str | None,
    request_compose: str | None,
    request_dir: str | None,
    request_unit: str | None,
) -> bool:
    """Pure selector-overlap rule (same criterion as discovery matching)."""
    if row_compose is not None and request_compose is not None:
        if row_compose == request_compose:
            return True
    if row_unit is not None and request_unit is not None:
        if row_unit == request_unit:
            return True
    if row_dir is not None and request_dir is not None:
        if _normalized_dir(row_dir) == _normalized_dir(request_dir):
            return True
    return False


def _record_of(row: Sequence[object] | object) -> AdoptedApplication:
    """Materialize one projection row (mapping) into the frozen dataclass."""
    return AdoptedApplication(
        application_id=row["application_id"],  # type: ignore[index]
        server_id=row["server_id"],  # type: ignore[index]
        name=row["name"],  # type: ignore[index]
        origin=ApplicationOrigin(row["origin"]),  # type: ignore[index]
        compose_project=row["compose_project"],  # type: ignore[index]
        working_dir=row["working_dir"],  # type: ignore[index]
        unit_name=row["unit_name"],  # type: ignore[index]
        external_working_dir=row["external_working_dir"],  # type: ignore[index]
        external_config_files=tuple(row["external_config_files"] or ()),  # type: ignore[index]
        external_environment_files=tuple(row["external_environment_files"] or ()),  # type: ignore[index]
        revision=row["revision"],  # type: ignore[index]
        schema_revision=row["schema_revision"],  # type: ignore[index]
        sha256=row["sha256"],  # type: ignore[index]
        adopted_at=row["adopted_at"],  # type: ignore[index]
        reconciled_at=row["reconciled_at"],  # type: ignore[index]
        stale=bool(row["stale"]),  # type: ignore[index]
    )


# ---------------------------------------------------------------------------
# Adoption worker: journal + projection over the canonical manifest store
# ---------------------------------------------------------------------------


class ApplicationAdoption:
    """Explicit adoption/recovery worker over :class:`ApplicationManifestStore`.

    Single-writer per ``server_id`` is a caller invariant (C05 rule 6); the
    journal's partial unique index (one live intent per application) and the
    projection overlap check are storage backstops. All file effects go
    through the manifest store; all SQL effects go through the session
    factory built by :func:`mai.database.create_app_engine`.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    # -- explicit adoption (C09 rule 1) ------------------------------------

    async def adopt(
        self,
        store: ApplicationManifestStore,
        request: AdoptionRequest,
        *,
        intent_id: uuid.UUID | None = None,
    ) -> AdoptionResult:
        """Adopt or update one application: manifest file + SQL projection.

        Duplicate/stale/ambiguous decisions happen BEFORE any effect:
        a live record for this ``application_id`` with
        ``expected_revision = 0`` refuses with ``already_adopted``; a
        revision-mismatched caller refuses with ``stale_revision``; selectors
        overlapping another live adoption on the same server refuse with
        ``reference_conflict``; a live record OR canonical manifest for this
        ``application_id`` under a DIFFERENT server refuses with
        ``cross_server_conflict`` (adoption never relocates an identity
        across servers). The deployment files named in
        ``external_paths`` are never touched (C09 rule 6).
        """
        if not isinstance(request, AdoptionRequest):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_FIELD,
                "request must be an AdoptionRequest instance",
            )
        sid = _validated_server_id(request.server_id)
        aid = _validated_application_id(request.application_id)
        iid = _validated_intent_id(intent_id) if intent_id is not None else uuid.uuid4()
        document = render_manifest(request)
        # Idempotent submit: a RECONCILED intent with this id and the same
        # document is a completed retry — converge (verify/repair) instead of
        # refusing it as a duplicate. Any other prior id/phase falls through
        # to the duplicate/stale/conflict decisions below.
        prior = await self._fetch_intent_by_id(iid)
        if (
            prior is not None
            and prior.phase == PHASE_RECONCILED
            and prior.op == OP_UPSERT
            and prior.server_id == sid
            and prior.application_id == aid
            and prior.target_sha256
            == hashlib.sha256(document.encode("utf-8")).hexdigest()
        ):
            return await self._already_or_repair_upsert(store, prior)
        row = await self._own_row(sid, aid)
        base_sha256: str | None = None
        if row is not None:
            if request.expected_revision == ABSENT_REVISION:
                raise AdoptionError(
                    AdoptionErrorCode.ALREADY_ADOPTED,
                    "an adoption record already exists for this application "
                    "id; updates require the currently observed revision",
                )
            if request.expected_revision != row.revision:
                raise AdoptionError(
                    AdoptionErrorCode.STALE_REVISION,
                    f"expected revision {request.expected_revision}, "
                    f"live revision is {row.revision}",
                )
            base_sha256 = row.sha256
        else:
            _validated_expected_revision(request.expected_revision)
            if request.expected_revision != ABSENT_REVISION:
                raise AdoptionError(
                    AdoptionErrorCode.STALE_REVISION,
                    f"expected revision {request.expected_revision} but no "
                    "adoption record exists",
                )
        foreign_server = await self._foreign_server(store, sid, aid)
        if foreign_server is not None:
            raise AdoptionError(
                AdoptionErrorCode.CROSS_SERVER_CONFLICT,
                "a live adoption record or canonical manifest for this "
                "application id already exists under a different server; "
                "cross-server relocation is refused, explicit admin "
                "resolution required",
            )
        conflicting = await self._conflicting_row(sid, aid, request)
        if conflicting is not None:
            raise AdoptionError(
                AdoptionErrorCode.REFERENCE_CONFLICT,
                "another live adoption on this server already claims the "
                "same compose project, working directory or unit name; "
                "ambiguous references are refused",
            )
        intent, resumed = await self._prepare(
            server_id=sid,
            application_id=aid,
            op=OP_UPSERT,
            document=document,
            expected_revision=request.expected_revision,
            base_sha256=base_sha256,
            intent_id=iid,
        )
        if intent.phase == PHASE_RECONCILED:
            return await self._already_or_repair_upsert(store, intent)
        if intent.phase == PHASE_PREPARED:
            self._commit_file_for_upsert(store, intent)
            intent = await self._mark_file_committed(intent.intent_id)
        observed = await self._reconcile_upsert_from_file(intent, store)
        return AdoptionResult(
            intent.intent_id,
            sid,
            aid,
            OP_UPSERT,
            "resumed" if resumed else "applied",
            observed.revision,
            observed.sha256,
        )

    async def revoke(
        self,
        store: ApplicationManifestStore,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        expected_revision: int,
        intent_id: uuid.UUID | None = None,
    ) -> AdoptionResult:
        """Revoke an adoption: tombstone first, then file, then projection.

        Non-destructive by construction (C09 rule 6): only the canonical
        manifest pair under the data root is removed — the external
        deployment paths recorded in the manifest are never touched. After a
        reconciled revocation the record is gone from the projection, so
        :meth:`managed_references` no longer feeds discovery's ``managed``
        tagging and maintenance has no adopted record to act on.
        """
        sid = _validated_server_id(server_id)
        aid = _validated_application_id(application_id)
        expected = _validated_expected_revision(expected_revision)
        retry_id = (
            _validated_intent_id(intent_id) if intent_id is not None else uuid.uuid4()
        )
        prior = await self._fetch_intent_by_id(retry_id)
        if (
            prior is not None
            and prior.phase == PHASE_RECONCILED
            and prior.op == OP_DELETE
            and prior.server_id == sid
            and prior.application_id == aid
        ):
            return await self._already_or_repair_delete(store, prior)
        row = await self._own_row(sid, aid)
        if row is None:
            raise AdoptionError(
                AdoptionErrorCode.NOT_ADOPTED,
                "no adoption record exists for this application id",
            )
        if expected != row.revision:
            raise AdoptionError(
                AdoptionErrorCode.STALE_REVISION,
                f"expected revision {expected}, live revision is {row.revision}",
            )
        intent, resumed = await self._prepare(
            server_id=sid,
            application_id=aid,
            op=OP_DELETE,
            document=None,
            expected_revision=expected,
            base_sha256=row.sha256,
            intent_id=retry_id,
        )
        if intent.phase == PHASE_RECONCILED:
            return await self._already_or_repair_delete(store, intent)
        if intent.phase == PHASE_PREPARED:
            self._delete_file(store, intent)
            intent = await self._mark_file_committed(intent.intent_id)
        await self._reconcile_delete(intent, store)
        return AdoptionResult(
            intent.intent_id, sid, aid, OP_DELETE,
            "resumed" if resumed else "applied", None, None,
        )

    # -- intent preparation (durable "prepared intent", C02 rule 5) --------

    async def _prepare(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        op: str,
        document: str | None,
        expected_revision: int,
        base_sha256: str | None,
        intent_id: uuid.UUID,
    ) -> tuple[JournalIntent, bool]:
        """Insert or resume one intent; returns (intent, resumed_live).

        Idempotent by ``intent_id``: an existing row with the same id,
        server, application, op and expected revision is returned as-is
        (retry support); any other divergence is rejected.
        """
        expected = _validated_expected_revision(expected_revision)
        if base_sha256 is not None and not _is_canonical_sha256(base_sha256):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_FIELD,
                "base hash is not a canonical sha256 value",
            )
        if op == OP_UPSERT:
            if document is None:
                raise AdoptionError(
                    AdoptionErrorCode.PAYLOAD_MISMATCH,
                    "upsert intent requires the manifest document",
                )
            _checked_document(document)
            params: dict[str, object] = {
                "intent_id": intent_id,
                "server_id": server_id,
                "application_id": application_id,
                "op": op,
                "phase": PHASE_PREPARED,
                "expected_revision": expected,
                "base_sha256": base_sha256,
                "target_revision": expected + 1,
                "target_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
                "schema_revision": MANIFEST_SCHEMA_VERSION,
                "payload": document,
            }
        else:
            params = {
                "intent_id": intent_id,
                "server_id": server_id,
                "application_id": application_id,
                "op": op,
                "phase": PHASE_PREPARED,
                "expected_revision": expected,
                "base_sha256": base_sha256,
                "target_revision": None,
                "target_sha256": None,
                "schema_revision": None,
                "payload": None,
            }
        async with self._sessions() as session, session.begin():
            existing = await self._fetch_intent(session, intent_id)
            if existing is not None:
                self._ensure_resume(
                    existing, server_id, application_id, op, expected,
                    params["target_sha256"],
                )
                return existing, existing.phase in _LIVE_PHASES
            try:
                await session.execute(_INSERT_INTENT, params)
            except IntegrityError as exc:
                raise AdoptionError(
                    AdoptionErrorCode.INTENT_PENDING,
                    "another live intent already exists for this application",
                ) from exc
            created = await self._fetch_intent(session, intent_id)
        assert created is not None  # inserted above in the same transaction
        return created, False

    @staticmethod
    def _ensure_resume(
        existing: JournalIntent,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        op: str,
        expected_revision: int,
        target_sha256: object,
    ) -> None:
        if (
            existing.server_id != server_id
            or existing.application_id != application_id
            or existing.op != op
        ):
            raise AdoptionError(
                AdoptionErrorCode.INTENT_MISMATCH,
                "intent id already used for a different server, application "
                "or operation",
            )
        if existing.expected_revision != expected_revision:
            raise AdoptionError(
                AdoptionErrorCode.PAYLOAD_MISMATCH,
                "retry does not match the journaled expected revision",
            )
        if op == OP_UPSERT and existing.target_sha256 != target_sha256:
            raise AdoptionError(
                AdoptionErrorCode.PAYLOAD_MISMATCH,
                "retry document hash differs from the journaled payload",
            )

    # -- recovery sweep (projection worker routine) -------------------------

    async def recover(
        self,
        store: ApplicationManifestStore,
        *,
        server_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[RecoveryOutcome]:
        """Reconcile every live intent against the authoritative files.

        Per-intent failures are recorded and the sweep continues; nothing is
        ever invented: reconcile writes only what the file read returned.
        """
        sid = _validated_server_id(server_id) if server_id is not None else None
        intents = await self.list_live_intents(server_id=sid, limit=limit)
        outcomes: list[RecoveryOutcome] = []
        for intent in intents:
            outcomes.append(await self._recover_one(store, intent))
        return outcomes

    async def _recover_one(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        try:
            if intent.op == OP_UPSERT:
                return await self._recover_upsert(store, intent)
            return await self._recover_delete(store, intent)
        except AdoptionError as exc:
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "conflict", exc.code.value, exc.detail, None,
            )
        except Exception as exc:  # noqa: BLE001 - sweep continues; value-free
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "error", type(exc).__name__,
                "unexpected failure; detail withheld to keep transcripts "
                "value-free",
                None,
            )

    async def _recover_upsert(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        if intent.phase == PHASE_FILE_COMMITTED:
            # W2: the file commit landed before the crash; the projection is
            # simply stale. Reconcile observed truth, never the claim.
            observed = await self._reconcile_upsert_from_file(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "advanced_reconciled", None,
                "file committed before crash; reconciled from file",
                observed.revision,
            )
        payload = intent.payload
        if payload is None:
            raise AdoptionError(
                AdoptionErrorCode.INTENT_PHASE,
                "prepared upsert intent has no manifest payload",
            )
        if intent.target_revision is None or intent.target_sha256 is None:
            raise AdoptionError(
                AdoptionErrorCode.INTENT_PHASE,
                "upsert intent is missing its target triple",
            )
        state = _read_file_or_absent(
            store, intent.server_id, intent.application_id
        )
        if state.error is not None:
            # Interrupted canonical/metadata pair or confinement failure:
            # surfaced, never repaired by invention (C02 rule 5).
            raise state.error
        stored = state.stored
        if stored is not None and _matches(
            stored, intent.target_revision, intent.target_sha256
        ):
            # W1, crash-after-replace variant: the file commit landed before
            # the journal advance — recognize it, never rewrite the file.
            await self._mark_file_committed(intent.intent_id)
            observed = await self._reconcile_upsert_from_file(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "advanced_reconciled", None,
                "file already at intent target", observed.revision,
            )
        if stored is not None and (
            stored.revision != intent.expected_revision
            or (
                intent.base_sha256 is not None
                and stored.sha256 != intent.base_sha256
            )
        ):
            raise AdoptionError(
                AdoptionErrorCode.FILE_DIVERGED,
                "canonical manifest is neither at the intent target nor at "
                "the observed baseline",
            )
        # W1, uncommitted variant: file unchanged/absent -> re-execute.
        committed = store.write(
            intent.server_id,
            intent.application_id,
            payload,
            expected_revision=intent.expected_revision,
        )
        if not _matches(committed, intent.target_revision, intent.target_sha256):
            raise AdoptionError(
                AdoptionErrorCode.FILE_DIVERGED,
                "re-executed file commit does not match the intent target",
            )
        await self._mark_file_committed(intent.intent_id)
        observed = await self._reconcile_upsert_from_file(intent, store)
        return RecoveryOutcome(
            intent.intent_id, intent.server_id, intent.application_id,
            intent.op, "reexecuted_reconciled", None,
            "file commit re-executed from journal payload", observed.revision,
        )

    async def _recover_delete(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        if intent.phase == PHASE_FILE_COMMITTED:
            # W2/W4: the file delete landed before the crash; purge the
            # projection row now (tombstone still live until reconciled).
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "advanced_reconciled", None,
                "file deleted before crash; projection purged", None,
            )
        state = _read_file_or_absent(store, intent.server_id, intent.application_id)
        if state.error is not None:
            raise state.error
        stored = state.stored
        if stored is not None and _matches(
            stored, intent.expected_revision, intent.base_sha256
        ):
            # Tombstone live, delete not yet executed -> re-execute it.
            self._delete_file(store, intent)
            await self._mark_file_committed(intent.intent_id)
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "reexecuted_reconciled", None,
                "file delete re-executed", None,
            )
        if stored is None:
            # Crash after the file delete, before the journal advance.
            await self._mark_file_committed(intent.intent_id)
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.application_id,
                intent.op, "advanced_reconciled", None,
                "file already absent", None,
            )
        # W4: a live manifest under a live tombstone is a conflict for the
        # admin — never a silent overwrite.
        raise AdoptionError(
            AdoptionErrorCode.TOMBSTONE_CONFLICT,
            "canonical manifest exists while a delete tombstone is live; "
            "explicit admin resolution required",
        )

    # -- full pipeline file/SQL primitives ---------------------------------

    def _commit_file_for_upsert(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> None:
        """Execute the file half of a prepared upsert (W1-aware)."""
        payload = intent.payload
        if payload is None:
            raise AdoptionError(
                AdoptionErrorCode.INTENT_PHASE,
                "prepared upsert intent has no manifest payload",
            )
        assert intent.target_revision is not None
        assert intent.target_sha256 is not None
        state = _read_file_or_absent(store, intent.server_id, intent.application_id)
        if state.error is not None:
            # Interrupted pair from an earlier crash: not silently repairable.
            raise state.error
        if state.stored is not None and _matches(
            state.stored, intent.target_revision, intent.target_sha256
        ):
            return  # crash-after-replace variant: file already committed
        try:
            committed = store.write(
                intent.server_id,
                intent.application_id,
                payload,
                expected_revision=intent.expected_revision,
            )
        except AdoptionError as exc:
            if exc.code is not AdoptionErrorCode.STALE_REVISION:
                raise
            # The file moved between the read above and the write. Under the
            # single-writer invariant this can only be our own committed
            # intent; anything else is surfaced, never overwritten.
            state = _read_file(store, intent.server_id, intent.application_id)
            if state.stored is None or not _matches(
                state.stored, intent.target_revision, intent.target_sha256
            ):
                raise
            return
        if not _matches(committed, intent.target_revision, intent.target_sha256):
            raise AdoptionError(
                AdoptionErrorCode.FILE_DIVERGED,
                "file commit result does not match the intent target",
            )

    def _delete_file(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> None:
        """Execute the file half of a tombstoned delete (W4-aware)."""
        try:
            store.delete(
                intent.server_id,
                intent.application_id,
                expected_revision=intent.expected_revision,
            )
        except AdoptionError as exc:
            if exc.code is AdoptionErrorCode.NOT_FOUND:
                return  # already gone: crash-after-delete variant
            if exc.code is not AdoptionErrorCode.STALE_REVISION:
                raise
            state = _read_file(store, intent.server_id, intent.application_id)
            if state.stored is None:
                return
            raise

    async def _mark_file_committed(self, intent_id: uuid.UUID) -> JournalIntent:
        """Advance prepared -> file_committed, dropping the document payload."""
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                _ADVANCE_FILE_COMMITTED,
                {
                    "intent_id": intent_id,
                    "prepared": PHASE_PREPARED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if result.rowcount != 1:
                existing = await self._fetch_intent(session, intent_id)
                if existing is None:
                    raise AdoptionError(
                        AdoptionErrorCode.INTENT_NOT_FOUND,
                        "no journal row for this intent id",
                    )
                raise AdoptionError(
                    AdoptionErrorCode.INTENT_PHASE,
                    f"intent is in phase {existing.phase}, not {PHASE_PREPARED}",
                )
            updated = await self._fetch_intent(session, intent_id)
        assert updated is not None
        return updated

    async def _reconcile_upsert_from_file(
        self, intent: JournalIntent, store: ApplicationManifestStore
    ) -> StoredManifest:
        """W2/W3: project the file's ACTUAL content, advancing the journal.

        The manifest is read and parsed at reconcile time; its observed
        fields and triple — never the intent's claim — are written, in the
        same transaction that advances the journal row to ``reconciled``
        ("no invented successful sync").
        """
        if intent.target_revision is None or intent.target_sha256 is None:
            raise AdoptionError(
                AdoptionErrorCode.INTENT_PHASE,
                "upsert intent is missing its target triple",
            )
        observed = _read_file(store, intent.server_id, intent.application_id)
        if observed.error is not None:
            raise observed.error
        stored = observed.stored
        assert stored is not None  # read succeeded
        if not _matches(stored, intent.target_revision, intent.target_sha256):
            raise AdoptionError(
                AdoptionErrorCode.FILE_DIVERGED,
                "canonical manifest does not match the intent target at "
                "reconcile time",
            )
        manifest = parse_manifest_text(stored.text)
        async with self._sessions() as session, session.begin():
            projected_server = (
                await session.execute(
                    _UPSERT_PROJECTION,
                    {
                        "application_id": manifest.application_id,
                        "server_id": manifest.server_id,
                        "name": manifest.name,
                        "origin": manifest.origin.value,
                        "compose_project": manifest.compose_project,
                        "working_dir": manifest.working_dir,
                        "unit_name": manifest.unit_name,
                        "external_working_dir": manifest.external_working_dir,
                        "external_config_files": list(manifest.external_config_files),
                        "external_environment_files": list(
                            manifest.external_environment_files
                        ),
                        "revision": stored.revision,
                        "schema_revision": stored.schema_revision,
                        "sha256": stored.sha256,
                    },
                )
            ).scalar()
            if projected_server is None or (
                uuid.UUID(str(projected_server)) != intent.server_id
            ):
                # Backstop refused the write (live row owned by another
                # server): abort the transaction — the journal stays live and
                # the relocation attempt is surfaced, never silently applied.
                raise AdoptionError(
                    AdoptionErrorCode.CROSS_SERVER_CONFLICT,
                    "projection row for this application id belongs to "
                    "another server; cross-server relocation is refused",
                )
            advanced = await session.execute(
                _ADVANCE_RECONCILED,
                {
                    "intent_id": intent.intent_id,
                    "reconciled": PHASE_RECONCILED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if advanced.rowcount != 1:
                existing = await self._fetch_intent(session, intent.intent_id)
                if existing is None or existing.phase != PHASE_RECONCILED:
                    raise AdoptionError(
                        AdoptionErrorCode.INTENT_PHASE,
                        f"intent is not in phase {PHASE_FILE_COMMITTED}",
                    )
                # Repair path: the row was already reconciled; the idempotent
                # projection write is the effect that matters here.
        return stored

    async def _reconcile_delete(
        self, intent: JournalIntent, store: ApplicationManifestStore
    ) -> None:
        """Purge the projection row only after the file absence confirms."""
        state = _read_file_or_absent(store, intent.server_id, intent.application_id)
        if state.error is not None:
            raise state.error
        if state.stored is not None:
            raise AdoptionError(
                AdoptionErrorCode.TOMBSTONE_CONFLICT,
                "canonical manifest exists while a delete tombstone is live; "
                "explicit admin resolution required",
            )
        async with self._sessions() as session, session.begin():
            await session.execute(
                _DELETE_PROJECTION,
                {
                    "application_id": intent.application_id,
                    "server_id": intent.server_id,
                },
            )
            advanced = await session.execute(
                _ADVANCE_RECONCILED,
                {
                    "intent_id": intent.intent_id,
                    "reconciled": PHASE_RECONCILED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if advanced.rowcount != 1:
                existing = await self._fetch_intent(session, intent.intent_id)
                if existing is None or existing.phase != PHASE_RECONCILED:
                    raise AdoptionError(
                        AdoptionErrorCode.INTENT_PHASE,
                        f"intent is not in phase {PHASE_FILE_COMMITTED}",
                    )

    async def _already_or_repair_upsert(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> AdoptionResult:
        """Reconciled-intent retry: verify convergence, repair if stale."""
        assert intent.target_revision is not None
        assert intent.target_sha256 is not None
        state = await self.read(intent.server_id, intent.application_id)
        if (
            state is not None
            and state.revision == intent.target_revision
            and state.sha256 == intent.target_sha256
            and not state.stale
        ):
            return AdoptionResult(
                intent.intent_id, intent.server_id, intent.application_id,
                OP_UPSERT, "already_reconciled", intent.target_revision,
                intent.target_sha256,
            )
        observed = _read_file_or_absent(store, intent.server_id, intent.application_id)
        if observed.error is not None:
            raise observed.error
        stored = observed.stored
        if stored is None or not _matches(
            stored, intent.target_revision, intent.target_sha256
        ):
            raise AdoptionError(
                AdoptionErrorCode.FILE_DIVERGED,
                "reconciled intent's target no longer matches the canonical "
                "manifest; explicit admin resolution required",
            )
        await self._reconcile_upsert_from_file(intent, store)
        return AdoptionResult(
            intent.intent_id, intent.server_id, intent.application_id,
            OP_UPSERT, "resumed", stored.revision, stored.sha256,
        )

    async def _already_or_repair_delete(
        self, store: ApplicationManifestStore, intent: JournalIntent
    ) -> AdoptionResult:
        """Reconciled delete-intent retry: verify absence on both sides."""
        record = await self.read(intent.server_id, intent.application_id)
        observed = _read_file_or_absent(store, intent.server_id, intent.application_id)
        if observed.error is not None:
            raise observed.error
        if observed.stored is None and record is None:
            return AdoptionResult(
                intent.intent_id, intent.server_id, intent.application_id,
                OP_DELETE, "already_reconciled", None, None,
            )
        if observed.stored is not None:
            raise AdoptionError(
                AdoptionErrorCode.TOMBSTONE_CONFLICT,
                "delete intent is reconciled but a canonical manifest exists "
                "again; explicit admin resolution required",
            )
        # Projection row survived while the file is gone: purge it now.
        await self._reconcile_delete(intent, store)
        return AdoptionResult(
            intent.intent_id, intent.server_id, intent.application_id,
            OP_DELETE, "resumed", None, None,
        )

    # -- reads and discovery bridge -----------------------------------------

    async def read(
        self, server_id: uuid.UUID, application_id: uuid.UUID
    ) -> AdoptedApplication | None:
        """One projected adoption record with the C02-W2 staleness flag."""
        sid = _validated_server_id(server_id)
        aid = _validated_application_id(application_id)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    _SELECT_OWN_ROW,
                    {"server_id": sid, "application_id": aid, "reconciled": PHASE_RECONCILED},
                )
            ).mappings().first()
        return None if row is None else _record_of(row)

    async def list_adopted(
        self, server_id: uuid.UUID
    ) -> tuple[AdoptedApplication, ...]:
        """All projected adoption records for one server (stable id order)."""
        sid = _validated_server_id(server_id)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    _SELECT_SERVER_ROWS,
                    {"server_id": sid, "reconciled": PHASE_RECONCILED},
                )
            ).mappings().all()
        return tuple(_record_of(row) for row in rows)

    async def managed_references(
        self, server_id: uuid.UUID
    ) -> tuple[ManagedReference, ...]:
        """Live adoption records as discovery ``ManagedReference`` selectors.

        The ONLY bridge direction between adoption and discovery: adoption
        feeds the ``managed`` tagging inputs; discovery output never reaches
        a mutation (C09 rule 1, acceptance 2).
        """
        records = await self.list_adopted(server_id)
        return tuple(
            ManagedReference(
                application_id=record.application_id,
                compose_project=record.compose_project,
                working_dir=record.working_dir,
                unit_name=record.unit_name,
            )
            for record in records
        )

    async def list_live_intents(
        self,
        *,
        server_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[JournalIntent]:
        """Live (non-reconciled) intents, stable order, bounded by ``limit``."""
        statement = (
            _SELECT_LIVE_INTENTS_SERVER if server_id is not None else _SELECT_LIVE_INTENTS_ALL
        )
        params: dict[str, object] = {"reconciled": PHASE_RECONCILED}
        if server_id is not None:
            params["server_id"] = server_id
        if limit is not None:
            statement = text(statement.text + " LIMIT :limit")
            params["limit"] = limit
        async with self._sessions() as session:
            rows = (await session.execute(statement, params)).mappings().all()
        return [_intent_of(row) for row in rows]

    # -- SQL helpers ---------------------------------------------------------

    async def _fetch_intent_by_id(self, intent_id: uuid.UUID) -> JournalIntent | None:
        async with self._sessions() as session:
            return await self._fetch_intent(session, intent_id)

    async def _fetch_intent(
        self, session: AsyncSession, intent_id: uuid.UUID
    ) -> JournalIntent | None:
        row = (
            await session.execute(_SELECT_INTENT, {"intent_id": intent_id})
        ).mappings().first()
        return None if row is None else _intent_of(row)

    async def _own_row(
        self, server_id: uuid.UUID, application_id: uuid.UUID
    ) -> AdoptedApplication | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    _SELECT_OWN_ROW,
                    {
                        "server_id": server_id,
                        "application_id": application_id,
                        "reconciled": PHASE_RECONCILED,
                    },
                )
            ).mappings().first()
        return None if row is None else _record_of(row)

    async def _conflicting_row(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        request: AdoptionRequest,
    ) -> uuid.UUID | None:
        """First live adoption whose selectors overlap the request's."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    _SELECT_CONFLICTING_ROWS,
                    {
                        "server_id": server_id,
                        "application_id": application_id,
                        "compose_project": request.compose_project,
                        "working_dir": request.working_dir,
                        "unit_name": request.unit_name,
                    },
                )
            ).mappings().first()
        if row is None:
            return None
        # Storage check plus the pure rule must agree; an unexpected overlap
        # shape is an integrity failure, not a silent pass.
        if not _selectors_conflict(
            row["compose_project"],
            row["working_dir"],
            row["unit_name"],
            request.compose_project,
            request.working_dir,
            request.unit_name,
        ):
            raise AdoptionError(
                AdoptionErrorCode.INVALID_FIELD,
                "storage overlap check disagreed with the selector rule",
            )
        return row["application_id"]

    async def _foreign_server(
        self,
        store: ApplicationManifestStore,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Another server holding this application identity, if any.

        Two sources are checked (either is a conflict): the live projection
        row — authoritative SQL state — and the canonical apps tree, which
        still refuses when the projection was purged while the original
        server's manifest pair survives (crash window or out-of-band purge).
        """
        async with self._sessions() as session:
            observed = await session.execute(
                _SELECT_FOREIGN_SERVER,
                {
                    "server_id": server_id,
                    "application_id": application_id,
                },
            )
        found = observed.scalar()
        if found is not None:
            return uuid.UUID(str(found))
        for other in store.servers_storing(application_id):
            if other != server_id:
                return other
        return None


# ---------------------------------------------------------------------------
# File-state classification helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileState:
    stored: StoredManifest | None
    error: AdoptionError | None


def _read_file(
    store: ApplicationManifestStore, server_id: uuid.UUID, application_id: uuid.UUID
) -> _FileState:
    """Classify one canonical read; rejections stay value-free."""
    try:
        return _FileState(stored=store.read(server_id, application_id), error=None)
    except AdoptionError as exc:
        return _FileState(stored=None, error=exc)


def _read_file_or_absent(
    store: ApplicationManifestStore, server_id: uuid.UUID, application_id: uuid.UUID
) -> _FileState:
    """Like :func:`_read_file` but NOT_FOUND means "manifest absent" — the
    normal create/delete baseline; every other rejection stays surfaced."""
    state = _read_file(store, server_id, application_id)
    if state.error is not None and state.error.code is AdoptionErrorCode.NOT_FOUND:
        return _FileState(stored=None, error=None)
    return state


def _matches(
    stored: StoredManifest, revision: int | None, sha256: str | None
) -> bool:
    return revision is not None and sha256 is not None and (
        stored.revision == revision and stored.sha256 == sha256
    )
