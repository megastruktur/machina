"""Paged service resolution and knowledge reads (ask_11, D2/D3, C1-C4).

The read half of the service-knowledge feature: same-server service
resolution (exact application id / canonical selector / ambiguous name
page), the bounded per-service knowledge read (operator map entry +
catalog runbook separated from learned facts, generation- and
status-scoped), the paginated append-only fact history, and the paged
durable operation receipts (:data:`MAX_SERIALIZED_ENVELOPE_BYTES`-bounded
envelopes throughout).

Scope discipline (C3): every read is pinned to an explicit authorized
``server_id``; a differing ``target_server_id`` is rejected via
:class:`mai.applications.knowledge_contracts.ScopeMismatchError` — never
substituted. Candidate resolution NEVER returns entries from another
server, even for identical service names.

Pure read service: the only state is the injected async session factory
and the confined :class:`~mai.applications.service_map.ServiceMapStore`;
every collection query applies deterministic ordering with ``LIMIT``/
``OFFSET`` and computes ``COUNT(*)`` under the exact same filter
predicate (AGENTS.md pagination invariant, C1).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.applications.knowledge_contracts import (
    DEFAULT_PAGE_LIMIT,
    FactPage,
    FactRecord,
    FactSource,
    FactSourceKind,
    FactStatus,
    MAX_PAGE_LIMIT,
    OperationPage,
    OperationReceipt,
    OperationStatus,
    SubEffectStatus,
    enforce_envelope_bound,
    validate_same_server_scope,
)
from mai.applications.service_map import ServiceMapEntry, ServiceMapStore

__all__ = [
    "FactRevisionPage",
    "FactRevisionRecord",
    "KnowledgeRead",
    "KnowledgeReadStore",
    "ServiceCandidate",
    "ServiceKnowledgeNotFoundError",
    "ServiceKnowledgeReadService",
    "ServiceResolution",
]

#: SQL identity for the 0018 tables (the frozen cross-slice vocabulary).
_FACT_TABLE: Final[str] = "service_learned_fact"
_REVISION_TABLE: Final[str] = "service_learned_fact_revision"
_OPERATION_TABLE: Final[str] = "service_learning_operation"
_IDENTITY_TABLE: Final[str] = "service_knowledge_identity"

#: Deterministic active-fact ordering: ``(created_at, fact_id)`` ascending
#: (mirrors ``deterministic_order_key``).
_FACT_ORDER_SQL: Final[str] = "created_at ASC, fact_id ASC"

#: Deterministic history ordering: newest change first, stable ties.
_HISTORY_ORDER_SQL: Final[str] = "changed_at DESC, revision DESC, revision_id DESC"

#: Deterministic operation ordering: newest acceptance first, stable ties
#: (mirrors ``ix_service_learning_operation_status``).
_OPERATION_ORDER_SQL: Final[str] = "accepted_at DESC, operation_id DESC"


class ServiceKnowledgeNotFoundError(ValueError):
    """No service exists for the requested ``(server_id, application_id)``
    in EITHER the operator map OR the knowledge identity table
    (``code = "not_found"``)."""

    code: Final[str] = "not_found"

    def __init__(self, server_id: uuid.UUID, application_id: uuid.UUID) -> None:
        super().__init__(
            f"no service knowledge exists for application {application_id} "
            f"on server {server_id}"
        )
        self.server_id = server_id
        self.application_id = application_id


class _ReadModel(BaseModel):
    """Base for every read-result shape (fail-closed capture)."""

    model_config = ConfigDict(extra="forbid")


class ServiceCandidate(_ReadModel):
    """One resolvable service candidate: the operator map entry merged
    with its knowledge identity defaults (D2)."""

    application_id: uuid.UUID = Field(
        description="Canonical application identity of the candidate service",
    )
    server_id: uuid.UUID = Field(
        description="Authorized server identity the candidate belongs to",
    )
    name: str = Field(
        description="Operator-curated display name of the service",
    )
    origin: str = Field(
        description="Adoptable discovery origin (compose, systemd_unit, manual)",
    )
    compose_project: str | None = Field(
        default=None,
        description="Compose project name for compose-origin candidates",
    )
    working_dir: str | None = Field(
        default=None,
        description="Compose working directory for compose-origin candidates",
    )
    unit_name: str | None = Field(
        default=None,
        description="Systemd unit name for systemd-origin candidates",
    )
    category: str = Field(
        description="Taxonomy category of the service",
    )
    installed: bool = Field(
        description="Whether the service is currently installed on the host",
    )
    map_revision: int = Field(
        description="Operator map revision of the candidate entry",
    )
    generation: int = Field(
        description="Knowledge identity generation (1 when no SQL row exists)",
    )
    service_revision: int = Field(
        description="Observed service revision (map revision when no SQL row exists)",
    )
    knowledge_revision: int = Field(
        description="Knowledge revision (0 when no SQL row exists)",
    )
    manual_identity: bool = Field(
        description="Whether this is an evidenced map-only manual identity",
    )
    auto_adopt_suppressed: bool = Field(
        description="Whether auto-adoption is suppressed for this identity",
    )


class ServiceResolution(_ReadModel):
    """The resolution result: exact hits first, otherwise the bounded
    deterministic candidate page (D2/AC1)."""

    exact_match: bool = Field(
        description="True when exactly one candidate matched by id or selector",
    )
    match_kind: str = Field(
        description="How the resolution matched: exact_application_id, "
        "exact_selector, or candidates",
    )
    ambiguous: bool = Field(
        description="True when more than one candidate matched the query",
    )
    candidates: list[ServiceCandidate] = Field(
        description="The ordered candidate page (single item on exact hits)",
    )
    total: int = Field(
        description="Total number of matching candidates on this server",
    )
    offset: int = Field(
        ge=0,
        description="Zero-based offset of this page into the candidate list",
    )
    limit: int = Field(
        ge=1,
        description="Requested maximum number of candidates in this page",
    )
    has_more: bool = Field(
        description="True when candidates exist beyond this page window",
    )


class KnowledgeRead(_ReadModel):
    """The bounded per-service knowledge read (D2/D3): operator-curated
    map state and catalog runbook CLEARLY SEPARATED from the active
    learned-fact page and the conflict summary."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Authorized server identity the knowledge belongs to",
    )
    application_id: uuid.UUID = Field(
        description="Canonical application identity of the service",
    )
    found_in_map: bool = Field(
        description="Whether an operator-curated map entry exists for this service",
    )
    generation: int = Field(
        description="Knowledge identity generation (1 when no SQL row exists)",
    )
    service_revision: int = Field(
        description="Observed service revision (map revision when no SQL row exists)",
    )
    knowledge_revision: int = Field(
        description="Knowledge revision (0 when no SQL row exists)",
    )
    manual_identity: bool = Field(
        description="Whether this is an evidenced map-only manual identity",
    )
    auto_adopt_suppressed: bool = Field(
        description="Whether auto-adoption is suppressed for this identity",
    )
    name: str | None = Field(
        default=None,
        description="Operator-curated display name (map entries only)",
    )
    origin: str | None = Field(
        default=None,
        description="Adoptable discovery origin (map entries only)",
    )
    category: str | None = Field(
        default=None,
        description="Taxonomy category (map entries only)",
    )
    description: str | None = Field(
        default=None,
        description="Operator-curated description (map entries only)",
    )
    custom_instructions: str | None = Field(
        default=None,
        description="Operator-curated custom instructions (map entries only, "
        "kept separate from learned facts)",
    )
    instructions: dict[str, str] | None = Field(
        default=None,
        description="Catalog runbook slots (map entries only)",
    )
    installed: bool | None = Field(
        default=None,
        description="Whether the service is currently installed (map entries only)",
    )
    map_revision: int | None = Field(
        default=None,
        description="Operator map revision (map entries only)",
    )
    catalog_runbook: dict[str, Any] | None = Field(
        default=None,
        description="Matching global catalog runbook document when one exists",
    )
    conflict_count: int = Field(
        description="Number of CONFLICT facts in the current generation",
    )
    conflicts: list[FactRecord] = Field(
        default_factory=list,
        description="Current-generation CONFLICT facts (populated when "
        "include_conflicts is requested; never part of the active page)",
    )
    facts: FactPage = Field(
        description="The bounded ACTIVE learned-fact page for the current generation",
    )


