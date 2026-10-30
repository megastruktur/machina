"""Jobs HTTP API: durable submit, status, cancel and event stream (C05/C08).

Ownership map (contracts.md **C05** runs/steps/events, **C08** UI/API and
auth, **C01** envelope/identity/scope; SERVICES ``mai-api`` row; cards
``05-job-schema``, ``05-run-lifecycle``, ``05-engine-adapter``,
``05-cancel-recovery``; consumer ``05-web-jobs``):

- **HTTP only persists intent** (acceptance 1). Submit inserts the
  ``job_runs`` row (the facade's submit-intent record), records the
  ``run.submitted`` intent-digest event plus the frozen ``None → queued``
  submit edge, and durably enqueues the run through the ONE engine
  adapter (:meth:`mai.jobs.engine.EngineClient.enqueue_run`, C05 rule 1:
  engine vocabulary stops at the adapter). No capability dispatch, no
  LLM, no SSH ever runs inside a request (acceptance 2): effects happen
  in the worker, gated by the reviewed dispatcher/policy/approval
  producers.
- **Idempotent submit** (C05 rule 5). The client-supplied ``run_id`` IS
  the idempotency key (deterministic workflow ID, C01 rule 1). A repeat
  submit whose canonical intent digest matches the recorded
  ``run.submitted`` event attaches to the SAME run (``200``,
  ``deduplicated=true``) and re-arms the engine enqueue, so a crash
  between the facade commit and the engine enqueue self-heals. A repeat
  with a different intent for the same ID is a genuinely new run that
  lost the ID race: structured ``409 conflict``, never a 500. The
  engine's own ID-in-use rejection maps to the same ``409``.
- **Target binding** (C01 rule 7, C05 rule 3). Submit names the target
  ``server_id`` explicitly and the profile revisions are bound from the
  canonical store at submit time — never from the request body. The
  embedded execution context (C03 rule 2) is assembled server-side: the
  principal is the validated session's ``hash_token`` identity (C01
  rule 2; the token preimage never leaves the cookie), and the payload
  is validated by the ONE run-intent producer
  (:func:`mai.jobs.steps.parse_run_intent`) before anything persists.
- **Status is replayed, never guessed.** Run/list state comes from the
  persisted event stream through the reviewed
  :class:`mai.jobs.lifecycle.RunLifecycle` replay (fail-closed); the API
  process never reads engine tables and never calls the engine for
  reads, so a request lifecycle stays free of engine calls entirely.
- **Cancel records intent** (C05 rule 2: requested ≠ verified). Cancel
  composes the reviewed safe-cancel facade
  (:meth:`mai.jobs.recovery.RecoveryService.acancel_run`): it records the
  request ONCE, asks the engine, and reports only a KNOWN terminal
  outcome. The HTTP observation budget is deliberately tiny (1 s): a run
  that has not landed answers ``202`` with ``outcome=null`` — never
  ``cancelled`` from a timeout (engine guard rule 3) — and re-entry
  (repeat POST) is idempotent and resumes the wait.
- **Event stream** (C05 rule 4, C08 rule 7 — the open C08 transport item
  settles HERE). Transport is SSE (``text/event-stream``) over
  ``GET /api/v1/jobs/runs/{run_id}/events``; each frame is
  ``id: <seq>`` + ``data: <frozen C05 rule 4 envelope JSON>``; the
  replay cursor travels as the standard SSE ``Last-Event-ID`` request
  header (the reviewed ``05-web-jobs`` consumer primitive already speaks
  exactly this shape). Replay is read-only and strictly
  cursor-exclusive, so a reconnect deduplicates by construction. The
  stream tails persisted events and closes itself once a terminal run
  is drained — a disconnected browser cancels nothing and work keeps
  running (SERVICES ``mai-web`` row; acceptance 2). Subscription is
  session-gated BEFORE any stream byte (AUTH-S5: 401 is an ordinary
  envelope response, not stream bytes) and origin-checked: an ``Origin``
  header that is present must be allowlisted (browsers omit ``Origin``
  on same-origin GETs, so the legitimate consumer passes; cross-site
  subscription attempts carry one and are rejected).
- **Failure mapping** is the C01 envelope everywhere: producer
  rejections keep their stable codes (``validation_failed``),
  state errors are value-free ``409 conflict``, unknown identities are
  ``404 not_found``, and an unusable dependency fails closed with
  ``503 unavailable`` — never a guessed success.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any, Final

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

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.auth.sessions import hash_token
from mai.contracts.conversations import EntityTagPreviewView
from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_RUN_STATES,
    JobEvent,
    RunState,
    event_to_wire,
    parse_job_event,
)
from mai.contracts.server import parse_server_profile_text
from mai.jobs import steps as run_steps
from mai.jobs.engine import EngineError, RunIdInUseError, validate_run_id
from mai.jobs.lifecycle import (
    RunLifecycle,
    RunLifecycleError,
    current_state as _replay_current,
    make_transition_event,
)
from mai.jobs.recovery import (
    CANCEL_REQUESTED_EVENT_TYPE,
    RecoveryError,
    RecoveryErrorCode,
    RecoveryService,
)
from mai.policy.evaluate import ExecutionContext, PolicyError, PolicyMode
from mai.profiles.files import FileStoreErrorCode, ProfileFileError, StoredProfile
from mai.tag_store import EntityTagPreview, apreview_job_tags
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

__all__ = ["register_jobs"]

#: Honest empty projection for ids the preview helper could not see
#: (unknown or cross-server): the field is always present, never null.
_EMPTY_TAG_PREVIEW: Final[EntityTagPreviewView] = EntityTagPreviewView(
    preview=[], total=0
)


def _preview_view(found: EntityTagPreview | None) -> EntityTagPreviewView:
    """Project the store's preview dataclass; absent id → honest empty."""
    if found is None:
        return _EMPTY_TAG_PREVIEW
    return EntityTagPreviewView(preview=list(found.preview), total=found.total)

