"""Tool-free structured intake (card 12-intake; act_CONTRACTS §5/§6).

The FIRST model call of an accepted user turn is an interpreter, not a
source of truth or authorization. This module owns that seam:

1. **Canonical snapshot** (:func:`aload_intake_snapshot`) — bounded,
   role-preserving read of the conversation transcript plus the current
   task revision, active plan steps and active constraints. No
   browser-owned history; no unbounded loads (AGENTS.md §1).
2. **Tool-free interpretation** (:class:`IntakeAgent`) — a PydanticAI
   agent with ZERO registered function tools whose only output channel
   is the contract-shaped ``final_result`` output tool
   (:class:`mai.contracts.conversations.TurnInterpretation`). The
   discriminated-union shape makes effect calls, authority fields
   (``approved``/``grants``/``execute_now``/``bypass_approval``),
   fabricated certainty and self-granted verified outcomes
   unrepresentable (§5: "The output schema contains NO writable
   ``approved=true`` ...").
3. **Bounded repair** — at most :data:`MAX_INTAKE_REPAIR_ATTEMPTS`
   structured validation-feedback retry per turn (value-free
   ``field → code`` feedback), then fail closed
   (:class:`IntakeRejection`). Never silent fabrication, never unbounded
   loops. The inner agent runs with ``retries=0`` so the repair budget
   is owned HERE, not by the model harness.
4. **Deterministic semantic/provenance validation**
   (:func:`validate_intake_decision`) — source-message identity,
   expected-revision CAS pre-check, variant↔task-state consistency and
   the author↔origin defense-in-depth from the ``R-11-task-store``
   review: the store's ``validate_constraint_proposals`` branches on the
   proposal's DECLARED ``origin``, so a model-produced proposal claiming
   ``origin=USER`` could otherwise bypass ``CONSTRAINT_ESCALATION``.
   Intake output is always model output: every ``origin=USER`` claim
   must be an ``add`` anchored to VERIFIED user messages (role ``user``)
   of the interpreted conversation. The same rule is enforced by
   :func:`apropose_model_task_update`, the model-author application seam
   later task tools must reuse.
5. **Application** (:func:`aapply_intake`) — persists only validated
   decisions through ``mai.agents.task_store`` (creation for
   ``plan``/``new_task``, model-author CAS delta for ``continue_task``);
   ``answer``/``clarify``/``blocked`` produce no task machinery at all
   (P1). The store remains the authoritative validator — every store
   rejection is fail-closed here too.

No effect execution happens anywhere in this module: it produces and
persists task/proposal state only, and never grants scope, approval,
authority or certainty (§5/§6/§9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Mapping, Sequence

from pydantic_ai import Agent, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput
from pydantic_core import ValidationError as PydanticCoreValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import conversation_store, task_store
from mai.agents.task_store import TaskMutationResult
from mai.contracts.conversations import (
    AnswerTurn,
    BlockedTurn,
    ClarifyTurn,
    ContinueTaskTurn,
    ConversationContractError,
    MessageRole,
    NewTaskTurn,
    PlanTurn,
    TurnInterpretation,
    TurnValidationError,
    turn_evidence_refs,
)
from mai.contracts.tasks import (
    TITLE_MAX_CHARS,
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    EvidenceRef,
    PatchOp,
    StepStatus,
    TaskContractError,
    TaskErrorCode,
    TaskFieldError,
    TaskPlanPatch,
    VerificationStatus,
)
from mai.agents.context import ServerContextMaterials

__all__ = [
    "DEFAULT_TRANSCRIPT_LIMIT",
    "IntakeAgent",
    "IntakeCallRecord",
    "IntakeRejection",
    "IntakeSnapshot",
    "MAX_INTAKE_REPAIR_ATTEMPTS",
    "OUTPUT_TOOL_NAME",
    "TRANSCRIPT_PROJECTION_MAX_CHARS",
    "aapply_intake",
    "ainterpret_turn",
    "aload_intake_snapshot",
    "apropose_model_task_update",
    "arun_intake_turn",
    "build_repair_prompt",
    "build_snapshot_prompt",
    "validate_intake_decision",
]

#: Bounded repair budget (brief AC 2): at most ONE structured
#: validation-feedback retry before the turn fails closed.
MAX_INTAKE_REPAIR_ATTEMPTS: Final[int] = 1

#: Name of the single structured-output tool PydanticAI declares for the
#: intake union. It is an OUTPUT channel, not an effect: the agent
#: registers zero function tools.
OUTPUT_TOOL_NAME: Final[str] = "final_result"

#: Recent-turn window of the canonical snapshot (bounded, deterministic).
DEFAULT_TRANSCRIPT_LIMIT: Final[int] = 20

#: Per-message character cap in the model-facing projection. Truncation
#: is explicit (never silent): every truncated row carries a marker.
TRANSCRIPT_PROJECTION_MAX_CHARS: Final[int] = 2000

_TRUNCATION_MARKER: Final[str] = "…[truncated]"

# Intake-local stable detail codes (extend the ratified task vocabulary;
# carried in ``fields`` mappings exactly like the frozen codes).
_SOURCE_MESSAGE_NOT_FOUND: Final[str] = "source_message_not_found"
_SOURCE_MESSAGE_NOT_USER: Final[str] = "source_message_not_user"
_SOURCE_MESSAGE_MISMATCH: Final[str] = "source_message_mismatch"
_ACTIVE_TASK_EXISTS: Final[str] = "active_task_exists"
_NO_ACTIVE_TASK: Final[str] = "no_active_task"
_UNEXPECTED_FOR_CREATE: Final[str] = "unexpected_for_create"
_CONSTRAINT_ORIGIN_ANCHOR: Final[str] = "constraint_origin_anchor"
_SERVER_SCOPE_MISMATCH: Final[str] = "server_scope_mismatch"
_PROGRESS_NOT_IN_PATCH: Final[str] = "progress_not_in_patch"


# ---------------------------------------------------------------------------
# records and rejections (value-free; safe to log/display)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntakeCallRecord:
    """Honest accounting of one intake interpretation (§5 budgeting)."""

    model_calls: int
    attempts: int
    repaired: bool
    #: Value-free feedback carried by each repair attempt (attempt 1 is
    #: the first call, so this tuple has one entry per repair).
    repair_feedback: tuple[Mapping[str, str], ...]
    #: Registered function tools on the intake agent (always zero:
    #: tool-free intake is a structural property, asserted by callers).
    function_tools: int = 0


@dataclass(frozen=True, slots=True)
class IntakeSnapshot:
    """Canonical conversation/task state one turn is interpreted against.

    Every collection is a bounded store page; the transcript is a bounded
    recent window — never assembled browser history. Content projected
    into ``transcript``/``source_message_content`` is the sanitized store
    copy (raw-secret exclusion already happened at submit; §2).
    """

    server_id: uuid.UUID
    conversation_id: uuid.UUID
    source_message_id: uuid.UUID
    source_message_role: MessageRole
    source_message_content: str
    #: ``(role, content)`` recent window, oldest first (bounded).
    transcript: tuple[tuple[MessageRole, str], ...]
    #: True when the transcript window omitted older turns (explicit,
    #: never hidden; scenario C3 honesty at the assembly layer).
    transcript_omitted_older: bool
    task_id: uuid.UUID | None
    task_revision: int | None
    task_phase: str | None
    task_title: str | None
    #: ``(step_id, status, verification, title)`` active steps,
    #: deterministic ``(position, step_id)`` order (bounded page).
    steps: tuple[tuple[str, str, str, str], ...]
    steps_omitted: bool
    #: ``(constraint_id, origin, statement)`` active constraints,
    #: deterministic ``(position, constraint_id)`` order (bounded page).
    constraints: tuple[tuple[str, str, str], ...]
    constraints_omitted: bool
    #: Canonical display name of the pinned server (label only; the
    #: ``server_id`` above is the ONLY identity/authorization). ``None``
    #: means the canonical profile was unavailable at composition time —
    #: rendered explicitly, never silently dropped.
    server_name: str | None = None
    #: ``linux_ssh``-style environment label of the pinned profile.
    server_environment: str | None = None
    #: Bounded projection of the canonical administrator server
    #: instructions for the pinned server.
    server_instructions: str | None = None


class IntakeRejection(ValueError):
    """Fail-closed outcome of one intake turn (P4).

    ``fields`` carries only field paths and stable codes — never model
    prose, message content or identifiers of other scopes — so the
    rejection is safe to log, store and surface (C01 rule 3).
    """

    def __init__(
        self,
        fields: Mapping[str, str],
        *,
        attempts: int,
        repaired: bool,
        model_calls: int,
    ) -> None:
        self.fields: dict[str, str] = dict(fields)
        self.attempts: int = attempts
        self.repaired: bool = repaired
        self.model_calls: int = model_calls
        summary = ", ".join(f"{field}: {code}" for field, code in self.fields.items())
        super().__init__(f"intake rejected ({summary})")


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    """Result of one interpreted AND applied intake turn."""

    turn: TurnInterpretation
    #: True when the decision bound a task episode
    #: (``plan``/``new_task``/``continue_task`` — an ordinary
    #: ``continue_task`` with ``todo_patch=None`` is a validated no-op
    #: bound to the EXISTING episode); False for
    #: ``answer``/``clarify``/``blocked`` (no task machinery, P1).
    applied: bool
    task_id: uuid.UUID | None
    revision: int | None
    #: ``created`` | ``deduplicated`` when applied; else ``None``.
    disposition: str | None
    record: IntakeCallRecord


# ---------------------------------------------------------------------------
# canonical snapshot (bounded, role-preserving)
# ---------------------------------------------------------------------------


def _project(content: str) -> str:
    """Bounded model-facing projection with an EXPLICIT truncation marker."""
    if len(content) <= TRANSCRIPT_PROJECTION_MAX_CHARS:
        return content
    return content[:TRANSCRIPT_PROJECTION_MAX_CHARS] + _TRUNCATION_MARKER


async def aload_intake_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
    transcript_limit: int = DEFAULT_TRANSCRIPT_LIMIT,
    server_materials: ServerContextMaterials | None = None,
) -> IntakeSnapshot:
    """Load the canonical bounded snapshot one turn is interpreted against.

    Fail-closed (:class:`IntakeRejection`) when the conversation or the
    source message does not exist in the requested server scope, or when
    the source message is not a verified user message (§1: roles are
    producer-assigned; intake interprets USER turns only).

    ``server_materials`` carries the host-pinned canonical server
    identity (name/environment/instructions from the SAME producer as
    executor assembly) into the FIRST intake call. It is validated
    fail-closed: a mismatch with the conversation's ``server_id`` is
    rejected BEFORE any model call; the pinned ``server_id`` (not the
    display name) remains the only identity/authorization.
    """
    if not 1 <= transcript_limit <= task_store.MAX_PAGE_LIMIT:
        raise ValueError(
            f"transcript_limit must be in [1, {task_store.MAX_PAGE_LIMIT}]"
        )
    if server_materials is not None and server_materials.server_id != server_id:
        raise IntakeRejection(
            {"server_id": _SERVER_SCOPE_MISMATCH},
            attempts=0,
            repaired=False,
            model_calls=0,
        )
    try:
        conversation = await conversation_store.aget_conversation(
            sessions, conversation_id, server_id=server_id
        )
    except ConversationContractError as exc:
        raise IntakeRejection(
            {"conversation_id": "conversation_not_found"},
            attempts=0,
            repaired=False,
            model_calls=0,
        ) from None

    async with sessions() as session:
        source_row = (
            await session.execute(
                text(
                    "SELECT role, content FROM conversation_messages "
                    "WHERE conversation_id = :cid AND message_id = :mid"
                ),
                {"cid": conversation_id, "mid": source_message_id},
            )
        ).mappings().first()
    if source_row is None:
        raise IntakeRejection(
            {"source_message_id": _SOURCE_MESSAGE_NOT_FOUND},
            attempts=0,
            repaired=False,
            model_calls=0,
        )
    source_role = MessageRole(str(source_row["role"]))
    if source_role is not MessageRole.USER:
        # Interpreting a non-user row would let forged or producer
        # material acquire user authority (§1); such rows are context
        # only, never the interpreted source.
        raise IntakeRejection(
            {"source_message_id": _SOURCE_MESSAGE_NOT_USER},
            attempts=0,
            repaired=False,
            model_calls=0,
        )

    # Bounded recent window: the LAST page of the transcript (deterministic
    # (seq, message_id) order), with the omission stated, never hidden.
    # The FIRST page is fetched at the FULL requested window: a
    # ``transcript_limit`` above the store's default page limit must
    # actually widen the window (R-12 N1) — fetching at the store default
    # kept the first 50 rows whenever ``total <= transcript_limit``, so the
    # most recent turns were dropped silently with no omission marker.
    page = await conversation_store.alist_messages(
        sessions, conversation_id, server_id=server_id, limit=transcript_limit
    )
    total = page.total
    offset = max(0, total - transcript_limit)
    if offset != page.offset:
        page = await conversation_store.alist_messages(
            sessions,
            conversation_id,
            server_id=server_id,
            limit=transcript_limit,
            offset=offset,
        )

    # Current task episode (V1: at most one open task per conversation).
    task_row = None
    steps_page = None
    constraints_page = None
    if conversation.active_task_id is not None:
        try:
            task_row = await task_store.aget_task(
                sessions, conversation.active_task_id, server_id=server_id
            )
        except TaskContractError as exc:
            raise IntakeRejection(
                {"task_id": TaskErrorCode.TASK_NOT_FOUND},
                attempts=0,
                repaired=False,
                model_calls=0,
            ) from None
        steps_page = await task_store.alist_steps(
            sessions, task_row.task_id, server_id=server_id
        )
        constraints_page = await task_store.alist_constraints(
            sessions, task_row.task_id, server_id=server_id
        )

    return IntakeSnapshot(
        server_id=server_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        source_message_role=source_role,
        source_message_content=_project(str(source_row["content"])),
        transcript=tuple((item.role, item.content) for item in page.items),
        transcript_omitted_older=offset > 0,
        task_id=task_row.task_id if task_row is not None else None,
        task_revision=task_row.revision if task_row is not None else None,
        task_phase=(
            task_row.requested_phase.value if task_row is not None else None
        ),
        task_title=task_row.title if task_row is not None else None,
        steps=(
            tuple(
                (row.step_id, row.status.value, row.verification.value, row.title)
                for row in steps_page.items
            )
            if steps_page is not None
            else ()
        ),
        steps_omitted=(steps_page.has_more if steps_page is not None else False),
        constraints=(
            tuple(
                (row.constraint_id, row.origin.value, row.statement)
                for row in constraints_page.items
            )
            if constraints_page is not None
            else ()
        ),
        constraints_omitted=(
            constraints_page.has_more if constraints_page is not None else False
        ),
        server_name=(
            server_materials.profile.name if server_materials is not None else None
        ),
        server_environment=(
            server_materials.profile.environment.value
            if server_materials is not None
            else None
        ),
        server_instructions=(
            _project(server_materials.prompt_text)
            if server_materials is not None
            else None
        ),
    )



# ---------------------------------------------------------------------------
# model-facing prompts (bounded, deterministic, no chain-of-thought capture)
# ---------------------------------------------------------------------------

_INTAKE_INSTRUCTIONS: Final[str] = """\
You are the structured intake interpreter of a task-execution assistant.
You have NO tools and NO execution abilities. Interpret the user turn and
respond with EXACTLY ONE `final_result` tool call whose `response`
argument is one TurnInterpretation JSON object.

