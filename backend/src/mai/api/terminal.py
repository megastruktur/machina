"""Interactive SSH PTY terminal surface (uir task ``u2-terminal``).

Two endpoints over the canonical server profile:

- ``GET /api/v1/servers/{server_id}/terminal/info`` — session-authenticated
  wire view of the pinned SSH target plus the WebSocket path to attach to
  (safe method: session layer only, mirroring ``mai.api.servers`` reads).
- ``WebSocket /api/v1/servers/{server_id}/terminal`` — an authenticated
  interactive PTY bridged between this WebSocket and one system OpenSSH
  client attached to a remote pseudo-terminal (``ssh -tt``).

Transport discipline:

- Zero third-party SSH libraries: the transport is the pinned system
  ``ssh`` binary through :func:`mai.execution.ssh.ssh_arguments` (frozen
  probe option surface), the profile-referenced identity key through
  :func:`mai.execution.ssh.resolve_identity_key` and the enrolled host key
  through :func:`mai.execution.ssh.pin_host_key`.
  ``backend/tests/test_execution_dependencies.py`` holds the negative
  controls; this module adds only stdlib process/PTY plumbing
  (``pty``/``os``/``fcntl``/``termios``/``struct``/``subprocess``).
- Admission (mirrors the auth ADR layer order for a socket: session, then
  exact ``Origin`` allowlist): a socket that fails either layer is accepted
  and immediately closed with ``1008`` — no terminal bytes are exchanged.
  Internal unavailability (no session store / no profile store / no
  credential resolver) closes with ``1011``; a missing profile or an
  unresolvable credential closes with ``1008`` (the caller named an
  unknown or unconfigurable target).
- The remote command attaches to the durable ``tmux`` session ``mai`` when
  the target has ``tmux`` (``tmux new-session -A`` attaches or creates) and
  falls back to the account's login shell otherwise.
- Bounded bridge: PTY output moves through a bounded queue with reader
  pausing (a slow browser can never balloon controller memory), UTF-8 is
  decoded incrementally (multibyte sequences may split across reads), and
  the child's process group is terminated then killed on disconnect so no
  ``ssh`` survives the socket.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import uuid
from pathlib import Path
from typing import Final

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    ErrorEnvelope,
    SESSION_COOKIE,
    _require_session,
    render_error_envelope,
)
from mai.auth.sessions import AuthError, SessionStore
from mai.contracts.server import SshIdentity, parse_server_profile_text
from mai.credentials.env import EnvFileError, EnvResolver
from mai.execution.ssh import (
    SshConnectError,
    pin_host_key,
    resolve_identity_key,
    ssh_arguments,
)
from mai.profiles.files import (
    FileStoreErrorCode,
    ProfileFileError,
    ProfileFileStore,
)

__all__ = [
    "DEFAULT_TERM",
    "PTY_COLUMNS",
    "PTY_ROWS",
    "REMOTE_SHELL_CMD",
    "TMUX_SESSION",
    "TERMINAL_WS_PATH_TEMPLATE",
    "TerminalSessionInfoView",
    "register_terminal",
    "ssh_client_argv",
]

router = APIRouter(prefix="/api/v1/servers", tags=["terminal"])


#: Default terminal type exported to the local ssh process and remote shell.
DEFAULT_TERM: Final = "xterm-256color"
#: tmux session name every terminal attaches to (``tmux new-session -A -s mai``).
TMUX_SESSION: Final = "mai"
#: The remote command: attach to tmux when present, login shell otherwise.
REMOTE_SHELL_CMD: Final = (
    f'if [ -z "${{TERM:-}}" ] || [ "${{TERM}}" = "dumb" ]; then export TERM={DEFAULT_TERM}; fi; '
    "if command -v tmux >/dev/null 2>&1; then "
    f"exec tmux new-session -A -s {TMUX_SESSION}; "
    'else exec "${SHELL:-/bin/sh}" -l; fi'
)
#: Initial PTY size (rows/columns); the client resizes on fit.
PTY_ROWS: Final = 24
PTY_COLUMNS: Final = 80
#: The WebSocket path template reported by the info view.
TERMINAL_WS_PATH_TEMPLATE: Final = "/api/v1/servers/{server_id}/terminal"

#: PTY read chunk (one queue item); 64 chunks ≈ 4 MiB burst headroom before
#: the reader pauses against the bounded outbox.
_PTY_CHUNK_BYTES: Final = 64 * 1024
_OUTBOX_LIMIT: Final = 64
#: Grace between SIGTERM and SIGKILL for the ssh process group on disconnect.
_KILL_GRACE_SECONDS: Final = 2.0
#: Upper bound for one client-declared resize (sane terminal geometry).
_MAX_TERMINAL_COLS: Final = 1024
_MAX_TERMINAL_ROWS: Final = 1024


class TerminalSessionInfoView(BaseModel):
    """Wire view for opening a terminal against one pinned target."""

    server_id: uuid.UUID = Field(description="Pinned server identity (opaque UUID).")
    host: str = Field(description="SSH target host from the stored profile.")
    port: int = Field(description="SSH target port from the stored profile.")
    user: str = Field(description="SSH login user from the stored profile.")
    ws_path: str = Field(
        description="Authenticated WebSocket path that carries the terminal stream."
    )
    tmux_session: str = Field(
        description="tmux session name the remote command attaches to."
    )


def ssh_client_argv(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    remote_command: str,
) -> list[str]:
    """The terminal's ``ssh`` argv: frozen probe surface plus forced remote PTY.

    ``-tt`` goes immediately before the destination so ``ssh`` allocates a
    remote pseudo-terminal even though the local side is a PTY master that
    ssh does not consider "a real terminal" for single ``-t`` purposes.
    """
    argv = ssh_arguments(
        identity,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        remote_command=remote_command,
    )
    return [*argv[:-2], "-tt", *argv[-2:]]


# ---------------------------------------------------------------------------
# Admission (session → origin; the socket-shaped boundary layers)
# ---------------------------------------------------------------------------


def _origin_allowlist(app: FastAPI) -> frozenset[str]:
    """The exact-origin allowlist built by the lifespan (auth ADR §6.3)."""
    origins = getattr(app.state, "allowed_origins", None) or getattr(
        app.state, "public_origins", frozenset()
    )
    return frozenset(origins)


def _known_hosts_path(store: ProfileFileStore, server_id: uuid.UUID) -> Path:
    """The per-server pinned host-key file next to its canonical profile."""
    return Path(store.root) / "servers" / str(server_id) / "known_hosts"


async def _admission(
    websocket: WebSocket, server_id: uuid.UUID
) -> tuple[int, str] | None:
    """Validate the socket the way HTTP mutations are validated.

    Returns ``None`` when admitted; otherwise the ``(code, reason)`` close
    pair. Layer order mirrors ADR §5: the principal comes from the
    validated session only, then the exact ``Origin`` allowlist (``Referer``
    never counts and is not consulted).
    """
    app: FastAPI = websocket.app
    sessions: SessionStore | None = getattr(app.state, "sessions", None)
    if sessions is None:
        return 1011, "sessions_unavailable"
    token = websocket.cookies.get(SESSION_COOKIE, "")
    if not token:
        return 1008, "unauthenticated"
    try:
        await sessions.validate(token)
    except AuthError:
        return 1008, "unauthenticated"

    origin = websocket.headers.get("origin", "")
    if not origin or origin not in _origin_allowlist(app):
        return 1008, "origin_rejected"

    store: ProfileFileStore | None = getattr(app.state, "profiles", None)
    if store is None:
        return 1011, "profiles_unavailable"
    try:
        stored = store.read(server_id)
    except ProfileFileError:
        return 1008, "unknown_server"

    credentials: EnvResolver | None = getattr(app.state, "credentials", None)
    if credentials is None:
        return 1011, "credentials_unavailable"
    profile = parse_server_profile_text(stored.text)
    try:
        await asyncio.to_thread(
            resolve_identity_key, credentials, profile.ssh.credential_ref
        )
    except SshConnectError, EnvFileError:
        return 1008, "invalid_credential"
    return None


# ---------------------------------------------------------------------------
# HTTP info view
# ---------------------------------------------------------------------------


def _profile_service(request: Request) -> ProfileFileStore:
    """The lifespan-built profile store; without it fail closed (C02)."""
    store = getattr(request.app.state, "profiles", None)
    if store is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="canonical profile service is unavailable",
        )
    return store


@router.get(
    "/{server_id}/terminal/info",
    status_code=HTTP_200_OK,
    response_model=TerminalSessionInfoView,
    summary="Terminal attach information",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for this server id",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Canonical profile service unavailable",
        },
    },
)
async def terminal_info(
    request: Request, server_id: uuid.UUID
) -> TerminalSessionInfoView:
    """Return the pinned SSH target and the WebSocket path to attach to.

    Safe method: session layer only (mirrors ``GET /api/v1/servers/{id}``).
    """
    await _require_session(request)
    store = _profile_service(request)
    try:
        stored = store.read(server_id)
    except ProfileFileError as exc:
        if exc.code is FileStoreErrorCode.NOT_FOUND:
            return render_error_envelope(
                request,
                HTTP_404_NOT_FOUND,
                "not_found",
                "no profile stored for this server id",
            )
        raise
    profile = parse_server_profile_text(stored.text)
    return TerminalSessionInfoView(
        server_id=server_id,
        host=profile.ssh.host,
        port=profile.ssh.port,
        user=profile.ssh.user,
        ws_path=TERMINAL_WS_PATH_TEMPLATE.format(server_id=server_id),
        tmux_session=TMUX_SESSION,
    )


# ---------------------------------------------------------------------------
# PTY ⇄ WebSocket bridge
# ---------------------------------------------------------------------------


class _PtyBridge:
    """Bounded PTY→WebSocket pump with reader pausing and incremental UTF-8."""

    def __init__(
        self, websocket: WebSocket, master_fd: int, loop: asyncio.AbstractEventLoop
    ):
        self._websocket = websocket
        self._fd = master_fd
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_OUTBOX_LIMIT)
        self._pending: list[bytes] = []
        self._reader_attached = False
        self._eof = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    # -- reader side (event-loop callback) ---------------------------------

    def attach(self) -> None:
        self._loop.add_reader(self._fd, self._on_readable)
        self._reader_attached = True

    def detach(self) -> None:
        if self._reader_attached:
            self._loop.remove_reader(self._fd)
            self._reader_attached = False

    def stop(self) -> None:
        """Unblock the pump exactly once (EOF marker through the queue)."""
        self._offer(None)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, _PTY_CHUNK_BYTES)
        except BlockingIOError, InterruptedError:
            return
        except OSError:
            data = b""  # EIO: the child closed its slave side
        if not data:
            self._eof = True
            self.detach()
            self.stop()
            return
        if not self._offer(data):
            self.detach()
            self._pending.append(data)

    def _offer(self, item: bytes | None) -> bool:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        return True

    def _resume(self) -> None:
        """Re-queue paused output and re-arm the reader once there is room."""
        while self._pending and self._offer(self._pending[0]):
            self._pending.pop(0)
        if not self._pending and not self._reader_attached:
            if self._eof:
                self.stop()
            else:
                self.attach()

    # -- pump side (task) ---------------------------------------------------

    async def pump(self) -> None:
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                await self._websocket.send_text(self._decoder.decode(chunk))
                self._resume()
        except Exception:
            return  # the socket died: the reaper thread owns the teardown now


async def _write_input(master_fd: int, data: bytes) -> None:
    """Write terminal input to the non-blocking PTY master, EAGAIN-tolerant.

    A partial write returns ``n > 0`` before any EAGAIN, so the loop never
    silently drops an already-accepted prefix; once the slave side is gone
    the session is dying and remaining bytes are discarded.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(master_fd, view)
            view = view[written:]
        except BlockingIOError, InterruptedError:
            await asyncio.sleep(0.005)
        except OSError:
            return


