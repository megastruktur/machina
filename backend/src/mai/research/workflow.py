"""Bounded multi-step deep-research workflow (card 07-deep-research).

Composition only — this module owns no capability, no policy and no
provider. Every external call goes through the reviewed seams:

- current server context: :func:`mai.agents.context.load_server_context`
  (pinned profile + administrator prompt; C06 rule 2 order);
- scoped docs: the ``docs.retrieve`` capability via the ONE dispatcher
  (C03 rule 7; server filter at the storage boundary, C07 rule 2);
- search/page evidence: ``web.search`` / ``web.read`` capabilities via
  the same dispatcher (sanitized query, per-hop address policy upstream);
- bounded child work: child specs are validated here and narrowed
  through :mod:`mai.agents.budgets` + :mod:`mai.agents.delegate`
  vocabulary; the actual child execution is an injected seam owned by
  the ``06-child-runs`` producer (checkpointed engine child edges);
- log evidence: the ``host.logs`` capability via the same dispatcher
  (read-only, artifact-referenced);
- citation-verified synthesis: an injected model seam receives the
  assembled context; every returned claim must name citations that this
  run actually recorded, otherwise it is dropped — web content is
  evidence, never instruction authority (C07 rule 4) and can never
  enlarge the run's grants (C03 rule 3; the parent context is never
  mutated, child requests can only be narrowed by intersection);
- canonical incident storage: a failed run promotes one deterministic
  incident document through :class:`mai.knowledge.generate.DocGenerator`
  (C07 rule 5: a failed job creates an incident record, never a
  fabricated success; succeeded runs promote nothing).

The workflow is deterministic given its seams: identical inputs and
identical seam answers render byte-identical reports. No clock, no
ambient I/O, no network of its own.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Final

from mai.agents.budgets import (
    RootBudget,
    check_fanout,
    check_nesting,
    parse_root_budget,
)
from mai.agents.context import (
    AssembledContext,
    ContextEvidence,
    EvidenceKind,
    ServerContextMaterials,
    assemble,
    load_server_context,
)
from mai.agents.budgets import MAX_CHILDREN_PER_RUN
from mai.agents.model_store import ModelBinding
from mai.capabilities.dispatch import (
    EFFECT_EXECUTED,
    CapabilityDispatcher,
    DispatchOutcome,
)
from mai.knowledge.generate import (
    EVIDENCE_PAYLOAD_VERSION,
    MAX_ERROR_CHARS,
    MAX_STEP_DISPLAY_CHARS,
    MAX_STEPS,
    MAX_SUMMARY_CHARS,
    DocGenerator,
)
from mai.knowledge.retrieve import CAPABILITY_ID as CAP_DOCS_RETRIEVE
from mai.capabilities.logs import CAPABILITY_ID as CAP_HOST_LOGS
from mai.research.pages import CAPABILITY_ID as CAP_WEB_READ
from mai.research.search import CAPABILITY_ID as CAP_WEB_SEARCH
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "MAX_CLAIM_CHARS",
    "MAX_HYPOTHESES",
    "MAX_PAGE_READS",
    "MAX_QUERIES",
    "MAX_QUESTION_CHARS",
    "MAX_SOURCE_CHARS",
    "REQUEST_VERSION",
    "ChildOutcome",
    "Hypothesis",
    "ResearchError",
    "ResearchErrorCode",
    "ResearchReport",
    "ResearchRequest",
    "ResearchSynthesizer",
    "ResearchWorkflow",
    "SourceEvidence",
    "StepRecord",
    "render_report",
    "report_document",
    "request_from_payload",
]

#: The payload document version this freeze validates.
REQUEST_VERSION: int = 1

#: Operator-scale bounds (never corpus-scale; C06 rule 3).
MAX_QUESTION_CHARS: int = 1024
MAX_QUERIES: int = 8
MAX_QUERY_CHARS: int = 512
MAX_PAGE_READS: int = 8
MAX_LOG_QUERIES: int = 4
MAX_LOG_SERVICE_CHARS: int = 64
MAX_LOG_LINES: int = 200
MAX_CHILD_TASK_CHARS: int = 2048
MAX_CHILD_NAME_CHARS: int = 128
MAX_CHILD_CAPABILITIES: int = 16
MAX_CHILDREN: Final[int] = MAX_CHILDREN_PER_RUN

#: The role handed to context assembly (single line, no control chars).
ROLE: Final[str] = "deep-research"

#: Retrieval fan-in: bounded excerpts per phase, never the corpus.
RETRIEVE_LIMIT: int = 8
MAX_SOURCES_PER_PHASE: int = 16

#: Report-side bounds.
MAX_SOURCE_CHARS: int = 2048
MAX_HYPOTHESES: int = 16
MAX_CLAIM_CHARS: int = 1024

#: Bounded child-wait defaults (the yielding parent's parking budgets).
DEFAULT_CHILDREN_WAIT_SECONDS: float = 60.0
DEFAULT_CHILDREN_POLL_SECONDS: float = 0.5
MAX_CHILDREN_WAIT_SECONDS: float = 300.0
MAX_CHILDREN_POLL_SECONDS: float = 30.0

#: Frozen trust labels for report sources.
TRUST_SERVER_SCOPED: Final[str] = "server_scoped"
TRUST_UNTRUSTED_EXTERNAL: Final[str] = "untrusted_external"
TRUST_DELEGATED_CHILD: Final[str] = "delegated_child"


# ---------------------------------------------------------------------------
# Error vocabulary (C01 envelope: stable machine-readable codes)
# ---------------------------------------------------------------------------


class ResearchErrorCode(StrEnum):
    """Stable machine-readable rejection codes (fail closed)."""

    INVALID_PAYLOAD = "invalid_payload"
    INVALID_WIRING = "invalid_wiring"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    SYNTHESIS_UNAVAILABLE = "synthesis_unavailable"
    CHILDREN_UNAVAILABLE = "children_unavailable"


class ResearchError(ValueError):
    """One structured, value-free workflow rejection."""

    def __init__(self, code: ResearchErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: ResearchErrorCode, detail: str) -> ResearchError:
    return ResearchError(code, detail)


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"{field} must be a non-empty string",
        )
    if len(value) > maximum:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"{field} exceeds {maximum} characters",
        )
    return value


# ---------------------------------------------------------------------------
# Request (strict, value-free; every bound checked before any effect)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogQuerySpec:
    """One bounded read-only log request."""

    service: str
    lines: int


@dataclass(frozen=True, slots=True)
class ChildSpecRequest:
    """One explicitly scoped child task request (C06 rule 5).

    ``capabilities`` is the child's request only — the effective child
    grants are narrowed downstream (intersection with the parent set);
    this module never unions, never defaults.
    """

    task: str
    role: str | None
    connection: str | None
    profile: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """The validated workflow payload (phase fan-in budgets included)."""

    question: str
    search_queries: tuple[str, ...]
    page_reads: tuple[str, ...]
    logs: tuple[LogQuerySpec, ...]
    children: tuple[ChildSpecRequest, ...]
    children_wait_seconds: float
    children_poll_seconds: float
    parent_depth: int
    root_budget: RootBudget | None
    synthesize: bool


def _bounded_str_list(
    value: object, field: str, maximum_items: int, maximum_chars: int
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _reject(ResearchErrorCode.INVALID_PAYLOAD, f"{field} must be a list")
    if len(value) > maximum_items:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"{field} exceeds {maximum_items} entries",
        )
    return tuple(
        _bounded_text(item, f"{field}[{index}]", maximum_chars)
        for index, item in enumerate(value)
    )


def request_from_payload(payload: object) -> ResearchRequest:
    """Validate one workflow payload; fail closed before any dispatch.

    Strict shape (the frozen field set at ``v`` = this freeze's
    version); every fan-in list is count- and size-bounded so the
    workflow cannot be turned into a fan-out amplifier. The budget
    document parses through the ``06-delegation-budgets`` vocabulary —
    a malformed budget is a payload rejection, never a narrower guess.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object",
        )
    allowed = {
        "v",
        "question",
        "search_queries",
        "page_reads",
        "logs",
        "children",
        "children_wait_seconds",
        "children_poll_seconds",
        "parent_depth",
        "root_budget",
        "synthesize",
    }
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"unknown field {unknown[0]!r}",
        )
    missing = {"v", "question"} - set(payload)
    if missing:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"missing field {sorted(missing)[0]!r}",
        )
    if payload["v"] != REQUEST_VERSION:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"v must be {REQUEST_VERSION}",
        )
    question = _bounded_text(payload["question"], "question", MAX_QUESTION_CHARS)
    search_queries = _bounded_str_list(
        payload.get("search_queries", []), "search_queries", MAX_QUERIES, MAX_QUERY_CHARS
    )
    page_reads = _bounded_str_list(
        payload.get("page_reads", []), "page_reads", MAX_PAGE_READS, MAX_QUESTION_CHARS
    )
    logs_raw = payload.get("logs", [])
    if not isinstance(logs_raw, list):
        raise _reject(ResearchErrorCode.INVALID_PAYLOAD, "logs must be a list")
    if len(logs_raw) > MAX_LOG_QUERIES:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"logs exceeds {MAX_LOG_QUERIES} entries",
        )
    logs: list[LogQuerySpec] = []
    for index, item in enumerate(logs_raw):
        if not isinstance(item, Mapping):
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD, f"logs[{index}] must be an object"
            )
        service = _bounded_text(item.get("service"), f"logs[{index}].service", MAX_LOG_SERVICE_CHARS)
        lines_raw = item.get("lines", MAX_LOG_LINES)
        if (
            isinstance(lines_raw, bool)
            or not isinstance(lines_raw, int)
            or not 1 <= lines_raw <= MAX_LOG_LINES
        ):
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD,
                f"logs[{index}].lines must be an integer in 1..{MAX_LOG_LINES}",
            )
        logs.append(LogQuerySpec(service=service, lines=lines_raw))
    children_raw = payload.get("children", [])
    if not isinstance(children_raw, list):
        raise _reject(ResearchErrorCode.INVALID_PAYLOAD, "children must be a list")
    if len(children_raw) > MAX_CHILDREN:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"children exceeds {MAX_CHILDREN} entries",
        )
    children: list[ChildSpecRequest] = []
    for index, item in enumerate(children_raw):
        if not isinstance(item, Mapping):
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD,
                f"children[{index}] must be an object",
            )
        allowed_child = {"task", "role", "connection", "profile", "capabilities"}
        unknown_child = sorted(str(key) for key in item if key not in allowed_child)
        if unknown_child:
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD,
                f"children[{index}] has unknown field {unknown_child[0]!r}",
            )
        task = _bounded_text(item.get("task"), f"children[{index}].task", MAX_CHILD_TASK_CHARS)
        role = item.get("role")
        connection = item.get("connection")
        profile = item.get("profile")
        for field, value in (("role", role), ("connection", connection), ("profile", profile)):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > MAX_CHILD_NAME_CHARS
            ):
                raise _reject(
                    ResearchErrorCode.INVALID_PAYLOAD,
                    f"children[{index}].{field} must be a string of "
                    f"1..{MAX_CHILD_NAME_CHARS} characters or null",
                )
        capabilities_raw = item.get("capabilities", [])
        capabilities = _bounded_str_list(
            capabilities_raw,
            f"children[{index}].capabilities",
            MAX_CHILD_CAPABILITIES,
            MAX_CHILD_NAME_CHARS,
        )
        children.append(
            ChildSpecRequest(
                task=task,
                role=role,
                connection=connection,
                profile=profile,
                capabilities=capabilities,
            )
        )
    wait = _bounded_seconds(
        payload.get("children_wait_seconds", DEFAULT_CHILDREN_WAIT_SECONDS),
        "children_wait_seconds",
        0.1,
        MAX_CHILDREN_WAIT_SECONDS,
    )
    poll = _bounded_seconds(
        payload.get("children_poll_seconds", DEFAULT_CHILDREN_POLL_SECONDS),
        "children_poll_seconds",
        0.05,
        MAX_CHILDREN_POLL_SECONDS,
    )
    parent_depth = payload.get("parent_depth", 0)
    if isinstance(parent_depth, bool) or not isinstance(parent_depth, int) or parent_depth < 0:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            "parent_depth must be a non-negative integer",
        )
    try:
        root_budget = parse_root_budget(payload.get("root_budget"))
    except Exception as exc:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"root_budget does not parse: {type(exc).__name__}",
        ) from None
    # The budget narrowing is the delegation contract (BUDGET-CAP): a
    # budgeted tree rejects an over-fan-out delegation BEFORE any child
    # exists. Unbudgeted trees are checked against the module bounds.
    if children:
        try:
            check_fanout(len(children), root_budget)
            check_nesting(parent_depth, root_budget)
        except Exception as exc:
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD,
                f"delegation exceeds its budget: {type(exc).__name__}",
            ) from None
    synthesize = payload.get("synthesize", False)
    if not isinstance(synthesize, bool):
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            "synthesize must be a boolean",
        )
    return ResearchRequest(
        question=question,
        search_queries=search_queries,
        page_reads=page_reads,
        logs=tuple(logs),
        children=tuple(children),
        children_wait_seconds=wait,
        children_poll_seconds=poll,
        parent_depth=parent_depth,
        root_budget=root_budget,
        synthesize=synthesize,
    )