Variant rules:
- `answer`: only for self-contained questions answerable from the given
  context. No plan, no steps, no task machinery.
- `clarify`: the turn cannot proceed safely without user input; provide
  at least one open question with a stable proposal_id.
- `plan`: propose a NEW task (objective, acceptance criteria, bounded
  `todo_patch` delta with `add` operations only). Choose
  `requested_phase` by the phase policy below — advisory, never
  permission.
- `new_task`: the user explicitly starts a new goal episode.
- `continue_task`: an ORDINARY continuation of the CURRENT task carries
  `todo_patch: null` — the user saying "continue", answering a
  question, or acknowledging progress edits no plan items. Carry a
  delta ONLY to edit the plan itself (add/retitle/reorder/re-depend/
  supersede pending items): a delta against the task at `base_revision`,
  never whole-list replacement, never a goal change. Preserve completed
  steps; update only affected items; new scope is proposed, never
  silently approved.
- `blocked`: the turn cannot proceed safely; state the stable
  `blocked_kind` and a safe reason.

Requested phase — autonomous simple-operation policy:
- An EXPLICIT, bounded request to add/install/change/enable one known
  thing (a reversible catalog-entry addition, a bounded known-service
  configuration update with backup) selects requested_phase "act"
  FROM THE OUTSET. Never park it in "inspect" to wait: inside the
  accepted "act" run, inspection is an automatic PREPARATORY tool
  step, and the run proceeds inspect -> mutate -> verify -> report in
  ONE turn — no second user message and no phase confirmation. Give
  such a plan explicit verification steps with concrete
  `expected_check` content.
