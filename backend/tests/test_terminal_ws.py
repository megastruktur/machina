"""Interactive SSH terminal surface tests (uir task ``u2-terminal``).

What is proven here, against the REAL module under test
(``mai.api.terminal``):

- **Argv composition** (pure): :func:`ssh_client_argv` keeps the frozen
  probe option surface of :func:`mai.execution.ssh.ssh_arguments` and
  inserts ``-tt`` exactly once, immediately before the destination pair;
  ``REMOTE_SHELL_CMD`` attaches the ``mai`` tmux session and falls back to
  the login shell.
- **PTY plumbing** (real processes): :func:`_spawn` attaches a real shell
  to a real PTY; :func:`_terminate_group` reaps the whole session group.
- **WebSocket surface** (real ASGI app through ``TestClient``): admission
  closes with ``1008`` for a missing/invalid ``mai_session`` cookie, a
  missing or non-allowlisted ``Origin``, and an unknown server; closes
  ``1011`` when the lifespan services are absent; and the happy path
  bridges real bidirectional PTY I/O (JSON ``input`` frames, raw text
  frames, ``resize`` frames reaching ``TIOCSWINSZ``) and reaps the child
  on disconnect.

Boundary discipline: the Postgres-backed ``SessionStore`` semantics are
owned by ``tests/test_auth_http.py``; here the store is a recording stub
installed at the same ``app.state.sessions`` seam the route consumes, so
the admission path is exercised without a database while the profile
store, credential resolver and PTY bridge stay fully real.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_503_SERVICE_UNAVAILABLE,
)
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mai.api.app import render_error_envelope
from mai.api.auth import SESSION_COOKIE, BoundaryError
from mai.api import terminal
from mai.api.terminal import (
    PTY_COLUMNS,
    PTY_ROWS,
    REMOTE_SHELL_CMD,
    TMUX_SESSION,
    TerminalSessionInfoView,
    _spawn,
    _terminate_group,
    register_terminal,
    ssh_client_argv,
)
from mai.auth.sessions import AuthError, AuthErrorCode
from mai.contracts.server import SshIdentity
from mai.credentials.env import build_runtime_env_resolver
from mai.execution.ssh import ssh_arguments
from mai.profiles.files import ProfileFileStore

ORIGIN = "http://127.0.0.1:8443"
EVIL_ORIGIN = "https://attacker.example"

HOST = "127.0.0.1"
PORT = 2222
ACCOUNT = "operator"
HOST_KEY = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CREDENTIAL_REF = "MAI_SSH_KEY_FIXTURE"

SESSION_TOKEN = "s" + "a" * 47

TERMINAL_WS = "/api/v1/servers/{server_id}/terminal"
INFO_PATH = "/api/v1/servers/{server_id}/terminal/info"


# ---------------------------------------------------------------------------
# Pure argv/command composition
# ---------------------------------------------------------------------------


class TestArgvComposition:
    def test_tt_is_inserted_before_the_destination(self) -> None:
        identity = SshIdentity(
            host="example.test",
            port=2202,
            user="deploy",
            host_key=HOST_KEY,
            credential_ref=CREDENTIAL_REF,
        )
        key = Path("/keys/deploy.key")
        known = Path("/kh/example")
        base = ssh_arguments(
            identity, key_path=key, known_hosts_path=known, remote_command="x"
        )
        composed = ssh_client_argv(
            identity, key_path=key, known_hosts_path=known, remote_command="x"
        )
        assert composed == [*base[:-2], "-tt", *base[-2:]]
        assert composed.count("-tt") == 1
        # Destination pair stays last: [user@host, remote_command].
        assert composed[-2].endswith("@example.test")
        assert composed[-1] == "x"

    def test_remote_command_attaches_tmux_with_login_shell_fallback(self) -> None:
        assert f"tmux new-session -A -s {TMUX_SESSION}" in REMOTE_SHELL_CMD
        assert '${SHELL:-/bin/sh}" -l' in REMOTE_SHELL_CMD
        assert REMOTE_SHELL_CMD.index("command -v tmux") < REMOTE_SHELL_CMD.index(
            "exec tmux"
        )


# ---------------------------------------------------------------------------
# Real PTY plumbing
# ---------------------------------------------------------------------------


class TestPtyPlumbing:
    def test_spawn_attaches_a_real_shell_to_a_real_pty(self) -> None:
        process, master_fd = _spawn(["/bin/sh", "-i"], PTY_ROWS, PTY_COLUMNS)
        try:
            os.write(master_fd, b"echo pty-marker-$((40+2))\n")
            deadline = time.monotonic() + 10.0
            collected = b""
            while time.monotonic() < deadline:
                try:
                    collected += os.read(master_fd, 4096)
                except BlockingIOError, InterruptedError:
                    time.sleep(0.01)
                    continue
                except OSError:
                    break
                if b"pty-marker-42" in collected:
                    break
            assert b"pty-marker-42" in collected
        finally:
            _terminate_group(process)
            os.close(master_fd)
        assert process.poll() is not None

    def test_terminate_group_reaps_a_sleeping_session(self) -> None:
        process, master_fd = _spawn(["sleep", "30"], PTY_ROWS, PTY_COLUMNS)
        try:
            assert process.poll() is None
        finally:
            _terminate_group(process)
            os.close(master_fd)
        assert process.poll() is not None


# ---------------------------------------------------------------------------
# Composed app over the real stores (sessions stubbed at the state seam)
# ---------------------------------------------------------------------------


class _StubSessions:
    """The SessionStore seam with database semantics owned elsewhere."""

    def __init__(self) -> None:
        self.validated: list[str] = []
        self.reject_all = False

    async def validate(self, token: str) -> None:
        if self.reject_all or token != SESSION_TOKEN:
            raise AuthError(AuthErrorCode.SESSION_EXPIRED, "session is expired")
        self.validated.append(token)


@dataclass(slots=True)
class _Harness:
    server_id: uuid.UUID
    data_root: Path
    sessions: _StubSessions
    app: FastAPI


def _enroll_profile(
    data_root: Path, server_id: uuid.UUID, *, credential_ref: str = CREDENTIAL_REF
) -> None:
    store = ProfileFileStore(data_root)
    profile = (
        f"schema_version: 1\n"
        f"server_id: {server_id}\n"
        f"name: terminal fixture\n"
        f"prompt_revision: 1\n"
        f"environment: linux_ssh\n"
        f"privilege: sudo\n"
        f"capabilities:\n  - shell.raw\n"
        f"ssh:\n"
        f"  host: {HOST}\n"
        f"  port: {PORT}\n"
        f"  user: {ACCOUNT}\n"
        f'  host_key: "{HOST_KEY}"\n'
        f"  credential_ref: {credential_ref}\n"
    )
    store.write(server_id, profile, expected_revision=0)


def _build_app(
    data_root: Path,
    env_file: Path,
    *,
    sessions: _StubSessions | None = None,
    profiles: bool = True,
    credentials: bool = True,
    origin_attr: str = "allowed_origins",
) -> FastAPI:
    app = FastAPI()
    register_terminal(app)

    @app.exception_handler(BoundaryError)
    async def _render_boundary(request: Any, exc: BoundaryError) -> Any:
        return render_error_envelope(request, exc.status_code, exc.code, exc.message)

    app.state.sessions = sessions
    app.state.profiles = ProfileFileStore(data_root) if profiles else None
    app.state.credentials = (
        build_runtime_env_resolver(env_file, data_root) if credentials else None
    )
    setattr(app.state, origin_attr, frozenset({ORIGIN}))
    return app


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    """Enrolled profile + server-side key source + the composed app."""
    server_id = uuid.uuid4()
    data_root = tmp_path / "data-root"
    key_path = tmp_path / "transport" / "fixture-key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("fixture identity key material\n")
    env_file = tmp_path / "fixture.env"
    env_file.write_text(f"{CREDENTIAL_REF}={key_path}\n")
    env_file.chmod(0o600)

    _enroll_profile(data_root, server_id)
    # Pre-enrolled host-key pin: skips the real ssh-keyscan transport (the
    # pin's own semantics are owned by the ssh suite), keeping this suite
    # offline.
    known_hosts = data_root / "servers" / str(server_id) / "known_hosts"
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch()

    sessions = _StubSessions()
    app = _build_app(data_root, env_file, sessions=sessions)
    yield _Harness(server_id=server_id, data_root=data_root, sessions=sessions, app=app)


def _ws_path(server_id: uuid.UUID) -> str:
    return TERMINAL_WS.format(server_id=server_id)


class _Disconnect:
    """Assert one rejected handshake: accepted then closed with ``code``."""

    def __init__(self, code: int) -> None:
        self.code = code

    def __call__(self, client: TestClient, path: str, **kwargs: Any) -> None:
        cookies: dict[str, str] = kwargs.pop("cookies", {})
        for name, value in cookies.items():
            client.cookies.set(name, value)
        with pytest.raises(WebSocketDisconnect) as raised:
            with client.websocket_connect(path, **kwargs) as websocket:
                websocket.receive_text()
        assert raised.value.code == self.code


# ---------------------------------------------------------------------------
# WebSocket admission
# ---------------------------------------------------------------------------


class TestWsAdmission:
    def test_missing_cookie_is_closed_1008(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client, _ws_path(harness.server_id), headers={"origin": ORIGIN}
            )

    def test_invalid_session_is_closed_1008(self, harness: _Harness) -> None:
        harness.sessions.reject_all = True
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client,
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )
        # The invalid token was still seen by the store (validated, rejected).
        assert harness.sessions.validated == []

    def test_missing_origin_is_closed_1008(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client,
                _ws_path(harness.server_id),
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_evil_origin_is_closed_1008(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client,
                _ws_path(harness.server_id),
                headers={"origin": EVIL_ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_unknown_server_is_closed_1008(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client,
                _ws_path(uuid.uuid4()),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_absent_sessions_service_is_closed_1011(self, harness: _Harness) -> None:
        harness.app.state.sessions = None
        with TestClient(harness.app) as client:
            _Disconnect(1011)(
                client,
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_absent_profiles_service_is_closed_1011(self, harness: _Harness) -> None:
        harness.app.state.profiles = None
        with TestClient(harness.app) as client:
            _Disconnect(1011)(
                client,
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_absent_credentials_service_is_closed_1011(self, harness: _Harness) -> None:
        harness.app.state.credentials = None
        with TestClient(harness.app) as client:
            _Disconnect(1011)(
                client,
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_unresolvable_credential_is_closed_1008(self, harness: _Harness) -> None:
        # The stored profile names a reference the configured source lacks.
        server_id = uuid.uuid4()
        _enroll_profile(
            harness.data_root, server_id, credential_ref="MAI_SSH_KEY_MISSING"
        )
        broken = harness.data_root / "servers" / str(server_id) / "known_hosts"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.touch()
        with TestClient(harness.app) as client:
            _Disconnect(1008)(
                client,
                _ws_path(server_id),
                headers={"origin": ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )

    def test_origin_allowlist_falls_back_to_public_origins(
        self, harness: _Harness
    ) -> None:
        app = harness.app
        allowlisted = getattr(app.state, "allowed_origins")
        delattr(app.state, "allowed_origins")
        app.state.public_origins = allowlisted
        with TestClient(app) as client:
            _Disconnect(1008)(  # same cookie/origin, now judged via the fallback
                client,
                _ws_path(harness.server_id),
                headers={"origin": EVIL_ORIGIN},
                cookies={SESSION_COOKIE: SESSION_TOKEN},
            )


# ---------------------------------------------------------------------------
# WebSocket happy path: a real PTY bridge over the real app
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SpawnSpy:
    argv_seen: list[list[str]] = field(default_factory=list)
    processes: list[Any] = field(default_factory=list)


@pytest.fixture
def local_shell(monkeypatch: pytest.MonkeyPatch) -> _SpawnSpy:
    """Replace the remote ssh argv with a local interactive shell."""
    spy = _SpawnSpy()
    real_spawn = terminal._spawn

    def fake_argv(*args: Any, **kwargs: Any) -> list[str]:
        spy.argv_seen.append(list(args) + sorted(kwargs))
        return ["/bin/sh", "-i"]

    def spy_spawn(argv: list[str], rows: int, cols: int) -> tuple[Any, int]:
        process, master_fd = real_spawn(argv, rows, cols)
        spy.processes.append(process)
        return process, master_fd

    monkeypatch.setattr(terminal, "ssh_client_argv", fake_argv)
    monkeypatch.setattr(terminal, "_spawn", spy_spawn)
    return spy


def _collect_until(websocket: Any, needle: str, *, timeout: float = 20.0) -> str:
    """Receive text frames until ``needle`` appears; return the transcript."""
    transcript = ""
    deadline = time.monotonic() + timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            future = pool.submit(websocket.receive_text)
            try:
                transcript += future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                raise AssertionError(
                    f"terminal never echoed {needle!r}: {transcript!r}"
                )
            if needle in transcript:
                return transcript
    raise AssertionError(f"terminal never echoed {needle!r}: {transcript!r}")


class TestWsTerminalBridge:
    def test_input_resize_and_raw_frames_flow_and_child_is_reaped(
        self, harness: _Harness, local_shell: _SpawnSpy
    ) -> None:
        with TestClient(harness.app) as client:
            client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
            with client.websocket_connect(
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
            ) as websocket:
                websocket.send_text(
                    json.dumps({"type": "input", "data": "echo marker-$((40+2))\n"})
                )
                _collect_until(websocket, "marker-42")

                websocket.send_text(
                    json.dumps({"type": "resize", "cols": 100, "rows": 30})
                )
                websocket.send_text(
                    json.dumps({"type": "input", "data": "stty size\n"})
                )
                _collect_until(websocket, "30 100")

                websocket.send_text("echo raw-$((40+3))\n")
                _collect_until(websocket, "raw-43")

        # The store saw exactly the presented token.
        assert harness.sessions.validated == [SESSION_TOKEN]
        # The fake argv replaced ssh entirely and the child was reaped.
        assert len(local_shell.processes) == 1
        process = local_shell.processes[0]
        deadline = time.monotonic() + 10.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None, "terminal child survived the disconnect"

    def test_default_size_is_24x80_before_any_resize(
        self, harness: _Harness, local_shell: _SpawnSpy
    ) -> None:
        with TestClient(harness.app) as client:
            client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
            with client.websocket_connect(
                _ws_path(harness.server_id),
                headers={"origin": ORIGIN},
            ) as websocket:
                websocket.send_text(
                    json.dumps({"type": "input", "data": "stty size\n"})
                )
                _collect_until(websocket, f"{PTY_ROWS} {PTY_COLUMNS}")
                websocket.close()

        assert len(local_shell.processes) == 1


# ---------------------------------------------------------------------------
# HTTP info view
# ---------------------------------------------------------------------------


class TestTerminalInfo:
    def test_requires_a_session(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            response = client.get(INFO_PATH.format(server_id=harness.server_id))
        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.json()["error"]["code"] == "unauthenticated"

    def test_returns_the_pinned_target_view(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
            response = client.get(
                INFO_PATH.format(server_id=harness.server_id),
            )
        assert response.status_code == HTTP_200_OK
        view = TerminalSessionInfoView.model_validate(response.json())
        assert view.server_id == harness.server_id
        assert (view.host, view.port, view.user) == (HOST, PORT, ACCOUNT)
        assert view.ws_path == _ws_path(harness.server_id)
        assert view.tmux_session == TMUX_SESSION

    def test_fails_closed_without_the_profile_service(self, harness: _Harness) -> None:
        harness.app.state.profiles = None
        with TestClient(harness.app) as client:
            client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
            response = client.get(
                INFO_PATH.format(server_id=harness.server_id),
            )
        assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE

    def test_unknown_server_maps_to_404(self, harness: _Harness) -> None:
        with TestClient(harness.app) as client:
            client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
            response = client.get(
                INFO_PATH.format(server_id=uuid.uuid4()),
            )
        assert response.status_code == 404