def _bounded_seconds(value: object, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD, f"{field} must be a number"
        )
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise _reject(
            ResearchErrorCode.INVALID_PAYLOAD,
            f"{field} must be within {minimum}..{maximum} seconds",
        )
    return parsed


# ---------------------------------------------------------------------------
# Evidence, hypotheses and step records (the report vocabulary)
# ---------------------------------------------------------------------------


class SourceStatus(StrEnum):
    """How one source answered; a denial/unavailable is never an excerpt."""

    OK = "ok"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """One recorded source: citation, trust label and bounded excerpt.

    ``trust`` is load-bearing: ``untrusted_external`` content is quoted
    evidence for the synthesis seam and is never promoted to system
    authority, policy or grants.
    """

    kind: str
    citation: str
    trust: str
    text: str
    truncated: bool
    status: SourceStatus
    error_type: str | None = None
    #: Opaque evidence-artifact handles (`artifact:<path>#<sha256>`) this
    #: source was read from; empty when the producer returned no artifact.
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One synthesis claim with its recorded citation basis.

    A hypothesis is never host truth: it is the model's claim narrowed
    to citations this run actually recorded (unknown basis references
    are dropped before the claim survives).
    """

    claim: str
    basis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One settled workflow step (the incident/runbook evidence shape)."""

    seq: int
    phase: str
    capability_id: str
    status: str  # "succeeded" | "failed" | "cancelled"
    display: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChildSpec:
    """One explicitly scoped child task (C06 rule 5: no arbitrary scope).

    ``capabilities`` is the child's REQUEST; the effective child grants
    are its intersection with the parent's granted set (GRANT-INTERSECT).
    There is deliberately no server/target field: a child runs on its
    parent's server, full stop. Owned here since the D09 deletion removed
    the old ``delegate`` module definition; the shape is unchanged.
    """

    task: str
    role: str | None
    connection: str | None
    profile: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """One settled child run as returned by the child-runner seam."""

    run_id: str
    status: str  # "succeeded" | "failed" | "cancelled"
    summary: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """The settled workflow outcome plus its optional canonical incident."""

    run_id: uuid.UUID
    server_id: uuid.UUID
    status: str  # "succeeded" | "failed"
    question: str
    steps: tuple[StepRecord, ...]
    sources: tuple[SourceEvidence, ...]
    hypotheses: tuple[Hypothesis, ...]
    dropped_claims: int
    prompt_revision: int | None
    prompt_sha256: str | None
    profile_revision: int | None
    error: str | None
    incident: Mapping[str, object] | None
    incident_error: str | None