- An explore/inspect/read-only request ("show", "list", "which
  ports") stays requested_phase "inspect" (or "plan" when the user
  asked only for a proposal); never select "act" for read-only
  intent.
- "act" means ELIGIBILITY only: it never implies approval, a grant,
  execution certainty or skipped verification. Effects still need
  their existing grants and operation approvals, and success requires
  the requested postcondition to be VERIFIED — finishing reads or
  producing a todo is not success.
- Ambiguity is a stop, never a guess: when the target or action is
  unclear, a grant, credential or explicit operation approval is
  missing, the work would expand scope, destroy/revert something or
  disrupt a service, or verification may hit unexpected state,
  respond `clarify` (or `blocked`) with the concrete reason instead
  of selecting "act".

Hard rules:
- `source_message_id` must be exactly the interpreted user message id
  given below.
- A constraint marked `origin: "user"` MUST be a verbatim-anchored user
  requirement: `suggested: "add"` with `source_message_refs` pointing at
  the user message ids it came from. You may NEVER mark a supersede of
  an existing constraint as user-originated; user constraint removal
  happens only through the user revision API.
- Never emit authority fields (`approved`, `grants`, `execute_now`,
  `bypass_approval`), never claim execution certainty, never mark steps
  executed or verified without real evidence references.
- Plan editing and task progress are DIFFERENT things. A `todo_patch`
  never reports task progress: item `status` and `verification` are
  evidence-bound state maintained by the task progress tools, never a
  model-ticked plan. Inside a `continue_task` delta every operation
  stays `status: "pending"` / `verification: "not_checked"`; an
  `update` echoes the step's current status and verification exactly
  as shown in the snapshot. Never claim a step ran or passed through
  the plan.
