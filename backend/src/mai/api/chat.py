"""Chat HTTP API: durable agent conversations over the agent-run facade.

Ownership map (contracts.md **C06** agent/context rules, **C05**
runs/steps/events, **C08** UI/API and auth, **C01**
envelope/identity/scope; cards ``06-agent-run``, ``06-child-runs``,
``06-delegation-budgets``, ``06-context``, ``06-tool-bridge``; consumers
``06-web-chat``, ``06-web-delegation``):

- **A chat message IS a durable agent run** (acceptance 1). Submit
  composes the reviewed ``06-agent-run`` intent (or, with explicit
  children, the ``06-child-runs`` delegating-parent intent), validates it
  through the ONE producers (``parse_agent_run_intent`` /
  ``parse_delegation_intent`` — never a re-implementation), persists the
  facade row plus its intent records in ONE transaction (the shared
  submit-intent writer of ``mai.api.jobs``) and durably enqueues through
  the ONE engine adapter. HTTP never executes a model round, tool call or
  child workflow (C05: effects run in the worker).
- **Attachments are permitted-artifact context references** (acceptance
  1). An attachment names an evidence artifact ALREADY persisted by the
  ``host.logs`` capability under the controller-side artifacts root —
  ``{server_id, op_id}`` canonical UUIDs only. The reference is resolved
  by confined reads (``<root>/<server_id>/<op_id>.json`` + ``.log``,
  paths from opaque UUIDs, C02 rule 3), integrity-checked against the
  provenance sidecar's ``artifact_sha256``, and recorded value-free on
  the run's stream (``chat.submitted``: identities + sha256 handles +
  sizes — never artifact content). There is deliberately NO path field:
  an arbitrary local-file attachment reference cannot be expressed, so
  no arbitrary local-file read is reachable (acceptance 2; the negative
  controls pin the boundary). Server scoping is enforced at submit: an
  attachment bound to another server is a validation failure, never a
  silent retarget (C01 rule 7). The model-facing inclusion of the
  referenced artifacts is the reviewed context seam's evidence tuple —
  the worker-side wiring of that tuple stays with the agent-run
  producer (its recorded boundary: "the seam is ready"); this surface
  guarantees only that a recorded reference resolved, matched its
  provenance and was bound into the durable intent record.
- **Status is replayed, never guessed** — the shared facade replay of
  ``mai.api.jobs``; the API process never reads engine tables for reads.
- **Delegation children are first-class** (C06 rule 5). Submit accepts
  explicit child task specifications (the frozen ``ChildSpec``
  vocabulary); effective child grants are the INTERSECTION with the
  parent's requested capabilities (GRANT-INTERSECT), applied by the
  reviewed ``plan_children`` in the worker. The conversation detail
  lists the persisted child rows; child event streams subscribe through
  the same session-gated SSE route. Every child/reference the surface
  exposes is an identity + state + provenance handle — task text, model
  prompts and artifact bodies never enter events or views (C05 rule 4),
  and replay is cursor-exclusive (acceptance 2: safe and replayable).
- **Cancel is explicit tree cancellation** (guard rule 4): a cancelled
  conversation cancels its children unless the caller explicitly keeps
  them; the request is recorded once and only KNOWN engine outcomes are
  reported (requested ≠ verified; a timeout answers ``202`` with
  ``outcome=null``).
- **Resume semantics on the integrated foundation** are the C05 rule 5
  intent-level ones: re-submitting the identical submission attaches to
  the SAME run and re-arms a lost engine enqueue (the crash-gap heal) —
  that IS the intent resume. Deeper resume forms are owned elsewhere by
  design: parked waits (``reconciling``/``waiting_children``) resume
  through the recovery/reconciliation producers, and the approval-bound
  one-shot operation resume is the agent-run producer's
  ``dispatch_once(approval_id=…)` bridge seam (worker-side; no HTTP may
  dispatch effects). This surface deliberately ships no route that
  would pretend otherwise.
- **Failure mapping** is the C01 envelope everywhere; an unusable
  artifacts root or engine fails closed with ``503 unavailable``.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Final

from dbos import error as dbos_error
from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_202_ACCEPTED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.agents import delegate as agent_delegate
from mai.agents import run as agent_run
from mai.agents.budgets import (
    BUDGET_PAYLOAD_VERSION,
    MAX_ACTIVE_RUNS,
    MAX_TOTAL_STEPS,
    MAX_TOTAL_TOKENS,
    BudgetError,
    check_fanout,
    parse_root_budget,
)
from mai.agents.context import MAX_EVIDENCE_ITEMS, MAX_ROLE_CHARS
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.api.jobs import (
    CANCEL_OUTCOME_BUDGET_SECONDS,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    CancelView,
    RunView,
    _RUN_COLUMNS,
    _canonical_profile,
    _conflict,
    _error,
    _event_stream,
    _jobs_services,
    _load_run_row,
    _not_found_run,
    _record_submit_intent,
    _replayed_state,
    _run_view,
    _sessions,
    _validation_error,
    _wire_ts,
)
from mai.api.servers import resolve_profiles_root
from mai.applications.service_map import (
    INSTRUCTION_KEYS,
    MAX_MATCHED_ENTRIES,
    ServiceMapEntry,
    ServiceMapStore,
)
from mai.auth.sessions import hash_token
from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    JobEvent,
    RunState,
    parse_job_event,
)
from mai.contracts.server import parse_server_profile_text
from mai.jobs.engine import EngineError, RunIdInUseError, validate_run_id
from mai.jobs.lifecycle import (
    RunLifecycleError,
    current_state as _replay_current,
)
from mai.jobs.recovery import (
    CANCEL_REQUESTED_EVENT_TYPE,
    RecoveryError,
    RecoveryErrorCode,
)
from mai.policy.evaluate import ExecutionContext, PolicyError, PolicyMode

__all__ = ["register_chat", "resolve_artifacts_root"]

# ---------------------------------------------------------------------------
# Transport/facade bounds (module-owned policy, all explicitly bounded)
# ---------------------------------------------------------------------------

#: The event type this surface owns: the chat submission record (C05
#: rule 4 slug grammar; open type vocabulary, frozen envelope shape).
#: Carries the WHOLE-submission digest (message + selection + children +
#: attachment references) so an idempotent re-submit compares everything
#: the administrator asked for, plus the value-free attachment handles.
CHAT_SUBMITTED_EVENT_TYPE: Final[str] = "chat.submitted"
CHAT_STEERED_EVENT_TYPE: Final[str] = "chat.steered"

#: The General Consult sentinel server id (``uuid.UUID(int=0)``): the
#: conversation has NO target host (no server selected in the UI). Chat
#: submits bound to it skip the canonical-profile binding (there is no
#: host to bind) and pin revision 1; host-scoped attachments are
#: rejected (General Consult names no target).
GLOBAL_CONSULT_SERVER_ID: Final[str] = "00000000-0000-0000-0000-000000000000"

#: The default conversation role (single-administrator product; C06
#: rule 2's role line). Overridable per submit within the frozen bounds.
DEFAULT_ROLE: Final[str] = "administrator"

#: One immutable ``@service`` chip: a Service Map lookup key (matched
#: case-insensitively against application_id/name/unit_name/
#: compose_project). Bounded like every other transport field.
ServiceTag = Annotated[str, Field(min_length=1, max_length=128)]

#: Attachment bounds. Per-artifact and aggregate read budgets mirror the
#: logs capability's per-stream output budget; the count cap is the
#: reviewed context seam's evidence-item cap (the references become that
#: seam's inputs).
MAX_ATTACHMENTS: Final[int] = MAX_EVIDENCE_ITEMS
MAX_ATTACHMENT_BYTES: Final[int] = 8 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES: Final[int] = 8 * 1024 * 1024

#: Explicit artifacts-root composition (C02 rule 1): an explicit env
#: value wins; otherwise the artifacts store lives under the explicit
#: data root. The ``host.logs`` executor receives ITS root from its own
#: host composition; both must point at the same store (recorded seam).
ARTIFACTS_ROOT_VAR: Final[str] = "MAI_ARTIFACTS_ROOT"
ARTIFACTS_DEFAULT_DIRNAME: Final[str] = "artifacts"


def resolve_artifacts_root(env: Mapping[str, str] | None = None) -> str:
    """The controller-side evidence artifacts root for this process.

    ``MAI_ARTIFACTS_ROOT`` when configured, else ``<data root>/artifacts``
    (the data root follows the C02 rule 1 convention: ``MAI_DATA_ROOT``,
    else ``~/.mai``; ``~`` expands exactly at this boundary).
    """
    source = os.environ if env is None else env
    explicit = source.get(ARTIFACTS_ROOT_VAR)
    if explicit:
        return os.path.expanduser(explicit)
    return str(Path(resolve_profiles_root(source)) / ARTIFACTS_DEFAULT_DIRNAME)


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the facade/producers)
# ---------------------------------------------------------------------------


class AttachmentBody(BaseModel):
    """One permitted-artifact context reference (identity only)."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    op_id: str


