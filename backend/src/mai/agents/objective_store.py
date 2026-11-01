"""Objective-domain store: identity, scope, status, run linkage (v2).

ONE storage seam for the Runtime v2 objective episode (runtime-v2.md §3,
§4, §8, §11; journal-store slice). Frozen-contract reuse, no second
vocabulary:

- **Authority discipline (§3.1, AC1):** the verbatim operator request and
  operator constraints are persisted exactly as projected — model text is
  never authority, never merged into these fields. Text is projected
  through :func:`mai.agents.output_safety.project_text` BEFORE the INSERT
  (C2: zero secret persistence).
- **Scope authority (§3.2, AC5):** scope edits require the exact operator
  action (:class:`ActorKind.OPERATOR`) AND the expected CAS revision; a
  child run can only bind with an initiating server already inside the
  operator-owned scope, and the ``handler`` CHECK admits only
  ``agent.run_v2`` — no in-flight legacy run becomes v2 (§10.2, durable at
  the DB level).
- **Transactional ledger consistency (AC2):** every authoritative
  transition (create, scope change, status change, completion) appends its
  ``objective.*`` event INSIDE the same transaction, under the objective
  row lock — the ledger can never contradict store state.
- **Evidence-bound completion (§8.3, AC3):** completing requires an
  :class:`ObjectiveCompletionVerdict`; a ``verified`` claim without
  evidence-bound criterion coverage is the structured
  ``completion_missing_evidence`` rejection — the honest state, never a
  silent success. Retransmitted verdicts (same ``idempotency_key``) are
  deduplicated onto the settled episode.
- **Bounded pages everywhere (AGENTS.md §1):** listings carry
  ``limit``/``offset``/``total``/``has_more`` with deterministic ordering
  and same-filter ``COUNT(*)``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.activity_store import (
    ObjectiveEventDraft,
    _OBJECTIVE_COLUMNS,
    _check_page_bounds,
    aappend_events,
    achecked_objective,
    ascope_server_rows,
)
from mai.agents.journal_store import _areplay_from_zero, _aupsert_cache
from mai.agents.output_safety import REDACTED, project_text
from mai.contracts.conversations import (
    DEFAULT_PAGE_LIMIT,
    compute_has_more,
    content_fingerprint,
)
from mai.contracts.objectives import (
    ACTOR_KINDS,
    MAX_OBJECTIVE_CONSTRAINTS,
    MAX_SCOPE_SERVERS,
    OBJECTIVE_COMPLETED_EVENT_TYPE,
    OBJECTIVE_CREATED_EVENT_TYPE,
    OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE,
    OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
    ORIGINAL_REQUEST_MAX_CHARS,
    ActorKind,
    ObjectiveBudget,
    ObjectiveCompletionVerdict,
    ObjectiveCompletedPayload,
    ObjectiveContractError,
    ObjectiveCreatedPayload,
    ObjectiveDetail,
    ObjectiveErrorCode,
    ObjectiveFieldError,
    ObjectiveListPage,
    ObjectiveRunLink,
    ObjectiveRunPage,
    ObjectiveScopeChangedPayload,
    ObjectiveScopeServer,
    ObjectiveStatus,
    ObjectiveStatusChangedPayload,
    ObjectiveSummary,
    validate_objective_completion,
    validate_scope_change,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "V2_HANDLER_NAME",
    "abind_run",
    "achange_scope",
    "achange_status",
    "acomplete_objective",
    "acreate_objective",
    "aget_objective",
    "alist_objective_runs",
    "alist_objectives",
    "ascope_server_ids",
]

#: The frozen v2 run handler identity (runtime-v2.md §2). Binding is only
#: legal for runs of this handler; the DB CHECK is the durable backstop.
V2_HANDLER_NAME: Final[str] = "agent.run_v2"

_CONSTRAINT_MAX_CHARS = 2_000

_TERMINAL_STATUSES: Final[frozenset[ObjectiveStatus]] = frozenset(
    {
        ObjectiveStatus.COMPLETED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
    }
)


def _not_found() -> ObjectiveContractError:
    return ObjectiveContractError(
        [
            ObjectiveFieldError(
                ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
                "objective_id",
                "no objective in the requesting server scope",
            )
        ]
    )


def _single(code: ObjectiveErrorCode, field: str, detail: str):
    return ObjectiveContractError(
        [ObjectiveFieldError(code.value, field, detail)]
    )


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


async def ascope_server_ids(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
) -> frozenset[uuid.UUID]:
    """The objective's explicit in-scope server identities."""
    async with sessions() as session:
        rows = await ascope_server_rows(session, objective_id)
    return frozenset(uuid.UUID(str(row["server_id"])) for row in rows)


