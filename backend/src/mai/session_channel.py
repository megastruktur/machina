"""Shared LISTEN/NOTIFY session-channel hub (D06).

ONE background listener per app process (never per connection) wakes the
fanout when any session commits; per-session fanout maps to per-connection
bounded outboxes. Discipline (sfc_PLAN D06; AGENTS.md caps):

- **Wakeup payload carries identity ONLY** — ``{"session_id": ...,
  "latest_seq": ...}`` — never content, never secrets.
- **NOTIFY loss falls back WITHOUT fixed fast polling**: on wakeup AND on
  reconnect every subscribed connection does a bounded cursor catch-up,
  and a coarse safety tick (default 5 s, bounded and documented) bounds
  the worst-case delivery delay to one tick. Idle behaviour = one tick
  wakeup, ZERO tail queries (the tick queries a session ONLY when that
  session has subscribed connections AND their cursors are behind).
- **Bounded fanout/outbox**: per-session sink sets and per-connection
  outboxes have explicit maxima. Overflow drops VOLATILE deltas first;
  if committed frames cannot fit, the connection is marked desynced and
  receives ``resync_required`` (committed entries are NEVER silently
  dropped — the client re-reads them from the durable store via
  snapshot/replay).
- **Dead clients are not retained**: a sink leaves the fanout map on
  disconnect or failed send (the map shrinks; no dead-client history in
  memory).

Publishers commit durably FIRST (the session store is the only authority),
then publish the wakeup; a lost wakeup costs one coarse tick of delay,
never correctness.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from mai.contracts.session_channel import (
    CHANNEL_NOTIFY_CHANNEL,
    MAX_CHANNEL_SNAPSHOT_ENTRIES,
    ServerChannelFrame,
)

__all__ = [
    "CHANNEL_OUTBOX_LIMIT",
    "CHANNEL_SESSION_SINK_LIMIT",
    "COARSE_TICK_SECONDS",
    "NOTIFY_RECONNECT_MAX_SECONDS",
    "SessionChannelHub",
    "SessionSink",
    "notify_session_committed",
]

#: Per-connection outbox bound (frames). Committed entries never silently
#: drop; overflow degrades to delta-dropping then resync_required.
CHANNEL_OUTBOX_LIMIT: Final[int] = 256

#: Per-session sink (connection) bound — one session's fanout map cap.
CHANNEL_SESSION_SINK_LIMIT: Final[int] = 64

#: The bounded coarse safety tick (seconds). NOT a hot tail loop: a tick
#: with no behind-cursor subscribers issues ZERO queries.
COARSE_TICK_SECONDS: Final[float] = 5.0

#: NOTIFY reconnect backoff cap (seconds; 0.5 → 1 → 2 → 4 … capped here).
NOTIFY_RECONNECT_MAX_SECONDS: Final[float] = 30.0

#: The wakeup payload vocabulary (identity only). ``updated_seq`` marks an
#: IN-PLACE entry update (same ``seq``, new payload): at-head connections
#: re-read that single entry and refresh what they already delivered.
_NOTIFY_KEYS: Final[frozenset[str]] = frozenset(
    {"session_id", "latest_seq", "updated_seq"}
)


@dataclass
class SessionSink:
    """One subscribed connection's bounded delivery state."""

    connection_id: uuid.UUID
    session_id: uuid.UUID
    server_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))
    outbox: asyncio.Queue[ServerChannelFrame] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CHANNEL_OUTBOX_LIMIT)
    )
    #: The connection's delivered-through committed cursor (exactly-once
    #: per connection); ``None`` = the client must re-snapshot.
    delivered_through: int | None = None
    #: Set when continuity is lost: the flusher sends ``resync_required``
    #: and the client re-snapshots (committed frames are recoverable from
    #: the durable store — never silently dropped).
    resync: str | None = None
    #: Volatile deltas dropped under backpressure (bounded counter).
    dropped_deltas: int = 0
    #: True while this sink's cursor sits at the session head (the last
    #: catch-up page was not full): the coarse tick then issues ZERO SQL
    #: for this session until a NOTIFY (or a reconnect, or a partial page)
    #: says otherwise — the idle fallback is NOT a tail loop.
    at_head: bool = False
    #: True while the route's own bounded replay is filling this sink's
    #: outbox: the hub catch-up skips it (no interleaved duplicate frames);
    #: the route clears the flag when the replay ends (at_head False → the
    #: hub resumes catch-up from the advanced cursor).
    replaying: bool = False
    closed: bool = False

    def offer(self, frame: ServerChannelFrame) -> None:
        """Append one frame under the bounded backpressure policy."""
        if self.closed:
            return
        kind = frame.type  # type: ignore[union-attr]
        if kind == "delta":
            if not self.outbox.full():
                self.outbox.put_nowait(frame)
            else:
                self.dropped_deltas += 1
            return
        if not self.outbox.full():
            self.outbox.put_nowait(frame)
            return
        # Overflow with a durable frame: drop queued VOLATILE deltas first.
        kept: list[ServerChannelFrame] = []
        while not self.outbox.empty():
            queued = self.outbox.get_nowait()
            if queued.type != "delta":  # type: ignore[union-attr]
                kept.append(queued)
        for queued in kept:
            self.outbox.put_nowait(queued)
        if self.outbox.full():
            # Committed frames cannot fit: continuity is lost — resync.
            self.resync = "outbox_overflow"
            with contextlib.suppress(asyncio.QueueFull):
                self.outbox.put_nowait(
                    _resync_frame("outbox_overflow", self.delivered_through or 0)
                )
            return
        self.outbox.put_nowait(frame)