class ChildBody(BaseModel):
    """One explicitly scoped child task (the frozen ``ChildSpec`` words).

    There is deliberately no server/target field: a child runs on its
    parent's server (C06 rule 5). ``capabilities`` is the child's
    REQUEST; the effective grants are the intersection with the parent's
    requested set, applied by the reviewed ``plan_children``.
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=agent_run.MAX_TASK_CHARS)
    role: str | None = Field(default=None, max_length=MAX_ROLE_CHARS)
    connection: str | None = Field(default=None, max_length=200)
    profile: str | None = Field(default=None, max_length=200)
    capabilities: list[str] = Field(default_factory=list)


class RootBudgetBody(BaseModel):
    """The frozen aggregate delegation-budget document (C05 rule 7).

    The strict ``job_runs.root_budget`` vocabulary (the ONE producer is
    :func:`mai.agents.budgets.parse_root_budget`; these bounds mirror
    it for a value-free 422): every cap NARROWS a module bound —
    ``max_depth``/``max_children`` the delegation module's nesting and
    fan-out bounds, ``max_active``/``max_steps``/``max_total_tokens``
    the ledger's tree-aggregate bounds. Omitted (the default ``None``)
    is the explicit unbudgeted root: the module bounds alone apply and
    no ``agent.budget_*`` events are written (backward compatible).
    """

    model_config = ConfigDict(extra="forbid")

    v: int = Field(default=BUDGET_PAYLOAD_VERSION)
    max_depth: int = Field(ge=1, le=agent_delegate.MAX_DELEGATION_DEPTH)
    max_children: int = Field(ge=1, le=agent_delegate.MAX_CHILDREN_PER_RUN)
    max_active: int = Field(ge=1, le=MAX_ACTIVE_RUNS)
    max_steps: int = Field(ge=1, le=MAX_TOTAL_STEPS)
    max_total_tokens: int = Field(ge=1, le=MAX_TOTAL_TOKENS)


class ChatSubmitBody(BaseModel):
    """Submit body (strict: unknown fields are rejected, value-free 422).

    ``run_id`` is REQUIRED: the client-generated idempotency key (C05
    rule 5). ``profile`` names the model profile selection explicitly
    (C06 rule 1); ``connection`` stays optional (the model store's
    stored pairing when omitted). ``capabilities`` is the conversation's
    explicit parent grant request — omitted means NO capability, never a
    default grant; delegation children are clamped to its intersection.
    ``root_budget`` is the optional aggregate delegation budget (C05
    rule 7): frozen on the run row, narrowed against the delegation
    gates, and enforced by the root ledger across the whole tree.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    server_id: str = GLOBAL_CONSULT_SERVER_ID
    message: str = Field(min_length=1, max_length=agent_run.MAX_TASK_CHARS)
    profile: str = Field(min_length=1, max_length=200)
    connection: str | None = Field(default=None, max_length=200)
    role: str = Field(default=DEFAULT_ROLE, min_length=1, max_length=MAX_ROLE_CHARS)
    rounds: int | None = Field(default=None, ge=1, le=agent_run.MAX_MAX_ROUNDS)
    mode: PolicyMode = PolicyMode.ASK
    credential_references: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    children: list[ChildBody] = Field(
        default_factory=list, max_length=agent_delegate.MAX_CHILDREN_PER_RUN
    )
    root_budget: RootBudgetBody | None = None
    attachments: list[AttachmentBody] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )
    service_tags: list[ServiceTag] = Field(default_factory=list, max_length=16)
    session_run_id: str | None = Field(default=None)