def _row_to_scope_server(
    row: Mapping[str, object],
) -> ObjectiveScopeServer:
    return ObjectiveScopeServer(
        server_id=row["server_id"],  # type: ignore[arg-type]
        added_by_actor_kind=ActorKind(str(row["added_by_actor_kind"])),
        added_at=row["added_at"],  # type: ignore[arg-type]
    )


def _row_to_detail(
    row: Mapping[str, object],
    scope_rows: Sequence[Mapping[str, object]],
) -> ObjectiveDetail:
    constraints_raw = row["constraints"]
    return ObjectiveDetail(
        objective_id=row["objective_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        title=row["title"],  # type: ignore[arg-type]
        status=ObjectiveStatus(str(row["status"])),
        revision=int(row["revision"]),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        original_request=str(row["original_request"]),
        constraints=[str(item) for item in (constraints_raw or [])],
        budget=(
            ObjectiveBudget.model_validate(row["budget"])
            if row["budget"] is not None
            else None
        ),
        scope=[_row_to_scope_server(scope_row) for scope_row in scope_rows],
    )


# ---------------------------------------------------------------------------
# creation (AC1)
# ---------------------------------------------------------------------------


async def acreate_objective(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    original_request: str,
    constraints: list[str] | tuple[str, ...] = (),
    scope_server_ids: list[uuid.UUID] | tuple[uuid.UUID, ...],
    title: str | None = None,
    budget: ObjectiveBudget | None = None,
    actor_kind: ActorKind = ActorKind.OPERATOR,
    client_request_id: uuid.UUID | None = None,
    objective_id: uuid.UUID | None = None,
) -> ObjectiveDetail:
    """Create one objective episode: verbatim request + constraints,
    explicit initial scope, bounded budget — and the ``objective.created``
    ledger event, all in ONE transaction (§3.1; AC1).

    ``scope_server_ids`` must be non-empty and contain the initiating
    server (the scope pin is always a scope member). Creation text is
    projected BEFORE persistence; oversized content is rejected, never
    silently cut. With ``client_request_id`` the creation is idempotent:
    same key + same projected content returns the SAME objective; same key
    + changed content is the stable ``client_request_conflict``.
    """
    if actor_kind is not ActorKind.OPERATOR:
        raise _single(
            ObjectiveErrorCode.OPERATOR_REQUIRED,
            "actor_kind",
            "objectives are created by the operator",
        )
    safe_request, _request_redacted = _project_bound(
        original_request, ORIGINAL_REQUEST_MAX_CHARS, "original_request"
    )
    safe_constraints = [
        _project_bound(item, _CONSTRAINT_MAX_CHARS, "constraints")[0]
        for item in constraints
    ]
    if len(safe_constraints) > MAX_OBJECTIVE_CONSTRAINTS:
        raise _single(
            ObjectiveErrorCode.INVALID_PAYLOAD,
            "constraints",
            f"an objective carries at most {MAX_OBJECTIVE_CONSTRAINTS} "
            "operator constraints",
        )
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-blank string when set")
        safe_title: str | None = project_text(title, max_chars=240)
    else:
        safe_title = None
    scope_list = list(scope_server_ids)
    if not scope_list:
        raise _single(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "scope_server_ids",
            "the initial objective scope cannot be empty",
        )
    if len(set(scope_list)) != len(scope_list):
        raise _single(
            ObjectiveErrorCode.INVALID_SCOPE_CHANGE,
            "scope_server_ids",
            "the initial scope carries duplicate servers",
        )
    if len(scope_list) > MAX_SCOPE_SERVERS:
        raise _single(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "scope_server_ids",
            f"the objective scope is bounded at {MAX_SCOPE_SERVERS} servers",
        )
    if server_id not in set(scope_list):
        raise _single(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "scope_server_ids",
            "the initiating server must be inside the initial scope",
        )
    oid = objective_id or uuid.uuid4()
    fingerprint = content_fingerprint(
        safe_request + "\n\x00" + "\n".join(safe_constraints)
    )
    payload = ObjectiveCreatedPayload(
        server_id=server_id,
        title=safe_title,
        scope_server_ids=sorted(scope_list, key=str),
        constraint_count=len(safe_constraints),
        budget=budget,
    )
    draft = ObjectiveEventDraft(
        type=OBJECTIVE_CREATED_EVENT_TYPE,
        actor_kind=ActorKind.OPERATOR,
        data=payload.model_dump(mode="json"),
    )
    async with sessions() as session, session.begin():
        if client_request_id is not None:
            existing = (
                await session.execute(
                    text(
                        f"SELECT {_OBJECTIVE_COLUMNS} FROM objectives "
                        "WHERE server_id = :sid AND client_request_id = :crid"
                    ),
                    {"sid": server_id, "crid": client_request_id},
                )
            ).mappings().first()
            if existing is not None:
                if str(existing["content_fingerprint"]) != fingerprint:
                    raise _single(
                        ObjectiveErrorCode.CLIENT_REQUEST_CONFLICT,
                        "client_request_id",
                        "the same client request id was used with changed "
                        "objective content",
                    )
                scope_rows = await ascope_server_rows(
                    session, existing["objective_id"]  # type: ignore[arg-type]
                )
                return _row_to_detail(existing, scope_rows)
        await session.execute(
            text(
                """
                INSERT INTO objectives
                    (objective_id, server_id, client_request_id,
                     content_fingerprint, title, original_request,
                     constraints, status, revision, budget)
                VALUES
                    (:oid, :sid, :crid, :fp, :title, :request,
                     CAST(:constraints AS jsonb), 'active', 1,
                     CAST(:budget AS jsonb))
                """
            ),
            {
                "oid": oid,
                "sid": server_id,
                "crid": client_request_id,
                "fp": fingerprint,
                "title": safe_title,
                "request": safe_request,
                "constraints": _json_dump(safe_constraints),
                "budget": (
                    _json_dump(budget.model_dump(mode="exclude_none=True"))
                    if budget is not None
                    else None
                ),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO objective_servers
                    (objective_id, server_id, added_by_actor_kind)
                SELECT :oid, sid, :actor
                FROM unnest(CAST(:sids AS uuid[])) AS sid
                """
            ),
            {
                "oid": oid,
                "actor": ActorKind.OPERATOR.value,
                "sids": [str(value) for value in scope_list],
            },
        )
        await aappend_events(session, oid, [draft])
        # Seed the journal cache from the ledger itself (the cache is the
        # fold through the created event's cursor — never hand-built).
        seeded = await _areplay_from_zero(session, oid)
        await _aupsert_cache(session, oid, seeded)
    return await aget_objective(sessions, oid, server_id=server_id)


# ---------------------------------------------------------------------------
# reads (C8 fail-closed scope; paginated listings)
# ---------------------------------------------------------------------------


async def aget_objective(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> ObjectiveDetail:
    """Scope-checked objective read (§3; a foreign-scope request fails
    closed with ``objective_not_found`` — existence never leaks)."""
    async with sessions() as session:
        row, scope_rows = await achecked_objective(
            session, objective_id, server_id=server_id
        )
    return _row_to_detail(row, scope_rows)


async def alist_objectives(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    status: ObjectiveStatus | None = None,
) -> ObjectiveListPage:
    """Deterministic page of objectives whose scope contains ``server_id``
    (AGENTS.md §1: same-filter ``COUNT(*)``, ``ORDER BY updated_at DESC,
    objective_id``, derived ``has_more``)."""
    _check_page_bounds(limit, offset)
    predicate = (
        "FROM objectives o WHERE EXISTS (SELECT 1 FROM objective_servers s "
        "WHERE s.objective_id = o.objective_id AND s.server_id = :sid)"
    )
    params: dict[str, object] = {"sid": server_id, "l": limit, "o": offset}
    if status is not None:
        if not isinstance(status, ObjectiveStatus):
            raise ValueError("status filter must be an ObjectiveStatus value")
        predicate += " AND o.status = :status"
        params["status"] = status.value
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) {predicate}"), params
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT o.objective_id, o.server_id, o.title, o.status,
                           o.revision, o.created_at, o.updated_at
                    {predicate}
                    ORDER BY o.updated_at DESC, o.objective_id
                    LIMIT :l OFFSET :o
                    """
                ),
                params,
            )
        ).mappings().all()
    items = [
        ObjectiveSummary(
            objective_id=row["objective_id"],  # type: ignore[arg-type]
            server_id=row["server_id"],  # type: ignore[arg-type]
            title=row["title"],  # type: ignore[arg-type]
            status=ObjectiveStatus(str(row["status"])),
            revision=int(row["revision"]),  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
        )
        for row in rows
    ]
    return ObjectiveListPage(
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


# ---------------------------------------------------------------------------
# scope change (operator-only, revision CAS; AC5)
# ---------------------------------------------------------------------------


async def achange_scope(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    expected_revision: int,
    add_server_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] = (),
    remove_server_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] = (),
    actor_kind: ActorKind,
    run_id: uuid.UUID | None = None,
) -> ObjectiveDetail:
    """Operator-only scope edit under the expected-revision CAS (§3.2).

    ONE transaction: lock the objective row, verify the requesting server
    is in scope, run the full authority check
    (:func:`validate_scope_change` — operator actor kind, CAS, no
    add/remove overlap, home server immortal, non-empty bounded result),
    apply the set delta, bump the revision, and append
    ``objective.scope_changed`` with the effective delta. A rejected edit
    changes nothing; a non-empty effective delta without its event is
    impossible (AC2/AC5).
    """
    add_list = list(add_server_ids)
    remove_list = list(remove_server_ids)
    for value in (*add_list, *remove_list):
        if not isinstance(value, uuid.UUID):
            raise ValueError("scope servers must be UUIDs")
    async with sessions() as session, session.begin():
        row, scope_rows = await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        current_revision = int(row["revision"])  # type: ignore[arg-type]
        current_scope = {
            uuid.UUID(str(scope_row["server_id"])) for scope_row in scope_rows
        }
        effective_add, effective_remove = validate_scope_change(
            add_server_ids=add_list,
            remove_server_ids=remove_list,
            current_scope=current_scope,
            home_server_id=row["server_id"],  # type: ignore[arg-type]
            actor_kind=actor_kind,
            expected_revision=expected_revision,
            current_revision=current_revision,
        )
        new_revision = current_revision + 1
        if effective_add:
            await session.execute(
                text(
                    """
                    INSERT INTO objective_servers
                        (objective_id, server_id, added_by_actor_kind)
                    SELECT :oid, sid, :actor
                    FROM unnest(CAST(:sids AS uuid[])) AS sid
                    ON CONFLICT (objective_id, server_id) DO NOTHING
                    """
                ),
                {
                    "oid": objective_id,
                    "actor": actor_kind.value,
                    "sids": [str(value) for value in effective_add],
                },
            )
        if effective_remove:
            await session.execute(
                text(
                    "DELETE FROM objective_servers "
                    "WHERE objective_id = :oid "
                    "AND server_id = ANY(CAST(:sids AS uuid[]))"
                ),
                {
                    "oid": objective_id,
                    "sids": [str(value) for value in effective_remove],
                },
            )
        if effective_add or effective_remove:
            await session.execute(
                text(
                    "UPDATE objectives SET revision = :rev, updated_at = now() "
                    "WHERE objective_id = :oid"
                ),
                {"rev": new_revision, "oid": objective_id},
            )
            payload = ObjectiveScopeChangedPayload(
                added_server_ids=list(effective_add),
                removed_server_ids=list(effective_remove),
                revision=new_revision,
            )
            await aappend_events(
                session,
                objective_id,
                [
                    ObjectiveEventDraft(
                        type=OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE,
                        actor_kind=actor_kind,
                        data=payload.model_dump(mode="json"),
                        run_id=run_id,
                    )
                ],
            )
    return await aget_objective(sessions, objective_id, server_id=server_id)


