"""Focused tests for the confined atomic profile file store (card 02-file-store).

Acceptance (BRIEF):
1. Write/read/delete by server ID uses root confinement, revision checks and
   atomic replace.
2. Traversal/symlink escape and stale revision rejected; crash before replace
   preserves previous valid content.

Boundary discipline (test policy, risk-critical slice): confinement,
atomicity and crash-preservation claims are exercised on a real temporary
filesystem. The pre-replace interruption is proven twice — deterministically
in-process (``os.replace`` interrupted) and with a **real SIGKILL** delivered
to a child process instrumented to die exactly before the replace call; the
disk state after the kill is inspected by a fresh store instance (restart
semantics). Remaining crash windows (W2 file/metadata divergence, W3/W4
projection) are asserted as honest detection, never silent repair — recovery
reconciliation is owned by ``02-profile-projection``. No DB, network or
ambient environment is touched; error payloads are checked value-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import SCHEMA_VERSION, ProfileValidationError
from mai.profiles.files import (
    MAX_DOCUMENT_BYTES,
    META_FILENAME,
    PROFILE_FILENAME,
    FileStoreErrorCode,
    ProfileFileError,
    ProfileFileStore,
    canonical_meta_relative_path,
    canonical_relative_path,
    main,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = uuid.UUID("7e6c1a34-9a2b-4c3d-8e5f-1029384756af")
OTHER_SERVER_ID = uuid.UUID("3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f")

# 43-char unpadded base64 body; same frozen fixture as the schema card.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"


def _profile_text(server_id: uuid.UUID = SERVER_ID, name: str = "build-host") -> str:
    return f"""\
schema_version: {SCHEMA_VERSION}
server_id: "{server_id}"
name: "{name}"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "{HOST_KEY}"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
  - shell.exec
  - files.read
