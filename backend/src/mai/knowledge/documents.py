"""Confined atomic canonical Markdown document/runbook store with provenance.

Card ``07-doc-store`` (service ``knowledge``), against contracts.md **C02**
(canonical files) and **C07** (documents, retrieval and memory), consuming
the reviewed prerequisite disciplines of ``02-file-store`` (confinement and
atomic-replace crash windows) and ``03-credentials`` (secret-boundary
handling, C03 rule 6):

- **C02 rules 1–3** — the data root is configured explicitly by the caller
  (default controller-host ``~/.mai/`` stays an operator decision, never an
  ambient inference); the store resolves the root once at construction and
  creates it with restrictive permissions (dirs ``0o700``, files ``0o600``).
  Canonical doc paths are derived structurally from opaque UUIDs plus fixed
  names only: ``servers/<server_id>/docs/<doc_id>.md``,
  ``servers/<server_id>/runbooks/<doc_id>.md``,
  ``servers/<server_id>/applications/<app_id>/docs/<doc_id>.md`` (and the
  ``runbooks`` twin), plus the explicit shared namespace ``shared/docs/…`` /
  ``shared/runbooks/…`` (C07 rule 1: "server_id or explicit shared
  namespace"). ``app_id`` is accepted only together with a ``server_id`` —
  applications live under a server (C02 rule 2 path scheme). Traversal and
  absolute user-supplied paths are unrepresentable by construction; a
  symbolic link at *any* component of a canonical path aborts the operation
  with ``path_escape``, and reads open the file with ``O_NOFOLLOW``
  (mirroring ``mai.profiles.files``, whose profile/prompt scheme this card
  extends with the docs/runbooks half of the C02 rule-2 layout).
- **C02 rule 4** — canonical version: integer content ``revision`` (starts
  at 1; ``0`` denotes absent) plus SHA-256 ``content hash``, recorded in the
  sidecar ``<doc_id>.doc.meta.json`` next to the canonical file. There is no
  schema producer for Markdown in the frozen set, so no ``schema_revision``
  field: the version pair is (revision, sha256). The Postgres projection
  (``07-vector-schema`` document-metadata table) stores the same revision;
  this module owns only the file half.
- **C02 rule 5 / crash windows** — atomic replace is temp file + ``fsync``
  + ``os.replace`` + directory ``fsync``. Write order per commit: canonical
  file, then metadata, then the durable reindex intent (C07 rule 2: stale
  chunks are removed on source update/delete — the intent is the durable
  outbox record a downstream indexer reconciles from; a crash between file
  commit and intent leaves the previous intent, which a rebuild-from-source
  reconciliation resolves, never an invented successful sync). Delete order
  (C02 W4): the tombstone reindex intent is made durable *first*, then the
  canonical file and metadata are removed; the purge of the intent itself is
  the indexer's ack (``purge_intent``), after the projection confirms.
  A canonical file reappearing while its tombstone intent is still live is
  a conflict surfaced as ``tombstone_conflict`` — never a silent overwrite.
- **C07 rule 1** — the sidecar carries the source record: ``source_kind``
  (``authored`` / ``imported`` / ``generated``), ``origin`` (opaque
  provenance string, e.g. the operator name or an import source path),
  ``origin_revision`` (optional version of the origin document) and
  ``verification`` (``unverified`` / ``verified`` / ``hypothesis``).
  Credentials are excluded before chunking by policy of the chunk-index
  consumer; this store never reads credential material. Observed/generated
  wall-clock times are deliberately *not* stored here: the module reads no
  clock (deterministic, evidence-safe), and the SQL projection owns
  timestamps — the file record is the revision/hash half of the C07 rule-1
  record, the projection is the other half.
- **C07 rule 5** — ``verification`` separates outcomes from hypotheses at
  the storage boundary; which writer may claim ``verified`` is the
  doc-generation card's rule, this store only persists the enum.
- **Secret boundary (C03 rule 6 discipline, integrated ``03-credentials``
  review)** — host-wide secret *content* scanning is prohibited, so this
  store never inspects content for secrets. The enforcement points are
  structural: (a) the two outside-read seams — the explicit import and the
  CLI ``write`` FILE argument, the only operations that ever read a
  caller-supplied path outside the data root ("outside docs import
  explicit" names the import seam; the write seam enforces the identical
  guard via the shared ``_read_outside_source``) — refuse a fixed
  deny-list of credential-material file names
  (``.env`` family, PEM/PKCS#12/private-key names) with
  ``secret_file_forbidden``, and refuse symlinked or non-regular sources
  (``symlink_forbidden`` / ``not_a_file``) — the same fail-closed posture as
  the ``.env`` resolver; (b) every canonical-tree path is confined as above.
  A rejected import or write performs no write: no canonical file, no
  metadata, no intent, so nothing indexable is ever materialized.

Error discipline: every rejection is a :class:`DocumentStoreError` carrying
a stable ``code`` (C01 envelope vocabulary style) and a value-free detail —
document content is never echoed. The module reads no process environment,
network or clock; results derive from explicit arguments and filesystem
state only. Pure stdlib (no Markdown dependency: structural checks only —
UTF-8, byte cap, no NUL; Markdown parsing belongs to the chunker).

Single-writer per document is a caller invariant (engine locks, C05 rule 6),
same as the profile file store. POSIX assumption: ``O_NOFOLLOW`` and
directory ``fsync`` are Linux/macOS facilities; Windows targets are a frozen
non-goal.

Production entrypoint (exit contract 0 success / 1 structured rejection /
2 usage or IO error)::

    uv run python -m mai.knowledge.documents read ROOT (--server ID | --shared) [--app ID] --namespace docs DOC_ID
    uv run python -m mai.knowledge.documents write ROOT (--server ID | --shared) [--app ID] --namespace docs DOC_ID FILE --expect-revision N [--source-kind KIND] [--origin TEXT] [--origin-revision TEXT] [--verification STATUS]
    uv run python -m mai.knowledge.documents import ROOT (--server ID | --shared) [--app ID] --namespace docs [--doc-id ID] --source PATH
    uv run python -m mai.knowledge.documents delete ROOT (--server ID | --shared) [--app ID] --namespace docs DOC_ID --expect-revision N
    uv run python -m mai.knowledge.documents intents ROOT (--server ID | --shared) [--app ID]

``read``/``write``/``import`` print one JSON line with the revision/hash and
provenance metadata (never the document content) so transcripts stay
value-free; ``intents`` prints the durable reindex outbox of one scope.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

__all__ = [
    "ABSENT_REVISION",
    "MAX_DOCUMENT_BYTES",
    "MAX_ORIGIN_BYTES",
    "MAX_ORIGIN_REVISION_CHARS",
    "DocumentNamespace",
    "DocumentStoreError",
    "DocumentStoreErrorCode",
    "DocumentStore",
    "ReindexIntent",
    "SourceKind",
    "StoredDocument",
    "VerificationStatus",
    "main",
]

SERVERS_DIR = "servers"
SHARED_DIR = "shared"
APPLICATIONS_DIR = "applications"
DOCS_DIR = "docs"
RUNBOOKS_DIR = "runbooks"
INTENTS_DIR = "reindex"
DOC_SUFFIX = ".md"
META_SUFFIX = ".doc.meta.json"
META_FORMAT = 1
INTENT_FORMAT = 1

#: Revision convention for "no canonical document stored" (create baseline).
ABSENT_REVISION = 0

#: Byte cap for one canonical Markdown document, enforced before any write.
#: A runbook is larger than a profile but still an operator-scale document;
#: 1 MiB bounds IO/parser work for downstream consumers while staying three
#: orders of magnitude above any legitimate fixture (profile cap is 64 KiB,
#: mai.profiles.files).
MAX_DOCUMENT_BYTES = 1024 * 1024

#: Provenance string bounds (C07 rule 1 origin/version fields).
MAX_ORIGIN_BYTES = 256
MAX_ORIGIN_REVISION_CHARS = 128

_HEX_DIGITS = frozenset("0123456789abcdef")

#: Credential-material file names refused by the explicit outside import
#: (C03 rule 6 posture of the integrated ``03-credentials`` review: refuse
#: by name, never scan content). ``.env`` carries credentials by contract;
#: PEM/PKCS#12/key names carry key material. Content-based "secret
#: detection" is explicitly out: it would be unreliable host-wide scanning.
_SECRET_STEMS = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_SECRET_SUFFIXES = frozenset({"pem", "key", "p12", "pfx"})


class DocumentNamespace(StrEnum):
    """Canonical collection below a scope (C02 rule 2 docs/runbooks layout)."""

    DOCS = "docs"
    RUNBOOKS = "runbooks"


class SourceKind(StrEnum):
    """C07 rule 1 ``source_kind``: how the canonical content originated."""

    AUTHORED = "authored"
    IMPORTED = "imported"
    GENERATED = "generated"


class VerificationStatus(StrEnum):
    """C07 rule 5 boundary: outcomes are separated from hypotheses."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    HYPOTHESIS = "hypothesis"


class DocumentStoreErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_SERVER_ID = "invalid_server_id"
    APP_REQUIRES_SERVER = "app_requires_server"
    INVALID_APP_ID = "invalid_app_id"
    INVALID_DOC_ID = "invalid_doc_id"
    INVALID_NAMESPACE = "invalid_namespace"
    INVALID_REVISION = "invalid_revision"
    INVALID_PROVENANCE = "invalid_provenance"
    INVALID_DOCUMENT = "invalid_document"
    DOCUMENT_TOO_LARGE = "document_too_large"
    PATH_ESCAPE = "path_escape"
    SECRET_FILE_FORBIDDEN = "secret_file_forbidden"
    NOT_A_FILE = "not_a_file"
    SYMLINK_FORBIDDEN = "symlink_forbidden"
    NOT_READABLE = "not_readable"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    ORPHAN_METADATA = "orphan_metadata"
    TOMBSTONE_CONFLICT = "tombstone_conflict"
    HASH_MISMATCH = "hash_mismatch"
    INTENT_INVALID = "intent_invalid"


class DocumentStoreError(ValueError):
    """One structured, value-free document-store rejection."""

    def __init__(self, code: DocumentStoreErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """Canonical Markdown document: content plus revision/hash + provenance."""

    doc_id: uuid.UUID
    server_id: uuid.UUID | None
    app_id: uuid.UUID | None
    namespace: DocumentNamespace
    text: str
    revision: int
    sha256: str
    source_kind: SourceKind
    origin: str
    origin_revision: str | None
    verification: VerificationStatus


@dataclass(frozen=True, slots=True)
class ReindexIntent:
    """One durable reindex outbox record (C02 rule 5 journal/outbox phase).

    ``op`` is ``upsert`` (index from the committed source) or ``delete``
    (remove all chunks of this source). A delete intent is a *tombstone*:
    it stays durable until the indexer acknowledges with
    :meth:`DocumentStore.purge_intent` (C02 W4: purge after the projection
    confirms, never before).
    """

    doc_id: uuid.UUID
    server_id: uuid.UUID | None
    app_id: uuid.UUID | None
    namespace: DocumentNamespace
    op: str
    revision: int
    sha256: str | None
    tombstone: bool


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (rename/unlink durability)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def _is_secret_name(name: str) -> bool:
    """Credential-material name deny-list for the explicit outside import."""
    lower = name.lower()
    if lower == ".env" or lower.startswith(".env."):
        return True
    if lower.endswith("~"):
        lower = lower[:-1]
    stem, dot, suffix = lower.partition(".")
    if not dot:
        return stem in _SECRET_STEMS
    if suffix in _SECRET_SUFFIXES:
        return True
    return stem in _SECRET_STEMS


def _checked_document(data: bytes) -> str:
    """Structural check of one candidate canonical Markdown document.

    Byte cap, strict UTF-8, no NUL bytes. Deliberately *no* content-based
    secret heuristics (C03 rule 6: no host-wide scanning) and no Markdown
    parsing (the chunker owns parsing).
    """
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentStoreError(
            DocumentStoreErrorCode.DOCUMENT_TOO_LARGE,
            f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    if b"\x00" in data:
        raise DocumentStoreError(
            DocumentStoreErrorCode.INVALID_DOCUMENT,
            "document contains NUL bytes and is not canonical Markdown text",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise DocumentStoreError(
            DocumentStoreErrorCode.INVALID_DOCUMENT,
            "document is not valid UTF-8 text",
        ) from None


def _read_outside_source(source: Path) -> bytes:
    """Guarded read of one caller-supplied path outside the data root.

    The single enforcement point shared by every outside-read seam (the
    explicit import and the CLI ``write`` FILE door): credential-material
    names refused (``secret_file_forbidden``), symlinked or non-regular
    sources refused (``symlink_forbidden`` / ``not_a_file``), unreadable
    sources refused (``not_readable``). Never follows a swap-able alias;
    content itself is never scanned for secrets (C03 rule 6).
    """
    name = source.name
    if _is_secret_name(name):
        raise DocumentStoreError(
            DocumentStoreErrorCode.SECRET_FILE_FORBIDDEN,
            f"refusing to read credential-material file name {name!r} "
            "into the canonical document store",
        )
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise DocumentStoreError(
            DocumentStoreErrorCode.NOT_A_FILE,
            f"outside source {source} does not exist",
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise DocumentStoreError(
            DocumentStoreErrorCode.SYMLINK_FORBIDDEN,
            f"outside source {source} is a symbolic link; refuse rather "
            "than follow a swap-able alias",
        )
    if not stat.S_ISREG(info.st_mode):
        raise DocumentStoreError(
            DocumentStoreErrorCode.NOT_A_FILE,
            f"outside source {source} is not a regular file",
        )
    try:
        return source.read_bytes()
    except PermissionError:
        raise DocumentStoreError(
            DocumentStoreErrorCode.NOT_READABLE,
            f"outside source {source} is not readable",
        ) from None


class DocumentStore:
    """Confined, revision-checked, atomic canonical document storage.

    Owns the file half of the C07 rule-1 source record; the SQL projection
    (``07-vector-schema``) owns the database half and reconciles from the
    durable reindex intents emitted here.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        """Resolved data root (C02 rule 1: explicitly configured entry point)."""
        return self._root

    # -- public operations -------------------------------------------------

    def read(
        self,
        doc_id: uuid.UUID,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace = DocumentNamespace.DOCS,
    ) -> StoredDocument:
        """Return the stored document, hash-verified against its metadata."""
        doc_id, server_id, app_id, namespace = self._validated_identity(
            doc_id, server_id=server_id, app_id=app_id, namespace=namespace
        )
        target, meta_path, intent_path = self._paths(
            server_id, app_id, namespace, doc_id
        )
        if not target.exists():
            raise DocumentStoreError(
                DocumentStoreErrorCode.NOT_FOUND,
                "no canonical document stored for this document id",
            )
        meta = self._load_meta(doc_id, server_id, app_id, namespace, meta_path)
        last_intent = self._read_intent(intent_path)
        if last_intent is not None and last_intent.tombstone:
            # C02 W4: a canonical file with a live tombstone is a surfaced
            # conflict, never a silent read or overwrite.
            raise DocumentStoreError(
                DocumentStoreErrorCode.TOMBSTONE_CONFLICT,
                "canonical content exists under a live tombstone intent; "
                "an operator must resolve the conflict before reading",
            )
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise DocumentStoreError(
                DocumentStoreErrorCode.NOT_FOUND,
                "no canonical document stored for this document id",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise DocumentStoreError(
                DocumentStoreErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery must "
                "reconcile from the file hash",
            )
        return StoredDocument(
            doc_id=doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            text=data.decode("utf-8"),
            revision=meta["revision"],
            sha256=digest,
            source_kind=SourceKind(meta["source_kind"]),
            origin=meta["origin"],
            origin_revision=meta["origin_revision"],
            verification=VerificationStatus(meta["verification"]),
        )

    def write(
        self,
        doc_id: uuid.UUID,
        text: str,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace = DocumentNamespace.DOCS,
        expected_revision: int,
        source_kind: SourceKind = SourceKind.AUTHORED,
        origin: str = "operator",
        origin_revision: str | None = None,
        verification: VerificationStatus = VerificationStatus.UNVERIFIED,
    ) -> StoredDocument:
        """Atomically commit one canonical Markdown document.

        ``expected_revision`` is the revision the caller last observed
        (``0`` to create). On success the revision is ``expected + 1`` and a
        durable ``upsert`` reindex intent has been emitted after the
        file+metadata pair. A concurrent/stale caller gets
        ``stale_revision``; a canonical file reappearing under a live
        tombstone is ``tombstone_conflict`` (C02 W4), never a silent
        overwrite.
        """
        doc_id, server_id, app_id, namespace = self._validated_identity(
            doc_id, server_id=server_id, app_id=app_id, namespace=namespace
        )
        self._validated_expected_revision(expected_revision)
        source_kind = self._validated_enum(source_kind, SourceKind, "source_kind")
        verification = self._validated_enum(
            verification, VerificationStatus, "verification"
        )
        origin, origin_revision = self._validated_provenance(origin, origin_revision)
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_DOCUMENT,
                "document is not valid UTF-8 text",
            ) from None
        checked_text = _checked_document(raw)
        data = raw
        target, meta_path, intent_path = self._paths(
            server_id, app_id, namespace, doc_id
        )
        current, last_intent = self._current_state(target, meta_path, intent_path)
        if last_intent is not None and last_intent.tombstone:
            # Recreate after a delete is allowed only from the exact
            # tombstone revision; anything else is a stale caller (C02 W4:
            # a resurrected file with a live tombstone is a surfaced
            # conflict, never a silent overwrite).
            if expected_revision != last_intent.revision:
                raise DocumentStoreError(
                    DocumentStoreErrorCode.STALE_REVISION,
                    f"expected revision {expected_revision}, but revision "
                    f"{last_intent.revision} was deleted (tombstone pending)",
                )
        elif current != expected_revision:
            raise DocumentStoreError(
                DocumentStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_scope_dirs(target)
        self._remove_stale_temps(target.parent, doc_id)
        digest = hashlib.sha256(data).hexdigest()
        revision = max(current, last_intent.revision if last_intent else 0) + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target, data)
        # C02 W2: canonical first, metadata second — the file is authoritative.
        self._atomic_write(
            meta_path,
            self._meta_bytes(
                doc_id,
                server_id,
                app_id,
                namespace,
                revision,
                digest,
                source_kind,
                origin,
                origin_revision,
                verification,
            ),
        )
        # Durable reindex intent, last: a crash above leaves the previous
        # intent, which rebuild-from-source reconciliation resolves.
        self._write_intent(
            intent_path,
            doc_id,
            server_id,
            app_id,
            namespace,
            op="upsert",
            revision=revision,
            sha256=digest,
        )
        return StoredDocument(
            doc_id=doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            text=checked_text,
            revision=revision,
            sha256=digest,
            source_kind=source_kind,
            origin=origin,
            origin_revision=origin_revision,
            verification=verification,
        )

    def import_document(
        self,
        source_path: str | os.PathLike[str],
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace = DocumentNamespace.DOCS,
        doc_id: uuid.UUID | None = None,
        expected_revision: int = ABSENT_REVISION,
        origin_revision: str | None = None,
        verification: VerificationStatus = VerificationStatus.UNVERIFIED,
    ) -> StoredDocument:
        """Explicitly import one outside Markdown file into the canonical store.

        One of the two outside-read seams (with the CLI ``write`` FILE
        door); both go through the same guarded read
        (:func:`_read_outside_source`): read-only, regular-file only,
        symlink refused, credential-material names refused
        (``secret_file_forbidden``), size/UTF-8 bounded. A rejected import
        materializes nothing — no canonical file, no metadata, no intent —
        so nothing indexable ever exists for it.
        """
        if doc_id is None:
            doc_id = uuid.uuid4()
        source = Path(source_path)
        data = _read_outside_source(source)
        text = _checked_document(data)
        return self.write(
            doc_id,
            text,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            expected_revision=expected_revision,
            source_kind=SourceKind.IMPORTED,
            origin=f"import:{source.resolve(strict=False)}",
            origin_revision=origin_revision,
            verification=verification,
        )

    def delete(
        self,
        doc_id: uuid.UUID,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace = DocumentNamespace.DOCS,
        expected_revision: int,
    ) -> ReindexIntent:
        """Revision-checked removal with a durable tombstone reindex intent.

        C02 W4: the tombstone intent is made durable *first*; only then are
        the canonical file and metadata removed. The intent stays until the
        indexer acknowledges with :meth:`purge_intent` (purge after the
        projection confirms).
        """
        doc_id, server_id, app_id, namespace = self._validated_identity(
            doc_id, server_id=server_id, app_id=app_id, namespace=namespace
        )
        self._validated_expected_revision(expected_revision)
        target, meta_path, intent_path = self._paths(
            server_id, app_id, namespace, doc_id
        )
        current, tombstone = self._current_state(target, meta_path, intent_path)
        if current == ABSENT_REVISION:
            raise DocumentStoreError(
                DocumentStoreErrorCode.NOT_FOUND,
                "nothing stored for this document id",
            )
        if current != expected_revision:
            raise DocumentStoreError(
                DocumentStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        # Tombstone first (durable), purge of file+metadata second.
        self._write_intent(
            intent_path,
            doc_id,
            server_id,
            app_id,
            namespace,
            op="delete",
            revision=current,
            sha256=digest,
        )
        self._remove_stale_temps(target.parent, doc_id)
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        _fsync_directory(target.parent)
        return ReindexIntent(
            doc_id=doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            op="delete",
            revision=current,
            sha256=digest,
            tombstone=True,
        )

    def read_intents(
        self,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace | None = None,
    ) -> list[ReindexIntent]:
        """Return the durable reindex outbox of one scope (all namespaces)."""
        if namespace is not None and not isinstance(namespace, DocumentNamespace):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_NAMESPACE,
                "namespace must be a DocumentNamespace member or None",
            )
        _, _, scope_rel = self._scope(server_id=server_id, app_id=app_id)
        scope_dir = self._confined(scope_rel)
        intents: list[ReindexIntent] = []
        if not scope_dir.is_dir():
            return intents
        namespaces = (
            (namespace,)
            if namespace is not None
            else (DocumentNamespace.DOCS, DocumentNamespace.RUNBOOKS)
        )
        for ns in namespaces:
            intents_dir = scope_dir / ns.value / INTENTS_DIR
            if not intents_dir.is_dir():
                continue
            for path in sorted(intents_dir.glob("*.json")):
                intents.append(self._load_intent(path))
        intents.sort(key=lambda item: (item.revision, str(item.doc_id)))
        return intents

    def purge_intent(
        self,
        doc_id: uuid.UUID,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace = DocumentNamespace.DOCS,
    ) -> None:
        """Indexer acknowledgment: remove one durable intent after reconcile.

        Owned here so the outbox lifecycle (emit → reconcile → purge) is
        confined to the same store that emits; C02 W4 bounds purge to
        "after the projection confirms", which is the caller's duty.
        """
        doc_id, server_id, app_id, namespace = self._validated_identity(
            doc_id, server_id=server_id, app_id=app_id, namespace=namespace
        )
        _, _, intent_path = self._paths(server_id, app_id, namespace, doc_id)
        intent_path.unlink(missing_ok=True)
        _fsync_directory(intent_path.parent)

    # -- confinement and state helpers ------------------------------------

    def _validated_identity(
        self,
        doc_id: object,
        *,
        server_id: object,
        app_id: object,
        namespace: object,
    ) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None, DocumentNamespace]:
        if not isinstance(doc_id, uuid.UUID):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_DOC_ID,
                "doc_id must be a uuid.UUID instance",
            )
        server_id, app_id = self._validated_scope(server_id, app_id)
        if not isinstance(namespace, DocumentNamespace):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_NAMESPACE,
                "namespace must be a DocumentNamespace member",
            )
        return doc_id, server_id, app_id, namespace

    @staticmethod
    def _validated_expected_revision(expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_REVISION,
                "expected_revision must be a non-negative int",
            )

    @staticmethod
    def _validated_enum(value: object, enum: type[StrEnum], field: str) -> StrEnum:
        if isinstance(value, enum):
            return value
        try:
            return enum(value)
        except ValueError:
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_PROVENANCE,
                f"{field} must be one of {sorted(member.value for member in enum)}",
            ) from None

    @staticmethod
    def _validated_provenance(
        origin: str, origin_revision: str | None
    ) -> tuple[str, str | None]:
        if (
            not isinstance(origin, str)
            or not origin
            or len(origin.encode("utf-8")) > MAX_ORIGIN_BYTES
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_PROVENANCE,
                f"origin must be a non-empty string of at most "
                f"{MAX_ORIGIN_BYTES} UTF-8 bytes",
            )
        if origin_revision is not None and (
            not isinstance(origin_revision, str)
            or not origin_revision
            or len(origin_revision) > MAX_ORIGIN_REVISION_CHARS
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_PROVENANCE,
                f"origin_revision must be a non-empty string of at most "
                f"{MAX_ORIGIN_REVISION_CHARS} characters or None",
            )
        return origin, origin_revision

    def _validated_scope(
        self,
        server_id: object,
        app_id: object,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        if server_id is not None and not isinstance(server_id, uuid.UUID):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INVALID_SERVER_ID,
                "server_id must be a uuid.UUID instance or None (shared)",
            )
        if app_id is not None:
            if server_id is None:
                raise DocumentStoreError(
                    DocumentStoreErrorCode.APP_REQUIRES_SERVER,
                    "app_id is accepted only together with a server_id",
                )
            if not isinstance(app_id, uuid.UUID):
                raise DocumentStoreError(
                    DocumentStoreErrorCode.INVALID_APP_ID,
                    "app_id must be a uuid.UUID instance",
                )
        return server_id, app_id

    def _scope(
        self,
        *,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, str]:
        """Scope identity + its relative directory (shared when server is None)."""
        server_id, app_id = self._validated_scope(server_id, app_id)
        if server_id is None:
            return None, None, SHARED_DIR
        if app_id is not None:
            return server_id, app_id, (
                f"{SERVERS_DIR}/{server_id}/{APPLICATIONS_DIR}/{app_id}"
            )
        return server_id, None, f"{SERVERS_DIR}/{server_id}"

    def _paths(
        self,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
        namespace: DocumentNamespace,
        doc_id: uuid.UUID,
    ) -> tuple[Path, Path, Path]:
        """Confined (canonical, metadata, intent) paths for one document."""
        _, _, scope_rel = self._scope(server_id=server_id, app_id=app_id)
        collection = f"{scope_rel}/{namespace.value}"
        base = f"{collection}/{doc_id}"
        return (
            self._confined(base + DOC_SUFFIX),
            self._confined(base + META_SUFFIX),
            self._confined(f"{collection}/{INTENTS_DIR}/{doc_id}.json"),
        )

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural (UUID + fixed names)."""
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise DocumentStoreError(
                    DocumentStoreErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise DocumentStoreError(
                DocumentStoreErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    def _current_state(
        self, target: Path, meta_path: Path, intent_path: Path
    ) -> tuple[int, ReindexIntent | None]:
        """Pairing-aware current revision plus the live intent, if any."""
        has_file = target.exists()
        has_meta = meta_path.exists()
        last_intent = self._read_intent(intent_path)
        if not has_file and not has_meta:
            return ABSENT_REVISION, last_intent
        if has_file and not has_meta:
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_MISSING,
                "canonical document exists without revision metadata; "
                "recovery must reconcile before further writes",
            )
        meta = self._load_meta_from(meta_path)
        if not has_file:
            raise DocumentStoreError(
                DocumentStoreErrorCode.ORPHAN_METADATA,
                "revision metadata exists without a canonical document; "
                "complete the delete before writing",
            )
        if last_intent is not None and last_intent.tombstone:
            raise DocumentStoreError(
                DocumentStoreErrorCode.TOMBSTONE_CONFLICT,
                "canonical content exists under a live tombstone intent; "
                "an operator must resolve the conflict before writing",
            )
        return int(meta["revision"]), last_intent

    def _load_meta(
        self,
        doc_id: uuid.UUID,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
        namespace: DocumentNamespace,
        meta_path: Path,
    ) -> dict[str, object]:
        meta = self._load_meta_from(meta_path)
        if (
            meta.get("doc_id") != str(doc_id)
            or meta.get("server_id") != (str(server_id) if server_id else None)
            or meta.get("app_id") != (str(app_id) if app_id else None)
            or meta.get("namespace") != namespace.value
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_INVALID,
                "revision metadata identity does not match the requested scope",
            )
        return meta

    def _load_meta_from(self, meta_path: Path) -> dict[str, object]:
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_MISSING,
                "canonical document exists without revision metadata; "
                "recovery must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        if not isinstance(meta, dict):
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        valid_source = any(
            meta.get("source_kind") == member.value for member in SourceKind
        )
        valid_verification = any(
            meta.get("verification") == member.value for member in VerificationStatus
        )
        if (
            meta.get("format") != META_FORMAT
            or not _is_positive_int(meta.get("revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
            or not isinstance(meta.get("origin"), str)
            or not valid_source
            or not valid_verification
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        origin_revision = meta.get("origin_revision")
        if origin_revision is not None and not isinstance(origin_revision, str):
            raise DocumentStoreError(
                DocumentStoreErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        return meta

    def _read_intent(self, intent_path: Path) -> ReindexIntent | None:
        if not intent_path.exists():
            return None
        return self._load_intent(intent_path)

    def _load_intent(self, path: Path) -> ReindexIntent:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} is not a valid JSON document",
            ) from None
        if not isinstance(payload, dict) or payload.get("format") != INTENT_FORMAT:
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} does not match the expected shape",
            )
        try:
            doc_id = uuid.UUID(str(payload.get("doc_id")))
        except (ValueError, AttributeError, TypeError):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} carries an invalid doc_id",
            ) from None
        server_raw = payload.get("server_id")
        app_raw = payload.get("app_id")
        namespace_raw = payload.get("namespace")
        op = payload.get("op")
        revision = payload.get("revision")
        sha256 = payload.get("sha256")
        tombstone = payload.get("tombstone")
        if (
            (server_raw is not None and not isinstance(server_raw, str))
            or (app_raw is not None and not isinstance(app_raw, str))
            or namespace_raw not in {member.value for member in DocumentNamespace}
            or op not in {"upsert", "delete"}
            or not _is_positive_int(revision)
            or not isinstance(tombstone, bool)
            or tombstone != (op == "delete")
        ):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} does not match the expected shape",
            )
        try:
            server_id = uuid.UUID(server_raw) if server_raw is not None else None
            app_id = uuid.UUID(app_raw) if app_raw is not None else None
        except ValueError:
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} carries an invalid scope id",
            ) from None
        if sha256 is not None and not _is_canonical_sha256(sha256):
            raise DocumentStoreError(
                DocumentStoreErrorCode.INTENT_INVALID,
                f"reindex intent {path.name} carries an invalid sha256",
            )
        return ReindexIntent(
            doc_id=doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=DocumentNamespace(namespace_raw),
            op=op,
            revision=revision,
            sha256=sha256,
            tombstone=tombstone,
        )

    def _ensure_scope_dirs(self, target: Path) -> None:
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / INTENTS_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
        current = directory
        while current != self._root and current.is_relative_to(self._root):
            os.chmod(current, 0o700)
            current = current.parent

    @staticmethod
    def _meta_bytes(
        doc_id: uuid.UUID,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
        namespace: DocumentNamespace,
        revision: int,
        digest: str,
        source_kind: SourceKind,
        origin: str,
        origin_revision: str | None,
        verification: VerificationStatus,
    ) -> bytes:
        payload = {
            "format": META_FORMAT,
            "doc_id": str(doc_id),
            "server_id": str(server_id) if server_id else None,
            "app_id": str(app_id) if app_id else None,
            "namespace": namespace.value,
            "revision": revision,
            "sha256": digest,
            "source_kind": source_kind.value,
            "origin": origin,
            "origin_revision": origin_revision,
            "verification": verification.value,
        }
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    def _write_intent(
        self,
        intent_path: Path,
        doc_id: uuid.UUID,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
        namespace: DocumentNamespace,
        *,
        op: str,
        revision: int,
        sha256: str | None,
    ) -> None:
        intent_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(intent_path.parent, 0o700)
        payload = {
            "format": INTENT_FORMAT,
            "doc_id": str(doc_id),
            "server_id": str(server_id) if server_id else None,
            "app_id": str(app_id) if app_id else None,
            "namespace": namespace.value,
            "op": op,
            "revision": revision,
            "sha256": sha256,
            "tombstone": op == "delete",
        }
        self._atomic_write(
            intent_path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        )

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
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(directory)

    @staticmethod
    def _remove_stale_temps(directory: Path, doc_id: uuid.UUID) -> None:
        """Best-effort cleanup of this store's crash-orphaned temp files."""
        if not directory.is_dir():
            return
        prefixes = (
            f"{doc_id}{DOC_SUFFIX}.",
            f"{doc_id}{META_SUFFIX}.",
            f"{doc_id}.json.",
        )
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _meta_view(stored: StoredDocument) -> dict[str, object]:
    """Value-free JSON view of one stored document (never the content)."""
    return {
        "doc_id": str(stored.doc_id),
        "server_id": str(stored.server_id) if stored.server_id else None,
        "app_id": str(stored.app_id) if stored.app_id else None,
        "namespace": stored.namespace.value,
        "revision": stored.revision,
        "sha256": stored.sha256,
        "source_kind": stored.source_kind.value,
        "origin": stored.origin,
        "origin_revision": stored.origin_revision,
        "verification": stored.verification.value,
    }


