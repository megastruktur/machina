"""Focused tests for the password challenge HTTP API (card 04-password-api).

Card ``04-password-api`` against contracts.md **C03 rule 4** (the human
decision and its bound one-use consumption), **C04 rule 2** (the secret
travels as one ``sudo -S`` stdin line — never argv/env), **C01**
(envelope, opaque identity, principal from the session only) and **C08
rule 2** (session → origin → CSRF on every mutation). The channel
semantics underneath are covered by ``tests/test_password_input.py``
(04-password-channel, including the REAL Arch sshd boundary); this
suite proves the HTTP surface joining them, i.e. the registered P04
scenario coverage: *use the actual protected fixture API to fulfill one
known challenge and attempt replay/wrong target; inspect sanitized logs
and the spent/expired decision state*.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain, with the REAL approval store and the REAL channel gate.
Only the ssh transport is a Popen stand-in over real pipe fds (the
channel unit suite owns the real-transport claims: stdin-only secret,
wipe, refusal classification, real sudo).

Secret hygiene: the fixture secret and credential are throwaway
synthetic constants for the disposable fixture DB; session/CSRF tokens
are minted at runtime and never printed; the assertions double as the
sanitized-logs negative control (the secret appears in NO response body,
NO error envelope and NO captured log record).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.auth.sessions import hash_token
from mai.database import create_app_engine
from mai.execution.commands import CommandRequest
from mai.execution.password_input import passworded_payload_hash
from mai.policy.approvals import Approval, ApprovalRequest, ApprovalStore
from mai.profiles.files import ProfileFileStore

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-04-password-api-only"
#: Throwaway challenge secret used as the presented sudo material.
SECRET = "challenge-s3cret-04-password-api-only"

ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

CAPABILITY = "shell.raw"
REQUESTER = "dispatcher"  # the host-side principal that requested the decision
ACCOUNT = "operator"  # the enrolled SSH/sudo target account
HOST = "127.0.0.1"
PORT = 2222
#: Inert OpenSSH-shaped fingerprint (43 base64 characters, schema shape).
HOST_KEY = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CREDENTIAL_REF = "MAI_SSH_KEY_FIXTURE"

CHALLENGE_PATH = "/api/v1/password-challenges"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the password challenge HTTP "
            "claims need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (D09): FK-safe and immune to future revisions
            # (the enumerated drop list went stale across migrations).
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("TRUNCATE approvals, admin_sessions, admin_credential")
        conn.commit()


def _expire(url: str, approval_id: uuid.UUID) -> None:
    """Test-only clock manipulation: push one decision into the past.

    ``created_at`` moves with it: the storage CHECK
    ``ck_approvals_expiry_after_creation`` is the boundary of last resort
    and refuses a naked expiry backdate — exactly as designed.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "UPDATE approvals SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' WHERE approval_id = %s",
            (str(approval_id),),
        )
        conn.commit()


def _build_app(
    url: str, data_root: Path, *, env_file: Path | None = None
) -> FastAPI:
    env: dict[str, str] = {
        "MAI_DATABASE_URL": url,
        "MAI_PUBLIC_ORIGINS": ORIGIN,
        "MAI_DATA_ROOT": str(data_root),
    }
    if env_file is not None:
        env["MAI_ENV_FILE"] = str(env_file)
    return create_app(env=env)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def fixture_env(tmp_path: Path) -> Path:
    """The configured server-side ``.env`` source (C03 rule 6) + its key.

    The transport identity key exists ONLY here: the wire can never name
    it (``ChallengeBody`` has no path fields); the enrolled profile names
    the reference ``MAI_SSH_KEY_FIXTURE`` and the lifespan-built resolver
    turns it into ``key_path`` at request time.
    """
    key_path = tmp_path / "transport" / "fixture-key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("fixture identity key material\n")
    env_file = tmp_path / "fixture.env"
    env_file.write_text(f"{CREDENTIAL_REF}={key_path}\n")
    env_file.chmod(0o600)  # the resolver refuses sources readable beyond owner
    return env_file


@pytest.fixture
async def approvals_app(
    migrated_url: str, tmp_path: Path, fixture_env: Path
):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(
        migrated_url, tmp_path / "data-root", env_file=fixture_env
    )
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()
    lane_engine = getattr(app.state, "password_lane_engine", None)
    if lane_engine is not None:
        await lane_engine.dispose()