# ---------------------------------------------------------------------------
# status transitions + evidence-bound completion (§8.3; AC3)
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: Final[dict[ObjectiveStatus, frozenset[ObjectiveStatus]]] = {
    ObjectiveStatus.ACTIVE: frozenset(
        {
            ObjectiveStatus.COMPLETED,
            ObjectiveStatus.FAILED,
            ObjectiveStatus.CANCELLED,
        }
    ),
}


def _check_transition(
    current: ObjectiveStatus, target: ObjectiveStatus
) -> None:
    if current in _TERMINAL_STATUSES:
        raise _single(
            ObjectiveErrorCode.INVALID_STATUS_TRANSITION,
            "to_status",
            f"a settled objective ({current.value}) never transitions again",
        )
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise _single(
            ObjectiveErrorCode.INVALID_STATUS_TRANSITION,
            "to_status",
            f"transition {current.value} -> {target.value} is not allowed",
        )


async def achange_status(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    expected_revision: int,
    to_status: ObjectiveStatus,
    actor_kind: ActorKind,
    run_id: uuid.UUID | None = None,
) -> ObjectiveDetail:
    """One status transition under the expected-revision CAS, appended with
    its ``objective.status_changed`` event in the same transaction.

    Only ``active -> {completed, failed, cancelled}`` exists; terminal
    states are frozen. ``completed`` must go through
    :func:`acomplete_objective` (evidence-bound); this seam admits the
    honest failure/cancellation paths.
    """
    if not isinstance(to_status, ObjectiveStatus):
        raise ValueError("to_status must be an ObjectiveStatus value")
    if to_status is ObjectiveStatus.COMPLETED:
        raise _single(
            ObjectiveErrorCode.INVALID_STATUS_TRANSITION,
            "to_status",
            "completion requires an evidence-bound verdict "
            "(use acomplete_objective)",
        )
    if actor_kind not in ACTOR_KINDS:
        raise ValueError("actor_kind must be an ActorKind value")
    async with sessions() as session, session.begin():
        row, _scope_rows = await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        current_revision = int(row["revision"])  # type: ignore[arg-type]
        if expected_revision != current_revision:
            raise _single(
                ObjectiveErrorCode.REVISION_CONFLICT,
                "expected_revision",
                "expected revision does not match the current objective "
                "revision",
            )
        current = ObjectiveStatus(str(row["status"]))
        _check_transition(current, to_status)
        new_revision = current_revision + 1
        await session.execute(
            text(
                "UPDATE objectives SET status = :status, revision = :rev, "
                "updated_at = now() WHERE objective_id = :oid"
            ),
            {"status": to_status.value, "rev": new_revision, "oid": objective_id},
        )
        payload = ObjectiveStatusChangedPayload(
            from_status=current,
            to_status=to_status,
            revision=new_revision,
        )
        await aappend_events(
            session,
            objective_id,
            [
                ObjectiveEventDraft(
                    type=OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
                    actor_kind=actor_kind,
                    data=payload.model_dump(mode="json"),
                    run_id=run_id,
                )
            ],
        )
    return await aget_objective(sessions, objective_id, server_id=server_id)


