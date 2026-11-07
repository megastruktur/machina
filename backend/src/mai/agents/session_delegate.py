"""Session-first durable delegation (``session.delegate``) — bounded children.

Campaign ``machina-session-first-chat`` D05 (sfc_PLAN.md D05; sfc_RESEARCH
§4). The parent session runner exposes ONE deferring tool,
``session.delegate``; executing it starts a REAL child session run (the D04
``agent.session_run`` workflow, DBOS child edge) with NARROWED authority.

Governing rules (fail-closed before any child identity is generated):

- **Depth 1** — only a root run (no ``parent_run_id``) may delegate; the
  delegate tool is never offered on a child surface and the execution gate
  re-checks the run's ancestry. Session bounds NARROW the reviewed
  delegation-module bounds (``budgets.check_nesting``/``check_fanout`` are
  cross-checked against this module's stricter caps, never widened).
- **Fan-out ≤ 3 per parent run; ≤ 4 descendants per root** — counted from
  the durable ``session_runs`` edges (bounded recursive walk), not from
  process state.
- **Grant intersection (GRANT-INTERSECT)** — the child's effective
  capabilities are the INTERSECTION of the parent's granted set with the
  requested set: a request can never ADD anything the parent lacks; an
  empty/absent request means NO capabilities.
- **Scope is inherited, never reassigned** — the child runs on the parent's
  server (there is deliberately no server field in the tool schema; the
  args contract forbids extra keys).
- **Honest outcomes** — a refused gate persists NOTHING and answers the
  parent with a denied tool result (``delegation_denied`` /
  ``delegation_depth_exhausted`` / ``fanout_exhausted`` /
  ``budget_exhausted``); a child that does not prove success keeps its
  honest state (cancelled stays cancelled; unknown stays unknown).
- **Restart safety** — the child identity is DETERMINISTIC (uuid5 over
  session + parent run + delegate call id) and every write is idempotent
  under that key, so a crash during delegation replays onto the SAME child
  (never a second admission, never a second effect).
- **Bounded summary, isolated history** — the parent receives exactly one
  tool result carrying a redacted, bounded summary document; the child's
  native history is never spliced into the parent's.

The module is free of the deleted-at-D09 conversation/task/objective
imports and the harness.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.tools import Tool as PydanticTool
from sqlalchemy import text as sql_text

from mai.agents.output_safety import project_text, redact_text
from mai.agents.budgets import check_fanout, check_nesting
from mai.contracts.sessions import (
    MAX_ALLOWED_CAPABILITIES,
    MAX_CONTEXT_REFS,
    MAX_DELEGATION_TIMEOUT_SECONDS,
    MAX_DELEGATION_TOOL_CALLS,
    MAX_SUMMARY_TEXT_CHARS,
    MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    DelegationSpec,
    EntryKind,
    SessionContractError,
    SessionErrorCode,
    SessionFieldError,
    SessionRunState,
)
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID

__all__ = [
    "MAX_CHILDREN_PER_SESSION_RUN",
    "MAX_DESCENDANTS_PER_ROOT",
    "MAX_SESSION_DELEGATION_DEPTH",
    "SESSION_DELEGATE_CAPABILITY_ID",
    "SESSION_DELEGATE_TOOL_NAME",
    "build_deferring_delegate_tool",
    "delegate_tool_json_schema",
    "narrow_child_capabilities",
    "parse_delegate_arguments",
]

# --- session delegation bounds (narrow the reviewed module bounds) --------------

#: Depth 1: a child may never delegate again (the tool is not even offered
#: on a child surface; the execution gate re-checks the ancestry).
MAX_SESSION_DELEGATION_DEPTH: Final[int] = 1

#: At most 3 children per parent run (the reviewed module bound is 8).
MAX_CHILDREN_PER_SESSION_RUN: Final[int] = 3

#: At most 4 total descendants per root session run.
MAX_DESCENDANTS_PER_ROOT: Final[int] = 4

#: The parent-facing capability id of the delegation tool.
SESSION_DELEGATE_CAPABILITY_ID: Final[str] = "session.delegate"
SESSION_DELEGATE_TOOL_NAME: Final[str] = "session_delegate"

#: Delegation argument bounds (the D01 contract's delegation vocabulary).
_MAX_GOAL_CHARS: Final[int] = MAX_SUMMARY_TEXT_CHARS
_MAX_TIMEOUT_SECONDS: Final[float] = MAX_DELEGATION_TIMEOUT_SECONDS
_MAX_ROUNDS: Final[int] = 8

#: Deterministic child-identity namespace (restart safety).
_CHILD_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID(
    "6d696e61-2d73-6663-8d73-657373696f6e"  # "mai-sfcs-session" nominal
)

#: Bounded child-summary char budget (the parent's tool result preview).
_CHILD_SUMMARY_MAX_CHARS: Final[int] = MAX_SUMMARY_TEXT_CHARS

#: The explicit truncation suffix inside a cut child summary (wire honesty:
#: the parent can always tell the preview was shortened, and by how much).
_CHILD_SUMMARY_TRUNCATION_MARKER: Final[str] = (
    "[truncated: {omitted} chars omitted]"
)

#: Ancestor/descendant walk guard (bounded, fail-closed).
_MAX_WALK_DEPTH: Final[int] = 8


# --- tool surface ---------------------------------------------------------------


def delegate_tool_json_schema() -> dict[str, object]:
    """The bounded delegation arguments schema (all fields required, no
    extras: a scope-widening attempt such as a ``server_id`` key is
    rejected at the tool contract itself)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "goal",
            "context_refs",
            "artifact_refs",
            "allowed_capabilities",
            "max_rounds",
            "max_tool_calls",
            "timeout_seconds",
        ],
        "properties": {
            "goal": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_GOAL_CHARS,
                "description": (
                    "Bounded explicit goal for the INTERNAL subordinate child"
                    " run of the CURRENT Machina session: executed on the SAME"
                    " server with a narrowed subset of this run's"
                    " allowed_capabilities; never an external agent/profile"
                ),
            },
            "context_refs": {
                "type": "array",
                "maxItems": MAX_CONTEXT_REFS,
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "description": "Explicit bounded context references the child may read",
            },
            "artifact_refs": {
                "type": "array",
                "maxItems": MAX_CONTEXT_REFS,
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "description": "Explicit artifact references handed to the child",
            },
            "allowed_capabilities": {
                "type": "array",
                "maxItems": MAX_ALLOWED_CAPABILITIES,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "Requested capability subset (INTERSECTED with the parent's grants)",
            },
            "max_rounds": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_ROUNDS,
                "description": "Maximum model rounds of the child execution",
            },
            "max_tool_calls": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DELEGATION_TOOL_CALLS,
                "description": "Maximum tool calls of the child execution",
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_TIMEOUT_SECONDS,
                "description": "Hard wall-clock budget of the child execution in seconds",
            },
        },
    }


