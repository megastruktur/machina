"""Live notification wakeup channel over PostgreSQL LISTEN/NOTIFY (nbi-live).

SessionChannelHub sibling (:mod:`mai.session_channel` discipline, D06):
ONE background LISTEN connection per app process fans identity-only
wakeups to per-connection bounded outboxes; the SSE route
(``GET /api/v1/notifications/events``) consumes one sink each.

Discipline (nbi_PLAN shared contracts; docs/architecture/notification-inbox.md §8):

- **Wakeup payloads carry identity ONLY** — ``{"kind": "created" |
  "updated", "alert_id": ..., "server_id": ...}`` — never title, body,
  summary, source or any secret material. The one degradation frame is
  ``{"kind": "resync"}`` (no identity at all): it means «refetch the
  durable store now» and is emitted when continuity is lost (outbox
  overflow, listener reconnect) or when a frame burst cannot be carried.
- **Durable store is the single truth; NOTIFY is an optimization.**
  Publishers commit the durable row FIRST and only then publish the
  wakeup (:func:`notify_notification_wakeup` runs the ``pg_notify``
  outside any transaction, on a dedicated autocommit connection). A
  lost or failed wakeup costs at most one coarse tick (default 5 s) of
  delay and never correctness: clients refetch the durable list/count on
  EVERY wakeup, on every reconnect, and at the bounded stream lifetime's
  end — no cursor, no replay table (the durable-event-stream design was
  rejected in research S3).
- **NOTIFY loss recovery without tail polling**: the LISTEN connection
  pushes notifications; between them the hub issues ZERO SQL. When the
  listener dies, notifications fired during the gap are unrecoverable
  from NOTIFY itself, so the reconnect path broadcasts ONE ``resync``
  wakeup per connected sink — the client refetches and lands exactly on
  the durable state (bounded reconnect backoff: 0.5 s doubling to a
  30 s cap).
- **Bounded fanout/memory**: per-sink outboxes have an explicit frame
  limit; overflow drains the identity frames and coalesces into ONE
  ``resync`` frame (the client refetches, nothing is silently faked as
  delivered). The in-flight hub buffer is bounded too: overflow
  broadcasts one ``resync`` instead of queueing unboundedly. Dead
  clients (disconnected sockets, closed sinks) are evicted — no
  dead-client history is retained in memory.
- **Coarse tick, not a hot loop**: the delivery loop wakes on NOTIFY or
  at most once per tick to flush buffered wakeups. An idle hub issues
  zero queries per tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util.concurrency import greenlet_spawn

__all__ = [
    "NOTIFICATION_COARSE_TICK_SECONDS",
    "NOTIFICATION_OUTBOX_LIMIT",
    "NOTIFICATION_PENDING_LIMIT",
    "NOTIFICATION_RECONNECT_MAX_SECONDS",
    "NOTIFICATION_SINK_LIMIT",
    "NOTIFY_CHANNEL",
    "WAKEUP_KINDS",
    "NotificationChannelHub",
    "NotificationSink",
    "WakeupFrame",
    "attach_notification_channel",
    "notify_notification_wakeup",
    "resync_frame",
]

#: The ONE PostgreSQL NOTIFY channel of the notification inbox.
NOTIFY_CHANNEL: Final[str] = "mai_notification_alerts"

#: Per-connection outbox bound (frames). Identity wakeups are volatile
#: (the durable store is the truth), so overflow degrades to ONE resync.
NOTIFICATION_OUTBOX_LIMIT: Final[int] = 256

#: Total subscribed-connection bound for the whole process (each sink is
#: one open SSE stream; the outboxes are the memory commitment).
NOTIFICATION_SINK_LIMIT: Final[int] = 256

#: In-flight wakeup bound between NOTIFY flushes. Identity frames beyond
#: the bound are coalesced into one resync broadcast — never unbounded.
NOTIFICATION_PENDING_LIMIT: Final[int] = 4096

#: The bounded coarse safety tick (seconds): the worst-case flush delay
#: for a buffered wakeup whose wake event was lost. NOT a tail loop: an
#: idle tick issues ZERO queries. Configurable for fast tests through
#: ``MAI_NOTIFICATION_CHANNEL_TICK_SECONDS`` (clamped 0.05..60).
NOTIFICATION_COARSE_TICK_SECONDS: Final[float] = 5.0

#: NOTIFY reconnect backoff cap (seconds; 0.5 → 1 → 2 → 4 … capped here).
NOTIFICATION_RECONNECT_MAX_SECONDS: Final[float] = 30.0

#: The wakeup vocabulary. ``created``/``updated`` name one durable alert
#: by identity; ``resync`` carries NO identity and means «refetch now»
#: (loss/overflow degradation — hub-internal, never publisher-sent).
WAKEUP_KINDS: Final[frozenset[str]] = frozenset(
    {"created", "updated", "resync"}
)

#: The strict NOTIFY payload whitelist (identity only). Any payload
#: carrying anything else — title, body, source, extra keys — is DROPPED
#: at the listener, never fanned out.
_NOTIFY_KEYS: Final[frozenset[str]] = frozenset(
    {"kind", "alert_id", "server_id"}
)


def resync_frame() -> WakeupFrame:
    """The one identity-free «refetch the durable store now» frame."""
    return WakeupFrame(kind="resync", alert_id=None, server_id=None)


@dataclass(frozen=True, slots=True)
class WakeupFrame:
    """One identity-only wakeup (or the identity-free resync signal)."""

    kind: str
    alert_id: str | None = None
    server_id: str | None = None

    def wire(self) -> str:
        """The exact SSE ``data:`` payload (identity only, stable order)."""
        if self.kind == "resync":
            return '{"kind":"resync"}'
        return json.dumps(
            {
                "kind": self.kind,
                "alert_id": self.alert_id,
                "server_id": self.server_id,
            },
            separators=(",", ":"),
        )


@dataclass
class NotificationSink:
    """One subscribed SSE connection's bounded delivery state.

    The subscribing hub constructs the outbox with its own configured
    bound, so the process has exactly one outbox limit; ``offer`` takes
    it explicitly from the hub.
    """

    connection_id: uuid.UUID
    outbox: asyncio.Queue[WakeupFrame]
    closed: bool = False
    #: Times overflow coalesced queued identity frames into one resync
    #: (bounded operational counter; proof/evidence surface).
    overflow_resyncs: int = 0

    def offer(self, frame: WakeupFrame, outbox_limit: int) -> None:
        """Append one frame under the bounded degradation policy.

        Identity frames are volatile: when the outbox is full they are
        drained and coalesced into ONE resync frame — the client refetches
        the durable store, no identity frame is silently pretended
        delivered, and memory stays bounded by ``outbox_limit``.
        """
        if self.closed:
            return
        if not self.outbox.full():
            self.outbox.put_nowait(frame)
            return
        while not self.outbox.empty():
            self.outbox.get_nowait()
        self.overflow_resyncs += 1
        self.outbox.put_nowait(resync_frame())


class NotificationChannelHub:
    """The ONE notification LISTEN + fanout owner per process.

    ``start(engine)`` opens the dedicated autocommit LISTEN connection on
    :data:`NOTIFY_CHANNEL` and runs the delivery loop; ``stop()`` closes
    both. The fanout is fleet-wide (the captain's inbox is global): every
    subscribed SSE connection receives every identity wakeup.
    """

    def __init__(
        self,
        *,
        coarse_tick_seconds: float = NOTIFICATION_COARSE_TICK_SECONDS,
        outbox_limit: int = NOTIFICATION_OUTBOX_LIMIT,
        pending_limit: int = NOTIFICATION_PENDING_LIMIT,
        sink_limit: int = NOTIFICATION_SINK_LIMIT,
    ) -> None:
        self._engine: AsyncEngine | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._coarse_tick_seconds = coarse_tick_seconds
        self._outbox_limit = outbox_limit
        self._pending_limit = pending_limit
        self._sink_limit = sink_limit
        self._sinks: dict[uuid.UUID, NotificationSink] = {}
        self._wake = asyncio.Event()
        #: Wakeups buffered between delivery-loop flushes (bounded; the
        #: single fanout path — the listener and resync broadcasts both
        #: land here, so offer/coalescing runs on ONE task only).
        self._pending: list[WakeupFrame] = []
        self._listen_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._raw_connection: Any = None
        # Bounded operational counters (proof/evidence surface).
        self.notify_count = 0
        self.tick_count = 0
        self.resync_count = 0
        self.coalesced_wakeup_count = 0
        self.reconnect_count = 0

    # -- lifecycle -----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the hub owns a live listener/delivery pair."""
        return (
            not self._stopped
            and self._listen_task is not None
            and self._delivery_task is not None
        )

    def start(self, engine: AsyncEngine) -> None:
        """Open the LISTEN connection and start the delivery loop."""
        if self._listen_task is not None:
            raise RuntimeError("notification channel already started")
        self._engine = engine
        self._stopped = False
        self._loop = asyncio.get_running_loop()
        self._listen_task = self._loop.create_task(
            self._listen_loop(), name="notification-channel-listen"
        )
        self._delivery_task = self._loop.create_task(
            self._delivery_loop(), name="notification-channel-delivery"
        )

    async def stop(self) -> None:
        """Cancel both loops and close the dedicated LISTEN connection.

        The raw-connection close runs through ``greenlet_spawn`` ON the
        owning loop (the pool reset is async; a plain thread would raise
        ``MissingGreenlet``) — the same discipline as the session hub.
        """
        self._stopped = True
        self._wake.set()
        for task in (self._listen_task, self._delivery_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._listen_task = None
        self._delivery_task = None
        raw = self._raw_connection
        self._raw_connection = None
        if raw is not None:
            with contextlib.suppress(Exception):
                await greenlet_spawn(raw.close)
        self._sinks.clear()
        self._pending.clear()

    # -- fanout ---------------------------------------------------------------

    def subscribe(self) -> NotificationSink:
        """Register one SSE connection (bounded; ``RuntimeError`` at cap).

        The caller registers the sink BEFORE it reads the durable store,
        so a wakeup landing between subscribe and refetch can only cause
        one extra refetch — never a missed one.
        """
        if len(self._sinks) >= self._sink_limit:
            raise RuntimeError("notification_fanout_full")
        sink = NotificationSink(
            connection_id=uuid.uuid4(),
            outbox=asyncio.Queue(maxsize=self._outbox_limit),
        )
        self._sinks[sink.connection_id] = sink
        return sink

    def unsubscribe(self, sink: NotificationSink) -> None:
        """Remove one connection (dead clients are never retained)."""
        self._sinks.pop(sink.connection_id, None)
        sink.closed = True

    def sink_count(self) -> int:
        """Number of currently subscribed connections."""
        return len(self._sinks)

    def _fanout(self, frame: WakeupFrame) -> None:
        """Offer one frame to every live sink; prune closed ones."""
        for connection_id, sink in list(self._sinks.items()):
            if sink.closed:
                self._sinks.pop(connection_id, None)
                continue
            sink.offer(frame, self._outbox_limit)

    def _enqueue(self, frame: WakeupFrame) -> None:
        """Buffer one frame for the delivery loop (bounded; coalescing)."""
        if len(self._pending) >= self._pending_limit:
            # Burst beyond the bound: identity frames collapse into ONE
            # fleet-wide refetch signal — honest and bounded.
            self.coalesced_wakeup_count += len(self._pending)
            self._pending.clear()
            self._pending.append(resync_frame())
            self.resync_count += 1
        elif frame.kind == "resync":
            # Resync coalesces with a queued resync; identity frames keep
            # their order relative to it (a queued resync already implies
            # a full refetch, so trailing identities are redundant).
            if self._pending and self._pending[-1].kind == "resync":
                return
            self.resync_count += 1
        self._pending.append(frame)
        self._wake.set()

    def publish_local(self, frame: WakeupFrame) -> None:
        """Buffer one in-process frame (the resync/refetch seam)."""
        self._enqueue(frame)

    # -- the listener (NOTIFY) -------------------------------------------------

    async def _listen_loop(self) -> None:
        """The dedicated autocommit LISTEN connection, bounded backoff.

        psycopg's async ``notifies()`` waits on the SOCKET in pure asyncio
        (``timeout`` = the coarse tick): an idle window issues ZERO SQL.
        On every (re)connect every live sink receives ONE resync wakeup —
        notifications fired while nobody listened are unrecoverable from
        NOTIFY itself, so the client refetches the durable store instead
        (loss ≤ reconnect backoff + one tick, never correctness).
        """
        backoff = 0.5
        while not self._stopped:
            raw = None
            driver: Any = None
            try:
                assert self._engine is not None
                raw = await self._engine.raw_connection()
                self._raw_connection = raw
                driver = raw.driver_connection
                await driver.set_autocommit(True)
                await driver.execute(f'LISTEN "{NOTIFY_CHANNEL}"')
                self.reconnect_count += 1
                backoff = 0.5
                # Missed-while-disconnected wakeups are recovered BY
                # REFETCH, never by faster polling.
                self._enqueue(resync_frame())
                # The inner loop stays on THIS connection: notifies()
                # exits on its timeout (the tick boundary, ZERO SQL) and
                # the while re-enters it — the connection is only closed
                # on a real failure (reconnect below).
                while not self._stopped:
                    async for _notify in driver.notifies(
                        timeout=self._coarse_tick_seconds
                    ):
                        self._on_notify_payload(_notify.channel, _notify.payload)
                    self._wake.set()
            except asyncio.CancelledError:
                # stop() owns the raw-connection close (an await inside a
                # cancelled task cannot run) — re-raise.
                raise
            except Exception:
                self.reconnect_count += 1
                if raw is not None:
                    with contextlib.suppress(Exception):
                        await greenlet_spawn(raw.close)
                    self._raw_connection = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, NOTIFICATION_RECONNECT_MAX_SECONDS)

    def restart_listener(self) -> None:
        """Kill + restart ONLY the LISTEN task (the NOTIFY-loss seam).

        Mirrors :meth:`SessionChannelHub.restart_listener`: the fanout and
        buffered state are untouched, the stale LISTEN connection is
        closed on the hub's loop BEFORE the fresh listener overwrites it,
        and the reconnect path broadcasts the refetch signal. Safe to
        call from any thread (the spawn hops via ``call_soon_threadsafe``).
        """
        self.reconnect_count += 1

        def _restart() -> None:
            if self._listen_task is not None:
                self._listen_task.cancel()
            stale = self._raw_connection
            self._raw_connection = None
            if stale is not None:

                async def _close_stale() -> None:
                    with contextlib.suppress(Exception):
                        await greenlet_spawn(stale.close)

                self._loop.create_task(
                    _close_stale(), name="notification-stale-listen-close"
                )
            self._listen_task = self._loop.create_task(
                self._listen_loop(), name="notification-channel-listen"
            )

        assert self._loop is not None
        self._loop.call_soon_threadsafe(_restart)

    def _on_notify_payload(self, channel: str, payload: str) -> None:
        """Parse one NOTIFY payload; STRICT identity-only whitelist."""
        if channel != NOTIFY_CHANNEL:
            return
        try:
            document = json.loads(payload)
        except ValueError:
            return
        if not isinstance(document, dict) or set(document) != _NOTIFY_KEYS:
            return
        kind = document.get("kind")
        if kind not in ("created", "updated"):
            return
        alert_raw = document.get("alert_id")
        server_raw = document.get("server_id")
        try:
            uuid.UUID(str(alert_raw))
            if server_raw is not None:
                uuid.UUID(str(server_raw))
        except (ValueError, TypeError):
            return
        self.notify_count += 1
        self._enqueue(
            WakeupFrame(
                kind=str(kind),
                alert_id=str(alert_raw),
                server_id=None if server_raw is None else str(server_raw),
            )
        )

    # -- the delivery loop (bounded flush + coarse tick) -----------------------

    async def _delivery_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._coarse_tick_seconds
                )
                self._wake.clear()
            except TimeoutError:
                pass  # the coarse safety tick — NOT a hot tail loop
            self.tick_count += 1
            if not self._pending:
                continue  # idle: zero fanout work, zero SQL
            batch = self._pending
            self._pending = []
            for frame in batch:
                self._fanout(frame)