class FactRevisionRecord(_ReadModel):
    """One append-only fact-history row joined with its fact identity
    (D3)."""

    model_config = ConfigDict(extra="forbid")

    revision_id: uuid.UUID = Field(
        description="Canonical UUID identity of the history row",
    )
    fact_id: uuid.UUID = Field(
        description="Learned fact this revision belongs to",
    )
    server_id: uuid.UUID = Field(
        description="Authorized server identity the fact belongs to",
    )
    application_id: uuid.UUID = Field(
        description="Canonical application identity of the service",
    )
    generation: int = Field(
        description="Knowledge identity generation of the fact",
    )
    category: str = Field(
        description="Taxonomy category of the fact",
    )
    key: str = Field(
        description="Semantic key of the fact",
    )
    revision: int = Field(
        description="Fact revision recorded by this history row",
    )
    statement: str = Field(
        description="Historical statement value recorded for this revision",
    )
    status: FactStatus = Field(
        description="Lifecycle status recorded for this revision",
    )
    source_kind: FactSourceKind = Field(
        description="Provenance kind recorded for this revision",
    )
    source_ref: str = Field(
        description="Bounded persisted provenance reference for this revision",
    )
    changed_at: datetime = Field(
        description="Trusted timestamp when this revision was recorded",
    )