def _intent_view(intent: ReindexIntent) -> dict[str, object]:
    return {
        "doc_id": str(intent.doc_id),
        "server_id": str(intent.server_id) if intent.server_id else None,
        "app_id": str(intent.app_id) if intent.app_id else None,
        "namespace": intent.namespace.value,
        "op": intent.op,
        "revision": intent.revision,
        "sha256": intent.sha256,
        "tombstone": intent.tombstone,
    }


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", metavar="UUID", help="server scope id")
    group.add_argument(
        "--shared",
        action="store_true",
        help="explicit shared namespace (C07 rule 1); default when no scope given",
    )
    parser.add_argument("--app", metavar="UUID", help="application scope id")
    parser.add_argument(
        "--namespace",
        choices=[member.value for member in DocumentNamespace],
        default=DocumentNamespace.DOCS.value,
        help="canonical collection (default: docs)",
    )


def _parse_scope(args: argparse.Namespace) -> tuple[uuid.UUID | None, uuid.UUID | None, DocumentNamespace]:
    server_id = uuid.UUID(args.server) if args.server else None
    app_id = uuid.UUID(args.app) if args.app else None
    namespace = DocumentNamespace(args.namespace)
    return server_id, app_id, namespace


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.knowledge.documents",
        description="Confined atomic canonical Markdown document store "
        "(metadata-only output; document content is never printed)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("root", metavar="ROOT", help="explicit data root")
        _add_scope_args(target)

    read = sub.add_parser("read", help="read one document (metadata only)")
    add_common(read)
    read.add_argument("doc_id", metavar="DOC_ID")

    write = sub.add_parser("write", help="atomically write one document")
    add_common(write)
    write.add_argument("doc_id", metavar="DOC_ID")
    write.add_argument(
        "file",
        metavar="FILE",
        help="UTF-8 Markdown source file outside the data root; the "
        "secret-name deny-list and symlink refusal apply exactly as for "
        "import",
    )
    write.add_argument("--expect-revision", type=int, required=True)
    write.add_argument(
        "--source-kind",
        choices=[member.value for member in SourceKind],
        default=SourceKind.AUTHORED.value,
    )
    write.add_argument("--origin", default="operator")
    write.add_argument("--origin-revision", default=None)
    write.add_argument(
        "--verification",
        choices=[member.value for member in VerificationStatus],
        default=VerificationStatus.UNVERIFIED.value,
    )

    imp = sub.add_parser(
        "import", help="explicitly import one outside Markdown file"
    )
    add_common(imp)
    imp.add_argument("--doc-id", metavar="DOC_ID", default=None)
    imp.add_argument("--source", metavar="PATH", required=True)
    imp.add_argument("--expect-revision", type=int, default=ABSENT_REVISION)
    imp.add_argument("--origin-revision", default=None)
    imp.add_argument(
        "--verification",
        choices=[member.value for member in VerificationStatus],
        default=VerificationStatus.UNVERIFIED.value,
    )

    delete = sub.add_parser("delete", help="delete with a durable tombstone intent")
    add_common(delete)
    delete.add_argument("doc_id", metavar="DOC_ID")
    delete.add_argument("--expect-revision", type=int, required=True)

    intents = sub.add_parser("intents", help="list the durable reindex outbox")
    add_common(intents)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 success / 1 structured rejection / 2 usage or IO."""
    args = _build_parser().parse_args(argv)
    try:
        store = DocumentStore(args.root)
        server_id, app_id, namespace = _parse_scope(args)
        if args.command == "read":
            stored = store.read(
                uuid.UUID(args.doc_id),
                server_id=server_id,
                app_id=app_id,
                namespace=namespace,
            )
            print(json.dumps(_meta_view(stored), sort_keys=True))
        elif args.command == "write":
            # Same guarded outside read as import_document: the FILE
            # argument is a caller-supplied path outside the data root, so
            # the secret-name deny-list and symlink refusal apply here too.
            text = _checked_document(_read_outside_source(Path(args.file)))
            stored = store.write(
                uuid.UUID(args.doc_id),
                text,
                server_id=server_id,
                app_id=app_id,
                namespace=namespace,
                expected_revision=args.expect_revision,
                source_kind=SourceKind(args.source_kind),
                origin=args.origin,
                origin_revision=args.origin_revision,
                verification=VerificationStatus(args.verification),
            )
            print(json.dumps(_meta_view(stored), sort_keys=True))
        elif args.command == "import":
            stored = store.import_document(
                args.source,
                server_id=server_id,
                app_id=app_id,
                namespace=namespace,
                doc_id=uuid.UUID(args.doc_id) if args.doc_id else None,
                expected_revision=args.expect_revision,
                origin_revision=args.origin_revision,
                verification=VerificationStatus(args.verification),
            )
            print(json.dumps(_meta_view(stored), sort_keys=True))
        elif args.command == "delete":
            intent = store.delete(
                uuid.UUID(args.doc_id),
                server_id=server_id,
                app_id=app_id,
                namespace=namespace,
                expected_revision=args.expect_revision,
            )
            print(json.dumps(_intent_view(intent), sort_keys=True))
        elif args.command == "intents":
            for intent in store.read_intents(server_id=server_id, app_id=app_id):
                print(json.dumps(_intent_view(intent), sort_keys=True))
    except DocumentStoreError as error:
        print(
            json.dumps({"error": error.code.value, "detail": error.detail}),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as error:
        print(
            json.dumps({"error": "usage_or_io", "detail": str(error)}),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