- Facts reference existing records only; anything else is an explicit
  `assumptions` entry.

Pinned server scope:
- The conversation may be pinned to one server, shown under
  "# Pinned server scope". Its server_id is the ONLY identity and
  authorization; the name is a display label.
- While a server is pinned, references like "this server", "this
  device" or "the device" mean THAT server. Do not ask the user to
  re-identify it and never claim no server/device context exists.
- The pinned scope is not knowledge of unseen service facts: an
  EXPLORATORY turn needing live state (ports, services, inventory)
  becomes a plan with requested_phase "inspect" targeting the pinned
  server; an EXPLICIT bounded change still selects "act" and reads
  the live state as its own preparatory step inside the run.
- After appropriate available inspection, genuine ambiguity about
  WHICH service may still be a clarify. Never invent inventory,
  ports or other unseen facts.
- You cannot change the pinned scope: never propose another server
  id/name as the target, and never treat a name you mention as
  authorization.
"""


def build_snapshot_prompt(snapshot: IntakeSnapshot) -> str:
    """Deterministic bounded model-facing projection of the snapshot."""
    lines: list[str] = [_INTAKE_INSTRUCTIONS, "# Canonical snapshot", ""]
    lines.append(
        f"conversation_id: {snapshot.conversation_id}"
    )
    if snapshot.server_id == uuid.UUID(int=0):
        lines.append("# Pinned server scope")
        lines.append(f"server_id: {uuid.UUID(int=0)}")
        if snapshot.server_name is not None:
            lines.append(f"server_name: {snapshot.server_name}")
        lines.append(
            "scope: general consult — no specific server is pinned; "
            "interpret the turn without a target server and never guess "
            "a host"
        )
    else:
        lines.append("# Pinned server scope")
        lines.append(f"server_id: {snapshot.server_id}")
        lines.append(
            "server_name: "
            + (
                snapshot.server_name
                if snapshot.server_name is not None
                else "(canonical profile unavailable; the server_id above "
                "remains the pinned scope)"
            )
        )
        if snapshot.server_environment is not None:
            lines.append(f"environment: {snapshot.server_environment}")
        if snapshot.server_instructions is not None:
            lines.append(
                "administrator server instructions (bounded): "
                + snapshot.server_instructions
            )
        else:
            lines.append(
                "administrator server instructions: (canonical profile "
                "unavailable)"
            )
    if snapshot.transcript_omitted_older:
        lines.append(
            f"(older transcript turns exist and are omitted from this "
            f"window; the most recent {len(snapshot.transcript)} are shown)"
        )
    lines.append("# Recent transcript (role: content)")
    for role, content in snapshot.transcript:
        lines.append(f"{role.value}: {_project(content)}")
    lines.append("")
    lines.append("# Interpreted user turn")
    lines.append(f"source_message_id: {snapshot.source_message_id}")
    lines.append(_project(snapshot.source_message_content))
    lines.append("")
    if snapshot.task_id is None:
        lines.append("# Active task: none")
    else:
        assert snapshot.task_revision is not None
        lines.append(
            f"# Active task {snapshot.task_id} at revision "
            f"{snapshot.task_revision} (phase {snapshot.task_phase})"
        )
        if snapshot.task_title:
            lines.append(f"title: {snapshot.task_title}")
        if snapshot.steps_omitted:
            lines.append(
                f"(plan shows the first {len(snapshot.steps)} active steps;"
                " more exist)"
            )
        lines.append("# Active plan steps (step_id | status | verification | title)")
        for step_id, status, verification, title in snapshot.steps:
            lines.append(f"{step_id} | {status} | {verification} | {title}")
        if snapshot.constraints_omitted:
            lines.append(
                f"(constraints show the first {len(snapshot.constraints)};"
                " more exist)"
            )
        lines.append("# Active constraints (constraint_id | origin | statement)")
        for constraint_id, origin, statement in snapshot.constraints:
            lines.append(f"{constraint_id} | {origin} | {statement}")
        lines.append(
            "An ordinary continuation carries `todo_patch: null`; a "
            "continue_task delta MUST carry base_revision = "
            f"{snapshot.task_revision} and never changes item status "
            "or verification."
        )
    lines.append("")
    lines.append(
        "Respond with exactly one `final_result` tool call now."
    )
    return "\n".join(lines)


def build_repair_prompt(snapshot: IntakeSnapshot, feedback: Mapping[str, str]) -> str:
    """Structured validation-feedback repair prompt (value-free).

    Carries the FULL canonical snapshot again: a repair attempt is a
    fresh bounded call, never a continuation that could silently lose
    context (§5: "Task context is constructed before the first call").
    """
    rendered = "\n".join(f"- {field}: {code}" for field, code in feedback.items())
    repair = (
        "# REPAIR REQUIRED\n"
        "Your previous TurnInterpretation was REJECTED by deterministic "
        "validation and had NO effect. Repair exactly these field errors "
        "and respond again with exactly one `final_result` tool call:\n"
        f"{rendered}\n"
        "Do not change anything else; do not invent new facts."
    )
    return f"{build_snapshot_prompt(snapshot)}\n\n{repair}"


# ---------------------------------------------------------------------------
# tool-free agent with bounded repair
# ---------------------------------------------------------------------------


def _feedback_fields(exc: BaseException) -> dict[str, str]:
    """Extract value-free ``field → code`` feedback from a failure chain.

    Understands pydantic validation errors (schema shape), the contract
    union's :class:`TurnValidationError` and task-domain
    :class:`TaskContractError`; anything else collapses to one generic
    ``turn_kind`` entry so the repair prompt stays bounded.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, TaskContractError):
            return dict(current.fields)
        if isinstance(current, TurnValidationError):
            return dict(current.fields)
        if isinstance(current, PydanticCoreValidationError):
            fields: dict[str, str] = {}
            for error in current.errors():
                parts = [str(part) for part in error["loc"]]
                # The output tool wraps the payload in a `response`
                # parameter; strip transport prefixes so consumers see the
                # contract-relative field path.
                while parts and parts[0] in ("response", "result"):
                    parts = parts[1:]
                loc = ".".join(parts) or "<root>"
                fields.setdefault(loc, str(error["type"]))
            return fields
        current = current.__cause__
    return {"turn_kind": "unexpected_model_behavior"}