# ---------------------------------------------------------------------------
# Transport/facade bounds (module-owned policy, all explicitly bounded)
# ---------------------------------------------------------------------------

#: The event type this surface owns: the submit-intent record (C05 rule 4
#: slug grammar; the TYPE vocabulary stays open, the envelope shape is frozen).
SUBMITTED_EVENT_TYPE: Final[str] = "run.submitted"

#: Bounded engine-outcome observation for the HTTP cancel call (seconds).
#: The request is recorded BEFORE any waiting and survives a timeout;
#: re-entry resumes the wait. A timeout NEVER claims ``cancelled``.
#: The poll cadence is the facade's constructor default (1 s), checked
#: against this budget — one extra engine read at most.
CANCEL_OUTCOME_BUDGET_SECONDS: Final[float] = 1.0

#: List page bounds (C01 rule 5: list endpoints are bounded/paginated).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

#: SSE transport bounds: per-poll replay batch, poll cadence, keepalive
#: comment cadence and the hard stream lifetime (the consumer reconnects
#: with its ``Last-Event-ID`` cursor and deduplicates by construction).
SSE_REPLAY_BATCH: Final[int] = 200
SSE_POLL_SECONDS: Final[float] = 0.5
SSE_PING_SECONDS: Final[float] = 15.0
SSE_MAX_STREAM_SECONDS: Final[float] = 600.0

_SUBMITTED_DIGEST_KEY: Final[str] = "payload_sha256"

#: The one transition event type the lifecycle owns (replay filter).
_TRANSITION_EVENT_TYPE: Final[str] = "run.transition"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the facade/engine producers)
# ---------------------------------------------------------------------------


class StepBody(BaseModel):
    """One ordered capability step of a run intent (C05 rule 3).

    ``payload`` is the capability operation document (digest-bound into
    approvals at effect time); ``resource`` optionally names the mutation
    lock identity for the step (C05 rule 6). Validation truth is the
    run-intent producer; this model only bounds transport.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]
    resource: str | None = Field(default=None, max_length=200)


class RunSubmitBody(BaseModel):
    """Submit body (strict: unknown fields are rejected, value-free 422).

    ``run_id`` is REQUIRED: the client-generated idempotency key (C05
    rule 5 — the deterministic workflow ID). ``mode`` selects the frozen
    prompting mode (C03 rule 5); it never widens any check. Waits stay at
    the frozen producer defaults (bounded knobs are engine-side).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    server_id: str
    model_profile: str = Field(min_length=1, max_length=200)
    mode: PolicyMode = PolicyMode.ASK
    credential_references: list[str] = Field(default_factory=list)
    steps: list[StepBody] = Field(min_length=1, max_length=run_steps.MAX_STEPS_PER_RUN)