def build_deferring_delegate_tool() -> PydanticTool:
    """The deferring ``session_delegate`` tool for the PARENT surface.

    The tool never executes inline (``CallDeferred``): the durable runner
    step performs the delegation, exactly like the capability tools
    (C06 rule 4 — deferral is a transport mechanism, never an
    authorization boundary). A child surface NEVER receives this tool.
    The child is an INTERNAL subordinate run of the current Machina
    session (same server, narrowed capability subset) — never an
    external agent/profile invocation.
    """

    def _defer(**_payload: Any) -> Any:
        raise CallDeferred

    return PydanticTool.from_schema(
        _defer,
        name=SESSION_DELEGATE_TOOL_NAME,
        description=(
            "Delegate a bounded sub-task to an INTERNAL subordinate child"
            " run of the current Machina session: a real child execution on"
            " the SAME server with a NARROWED subset of this run's"
            " allowed_capabilities. It never contacts external agent systems"
            " or profiles (e.g. Hermes profiles) or other servers; returns a"
            " bounded child summary."
        ),
        json_schema=delegate_tool_json_schema(),
    )


# --- argument parsing (tool schema mirror; fail closed) ---------------------------


def parse_delegate_arguments(args: Mapping[str, object]) -> dict[str, object]:
    """Validate one delegate-call argument document against the bounded
    schema (strict: unknown keys — e.g. a ``server_id`` scope-widening
    attempt — are rejected)."""
    schema = delegate_tool_json_schema()
    required = set(schema["required"])  # type: ignore[arg-type]
    properties = schema["properties"]  # type: ignore[index]
    unknown = set(args) - set(properties)
    if unknown:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    sorted(unknown)[0],  # type: ignore[arg-type]
                    "unknown delegation argument (extra keys are forbidden)",
                )
            ]
        )
    missing = required - set(args)
    if missing:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    sorted(missing)[0],  # type: ignore[arg-type]
                    "required delegation argument is missing",
                )
            ]
        )
    for key in ("goal",):
        value = args[key]
        if not isinstance(value, str) or not 1 <= len(value) <= _MAX_GOAL_CHARS:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        key,
                        f"{key} must be 1..{_MAX_GOAL_CHARS} characters",
                    )
                ]
            )
    for key, cap in (
        ("context_refs", MAX_CONTEXT_REFS),
        ("artifact_refs", MAX_CONTEXT_REFS),
        ("allowed_capabilities", MAX_ALLOWED_CAPABILITIES),
    ):
        value = args[key]
        if not isinstance(value, list) or len(value) > cap:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        key,
                        f"{key} must be a list of at most {cap} strings",
                    )
                ]
            )
        for item in value:
            if not isinstance(item, str) or not 1 <= len(item) <= 512:
                raise SessionContractError(
                    [
                        SessionFieldError(
                            SessionErrorCode.FORBIDDEN_FIELD,
                            key,
                            "list items must be 1..512 character strings",
                        )
                    ]
                )
    for key, low, high in (
        ("max_rounds", 1, 8),
        ("max_tool_calls", 1, MAX_DELEGATION_TOOL_CALLS),
    ):
        value = args[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise SessionContractError(
                [
                    SessionFieldError(
                        "invalid_delegation_argument",
                        key,
                        f"{key} must be an integer",
                    )
                ]
            )
        if not low <= value <= high:
            raise SessionContractError(
                [
                    SessionFieldError(
                        "invalid_delegation_argument",
                        key,
                        f"{key} must be in [{low}, {high}]",
                    )
                ]
            )
    timeout = args["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < float(timeout) <= _MAX_TIMEOUT_SECONDS
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "timeout_seconds",
                    "timeout_seconds must be a positive number",
                )
            ]
        )
    return dict(args)