class ChatCancelBody(BaseModel):
    """Cancel body: explicit tree cancellation (guard rule 4).

    A conversation cancel defaults to cancelling its children — a
    cancelled chat must never orphan delegated work. (Deliberately a
    distinct wire type from the jobs surface's ``CancelBody``: the
    defaults differ and the exported component names must not collide.)
    """

    model_config = ConfigDict(extra="forbid")

    cancel_children: bool = True


class AttachmentView(BaseModel):
    """One recorded attachment reference (identity + integrity handles)."""

    server_id: str
    op_id: str
    sha256: str
    bytes: int


class ConversationView(RunView):
    """One conversation: the run view plus its recorded references.

    ``service_tags`` are the immutable ``@service`` chips recorded at
    submission (read back from the run's ``chat.submitted`` record).
    """

    attachments: list[AttachmentView] = Field(default_factory=list)
    children: list[RunView] = Field(default_factory=list)
    service_tags: list[str] = Field(default_factory=list)
    session_run_id: str | None = Field(default=None)


class ChatCreated(BaseModel):
    """Submit outcome: the conversation view plus the dedup verdict."""

    conversation: ConversationView
    deduplicated: bool


class ChatPage(BaseModel):
    """One bounded, explicitly server-scoped list page (C01 rule 5/7)."""

    conversations: list[ConversationView]
    limit: int
    offset: int
    total: int


# ---------------------------------------------------------------------------
# Attachment resolution (the confined read; acceptance 1/2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedAttachment:
    """One artifact reference validated against its provenance sidecar."""

    server_id: str
    op_id: str
    sha256: str
    total_bytes: int