class ResearchSynthesizer(Protocol):
    """The injected model seam (C06 rule 6: plumbing tests use fakes).

    The implementation receives the exact assembled context (system
    authority is the server prompt alone; every source is a fenced,
    boundary-tagged evidence block) and returns claims. The workflow —
    not the model — owns citation verification.
    """

    async def synthesize(
        self,
        *,
        context: AssembledContext,
        sources: Sequence[SourceEvidence],
    ) -> Sequence[Hypothesis]: ...


#: The materials seam: current pinned server context for one assembly.
MaterialsLoader = Callable[[uuid.UUID], ServerContextMaterials]

#: The child seam: bounded child execution owned by ``06-child-runs``.
ChildRunner = Callable[
    [Sequence[ChildSpec], ExecutionContext, float, float],
    Awaitable[Sequence[ChildOutcome]],
]


def _add_step(
    steps: list[StepRecord],
    phase: str,
    capability_id: str,
    outcome: str,
    display: str,
    refs: Sequence[str] = (),
) -> None:
    clipped, _ = _clip(display, MAX_STEP_DISPLAY_CHARS)
    steps.append(
        StepRecord(
            seq=len(steps) + 1,
            phase=phase,
            capability_id=capability_id,
            status=outcome,
            display=clipped,
            evidence_refs=tuple(refs)[:16],
        )
    )