class CancelBody(BaseModel):
    """Cancel body: explicit tree cancellation (engine guard rule 4)."""

    model_config = ConfigDict(extra="forbid")

    cancel_children: bool = False


class StepView(BaseModel):
    """One durable step row (intent + safe receipt; C04 rule 4)."""

    seq: int
    capability_id: str
    state: str
    receipt: dict[str, Any] | None


class RunView(BaseModel):
    """One run: stable identity binding plus replayed facade state."""

    run_id: str
    server_id: str
    parent_run_id: str | None
    depth: int
    profile_revision: int
    prompt_revision: int
    model_profile: str
    requested_grants: list[str]
    label: str
    state: str | None
    cancel_requested_at: str | None
    submitted_at: str
    updated_at: str
    tag_preview: EntityTagPreviewView


class RunDetail(RunView):
    """One run plus its durable step intent rows."""

    steps: list[StepView]


class RunCreated(BaseModel):
    """Submit outcome: the run view plus the dedup verdict (C05 rule 5)."""

    run: RunView
    deduplicated: bool


class RunPage(BaseModel):
    """One bounded, explicitly server-scoped list page (C01 rule 5/7)."""

    runs: list[RunView]
    limit: int
    offset: int
    total: int


class CancelView(BaseModel):
    """Cancel outcome: a KNOWN engine outcome, or an honest pending.

    ``outcome`` is the engine's terminal state value — ``null`` means the
    budget expired WITHOUT a known outcome (guard rule 3: never claim
    ``cancelled`` from a timeout); the request stays recorded.
    """

    run_id: str
    state: str | None
    outcome: str | None
    requested_at: str | None
    outcome_at: str | None
    requested_now: bool
    unresolved_seqs: list[int]


# ---------------------------------------------------------------------------
# Facade access + small helpers
# ---------------------------------------------------------------------------


def _jobs_services(request: Request) -> tuple[RunLifecycle, RecoveryService]:
    """The lifespan-composed facade pair; without them fail closed."""
    lifecycle = getattr(request.app.state, "job_lifecycle", None)
    recovery = getattr(request.app.state, "job_recovery", None)
    if lifecycle is None or recovery is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "jobs domain service is unavailable",
        )
    return lifecycle, recovery


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app-side session factory (present whenever the facade is)."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "jobs domain service is unavailable",
        )
    return factory


def _canonical_profile(request: Request, server_id: uuid.UUID) -> StoredProfile:
    """The canonical profile for the explicitly named target (C01 rule 7).

    Submit binds the run to an EXISTING canonical server: an unknown
    target is ``404 not_found``, unusable canonical state is surfaced
    (never silently repaired), and a missing store fails closed.
    """
    store = getattr(request.app.state, "profiles", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "canonical profile service is unavailable",
        )
    try:
        return store.read(server_id)
    except ProfileFileError as exc:
        if exc.code is FileStoreErrorCode.NOT_FOUND:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no profile stored for this server id",
            ) from None
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "conflict",
            "canonical profile state requires recovery",
        ) from None


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    fields: dict[str, str] | None = None,
) -> Response:
    """The C01 structured envelope for one rejection."""
    return render_error_envelope(request, status, code, message, fields=fields)


def _validation_error(request: Request, field: str, detail: str) -> Response:
    return _error(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "request body failed validation",
        fields={field: detail},
    )


def _conflict(request: Request, message: str) -> Response:
    return _error(request, HTTP_409_CONFLICT, "conflict", message)


def _not_found_run(request: Request) -> Response:
    return _error(
        request, HTTP_404_NOT_FOUND, "not_found", "no run stored for this run id"
    )