def _resync_frame(reason: str, latest_seq: int) -> ServerChannelFrame:
    from mai.contracts.session_channel import ServerResyncRequiredFrame

    return ServerResyncRequiredFrame(reason_code=reason, latest_seq=latest_seq)


class SessionChannelHub:
    """The ONE listener + fanout owner per process.

    ``start(engine)`` opens the dedicated NOTIFY connection (autocommit,
    same engine pool), subscribes ``LISTEN mai_session_events`` and runs
    the delivery loop; ``stop()`` closes both. ``publish_delta`` and the
    API layer feed volatile/live frames; committed catch-up reads the
    durable store through the injected bounded reader.
    """

    def __init__(
        self,
        *,
        entry_reader: Callable[..., Any],
        coarse_tick_seconds: float = COARSE_TICK_SECONDS,
        replay_page_limit: int = 20,
    ) -> None:
        self._engine: AsyncEngine | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._entry_reader = entry_reader
        self._coarse_tick_seconds = coarse_tick_seconds
        self._replay_page_limit = replay_page_limit
        self._sessions: dict[uuid.UUID, dict[uuid.UUID, SessionSink]] = {}
        self._wake = asyncio.Event()
        self._pending_notify_sessions: set[uuid.UUID] = set()
        #: In-place entry updates awaiting delivery (S1): per session, the
        #: ``updated_seq`` values from notify payloads whose entries may
        #: already sit at or behind a sink's cursor. Consumed (and re-read)
        #: by the catch-up; dropped when the session's last sink leaves.
        self._pending_updated_seqs: dict[uuid.UUID, set[int]] = {}
        self._listen_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped = False
        # Bounded operational counters (proof/evidence surface).
        self.notify_count = 0
        self.tick_count = 0
        self.catchup_query_count = 0
        self.catchup_error_count = 0
        self.reconnect_count = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._stopped = False
        self._raw_connection = None
        self._loop = asyncio.get_running_loop()
        self._listen_task = self._loop.create_task(
            self._listen_loop(), name="session-channel-listen"
        )
        self._loop_task = self._loop.create_task(
            self._delivery_loop(), name="session-channel-delivery"
        )

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for task in (self._listen_task, self._loop_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # The dedicated LISTEN connection closes OUTSIDE the cancelled
        # listener task (an await in a cancelled task cannot run).
        raw = getattr(self, "_raw_connection", None)
        if raw is not None:
            with contextlib.suppress(Exception):
                await raw.close()
        self._raw_connection = None
        self._sessions.clear()
        self._pending_updated_seqs.clear()

    # -- fanout ---------------------------------------------------------------

    def subscribe(self, sink: SessionSink) -> None:
        """Register one connection (the subscription is established BEFORE
        the snapshot read — the snapshot/live race ordering rule)."""
        session_sinks = self._sessions.setdefault(sink.session_id, {})
        if len(session_sinks) >= CHANNEL_SESSION_SINK_LIMIT and (
            sink.connection_id not in session_sinks
        ):
            raise RuntimeError("session_fanout_full")
        session_sinks[sink.connection_id] = sink
        self._wake.set()

    def unsubscribe(self, sink: SessionSink) -> None:
        """Remove one connection (dead clients are never retained)."""
        session_sinks = self._sessions.get(sink.session_id)
        if session_sinks is not None:
            session_sinks.pop(sink.connection_id, None)
            if not session_sinks:
                self._sessions.pop(sink.session_id, None)
                # No subscribers left: the pending in-place updates die
                # with the fanout (a future subscriber re-snapshots the
                # durable store, which already holds the updated rows).
                self._pending_updated_seqs.pop(sink.session_id, None)
        sink.closed = True

    def sink_count(self, session_id: uuid.UUID | None = None) -> int:
        if session_id is None:
            return sum(len(sinks) for sinks in self._sessions.values())
        return len(self._sessions.get(session_id, {}))

    def publish_delta(
        self, session_id: uuid.UUID, frame: ServerChannelFrame
    ) -> int:
        """Fan one volatile delta to the session's sinks (dropped first
        under backpressure — never durable)."""
        delivered = 0
        for sink in list(self._sessions.get(session_id, {}).values()):
            before = sink.dropped_deltas
            sink.offer(frame)
            if sink.dropped_deltas == before:
                delivered += 1
        return delivered

    # -- the listener (NOTIFY) -------------------------------------------------

    async def _listen_loop(self) -> None:
        """The dedicated autocommit LISTEN connection with bounded
        reconnect backoff.

        The psycopg async ``notifies()`` generator waits on the SOCKET in
        pure asyncio (``timeout`` = the coarse tick): an idle 60 s window
        issues ZERO SQL statements on this connection — PostgreSQL pushes
        the notification, the event loop wakes, no tail query exists. On
        (re)connect the bounded cursor catch-up recovers NOTIFYs missed
        while disconnected (NOTIFY loss never loses entries).
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
                await driver.execute(f'LISTEN "{CHANNEL_NOTIFY_CHANNEL}"')
                self.reconnect_count += 1
                backoff = 0.5
                # Reconnect: NOTIFYs missed while disconnected are
                # recovered by a forced cursor catch-up of every
                # subscribed session (bounded, one query each).
                self._pending_notify_sessions.update(self._sessions.keys())
                self._wake.set()
                # The inner loop stays on THIS connection: the notifies()
                # generator exits on its timeout (the coarse tick boundary,
                # ZERO SQL) and the while re-enters it — the connection is
                # only closed on a real failure (reconnect below).
                while not self._stopped:
                    async for _notify in driver.notifies(
                        timeout=self._coarse_tick_seconds
                    ):
                        self._on_notify_payload(
                            _notify.channel, _notify.payload
                        )
                    self._wake.set()
            except asyncio.CancelledError:
                # stop() owns the raw-connection close (an await inside a
                # cancelled task cannot run) — the connection stays on the
                # hub; re-raise.
                raise
            except Exception:
                self.reconnect_count += 1
                if raw is not None:
                    with contextlib.suppress(Exception):
                        await raw.close()
                    self._raw_connection = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, NOTIFY_RECONNECT_MAX_SECONDS)

    def restart_listener(self) -> None:
        """Kill + restart ONLY the LISTEN task (the NOTIFY-loss recovery
        seam; R06 proof 3). The fanout/delivery state is untouched: every
        subscribed session is force-polled by the reconnect catch-up, so
        the notifications lost while the listener was dead are recovered
        BY CURSOR, never by faster polling. Safe to call from any thread
        (the spawn hops to the hub's own loop)."""
        self.reconnect_count += 1

        def _restart() -> None:
            if self._listen_task is not None:
                self._listen_task.cancel()
            # Close the PREVIOUS LISTEN connection before the new listener
            # overwrites ``_raw_connection`` (S1): the cancelled task's
            # error path leaves the connection open on purpose (stop()
            # owns its close), so a restart here would otherwise leak one
            # pooled connection per restart. The close is async — it hops
            # onto the hub's loop as its own task, before the fresh
            # listener task is spawned.
            stale = getattr(self, "_raw_connection", None)
            self._raw_connection = None
            if stale is not None:

                async def _close_stale() -> None:
                    with contextlib.suppress(Exception):
                        await stale.close()

                self._loop.create_task(
                    _close_stale(), name="session-channel-stale-listen-close"
                )
            self._listen_task = self._loop.create_task(
                self._listen_loop(), name="session-channel-listen"
            )

        self._pending_notify_sessions.update(self._sessions.keys())
        self._loop.call_soon_threadsafe(_restart)
        self._wake.set()

    def _on_notify_payload(self, _channel: str, payload: str) -> None:
        try:
            document = json.loads(payload)
        except ValueError:
            return
        if not isinstance(document, dict) or not set(document) <= _NOTIFY_KEYS:
            return
        try:
            session_id = uuid.UUID(str(document.get("session_id")))
        except ValueError:
            return
        self.notify_count += 1
        self._pending_notify_sessions.add(session_id)
        updated_raw = document.get("updated_seq")
        if updated_raw is not None:
            # An IN-PLACE entry update (same seq, new payload): remember
            # the seq so the catch-up re-reads it for sinks whose cursor
            # already passed it (at-head connections refresh in place).
            try:
                updated_seq = int(updated_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            if updated_seq < 1:
                return
            seqs = self._pending_updated_seqs.setdefault(session_id, set())
            seqs.add(updated_seq)
        self._wake.set()

    # -- the delivery loop (bounded catch-up + coarse tick) -------------------

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
            # NO per-tick session seeding here: a subscribed-but-idle
            # session at the head issues ZERO SQL until a NOTIFY (socket
            # wake), a reconnect, or a partial catch-up page says otherwise.
            try:
                await self._catch_up_all()
            except Exception as _error:
                # One poisoned pass never kills the delivery loop (S1: a
                # fanout fault surfaced exactly this way): the failure
                # stays observable (bounded counter) and the loop retries
                # on the next wakeup/tick — never silent.
                self.catchup_error_count += 1
                import sys as _sys

                print(
                    f"session channel: catch-up pass failed: "
                    f"{type(_error).__name__}: {_error}",
                    file=_sys.stderr,
                    flush=True,
                )

    async def _catch_up_all(self) -> None:
        # Only sessions with WORK to do are queried: a NOTIFY named the
        # session, OR a sink is behind a partial page (at_head False).
        # A subscribed-but-idle session at the head issues ZERO SQL —
        # the idle fallback is not a tail loop (R06 proof 5).
        candidates = {
            session_id
            for session_id in self._pending_notify_sessions
            if self._sessions.get(session_id)
        }
        candidates.update(
            session_id
            for session_id, sinks in self._sessions.items()
            if any(
                not sink.at_head and not sink.replaying
                for sink in sinks.values()
            )
        )
        for session_id in sorted(candidates):
            await self._catch_up_session(session_id)
        self._pending_notify_sessions.clear()

    async def _catch_up_session(self, session_id: uuid.UUID) -> None:
        """One bounded catch-up read per session; frames fan out to each
        sink whose cursor is behind (per-connection exactly-once by seq).

        ``floor == 0`` (a session that was EMPTY at subscribe) reads the
        newest page WITHOUT the exclusive cursor — the D02 ``after_seq``
        cursor floor is 1, and cursor 0 means "from the beginning", which
        for an empty-at-subscribe session IS the newest page.
        """
        sinks = list(self._sessions.get(session_id, {}).values())
        if not sinks:
            return
        server_id = sinks[0].server_id
        floor = min(
            (sink.delivered_through or 0) for sink in sinks
        )
        try:
            page = await self._entry_reader(
                session_id=session_id,
                server_id=server_id,
                after_seq=(floor if floor >= 1 else None),
                limit=50,
            )
            self.catchup_query_count += 1
        except Exception as _error:
            # A transient store failure stays observable (bounded counter)
            # and retries on the next wakeup/tick — never silent.
            self.catchup_error_count += 1
            import sys as _sys

            print(
                f"session channel: catch-up read failed: "
                f"{type(_error).__name__}: {_error}",
                file=_sys.stderr,
                flush=True,
            )
            return
        if page is None or not getattr(page, "items", None):
            for sink in sinks:
                if not sink.replaying:
                    sink.at_head = True  # nothing behind: the head
            await self._deliver_in_place_updates(session_id, server_id, sinks)
            return
        entries = list(page.items)
        at_head = not bool(page.has_more)
        for sink in sinks:
            if sink.replaying:
                continue  # the route's own replay owns this outbox
            cursor = sink.delivered_through or 0
            stream_id = f"stream:{sink.connection_id}"
            for entry in entries:
                if entry.seq <= cursor:
                    continue
                sink.offer(_committed_frame(session_id, stream_id, entry))
                if entry.payload.kind.value == "run_state":
                    run_state = _run_state_frame(entry)
                    if run_state is not None:
                        sink.offer(run_state)
                cursor = entry.seq
            sink.delivered_through = max(cursor, sink.delivered_through or 0)
            sink.at_head = at_head
        await self._deliver_in_place_updates(session_id, server_id, sinks)

    async def _deliver_in_place_updates(
        self,
        session_id: uuid.UUID,
        server_id: uuid.UUID,
        sinks: list[SessionSink],
    ) -> None:
        """Re-read entries updated IN PLACE and re-offer them as committed
        frames (S1). An at-head sink's cursor already passed the updated
        seq, so the page catch-up (``seq > cursor``) never sees it again;
        the single-entry re-read refreshes the client's ``committedBySeq``
        in place (tool-group flips, tool-result reconciliation, delegation
        completion). Sinks behind the seq read the updated row during
        their normal page catch-up, so they are not re-offered here.
        """
        pending = self._pending_updated_seqs.get(session_id)
        if not pending:
            return
        captured = sorted(pending)
        # Consume exactly the captured seqs; a notify landing during the
        # reads below stays pending for the next wakeup.
        pending.difference_update(captured)
        for seq in captured:
            targets = [
                sink
                for sink in sinks
                if not sink.replaying and (sink.delivered_through or 0) >= seq
            ]
            if not targets:
                # Nobody has delivered the pre-update row: a behind sink
                # catches it on its page read, a replaying sink through
                # its route replay, a future subscriber via snapshot.
                continue
            try:
                page = await self._entry_reader(
                    session_id=session_id,
                    server_id=server_id,
                    after_seq=(seq - 1 if seq > 1 else None),
                    limit=1,
                )
                self.catchup_query_count += 1
            except Exception as _error:
                # A transient store failure stays observable (bounded
                # counter) and RETRIES on the next wakeup: the seq stays
                # pending — never silent.
                self.catchup_error_count += 1
                import sys as _sys

                print(
                    f"session channel: in-place update re-read failed: "
                    f"{type(_error).__name__}: {_error}",
                    file=_sys.stderr,
                    flush=True,
                )
                self._pending_updated_seqs.setdefault(session_id, set()).add(
                    seq
                )
                continue
            entry = None
            if page is not None and getattr(page, "items", None):
                for candidate in page.items:
                    if candidate.seq == seq:
                        entry = candidate
                        break
            if entry is None:
                continue  # stale notify: nothing left to refresh
            for sink in targets:
                stream_id = f"stream:{sink.connection_id}"
                sink.offer(_committed_frame(session_id, stream_id, entry))


def _committed_frame(
    session_id: uuid.UUID, stream_id: str, entry: Any
) -> ServerChannelFrame:
    from mai.contracts.session_channel import ServerCommittedFrame

    return ServerCommittedFrame(stream_id=stream_id, entry=entry)


def _run_state_frame(entry: Any) -> ServerChannelFrame | None:
    from mai.contracts.session_channel import ServerRunStateFrame

    # The transition's run identity lives on the ENTRY ENVELOPE, not the
    # payload (``RunStateEntry`` carries state/reason only). A run-scoped
    # entry always names its run; without one there is no frame to send
    # (the committed frame still carries the entry itself).
    if entry.run_id is None:
        return None
    payload = entry.payload
    return ServerRunStateFrame(
        run_id=uuid.UUID(str(entry.run_id)),
        state=payload.state,
        reason_code=payload.reason_code,
    )


# -- publisher ------------------------------------------------------------------


async def notify_session_committed(
    engine: AsyncEngine,
    session_id: uuid.UUID,
    latest_seq: int,
) -> None:
    """Publish the wakeup AFTER the durable commit (identity-only payload).

    A lost or failed wakeup costs one coarse tick of delay — correctness
    lives in the durable store, never in the notify.
    """
    payload = json.dumps(
        {"session_id": str(session_id), "latest_seq": int(latest_seq)}
    )
    # The wakeup goes through a RAW autocommit driver connection (the
    # listener's own pattern): setting isolation on a live async
    # connection is ignored, and a transactional pg_notify would die in
    # the rollback at close — the wakeup would NEVER fire.
    raw = await engine.raw_connection()
    try:
        driver = raw.driver_connection
        await driver.set_autocommit(True)
        await driver.execute(
            "SELECT pg_notify(%s, %s)", (CHANNEL_NOTIFY_CHANNEL, payload)
        )
    finally:
        with contextlib.suppress(Exception):
            await raw.close()


# -- re-export for typing convenience -------------------------------------------

assert MAX_CHANNEL_SNAPSHOT_ENTRIES >= 1
