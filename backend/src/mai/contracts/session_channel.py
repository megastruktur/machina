"""Versioned session-channel frame contracts (D06).

Pure module: no env access, no IO, no clock reads. The WebSocket session
channel speaks EXACTLY these discriminated frames (``type`` discriminator,
``extra="forbid"``, every field described and bounded per the D01 contract
discipline):

- **Client frames** — ``hello`` (protocol version + replay cursor + the
  CSRF double-submit proof), ``subscribe`` (re-subscribe/replay from a
  cursor), ``ack`` (the client's own committed cursor), ``ping``.
- **Server frames** — ``hello_ok``, ``snapshot`` (bounded newest page when
  no cursor), ``entries`` (bounded ordered replay page), ``delta``
  (VOLATILE uncommitted stream text — never a committed entry),
  ``committed`` (ONE durable entry, exactly once per ``seq`` per
  connection), ``run_state``, ``resync_required``, ``error`` (bounded,
  VALUE-FREE — never content, never secrets), ``pong``.

Stream identity rules (sfc_PLAN D06):

- ``stream_id`` identifies one RUN's volatile text stream; a ``delta`` is
  bound to ``(stream_id, delta_id)`` and is durable NOWHERE.
- a ``committed`` entry is bound to ``entry_id`` + ``seq``; the server
  delivers each committed ``seq`` exactly ONCE per connection.
- a volatile delta can NEVER be promoted to a committed entry by the
  client: the client surface has no frame that carries entry payloads at
  all (fail-closed vocabulary).
"""

from __future__ import annotations

import json
from typing import Annotated, Final, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from uuid import UUID

from mai.contracts.sessions import (
    MAX_PAGE_BYTES,
    HISTORY_PAGE_MAX_ENTRIES,
    SessionContractError,
    SessionEntryView,
    SessionErrorCode,
    SessionFieldError,
    SessionRunState,
)

__all__ = [
    "CHANNEL_PROTOCOL_VERSION",
    "CHANNEL_NOTIFY_CHANNEL",
    "ClientChannelFrame",
    "ClientHelloFrame",
    "ClientAckFrame",
    "ClientPingFrame",
    "ClientSubscribeFrame",
    "MAX_CHANNEL_ERROR_CODE_CHARS",
    "MAX_CHANNEL_REASON_CHARS",
    "MAX_CHANNEL_SNAPSHOT_ENTRIES",
    "MAX_FRAME_BYTES",
    "MAX_STREAM_TEXT_CHARS",
    "SERVER_FRAME_TYPES",
    "ServerChannelErrorFrame",
    "ServerChannelFrame",
    "ServerCommittedFrame",
    "ServerDeltaFrame",
    "ServerEntriesFrame",
    "ServerHelloOkFrame",
    "ServerPongFrame",
    "ServerResyncRequiredFrame",
    "ServerRunStateFrame",
    "ServerSnapshotFrame",
    "StreamIdentity",
    "validate_client_frame",
    "validate_server_frame",
]

#: The one protocol version this surface speaks; a ``hello`` naming
#: anything else is refused with ``version_unsupported``.
CHANNEL_PROTOCOL_VERSION: Final[int] = 1

#: Frame byte ceiling (AGENTS.md capability cap): a client frame beyond
#: this is refused before parsing (bounded, value-free).
MAX_FRAME_BYTES: Final[int] = 64 * 1024

#: Bounded human-reason / code fields on server frames.
MAX_CHANNEL_REASON_CHARS: Final[int] = 200
MAX_CHANNEL_ERROR_CODE_CHARS: Final[int] = 64

#: The newest-entry snapshot bound when the client sends no cursor.
MAX_CHANNEL_SNAPSHOT_ENTRIES: Final[int] = 30

#: Volatile delta text bound (a delta is a bounded stream chunk, never a
#: transcript-sized payload).
MAX_STREAM_TEXT_CHARS: Final[int] = 4096

#: The dedicated PostgreSQL NOTIFY channel (payload: session id + latest
#: cursor ONLY — never content, never secrets).
CHANNEL_NOTIFY_CHANNEL: Final[str] = "mai_session_events"

#: Server frame type vocabulary (mirrored value-free for operators/tests).
SERVER_FRAME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "hello_ok",
        "snapshot",
        "entries",
        "delta",
        "committed",
        "run_state",
        "resync_required",
        "error",
        "pong",
    }
)