async def _load_run_row(session: AsyncSession, run_id: str) -> dict[str, object] | None:
    """One ``job_runs`` row projected for the wire, or ``None``.

    ``cancel_requested_at`` is the FACADE-RECORDED request: the reviewed
    producer (``05-cancel-recovery``) records it as the
    ``run.cancel_requested`` EVENT — the column is only a fallback, so the
    view derives the timestamp from the record that actually exists.
    """
    row = (
        (
            await session.execute(
                text(f"SELECT {_RUN_COLUMNS} FROM job_runs WHERE run_id = :r"),
                {"r": uuid.UUID(run_id)},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def _replayed_state(lifecycle: RunLifecycle, run_id: str) -> str | None:
    """The run's facade state via the fail-closed replay (never an engine).

    A log that fails replay validation is canonical state needing
    recovery: value-free ``409 conflict`` through the shared boundary
    handler, never a guessed state and never a raw 500.
    """
    try:
        state = await lifecycle.current_state(run_id)
    except RunLifecycleError as exc:
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "conflict",
            f"persisted run state requires recovery ({exc.code.value})",
        ) from None
    return None if state is None else state.value


_RUN_COLUMNS: Final[str] = """
    run_id::text AS run_id, server_id::text AS server_id,
    parent_run_id::text AS parent_run_id, depth, profile_revision,
    prompt_revision, model_profile, requested_grants, label,
    COALESCE(
        cancel_requested_at,
        (SELECT MAX(ts) FROM job_events
         WHERE job_events.run_id = job_runs.run_id
           AND type = 'run.cancel_requested')
    ) AS cancel_requested_at,
    submitted_at, updated_at
"""


def _run_view(
    row: Mapping[str, object],
    state: str | None,
    tag_preview: EntityTagPreviewView,
) -> RunView:
    """Type one projected row + replayed state into the wire view."""
    return RunView(
        run_id=str(row["run_id"]),
        server_id=str(row["server_id"]),
        parent_run_id=(
            None if row["parent_run_id"] is None else str(row["parent_run_id"])
        ),
        depth=int(row["depth"]),  # type: ignore[arg-type]
        profile_revision=int(row["profile_revision"]),  # type: ignore[arg-type]
        prompt_revision=int(row["prompt_revision"]),  # type: ignore[arg-type]
        model_profile=str(row["model_profile"]),
        requested_grants=list(row["requested_grants"]),  # type: ignore[arg-type]
        label=str(row["label"]),
        state=state,
        cancel_requested_at=(
            None
            if row["cancel_requested_at"] is None
            else _wire_ts(row["cancel_requested_at"])  # type: ignore[arg-type]
        ),
        submitted_at=_wire_ts(row["submitted_at"]),  # type: ignore[arg-type]
        updated_at=_wire_ts(row["updated_at"]),  # type: ignore[arg-type]
        tag_preview=tag_preview,
    )


# ---------------------------------------------------------------------------
# Submit: intent document + facade transaction (the submit owner, C05)
# ---------------------------------------------------------------------------


def _intent_document(
    body: RunSubmitBody,
    run_id: str,
    server_id: str,
    context: ExecutionContext,
) -> dict[str, object]:
    """Assemble the frozen run-intent document (C05 rule 3; producer shape).

    Steps are ordered implicitly from the array (``seq`` ascending from 1,
    the durable total order the producer enforces); the request carries no
    redundant ``seq``.
    """
    return {
        "v": run_steps.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": server_id,
        "context": context.to_json(),
        "steps": [
            {
                "seq": index + 1,
                "capability_id": step.capability_id,
                "payload": step.payload,
                **({"resource": step.resource} if step.resource else {}),
            }
            for index, step in enumerate(body.steps)
        ],
    }


#: Bounded run label (the wire/human identifier stays display-safe).
MAX_RUN_LABEL_CHARS: Final[int] = 200


def _run_label(payload: RunSubmitBody) -> str:
    """Derive the run label from the canonical intent (W1).

    The first step's ``label`` payload field when it is a non-empty
    string, else the first step's ``capability_id``. Overlong values are
    truncated at :data:`MAX_RUN_LABEL_CHARS` (the stored CHECK bound).
    """
    first = payload.steps[0]
    raw = first.payload.get("label")
    label = raw if isinstance(raw, str) and raw.strip() else first.capability_id
    return label.strip()[:MAX_RUN_LABEL_CHARS]


async def _record_submit_intent(
    session: AsyncSession,
    *,
    run_id: str,
    server_id: str,
    profile_revision: int,
    prompt_revision: int,
    model_profile: str,
    requested_grants: list[str],
    digest: str,
    label: str,
    now: datetime,
    root_budget: dict[str, object] | None = None,
) -> bool:
    """Insert the ``job_runs`` row + its two intent events, atomically.

    Returns ``True`` exactly when THIS transaction created the run. The
    row insert is the submit serialization point (the PK is the C05 rule 5
    dedup key); the ``run.submitted`` digest event and the frozen
    ``None → queued`` submit edge commit with it, so the recorded intent
    can never exist without its identity binding and vice versa. Seq
    allocation follows the documented whole-stream discipline (row-locked
    run, ``MAX(seq)+1``; the storage ``UNIQUE (run_id, seq)`` is the
    backstop — lifecycle acceptance 1). ``root_budget`` is the validated
    aggregate delegation-budget document frozen at submit (``None`` = the
    explicit unbudgeted root).
    """
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     label, root_budget, submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, :profile_revision,
                     :prompt_revision, :model_profile, :requested_grants,
                     :label, :root_budget, :now, :now)
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": uuid.UUID(server_id),
                "profile_revision": profile_revision,
                "prompt_revision": prompt_revision,
                "model_profile": model_profile,
                "requested_grants": json.dumps(requested_grants),
                "label": label,
                "root_budget": (
                    json.dumps(root_budget) if root_budget is not None else None
                ),
                "now": now,
            },
        )
    ).scalar()
    if inserted is None:
        return False
    # 0011 mandatory-tag invariant: THIS transaction created the run, so the
    # classification commits with the same transaction (history-tags
    # W1-FIX1); the dedup replay path above returns unchanged.
    await session.execute(
        text(
            """
            INSERT INTO job_tags (job_id, tag_id, assignment_source)
            VALUES (:run_id, :tag, :source)
            """
        ),
        {
            "run_id": uuid.UUID(run_id),
            "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
            "source": SYSTEM_ASSIGNMENT_SOURCE,
        },
    )
    seq = (
        await session.execute(
            text("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE run_id = :r"),
            {"r": uuid.UUID(run_id)},
        )
    ).scalar_one()
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
            "type": SUBMITTED_EVENT_TYPE,
            "v": EVENT_PAYLOAD_VERSION,
            "data": json.dumps({_SUBMITTED_DIGEST_KEY: digest}),
        },
    )
    transition = make_transition_event(run_id, seq + 1, now, None, RunState.QUEUED)
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
            "seq": transition.seq,
            "ts": transition.ts,
            "type": transition.type,
            "v": transition.v,
            "data": json.dumps(transition.data),
        },
    )
    return True


