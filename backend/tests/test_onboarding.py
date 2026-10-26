"""Focused tests for the guided-onboarding building blocks (so-01).

Frozen contract: ``plans/server-onboarding/so_PLAN.md`` sections
"Controller public key" and "Side-effect-free connection test" plus
``so_01-onboarding-probe.md``. Coverage:

- **Pure/config**: the server-side default credential reference
  (``MAI_SSH_CREDENTIAL_REF`` override, ``MAI_SSH_KEY_MAI`` fallback).
- **Export**: real ``ssh-keygen -y`` derivation against real synthetic
  keypairs (cross-checked with ``ssh-keygen -lf``), value-free rejections
  for missing/invalid/unsupported/too-open keys, bounded output/time.
- **Pinned test**: fixed bounded privilege probes via faked subprocesses
  (password-required sudo, absent sudo, non-root uid, unsupported kernel)
  and the FULL real transport against a disposable Arch sshd container
  (``mai-fxt-so-*`` names, patterned after ``tests/test_ssh.py``): real
  discovery, real pinned identity probe, real sudo/uid checks, bad-key and
  changed-fingerprint negative controls, cleanup proof on every path.

Synthetic credentials only; nothing here touches production resources.
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.credentials.env import EnvErrorCode, EnvFileError, EnvResolver
from mai.execution.onboarding import (
    DEFAULT_CREDENTIAL_REF_VAR,
    FALLBACK_CREDENTIAL_REF,
    OnboardingError,
    OnboardingErrorCode,
    default_credential_ref,
    discover_host_key_candidates,
    export_public_key,
    resolve_default_identity_key,
    run_connection_test,
)
from mai.execution.ssh import target_fingerprint

#: Synthetic non-disclosure marker (NOT a secret): the fixture "private key"
#: file body used when a rejection must not leak file content.
KEY_SENTINEL = "mai-so-01-onboarding-fixture-key-body-not-a-secret"


def _identity(
    host: str = "127.0.0.1",
    port: int = 2222,
    host_key: str = "x",
    user: str = "root",
    credential_ref: str = FALLBACK_CREDENTIAL_REF,
) -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user=user,
        host_key=host_key,
        credential_ref=credential_ref,
    )


def _generate_key(path: Path) -> str:
    """Real synthetic ed25519 keypair; returns the public-key line."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path),
         "-C", "so-01-onboarding-test"],
        check=True, capture_output=True,
    )
    os.chmod(path, 0o600)
    return path.with_suffix(".pub").read_text(encoding="utf-8").strip()


def _write_env_file(path: Path, entries: dict[str, str]) -> EnvResolver:
    lines = "".join(f"{key}='{value}'\n" for key, value in entries.items())
    path.write_text(lines, encoding="utf-8")
    os.chmod(path, 0o600)
    return EnvResolver(path)


# ---------------------------------------------------------------------------
# Default credential reference (server-side configuration)
# ---------------------------------------------------------------------------


class TestDefaultCredentialRef:
    def test_fallback_applies_without_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(DEFAULT_CREDENTIAL_REF_VAR, raising=False)
        assert default_credential_ref({}) == FALLBACK_CREDENTIAL_REF
        monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                            if k != DEFAULT_CREDENTIAL_REF_VAR})
        assert default_credential_ref(None) == FALLBACK_CREDENTIAL_REF

    def test_override_wins_and_blank_falls_back(self) -> None:
        env = {DEFAULT_CREDENTIAL_REF_VAR: "MAI_SSH_KEY_DEPLOY"}
        assert default_credential_ref(env) == "MAI_SSH_KEY_DEPLOY"
        assert default_credential_ref({DEFAULT_CREDENTIAL_REF_VAR: "   "}) == (
            FALLBACK_CREDENTIAL_REF
        )


# ---------------------------------------------------------------------------
# Controller public-key export (real ssh-keygen, value-free rejections)
# ---------------------------------------------------------------------------


