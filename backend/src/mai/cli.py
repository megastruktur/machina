"""Unified scoped operator CLI for bootstrap recovery and health (P12).

Card ``12-cli-ops`` — the single stable command surface that operator scripts
(``scripts/install-host.sh`` from 12-bootstrap, ``scripts/backup-restore.sh``
from 12-backup-restore, ``scripts/uninstall-host.sh`` from 12-uninstall) call
by name, instead of guessing at module entrypoints or HTTP endpoints
(acceptance 2: scripts call the actual stable CLI; no bypass that grants
remote root silently).

Registered commands are exactly the documented operator set
(:data:`DOCUMENTED_COMMANDS`; the parser registers nothing else and accepts
no exec/shell/host escape parameter — enforced by ``backend/tests/test_cli.py``):

- ``mai bootstrap``  — first-time data-root + canonical profile creation
  (create-only; a rerun is refused with the store's ``stale_revision`` and
  the existing pair is preserved — rerun safety, never silently weakened).
- ``mai doctor``     — non-mutating health report over the canonical tree
  (permissions, hash/revision consistency = C02 window W2 detection, orphan
  metadata, stale schema revision, symlinks, optional ``.env`` fail-closed
  check via :class:`~mai.credentials.env.EnvResolver`).
- ``mai status``     — read-only value-free inventory (version triples only,
  never document content).
- ``mai backup``     — archive the whole data root (every regular file) with
  an in-archive SHA-256 manifest; refuses to clobber an existing output.
- ``mai restore``    — verify an archive (manifest present, every member
  hashed, no extra members) BEFORE any target effect, then restore into a
  nonexistent-or-empty data root only (explicit clean-target intent).
- ``mai uninstall``  — requires exactly one explicit data intent
  (``--keep-data`` | ``--delete-data``; C09 rule 5: an inferred flag must
  never become delete); ``--delete-data`` refuses foreign top-level entries
  so managed application data is never removed implicitly.
- ``mai admin write`` / ``mai admin delete`` — admin-controlled canonical
  profile mutation/removal with explicit expected revision (C02 rule 4/6;
  privilege or grant changes are deliberate revision-checked acts, never a
  bootstrap side effect).

Binding contracts:

- **C02 rule 1** — every command takes an explicit ``--data-root`` (required,
  no default, no ambient inference). Only ``bootstrap`` and ``restore`` may
  create a data root; every other command refuses a missing root *without*
  constructing :class:`~mai.profiles.files.ProfileFileStore` (whose
  constructor creates/chmods the root) — an invalid data root therefore fails
  without mutation.
- **C02 rule 2/3** — canonical paths come from the store/contract producers
  only; no command accepts a free-form path inside the data root.
- **C02 rule 4** — status/doctor report the version triple (revision,
  ``schema_revision``, SHA-256) and recompute content hashes; a mismatch is
  reported (``hash_mismatch``), never silently repaired ("no invented
  successful sync" — recovery reconciliation is the projection card's).
- **C03 rule 6** — credential material never appears on any output stream.
  The CLI never prints profile text, ``.env`` values or environment values;
  error details are value-free (producer modules already guarantee this and
  the CLI adds no interpolation of document content).
- **C05 rule 9 / BACKUP-RESTORE row** — the CLI owns only the *file half*
  (canonical data root). Postgres/DBOS state rides the single ``pg_dump``
  performed by the operator script layer; this module never touches a
  database and says so in ``backup``/``restore`` help text.
- **C08 rule 4 / G1-G2** — no command executes anything on a remote target,
  grants privilege, or bypasses enrollment: the parser has no exec/shell/
  host/sudo parameter on any subcommand, and profile content changes flow
  exclusively through the schema-validating, revision-checked store.

Exit contract (same discipline as ``mai.profiles.files``): ``0`` success,
``1`` structured rejection (one JSON ``{"error": {"code", "message"}}`` line
on stderr; ``profile_validation_failed`` carries the producer's ``fields``),
``2`` usage or IO error (argparse errors and unreadable files; value-free).
Success output is one JSON line on stdout. The module reads the process
environment only for the *presence* of ``MAI_DATABASE_URL`` (doctor
information; the value is never echoed).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from mai.contracts.server import SCHEMA_VERSION, ProfileValidationError, parse_server_profile_text
from mai.credentials.env import EnvFileError, EnvResolver
from mai.profiles.files import (
    META_FILENAME,
    PROFILE_FILENAME,
    SERVERS_DIR,
    FileStoreErrorCode,
    ProfileFileError,
    ProfileFileStore,
)

__all__ = [
    "ARCHIVE_MANIFEST_NAME",
    "BACKUP_FORMAT",
    "DOCUMENTED_COMMANDS",
    "EXIT_USAGE_ERROR",
    "OWNED_TOP_LEVEL_ENTRIES",
    "CliErrorCode",
    "build_parser",
    "main",
]

#: The complete documented operator command set. The parser registers exactly
#: these — acceptance 1 ("only documented operator commands registered") is a
#: test-pinned invariant, not prose.
DOCUMENTED_COMMANDS = (
    "bootstrap",
    "doctor",
    "status",
    "backup",
    "restore",
    "uninstall",
    "admin",
)

#: Version of the in-archive manifest written by ``backup`` and required by
#: ``restore``. Bumped only by deliberate reconciliation with consumers.
BACKUP_FORMAT = 1

#: Manifest member name inside a backup archive.
ARCHIVE_MANIFEST_NAME = "manifest.json"

#: argparse exits with status 2 on usage errors; the module's IO-error branch
#: matches that convention so scripts can treat 2 as "caller mistake".
EXIT_USAGE_ERROR = 2


class CliErrorCode(StrEnum):
    """Stable machine-readable CLI-level rejection codes (C01 vocabulary)."""

    DATA_ROOT_NOT_FOUND = "data_root_not_found"
    DATA_ROOT_NOT_DIRECTORY = "data_root_not_directory"
    PROFILE_FILE_NOT_READABLE = "profile_file_not_readable"
    OUTPUT_EXISTS = "output_exists"
    TARGET_NOT_CLEAN = "target_not_clean"
    ARCHIVE_INVALID = "archive_invalid"
    FOREIGN_ENTRIES = "foreign_entries"
    UNEXPECTED_ENTRY = "unexpected_entry"


# ---------------------------------------------------------------------------
# Output helpers (value-free discipline lives here)
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _fail(code: str, message: str) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}, sort_keys=True), file=sys.stderr)
    return 1


def _parse_server_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _read_profile_file(path: str) -> str | None:
    """Read a profile document; ``None`` when unreadable (caller emits)."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _require_existing_root(raw: str) -> Path | None:
    """Return the existing directory root, or ``None`` after emitting.

    Deliberately does NOT construct :class:`ProfileFileStore`: its
    constructor creates/chmods the root, which would mutate on a miss.
    """
    root = Path(raw)
    if not root.exists():
        _fail(CliErrorCode.DATA_ROOT_NOT_FOUND.value, "data root does not exist")
        return None
    if not root.is_dir():
        _fail(CliErrorCode.DATA_ROOT_NOT_DIRECTORY.value, "data root is not a directory")
        return None
    return root


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    server_id = _parse_server_id(args.server_id)
    if server_id is None:
        return _fail(
            FileStoreErrorCode.INVALID_SERVER_ID.value,
            "server_id is not a valid UUID",
        )
    text = _read_profile_file(args.profile)
    if text is None:
        return _fail(
            CliErrorCode.PROFILE_FILE_NOT_READABLE.value,
            "profile document is not a readable UTF-8 file",
        )
    try:
        parse_server_profile_text(text)
    except ProfileValidationError as exc:
        print(
            json.dumps(
                {"error": {"code": "profile_validation_failed", "fields": exc.fields}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    store = ProfileFileStore(args.data_root)
    try:
        stored = store.write(server_id, text, expected_revision=0)
    except ProfileFileError as exc:
        return _fail(exc.code.value, exc.detail)
    _emit(
        {
            "bootstrapped": True,
            "data_root": str(store.root),
            "server_id": str(stored.server_id),
            "revision": stored.revision,
            "schema_revision": stored.schema_revision,
            "sha256": stored.sha256,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# doctor (non-mutating health report)
# ---------------------------------------------------------------------------


def _mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _doctor_server(server_dir: Path) -> tuple[dict[str, object] | None, list[dict[str, str]]]:
    """Inspect one ``servers/<id>/`` directory; returns (inventory, findings).

    Known canonical entries are the frozen C02 rule-2 scheme members
    (``profile.yaml``, ``profile.meta.json``, ``prompt.md``,
    ``applications/``); anything else is an ``unexpected_entry`` finding.
    """
    findings: list[dict[str, str]] = []
    name = server_dir.name
    try:
        server_id = uuid.UUID(name)
    except ValueError:
        return None, [{"code": "invalid_server_directory", "detail": "directory name is not a UUID", "server_id": name}]
    if server_dir.is_symlink() or not server_dir.is_dir():
        return None, [{"code": "server_directory_not_directory", "detail": "servers entry is not a real directory", "server_id": str(server_id)}]

    def finding(code: str, detail: str) -> None:
        findings.append({"code": code, "detail": detail, "server_id": str(server_id)})

    profile_path = server_dir / PROFILE_FILENAME
    meta_path = server_dir / META_FILENAME
    for path, what in ((profile_path, "canonical profile"), (meta_path, "revision metadata")):
        if path.is_symlink():
            finding("symlink_forbidden", f"{what} is a symbolic link")
    inventory: dict[str, object] | None = None
    if not profile_path.exists():
        if meta_path.exists():
            finding("orphan_metadata", "revision metadata without a canonical profile")
        else:
            finding("profile_missing", "neither canonical profile nor revision metadata present")
        return None, findings
    if not meta_path.exists():
        finding("metadata_missing", "canonical profile without revision metadata")
        return None, findings
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        finding("metadata_invalid", "revision metadata is not readable JSON")
        return None, findings
    if not isinstance(meta, dict):
        finding("metadata_invalid", "revision metadata is not a JSON object")
        return None, findings
    revision = meta.get("revision")
    schema_revision = meta.get("schema_revision")
    expected_hash = meta.get("sha256")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        finding("metadata_invalid", "revision is not a positive integer")
        return None, findings
    if not isinstance(schema_revision, int) or isinstance(schema_revision, bool):
        finding("metadata_invalid", "schema_revision is not an integer")
        return None, findings
    if not isinstance(expected_hash, str):
        finding("metadata_invalid", "sha256 is not a string")
        return None, findings
    if schema_revision != SCHEMA_VERSION:
        finding(
            "schema_revision_stale",
            "stored schema revision differs from the current producer schema",
        )
    try:
        data = profile_path.read_bytes()
    except OSError:
        finding("profile_unreadable", "canonical profile is not readable")
        return None, findings
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_hash:
        finding(
            "hash_mismatch",
            "canonical content hash differs from revision metadata "
            "(C02 window W2: file authoritative, reconciliation owned by the projection card)",
        )
    inventory = {
        "server_id": str(server_id),
        "revision": revision,
        "schema_revision": schema_revision,
        "sha256": digest,
    }
    known = {PROFILE_FILENAME, META_FILENAME, "prompt.md", "applications"}
    for entry in sorted(server_dir.iterdir()):
        if entry.name not in known:
            finding("unexpected_entry", f"unexpected entry in server directory: {entry.name}")
    if profile_path.is_file() and not profile_path.is_symlink() and _mode_of(profile_path) & 0o077:
        finding("permissions_too_open", "canonical profile is group/other accessible")
    return inventory, findings


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.data_root)
    findings: list[dict[str, str]] = []
    servers: list[dict[str, object]] = []
    if not root.exists():
        findings.append({"code": "root_missing", "detail": "data root does not exist"})
    elif not root.is_dir():
        findings.append({"code": "root_not_directory", "detail": "data root is not a directory"})
    else:
        if _mode_of(root) != 0o700:
            findings.append(
                {
                    "code": "permissions_too_open",
                    "detail": f"data root mode is {oct(_mode_of(root))}, expected 0o700",
                }
            )
        if root.is_symlink():
            findings.append({"code": "symlink_forbidden", "detail": "data root is a symbolic link"})
        servers_dir = root / SERVERS_DIR
        if not servers_dir.exists():
            findings.append({"code": "root_uninitialized", "detail": "no canonical servers directory present"})
        elif servers_dir.is_symlink() or not servers_dir.is_dir():
            findings.append({"code": "servers_directory_invalid", "detail": "servers entry is not a real directory"})
        else:
            if _mode_of(servers_dir) & 0o077:
                findings.append(
                    {
                        "code": "permissions_too_open",
                        "detail": f"servers directory mode is {oct(_mode_of(servers_dir))}, expected 0o700",
                    }
                )
            for entry in sorted(servers_dir.iterdir()):
                inventory, server_findings = _doctor_server(entry)
                findings.extend(server_findings)
                if inventory is not None:
                    servers.append(inventory)
    if args.env_file is not None:
        try:
            EnvResolver(args.env_file)
        except EnvFileError as exc:
            findings.append({"code": f"env_file_{exc.code.value}", "detail": exc.detail})
    env_status: dict[str, object] | None = None
    if args.env_file is not None:
        env_ok = not any(f["code"].startswith("env_file_") for f in findings)
        env_status = {"path": str(args.env_file), "ok": env_ok}
    report = {
        "data_root": str(root),
        "ok": not findings,
        "servers": sorted(servers, key=lambda s: str(s["server_id"])),
        "findings": findings,
        "env_file": env_status,
        "database_url_configured": bool(os.environ.get("MAI_DATABASE_URL")),
    }
    _emit(report)
    return 0 if not findings else 1


# ---------------------------------------------------------------------------
# status (read-only inventory)
# ---------------------------------------------------------------------------


def _scan_inventory(root: Path) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Read-only metadata scan mirroring the doctor consistency checks."""
    servers_dir = root / SERVERS_DIR
    servers: list[dict[str, object]] = []
    findings: list[dict[str, str]] = []
    if not servers_dir.is_dir() or servers_dir.is_symlink():
        return servers, findings
    for entry in sorted(servers_dir.iterdir()):
        inventory, server_findings = _doctor_server(entry)
        findings.extend(server_findings)
        if inventory is not None:
            servers.append(inventory)
    return servers, findings


def _cmd_status(args: argparse.Namespace) -> int:
    root = _require_existing_root(args.data_root)
    if root is None:
        return 1
    if args.server_id is not None:
        server_id = _parse_server_id(args.server_id)
        if server_id is None:
            return _fail(FileStoreErrorCode.INVALID_SERVER_ID.value, "server_id is not a valid UUID")
        server_dir = root / SERVERS_DIR / str(server_id)
        canonical = server_dir / PROFILE_FILENAME
        if not canonical.exists():
            return _fail(FileStoreErrorCode.NOT_FOUND.value, "no canonical profile stored for this server id")
        inventory, findings = _doctor_server(server_dir)
        if findings:
            return _fail(findings[0]["code"], findings[0]["detail"])
        assert inventory is not None
        _emit(inventory)
        return 0
    servers, findings = _scan_inventory(root)
    _emit(
        {
            "data_root": str(root),
            "servers": servers,
            "findings": findings,
            "ok": not findings,
        }
    )
    return 0 if not findings else 1


# ---------------------------------------------------------------------------
# backup / restore (file half; Postgres rides the operator's single pg_dump)
# ---------------------------------------------------------------------------


def _collect_regular_files(root: Path) -> list[tuple[Path, str]]:
    """Every regular file below root as (path, archive-relative name).

    Refuses symbolic links and non-regular entries: a backup must never
    follow an escape alias out of the canonical tree (C02 rule 3).
    """
    collected: list[tuple[Path, str]] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        for name in sorted(dirnames):
            if (here / name).is_symlink():
                raise ProfileFileError(
                    FileStoreErrorCode.PATH_ESCAPE,
                    f"symbolic link directory in data root: {Path(name)}",
                )
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink())
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink() or not path.is_file():
                raise ProfileFileError(
                    CliErrorCode.UNEXPECTED_ENTRY,
                    f"non-regular entry in data root: {path.relative_to(root)}",
                )
            collected.append((path, path.relative_to(root).as_posix()))
    return collected


def _write_archive(root: Path, output: Path) -> int:
    try:
        files = _collect_regular_files(root)
    except ProfileFileError as exc:
        return _fail(exc.code.value, exc.detail)
    manifest = {
        "format": BACKUP_FORMAT,
        "files": [
            {"path": rel, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path, rel in files
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".mai-backup-", dir=output.parent)
    os.close(fd)
    staging_path = Path(staging)
    try:
        with tarfile.open(staging_path, "w:gz") as archive:
            for path, rel in files:
                info = tarfile.TarInfo(name=rel)
                info.size = path.stat().st_size
                info.mode = 0o600
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(name=ARCHIVE_MANIFEST_NAME)
            info.size = len(manifest_bytes)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(manifest_bytes))
        os.replace(staging_path, output)
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    _emit(
        {
            "archive": str(output),
            "entries": len(files),
            "bytes": output.stat().st_size,
            "sha256": digest,
        }
    )
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    root = _require_existing_root(args.data_root)
    if root is None:
        return 1
    output = Path(args.output)
    if output.exists():
        return _fail(CliErrorCode.OUTPUT_EXISTS.value, "archive output path already exists")
    return _write_archive(root, output)


def _verify_archive(archive_path: Path) -> list[dict[str, object]]:
    """Return the verified manifest file list; raise ProfileFileError on any
    structural problem. Verification happens before ANY target effect."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if ARCHIVE_MANIFEST_NAME not in names:
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "archive does not contain the backup manifest",
                )
            manifest_member = archive.extractfile(ARCHIVE_MANIFEST_NAME)
            if manifest_member is None:
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "backup manifest is not a regular archive member",
                )
            try:
                manifest = json.loads(manifest_member.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "backup manifest is not readable JSON",
                ) from None
            if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "backup manifest format is unsupported",
                )
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "backup manifest has no file list",
                )
            listed: dict[str, str] = {}
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                    raise ProfileFileError(
                        CliErrorCode.ARCHIVE_INVALID,
                        "backup manifest entry is malformed",
                    )
                listed[entry["path"]] = entry["sha256"]
            data_members = set(names) - {ARCHIVE_MANIFEST_NAME}
            if data_members != set(listed):
                raise ProfileFileError(
                    CliErrorCode.ARCHIVE_INVALID,
                    "archive members and manifest disagree (truncated or tampered archive)",
                )
            for member in members:
                if member.name == ARCHIVE_MANIFEST_NAME:
                    continue
                if member.issym() or member.islnk() or not member.isfile():
                    raise ProfileFileError(
                        CliErrorCode.ARCHIVE_INVALID,
                        "archive member is not a regular file",
                    )
                handle = archive.extractfile(member)
                if handle is None:
                    raise ProfileFileError(
                        CliErrorCode.ARCHIVE_INVALID,
                        "archive member is not readable",
                    )
                if hashlib.sha256(handle.read()).hexdigest() != listed[member.name]:
                    raise ProfileFileError(
                        CliErrorCode.ARCHIVE_INVALID,
                        "archive member hash differs from the manifest",
                    )
            return entries
    except tarfile.TarError:
        raise ProfileFileError(
            CliErrorCode.ARCHIVE_INVALID,
            "archive is not a readable gzip tar backup",
        ) from None