def _resize(master_fd: int, cols: int, rows: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)


async def _control_frame(master_fd: int, message: str) -> bool:
    """Handle one JSON control frame; ``False`` means "treat as raw input"."""
    try:
        parsed = json.loads(message)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    kind = parsed.get("type")
    if kind == "input":
        data = parsed.get("data")
        if isinstance(data, str):
            await _write_input(master_fd, data.encode("utf-8"))
            return True
        return False
    if kind == "resize":
        cols = parsed.get("cols")
        rows = parsed.get("rows")
        if (
            isinstance(cols, int)
            and isinstance(rows, int)
            and 0 < cols <= _MAX_TERMINAL_COLS
            and 0 < rows <= _MAX_TERMINAL_ROWS
        ):
            try:
                _resize(master_fd, cols, rows)
            except OSError:
                pass
            return True
        return False
    return False


def _spawn(
    argv: list[str], rows: int, cols: int
) -> tuple[subprocess.Popen[bytes], int]:
    """Open one PTY and start ``argv`` attached to its slave side."""
    master_fd, slave_fd = pty.openpty()
    try:
        _resize(master_fd, cols, rows)
        os.set_blocking(master_fd, False)
        child_env = os.environ.copy()
        if not child_env.get("TERM") or child_env.get("TERM") == "dumb":
            child_env["TERM"] = DEFAULT_TERM
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)  # the child holds its own copy now
    return process, master_fd