class TestExportPublicKey:
    def test_real_ed25519_export_matches_ssh_keygen(self, tmp_path: Path) -> None:
        pub_line = _generate_key(tmp_path / "controller_key")
        key_type, blob, _comment = pub_line.split()
        exported = export_public_key(tmp_path / "controller_key")
        # ssh-keygen -y reproduces the stored comment, so the exported line
        # is the validated full public-key line.
        assert exported.public_key == pub_line
        assert exported.key_type == "ssh-ed25519"
        # Cross-check the fingerprint against the real ssh-keygen binary.
        keyfile = tmp_path / "pub.check"
        keyfile.write_text(exported.public_key + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0
        assert exported.fingerprint == keygen.stdout.split()[1]
        assert exported.fingerprint.startswith("SHA256:")

    def test_real_rsa_export_supported(self, tmp_path: Path) -> None:
        key_path = tmp_path / "rsa_key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "",
             "-f", str(key_path), "-C", "so-01-rsa"],
            check=True, capture_output=True,
        )
        os.chmod(key_path, 0o600)
        exported = export_public_key(key_path)
        assert exported.key_type == "ssh-rsa"

    def test_missing_key_file_rejected_value_free(self, tmp_path: Path) -> None:
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(tmp_path / "absent")
        assert excinfo.value.code is OnboardingErrorCode.KEY_EXPORT_FAILED
        # No local path and no raw stderr reaches the detail.
        assert str(tmp_path) not in excinfo.value.detail
        assert "ssh-keygen:" not in excinfo.value.detail

    def test_non_key_file_rejected(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus"
        bogus.write_text(KEY_SENTINEL, encoding="utf-8")
        os.chmod(bogus, 0o600)
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(bogus)
        assert excinfo.value.code is OnboardingErrorCode.KEY_EXPORT_FAILED
        assert KEY_SENTINEL not in excinfo.value.detail

    def test_too_open_permissions_rejected(self, tmp_path: Path) -> None:
        key_path = tmp_path / "open_key"
        _generate_key(key_path)
        os.chmod(key_path, 0o644)  # OpenSSH refuses unprotected private keys
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(key_path)
        assert excinfo.value.code is OnboardingErrorCode.KEY_EXPORT_FAILED

    def test_unsupported_key_type_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(_command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(_command, 0, "weird-key AAAA\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(Path("/unused"))
        assert excinfo.value.code is OnboardingErrorCode.KEY_EXPORT_FAILED

    def test_multiline_output_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(_command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                _command, 0, "ssh-ed25519 AAAA\nsecond line\n", ""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(Path("/unused"))
        assert excinfo.value.code is OnboardingErrorCode.KEY_EXPORT_FAILED

    def test_output_budget_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        big = "ssh-ed25519 " + "A" * (8 * 1024 + 1)
        monkeypatch.setattr(
            subprocess, "run",
            lambda _c, **_k: subprocess.CompletedProcess(_c, 0, big, ""),
        )
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(Path("/unused"))
        assert excinfo.value.code is OnboardingErrorCode.BOUNDED_OUTPUT_EXCEEDED

    def test_timeout_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(_c: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="ssh-keygen", timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(OnboardingError) as excinfo:
            export_public_key(Path("/unused"))
        assert excinfo.value.code is OnboardingErrorCode.TIMEOUT


# ---------------------------------------------------------------------------
# Default reference resolution through the real layered resolver (C03.6)
# ---------------------------------------------------------------------------


class TestResolveDefaultIdentityKey:
    def test_fallback_reference_resolves_to_existing_file(self, tmp_path: Path) -> None:
        key_path = tmp_path / "controller_key"
        _generate_key(key_path)
        resolver = _write_env_file(tmp_path / "credentials.env",
                                   {FALLBACK_CREDENTIAL_REF: str(key_path)})
        assert resolve_default_identity_key(resolver, {}) == key_path

    def test_override_reference_wins(self, tmp_path: Path) -> None:
        fallback_key = tmp_path / "fallback_key"
        deploy_key = tmp_path / "deploy_key"
        _generate_key(fallback_key)
        _generate_key(deploy_key)
        resolver = _write_env_file(
            tmp_path / "credentials.env",
            {FALLBACK_CREDENTIAL_REF: str(fallback_key),
             "MAI_SSH_KEY_DEPLOY": str(deploy_key)},
        )
        env = {DEFAULT_CREDENTIAL_REF_VAR: "MAI_SSH_KEY_DEPLOY"}
        assert resolve_default_identity_key(resolver, env) == deploy_key

    def test_missing_reference_raises_env_error(self, tmp_path: Path) -> None:
        resolver = _write_env_file(tmp_path / "credentials.env", {"OTHER": "/x"})
        with pytest.raises(EnvFileError) as excinfo:
            resolve_default_identity_key(resolver, {})
        assert excinfo.value.code is EnvErrorCode.MISSING_KEY

    def test_unresolvable_key_file_is_invalid_configuration(self, tmp_path: Path) -> None:
        resolver = _write_env_file(
            tmp_path / "credentials.env", {FALLBACK_CREDENTIAL_REF: str(tmp_path / "gone")}
        )
        with pytest.raises(OnboardingError) as excinfo:
            resolve_default_identity_key(resolver, {})
        assert excinfo.value.code is OnboardingErrorCode.INVALID_CONFIGURATION


# ---------------------------------------------------------------------------
# Privilege probes and failure mapping (faked subprocess, real orchestration)
# ---------------------------------------------------------------------------


_LINUX_IDENTITY_OUT = (
    "kernel_name=Linux\nkernel_release=6.12.1-arch1-1\n"
    "machine=x86_64\nhostname=probe-target\nboot_id=fake-boot-id\n"
    "os_id=arch\nos_pretty_name=Arch Linux\n"
)


def _fake_transport(monkeypatch: pytest.MonkeyPatch, fingerprint: str, script: dict[str, object]) -> None:
    """Fake subprocess.run: keyscan output first, then scripted ssh replies.

    ``script`` maps the remote command to either ``(rc, stdout)`` or an
    exception instance; the fixed identity probe answers with a valid
    Linux identity unless overridden.
    """
    scan_line = f"[127.0.0.1]:2222 ssh-ed25519 {_blob_for(fingerprint)}"

    def fake_run(command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ssh-keyscan":
            return subprocess.CompletedProcess(command, 0, scan_line + "\n", "")
        remote = command[-1]
        outcome = script.get(remote, (0, _LINUX_IDENTITY_OUT))
        if isinstance(outcome, Exception):
            raise outcome
        rc, out = outcome  # type: ignore[misc]
        return subprocess.CompletedProcess(command, rc, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _blob_for(fingerprint: str) -> str:
    """The (fixed) base64 blob whose SHA256 fingerprint equals the pin."""
    blob = base64.b64decode(_BLOB_BY_FINGERPRINT[fingerprint])
    return base64.b64encode(blob).decode("ascii")


_BLOB_BY_FINGERPRINT: dict[str, str] = {}


def _register_blob(fingerprint: str, blob_b64: str) -> str:
    _BLOB_BY_FINGERPRINT[fingerprint] = blob_b64
    return fingerprint


def _make_fingerprint(seed: bytes) -> str:
    blob_b64 = base64.b64encode(seed).decode("ascii")
    return _register_blob(target_fingerprint("ssh-ed25519", blob_b64), blob_b64)


_PIN_A = _make_fingerprint(b"A" * 32)
_PIN_B = _make_fingerprint(b"B" * 32)


class TestRunConnectionTestFakes:
    """Bounded privilege outcomes that need no live target."""

    def test_password_required_sudo_is_privilege_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_transport(
            monkeypatch, _PIN_A,
            {"sudo -n true": (1, ""), "id -u": (0, "1000\n")},
        )
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key=_PIN_A),
                privilege=PrivilegeMode.SUDO,
                key_path=tmp_path / "k",
                tmp_dir=tmp_path,
            )
        assert excinfo.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE
        # Cleanup on failure: no temporary known-hosts file survived.
        assert list(tmp_path.iterdir()) == []

    def test_absent_sudo_rc_127_is_privilege_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_transport(monkeypatch, _PIN_A, {"sudo -n true": (127, "")})
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key=_PIN_A),
                privilege=PrivilegeMode.SUDO,
                key_path=tmp_path / "k",
                tmp_dir=tmp_path,
            )
        assert excinfo.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE

    def test_non_root_uid_is_privilege_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_transport(monkeypatch, _PIN_A, {"id -u": (0, "1000\n")})
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key=_PIN_A),
                privilege=PrivilegeMode.ROOT,
                key_path=tmp_path / "k",
                tmp_dir=tmp_path,
            )
        assert excinfo.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE

    def test_none_privilege_runs_no_remote_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_remote: list[str] = []

        def fake_run(command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "ssh":
                seen_remote.append(command[-1])
                return subprocess.CompletedProcess(command, 0, _LINUX_IDENTITY_OUT, "")
            scan = f"[127.0.0.1]:2222 ssh-ed25519 {_blob_for(_PIN_A)}"
            return subprocess.CompletedProcess(command, 0, scan + "\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = run_connection_test(
            _identity(host_key=_PIN_A),
            privilege=PrivilegeMode.NONE,
            key_path=tmp_path / "k",
            tmp_dir=tmp_path,
        )
        assert seen_remote and "sudo" not in " ".join(seen_remote)
        assert result.identity.kernel_name == "Linux"
        # The temporary known-hosts file was removed on success.
        assert not list(tmp_path.glob("mai-onboarding-known-hosts-*"))

    def test_unsupported_kernel_is_environment_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "ssh":
                out = (
                    "kernel_name=Darwin\nkernel_release=24.0.0\n"
                    "machine=arm64\nhostname=mac\nboot_id=x\n"
                )
                return subprocess.CompletedProcess(command, 0, out, "")
            scan = f"[127.0.0.1]:2222 ssh-ed25519 {_blob_for(_PIN_A)}"
            return subprocess.CompletedProcess(command, 0, scan + "\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key=_PIN_A),
                privilege=PrivilegeMode.NONE,
                key_path=tmp_path / "k",
                tmp_dir=tmp_path,
            )
        assert excinfo.value.code is OnboardingErrorCode.ENVIRONMENT_UNSUPPORTED

    def test_malformed_fingerprint_rejected_before_any_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("no subprocess may run for a malformed pin")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key="not-a-fingerprint"),
                privilege=PrivilegeMode.NONE,
                key_path=tmp_path / "k",
            )
        assert excinfo.value.code is OnboardingErrorCode.INVALID_CONFIGURATION


