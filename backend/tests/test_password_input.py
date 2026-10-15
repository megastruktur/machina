"""Approval-gated sudo password channel tests (card 04-password-channel).

Covers ``backend/src/mai/execution/password_input.py`` against contracts.md
**C03 rules 2/4/6** (approval-gated secret flow, one-use binding) and
**C04 rule 2** (credentials never in shell/argv), mirroring the
``test_privilege.py`` / ``test_approvals.py`` producer patterns:

- **Acceptance 1** — explicit authenticated account/challenge binding and
  short-lived single-use secret transport: the unit layer pins the
  fail-closed order (structural → account → payload-hash → ONE-USE
  consume → transport) with spy stores/processes; the fixture layer runs
  the REAL gate (real Postgres approval consumed by the real channel, real
  Arch sshd target). The secret enters ONLY the stdin pipe: argv and env
  are proven secret-free at both boundaries; the handed buffer is zeroed
  after every attempt, whatever the outcome.
- **Acceptance 2** — wrong/expired/replayed input rejected and the
  locked-account limitation explicit: wrong password classified
  ``password_rejected`` at the real boundary (payload provably never
  started — no canary), replay/expiry denials leave nothing transported,
  and a shadow-locked account is proven INDISTINGUISHABLE from a wrong
  password through this lane (measured ``passwd -l`` behavior) with
  recovery after unlock. No retry loop exists: a spent decision can never
  fund a second attempt.

Secret hygiene: the fixture password is throwaway runtime material
generated per fixture start, piped to ``chpasswd`` via stdin (never an
argv), never printed to transcripts or assertion messages; approval
payloads are hashes only.
"""

from __future__ import annotations

import atexit
import os
import secrets
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.execution.commands import (
    MAX_REMOTE_COMMAND_BYTES,
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    compose_remote_command,
    execute_command,
)
from mai.execution.password_input import (
    MAX_SECRET_BYTES,
    PasswordChallenge,
    PasswordChannelError,
    PasswordErrorCode,
    execute_passworded_command,
    guarded_passworded_payload,
    passworded_payload_hash,
    passworded_remote_command,
)
from mai.execution.privilege import PAYLOAD_START_MARKER
from mai.execution.ssh import (
    SshConnectError,
    pin_host_key,
    resolve_identity_key,
)
from mai.policy.approvals import (
    Approval,
    ApprovalError,
    ApprovalErrorCode,
    ApprovalRequest,
    ApprovalStore,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

PRINCIPAL = "admin"
CAPABILITY = "shell.raw"


def _request(**overrides: object) -> CommandRequest:
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "fixture display label",
        "timeout": 10.0,
        "output_budget": 4096,
        "argv": ("/usr/bin/id", "-u"),
    }
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


