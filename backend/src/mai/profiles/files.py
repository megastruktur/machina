"""Confined atomic canonical file storage for server profiles.

Card ``02-file-store`` consumer of the frozen schema producer
``mai.contracts.server`` (integrated slice ``02-profile-schema``), against
contracts.md **C02**:

- **C02 rule 1** — the data root is configured explicitly by the caller
  (default controller-host ``~/.mai/`` stays an operator decision, never an
  ambient inference); the store resolves the root once at construction and
  creates it with restrictive permissions (dirs ``0o700``, files ``0o600``).
  No other filesystem location is ever touched: every path is derived
  structurally from an opaque UUID plus fixed file names.
- **C02 rules 2–3** — canonical path scheme ``servers/<server_id>/profile.yaml``
  comes verbatim from :func:`mai.contracts.server.canonical_relative_path`,
  which accepts only a :class:`uuid.UUID`, so traversal and absolute
  user-supplied paths are unrepresentable by construction (same pattern as the
  schema card). Symlink escape/aliasing is rejected at run time: any
  symbolic link at *any* component of the canonical or metadata path aborts the
  operation with ``path_escape`` (this includes a symlinked ``servers`` dir and
  a symlinked canonical file; a canonical-file symlink pointing inside the root
  is equally rejected — the alias could be swapped later). Reads additionally
  open the canonical file with ``O_NOFOLLOW``.
- **C02 rule 4** — canonical version triple: integer content ``revision``
  (starts at 1; revision ``0`` denotes "absent"), ``schema_revision`` (pinned
  by the frozen producer, ``SCHEMA_VERSION``) and SHA-256 ``content hash``.
  The triple lives in the sidecar ``servers/<server_id>/profile.meta.json``
  next to the canonical file. The Postgres projection (card
  ``02-profile-projection``) stores the same revision; this module owns only
  the file half.
- **C02 rule 5 / crash windows** — atomic replace is temp file + ``fsync`` +
  ``os.replace`` + directory ``fsync``; a crash before the canonical replace
  leaves the previous valid content and revision fully intact (window W1
  file-half) and stale temp files are cleaned by the next successful write.
  The canonical file is replaced *before* its metadata: the file is
  authoritative (window W2), so the intermediate state (new file, lagging
  metadata) is detected by :meth:`ProfileFileStore.read` as ``hash_mismatch``
  and reported — never silently reconciled ("no invented successful sync");
  journal/recovery reconciliation is owned by ``02-profile-projection``.
  Deletion removes the canonical file first, then the metadata; a crash
  between the two leaves detectable orphan metadata that a follow-up
  :meth:`ProfileFileStore.delete` with the same expected revision completes.
  Windows W3/W4 (projection transactionality, tombstone/purge) belong to the
  projection card.

Additional binding rules consumed here:

- **C01 rule 1** — identity is the opaque UUID only; a document whose embedded
  ``server_id`` differs from the storage identity is rejected
  (``server_id_mismatch``) so the canonical path can never disagree with the
  stored identity that the projection keys on.
- **C02 rule 2 / producer contract** — ``write`` validates the document with
  :func:`mai.contracts.server.parse_server_profile_text` *before* any
  filesystem effect, so the canonical store can never hold a schema-invalid
  profile; :class:`~mai.contracts.server.ProfileValidationError` propagates
  unchanged (the API card maps it 1:1 onto the C01 envelope).
- **Resource-exhaustion boundary (owned here per reviewed sibling evidence
  ``mai_evidence/02-profile-schema/01-commands-and-results.md`` §5 item 2:
  "canonical-file IO hardening belongs to ``02-file-store``; same parsing
  call, single consumer")** — before the parse call, ``write`` enforces a
  byte cap (:data:`MAX_DOCUMENT_BYTES`) and refuses YAML alias references via
  a construction-free :func:`yaml.compose` node-identity walk (AST-level, not
  substring guessing; composed aliases reuse the anchor's node object), so
  anchor/alias documents and pathologically nested documents are rejected
  structurally (``document_too_large`` / ``document_alias_forbidden`` /
  ``document_too_deep``) instead of relying on loader memoization behavior.
  Quoted ``&``/``*`` document content is unaffected.
- **C05 rule 6** — mutation lock keys include ``server_id``; writers are
  serialized per server by the engine before reaching this store. The
  expected-revision check is the stale-caller guard for read-modify-write
  across API calls, not a replacement for that lock.

Error discipline: every rejection is a :class:`ProfileFileError` carrying a
stable ``code`` (C01 envelope vocabulary style) and a value-free detail —
profile content is never echoed (secret non-disclosure, test policy). The
module reads no process environment, network or clock; results derive from
explicit arguments and filesystem state only.

POSIX assumption: ``O_NOFOLLOW`` and directory ``fsync`` are Linux/macOS
facilities; Windows targets are a frozen non-goal (C02, runtime ADR).

Production entrypoint (exit contract 0 success / 1 structured rejection /
2 usage or IO error)::

    uv run python -m mai.profiles.files read ROOT SERVER_ID
    uv run python -m mai.profiles.files write ROOT SERVER_ID FILE --expect-revision N
    uv run python -m mai.profiles.files delete ROOT SERVER_ID --expect-revision N

``read``/``write`` print one JSON line with the version triple (never the
document content) so transcripts stay value-free.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode

from mai.contracts.server import (
    SCHEMA_VERSION,
    ProfileValidationError,
    canonical_relative_path,
    parse_server_profile_text,
)

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "META_FILENAME",
    "META_FORMAT",
    "PROFILE_FILENAME",
    "FileStoreErrorCode",
    "ProfileFileError",
    "ProfileFileStore",
    "StoredProfile",
    "canonical_meta_relative_path",
    "main",
]

PROFILE_FILENAME = "profile.yaml"
META_FILENAME = "profile.meta.json"
META_FORMAT = 1
SERVERS_DIR = "servers"

#: Byte cap for one canonical document, enforced before parsing. A profile is
#: a small admin-controlled document; 64 KiB is two orders of magnitude above
#: any legitimate fixture and bounds parser work (resource-exhaustion
#: boundary owned by this card, sibling schema evidence §5 item 2).
MAX_DOCUMENT_BYTES = 64 * 1024

#: Revision convention for "no canonical file stored" (create baseline).
ABSENT_REVISION = 0

_HEX_DIGITS = frozenset("0123456789abcdef")


class FileStoreErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_REVISION = "invalid_revision"
    SERVER_ID_MISMATCH = "server_id_mismatch"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DOCUMENT_ALIAS_FORBIDDEN = "document_alias_forbidden"
    PATH_ESCAPE = "path_escape"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    ORPHAN_METADATA = "orphan_metadata"
    HASH_MISMATCH = "hash_mismatch"


class ProfileFileError(ValueError):
    """One structured, value-free file-store rejection."""

    def __init__(self, code: FileStoreErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class StoredProfile:
    """Canonical profile as stored: content plus the C02 rule-4 version triple."""

    server_id: uuid.UUID
    text: str
    revision: int
    schema_revision: int
    sha256: str


def canonical_meta_relative_path(server_id: uuid.UUID) -> str:
    """Sidecar metadata path (C02 rule 4) below the approved data root.

    Like the canonical path, this accepts only a :class:`uuid.UUID`, so the
    relative path is traversal-proof by construction.
    """
    return f"{SERVERS_DIR}/{server_id}/{META_FILENAME}"


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


def _contains_alias(root: Node) -> bool:
    """Detect YAML alias references before any construction.

    Composed aliases reuse the anchor's node object (measured on the pinned
    pyyaml), so node-identity reuse in the composed tree proves an alias
    reference; literally duplicated subtrees are distinct objects and stay
    legal. Iterative walk — no recursion on hostile documents.
    """
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
    """Encode and harden one candidate canonical document before parsing.

    Byte cap first, then a construction-free :func:`yaml.compose` walk
    refusing alias references (billion-laughs hardening owned by this card).
    Unparsable YAML defers to the schema producer's stable
    ``unparsable_document`` rejection; the returned bytes are the exact
    canonical representation hashed and stored on success.
    """
    data = text.encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ProfileFileError(
            FileStoreErrorCode.DOCUMENT_TOO_LARGE,
            f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return data
    except RecursionError:
        raise ProfileFileError(
            FileStoreErrorCode.DOCUMENT_TOO_DEEP,
            "document nesting exceeds the supported depth",
        ) from None
    if node is not None and _contains_alias(node):
        raise ProfileFileError(
            FileStoreErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
            "document contains YAML alias nodes; anchor/alias expansion is "
            "not permitted in canonical files",
        )
    return data


class ProfileFileStore:
    """Confined, revision-checked, atomic canonical profile storage (C02).

    Single-writer per ``server_id`` is a caller invariant (engine locks,
    C05 rule 6); the store guarantees atomic visibility of each committed
    file pair member, stale-caller rejection and root confinement.
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

    def read(self, server_id: uuid.UUID) -> StoredProfile:
        """Return the stored canonical profile, hash-verified against metadata."""
        server_id = self._validated_server_id(server_id)
        target = self._confined(canonical_relative_path(server_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id))
        if not target.exists():
            raise ProfileFileError(
                FileStoreErrorCode.NOT_FOUND,
                "no canonical profile stored for this server id",
            )
        meta = self._load_meta(server_id, meta_path)
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise ProfileFileError(
                FileStoreErrorCode.NOT_FOUND,
                "no canonical profile stored for this server id",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise ProfileFileError(
                FileStoreErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery must "
                "reconcile from the file hash",
            )
        return StoredProfile(
            server_id=server_id,
            text=data.decode("utf-8"),
            revision=meta["revision"],
            schema_revision=meta["schema_revision"],
            sha256=digest,
        )

    def write(
        self, server_id: uuid.UUID, text: str, *, expected_revision: int
    ) -> StoredProfile:
        """Validate then atomically replace the canonical profile.

        ``expected_revision`` is the revision the caller last observed
        (``0`` to create). On success the revision is ``expected + 1``.
        A schema-invalid document raises
        :class:`~mai.contracts.server.ProfileValidationError` before any
        filesystem effect; a concurrent/stale caller gets ``stale_revision``.
        """
        server_id = self._validated_server_id(server_id)
        self._validated_expected_revision(expected_revision)
        data = _checked_document(text)
        try:
            profile = parse_server_profile_text(text)
        except RecursionError:
            raise ProfileFileError(
                FileStoreErrorCode.DOCUMENT_TOO_DEEP,
                "document nesting exceeds the supported depth",
            ) from None
        if profile.server_id != server_id:
            raise ProfileFileError(
                FileStoreErrorCode.SERVER_ID_MISMATCH,
                "embedded server_id does not match the storage identity",
            )
        target = self._confined(canonical_relative_path(server_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id))
        current = self._current_revision(
            server_id, target, meta_path, tolerate_orphan=False
        )
        if current != expected_revision:
            raise ProfileFileError(
                FileStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_server_dir(target.parent)
        self._remove_stale_temps(target.parent)
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target, data)
        # C02 W2: canonical first, metadata second — the file is authoritative.
        # A crash in between leaves the new file with lagging metadata, which
        # read() reports as hash_mismatch; reconciliation is the projection
        # card's journal duty, never invented here.
        self._atomic_write(meta_path, self._meta_bytes(server_id, revision, digest))
        return StoredProfile(
            server_id=server_id,
            text=text,
            revision=revision,
            schema_revision=SCHEMA_VERSION,
            sha256=digest,
        )

    def delete(self, server_id: uuid.UUID, *, expected_revision: int) -> None:
        """Revision-checked removal: canonical file first, metadata second.

        Completing a half-deleted state (metadata without canonical file,
        C02 delete crash window) is done by repeating the delete with the
        metadata's revision.
        """
        server_id = self._validated_server_id(server_id)
        self._validated_expected_revision(expected_revision)
        target = self._confined(canonical_relative_path(server_id))
        meta_path = self._confined(canonical_meta_relative_path(server_id))
        current = self._current_revision(
            server_id, target, meta_path, tolerate_orphan=True
        )
        if current == ABSENT_REVISION:
            raise ProfileFileError(
                FileStoreErrorCode.NOT_FOUND,
                "nothing stored for this server id",
            )
        if current != expected_revision:
            raise ProfileFileError(
                FileStoreErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._remove_stale_temps(target.parent)
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        _fsync_directory(target.parent)

    # -- confinement and state helpers ------------------------------------

    def _validated_server_id(self, server_id: object) -> uuid.UUID:
        if not isinstance(server_id, uuid.UUID):
            raise ProfileFileError(
                FileStoreErrorCode.INVALID_SERVER_ID,
                "server_id must be a uuid.UUID instance",
            )
        return server_id

    @staticmethod
    def _validated_expected_revision(expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ProfileFileError(
                FileStoreErrorCode.INVALID_REVISION,
                "expected_revision must be a non-negative int",
            )

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural (UUID + fixed names)."""
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise ProfileFileError(
                    FileStoreErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise ProfileFileError(
                FileStoreErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    def _load_meta(self, server_id: uuid.UUID, meta_path: Path) -> dict[str, object]:
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            raise ProfileFileError(
                FileStoreErrorCode.METADATA_MISSING,
                "canonical profile exists without revision metadata; recovery "
                "must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProfileFileError(
                FileStoreErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        if (
            not isinstance(meta, dict)
            or meta.get("format") != META_FORMAT
            or meta.get("server_id") != str(server_id)
            or not _is_positive_int(meta.get("revision"))
            or not _is_positive_int(meta.get("schema_revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
        ):
            raise ProfileFileError(
                FileStoreErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        return meta

    def _current_revision(
        self,
        server_id: uuid.UUID,
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
            raise ProfileFileError(
                FileStoreErrorCode.METADATA_MISSING,
                "canonical profile exists without revision metadata; recovery "
                "must reconcile before further writes",
            )
        meta = self._load_meta(server_id, meta_path)
        if not has_file and not tolerate_orphan:
            raise ProfileFileError(
                FileStoreErrorCode.ORPHAN_METADATA,
                "revision metadata exists without a canonical profile; "
                "complete the delete before writing",
            )
        return int(meta["revision"])

    @staticmethod
    def _ensure_server_dir(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)

    @staticmethod
    def _meta_bytes(
        server_id: uuid.UUID, revision: int, digest: str
    ) -> bytes:
        payload = {
            "format": META_FORMAT,
            "server_id": str(server_id),
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
        _fsync_directory(directory)

    @staticmethod
    def _remove_stale_temps(directory: Path) -> None:
        """Best-effort cleanup of this store's crash-orphaned temp files."""
        if not directory.is_dir():
            return
        prefixes = (f"{PROFILE_FILENAME}.", f"{META_FILENAME}.")
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _meta_view(stored: StoredProfile) -> dict[str, object]:
    return {
        "server_id": str(stored.server_id),
        "revision": stored.revision,
        "schema_revision": stored.schema_revision,
        "sha256": stored.sha256,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.profiles.files",
        description=(
            "Confined atomic canonical profile file store (contracts.md C02). "
            "All output is value-free; document content is never printed."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("root", help="data root directory (created 0o700)")
        target.add_argument("server_id", help="opaque stable server UUID")

    read_command = subparsers.add_parser(
        "read", help="print the stored version triple as one JSON line"
    )
    add_common(read_command)

    write_command = subparsers.add_parser(
        "write", help="validate and atomically store a profile document"
    )
    add_common(write_command)
    write_command.add_argument("profile_file", help="path of the profile YAML to store")
    write_command.add_argument(
        "--expect-revision",
        type=int,
        required=True,
        help="revision the caller last observed (0 = create)",
    )

    delete_command = subparsers.add_parser(
        "delete", help="revision-checked removal of the canonical pair"
    )
    add_common(delete_command)
    delete_command.add_argument(
        "--expect-revision",
        type=int,
        required=True,
        help="revision the caller last observed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; exit 0 success / 1 structured rejection / 2 usage or IO."""
    args = _build_parser().parse_args(argv)
    try:
        server_id = uuid.UUID(args.server_id)
    except ValueError:
        print(
            json.dumps(
                {
                    "error": {
                        "code": FileStoreErrorCode.INVALID_SERVER_ID.value,
                        "message": "server_id is not a valid UUID",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1
    try:
        store = ProfileFileStore(args.root)
        if args.command == "read":
            print(json.dumps(_meta_view(store.read(server_id)), sort_keys=True))
        elif args.command == "write":
            text = Path(args.profile_file).read_text(encoding="utf-8")
            stored = store.write(server_id, text, expected_revision=args.expect_revision)
            print(json.dumps(_meta_view(stored), sort_keys=True))
        else:
            store.delete(server_id, expected_revision=args.expect_revision)
            print(json.dumps({"server_id": str(server_id), "deleted": True}, sort_keys=True))
        return 0
    except ProfileValidationError as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "profile_validation_failed",
                        "fields": exc.fields,
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1
    except ProfileFileError as exc:
        print(
            json.dumps(
                {"error": {"code": exc.code.value, "message": exc.detail}}
            ),
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"io error: {exc.strerror or exc.__class__.__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