def narrow_child_capabilities(
    parent_allowed: Sequence[str],
    requested: Sequence[str],
) -> tuple[str, ...]:
    """GRANT-INTERSECT: the child's effective capabilities.

    The intersection preserves the PARENT's grant order and drops anything
    the parent does not hold; an empty/absent request yields NO
    capabilities (a child can never widen authority by asking).
    """
    requested_set = {cap for cap in requested if isinstance(cap, str) and cap}
    return tuple(
        cap
        for cap in dict.fromkeys(parent_allowed)
        if isinstance(cap, str) and cap and cap in requested_set
    )[:MAX_ALLOWED_CAPABILITIES]


# --- durable gates and child identity ----------------------------------------------


def child_run_id_for(
    session_id: uuid.UUID, parent_run_id: uuid.UUID, delegate_call_id: str
) -> uuid.UUID:
    """The DETERMINISTIC child identity (restart safety): a crash during
    delegation replays onto the SAME child, never a second admission."""
    return uuid.uuid5(
        _CHILD_ID_NAMESPACE,
        f"session-delegation:{session_id}:{parent_run_id}:{delegate_call_id}",
    )


async def arun_depth(
    sessions: Any, session_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    """The run's delegation depth (0 for a root; bounded ancestor walk)."""
    depth = 0
    current: uuid.UUID | None = run_id
    async with sessions() as session:
        while current is not None and depth <= _MAX_WALK_DEPTH:
            row = (
                await session.execute(
                    sql_text(
                        "SELECT parent_run_id FROM session_runs "
                        "WHERE run_id = :rid AND session_id = :sid"
                    ),
                    {"rid": current, "sid": session_id},
                )
            ).first()
            parent = row[0] if row else None
            if parent is None:
                break
            depth += 1
            current = uuid.UUID(str(parent))
    return depth


async def aroot_run_id(
    sessions: Any, session_id: uuid.UUID, run_id: uuid.UUID
) -> uuid.UUID:
    """The run's tree root (bounded ancestor walk; the run itself when it
    has no parent)."""
    root = run_id
    current: uuid.UUID | None = run_id
    async with sessions() as session:
        while current is not None:
            row = (
                await session.execute(
                    sql_text(
                        "SELECT parent_run_id FROM session_runs "
                        "WHERE run_id = :rid AND session_id = :sid"
                    ),
                    {"rid": current, "sid": session_id},
                )
            ).first()
            parent = row[0] if row else None
            if parent is None:
                break
            root = uuid.UUID(str(parent))
            current = root
    return root


async def arun_exists(
    sessions: Any, session_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """Whether the session already carries this run row (the deterministic
    plan replay marker; pure read)."""
    async with sessions() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT 1 FROM session_runs "
                    "WHERE run_id = :rid AND session_id = :sid"
                ),
                {"rid": run_id, "sid": session_id},
            )
        ).first()
    return row is not None