class AttachmentRejected(ValueError):
    """One value-free attachment-reference validation failure (422)."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.detail = detail


def _canonical_attachment_uuid(value: str, field: str, index: int) -> uuid.UUID:
    label = f"attachments[{index}].{field}"
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise AttachmentRejected(label, "must be a canonical UUID string") from None
    if str(parsed) != value:
        raise AttachmentRejected(label, "must be a canonical UUID string")
    return parsed


def _resolve_attachments(
    env: Mapping[str, str] | None,
    target_server: uuid.UUID,
    attachments: list[AttachmentBody],
) -> list[ResolvedAttachment]:
    """Resolve every attachment reference by confined, integrity-checked reads.

    The ONLY filesystem vocabulary is ``<root>/<server_id>/<op_id>.log``
    (+ ``.json`` sidecar) with both components validated canonical UUIDs —
    the layout ``write_evidence_artifact`` freezes. No caller-supplied
    path component ever joins the root, so no arbitrary local-file read
    is expressible (acceptance 2; the negative controls pin the boundary).
    Failures classify honestly: unusable root ``503``, unknown artifact
    ``404``, drifted artifact/provenance ``409``, scope/shape ``422``.
    """
    root = resolve_artifacts_root(env)
    if (
        not root.startswith("/")
        or not root.isprintable()
        or "\x00" in root
        or len(root) > 4096
    ):
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "chat artifact store is unavailable",
        )
    base_root = Path(root)
    resolved: list[ResolvedAttachment] = []
    total_bytes = 0
    for index, attachment in enumerate(attachments):
        server_id = _canonical_attachment_uuid(
            attachment.server_id, "server_id", index
        )
        op_id = _canonical_attachment_uuid(attachment.op_id, "op_id", index)
        if server_id != target_server:
            raise AttachmentRejected(
                f"attachments[{index}].server_id",
                "is bound to another server; context references are "
                "single-server scoped",
            )
        directory = base_root / str(server_id)
        try:
            sidecar_raw = (directory / f"{op_id}.json").read_bytes()
            body = (directory / f"{op_id}.log").read_bytes()
        except OSError:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no artifact stored for this reference",
            ) from None
        if len(body) > MAX_ATTACHMENT_BYTES:
            raise AttachmentRejected(
                f"attachments[{index}]",
                "exceeds the bounded artifact size",
            )
        total_bytes += len(body)
        if total_bytes > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise AttachmentRejected(
                "attachments", "exceed the bounded total artifact size"
            )
        try:
            sidecar = json.loads(sidecar_raw)
        except ValueError:
            raise BoundaryError(
                HTTP_409_CONFLICT,
                "conflict",
                "stored artifact provenance requires recovery",
            ) from None
        digest = (
            sidecar.get("artifact_sha256") if isinstance(sidecar, Mapping) else None
        )
        if not isinstance(digest, str) or not digest:
            raise BoundaryError(
                HTTP_409_CONFLICT,
                "conflict",
                "stored artifact provenance requires recovery",
            )
        if hashlib.sha256(body).hexdigest() != digest:
            raise BoundaryError(
                HTTP_409_CONFLICT,
                "conflict",
                "stored artifact bytes do not match their provenance record",
            )
        resolved.append(
            ResolvedAttachment(
                server_id=str(server_id),
                op_id=str(op_id),
                sha256=digest,
                total_bytes=len(body),
            )
        )
    return resolved


# ---------------------------------------------------------------------------
# Submission document + durable records
# ---------------------------------------------------------------------------


def _canonical(document: object) -> str:
    return agent_run.canonical_json(document)


def _dedup_preserving_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _submission_document(body: ChatSubmitBody) -> dict[str, object]:
    """The canonical whole-submission document (the dedup digest input).

    ``service_tags`` participates in the digest ONLY when non-empty, so
    submissions without tags produce the exact same digest they always
    did (backward compatible; the tags are immutable per run).
    """
    document: dict[str, object] = {
        "run_id": body.run_id,
        "server_id": body.server_id,
        "message": body.message,
        "role": body.role,
        "connection": body.connection,
        "profile": body.profile,
        "rounds": body.rounds,
        "mode": body.mode.value,
        "credential_references": list(body.credential_references),
        "capabilities": list(body.capabilities),
        "children": [
            {
                "task": child.task,
                "role": child.role,
                "connection": child.connection,
                "profile": child.profile,
                "capabilities": list(child.capabilities),
            }
            for child in body.children
        ],
        "root_budget": (
            body.root_budget.model_dump() if body.root_budget is not None else None
        ),
        "attachments": [
            {"server_id": item.server_id, "op_id": item.op_id}
            for item in body.attachments
        ],
    }
    if body.service_tags:
        document["service_tags"] = list(body.service_tags)
    if body.session_run_id is not None:
        document["session_run_id"] = body.session_run_id
    return document


def _service_details_appendix(entries: list[ServiceMapEntry]) -> str:
    """The structured Service Map context appended to the agent task.

    Each matched entry contributes its identity, category, description
    and operational runbook (install/update/uninstall/backup/verify) —
    the agent's grounded service knowledge for the tagged conversation.
    """
    lines = ["", "## Service map context (@service tags)", ""]
    for entry in entries:
        lines.append(
            f"### {entry.name} — category `{entry.category}`, "
            f"origin `{entry.origin}` (id {entry.application_id})"
        )
        if entry.description:
            lines.append(entry.description)
        for key in INSTRUCTION_KEYS:
            value = getattr(entry.instructions, key)
            if value:
                lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines)


def _agent_document(
    body: ChatSubmitBody,
    run_id: str,
    server_id: str,
    context: ExecutionContext,
    service_details: list[ServiceMapEntry] | None = None,
) -> dict[str, object]:
    """The frozen agent-run intent document (the ONE producer's shape).

    ``service_details`` (the resolved ``@service`` tags) enriches the
    task text with the tagged services' map metadata + runbook. The
    appendix is bounded to the producer's task limit: entries drop from
    the tail until the enriched task fits (the tags stay recorded on the
    submission either way).
    """
    task = body.message
    if service_details:
        entries = list(service_details[:MAX_MATCHED_ENTRIES])
        while entries:
            enriched = task + _service_details_appendix(entries)
            if len(enriched) <= agent_run.MAX_TASK_CHARS:
                task = enriched
                break
            entries.pop()
    document: dict[str, object] = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": server_id,
        "context": context.to_json(),
        "role": body.role,
        "task": task,
        "connection": body.connection,
        "profile": body.profile,
    }
    if body.rounds is not None:
        document["rounds"] = body.rounds
    return document


async def _record_chat_submission(
    session: AsyncSession,
    *,
    run_id: str,
    digest: str,
    handler: str,
    attachments: list[ResolvedAttachment],
    child_count: int,
    service_tags: list[str],
    now: datetime,
    session_run_id: str | None = None,
) -> None:
    """Append the ``chat.submitted`` submission record (one transaction).

    Commits with the facade row and its run-level intent events (the
    shared writer appended those first), so the submission record exists
    exactly when the durable intent does. Seq follows the documented
    whole-stream discipline (``MAX(seq)+1``; the storage
    ``UNIQUE (run_id, seq)`` is the backstop). ``service_tags`` (the
    immutable ``@service`` chips) are recorded on the event; readers
    tolerate an absent key (submissions predating the field).
    """
    seq = (
        await session.execute(
            text("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE run_id = :r"),
            {"r": uuid.UUID(run_id)},
        )
    ).scalar_one()
    data: dict[str, object] = {
        "submission_sha256": digest,
        "handler": handler,
        "children": child_count,
        "attachments": [
            {
                "server_id": item.server_id,
                "op_id": item.op_id,
                "sha256": item.sha256,
                "bytes": item.total_bytes,
            }
            for item in attachments
        ],
        "service_tags": list(service_tags),
    }
    if session_run_id is not None:
        data["session_run_id"] = session_run_id
    await session.execute(
        text(
            """
            INSERT INTO job_events (event_id, run_id, seq, ts, type, v, data)
            VALUES (:event_id, :run_id, :seq, :ts, :type, :v, :data)
            """
        ),
        {
            "event_id": uuid.uuid4(),
            "run_id": uuid.UUID(run_id),
            "seq": seq,
            "ts": now,
            "type": CHAT_SUBMITTED_EVENT_TYPE,
            "v": EVENT_PAYLOAD_VERSION,
            "data": json.dumps(data),
        },
    )


async def _record_chat_steered(
    session: AsyncSession,
    *,
    run_id: str,
    steer_id: str,
    digest: str,
    message_chars: int,
    now: datetime,
) -> None:
    """Append a value-free ``chat.steered`` event when an active run is steered."""
    seq = (
        await session.execute(
            text("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE run_id = :r"),
            {"r": uuid.UUID(run_id)},
        )
    ).scalar_one()
    data = {
        "steer_id": steer_id,
        "submission_sha256": digest,
        "message_chars": message_chars,
    }
    await session.execute(
        text(
            """
            INSERT INTO job_events (event_id, run_id, seq, ts, type, v, data)
            VALUES (:event_id, :run_id, :seq, :ts, :type, :v, :data)
            """
        ),
        {
            "event_id": uuid.uuid4(),
            "run_id": uuid.UUID(run_id),
            "seq": seq,
            "ts": now,
            "type": CHAT_STEERED_EVENT_TYPE,
            "v": EVENT_PAYLOAD_VERSION,
            "data": json.dumps(data),
        },
    )


# ---------------------------------------------------------------------------
# View assembly
# ---------------------------------------------------------------------------


async def _child_states(
    factory: async_sessionmaker[AsyncSession],
    children_rows: list[Mapping[str, object]],
) -> dict[str, str | None]:
    """Replay the child rows' states from their own transition events."""
    if not children_rows:
        return {}
    async with factory() as session:
        event_rows = (
            (
                await session.execute(
                    text(
                        "SELECT run_id::text AS run_id, seq, ts, type, v, data "
                        "FROM job_events WHERE run_id = ANY(:ids) "
                        "AND type = 'run.transition' ORDER BY run_id, seq"
                    ),
                    {"ids": [row["run_id"] for row in children_rows]},
                )
            )
            .mappings()
            .all()
        )
    per_run: dict[str, list[JobEvent]] = {
        str(row["run_id"]): [] for row in children_rows
    }
    for event_row in event_rows:
        per_run[str(event_row["run_id"])].append(
            parse_job_event(
                {
                    "seq": event_row["seq"],
                    "run_id": str(event_row["run_id"]),
                    "ts": event_row["ts"].isoformat(),
                    "type": event_row["type"],
                    "v": event_row["v"],
                    "data": event_row["data"],
                }
            )
        )
    states: dict[str, str | None] = {}
    for run_id, events in per_run.items():
        current = _replay_current(sorted(events, key=lambda e: e.seq))
        states[run_id] = None if current is None else current.value
    return states


async def _conversation_view(
    factory: async_sessionmaker[AsyncSession],
    row: Mapping[str, object],
    state: str | None,
) -> ConversationView:
    """One conversation view: run fields + recorded references + children.

    Children are the PERSISTED child rows of exactly this run
    (``parent_run_id`` linkage); their states replay from their own
    transition events. Attachment references come from this run's own
    ``chat.submitted`` record — identities and integrity handles only.
    """
    identity = str(row["run_id"])
    async with factory() as session:
        children_rows = list(
            (
                await session.execute(
                    text(
                        f"SELECT {_RUN_COLUMNS} FROM job_runs "
                        "WHERE parent_run_id = :p ORDER BY submitted_at, run_id"
                    ),
                    {"p": uuid.UUID(identity)},
                )
            )
            .mappings()
            .all()
        )
        submission_event = (
            (
                await session.execute(
                    text(
                        "SELECT data FROM job_events WHERE run_id = :r "
                        f"AND type = '{CHAT_SUBMITTED_EVENT_TYPE}' ORDER BY seq "
                        "DESC LIMIT 1"
                    ),
                    {"r": uuid.UUID(identity)},
                )
            )
            .mappings()
            .first()
        )
    child_states = await _child_states(factory, children_rows)
    attachments: list[AttachmentView] = []
    service_tags: list[str] = []
    session_run_id: str | None = None
    if submission_event is not None:
        data = submission_event["data"]
        if isinstance(data, Mapping):
            for item in data.get("attachments") or []:
                if isinstance(item, Mapping):
                    attachments.append(
                        AttachmentView(
                            server_id=str(item["server_id"]),
                            op_id=str(item["op_id"]),
                            sha256=str(item["sha256"]),
                            bytes=int(item["bytes"]),  # type: ignore[arg-type]
                        )
                    )
            raw_tags = data.get("service_tags") or []
            if isinstance(raw_tags, list):
                service_tags = [str(tag) for tag in raw_tags if isinstance(tag, str)]
            raw_session_run = data.get("session_run_id")
            if isinstance(raw_session_run, str) and raw_session_run:
                session_run_id = raw_session_run
    return ConversationView(
        **_run_view(row, state).model_dump(),
        attachments=attachments,
        children=[
            _run_view(child, child_states.get(str(child["run_id"])))
            for child in children_rows
        ],
        service_tags=service_tags,
        session_run_id=session_run_id,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post(
    "/messages",
    status_code=HTTP_201_CREATED,
    response_model=ChatCreated,
    summary="Submit a chat message as a durable agent conversation (C05 rule 5)",
    responses={
        HTTP_200_OK: {
            "model": ChatCreated,
            "description": "Duplicate submission attached to the existing run "
            "(the intent-level resume: a lost enqueue re-arms)",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token missing/mismatched",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile for the target server, or no "
            "artifact stored for an attachment reference",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different submission, or stored "
            "artifact state requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Chat domain, artifact store or engine unavailable",
        },
    },
)
async def submit_message(request: Request, payload: ChatSubmitBody) -> Response:
    """Persist one chat submission and durably enqueue the conversation.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The target profile's revisions are bound from the canonical store;
    attachment references are resolved by confined, integrity-checked
    reads; the composed intent is validated by the ONE producers; the
    facade row, the intent digest events and the submit edge commit
    atomically; the engine enqueue follows the commit so a crash leaves
    a re-armable gap, not a half intent.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)

    try:
        run_id = validate_run_id(payload.run_id)
    except EngineError:
        return _validation_error(request, "run_id", "must be a canonical UUID string")
    raw_server_id = payload.server_id.strip()
    try:
        server_uuid = uuid.UUID(raw_server_id or GLOBAL_CONSULT_SERVER_ID)
    except ValueError:
        return _validation_error(
            request, "server_id", "must be a canonical UUID string"
        )
    server_id = str(server_uuid)
    general_consult = server_uuid == uuid.UUID(int=0)
    if general_consult:
        # General Consult has no target host: no canonical-profile
        # binding (revisions pin to 1) and no host-scoped attachments.
        if payload.attachments:
            return _validation_error(
                request,
                "attachments",
                "general consult names no target host; host-scoped "
                "attachments are rejected",
            )
        profile_revision = 1
        prompt_revision = 1
    else:
        stored = _canonical_profile(request, server_uuid)
        profile = parse_server_profile_text(stored.text)
        profile_revision = stored.revision
        prompt_revision = profile.prompt_revision

    try:
        resolved_attachments = _resolve_attachments(
            request.app.state.env, server_uuid, payload.attachments
        )
    except AttachmentRejected as exc:
        return _validation_error(request, exc.field, exc.detail)

    capabilities = _dedup_preserving_order(payload.capabilities)
    if general_consult:
        # General Consult (no target host): expose the internal-API surface
        # automatically so the chat agent can operate the system without a
        # pinned server. The UI's effectiveCaps union is preserved — the
        # agent sees the full catalog, not just the host-scoped subset.
        capabilities = _dedup_preserving_order(
            list(capabilities) + list(INTERNAL_API_CAPABILITY_IDS)
        )
    root_budget: dict[str, object] | None = None
    if payload.root_budget is not None:
        budget_document = payload.root_budget.model_dump()
        try:
            budget = parse_root_budget(budget_document)
        except BudgetError as exc:
            return _validation_error(request, "root_budget", exc.detail)
        if payload.children:
            # The whole-batch gate, early: a delegation that could never
            # fit the budget's fan-out is a 422, not a failed run.
            try:
                check_fanout(len(payload.children), budget)
            except BudgetError as exc:
                return _validation_error(request, "root_budget", exc.detail)
        root_budget = budget_document
    try:
        context = ExecutionContext(
            principal=hash_token(token),
            server_id=server_uuid,
            run_id=uuid.UUID(run_id),
            parent_scope=None,
            profile_revision=str(profile_revision),
            prompt_revision=str(prompt_revision),
            credential_references=tuple(payload.credential_references),
            allowed_capabilities=capabilities,
            mode=payload.mode,
        )
    except PolicyError as exc:
        return _validation_error(request, "context", exc.detail)

    # The @service chips: resolve tagged services against the server's
    # Service Map (when there IS a target host) and enrich the agent
    # task with their map metadata + runbook instructions. An
    # unavailable map store degrades to no enrichment — the submission
    # itself is never blocked by the catalogue.
    service_details: list[ServiceMapEntry] = []
    if payload.service_tags and not general_consult:
        map_store = getattr(request.app.state, "service_map", None)
        if isinstance(map_store, ServiceMapStore):
            service_details = list(
                map_store.find_entries(server_uuid, payload.service_tags)
            )

    agent_document = _agent_document(
        body=payload,
        run_id=run_id,
        server_id=server_id,
        context=context,
        service_details=service_details,
    )
    if payload.children:
        handler = agent_delegate.HANDLER_NAME
        engine_document: dict[str, object] = {
            "v": agent_delegate.DELEGATION_PAYLOAD_VERSION,
            "run": agent_document,
            "children": [
                {
                    "task": child.task,
                    **({"role": child.role} if child.role is not None else {}),
                    **(
                        {"connection": child.connection}
                        if child.connection is not None
                        else {}
                    ),
                    **(
                        {"profile": child.profile}
                        if child.profile is not None
                        else {}
                    ),
                    "capabilities": list(child.capabilities),
                }
                for child in payload.children
            ],
        }
        try:
            agent_delegate.parse_delegation_intent(_canonical(engine_document))
        except agent_delegate.DelegationError as exc:
            return _validation_error(request, "children", exc.detail)
    else:
        handler = agent_run.HANDLER_NAME
        engine_document = agent_document
        try:
            agent_run.parse_agent_run_intent(_canonical(engine_document))
        except agent_run.AgentRunError as exc:
            return _validation_error(request, "message", exc.detail)

    payload_json = _canonical(engine_document)
    engine_digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
    submission_digest = hashlib.sha256(
        _canonical(_submission_document(payload)).encode("ascii")
    ).hexdigest()

    session_run_id: str | None = None
    if payload.session_run_id is not None:
        try:
            session_run_id = validate_run_id(payload.session_run_id)
        except EngineError:
            return _validation_error(
                request, "session_run_id", "must be a canonical UUID string"
            )

    factory = _sessions(request)
    now = datetime.now(timezone.utc)
    if session_run_id is not None and session_run_id != run_id:
        async with factory() as session:
            existing_session_row = await _load_run_row(session, session_run_id)
        if existing_session_row is not None:
            existing_state = await _replayed_state(lifecycle, session_run_id)
            if existing_state is not None and existing_state not in (
                RunState.SUCCEEDED.value,
                RunState.FAILED.value,
                RunState.CANCELLED.value,
            ):
                steered_ok = False
                try:
                    await asyncio.to_thread(
                        recovery.client.send_message,
                        session_run_id,
                        {"steer_id": run_id, "message": payload.message},
                        topic=agent_run.STEER_TOPIC,
                        idempotency_key=run_id,
                    )
                    steered_ok = True
                except Exception:
                    steered_ok = False
                if steered_ok:
                    async with factory() as session:
                        async with session.begin():
                            await _record_chat_steered(
                                session,
                                run_id=session_run_id,
                                steer_id=run_id,
                                digest=submission_digest,
                                message_chars=len(payload.message),
                                now=now,
                            )
                    view = await _conversation_view(
                        factory, existing_session_row, existing_state
                    )
                    steered_view = ChatCreated(
                        conversation=view, deduplicated=False
                    )
                    return Response(
                        status_code=HTTP_200_OK,
                        content=steered_view.model_dump_json(),
                        media_type="application/json",
                    )

    async with factory() as session:
        async with session.begin():
            created = await _record_submit_intent(
                session,
                run_id=run_id,
                server_id=server_id,
                profile_revision=profile_revision,
                prompt_revision=prompt_revision,
                model_profile=payload.profile,
                requested_grants=sorted(capabilities),
                digest=engine_digest,
                now=now,
                root_budget=root_budget,
            )
            if created:
                await _record_chat_submission(
                    session,
                    run_id=run_id,
                    digest=submission_digest,
                    handler=handler,
                    attachments=resolved_attachments,
                    child_count=len(payload.children),
                    service_tags=list(payload.service_tags),
                    now=now,
                    session_run_id=session_run_id,
                )
    if not created:
        # Duplicate submit: same key must carry the SAME submission
        # (C05 rule 5 at chat granularity — message, selection, children
        # AND attachment references all participate in the comparison).
        events = await lifecycle.event_stream(run_id)
        recorded = next(
            (e for e in events if e.type == CHAT_SUBMITTED_EVENT_TYPE), None
        )
        recorded_digest = (
            recorded.data.get("submission_sha256")
            if recorded is not None and isinstance(recorded.data, Mapping)
            else None
        )
        if recorded_digest != submission_digest:
            return _conflict(
                request, "run id is already used by a different submission"
            )
        # Crash-gap healing: a committed facade row without its submit
        # edge re-anchors idempotently (the intent-level resume).
        if await lifecycle.current_state(run_id) is None:
            await lifecycle.record_transition(run_id, RunState.QUEUED)
    try:
        await asyncio.to_thread(
            recovery.client.enqueue_run, run_id, handler, payload_json
        )
    except RunIdInUseError:
        if created:
            # Unreachable in order (the row precedes every enqueue), but a
            # lost race must surface structured, never as a 500 (C05 r5).
            return _conflict(
                request, "run id is already used by a different submission"
            )
    except dbos_error.DBOSException, OSError:
        return _error(
            request,
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "jobs engine is unavailable",
        )
    async with factory() as session:
        row = await _load_run_row(session, run_id)
    assert row is not None  # committed above; absence is a bug
    state = await _replayed_state(lifecycle, run_id)
    view = await _conversation_view(factory, row, state)
    created_view = ChatCreated(conversation=view, deduplicated=not created)
    if created:
        return created_view  # the route's 201
    return Response(
        status_code=HTTP_200_OK,
        content=created_view.model_dump_json(),
        media_type="application/json",
    )


@router.get(
    "/runs",
    status_code=HTTP_200_OK,
    response_model=ChatPage,
    summary="List conversations (bounded page; one server or all servers)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def list_conversations(
    request: Request,
    server_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """One bounded page of root conversations (C01 rules 5 and 7).

    Safe method: session layer only. With an explicit ``server_id`` the
    page is exactly that server's scope (the frozen C01 rule 7
    behaviour); with ``server_id`` omitted the page lists recent ROOT
    conversations across ALL servers (including General Consult), so the
    Floating Chat History Selector can restore any session's server +
    service tags. Conversations are the ROOT runs (depth 0); delegated
    children are reachable through their parent's detail. States replay
    from the persisted transition events.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)
    factory = _sessions(request)
    scope = None if server_id is None else str(server_id)
    not_superseded = (
        "NOT EXISTS ("
        "SELECT 1 FROM job_events je "
        f"WHERE je.type = '{CHAT_SUBMITTED_EVENT_TYPE}' "
        "AND je.data->>'session_run_id' = job_runs.run_id::text"
        ")"
    )
    where = (
        f"depth = 0 AND {not_superseded}"
        if scope is None
        else f"server_id = :s AND depth = 0 AND {not_superseded}"
    )
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    text(
                        f"SELECT {_RUN_COLUMNS} FROM job_runs "
                        f"WHERE {where} "
                        "ORDER BY submitted_at DESC, run_id LIMIT :l OFFSET :o"
                    ),
                        {"s": scope, "l": limit, "o": offset}
                        if scope is not None
                        else {"l": limit, "o": offset},
                    )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM job_runs WHERE {where}"),
                {"s": scope} if scope is not None else {},
            )
        ).scalar_one()
        run_ids = [str(row["run_id"]) for row in rows]
        transitions: list[Mapping[str, object]] = []
        if run_ids:
            transitions = list(
                (
                    await session.execute(
                        text(
                            "SELECT run_id::text AS run_id, seq, ts, type, v, data "
                            "FROM job_events WHERE run_id = ANY(:ids) "
                            "AND type = 'run.transition' ORDER BY run_id, seq"
                        ),
                        {"ids": [uuid.UUID(r) for r in run_ids]},
                    )
                )
                .mappings()
                .all()
            )
    states: dict[str, str | None] = {run_id: None for run_id in run_ids}
    per_run: dict[str, list[JobEvent]] = {run_id: [] for run_id in run_ids}
    try:
        for event_row in transitions:
            per_run[str(event_row["run_id"])].append(
                parse_job_event(
                    {
                        "seq": event_row["seq"],
                        "run_id": str(event_row["run_id"]),
                        "ts": event_row["ts"].isoformat(),
                        "type": event_row["type"],
                        "v": event_row["v"],
                        "data": event_row["data"],
                    }
                )
            )
        for run_id, events in per_run.items():
            current = _replay_current(sorted(events, key=lambda e: e.seq))
            states[run_id] = None if current is None else current.value
    except RunLifecycleError as exc:
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "conflict",
            f"persisted run state requires recovery ({exc.code.value})",
        ) from None
    conversations: list[ConversationView] = []
    for row in rows:
        conversations.append(
            await _conversation_view(factory, row, states[str(row["run_id"])])
        )
    return ChatPage(
        conversations=conversations,
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/runs/{run_id}",
    status_code=HTTP_200_OK,
    response_model=ConversationView,
    summary="Read one conversation: binding, replayed state, children, references",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run stored for this run id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Persisted run state requires recovery",
        },
    },
)
async def read_conversation(request: Request, run_id: uuid.UUID) -> Response:
    """One conversation's durable record; state is replayed, never engine-called.

    Children are identity+state views of the persisted child rows; the
    recorded attachment references are identity+integrity handles. Task
    text, prompts and artifact bodies never appear (C05 rule 4).
    """
    await _require_session(request)
    lifecycle, _recovery = _jobs_services(request)
    identity = str(run_id)
    factory = _sessions(request)
    async with factory() as session:
        row = await _load_run_row(session, identity)
    if row is None:
        return _not_found_run(request)
    state = await _replayed_state(lifecycle, identity)
    return await _conversation_view(factory, row, state)