class _ChannelFrame(BaseModel):
    """Shared frame discipline: closed vocabulary, versioned by the
    protocol constant carried in ``hello``/``hello_ok``."""

    model_config = ConfigDict(extra="forbid")


# --- client frames -----------------------------------------------------------------


class ClientHelloFrame(_ChannelFrame):
    """Open one channel: protocol handshake + replay cursor + the CSRF
    double-submit proof (constant-time compared against the cookie)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["hello"] = Field(
        default="hello",
        description="Frame discriminator: channel open request",
    )
    protocol_version: int = Field(
        ge=1,
        le=64,
        description="Protocol version the client speaks (this server "
        f"accepts exactly {CHANNEL_PROTOCOL_VERSION}; anything else is "
        "refused version_unsupported)",
    )
    after_seq: int | None = Field(
        default=None,
        ge=1,
        description="Replay cursor: committed entries strictly after this "
        "sequence are replayed; None means newest-page snapshot",
    )
    csrf_token: str = Field(
        min_length=1,
        max_length=256,
        description="Double-submit CSRF proof equal to the mai_csrf cookie",
    )
    client_request_id: UUID = Field(
        description="Client idempotency key of this channel open",
    )


class ClientSubscribeFrame(_ChannelFrame):
    """(Re-)subscribe from a cursor: bounded replay of committed entries
    after ``after_seq``, then live delivery."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"] = Field(
        default="subscribe",
        description="Frame discriminator: replay/re-subscribe request",
    )
    after_seq: int | None = Field(
        default=None,
        ge=1,
        description="Replay cursor for this subscription (None: newest page)",
    )
    client_request_id: UUID = Field(
        description="Client idempotency key of this subscription request",
    )


class ClientAckFrame(_ChannelFrame):
    """Advance THIS connection's committed cursor (affects only the
    connection's own catch-up; the durable store is the only authority)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ack"] = Field(
        default="ack",
        description="Frame discriminator: committed-cursor acknowledgement",
    )
    after_seq: int = Field(
        ge=1,
        description="The client's own committed cursor (last handled sequence)",
    )


class ClientPingFrame(_ChannelFrame):
    """Application-level heartbeat probe; answered by ``pong``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ping"] = Field(
        default="ping",
        description="Frame discriminator: heartbeat probe",
    )


ClientChannelFrame = Annotated[
    ClientHelloFrame
    | ClientSubscribeFrame
    | ClientAckFrame
    | ClientPingFrame,
    Field(discriminator="type"),
]


# --- server frames -----------------------------------------------------------------


class ServerHelloOkFrame(_ChannelFrame):
    """Handshake accepted: protocol pinned, stream/scope identity and the
    session's latest committed sequence."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["hello_ok"] = Field(
        default="hello_ok",
        description="Frame discriminator: handshake accepted",
    )
    protocol_version: int = Field(
        ge=1,
        description="Protocol version the server pinned for this connection",
    )
    session_id: UUID = Field(
        description="Session identity this connection is bound to",
    )
    server_id: UUID = Field(
        description="Server scope pinning the bound session",
    )
    stream_id: str = Field(
        min_length=1,
        max_length=128,
        description="Volatile stream identity of this connection "
        "(deltas are bound to it; committed entries are not)",
    )
    latest_seq: int = Field(
        ge=0,
        description="Highest committed entry sequence at handshake time",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Bounded optional channel capabilities the server offers",
    )


class ServerSnapshotFrame(_ChannelFrame):
    """Bounded newest-page snapshot (no cursor handshake). The page shape
    mirrors the D01 history page: explicit cursor continuity fields, byte
    bounds and honest truncation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["snapshot"] = Field(
        default="snapshot",
        description="Frame discriminator: bounded newest-page snapshot",
    )
    session_id: UUID = Field(
        description="Session identity the snapshot belongs to",
    )
    entries: list[SessionEntryView] = Field(
        max_length=MAX_CHANNEL_SNAPSHOT_ENTRIES,
        description="Bounded newest committed entries (chronological)",
    )
    latest_seq: int = Field(
        ge=0,
        description="Highest committed sequence on the session (snapshot "
        "ordering authority)",
    )
    has_more: bool = Field(
        description="True when newer committed entries exist above this page",
    )
    has_older: bool = Field(
        description="True when older committed entries exist below this page",
    )
    oldest_seq: int | None = Field(
        default=None,
        ge=1,
        description="Lowest sequence carried by this snapshot, when non-empty",
    )
    next_before_seq: int | None = Field(
        default=None,
        ge=1,
        description="Exclusive keyset cursor for the next older page, when "
        "has_older",
    )
    byte_size: int = Field(
        ge=0,
        le=MAX_PAGE_BYTES,
        description="Declared serialized size of the snapshot entries",
    )
    truncated_by_bytes: bool = Field(
        description="True when the byte budget cut entries before the count "
        "limit",
    )