"""


def _codes_of(exc: ProfileFileError) -> FileStoreErrorCode:
    return exc.code


# ---------------------------------------------------------------------------
# AC1 — write/read/delete by server ID with revision checks and atomic replace
# ---------------------------------------------------------------------------


def test_create_write_read_roundtrip_revision_one(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    text = _profile_text()
    stored = store.write(SERVER_ID, text, expected_revision=0)
    assert stored.revision == 1
    assert stored.schema_revision == SCHEMA_VERSION
    assert stored.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert store.read(SERVER_ID) == stored


def test_update_bumps_revision_and_replaces_content(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    first = store.write(SERVER_ID, _profile_text(), expected_revision=0)
    second = store.write(
        SERVER_ID, _profile_text(name="renamed-host"), expected_revision=1
    )
    assert (first.revision, second.revision) == (1, 2)
    reread = store.read(SERVER_ID)
    assert reread.revision == 2
    assert reread.text == _profile_text(name="renamed-host")


def test_stale_write_rejected_and_previous_pair_intact(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    original = _profile_text()
    store.write(SERVER_ID, original, expected_revision=0)
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, _profile_text(name="other"), expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.STALE_REVISION
    reread = store.read(SERVER_ID)
    assert (reread.revision, reread.text) == (1, original)


def test_stale_delete_rejected_and_content_intact(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    with pytest.raises(ProfileFileError) as excinfo:
        store.delete(SERVER_ID, expected_revision=5)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.STALE_REVISION
    assert store.read(SERVER_ID).revision == 1


def test_delete_with_matching_revision_then_recreate(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    store.delete(SERVER_ID, expected_revision=1)
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.NOT_FOUND
    recreated = store.write(SERVER_ID, _profile_text(), expected_revision=0)
    assert recreated.revision == 1


def test_delete_absent_is_not_found(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    with pytest.raises(ProfileFileError) as excinfo:
        store.delete(SERVER_ID, expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.NOT_FOUND


def test_read_absent_is_not_found(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.NOT_FOUND


def test_invalid_document_rejected_before_any_filesystem_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data-root"
    store = ProfileFileStore(root)
    invalid = _profile_text().replace("linux_ssh", "docker")
    with pytest.raises(ProfileValidationError) as excinfo:
        store.write(SERVER_ID, invalid, expected_revision=0)
    assert "environment" in excinfo.value.fields
    assert not (root / "servers").exists()
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.NOT_FOUND


def test_embedded_server_id_must_match_storage_identity(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    foreign = _profile_text(server_id=OTHER_SERVER_ID)
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, foreign, expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.SERVER_ID_MISMATCH
    with pytest.raises(ProfileFileError):
        store.read(SERVER_ID)


@pytest.mark.parametrize("bad_id", [str(SERVER_ID), str(SERVER_ID).upper(), 5, None])
def test_non_uuid_identity_rejected(tmp_path: Path, bad_id: object) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(bad_id, _profile_text(), expected_revision=0)  # type: ignore[arg-type]
    assert _codes_of(excinfo.value) is FileStoreErrorCode.INVALID_SERVER_ID


@pytest.mark.parametrize("bad_revision", [-1, True, "1", 1.5, None])
def test_invalid_expected_revision_rejected(
    tmp_path: Path, bad_revision: object
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, _profile_text(), expected_revision=bad_revision)  # type: ignore[arg-type]
    assert _codes_of(excinfo.value) is FileStoreErrorCode.INVALID_REVISION


def test_two_server_ids_are_isolated(tmp_path: Path) -> None:
    # CROSS-SERVER fixture obligation: two distinct stable server IDs never
    # exchange canonical content at the storage boundary.
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    store.write(
        OTHER_SERVER_ID, _profile_text(OTHER_SERVER_ID, name="other-host"), expected_revision=0
    )
    mine = store.read(SERVER_ID)
    other = store.read(OTHER_SERVER_ID)
    assert mine.text != other.text
    assert mine.server_id != other.server_id
    assert mine.sha256 != other.sha256
    assert mine.revision == other.revision == 1


def test_revision_state_survives_store_reopen(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    first = ProfileFileStore(root)
    first.write(SERVER_ID, _profile_text(), expected_revision=0)
    first.write(SERVER_ID, _profile_text(name="v2"), expected_revision=1)
    reopened = ProfileFileStore(root)
    assert reopened.read(SERVER_ID).revision == 2


# ---------------------------------------------------------------------------
# AC1 — restrictive permissions and metadata shape (C02 rules 1 and 4)
# ---------------------------------------------------------------------------


def test_permissions_are_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = ProfileFileStore(root)
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    assert (root.stat().st_mode & 0o777) == 0o700
    server_dir = root / "servers" / str(SERVER_ID)
    assert (server_dir.stat().st_mode & 0o777) == 0o700
    for name in (PROFILE_FILENAME, META_FILENAME):
        assert ((server_dir / name).stat().st_mode & 0o777) == 0o600


def test_metadata_carries_frozen_version_triple(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    stored = store.write(SERVER_ID, _profile_text(), expected_revision=0)
    meta = json.loads(
        (store.root / canonical_meta_relative_path(SERVER_ID)).read_bytes()
    )
    assert meta == {
        "format": 1,
        "server_id": str(SERVER_ID),
        "revision": stored.revision,
        "schema_revision": SCHEMA_VERSION,
        "sha256": stored.sha256,
    }


# ---------------------------------------------------------------------------
# AC2 — symlink escape / aliasing rejected at every path component
# ---------------------------------------------------------------------------


def _outside_fixture(tmp_path: Path) -> tuple[Path, bytes]:
    outside = tmp_path / "outside-fixture"
    outside.mkdir()
    target = outside / "escaped.yaml"
    target.write_bytes(b"outside: true\n")
    return target, b"outside: true\n"


def test_symlinked_canonical_file_to_outside_rejected(tmp_path: Path) -> None:
    # Brief scenario fixture: a canonical file that is a symlink pointing
    # outside the root must be refused, and the outside target untouched.
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    canonical = store.root / canonical_relative_path(SERVER_ID)
    canonical.unlink()
    canonical.symlink_to(outside_target)
    for operation in (
        lambda: store.read(SERVER_ID),
        lambda: store.write(SERVER_ID, _profile_text(), expected_revision=1),
        lambda: store.delete(SERVER_ID, expected_revision=1),
    ):
        with pytest.raises(ProfileFileError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is FileStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_server_dir_to_outside_rejected(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    server_link = store.root / "servers" / str(SERVER_ID)
    server_link.parent.mkdir(parents=True, exist_ok=True)
    server_link.symlink_to(outside_target, target_is_directory=True)
    for operation in (
        lambda: store.read(SERVER_ID),
        lambda: store.write(SERVER_ID, _profile_text(), expected_revision=0),
        lambda: store.delete(SERVER_ID, expected_revision=0),
    ):
        with pytest.raises(ProfileFileError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is FileStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_servers_dir_rejected(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    servers = store.root / "servers"
    servers.symlink_to(outside_target, target_is_directory=True)
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_metadata_file_rejected(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    meta = store.root / canonical_meta_relative_path(SERVER_ID)
    meta.unlink()
    meta.symlink_to(outside_target)
    for operation in (
        lambda: store.read(SERVER_ID),
        lambda: store.delete(SERVER_ID, expected_revision=1),
    ):
        with pytest.raises(ProfileFileError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is FileStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_data_root_config_is_resolved_and_confined(tmp_path: Path) -> None:
    # C02 rule 1: the root is configured explicitly; a symlinked root path is
    # resolved once at construction and confinement holds below it.
    real_root = tmp_path / "real-root"
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    store = ProfileFileStore(link_root)
    assert store.root == real_root.resolve()
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    assert store.read(SERVER_ID).revision == 1


# ---------------------------------------------------------------------------
# AC2 — integrity metadata honestly reported, never invented
# ---------------------------------------------------------------------------


def test_external_content_tamper_reported_as_hash_mismatch(
    tmp_path: Path,
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    canonical = store.root / canonical_relative_path(SERVER_ID)
    canonical.write_bytes(canonical.read_bytes() + b"tampered: true\n")
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.HASH_MISMATCH
    assert "build-host" not in str(excinfo.value)


def test_missing_metadata_reported_not_reconstructed(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    (store.root / canonical_meta_relative_path(SERVER_ID)).unlink()
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.METADATA_MISSING


def test_invalid_metadata_shape_reported(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    meta_path = store.root / canonical_meta_relative_path(SERVER_ID)
    meta_path.write_bytes(b"{not json")
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.METADATA_INVALID


def test_orphan_metadata_blocks_write_until_delete_completes(
    tmp_path: Path,
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    (store.root / canonical_relative_path(SERVER_ID)).unlink()
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, _profile_text(), expected_revision=1)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.ORPHAN_METADATA
    store.delete(SERVER_ID, expected_revision=1)
    assert store.write(SERVER_ID, _profile_text(), expected_revision=0).revision == 1


# ---------------------------------------------------------------------------
# AC2 — crash before replace preserves previous valid content (W1 file-half)
# ---------------------------------------------------------------------------


def _server_dir(store: ProfileFileStore) -> Path:
    return store.root / "servers" / str(SERVER_ID)


def test_interrupted_before_canonical_replace_preserves_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    original = _profile_text()
    store.write(SERVER_ID, original, expected_revision=0)

    def _interrupted(src: object, dst: object) -> None:
        raise OSError("interrupted before replace")

    monkeypatch.setattr("mai.profiles.files.os.replace", _interrupted)
    with pytest.raises(OSError):
        store.write(SERVER_ID, _profile_text(name="never-stored"), expected_revision=1)
    monkeypatch.undo()

    reread = store.read(SERVER_ID)
    assert (reread.revision, reread.text) == (1, original)
    recovered = store.write(SERVER_ID, _profile_text(name="v2"), expected_revision=1)
    assert recovered.revision == 2
    assert not list(_server_dir(store).glob("*.tmp"))


def test_interrupted_after_canonical_replace_is_detectable_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C02 W2: canonical replaced, metadata not yet — read() reports the
    # divergence (file authoritative) instead of inventing a successful sync.
    store = ProfileFileStore(tmp_path / "data-root")
    original = _profile_text()
    store.write(SERVER_ID, original, expected_revision=0)
    real_replace = os.replace
    calls = {"count": 0}

    def _half_committed(src: object, dst: object) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("interrupted between replaces")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("mai.profiles.files.os.replace", _half_committed)
    with pytest.raises(OSError):
        store.write(SERVER_ID, _profile_text(name="w2-window"), expected_revision=1)
    monkeypatch.undo()

    canonical = store.root / canonical_relative_path(SERVER_ID)
    assert b"w2-window" in canonical.read_bytes()  # file is authoritative
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.HASH_MISMATCH


def test_real_sigkill_before_replace_preserves_previous_content(
    tmp_path: Path,
) -> None:
    """Real-boundary proof: SIGKILL at the replace boundary, then restart."""
    store = ProfileFileStore(tmp_path / "data-root")
    original = _profile_text()
    store.write(SERVER_ID, original, expected_revision=0)

    child_script = """