async def acomplete_objective(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    expected_revision: int,
    verdict: ObjectiveCompletionVerdict,
    actor_kind: ActorKind = ActorKind.AGENT,
    run_id: uuid.UUID | None = None,
) -> ObjectiveDetail:
    """Settle the objective as ``completed`` with an evidence-bound verdict
    (§8.3; AC3).

    The verdict can never self-grant success: :func:`validate_objective_completion`
    enforces the ``TaskCompletionVerdict`` discipline — a ``verified``
    claim needs passed criteria bound to evidence, otherwise the structured
    ``completion_missing_evidence`` rejection records the honest state.
    Retransmitting the SAME ``verdict.idempotency_key`` after a successful
    completion returns the settled episode (the key is the dedup token);
    a DIFFERENT completion attempt against a settled episode is the
    structured invalid-transition rejection — settled states never
    transition again.
    """
    if verdict.objective_id != objective_id:
        raise _single(
            ObjectiveErrorCode.INVALID_PAYLOAD,
            "verdict.objective_id",
            "the verdict names a different objective",
        )
    async with sessions() as session, session.begin():
        row, _scope_rows = await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        current = ObjectiveStatus(str(row["status"]))
        current_revision = int(row["revision"])  # type: ignore[arg-type]
        if current is ObjectiveStatus.COMPLETED:
            last_key = await _alast_completed_verdict(session, objective_id)
            if last_key == str(verdict.idempotency_key):
                pass  # retransmission: deduplicate onto the settled episode
            else:
                raise _single(
                    ObjectiveErrorCode.INVALID_STATUS_TRANSITION,
                    "to_status",
                    "a completed objective never transitions again",
                )
        else:
            if expected_revision != current_revision:
                raise _single(
                    ObjectiveErrorCode.REVISION_CONFLICT,
                    "expected_revision",
                    "expected revision does not match the current objective "
                    "revision",
                )
            _check_transition(current, ObjectiveStatus.COMPLETED)
            validate_objective_completion(verdict)
            new_revision = current_revision + 1
            await session.execute(
                text(
                    "UPDATE objectives SET status = 'completed', "
                    "revision = :rev, updated_at = now() "
                    "WHERE objective_id = :oid"
                ),
                {"rev": new_revision, "oid": objective_id},
            )
            payload = ObjectiveCompletedPayload(
                from_status=current,
                revision=new_revision,
                idempotency_key=verdict.idempotency_key,
                task_outcome=verdict.task_outcome,
                execution_status=verdict.execution_status,
                domain_outcome=verdict.domain_outcome,
                effect_certainty=verdict.effect_certainty,
                missing_evidence=False,
                evidence_count=len(verdict.evidence_refs),
                criterion_count=len(verdict.criterion_results),
                error_code=verdict.error_code,
            )
            await aappend_events(
                session,
                objective_id,
                [
                    ObjectiveEventDraft(
                        type=OBJECTIVE_COMPLETED_EVENT_TYPE,
                        actor_kind=actor_kind,
                        data=payload.model_dump(mode="json"),
                        run_id=run_id,
                    )
                ],
            )
    return await aget_objective(sessions, objective_id, server_id=server_id)