class IntakeAgent:
    """The tool-free first-call interpreter (§5; brief AC 1).

    The wrapped PydanticAI agent registers ZERO function tools and runs
    with ``retries=0``: any schema/shape failure surfaces after exactly
    ONE model call, and the bounded repair loop below owns the only
    possible retry. Output type is the ratified
    :class:`TurnInterpretation` union via the ``final_result`` output
    tool — authority fields and effect calls are unrepresentable.
    """

    def __init__(
        self,
        model: Model,
        *,
        max_repair_attempts: int = MAX_INTAKE_REPAIR_ATTEMPTS,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must be >= 0")
        self._model = model
        self._max_repair_attempts = max_repair_attempts

    @property
    def function_tools(self) -> int:
        """Registered function tools (always zero — structural guarantee)."""
        return 0

    def _build_agent(self) -> Agent[None, TurnInterpretation]:
        agent: Agent[None, TurnInterpretation] = Agent(
            self._model,
            output_type=ToolOutput(TurnInterpretation),
            tools=[],
            retries=0,
        )
        return agent

    async def _attempt(
        self, agent: Agent[None, TurnInterpretation], prompt: str
    ) -> TurnInterpretation:
        """One model attempt; schema/shape failures carry structured cause."""
        result = await agent.run(prompt)
        turn = result.output
        if not isinstance(
            turn,
            (AnswerTurn, ClarifyTurn, PlanTurn, ContinueTaskTurn, NewTaskTurn, BlockedTurn),
        ):  # pragma: no cover - union output is fully validated
            raise UnexpectedModelBehavior("non-union output")
        return turn

    async def interpret(
        self,
        snapshot: IntakeSnapshot,
    ) -> tuple[TurnInterpretation, IntakeCallRecord]:
        """Interpret one turn with bounded repair (never persists)."""
        agent = self._build_agent()
        prompt = build_snapshot_prompt(snapshot)
        feedback: tuple[Mapping[str, str], ...] = ()
        attempts = 0
        while True:
            attempts += 1
            try:
                turn = await self._attempt(agent, prompt)
            except UnexpectedModelBehavior as exc:
                fields = _feedback_fields(exc)
                if len(feedback) < self._max_repair_attempts:
                    feedback = (*feedback, fields)
                    prompt = build_repair_prompt(snapshot, fields)
                    continue
                raise IntakeRejection(
                    fields,
                    attempts=attempts,
                    repaired=bool(feedback),
                    model_calls=attempts,
                ) from None
            record = IntakeCallRecord(
                model_calls=attempts,
                attempts=attempts,
                repaired=bool(feedback),
                repair_feedback=feedback,
                function_tools=self.function_tools,
            )
            return turn, record


async def ainterpret_turn(
    model: Model,
    snapshot: IntakeSnapshot,
    *,
    max_repair_attempts: int = MAX_INTAKE_REPAIR_ATTEMPTS,
) -> tuple[TurnInterpretation, IntakeCallRecord]:
    """Functional form of :class:`IntakeAgent.interpret` (one-shot use)."""
    return await IntakeAgent(model, max_repair_attempts=max_repair_attempts).interpret(
        snapshot
    )


# ---------------------------------------------------------------------------
# deterministic semantic/provenance validation (§6; P4; R-11 defense)
# ---------------------------------------------------------------------------


async def _averified_user_anchors(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    refs: Sequence[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    """Verified-anchor map for constraint source references.

    A reference counts ONLY when the row exists in THIS conversation AND
    its producer-assigned role is ``user`` (§1: assistant/tool/system or
    legacy rows never carry user authority, even when a model claims
    they do).
    """
    unique = sorted(set(refs))
    if not unique:
        return {}
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT message_id, role FROM conversation_messages "
                    "WHERE conversation_id = :cid "
                    "AND message_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"cid": conversation_id, "ids": list(unique)},
            )
        ).all()
    verified = {
        row[0]: MessageRole(str(row[1])) is MessageRole.USER for row in rows
    }
    return {ref: bool(verified.get(ref, False)) for ref in unique}


#: Existence predicate per evidence kind, mirroring the store's ratified
#: ``_EVIDENCE_SOURCE_SQL`` sources (single validation truth lives in the
#: store; this pre-check only feeds the bounded repair loop EARLIER).
#: ``artifact`` has no app-owned registry at this head → always closed
#: (same documented limitation as the store).
_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"message", "run_event", "observation", "document", "artifact"}
)


async def _aevidence_exists_map(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    refs: Sequence[EvidenceRef],
) -> dict[uuid.UUID, bool]:
    """Existence map for turn-level evidence references (P4).

    Hallucinated evidence/source IDs are rejected BEFORE any store call
    so the repair feedback is precise; the store re-checks patch-step
    evidence authoritatively at apply time.
    """
    unique = {ref.evidence_id for ref in refs}
    if not unique:
        return {}
    for ref in refs:
        if ref.kind.value not in _EVIDENCE_KINDS:  # pragma: no cover
            raise ValueError(f"unknown evidence kind {ref.kind!r}")
    async with sessions() as session:
        message_ids = [
            ref.evidence_id for ref in refs if ref.kind is EvidenceKind.MESSAGE
        ]
        run_ids = [
            ref.evidence_id for ref in refs if ref.kind is EvidenceKind.RUN_EVENT
        ]
        known: set[uuid.UUID] = set()
        if message_ids:
            known.update(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT message_id FROM conversation_messages "
                            "WHERE conversation_id = :cid "
                            "AND message_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"cid": conversation_id, "ids": message_ids},
                    )
                ).all()
            )
        if run_ids:
            known.update(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT run_id FROM conversation_runs "
                            "WHERE conversation_id = :cid "
                            "AND run_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"cid": conversation_id, "ids": run_ids},
                    )
                ).all()
            )
        # observation/document sources are app-owned global tables; a
        # cross-check here would duplicate the store's exact predicates
        # without adding repair value, so only in-conversation kinds are
        # pre-checked and the rest are left to the authoritative store.
        observation_ids = [
            ref.evidence_id
            for ref in refs
            if ref.kind in (EvidenceKind.OBSERVATION, EvidenceKind.DOCUMENT)
        ]
        if observation_ids:
            known.update(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT observation_id FROM server_observations "
                            "WHERE observation_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": observation_ids},
                    )
                ).all()
            )
            known.update(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT doc_id FROM knowledge_documents "
                            "WHERE doc_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": observation_ids},
                    )
                ).all()
            )
    exists: dict[uuid.UUID, bool] = {eid: (eid in known) for eid in unique}
    for ref in refs:
        if ref.kind is EvidenceKind.ARTIFACT:
            # No app-owned artifact registry at this head → always closed
            # (mirrors the store's documented limitation).
            exists[ref.evidence_id] = False
    return exists