@pytest.fixture
async def store(migrated_url: str):
    """The host-side producer store over its own pooled connections."""
    engine, factory = create_app_engine(migrated_url)
    yield ApprovalStore(factory)
    await engine.dispose()


@dataclass(slots=True)
class _Target:
    """The enrolled target: profile identity + SERVER-side transport material.

    ``key_path`` (what ``credential_ref`` resolves to through the
    configured ``.env`` source) and ``known_hosts_path`` (the canonical
    enrolled host-key store under the data root) never appear in any
    submission body — no test may name a transport path on the wire.
    """

    server_id: uuid.UUID
    env_file: Path
    key_path: Path
    known_hosts_path: Path
    credential_ref: str = CREDENTIAL_REF

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "host": HOST,
            "port": PORT,
            "user": ACCOUNT,
            "host_key": HOST_KEY,
            "credential_ref": self.credential_ref,
        }


def _enroll_target(tmp_path: Path, *, credential_ref: str = CREDENTIAL_REF) -> _Target:
    """Enroll one target profile in the app's data root (real store)."""
    server_id = uuid.uuid4()
    target = _Target(
        server_id=server_id,
        env_file=tmp_path / "fixture.env",
        key_path=tmp_path / "transport" / "fixture-key",
        known_hosts_path=(
            tmp_path / "data-root" / "servers" / str(server_id) / "known_hosts"
        ),
        credential_ref=credential_ref,
    )
    target.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    # Enrolled host-key pin stand-in (the explicit admin enrollment flow —
    # mai.execution.ssh.pin_host_key — writes the real file; the channel's
    # pre-consume isfile check is the contract under test here, never the
    # pin's content).
    target.known_hosts_path.touch(exist_ok=True)
    store = ProfileFileStore(tmp_path / "data-root")
    profile = (
        f"schema_version: 1\n"
        f"server_id: {target.server_id}\n"
        f"name: fixture target\n"
        f"prompt_revision: 1\n"
        f"environment: linux_ssh\n"
        f"privilege: sudo\n"
        f"capabilities:\n  - {CAPABILITY}\n"
        f"ssh:\n"
        f"  host: {HOST}\n"
        f"  port: {PORT}\n"
        f"  user: {ACCOUNT}\n"
        f"  host_key: \"{HOST_KEY}\"\n"
        f"  credential_ref: {credential_ref}\n"
    )
    store.write(target.server_id, profile, expected_revision=0)
    return target


@pytest.fixture
def enrolled_target(tmp_path: Path, fixture_env: Path) -> _Target:
    """One enrolled target over the fixture's server-side credential source."""
    return _enroll_target(tmp_path)


# ---------------------------------------------------------------------------
# Fake transport: Popen stand-in over real pipe fds (channel unit suite
# owns the real-process claims; here it only observably receives the
# secret or provably never spawns)
# ---------------------------------------------------------------------------


class _FakeProc:
    """Popen stand-in over REAL pipe fds (drives the real capture loop)."""

    def __init__(
        self,
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self._returncode = returncode
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        if stdout:
            os.write(w_out, stdout)
        if stderr:
            os.write(w_err, stderr)
        os.close(w_out)
        os.close(w_err)
        self.stdout = open(r_out, "rb", buffering=0)  # noqa: SIM115 - pipe handle
        self.stderr = open(r_err, "rb", buffering=0)  # noqa: SIM115 - pipe handle
        r_in, w_in = os.pipe()
        self._stdin_reader = r_in
        self.stdin = open(w_in, "wb", buffering=0)  # noqa: SIM115 - pipe handle
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def stdin_bytes(self) -> bytes:
        return os.read(self._stdin_reader, 4096)

    def dispose(self) -> None:
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _operation(target: _Target, **overrides: object) -> CommandRequest:
    """The bound benign operation (its payload hash is what is approved)."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": target.server_id,
        "display": "fixture display label",
        "timeout": 10.0,
        "output_budget": 4096,
        "argv": ("/usr/bin/id", "-u"),
    }
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


def _spawn_patch(
    monkeypatch: pytest.MonkeyPatch, *procs: _FakeProc
) -> list[list[str]]:
    """Queue Popen stand-ins; every denial test asserts zero calls."""
    queue = list(procs)
    calls: list[list[str]] = []

    def _spy(argv: list[str], **_kwargs: object) -> _FakeProc:
        calls.append(list(argv))
        if not queue:
            raise AssertionError("unexpected transport spawn")
        return queue.pop(0)

    monkeypatch.setattr(
        "mai.execution.password_input.subprocess.Popen", _spy
    )
    return calls


async def _pending(
    store: ApprovalStore,
    target: _Target,
    operation: CommandRequest,
) -> Approval:
    """One pending decision bound to the exact transported payload hash."""
    return await store.create(
        ApprovalRequest(
            capability_id=CAPABILITY,
            server_id=target.server_id,
            payload_hash=passworded_payload_hash(operation),
            policy_revision=1,
            created_by=REQUESTER,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )


# ---------------------------------------------------------------------------
# Minimal ASGI harness (no test-client dependency; bodies + cookies)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.decode("ascii").lower() == name:
                return value.decode("ascii")
        return None

    def header_all(self, name: str) -> list[str]:
        return [
            value.decode("ascii")
            for key, value in self.headers
            if key.decode("ascii").lower() == name
        ]

    def json(self) -> Any:
        return json.loads(self.body)


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 0),
) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable."""
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in (headers or {}).items()
    )
    if cookies:
        scope_headers.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        )
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        scope_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": scope_headers,
        "client": client,
        "server": ("fixture", 80),
    }
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - ServerErrorMiddleware re-raises after reply
        pass
    return response