class TestTempKnownHostsLifecycle:
    """The temp pin file: closed descriptor, bounded rescan, full cleanup."""

    def test_descriptor_closed_and_file_removed_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        captured: dict[str, int] = {}
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            captured["fd"] = fd
            return fd, name

        monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
        _fake_transport(monkeypatch, _PIN_A, {})
        result = run_connection_test(
            _identity(host_key=_PIN_A),
            privilege=PrivilegeMode.NONE,
            key_path=tmp_path / "k",
            tmp_dir=tmp_path,
        )
        assert result.host_key == _PIN_A
        # The descriptor was closed immediately after mkstemp, not leaked
        # for the lifetime of the call.
        with pytest.raises(OSError):
            os.fstat(captured["fd"])
        # Success cleanup: no temporary known-hosts file survived.
        assert list(tmp_path.iterdir()) == []

    def test_descriptor_closed_and_file_removed_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        captured: dict[str, int] = {}
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            captured["fd"] = fd
            return fd, name

        monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
        _fake_transport(monkeypatch, _PIN_A, {"sudo -n true": (1, "")})
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                _identity(host_key=_PIN_A),
                privilege=PrivilegeMode.SUDO,
                key_path=tmp_path / "k",
                tmp_dir=tmp_path,
            )
        assert excinfo.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE
        with pytest.raises(OSError):
            os.fstat(captured["fd"])
        assert list(tmp_path.iterdir()) == []

    def test_request_timeout_bounds_the_pin_rescan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}
        scan_line = f"[127.0.0.1]:2222 ssh-ed25519 {_blob_for(_PIN_A)}"

        def fake_run(command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "ssh-keyscan":
                seen["-T"] = command[command.index("-T") + 1]
                return subprocess.CompletedProcess(command, 0, scan_line + "\n", "")
            return subprocess.CompletedProcess(command, 0, _LINUX_IDENTITY_OUT, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        run_connection_test(
            _identity(host_key=_PIN_A),
            privilege=PrivilegeMode.NONE,
            key_path=tmp_path / "k",
            timeout=7,
            tmp_dir=tmp_path,
        )
        assert seen["-T"] == "7"


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture (mai-fxt-so-* namespace; so-01 owns it)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-so-01-ssh-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-so-01-ssh-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-so-01-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_SO01_FIXTURE"
FIXTURE_LIMITED_REF = "MAI_SSH_KEY_SO01_LIMITED"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


_image_ensured = False


def _ensure_fixture_image() -> str:
    """The disposable Arch+openssh+sudo image, task-owned.

    The ``mai-fxt-04`` image is NOT reused even when present: it installs
    openssh only, so the bounded ``sudo -n true`` probe would fail there
    (observed) and password-required-sudo coverage would silently depend
    on another suite's image contents. This task's image is self-contained
    and read-only for everyone else.
    """
    global _image_ensured
    if _image_ensured and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return FIXTURE_IMAGE_TAG
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
        "&& ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True
    return FIXTURE_IMAGE_TAG


class SoSshdFixture:
    """One disposable Arch sshd container; removes itself with proof."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.limited_key_path = self.scratch / "limited_key"
        self.port = 0
        self.cleanup_proof = "<not cleaned>"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> SshIdentity:
        for key_path in (self.key_path, self.limited_key_path):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path),
                 "-C", f"{FIXTURE_CONTAINER}-test"],
                check=True, capture_output=True,
            )
            os.chmod(key_path, 0o600)
        image = _ensure_fixture_image()
        run = _docker(
            "run", "--rm", "-d", "--name", FIXTURE_CONTAINER,
            "-p", "127.0.0.1::22",
            image,
            "sh", "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._provision_users()
        self._wait_keyscan()
        return self.identity_for(self._current_fingerprint())

    def _refresh_port(self) -> None:
        port_out = _docker(
            "inspect", "--format",
            "{{(index (index .NetworkSettings.Ports \"22/tcp\") 0).HostPort}}",
            FIXTURE_CONTAINER,
        )
        assert port_out.returncode == 0 and port_out.stdout.strip().isdigit(), (
            f"fixture port inspect failed: {port_out.stderr[-400:]}"
        )
        self.port = int(port_out.stdout.strip())

    def _provision_users(self) -> None:
        root_pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
             "&& cat > /root/.ssh/authorized_keys "
             "&& chmod 600 /root/.ssh/authorized_keys"],
            input=root_pub, capture_output=True, timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]
        limited_pub = self.limited_key_path.with_suffix(".pub").read_bytes()
        limited = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             "useradd -m -s /bin/bash limited "
             "&& mkdir -p /home/limited/.ssh && chmod 700 /home/limited/.ssh "
             "&& cat > /home/limited/.ssh/authorized_keys "
             "&& chmod 600 /home/limited/.ssh/authorized_keys "
             "&& chown -R limited:limited /home/limited/.ssh "
             "&& echo 'limited ALL=(ALL) ALL' >> /etc/sudoers"],
            input=limited_pub, capture_output=True, timeout=30,
        )
        assert limited.returncode == 0, limited.stderr.decode()[-800:]

    def identity_for(self, host_key: str, user: str = "root") -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user=user,
            host_key=host_key,
            credential_ref=(
                FIXTURE_CREDENTIAL_REF if user == "root" else FIXTURE_LIMITED_REF
            ),
        )

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True, text=True, timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        """Fixture value cross-checked against the real ssh-keygen."""
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        ed25519 = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        fingerprint = keygen.stdout.split()[1]
        computed = target_fingerprint(ed25519.split()[1], ed25519.split()[2])
        assert computed == fingerprint, (
            f"pure fingerprint {computed} disagrees with ssh-keygen {fingerprint}"
        )
        return fingerprint

    def candidates(self) -> dict[str, str]:
        """Parsed {key_type: fingerprint} of everything the target presents."""
        found: dict[str, str] = {}
        for line in self._scan_now():
            parts = line.split()
            if len(parts) == 3:
                found[parts[1]] = target_fingerprint(parts[1], parts[2])
        return found

    def _wait_keyscan(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self._scan_now():
                    return
                last_error = "empty keyscan"
            except subprocess.TimeoutExpired as error:
                last_error = str(error)
            time.sleep(1.0)
        raise AssertionError(f"fixture sshd never became ready: {last_error}")

    def env_resolver(self) -> EnvResolver:
        return _write_env_file(
            self.scratch / "fixture.env",
            {FIXTURE_CREDENTIAL_REF: str(self.key_path),
             FIXTURE_LIMITED_REF: str(self.limited_key_path)},
        )

    def scratch_files(self) -> set[str]:
        return {entry.name for entry in self.scratch.iterdir()}

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$",
            "--format", "{{.Names}}",
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: "
            f"'{listing.stdout.strip() or '<none>'}'; "
            f"scratch {self.scratch} exists: {self.scratch.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture()
def so_sshd() -> SoSshdFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: the so-01 sshd fixture tests require it")
    fixture = SoSshdFixture()
    try:
        fixture.identity = fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.mark.real_target
class TestAgainstSoFixture:
    """Real transport: discovery, pinned probe, privilege, negative controls."""

    def test_discovery_returns_all_candidates_without_writes(
        self, so_sshd: SoSshdFixture
    ) -> None:
        presented = so_sshd.candidates()
        assert "ssh-ed25519" in presented and "ssh-rsa" in presented
        before = so_sshd.scratch_files()
        scanned = discover_host_key_candidates(so_sshd.identity)
        fingerprints = {key.fingerprint for key in scanned}
        # Every presented key is a candidate; none is collapsed away.
        assert fingerprints == set(presented.values())
        assert {key.key_type for key in scanned} == set(presented)
        # Side-effect-free: nothing appeared on disk around the call.
        assert so_sshd.scratch_files() == before

    def test_pinned_test_reports_real_linux_identity_and_privilege(
        self, so_sshd: SoSshdFixture
    ) -> None:
        resolver = so_sshd.env_resolver()
        key_path = resolve_default_identity_key(
            resolver, {DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_CREDENTIAL_REF}
        )
        pin = so_sshd.identity.host_key
        tmp_dir = so_sshd.scratch / "tmp"
        tmp_dir.mkdir()
        for privilege in (PrivilegeMode.NONE, PrivilegeMode.SUDO, PrivilegeMode.ROOT):
            result = run_connection_test(
                so_sshd.identity_for(pin),
                privilege=privilege,
                key_path=key_path,
                tmp_dir=tmp_dir,
            )
            assert result.host_key == pin
            assert result.key_type == "ssh-ed25519"
            assert result.privilege is privilege
            # Observed facts from the REAL target, not controller inference.
            assert result.identity.kernel_name == "Linux"
            assert result.identity.os_id == "arch"
            assert result.identity.boot_id
            assert result.identity.hostname
        # Cleanup on success: no temporary known-hosts file survived.
        assert not list(tmp_dir.iterdir())

    def test_bad_key_fails_authentication(self, so_sshd: SoSshdFixture) -> None:
        stranger = so_sshd.scratch / "stranger_key"
        _generate_key(stranger)  # never installed on the target
        resolver = _write_env_file(
            so_sshd.scratch / "stranger.env", {FIXTURE_CREDENTIAL_REF: str(stranger)}
        )
        key_path = resolve_default_identity_key(
            resolver, {DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_CREDENTIAL_REF}
        )
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                so_sshd.identity,
                privilege=PrivilegeMode.NONE,
                key_path=key_path,
                tmp_dir=so_sshd.scratch,
            )
        assert excinfo.value.code is OnboardingErrorCode.AUTH_FAILED

    def test_changed_fingerprint_refused_without_writes(
        self, so_sshd: SoSshdFixture
    ) -> None:
        other = _generate_key(so_sshd.scratch / "unused-keypair")
        stale_pin = target_fingerprint(other.split()[0], other.split()[1])
        before = so_sshd.scratch_files()
        with pytest.raises(OnboardingError) as excinfo:
            run_connection_test(
                so_sshd.identity_for(stale_pin),
                privilege=PrivilegeMode.NONE,
                key_path=so_sshd.key_path,
                tmp_dir=so_sshd.scratch,
            )
        assert excinfo.value.code is OnboardingErrorCode.HOST_KEY_MISMATCH
        # Nothing was written by the refused attempt.
        assert so_sshd.scratch_files() == before

    def test_password_required_sudo_and_non_root_are_distinct(
        self, so_sshd: SoSshdFixture
    ) -> None:
        resolver = so_sshd.env_resolver()
        limited_key = resolve_default_identity_key(resolver, {DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_LIMITED_REF})
        limited_pin = so_sshd.identity.host_key
        with pytest.raises(OnboardingError) as sudo_exc:
            run_connection_test(
                so_sshd.identity_for(limited_pin, user="limited"),
                privilege=PrivilegeMode.SUDO,
                key_path=limited_key,
                tmp_dir=so_sshd.scratch,
            )
        assert sudo_exc.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE
        with pytest.raises(OnboardingError) as root_exc:
            run_connection_test(
                so_sshd.identity_for(limited_pin, user="limited"),
                privilege=PrivilegeMode.ROOT,
                key_path=limited_key,
                tmp_dir=so_sshd.scratch,
            )
        assert root_exc.value.code is OnboardingErrorCode.PRIVILEGE_UNAVAILABLE
        assert sudo_exc.value.detail != root_exc.value.detail


class TestUnreachableRealTransport:
    """A genuinely closed loopback endpoint through the real ssh-keyscan."""

    def test_unreachable_port_maps_to_unreachable(self, tmp_path: Path) -> None:
        # A bound-then-closed loopback port refuses immediately; keyscan
        # answers with no host keys, which the transport maps to
        # unreachable — the honest "nothing to test there" outcome.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        with pytest.raises(OnboardingError) as excinfo:
            discover_host_key_candidates(
                _identity(host="127.0.0.1", port=closed_port),
                timeout=5,
            )
        assert excinfo.value.code is OnboardingErrorCode.UNREACHABLE