async def _alast_completed_verdict(
    session: AsyncSession, objective_id: uuid.UUID
) -> str | None:
    row = (
        await session.execute(
            text(
                "SELECT data->>'idempotency_key' FROM objective_events "
                "WHERE objective_id = :oid AND type = :etype "
                "ORDER BY seq DESC LIMIT 1"
            ),
            {
                "oid": objective_id,
                "etype": OBJECTIVE_COMPLETED_EVENT_TYPE,
            },
        )
    ).first()
    return None if row is None else (str(row[0]) if row[0] is not None else None)


# ---------------------------------------------------------------------------
# run binding (§3.3; AC1 linkage + AC4/AC5 guards)
# ---------------------------------------------------------------------------


async def abind_run(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    initiating_server_id: uuid.UUID,
    actor_kind: ActorKind,
    handler: str = V2_HANDLER_NAME,
    parent_run_id: uuid.UUID | None = None,
) -> ObjectiveRunLink:
    """Bind one durable v2 run to the objective (§3.3; AC1).

    Guards, all fail-closed in ONE transaction:

    - ``handler`` must be exactly ``agent.run_v2`` (the DB CHECK is the
      durable §10.2 backstop: an in-flight legacy run can never become
      v2 through the only binding seam);
    - the initiating server must be a member of the objective scope AT
      BIND TIME (a child cannot smuggle an out-of-scope server in);
    - a ``parent_run_id`` must itself be bound to the SAME objective
      (delegation children stay inside the episode);
    - re-binding the same run is an idempotent no-op returning the
      original link.
    """
    if handler != V2_HANDLER_NAME:
        raise _single(
            ObjectiveErrorCode.LEGACY_RUN_NOT_CONVERTIBLE,
            "handler",
            f"only the {V2_HANDLER_NAME!r} handler binds to a v2 objective; "
            "legacy runs stay legacy (frozen replay, §10)",
        )
    if actor_kind not in ACTOR_KINDS:
        raise ValueError("actor_kind must be an ActorKind value")
    async with sessions() as session, session.begin():
        row, scope_rows = await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        del row
        scope_ids = {
            uuid.UUID(str(scope_row["server_id"])) for scope_row in scope_rows
        }
        if initiating_server_id not in scope_ids:
            raise _single(
                ObjectiveErrorCode.RUN_NOT_IN_SCOPE,
                "initiating_server_id",
                "the run's initiating server is not in the objective scope; "
                "no agent may add a server — the operator expands the scope",
            )
        if parent_run_id is not None:
            parent = (
                await session.execute(
                    text(
                        "SELECT objective_id FROM objective_runs "
                        "WHERE run_id = :rid"
                    ),
                    {"rid": parent_run_id},
                )
            ).first()
            if parent is None:
                raise _single(
                    ObjectiveErrorCode.INVALID_PAYLOAD,
                    "parent_run_id",
                    "the parent run is not bound to any objective",
                )
            if parent[0] != objective_id:
                raise _single(
                    ObjectiveErrorCode.INVALID_PAYLOAD,
                    "parent_run_id",
                    "delegation children bind to the parent's objective",
                )
        await session.execute(
            text(
                """
                INSERT INTO objective_runs
                    (objective_id, run_id, parent_run_id,
                     initiating_server_id, handler, actor_kind)
                VALUES (:oid, :rid, :parent, :isid, :handler, :actor)
                ON CONFLICT (objective_id, run_id) DO NOTHING
                """
            ),
            {
                "oid": objective_id,
                "rid": run_id,
                "parent": parent_run_id,
                "isid": initiating_server_id,
                "handler": handler,
                "actor": actor_kind.value,
            },
        )
        link = (
            await session.execute(
                text(
                    """
                    SELECT objective_id, run_id, parent_run_id,
                           initiating_server_id, handler, actor_kind,
                           created_at
                    FROM objective_runs
                    WHERE objective_id = :oid AND run_id = :rid
                    """
                ),
                {"oid": objective_id, "rid": run_id},
            )
        ).mappings().one()
    return ObjectiveRunLink(
        objective_id=link["objective_id"],  # type: ignore[arg-type]
        run_id=link["run_id"],  # type: ignore[arg-type]
        parent_run_id=link["parent_run_id"],  # type: ignore[arg-type]
        initiating_server_id=link["initiating_server_id"],  # type: ignore[arg-type]
        handler=str(link["handler"]),  # type: ignore[arg-type]
        actor_kind=ActorKind(str(link["actor_kind"])),
        created_at=link["created_at"],  # type: ignore[arg-type]
    )


