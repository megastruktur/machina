"""Focused tests for the unified operator CLI (card 12-cli-ops).

Acceptance (BRIEF):
1. Only documented operator commands registered with explicit data
   root/target and safe nonzero errors.
2. Bootstrap/backup scripts call the actual stable CLI, not guessed
   endpoints; no bypass that grants remote root silently.

Boundary discipline (test policy, risk-critical slice): exit codes,
structured value-free errors, non-mutation on refusal and the "no hidden
command / no exec-shell-host escape parameter" property are exercised
in-process against a real temporary filesystem, plus one real-subprocess
entrypoint run of ``python -m mai.cli --help`` pinning the registered
surface of the installed package. Secret non-disclosure is checked with
sentinel markers planted in the profile path, the process environment and
the ``.env`` fixture — no marker may reach any output stream. No DB,
network or ambient state is touched.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import pytest

from mai.cli import (
    ARCHIVE_MANIFEST_NAME,
    DOCUMENTED_COMMANDS,
    build_parser,
    main,
)
from mai.contracts.server import SCHEMA_VERSION
from mai.profiles.files import ProfileFileStore

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = uuid.UUID("7e6c1a34-9a2b-4c3d-8e5f-1029384756af")
OTHER_SERVER_ID = uuid.UUID("3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f")

# 43-char unpadded base64 body; same frozen fixture as the schema card.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

ADMIN_PASSWORD_MARKER = "super-secret-admin-marker-7f3a"
ENV_VALUE_MARKER = "super-secret-env-marker-9b2c"


def _profile_text(
    server_id: uuid.UUID = SERVER_ID, name: str = "build-host", privilege: str = "none"
) -> str:
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
privilege: {privilege}
capabilities:
  - shell.exec
  - files.read
"""


def _write_profile(path: Path, **kwargs: str) -> Path:
    path.write_text(_profile_text(**kwargs), encoding="utf-8")
    return path


def _err_of(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().err.strip())["error"]


def _bootstrap(root: Path, profile: Path, server_id: uuid.UUID = SERVER_ID) -> int:
    return main(
        [
            "bootstrap",
            "--data-root",
            str(root),
            "--server-id",
            str(server_id),
            "--profile",
            str(profile),
        ]
    )


def _provisioned_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], privilege: str = "none"
) -> tuple[Path, Path]:
    """One bootstrapped data root with a single canonical profile."""
    root = tmp_path / "data-root"
    profile = _write_profile(tmp_path / "profile.yaml", privilege=privilege)
    assert _bootstrap(root, profile) == 0
    capsys.readouterr()  # drain the bootstrap success line
    return root, profile


def _provisioned_short_root(capsys: pytest.CaptureFixture[str]) -> tuple[Path, Path]:
    """A bootstrapped data root under a SHORT temporary base path.

    AF_UNIX ``sun_path`` is capped at 108 bytes; the pytest tmp tree can
    exceed that, so unix-socket tests provision their data root under the
    system temp base instead. Returns ``(root, base)`` — the caller owns
    ``base`` cleanup (``shutil.rmtree``).
    """
    base = Path(tempfile.mkdtemp(prefix="mai-skt-", dir="/tmp" if os.path.isdir("/tmp") else None))
    root = base / "r"
    profile = _write_profile(base / "p.yaml")
    assert _bootstrap(root, profile) == 0
    capsys.readouterr()  # drain the bootstrap success line
    return root, base


# ---------------------------------------------------------------------------
# AC1 — registry: only documented commands, explicit data root, safe errors
# ---------------------------------------------------------------------------


