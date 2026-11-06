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
  that IS the intent resume. The runtime-v2 controls (§9) are
  state-specific and never generic replays: ``resume`` exposes only the
  valid continuation per state (a settled conversation resumes through
  the next submitted turn; ``waiting_children``/``reconciling``/
  ``waiting_approval`` are structured refusals), ``reconcile``
  classifies open unknown effects through the recovery owner (the ONLY
  uncertain-edge writer — never re-execution), and the approval-bound
  one-shot operation resume stays the agent-run producer's
  ``dispatch_once(approval_id=…)` bridge seam (worker-side; no HTTP may
  dispatch effects).
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
from typing import Annotated, Final, Literal

from dbos import error as dbos_error
from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
from mai.agents import run_v2 as agent_run_v2
from mai.agents.conversation_store import (
    aactive_run,
    aattach_run,
    acreate_conversation,
    aenqueue_steering,
    aget_conversation,
    arelease_run,
    asubmit_turn,
)
from mai.agents.objective_store import (
    abind_run,
    abind_run_tx,
    acreate_objective_tx,
    aget_objective as aget_objective_scope_checked,
)
from mai.agents.objective_store import alist_objective_runs
from mai.contracts.objectives import (
    ActorKind,
    ObjectiveContractError,
    ObjectiveErrorCode,
    ObjectiveStatus,
)
from mai.agents.budgets import (
    BUDGET_PAYLOAD_VERSION,
    MAX_ACTIVE_RUNS,
    MAX_TOTAL_STEPS,
    MAX_TOTAL_TOKENS,
    BudgetError,
    check_fanout,
    parse_root_budget,
)
from mai.agents.context import (
    MAX_EVIDENCE_ITEMS,
    MAX_ROLE_CHARS,
    format_service_map_section,
)
from mai.agents.model_store import ModelStoreError
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.capabilities.history_tags import TAGS_CAPABILITY_IDS
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.capabilities.task_state import (
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_STATE_CAPABILITY_IDS,
    TASK_UPDATE_ITEM_CAPABILITY,
)
from mai.plugins.config import PluginConfigStore, parse_plugin_config_text
from mai.policy.config import GrantConfigStore
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
    _preview_view,
    _record_submit_intent,
    _replayed_state,
    _run_view,
    _sessions,
    _validation_error,
    _wire_ts,
)
from mai.api.servers import resolve_profiles_root
from mai.applications.service_map import (
    MAX_MATCHED_ENTRIES,
    ServiceMapEntry,
    ServiceMapStore,
)
from mai.auth.sessions import hash_token
from mai.contracts.conversations import (
    MAX_MESSAGE_CHARS,
    ConversationContractError,
    ConversationErrorCode,
    ConversationSteerRequest,
    ConversationTurnSubmitRequest,
    ConversationTurnSubmitResponse,
    MessageSourceKind,
    SteerAction,
    TurnDisposition,
    assert_history_available,
    assert_writer_admission,
)
from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    JobEvent,
    RunState,
    TERMINAL_RUN_STATES,
    parse_job_event,
)
from mai.contracts.server import parse_server_profile_text
from mai.contracts.tasks import TaskErrorCode
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
from mai.execution.reconcile import ReconciliationClass
from mai.policy.evaluate import ExecutionContext, PolicyError, PolicyMode
from mai.tag_store import EntityTagPreview, apreview_job_tags

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
CHAT_RENAMED_EVENT_TYPE: Final[str] = "chat.renamed"
#: Structured rejection when steering targets a conversation whose writer
#: lease is free (nothing is executing). Kept local to this seam: the
#: frozen ``ConversationErrorCode`` vocabulary has no fitting member and
#: the contracts module is outside this card's allowlist.
NO_ACTIVE_RUN_CODE: Final[str] = "no_active_run"
MAX_CHAT_TITLE_CHARS: Final[int] = 120

#: The General Consult sentinel server id (``uuid.UUID(int=0)``): the
#: conversation has NO target host (no server selected in the UI). Chat
#: submits bound to it skip the canonical-profile binding (there is no
#: host to bind) and pin revision 1; host-scoped attachments are
#: rejected (General Consult names no target).
GLOBAL_CONSULT_SERVER_ID: Final[str] = "00000000-0000-0000-0000-000000000000"

#: The default conversation role (single-administrator product; C06
#: rule 2's role line). Overridable per submit within the frozen bounds.
DEFAULT_ROLE: Final[str] = "administrator"

#: The implicit task revision while NO task episode exists yet (the
#: ``agent_tasks`` rows are migration 0009's, owned by ``11-task-store``).
#: Every turn response reports this revision; an ``expected_revision``
#: CAS pin must be ``None`` or exactly this value until task episodes
#: land — anything else fails closed with the stable ``revision_conflict``.
INITIAL_TASK_REVISION: Final[int] = 1

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

#: Server-profile capability slugs → the concrete capability ids they
#: enable (C03 rule 3 vocabulary). The worker's grant bootstrap makes
#: exactly the same mapping, so an auto-resolved chat context never
#: requests a capability the dispatcher would refuse.
SERVER_PROFILE_CAPABILITY_MAP: Final[dict[str, str]] = {
    "host_shell": "host.shell",
    "host.shell": "host.shell",
    "journald_logs": "host.logs",
    "host.logs": "host.logs",
    "file_read": "host.file_read",
    "host.file_read": "host.file_read",
}

#: Built-in controller-side defaults of the auto-resolved chat grant
#: set (alongside ``INTERNAL_API_CAPABILITY_IDS``): document retrieval,
#: web research, the HTTP service probe, the host journal reader, the
#: task-state agent tools (run-identity scoped: the model can only see
#: and update the task bound to its own conversation — §7) and the
#: ``tags.*`` history vocabulary (mutations stripped again by the
#: read-only exclusion set below).
BUILTIN_CHAT_CAPABILITIES: Final[tuple[str, ...]] = (
    "docs.retrieve",
    "web.search",
    "web.read",
    "service.http",
    "host.logs",
    *TASK_STATE_CAPABILITY_IDS,
    *TAGS_CAPABILITY_IDS,
)

#: Capabilities a read-only conversation strips even without a
#: registry descriptor: raw shell, every internal-API mutation, the
#: ``tags.*`` mutations and the task mutation lane (``task.read`` is
#: read-class and survives). Registered descriptors with a non-read
#: effect are excluded on top by the registry pass.
READ_ONLY_EXCLUDED_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "host.shell",
        "app.adopt",
        "app.unadopt",
        "app.upsert_map_entry",
        "app.delete_map_entry",
        "app.upsert_catalog_runbook",
        "app.delete_catalog_runbook",
        "secret.store",
        "tags.assign",
        "tags.remove",
        "tags.create",
        "tags.delete",
        TASK_PROPOSE_UPDATE_CAPABILITY,
        TASK_UPDATE_ITEM_CAPABILITY,
    }
)

#: Read-only fallback pair: both are read-class internal capabilities,
#: so the read-only context is ALWAYS a valid non-empty tuple even when
#: the resolved set carried nothing else readable.
READ_ONLY_FALLBACK_CAPABILITIES: Final[tuple[str, ...]] = (
    "session.read",
    "docs.retrieve",
)

#: The strict non-mutation instruction appended to a read-only
#: conversation's task (exact frozen wording).
READ_ONLY_INSTRUCTION: Final[str] = (
    "\n\n[READ-ONLY MODE: Write and mutate capabilities are disabled. "
    "Do NOT modify, create, delete, install, update, restart, or change "
    "any system state, files, configurations, or services. Only read, "
    "inspect, and answer questions.]"
)


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
    stored pairing when omitted). ``allowed_capabilities`` is the
    conversation's explicit parent grant request — omitted or empty
    means the agent runs with EVERY capability granted for the target
    server (grant store, server-profile capabilities, enabled plugin
    bindings and the built-in defaults) under the default ``delegated``
    mode; delegation children are clamped to its intersection.
    ``read_only`` strips every non-read capability and pins a strict
    non-mutation instruction onto the agent task. ``root_budget`` is
    the optional aggregate delegation budget (C05 rule 7): frozen on
    the run row, narrowed against the delegation gates, and enforced
    by the root ledger across the whole tree.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    server_id: str = GLOBAL_CONSULT_SERVER_ID
    message: str = Field(min_length=1, max_length=agent_run.MAX_TASK_CHARS)
    profile: str = Field(min_length=1, max_length=200)
    connection: str | None = Field(default=None, max_length=200)
    role: str = Field(default=DEFAULT_ROLE, min_length=1, max_length=MAX_ROLE_CHARS)
    rounds: int | None = Field(default=None, ge=1, le=agent_run.MAX_MAX_ROUNDS)
    mode: PolicyMode = Field(default=PolicyMode.DELEGATED)
    credential_references: list[str] = Field(default_factory=list)
    allowed_capabilities: list[str] | None = Field(default=None, max_length=64)
    read_only: bool = Field(default=False)
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


class ChatRenameBody(BaseModel):
    """Rename body: durable user-defined conversation title."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_CHAT_TITLE_CHARS)


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
    ``title`` is the user-assigned conversation name when renamed.
    """

    attachments: list[AttachmentView] = Field(default_factory=list)
    children: list[RunView] = Field(default_factory=list)
    service_tags: list[str] = Field(default_factory=list)
    session_run_id: str | None = Field(default=None)
    title: str | None = Field(default=None)


class ChatCreated(BaseModel):
    """Submit outcome: the conversation view plus the dedup verdict."""

    conversation: ConversationView
    deduplicated: bool


class ChatSteerView(BaseModel):
    """Steer/control outcome (§3): the durable control row's identity.

    ``delivery_status`` is always ``pending`` at response time: the row
    is durable BEFORE the caller is told anything, and it is consumed
    by the lease-holding run at its next safe effect boundary — the
    UI displays pending/applied distinctly and never fabricates an
    instant state.
    """

    conversation_id: uuid.UUID = Field(description="Target conversation identity")
    steering_id: uuid.UUID = Field(
        description="Durable steering/control row identity",
    )
    run_id: uuid.UUID | None = Field(
        description="Lease-holding run the control addresses, when one exists",
    )
    action: SteerAction = Field(description="steer, pause or cancel")
    client_request_id: uuid.UUID = Field(
        description="Idempotency key deduplicating repeated control submissions",
    )
    disposition: TurnDisposition = Field(
        description="created or deduplicated (same-key retransmission)",
    )
    delivery_status: Literal["pending"] = Field(
        description="Always pending at response time; the active run "
        "consumes the control at a safe effect boundary",
    )


class ChatPage(BaseModel):
    """One bounded, explicitly server-scoped list page (C01 rule 5/7)."""

    conversations: list[ConversationView]
    limit: int
    offset: int
    total: int


class ConversationResumeBody(BaseModel):
    """One state-specific operator resume request (§9; AC4).

    ``message`` is the operator's continuation instruction: the resumed
    run starts FROM the conversation's last settled checkpoint (the
    canonical transcript plus the objective episode context) — never a
    generic replay of effects. Omitted, the request is a pure state
    probe: it either names the exact structured refusal for the current
    parked state or answers ``resume_not_applicable``.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="Explicit server scope (§1 rule 2)")
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description="Continuation instruction for the resumed run",
    )

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("message must not be blank when set")
        return value


class StepClassificationBody(BaseModel):
    """One operator classification of one open effect (§9; AC4).

    The frozen vocabulary: ``confirm_applied`` (the effect IS on the
    target), ``confirm_not_applied`` (it is NOT) or ``cancel`` (stop
    without executing). Classification NEVER re-executes anything and
    never fabricates a receipt — it closes one ``outcome_unknown`` row
    from the host truth the operator attests.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1, description="The durable step row's sequence")
    verdict: Literal["confirm_applied", "confirm_not_applied", "cancel"] = Field(
        description="The operator's classification of the uncertain effect",
    )


class ConversationReconcileBody(BaseModel):
    """One bounded classification batch for a parked ``reconciling`` run."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="Explicit server scope (§1 rule 2)")
    classifications: list[StepClassificationBody] = Field(
        min_length=1,
        max_length=64,
        description="One entry per open ``outcome_unknown`` step (bounded)",
    )


class StepResolutionView(BaseModel):
    """One classification outcome exactly as the recovery owner landed it."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(description="The classified step row's sequence")
    state: str = Field(
        description="The durable step state AFTER the classification",
    )
    verdict: str = Field(description="The applied classification vocabulary value")
    wake_sent: bool = Field(
        description="Whether the parked run was woken (best effort by contract)",
    )


class ConversationReconcileView(BaseModel):
    """The honest reconcile outcome: per-step resolutions, never a guess."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    run_id: uuid.UUID = Field(description="The classified run's identity")
    run_state: str | None = Field(
        description="The facade state replayed AFTER the classifications",
    )
    results: list[StepResolutionView] = Field(
        description="Per-step resolution outcomes in request order",
    )


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


def _plugin_capability_ids(
    plugin_store: object, server_id: uuid.UUID
) -> list[str]:
    """Capability ids of every ENABLED plugin binding for this target.

    Both per-server bindings and the global sentinel binding
    (``00000000-0000-0000-0000-000000000000``) count. Any unusable
    plugin store or record degrades to fewer capabilities — the
    submission itself is never blocked by plugin configuration.
    """
    capabilities: list[str] = []
    if not isinstance(plugin_store, PluginConfigStore):
        return capabilities
    try:
        for plugin_name in plugin_store.list_plugins():
            try:
                stored_doc = plugin_store.read_config(plugin_name)
                doc = parse_plugin_config_text(stored_doc.text)
            except Exception:
                continue
            if not doc.enabled:
                continue
            try:
                for binding in plugin_store.list_bindings(plugin_name):
                    if binding.server_id in (server_id, uuid.UUID(int=0)):
                        capabilities.extend(binding.capabilities)
            except Exception:
                continue
    except Exception:
        pass
    return capabilities


def _server_profile_capability_ids(
    profile_capabilities: tuple[str, ...],
) -> list[str]:
    """Concrete capability ids enabled by one server profile.

    The worker's grant bootstrap grants both the raw slug and its
    underscore-normalized form plus the explicit ``host_shell`` /
    ``journald_logs`` mappings; this surface resolves exactly the same
    set so auto-resolved chats never exceed the dispatcher's grants.
    """
    resolved: list[str] = []
    for slug in profile_capabilities:
        resolved.append(slug)
        resolved.append(slug.replace("_", "."))
        mapped = SERVER_PROFILE_CAPABILITY_MAP.get(slug)
        if mapped is not None:
            resolved.append(mapped)
    return resolved


def _resolve_default_capabilities(
    request: Request,
    server_id: uuid.UUID,
    profile_capabilities: tuple[str, ...],
) -> list[str]:
    """Every capability granted for the target when the submit omits its
    own ``allowed_capabilities`` (auto-capabilities by default).

    Sources, unioned and deduplicated: the grant configuration store
    (entries naming this server), the canonical server profile's
    capabilities, enabled plugin bindings (per-server + global), the
    internal-API surface and the built-in controller-side defaults.
    An unusable source degrades to fewer capabilities — never an error.
    """
    resolved: list[str] = []
    grants_store = getattr(request.app.state, "grants", None)
    if isinstance(grants_store, GrantConfigStore):
        try:
            for capability_id, servers in grants_store.read().grants.items():
                if server_id in servers:
                    resolved.append(capability_id)
        except Exception:
            pass
    resolved.extend(_server_profile_capability_ids(profile_capabilities))
    resolved.extend(
        _plugin_capability_ids(
            getattr(request.app.state, "plugins", None), server_id
        )
    )
    resolved.extend(INTERNAL_API_CAPABILITY_IDS)
    resolved.extend(BUILTIN_CHAT_CAPABILITIES)
    return list(_dedup_preserving_order(resolved))


def _read_only_capabilities(
    request: Request, capabilities: list[str]
) -> list[str]:
    """Strip every non-read capability for a read-only conversation.

    The static mutation vocabulary is always excluded; registry-known
    descriptors with ``effect_class != "read"`` are excluded too (an
    absent registry excludes nothing extra). ``session.read`` /
    ``docs.retrieve`` are ensured last so the ``ExecutionContext``
    always carries a valid, non-empty read-only capability tuple.
    """
    excluded = set(READ_ONLY_EXCLUDED_CAPABILITIES)
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            for descriptor in registry.list_capabilities():
                if descriptor.effect_class != "read":
                    excluded.add(descriptor.identifier)
        except Exception:
            pass
    filtered = [
        capability_id
        for capability_id in capabilities
        if capability_id not in excluded
    ]
    filtered.extend(READ_ONLY_FALLBACK_CAPABILITIES)
    return list(_dedup_preserving_order(filtered))


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
        "allowed_capabilities": list(body.allowed_capabilities or []),
        "read_only": body.read_only,
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

    Delegates to the ONE producer formatter (``mai.agents.context.
    format_service_map_section``): identity, category, description,
    server custom instructions and the operational runbook per matched
    entry — the agent's grounded service knowledge for the tagged
    conversation.
    """
    return format_service_map_section(entries)


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
    submission either way). A read-only conversation pins the frozen
    non-mutation instruction onto the task; the message tail yields —
    minimally — so the enriched task still fits the producer's bound.
    """
    read_only_suffix = READ_ONLY_INSTRUCTION if body.read_only else ""
    task = body.message + read_only_suffix
    if len(task) > agent_run.MAX_TASK_CHARS:
        task = (
            body.message[: agent_run.MAX_TASK_CHARS - len(read_only_suffix)]
            + read_only_suffix
        )
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
    title: str | None = None,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    requested_phase: str | None = None,
    task_action: str | None = None,
) -> None:
    """Append the ``chat.submitted`` submission record (one transaction).

    Commits with the facade row and its run-level intent events (the
    shared writer appended those first), so the submission record exists
    exactly when the durable intent does. Seq follows the documented
    whole-stream discipline (``MAX(seq)+1``; the storage
    ``UNIQUE (run_id, seq)`` is the backstop). ``service_tags`` (the
    immutable ``@service`` chips) are recorded on the event; readers
    tolerate an absent key (submissions predating the field). The
    canonical-turn seam records its identity binding alongside
    (``conversation_id``/``message_id``) plus the advisory episode
    fields (``requested_phase``/``task_action``) — provenance metadata
    for the downstream intake/steering slices, never permission.
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
    if title is not None:
        data["title"] = title
    if conversation_id is not None:
        data["conversation_id"] = str(conversation_id)
    if message_id is not None:
        data["message_id"] = str(message_id)
    if requested_phase is not None:
        data["requested_phase"] = requested_phase
    if task_action is not None:
        data["task_action"] = task_action
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


async def _record_chat_renamed(
    session: AsyncSession,
    *,
    run_id: str,
    title: str,
    now: datetime,
) -> None:
    """Append a ``chat.renamed`` event and touch ``job_runs.updated_at``."""
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
            "type": CHAT_RENAMED_EVENT_TYPE,
            "v": EVENT_PAYLOAD_VERSION,
            "data": json.dumps({"title": title}),
        },
    )
    await session.execute(
        text("UPDATE job_runs SET updated_at = :now WHERE run_id = :r"),
        {"now": now, "r": uuid.UUID(run_id)},
    )


async def _resolve_chat_title(
    session: AsyncSession,
    run_id: str,
    submission_data: Mapping[str, object] | None = None,
) -> str | None:
    """Resolve the latest user-defined conversation title across session turns."""
    current_id: str | None = run_id
    current_sub: Mapping[str, object] | None = submission_data
    seen: set[str] = set()
    for _ in range(16):
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        try:
            run_uuid = uuid.UUID(current_id)
        except ValueError:
            break
        rename_row = (
            (
                await session.execute(
                    text(
                        "SELECT data FROM job_events WHERE run_id = :r "
                        f"AND type = '{CHAT_RENAMED_EVENT_TYPE}' ORDER BY seq "
                        "DESC LIMIT 1"
                    ),
                    {"r": run_uuid},
                )
            )
            .mappings()
            .first()
        )
        if rename_row is not None and isinstance(rename_row["data"], Mapping):
            raw_title = rename_row["data"].get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                return raw_title.strip()
        if current_sub is None:
            sub_row = (
                (
                    await session.execute(
                        text(
                            "SELECT data FROM job_events WHERE run_id = :r "
                            f"AND type = '{CHAT_SUBMITTED_EVENT_TYPE}' ORDER BY seq "
                            "DESC LIMIT 1"
                        ),
                        {"r": run_uuid},
                    )
                )
                .mappings()
                .first()
            )
            current_sub = (
                sub_row["data"]
                if sub_row is not None and isinstance(sub_row["data"], Mapping)
                else None
            )
        if isinstance(current_sub, Mapping):
            sub_title = current_sub.get("title")
            if isinstance(sub_title, str) and sub_title.strip():
                return sub_title.strip()
            next_session = current_sub.get("session_run_id")
            current_id = (
                next_session
                if isinstance(next_session, str) and next_session
                else None
            )
            current_sub = None
        else:
            break
    return None


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
# Modern canonical turn submission (act_CONTRACTS §3; 05-turn-submit)
# ---------------------------------------------------------------------------


def _conversation_lock_key(conversation_id: uuid.UUID) -> int:
    """The per-conversation submit serialization key (C6).

    A transaction-scoped Postgres advisory lock keyed by the
    conversation's UUID serializes the whole admission window
    (create-when-missing / dedup / writer-lease admission / intent /
    run attachment) across concurrent submitters and processes. Distinct
    conversations map to distinct bigint keys; the astronomically
    unlikely birthday collision would only add spurious serialization,
    never incorrectness.
    """
    return conversation_id.int & ((1 << 63) - 1)


def _resolve_submit_profile(request: Request, profile: str | None) -> str:
    """The run's pinned model profile name (C06 rule 1).

    An explicit ``profile`` rides exactly as asked; an omitted one pins
    the controller's stored model selection AT SUBMIT TIME, so the run
    row and the durable intent record exactly which configuration the
    turn admitted (an administrator edit can never move a bound run).
    A missing store or stored selection fails closed (``503``), never a
    guessed profile name.
    """
    if profile is not None:
        return profile
    store = getattr(request.app.state, "models", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "model configuration service is unavailable",
        )
    try:
        return store.get_selection().profile
    except ModelStoreError:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "no model selection stored",
        ) from None


def _modern_turn_document(
    *,
    run_id: str,
    server_id: str,
    context: ExecutionContext,
    profile: str,
    task: str,
) -> dict[str, object]:
    """The frozen agent-run intent document for one canonical turn.

    The ONE producer's shape with the modern seam's fixed vocabulary:
    the default role, no connection override, no delegation children and
    no attachment enrichment are expressible here — the modern submit
    admits exactly the §3 body. ``task`` is the PROJECTED transcript
    content (C2 ordering: raw-secret exclusion precedes persistence and
    model forwarding), so the durable intent can never carry a secret
    the store itself refused.
    """
    return {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": server_id,
        "context": context.to_json(),
        "role": DEFAULT_ROLE,
        "task": task,
        "connection": None,
        "profile": profile,
    }


def _modern_submission_document(
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    content_fingerprint: str,
    profile: str,
    requested_phase: str | None,
    task_action: str | None,
    objective_id: uuid.UUID | None = None,
    mode: str | None = None,
) -> dict[str, object]:
    """The modern whole-submission digest input (the dedup identity).

    Covers everything the turn asked for: the canonical conversation and
    message identities, the PROJECTED content fingerprint, the pinned
    profile, the advisory episode fields and the v2 objective binding —
    a retransmission under the same ``client_request_id`` must carry all
    of them identically (C4); any difference fails closed as
    ``client_request_conflict``.
    """
    return {
        "v": 1,
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
        "content_fingerprint": content_fingerprint,
        "profile": profile,
        "requested_phase": requested_phase,
        "task_action": task_action,
        "mode": mode,
        **(
            {"objective_id": str(objective_id)}
            if objective_id is not None
            else {}
        ),
    }


def _v2_turn_document(
    *,
    run_id: str,
    server_id: str,
    objective_id: str,
    context: ExecutionContext,
    profile: str,
    task: str,
) -> dict[str, object]:
    """The frozen ``agent.run_v2`` intent document for one canonical turn.

    The runtime-v2.md §2/§14 shape the ONE producer validates strictly:
    ``payload_version`` 2, the explicit ``objective_id`` binding (§3.3)
    and the identity-matched execution context. ``task`` is the PROJECTED
    transcript content (C2 ordering: raw-secret exclusion precedes
    persistence and model forwarding).
    """
    return {
        "payload_version": agent_run_v2.RUN_PAYLOAD_VERSION_V2,
        "run_id": run_id,
        "server_id": server_id,
        "objective_id": objective_id,
        "context": context.to_json(),
        "role": DEFAULT_ROLE,
        "task": task,
        "connection": None,
        "profile": profile,
    }


class ObjectiveTurnRejection(ValueError):
    """One value-free objective-binding rejection (C01 envelope input).

    ``status`` carries the HTTP class (404 scope-blind not-found /
    409 conflict); ``code``/``fields``/``detail`` ride the envelope
    verbatim. Nothing about another scope's identities leaks (C8).
    """

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.fields = fields


async def _resolve_turn_objective(
    factory: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    server_uuid: uuid.UUID,
    requested_objective_id: uuid.UUID | None,
    *,
    is_create: bool,
    client_request_id: uuid.UUID,
) -> tuple[uuid.UUID | None, bool]:
    """Resolve the turn's effective v2 objective binding (§3.3; AC1).

    A RETRANSMISSION resolves first and exactly: the key's own run's
    durable binding reconstructs its originally admitted document —
    correct under any rollover history (the replay digest must match the
    RECORDED admission, never the conversation's current episode).

    Explicit wins: a requested ``objective_id`` is scope-checked against
    the initiating server (a foreign-scope reference fails closed) and
    must be ``active``. A conversation ALREADY bound to an objective
    stays v2: the binding probe keeps follow-up turns on the same
    episode (an in-flight v2 conversation is never downgraded to a
    legacy run). An explicit request that disagrees with the
    conversation's bound episode, a settled episode, or a foreign-scope
    reference raises :class:`ObjectiveTurnRejection`.

    The DIRECT-AGENT DEFAULT: a canonical turn with NO explicit and NO
    active binding runs v2 too. ``(turn_objective_id, auto_episode)``
    carries the decision — ``auto_episode`` marks that the admission
    must START a new episode inside its transaction (a fresh
    create-route conversation, or a follow-up whose current episode is
    terminal: the rollover keeps the old history untouched). A
    follow-up on an UNBOUND conversation stays legacy — historical
    legacy conversations and their drain/read semantics remain intact.
    """
    bound_to_run = await _arun_bound_objective(factory, client_request_id)
    if bound_to_run is not None:
        return bound_to_run, False
    bound = await _abound_objective(factory, conversation_id)
    if requested_objective_id is not None:
        try:
            detail = await aget_objective_scope_checked(
                factory, requested_objective_id, server_id=server_uuid
            )
        except ObjectiveContractError as exc:
            if (
                ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value
                in set(exc.fields.values())
            ):
                raise ObjectiveTurnRejection(
                    HTTP_404_NOT_FOUND,
                    ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
                    "no objective in the requesting server scope",
                ) from None
            raise ObjectiveTurnRejection(
                HTTP_409_CONFLICT,
                ObjectiveErrorCode.INVALID_PAYLOAD.value,
                "objective contract rejected",
                fields=exc.fields,
            ) from None
        if detail.status is not ObjectiveStatus.ACTIVE:
            raise ObjectiveTurnRejection(
                HTTP_409_CONFLICT,
                ObjectiveErrorCode.INVALID_STATUS_TRANSITION.value,
                "the objective episode is settled; new runs bind active "
                "objectives only",
                fields={
                    "objective_id": (
                        ObjectiveErrorCode.INVALID_STATUS_TRANSITION.value
                    )
                },
            )
        if bound is not None and bound[0] != requested_objective_id:
            raise ObjectiveTurnRejection(
                HTTP_409_CONFLICT,
                ObjectiveErrorCode.INVALID_PAYLOAD.value,
                "the conversation is bound to a different objective episode",
                fields={"objective_id": ObjectiveErrorCode.INVALID_PAYLOAD.value},
            )
        return requested_objective_id, False
    if bound is not None:
        bound_id, bound_status = bound
        if bound_status is ObjectiveStatus.ACTIVE:
            return bound_id, False
        # The bound episode is terminal: the conversation's next run
        # rolls over into a NEW objective episode (old history unchanged).
        return None, True
    if is_create:
        # A canonical NEW chat selects the v2 runtime by default; its
        # episode is created inside the admission transaction.
        return None, True
    return None, False


async def _abound_objective(
    factory: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
) -> tuple[uuid.UUID, ObjectiveStatus] | None:
    """The objective episode this conversation's runs CURRENTLY bind to.

    The conversation's LIVE episode wins: any active binding (the newest
    one, submit order descending) — the earliest terminal bindings of
    rolled-over episodes never hijack selection. With no active binding
    the NEWEST binding rides along with its CURRENT status, so the
    direct-agent default can distinguish a live episode from a terminal
    one (the rollover decision).
    """
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT r.objective_id, o.status FROM objective_runs r "
                    "JOIN conversation_runs cr ON cr.run_id = r.run_id "
                    "JOIN objectives o ON o.objective_id = r.objective_id "
                    "WHERE cr.conversation_id = :cid "
                    "ORDER BY (o.status = 'active') DESC, "
                    "cr.created_at DESC, r.run_id DESC LIMIT 1"
                ),
                {"cid": conversation_id},
            )
        ).first()
    if row is None:
        return None
    return uuid.UUID(str(row[0])), ObjectiveStatus(str(row[1]))


async def _arun_bound_objective(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> uuid.UUID | None:
    """The objective THIS run bound at its admission (exact).

    Rollover-proof retransmission identity: a replayed key rebuilds its
    originally admitted v2 document from its own durable binding, never
    from the conversation's current episode.
    """
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT objective_id FROM objective_runs "
                    "WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
        ).first()
    if row is None:
        return None
    return uuid.UUID(str(row[0]))


def _objective_turn_conflict(
    request: Request, exc: ObjectiveContractError
) -> Response:
    """Map a submit-time binding rejection onto the C01 envelope."""
    fields = exc.fields
    codes = set(fields.values())
    if ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value in codes:
        return _error(
            request,
            HTTP_404_NOT_FOUND,
            ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
            "no objective in the requesting server scope",
            fields=fields,
        )
    return _error(
        request,
        HTTP_409_CONFLICT,
        next(iter(codes)) if codes else "invalid_payload",
        "objective binding rejected",
        fields=fields,
    )


def _not_found_conversation(request: Request) -> Response:
    """The scope-blind 404 (C8): existence of another server's conversation
    never leaks through the error channel."""
    return _error(
        request,
        HTTP_404_NOT_FOUND,
        ConversationErrorCode.CONVERSATION_NOT_FOUND.value,
        "no conversation in the requesting server scope",
    )


def _conversation_contract_error(
    request: Request, exc: ConversationContractError
) -> Response:
    """Map the store's closed rejections onto the C01 envelope.

    The stable codes ride verbatim (``conversation_not_found`` → 404;
    the 409 vocabulary ``client_request_conflict`` /
    ``conversation_busy`` / ``history_reconstruction_incomplete``);
    every message is value-free by contract, so nothing about another
    scope's identities can leak through the error channel (C8).
    """
    fields = exc.fields
    codes = set(fields.values())
    if ConversationErrorCode.CONVERSATION_NOT_FOUND.value in codes:
        return _error(
            request,
            HTTP_404_NOT_FOUND,
            ConversationErrorCode.CONVERSATION_NOT_FOUND.value,
            "no conversation in the requesting server scope",
            fields=fields,
        )
    if ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value,
            "idempotency key retransmitted with changed content",
            fields=fields,
        )
    if ConversationErrorCode.CONVERSATION_BUSY.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            ConversationErrorCode.CONVERSATION_BUSY.value,
            "another writer holds the conversation lease",
            fields=fields,
        )
    if ConversationErrorCode.HISTORY_RECONSTRUCTION_INCOMPLETE.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            ConversationErrorCode.HISTORY_RECONSTRUCTION_INCOMPLETE.value,
            "history reconstruction is incomplete; explicit user scope "
            "confirmation is required before new mutations",
            fields=fields,
        )
    return _conflict(request, "conversation contract rejected")


def _turn_response(response: ConversationTurnSubmitResponse) -> Response:
    """One admitted turn at its disposition's honest status (201/200/202)."""
    return Response(
        status_code=response.http_status,
        content=response.model_dump_json(),
        media_type="application/json",
    )


async def _submit_canonical_turn(
    request: Request,
    payload: ConversationTurnSubmitRequest,
    *,
    conversation_id: uuid.UUID | None,
) -> Response:
    """Admit one user turn through the canonical conversation store (§3).

    ONE admission window per conversation, serialized by the advisory
    lock: create-when-missing, scope/completeness gates, the canonical
    message insert (dedup on ``(conversation_id, client_request_id)``),
    the one-writer lease admission, the durable enqueue intent
    (``job_runs`` row + ``run.submitted`` + ``chat.submitted``, the
    shared submit-intent writer) and the idempotent run attachment. The
    engine enqueue FOLLOWS the commit exactly like the legacy seam: a
    crash leaves a re-armable gap (the retry re-anchors and re-enqueues
    idempotently), never a half-recorded turn (C4). The model-facing
    task text is the PROJECTED store content, never the raw payload.

    DIRECT-AGENT DEFAULT (runtime-v2 §3.3): a canonical turn with no
    explicit and no actively bound objective runs ``agent.run_v2`` — the
    admission starts its objective episode INSIDE this transaction
    through the tx-scoped objective-store seams
    (:func:`mai.agents.objective_store.acreate_objective_tx` /
    ``abind_run_tx``), keyed by ``client_request_id`` and scoped exactly
    to the selected server (the zero UUID for General Consult). A
    conversation bound to a terminal episode rolls over into a new
    episode on its next run; historical legacy conversations (no
    binding) stay legacy. No second request, no intake planner, no
    orphan on failure: the episode commits and rolls back with the run.

    ``conversation_id is None`` (the create route) derives the new
    conversation's identity from ``client_request_id`` — the idempotency
    key therefore names at most ONE conversation, and a retry after any
    crash recovers the exact conversation/message/run triple without a
    second row of any kind. Follow-up runs record
    ``session_run_id=<conversation_id>`` so the legacy surface keeps
    collapsing the conversation into ONE history entry (C1
    compatibility): the first run of a conversation shares its id.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)

    client_request_id = payload.client_request_id
    target = client_request_id if conversation_id is None else conversation_id
    server_uuid = payload.server_id
    server_id = str(server_uuid)
    general_consult = server_uuid == uuid.UUID(int=0)
    if general_consult:
        # General Consult has no target host: no canonical-profile
        # binding (revisions pin to 1), exactly like the legacy seam.
        profile_revision = 1
        prompt_revision = 1
        profile_capability_ids: tuple[str, ...] = ()
    else:
        stored = _canonical_profile(request, server_uuid)
        profile = parse_server_profile_text(stored.text)
        profile_capability_ids = profile.capabilities
        profile_revision = stored.revision
        prompt_revision = profile.prompt_revision
    # The §3 body expresses no explicit grant list, no children and no
    # attachments: the turn runs with EVERY capability granted for the
    # target (the documented auto-resolution) and no delegation.
    capabilities = _resolve_default_capabilities(
        request, server_uuid, profile_capability_ids
    )
    # Fails closed before ANY store write when the model configuration
    # cannot pin a profile (503, never a guessed profile name).
    model_profile = _resolve_submit_profile(request, payload.profile)
    # CAS gate BEFORE any store write: today's implicit task revision is
    # the initial one (task episodes arrive with migration 0009); a pin
    # against anything else is a stale request, structured 409, never
    # last-writer-wins (§1).
    if (
        payload.expected_revision is not None
        and payload.expected_revision != INITIAL_TASK_REVISION
    ):
        return _error(
            request,
            HTTP_409_CONFLICT,
            TaskErrorCode.REVISION_CONFLICT.value,
            "expected task revision does not match the current revision",
            fields={"expected_revision": TaskErrorCode.REVISION_CONFLICT.value},
        )
    try:
        context = ExecutionContext(
            principal=hash_token(token),
            server_id=server_uuid,
            run_id=client_request_id,
            parent_scope=None,
            profile_revision=str(profile_revision),
            prompt_revision=str(prompt_revision),
            credential_references=(),
            allowed_capabilities=capabilities,
            mode=(
                PolicyMode(payload.mode)
                if payload.mode is not None
                else PolicyMode.DELEGATED
            ),
        )
    except PolicyError as exc:
        return _validation_error(request, "context", exc.detail)

    factory = _sessions(request)
    now = datetime.now(timezone.utc)
    # Runtime v2 (§3.3; AC1): resolve the turn's objective binding BEFORE
    # any store write — scope-checked, active-episode checked,
    # conversation-stability checked. ``None`` with ``auto_episode`` marks
    # the DIRECT-AGENT DEFAULT: the admission starts a new v2 episode; a
    # resolved id selects ``agent.run_v2`` for THIS run; ``None`` without
    # an auto episode is the legacy handler path (historical legacy
    # conversations stay legacy).
    try:
        turn_objective_id, auto_episode = await _resolve_turn_objective(
            factory,
            target,
            server_uuid,
            payload.objective_id,
            is_create=conversation_id is None,
            client_request_id=client_request_id,
        )
    except ObjectiveTurnRejection as rejection:
        return _error(
            request,
            rejection.status,
            rejection.code,
            rejection.detail,
            fields=rejection.fields,
        )
    requested_phase = (
        payload.requested_phase.value if payload.requested_phase is not None else None
    )
    try:
        async with factory() as guard, guard.begin():
            # C6: the WHOLE admission window below serializes per
            # conversation (create / dedup / lease admission / intent /
            # attach) across concurrent submitters and processes.
            await guard.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _conversation_lock_key(target)},
            )
            if conversation_id is None:
                try:
                    await aget_conversation(factory, target, server_id=server_uuid)
                except ConversationContractError:
                    try:
                        await acreate_conversation(
                            factory, server_id=server_uuid, conversation_id=target
                        )
                    except IntegrityError:
                        # The client-chosen id is bound to ANOTHER server's
                        # scope: the scope-blind 404, never a leak (C8).
                        return _not_found_conversation(request)
            conversation = await aget_conversation(
                factory, target, server_id=server_uuid
            )
            assert_history_available(
                conversation.history_completeness, user_confirmed=False
            )
            active = await aactive_run(factory, target, server_id=server_uuid)
            if (
                active is not None
                and active.run_id != client_request_id
                and await _replayed_state(lifecycle, str(active.run_id))
                in (
                    RunState.SUCCEEDED.value,
                    RunState.FAILED.value,
                    RunState.CANCELLED.value,
                )
            ):
                # The lease-holding run is TERMINAL: the conversation's
                # writer lease is stale bookkeeping, so it releases here —
                # a finished conversation can continue (C1). A run that
                # merely sits queued/reconciling keeps holding the lease.
                await arelease_run(
                    factory,
                    conversation_id=target,
                    server_id=server_uuid,
                    run_id=active.run_id,
                )
                active = None
            lease_holder = (
                None
                if active is None or active.run_id == client_request_id
                else active.run_id
            )
            # One writer/executor lease at a time (§1): an exclusive new
            # episode cannot start under a live lease — rejected BEFORE any
            # store write. A retry of the lease-holding turn itself bypasses
            # the gate and re-arms below (C4).
            assert_writer_admission(
                lease_holder, follow_up=payload.task_action != "new_task"
            )
            turn = await asubmit_turn(
                factory,
                conversation_id=target,
                server_id=server_uuid,
                client_request_id=client_request_id,
                message=payload.message,
                source_kind=MessageSourceKind.API,
            )
            if lease_holder is not None:
                # Active-run delivery (07-steering): the canonical
                # message row is durable (asubmit_turn committed); the
                # steering row is the durable DELIVERY identity the
                # lease-holding run consumes exactly once at its next
                # safe effect boundary — appended BEFORE the caller is
                # told anything, so a crash between append and
                # consumption recovers through the run's own poll
                # (C4/C5). Same idempotency key as the message: a
                # retransmitted submit replays onto the SAME row.
                await aenqueue_steering(
                    factory,
                    conversation_id=target,
                    server_id=server_uuid,
                    action=SteerAction.STEER,
                    client_request_id=client_request_id,
                    content=turn.message.content,
                    run_id=lease_holder,
                )
                return _turn_response(
                    ConversationTurnSubmitResponse(
                        conversation_id=target,
                        message_id=turn.message.message_id,
                        task_id=conversation.active_task_id,
                        run_id=None,
                        revision=INITIAL_TASK_REVISION,
                        disposition=TurnDisposition.QUEUED_FOLLOWUP,
                    )
                )
            objective_bound_here = False
            if turn_objective_id is None and auto_episode:
                # Direct-agent default (runtime-v2 §3.3): this turn starts
                # its own objective episode INSIDE the ONE admission
                # transaction — the tx-scoped objective-store seams keep
                # the reviewed validation, projection, (server_id,
                # client_request_id) idempotency and objective.created
                # event semantics, so a failure rolls the objective back
                # TOGETHER with its run (no orphan, no second request,
                # no intake planner). The original request is the
                # PROJECTED persisted user content; the initial scope is
                # exactly the selected server (the zero UUID for General
                # Consult — no host authority expansion). Retransmissions
                # never re-create: an existing job_runs row means the
                # ORIGINAL submission already decided this run's runtime,
                # so the digest below replays verbatim (the terminal-
                # episode rollover keeps the old binding untouched — old
                # history unchanged).
                already_admitted = (
                    await guard.execute(
                        text("SELECT 1 FROM job_runs WHERE run_id = :rid"),
                        {"rid": str(client_request_id)},
                    )
                ).first()
                if already_admitted is None:
                    episode = await acreate_objective_tx(
                        guard,
                        server_id=server_uuid,
                        original_request=turn.message.content,
                        scope_server_ids=[server_uuid],
                        client_request_id=client_request_id,
                    )
                    turn_objective_id = episode.objective_id
                    objective_bound_here = True
                else:
                    # Retransmission (the pre-lock probes raced the original
                    # commit): the key's OWN run binding reconstructs the
                    # originally admitted v2 document verbatim — exact
                    # under any rollover history (C4).
                    run_bound = await _arun_bound_objective(
                        factory, client_request_id
                    )
                    if run_bound is not None:
                        turn_objective_id = run_bound
            if turn_objective_id is not None:
                # Runtime v2 (§2/§3.3): the v2 intent selects the v2
                # handler explicitly and carries the objective binding;
                # the ONE v2 producer validates it strictly.
                engine_document = _v2_turn_document(
                    run_id=str(client_request_id),
                    server_id=server_id,
                    objective_id=str(turn_objective_id),
                    context=context,
                    profile=model_profile,
                    task=turn.message.content,
                )
                try:
                    agent_run_v2.parse_agent_run_intent_v2(
                        _canonical(engine_document)
                    )
                except agent_run.AgentRunError as exc:
                    return _validation_error(request, "message", exc.detail)
                turn_handler = agent_run_v2.HANDLER_NAME
            else:
                engine_document = _modern_turn_document(
                    run_id=str(client_request_id),
                    server_id=server_id,
                    context=context,
                    profile=model_profile,
                    task=turn.message.content,
                )
                try:
                    agent_run.parse_agent_run_intent(_canonical(engine_document))
                except agent_run.AgentRunError as exc:
                    return _validation_error(request, "message", exc.detail)
                turn_handler = agent_run.HANDLER_NAME
            payload_json = _canonical(engine_document)
            engine_digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
            submission_digest = hashlib.sha256(
                _canonical(
                    _modern_submission_document(
                        conversation_id=target,
                        message_id=turn.message.message_id,
                        content_fingerprint=turn.message.content_fingerprint,
                        profile=model_profile,
                        requested_phase=requested_phase,
                        task_action=payload.task_action,
                        objective_id=turn_objective_id,
                        mode=payload.mode,
                    )
                ).encode("ascii")
            ).hexdigest()
            created = await _record_submit_intent(
                guard,
                run_id=str(client_request_id),
                server_id=server_id,
                profile_revision=profile_revision,
                prompt_revision=prompt_revision,
                model_profile=model_profile,
                requested_grants=sorted(capabilities),
                digest=engine_digest,
                label=next(iter(sorted(capabilities)), model_profile),
                now=now,
            )
            if objective_bound_here and turn_objective_id is not None:
                # The automatic episode's durable run↔objective binding
                # commits INSIDE the admission transaction (the job_runs
                # row above satisfies the binding FK): a failure rolls
                # the objective, its binding, the intent and the
                # submission record back together — no orphan. The
                # worker-side bind stays the idempotent backstop.
                await abind_run_tx(
                    guard,
                    turn_objective_id,
                    server_id=server_uuid,
                    run_id=client_request_id,
                    initiating_server_id=server_uuid,
                    actor_kind=ActorKind.OPERATOR,
                )
            if created:
                # Legacy-surface compatibility (C1): follow-up runs chain
                # to the conversation's FIRST run (which shares the
                # conversation id) so Chat History shows one entry.
                await _record_chat_submission(
                    guard,
                    run_id=str(client_request_id),
                    digest=submission_digest,
                    handler=turn_handler,
                    attachments=[],
                    child_count=0,
                    service_tags=[],
                    now=now,
                    session_run_id=(
                        str(target) if target != client_request_id else None
                    ),
                    conversation_id=target,
                    message_id=turn.message.message_id,
                    requested_phase=requested_phase,
                    task_action=payload.task_action,
                )
            # The writer-lease hand-over commits INSIDE the serialized
            # window: a concurrent submitter can never observe the gap
            # between intent and lease (two active runs are unreachable).
            await aattach_run(
                factory,
                conversation_id=target,
                server_id=server_uuid,
                run_id=client_request_id,
            )
    except ConversationContractError as exc:
        return _conversation_contract_error(request, exc)
    except ObjectiveContractError as exc:
        # The automatic episode's tx-scoped create/bind failed closed:
        # the admission transaction already rolled the objective, its
        # binding, the run intent and the submission record back — map
        # the rejection, never leave an orphan (C01 envelope).
        return _objective_turn_conflict(request, exc)
    if created and turn_objective_id is not None and not objective_bound_here:
        # §3.3 (AC1): the durable run↔objective binding lands at SUBMIT
        # under the operator's authority — the truthful marker for reads
        # and the drain inventory exists before the worker starts; the
        # worker-side bind is the idempotent backstop (same seam).
        try:
            await abind_run(
                factory,
                turn_objective_id,
                server_id=server_uuid,
                run_id=client_request_id,
                initiating_server_id=server_uuid,
                actor_kind=ActorKind.OPERATOR,
            )
        except ObjectiveContractError as exc:
            return _objective_turn_conflict(request, exc)
    if not created:
        # Duplicate submit: same key must carry the SAME turn (C4).
        events = await lifecycle.event_stream(str(client_request_id))
        recorded = next(
            (e for e in events if e.type == CHAT_SUBMITTED_EVENT_TYPE), None
        )
        recorded_digest = (
            recorded.data.get("submission_sha256")
            if recorded is not None and isinstance(recorded.data, Mapping)
            else None
        )
        if recorded_digest != submission_digest:
            return _error(
                request,
                HTTP_409_CONFLICT,
                ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value,
                "idempotency key is already used by a different submission",
                fields={
                    "client_request_id": (
                        ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value
                    )
                },
            )
        # Crash-gap healing: a committed facade row without its submit
        # edge re-anchors idempotently (the intent-level resume).
        if await lifecycle.current_state(str(client_request_id)) is None:
            await lifecycle.record_transition(
                str(client_request_id), RunState.QUEUED
            )
    try:
        await asyncio.to_thread(
            recovery.client.enqueue_run,
            str(client_request_id),
            turn_handler,
            payload_json,
        )
    except RunIdInUseError:
        if created:
            # Unreachable in order (the row precedes every enqueue), but a
            # lost race must surface structured, never as a 500 (C05 r5).
            return _error(
                request,
                HTTP_409_CONFLICT,
                ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value,
                "idempotency key is already used by a different submission",
                fields={
                    "client_request_id": (
                        ConversationErrorCode.CLIENT_REQUEST_CONFLICT.value
                    )
                },
            )
    except (dbos_error.DBOSException, OSError):
        return _error(
            request,
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "jobs engine is unavailable",
        )
    disposition = (
        TurnDisposition.CREATED if created else TurnDisposition.DEDUPLICATED
    )
    return _turn_response(
        ConversationTurnSubmitResponse(
            conversation_id=target,
            message_id=turn.message.message_id,
            task_id=conversation.active_task_id,
            run_id=client_request_id,
            revision=INITIAL_TASK_REVISION,
            disposition=disposition,
        )
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


def _tag_preview_scopes(
    row: Mapping[str, object],
    children_rows: list[Mapping[str, object]],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Group the runs to preview by each run's OWN server scope.

    Every previewed row keeps its durable server pin (a v2 delegation
    child carries its own initiating server, §7.2), so one parent-scoped
    query would silently miss a cross-server child; the merge covers the
    parent and every child with one grouped query per distinct server.
    """
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for item in (row, *children_rows):
        grouped.setdefault(uuid.UUID(str(item["server_id"])), []).append(
            uuid.UUID(str(item["run_id"]))
        )
    return grouped


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
        submission_data = (
            submission_event["data"]
            if submission_event is not None
            and isinstance(submission_event["data"], Mapping)
            else None
        )
        title = await _resolve_chat_title(session, identity, submission_data)
    child_states = await _child_states(factory, children_rows)
    parent_run_id = uuid.UUID(identity)
    # Tag previews are server-scoped reads: every run is previewed under
    # its OWN server scope and the pages merged — a v2 delegation child is
    # pinned to its own initiating server (§7.2), which may differ from
    # the parent's, and one parent-scoped query would miss it.
    previews: dict[uuid.UUID, EntityTagPreview] = {}
    for scope_server_id, preview_ids in _tag_preview_scopes(
        row, children_rows
    ).items():
        previews.update(
            await apreview_job_tags(factory, preview_ids, server_id=scope_server_id)
        )
    attachments: list[AttachmentView] = []
    service_tags: list[str] = []
    session_run_id: str | None = None
    if submission_data is not None:
        for item in submission_data.get("attachments") or []:
            if isinstance(item, Mapping):
                attachments.append(
                    AttachmentView(
                        server_id=str(item["server_id"]),
                        op_id=str(item["op_id"]),
                        sha256=str(item["sha256"]),
                        bytes=int(item["bytes"]),  # type: ignore[arg-type]
                    )
                )
        raw_tags = submission_data.get("service_tags") or []
        if isinstance(raw_tags, list):
            service_tags = [str(tag) for tag in raw_tags if isinstance(tag, str)]
        raw_session_run = submission_data.get("session_run_id")
        if isinstance(raw_session_run, str) and raw_session_run:
            session_run_id = raw_session_run
    return ConversationView(
        **_run_view(
            row,
            state,
            tag_preview=_preview_view(previews.get(parent_run_id)),
        ).model_dump(),
        attachments=attachments,
        children=[
            _run_view(
                child,
                child_states.get(str(child["run_id"])),
                tag_preview=_preview_view(
                    previews.get(uuid.UUID(str(child["run_id"])))
                ),
            )
            for child in children_rows
        ],
        service_tags=service_tags,
        session_run_id=session_run_id,
        title=title,
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
        profile_capability_ids: tuple[str, ...] = ()
    else:
        stored = _canonical_profile(request, server_uuid)
        profile = parse_server_profile_text(stored.text)
        profile_capability_ids = profile.capabilities
        profile_revision = stored.revision
        prompt_revision = profile.prompt_revision

    try:
        resolved_attachments = _resolve_attachments(
            request.app.state.env, server_uuid, payload.attachments
        )
    except AttachmentRejected as exc:
        return _validation_error(request, exc.field, exc.detail)

    if payload.allowed_capabilities:
        # Explicit request: the submitted list rides exactly as asked,
        # unioned with the internal-API tool surface (app.discover,
        # app.list_adopted, app.adopt, app.unadopt, app.upsert_map_entry,
        # app.delete_map_entry, session.read) and controller-side web
        # research tools (web.search, web.read) plus enabled plugin
        # bindings, so the chat agent can always operate Machina services
        # and consult external docs.
        capabilities = _dedup_preserving_order(
            list(payload.allowed_capabilities)
            + list(INTERNAL_API_CAPABILITY_IDS)
            + ["web.search", "web.read"]
            + _plugin_capability_ids(
                getattr(request.app.state, "plugins", None), server_uuid
            )
        )
    else:
        # Auto-capabilities by default: everything granted for the target.
        capabilities = _resolve_default_capabilities(
            request, server_uuid, profile_capability_ids
        )
    if payload.read_only:
        capabilities = _read_only_capabilities(request, capabilities)
    # Scoped per-plugin credentials (C4/H2/M7): plugin credential_refs
    # deliberately stay OUT of the chat context's credential_references.
    # A native plugin's secrets resolve on demand in the worker's
    # _composite_executor only when one of that plugin's own capability
    # dispatches — a missing OP_SERVICE_ACCOUNT_TOKEN must never break
    # host.shell, web.search or any other capability in the same chat.
    effective_cred_refs = _dedup_preserving_order(
        list(payload.credential_references)
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
            credential_references=tuple(effective_cred_refs),
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
                label=next(iter(sorted(capabilities)), payload.profile),
                now=now,
                root_budget=root_budget,
            )
            if created:
                inherited_title = (
                    await _resolve_chat_title(session, session_run_id)
                    if session_run_id is not None and session_run_id != run_id
                    else None
                )
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
                    title=inherited_title,
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


@router.post(
    "/conversations",
    status_code=HTTP_201_CREATED,
    response_model=ConversationTurnSubmitResponse,
    summary="Create a conversation and submit its first turn (canonical seam)",
    responses={
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
            "description": "The submitted idempotency key is already bound to "
            "another server's conversation scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Idempotency key retransmitted with changed content, "
            "or another writer holds the conversation lease",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Model configuration or jobs engine unavailable",
        },
    },
)
async def create_conversation(
    request: Request, payload: ConversationTurnSubmitRequest
) -> Response:
    """Create one conversation and atomically admit its first turn (§3).

    The modern seam: ``client_request_id`` IS the new conversation's
    identity and the first run's idempotency key, so a retry after any
    crash or network timeout recovers the exact conversation, message
    and run triple — deduplicated (``200``), never a second row (C4).
    The canonical user message and the durable enqueue intent commit
    through the conversation store's serialized admission window (C6);
    the engine enqueue follows the commit (re-armable gap, never a half
    intent).
    """
    return await _submit_canonical_turn(request, payload, conversation_id=None)


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=HTTP_201_CREATED,
    response_model=ConversationTurnSubmitResponse,
    summary="Submit one follow-up turn onto a conversation (canonical seam)",
    responses={
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
            "description": "No conversation in the requesting server scope "
            "(cross-server references fail closed, C8)",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Idempotency key retransmitted with changed content, "
            "another writer holds the conversation lease, a stale task "
            "revision was pinned, or history reconstruction is incomplete",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Model configuration or jobs engine unavailable",
        },
    },
)
async def submit_conversation_message(
    request: Request, conversation_id: uuid.UUID, payload: ConversationTurnSubmitRequest
) -> Response:
    """Append one user turn to an existing conversation (§3).

    The turn records ONLY the actual new user text — the canonical
    transcript is assembled server-side at execution time, never
    browser-stitched (C1). A finished follow-up starts a new durable run
    bound to the SAME canonical conversation; a turn arriving while
    another run holds the writer lease is admitted, recorded and
    honestly reported ``queued_followup`` (``202``, no competing
    executor). Retrying the SAME ``client_request_id`` deduplicates
    against the original turn (C4); a conflicting payload under the same
    key fails closed (``409 client_request_conflict``).
    """
    return await _submit_canonical_turn(
        request, payload, conversation_id=conversation_id
    )


@router.post(
    "/conversations/{conversation_id}/steer",
    status_code=HTTP_201_CREATED,
    response_model=ChatSteerView,
    summary="Submit one deterministic steering/control request onto an active run",
    responses={
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
            "description": "No conversation in the requesting server scope "
            "(cross-server references fail closed, C8)",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "No active run holds the conversation lease, or the "
            "idempotency key was retransmitted (deduplicated, 200)",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation (steer requires a "
            "message; pause/cancel carry none)",
        },
    },
)
async def steer_conversation(
    request: Request, conversation_id: uuid.UUID, payload: ConversationSteerRequest
) -> Response:
    """Record one active-run steering/control request (§3; C5).

    DURABLE FIRST: the ``conversation_steering`` row commits before the
    caller is told anything and before any delivery, so a crash between
    the append and the run's consumption recovers through the run's own
    safe-boundary poll — and a retransmitted ``client_request_id``
    deduplicates onto the SAME row, never a second control (C4). Pause
    and cancel are deterministic: they are recorded without any intake
    model call and consumed at the run's next safe effect boundary;
    in-flight effects are never claimed killed without their receipt.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    if payload.conversation_id != conversation_id:
        return _validation_error(
            request,
            "conversation_id",
            "body conversation id does not match the target path",
        )
    client_request_id = (
        payload.client_request_id
        if payload.client_request_id is not None
        else uuid.uuid4()
    )
    factory = _sessions(request)
    try:
        conversation = await aget_conversation(
            factory, conversation_id, server_id=payload.server_id
        )
    except ConversationContractError as exc:
        return _conversation_contract_error(request, exc)
    active = await aactive_run(factory, conversation_id, server_id=payload.server_id)
    if active is None:
        # Nothing is executing: there is no run to steer. Structured and
        # value-free — never a guessed acceptance, never a 500 (C05 r5).
        return _error(
            request,
            HTTP_409_CONFLICT,
            NO_ACTIVE_RUN_CODE,
            "no active run holds this conversation's writer lease",
            fields={"conversation_id": NO_ACTIVE_RUN_CODE},
        )
    try:
        row, disposition = await aenqueue_steering(
            factory,
            conversation_id=conversation_id,
            server_id=payload.server_id,
            action=payload.action,
            client_request_id=client_request_id,
            content=payload.message,
            run_id=active.run_id,
        )
    except ConversationContractError as exc:
        return _conversation_contract_error(request, exc)
    view = ChatSteerView(
        conversation_id=conversation.conversation_id,
        steering_id=row.steering_id,
        run_id=active.run_id,
        action=payload.action,
        client_request_id=client_request_id,
        disposition=disposition,
        delivery_status="pending",
    )
    return Response(
        status_code=HTTP_201_CREATED
        if disposition is TurnDisposition.CREATED
        else HTTP_200_OK,
        content=view.model_dump_json(),
        media_type="application/json",
    )


@router.post(
    "/conversations/{conversation_id}/resume",
    status_code=HTTP_201_CREATED,
    response_model=ConversationTurnSubmitResponse,
    summary="State-specific operator resume (§9): only the valid resume per state",
    responses={
        HTTP_200_OK: {
            "model": ConversationTurnSubmitResponse,
            "description": "Resume of a parked conversation was already "
            "expressed by an identical durable request (deduplicated)",
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
            "description": "No conversation in the requesting server scope "
            "(cross-server references fail closed, C8)",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The current run state has NO generic resume: "
            "``waiting_children`` is child-driven, ``reconciling`` requires "
            "operator classification first, ``waiting_approval`` resumes only "
            "the payload-bound approval decision, and queued/running runs are "
            "already executing (steering is the control); a settled "
            "conversation resumes through the next submitted turn",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Model configuration or jobs engine unavailable",
        },
    },
)
async def resume_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    payload: ConversationResumeBody,
) -> Response:
    """State-specific resume (runtime-v2.md §9; AC4).

    ONE gate over the frozen park vocabulary — never a generic replay
    button:

    - ``waiting_children`` → ``409 resume_rejected``: child completion is
      the only producer that wakes the run (the delegation module's
      settled-child seam); a manual force-resume would execute past
      unproven children.
    - ``reconciling`` → ``409 classification_required``: unknown effects
      NEVER auto-resume into execution; the operator classifies each open
      step first (the reconcile route below).
    - ``waiting_approval`` → ``409 approval_decision_required``: the ONE
      resume is the payload-bound approval decision
      (``dispatch_once(approval_id=…)`` worker-side), never a generic wake.
    - ``queued``/``running`` → ``409 resume_not_applicable``: the run is
      executing; steering is the control.
    - No active run (settled/paused): the operator resume from the last
      settled checkpoint IS the next submitted turn — the checkpoint is
      the canonical transcript plus the objective episode, and the new
      run continues from exactly that state (v2 conversations stay v2).
      With ``message`` the request delegates to the canonical follow-up
      admission (created/``201``); without one, ``409
      resume_not_applicable`` names the honest next action.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)
    factory = _sessions(request)
    try:
        await aget_conversation(factory, conversation_id, server_id=payload.server_id)
    except ConversationContractError as exc:
        return _conversation_contract_error(request, exc)
    active = await aactive_run(factory, conversation_id, server_id=payload.server_id)
    if active is not None:
        state = await _replayed_state(lifecycle, str(active.run_id))
        if state is not None and state not in TERMINAL_RUN_STATES:
            parked = RunState(state)
            if parked is RunState.WAITING_CHILDREN:
                return _error(
                    request,
                    HTTP_409_CONFLICT,
                    "resume_rejected",
                    "the run waits on child completion; children wake it "
                    "through the settled-child producers",
                    fields={"conversation_id": "resume_rejected"},
                )
            if parked is RunState.RECONCILING:
                return _error(
                    request,
                    HTTP_409_CONFLICT,
                    "classification_required",
                    "unknown effects are open; classify them through the "
                    "reconcile route before the run wakes",
                    fields={"conversation_id": "classification_required"},
                )
            if parked is RunState.WAITING_APPROVAL:
                return _error(
                    request,
                    HTTP_409_CONFLICT,
                    "approval_decision_required",
                    "the run waits on a payload-bound approval; the approval "
                    "decision is the only resume",
                    fields={"conversation_id": "approval_decision_required"},
                )
            return _error(
                request,
                HTTP_409_CONFLICT,
                "resume_not_applicable",
                "the run is already executing; steering is the control",
                fields={"conversation_id": "resume_not_applicable"},
            )
    if payload.message is None:
        return _error(
            request,
            HTTP_409_CONFLICT,
            "resume_not_applicable",
            "a settled conversation resumes by submitting its next turn "
            "from the last settled checkpoint",
            fields={"conversation_id": "resume_not_applicable"},
        )
    turn = ConversationTurnSubmitRequest(
        server_id=payload.server_id,
        client_request_id=uuid.uuid4(),
        message=payload.message,
    )
    return await _submit_canonical_turn(request, turn, conversation_id=conversation_id)


@router.post(
    "/conversations/{conversation_id}/reconcile",
    status_code=HTTP_200_OK,
    response_model=ConversationReconcileView,
    summary="Classify open unknown effects of a parked run (§9; AC4)",
    responses={
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
            "description": "No conversation in the requesting server scope, "
            "or a classified seq has no step row",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The run is not parked in ``reconciling``, or a "
            "classified row is not open for reconciliation",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def reconcile_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    payload: ConversationReconcileBody,
) -> Response:
    """Operator classification of unknown effects (§9; C04 rule 5/7).

    The ONLY path that closes an ``outcome_unknown`` row from the HTTP
    surface: each entry maps ``confirm_applied``/``confirm_not_applied``/
    ``cancel`` onto the recovery owner's FINAL verdict classes and lands
    through :meth:`RecoveryService.aresolve_step` — the guarded
    single-owner writer that disposes the recorded lock and wakes the
    parked run (best effort). The run is NEVER re-executed by this
    route: a live parked workflow continues from its own durable loop,
    and an engine-orphaned park stays settled history.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, recovery = _jobs_services(request)
    factory = _sessions(request)
    try:
        await aget_conversation(factory, conversation_id, server_id=payload.server_id)
    except ConversationContractError as exc:
        return _conversation_contract_error(request, exc)
    active = await aactive_run(factory, conversation_id, server_id=payload.server_id)
    if active is None:
        return _error(
            request,
            HTTP_409_CONFLICT,
            NO_ACTIVE_RUN_CODE,
            "no active run holds this conversation's writer lease",
            fields={"conversation_id": NO_ACTIVE_RUN_CODE},
        )
    state = await _replayed_state(lifecycle, str(active.run_id))
    if state != RunState.RECONCILING.value:
        return _error(
            request,
            HTTP_409_CONFLICT,
            "classification_not_applicable",
            "only a run parked in reconciling classifies unknown effects",
            fields={"conversation_id": "classification_not_applicable"},
        )
    verdict_map = {
        "confirm_applied": ReconciliationClass.FINAL_SUCCEEDED,
        "confirm_not_applied": ReconciliationClass.FINAL_FAILED,
        "cancel": ReconciliationClass.FINAL_CANCELLED,
    }
    results: list[StepResolutionView] = []
    for entry in payload.classifications:
        try:
            resolved = await recovery.aresolve_step(
                str(active.run_id),
                entry.seq,
                verdict_map[entry.verdict],
                source="conversation_api",
            )
        except RecoveryError as exc:
            code = exc.code
            if code in (
                RecoveryErrorCode.STEP_NOT_FOUND,
                RecoveryErrorCode.RUN_NOT_FOUND,
            ):
                return _error(
                    request,
                    HTTP_404_NOT_FOUND,
                    code.value,
                    "no open step row for this classification",
                    fields={"seq": code.value},
                )
            return _error(
                request,
                HTTP_409_CONFLICT,
                code.value,
                "the classification was rejected by the recovery owner",
                fields={"seq": code.value},
            )
        results.append(
            StepResolutionView(
                seq=entry.seq,
                state=resolved.state.value,
                verdict=resolved.verdict.value,
                wake_sent=bool(resolved.wake_sent),
            )
        )
    final_state = await _replayed_state(lifecycle, str(active.run_id))
    return ConversationReconcileView(
        conversation_id=conversation_id,
        run_id=active.run_id,
        run_state=final_state,
        results=results,
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


@router.patch(
    "/runs/{run_id}",
    status_code=HTTP_200_OK,
    response_model=ConversationView,
    summary="Rename a conversation (durable user-defined title)",
    responses={
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
            "description": "No run stored for this run id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Persisted run state requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def rename_conversation(
    request: Request, run_id: uuid.UUID, payload: ChatRenameBody
) -> ConversationView | Response:
    """Persist a user-defined conversation title on the run's event stream."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    lifecycle, _recovery = _jobs_services(request)
    title = payload.title.strip()
    if not title:
        return _validation_error(request, "title", "must not be blank")
    identity = str(run_id)
    factory = _sessions(request)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        async with session.begin():
            locked = (
                await session.execute(
                    text("SELECT run_id FROM job_runs WHERE run_id = :r FOR UPDATE"),
                    {"r": run_id},
                )
            ).scalar()
            if locked is None:
                return _not_found_run(request)
            await _record_chat_renamed(
                session,
                run_id=identity,
                title=title,
                now=now,
            )
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
    "/runs/{run_id}/steps/{seq}",
    status_code=HTTP_200_OK,
    summary="Read one tool call's lazy-loaded params and response",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No step stored for this run id and seq",
        },
    },
)
async def read_tool_step(
    request: Request, run_id: uuid.UUID, seq: int
) -> Response:
    """Lazy-load one tool call's params and response for an expanded chat card.

    Reads the persisted ``job_steps`` row (including ``params`` and
    ``response`` in ``receipt``) and falls back to the target-side receipt
    directory or DBOS ``operation_outputs`` if the step predates inline
    ``params``/``response`` persistence.
    """
    await _require_session(request)
    factory = _sessions(request)
    async with factory() as session:
        step_row = (
            (
                await session.execute(
                    text(
                        "SELECT seq, capability_id, state, receipt FROM job_steps "
                        "WHERE run_id = :r AND seq = :s"
                    ),
                    {"r": run_id, "s": seq},
                )
            )
            .mappings()
            .first()
        )
    if step_row is None:
        return _error(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "no step stored for this run id and seq",
        )
    receipt = dict(step_row["receipt"]) if isinstance(step_row["receipt"], Mapping) else {}
    params = receipt.get("params")
    response_data = receipt.get("response")
    if params is None or response_data is None:
        try:
            import base64
            import pickle

            async with factory() as session:
                dbos_rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT function_name, output FROM dbos.operation_outputs "
                                "WHERE workflow_uuid = :w "
                                "AND function_name IN ('agents.model_round', 'agents.tool_execute') "
                                "ORDER BY function_id ASC"
                            ),
                            {"w": str(run_id)},
                        )
                    )
                    .mappings()
                    .all()
                )
            for drow in dbos_rows:
                raw_out = drow.get("output")
                if not isinstance(raw_out, str) or not raw_out:
                    continue
                val = pickle.loads(base64.b64decode(raw_out))
                if not isinstance(val, Mapping):
                    continue
                if drow["function_name"] == "agents.model_round" and params is None:
                    calls = val.get("calls")
                    if isinstance(calls, list):
                        for c in calls:
                            if isinstance(c, Mapping) and int(c.get("seq", -1)) == seq:
                                params = c.get("payload")
                                break
                elif drow["function_name"] == "agents.tool_execute" and response_data is None:
                    if int(val.get("seq", -1)) == seq:
                        rtext = val.get("result_text")
                        if isinstance(rtext, str) and rtext:
                            try:
                                parsed_rt = json.loads(rtext)
                                if (
                                    isinstance(parsed_rt, Mapping)
                                    and isinstance(parsed_rt.get("output"), str)
                                ):
                                    try:
                                        response_data = json.loads(parsed_rt["output"])
                                    except Exception:
                                        response_data = parsed_rt
                                else:
                                    response_data = parsed_rt
                            except Exception:
                                response_data = rtext
                        break
        except Exception:
            pass
    return Response(
        status_code=HTTP_200_OK,
        content=json.dumps(
            {
                "run_id": str(run_id),
                "seq": int(step_row["seq"]),
                "capability_id": str(step_row["capability_id"]),
                "state": str(step_row["state"]),
                "op_id": receipt.get("op_id"),
                "payload_hash": receipt.get("payload_hash"),
                "reason": receipt.get("reason"),
                "exit_code": receipt.get("exit_code"),
                "params": params,
                "response": response_data,
                "receipt": {
                    k: v
                    for k, v in receipt.items()
                    if k not in ("params", "response")
                },
            }
        ),
        media_type="application/json",
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