def _constraint_proposal_errors(
    proposals: Sequence[ConstraintProposal],
    anchors: Mapping[uuid.UUID, bool],
) -> list[TaskFieldError]:
    """Author↔origin defense-in-depth on intake proposals (R-11).

    Intake output is ALWAYS model output. Therefore a proposal may claim
    ``origin=USER`` only when it is an ``add`` whose every
    ``source_message_refs`` entry is a verified user message of the
    interpreted conversation. Any ``origin=USER`` supersede is refused
    outright: user constraint removal happens only through the user
    revision API — a claimed origin can never bypass
    ``CONSTRAINT_ESCALATION``.
    """
    errors: list[TaskFieldError] = []
    for index, proposal in enumerate(proposals):
        if proposal.origin is not ConstraintOrigin.USER:
            continue
        field = f"constraints_proposed[{index}]"
        if proposal.suggested != "add":
            errors.append(
                TaskFieldError(
                    TaskErrorCode.CONSTRAINT_ESCALATION,
                    field,
                    "a model-produced proposal cannot claim user origin to "
                    "supersede a constraint; an explicit user correction is "
                    "required",
                )
            )
            continue
        if not proposal.source_message_refs:
            errors.append(
                TaskFieldError(
                    _CONSTRAINT_ORIGIN_ANCHOR,
                    field,
                    "a user-origin constraint proposal must anchor to at "
                    "least one verified user message",
                )
            )
            continue
        for ref_index, ref in enumerate(proposal.source_message_refs):
            if not anchors.get(ref, False):
                errors.append(
                    TaskFieldError(
                        _CONSTRAINT_ORIGIN_ANCHOR,
                        f"{field}.source_message_refs[{ref_index}]",
                        "constraint source is not a verified user message "
                        "of this conversation",
                    )
                )
    return errors


def _continue_progress_errors(
    patch: TaskPlanPatch, snapshot: IntakeSnapshot
) -> list[TaskFieldError]:
    """Plan-edit vs progress defense on ``continue_task`` deltas (A2).

    A ``continue_task`` ``todo_patch`` is plan editing ONLY: item
    ``status`` and ``verification`` are evidence-bound task state owned
    by the progress seams (``task.update_item`` / completion verdicts),
    never a model-ticked plan. Adds/supersedes must start ``pending`` /
    ``not_checked``; an ``update`` must echo the step's CURRENT state as
    shown in the snapshot. The store independently re-checks update
    equality at apply (single validation truth stays in the store); this
    pre-check exists so the offending field path reaches the ONE bounded
    structured repair BEFORE any apply attempt. Steps hidden by the
    bounded snapshot page are left to the store's authoritative check.
    """
    current = {
        step_id: (StepStatus(status), VerificationStatus(verification))
        for step_id, status, verification, _title in snapshot.steps
    }
    errors: list[TaskFieldError] = []
    for index, operation in enumerate(patch.operations):
        step = operation.step
        if operation.op is PatchOp.UPDATE:
            state = current.get(step.step_id)
            if state is None:
                continue
            if step.status is not state[0]:
                errors.append(
                    TaskFieldError(
                        _PROGRESS_NOT_IN_PATCH,
                        f"todo_patch.operations[{index}].step.status",
                        "a continue_task plan patch never reports item "
                        "progress; echo the current status shown in the "
                        "snapshot or carry todo_patch: null",
                    )
                )
            if step.verification is not state[1]:
                errors.append(
                    TaskFieldError(
                        _PROGRESS_NOT_IN_PATCH,
                        f"todo_patch.operations[{index}].step.verification",
                        "a continue_task plan patch never reports item "
                        "verification; echo the current verification shown "
                        "in the snapshot or carry todo_patch: null",
                    )
                )
            continue
        if step.status is not StepStatus.PENDING:
            errors.append(
                TaskFieldError(
                    _PROGRESS_NOT_IN_PATCH,
                    f"todo_patch.operations[{index}].step.status",
                    "a continue_task plan patch adds pending plan items "
                    "only; execution status is reported through the task "
                    "progress tools, never the plan",
                )
            )
        if step.verification is not VerificationStatus.NOT_CHECKED:
            errors.append(
                TaskFieldError(
                    _PROGRESS_NOT_IN_PATCH,
                    f"todo_patch.operations[{index}].step.verification",
                    "a continue_task plan patch adds unchecked plan items "
                    "only; verification is evidence-bound task state, "
                    "never a plan tick",
                )
            )
    return errors