class TestRegistryAndUsage:
    def test_help_of_installed_entrypoint_lists_exactly_the_documented_commands(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "mai.cli", "--help"],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "{" + ",".join(DOCUMENTED_COMMANDS) + "}" in proc.stdout

    def test_registered_commands_are_exactly_the_documented_set(self) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if action.dest == "command"
        )
        assert tuple(sorted(subparsers.choices)) == tuple(sorted(DOCUMENTED_COMMANDS))
        admin = subparsers.choices["admin"]
        admin_sub = next(action for action in admin._actions if action.dest == "admin_command")
        assert tuple(sorted(admin_sub.choices)) == ("delete", "write")

    @pytest.mark.parametrize("command", DOCUMENTED_COMMANDS)
    def test_every_command_requires_an_explicit_data_root(self, command: str) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if action.dest == "command"
        )
        option_strings = {
            option
            for choice in subparsers.choices.values()
            for action in choice._actions
            for option in action.option_strings
        }
        assert "--data-root" in option_strings
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args([command])
        assert excinfo.value.code == 2

    def test_no_command_offers_an_exec_shell_host_or_privilege_escape(self) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if action.dest == "command"
        )
        forbidden_prefixes = (
            "--exec",
            "--command",
            "--shell",
            "--sudo",
            "--root",
            "--as-root",
            "--remote",
            "--raw",
            "--run",
            "--host",
            "-e",
            "-c",
        )
        for choice in subparsers.choices.values():
            for action in choice._actions:
                for option in action.option_strings:
                    assert not option.startswith(forbidden_prefixes)
            nested = getattr(choice, "_subparsers", None)
            if nested is not None:
                nested_action = next(
                    action
                    for action in choice._actions
                    if action.dest == "admin_command"
                )
                for nested_choice in nested_action.choices.values():
                    for action in nested_choice._actions:
                        for option in action.option_strings:
                            assert not option.startswith(forbidden_prefixes)

    def test_unknown_command_is_usage_error_and_mutates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        with pytest.raises(SystemExit) as excinfo:
            main(["frobnicate", "--data-root", str(root)])
        assert excinfo.value.code == 2
        assert "invalid choice" in capsys.readouterr().err
        assert not root.exists()

    def test_missing_data_root_is_usage_error_and_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        with pytest.raises(SystemExit) as excinfo:
            main(["status", "--server-id", str(SERVER_ID)])
        assert excinfo.value.code == 2
        assert "--data-root" in capsys.readouterr().err
        assert not root.exists()


