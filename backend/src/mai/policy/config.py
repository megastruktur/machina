"""Versioned canonical storage for the server grant configuration.

Card ``03-policy-config`` consumer of the reviewed producers
``mai.policy.evaluate`` (decision vocabulary, ``03-policy``) and the
``02-file-store`` storage discipline, against contracts.md **C02
rules 1–6 and C03 rules 3–4**:

- **C02 rule 1** — the data root is configured explicitly by the caller
  (never an ambient ``~/.mai`` inference); the store resolves it once at
  construction and creates it with restrictive permissions (dirs
  ``0o700``, files ``0o600`` by ``mkstemp`` default). No other
  filesystem location is ever touched: the config document lives at the
  module-fixed path ``policy/config.json`` with its sidecar
  ``policy/config.meta.json`` below the root — no caller-supplied path
  component exists in this API.
- **C02 rule 3** — confinement is structural (fixed names below the
  explicit root) and verified at run time: a symbolic link at *any*
  component of either path aborts the operation with ``path_escape``
  (same rule as the profile store, including a symlinked ``policy``
  directory); reads open the canonical file with ``O_NOFOLLOW``.
- **C02 rule 4** — canonical version triple: integer content
  ``revision`` (starts at 1; revision ``0`` denotes "no configuration
  stored"), ``schema_revision`` (frozen by this card,
  :data:`CONFIG_SCHEMA_VERSION`) and SHA-256 content hash in the
  sidecar metadata. The stored document **is** the policy revision:
  ``policy.revision`` inside the document must equal the file revision,
  so one counter serves the C03 rule-4 approval binding — every config
  change (grant add/revoke, deny-list change) is a new policy revision.
- **C02 rules 5–6 / C07 rule 5** — atomic replace is temp file +
  ``fsync`` + ``os.replace`` + directory ``fsync``; the canonical file
  is replaced *before* its metadata, so an interrupted commit leaves
  the new content with lagging metadata, which :meth:`GrantConfigStore.read`
  reports as ``hash_mismatch`` — never silently reconciled ("no
  invented successful sync"). There is no SQL projection of this
  document in P03 (windows W3/W4 do not apply here yet); a future
  projection card would own the journal/outbox halves explicitly.
  **Server prompts are a different namespace** (``servers/<id>/prompt.md``,
  owned by the profile cards): this store's write path accepts only the
  frozen document schema (exact field sets, validated capability slugs
  and server UUIDs via the ``03-policy`` producers) — generated prompt
  or prose content cannot be stored here, and nothing in this module
  can write a prompt file.

- **C03 rules 3–4 (AC mapping)** — the persisted document is exactly
  what :func:`mai.policy.evaluate.evaluate` consumes: the current
  scoped grant set (capability id → explicit server UUIDs, no
  wildcards — an empty scope means *nowhere*) and the current
  :class:`mai.policy.evaluate.Policy`. The store keeps **no cache**:
  every :meth:`GrantConfigStore.read` hits the filesystem, so a
  dispatcher can reload the current policy revision immediately before
  each execution and a revocation committed by any process takes
  effect on the next read.

Revision conflict handling (AC1): ``write`` takes the caller's last
observed ``expected_revision`` (``0`` to create). The caller states the
**next** revision explicitly inside the document
(``policy.revision == expected_revision + 1``) — the store never
invents or mutates a revision; a stale caller or a disagreeing document
is rejected (``stale_revision`` / ``revision_conflict``) before any
filesystem effect. Single-writer per data root remains a caller
invariant (engine locks, C05 rule 6); the expected-revision check is
the stale-caller guard, not a replacement for that lock.

Error discipline: every rejection is a :class:`GrantConfigError`
carrying a stable ``code`` and a value-free detail — server names,
credential material and document prose are never echoed. Producer
rejections from ``policy_from_json``/``grants_from_json`` are mapped
onto ``policy_invalid``/``grants_invalid`` with their value-free
details (they quote field names and slugs only). The module reads no
process environment, network or clock.

POSIX assumption: ``O_NOFOLLOW`` and directory ``fsync`` are
Linux/macOS facilities; Windows targets are a frozen non-goal (C02,
runtime ADR).

Production entrypoint (exit contract 0 success / 1 structured
rejection / 2 usage or IO error)::

    uv run python -m mai.policy.config read ROOT
    uv run python -m mai.policy.config write ROOT FILE --expect-revision N

``read``/``write`` print one JSON line: the version triple plus the
stored logical configuration (policy revision, denied capability slugs,
grants). Configuration ids are not secret material; error output stays
value-free.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mai.policy.evaluate import (
    Policy,
    PolicyError,
    grants_from_json,
    policy_from_json,
)

if TYPE_CHECKING:
    import uuid

__all__ = [
    "ABSENT_REVISION",
    "CONFIG_META_FILENAME",
    "CONFIG_META_FORMAT",
    "CONFIG_FILENAME",
    "CONFIG_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "POLICY_DIR",
    "GrantConfigError",
    "GrantConfigErrorCode",
    "GrantConfigStore",
    "StoredGrantConfig",
    "canonical_config_meta_relative_path",
    "canonical_config_relative_path",
    "main",
]

POLICY_DIR = "policy"
CONFIG_FILENAME = "config.json"
CONFIG_META_FILENAME = "config.meta.json"
CONFIG_META_FORMAT = 1

#: Schema revision of the stored document shape, frozen by this card
#: against C02 rule 4 (the ``02-policy-config`` producer).
CONFIG_SCHEMA_VERSION = 1

#: Byte cap for one canonical configuration document, enforced on the
#: canonical serialization before any filesystem effect. A grant
#: configuration is a small admin-controlled document; 64 KiB is two
#: orders of magnitude above any legitimate fixture (same boundary as
#: the profile store) and bounds parser work.
MAX_DOCUMENT_BYTES = 64 * 1024

#: Revision convention for "no configuration stored yet" (create baseline).
ABSENT_REVISION = 0

#: The exact frozen document vocabulary: policy + grants, nothing else.
#: An unknown field — including generated prompt/prose annotations — is
#: a rejection, never an ignored passenger (C02 rule 6 / C07 rule 5).
_DOCUMENT_FIELDS = frozenset({"policy", "grants"})


class GrantConfigErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_REVISION = "invalid_revision"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DOCUMENT_NOT_JSON = "document_not_json"
    DOCUMENT_SHAPE_INVALID = "document_shape_invalid"
    POLICY_INVALID = "policy_invalid"
    GRANTS_INVALID = "grants_invalid"
    PATH_ESCAPE = "path_escape"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    REVISION_CONFLICT = "revision_conflict"
    ORPHAN_METADATA = "orphan_metadata"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    HASH_MISMATCH = "hash_mismatch"


class GrantConfigError(ValueError):
    """One structured, value-free grant-config rejection."""

    def __init__(self, code: GrantConfigErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class StoredGrantConfig:
    """Canonical grant configuration as stored (C02 rule-4 triple + content).

    ``policy`` and ``grants`` are the validated, typed documents the
    policy evaluator consumes — ready to feed ``evaluate()`` without a
    second validation pass. ``revision`` is both the file revision and
    the ``policy.revision`` (one counter; C03 rule 4 approval binding).
    """

    revision: int
    schema_revision: int
    sha256: str
    policy: Policy
    grants: dict[str, frozenset[uuid.UUID]]

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON view (stable key order)."""
        return {
            "revision": self.revision,
            "schema_revision": self.schema_revision,
            "sha256": self.sha256,
            "policy": {
                "revision": self.policy.revision,
                "denied_capabilities": sorted(self.policy.denied_capabilities),
            },
            "grants": [
                {
                    "capability_id": capability_id,
                    "server_ids": sorted(str(s) for s in self.grants[capability_id]),
                }
                for capability_id in sorted(self.grants)
            ],
        }