class ServerEntriesFrame(_ChannelFrame):
    """One bounded ordered replay page (cursor resume). Every page carries
    the honest continuation cursor; a huge requested range arrives as
    bounded pages plus ``resync_required``, never one full transcript."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["entries"] = Field(
        default="entries",
        description="Frame discriminator: bounded ordered replay page",
    )
    entries: list[SessionEntryView] = Field(
        max_length=HISTORY_PAGE_MAX_ENTRIES,
        description="Bounded committed entries strictly after the client "
        "cursor, in sequence order",
    )
    latest_seq: int = Field(
        ge=0,
        description="Highest committed sequence on the session",
    )
    has_more: bool = Field(
        description="True when more replay pages follow",
    )
    has_older: bool = Field(
        description="True when older committed entries exist below this page",
    )
    next_before_seq: int | None = Field(
        default=None,
        ge=1,
        description="Exclusive keyset cursor for the next older page, when "
        "has_older",
    )


class ServerDeltaFrame(_ChannelFrame):
    """VOLATILE, uncommitted stream text. Bound to ``(stream_id,
    delta_id)``; durable NOWHERE; never a committed entry; dropped first
    under backpressure."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["delta"] = Field(
        default="delta",
        description="Frame discriminator: volatile stream text delta",
    )
    stream_id: str = Field(
        min_length=1,
        max_length=128,
        description="Volatile stream identity this delta belongs to",
    )
    delta_id: str = Field(
        min_length=1,
        max_length=128,
        description="Ordered delta identity within its stream",
    )
    text: str = Field(
        max_length=MAX_STREAM_TEXT_CHARS,
        description="Bounded uncommitted text chunk (volatile, never "
        "durable, never authority)",
    )
    run_id: UUID = Field(
        description="Run whose volatile stream produced this delta",
    )


class ServerCommittedFrame(_ChannelFrame):
    """ONE durable entry, delivered exactly once per ``seq`` per
    connection (the snapshot/live race is resolved by the connection's
    delivered-through cursor)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["committed"] = Field(
        default="committed",
        description="Frame discriminator: one durable committed entry",
    )
    stream_id: str = Field(
        min_length=1,
        max_length=128,
        description="Connection stream identity carrying this delivery",
    )
    entry: SessionEntryView = Field(
        description="The durable committed entry (bound to entry_id + seq)",
    )


class ServerRunStateFrame(_ChannelFrame):
    """One frozen run-state transition mirrored from the durable
    ``run_state`` entries (never model prose)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["run_state"] = Field(
        default="run_state",
        description="Frame discriminator: frozen run-state transition",
    )
    run_id: UUID = Field(
        description="Run whose lifecycle state changed",
    )
    state: SessionRunState = Field(
        description="New frozen lifecycle state of the run",
    )
    reason_code: str | None = Field(
        default=None,
        max_length=MAX_CHANNEL_ERROR_CODE_CHARS,
        description="Stable machine-readable transition reason, when present",
    )


class ServerResyncRequiredFrame(_ChannelFrame):
    """The connection lost continuity (cursor forgery suspected, replay
    budget exhausted, outbox overflow): the client MUST re-snapshot from
    the durable store."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["resync_required"] = Field(
        default="resync_required",
        description="Frame discriminator: client must re-snapshot",
    )
    reason_code: str = Field(
        min_length=1,
        max_length=MAX_CHANNEL_ERROR_CODE_CHARS,
        description="Stable machine-readable resync reason",
    )
    latest_seq: int = Field(
        ge=0,
        description="Highest committed sequence known to the server",
    )


class ServerChannelErrorFrame(_ChannelFrame):
    """Bounded, VALUE-FREE channel error (never content, never secrets)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["error"] = Field(
        default="error",
        description="Frame discriminator: bounded value-free channel error",
    )
    code: str = Field(
        min_length=1,
        max_length=MAX_CHANNEL_ERROR_CODE_CHARS,
        description="Stable machine-readable error code",
    )
    reason: str = Field(
        max_length=MAX_CHANNEL_REASON_CHARS,
        description="Value-free bounded reason (never echoes content)",
    )