# -- publisher ------------------------------------------------------------------


async def notify_notification_wakeup(
    engine: AsyncEngine,
    *,
    kind: str,
    alert_id: uuid.UUID | str,
    server_id: uuid.UUID | str | None,
) -> None:
    """Publish one identity-only wakeup AFTER the durable commit.

    The ``pg_notify`` runs OUTSIDE any transaction on a dedicated
    autocommit connection (a transactional notify would die in the
    rollback at close and NEVER fire), mirroring
    :func:`mai.session_channel.notify_session_committed`. The payload
    whitelist is enforced here too: kind/``alert_id``/``server_id`` and
    nothing else. Callers MUST have committed the durable row first —
    the notify is the optimization, the row is the truth.
    """
    if kind not in ("created", "updated"):
        raise ValueError("wakeup kind must be 'created' or 'updated'")
    payload = json.dumps(
        {
            "kind": kind,
            "alert_id": str(alert_id),
            "server_id": None if server_id is None else str(server_id),
        },
        separators=(",", ":"),
    )
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            sql_text("SELECT pg_notify(:chan, :payload)"),
            {"chan": NOTIFY_CHANNEL, "payload": payload},
        )


# -- app wiring (no api/app.py edit needed) --------------------------------------


def attach_notification_channel(app: FastAPI) -> None:
    """Start the hub with the app engine; stop it at shutdown.

    Wraps the router's lifespan context so ``api/app.py`` stays
    untouched: the wrapped lifespan first runs the original one (which
    creates ``app.state.engine``), THEN starts the hub; on exit the hub
    stops BEFORE the original teardown disposes the engine — the exact
    session-channel ordering. A database-less app (no engine) never
    starts a hub; the SSE route fails closed with 503.

    The tick is configurable for fast tests through
    ``MAI_NOTIFICATION_CHANNEL_TICK_SECONDS`` (clamped 0.05..60 s).
    Idempotent: a second call is a no-op.
    """
    if getattr(app.state, "_notification_channel_attached", False):
        return
    app.state._notification_channel_attached = True
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_channel(app_: FastAPI) -> AsyncIterator[None]:
        async with inner_lifespan(app_):
            engine = getattr(app_.state, "engine", None)
            if (
                engine is not None
                and getattr(app_.state, "notification_channel", None) is None
            ):
                env = getattr(app_.state, "env", None) or os.environ
                tick_raw = env.get("MAI_NOTIFICATION_CHANNEL_TICK_SECONDS")
                try:
                    tick = (
                        float(tick_raw) if tick_raw is not None else 5.0
                    )
                except ValueError:
                    tick = 5.0
                tick = min(max(tick, 0.05), 60.0)
                hub = NotificationChannelHub(coarse_tick_seconds=tick)
                app_.state.notification_channel = hub
                hub.start(engine)
            try:
                yield
            finally:
                hub = getattr(app_.state, "notification_channel", None)
                if hub is not None:
                    await hub.stop()

    app.router.lifespan_context = _lifespan_with_channel