def _clip(text: str, maximum: int) -> tuple[str, bool]:
    if len(text) <= maximum:
        return text, False
    return text[:maximum], True


def _log_artifact_refs(document: Mapping[str, object]) -> tuple[str, ...]:
    """The opaque artifact handle(s) one host.logs document was backed by."""
    artifact = document.get("artifact")
    if not isinstance(artifact, Mapping):
        return ()
    artifact_path = str(artifact.get("path") or "")
    artifact_sha = str(artifact.get("sha256") or "")
    if not artifact_path:
        return ()
    return (f"artifact:{artifact_path}#{artifact_sha}",)


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


class ResearchWorkflow:
    """The bounded deep-research composition (one run = one report).

    Every external effect flows through the constructor-injected
    dispatcher; the context, budget and document producers are the
    reviewed modules this card composes. Nothing here decides policy,
    widens grants or invents a second call path.
    """

    def __init__(
        self,
        dispatcher: CapabilityDispatcher,
        *,
        materials_loader: MaterialsLoader | None = None,
        data_root: object | None = None,
        synthesizer: ResearchSynthesizer | None = None,
        children_runner: ChildRunner | None = None,
        generator: DocGenerator | None = None,
    ) -> None:
        if not isinstance(dispatcher, CapabilityDispatcher):
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "dispatcher must be a CapabilityDispatcher",
            )
        if (materials_loader is None) == (data_root is None):
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "exactly one of materials_loader or data_root is required",
            )
        if materials_loader is None:
            root = data_root

            def materials_loader(server_id: uuid.UUID) -> ServerContextMaterials:
                return load_server_context(root, server_id)  # type: ignore[arg-type]

        if synthesizer is not None and not callable(
            getattr(synthesizer, "synthesize", None)
        ):
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "synthesizer must expose an async synthesize()",
            )
        if children_runner is not None and not callable(children_runner):
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "children_runner must be an async callable",
            )
        if generator is not None and not isinstance(generator, DocGenerator):
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "generator must be a DocGenerator",
            )
        self._dispatcher = dispatcher
        self._materials_loader = materials_loader
        self._synthesizer = synthesizer
        self._children_runner = children_runner
        self._generator = generator

    def __repr__(self) -> str:
        return (
            f"<ResearchWorkflow dispatcher={self._dispatcher!r} "
            f"synthesizer={self._synthesizer is not None} "
            f"children_runner={self._children_runner is not None} "
            f"generator={self._generator is not None}>"
        )

    async def run(
        self,
        *,
        context: ExecutionContext,
        payload: Mapping[str, object],
        binding: ModelBinding | None = None,
    ) -> ResearchReport:
        """Execute one bounded research run; return the settled report.

        Order: validate (no effect possible) → context pin → scoped docs
        → search → pages → logs → bounded children → assembly →
        synthesis → optional incident promotion. Capability denials and
        unavailability are recorded honestly per step and never crash the
        run; seam failures (context load, child runner, synthesizer) are
        the run's failure boundary and promote a canonical incident when
        a generator is wired.
        """
        if not isinstance(context, ExecutionContext):
            raise _reject(
                ResearchErrorCode.INVALID_PAYLOAD,
                "context must be an ExecutionContext",
            )
        request = request_from_payload(payload)
        if request.children and self._children_runner is None:
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "payload requests children but no children_runner is wired",
            )
        if request.synthesize and self._synthesizer is None:
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "payload requests synthesis but no synthesizer is wired",
            )
        if request.synthesize and binding is None:
            raise _reject(
                ResearchErrorCode.INVALID_WIRING,
                "payload requests synthesis but no model binding is pinned",
            )

        steps: list[StepRecord] = []
        sources: list[SourceEvidence] = []
        hypotheses: list[Hypothesis] = ()
        dropped_claims = 0
        error: str | None = None
        status = "succeeded"
        materials: ServerContextMaterials | None = None

        def step(
            phase: str,
            capability_id: str,
            outcome: str,
            display: str,
            refs: Sequence[str] = (),
        ) -> None:
            clipped, _ = _clip(display, MAX_STEP_DISPLAY_CHARS)
            steps.append(
                StepRecord(
                    seq=len(steps) + 1,
                    phase=phase,
                    capability_id=capability_id,
                    status=outcome,
                    display=clipped,
                    evidence_refs=tuple(refs)[:16],
                )
            )

        # -- Phase 1: current server context (fatal seam) ---------------
        try:
            materials = self._materials_loader(context.server_id)
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            status = "failed"
            error = _clip(str(exc) or type(exc).__name__, MAX_ERROR_CHARS)[0]
            step("context", "server.context", "failed", f"context load: {error}")
            return await self._finish(
                context=context,
                request=request,
                status=status,
                error=error,
                steps=steps,
                sources=sources,
                hypotheses=hypotheses,
                dropped_claims=dropped_claims,
                materials=materials,
            )
        step(
            "context",
            "server.context",
            "succeeded",
            f"prompt rev {materials.prompt_revision} pinned",
            refs=[f"prompt:{materials.prompt_sha256}"],
        )

        # -- Phase 2: scoped docs (server filter upstream, C07 rule 2) --
        outcome = await self._dispatch(
            context,
            CAP_DOCS_RETRIEVE,
            {"query": request.question, "scope": "server", "limit": RETRIEVE_LIMIT},
        )
        document = self._record_capability(
            outcome,
            phase="docs",
            source_kind="scoped_doc",
            trust=TRUST_SERVER_SCOPED,
            steps=steps,
            sources=sources,
            step_display="docs.retrieve scope=server",
        )
        if document is not None:
            for excerpt in document.get("excerpts", [])[:MAX_SOURCES_PER_PHASE]:
                if not isinstance(excerpt, Mapping):
                    continue
                citation = str(excerpt.get("citation") or "")
                text = str(excerpt.get("excerpt") or "")
                clipped_text, clipped = _clip(text, MAX_SOURCE_CHARS)
                sources.append(
                    SourceEvidence(
                        kind="scoped_doc",
                        citation=citation,
                        trust=TRUST_SERVER_SCOPED,
                        text=clipped_text,
                        truncated=bool(excerpt.get("truncated")) or clipped,
                        status=SourceStatus.OK,
                    )
                )

        # -- Phase 3: sanitized web search ------------------------------
        for query in request.search_queries:
            outcome = await self._dispatch(
                context, CAP_WEB_SEARCH, {"query": query}
            )
            document = self._record_capability(
                outcome,
                phase="search",
                source_kind="web_search",
                trust=TRUST_UNTRUSTED_EXTERNAL,
                steps=steps,
                sources=sources,
                step_display=f"web.search {query}",
            )
            if document is None:
                continue
            for hit in document.get("results", [])[:MAX_SOURCES_PER_PHASE]:
                if not isinstance(hit, Mapping):
                    continue
                citation = str(hit.get("citation") or hit.get("url") or "")
                snippet = str(hit.get("snippet") or "")
                text, clipped = _clip(snippet, MAX_SOURCE_CHARS)
                sources.append(
                    SourceEvidence(
                        kind="web_search_hit",
                        citation=citation,
                        trust=TRUST_UNTRUSTED_EXTERNAL,
                        text=text,
                        truncated=bool(hit.get("truncated")) or clipped,
                        status=SourceStatus.OK,
                    )
                )

        # -- Phase 4: bounded page reads --------------------------------
        for url in request.page_reads:
            outcome = await self._dispatch(context, CAP_WEB_READ, {"url": url})
            document = self._record_capability(
                outcome,
                phase="pages",
                source_kind="web_page",
                trust=TRUST_UNTRUSTED_EXTERNAL,
                steps=steps,
                sources=sources,
                step_display=f"web.read {url}",
            )
            if document is None:
                continue
            markdown = str(document.get("markdown") or "")
            text, clipped = _clip(markdown, MAX_SOURCE_CHARS)
            sources.append(
                SourceEvidence(
                    kind="web_page",
                    citation=str(document.get("citation") or document.get("final_url") or url),
                    trust=TRUST_UNTRUSTED_EXTERNAL,
                    text=text,
                    truncated=bool(document.get("truncated")) or clipped,
                    status=SourceStatus.OK,
                )
            )

        # -- Phase 5: read-only log evidence ----------------------------
        for log_query in request.logs:
            outcome = await self._dispatch(
                context,
                CAP_HOST_LOGS,
                {"source": "journal", "service": log_query.service, "lines": log_query.lines},
            )
            document = self._record_capability(
                outcome,
                phase="logs",
                source_kind="host_logs",
                trust=TRUST_SERVER_SCOPED,
                steps=steps,
                sources=sources,
                step_display=f"host.logs {log_query.service}",
                refs_from_document=_log_artifact_refs,
            )
            if document is None:
                continue
            excerpt = str(document.get("excerpt") or "")
            text, clipped = _clip(excerpt, MAX_SOURCE_CHARS)
            refs = _log_artifact_refs(document)
            sources.append(
                SourceEvidence(
                    kind="host_logs",
                    citation=f"host.logs:{log_query.service}",
                    trust=TRUST_SERVER_SCOPED,
                    text=text,
                    truncated=bool(document.get("excerpt_truncated")) or clipped,
                    status=SourceStatus.OK,
                    artifact_refs=refs,
                )
            )

        # -- Phase 6: bounded child work (explicit scope only) ----------
        if request.children:
            specs = tuple(
                ChildSpec(
                    task=child.task,
                    role=child.role,
                    connection=child.connection,
                    profile=child.profile,
                    capabilities=child.capabilities,
                )
                for child in request.children
            )
            try:
                outcomes = await self._children_runner(  # type: ignore[misc]
                    specs, context, request.children_wait_seconds, request.children_poll_seconds
                )
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                status = "failed"
                error = _clip(str(exc) or type(exc).__name__, MAX_ERROR_CHARS)[0]
                step("children", "agent.delegate", "failed", f"child seam: {error}")
            else:
                step(
                    "children",
                    "agent.delegate",
                    "succeeded",
                    f"{len(outcomes)} child run(s) settled",
                    refs=[f"run:{child.run_id}" for child in outcomes],
                )
                for child in outcomes:
                    text, _ = _clip(child.summary, MAX_SOURCE_CHARS)
                    sources.append(
                        SourceEvidence(
                            kind="child_run",
                            citation=f"run:{child.run_id}",
                            trust=TRUST_DELEGATED_CHILD,
                            text=text,
                            truncated=False,
                            status=SourceStatus.OK,
                        )
                    )

        # -- Phase 7: bounded assembly + citation-verified synthesis ----
        if status == "succeeded" and request.synthesize:
            assembled = self._assemble(binding, context, request, materials, sources)
            try:
                raw = await self._synthesizer.synthesize(  # type: ignore[union-attr]
                    context=assembled, sources=tuple(sources)
                )
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                status = "failed"
                error = _clip(str(exc) or type(exc).__name__, MAX_ERROR_CHARS)[0]
                step("synthesis", "model.synthesize", "failed", f"synthesizer: {error}")
            else:
                citations = {source.citation for source in sources}
                kept: list[Hypothesis] = []
                for item in list(raw)[:MAX_HYPOTHESES]:
                    if not isinstance(item, Hypothesis):
                        dropped_claims += 1
                        continue
                    claim, clipped = _clip(item.claim, MAX_CLAIM_CHARS)
                    basis = tuple(ref for ref in item.basis if ref in citations)
                    if not claim.strip() or not basis:
                        dropped_claims += 1
                        continue
                    kept.append(Hypothesis(claim=claim, basis=basis))
                    if clipped or len(basis) != len(item.basis):
                        dropped_claims += 1
                hypotheses = tuple(kept)
                step(
                    "synthesis",
                    "model.synthesize",
                    "succeeded",
                    f"{len(hypotheses)} hypothesis(es), "
                    f"{dropped_claims} unverifiable claim(s) dropped",
                )

        return await self._finish(
            context=context,
            request=request,
            status=status,
            error=error,
            steps=steps,
            sources=sources,
            hypotheses=hypotheses,
            dropped_claims=dropped_claims,
            materials=materials,
        )

    # -- internal helpers ------------------------------------------------

    async def _dispatch(
        self,
        context: ExecutionContext,
        capability_id: str,
        payload: Mapping[str, object],
    ) -> DispatchOutcome:
        return await self._dispatcher.dispatch(
            context=context,
            capability_id=capability_id,
            payload=payload,
        )

    def _record_capability(
        self,
        outcome: DispatchOutcome,
        *,
        phase: str,
        source_kind: str,
        trust: str,
        steps: list[StepRecord],
        sources: list[SourceEvidence],
        step_display: str,
        refs_from_document: Callable[[Mapping[str, object]], Sequence[str]]
        | None = None,
    ) -> Mapping[str, object] | None:
        """Record one dispatch outcome; return the parsed document if ok.

        ``refs_from_document`` derives the succeeded step's evidence refs
        from the parsed document (e.g. the host.logs artifact handle), so
        the step record — and the incident payload built from it — cites
        the exact artifact the source was read from.
        """
        if not outcome.allowed:
            _add_step(
                steps,
                phase,
                outcome.capability_id,
                "failed",
                f"{step_display}: {outcome.reason}",
            )
            sources.append(
                SourceEvidence(
                    kind=source_kind,
                    citation="",
                    trust=trust,
                    text="",
                    truncated=False,
                    status=SourceStatus.DENIED,
                )
            )
            return None
        if outcome.effect != EFFECT_EXECUTED or outcome.output is None:
            _add_step(
                steps,
                phase,
                outcome.capability_id,
                "failed",
                f"{step_display}: effect={outcome.effect}",
            )
            sources.append(
                SourceEvidence(
                    kind=source_kind,
                    citation="",
                    trust=trust,
                    text="",
                    truncated=False,
                    status=SourceStatus.UNAVAILABLE,
                    error_type=outcome.error_type,
                )
            )
            return None
        document = json.loads(outcome.output.text)
        if document.get("status") not in ("ok", "succeeded"):
            _add_step(
                steps,
                phase,
                outcome.capability_id,
                "failed",
                f"{step_display}: status={document.get('status')}",
            )
            sources.append(
                SourceEvidence(
                    kind=source_kind,
                    citation="",
                    trust=trust,
                    text="",
                    truncated=False,
                    status=SourceStatus.UNAVAILABLE,
                    error_type=document.get("error_type"),
                )
            )
            return None
        refs: tuple[str, ...] = ()
        if refs_from_document is not None:
            refs = tuple(refs_from_document(document))
        _add_step(
            steps, phase, outcome.capability_id, "succeeded", step_display, refs=refs
        )
        return document

    def _assemble(
        self,
        binding: ModelBinding | None,
        context: ExecutionContext,
        request: ResearchRequest,
        materials: ServerContextMaterials,
        sources: Sequence[SourceEvidence],
    ) -> AssembledContext:
        """Pin the bounded model context: sources are evidence blocks only.

        Kind mapping follows C06 rule 2/3: retrieved docs are ``doc``,
        log artifacts are ``log``, web material and child summaries are
        ``tool_output``. ``server_id`` binds every item to the run's
        server (scoped docs were filtered upstream at the storage
        boundary); nothing from the web becomes system authority.
        """
        kind_map = {
            "scoped_doc": EvidenceKind.DOC,
            "host_logs": EvidenceKind.LOG,
        }
        evidence = []
        for source in sources:
            if source.status is not SourceStatus.OK or not source.text:
                continue
            kind = kind_map.get(source.kind, EvidenceKind.TOOL_OUTPUT)
            evidence.append(
                ContextEvidence(
                    kind=kind,
                    source=source.citation or source.kind,
                    source_revision=source.citation,
                    text=source.text,
                    server_id=context.server_id,
                )
            )
        return assemble(
            binding,
            materials,
            role=ROLE,
            task=request.question,
            evidence=evidence,
        )

    async def _finish(
        self,
        *,
        context: ExecutionContext,
        request: ResearchRequest,
        status: str,
        error: str | None,
        steps: list[StepRecord],
        sources: list[SourceEvidence],
        hypotheses: tuple[Hypothesis, ...],
        dropped_claims: int,
        materials: ServerContextMaterials | None,
    ) -> ResearchReport:
        incident: Mapping[str, object] | None = None
        incident_error: str | None = None
        if status == "failed" and self._generator is not None:
            payload = self._incident_payload(context, request, error, steps)
            try:
                result = await self._generator.generate(payload)
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                incident_error = _clip(str(exc) or type(exc).__name__, MAX_ERROR_CHARS)[0]
            else:
                incident = {
                    "doc_id": str(result.doc.doc_id),
                    "namespace": result.doc.namespace.value,
                    "classification": result.classification.value,
                    "verification": result.verification.value,
                    "revision": result.doc.revision,
                }
        return ResearchReport(
            run_id=context.run_id,
            server_id=context.server_id,
            status=status,
            question=request.question,
            steps=tuple(steps[:MAX_STEPS]),
            sources=tuple(sources),
            hypotheses=hypotheses,
            dropped_claims=dropped_claims,
            prompt_revision=None if materials is None else materials.prompt_revision,
            prompt_sha256=None if materials is None else materials.prompt_sha256,
            profile_revision=None if materials is None else materials.profile_revision,
            error=error,
            incident=incident,
            incident_error=incident_error,
        )

    @staticmethod
    def _incident_payload(
        context: ExecutionContext,
        request: ResearchRequest,
        error: str | None,
        steps: list[StepRecord],
    ) -> dict[str, object]:
        """The settled-failure evidence document (C07 rule 5 incident)."""
        summary, _ = _clip(
            f"deep-research run failed: {request.question}", MAX_SUMMARY_CHARS
        )
        return {
            "v": EVIDENCE_PAYLOAD_VERSION,
            "run_id": str(context.run_id),
            "server_id": str(context.server_id),
            "status": "failed",
            "host_verified": False,
            "summary": summary,
            "error": error,
            "steps": [
                {
                    "seq": record.seq,
                    "capability_id": record.capability_id,
                    "status": record.status,
                    "verified": False,
                    "display": record.display,
                    "exit_code": None,
                    "evidence_refs": list(record.evidence_refs),
                }
                for record in steps[:MAX_STEPS]
            ],
        }