def validate_intake_decision(
    turn: TurnInterpretation,
    snapshot: IntakeSnapshot,
    *,
    constraint_anchors: Mapping[uuid.UUID, bool],
    evidence_exists: Mapping[uuid.UUID, bool],
    intake_replay_task_id: uuid.UUID | None = None,
) -> None:
    """Deterministic semantic/provenance validation of one parsed turn.

    Checks what the STORE cannot know at its layer (and what must be
    known BEFORE any repair round):

    - the turn interprets exactly the snapshot's user message;
    - variant↔task-state consistency (§1: one active episode; no silent
      reset, no orphan continuation). ``intake_replay_task_id`` marks the
      C4 replay case (the SAME turn already created a task): the
      active-episode check is suppressed so the store's idempotent
      replay can deduplicate; every other rule still applies;
    - expected-revision pre-check with stable ``revision_conflict``
      feedback (the store re-checks authoritatively at apply);
    - create-variants cannot pin a task revision that does not exist;
    - constraint author↔origin anchoring (R-11 defense-in-depth);
    - turn-level evidence/source references exist (hallucinated refs are
      rejected; the store re-checks patch-step evidence at apply);
    - a ``continue_task`` ``todo_patch`` is a plan EDIT only: any
      status/verification reporting through it is refused with the
      offending field path so it reaches the ONE bounded structured
      repair (the store re-checks update equality authoritatively at
      apply); ``todo_patch = null`` is the validated ordinary
      continuation.

    Raises :class:`TaskContractError` with value-free fields; the bounded
    repair loop feeds the mapping back to the model once, then fails
    closed. Structural patch rules (cycles, unknown deps, duplicate ids)
    are delegated to the store's ratified validators at apply time — a
    single source of validation truth.
    """
    errors: list[TaskFieldError] = []
    if turn.source_message_id != snapshot.source_message_id:
        errors.append(
            TaskFieldError(
                _SOURCE_MESSAGE_MISMATCH,
                "source_message_id",
                "interpretation must reference exactly the interpreted "
                "user message",
            )
        )
    for path, ref in turn_evidence_refs(turn):
        if not evidence_exists.get(ref.evidence_id, False):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.UNKNOWN_EVIDENCE_REF,
                    path,
                    "evidence reference does not exist in the task scope",
                )
            )

    if isinstance(turn, (PlanTurn, NewTaskTurn)):
        if snapshot.task_id is not None and intake_replay_task_id is None:
            errors.append(
                TaskFieldError(
                    _ACTIVE_TASK_EXISTS,
                    "turn_kind",
                    "the conversation already has an active task episode; "
                    "ambiguous goal changes must be confirmed by the user, "
                    "never reset silently",
                )
            )
        if turn.expected_task_revision is not None:
            errors.append(
                TaskFieldError(
                    _UNEXPECTED_FOR_CREATE,
                    "expected_task_revision",
                    "a task-creating turn cannot pin a revision before the "
                    "task exists",
                )
            )
    elif isinstance(turn, ContinueTaskTurn):
        if snapshot.task_id is None or snapshot.task_revision is None:
            errors.append(
                TaskFieldError(
                    _NO_ACTIVE_TASK,
                    "turn_kind",
                    "a continuation delta requires an active task episode",
                )
            )
        else:
            if (
                turn.expected_task_revision is not None
                and turn.expected_task_revision != snapshot.task_revision
            ):
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.REVISION_CONFLICT,
                        "expected_task_revision",
                        "interpretation was produced against a stale task "
                        "revision",
                    )
                )
            if turn.todo_patch is not None:
                if turn.todo_patch.base_revision != snapshot.task_revision:
                    errors.append(
                        TaskFieldError(
                            TaskErrorCode.REVISION_CONFLICT,
                            "todo_patch.base_revision",
                            "patch base revision is stale",
                        )
                    )
                # A2: a continue_task patch is a plan EDIT; any
                # status/verification reporting through it reaches the
                # ONE bounded structured repair with the offending
                # field path, BEFORE any apply attempt.
                errors.extend(
                    _continue_progress_errors(turn.todo_patch, snapshot)
                )

    errors.extend(
        _constraint_proposal_errors(
            tuple(getattr(turn, "constraints_proposed", ()) or ()),
            constraint_anchors,
        )
    )
    if errors:
        raise TaskContractError(errors)


# ---------------------------------------------------------------------------
# application seam (persisting validated decisions; store = authority)
# ---------------------------------------------------------------------------


def _stable_intake_key(
    conversation_id: uuid.UUID, source_message_id: uuid.UUID
) -> uuid.UUID:
    """Deterministic idempotency key: re-interpreting the SAME user turn
    deduplicates onto the original task (C4) instead of duplicating it."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"intake:{conversation_id}:{source_message_id}"
    )


async def _atask_by_intake_key(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    intake_key: uuid.UUID,
) -> uuid.UUID | None:
    """The task a PREVIOUS intake of the same turn created, if any (C4)."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT task_id FROM agent_tasks "
                    "WHERE conversation_id = :cid "
                    "AND client_request_id = :key "
                    "LIMIT 1"
                ),
                {"cid": conversation_id, "key": intake_key},
            )
        ).mappings().first()
    return row["task_id"] if row is not None else None


def _create_title(turn: PlanTurn | NewTaskTurn) -> str:
    """Bounded header title: explicit patch title, else a display-safe
    mechanical trim of the objective (the trim is stated by the ellipsis)."""
    title = turn.todo_patch.title
    if title is not None and title.strip():
        return title
    objective = turn.objective.strip()
    if len(objective) <= TITLE_MAX_CHARS:
        return objective
    return objective[: TITLE_MAX_CHARS - 1] + "…"