def canonical_config_relative_path() -> str:
    """Module-fixed canonical path below the approved data root (C02 rule 3)."""
    return f"{POLICY_DIR}/{CONFIG_FILENAME}"


def canonical_config_meta_relative_path() -> str:
    """Module-fixed sidecar metadata path below the approved data root."""
    return f"{POLICY_DIR}/{CONFIG_META_FILENAME}"


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (rename durability)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_canonical_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= frozenset("0123456789abcdef")
    )


def _canonical_document(
    policy: Policy, grants: dict[str, frozenset[uuid.UUID]]
) -> dict[str, object]:
    """Deterministic canonical document (sorted keys and members).

    The canonical byte form is a function of the validated content
    alone — formatting, member order and duplicates cannot diverge the
    hash of two logically identical configurations.
    """
    return {
        "policy": {
            "revision": policy.revision,
            "denied_capabilities": sorted(policy.denied_capabilities),
        },
        "grants": [
            {
                "capability_id": capability_id,
                "server_ids": sorted(str(s) for s in grants[capability_id]),
            }
            for capability_id in sorted(grants)
        ],
    }


def _serialize(document: dict[str, object]) -> bytes:
    """Canonical bytes for one already-validated document (bounded).

    The canonical shape has fixed depth and validated leaf values, so
    the only resource bound that can fire is the byte cap.
    """
    data = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise GrantConfigError(
            GrantConfigErrorCode.DOCUMENT_TOO_LARGE,
            f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    return data


def _validated_document(document: object) -> tuple[Policy, dict[str, frozenset]]:
    """Validate one candidate document through the ``03-policy`` producers.

    The top-level shape is exact ({``policy``, ``grants``}); producer
    rejections are mapped onto this card's stable codes. Prose, prompt
    text or extra annotation fields cannot pass — the only route into
    the grants namespace is the frozen schema.
    """
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise GrantConfigError(
            GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID,
            "document must be an object with exactly: policy, grants",
        )
    try:
        policy = policy_from_json(document["policy"])
    except PolicyError as error:
        raise GrantConfigError(
            GrantConfigErrorCode.POLICY_INVALID, error.detail
        ) from None
    try:
        grants = grants_from_json(document["grants"])
    except PolicyError as error:
        raise GrantConfigError(
            GrantConfigErrorCode.GRANTS_INVALID, error.detail
        ) from None
    return policy, grants


class GrantConfigStore:
    """Confined, revision-checked, atomic grant-configuration storage.

    The store holds no cached state: every operation resolves the
    current files, so a read always observes the latest committed
    revision (AC2: reload-before-execution sees revocation). Single
    writer per data root is a caller invariant (C05 rule 6).
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

    def read(self) -> StoredGrantConfig:
        """Return the current configuration, hash-verified against metadata.

        Raises :class:`GrantConfigError` with ``not_found`` before the
        first committed write, ``hash_mismatch`` for an interrupted
        commit or external modification (recovery must reconcile from
        the file hash — never invented here).
        """
        target = self._confined(canonical_config_relative_path())
        meta_path = self._confined(canonical_config_meta_relative_path())
        if not target.exists():
            raise GrantConfigError(
                GrantConfigErrorCode.NOT_FOUND,
                "no grant configuration stored below this data root",
            )
        meta = self._load_meta(meta_path)
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise GrantConfigError(
                GrantConfigErrorCode.NOT_FOUND,
                "no grant configuration stored below this data root",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise GrantConfigError(
                GrantConfigErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery must "
                "reconcile from the file hash",
            )
        policy, grants = self._parse_stored(data)
        if policy.revision != meta["revision"]:
            raise GrantConfigError(
                GrantConfigErrorCode.REVISION_CONFLICT,
                "stored policy revision disagrees with the file revision "
                "metadata; recovery must reconcile before further use",
            )
        return StoredGrantConfig(
            revision=meta["revision"],
            schema_revision=meta["schema_revision"],
            sha256=digest,
            policy=policy,
            grants=grants,
        )

    def write(self, document: object, *, expected_revision: int) -> StoredGrantConfig:
        """Validate then atomically replace the canonical configuration.

        ``expected_revision`` is the revision the caller last observed
        (``0`` to create); the document itself names the next revision
        (``policy.revision == expected_revision + 1`` — never invented
        here). On success the stored revision is ``expected + 1`` and
        the document's policy revision equals it. A stale caller gets
        ``stale_revision``; a document disagreeing with its own next
        revision gets ``revision_conflict``; any schema deviation is
        rejected before any filesystem effect.
        """
        self._validated_expected_revision(expected_revision)
        policy, grants = _validated_document(document)
        if policy.revision != expected_revision + 1:
            raise GrantConfigError(
                GrantConfigErrorCode.REVISION_CONFLICT,
                f"document policy revision {policy.revision} must equal "
                f"expected_revision + 1 ({expected_revision + 1})",
            )
        data = _serialize(_canonical_document(policy, grants))
        target = self._confined(canonical_config_relative_path())
        meta_path = self._confined(canonical_config_meta_relative_path())
        current = self._current_revision(target, meta_path)
        if current != expected_revision:
            raise GrantConfigError(
                GrantConfigErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_policy_dir(target.parent)
        self._remove_stale_temps(target.parent)
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # W1 (file half): a crash before this replace leaves the previous
        # pair fully intact.
        self._atomic_write(target, data)
        # Canonical first, metadata second: an interrupted commit leaves the
        # new content with lagging metadata, reported by read() as
        # hash_mismatch — never silently reconciled (C02 rule 5).
        self._atomic_write(meta_path, self._meta_bytes(revision, digest))
        return StoredGrantConfig(
            revision=revision,
            schema_revision=CONFIG_SCHEMA_VERSION,
            sha256=digest,
            policy=policy,
            grants=grants,
        )

    # -- validation and state helpers --------------------------------------

    @staticmethod
    def _validated_expected_revision(expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise GrantConfigError(
                GrantConfigErrorCode.INVALID_REVISION,
                "expected_revision must be a non-negative int",
            )

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural (fixed names)."""
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise GrantConfigError(
                    GrantConfigErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise GrantConfigError(
                GrantConfigErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    def _load_meta(self, meta_path: Path) -> dict[str, object]:
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            raise GrantConfigError(
                GrantConfigErrorCode.METADATA_MISSING,
                "canonical configuration exists without revision metadata; "
                "recovery must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GrantConfigError(
                GrantConfigErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        if (
            not isinstance(meta, dict)
            or meta.get("format") != CONFIG_META_FORMAT
            or not _is_positive_int(meta.get("revision"))
            or not _is_positive_int(meta.get("schema_revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
        ):
            raise GrantConfigError(
                GrantConfigErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        return meta

    def _current_revision(self, target: Path, meta_path: Path) -> int:
        """Pairing-aware current revision; ``ABSENT_REVISION`` when both absent."""
        has_file = target.exists()
        has_meta = meta_path.exists()
        if not has_file and not has_meta:
            return ABSENT_REVISION
        if has_file and not has_meta:
            raise GrantConfigError(
                GrantConfigErrorCode.METADATA_MISSING,
                "canonical configuration exists without revision metadata; "
                "recovery must reconcile before further writes",
            )
        meta = self._load_meta(meta_path)
        if not has_file:
            raise GrantConfigError(
                GrantConfigErrorCode.ORPHAN_METADATA,
                "revision metadata exists without a canonical configuration; "
                "recovery must reconcile before writing",
            )
        return int(meta["revision"])

    def _parse_stored(self, data: bytes) -> tuple[Policy, dict[str, frozenset]]:
        """Validate stored bytes through the same producers as ``write``.

        Defense in depth: the hash check proves the bytes are the ones
        this store committed; the producers prove they still mean what
        the schema says (both fail closed).
        """
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise GrantConfigError(
                GrantConfigErrorCode.DOCUMENT_NOT_JSON,
                "canonical configuration is not a valid JSON document",
            ) from None
        try:
            return _validated_document(document)
        except GrantConfigError as error:
            raise GrantConfigError(
                GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID,
                f"stored configuration fails schema validation ({error.detail})",
            ) from None

    @staticmethod
    def _ensure_policy_dir(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)

    @staticmethod
    def _meta_bytes(revision: int, digest: str) -> bytes:
        payload = {
            "format": CONFIG_META_FORMAT,
            "revision": revision,
            "schema_revision": CONFIG_SCHEMA_VERSION,
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
        prefixes = (f"{CONFIG_FILENAME}.", f"{CONFIG_META_FILENAME}.")
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


# ---------------------------------------------------------------------------
# Production entrypoint: one command -> one JSON line (0/1/2 exit contract)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.policy.config",
        description=(
            "Confined atomic storage for the versioned server grant "
            "configuration (contracts.md C02/C03). All error output is "
            "value-free. Exit 0 success, 1 structured rejection, 2 usage "
            "or IO error."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_command = subparsers.add_parser(
        "read",
        help="print the current stored configuration as one JSON line",
    )
    read_command.add_argument("root", help="data root directory (created 0o700)")

    write_command = subparsers.add_parser(
        "write",
        help="validate and atomically store the grant configuration document",
    )
    write_command.add_argument("root", help="data root directory (created 0o700)")
    write_command.add_argument(
        "document_file", help="path of the configuration JSON document"
    )
    write_command.add_argument(
        "--expect-revision",
        type=int,
        required=True,
        help="revision the caller last observed (0 = create)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; exit 0 success / 1 structured rejection / 2 usage or IO."""
    args = _build_parser().parse_args(argv)
    try:
        store = GrantConfigStore(args.root)
        if args.command == "read":
            print(json.dumps(store.read().to_json(), sort_keys=True))
        else:
            try:
                text = Path(args.document_file).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"io error: {exc.strerror or exc.__class__.__name__}", file=sys.stderr)
                return 2
            try:
                document = json.loads(text)
            except RecursionError:
                raise GrantConfigError(
                    GrantConfigErrorCode.DOCUMENT_TOO_DEEP,
                    "document nesting exceeds the supported depth",
                ) from None
            except (ValueError, UnicodeDecodeError):
                raise GrantConfigError(
                    GrantConfigErrorCode.DOCUMENT_NOT_JSON,
                    "configuration document file is unreadable or not JSON",
                ) from None
            stored = store.write(document, expected_revision=args.expect_revision)
            print(json.dumps(stored.to_json(), sort_keys=True))
        return 0
    except GrantConfigError as exc:
        print(
            json.dumps({"error": {"code": exc.code.value, "message": exc.detail}}),
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"io error: {exc.strerror or exc.__class__.__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