class ServerPongFrame(_ChannelFrame):
    """Heartbeat answer."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["pong"] = Field(
        default="pong",
        description="Frame discriminator: heartbeat answer",
    )


ServerChannelFrame = Annotated[
    ServerHelloOkFrame
    | ServerSnapshotFrame
    | ServerEntriesFrame
    | ServerDeltaFrame
    | ServerCommittedFrame
    | ServerRunStateFrame
    | ServerResyncRequiredFrame
    | ServerChannelErrorFrame
    | ServerPongFrame,
    Field(discriminator="type"),
]


# --- validation helpers ------------------------------------------------------------


def validate_client_frame(raw: str | bytes) -> ClientChannelFrame:
    """Parse and validate one raw client frame (bounded, fail-closed).

    Raises :class:`SessionContractError` with value-free codes for: an
    oversized frame, invalid JSON, an unknown/missing discriminator, a
    schema violation, or a wrong protocol version. The exception never
    carries frame content.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if len(raw) > MAX_FRAME_BYTES:
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_too_large",
                    "frame",
                    f"a channel frame must carry at most {MAX_FRAME_BYTES} "
                    "bytes",
                )
            ]
        )
    try:
        document = json.loads(raw) if raw.strip() else None
    except ValueError:
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_malformed",
                    "frame",
                    "the channel frame is not valid JSON",
                )
            ]
        ) from None
    if not isinstance(document, dict):
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_malformed",
                    "frame",
                    "the channel frame must be a JSON object",
                )
            ]
        )
    frame_type = document.get("type")
    if frame_type == "hello":
        model = ClientHelloFrame
    elif frame_type == "subscribe":
        model = ClientSubscribeFrame
    elif frame_type == "ack":
        model = ClientAckFrame
    elif frame_type == "ping":
        model = ClientPingFrame
    else:
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_unknown_type",
                    "type",
                    "the channel frame type is unknown or missing",
                )
            ]
        )
    try:
        frame = model.model_validate(document)
    except ValidationError as exc:
        # Value-free: the codes name the failing field path, never the
        # frame content.
        fields = [
            SessionFieldError(
                "frame_invalid_field",
                str(errored["loc"][0] if errored.get("loc") else "frame"),
                "the channel frame field is missing or out of bounds",
            )
            for errored in exc.errors()[:8]
        ]
        raise SessionContractError(fields) from None
    if isinstance(frame, ClientHelloFrame) and (
        frame.protocol_version != CHANNEL_PROTOCOL_VERSION
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    "version_unsupported",
                    "protocol_version",
                    f"this server speaks protocol version "
                    f"{CHANNEL_PROTOCOL_VERSION} only",
                )
            ]
        )
    return frame


def validate_server_frame(frame: ServerChannelFrame) -> ServerChannelFrame:
    """Validate one server frame against the closed union (schema tests use
    this to prove malformed server examples fail validation too)."""
    if not isinstance(frame, _ChannelFrame):
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_malformed",
                    "frame",
                    "the server frame is not a channel frame",
                )
            ]
        )
    if frame.type not in SERVER_FRAME_TYPES:  # type: ignore[union-attr]
        raise SessionContractError(
            [
                SessionFieldError(
                    "frame_unknown_type",
                    "type",
                    "the server frame type is unknown",
                )
            ]
        )
    return frame


class StreamIdentity:
    """Stream identity rules, enforced at one place (fail-closed).

    ``stream_id`` names one run's volatile stream; deltas bind to
    ``(stream_id, delta_id)``; committed entries bind to ``entry_id`` +
    ``seq``. There is deliberately NO client frame that could promote a
    delta: the client vocabulary carries no entry payloads at all.
    """

    @staticmethod
    def stream_id_for(run_id: UUID, connection_id: UUID) -> str:
        """The volatile stream id of one run on one connection (bounded)."""
        return f"stream:{run_id}:{connection_id}"[:128]

    @staticmethod
    def delta_key(stream_id: str, delta_id: str) -> str:
        """The ``(stream_id, delta_id)`` binding key of one delta."""
        return f"{stream_id}/{delta_id}"[:260]

    @staticmethod
    def committed_key(entry: SessionEntryView) -> str:
        """The ``entry_id`` + ``seq`` binding key of one committed entry."""
        return f"{entry.entry_id}:{entry.seq}"