# ---------------------------------------------------------------------------
# Report rendering (deterministic; sources vs hypotheses kept distinct)
# ---------------------------------------------------------------------------


def report_document(report: ResearchReport) -> dict[str, object]:
    """The frozen JSON view of one report (stable key order)."""
    return {
        "v": REQUEST_VERSION,
        "run_id": str(report.run_id),
        "server_id": str(report.server_id),
        "status": report.status,
        "question": report.question,
        "prompt_revision": report.prompt_revision,
        "prompt_sha256": report.prompt_sha256,
        "profile_revision": report.profile_revision,
        "error": report.error,
        "steps": [
            {
                "seq": step.seq,
                "phase": step.phase,
                "capability_id": step.capability_id,
                "status": step.status,
                "display": step.display,
                "evidence_refs": list(step.evidence_refs),
            }
            for step in report.steps
        ],
        "sources": [
            {
                "kind": source.kind,
                "citation": source.citation,
                "trust": source.trust,
                "status": source.status.value,
                "truncated": source.truncated,
                "error_type": source.error_type,
                "artifact_refs": list(source.artifact_refs),
                "text": source.text,
            }
            for source in report.sources
        ],
        "hypotheses": [
            {"claim": hypothesis.claim, "basis": list(hypothesis.basis)}
            for hypothesis in report.hypotheses
        ],
        "dropped_claims": report.dropped_claims,
        "incident": None if report.incident is None else dict(report.incident),
        "incident_error": report.incident_error,
    }


