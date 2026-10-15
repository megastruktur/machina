"""Focused tests for the SSH transport (card ``04-ssh-connect``, C04).

Acceptance coverage:

- **AC1** — transport is the system OpenSSH client with exactly the frozen
  probe option surface (04-dependencies); the profile host-key pin is
  verified before anything is trusted and a mismatch/changed key is refused
  with nothing written (SSH-PIN negative control, real fixture); the
  credential reference resolves through the real ``.env`` resolver
  (C03 rule 6) and key material never reaches any error/repr path.
- **AC2** — the returned target identity derives only from the target's own
  reports; a non-Linux target is refused (``environment_unsupported``)
  instead of described by local assumptions, and the real fixture run
  returns the actual Arch target identity.

Two layers, per the test policy ("use a real host when the claim depends on
it"):

- pure/subprocess-mapped unit tests (always run, no docker);
- fixture-backed tests against a disposable Arch sshd container
  (``mai-fxt-04-ssh-arch``, namespace rule D3), auto-skipped when docker is
  unavailable on the host. The fixture is producer-owned and removed with
  recorded proof (runtime-environment ADR D4).

Secret hygiene: every credential below is a throwaway fixture key generated
in ``/tmp/mai-fxt-04-ssh-*`` and deleted afterwards; the ``.env`` holds a
PATH to it, never material; a sentinel marker proves non-disclosure.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvErrorCode, EnvFileError, EnvResolver
from mai.execution.ssh import (
    CONNECT_TIMEOUT_SECONDS,
    PinnedHostKey,
    SshConnectError,
    SshErrorCode,
    TargetIdentity,
    parse_identity_output,
    parse_scan_output,
    pin_host_key,
    probe_environment,
    resolve_identity_key,
    scan_host_keys,
    ssh_arguments,
    target_fingerprint,
)

#: Synthetic non-disclosure marker (NOT a secret): the fixture "private key"
#: file body. Any error/repr path leaking file content would surface it.
KEY_SENTINEL = "mai-04-ssh-connect-fixture-key-body-not-a-secret"


def _identity(host: str = "203.0.113.10", port: int = 2222, host_key: str = "x") -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user="root",
        host_key=host_key,
        credential_ref="MAI_SSH_KEY_TEST",
    )


# ---------------------------------------------------------------------------
# Pure helpers: fingerprints, scan parsing, argv surface
# ---------------------------------------------------------------------------


class TestFingerprints:
    def test_fingerprint_matches_openssh_sha256_format(self) -> None:
        blob = base64.b64encode(b"mai-04-ssh-connect-key-blob").decode()
        fp = target_fingerprint("ssh-ed25519", blob)
        digest = hashlib.sha256(b"mai-04-ssh-connect-key-blob").digest()
        expected = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
        assert fp == expected
        assert len(fp) == len("SHA256:") + 43

    def test_invalid_base64_rejected(self) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            target_fingerprint("ssh-ed25519", "!!!not-base64!!!")
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION


class TestScanParsing:
    def test_lines_parsed_and_banners_dropped(self) -> None:
        blob = base64.b64encode(b"blob-one").decode()
        raw = (
            "# 203.0.113.10 SSH-2.0-OpenSSH_10.0\n"
            f"[203.0.113.10]:2222 ssh-ed25519 {blob}\n"
            "scan warning line without three fields\n"
        )
        scanned = parse_scan_output(raw)
        assert len(scanned) == 1
        assert scanned[0].marker == "[203.0.113.10]:2222"
        assert scanned[0].key_type == "ssh-ed25519"
        assert scanned[0].known_hosts_line == f"[203.0.113.10]:2222 ssh-ed25519 {blob}"

    def test_multiple_key_types_preserved(self) -> None:
        ed = base64.b64encode(b"ed").decode()
        rsa = base64.b64encode(b"rsa").decode()
        scanned = parse_scan_output(f"h ssh-ed25519 {ed}\nh ssh-rsa {rsa}\n")
        assert [key.key_type for key in scanned] == ["ssh-ed25519", "ssh-rsa"]


class TestArgvSurface:
    def test_exactly_the_frozen_probe_option_surface(self) -> None:
        identity = _identity(host="build.example", port=2200)
        argv = ssh_arguments(
            identity,
            key_path=Path("/keys/k"),
            known_hosts_path=Path("/kh/known_hosts"),
            remote_command="true",
        )
        assert argv[0] == "ssh"
        assert argv[argv.index("-p") + 1] == "2200"
        assert argv[argv.index("-i") + 1] == "/keys/k"
        options = [argv[i + 1] for i, part in enumerate(argv) if part == "-o"]
        assert options == [
            "IdentitiesOnly=yes",
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=/kh/known_hosts",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        ]
        assert argv[-2] == "root@build.example"
        assert argv[-1] == "true"

    def test_bracketed_ipv6_normalized_for_transport(self) -> None:
        identity = _identity(host="[2001:db8::10]", port=22)
        argv = ssh_arguments(
            identity,
            key_path=Path("/keys/k"),
            known_hosts_path=Path("/kh"),
            remote_command="true",
        )
        assert argv[-2] == "root@2001:db8::10"

    def test_keyscan_argv_normalizes_bracketed_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, list[str]] = {}

        class _FakeProc:
            returncode = 0
            stdout = "h ssh-ed25519 AAAAAQ==\n"
            stderr = ""

        def fake_run(command: list[str], **_kwargs: object) -> _FakeProc:
            captured["argv"] = command
            return _FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        scanned = scan_host_keys(_identity(host="[2001:db8::10]", port=2200))
        assert captured["argv"][0] == "ssh-keyscan"
        assert captured["argv"][captured["argv"].index("-p") + 1] == "2200"
        assert captured["argv"][-1] == "2001:db8::10"
        assert scanned[0].marker == "h"


# ---------------------------------------------------------------------------
# Enrollment path: pin, refuse, never half-write (SSH-PIN)
# ---------------------------------------------------------------------------


class TestPinHostKey:
    def _scanned(self, blob: bytes, marker: str = "[203.0.113.10]:2222") -> str:
        return f"{marker} ssh-ed25519 {base64.b64encode(blob).decode()}\n"

    def test_mismatch_refused_and_nothing_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        known_hosts = tmp_path / "known_hosts"
        pinned = target_fingerprint("ssh-ed25519", base64.b64encode(b"admin-pinned").decode())
        identity = _identity(host_key=pinned)
        monkeypatch.setattr(
            "mai.execution.ssh.scan_host_keys",
            lambda *_a, **_k: parse_scan_output(self._scanned(b"rotated-on-target")),
        )
        with pytest.raises(SshConnectError) as excinfo:
            pin_host_key(identity, known_hosts)
        assert excinfo.value.code is SshErrorCode.HOST_KEY_MISMATCH
        assert not known_hosts.exists()
        assert not (tmp_path / "known_hosts.tmp").exists()

    def test_match_writes_single_pinned_line_mode_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        known_hosts = tmp_path / "sub" / "known_hosts"
        blob = b"the-admin-accepted-key"
        pinned = target_fingerprint("ssh-ed25519", base64.b64encode(blob).decode())
        identity = _identity(host_key=pinned)
        raw = self._scanned(b"other-host-key") + self._scanned(blob)
        monkeypatch.setattr(
            "mai.execution.ssh.scan_host_keys",
            lambda *_a, **_k: parse_scan_output(raw),
        )
        pinned_result = pin_host_key(identity, known_hosts)
        assert isinstance(pinned_result, PinnedHostKey)
        assert pinned_result.fingerprint == pinned
        lines = known_hosts.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # only the pinned key is recorded, never the rest
        assert base64.b64encode(blob).decode() in lines[0]
        tmp_leftover = known_hosts.with_name("known_hosts.tmp")
        assert not tmp_leftover.exists()
        assert stat.S_IMODE(known_hosts.stat().st_mode) == 0o600

    def test_malformed_profile_fingerprint_rejected_before_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> tuple[()]:
            raise AssertionError("keyscan must not run for a malformed pin")

        monkeypatch.setattr("mai.execution.ssh.scan_host_keys", boom)
        with pytest.raises(SshConnectError) as excinfo:
            pin_host_key(_identity(host_key="SHA256:tooshort"), tmp_path / "known_hosts")
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION


# ---------------------------------------------------------------------------
# Credential reference resolution through the real .env resolver (C03.6)
# ---------------------------------------------------------------------------


class TestResolveIdentityKey:
    def test_resolves_path_reference_and_requires_existing_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        key_file = tmp_path / "fixture_key"
        key_file.write_text(KEY_SENTINEL, encoding="utf-8")
        os.chmod(key_file, 0o600)
        env_file.write_text(f"MAI_SSH_KEY_TEST={key_file}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        resolver = EnvResolver(env_file)
        resolved = resolve_identity_key(resolver, "MAI_SSH_KEY_TEST")
        assert resolved == key_file
        # The module never opens the key: the sentinel body stays unread.
        assert key_file.read_text(encoding="utf-8") == KEY_SENTINEL

    def test_missing_key_file_fails_closed(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MAI_SSH_KEY_TEST=/nonexistent/fixture_key\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        with pytest.raises(SshConnectError) as excinfo:
            resolve_identity_key(EnvResolver(env_file), "MAI_SSH_KEY_TEST")
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION
        # The .env VALUE (a path) may appear; it is configuration, not material.
        assert KEY_SENTINEL not in str(excinfo.value)

    def test_resolver_errors_propagate_structured(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_NAME=x\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        with pytest.raises(EnvFileError) as excinfo:
            resolve_identity_key(EnvResolver(env_file), "MAI_SSH_KEY_TEST")
        assert excinfo.value.code is EnvErrorCode.MISSING_KEY


# ---------------------------------------------------------------------------
# Identity parsing: target-reported facts only (acceptance 2)
# ---------------------------------------------------------------------------

_ARCH_LIKE_OUTPUT = (
    "kernel_name=Linux\n"
    "kernel_release=6.12.8-arch1-1\n"
    "machine=x86_64\n"
    "hostname=mai-fxt-04-ssh-arch\n"
    "boot_id=3f2e4d1a-0000-0000-0000-000000000001\n"
    'os_pretty_name="Arch Linux"\n'
    "os_id=arch\n"
)

_BUSYBOX_LIKE_OUTPUT = (
    "kernel_name=Linux\n"
    "kernel_release=6.1.0\n"
    "machine=aarch64\n"
    "hostname=fixture\n"
    "boot_id=\n"
)


class TestParseIdentityOutput:
    def test_arch_reports_parsed_with_quotes_stripped(self) -> None:
        identity = parse_identity_output(_ARCH_LIKE_OUTPUT)
        assert identity == TargetIdentity(
            hostname="mai-fxt-04-ssh-arch",
            kernel_name="Linux",
            kernel_release="6.12.8-arch1-1",
            machine="x86_64",
            os_id="arch",
            os_version_id=None,
            os_pretty_name="Arch Linux",
            boot_id="3f2e4d1a-0000-0000-0000-000000000001",
        )

    def test_version_id_never_leaks_into_os_id(self) -> None:
        output = (
            "kernel_name=Linux\nkernel_release=1\nmachine=m\nhostname=h\n"
            "os_version_id=12\nos_id=debian\n"
        )
        identity = parse_identity_output(output)
        assert identity.os_id == "debian"
        assert identity.os_version_id == "12"

    def test_absent_sources_reported_as_none_not_assumed(self) -> None:
        identity = parse_identity_output(_BUSYBOX_LIKE_OUTPUT)
        assert identity.os_id is None
        assert identity.os_version_id is None
        assert identity.os_pretty_name is None
        assert identity.boot_id is None
        assert identity.kernel_name == "Linux"

    def test_non_linux_kernel_refused_not_guessed(self) -> None:
        output = _BUSYBOX_LIKE_OUTPUT.replace("kernel_name=Linux", "kernel_name=Darwin")
        with pytest.raises(SshConnectError) as excinfo:
            parse_identity_output(output)
        assert excinfo.value.code is SshErrorCode.ENVIRONMENT_UNSUPPORTED
        assert "Darwin" in excinfo.value.detail

    def test_missing_required_fields_rejected(self) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            parse_identity_output("kernel_name=Linux\nkernel_release=1\n")
        assert excinfo.value.code is SshErrorCode.SSH_FAILED

    def test_multi_token_field_rejected(self) -> None:
        output = (
            "kernel_name=Linux\nkernel_release=1 2\nmachine=m\nhostname=h\n"
        )
        with pytest.raises(SshConnectError) as excinfo:
            parse_identity_output(output)
        assert excinfo.value.code is SshErrorCode.SSH_FAILED


# ---------------------------------------------------------------------------
# Failure mapping (subprocess-level, no live target needed)
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


@pytest.fixture()
def enrolled_pin(tmp_path: Path) -> Path:
    """A managed known-hosts file with any content (enrollment happened)."""
    pin = tmp_path / "known_hosts"
    pin.write_text("[203.0.113.10]:2222 ssh-ed25519 AAAA\n", encoding="utf-8")
    return pin


class TestProbeEnvironmentFailures:
    def test_refuses_connect_without_enrollment(self, tmp_path: Path) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            probe_environment(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=tmp_path / "absent-known-hosts",
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION
        assert "enroll" in excinfo.value.detail

    def test_changed_host_key_mapped_to_mismatch(
        self, tmp_path: Path, enrolled_pin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(_command: list[str], **_k: object) -> _Completed:
            return _Completed(rc=255, err="Host key verification failed.")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SshConnectError) as excinfo:
            probe_environment(_identity(), key_path=tmp_path / "k", known_hosts_path=enrolled_pin)
        assert excinfo.value.code is SshErrorCode.HOST_KEY_MISMATCH
        assert "explicit admin action" in excinfo.value.detail

    def test_auth_and_unreachable_and_generic_mapping(
        self, tmp_path: Path, enrolled_pin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cases = [
            (255, "Permission denied (publickey).", SshErrorCode.AUTH_FAILED),
            (255, "ssh: connect to host 203.0.113.10 port 2222: Connection refused", SshErrorCode.UNREACHABLE),
            (255, "some unexpected transport error", SshErrorCode.SSH_FAILED),
        ]
        for rc, err, expected in cases:
            monkeypatch.setattr(
                subprocess, "run", lambda _c, _rc=rc, _err=err, **_k: _Completed(rc=_rc, err=_err)
            )
            with pytest.raises(SshConnectError) as excinfo:
                probe_environment(_identity(), key_path=tmp_path / "k", known_hosts_path=enrolled_pin)
            assert excinfo.value.code is expected

    def test_timeout_mapped(self, tmp_path: Path, enrolled_pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(_command: list[str], **_k: object) -> _Completed:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SshConnectError) as excinfo:
            probe_environment(_identity(), key_path=tmp_path / "k", known_hosts_path=enrolled_pin)
        assert excinfo.value.code is SshErrorCode.TIMEOUT

    def test_success_parsed(self, tmp_path: Path, enrolled_pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda _c, **_k: _Completed(rc=0, out=_ARCH_LIKE_OUTPUT)
        )
        identity = probe_environment(_identity(), key_path=tmp_path / "k", known_hosts_path=enrolled_pin)
        assert identity.os_id == "arch"

    def test_error_details_are_bounded_and_single_line(
        self, tmp_path: Path, enrolled_pin: Path
    ) -> None:
        """Details stay bounded regardless of hostile multi-line stderr."""
        err = "line one\nline two " + "x" * 500 + "\nline three"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(subprocess, "run", lambda _c, **_k: _Completed(rc=255, err=err))
        try:
            with pytest.raises(SshConnectError) as excinfo:
                probe_environment(
                    _identity(), key_path=tmp_path / "k", known_hosts_path=enrolled_pin
                )
        finally:
            monkey.undo()
        assert excinfo.value.code is SshErrorCode.SSH_FAILED
        assert "\n" not in excinfo.value.detail
        assert len(excinfo.value.detail) < 400


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture (runtime-environment ADR D1–D5; SSH-PIN owner)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-04-ssh-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-ssh-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-ssh-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_FIXTURE"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


_image_ensured = False


def _ensure_fixture_image() -> None:
    """Build the disposable Arch+openssh fixture image once (pinned base)."""
    global _image_ensured
    if _image_ensured and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh && ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.cleanup_proof = "<not cleaned>"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> SshIdentity:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_path),
             "-C", f"{FIXTURE_CONTAINER}-test"],
            check=True, capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        _ensure_fixture_image()
        run = _docker(
            "run", "--rm", "-d", "--name", FIXTURE_CONTAINER,
            "-p", "127.0.0.1::22",
            FIXTURE_IMAGE_TAG,
            "sh", "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._provision_authorized_key()
        self._wait_keyscan()
        return self.identity_for(self._current_fingerprint())

    def _refresh_port(self) -> None:
        # docker re-allocates the ephemeral host port on every (re)start.
        port_out = _docker(
            "inspect", "--format",
            "{{(index (index .NetworkSettings.Ports \"22/tcp\") 0).HostPort}}",
            FIXTURE_CONTAINER,
        )
        assert port_out.returncode == 0 and port_out.stdout.strip().isdigit(), (
            f"fixture port inspect failed: {port_out.stderr[-400:]}"
        )
        self.port = int(port_out.stdout.strip())

    def identity_for(self, host_key: str) -> SshIdentity:
        """Profile-shaped identity for the CURRENT endpoint and given pin."""
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user="root",
            host_key=host_key,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def _provision_authorized_key(self) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
             "&& cat > /root/.ssh/authorized_keys "
             "&& chmod 600 /root/.ssh/authorized_keys"],
            input=pub, capture_output=True, timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True, text=True, timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        """Enrollment fixture value: cross-checked against real ssh-keygen."""
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

    def rotate_host_keys(self) -> None:
        """Recreate the presented host key material (fixture restart)."""
        restart = _docker("restart", "-t", "1", FIXTURE_CONTAINER)
        assert restart.returncode == 0, restart.stderr
        self._refresh_port()  # docker re-binds the ephemeral host port
        self._wait_keyscan()

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$", "--format", "{{.Names}}"
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        # Remove the whole namespace root (the image build context is a
        # sibling of the per-test scratch, so child-only removal would leave
        # it behind; _ensure_fixture_image() recreates everything on demand).
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: '{listing.stdout.strip() or '<none>'}'; "
            f"scratch {self.scratch} exists: {self.scratch.exists()}; "
            f"parent {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture()
def arch_sshd() -> ArchSshdFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: Arch sshd fixture tests require it")
    fixture = ArchSshdFixture()
    try:
        fixture.identity = fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


class TestAgainstArchFixture:
    """SSH-PIN scenario: enroll → connect → rotate → refuse → re-enroll."""

    def test_connect_returns_actual_arch_identity(self, arch_sshd: ArchSshdFixture) -> None:
        identity = arch_sshd.identity
        key_path = resolve_identity_key(arch_sshd.env_resolver(), identity.credential_ref)
        pinned = pin_host_key(identity, arch_sshd.scratch / "known_hosts")
        assert pinned.key_type == "ssh-ed25519"
        target = probe_environment(identity, key_path=key_path, known_hosts_path=arch_sshd.scratch / "known_hosts")
        assert isinstance(target, TargetIdentity)
        # Observed facts from the REAL target — an Arch fixture must report
        # itself as Arch; nothing here is derivable from the controller.
        assert target.kernel_name == "Linux"
        assert target.os_id == "arch"
        assert target.os_pretty_name == "Arch Linux"
        assert target.hostname and " " not in target.hostname
        assert target.boot_id  # reported boot identity (container shares host kernel)

    def test_probe_before_enrollment_is_refused(self, arch_sshd: ArchSshdFixture) -> None:
        key_path = resolve_identity_key(arch_sshd.env_resolver(), arch_sshd.identity.credential_ref)
        with pytest.raises(SshConnectError) as excinfo:
            probe_environment(
                arch_sshd.identity,
                key_path=key_path,
                known_hosts_path=arch_sshd.scratch / "never-enrolled",
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION

    def test_rotated_host_key_refused_then_explicit_reenrollment_accepted(
        self, arch_sshd: ArchSshdFixture
    ) -> None:
        identity = arch_sshd.identity
        known_hosts = arch_sshd.scratch / "known_hosts"
        key_path = resolve_identity_key(arch_sshd.env_resolver(), identity.credential_ref)
        pin_host_key(identity, known_hosts)
        before = probe_environment(identity, key_path=key_path, known_hosts_path=known_hosts)
        assert before.os_id == "arch"

        old_fingerprint = identity.host_key
        pinned_before = known_hosts.read_text(encoding="utf-8")
        arch_sshd.rotate_host_keys()  # fixture restart regenerates host keys

        # The endpoint identity to probe is the LIVE endpoint carrying the
        # STALE pin (docker re-bound the ephemeral port on restart).
        stale = arch_sshd.identity_for(old_fingerprint)

        # 1. stale pin: ssh-level refusal, no auto-trust, no re-pin on disk
        with pytest.raises(SshConnectError) as excinfo:
            probe_environment(stale, key_path=key_path, known_hosts_path=known_hosts)
        assert excinfo.value.code is SshErrorCode.HOST_KEY_MISMATCH
        assert known_hosts.read_text(encoding="utf-8") == pinned_before

        # 2. re-enrollment attempt with the OLD pin must also refuse
        with pytest.raises(SshConnectError) as excinfo:
            pin_host_key(stale, known_hosts)
        assert excinfo.value.code is SshErrorCode.HOST_KEY_MISMATCH
        assert known_hosts.read_text(encoding="utf-8") == pinned_before

        # 3. explicit enrollment with the NEW accepted fingerprint
        rotated = arch_sshd.identity_for(arch_sshd._current_fingerprint())
        repinned = pin_host_key(rotated, known_hosts)
        assert repinned.fingerprint == rotated.host_key
        assert repinned.fingerprint != old_fingerprint
        after = probe_environment(rotated, key_path=key_path, known_hosts_path=known_hosts)
        assert after.os_id == "arch"
        assert after.hostname == before.hostname