def _set_cookies(response: HttpResponse) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in response.header_all("set-cookie"):
        name, _, value = raw.partition("=")
        parsed[name] = value.split(";", 1)[0]
    return parsed


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    assert "Traceback" not in response.body.decode()
    return body


async def login_session(app: FastAPI) -> dict[str, str]:
    """Bootstrap once and return the authenticated cookie jar."""
    bootstrap = await call_app(
        app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar = _set_cookies(login)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _submission_body(
    record: Approval,
    operation: CommandRequest,
    target: _Target,
    *,
    server_id: uuid.UUID | None = None,
    payload_hash: str | None = None,
    approval_id: uuid.UUID | None = None,
    account: str = ACCOUNT,
    identity: Mapping[str, Any] | None = None,
    secret: str = SECRET,
) -> dict[str, Any]:
    """The exact pinned challenge presentation (echoed field-for-field)."""
    body: dict[str, Any] = {
        "approval_id": str(approval_id or record.approval_id),
        "capability_id": record.capability_id,
        "server_id": str(server_id or record.server_id),
        "payload_hash": payload_hash or record.payload_hash,
        "policy_revision": record.policy_revision,
        "account": account,
        "operation": {
            "op_id": str(operation.op_id),
            "server_id": str(server_id or operation.server_id),
            "display": operation.display,
            "timeout": operation.timeout,
            "output_budget": operation.output_budget,
            "argv": list(operation.argv or ()),
            "shell": operation.shell,
            "working_directory": operation.working_directory,
        },
        "identity": dict(identity if identity is not None else target.identity),
        "secret": secret,
    }
    return body


_OMIT: Final = object()  # sentinel: deliberately omit the header/cookies


async def _submit(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
    *,
    origin: str | None = ORIGIN,
    csrf: Any = None,  # None ⇒ the jar's token; _OMIT ⇒ header absent
    cookies: Any = None,  # None ⇒ the jar; _OMIT ⇒ no cookie header
) -> HttpResponse:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["origin"] = origin
    if csrf is not _OMIT:
        headers["x-csrf-token"] = jar[CSRF_COOKIE] if csrf is None else str(csrf)
    use_cookies: Mapping[str, str] | None = (
        jar if cookies is None else (None if cookies is _OMIT else cookies)
    )
    return await call_app(
        app,
        "POST",
        CHALLENGE_PATH,
        json_body=dict(body),
        headers=headers,
        cookies=use_cookies,
    )


# ---------------------------------------------------------------------------
# Acceptance 1: authenticated CSRF-protected bound submission forwards the
# secret to the channel without persistence or logging
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_challenge_fulfills_one_known_challenge(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact presentation + secret ⇒ decision spent once, outcome returned."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    proc = _FakeProc(0, stdout=b"1000\n")
    calls = _spawn_patch(monkeypatch, proc)
    jar = await login_session(approvals_app)

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 200, response.body
    view = response.json()
    assert set(view) == {
        "op_id",
        "server_id",
        "display",
        "status",
        "exit_code",
        "stdout",
        "stdout_bytes",
        "stdout_truncated",
        "stderr",
        "stderr_bytes",
        "stderr_truncated",
        "timed_out",
        "cwd_change_failed",
        "duration_seconds",
    }
    assert view["status"] == "succeeded"
    assert view["exit_code"] == 0
    assert view["op_id"] == str(operation.op_id)
    assert view["server_id"] == str(enrolled_target.server_id)
    # The secret traveled as ONE stdin line to the transport, never argv.
    assert proc.stdin_bytes() == SECRET.encode() + b"\n"
    argv_blob = "\x00".join(calls[0]).encode()
    assert SECRET.encode() not in argv_blob
    # The transport paths are SERVER-side only (path-injection fix): the
    # spawned argv carries the .env-resolved identity key and the
    # canonical enrolled known-hosts store — exactly what the server
    # resolved, never anything a client could have named.
    assert calls[0][calls[0].index("-i") + 1] == str(enrolled_target.key_path)
    known_hosts_options = [
        argument
        for argument in calls[0]
        if argument.startswith("UserKnownHostsFile=")
    ]
    assert known_hosts_options == [
        f"UserKnownHostsFile={enrolled_target.known_hosts_path}"
    ]
    # The consuming principal is the session's server-side identity only
    # (C01 rule 2): no body field could have supplied it.
    spent = await store.get(record.approval_id)
    assert spent is not None and spent.status == "consumed"
    assert spent.consumed_by == hash_token(jar[SESSION_COOKIE])
    # Nothing about the submission is echoed back.
    assert SECRET not in response.body.decode()
    proc.dispose()


@pytest.mark.anyio
async def test_challenge_is_submit_only_no_secret_read_route(
    approvals_app: FastAPI,
) -> None:
    """No generic secret-read endpoint exists on the registered surface."""
    schema = approvals_app.openapi()
    challenge_paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if "password" in path
    }
    assert set(challenge_paths) == {CHALLENGE_PATH}
    assert set(challenge_paths[CHALLENGE_PATH]) == {"post"}
    body_schema = schema["components"]["schemas"]["ChallengeBody"]
    assert "secret" in body_schema["properties"]
    # Transport paths never travel the wire (path-injection fix): the
    # contract itself refuses unknown fields, so a client-supplied
    # key_path/known_hosts_path is unrepresentable, not merely unused.
    assert "key_path" not in body_schema["properties"]
    assert "known_hosts_path" not in body_schema["properties"]
    assert body_schema.get("additionalProperties") is False
    outcome_properties = schema["components"]["schemas"][
        "ChallengeOutcomeView"
    ]["properties"]
    assert "secret" not in outcome_properties
    assert not any("password" in name.lower() for name in outcome_properties)


@pytest.mark.anyio
async def test_unavailable_lane_service_fails_closed(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the lane store the surface answers 503 and touches nothing."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    monkeypatch.setattr(approvals_app.state, "password_approvals", None)

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 503
    assert _error(response)["code"] == "unavailable"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


# ---------------------------------------------------------------------------
# Transport paths never travel the wire (path-injection fix round)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_client_supplied_transport_paths_are_rejected(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body carrying transport paths is a 422; nothing runs, nothing spent.

    Path-injection negative control: the client names a controller file
    probe for ``ssh -i`` and plants a known-hosts file that would defeat
    the C04 rule 1 pin. The wire has NO such fields (``extra="forbid"``)
    — the presentation is refused whole, never partially honored or
    silently ignored, and the honest presentation afterwards still spends
    the untouched decision.
    """
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    planted = tmp_path / "attacker-known-hosts"
    planted.write_text(f"{HOST} ssh-ed25519 AAAAattacker-controlled\n")
    body = _submission_body(record, operation, enrolled_target)
    body["key_path"] = "/etc/shadow"
    body["known_hosts_path"] = str(planted)

    response = await _submit(approvals_app, jar, body)

    assert response.status == 422
    assert _error(response)["code"] == "validation_failed"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"

    # The decision was never consumed: the honest (path-free) presentation
    # fulfills it, and the transport uses ONLY server-resolved material.
    proc = _FakeProc(0, stdout=b"1000\n")
    retry_calls = _spawn_patch(monkeypatch, proc)
    retry = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )
    assert retry.status == 200, retry.body
    assert len(retry_calls) == 1
    assert (
        retry_calls[0][retry_calls[0].index("-i") + 1]
        == str(enrolled_target.key_path)
    )
    proc.dispose()


@pytest.mark.anyio
async def test_unenrolled_known_hosts_refuses_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No enrolled known-hosts store ⇒ invalid_configuration, nothing spent.

    The host-key store is resolved server-side to the canonical enrolled
    path; a client cannot substitute a file of its choosing — the only
    escape is the explicit admin enrollment at that exact path.
    """
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    enrolled_target.known_hosts_path.unlink()

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 422
    assert _error(response)["code"] == "invalid_configuration"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"

    # Explicit (re-)enrollment at the canonical path — never a client-named
    # substitute — is the only way back to a spendable challenge.
    enrolled_target.known_hosts_path.touch()
    proc = _FakeProc(0, stdout=b"1000\n")
    retry_calls = _spawn_patch(monkeypatch, proc)
    retry = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )
    assert retry.status == 200, retry.body
    assert len(retry_calls) == 1
    proc.dispose()


@pytest.mark.anyio
async def test_unresolvable_credential_ref_refuses_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference the .env source does not name ⇒ invalid_configuration.

    C03 rule 6 server-side: the identity key comes ONLY from the
    configured source; a profile naming an unresolvable reference can
    enroll but never transport, and the failure consumes nothing.
    """
    other = _enroll_target(tmp_path, credential_ref="MAI_SSH_KEY_ABSENT")
    operation = _operation(other)
    record = await _pending(store, other, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)

    response = await _submit(approvals_app, jar, _submission_body(record, operation, other))

    assert response.status == 422
    assert _error(response)["code"] == "invalid_configuration"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_stale_identity_key_refuses_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference whose key file no longer exists refuses; nothing spent.

    The server-side resolution checks the resolved path (existing regular
    file) BEFORE the one-use gate — a stale controller-side key is an
    ``invalid_configuration`` 422, never an unhandled error and never a
    consumed decision.
    """
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    enrolled_target.key_path.unlink()

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 422
    assert _error(response)["code"] == "invalid_configuration"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_unconfigured_credential_source_fails_closed(
    migrated_url: str,
    tmp_path: Path,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without MAI_ENV_FILE the surface 503s before touching any state.

    The resolver is composition-bound (lifespan); with no configured
    source nothing can ever resolve, so the lane fails closed instead of
    falling back to any ambient secret source.
    """
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    try:
        async with _running(app):
            operation = _operation(enrolled_target)
            record = await _pending(store, enrolled_target, operation)
            calls = _spawn_patch(monkeypatch)
            jar = await login_session(app)

            response = await _submit(
                app, jar, _submission_body(record, operation, enrolled_target)
            )

            assert response.status == 503
            assert _error(response)["code"] == "unavailable"
            assert calls == []
            still = await store.get(record.approval_id)
            assert still is not None and still.status == "pending"
    finally:
        if app.state.engine is not None:
            await app.state.engine.dispose()
        lane_engine = getattr(app.state, "password_lane_engine", None)
        if lane_engine is not None:
            await lane_engine.dispose()


# ---------------------------------------------------------------------------
# Acceptance 2: wrong principal/target/expired/replayed challenges denied
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_replay_denied_and_decision_spent_once(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second identical submission is already_consumed; no second effect."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch, _FakeProc(0, stdout=b"1000\n"))
    jar = await login_session(approvals_app)
    body = _submission_body(record, operation, enrolled_target)

    first = await _submit(approvals_app, jar, body)
    assert first.status == 200
    second = await _submit(approvals_app, jar, body)

    assert second.status == 409
    assert _error(second)["code"] == "already_consumed"
    assert len(calls) == 1  # exactly one transport attempt ever
    spent = await store.get(record.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.anyio
async def test_wrong_target_denied_leaves_decision_pending(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval bound to another target is refused (store binding)."""
    other_target = uuid.uuid4()
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)

    # Presentation echoes the enrolled profile for a DIFFERENT server id;
    # the store's binding is what refuses it.
    body = _submission_body(
        record,
        operation,
        enrolled_target,
        server_id=other_target,
    )
    body["operation"]["server_id"] = str(other_target)
    response = await _submit(approvals_app, jar, body)

    assert response.status == 404  # no enrolled profile for that target
    assert _error(response)["code"] == "not_found"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_target_redirect_denied_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A presented identity the enrolled profile does not name is refused."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    redirected = dict(enrolled_target.identity)
    redirected["port"] = PORT + 1  # the profile pins this endpoint
    body = _submission_body(
        record, operation, enrolled_target, identity=redirected
    )

    response = await _submit(approvals_app, jar, body)

    assert response.status == 409
    assert _error(response)["code"] == "target_mismatch"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_altered_operation_denied_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely different command than the approved payload is refused."""
    operation = _operation(enrolled_target)
    altered = _operation(enrolled_target, argv=("/usr/bin/touch", "/tmp/x"))
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    body = _submission_body(
        record, altered, enrolled_target, payload_hash=record.payload_hash
    )

    response = await _submit(approvals_app, jar, body)

    assert response.status == 409
    assert _error(response)["code"] == "payload_mismatch"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_expired_challenge_denied(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backdated (expired) decision is refused; nothing runs or changes."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    _expire(migrated_url, record.approval_id)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 409
    assert _error(response)["code"] == "expired"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_unknown_approval_denied(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown decision id is not_found; nothing runs."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    body = _submission_body(
        record, operation, enrolled_target, approval_id=uuid.uuid4()
    )

    response = await _submit(approvals_app, jar, body)

    assert response.status == 404
    assert _error(response)["code"] == "not_found"
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("origin", "csrf", "cookies", "expected"),
    [
        (ORIGIN, None, _OMIT, (401, "unauthenticated")),
        (EVIL_ORIGIN, None, None, (403, "origin_rejected")),
        (ORIGIN, "mismatched", None, (403, "csrf_rejected")),
    ],
)
async def test_boundary_layers_reject_before_any_state_change(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None,
    csrf: Any,
    cookies: Any,
    expected: tuple[int, str],
) -> None:
    """Session → origin → CSRF run before anything is touched (ADR §5)."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    body = _submission_body(record, operation, enrolled_target)

    response = await _submit(
        approvals_app,
        jar,
        body,
        origin=origin,
        csrf=csrf,
        cookies=cookies,
    )

    status, code = expected
    assert response.status == status, response.body
    assert _error(response)["code"] == code
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_wrong_account_denied_before_consume(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sudo -S`` authenticates the invoking account only (binding)."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    body = _submission_body(
        record, operation, enrolled_target, account="someone-else"
    )

    response = await _submit(approvals_app, jar, body)

    assert response.status == 409
    assert _error(response)["code"] == "account_mismatch"
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_wrong_secret_classified_and_decision_spent(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classified sudo refusal is a 403; the decision is honestly spent."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    proc = _FakeProc(1, stderr=b"sudo: 1 incorrect password attempt\n")
    calls = _spawn_patch(monkeypatch, proc)
    jar = await login_session(approvals_app)

    response = await _submit(
        approvals_app, jar, _submission_body(record, operation, enrolled_target)
    )

    assert response.status == 403
    envelope = _error(response)
    assert envelope["code"] == "password_rejected"
    assert SECRET not in response.body.decode()
    assert len(calls) == 1  # exactly ONE authentication attempt, no retry
    spent = await store.get(record.approval_id)
    assert spent is not None and spent.status == "consumed"
    proc.dispose()


# ---------------------------------------------------------------------------
# Hygiene and envelope discipline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_invalid_secret_rejected_without_echoing_value(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newline cannot travel the single-line stdin protocol (pre-consume)."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch)
    jar = await login_session(approvals_app)
    body = _submission_body(
        record, operation, enrolled_target, secret="two\nlines"
    )

    response = await _submit(approvals_app, jar, body)

    assert response.status == 422
    assert _error(response)["code"] == "validation_failed"
    assert SECRET.encode() not in response.body  # "two\nlines" never echoed
    assert calls == []
    still = await store.get(record.approval_id)
    assert still is not None and still.status == "pending"


@pytest.mark.anyio
async def test_secret_never_reaches_any_log_record(
    approvals_app: FastAPI,
    store: ApprovalStore,
    enrolled_target: _Target,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanitized logs: a full deny+fulfill cycle logs nothing, ever."""
    operation = _operation(enrolled_target)
    record = await _pending(store, enrolled_target, operation)
    calls = _spawn_patch(monkeypatch, _FakeProc(0, stdout=b"1000\n"))
    jar = await login_session(approvals_app)
    body = _submission_body(record, operation, enrolled_target)

    with caplog.at_level(logging.DEBUG):
        denied = await _submit(
            approvals_app, jar, dict(body, secret="two\nlines")
        )
        fulfilled = await _submit(approvals_app, jar, body)

    assert denied.status == 422
    assert fulfilled.status == 200
    assert len(calls) == 1
    assert SECRET not in caplog.text
    assert "two" not in caplog.text or "lines" not in caplog.text