def render_report(report: ResearchReport) -> str:
    """Render the deterministic Markdown report (pure).

    ``## Sources`` quotes every recorded source under its trust label;
    ``## Hypotheses`` carries only citation-backed claims and stays
    visibly empty (with an explicit notice) when nothing verifiable was
    produced. A failed run's canonical incident reference is recorded,
    never a fabricated procedure (C07 rule 5).
    """
    lines: list[str] = []
    title = "Deep-research report"
    lines.append(f"# {title}: run {report.run_id}")
    lines.append("")
    lines.append(f"- status: {report.status}")
    lines.append(f"- server_id: `{report.server_id}`")
    if report.prompt_revision is not None:
        lines.append(f"- prompt_revision: {report.prompt_revision}")
        lines.append(f"- prompt_sha256: `{report.prompt_sha256}`")
    if report.profile_revision is not None:
        lines.append(f"- profile_revision: {report.profile_revision}")
    lines.append("")
    lines.append("## Question")
    lines.append("")
    lines.append(report.question)
    lines.append("")
    lines.append("## Sources")
    lines.append("")
    if not report.sources:
        lines.append("No sources were recorded.")
        lines.append("")
    for source in report.sources:
        lines.append(
            f"### [{source.kind}|{source.trust}] {source.citation or '(no citation)'}"
        )
        lines.append("")
        lines.append(f"- status: {source.status.value}")
        if source.truncated:
            lines.append("- truncated: true")
        if source.error_type is not None:
            lines.append(f"- error_type: {source.error_type}")
        if source.artifact_refs:
            handles = " ".join(f"`{ref}`" for ref in source.artifact_refs)
            lines.append(f"- artifact_refs: {handles}")
        if source.text:
            lines.append("")
            lines.append("```text")
            lines.append(source.text)
            lines.append("```")
        lines.append("")
    lines.append("## Hypotheses")
    lines.append("")
    if not report.hypotheses:
        lines.append(
            "No citation-backed hypotheses. Sources above are evidence, "
            "not conclusions; absent host verification nothing here is "
            "an instruction."
        )
        lines.append("")
    for index, hypothesis in enumerate(report.hypotheses, start=1):
        lines.append(f"{index}. {hypothesis.claim}")
        for ref in hypothesis.basis:
            lines.append(f"   - basis: {ref}")
        lines.append("")
    if report.dropped_claims:
        lines.append(
            f"_{report.dropped_claims} synthesis claim(s) dropped: no "
            "recorded citation basis._"
        )
        lines.append("")
    lines.append("## Steps")
    lines.append("")
    if not report.steps:
        lines.append("No steps were recorded.")
        lines.append("")
    for step in report.steps:
        refs = " ".join(f"`{ref}`" for ref in step.evidence_refs)
        lines.append(
            f"{step.seq}. **{step.phase}** `{step.capability_id}` "
            f"— {step.status} — {step.display}"
            + (f" — {refs}" if refs else "")
        )
    lines.append("")
    if report.error is not None:
        lines.append("## Failure")
        lines.append("")
        lines.append(report.error)
        lines.append("")
    if report.incident is not None:
        lines.append("## Canonical incident")
        lines.append("")
        lines.append(f"- doc_id: `{report.incident['doc_id']}`")
        lines.append(f"- namespace: {report.incident['namespace']}")
        lines.append(f"- classification: {report.incident['classification']}")
        lines.append(f"- verification: {report.incident['verification']}")
        lines.append(f"- revision: {report.incident['revision']}")
        lines.append("")
    if report.incident_error is not None:
        lines.append(f"incident promotion failed: {report.incident_error}")
        lines.append("")
    lines.append("---")
    lines.append(
        f"generated_by: mai.research.workflow v{REQUEST_VERSION} "
        "(deterministic composition; sources are quoted under their trust "
        "labels and hypotheses require recorded citation bases)"
    )
    return "\n".join(lines) + "\n"