async def _enqueue_run(
    recovery: RecoveryService, run_id: str, payload_json: str
) -> None:
    """Durably enqueue through the ONE engine adapter (off the event loop).

    Raises :class:`RunIdInUseError` when the engine already holds the
    workflow ID (the C05 rule 5 reject policy); other engine failures
    surface as ``DBOSException``/``OSError`` for the 503 mapping.
    """
    await asyncio.to_thread(
        recovery.client.enqueue_run, run_id, run_steps.HANDLER_NAME, payload_json
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post(
    "/runs",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit a run (idempotent durable intent, C05 rule 5)",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id already used by a different submit intent",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain or canonical profile service unavailable",
        },
    },
)
async def submit_run(request: Request, payload: RunSubmitBody) -> Response:
    """Persist one run intent and durably enqueue it (HTTP never executes).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The target profile's revisions are bound from the canonical store; the
    intent is validated by the ONE run-intent producer; the facade row,
    the intent-digest event and the submit edge commit atomically; the
    engine enqueue follows the commit so a crash leaves a re-armable gap,
    not a half intent.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)

    try:
        run_id = validate_run_id(payload.run_id)
    except EngineError:
        return _validation_error(request, "run_id", "must be a canonical UUID string")
    try:
        server_id = str(uuid.UUID(payload.server_id))
    except ValueError:
        return _validation_error(
            request, "server_id", "must be a canonical UUID string"
        )
    stored = _canonical_profile(request, uuid.UUID(server_id))
    profile = parse_server_profile_text(stored.text)

    context: ExecutionContext
    try:
        context = ExecutionContext(
            principal=hash_token(token),
            server_id=uuid.UUID(server_id),
            run_id=uuid.UUID(run_id),
            parent_scope=None,
            profile_revision=str(stored.revision),
            prompt_revision=str(profile.prompt_revision),
            credential_references=tuple(payload.credential_references),
            allowed_capabilities=tuple(
                dict.fromkeys(step.capability_id for step in payload.steps)
            ),
            mode=payload.mode,
        )
    except PolicyError as exc:
        return _validation_error(request, "context", exc.detail)
    document = _intent_document(payload, run_id, server_id, context)
    try:
        payload_json = run_steps.canonical_json(document)
        run_steps.parse_run_intent(payload_json)  # the ONE producer validates
    except run_steps.StepsError as exc:
        return _validation_error(request, "steps", str(exc))
    digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()

    factory = _sessions(request)
    async with factory() as session:
        async with session.begin():
            created = await _record_submit_intent(
                session,
                run_id=run_id,
                server_id=server_id,
                profile_revision=stored.revision,
                prompt_revision=profile.prompt_revision,
                model_profile=payload.model_profile,
                requested_grants=sorted({step.capability_id for step in payload.steps}),
                digest=digest,
                label=_run_label(payload),
                now=datetime.now(timezone.utc),
            )
    if not created:
        # Duplicate submit: same key must carry the SAME intent (C05 rule 5).
        events = await lifecycle.event_stream(run_id)
        recorded = next((e for e in events if e.type == SUBMITTED_EVENT_TYPE), None)
        if recorded is None or recorded.data.get(_SUBMITTED_DIGEST_KEY) != digest:
            return _conflict(
                request, "run id is already used by a different submit intent"
            )
        # Crash-gap healing: a committed facade row without its submit edge
        # (crash between commit and enqueue) re-anchors idempotently.
        if await lifecycle.current_state(run_id) is None:
            await lifecycle.record_transition(run_id, RunState.QUEUED)
    try:
        await _enqueue_run(recovery, run_id, payload_json)
    except RunIdInUseError:
        if created:
            # Unreachable in order (the row precedes every enqueue), but a
            # lost race must surface structured, never as a 500 (C05 r5).
            return _conflict(
                request, "run id is already used by a different submit intent"
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
    previews = await apreview_job_tags(
        factory,
        [uuid.UUID(run_id)],
        server_id=uuid.UUID(str(row["server_id"])),
    )
    created_view = RunCreated(
        run=_run_view(
            row,
            state,
            tag_preview=_preview_view(previews.get(uuid.UUID(run_id))),
        ),
        deduplicated=not created,
    )
    if created:
        return created_view  # the route's 201
    # Duplicate attach: same intent, SAME run — 200, never a second 201.
    return Response(
        status_code=HTTP_200_OK,
        content=created_view.model_dump_json(),
        media_type="application/json",
    )


@router.get(
    "/runs",
    status_code=HTTP_200_OK,
    response_model=RunPage,
    summary="List runs for one explicit server scope (bounded page)",
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
async def list_runs(
    request: Request,
    server_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> Response:
    """One bounded page of one server's runs (C01 rules 5 and 7).

    Safe method: session layer only. The server scope is EXPLICIT and
    required — default shared retrieval is never unfiltered multi-server
    access (C01 rule 7). States are replayed from the persisted
    transition events of exactly this page's runs.

    Filters (``q`` case-insensitive substring over run_id, label and
    model_profile; ``date_from``/``date_to`` over ``submitted_at``) apply
    BEFORE pagination: the same predicate drives both the page query and
    the pre-slice ``COUNT(*)``, so ``total`` is the filtered total.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)
    factory = _sessions(request)
    scope = str(server_id)
    predicates = ["server_id = :s"]
    params: dict[str, object] = {"s": scope, "l": limit, "o": offset}
    if q is not None:
        needle = f"%{q}%"
        predicates.append(
            "(run_id::text ILIKE :q OR label ILIKE :q "
            "OR model_profile ILIKE :q)"
        )
        params["q"] = needle
    if date_from is not None:
        predicates.append("submitted_at >= :date_from")
        params["date_from"] = date_from.astimezone(timezone.utc)
    if date_to is not None:
        predicates.append("submitted_at <= :date_to")
        params["date_to"] = date_to.astimezone(timezone.utc)
    where = " AND ".join(predicates)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        f"SELECT {_RUN_COLUMNS} FROM job_runs "
                        f"WHERE {where} ORDER BY submitted_at DESC, run_id "
                        "LIMIT :l OFFSET :o"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM job_runs WHERE {where}"),
                {k: v for k, v in params.items() if k not in ("l", "o")},
            )
        ).scalar_one()
        run_ids = [str(row["run_id"]) for row in rows]
        transitions: list[Mapping[str, object]] = []
        if run_ids:
            transitions = (
                (
                    await session.execute(
                        text(
                            "SELECT run_id::text AS run_id, seq, ts, type, v, data "
                            "FROM job_events WHERE run_id = ANY(:ids) "
                            f"AND type = '{_TRANSITION_EVENT_TYPE}' "
                            "ORDER BY run_id, seq"
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
        for row in transitions:
            per_run[str(row["run_id"])].append(
                parse_job_event(
                    {
                        "seq": row["seq"],
                        "run_id": str(row["run_id"]),
                        "ts": row["ts"].isoformat(),
                        "type": row["type"],
                        "v": row["v"],
                        "data": row["data"],
                    }
                )
            )
        for run_id, events in per_run.items():
            current = _replay_current(sorted(events, key=lambda e: e.seq))
            states[run_id] = None if current is None else current.value
    except RunLifecycleError as exc:
        return _error(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            f"persisted run state requires recovery ({exc.code.value})",
        )
    previews = await apreview_job_tags(
        factory,
        [uuid.UUID(r) for r in run_ids],
        server_id=server_id,
    )
    return RunPage(
        runs=[
            _run_view(
                row,
                states[str(row["run_id"])],
                tag_preview=_preview_view(
                    previews.get(uuid.UUID(str(row["run_id"])))
                ),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/runs/{run_id}",
    status_code=HTTP_200_OK,
    response_model=RunDetail,
    summary="Read one run: binding, replayed state and durable steps",
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
async def read_run(request: Request, run_id: uuid.UUID) -> Response:
    """One run's durable record; state is replayed, never engine-called."""
    await _require_session(request)
    lifecycle, _recovery = _jobs_services(request)
    identity = str(run_id)
    factory = _sessions(request)
    async with factory() as session:
        row = await _load_run_row(session, identity)
        if row is None:
            return _not_found_run(request)
        steps_rows = (
            (
                await session.execute(
                    text(
                        "SELECT seq, capability_id, state, receipt FROM job_steps "
                        "WHERE run_id = :r ORDER BY seq"
                    ),
                    {"r": run_id},
                )
            )
            .mappings()
            .all()
        )
    state = await _replayed_state(lifecycle, identity)
    previews = await apreview_job_tags(
        factory,
        [run_id],
        server_id=uuid.UUID(str(row["server_id"])),
    )
    return RunDetail(
        **_run_view(
            row, state, tag_preview=_preview_view(previews.get(run_id))
        ).model_dump(),
        steps=[
            StepView(
                seq=int(step_row["seq"]),
                capability_id=str(step_row["capability_id"]),
                state=str(step_row["state"]),
                receipt=(
                    None if step_row["receipt"] is None else dict(step_row["receipt"])  # type: ignore[arg-type]
                ),
            )
            for step_row in steps_rows
        ],
    )


@router.post(
    "/runs/{run_id}/cancel",
    status_code=HTTP_200_OK,
    response_model=CancelView,
    summary="Cancel a run (records the request; reports only known outcomes)",
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
async def cancel_run(
    request: Request, run_id: uuid.UUID, payload: CancelBody
) -> Response:
    """Record the cancel request and report only a KNOWN engine outcome.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    Composes the reviewed safe-cancel facade: the request is recorded
    ONCE (idempotent re-entry), the engine cancellation is explicit, and
    the tiny HTTP observation budget expires into an honest ``202`` with
    ``outcome=null`` — never a claimed ``cancelled`` (guard rule 3).
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
            # The request IS recorded (recorded before any wait); report
            # the pending state honestly — outcome stays null.
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


# ---------------------------------------------------------------------------
# Event stream (C08 rule 7 transport decision: SSE + Last-Event-ID cursor)
# ---------------------------------------------------------------------------


async def _fetch_batch(
    session: AsyncSession, run_id: str, after_seq: int
) -> list[JobEvent]:
    """The next replay batch strictly after the cursor, fail-closed typed.

    Every row is re-validated against the frozen envelope (C05 rule 4):
    shape drift from any producer fails the stream instead of leaking
    into a replay.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT seq, ts, type, v, data FROM job_events "
                    "WHERE run_id = :r AND seq > :c ORDER BY seq LIMIT :b"
                ),
                {"r": uuid.UUID(run_id), "c": after_seq, "b": SSE_REPLAY_BATCH},
            )
        )
        .mappings()
        .all()
    )
    return [
        parse_job_event(
            {
                "seq": row["seq"],
                "run_id": run_id,
                "ts": row["ts"].isoformat(),
                "type": row["type"],
                "v": row["v"],
                "data": row["data"],
            }
        )
        for row in rows
    ]


async def _drained_run_is_terminal(session: AsyncSession, run_id: str) -> bool:
    """True once the run's latest recorded transition is terminal.

    A terminal run can never emit another event (terminal states have no
    out-edges), so a drained terminal stream closes itself.
    """
    row = (
        await session.execute(
            text(
                "SELECT data FROM job_events WHERE run_id = :r "
                f"AND type = '{_TRANSITION_EVENT_TYPE}' ORDER BY seq DESC LIMIT 1"
            ),
            {"r": uuid.UUID(run_id)},
        )
    ).scalar()
    if row is None:
        return False
    target = row.get("to") if isinstance(row, Mapping) else None
    try:
        return RunState(str(target)) in TERMINAL_RUN_STATES
    except ValueError:
        return False


async def _event_stream(
    sessions: async_sessionmaker[AsyncSession], run_id: str, cursor: int
) -> AsyncIterator[str]:
    """The SSE body: cursor-exclusive replay, then tail, then self-close."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SSE_MAX_STREAM_SECONDS
    last_frame = loop.time()
    while True:
        async with sessions() as session:
            batch = await _fetch_batch(session, run_id, cursor)
            terminal = (
                await _drained_run_is_terminal(session, run_id) if not batch else False
            )
        for event in batch:
            yield (
                f"id: {event.seq}\n"
                f"data: {json.dumps(event_to_wire(event), separators=(',', ':'))}\n\n"
            )
            cursor = event.seq
            last_frame = loop.time()
        if batch:
            continue  # drain bursts before polling again
        if terminal:
            return  # drained past a terminal run: nothing new can arrive
        now = loop.time()
        if now >= deadline:
            return  # bounded stream; the client reconnects from its cursor
        if now - last_frame >= SSE_PING_SECONDS:
            yield ": ping\n\n"  # keepalive comment; consumers ignore it
            last_frame = now
        await asyncio.sleep(SSE_POLL_SECONDS)


@router.get(
    "/runs/{run_id}/events",
    status_code=HTTP_200_OK,
    summary="Subscribe to a run's persisted events (SSE, replay cursor)",
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
async def stream_events(request: Request, run_id: uuid.UUID) -> Response:
    """SSE subscription over the persisted event stream (C05 rule 4).

    The replay cursor is the standard SSE ``Last-Event-ID`` header; absent
    means "from the run's beginning". Frames are strictly cursor-exclusive
    and ordered by ``seq``, so a reconnect with the last seen ``id``
    deduplicates by construction (EVT-REPLAY). Authorization completes
    BEFORE the response starts: an unauthenticated call is an ordinary 401
    envelope, never stream bytes (AUTH-S5). The stream is read-only: a
    disconnected browser cancels nothing (C08 rule 7).
    """
    await _require_session(request)  # 401 before any stream byte
    origin = request.headers.get("origin")
    if origin and origin not in request.app.state.public_origins:
        # C08 rule 7 "origin-checked": browsers omit Origin on same-origin
        # GETs, so the legitimate consumer passes; cross-site attempts,
        # which always carry Origin, are rejected here.
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


def register_jobs(app: FastAPI) -> None:
    """Register the jobs surface under the shared boundary (C01/C08)."""
    app.include_router(router)