def _signal_group(process: subprocess.Popen[bytes]) -> bool:
    """SIGTERM the session leader's group now; ``False`` when already gone."""
    if process.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        return False
    return True


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the group; escalate to SIGKILL after the grace window."""
    if not _signal_group(process):
        return
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
        try:
            process.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _reap_child(process: subprocess.Popen[bytes], master_fd: int) -> None:
    """Finish the teardown off the event loop (cancellation-proof).

    Interactive shells ignore SIGTERM (job control), so the escalation half
    of :func:`_terminate_group` must never depend on a live cancellable
    task: this runs on its own daemon thread.
    """
    _terminate_group(process)
    try:
        os.close(master_fd)
    except OSError:
        pass


@router.websocket("/{server_id}/terminal")
async def _terminal_socket(websocket: WebSocket, server_id: uuid.UUID) -> None:
    """One authenticated interactive terminal (see module docstring)."""
    await websocket.accept()
    rejection = await _admission(websocket, server_id)
    if rejection is not None:
        await websocket.close(code=rejection[0], reason=rejection[1])
        return

    app: FastAPI = websocket.app
    store: ProfileFileStore = app.state.profiles
    stored = store.read(server_id)
    profile = parse_server_profile_text(stored.text)
    identity = SshIdentity(
        host=profile.ssh.host,
        port=profile.ssh.port,
        user=profile.ssh.user,
        host_key=profile.ssh.host_key,
        credential_ref=profile.ssh.credential_ref,
    )

    try:
        key_path: Path = await asyncio.to_thread(
            resolve_identity_key, app.state.credentials, profile.ssh.credential_ref
        )
        known_hosts_path = _known_hosts_path(store, server_id)
        if not known_hosts_path.exists():
            await asyncio.to_thread(pin_host_key, identity, known_hosts_path)
        argv = ssh_client_argv(
            identity,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            remote_command=REMOTE_SHELL_CMD,
        )
        process, master_fd = await asyncio.to_thread(
            _spawn, argv, PTY_ROWS, PTY_COLUMNS
        )
    except SshConnectError, EnvFileError, OSError, ValueError:
        await websocket.close(code=1011, reason="terminal_spawn_failed")
        return

    loop = asyncio.get_running_loop()
    bridge = _PtyBridge(websocket, master_fd, loop)
    bridge.attach()
    pump_task = loop.create_task(bridge.pump())
    try:
        while True:
            message = await websocket.receive_text()
            if message.startswith("{") and await _control_frame(master_fd, message):
                continue
            await _write_input(master_fd, message.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        bridge.detach()
        # The immediate SIGTERM must not sit behind a cancellable await: a
        # host shutdown (or the test transport's scope cancel) can strike
        # the moment the client disconnects. Escalation continues on a
        # daemon thread either way.
        _signal_group(process)
        threading.Thread(
            target=_reap_child,
            args=(process, master_fd),
            name="mai-terminal-reap",
            daemon=True,
        ).start()
        bridge.stop()
        try:
            await asyncio.wait_for(pump_task, timeout=_KILL_GRACE_SECONDS)
        except Exception:
            pump_task.cancel()


def register_terminal(app: FastAPI) -> None:
    """Register the terminal surface under the shared boundary (api-wiring)."""
    app.include_router(router)