# ---------------------------------------------------------------------------
# bootstrap — create-only provisioning with rerun safety
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_bootstrap_creates_root_with_restrictive_permissions_and_profile(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        profile = _write_profile(tmp_path / "profile.yaml")
        assert _bootstrap(root, profile) == 0
        payload = json.loads(capsys.readouterr().out)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert payload["bootstrapped"] is True
        assert payload["revision"] == 1
        assert payload["schema_revision"] == SCHEMA_VERSION
        stored = ProfileFileStore(root).read(SERVER_ID)
        assert stored.revision == 1

    def test_bootstrap_rerun_is_refused_and_existing_pair_is_preserved(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, profile = _provisioned_root(tmp_path, capsys)
        before = ProfileFileStore(root).read(SERVER_ID)
        escalated = _write_profile(tmp_path / "escalated.yaml", privilege="root")
        assert _bootstrap(root, escalated) == 1
        error = _err_of(capsys)
        assert error["code"] == "stale_revision"
        after = ProfileFileStore(root).read(SERVER_ID)
        assert (after.revision, after.sha256, after.text) == (
            before.revision,
            before.sha256,
            before.text,
        )

    def test_bootstrap_with_invalid_profile_mutates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            _profile_text().replace("linux_ssh", "docker"), encoding="utf-8"
        )
        assert _bootstrap(root, bad) == 1
        error = _err_of(capsys)
        assert error["code"] == "profile_validation_failed"
        assert "environment" in error["fields"]
        assert not root.exists()

    def test_bootstrap_with_unreadable_profile_mutates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        assert _bootstrap(root, tmp_path / "absent.yaml") == 1
        assert _err_of(capsys)["code"] == "profile_file_not_readable"
        assert not root.exists()

    def test_bootstrap_with_invalid_server_id_is_structured(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        profile = _write_profile(tmp_path / "profile.yaml")
        assert main(
            [
                "bootstrap",
                "--data-root",
                str(root),
                "--server-id",
                "not-a-uuid",
                "--profile",
                str(profile),
            ]
        ) == 1
        assert _err_of(capsys)["code"] == "invalid_server_id"
        assert not root.exists()

    def test_privilege_escalation_requires_an_explicit_revision_checked_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        before = ProfileFileStore(root).read(SERVER_ID)
        escalated = _write_profile(tmp_path / "escalated.yaml", privilege="root")
        # A plain (revision-less) re-provision can never widen privilege:
        assert _bootstrap(root, escalated) == 1
        capsys.readouterr()
        # The only path is an explicit expected-revision admin write:
        assert (
            main(
                [
                    "admin",
                    "write",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(SERVER_ID),
                    "--profile",
                    str(escalated),
                    "--expect-revision",
                    "1",
                ]
            )
            == 0
        )
        after = ProfileFileStore(root).read(SERVER_ID)
        assert after.revision == 2
        assert after.revision == before.revision + 1


# ---------------------------------------------------------------------------
# doctor — non-mutating health report
# ---------------------------------------------------------------------------


class TestDoctor:
    def _doctor(self, root: Path, *extra: str) -> int:
        return main(["doctor", "--data-root", str(root), *extra])

    def test_healthy_root_reports_ok(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        assert self._doctor(root) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is True
        assert report["findings"] == []
        assert report["servers"][0]["server_id"] == str(SERVER_ID)

    def test_missing_root_is_reported_without_creation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        assert self._doctor(root) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is False
        assert report["findings"][0]["code"] == "root_missing"
        assert not root.exists()

    def test_tampered_content_is_reported_and_never_repaired(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        canonical = root / "servers" / str(SERVER_ID) / "profile.yaml"
        before = canonical.read_bytes()
        canonical.write_bytes(before + b"# drift\n")
        assert self._doctor(root) == 1
        report = json.loads(capsys.readouterr().out)
        codes = [finding["code"] for finding in report["findings"]]
        assert codes == ["hash_mismatch"]
        assert canonical.read_bytes() == before + b"# drift\n"  # reported, not repaired

    def test_orphan_metadata_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        server_dir = root / "servers" / str(SERVER_ID)
        (server_dir / "profile.meta.json").rename(tmp_path / "stash.meta.json")
        assert self._doctor(root) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["findings"][0]["code"] == "metadata_missing"

    def test_loose_root_permissions_are_flagged_and_not_silently_repaired(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        os.chmod(root, 0o755)
        assert self._doctor(root) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["findings"][0]["code"] == "permissions_too_open"
        assert stat.S_IMODE(root.stat().st_mode) == 0o755  # doctor never mutates

    def test_uninitialized_root_is_a_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        root.mkdir(mode=0o700)
        assert self._doctor(root) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["findings"][0]["code"] == "root_uninitialized"

    def test_env_file_check_is_value_free_ok_and_structured_on_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        env_file = tmp_path / "controller.env"
        env_file.write_text(f"MAI_SSH_KEY_VAULT={ENV_VALUE_MARKER}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        assert self._doctor(root, "--env-file", str(env_file)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["env_file"] == {"path": str(env_file), "ok": True}
        captured = capsys.readouterr()
        assert ENV_VALUE_MARKER not in captured.out + captured.err
        os.chmod(env_file, 0o644)
        assert self._doctor(root, "--env-file", str(env_file)) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["findings"][0]["code"] == "env_file_permissions_too_open"
        captured = capsys.readouterr()
        assert ENV_VALUE_MARKER not in captured.out + captured.err

    def test_doctor_output_is_value_free(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        assert self._doctor(root) == 0
        captured = capsys.readouterr()
        assert "build-host" not in captured.out
        assert "198.51.100.10" not in captured.out


# ---------------------------------------------------------------------------
# status — read-only inventory
# ---------------------------------------------------------------------------


class TestStatus:
    def test_inventory_reports_version_triples_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        assert main(["status", "--data-root", str(root)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert [s["server_id"] for s in payload["servers"]] == [str(SERVER_ID)]
        assert payload["servers"][0]["revision"] == 1
        captured = capsys.readouterr()
        assert "build-host" not in captured.out

    def test_single_server_matches_the_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        stored = ProfileFileStore(root).read(SERVER_ID)
        assert (
            main(["status", "--data-root", str(root), "--server-id", str(SERVER_ID)]) == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "server_id": str(SERVER_ID),
            "revision": stored.revision,
            "schema_revision": stored.schema_revision,
            "sha256": stored.sha256,
        }

    def test_unknown_server_is_structured_not_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        assert (
            main(
                [
                    "status",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(OTHER_SERVER_ID),
                ]
            )
            == 1
        )
        assert _err_of(capsys)["code"] == "not_found"

    def test_missing_root_fails_without_mutation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        assert main(["status", "--data-root", str(root)]) == 1
        assert _err_of(capsys)["code"] == "data_root_not_found"
        assert not root.exists()


# ---------------------------------------------------------------------------
# admin — explicit revision-controlled mutations
# ---------------------------------------------------------------------------


class TestAdmin:
    def test_admin_write_bumps_revision(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        renamed = _write_profile(tmp_path / "renamed.yaml", name="renamed-host")
        assert (
            main(
                [
                    "admin",
                    "write",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(SERVER_ID),
                    "--profile",
                    str(renamed),
                    "--expect-revision",
                    "1",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["revision"] == 2

    def test_stale_admin_write_is_refused_and_content_intact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, profile = _provisioned_root(tmp_path, capsys)
        before = ProfileFileStore(root).read(SERVER_ID)
        renamed = _write_profile(tmp_path / "renamed.yaml", name="renamed-host")
        assert (
            main(
                [
                    "admin",
                    "write",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(SERVER_ID),
                    "--profile",
                    str(renamed),
                    "--expect-revision",
                    "0",
                ]
            )
            == 1
        )
        assert _err_of(capsys)["code"] == "stale_revision"
        after = ProfileFileStore(root).read(SERVER_ID)
        assert (after.revision, after.text) == (before.revision, before.text)

    def test_invalid_admin_write_has_no_effect(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        before = ProfileFileStore(root).read(SERVER_ID)
        bad = tmp_path / "bad.yaml"
        bad.write_text(_profile_text().replace("none", "shell"), encoding="utf-8")
        assert (
            main(
                [
                    "admin",
                    "write",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(SERVER_ID),
                    "--profile",
                    str(bad),
                    "--expect-revision",
                    "1",
                ]
            )
            == 1
        )
        error = _err_of(capsys)
        assert error["code"] == "profile_validation_failed"
        assert "privilege" in error["fields"]
        after = ProfileFileStore(root).read(SERVER_ID)
        assert (after.revision, after.sha256) == (before.revision, before.sha256)

    def test_admin_delete_is_revision_checked_and_stale_refusal_keeps_data(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)

        def delete(expect_revision: str) -> list[str]:
            return [
                "admin",
                "delete",
                "--data-root",
                str(root),
                "--server-id",
                str(SERVER_ID),
                "--expect-revision",
                expect_revision,
            ]

        assert main(delete("7")) == 1
        assert _err_of(capsys)["code"] == "stale_revision"
        assert ProfileFileStore(root).read(SERVER_ID).revision == 1
        assert main(delete("1")) == 0
        assert json.loads(capsys.readouterr().out)["deleted"] is True
        assert (
            main(["status", "--data-root", str(root), "--server-id", str(SERVER_ID)]) == 1
        )
        assert _err_of(capsys)["code"] == "not_found"

    def test_admin_on_missing_root_is_refused_without_creation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        profile = _write_profile(tmp_path / "profile.yaml")
        assert (
            main(
                [
                    "admin",
                    "write",
                    "--data-root",
                    str(root),
                    "--server-id",
                    str(SERVER_ID),
                    "--profile",
                    str(profile),
                    "--expect-revision",
                    "0",
                ]
            )
            == 1
        )
        assert _err_of(capsys)["code"] == "data_root_not_found"
        assert not root.exists()


# ---------------------------------------------------------------------------
# backup / restore — file half with verified manifest (C05 rule 9: the DB
# rides the operator's single pg_dump, never this archive)
# ---------------------------------------------------------------------------


class TestBackupRestore:
    def test_backup_then_restore_reproduces_the_canonical_tree_exactly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["entries"] == 2  # profile.yaml + profile.meta.json
        assert len(payload["sha256"]) == 64
        before = ProfileFileStore(root).read(SERVER_ID)

        target = tmp_path / "restored-root"
        assert main(["restore", "--data-root", str(target), "--from", str(archive)]) == 0
        restored = json.loads(capsys.readouterr().out)
        assert restored["entries"] == 2
        after = ProfileFileStore(target).read(SERVER_ID)
        assert (after.revision, after.schema_revision, after.sha256, after.text) == (
            before.revision,
            before.schema_revision,
            before.sha256,
            before.text,
        )
        # The manifest is archive metadata, never data-root content:
        assert not (target / ARCHIVE_MANIFEST_NAME).exists()
        assert main(["doctor", "--data-root", str(target)]) == 0
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_restored_root_is_fully_uninstallable_with_delete_data(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A restored root contains only canonical entries — the archive
        manifest must never materialize as a foreign top-level entry."""
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
        capsys.readouterr()
        target = tmp_path / "restored-root"
        assert main(["restore", "--data-root", str(target), "--from", str(archive)]) == 0
        capsys.readouterr()
        assert sorted(entry.name for entry in target.iterdir()) == ["servers"]
        assert main(["uninstall", "--data-root", str(target), "--delete-data"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] is True
        assert payload["removed_entries"] == ["servers"]
        assert not target.exists()

    def test_backup_of_missing_root_writes_no_archive(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 1
        assert _err_of(capsys)["code"] == "data_root_not_found"
        assert not archive.exists()

    def test_backup_refuses_to_clobber_an_existing_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        archive.write_bytes(b"prior artifact")
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 1
        assert _err_of(capsys)["code"] == "output_exists"
        assert archive.read_bytes() == b"prior artifact"

    def test_backup_refuses_a_symlink_escape_without_following_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        outside = tmp_path / "outside.yaml"
        outside.write_text(_profile_text(), encoding="utf-8")
        link = root / "servers" / str(SERVER_ID) / "escape.yaml"
        link.symlink_to(outside)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 1
        assert _err_of(capsys)["code"] == "unexpected_entry"
        assert not archive.exists()
        assert link.is_symlink() and outside.exists()

    def test_backup_skips_a_dead_unix_socket_and_reports_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """t_8168ffeb: a stale op-daemon.sock (daemon exited, nothing
        listening) is skipped, not refused — the data root's steady state
        after container-restart cycles must stay backupable."""
        root, base = _provisioned_short_root(capsys)
        try:
            stale = root / "op-daemon.sock"  # top level: sun_path limit
            maker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            maker.bind(str(stale))
            maker.close()  # socket file remains, nothing listens: dead
            archive = tmp_path / "backup.tar.gz"
            assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
            payload = json.loads(capsys.readouterr().out)
            assert payload["skipped_dead_sockets"] == ["op-daemon.sock"]
            assert payload["entries"] == 2  # profile + meta only
            with tarfile.open(archive) as bundle:
                names = bundle.getnames()
                manifest = json.loads(bundle.extractfile("manifest.json").read())  # type: ignore[union-attr]
            assert "op-daemon.sock" not in names
            assert "manifest.json" in names
            # A dead socket is skipped in place, never removed by the backup.
            assert stale.is_socket()
            # The manifest records the skip so restore-time audits see it.
            assert manifest["skipped_dead_sockets"] == ["op-daemon.sock"]
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_backup_refuses_a_live_unix_socket(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A LIVE daemon's socket (a listener accepts the probe) is an
        operational fact the operator must see — backup still refuses."""
        root, base = _provisioned_short_root(capsys)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(root / "op-daemon.sock"))
        server.listen(1)
        try:
            archive = tmp_path / "backup.tar.gz"
            assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 1
            error = _err_of(capsys)
            assert error["code"] == "unexpected_entry"
            assert "op-daemon.sock" in str(error["message"])
            assert not archive.exists()
            assert (root / "op-daemon.sock").is_socket()
        finally:
            server.close()
            shutil.rmtree(base, ignore_errors=True)

    def test_backup_refuses_a_fifo_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FIFOs are not sockets and carry no liveness semantics — the
        non-regular refusal (C02 rule 3) keeps refusing them."""
        root, _ = _provisioned_root(tmp_path, capsys)
        fifo = root / "servers" / str(SERVER_ID) / "pipe"
        os.mkfifo(fifo)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 1
        assert _err_of(capsys)["code"] == "unexpected_entry"
        assert not archive.exists()

    def test_restore_refuses_a_dirty_target_without_mutation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
        capsys.readouterr()
        target = tmp_path / "busy-root"
        target.mkdir()
        (target / "precious.txt").write_bytes(b"operator data")
        assert main(["restore", "--data-root", str(target), "--from", str(archive)]) == 1
        assert _err_of(capsys)["code"] == "target_not_clean"
        assert (target / "precious.txt").read_bytes() == b"operator data"
        assert list(target.iterdir()) == [target / "precious.txt"]

    def test_restore_refuses_a_tampered_member_before_any_target_effect(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
        capsys.readouterr()

        tampered = tmp_path / "tampered.tar.gz"
        with tarfile.open(archive, "r:gz") as source:
            members = [(m, source.extractfile(m).read()) for m in source.getmembers()]
        with tarfile.open(tampered, "w:gz") as sink:
            for member, data in members:
                if member.name == ARCHIVE_MANIFEST_NAME:
                    sink.addfile(member, io.BytesIO(data))
                else:
                    drifted = b"tampered " + data
                    member.size = len(drifted)
                    sink.addfile(member, io.BytesIO(drifted))

        target = tmp_path / "restored-root"
        assert main(["restore", "--data-root", str(target), "--from", str(tampered)]) == 1
        assert _err_of(capsys)["code"] == "archive_invalid"
        assert not target.exists()  # verification precedes any target effect

    def test_restore_refuses_an_archive_without_manifest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        bare = tmp_path / "bare.tar.gz"
        profile_path = root / "servers" / str(SERVER_ID) / "profile.yaml"
        with tarfile.open(bare, "w:gz") as sink:
            data = profile_path.read_bytes()
            member = tarfile.TarInfo(name="servers/x/profile.yaml")
            member.size = len(data)
            member.mode = 0o600
            sink.addfile(member, io.BytesIO(data))
        target = tmp_path / "restored-root"
        assert main(["restore", "--data-root", str(target), "--from", str(bare)]) == 1
        assert _err_of(capsys)["code"] == "archive_invalid"
        assert not target.exists()


# ---------------------------------------------------------------------------
# uninstall — explicit keep/delete-data intent (C09 rule 5)
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_without_explicit_intent_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        with pytest.raises(SystemExit) as excinfo:
            main(["uninstall", "--data-root", str(root)])
        assert excinfo.value.code == 2
        assert "one of the arguments" in capsys.readouterr().err
        assert ProfileFileStore(root).read(SERVER_ID).revision == 1

    def test_keep_data_removes_nothing_and_prints_the_inventory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        before = ProfileFileStore(root).read(SERVER_ID)
        assert main(["uninstall", "--data-root", str(root), "--keep-data"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "uninstalled": True,
            "data_root": str(root),
            "deleted": False,
            "retained_entries": ["servers"],
        }
        assert ProfileFileStore(root).read(SERVER_ID) == before

    def test_delete_data_removes_the_owned_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        assert main(["uninstall", "--data-root", str(root), "--delete-data"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] is True
        assert payload["removed_entries"] == ["servers"]
        assert not root.exists()

    def test_delete_data_refuses_foreign_entries_and_deletes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        foreign_dir = root / "compose-project"
        foreign_dir.mkdir()
        (foreign_dir / "app-data.db").write_bytes(b"managed application data")
        (root / "operator-notes.txt").write_bytes(b"keep me")
        assert main(["uninstall", "--data-root", str(root), "--delete-data"]) == 1
        error = _err_of(capsys)
        assert error["code"] == "foreign_entries"
        assert "compose-project" in error["message"]
        assert "operator-notes.txt" in error["message"]
        assert ProfileFileStore(root).read(SERVER_ID).revision == 1
        assert (foreign_dir / "app-data.db").read_bytes() == b"managed application data"
        assert (root / "operator-notes.txt").read_bytes() == b"keep me"

    def test_uninstall_on_missing_root_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "data-root"
        assert main(["uninstall", "--data-root", str(root), "--keep-data"]) == 1
        assert _err_of(capsys)["code"] == "data_root_not_found"
        assert main(["uninstall", "--data-root", str(root), "--delete-data"]) == 1
        assert _err_of(capsys)["code"] == "data_root_not_found"
        assert not root.exists()


# ---------------------------------------------------------------------------
# secret non-disclosure across success and failure paths
# ---------------------------------------------------------------------------


class TestSecretNonDisclosure:
    def test_admin_password_env_value_never_reaches_any_output(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MAI_ADMIN_PASSWORD", ADMIN_PASSWORD_MARKER)
        root, profile = _provisioned_root(tmp_path, capsys)
        streams = [capsys.readouterr()]
        # Failure paths too:
        _write_profile(tmp_path / "again.yaml")
        assert _bootstrap(root, tmp_path / "again.yaml") == 1
        streams.append(capsys.readouterr())
        assert main(["doctor", "--data-root", str(root)]) == 0
        streams.append(capsys.readouterr())
        assert main(["status", "--data-root", str(root)]) == 0
        streams.append(capsys.readouterr())
        for captured in streams:
            assert ADMIN_PASSWORD_MARKER not in captured.out + captured.err

    def test_planted_profile_and_env_markers_never_surface_in_backup_or_restore(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _provisioned_root(tmp_path, capsys)
        archive = tmp_path / "backup.tar.gz"
        assert main(["backup", "--data-root", str(root), "--output", str(archive)]) == 0
        target = tmp_path / "restored-root"
        assert main(["restore", "--data-root", str(target), "--from", str(archive)]) == 0
        captured = capsys.readouterr()
        assert ENV_VALUE_MARKER not in captured.out + captured.err
        assert "198.51.100.10" not in captured.out + captured.err
