"""Confined atomic persistence for model connections and model profiles.

Card ``06-model-store`` — consumer of the frozen schema producer
``mai.agents.models`` (integrated slice ``06-model-config``) and of the
storage semantics proven by ``mai.profiles.files`` (integrated slice
``02-file-store``), against contracts.md **C02/C03/C06**:

- **C06 rule 1** — provider endpoint config and model selection are separate
  records. A connection (base URL, credential *reference*, timeouts, optional
  headers, declared capabilities) and a model profile (model identifier,
  allowlisted settings) are stored as independent versioned documents; the
  selected (connection, profile) pair is a third record that names both and
  is validated at selection time with
  :func:`mai.agents.models.validate_selection` (pairing + capability
  gating), never implied by directory layout.
- **C03 rule 6** — a connection stores only the credential *reference*.
  Inline credential fields are already rejected by the producer's parser
  (``inline_credential_forbidden``); the store additionally never echoes
  document content: errors, CLI output and metadata are value-free (names,
  revisions and hashes only). Credential *values* re-resolve at call time
  through :class:`mai.credentials.env.EnvResolver`
  (:func:`mai.agents.models.build_model`) — never through this store.
- **C02 storage semantics** — C02 rule 2 freezes the canonical path scheme
  for *server* profiles; the model-config subtree layout
  (``models/connections/<name>.json``, ``models/profiles/<name>.json``,
  ``models/selection.json``) is owned by this module under the same frozen
  semantics: the data root is configured explicitly (C02 rule 1 — never an
  ambient ``HOME`` inference) and created ``0o700``; every path is derived
  structurally from a validated slug name plus fixed file names, so
  traversal and absolute user-supplied paths are unrepresentable; any
  symbolic link at any path component aborts the operation with
  ``path_escape`` (reads additionally open with ``O_NOFOLLOW``); each record
  carries the C02 rule-4 version triple — integer ``revision`` (starts at 1,
  ``0`` = absent), ``schema_revision``
  (:data:`mai.agents.models.SCHEMA_VERSION`) and SHA-256 ``content hash`` —
  in a ``<record>.meta.json`` sidecar.
- **C02 rule 5 / crash windows** — atomic replace is temp file + ``fsync`` +
  ``os.replace`` + directory ``fsync``. The canonical file is replaced before
  its metadata (window W2: the file is authoritative; a crash in between is
  reported by the read operations as ``hash_mismatch``, never silently
  reconciled). Stale temp files from an interrupted write are cleaned by the
  next successful write. Deletion removes the canonical file first, then the
  metadata; a crash between the two leaves orphan metadata that the retrying
  delete completes. Write-journal/outbox phases and the Postgres projection
  are outside this card (C02 rule 5).

Write-path validation: both ``write_*`` operations validate the document
with the frozen producer *before* any filesystem effect, so the store can
never hold a schema-invalid document;
:class:`mai.agents.models.ModelConfigError` propagates unchanged (the API
card maps it 1:1 onto the C01 envelope). A byte cap
(:data:`MAX_DOCUMENT_BYTES`) bounds parser work before parsing; documents
must be JSON (the producer's document format — its CLI loads JSON) and
unparsable JSON is a classified ``unparsable_document`` rejection. JSON has
no alias/anchor expansion surface; within the byte cap the pinned runtime's
JSON parser does not recurse to the interpreter limit (measured on 3.14.6:
32768 nesting levels parse), and a ``RecursionError`` is still mapped to
``document_too_deep`` defensively.

Identity: the record name is the storage identity. The caller supplies it
explicitly (from the admin route, not from document text) and the embedded
document ``name`` must match (``name_mismatch``), mirroring the canonical
``server_id`` rule so a stored document can never disagree with the identity
its path keys on.

Version conflicts: every mutation takes ``expected_revision`` (the revision
the caller last observed; ``0`` to create) and fails with ``stale_revision``
otherwise — the stale-caller guard for read-modify-write across API calls.
Single-writer-per-record is a caller invariant (engine mutation locks, C05
rule 6 pattern); the store guarantees atomic visibility of each committed
pair member and root confinement.

Active-run binding (acceptance 2): :meth:`ModelConfigStore.bind` pins an
explicit (connection, profile) pair together with both version triples into
an immutable :class:`ModelBinding`. A run holding a binding keeps the pinned
profile revision even after an administrator edits the stored documents;
:meth:`ModelConfigStore.revalidate` reports the drift explicitly, and
:meth:`ModelBinding.rebuild` re-resolves credentials (fresh ``EnvResolver``)
against the pinned revision — credentials/grants revalidate, the profile
revision is retained. Deleting a document that the stored selection names is
refused (``selection_conflict``) so the store never keeps a dangling
selection; re-point the selection first.

Error discipline: every rejection is a :class:`ModelStoreError` carrying a
stable ``code`` (C01 envelope vocabulary) and a value-free detail — document
content and credential material are never echoed. The module reads no
process environment, network or clock; results derive from explicit
arguments and filesystem state only.

POSIX assumption: ``O_NOFOLLOW`` and directory ``fsync`` are Linux/macOS
facilities; Windows targets are a frozen non-goal (C02, runtime ADR).

Production entrypoint (exit contract 0 success / 1 structured rejection /
2 usage or IO error)::

    uv run python -m mai.agents.model_store write-connection ROOT NAME FILE --expect-revision N
    uv run python -m mai.agents.model_store read-connection ROOT NAME
    uv run python -m mai.agents.model_store set-selection ROOT CONNECTION PROFILE --expect-revision N
    uv run python -m mai.agents.model_store get-selection ROOT
    uv run python -m mai.agents.model_store list ROOT

All output is value-free: names, revisions and hashes — never document
content or credential material.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mai.agents.models import (
    SCHEMA_VERSION,
    ModelConfigError,
    ModelConnection,
    ModelProfile,
    ModelSelection,
    build_model,
    parse_connection,
    parse_profile,
    validate_selection,
)
from mai.credentials.env import EnvResolver

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from mai.agents.models import ResolvedModel

__all__ = [
    "ABSENT_REVISION",
    "CONNECTIONS_DIRNAME",
    "MAX_DOCUMENT_BYTES",
    "META_FORMAT",
    "MODELS_DIRNAME",
    "PROFILES_DIRNAME",
    "SELECTION_FILENAME",
    "BindingStatus",
    "ModelBinding",
    "ModelConfigStore",
    "ModelStoreError",
    "ModelStoreErrorCode",
    "StoredDocument",
    "StoredSelection",
    "main",
]

MODELS_DIRNAME = "models"
CONNECTIONS_DIRNAME = "connections"
PROFILES_DIRNAME = "profiles"
SELECTION_FILENAME = "selection.json"
META_SUFFIX = ".meta.json"
META_FORMAT = 1

#: Byte cap for one canonical document, enforced before parsing. A model
#: config document is a small admin-controlled JSON document; 64 KiB is two
#: orders of magnitude above any legitimate fixture and bounds parser work
#: (same boundary as the canonical profile store).
MAX_DOCUMENT_BYTES = 64 * 1024

#: Revision convention for "no canonical record stored" (create baseline).
ABSENT_REVISION = 0

#: Record kinds.
CONNECTION_KIND = "connection"
PROFILE_KIND = "profile"
SELECTION_KIND = "selection"

_KIND_DIRS = {CONNECTION_KIND: CONNECTIONS_DIRNAME, PROFILE_KIND: PROFILES_DIRNAME}

#: Connection name / profile name: lowercase slug, frozen by the producer
#: (``mai.agents.models``). Kept local for the same reason the producer keeps
#: a local copy of the credential-reference shape: the defining regex is
#: private there, and a divergence would be a review finding, not a silent
#: import of a private symbol.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_HEX_DIGITS = frozenset("0123456789abcdef")


class ModelStoreErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_NAME = "invalid_name"
    INVALID_REVISION = "invalid_revision"
    NAME_MISMATCH = "name_mismatch"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    UNPARSABLE_DOCUMENT = "unparsable_document"
    PATH_ESCAPE = "path_escape"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    ORPHAN_METADATA = "orphan_metadata"
    HASH_MISMATCH = "hash_mismatch"
    #: Deleting a connection/profile that the stored selection names would
    #: leave a dangling selection; re-point (or clear) the selection first.
    SELECTION_CONFLICT = "selection_conflict"


class ModelStoreError(ValueError):
    """One structured, value-free model-store rejection."""

    def __init__(self, code: ModelStoreErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """Stored connection/profile: content plus the C02 rule-4 version triple."""

    kind: str
    name: str
    text: str
    revision: int
    schema_revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredSelection:
    """The selected (connection, profile) pair as a versioned record.

    The selection *names* both records; it does not pin their revisions —
    :meth:`ModelConfigStore.bind` is the revision-pinning operation for
    active runs.
    """

    connection: str
    profile: str
    revision: int
    schema_revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """An active run's explicit (connection, profile) revision pin.

    Holds the *parsed* documents captured at bind time together with both
    version triples, so the run keeps its exact profile revision across
    administrator edits (acceptance 2) while :meth:`rebuild` re-resolves
    credential material at call time (C03 rule 6).
    """

    connection: ModelConnection
    profile: ModelProfile
    connection_revision: int
    profile_revision: int
    connection_sha256: str
    profile_sha256: str

    def selection(self) -> ModelSelection:
        """The validated selection for the pinned documents."""
        return validate_selection(self.connection, self.profile)

    def rebuild(self, *, resolver: EnvResolver | None = None) -> "ResolvedModel":
        """Build a live model from the pinned revision with fresh credentials.

        Credential material resolves at call time through ``resolver``
        (C03 rule 6); the pinned documents are never re-read from the store,
        so an administrator edit cannot silently move a bound run.
        """
        return build_model(self.selection(), resolver=resolver)


@dataclass(frozen=True, slots=True)
class BindingStatus:
    """Drift report for a binding against the store's current records.

    ``*_missing`` — the named record was deleted after the bind.
    ``*_changed`` — the stored revision/hash differs from the pin.
    ``stored_*_revision`` — current stored revision (``None`` when missing).
    """

    connection_missing: bool
    profile_missing: bool
    connection_changed: bool
    profile_changed: bool
    stored_connection_revision: int | None
    stored_profile_revision: int | None


class ModelConfigStore:
    """Confined, revision-checked, atomic model-configuration storage.

    Single-writer per record is a caller invariant (engine mutation locks);
    the store guarantees atomic visibility of each committed file pair
    member, stale-caller rejection and root confinement.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        """Resolved data root (C02 rule 1: explicitly configured entry point)."""
        return self._root

    # -- connections -------------------------------------------------------

    def write_connection(
        self, name: str, text: str, *, expected_revision: int
    ) -> StoredDocument:
        """Validate then atomically store one connection document."""
        return self._write_document(CONNECTION_KIND, name, text, expected_revision)

    def read_connection(self, name: str) -> StoredDocument:
        """Return the stored connection, hash-verified against its metadata."""
        return self._read_document(CONNECTION_KIND, name)

    def list_connections(self) -> tuple[str, ...]:
        """Stored connection names, sorted."""
        return self._list_kind(CONNECTION_KIND)

    def delete_connection(self, name: str, *, expected_revision: int) -> None:
        """Revision-checked removal (canonical file first, metadata second)."""
        self._delete_document(CONNECTION_KIND, name, expected_revision)

    # -- profiles ----------------------------------------------------------

    def write_profile(
        self, name: str, text: str, *, expected_revision: int
    ) -> StoredDocument:
        """Validate then atomically store one model-profile document."""
        return self._write_document(PROFILE_KIND, name, text, expected_revision)

    def read_profile(self, name: str) -> StoredDocument:
        """Return the stored profile, hash-verified against its metadata."""
        return self._read_document(PROFILE_KIND, name)

    def list_profiles(self) -> tuple[str, ...]:
        """Stored profile names, sorted."""
        return self._list_kind(PROFILE_KIND)

    def delete_profile(self, name: str, *, expected_revision: int) -> None:
        """Revision-checked removal (canonical file first, metadata second)."""
        self._delete_document(PROFILE_KIND, name, expected_revision)

    # -- selection ---------------------------------------------------------

    def set_selection(
        self, connection: str, profile: str, *, expected_revision: int
    ) -> StoredSelection:
        """Select an explicit (connection, profile) pair as a versioned record.

        Both documents must already be stored, and the pair must satisfy
        :func:`mai.agents.models.validate_selection` (name pairing plus
        endpoint-capability gating, C06 rule 1) — a
        :class:`~mai.agents.models.ModelConfigError` propagates unchanged.
        """
        connection = self._validated_name(connection)
        profile = self._validated_name(profile)
        self._validated_expected_revision(expected_revision)
        connection_doc = self.read_connection(connection)
        profile_doc = self.read_profile(profile)
        # Producer-owned binding check: pairing + capability gating (C06).
        validate_selection(
            parse_connection(self._document_mapping(connection_doc.text)),
            parse_profile(self._document_mapping(profile_doc.text)),
        )
        current = self._current_revision_of(
            self._selection_relative(), self._selection_meta_relative(),
            load_meta=lambda: self._load_meta(SELECTION_KIND, None),
        )
        if current != expected_revision:
            raise ModelStoreError(
                ModelStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        text = (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "connection": connection,
                    "profile": profile,
                },
                sort_keys=True,
            )
            + "\n"
        )
        data = text.encode("utf-8")
        target = self._confined(self._selection_relative())
        meta_path = self._confined(self._selection_meta_relative())
        self._ensure_dir(target.parent)
        self._remove_stale_temps(
            target.parent, (f"{SELECTION_FILENAME}.", f"{SELECTION_FILENAME}{META_SUFFIX}.")
        )
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target, data)
        # C02 W2: canonical first, metadata second — the file is authoritative.
        self._atomic_write(
            meta_path, self._meta_bytes(SELECTION_KIND, None, revision, digest)
        )
        return StoredSelection(
            connection=connection,
            profile=profile,
            revision=revision,
            schema_revision=SCHEMA_VERSION,
            sha256=digest,
        )

    def get_selection(self) -> StoredSelection:
        """Return the stored selection, hash-verified against its metadata."""
        target = self._confined(self._selection_relative())
        meta_path = self._confined(self._selection_meta_relative())
        if not target.exists():
            raise ModelStoreError(
                ModelStoreErrorCode.NOT_FOUND,
                "no model selection stored",
            )
        meta = self._load_meta(SELECTION_KIND, None)
        data = self._read_verified(target, meta)
        document = self._document_mapping(data.decode("utf-8"))
        shape = (
            isinstance(document, dict)
            and set(document) == {"schema_version", "connection", "profile"}
            and document.get("schema_version") == SCHEMA_VERSION
        )
        if not shape:
            raise ModelStoreError(
                ModelStoreErrorCode.METADATA_INVALID,
                "stored selection does not match the frozen selection shape",
            )
        return StoredSelection(
            connection=self._validated_name(document["connection"]),
            profile=self._validated_name(document["profile"]),
            revision=int(meta["revision"]),  # type: ignore[arg-type]
            schema_revision=int(meta["schema_revision"]),  # type: ignore[arg-type]
            sha256=str(meta["sha256"]),
        )

    def clear_selection(self, *, expected_revision: int) -> None:
        """Revision-checked removal of the selection record."""
        self._validated_expected_revision(expected_revision)
        target = self._confined(self._selection_relative())
        meta_path = self._confined(self._selection_meta_relative())
        current = self._current_revision_of(
            self._selection_relative(),
            self._selection_meta_relative(),
            load_meta=lambda: self._load_meta(SELECTION_KIND, None),
            tolerate_orphan=True,
        )
        if current == ABSENT_REVISION:
            raise ModelStoreError(
                ModelStoreErrorCode.NOT_FOUND,
                "no model selection stored",
            )
        if current != expected_revision:
            raise ModelStoreError(
                ModelStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        self._fsync_directory(target.parent)

    # -- active-run binding (acceptance 2) ---------------------------------

    def bind(
        self, connection: str | None = None, profile: str | None = None
    ) -> ModelBinding:
        """Pin an explicit (connection, profile) pair for an active run.

        Without arguments, the stored selection names the pair (a missing
        selection is a classified ``not_found``). With explicit names the
        stored selection is not consulted. Both documents must be stored and
        pair-valid (:func:`mai.agents.models.validate_selection`).
        """
        if connection is None or profile is None:
            stored_profiles = self.list_profiles()
            if (
                profile is not None
                and profile in stored_profiles
                and connection is None
            ):
                profile = self._validated_name(profile)
                profile_doc = self.read_profile(profile)
                raw_mapping = self._document_mapping(profile_doc.text)
                parsed_profile = parse_profile(raw_mapping)  # type: ignore[arg-type]
                connection = parsed_profile.connection
            else:
                selected = self.get_selection()
                connection = (
                    connection if connection is not None else selected.connection
                )
                if profile is None or (
                    profile == "default" and "default" not in stored_profiles
                ):
                    profile = selected.profile
        else:
            connection = self._validated_name(connection)
            profile = self._validated_name(profile)
            if profile == "default" and "default" not in self.list_profiles():
                try:
                    selected = self.get_selection()
                    profile = selected.profile
                except ModelStoreError:
                    pass
        connection_doc = self.read_connection(connection)
        profile_doc = self.read_profile(profile)
        parsed_connection = parse_connection(
            self._document_mapping(connection_doc.text)
        )
        parsed_profile = parse_profile(self._document_mapping(profile_doc.text))
        # Producer-owned binding check: pairing + capability gating (C06) —
        # eager, so an invalid explicit pair is rejected at bind time.
        validate_selection(parsed_connection, parsed_profile)
        return ModelBinding(
            connection=parsed_connection,
            profile=parsed_profile,
            connection_revision=connection_doc.revision,
            profile_revision=profile_doc.revision,
            connection_sha256=connection_doc.sha256,
            profile_sha256=profile_doc.sha256,
        )

    def revalidate(self, binding: ModelBinding) -> BindingStatus:
        """Report drift between a pinned binding and the stored records.

        The binding itself is never mutated: the caller decides whether to
        keep the pinned revision (credentials/grants revalidate, profile
        retained) or to re-bind explicitly.
        """
        fields: dict[str, object] = {
            "connection_missing": False,
            "profile_missing": False,
            "connection_changed": False,
            "profile_changed": False,
            "stored_connection_revision": None,
            "stored_profile_revision": None,
        }
        for kind, current_name, pinned_revision, pinned_digest in (
            (
                CONNECTION_KIND,
                binding.connection.name,
                binding.connection_revision,
                binding.connection_sha256,
            ),
            (
                PROFILE_KIND,
                binding.profile.name,
                binding.profile_revision,
                binding.profile_sha256,
            ),
        ):
            prefix = "connection" if kind == CONNECTION_KIND else "profile"
            try:
                current = self._read_document(kind, current_name)
            except ModelStoreError as error:
                if error.code is not ModelStoreErrorCode.NOT_FOUND:
                    raise
                fields[f"{prefix}_missing"] = True
                continue
            fields[f"stored_{prefix}_revision"] = current.revision
            fields[f"{prefix}_changed"] = (
                current.revision != pinned_revision
                or current.sha256 != pinned_digest
            )
        return BindingStatus(**fields)  # type: ignore[arg-type]

    # -- shared document machinery -----------------------------------------

    def _write_document(
        self, kind: str, name: str, text: str, expected_revision: int
    ) -> StoredDocument:
        name = self._validated_name(name)
        self._validated_expected_revision(expected_revision)
        data = self._checked_document(text)
        parsed = self._parse_document(kind, self._document_mapping(text))
        if parsed.name != name:
            raise ModelStoreError(
                ModelStoreErrorCode.NAME_MISMATCH,
                f"embedded document name {parsed.name!r} does not match the "
                f"storage identity {name!r}",
            )
        target = self._confined(self._document_relative(kind, name))
        meta_path = self._confined(self._meta_relative(kind, name))
        current = self._current_revision_of(
            self._document_relative(kind, name),
            self._meta_relative(kind, name),
            load_meta=lambda: self._load_meta(kind, name),
        )
        if current != expected_revision:
            raise ModelStoreError(
                ModelStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_dir(target.parent)
        self._remove_stale_temps(
            target.parent, (f"{name}.json.", f"{name}{META_SUFFIX}.")
        )
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target, data)
        # C02 W2: canonical first, metadata second — the file is authoritative.
        # A crash in between leaves the new file with lagging metadata, which
        # the read operations report as hash_mismatch; reconciliation is the
        # projection card's journal duty, never invented here.
        self._atomic_write(meta_path, self._meta_bytes(kind, name, revision, digest))
        return StoredDocument(
            kind=kind,
            name=name,
            text=text,
            revision=revision,
            schema_revision=SCHEMA_VERSION,
            sha256=digest,
        )

    def _read_document(self, kind: str, name: str) -> StoredDocument:
        name = self._validated_name(name)
        target = self._confined(self._document_relative(kind, name))
        meta_path = self._confined(self._meta_relative(kind, name))
        if not target.exists():
            raise ModelStoreError(
                ModelStoreErrorCode.NOT_FOUND,
                f"no {kind} stored under this name",
            )
        meta = self._load_meta(kind, name)
        data = self._read_verified(target, meta)
        return StoredDocument(
            kind=kind,
            name=name,
            text=data.decode("utf-8"),
            revision=int(meta["revision"]),  # type: ignore[arg-type]
            schema_revision=int(meta["schema_revision"]),  # type: ignore[arg-type]
            sha256=str(meta["sha256"]),
        )

    def _list_kind(self, kind: str) -> tuple[str, ...]:
        directory = self._confined(f"{MODELS_DIRNAME}/{_KIND_DIRS[kind]}")
        if not directory.is_dir():
            return ()
        names = [
            entry.name[: -len(".json")]
            for entry in directory.iterdir()
            if entry.name.endswith(".json") and not entry.name.endswith(META_SUFFIX)
        ]
        return tuple(sorted(names))

    def _delete_document(self, kind: str, name: str, expected_revision: int) -> None:
        name = self._validated_name(name)
        self._validated_expected_revision(expected_revision)
        self._reject_selection_conflict(kind, name)
        target = self._confined(self._document_relative(kind, name))
        meta_path = self._confined(self._meta_relative(kind, name))
        current = self._current_revision_of(
            self._document_relative(kind, name),
            self._meta_relative(kind, name),
            load_meta=lambda: self._load_meta(kind, name),
            tolerate_orphan=True,
        )
        if current == ABSENT_REVISION:
            raise ModelStoreError(
                ModelStoreErrorCode.NOT_FOUND,
                f"nothing stored for this {kind} name",
            )
        if current != expected_revision:
            raise ModelStoreError(
                ModelStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._remove_stale_temps(
            target.parent, (f"{name}.json.", f"{name}{META_SUFFIX}.")
        )
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        self._fsync_directory(target.parent)

    def _reject_selection_conflict(self, kind: str, name: str) -> None:
        """Refuse deleting a record the stored selection names.

        Best-effort ordering under the caller's single-writer invariant: the
        selection is read before the delete begins.
        """
        try:
            selected = self.get_selection()
        except ModelStoreError as error:
            if error.code is not ModelStoreErrorCode.NOT_FOUND:
                raise
            return
        held = selected.connection if kind == CONNECTION_KIND else selected.profile
        if held == name:
            raise ModelStoreError(
                ModelStoreErrorCode.SELECTION_CONFLICT,
                f"stored selection names this {kind}; re-point or clear the "
                "selection before deleting",
            )

    # -- confinement and state helpers -------------------------------------

    @staticmethod
    def _validated_name(name: object) -> str:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ModelStoreError(
                ModelStoreErrorCode.INVALID_NAME,
                "name must match [a-z0-9][a-z0-9_-]{0,63}",
            )
        return name

    @staticmethod
    def _validated_expected_revision(expected_revision: object) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ModelStoreError(
                ModelStoreErrorCode.INVALID_REVISION,
                "expected_revision must be a non-negative int",
            )

    def _document_relative(self, kind: str, name: str) -> str:
        return f"{MODELS_DIRNAME}/{_KIND_DIRS[kind]}/{name}.json"

    def _meta_relative(self, kind: str, name: str) -> str:
        return f"{MODELS_DIRNAME}/{_KIND_DIRS[kind]}/{name}{META_SUFFIX}"

    def _selection_relative(self) -> str:
        return f"{MODELS_DIRNAME}/{SELECTION_FILENAME}"

    def _selection_meta_relative(self) -> str:
        return f"{MODELS_DIRNAME}/{SELECTION_FILENAME}{META_SUFFIX}"

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural (validated
        slug + fixed names)."""
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise ModelStoreError(
                    ModelStoreErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise ModelStoreError(
                ModelStoreErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    @staticmethod
    def _checked_document(text: str) -> bytes:
        """Encode and harden one candidate document before parsing.

        Byte cap first (bounds parser work). The returned bytes are the
        exact canonical representation hashed and stored on success;
        unparsable JSON is classified by :meth:`_document_mapping`.
        """
        data = text.encode("utf-8")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ModelStoreError(
                ModelStoreErrorCode.DOCUMENT_TOO_LARGE,
                f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
            )
        return data

    @staticmethod
    def _document_mapping(text: str) -> object:
        """Parse one stored candidate document (JSON, producer's format).

        Unparsable JSON is a classified ``unparsable_document`` rejection; a
        parser recursion failure is ``document_too_deep`` (defensive symmetry
        with the canonical profile store — within the byte cap the pinned
        runtime's JSON parser does not recurse to the interpreter limit).
        """
        try:
            return json.loads(text)
        except RecursionError:
            raise ModelStoreError(
                ModelStoreErrorCode.DOCUMENT_TOO_DEEP,
                "document nesting exceeds the supported depth",
            ) from None
        except ValueError:
            raise ModelStoreError(
                ModelStoreErrorCode.UNPARSABLE_DOCUMENT,
                "document is not a valid JSON document",
            ) from None

    @staticmethod
    def _parse_document(
        kind: str, document: object
    ) -> ModelConnection | ModelProfile:
        """Producer-owned validation before any filesystem effect."""
        if kind == CONNECTION_KIND:
            return parse_connection(document)  # type: ignore[arg-type]
        return parse_profile(document)  # type: ignore[arg-type]

    @staticmethod
    def _read_verified(target: Path, meta: dict[str, object]) -> bytes:
        """Read a canonical file with ``O_NOFOLLOW`` and verify its hash."""
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise ModelStoreError(
                ModelStoreErrorCode.NOT_FOUND,
                "no canonical record stored for this identity",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise ModelStoreError(
                ModelStoreErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery must "
                "reconcile from the file hash",
            )
        return data

    def _load_meta(
        self, kind: str, name: str | None
    ) -> dict[str, object]:
        """Load and shape-check the sidecar metadata for a record path."""
        meta_path = self._confined(
            self._selection_meta_relative()
            if kind == SELECTION_KIND
            else self._meta_relative(kind, str(name))
        )
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            raise ModelStoreError(
                ModelStoreErrorCode.METADATA_MISSING,
                "canonical record exists without revision metadata; recovery "
                "must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ModelStoreError(
                ModelStoreErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        if (
            not isinstance(meta, dict)
            or meta.get("format") != META_FORMAT
            or meta.get("kind") != kind
            or meta.get("name") != name
            or not _is_positive_int(meta.get("revision"))
            or not _is_positive_int(meta.get("schema_revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
        ):
            raise ModelStoreError(
                ModelStoreErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        return meta

    def _current_revision_of(
        self,
        document_relative: str,
        meta_relative: str,
        *,
        load_meta: Callable[[], dict[str, object]],
        tolerate_orphan: bool = False,
    ) -> int:
        """Pairing-aware current revision; ``ABSENT_REVISION`` when both absent."""
        target = self._confined(document_relative)
        meta_path = self._confined(meta_relative)
        has_file = target.exists()
        has_meta = meta_path.exists()
        if not has_file and not has_meta:
            return ABSENT_REVISION
        if has_file and not has_meta:
            raise ModelStoreError(
                ModelStoreErrorCode.METADATA_MISSING,
                "canonical record exists without revision metadata; recovery "
                "must reconcile before further writes",
            )
        meta = load_meta()
        if not has_file and not tolerate_orphan:
            raise ModelStoreError(
                ModelStoreErrorCode.ORPHAN_METADATA,
                "revision metadata exists without a canonical record; "
                "complete the delete before writing",
            )
        return int(meta["revision"])  # type: ignore[arg-type]

    @staticmethod
    def _ensure_dir(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)

    @staticmethod
    def _meta_bytes(
        kind: str, name: str | None, revision: int, digest: str
    ) -> bytes:
        payload = {
            "format": META_FORMAT,
            "kind": kind,
            "name": name,
            "revision": revision,
            "schema_revision": SCHEMA_VERSION,
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
        ModelConfigStore._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist a directory entry change (rename/unlink durability)."""
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _remove_stale_temps(directory: Path, prefixes: tuple[str, ...]) -> None:
        """Best-effort cleanup of this store's crash-orphaned temp files."""
        if not directory.is_dir():
            return
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _document_view(stored: StoredDocument) -> dict[str, object]:
    return {
        "kind": stored.kind,
        "name": stored.name,
        "revision": stored.revision,
        "schema_revision": stored.schema_revision,
        "sha256": stored.sha256,
    }


def _selection_view(stored: StoredSelection) -> dict[str, object]:
    return {
        "kind": SELECTION_KIND,
        "connection": stored.connection,
        "profile": stored.profile,
        "revision": stored.revision,
        "schema_revision": stored.schema_revision,
        "sha256": stored.sha256,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.agents.model_store",
        description=(
            "Confined atomic model-configuration store (contracts.md C02/C06). "
            "All output is value-free; document content and credential "
            "material are never printed."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_root(target: argparse.ArgumentParser) -> None:
        target.add_argument("root", help="data root directory (created 0o700)")

    def add_name(target: argparse.ArgumentParser) -> None:
        target.add_argument("name", help="record name ([a-z0-9][a-z0-9_-]{0,63})")

    def add_expect(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--expect-revision",
            type=int,
            required=True,
            help="revision the caller last observed (0 = create)",
        )

    for verb, kind in (("connection", CONNECTION_KIND), ("profile", PROFILE_KIND)):
        write_command = subparsers.add_parser(
            f"write-{verb}",
            help=f"validate and atomically store a {kind} JSON document",
        )
        add_root(write_command)
        add_name(write_command)
        write_command.add_argument(
            "document", help=f"path of the {kind} JSON document to store"
        )
        add_expect(write_command)

        read_command = subparsers.add_parser(
            f"read-{verb}",
            help=f"print the stored {kind} version triple as one JSON line",
        )
        add_root(read_command)
        add_name(read_command)

        delete_command = subparsers.add_parser(
            f"delete-{verb}", help=f"revision-checked removal of the {kind} pair"
        )
        add_root(delete_command)
        add_name(delete_command)
        add_expect(delete_command)

    list_command = subparsers.add_parser(
        "list", help="print stored connection and profile names"
    )
    add_root(list_command)

    set_command = subparsers.add_parser(
        "set-selection", help="store the selected (connection, profile) pair"
    )
    add_root(set_command)
    set_command.add_argument("connection", help="stored connection name")
    set_command.add_argument("profile", help="stored profile name")
    add_expect(set_command)

    get_command = subparsers.add_parser(
        "get-selection", help="print the stored selection as one JSON line"
    )
    add_root(get_command)

    clear_command = subparsers.add_parser(
        "clear-selection", help="revision-checked removal of the selection"
    )
    add_root(clear_command)
    add_expect(clear_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; exit 0 success / 1 structured rejection / 2 usage or IO."""
    args = _build_parser().parse_args(argv)
    try:
        store = ModelConfigStore(args.root)
        command = args.command
        if command == "write-connection":
            stored = store.write_connection(
                args.name,
                Path(args.document).read_text(encoding="utf-8"),
                expected_revision=args.expect_revision,
            )
            print(json.dumps(_document_view(stored), sort_keys=True))
        elif command == "write-profile":
            stored = store.write_profile(
                args.name,
                Path(args.document).read_text(encoding="utf-8"),
                expected_revision=args.expect_revision,
            )
            print(json.dumps(_document_view(stored), sort_keys=True))
        elif command == "read-connection":
            print(json.dumps(_document_view(store.read_connection(args.name)), sort_keys=True))
        elif command == "read-profile":
            print(json.dumps(_document_view(store.read_profile(args.name)), sort_keys=True))
        elif command == "delete-connection":
            store.delete_connection(args.name, expected_revision=args.expect_revision)
            print(
                json.dumps(
                    {"kind": CONNECTION_KIND, "name": args.name, "deleted": True},
                    sort_keys=True,
                )
            )
        elif command == "delete-profile":
            store.delete_profile(args.name, expected_revision=args.expect_revision)
            print(
                json.dumps(
                    {"kind": PROFILE_KIND, "name": args.name, "deleted": True},
                    sort_keys=True,
                )
            )
        elif command == "list":
            print(
                json.dumps(
                    {
                        "connections": list(store.list_connections()),
                        "profiles": list(store.list_profiles()),
                    },
                    sort_keys=True,
                )
            )
        elif command == "set-selection":
            stored = store.set_selection(
                args.connection, args.profile, expected_revision=args.expect_revision
            )
            print(json.dumps(_selection_view(stored), sort_keys=True))
        elif command == "get-selection":
            print(json.dumps(_selection_view(store.get_selection()), sort_keys=True))
        else:  # clear-selection
            store.clear_selection(expected_revision=args.expect_revision)
            print(
                json.dumps(
                    {"kind": SELECTION_KIND, "deleted": True}, sort_keys=True
                )
            )
        return 0
    except ModelConfigError as error:
        print(
            json.dumps(
                {"error": {"code": "model_config_rejected", "fields": error.fields}}
            ),
            file=sys.stderr,
        )
        return 1
    except ModelStoreError as error:
        print(
            json.dumps({"error": {"code": error.code.value, "message": error.detail}}),
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(
            f"io error: {error.strerror or error.__class__.__name__}",
            file=sys.stderr,
        )
        return 2
    except UnicodeDecodeError:
        print(
            json.dumps(
                {
                    "error": {
                        "code": ModelStoreErrorCode.UNPARSABLE_DOCUMENT.value,
                        "message": "document is not valid UTF-8",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