async def aapply_intake(
    sessions: async_sessionmaker[AsyncSession],
    snapshot: IntakeSnapshot,
    turn: TurnInterpretation,
) -> tuple[uuid.UUID | None, int | None, str | None]:
    """Apply ONE ALREADY-VALIDATED decision against the task store.

    ``answer``/``clarify``/``blocked`` persist nothing (P1: no artificial
    todo machinery). ``plan``/``new_task`` create the episode;
    ``continue_task`` applies the model-author CAS delta, or — for the
    ordinary ``todo_patch = null`` continuation — is a VALIDATED no-op
    that binds the existing episode without any store write. All
    structural patch/evidence/authority rules are enforced by the
    store's ratified validators — a store rejection here is fail-closed
    and feeds the bounded repair loop.

    Returns ``(task_id, revision, disposition)``; ``None`` when nothing
    was persisted. Raises :class:`TaskContractError` /
    :class:`IntakeRejection` on rejection.
    """
    if isinstance(turn, (AnswerTurn, ClarifyTurn, BlockedTurn)):
        return (None, None, None)

    if isinstance(turn, (PlanTurn, NewTaskTurn)):
        result = await task_store.acreate_task(
            sessions,
            server_id=snapshot.server_id,
            conversation_id=snapshot.conversation_id,
            title=_create_title(turn),
            summary=turn.objective,
            requested_phase=turn.requested_phase,
            steps=tuple(operation.step for operation in turn.todo_patch.operations),
            constraints=tuple(turn.constraints_proposed),
            criteria=tuple(turn.acceptance_criteria),
            source_message_id=turn.source_message_id,
            client_request_id=_stable_intake_key(
                snapshot.conversation_id, turn.source_message_id
            ),
        )
        return (
            result.task.task_id,
            result.revision.revision,
            result.disposition.value,
        )

    assert isinstance(turn, ContinueTaskTurn)  # union is exhaustive
    assert snapshot.task_id is not None  # validate_intake_decision guarantees
    if turn.todo_patch is None:
        # Ordinary continuation (A2): a VALIDATED no-op — the decision
        # binds the run to the active episode and persists nothing, so
        # the task keeps its revision and ``disposition`` stays ``None``.
        return (snapshot.task_id, snapshot.task_revision, None)
    result = await task_store.apropose_update(
        sessions,
        snapshot.task_id,
        server_id=snapshot.server_id,
        author="model",
        patch=turn.todo_patch,
    )
    return (
        result.task.task_id,
        result.revision.revision,
        result.disposition.value,
    )


async def apropose_model_task_update(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    conversation_id: uuid.UUID,
    patch: TaskPlanPatch,
    constraint_proposals: Sequence[ConstraintProposal] = (),
) -> TaskMutationResult:
    """Model-author update seam with author↔origin defense (R-11).

    Later task tools (§7) must route model-authored
    ``task.propose_update`` calls through HERE instead of calling the
    store directly: the store's validator branches on the proposal's
    DECLARED origin, so this seam first refuses every ``origin=USER``
    proposal that is not an ``add`` anchored to verified user messages
    of the conversation, then delegates to
    :func:`mai.agents.task_store.apropose_update` with
    ``author="model"`` (which independently refuses model supersedes of
    user-origin constraints).
    """
    refs: list[uuid.UUID] = []
    for proposal in constraint_proposals:
        refs.extend(proposal.source_message_refs)
    anchors = await _averified_user_anchors(sessions, conversation_id, refs)
    errors = _constraint_proposal_errors(constraint_proposals, anchors)
    if errors:
        raise TaskContractError(errors)
    return await task_store.apropose_update(
        sessions,
        task_id,
        server_id=server_id,
        author="model",
        patch=patch,
        constraint_proposals=constraint_proposals,
    )


# ---------------------------------------------------------------------------
# the full seam: snapshot -> interpret -> validate (bounded repair) -> apply
# ---------------------------------------------------------------------------


async def arun_intake_turn(
    model: Model,
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
    max_repair_attempts: int = MAX_INTAKE_REPAIR_ATTEMPTS,
    transcript_limit: int = DEFAULT_TRANSCRIPT_LIMIT,
    server_materials: ServerContextMaterials | None = None,
) -> IntakeOutcome:
    """Interpret and apply one accepted user turn end-to-end.

    ``snapshot → tool-free interpretation → deterministic
    validation ⇄ (≤ 1 structured-feedback repair) → task-store apply``.
    Any unrepairable failure raises :class:`IntakeRejection` with
    value-free fields and honest attempt accounting; nothing is persisted
    for a rejected turn (P4: no effects on invalid intake).

    ``server_materials`` (validated fail-closed in the snapshot loader)
    renders the pinned server identity into the FIRST intake call.
    """
    snapshot = await aload_intake_snapshot(
        sessions,
        server_id=server_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        transcript_limit=transcript_limit,
        server_materials=server_materials,
    )
    agent = IntakeAgent(model, max_repair_attempts=max_repair_attempts)
    core_agent = agent._build_agent()  # noqa: SLF001 - same-module seam
    prompt = build_snapshot_prompt(snapshot)
    feedback: tuple[Mapping[str, str], ...] = ()
    attempts = 0
    while True:
        attempts += 1
        turn: TurnInterpretation
        task_id: uuid.UUID | None
        revision: int | None
        disposition: str | None
        failure: dict[str, str] | None = None
        try:
            turn = await agent._attempt(core_agent, prompt)  # noqa: SLF001
        except UnexpectedModelBehavior as exc:
            failure = _feedback_fields(exc)
        else:
            refs: list[uuid.UUID] = []
            for proposal in tuple(getattr(turn, "constraints_proposed", ()) or ()):
                refs.extend(proposal.source_message_refs)
            evidence_refs = [ref for _path, ref in turn_evidence_refs(turn)]
            anchors = await _averified_user_anchors(sessions, conversation_id, refs)
            evidence_exists = await _aevidence_exists_map(
                sessions, conversation_id, evidence_refs
            )
            replay_task_id: uuid.UUID | None = None
            if (
                isinstance(turn, (PlanTurn, NewTaskTurn))
                and snapshot.task_id is not None
            ):
                replay_task_id = await _atask_by_intake_key(
                    sessions,
                    conversation_id,
                    _stable_intake_key(conversation_id, turn.source_message_id),
                )
            try:
                validate_intake_decision(
                    turn,
                    snapshot,
                    constraint_anchors=anchors,
                    evidence_exists=evidence_exists,
                    intake_replay_task_id=replay_task_id,
                )
                task_id, revision, disposition = await aapply_intake(
                    sessions, snapshot, turn
                )
            except TaskContractError as exc:
                failure = dict(exc.fields)
        if failure is not None:
            if len(feedback) < max_repair_attempts:
                feedback = (*feedback, failure)
                prompt = build_repair_prompt(snapshot, failure)
                continue
            raise IntakeRejection(
                failure,
                attempts=attempts,
                repaired=bool(feedback),
                model_calls=attempts,
            ) from None

        return IntakeOutcome(
            turn=turn,
            applied=task_id is not None,
            task_id=task_id,
            revision=revision,
            disposition=disposition,
            record=IntakeCallRecord(
                model_calls=attempts,
                attempts=attempts,
                repaired=bool(feedback),
                repair_feedback=feedback,
                function_tools=agent.function_tools,
            ),
        )