@router.post(
    "/runs/{run_id}/cancel",
    status_code=HTTP_200_OK,
    response_model=CancelView,
    summary="Cancel a conversation (records the request; reports known outcomes)",
    responses={
        HTTP_202_ACCEPTED: {
            "model": CancelView,
            "description": "Request recorded; engine outcome not observed within "
            "the budget — outcome stays null, re-entry resumes the wait",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run stored for this run id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The engine has no workflow under this run id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def cancel_conversation(
    request: Request, run_id: uuid.UUID, payload: ChatCancelBody
) -> Response:
    """Record the cancel request and report only a KNOWN engine outcome.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    Composes the reviewed safe-cancel facade over the explicit tree
    cancellation (guard rule 4): the request is recorded ONCE, the
    engine cancellation covers the conversation's children unless the
    caller explicitly keeps them, and the tiny HTTP observation budget
    expires into an honest ``202`` — never a claimed ``cancelled``.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)
    identity = str(run_id)
    try:
        result = await recovery.acancel_run(
            identity,
            cancel_children=payload.cancel_children,
            outcome_timeout=CANCEL_OUTCOME_BUDGET_SECONDS,
        )
    except RecoveryError as exc:
        if exc.code is RecoveryErrorCode.RUN_NOT_FOUND:
            return _not_found_run(request)
        if exc.code is RecoveryErrorCode.ENGINE_UNKNOWN_RUN:
            return _conflict(request, "the engine has no workflow under this run id")
        if exc.code is RecoveryErrorCode.CANCEL_OUTCOME_TIMEOUT:
            events = await lifecycle.event_stream(identity)
            requested_at = next(
                (e.ts for e in events if e.type == CANCEL_REQUESTED_EVENT_TYPE),
                None,
            )
            state = await _replayed_state(lifecycle, identity)
            return Response(
                status_code=HTTP_202_ACCEPTED,
                content=CancelView(
                    run_id=identity,
                    state=state,
                    outcome=None,
                    requested_at=(
                        None if requested_at is None else _wire_ts(requested_at)
                    ),
                    outcome_at=None,
                    requested_now=False,
                    unresolved_seqs=[],
                ).model_dump_json(),
                media_type="application/json",
            )
        raise  # unmapped producer failure: the generic envelope path
    return CancelView(
        run_id=result.run_id,
        state=result.outcome.value,
        outcome=result.outcome.value,
        requested_at=_wire_ts(result.requested_at),
        outcome_at=_wire_ts(result.outcome_at),
        requested_now=result.requested_now,
        unresolved_seqs=list(result.unresolved_seqs),
    )


@router.get(
    "/runs/{run_id}/events",
    status_code=HTTP_200_OK,
    summary="Subscribe to a conversation's persisted events (SSE, replay cursor)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session (answered before any stream byte)",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "An Origin header that is not allowlisted",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run stored for this run id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Last-Event-ID cursor is not a non-negative integer",
        },
    },
)
async def stream_conversation_events(request: Request, run_id: uuid.UUID) -> Response:
    """SSE subscription over the conversation's event stream (C05 rule 4).

    The same transport the jobs surface froze: the replay cursor is the
    standard SSE ``Last-Event-ID`` header; frames are strictly
    cursor-exclusive and ordered by ``seq``, so a reconnect with the
    last seen ``id`` deduplicates by construction. Authorization
    completes BEFORE the response starts; the stream is read-only (a
    disconnected browser cancels nothing, C08 rule 7). Delegation
    children subscribe through this same route with their own run ids —
    their identities are discovered from the parent's
    ``agent.delegation_planned`` event (value-free references).
    """
    await _require_session(request)  # 401 before any stream byte
    origin = request.headers.get("origin")
    if origin and origin not in request.app.state.public_origins:
        raise BoundaryError(
            HTTP_403_FORBIDDEN, "origin_rejected", "request origin is not allowed"
        )
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    sessions = _sessions(request)
    identity = str(run_id)
    async with sessions() as session:
        if await _load_run_row(session, identity) is None:
            return _not_found_run(request)
    raw_cursor = request.headers.get("last-event-id")
    if raw_cursor is None or raw_cursor == "":
        cursor = 0
    else:
        try:
            cursor = int(raw_cursor)
        except ValueError:
            cursor = -1
        if cursor < 0:
            return _validation_error(
                request, "last-event-id", "must be a non-negative integer"
            )
    return StreamingResponse(
        _event_stream(sessions, identity, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Registration (api-wiring exclusive resource)
# ---------------------------------------------------------------------------


def register_chat(app: FastAPI) -> None:
    """Register the chat surface under the shared boundary (C01/C08)."""
    app.include_router(router)