def _challenge(request: CommandRequest, **overrides: object) -> PasswordChallenge:
    values: dict[str, object] = {
        "approval_id": uuid.uuid4(),
        "capability_id": CAPABILITY,
        "server_id": request.server_id,
        "payload_hash": passworded_payload_hash(request),
        "policy_revision": 1,
        "account": FIXTURE_USER,
        "consuming_principal": PRINCIPAL,
    }
    values.update(overrides)
    return PasswordChallenge(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure composition
# ---------------------------------------------------------------------------


class TestPasswordedComposition:
    def test_guarded_payload_carries_marker_then_command(self) -> None:
        request = _request(argv=("echo", "hi"))
        guarded = guarded_passworded_payload(request)
        assert guarded.startswith(
            f"echo '{PAYLOAD_START_MARKER}' 1>&2; "
        )
        assert guarded.endswith(compose_remote_command(request))

    def test_remote_command_is_sudo_stdin_lane(self) -> None:
        import shlex

        request = _request(argv=("echo", "hi"))
        remote = passworded_remote_command(request)
        guarded = guarded_passworded_payload(request)
        assert remote == " ".join(
            shlex.quote(element)
            for element in ("sudo", "-S", "-p", "", "--", "/bin/sh", "-c", guarded)
        )
        # The stdin lane: the composition has no place a secret could enter
        # as an argument, and the sudo option end protects the payload.
        assert remote.startswith("sudo -S -p '' -- /bin/sh -c ")

    def test_payload_quoted_once_metacharacters_stay_literal(self) -> None:
        request = _request(
            argv=("printf", "%s\\n", "hello; touch /tmp/pwned", "$(id)")
        )
        remote = passworded_remote_command(request)
        # The payload appears exactly once: inside the single quoted -c
        # element; an injected fragment can never terminate it.
        assert remote.count("touch /tmp/pwned") == 1

    def test_cwd_guard_travels_inside_the_escalation(self) -> None:
        request = _request(working_directory="/opt/my dir")
        guarded = guarded_passworded_payload(request)
        assert guarded.startswith(f"echo '{PAYLOAD_START_MARKER}' 1>&2; ")
        assert "cd '/opt/my dir' 2>/dev/null" in guarded

    def test_oversized_transport_rejected(self) -> None:
        request = _request(argv=None, shell="x" * (MAX_REMOTE_COMMAND_BYTES - 30))
        with pytest.raises(CommandRequestError):
            passworded_remote_command(request)

    def test_payload_hash_binds_exact_transport_bytes(self) -> None:
        import hashlib

        request = _request()
        expected = hashlib.sha256(
            guarded_passworded_payload(request).encode("utf-8")
        ).hexdigest()
        assert passworded_payload_hash(request) == expected
        altered = _request(argv=("/usr/bin/id", "-g"))
        assert passworded_payload_hash(altered) != expected


# ---------------------------------------------------------------------------
# Challenge validation (value-free, pre-consume)
# ---------------------------------------------------------------------------


class TestChallengeValidation:
    def _good(self, **overrides: object) -> dict[str, object]:
        request = _request()
        values: dict[str, object] = {
            "approval_id": uuid.uuid4(),
            "capability_id": CAPABILITY,
            "server_id": request.server_id,
            "payload_hash": passworded_payload_hash(request),
            "policy_revision": 1,
            "account": "mai-fxt",
            "consuming_principal": PRINCIPAL,
        }
        values.update(overrides)
        return values

    @pytest.mark.parametrize(
        "field",
        [
            "approval_id",
            "capability_id",
            "server_id",
            "payload_hash",
            "policy_revision",
            "account",
            "consuming_principal",
        ],
    )
    def test_malformed_field_rejected_value_free(self, field: str) -> None:
        values = self._good(**{field: _MALFORMED[field]})
        with pytest.raises(PasswordChannelError) as excinfo:
            PasswordChallenge(**values)  # type: ignore[arg-type]
        assert excinfo.value.code is PasswordErrorCode.INVALID_REQUEST
        detail = excinfo.value.detail
        assert "\n" not in detail and len(detail) < 300

    def test_uppercase_hash_is_rejected(self) -> None:
        values = self._good(payload_hash=passworded_payload_hash(_request()).upper())
        with pytest.raises(PasswordChannelError):
            PasswordChallenge(**values)  # type: ignore[arg-type]


_MALFORMED: dict[str, object] = {
    "approval_id": "not-a-uuid",
    "capability_id": "BAD CAP",
    "server_id": 42,
    "payload_hash": "z" * 64,
    "policy_revision": 0,
    "account": "bad account!",
    "consuming_principal": "",
}


# ---------------------------------------------------------------------------
# Unit: the gated transport over a fake process + spy store
# ---------------------------------------------------------------------------


class _FakeProc:
    """Popen stand-in over REAL pipe fds (drives the real capture loop)."""

    def __init__(
        self,
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hold_open: bool = False,
        stdin_broken: bool = False,
    ) -> None:
        self._returncode = returncode
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        if stdout:
            os.write(w_out, stdout)
        if stderr:
            os.write(w_err, stderr)
        self._writers = [w_out, w_err]
        if not hold_open:
            os.close(w_out)
            os.close(w_err)
            self._writers = []
        self.stdout = open(r_out, "rb", buffering=0)  # noqa: SIM115 - pipe handle, closed in dispose()
        self.stderr = open(r_err, "rb", buffering=0)  # noqa: SIM115 - pipe handle, closed in dispose()
        if stdin_broken:
            class _Broken:
                def write(self, _data: object) -> int:
                    raise BrokenPipeError()

                def close(self) -> None:
                    pass

            self.stdin = _Broken()
            self._stdin_reader: int | None = None
        else:
            r_in, w_in = os.pipe()
            self._stdin_reader = r_in
            self.stdin = open(w_in, "wb", buffering=0)  # noqa: SIM115 - pipe handle, closed with the process
        self.returncode: int | None = None
        self.killed = False
        self.argv_seen: list[str] | None = None

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def stdin_bytes(self) -> bytes:
        if self._stdin_reader is None:
            return b""
        return os.read(self._stdin_reader, 4096)

    def dispose(self) -> None:
        for fd in getattr(self, "_writers", []):
            os.close(fd)
        self._writers = []
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass


class _SpyStore:
    """ApprovalStore stand-in recording consume calls (unit layer only)."""

    def __init__(self, *, error: ApprovalError | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._error = error

    async def consume(self, approval_id: uuid.UUID, **binding: object) -> Approval:
        self.calls.append({"approval_id": approval_id, **binding})
        if self._error is not None:
            raise self._error
        now = datetime.now(UTC)
        return Approval(
            approval_id=approval_id,
            capability_id=str(binding["capability_id"]),
            server_id=binding["server_id"],  # type: ignore[arg-type]
            payload_hash=str(binding["payload_hash"]),
            policy_revision=int(binding["policy_revision"]),  # type: ignore[call-overload]
            created_by="creator",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            consumed_at=now,
            consumed_by=str(binding["consumed_by"]),
        )


def _identity(user: str = "mai-fxt") -> SshIdentity:
    return SshIdentity(
        host="203.0.113.10",
        port=22,
        user=user,
        host_key="SHA256:" + "a" * 43,
        credential_ref="MAI_SSH_KEY_UNIT",
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    proc: _FakeProc,
    *,
    store: object | None = None,
    challenge: PasswordChallenge | None = None,
    identity: SshIdentity | None = None,
    request: CommandRequest | None = None,
    secret_text: str = "unit-secret-0123",
    known_hosts: Path | None = None,
) -> CommandOutcome:
    def _spy(argv: list[str], **_kwargs: object) -> _FakeProc:
        proc.argv_seen = list(argv)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _spy)
    used_request = request if request is not None else _request()
    used_challenge = challenge if challenge is not None else _challenge(used_request)
    used_store = store if store is not None else _SpyStore()
    kh = known_hosts if known_hosts is not None else _KNOWN_HOSTS
    if kh is _KNOWN_HOSTS:
        kh.parent.mkdir(parents=True, exist_ok=True)
        kh.touch(exist_ok=True)
    outcome = await execute_passworded_command(
        used_store,  # type: ignore[arg-type]
        used_challenge,
        identity if identity is not None else _identity(),
        used_request,
        bytearray(secret_text, "utf-8"),
        key_path="/fixture/key",
        known_hosts_path=kh,
    )
    return outcome


@pytest.fixture()
def known_hosts(tmp_path: Path) -> Path:
    path = tmp_path / "known_hosts"
    path.write_text("", encoding="utf-8")
    return path


_KNOWN_HOSTS = Path("/tmp/mai-fxt-04-password-channel-unit-known-hosts")

# C10 rule 2 namespace hygiene: the shared unit known-hosts scratch is
# recreated per call (see ``_run``) and removed for good when the test
# session ends, so a suite run leaves no /tmp/mai-fxt-04-* unit residue
# (04-verify acceptance). Same-test direct reuses of the path keep their
# isfile precondition satisfied.
atexit.register(_KNOWN_HOSTS.unlink, True)


@pytest.mark.anyio
class TestGatedTransportUnit:
    async def test_secret_travels_via_stdin_never_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(0, stdout=b"0\n")
        request = _request()
        outcome = await _run(monkeypatch, proc, request=request)
        assert outcome.status is CommandStatus.SUCCEEDED
        assert proc.argv_seen is not None
        argv_blob = "\x00".join(proc.argv_seen).encode("utf-8")
        assert b"unit-secret-0123" not in argv_blob  # C04 rule 2
        fed = proc.stdin_bytes()
        assert fed == b"unit-secret-0123\n"  # exactly one stdin line

    async def test_buffer_wiped_after_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(0)
        buffer = bytearray(b"wiped-after-use")
        captured: list[list[str]] = []

        def _spy(argv: list[str], **_k: object) -> _FakeProc:
            captured.append(list(argv))
            return proc

        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        await execute_passworded_command(
            _SpyStore(),
            _challenge(request),
            _identity(),
            request,
            buffer,
            key_path="/fixture/key",
            known_hosts_path=_KNOWN_HOSTS,
        )
        assert buffer == b"\x00" * len(buffer)

    async def test_account_mismatch_refused_before_consume_and_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("Popen must not run for a mismatched account")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        store = _SpyStore()
        request = _request()
        buffer = bytearray(b"unused-secret")
        with pytest.raises(PasswordChannelError) as excinfo:
            await execute_passworded_command(
                store,  # type: ignore[arg-type]
                _challenge(request, account="someone-else"),
                _identity(user="mai-fxt"),
                request,
                buffer,
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert excinfo.value.code is PasswordErrorCode.ACCOUNT_MISMATCH
        assert store.calls == []  # the decision was NOT consumed
        assert spawned is False
        assert b"unused-secret" not in str(excinfo.value).encode()
        assert buffer == bytearray(b"unused-secret")  # pre-transport: untouched

    async def test_payload_binding_mismatch_refused_before_consume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("Popen must not run for a mismatched payload")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        store = _SpyStore()
        request = _request()
        # A genuinely different COMMAND (identity fields never enter the
        # payload hash — only the exact transported bytes do).
        other = _request(argv=("/bin/sh", "-c", "echo different-command"))
        buffer = bytearray(b"some-secret")
        with pytest.raises(PasswordChannelError) as excinfo:
            await execute_passworded_command(
                store,  # type: ignore[arg-type]
                _challenge(request, payload_hash=passworded_payload_hash(other)),
                _identity(),
                request,
                buffer,
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert excinfo.value.code is PasswordErrorCode.PAYLOAD_BINDING_MISMATCH
        assert "NOT consumed" in excinfo.value.detail
        assert store.calls == []
        assert spawned is False

    @pytest.mark.parametrize(
        ("code", "store_code"),
        [
            (ApprovalErrorCode.EXPIRED, ApprovalErrorCode.EXPIRED),
            (ApprovalErrorCode.ALREADY_CONSUMED, ApprovalErrorCode.ALREADY_CONSUMED),
            (ApprovalErrorCode.PAYLOAD_MISMATCH, ApprovalErrorCode.PAYLOAD_MISMATCH),
        ],
    )
    async def test_approval_denial_propagates_unchanged_and_wipes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        code: ApprovalErrorCode,
        store_code: ApprovalErrorCode,
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("no transport after an approval denial")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        store = _SpyStore(error=ApprovalError(store_code, "store denial"))
        request = _request()
        buffer = bytearray(b"never-transported")
        with pytest.raises(ApprovalError) as excinfo:
            await execute_passworded_command(
                store,  # type: ignore[arg-type]
                _challenge(request),
                _identity(),
                request,
                buffer,
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert excinfo.value.code is code
        assert len(store.calls) == 1  # the consume was attempted exactly once
        assert spawned is False
        assert buffer == b"\x00" * len(buffer)  # consumed/wiped even on denial

    async def test_wrong_password_classified_value_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(1, stderr=b"sudo: 1 incorrect password attempt\n")
        buffer = bytearray(b"wrong-guess-1")
        captured: list[list[str]] = []

        def _spy(argv: list[str], **_k: object) -> _FakeProc:
            captured.append(list(argv))
            return proc

        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        with pytest.raises(PasswordChannelError) as excinfo:
            await execute_passworded_command(
                _SpyStore(),
                _challenge(request),
                _identity(),
                request,
                buffer,
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert excinfo.value.code is PasswordErrorCode.PASSWORD_REJECTED
        detail = excinfo.value.detail
        assert "\n" not in detail and len(detail) < 400
        assert "incorrect password attempt" in detail
        assert buffer == b"\x00" * len(buffer)

    async def test_refusal_detail_redacts_presented_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hostile/buggy target echoing the presented material back: the
        # detail must never carry it (defense in depth).
        proc = _FakeProc(
            1, stderr=b"sudo: 1 incorrect password attempt: echoed-S3cret!\n"
        )
        captured: list[list[str]] = []

        def _spy(argv: list[str], **_k: object) -> _FakeProc:
            captured.append(list(argv))
            return proc

        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        with pytest.raises(PasswordChannelError) as excinfo:
            await execute_passworded_command(
                _SpyStore(),
                _challenge(request),
                _identity(),
                request,
                bytearray(b"echoed-S3cret!"),
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert "echoed-S3cret!" not in excinfo.value.detail
        assert "***redacted***" in excinfo.value.detail

    async def test_locked_account_marker_classifies_account_locked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(
            1,
            stderr=(
                b"sudo: account or password is expired/locked, "
                b"contact your administrator\n"
            ),
        )
        captured: list[list[str]] = []

        def _spy(argv: list[str], **_k: object) -> _FakeProc:
            captured.append(list(argv))
            return proc

        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        with pytest.raises(PasswordChannelError) as excinfo:
            await execute_passworded_command(
                _SpyStore(),
                _challenge(request),
                _identity(),
                request,
                bytearray(b"correct-pw"),
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        assert excinfo.value.code is PasswordErrorCode.ACCOUNT_LOCKED

    async def test_unknown_refusal_stays_generic_payload_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(1, stderr=b"sudo: some future refusal text\n")
        outcome = await _run(monkeypatch, proc, secret_text="pw-unknown")
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == 1

    async def test_truncated_stderr_is_never_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cut refusal stream is evidence we cannot read: truncation must
        # refuse the classification (generic failed outcome, fail-safe).
        proc = _FakeProc(
            1,
            stderr=b"x" * 9999 + b"\nsudo: 1 incorrect password attempt\n",
        )
        request = _request(output_budget=64)
        outcome = await _run(monkeypatch, proc, request=request, secret_text="pw")
        assert outcome.stderr_truncated is True
        assert outcome.status is CommandStatus.FAILED
        proc.dispose()

    async def test_started_payload_failure_returns_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(
            3, stderr=f"{PAYLOAD_START_MARKER}\npayload blew up\n".encode()
        )
        outcome = await _run(monkeypatch, proc, secret_text="pw")
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == 3

    async def test_transport_failure_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(255, stderr=b"root@203.0.113.10: Permission denied (publickey).")
        with pytest.raises(SshConnectError) as excinfo:
            await _run(monkeypatch, proc, secret_text="pw")
        assert "auth_failed" in str(excinfo.value)

    async def test_timeout_stays_unknown_and_wipes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(0, stdout=b"partial", hold_open=True)
        buffer = bytearray(b"pw-timeout")
        captured: list[list[str]] = []

        def _spy(argv: list[str], **_k: object) -> _FakeProc:
            captured.append(list(argv))
            return proc

        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request(timeout=0.2)
        try:
            outcome = await execute_passworded_command(
                _SpyStore(),
                _challenge(request),
                _identity(),
                request,
                buffer,
                key_path="/fixture/key",
                known_hosts_path=_KNOWN_HOSTS,
            )
        finally:
            proc.dispose()
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None
        assert buffer == b"\x00" * len(buffer)

    async def test_broken_stdin_never_hangs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(1, stderr=b"", stdin_broken=True)
        outcome = await _run(monkeypatch, proc, secret_text="pw")
        assert outcome.status is CommandStatus.FAILED

    async def test_secret_bounds_enforced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = _request()
        for bad in (
            b"",  # empty
            b"x" * (MAX_SECRET_BYTES + 1),  # oversized
            b"has\nnewline",
            b"has\x00nul",
            "not-a-bytearray",  # immutable type cannot honor the wipe
        ):
            with pytest.raises(PasswordChannelError) as excinfo:
                await execute_passworded_command(
                    _SpyStore(),  # type: ignore[arg-type]
                    _challenge(request),
                    _identity(),
                    request,
                    bad,  # type: ignore[arg-type]
                    key_path="/fixture/key",
                    known_hosts_path=_KNOWN_HOSTS,
                )
            assert excinfo.value.code is PasswordErrorCode.INVALID_REQUEST

    async def test_missing_known_hosts_refused_pre_consume(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("no transport without an enrolled host key")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        store = _SpyStore()
        request = _request()
        with pytest.raises(SshConnectError):
            await execute_passworded_command(
                store,  # type: ignore[arg-type]
                _challenge(request),
                _identity(),
                request,
                bytearray(b"pw"),
                key_path="/fixture/key",
                known_hosts_path=tmp_path / "absent-known-hosts",
            )
        assert store.calls == []


# ---------------------------------------------------------------------------
# Live boundaries: real Postgres approvals + real Arch sshd with a
# passworded sudo grant (runtime-environment ADR D1–D5; namespace
# mai-fxt-04-password-channel-*). Auto-skips when docker or PG is absent.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-04-password-channel-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-password-channel-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-password-channel/tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_PW_CHANNEL_FIXTURE"
FIXTURE_USER = "mai-fxt"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


_image_ensured = False


def _ensure_fixture_image() -> None:
    global _image_ensured
    if _image_ensured and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo shadow "
        "&& ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1800)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True


class ArchPasswordFixture:
    """Disposable Arch sshd container with a PASSWORDED sudo account.

    The transport authenticates by throwaway SSH key; sudo authenticates
    by the account password (``mai-fxt ALL=(root:root) ALL`` — no
    NOPASSWD). Lock/unlock is done in place for the acceptance-2 scenario.
    """

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        #: Throwaway runtime material, piped via stdin to chpasswd — never
        #: an argv, never printed.
        self.password = f"pw-{secrets.token_urlsafe(18)}"
        self.host_key_fingerprint = ""
        self.cleanup_proof = "<not cleaned>"

    def start(self) -> None:
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
            f"&& useradd -m -s /bin/sh {FIXTURE_USER} "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._wait_account(FIXTURE_USER)
        self._provision_authorized_key("root", "/root")
        self._provision_authorized_key(FIXTURE_USER, f"/home/{FIXTURE_USER}")
        self.set_password(self.password)
        self.write_sudo_grant()
        self._wait_keyscan()
        self.host_key_fingerprint = self._current_fingerprint()

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

    def _wait_account(self, name: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        check = _docker("exec", FIXTURE_CONTAINER, "true")
        while time.monotonic() < deadline:
            check = _docker("exec", FIXTURE_CONTAINER, "id", "-u", name)
            if check.returncode == 0:
                return
            time.sleep(0.2)
        raise AssertionError(
            f"fixture account {name} never appeared: {check.stderr[-400:]}"
        )

    def _provision_authorized_key(self, user: str, home: str) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             (f"mkdir -p {home}/.ssh && chmod 700 {home}/.ssh "
              f"&& cat > {home}/.ssh/authorized_keys "
              f"&& chmod 600 {home}/.ssh/authorized_keys "
              f"&& chown -R {user}:{user} {home}/.ssh")],
            input=pub, capture_output=True, timeout=30, check=False,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    # -- account/sudo state (acceptance-2 scenario controls) ----------------

    def set_password(self, password: str) -> None:
        # Password travels on chpasswd's STDIN, never its argv.
        result = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "chpasswd"],
            input=f"{FIXTURE_USER}:{password}\n",
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr[-400:]

    def write_sudo_grant(self) -> None:
        script = (
            "grep -q '^@includedir /etc/sudoers.d$' /etc/sudoers "
            "|| echo '@includedir /etc/sudoers.d' >> /etc/sudoers; "
            f"echo '{FIXTURE_USER} ALL=(root:root) ALL' > "
            "/etc/sudoers.d/mai-fxt-passworded; "
            "chmod 440 /etc/sudoers.d/mai-fxt-passworded; "
            "visudo -cf /etc/sudoers.d/mai-fxt-passworded"
        )
        result = _docker("exec", FIXTURE_CONTAINER, "sh", "-c", script)
        assert result.returncode == 0, (
            f"sudoers provisioning failed: {result.stdout[-400:]}{result.stderr[-400:]}"
        )

    def lock_account(self) -> None:
        result = _docker("exec", FIXTURE_CONTAINER, "passwd", "-l", FIXTURE_USER)
        assert result.returncode == 0, result.stderr[-400:]

    def unlock_account(self) -> None:
        result = _docker("exec", FIXTURE_CONTAINER, "passwd", "-u", FIXTURE_USER)
        assert result.returncode == 0, result.stderr[-400:]
        # Operator reconciliation on the disposable fixture: failed sudo
        # attempts arm the target's pam_faillock tally (measured: even the
        # CORRECT password is then denied until an explicit reset). The
        # channel deliberately never retries, so clearing the tally is the
        # fixture-side stand-in for account reconciliation.
        reset = _docker(
            "exec", FIXTURE_CONTAINER, "faillock", "--user", FIXTURE_USER, "--reset"
        )
        assert reset.returncode == 0, reset.stderr[-400:]

    # -- identities, probes, lifecycle ---------------------------------------

    def identity_for(self, user: str) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user=user,
            host_key=self.host_key_fingerprint,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def transport(self, user: str) -> tuple[Path, Path]:
        identity = self.identity_for(user)
        key_path = resolve_identity_key(self.env_resolver(), identity.credential_ref)
        known_hosts = self.scratch / f"known_hosts-{user}"
        if not known_hosts.exists():
            pin_host_key(identity, known_hosts)
        return key_path, known_hosts

    async def run_channel(
        self,
        user: str,
        store: ApprovalStore,
        challenge: PasswordChallenge,
        request: CommandRequest,
        secret: bytearray,
    ) -> CommandOutcome:
        key_path, known_hosts = self.transport(user)
        return await execute_passworded_command(
            store,
            challenge,
            self.identity_for(user),
            request,
            secret,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

    def run_unprivileged(self, user: str, request: CommandRequest) -> CommandOutcome:
        key_path, known_hosts = self.transport(user)
        return execute_command(
            self.identity_for(user), request,
            key_path=key_path, known_hosts_path=known_hosts,
        )

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        ed25519 = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)],
            capture_output=True, text=True, check=False,
        )
        assert keygen.returncode == 0, keygen.stderr
        return keygen.stdout.split()[1]

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

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$", "--format",
            "{{.Names}}",
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        # 04-verify hygiene: drop the empty namespace grandparent
        # (/tmp/mai-fxt-04-password-channel) once tests/ is gone.
        try:
            FIXTURE_SCRATCH.parent.rmdir()
        except OSError:
            pass
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: "
            f"'{listing.stdout.strip() or '<none>'}'; "
            f"scratch {self.scratch} exists: {self.scratch.exists()}; "
            f"parent {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0003``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the approval-gate claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS approvals, admin_sessions, "
                "admin_credential, profile_projection_journal, "
                "profile_projection, alembic_version"
            )
            conn.commit()
        command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def store(migrated_url: str) -> ApprovalStore:
    engine, factory = create_app_engine(migrated_url)
    try:
        yield ApprovalStore(factory)
    finally:
        await engine.dispose()


@pytest.fixture
def clean_approvals(migrated_url: str):
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE approvals")
        conn.commit()
    yield


@pytest.fixture()
def arch_pw(migrated_url: str) -> ArchPasswordFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: Arch password-channel fixture tests require it")
    fixture = ArchPasswordFixture()
    try:
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


async def _create_approval(
    store: ApprovalStore, request: CommandRequest, *, principal: str = PRINCIPAL
) -> Approval:
    return await store.create(
        ApprovalRequest(
            capability_id=CAPABILITY,
            server_id=request.server_id,
            payload_hash=passworded_payload_hash(request),
            policy_revision=1,
            created_by=principal,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )


def _backdate(migrated_url: str, approval_id: uuid.UUID) -> None:
    """Clock simulator: move anchor + expiry together (CHECK-preserving)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE approvals SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' WHERE approval_id = %s",
            (approval_id,),
        )
        conn.commit()


@pytest.mark.anyio
class TestAgainstArchPasswordFixture:
    """Real-boundary acceptance: a real gate spends a real decision."""

    async def test_correct_secret_runs_privileged_payload(
        self, arch_pw: ArchPasswordFixture, store: ApprovalStore, clean_approvals
    ) -> None:
        # Scenario half 1: approved challenge + correct secret ⇒ the benign
        # payload runs as REAL uid 0 on the disposable target.
        request = _request(
            argv=("/bin/sh", "-c", "/usr/bin/id -u"), timeout=30.0, output_budget=1024
        )
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        secret = bytearray(arch_pw.password, "utf-8")
        outcome = await arch_pw.run_channel(FIXTURE_USER, store, challenge, request, secret)
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.stdout == "0\n"
        assert secret == b"\x00" * len(secret)  # wiped after use
        record = await store.get(approval.approval_id)
        assert record is not None and record.status == "consumed"

    async def test_wrong_secret_rejected_payload_never_runs(
        self, arch_pw: ArchPasswordFixture, store: ApprovalStore, clean_approvals
    ) -> None:
        request = _request(
            argv=("/bin/sh", "-c", "touch /tmp/mai-pw-canary; /usr/bin/id -u"),
            timeout=30.0,
            output_budget=1024,
        )
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        started = time.monotonic()
        with pytest.raises(PasswordChannelError) as excinfo:
            await arch_pw.run_channel(
                FIXTURE_USER,
                store,
                challenge,
                request,
                bytearray(b"definitely-not-the-password"),
            )
        elapsed = time.monotonic() - started
        assert excinfo.value.code is PasswordErrorCode.PASSWORD_REJECTED
        assert elapsed < 15.0  # single attempt, no retry loop, no hang
        probe = arch_pw.run_unprivileged(
            FIXTURE_USER,
            _request(argv=("test", "!", "-e", "/tmp/mai-pw-canary"), output_budget=1024),
        )
        # `test ! -e` exits 0 exactly when the canary is absent: the payload
        # provably never started behind the rejected secret.
        assert probe.status is CommandStatus.SUCCEEDED
        record = await store.get(approval.approval_id)
        assert record is not None and record.status == "consumed"
        # One-use honesty: the spent decision cannot fund a second attempt.
        with pytest.raises(ApprovalError) as replay:
            await arch_pw.run_channel(
                FIXTURE_USER,
                store,
                challenge,
                request,
                bytearray(arch_pw.password, "utf-8"),
            )
        assert replay.value.code is ApprovalErrorCode.ALREADY_CONSUMED

    async def test_replayed_challenge_is_denied_without_transport(
        self,
        arch_pw: ArchPasswordFixture,
        store: ApprovalStore,
        clean_approvals,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request(
            argv=("/bin/sh", "-c", "touch /tmp/mai-pw-canary-2"), timeout=30.0
        )
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        outcome = await arch_pw.run_channel(
            FIXTURE_USER, store, challenge, request,
            bytearray(arch_pw.password, "utf-8"),
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        # The first (and only) execution created the canary.
        exists = arch_pw.run_unprivileged(
            FIXTURE_USER,
            _request(argv=("test", "-e", "/tmp/mai-pw-canary-2"), output_budget=1024),
        )
        assert exists.status is CommandStatus.SUCCEEDED

        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> object:
            nonlocal spawned
            spawned = True
            raise AssertionError("replay must not transport the secret")

        monkeypatch.setattr(
            "mai.execution.password_input.subprocess.Popen", _no_spawn
        )
        with pytest.raises(ApprovalError) as excinfo:
            await arch_pw.run_channel(
                FIXTURE_USER, store, challenge, request,
                bytearray(arch_pw.password, "utf-8"),
            )
        assert excinfo.value.code is ApprovalErrorCode.ALREADY_CONSUMED
        assert spawned is False  # exactly one execution, no second transport

    async def test_expired_decision_denied_without_transport(
        self,
        arch_pw: ArchPasswordFixture,
        store: ApprovalStore,
        clean_approvals,
        migrated_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request(
            argv=("/bin/sh", "-c", "touch /tmp/mai-pw-canary-3"), timeout=30.0
        )
        approval = await _create_approval(store, request)
        _backdate(migrated_url, approval.approval_id)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        # Warm the pinned transport BEFORE the no-spawn spy: host-key
        # pinning itself drives subprocess and is not the gate under test.
        arch_pw.transport(FIXTURE_USER)
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> object:
            nonlocal spawned
            spawned = True
            raise AssertionError("expired decision must not transport the secret")

        monkeypatch.setattr(
            "mai.execution.password_input.subprocess.Popen", _no_spawn
        )
        with pytest.raises(ApprovalError) as excinfo:
            await arch_pw.run_channel(
                FIXTURE_USER, store, challenge, request,
                bytearray(arch_pw.password, "utf-8"),
            )
        assert excinfo.value.code is ApprovalErrorCode.EXPIRED
        assert spawned is False

    async def test_account_mismatch_denied_decision_stays_pending(
        self,
        arch_pw: ArchPasswordFixture,
        store: ApprovalStore,
        clean_approvals,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request(timeout=30.0)
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account="not-this-account"
        )
        # Warm the pinned transport BEFORE the no-spawn spy (see above).
        arch_pw.transport(FIXTURE_USER)
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> object:
            nonlocal spawned
            spawned = True
            raise AssertionError("account mismatch must not transport anything")

        monkeypatch.setattr(
            "mai.execution.password_input.subprocess.Popen", _no_spawn
        )
        with pytest.raises(PasswordChannelError) as excinfo:
            await arch_pw.run_channel(
                FIXTURE_USER, store, challenge, request,
                bytearray(arch_pw.password, "utf-8"),
            )
        assert excinfo.value.code is PasswordErrorCode.ACCOUNT_MISMATCH
        assert spawned is False
        record = await store.get(approval.approval_id)
        assert record is not None and record.status == "pending"

    async def test_payload_swap_denied_decision_stays_pending(
        self,
        arch_pw: ArchPasswordFixture,
        store: ApprovalStore,
        clean_approvals,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        approved = _request(timeout=30.0)
        approval = await _create_approval(store, approved)
        presented = _request(
            argv=("/bin/sh", "-c", "touch /tmp/mai-pw-canary-4"), timeout=30.0
        )
        # The approval (and therefore the challenge) is bound to APPROVED's
        # payload; the caller attempts to run PRESENTED over it. The channel
        # must refuse pre-consume — the decision stays pending.
        challenge = _challenge(
            approved, approval_id=approval.approval_id
        )
        assert challenge.payload_hash == passworded_payload_hash(approved)
        assert challenge.payload_hash != passworded_payload_hash(presented)
        # Warm the pinned transport BEFORE the no-spawn spy (see above).
        arch_pw.transport(FIXTURE_USER)
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> object:
            nonlocal spawned
            spawned = True
            raise AssertionError("altered payload must not transport the secret")

        monkeypatch.setattr(
            "mai.execution.password_input.subprocess.Popen", _no_spawn
        )
        with pytest.raises(PasswordChannelError) as excinfo:
            await arch_pw.run_channel(
                FIXTURE_USER, store, challenge, presented,
                bytearray(arch_pw.password, "utf-8"),
            )
        assert excinfo.value.code is PasswordErrorCode.PAYLOAD_BINDING_MISMATCH
        assert spawned is False
        record = await store.get(approval.approval_id)
        assert record is not None and record.status == "pending"

    async def test_locked_account_limitation_explicit_with_recovery(
        self, arch_pw: ArchPasswordFixture, store: ApprovalStore, clean_approvals
    ) -> None:
        # Acceptance 2, locked-account half: shadow-locked, even the CORRECT
        # password is refused exactly like a wrong one (measured) — the two
        # are indistinguishable at this boundary; the channel reports
        # password_rejected, never retries, and the account recovers after
        # an explicit unlock.
        request = _request(
            argv=("/bin/sh", "-c", "/usr/bin/id -u"), timeout=30.0, output_budget=1024
        )
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        arch_pw.lock_account()
        try:
            with pytest.raises(PasswordChannelError) as excinfo:
                await arch_pw.run_channel(
                    FIXTURE_USER, store, challenge, request,
                    bytearray(arch_pw.password, "utf-8"),
                )
            # The measured limitation: locked ≡ wrong password here.
            assert excinfo.value.code is PasswordErrorCode.PASSWORD_REJECTED
            assert "incorrect password attempt" in excinfo.value.detail
        finally:
            arch_pw.unlock_account()

        recovery = await _create_approval(store, request)
        recovery_challenge = _challenge(
            request, approval_id=recovery.approval_id, account=FIXTURE_USER
        )
        outcome = await arch_pw.run_channel(
            FIXTURE_USER, store, recovery_challenge, request,
            bytearray(arch_pw.password, "utf-8"),
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.stdout == "0\n"

    async def test_secret_never_disclosed_in_streams_or_state(
        self, arch_pw: ArchPasswordFixture, store: ApprovalStore, clean_approvals
    ) -> None:
        request = _request(
            argv=("/bin/sh", "-c", "/usr/bin/env; /bin/cat /proc/self/environ"),
            timeout=30.0,
            output_budget=64 * 1024,
        )
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        secret = bytearray(arch_pw.password, "utf-8")
        outcome = await arch_pw.run_channel(
            FIXTURE_USER, store, challenge, request, secret
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert arch_pw.password not in outcome.stdout
        assert arch_pw.password not in outcome.stderr
        record = await store.get(approval.approval_id)
        assert record is not None
        assert arch_pw.password not in repr(record)
        assert arch_pw.password not in str(record.to_json())

    async def test_budget_cut_payload_reports_unknown(
        self, arch_pw: ArchPasswordFixture, store: ApprovalStore, clean_approvals
    ) -> None:
        request = _request(argv=("sleep", "10"), timeout=2.0, output_budget=1024)
        approval = await _create_approval(store, request)
        challenge = _challenge(
            request, approval_id=approval.approval_id, account=FIXTURE_USER
        )
        outcome = await arch_pw.run_channel(
            FIXTURE_USER, store, challenge, request,
            bytearray(arch_pw.password, "utf-8"),
        )
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None