import os, signal, sys, uuid
from mai.profiles.files import ProfileFileStore
root, server_id = sys.argv[1], uuid.UUID(sys.argv[2])
text = sys.stdin.read()
def _die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = _die
ProfileFileStore(root).write(server_id, text, expected_revision=1)
raise SystemExit("write returned; kill hook did not fire")
"""
    completed = subprocess.run(
        [sys.executable, "-c", child_script, str(store.root), str(SERVER_ID)],
        input=_profile_text(name="killed-mid-write"),
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    # Restart semantics: a fresh store observes the untouched previous pair.
    restarted = ProfileFileStore(store.root)
    reread = restarted.read(SERVER_ID)
    assert (reread.revision, reread.text) == (1, original)
    # Recovery: the next successful write cleans the orphaned temp file.
    recovered = restarted.write(
        SERVER_ID, _profile_text(name="after-restart"), expected_revision=1
    )
    assert recovered.revision == 2
    assert not list(_server_dir(restarted).glob("*.tmp"))


def test_interrupted_delete_completes_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    real_unlink = Path.unlink
    calls = {"count": 0}

    def _half_deleted(self: Path, missing_ok: bool = False) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("interrupted between unlinks")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _half_deleted)
    with pytest.raises(OSError):
        store.delete(SERVER_ID, expected_revision=1)
    monkeypatch.undo()

    meta_path = store.root / canonical_meta_relative_path(SERVER_ID)
    assert meta_path.exists()
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.NOT_FOUND
    store.delete(SERVER_ID, expected_revision=1)  # completes the half delete
    assert not meta_path.exists()
    assert store.write(SERVER_ID, _profile_text(), expected_revision=0).revision == 1


def test_error_details_never_carry_document_values(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    store.write(SERVER_ID, _profile_text(), expected_revision=0)
    canonical = store.root / canonical_relative_path(SERVER_ID)
    canonical.write_bytes(b'{"spoofed": "MAI_SSH_KEY_BUILD_HOST"}\n')
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(SERVER_ID)
    rendered = f"{excinfo.value} {excinfo.value.detail}"
    assert "MAI_SSH_KEY_BUILD_HOST" not in rendered
    assert "deploy" not in rendered


# ---------------------------------------------------------------------------
# Resource-exhaustion boundary (owned per 02-profile-schema evidence §5 item 2)
# ---------------------------------------------------------------------------


def test_alias_bomb_rejected_before_expansion(tmp_path: Path) -> None:
    # Billion-laughs shape (5 levels x 10 refs). The pinned pyyaml memoizes
    # construction (measured), but the contractual boundary is structural:
    # alias references must be refused at the node walk, never relied on
    # loader-side; the rejection code proves the guard fires before both
    # construction and schema parsing.
    store = ProfileFileStore(tmp_path / "data-root")
    bomb = "a0: &a0 [" + ", ".join(["x"] * 10) + "]\n"
    for level in range(1, 6):
        bomb += (
            f"a{level}: &a{level} ["
            + ", ".join([f"*a{level - 1}"] * 10)
            + "]\n"
        )
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, bomb, expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.DOCUMENT_ALIAS_FORBIDDEN
    with pytest.raises(ProfileFileError):
        store.read(SERVER_ID)


def test_oversized_document_rejected_before_parse(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    oversized = "name: " + "x" * MAX_DOCUMENT_BYTES  # 6 + cap bytes > cap
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, oversized, expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.DOCUMENT_TOO_LARGE
    with pytest.raises(ProfileFileError):
        store.read(SERVER_ID)


def test_deeply_nested_document_rejected_not_crashing(
    tmp_path: Path,
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    deep = "name: " + "[" * 30_000 + "]" * 30_000 + "\n"
    with pytest.raises(ProfileFileError) as excinfo:
        store.write(SERVER_ID, deep, expected_revision=0)
    assert _codes_of(excinfo.value) is FileStoreErrorCode.DOCUMENT_TOO_DEEP
    with pytest.raises(ProfileFileError):
        store.read(SERVER_ID)


def test_quoted_anchor_alias_characters_are_not_false_positives(
    tmp_path: Path,
) -> None:
    # The guard is AST-level: quoted &, * and [ in scalar values are legal
    # document content and must round-trip untouched.
    store = ProfileFileStore(tmp_path / "data-root")
    text = _profile_text(name="R&D * research [v1]")
    stored = store.write(SERVER_ID, text, expected_revision=0)
    assert store.read(SERVER_ID).text == stored.text


# ---------------------------------------------------------------------------
# Production entrypoint (exit contract 0 / 1 / 2)
# ---------------------------------------------------------------------------


def test_cli_write_read_delete_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(_profile_text(), encoding="utf-8")
    assert main(["write", str(root), str(SERVER_ID), str(profile_file), "--expect-revision", "0"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["revision"] == 1
    assert main(["read", str(root), str(SERVER_ID)]) == 0
    assert json.loads(capsys.readouterr().out) == written
    assert main(["delete", str(root), str(SERVER_ID), "--expect-revision", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    assert main(["read", str(root), str(SERVER_ID)]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == FileStoreErrorCode.NOT_FOUND.value


def test_cli_output_is_value_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(_profile_text(), encoding="utf-8")
    assert main(["write", str(root), str(SERVER_ID), str(profile_file), "--expect-revision", "0"]) == 0
    captured = capsys.readouterr()
    assert "build-host" not in captured.out
    assert "deploy" not in captured.out


def test_cli_stale_write_exits_one_with_stable_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(_profile_text(), encoding="utf-8")
    assert main(["write", str(root), str(SERVER_ID), str(profile_file), "--expect-revision", "0"]) == 0
    capsys.readouterr()
    assert main(["write", str(root), str(SERVER_ID), str(profile_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == FileStoreErrorCode.STALE_REVISION.value


def test_cli_invalid_document_exits_one_with_schema_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    profile_file = tmp_path / "bad.yaml"
    profile_file.write_text(
        _profile_text().replace("linux_ssh", "docker"), encoding="utf-8"
    )
    assert main(["write", str(root), str(SERVER_ID), str(profile_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "profile_validation_failed"
    assert "environment" in error["error"]["fields"]


def test_cli_malformed_uuid_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["read", str(tmp_path / "data-root"), "not-a-uuid"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == FileStoreErrorCode.INVALID_SERVER_ID.value


def test_cli_missing_profile_file_exits_two(tmp_path: Path) -> None:
    assert (
        main(
            [
                "write",
                str(tmp_path / "data-root"),
                str(SERVER_ID),
                str(tmp_path / "absent.yaml"),
                "--expect-revision",
                "0",
            ]
        )
        == 2
    )


def test_cli_usage_error_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_module_entrypoint_runs_under_python_m(tmp_path: Path) -> None:
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(_profile_text(), encoding="utf-8")
    root = tmp_path / "data-root"
    write = subprocess.run(
        [
            sys.executable,
            "-m",
            "mai.profiles.files",
            "write",
            str(root),
            str(SERVER_ID),
            str(profile_file),
            "--expect-revision",
            "0",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert write.returncode == 0, write.stderr
    assert json.loads(write.stdout)["revision"] == 1
    read = subprocess.run(
        [sys.executable, "-m", "mai.profiles.files", "read", str(root), str(SERVER_ID)],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout) == json.loads(write.stdout)


def test_module_entrypoint_help_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mai.profiles.files", "--help"],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    assert "read" in completed.stdout