async def alist_objective_runs(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> ObjectiveRunPage:
    """Deterministic page of the objective's run/child bindings."""
    _check_page_bounds(limit, offset)
    async with sessions() as session:
        await achecked_objective(session, objective_id, server_id=server_id)
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM objective_runs "
                        "WHERE objective_id = :oid"
                    ),
                    {"oid": objective_id},
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    """
                    SELECT objective_id, run_id, parent_run_id,
                           initiating_server_id, handler, actor_kind,
                           created_at
                    FROM objective_runs
                    WHERE objective_id = :oid
                    ORDER BY created_at, run_id
                    LIMIT :l OFFSET :o
                    """
                ),
                {"oid": objective_id, "l": limit, "o": offset},
            )
        ).mappings().all()
    items = [
        ObjectiveRunLink(
            objective_id=row["objective_id"],  # type: ignore[arg-type]
            run_id=row["run_id"],  # type: ignore[arg-type]
            parent_run_id=row["parent_run_id"],  # type: ignore[arg-type]
            initiating_server_id=row["initiating_server_id"],  # type: ignore[arg-type]
            handler=str(row["handler"]),
            actor_kind=ActorKind(str(row["actor_kind"])),
            created_at=row["created_at"],  # type: ignore[arg-type]
        )
        for row in rows
    ]
    return ObjectiveRunPage(
        objective_id=objective_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _project_bound(value: str, bound: int, field: str) -> tuple[str, bool]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    safe = project_text(value, max_chars=bound)
    if len(safe) > bound:  # defensive; project_text already bounds
        raise ValueError(f"{field} exceeds {bound} projected chars")
    return safe, REDACTED in safe


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