def _cmd_restore(args: argparse.Namespace) -> int:
    archive_path = Path(args.archive)
    if not archive_path.is_file():
        return _fail(CliErrorCode.ARCHIVE_INVALID.value, "backup archive does not exist")
    target = Path(args.data_root)
    if target.exists():
        if not target.is_dir():
            return _fail(CliErrorCode.TARGET_NOT_CLEAN.value, "restore target exists and is not a directory")
        if any(target.iterdir()):
            return _fail(
                CliErrorCode.TARGET_NOT_CLEAN.value,
                "restore target is not empty; a restore needs an explicitly clean target",
            )
    try:
        entries = _verify_archive(archive_path)
    except ProfileFileError as exc:
        return _fail(exc.code.value, exc.detail)
    store = ProfileFileStore(target)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name != ARCHIVE_MANIFEST_NAME]
        archive.extractall(path=store.root, members=members, filter="data")
    for path in store.root.rglob("*"):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    _emit(
        {
            "restored": True,
            "data_root": str(store.root),
            "entries": len(entries),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# uninstall (explicit keep/delete-data intent; C09 rule 5)
# ---------------------------------------------------------------------------

#: Top-level entries a MAI data root may legitimately contain. Anything else
#: is foreign and blocks ``--delete-data`` (managed application data must
#: never be removed implicitly).
OWNED_TOP_LEVEL_ENTRIES = frozenset({SERVERS_DIR})


def _cmd_uninstall(args: argparse.Namespace) -> int:
    root = _require_existing_root(args.data_root)
    if root is None:
        return 1
    if args.keep_data:
        entries = sorted(entry.name for entry in root.iterdir())
        _emit(
            {
                "uninstalled": True,
                "data_root": str(root),
                "deleted": False,
                "retained_entries": entries,
            }
        )
        return 0
    foreign = sorted(entry.name for entry in root.iterdir() if entry.name not in OWNED_TOP_LEVEL_ENTRIES)
    if foreign:
        return _fail(
            CliErrorCode.FOREIGN_ENTRIES.value,
            "data root holds entries MAI does not own; move them before delete-data: "
            + ", ".join(foreign),
        )
    owned = sorted(entry.name for entry in root.iterdir())
    shutil.rmtree(root)
    _emit(
        {
            "uninstalled": True,
            "data_root": str(root),
            "deleted": True,
            "removed_entries": owned,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# admin (explicit revision-checked canonical mutations)
# ---------------------------------------------------------------------------


def _meta_view(stored: object) -> dict[str, object]:
    return {
        "server_id": str(stored.server_id),  # type: ignore[attr-defined]
        "revision": stored.revision,  # type: ignore[attr-defined]
        "schema_revision": stored.schema_revision,  # type: ignore[attr-defined]
        "sha256": stored.sha256,  # type: ignore[attr-defined]
    }


def _cmd_admin(args: argparse.Namespace) -> int:
    root = _require_existing_root(args.data_root)
    if root is None:
        return 1
    server_id = _parse_server_id(args.server_id)
    if server_id is None:
        return _fail(FileStoreErrorCode.INVALID_SERVER_ID.value, "server_id is not a valid UUID")
    store = ProfileFileStore(root)
    try:
        if args.admin_command == "write":
            text = _read_profile_file(args.profile)
            if text is None:
                return _fail(
                    CliErrorCode.PROFILE_FILE_NOT_READABLE.value,
                    "profile document is not a readable UTF-8 file",
                )
            stored = store.write(server_id, text, expected_revision=args.expect_revision)
            _emit(_meta_view(stored))
        else:
            store.delete(server_id, expected_revision=args.expect_revision)
            _emit({"server_id": str(server_id), "deleted": True})
        return 0
    except ProfileValidationError as exc:
        print(
            json.dumps(
                {"error": {"code": "profile_validation_failed", "fields": exc.fields}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except ProfileFileError as exc:
        return _fail(exc.code.value, exc.detail)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai",
        description=(
            "Scoped operator CLI for MAI bootstrap recovery and health "
            "(contracts.md C02/C03/C05/C09). All output is value-free; profile "
            "text, credential references' resolved values and environment "
            "values are never printed. File half only: Postgres state rides "
            "the operator's single pg_dump (C05 guard rule 9)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_data_root(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--data-root",
            required=True,
            help="explicit data root directory (C02 rule 1; never inferred)",
        )

    bootstrap_command = subparsers.add_parser(
        "bootstrap",
        help="create the data root and the first canonical profile (create-only)",
        description=(
            "First-time provisioning: creates the data root (0o700) and stores "
            "the initial profile at revision 1. A rerun over an existing "
            "profile is refused (stale_revision) and the existing pair is "
            "preserved — deliberate re-provisioning goes through "
            "'mai admin write --expect-revision'."
        ),
    )
    add_data_root(bootstrap_command)
    bootstrap_command.add_argument("--server-id", required=True, help="opaque stable server UUID")
    bootstrap_command.add_argument("--profile", required=True, help="path of the profile YAML to store")

    doctor_command = subparsers.add_parser(
        "doctor",
        help="non-mutating health report over the canonical data root",
        description=(
            "Checks root/servers permissions, per-server hash/revision "
            "consistency (C02 window W2 detection), orphan metadata, stale "
            "schema revisions, symlinks and (optionally) the fail-closed "
            ".env credential source. Never repairs anything."
        ),
    )
    add_data_root(doctor_command)
    doctor_command.add_argument(
        "--env-file",
        default=None,
        help="optional explicit .env credential source to fail-closed check (names/values never printed)",
    )

    status_command = subparsers.add_parser(
        "status",
        help="read-only inventory of stored profiles (version triples only)",
    )
    add_data_root(status_command)
    status_command.add_argument(
        "--server-id",
        default=None,
        help="report a single server's version triple instead of the inventory",
    )

    backup_command = subparsers.add_parser(
        "backup",
        help="archive the whole data root with an in-archive SHA-256 manifest",
        description=(
            "File half only: archives every regular file below the data root. "
            "Postgres/DBOS state is NOT included — the operator script owns "
            "the single pg_dump (C05 guard rule 9)."
        ),
    )
    add_data_root(backup_command)
    backup_command.add_argument("--output", required=True, help="archive path (must not already exist)")

    restore_command = subparsers.add_parser(
        "restore",
        help="verify a backup archive, then restore it into a clean data root",
        description=(
            "Verifies the manifest and every member hash BEFORE any target "
            "effect, then restores into a nonexistent-or-empty data root. "
            "File half only; database restore is the operator script's "
            "single pg_dump load (C05 guard rule 9)."
        ),
    )
    add_data_root(restore_command)
    restore_command.add_argument("--from", dest="archive", required=True, help="backup archive path")

    uninstall_command = subparsers.add_parser(
        "uninstall",
        help="remove controller data with an explicit keep/delete-data intent",
        description=(
            "Requires exactly one of --keep-data / --delete-data (C09 rule 5: "
            "an inferred absent flag must never become delete). --delete-data "
            "refuses a data root holding foreign top-level entries so managed "
            "application data is never removed implicitly."
        ),
    )
    add_data_root(uninstall_command)
    intent = uninstall_command.add_mutually_exclusive_group(required=True)
    intent.add_argument("--keep-data", action="store_true", help="remove nothing; print the retained inventory")
    intent.add_argument("--delete-data", action="store_true", help="delete the MAI-owned data root")

    admin_parser = subparsers.add_parser(
        "admin",
        help="explicit revision-controlled canonical profile operations",
        description=(
            "Admin-controlled canonical mutations (C02 rule 4/6): every "
            "change is a schema-validated, expected-revision-checked write "
            "or delete. There is no bypass form; privilege/grant changes are "
            "deliberate versioned acts."
        ),
    )
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command", required=True)

    admin_write = admin_subparsers.add_parser(
        "write",
        help="validate and atomically replace a canonical profile at an explicit revision",
    )
    add_data_root(admin_write)
    admin_write.add_argument("--server-id", required=True, help="opaque stable server UUID")
    admin_write.add_argument("--profile", required=True, help="path of the profile YAML to store")
    admin_write.add_argument(
        "--expect-revision",
        type=int,
        required=True,
        help="revision the caller last observed (0 = create)",
    )

    admin_delete = admin_subparsers.add_parser(
        "delete",
        help="revision-checked removal of one canonical profile pair",
    )
    add_data_root(admin_delete)
    admin_delete.add_argument("--server-id", required=True, help="opaque stable server UUID")
    admin_delete.add_argument(
        "--expect-revision",
        type=int,
        required=True,
        help="revision the caller last observed",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    """Public parser factory (tests pin the registered surface on it)."""
    return _build_parser()


_DISPATCH = {
    "bootstrap": _cmd_bootstrap,
    "doctor": _cmd_doctor,
    "status": _cmd_status,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "uninstall": _cmd_uninstall,
    "admin": _cmd_admin,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; exit 0 success / 1 structured rejection / 2 usage or IO."""
    args = _build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args)
    except ProfileFileError as exc:
        return _fail(exc.code.value, exc.detail)
    except OSError as exc:
        print(f"io error: {exc.strerror or exc.__class__.__name__}", file=sys.stderr)
        return EXIT_USAGE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