async def achild_count(
    sessions: Any, session_id: uuid.UUID, parent_run_id: uuid.UUID
) -> int:
    """Children of one parent run (durable edge count)."""
    async with sessions() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT COUNT(*) FROM session_runs "
                    "WHERE session_id = :sid AND parent_run_id = :pid"
                ),
                {"sid": session_id, "pid": parent_run_id},
            )
        ).scalar_one()
    return int(row)


async def adescendant_count(
    sessions: Any, session_id: uuid.UUID, root_run_id: uuid.UUID
) -> int:
    """Total descendants of one root run (bounded recursive walk; depth 1
    enforcement keeps this at a single level in practice)."""
    async with sessions() as session:
        row = (
            await session.execute(
                sql_text(
                    """
                    WITH RECURSIVE tree(run_id) AS (
                        SELECT run_id FROM session_runs
                        WHERE session_id = :sid AND parent_run_id = :rid
                        UNION
                        SELECT c.run_id FROM session_runs c
                        JOIN tree t ON c.parent_run_id = t.run_id
                    )
                    SELECT COUNT(*) FROM tree
                    """
                ),
                {"sid": session_id, "rid": root_run_id},
            )
        ).scalar_one()
    return int(row)


async def acreate_child_facade_row(
    sessions: Any,
    *,
    child_run_id: uuid.UUID,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    parent_run_id: uuid.UUID,
    model_profile: str,
    requested_grants: Sequence[str],
) -> None:
    """Insert the child's facade ``job_runs`` row + mandatory classification.

    The reviewed lifecycle/step machinery FK-references ``job_runs``; the
    0011 mandatory-tag invariant requires the classification in the SAME
    transaction (the real writer's discipline). The facade link names the
    PARENT run's facade row (``depth`` 1 ⇒ parent present, per the
    ``ck_job_runs_root_depth`` schema check). Idempotent on the run id.
    """
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                sql_text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, parent_run_id,
                         profile_revision, prompt_revision, model_profile,
                         requested_grants, submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, 1, :parent_run_id, 1, 1, :profile,
                         :grants, now(), now())
                    ON CONFLICT (run_id) DO NOTHING
                    """
                ),
                {
                    "run_id": child_run_id,
                    "server_id": server_id,
                    "parent_run_id": parent_run_id,
                    "profile": model_profile,
                    "grants": json.dumps(sorted(requested_grants)),
                },
            )
            await session.execute(
                sql_text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (:run_id, :tag, 'system')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"run_id": child_run_id, "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)},
            )


# --- the delegation execution plan (gate order, fail closed) ------------------------


async def aplan_session_delegation(
    sessions: Any,
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    parent_run_id: uuid.UUID,
    delegate_call_id: str,
    args: Mapping[str, object],
    parent_allowed_capabilities: Sequence[str],
    parent_mode: str,
    model_profile: str,
    parent_context_json: Mapping[str, object] | None = None,
    connection: str | None = None,
    budget_ledger: Any | None = None,
) -> dict[str, object]:
    """Gate and plan one child delegation (fails closed, persists nothing).

    Gate order (nothing persisted on any rejection): ancestry/depth gate →
    fan-out gate → root-descendant gate → budget reservation (reviewed
    ledger; an unbudgeted root keeps the session bounds as the only
    enforcement) → grant intersection → the child payload with NARROWED
    authority. The child identity is DETERMINISTIC, so a crash/replay
    lands on the SAME child.

    Returns the delegation plan document (child id, narrowed grants, child
    payload, parent delegation entry payload).
    """
    # 0. The arguments contract (schema mirror; scope-widening keys refused).
    parsed = parse_delegate_arguments(args)
    narrowed = narrow_child_capabilities(
        [
            cap
            for cap in parent_allowed_capabilities
            if isinstance(cap, str) and cap
        ],
        parsed["allowed_capabilities"],  # type: ignore[arg-type]
    )

    # The child identity is DERIVED first (pure, no persistence): the gates
    # below EXEMPT this call's own deterministic child when it already
    # exists, so a replayed plan step (crash between the child-row commit
    # and the DBOS step record — R05 F1) re-counts accurately and converges
    # on the SAME child instead of falsely denying.
    child_run_id = child_run_id_for(session_id, parent_run_id, delegate_call_id)
    replaying = await arun_exists(sessions, session_id, child_run_id)

    # 1. ancestry/depth gate (session bound: depth 1 — a child never delegates).
    depth = await arun_depth(sessions, session_id, parent_run_id)
    if depth >= MAX_SESSION_DELEGATION_DEPTH:
        raise SessionContractError(
            [
                SessionFieldError(
                    "delegation_depth_exhausted",
                    "depth",
                    "delegation depth exhausted: children cannot delegate "
                    "(session bound: depth 1)",
                )
            ]
        )
    # Reviewed cross-check (module bound 4 ≥ session bound 1; narrowed).
    child_depth = check_nesting(depth, None)
    del child_depth

    # 2. fan-out gate: this delegation adds exactly one child (none when it
    #    is this plan's own replay); the parent's durable children must
    #    stay under the session cap.
    children = await achild_count(sessions, session_id, parent_run_id)
    if replaying:
        children = max(0, children - 1)
    if children >= MAX_CHILDREN_PER_SESSION_RUN:
        raise SessionContractError(
            [
                SessionFieldError(
                    "fanout_exhausted",
                    "children",
                    "fan-out exhausted: at most "
                    f"{MAX_CHILDREN_PER_SESSION_RUN} children per parent run",
                )
            ]
        )
    check_fanout(1, None)  # reviewed cross-check (module bound 8 ≥ 3)

    # 3. descendant gate: the ROOT's whole tree stays ≤ 4 (the tree-wide
    #    bound, not a per-subtree bound — the root is derived by a bounded
    #    ancestor walk).
    root_run_id = await aroot_run_id(sessions, session_id, parent_run_id)
    descendants = await adescendant_count(sessions, session_id, root_run_id)
    if replaying:
        descendants = max(0, descendants - 1)
    if descendants >= MAX_DESCENDANTS_PER_ROOT:
        raise SessionContractError(
            [
                SessionFieldError(
                    "descendant_budget_exhausted",
                    "descendants",
                    "descendant budget exhausted: at most "
                    f"{MAX_DESCENDANTS_PER_ROOT} descendants per root run",
                )
            ]
        )

    # 4. budget reservation (reviewed ledger: an unbudgeted root is a
    #    no-op; a budgeted root reserves exactly one active slot for this
    #    child — an idempotent no-op on this plan's own replay).
    if budget_ledger is not None:
        await budget_ledger.areserve(
            str(parent_run_id), str(parent_run_id), [str(child_run_id)]
        )

    # 5. the child payload (the D04 runner executes it; scope/grants are the
    #    narrowed intersection; credentials and approvals are NOT widened).
    context_refs = list(parsed["context_refs"])  # type: ignore[arg-type]
    artifact_refs = list(parsed["artifact_refs"])  # type: ignore[arg-type]
    spec = DelegationSpec(
        session_id=session_id,
        parent_run_id=parent_run_id,
        goal=str(parsed["goal"]),
        context_refs=context_refs,
        artifact_refs=artifact_refs,
        allowed_capabilities=list(narrowed),
        max_rounds=int(parsed["max_rounds"]),  # type: ignore[arg-type]
        max_tool_calls=int(parsed["max_tool_calls"]),  # type: ignore[arg-type]
        timeout_seconds=float(parsed["timeout_seconds"]),  # type: ignore[arg-type]
    )
    child_payload = build_child_intent_document(
        session_id=session_id,
        server_id=server_id,
        child_run_id=child_run_id,
        parent_run_id=parent_run_id,
        parent_context_json=(
            dict(parent_context_json)
            if parent_context_json is not None
            else {
                "principal": "unknown",
                "mode": parent_mode,
                "profile_revision": "1",
                "prompt_revision": "1",
            }
        ),
        goal=spec.goal,
        narrowed_capabilities=list(narrowed),
        model_profile=model_profile,
        client_request_id=str(
            uuid.uuid5(
                uuid.UUID(int=0),
                f"session-delegation:{session_id}:{delegate_call_id}",
            )
        ),
        max_rounds=spec.max_rounds,
        timeout_seconds=spec.timeout_seconds,
        connection=connection,
    )
    return {
        "child_run_id": str(child_run_id),
        "narrowed_capabilities": list(narrowed),
        "spec": spec.model_dump(mode="json"),
        "child_payload": child_payload,
        "delegation_entry": {
            "kind": EntryKind.DELEGATION.value,
            "child_run_id": str(child_run_id),
            "child_session_id": None,
            "goal": spec.goal,
            "status": SessionRunState.QUEUED.value,
            "allowed_capabilities": list(narrowed),
            "result_summary": None,
            "error_code": None,
        },
        "delegation_source_id": f"delegation:{delegate_call_id}",
    }


async def aupdate_entry_payload_by_source(
    sessions: Any,
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    durable_source_id: str,
    payload: dict[str, object],
) -> bool:
    """Update one keyed session entry's payload in place (idempotent)."""
    from mai.session_store import anotify_entry_updated

    async with sessions() as session:
        async with session.begin():
            updated = (
                await session.execute(
                    sql_text(
                        """
                        UPDATE session_entries e
                        SET payload = CAST(:payload AS jsonb)
                        FROM sessions s
                        WHERE s.session_id = e.session_id
                          AND s.server_id = :server_id
                          AND e.session_id = :sid
                          AND e.durable_source_id = :src
                        RETURNING e.seq
                        """
                    ),
                    {
                        "payload": json.dumps(payload, default=str),
                        "server_id": server_id,
                        "sid": session_id,
                        "src": durable_source_id,
                    },
                )
            ).mappings().first()
            if updated is None:
                return False
            # Transactional in-place-update wakeup (S1): the NOTIFY is
            # held until COMMIT, so an at-head channel connection re-reads
            # the updated row only after it is durably committed (e.g.
            # delegation completion).
            await anotify_entry_updated(
                session, session_id=session_id, seq=int(updated["seq"])
            )
            return True


