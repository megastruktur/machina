"""Profile projection and reconciliation journal (C02 rules 4–5).

Card ``02-profile-projection``, consumer of the integrated producers
``mai.profiles.files`` (canonical file half) and ``mai.contracts.server``
(frozen schema). Contracts.md **C02**:

- **C02 rule 4** — the Postgres projection stores the same canonical version
  triple the file store commits: integer content ``revision`` (1-based;
  ``0`` = absent), ``schema_revision`` (``SCHEMA_VERSION``) and SHA-256
  content hash. The projection stores *only* identity/version metadata —
  never the document text — so the canonical document is never double-written
  into SQL.
- **C02 rule 5** — the write journal phases are exactly the frozen shape
  ``prepared -> file committed -> projection reconciled``. One journal row
  per intent (``profile_projection_journal``); a partial unique index allows
  at most one live (non-reconciled) intent per ``server_id``, mirroring the
  per-server single-writer invariant (C05 rule 6).

Crash-window ownership (C02 "Crash windows"):

- **W1** (crash after ``prepared``, before file commit): recovery compares
  the file hash/revision against the intent; an unchanged or absent file is
  re-executed from the stored payload, a file already at the intent's target
  is recognized as committed and only advanced. No half-written file is ever
  visible (the store's temp+rename guarantees that half).
- **W2** (crash after file commit, before projection): the file is
  authoritative. Recovery reconciles the projection from the file's actual
  hash/revision — never from the intent's claim ("no invented successful
  sync"); the projection row is stale (``read_projection`` reports
  ``stale=True``) until that reconcile lands.
- **W3** (crash during projection write): the projection-row write and the
  journal phase advance to ``reconciled`` execute in ONE Postgres
  transaction, so a crash leaves the pair either both old or both new —
  never mixed.
- **W4** (delete with projection write pending): the delete intent is the
  tombstone and is journaled BEFORE the canonical file is removed; the
  projection row is purged only after the reconcile confirms. A file that
  reappears while a tombstone is live is surfaced as a ``tombstone_conflict``
  to the admin — never silently overwritten.

Document payload discipline: an upsert intent carries the document text in
its journal row **only while the phase is ``prepared``** (W1 re-execution
must survive the loss of the originating caller). The payload is NULLed in
the same statement that advances the phase to ``file_committed`` and is
CHECK-constrained to NULL in every later phase, so at rest SQL never holds a
second copy of the canonical document. Payloads are never echoed into
errors, transcripts or CLI output (secret non-disclosure).

Error discipline: every rejection raised by this module is a
:class:`ProjectionError` with a stable :class:`ProjectionErrorCode` and a
value-free detail. Store-level :class:`~mai.profiles.files.ProfileFileError`
rejections propagate unchanged from the ``apply_*`` entry points (one
structured rejection vocabulary per producer); the recovery sweep records
them per intent and continues.

Recovery worker: :meth:`ProfileProjection.recover` is the projection worker
routine for the FILE-CRASH scenario (runtime-scenarios.md §CT). It is a
plain awaitable that the durable-engine adapter (card 05-engine-adapter)
will schedule; this card does not create a second scheduler.

POSIX/Postgres assumptions: the file half requires ``O_NOFOLLOW`` and
directory fsync (frozen non-goal: Windows); the SQL half requires
PostgreSQL (the only dialect :mod:`mai.database` accepts). SQLAlchemy and
psycopg arrive via the locked ``dbos==3.2.0``/``alembic`` dependency pins —
no dependency was added by this card.

Production entrypoint (exit contract 0 success / 1 structured conflict or
rejection / 2 usage or configuration error)::

    uv run python -m mai.profiles.projection status ROOT [SERVER_ID]
    uv run python -m mai.profiles.projection recover ROOT [SERVER_ID]

Both subcommands print one JSON line per entity/outcome. Transcripts are
value-free: intent payloads are reported as byte counts only, never text.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.server import SCHEMA_VERSION, parse_server_profile_text
from mai.database import DatabaseConfigError, create_app_engine, resolve_database_url
from mai.profiles.files import (
    FileStoreErrorCode,
    ProfileFileError,
    ProfileFileStore,
    StoredProfile,
)

__all__ = [
    "OP_DELETE",
    "OP_UPSERT",
    "PHASE_FILE_COMMITTED",
    "PHASE_PREPARED",
    "PHASE_RECONCILED",
    "ApplyResult",
    "JournalIntent",
    "ProfileProjection",
    "ProjectionErrorCode",
    "ProjectionError",
    "ProjectionState",
    "RecoveryOutcome",
    "main",
]

#: Journal operations (C02 rule 5; ``delete`` is the tombstone carrier).
OP_UPSERT = "upsert"
OP_DELETE = "delete"

#: Frozen journal phases (C02 rule 5): ``prepared -> file committed ->
#: projection reconciled``.
PHASE_PREPARED = "prepared"
PHASE_FILE_COMMITTED = "file_committed"
PHASE_RECONCILED = "reconciled"

_LIVE_PHASES = (PHASE_PREPARED, PHASE_FILE_COMMITTED)


class ProjectionErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_INTENT_ID = "invalid_intent_id"
    INVALID_REVISION = "invalid_revision"
    INVALID_HASH = "invalid_hash"
    INVALID_DOCUMENT = "invalid_document"
    INTENT_NOT_FOUND = "intent_not_found"
    INTENT_PENDING = "intent_pending"
    INTENT_MISMATCH = "intent_mismatch"
    INTENT_PHASE = "intent_phase"
    PAYLOAD_MISMATCH = "payload_mismatch"
    FILE_DIVERGED = "file_diverged"
    TOMBSTONE_CONFLICT = "tombstone_conflict"


class ProjectionError(ValueError):
    """One structured, value-free projection/journal rejection."""

    def __init__(self, code: ProjectionErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class JournalIntent:
    """One durable write intent (C02 rule 5 journal row)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    op: str
    phase: str
    expected_revision: int
    base_sha256: str | None
    target_revision: int | None
    target_sha256: str | None
    schema_revision: int | None
    payload: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """Projected canonical version triple (C02 rule 4) plus staleness.

    ``stale`` is True while a live (non-reconciled) journal intent exists
    for the server: consumers must treat the triple as not yet reconciled
    (C02 W2: read only after reconcile, or show stale).
    """

    server_id: uuid.UUID
    revision: int
    schema_revision: int
    sha256: str
    first_reconciled_at: datetime
    reconciled_at: datetime
    stale: bool


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of one ``apply_*`` call (idempotent intent application)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    op: str
    #: ``applied`` (fresh full pipeline), ``resumed`` (completed a pre-existing
    #: live intent) or ``already_reconciled`` (no effect; retry of a done
    #: intent).
    outcome: str
    revision: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Per-intent result of one recovery sweep (value-free)."""

    intent_id: uuid.UUID
    server_id: uuid.UUID
    op: str
    #: ``advanced_reconciled`` (file already at target), ``reexecuted_reconciled``
    #: (file effect re-executed from the journal), ``conflict`` or ``error``.
    status: str
    code: str | None
    detail: str
    revision: int | None


# ---------------------------------------------------------------------------
# Input validation (value-free; no filesystem or SQL effect before success)
# ---------------------------------------------------------------------------


def _validated_server_id(server_id: object) -> uuid.UUID:
    if not isinstance(server_id, uuid.UUID):
        raise ProjectionError(
            ProjectionErrorCode.INVALID_SERVER_ID,
            "server_id must be a uuid.UUID instance",
        )
    return server_id


def _validated_intent_id(intent_id: object) -> uuid.UUID:
    if not isinstance(intent_id, uuid.UUID):
        raise ProjectionError(
            ProjectionErrorCode.INVALID_INTENT_ID,
            "intent_id must be a uuid.UUID instance",
        )
    return intent_id


def _validated_expected_revision(expected_revision: object) -> int:
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ProjectionError(
            ProjectionErrorCode.INVALID_REVISION,
            "expected_revision must be a non-negative int",
        )
    return expected_revision


def _validated_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ProjectionError(
            ProjectionErrorCode.INVALID_HASH,
            "sha256 must be a lowercase 64-hex digest",
        )
    return value


def _validated_document(document: object) -> str:
    """Reject non-text and schema-invalid documents before any effect.

    Validation uses the frozen schema producer — the same call the file
    store performs at write time — so a journal intent can never be
    prepared for a document the canonical store would refuse.
    """
    if not isinstance(document, str):
        raise ProjectionError(
            ProjectionErrorCode.INVALID_DOCUMENT,
            "document must be text",
        )
    parse_server_profile_text(document)
    return document


def _document_sha256(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SQL statements (schema owned by migrations/versions/0001_profiles.py)
# ---------------------------------------------------------------------------

_INSERT_INTENT = text(
    """
    INSERT INTO profile_projection_journal (
        intent_id, server_id, op, phase, expected_revision, base_sha256,
        target_revision, target_sha256, schema_revision, payload
    ) VALUES (
        :intent_id, :server_id, :op, :phase, :expected_revision, :base_sha256,
        :target_revision, :target_sha256, :schema_revision, :payload
    )
    """
)

_SELECT_INTENT = text(
    """
    SELECT intent_id, server_id, op, phase, expected_revision, base_sha256,
           target_revision, target_sha256, schema_revision, payload,
           created_at, updated_at
      FROM profile_projection_journal
     WHERE intent_id = :intent_id
    """
)

_SELECT_LIVE_INTENTS_ALL = text(
    """
    SELECT intent_id, server_id, op, phase, expected_revision, base_sha256,
           target_revision, target_sha256, schema_revision, payload,
           created_at, updated_at
      FROM profile_projection_journal
     WHERE phase <> :reconciled
     ORDER BY created_at, intent_id
    """
)

_SELECT_LIVE_INTENTS_SERVER = text(
    """
    SELECT intent_id, server_id, op, phase, expected_revision, base_sha256,
           target_revision, target_sha256, schema_revision, payload,
           created_at, updated_at
      FROM profile_projection_journal
     WHERE phase <> :reconciled AND server_id = :server_id
     ORDER BY created_at, intent_id
    """
)

_SELECT_PROJECTION = text(
    """
    SELECT p.server_id, p.revision, p.schema_revision, p.sha256,
           p.first_reconciled_at, p.reconciled_at,
           (j.intent_id IS NOT NULL) AS stale
      FROM profile_projection p
      LEFT JOIN profile_projection_journal j
             ON j.server_id = p.server_id AND j.phase <> :reconciled
     WHERE p.server_id = :server_id
    """
)

#: W1 handoff: the payload dies here — every later phase is CHECK-forced to
#: hold no document text.
_ADVANCE_FILE_COMMITTED = text(
    """
    UPDATE profile_projection_journal
       SET phase = :file_committed, payload = NULL, updated_at = now()
     WHERE intent_id = :intent_id AND phase = :prepared
    """
)

_ADVANCE_RECONCILED = text(
    """
    UPDATE profile_projection_journal
       SET phase = :reconciled, updated_at = now()
     WHERE intent_id = :intent_id AND phase = :file_committed
    """
)

#: W3 core: projection-row write and journal advance share ONE transaction.
_UPSERT_PROJECTION = text(
    """
    INSERT INTO profile_projection (server_id, revision, schema_revision, sha256)
    VALUES (:server_id, :revision, :schema_revision, :sha256)
    ON CONFLICT (server_id) DO UPDATE
       SET revision = EXCLUDED.revision,
           schema_revision = EXCLUDED.schema_revision,
           sha256 = EXCLUDED.sha256,
           reconciled_at = now()
    """
)

_DELETE_PROJECTION = text(
    "DELETE FROM profile_projection WHERE server_id = :server_id"
)

_PURGE_RECONCILED = text(
    "DELETE FROM profile_projection_journal WHERE phase = :reconciled"
)

_PURGE_RECONCILED_BEFORE = text(
    """
    DELETE FROM profile_projection_journal
     WHERE phase = :reconciled AND updated_at < :before
    """
)


def _intent_of(row: Sequence[object] | object) -> JournalIntent:
    """Materialize one journal row (mapping) into the frozen dataclass."""
    return JournalIntent(
        intent_id=row["intent_id"],  # type: ignore[index]
        server_id=row["server_id"],  # type: ignore[index]
        op=row["op"],  # type: ignore[index]
        phase=row["phase"],  # type: ignore[index]
        expected_revision=row["expected_revision"],  # type: ignore[index]
        base_sha256=row["base_sha256"],  # type: ignore[index]
        target_revision=row["target_revision"],  # type: ignore[index]
        target_sha256=row["target_sha256"],  # type: ignore[index]
        schema_revision=row["schema_revision"],  # type: ignore[index]
        payload=row["payload"],  # type: ignore[index]
        created_at=row["created_at"],  # type: ignore[index]
        updated_at=row["updated_at"],  # type: ignore[index]
    )


# ---------------------------------------------------------------------------
# File-state classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileState:
    stored: StoredProfile | None
    error: ProfileFileError | None


def _read_file(store: ProfileFileStore, server_id: uuid.UUID) -> _FileState:
    """Classify one canonical read; rejections stay value-free."""
    try:
        return _FileState(stored=store.read(server_id), error=None)
    except ProfileFileError as exc:
        return _FileState(stored=None, error=exc)


def _read_file_or_absent(store: ProfileFileStore, server_id: uuid.UUID) -> _FileState:
    """Like :func:`_read_file` but a NOT_FOUND store rejection means "the
    canonical profile is absent" — the normal create/delete baseline —
    while every other rejection (interrupted pair, confinement, tamper)
    stays surfaced."""
    state = _read_file(store, server_id)
    if state.error is not None and (
        state.error.code is FileStoreErrorCode.NOT_FOUND
    ):
        return _FileState(stored=None, error=None)
    return state


def _matches(stored: StoredProfile, revision: int | None, sha256: str | None) -> bool:
    return revision is not None and sha256 is not None and (
        stored.revision == revision and stored.sha256 == sha256
    )


# ---------------------------------------------------------------------------
# Projection worker
# ---------------------------------------------------------------------------


class ProfileProjection:
    """Reconciliation journal worker over the canonical profile file store.

    Single-writer per ``server_id`` is a caller invariant (C05 rule 6): the
    journal's partial unique index blocks a second *live* intent per server,
    but it is not a distributed lock. All file effects go through
    :class:`~mai.profiles.files.ProfileFileStore`; all SQL effects go through
    the session factory built by :func:`mai.database.create_app_engine`.
    File-store calls are small bounded local atomic writes executed inline.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    # -- intent preparation (durable "prepared intent", C02 rule 5) --------

    async def prepare_upsert(
        self,
        server_id: uuid.UUID,
        document: str,
        *,
        expected_revision: int,
        base_sha256: str | None = None,
        intent_id: uuid.UUID | None = None,
    ) -> JournalIntent:
        """Journal the upsert intent (create/update path)."""
        intent, _ = await self._prepare(
            server_id=server_id,
            op=OP_UPSERT,
            document=document,
            expected_revision=expected_revision,
            base_sha256=base_sha256,
            intent_id=intent_id,
        )
        return intent

    async def prepare_delete(
        self,
        server_id: uuid.UUID,
        *,
        expected_revision: int,
        base_sha256: str | None = None,
        intent_id: uuid.UUID | None = None,
    ) -> JournalIntent:
        """Journal the delete tombstone BEFORE any file effect (C02 W4)."""
        intent, _ = await self._prepare(
            server_id=server_id,
            op=OP_DELETE,
            document=None,
            expected_revision=expected_revision,
            base_sha256=base_sha256,
            intent_id=intent_id,
        )
        return intent

    async def _prepare(
        self,
        *,
        server_id: uuid.UUID,
        op: str,
        document: str | None,
        expected_revision: int,
        base_sha256: str | None,
        intent_id: uuid.UUID | None,
    ) -> tuple[JournalIntent, bool]:
        """Insert or resume one intent; returns (intent, resumed_live).

        Idempotent by ``intent_id``: an existing row with the same id, server,
        op, expected revision and payload hash is returned as-is (retry
        support); any other divergence is rejected. At most one live intent
        per server exists (partial unique index, C05 rule 6 mirror).
        """
        sid = _validated_server_id(server_id)
        iid = (
            _validated_intent_id(intent_id)
            if intent_id is not None
            else uuid.uuid4()
        )
        expected = _validated_expected_revision(expected_revision)
        base = _validated_sha256(base_sha256)
        if op == OP_UPSERT:
            document = _validated_document(document)
            params: dict[str, object] = {
                "intent_id": iid,
                "server_id": sid,
                "op": op,
                "phase": PHASE_PREPARED,
                "expected_revision": expected,
                "base_sha256": base,
                "target_revision": expected + 1,
                "target_sha256": _document_sha256(document),
                "schema_revision": SCHEMA_VERSION,
                "payload": document,
            }
        else:
            params = {
                "intent_id": iid,
                "server_id": sid,
                "op": op,
                "phase": PHASE_PREPARED,
                "expected_revision": expected,
                "base_sha256": base,
                "target_revision": None,
                "target_sha256": None,
                "schema_revision": None,
                "payload": None,
            }
        async with self._sessions() as session, session.begin():
            existing = await self._fetch_intent(session, iid)
            if existing is not None:
                self._ensure_resume(existing, sid, op, expected, params["target_sha256"])
                return existing, existing.phase in _LIVE_PHASES
            try:
                await session.execute(_INSERT_INTENT, params)
            except IntegrityError as exc:
                raise ProjectionError(
                    ProjectionErrorCode.INTENT_PENDING,
                    "another live intent already exists for this server",
                ) from exc
            created = await self._fetch_intent(session, iid)
        assert created is not None  # inserted above in the same transaction
        return created, False

    @staticmethod
    def _ensure_resume(
        existing: JournalIntent,
        server_id: uuid.UUID,
        op: str,
        expected_revision: int,
        target_sha256: object,
    ) -> None:
        if existing.server_id != server_id or existing.op != op:
            raise ProjectionError(
                ProjectionErrorCode.INTENT_MISMATCH,
                "intent id already used for a different server or operation",
            )
        if existing.expected_revision != expected_revision:
            raise ProjectionError(
                ProjectionErrorCode.PAYLOAD_MISMATCH,
                "retry does not match the journaled expected revision",
            )
        if op == OP_UPSERT and existing.target_sha256 != target_sha256:
            raise ProjectionError(
                ProjectionErrorCode.PAYLOAD_MISMATCH,
                "retry document hash differs from the journaled payload",
            )

    # -- full application pipelines (prepare -> file -> reconcile) ---------

    async def apply_upsert(
        self,
        store: ProfileFileStore,
        server_id: uuid.UUID,
        document: str,
        *,
        expected_revision: int,
        base_sha256: str | None = None,
        intent_id: uuid.UUID | None = None,
    ) -> ApplyResult:
        """Prepare, commit the file, then reconcile — the full C02 pipeline.

        Retrying a reconciled intent with the same ``intent_id`` performs no
        file or SQL effect and reports ``already_reconciled`` (one resulting
        profile per intent — FILE-CRASH scenario). A live intent is resumed
        from its recorded phase.
        """
        sid = _validated_server_id(server_id)
        intent, resumed = await self._prepare(
            server_id=sid,
            op=OP_UPSERT,
            document=document,
            expected_revision=expected_revision,
            base_sha256=base_sha256,
            intent_id=intent_id,
        )
        assert intent.target_revision is not None
        assert intent.target_sha256 is not None
        if intent.phase == PHASE_RECONCILED:
            return await self._already_or_repair_upsert(store, intent)
        if intent.phase == PHASE_PREPARED:
            self._commit_file_for_upsert(store, intent)
            intent = await self._mark_file_committed(intent.intent_id)
        observed = await self._reconcile_upsert_from_file(intent, store)
        return ApplyResult(
            intent.intent_id, sid, OP_UPSERT,
            "resumed" if resumed else "applied",
            observed.revision, observed.sha256,
        )

    async def apply_delete(
        self,
        store: ProfileFileStore,
        server_id: uuid.UUID,
        *,
        expected_revision: int,
        base_sha256: str | None = None,
        intent_id: uuid.UUID | None = None,
    ) -> ApplyResult:
        """Tombstone first, delete the file, then purge the projection (W4)."""
        sid = _validated_server_id(server_id)
        intent, resumed = await self._prepare(
            server_id=sid,
            op=OP_DELETE,
            document=None,
            expected_revision=expected_revision,
            base_sha256=base_sha256,
            intent_id=intent_id,
        )
        if intent.phase == PHASE_RECONCILED:
            return await self._already_or_repair_delete(store, intent)
        if intent.phase == PHASE_PREPARED:
            self._delete_file(store, intent)
            intent = await self._mark_file_committed(intent.intent_id)
        await self._reconcile_delete(intent, store)
        return ApplyResult(
            intent.intent_id, sid, OP_DELETE,
            "resumed" if resumed else "applied", None, None,
        )

    async def _already_or_repair_upsert(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> ApplyResult:
        """Reconciled-intent retry: verify convergence, repair if stale.

        A reconciled row means the intent's own file effect was journaled as
        complete — but only the projection holding the target triple proves
        it is actually visible. If the projection drifted (canonical file
        replaced outside the journal after reconcile), the retry converges
        from the file's observed truth instead of claiming a success the
        projection does not hold; a file that no longer matches the intent's
        target at all is surfaced, never overwritten.
        """
        assert intent.target_revision is not None
        assert intent.target_sha256 is not None
        state = await self.read_projection(intent.server_id)
        if (
            state is not None
            and state.revision == intent.target_revision
            and state.sha256 == intent.target_sha256
            and not state.stale
        ):
            return ApplyResult(
                intent.intent_id, intent.server_id, OP_UPSERT,
                "already_reconciled", intent.target_revision,
                intent.target_sha256,
            )
        observed = _read_file_or_absent(store, intent.server_id)
        if observed.error is not None:
            raise observed.error
        stored = observed.stored
        if stored is None or not _matches(
            stored, intent.target_revision, intent.target_sha256
        ):
            raise ProjectionError(
                ProjectionErrorCode.FILE_DIVERGED,
                "reconciled intent's target no longer matches the canonical "
                "file; explicit admin resolution required",
            )
        await self._reconcile_upsert_from_file(intent, store)
        return ApplyResult(
            intent.intent_id, intent.server_id, OP_UPSERT, "resumed",
            stored.revision, stored.sha256,
        )

    async def _already_or_repair_delete(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> ApplyResult:
        """Reconciled delete-intent retry: verify absence on both sides."""
        state = await self.read_projection(intent.server_id)
        observed = _read_file_or_absent(store, intent.server_id)
        if observed.error is not None:
            raise observed.error
        if observed.stored is None and state is None:
            return ApplyResult(
                intent.intent_id, intent.server_id, OP_DELETE,
                "already_reconciled", None, None,
            )
        if observed.stored is not None:
            raise ProjectionError(
                ProjectionErrorCode.TOMBSTONE_CONFLICT,
                "delete intent is reconciled but a canonical file exists "
                "again; explicit admin resolution required",
            )
        # Projection row survived while the file is gone: purge it now.
        await self._reconcile_delete(intent, store)
        return ApplyResult(
            intent.intent_id, intent.server_id, OP_DELETE, "resumed", None, None,
        )

    # -- recovery sweep (projection worker routine) -------------------------

    async def recover(
        self,
        store: ProfileFileStore,
        *,
        server_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[RecoveryOutcome]:
        """Reconcile every live intent against the authoritative files.

        Per-intent failures are recorded and the sweep continues; nothing is
        ever invented: reconcile writes only what the file read returned.
        """
        sid = (
            _validated_server_id(server_id) if server_id is not None else None
        )
        intents = await self.list_live_intents(server_id=sid, limit=limit)
        outcomes: list[RecoveryOutcome] = []
        for intent in intents:
            outcomes.append(await self._recover_one(store, intent))
        return outcomes

    async def _recover_one(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        try:
            if intent.op == OP_UPSERT:
                return await self._recover_upsert(store, intent)
            return await self._recover_delete(store, intent)
        except (ProfileFileError, ProjectionError) as exc:
            code = exc.code
            code_value = (
                code.value if isinstance(code, ProjectionErrorCode) else str(code)
            )
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op, "conflict",
                code_value, exc.detail, None,
            )
        except Exception as exc:  # noqa: BLE001 - sweep continues; value-free
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op, "error",
                type(exc).__name__,
                "unexpected failure; detail withheld to keep transcripts "
                "value-free",
                None,
            )

    async def _recover_upsert(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        if intent.phase == PHASE_FILE_COMMITTED:
            # W2: the file commit landed before the crash; the projection is
            # simply stale. Reconcile observed truth, never the claim.
            # (Payload is NULL in this phase — only re-execution needs it.)
            observed = await self._reconcile_upsert_from_file(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op,
                "advanced_reconciled", None,
                "file committed before crash; reconciled from file",
                observed.revision,
            )
        payload = intent.payload
        if payload is None:
            raise ProjectionError(
                ProjectionErrorCode.INTENT_PHASE,
                "prepared upsert intent has no document payload",
            )
        if intent.target_revision is None or intent.target_sha256 is None:
            raise ProjectionError(
                ProjectionErrorCode.INTENT_PHASE,
                "upsert intent is missing its target triple",
            )
        state = _read_file_or_absent(store, intent.server_id)
        if state.error is not None:
            # Interrupted canonical/metadata pair or confinement failure:
            # surfaced, never repaired by invention (C02 rule 5).
            raise state.error
        stored = state.stored
        if stored is not None and _matches(
            stored, intent.target_revision, intent.target_sha256
        ):
            # W1, crash-after-replace variant: the file commit landed before
            # the journal advance — recognize it, never rewrite the file.
            await self._mark_file_committed(intent.intent_id)
            observed = await self._reconcile_upsert_from_file(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op,
                "advanced_reconciled", None, "file already at intent target",
                observed.revision,
            )
        if stored is not None and (
            stored.revision != intent.expected_revision
            or (
                intent.base_sha256 is not None
                and stored.sha256 != intent.base_sha256
            )
        ):
            raise ProjectionError(
                ProjectionErrorCode.FILE_DIVERGED,
                "canonical file is neither at the intent target nor at the "
                "observed baseline",
            )
        # W1, uncommitted variant: file unchanged/absent -> re-execute.
        committed = store.write(
            intent.server_id,
            payload,
            expected_revision=intent.expected_revision,
        )
        if not _matches(
            committed, intent.target_revision, intent.target_sha256
        ):
            raise ProjectionError(
                ProjectionErrorCode.FILE_DIVERGED,
                "re-executed file commit does not match the intent target",
            )
        await self._mark_file_committed(intent.intent_id)
        observed = await self._reconcile_upsert_from_file(intent, store)
        return RecoveryOutcome(
            intent.intent_id, intent.server_id, intent.op,
            "reexecuted_reconciled", None,
            "file commit re-executed from journal payload", observed.revision,
        )

    async def _recover_delete(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> RecoveryOutcome:
        if intent.phase == PHASE_FILE_COMMITTED:
            # W2/W4: the file delete landed before the crash; purge the
            # projection row now (tombstone still live until reconciled).
            # A resurrected file surfaces as tombstone_conflict here too.
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op,
                "advanced_reconciled", None,
                "file deleted before crash; projection purged", None,
            )
        state = _read_file_or_absent(store, intent.server_id)
        if state.error is not None:
            raise state.error
        stored = state.stored
        if stored is not None and _matches(
            stored, intent.expected_revision, intent.base_sha256
        ):
            # Tombstone live, delete not yet executed -> re-execute it.
            self._delete_file(store, intent)
            await self._mark_file_committed(intent.intent_id)
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op,
                "reexecuted_reconciled", None, "file delete re-executed",
                None,
            )
        if stored is None:
            # Crash after the file delete, before the journal advance.
            await self._mark_file_committed(intent.intent_id)
            await self._reconcile_delete(intent, store)
            return RecoveryOutcome(
                intent.intent_id, intent.server_id, intent.op,
                "advanced_reconciled", None, "file already absent", None,
            )
        # W4: a live file under a live tombstone is a conflict for the admin.
        raise ProjectionError(
            ProjectionErrorCode.TOMBSTONE_CONFLICT,
            "canonical file exists while a delete tombstone is live; "
            "explicit admin resolution required",
        )

    # -- journal/SQL primitives ---------------------------------------------

    async def _mark_file_committed(self, intent_id: uuid.UUID) -> JournalIntent:
        """Advance prepared -> file_committed, dropping the document payload."""
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                _ADVANCE_FILE_COMMITTED,
                {
                    "intent_id": intent_id,
                    "prepared": PHASE_PREPARED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if result.rowcount != 1:
                existing = await self._fetch_intent(session, intent_id)
                if existing is None:
                    raise ProjectionError(
                        ProjectionErrorCode.INTENT_NOT_FOUND,
                        "no journal row for this intent id",
                    )
                raise ProjectionError(
                    ProjectionErrorCode.INTENT_PHASE,
                    f"intent is in phase {existing.phase}, not {PHASE_PREPARED}",
                )
            updated = await self._fetch_intent(session, intent_id)
        assert updated is not None
        return updated

    async def _reconcile_upsert_from_file(
        self, intent: JournalIntent, store: ProfileFileStore
    ) -> StoredProfile:
        """W2/W3: project the file's ACTUAL triple, advancing the journal.

        The file is read at reconcile time; its observed triple — never the
        intent's claim — is written, in the same transaction that advances
        the journal row to ``reconciled`` ("no invented successful sync").
        """
        if intent.target_revision is None or intent.target_sha256 is None:
            raise ProjectionError(
                ProjectionErrorCode.INTENT_PHASE,
                "upsert intent is missing its target triple",
            )
        observed = _read_file(store, intent.server_id)
        if observed.error is not None:
            raise observed.error
        stored = observed.stored
        assert stored is not None  # read succeeded
        if not _matches(stored, intent.target_revision, intent.target_sha256):
            raise ProjectionError(
                ProjectionErrorCode.FILE_DIVERGED,
                "canonical file does not match the intent target at "
                "reconcile time",
            )
        async with self._sessions() as session, session.begin():
            await session.execute(
                _UPSERT_PROJECTION,
                {
                    "server_id": intent.server_id,
                    "revision": stored.revision,
                    "schema_revision": stored.schema_revision,
                    "sha256": stored.sha256,
                },
            )
            advanced = await session.execute(
                _ADVANCE_RECONCILED,
                {
                    "intent_id": intent.intent_id,
                    "reconciled": PHASE_RECONCILED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if advanced.rowcount != 1:
                existing = await self._fetch_intent(session, intent.intent_id)
                if existing is None or existing.phase != PHASE_RECONCILED:
                    raise ProjectionError(
                        ProjectionErrorCode.INTENT_PHASE,
                        f"intent is not in phase {PHASE_FILE_COMMITTED}",
                    )
                # Repair path: the row was already reconciled; the idempotent
                # projection write is the effect that matters here.
        return stored

    async def _reconcile_delete(
        self, intent: JournalIntent, store: ProfileFileStore
    ) -> None:
        """Purge the projection row only after the file absence confirms."""
        state = _read_file_or_absent(store, intent.server_id)
        if state.error is not None:
            raise state.error
        if state.stored is not None:
            raise ProjectionError(
                ProjectionErrorCode.TOMBSTONE_CONFLICT,
                "canonical file exists while a delete tombstone is live; "
                "explicit admin resolution required",
            )
        async with self._sessions() as session, session.begin():
            await session.execute(
                _DELETE_PROJECTION, {"server_id": intent.server_id}
            )
            advanced = await session.execute(
                _ADVANCE_RECONCILED,
                {
                    "intent_id": intent.intent_id,
                    "reconciled": PHASE_RECONCILED,
                    "file_committed": PHASE_FILE_COMMITTED,
                },
            )
            if advanced.rowcount != 1:
                existing = await self._fetch_intent(session, intent.intent_id)
                if existing is None or existing.phase != PHASE_RECONCILED:
                    raise ProjectionError(
                        ProjectionErrorCode.INTENT_PHASE,
                        f"intent is not in phase {PHASE_FILE_COMMITTED}",
                    )

    def _commit_file_for_upsert(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> None:
        """Execute the file half of a prepared upsert (W1-aware)."""
        payload = intent.payload
        if payload is None:
            raise ProjectionError(
                ProjectionErrorCode.INTENT_PHASE,
                "prepared upsert intent has no document payload",
            )
        assert intent.target_revision is not None
        assert intent.target_sha256 is not None
        state = _read_file_or_absent(store, intent.server_id)
        if state.error is not None:
            # Interrupted pair from an earlier crash: not silently repairable.
            raise state.error
        if state.stored is not None and _matches(
            state.stored, intent.target_revision, intent.target_sha256
        ):
            return  # crash-after-replace variant: file already committed
        try:
            committed = store.write(
                intent.server_id,
                payload,
                expected_revision=intent.expected_revision,
            )
        except ProfileFileError as exc:
            if exc.code is not FileStoreErrorCode.STALE_REVISION:
                raise
            # The file moved between the read above and the write. Under the
            # single-writer invariant this can only be our own committed
            # intent; anything else is surfaced, never overwritten.
            state = _read_file(store, intent.server_id)
            if state.stored is None or not _matches(
                state.stored, intent.target_revision, intent.target_sha256
            ):
                raise
            return
        if not _matches(
            committed, intent.target_revision, intent.target_sha256
        ):
            raise ProjectionError(
                ProjectionErrorCode.FILE_DIVERGED,
                "file commit result does not match the intent target",
            )

    def _delete_file(
        self, store: ProfileFileStore, intent: JournalIntent
    ) -> None:
        """Execute the file half of a tombstoned delete (W4-aware)."""
        try:
            store.delete(
                intent.server_id, expected_revision=intent.expected_revision
            )
        except ProfileFileError as exc:
            if exc.code is FileStoreErrorCode.NOT_FOUND:
                return  # already gone: crash-after-delete variant
            if exc.code is not FileStoreErrorCode.STALE_REVISION:
                raise
            state = _read_file(store, intent.server_id)
            if state.stored is None:
                return
            raise

    # -- reads and maintenance ----------------------------------------------

    async def read_projection(self, server_id: uuid.UUID) -> ProjectionState | None:
        """Projected triple with the C02-W2 staleness flag."""
        sid = _validated_server_id(server_id)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    _SELECT_PROJECTION,
                    {"server_id": sid, "reconciled": PHASE_RECONCILED},
                )
            ).mappings().first()
        if row is None:
            return None
        return ProjectionState(
            server_id=row["server_id"],
            revision=row["revision"],
            schema_revision=row["schema_revision"],
            sha256=row["sha256"],
            first_reconciled_at=row["first_reconciled_at"],
            reconciled_at=row["reconciled_at"],
            stale=bool(row["stale"]),
        )

    async def get_intent(self, intent_id: uuid.UUID) -> JournalIntent | None:
        """Fetch one journal row by id (diagnostics, retry bookkeeping)."""
        iid = _validated_intent_id(intent_id)
        async with self._sessions() as session:
            return await self._fetch_intent(session, iid)

    async def list_live_intents(
        self,
        *,
        server_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[JournalIntent]:
        """Every non-reconciled intent, oldest first (sweep order)."""
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise ProjectionError(
                ProjectionErrorCode.INVALID_REVISION,
                "limit must be a positive int",
            )
        sid = (
            _validated_server_id(server_id) if server_id is not None else None
        )
        statement = (
            _SELECT_LIVE_INTENTS_SERVER if sid is not None else _SELECT_LIVE_INTENTS_ALL
        )
        params: dict[str, object] = {"reconciled": PHASE_RECONCILED}
        if sid is not None:
            params["server_id"] = sid
        if limit is not None:
            statement = text(statement.text + " LIMIT :limit")
            params["limit"] = limit
        async with self._sessions() as session:
            rows = (await session.execute(statement, params)).mappings().all()
        return [_intent_of(row) for row in rows]

    async def purge_reconciled(self, *, before: datetime | None = None) -> int:
        """W4 purge: drop reconciled history/tombstones (explicit maintenance).

        Reconciled rows are retained until this is called so that intent
        retries stay idempotent; the tombstone lifecycle still honors the
        frozen order — purge happens only after the projection confirmed.
        """
        async with self._sessions() as session, session.begin():
            if before is None:
                result = await session.execute(
                    _PURGE_RECONCILED, {"reconciled": PHASE_RECONCILED}
                )
            else:
                result = await session.execute(
                    _PURGE_RECONCILED_BEFORE,
                    {"reconciled": PHASE_RECONCILED, "before": before},
                )
        return int(result.rowcount or 0)

    # -- internals -----------------------------------------------------------

    async def _fetch_intent(
        self, session: AsyncSession, intent_id: uuid.UUID
    ) -> JournalIntent | None:
        row = (
            await session.execute(_SELECT_INTENT, {"intent_id": intent_id})
        ).mappings().first()
        return None if row is None else _intent_of(row)


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _public_intent(intent: JournalIntent) -> dict[str, object]:
    """Value-free view: payload reported as a byte count, never as text."""
    return {
        "kind": "journal",
        "intent_id": str(intent.intent_id),
        "server_id": str(intent.server_id),
        "op": intent.op,
        "phase": intent.phase,
        "expected_revision": intent.expected_revision,
        "target_revision": intent.target_revision,
        "payload_bytes": (
            len(intent.payload.encode("utf-8"))
            if intent.payload is not None
            else 0
        ),
        "created_at": intent.created_at.isoformat(),
        "updated_at": intent.updated_at.isoformat(),
    }


def _public_projection(state: ProjectionState) -> dict[str, object]:
    return {
        "kind": "projection",
        "server_id": str(state.server_id),
        "revision": state.revision,
        "schema_revision": state.schema_revision,
        "sha256": state.sha256,
        "stale": state.stale,
        "reconciled_at": state.reconciled_at.isoformat(),
    }


def _public_outcome(outcome: RecoveryOutcome) -> dict[str, object]:
    return {
        "kind": "recovery",
        "intent_id": str(outcome.intent_id),
        "server_id": str(outcome.server_id),
        "op": outcome.op,
        "status": outcome.status,
        "code": outcome.code,
        "detail": outcome.detail,
        "revision": outcome.revision,
    }


async def _run_status(
    url: str, server_id: uuid.UUID | None
) -> int:
    engine, sessions = create_app_engine(url)
    projection = ProfileProjection(sessions)
    try:
        intents = await projection.list_live_intents(server_id=server_id)
        for intent in intents:
            print(json.dumps(_public_intent(intent)))
        if server_id is not None:
            state = await projection.read_projection(server_id)
            if state is None:
                print(
                    json.dumps(
                        {
                            "kind": "projection",
                            "server_id": str(server_id),
                            "absent": True,
                        }
                    )
                )
            else:
                print(json.dumps(_public_projection(state)))
        print(json.dumps({"kind": "status", "live_intents": len(intents)}))
    finally:
        await engine.dispose()
    return 0


async def _run_recover(
    url: str, store: ProfileFileStore, server_id: uuid.UUID | None
) -> list[RecoveryOutcome]:
    engine, sessions = create_app_engine(url)
    projection = ProfileProjection(sessions)
    try:
        return await projection.recover(store, server_id=server_id)
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.profiles.projection",
        description="Profile projection and reconciliation journal (C02).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        help="list live journal intents (value-free JSON lines); "
        "with SERVER_ID also print the projected triple",
    )
    status.add_argument("root", help="canonical data root for the file store")
    status.add_argument(
        "server_id", nargs="?", default=None, help="restrict to one server"
    )

    recover = sub.add_parser(
        "recover",
        help="run the reconciliation sweep against the authoritative files",
    )
    recover.add_argument("root", help="canonical data root for the file store")
    recover.add_argument(
        "server_id", nargs="?", default=None, help="restrict sweep to one server"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; exit 0 success / 1 structured conflict or rejection /
    2 usage or configuration error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        url = resolve_database_url()
    except DatabaseConfigError as exc:
        print(
            json.dumps(
                {"error": {"code": exc.health.value, "message": str(exc)}}
            ),
            file=sys.stderr,
        )
        return 2
    try:
        sid = (
            _validated_server_id(uuid.UUID(args.server_id))
            if args.server_id is not None
            else None
        )
    except (ValueError, ProjectionError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": ProjectionErrorCode.INVALID_SERVER_ID.value,
                        "message": "SERVER_ID must be a canonical UUID string",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1
    if args.command == "status":
        return asyncio.run(_run_status(url, sid))
    store = ProfileFileStore(args.root)
    outcomes = asyncio.run(_run_recover(url, store, sid))
    unresolved = 0
    for outcome in outcomes:
        print(json.dumps(_public_outcome(outcome)))
        if outcome.status in ("conflict", "error"):
            unresolved += 1
    print(
        json.dumps(
            {
                "kind": "recovery_summary",
                "count": len(outcomes),
                "unresolved": unresolved,
            }
        )
    )
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