class FactRevisionPage(_ReadModel):
    """One bounded page of fact-history rows (C3): the frozen paging
    metadata plus the ordered rows."""

    model_config = ConfigDict(extra="forbid")

    items: list[FactRevisionRecord] = Field(
        description="The ordered history rows inside this page window",
    )
    limit: int = Field(
        ge=1,
        description="Requested maximum number of history rows in this page",
    )
    offset: int = Field(
        ge=0,
        description="Zero-based offset of this page into the history",
    )
    total: int = Field(
        description="Total number of history rows matching the filter",
    )
    has_more: bool = Field(
        description="True when history rows exist beyond this page window",
    )


# ---------------------------------------------------------------------------
# the read service
# ---------------------------------------------------------------------------


def _validated_paging(limit: int, offset: int) -> tuple[int, int]:
    """Fail-closed paging bounds (AGENTS.md invariant, C1)."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return limit, offset


def _candidate_of(
    entry: ServiceMapEntry,
    identity: Mapping[str, Any] | None,
) -> ServiceCandidate:
    """Merge one map entry with its knowledge identity defaults (D2)."""
    if identity is not None:
        generation = int(identity["generation"])
        service_revision = int(identity["service_revision"])
        knowledge_revision = int(identity["knowledge_revision"])
        manual_identity = bool(identity["manual_identity"])
        auto_adopt_suppressed = bool(identity["auto_adopt_suppressed"])
    else:
        generation = 1
        service_revision = entry.revision
        knowledge_revision = 0
        manual_identity = entry.origin == "manual"
        auto_adopt_suppressed = False
    return ServiceCandidate(
        application_id=uuid.UUID(entry.application_id),
        server_id=uuid.UUID(entry.server_id),
        name=entry.name,
        origin=entry.origin,
        compose_project=entry.compose_project,
        working_dir=entry.working_dir,
        unit_name=entry.unit_name,
        category=entry.category,
        installed=entry.installed,
        map_revision=entry.revision,
        generation=generation,
        service_revision=service_revision,
        knowledge_revision=knowledge_revision,
        manual_identity=manual_identity,
        auto_adopt_suppressed=auto_adopt_suppressed,
    )


def _fact_record_of(row: Mapping[str, Any]) -> FactRecord:
    """Build the frozen :class:`FactRecord` wire shape from one SQL row."""
    return FactRecord(
        fact_id=row["fact_id"],
        category=row["category"],
        key=row["key"],
        statement=row["statement"],
        revision=row["revision"],
        created_at=row["created_at"],
        source=FactSource(
            source_kind=FactSourceKind(row["source_kind"]),
            source_ref=row["source_ref"],
            verified_at=row["verified_at"],
        ),
        supersedes_fact_id=row["supersedes_fact_id"],
        status=FactStatus(row["status"]),
    )


def _selector_matches(entry: ServiceMapEntry, selector: Any) -> bool:
    """Canonical selector match per the entry origin (D2/AC1-b)."""
    if getattr(selector, "application_id", None) is not None:
        return entry.application_id == str(selector.application_id)
    if getattr(selector, "systemd_unit", None) is not None:
        return entry.origin == "systemd_unit" and (
            entry.unit_name == selector.systemd_unit
        )
    compose_project = getattr(selector, "compose_project", None)
    compose_directory = getattr(selector, "compose_directory", None)
    if compose_project is not None:
        return (
            entry.origin == "compose"
            and entry.compose_project == compose_project
            and (
                compose_directory is None
                or entry.working_dir == compose_directory
            )
        )
    return False


class KnowledgeReadStore:
    """Paged identity/knowledge/history/operation reads over the
    confined :class:`~mai.applications.service_map.ServiceMapStore` and
    the 0018 SQL tables (async session factory injected; every query is
    a bounded ``text()`` statement with deterministic ordering and a
    matching ``COUNT(*)``)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        map_store: ServiceMapStore,
    ) -> None:
        self._sessions = sessions
        self._map_store = map_store

    # -- internals ---------------------------------------------------------

    async def _entries(self, server_id: uuid.UUID) -> tuple[ServiceMapEntry, ...]:
        return await asyncio.to_thread(self._map_store.list_entries, server_id)

    async def _identity_row(
        self, server_id: uuid.UUID, application_id: uuid.UUID
    ) -> Mapping[str, Any] | None:
        query = sa.text(
            f"SELECT generation, service_revision, knowledge_revision, "
            f"manual_identity, auto_adopt_suppressed FROM {_IDENTITY_TABLE} "
            f"WHERE server_id = :sid AND application_id = :aid"
        )
        async with self._sessions() as session:
            row = (
                await session.execute(
                    query,
                    {"sid": server_id, "aid": application_id},
                )
            ).mappings().first()
        return row

    def _identity_defaults(self, entry: ServiceMapEntry | None) -> dict[str, Any]:
        return {
            "generation": 1,
            "service_revision": entry.revision if entry is not None else 0,
            "knowledge_revision": 0,
            "manual_identity": entry.origin == "manual" if entry is not None else False,
            "auto_adopt_suppressed": False,
        }

    # -- resolution (app.resolve_service foundation, D2/AC1) -----------------

    async def resolve_service(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID | None = None,
        selector: Any | None = None,
        query: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        target_server_id: uuid.UUID | None = None,
    ) -> ServiceResolution:
        """Same-server service resolution (D2/AC1): exact application id
        first, exact canonical selector second, otherwise the bounded
        deterministic candidate page. Never returns entries from another
        server."""
        limit, offset = _validated_paging(limit, offset)
        validate_same_server_scope(target_server_id, server_id)
        entries = await self._entries(server_id)
        wanted = str(server_id)
        entries = tuple(
            entry for entry in entries if entry.server_id == wanted
        )

        # (a) Exact application id.
        if application_id is not None:
            matched = [
                entry
                for entry in entries
                if entry.application_id == str(application_id)
            ]
            if matched:
                return await self._resolution_page(
                    matched,
                    exact_match=True,
                    match_kind="exact_application_id",
                    server_id=server_id,
                    offset=offset,
                    limit=limit,
                )

        # (b) Exact canonical selector (unique match only).
        if selector is not None:
            matched = [entry for entry in entries if _selector_matches(entry, selector)]
            if len(matched) == 1:
                return await self._resolution_page(
                    matched,
                    exact_match=True,
                    match_kind="exact_selector",
                    server_id=server_id,
                    offset=offset,
                    limit=limit,
                )

        # (c) Candidate search / ambiguous name page.
        needle = (query or "").strip().lower()
        matched = [
            entry
            for entry in entries
            if needle
            and (
                needle in entry.name.lower()
                or (entry.compose_project or "").lower().find(needle) >= 0
                or (entry.unit_name or "").lower().find(needle) >= 0
                or needle in entry.category.lower()
                or needle in entry.application_id.lower()
            )
        ]
        if selector is not None and not matched:
            matched = [
                entry for entry in entries if _selector_matches(entry, selector)
            ]
        matched.sort(key=lambda entry: (entry.name.lower(), entry.application_id))
        total = len(matched)
        exact_name_hit = (
            total == 1
            and bool(needle)
            and matched[0].name.lower() == needle
        )
        return await self._resolution_page(
            matched,
            exact_match=total == 1 and exact_name_hit,
            match_kind="candidates",
            ambiguous=total > 1,
            server_id=server_id,
            offset=offset,
            limit=limit,
            total=total,
        )

    async def _resolution_page(
        self,
        matched: Sequence[ServiceMapEntry],
        *,
        exact_match: bool,
        match_kind: str,
        ambiguous: bool = False,
        server_id: uuid.UUID,
        offset: int,
        limit: int,
        total: int | None = None,
    ) -> ServiceResolution:
        window = list(matched)[offset : offset + limit]
        total = total if total is not None else len(matched)
        candidates: list[ServiceCandidate] = []
        for entry in window:
            identity = await self._identity_row(
                server_id, uuid.UUID(entry.application_id)
            )
            candidates.append(_candidate_of(entry, identity))
        return ServiceResolution(
            exact_match=exact_match,
            match_kind=match_kind,
            ambiguous=ambiguous,
            candidates=candidates,
            total=total,
            offset=offset,
            limit=limit,
            has_more=total > offset + len(window),
        )

    # -- bounded knowledge read (app.get_service_knowledge, D2/D3) ----------

    async def get_service_knowledge(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        include_conflicts: bool = False,
        target_server_id: uuid.UUID | None = None,
    ) -> KnowledgeRead:
        """The bounded per-service knowledge read (D2/D3): operator map
        entry + catalog runbook separated from the ACTIVE learned-fact
        page of the CURRENT generation (conflicts/retracted/superseded
        and older generations excluded) plus the conflict summary."""
        limit, offset = _validated_paging(limit, offset)
        validate_same_server_scope(target_server_id, server_id)
        entry = await asyncio.to_thread(
            self._map_store.get_entry, server_id, application_id
        )
        identity = await self._identity_row(server_id, application_id)
        if entry is None and identity is None:
            raise ServiceKnowledgeNotFoundError(server_id, application_id)
        if identity is not None:
            generation = int(identity["generation"])
            service_revision = int(identity["service_revision"])
            knowledge_revision = int(identity["knowledge_revision"])
            manual_identity = bool(identity["manual_identity"])
            auto_adopt_suppressed = bool(identity["auto_adopt_suppressed"])
        elif entry is not None:
            defaults = self._identity_defaults(entry)
            generation = defaults["generation"]
            service_revision = defaults["service_revision"]
            knowledge_revision = defaults["knowledge_revision"]
            manual_identity = defaults["manual_identity"]
            auto_adopt_suppressed = defaults["auto_adopt_suppressed"]
        else:  # pragma: no cover - handled by the not-found raise above
            raise ServiceKnowledgeNotFoundError(server_id, application_id)

        where_sql = (
            "server_id = :sid AND application_id = :aid "
            "AND generation = :gen AND status = 'active'"
        )
        params: dict[str, Any] = {
            "sid": server_id,
            "aid": application_id,
            "gen": generation,
            "limit": limit,
            "offset": offset,
        }
        async with self._sessions() as session:
            total = int(
                (
                    await session.execute(
                        sa.text(
                            f"SELECT COUNT(*) FROM {_FACT_TABLE} WHERE {where_sql}"
                        ),
                        params,
                    )
                ).scalar_one()
            )
            rows = (
                await session.execute(
                    sa.text(
                        f"SELECT fact_id, category, key, statement, revision, "
                        f"created_at, status, source_kind, source_ref, "
                        f"verified_at, supersedes_fact_id FROM {_FACT_TABLE} "
                        f"WHERE {where_sql} "
                        f"ORDER BY {_FACT_ORDER_SQL} LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).mappings().all()
            conflict_where = (
                "server_id = :sid AND application_id = :aid "
                "AND generation = :gen AND status = 'conflict'"
            )
            conflict_count = int(
                (
                    await session.execute(
                        sa.text(
                            f"SELECT COUNT(*) FROM {_FACT_TABLE} WHERE {conflict_where}"
                        ),
                        params,
                    )
                ).scalar_one()
            )
            conflict_rows: list[Any] = []
            if include_conflicts:
                conflict_rows = (
                    await session.execute(
                        sa.text(
                            f"SELECT fact_id, category, key, statement, revision, "
                            f"created_at, status, source_kind, source_ref, "
                            f"verified_at, supersedes_fact_id FROM {_FACT_TABLE} "
                            f"WHERE {conflict_where} "
                            f"ORDER BY {_FACT_ORDER_SQL} LIMIT :limit"
                        ),
                        params,
                    )
                ).mappings().all()
        conflicts = [_fact_record_of(row) for row in conflict_rows]
        facts = FactPage(
            items=[_fact_record_of(row) for row in rows],
            limit=limit,
            offset=offset,
            total=total,
            has_more=total > offset + len(rows),
        )

        catalog_runbook: dict[str, Any] | None = None
        if entry is not None:
            for runbook in await asyncio.to_thread(
                self._map_store.list_catalog_runbooks
            ):
                if runbook.name.lower() == entry.name.lower():
                    catalog_runbook = runbook.to_json()
                    break

        read = KnowledgeRead(
            server_id=server_id,
            application_id=application_id,
            found_in_map=entry is not None,
            generation=generation,
            service_revision=service_revision,
            knowledge_revision=knowledge_revision,
            manual_identity=manual_identity,
            auto_adopt_suppressed=auto_adopt_suppressed,
            name=entry.name if entry is not None else None,
            origin=entry.origin if entry is not None else None,
            category=entry.category if entry is not None else None,
            description=entry.description if entry is not None else None,
            custom_instructions=(
                entry.custom_instructions if entry is not None else None
            ),
            instructions=(
                entry.instructions.to_json() if entry is not None else None
            ),
            installed=entry.installed if entry is not None else None,
            map_revision=entry.revision if entry is not None else None,
            catalog_runbook=catalog_runbook,
            conflict_count=conflict_count,
            conflicts=conflicts,
            facts=facts,
        )
        enforce_envelope_bound(read)
        enforce_envelope_bound(facts)
        return read

    # -- append-only fact history (C3) ---------------------------------------

    async def list_fact_history(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        fact_id: uuid.UUID | None = None,
        generation: int | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        target_server_id: uuid.UUID | None = None,
    ) -> FactRevisionPage:
        """The paginated append-only fact revision history (C3): newest
        change first, ``COUNT(*)`` under the exact same filter."""
        limit, offset = _validated_paging(limit, offset)
        validate_same_server_scope(target_server_id, server_id)
        where_sql = "r.server_id = :sid AND r.application_id = :aid"
        params: dict[str, Any] = {
            "sid": server_id,
            "aid": application_id,
            "limit": limit,
            "offset": offset,
        }
        if fact_id is not None:
            where_sql += " AND r.fact_id = :fact_id"
            params["fact_id"] = fact_id
        if generation is not None:
            where_sql += " AND f.generation = :generation"
            params["generation"] = generation
        select_sql = (
            f"SELECT r.revision_id, r.fact_id, r.server_id, r.application_id, "
            f"r.revision, r.statement, r.status, r.source_kind, r.source_ref, "
            f"r.changed_at, f.category, f.key, f.generation "
            f"FROM {_REVISION_TABLE} r JOIN {_FACT_TABLE} f "
            f"ON r.fact_id = f.fact_id WHERE {where_sql} "
            f"ORDER BY {_HISTORY_ORDER_SQL} LIMIT :limit OFFSET :offset"
        )
        count_sql = (
            f"SELECT COUNT(*) FROM {_REVISION_TABLE} r JOIN {_FACT_TABLE} f "
            f"ON r.fact_id = f.fact_id WHERE {where_sql}"
        )
        async with self._sessions() as session:
            total = int((await session.execute(sa.text(count_sql), params)).scalar_one())
            rows = (
                await session.execute(sa.text(select_sql), params)
            ).mappings().all()
        items = [
            FactRevisionRecord(
                revision_id=row["revision_id"],
                fact_id=row["fact_id"],
                server_id=row["server_id"],
                application_id=row["application_id"],
                generation=row["generation"],
                category=row["category"],
                key=row["key"],
                revision=row["revision"],
                statement=row["statement"],
                status=FactStatus(row["status"]),
                source_kind=FactSourceKind(row["source_kind"]),
                source_ref=row["source_ref"],
                changed_at=row["changed_at"],
            )
            for row in rows
        ]
        page = FactRevisionPage(
            items=items,
            limit=limit,
            offset=offset,
            total=total,
            has_more=total > offset + len(items),
        )
        enforce_envelope_bound(page)
        return page

    # -- durable operation receipts (app.get_learning_status, D5) ------------

    async def list_learning_operations(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        source_run_id: uuid.UUID | None = None,
        status: OperationStatus | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        target_server_id: uuid.UUID | None = None,
    ) -> OperationPage:
        """The paginated durable operation receipts (D5): newest
        acceptance first, ``COUNT(*)`` under the exact same filter,
        envelope-bounded."""
        limit, offset = _validated_paging(limit, offset)
        validate_same_server_scope(target_server_id, server_id)
        where_sql = "server_id = :sid"
        params: dict[str, Any] = {
            "sid": server_id,
            "limit": limit,
            "offset": offset,
        }
        if application_id is not None:
            where_sql += " AND application_id = :aid"
            params["aid"] = application_id
        if operation_id is not None:
            where_sql += " AND operation_id = :oid"
            params["oid"] = operation_id
        if source_run_id is not None:
            where_sql += " AND source_run_id = :rid"
            params["rid"] = source_run_id
        if status is not None:
            where_sql += " AND status = :status"
            params["status"] = OperationStatus(status).value
        select_sql = (
            f"SELECT operation_id, server_id, application_id, generation, "
            f"payload_digest, workflow_id, status, sub_effects, accepted_at, "
            f"updated_at, error_code, error_detail FROM {_OPERATION_TABLE} "
            f"WHERE {where_sql} ORDER BY {_OPERATION_ORDER_SQL} "
            f"LIMIT :limit OFFSET :offset"
        )
        count_sql = (
            f"SELECT COUNT(*) FROM {_OPERATION_TABLE} WHERE {where_sql}"
        )
        async with self._sessions() as session:
            total = int((await session.execute(sa.text(count_sql), params)).scalar_one())
            rows = (
                await session.execute(sa.text(select_sql), params)
            ).mappings().all()
        items = [
            OperationReceipt(
                operation_id=row["operation_id"],
                status=OperationStatus(row["status"]),
                payload_digest=row["payload_digest"],
                sub_effects={
                    key: SubEffectStatus(value)
                    for key, value in dict(row["sub_effects"] or {}).items()
                },
                accepted_at=row["accepted_at"],
                workflow_id=row["workflow_id"],
            )
            for row in rows
        ]
        page = OperationPage.build(
            items,
            total=total,
            offset=offset,
            limit=limit,
        )
        enforce_envelope_bound(page)
        return page

    async def get_learning_status(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        source_run_id: uuid.UUID | None = None,
        target_server_id: uuid.UUID | None = None,
    ) -> OperationReceipt | None:
        """The LATEST durable operation receipt matching the filters
        (``app.get_learning_status``), ``None`` when none exists."""
        page = await self.list_learning_operations(
            server_id=server_id,
            application_id=application_id,
            operation_id=operation_id,
            source_run_id=source_run_id,
            limit=1,
            offset=0,
            target_server_id=target_server_id,
        )
        return page.items[0] if page.items else None


#: Descriptive alias (both names are valid exports; the store is THE
#: read service for the knowledge feature).
ServiceKnowledgeReadService = KnowledgeReadStore