def build_child_intent_document(
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    child_run_id: uuid.UUID,
    parent_run_id: uuid.UUID,
    parent_context_json: Mapping[str, object],
    goal: str,
    narrowed_capabilities: Sequence[str],
    model_profile: str,
    client_request_id: str,
    max_rounds: int,
    timeout_seconds: float,
    connection: str | None = None,
) -> dict[str, object]:
    """Build the child ``agent.session_run`` payload with NARROWED authority.

    The child context: same server, same principal, ``parent_scope`` naming
    this parent run + server, capability set = the narrowed intersection,
    NO credential references (never widened), mode inherited.
    """
    from mai.policy.evaluate import ExecutionContext, ParentScope, PolicyMode

    parent_mode = str(parent_context_json.get("mode", "delegated"))
    principal = str(parent_context_json.get("principal", "unknown"))
    profile_revision = str(parent_context_json.get("profile_revision", "1"))
    prompt_revision = str(parent_context_json.get("prompt_revision", "1"))
    context = ExecutionContext(
        principal=principal,
        server_id=server_id,
        run_id=child_run_id,
        parent_scope=ParentScope(run_id=parent_run_id, server_id=server_id),
        profile_revision=profile_revision,
        prompt_revision=prompt_revision,
        credential_references=(),
        allowed_capabilities=tuple(narrowed_capabilities),
        mode=PolicyMode(parent_mode),
    )
    return {
        "payload_version": 1,
        "session_id": str(session_id),
        "run_id": str(child_run_id),
        "server_id": str(server_id),
        "parent_run_id": str(parent_run_id),
        "client_request_id": client_request_id,
        "source": "api",
        "prompt": goal,
        "context": context.to_json(),
        "connection": connection,
        "profile": model_profile,
        "max_rounds": max(1, min(int(max_rounds), 16)),
        # The child's wait windows are bounded by ITS OWN wall-clock budget:
        # reconciliation resolves (honestly) BEFORE the delegation deadline
        # so the parent's deadline read sees the child's settled truth, and
        # an approval park can never outlive the budget either.
        "approval_wait_seconds": min(3600.0, float(timeout_seconds)),
        "reconcile_wait_seconds": min(
            60.0, max(5.0, float(timeout_seconds) / 2.0)
        ),
        "wait_poll_seconds": 0.5,
        "document_ref": None,
        "runbook_ref": None,
    }


def bounded_child_summary_document(
    *,
    child_run_id: uuid.UUID,
    child_status: str,
    summary_text: str | None,
    error_code: str | None,
) -> dict[str, object]:
    """The bounded, redacted parent-facing summary document.

    GUARANTEE: ``json.dumps(document, ensure_ascii=False)`` NEVER exceeds
    ``MAX_TOOL_OUTPUT_PREVIEW_CHARS`` characters. The runner settles the
    delegation with ``preview[:MAX_TOOL_OUTPUT_PREVIEW_CHARS]``, so an
    oversized document would be cut mid-string and hand the parent
    unparseable JSON. Redaction runs FIRST (a cut can never split a
    secret); when the redacted summary must be shortened to fit the
    serialized budget, an explicit ``[truncated: N chars omitted]``
    suffix ends the ``summary`` field and the kept prefix shrinks until
    the escaped JSON fits (escaping — ``\\n``, ``\\"``, ``\\\\`` — can
    double a character's serialized width, so the fit is checked on the
    serialized document, never on the raw character count).
    """
    safe_summary = ""
    if summary_text:
        safe_summary = project_text(
            redact_text(summary_text), max_chars=_CHILD_SUMMARY_MAX_CHARS
        )

    def document(summary: str) -> dict[str, object]:
        return {
            "child_run_id": str(child_run_id),
            "status": child_status,
            "summary": summary,
            "error_code": error_code,
        }

    def serialized(summary: str) -> str:
        return json.dumps(document(summary), ensure_ascii=False)

    summary = safe_summary
    if len(serialized(summary)) > MAX_TOOL_OUTPUT_PREVIEW_CHARS:
        # The JSON wrapper (keys, run id, status, error code) plus escaping
        # pushes the document over the preview budget: cut the kept prefix
        # until body + explicit truncation marker serialize within budget.
        keep = len(safe_summary)
        while True:
            omitted = len(safe_summary) - keep
            marker = (
                "\n" + _CHILD_SUMMARY_TRUNCATION_MARKER.format(omitted=omitted)
            )
            overshoot = (
                len(serialized(safe_summary[:keep] + marker))
                - MAX_TOOL_OUTPUT_PREVIEW_CHARS
            )
            if overshoot <= 0:
                summary = safe_summary[:keep] + marker
                break
            # JSON string escaping is per-character, so every removed
            # character frees at least one serialized character: ``keep``
            # strictly decreases and the loop reaches a fitting document
            # (at ``keep == 0`` the fixed overhead + marker alone is far
            # below the budget).
            keep = max(0, keep - overshoot)

    return {
        "child_run_id": str(child_run_id),
        "status": child_status,
        "summary": summary,
        "error_code": error_code,
    }
