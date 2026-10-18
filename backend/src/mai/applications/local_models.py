"""Durable, approval-gated, resource-locked LOCAL MODEL service install,
model-weight pull and verified endpoint registration (card
``11-model-install``).

Consumer of the integrated producers ``mai.applications.recipes`` (the
validated self-hosted inference recipe + policy-bound plan, frozen by
``11-model-recipe`` / ``08-recipe-schema``), ``mai.applications.install``
(P08 vocabulary, receipts and lease discipline), ``mai.agents.model_store``
(06-model-store, C02-canonical model configuration) and the P03/P04/P05
seams. The dedicated path exists because the first local inference recipe
(``catalogue/local-inference.yaml``) declares NO health section (the image
carries no curl, so a rendered argv probe could never pass) — the P08
install boundary refuses exactly that shape (``health_not_declared``).
Truth for this service class is established differently, and honestly:

- **Hardware prerequisites BEFORE any effect (fail-closed).** The recipe's
  prerequisite tokens (``docker`` plus the frozen ``hw.*`` families from the
  ``11-model-recipe`` producer) are verified against REAL target
  measurements — :func:`collect_target_facts` reads the target arch, CPU
  flags, RAM, the REAL Docker data root and its free space through the
  bounded command lane — and :func:`evaluate_hardware_checks` reproduces
  the producer-frozen semantics: an unknown token, an unmeasurable fact or
  an unsatisfied bound counts as NOT satisfied. Unsatisfied hardware
  refuses before any download or start (acceptance 2 of ``11-model-recipe``),
  and before the one-use approval is consumed (a missing prerequisite must
  never spend the operator's decision).
- **Completion gate = the real OpenAI-compatible endpoint.** Deployment
  success requires the target-side loopback probe of ``GET /v1/models`` on
  the published port to answer ``200`` with a parseable model list within
  the start budget. There is no declared argv healthcheck to lean on and
  none is invented; a gate that cannot run (no curl on the target) fails
  closed.
- **Weights are a separate, explicit admin acquisition.** The plan's
  operation vocabulary has no weights op (``11-model-recipe``); this module
  adds the bounded pull workflow through the granted lifecycle tools: the
  full P03 gate (policy decision re-checked immediately before the effect,
  one-use approval consumed bound to the pull payload hash), one exclusive
  ``application:<id>`` lease with heartbeat (the SAME key the P08 paths
  use, so install/update/remove and weight pulls serialize), and the
  bounded command lane whose timeout cut IS the cancellation: a cut pull is
  ``outcome_unknown`` (the payload may live), the lease is left to lapse
  into a fence, and a re-run converges because the ollama daemon resumes
  its own partial blobs. A deterministic pull failure is verified, never
  fabricated: the partial-blob state is read and recorded in notes, and
  registration does not happen.
- **Registration only after a real response, capabilities reported
  honestly (acceptance 2).** After a verified pull the module probes
  ``GET /api/tags`` (digest + size metadata straight from the serving
  daemon) and runs ONE bounded tool-call probe via
  ``POST /v1/chat/completions``; tool-call support is declared in the
  registered connection ONLY when a real tool call came back — anything
  else (error, refusal, plain answer) registers ``tool_calls: false``.
  No capability is ever inferred from the model family name. The
  connection document is written through the C02 store pipeline
  (validated, revision-checked, atomic; deterministic per-server name);
  an existing record is converged only when byte-identical and REFUSED
  (never overwritten) when it diverges — admin configuration is not
  silently mutated by an install path.

Failure policy mirrors P08: a deterministic failure after the start effect
triggers a VERIFIED rollback (``docker compose down`` without volume flags
— data deletion is unrepresentable, C09 rule 5); the canonical manifest is
written only for fully verified success; an unparseable or missing gate
response is a failure, never an invented pass.

Pure stdlib + integrated sibling modules; no engine registration here:
composition (session factories, SSH identity resolution, providers, the
model store root) is the caller's wiring, exactly like
:class:`~mai.applications.install.InstallService`.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, sleep
from urllib.parse import urlsplit

from mai.agents.model_store import (
    ModelConfigStore,
    ModelStoreError,
    ModelStoreErrorCode,
)
from mai.agents.models import parse_connection
from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    ApplicationAdoption,
    ApplicationManifestStore,
    parse_manifest_text,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.install import (
    DEFAULT_LEASE_TTL_SECONDS,
    LOCK_RESOURCE_PREFIX,
    AppliedOperation,
    install_intent_id,
)
from mai.applications.recipes import (
    OP_CREATE_DIRECTORIES,
    OP_PULL_IMAGE,
    OP_REQUIRE_BACKUP,
    OP_START,
    OP_VERIFY_PREREQUISITES,
    OP_WAIT_HEALTHY,
    OP_WRITE_COMPOSE,
    OP_WRITE_CREDENTIALS,
    ApplicationRecipe,
    DeploymentPlan,
    PlanOperation,
    TargetEnvironment,
    build_deployment_plan,
)
from mai.credentials.env import EnvFileError, EnvResolver
from mai.execution.commands import (
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
)
from mai.jobs.resource_locks import (
    LockLease,
    ResourceLockError,
    ResourceLockService,
)
from mai.plugins.registry import CapabilityDescriptor
from mai.policy.approvals import ApprovalError, ApprovalStore
from mai.policy.evaluate import (
    ExecutionContext,
    Policy,
    PolicyDecision,
    ScopedGrants,
    evaluate,
)

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_WEIGHTS_PULL_TIMEOUT_SECONDS",
    "LOCK_RESOURCE_PREFIX",
    "ModelInstallError",
    "ModelInstallErrorCode",
    "ModelInstallRequest",
    "ModelInstallResult",
    "ModelPullRequest",
    "ModelPullResult",
    "LocalModelService",
    "collect_target_facts",
    "evaluate_hardware_checks",
]

#: The capability slug this module executes (C03 rule 1 shape). Grants name
#: this id; the descriptor wired into the service must carry it.
CAPABILITY_ID: str = "model.install"

#: ``LOCK_RESOURCE_PREFIX`` and ``DEFAULT_LEASE_TTL_SECONDS`` are re-exported
#: from the P08 executor: this module locks the SAME per-application key
#: (``application:<id>``, C05 rule 6) with the same default lease budget, so
#: installs, updates, removals and weight pulls on one application identity
#: can never interleave.

#: Weight pull bound (weights budget ≤ 5 GiB per the recipe disk token; the
#: lane's cut is the cancellation, C04 rule 5).
DEFAULT_WEIGHTS_PULL_TIMEOUT_SECONDS: float = 1800.0

# Per-lane bounds (controller-enforced; every remote command carries its own
# timeout so one stuck read can never wedge the workflow).
_FACT_TIMEOUT_SECONDS = 30.0
_DEFAULT_OP_TIMEOUT_SECONDS = 120.0
_UP_TIMEOUT_SECONDS = 600.0
_DOWN_TIMEOUT_SECONDS = 300.0
_OUTPUT_BUDGET_BYTES = 64 * 1024
_ENDPOINT_POLL_INTERVAL_SECONDS = 2.0
_ENDPOINT_START_BUDGET_SECONDS = 180.0
_TOOL_PROBE_TIMEOUT_SECONDS = 300.0
_VERIFY_TIMEOUT_SECONDS = 30.0

#: Result status vocabulary (audit receipt of one attempt).
STATUS_REFUSED = "refused"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"

#: Effect vocabulary (never claim more than was proven).
EFFECT_NOT_ATTEMPTED = "not_attempted"
EFFECT_EXECUTED = "executed"
EFFECT_UNKNOWN = "unknown"

#: Per-operation action vocabulary (verify-then-mutate receipt).
_ACTION_APPLIED = "applied"
_ACTION_VERIFIED = "verified"

#: The prerequisite token grammar this executor understands: the ``docker``
#: token plus the ``hw.*`` families frozen by the ``11-model-recipe``
#: producer. Anything else is a refusal, never silence.
_KNOWN_PREREQUISITES = frozenset({"docker"})
_HARDWARE_TOKEN_RE = re.compile(r"^hw\.(arch|cpu|ram|vram|disk)\.[a-z0-9_.+-]+$")

_GIB = 1024**3

#: Model reference grammar for one explicit weight acquisition (ollama model
#: library names such as ``qwen2.5:7b-instruct-q4_K_M``): no whitespace, no
#: shell metacharacters, no leading separator. The value travels as a single
#: argv element — the grammar is defense in depth for receipts, not the
#: quoting boundary.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

#: License metadata bounds (acceptance 1: every acquisition carries license
#: metadata; the recipe pre-authorizes none, so the admin action supplies it).
_LICENSE_MAX_CHARS = 256

#: Connection name grammar mirror (``mai.agents.models`` producer); the
#: derived per-server name must satisfy it before any store write.
_CONNECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Base-URL grammar for the registered endpoint (mirrors the connection
#: schema: absolute http(s), host present, no userinfo/query/fragment).
_BASE_URL_MAX_CHARS = 2048


# ---------------------------------------------------------------------------
# Hardware prerequisite evaluation (producer semantics, frozen by
# backend/tests/test_inference_recipe.py — fail-closed on everything)
# ---------------------------------------------------------------------------


def evaluate_hardware_checks(
    declared: frozenset[str] | set[str], facts: dict[str, object]
) -> frozenset[str]:
    """Return the declared prerequisite tokens the facts do NOT satisfy.

    Check semantics (frozen by the ``11-model-recipe`` producer):

    ================  ==================================================
    ``docker``        ``facts["docker_compose_ready"]`` is exactly True
    ``hw.arch.<a>``   ``facts["arch"]`` equals ``<a>``
    ``hw.cpu.<f>``    ``facts["cpu_flags"]`` contains ``<f>`` as a
                      whitespace-separated token
    ``hw.ram.<N>gb``  ``facts["ram_bytes"]`` >= N GiB
    ``hw.vram.<N>gb`` ``facts["vram_bytes"]`` >= N GiB (reserved; never
                      declared by the CPU-only v1 recipe)
    ``hw.disk.<N>gb`` ``facts["disk_free_bytes"]`` >= N GiB
    ================  ==================================================

    A token outside the frozen families cannot be interpreted and therefore
    counts as NOT satisfied: fail closed, never silently pass.
    """
    unsatisfied: set[str] = set()
    for token in sorted(declared):
        if token == "docker":
            if facts.get("docker_compose_ready") is not True:
                unsatisfied.add(token)
            continue
        match = _HARDWARE_TOKEN_RE.fullmatch(token)
        if match is None:
            unsatisfied.add(token)
            continue
        family = match.group(1)
        spec = token.removeprefix(f"hw.{family}.")
        if family == "arch":
            if facts.get("arch") != spec:
                unsatisfied.add(token)
        elif family == "cpu":
            flags = facts.get("cpu_flags", "")
            satisfied = isinstance(flags, str) and spec in flags.split()
            if not satisfied:
                unsatisfied.add(token)
        else:
            try:
                budget = int(spec.removesuffix("gb"))
            except ValueError:
                unsatisfied.add(token)
                continue
            key = {
                "ram": "ram_bytes",
                "vram": "vram_bytes",
                "disk": "disk_free_bytes",
            }[family]
            value = facts.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                unsatisfied.add(token)
            elif value < budget * _GIB:
                unsatisfied.add(token)
    return frozenset(unsatisfied)


def collect_target_facts(
    transport: Callable[[CommandRequest], CommandOutcome],
    server_id: uuid.UUID,
    *,
    need_vram: bool = False,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    """Measure the REAL target facts the hardware checks evaluate.

    Every fact is a bounded read through the P04 command lane; nothing is
    assumed from the plan or the recipe. Returns
    ``(facts, unknown, measurement_notes)`` where ``unknown`` names the
    measurements that could not be taken — the caller fails those checks
    closed (an absent fact never satisfies a token) — and
    ``measurement_notes`` records honest caveats about HOW a fact was
    measured.

    The disk budget is measured at the REAL Docker data root
    (``docker info --format {{.DockerRootDir}}`` → ``df -B1``), not at the
    compose workdir: images and pulled weights land under the daemon root.
    A daemon-reported root that is not statable (the common unprivileged
    case: ``/var/lib/docker`` is root-only) falls back to the nearest
    statable ancestor — the filesystem that will actually hold the data —
    with the fallback recorded in ``measurement_notes``; nothing statable
    at all leaves the disk fact unknown (fail closed). A daemon that will
    not answer makes ``docker`` AND the disk check fail closed together.
    """
    facts: dict[str, object] = {}
    unknown: list[str] = []
    measurement_notes: list[str] = []

    def read(argv: tuple[str, ...]) -> CommandOutcome | None:
        try:
            return transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=server_id,
                    display="measure model-install prerequisite fact",
                    timeout=_FACT_TIMEOUT_SECONDS,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=argv,
                )
            )
        except CommandRequestError:
            return None

    def note_unknown(what: str) -> None:
        unknown.append(what)

    arch = read(("uname", "-m"))
    if arch is not None and arch.status is CommandStatus.SUCCEEDED:
        facts["arch"] = arch.stdout.strip()
    else:
        note_unknown("arch")

    flags = read(("grep", "-m1", "^flags", "/proc/cpuinfo"))
    if flags is not None and flags.status is CommandStatus.SUCCEEDED:
        _prefix, _sep, value = flags.stdout.partition(":")
        facts["cpu_flags"] = value.strip()
    else:
        note_unknown("cpu_flags")

    meminfo = read(("awk", "/MemTotal/ {print $2}", "/proc/meminfo"))
    if meminfo is not None and meminfo.status is CommandStatus.SUCCEEDED:
        try:
            facts["ram_bytes"] = int(meminfo.stdout.strip()) * 1024
        except ValueError:
            note_unknown("ram_bytes")
    else:
        note_unknown("ram_bytes")

    daemon = read(("docker", "version", "--format", "{{.Server.Version}}"))
    compose = read(("docker", "compose", "version"))
    daemon_ok = daemon is not None and daemon.status is CommandStatus.SUCCEEDED
    compose_ok = compose is not None and compose.status is CommandStatus.SUCCEEDED
    facts["docker_compose_ready"] = daemon_ok and compose_ok
    if not daemon_ok:
        note_unknown("docker_root")
    if not compose_ok:
        note_unknown("compose_plugin")

    root = read(("docker", "info", "--format", "{{.DockerRootDir}}"))
    root_dir = ""
    if root is not None and root.status is CommandStatus.SUCCEEDED:
        root_dir = root.stdout.strip()
    if not root_dir:
        facts["docker_compose_ready"] = False
        note_unknown("docker_root")
        note_unknown("disk_free_bytes")
    else:
        # df the data root; when it is not statable, walk up to the nearest
        # statable ancestor (bounded) — the filesystem that will hold the
        # data. Nothing statable ⇒ the fact stays unknown (fail closed).
        candidate = root_dir
        measured = False
        for _depth in range(8):
            free = read(("df", "-B1", "--output=avail", "--", candidate))
            if free is not None and free.status is CommandStatus.SUCCEEDED:
                lines = [ln.strip() for ln in free.stdout.splitlines() if ln.strip()]
                try:
                    facts["disk_free_bytes"] = int(lines[-1])
                    measured = True
                except ValueError, IndexError:
                    pass
                break
            if candidate == "/":
                break
            candidate = candidate.rsplit("/", 1)[0] or "/"
        if measured:
            if candidate != root_dir:
                measurement_notes.append(
                    f"disk_free_bytes measured at {candidate}: the Docker "
                    "data root itself is not statable from the controller "
                    "session"
                )
        else:
            note_unknown("disk_free_bytes")

    if need_vram:
        vram = read(
            (
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            )
        )
        if vram is not None and vram.status is CommandStatus.SUCCEEDED:
            try:
                facts["vram_bytes"] = (
                    int(vram.stdout.strip().splitlines()[0]) * 1024 * 1024
                )
            except ValueError, IndexError:
                note_unknown("vram_bytes")
        else:
            note_unknown("vram_bytes")

    return facts, tuple(unknown), tuple(measurement_notes)


# ---------------------------------------------------------------------------
# Structured rejection (C01 vocabulary; raised only for wiring/input, never
# for gate denials or execution outcomes — those are returned as results)
# ---------------------------------------------------------------------------


class ModelInstallErrorCode(StrEnum):
    """Stable machine-readable refusal/failure codes."""

    INVALID_REQUEST = "invalid_request"
    INVALID_PLAN = "invalid_plan"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    LOCK_UNAVAILABLE = "lock_unavailable"
    EXTERNAL_RECORD_CONFLICT = "external_record_conflict"
    CROSS_SERVER_CONFLICT = "cross_server_conflict"
    STATE_DIVERGED = "state_diverged"
    PORT_CONFLICT = "port_conflict"
    BACKUP_REQUIRED = "backup_required"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    UNSUPPORTED_PREREQUISITE = "unsupported_prerequisite"
    HARDWARE_UNSATISFIED = "hardware_unsatisfied"
    SERVICE_NOT_INSTALLED = "service_not_installed"
    SERVICE_NOT_RUNNING = "service_not_running"
    ENDPOINT_UNVERIFIED = "endpoint_unverified"
    REGISTRATION_REFUSED = "registration_refused"
    APPLY_FAILED = "apply_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ModelInstallError(ValueError):
    """Structured, value-free wiring/input rejection."""

    def __init__(self, code: ModelInstallErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Requests and receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelInstallRequest:
    """One validated model-service install intent.

    ``context.server_id`` MUST equal ``server_id`` (C05 rule 3: the run
    binding names the target). ``approval_id`` is the pending one-use
    decision bound to the plan this service rebuilds — a plan hash mismatch
    is the C03 rule 4 altered-payload denial.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    #: Explicit operator confirmation for pre-existing volume data
    #: (interrupted-install resume with data on disk), C09 rule 4/5.
    backup_confirmed: bool = False

    def __post_init__(self) -> None:
        _validated_identity("server_id", self.server_id)
        _validated_identity("application_id", self.application_id)
        if not isinstance(self.recipe, ApplicationRecipe):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        _validated_identity("approval_id", self.approval_id)
        if not isinstance(self.backup_confirmed, bool):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "backup_confirmed must be a bool",
            )


@dataclass(frozen=True, slots=True)
class ModelPullRequest:
    """One explicit model-weight acquisition + endpoint registration intent.

    ``model`` is the upstream reference (e.g. ``qwen2.5:7b-instruct-q4_K_M``).
    ``license`` is REQUIRED admin metadata naming the license under which the
    operator acquires the weights (the recipe pre-authorizes no model and no
    license) and is carried value-free into the receipt. ``base_url`` is the
    controller-reachable OpenAI-compatible endpoint of the installed service
    (published host port, ``/v1``-rooted); it is validated at construction
    so a malformed endpoint refuses locally without touching any gate.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    model: str
    license: str
    base_url: str

    def __post_init__(self) -> None:
        _validated_identity("server_id", self.server_id)
        _validated_identity("application_id", self.application_id)
        if not isinstance(self.recipe, ApplicationRecipe):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        _validated_identity("approval_id", self.approval_id)
        if not isinstance(self.model, str) or not _MODEL_NAME_RE.fullmatch(self.model):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "model must be an upstream model reference without shell "
                "metacharacters",
            )
        if (
            not isinstance(self.license, str)
            or not self.license.strip()
            or len(self.license) > _LICENSE_MAX_CHARS
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in self.license)
        ):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "license metadata is required on every explicit acquisition "
                "(acceptance 1)",
            )
        if not isinstance(self.base_url, str) or not _valid_base_url(self.base_url):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "base_url must be an absolute http(s) URL with a host and no "
                "userinfo/query/fragment",
            )


@dataclass(frozen=True, slots=True)
class ModelInstallResult:
    """The audit receipt of one model-service install attempt.

    Status/effect pairs never overclaim: ``refused`` pairs with
    ``effect="not_attempted"``, ``outcome_unknown`` with
    ``effect="unknown"``; only verified success or a verified rollback
    claims ``effect="executed"``. ``endpoint_verified`` is True only when
    the real ``GET /v1/models`` loopback probe answered within budget.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    plan_sha256: str
    status: str
    effect: str
    reason: str | None
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    manifest_revision: int | None = None
    manifest_sha256: str | None = None
    endpoint_verified: bool = False
    hardware_unsatisfied: tuple[str, ...] = ()
    published_ports: tuple[int, ...] = ()
    rollback_attempted: bool = False
    rollback_verified: bool = False
    lock_released: bool = False


@dataclass(frozen=True, slots=True)
class ModelPullResult:
    """The audit receipt of one weight pull + registration attempt.

    ``digest``/``size_bytes`` are the values the serving daemon itself
    reported for the pulled model (checksum metadata, acceptance 1).
    ``tool_calls_observed`` is the HONEST probe verdict: True only when a
    real tool call came back. ``connection_*`` fields identify the C02
    store record written on verified completion.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    status: str
    effect: str
    reason: str | None
    model: str
    license: str
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    payload_sha256: str = ""
    published_port: int | None = None
    digest: str | None = None
    size_bytes: int | None = None
    tool_calls_observed: bool = False
    connection_name: str | None = None
    connection_revision: int | None = None
    connection_sha256: str | None = None
    lock_released: bool = False


def _validated_identity(field: str, value: object) -> None:
    if not isinstance(value, uuid.UUID):
        raise ModelInstallError(
            ModelInstallErrorCode.INVALID_REQUEST, f"{field} must be a uuid.UUID"
        )


def _valid_base_url(raw: str) -> bool:
    if not raw or len(raw) > _BASE_URL_MAX_CHARS:
        return False
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    return True


# ---------------------------------------------------------------------------
# Target-side command composition (base64 channel like the P08 executor)
# ---------------------------------------------------------------------------


def _write_file_command(server_id: uuid.UUID, path: str, data: bytes) -> CommandRequest:
    """Atomically provision one target file via the base64 channel."""
    directory = path.rsplit("/", 1)[0]
    encoded = base64.b64encode(data).decode("ascii")
    shell = (
        "umask 077 && "
        f"mkdir -p {shlex.quote(directory)} && "
        f"printf %s '{encoded}' | base64 -d > {shlex.quote(path)}.tmp && "
        f"mv {shlex.quote(path)}.tmp {shlex.quote(path)} && "
        f"sha256sum -- {shlex.quote(path)}"
    )
    return CommandRequest(
        op_id=uuid.uuid4(),
        server_id=server_id,
        display="provision managed application file",
        timeout=_DEFAULT_OP_TIMEOUT_SECONDS,
        output_budget=_OUTPUT_BUDGET_BYTES,
        shell=shell,
    )


def _compose_argv(plan: DeploymentPlan, *rest: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--project-name",
        plan.project_name,
        "--file",
        plan.compose_path,
        *rest,
    )


def _published_port(plan: DeploymentPlan) -> int | None:
    """First published host port of the managed service (None ⇒ none)."""
    hosts = sorted(binding.host for binding in plan.ports)
    return hosts[0] if hosts else None


def _parse_model_list(stdout: str) -> list[dict[str, object]] | None:
    """Parse one real model-list response (``None`` ⇒ not parseable).

    Accepts the OpenAI shape (``{"data": [...]}``) and the native daemon
    shape (``{"models": [...]}``); every entry must be a mapping with a
    string id/name. A key present with JSON ``null`` (the REAL fresh
    daemon's ``{"object":"list","data":null}`` while no weights exist) is
    the valid empty list; a MISSING key is not parseable (fail closed).
    """
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, Mapping):
        return None
    for key in ("data", "models"):
        if key not in document:
            continue
        entries = document[key]
        if entries is None:
            return []
        if not isinstance(entries, list):
            return None
        parsed: list[dict[str, object]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                return None
            name = entry.get("id") if "id" in entry else entry.get("name")
            if not isinstance(name, str) or not name:
                return None
            parsed.append({**entry, "name": name})
        return parsed
    return None


def _tool_call_observed(stdout: str) -> bool | None:
    """True iff one real chat completion carried a real tool call.

    ``None`` ⇒ the response is not a parseable chat completion (the probe
    verdict is then False-with-note, never an inferred capability).
    """
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    choices = document.get("choices") if isinstance(document, Mapping) else None
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        return None
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and len(tool_calls) > 0


class _LeaseHeartbeat:
    """Keep the apply's lease alive; expose a lapsed flag (fail closed)."""

    def __init__(self, locks: ResourceLockService, lease: LockLease) -> None:
        self._locks = locks
        self._lease = lease
        self._stop = threading.Event()
        self._lapsed = False
        interval = max(1.0, min(10.0, lease.lease_ttl / 3.0))
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True, name="mai-model-hb"
        )

    def _loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                alive = self._locks.heartbeat(self._lease)
            except ResourceLockError:
                alive = False
            if not alive:
                self._lapsed = True
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    @property
    def lapsed(self) -> bool:
        return self._lapsed


@dataclass(frozen=True, slots=True)
class _TargetState:
    """Verified target classification (all values read, never assumed)."""

    compose_present: bool
    compose_hash_matches: bool
    project_running: bool


#: Adoption refusals a recovery sweep may legitimately clear before ONE
#: retry (C02 W1/W2 interrupted pairs; a live prior intent).
_RECOVERABLE_ADOPTION_CODES = frozenset(
    {
        AdoptionErrorCode.STALE_REVISION,
        AdoptionErrorCode.INTENT_PENDING,
        AdoptionErrorCode.FILE_DIVERGED,
    }
)


class LocalModelService:
    """The enforced local-model boundary: identity → plan → hardware facts
    → policy → approval → lock → verify-then-mutate apply → real endpoint
    gate → canonical update (and, for pulls: verified listing → honest
    capability probe → C02 store registration).

    Wiring is constructor-checked and fail-closed (dispatch pattern). The
    grants/policy/adopted providers are called per attempt — current state
    is re-read, never cached (C03 rule 3).
    """

    def __init__(
        self,
        *,
        transport: Callable[[CommandRequest], CommandOutcome],
        locks: ResourceLockService,
        approvals: ApprovalStore,
        adoption: ApplicationAdoption,
        store: ApplicationManifestStore,
        model_store: ModelConfigStore,
        grants: Callable[[], ScopedGrants],
        policy: Callable[[], Policy],
        adopted: Callable[[uuid.UUID], bool],
        descriptor: CapabilityDescriptor,
        credentials: Callable[[], EnvResolver] | None = None,
        lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
        weights_pull_timeout: float = DEFAULT_WEIGHTS_PULL_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(transport):
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST, "transport must be callable"
            )
        for name, value in (
            ("locks", locks),
            ("approvals", approvals),
            ("adoption", adoption),
            ("store", store),
            ("model_store", model_store),
            ("descriptor", descriptor),
        ):
            expected: type = {
                "locks": ResourceLockService,
                "approvals": ApprovalStore,
                "adoption": ApplicationAdoption,
                "store": ApplicationManifestStore,
                "model_store": ModelConfigStore,
                "descriptor": CapabilityDescriptor,
            }[name]
            if not isinstance(value, expected):
                raise ModelInstallError(
                    ModelInstallErrorCode.INVALID_REQUEST,
                    f"{name} must be a {expected.__name__}",
                )
        for name, provider in (
            ("grants", grants),
            ("policy", policy),
            ("adopted", adopted),
        ):
            if not callable(provider):
                raise ModelInstallError(
                    ModelInstallErrorCode.INVALID_REQUEST, f"{name} must be callable"
                )
        if descriptor.identifier != CAPABILITY_ID:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "descriptor identifier must be model.install",
            )
        if descriptor.effect_class != "privileged":
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "model install is a privileged effect; the descriptor must "
                "declare the privileged effect class",
            )
        for name, value in (
            ("lease_ttl", lease_ttl),
            ("weights_pull_timeout", weights_pull_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelInstallError(
                    ModelInstallErrorCode.INVALID_REQUEST, f"{name} must be a number"
                )
        self._transport = transport
        self._locks = locks
        self._approvals = approvals
        self._adoption = adoption
        self._store = store
        self._model_store = model_store
        self._grants = grants
        self._policy = policy
        self._adopted = adopted
        self._descriptor = descriptor
        self._credentials = credentials
        self._lease_ttl = float(lease_ttl)
        self._weights_pull_timeout = float(weights_pull_timeout)

    # -- entry: model-service install ----------------------------------------

    async def install_service(
        self,
        request: ModelInstallRequest,
        *,
        intent_id: uuid.UUID | None = None,
    ) -> ModelInstallResult:
        """Install the recipe's model service under the full gate chain.

        ``intent_id`` defaults to the deterministic plan-derived id so a
        re-run after interruption converges the canonical update instead of
        duplicating it.
        """
        sid, aid = request.server_id, request.application_id
        plan = build_deployment_plan(request.recipe, sid, aid, request.target)
        _validate_plan_vocabulary(plan)
        refusal = await self._prelock_guards(request, plan)
        if refusal is not None:
            return refusal

        try:
            lease = self._locks.acquire(
                str(sid),
                f"{LOCK_RESOURCE_PREFIX}{aid}",
                owner_ref=f"model-install:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return self._refused(
                request,
                plan,
                ModelInstallErrorCode.LOCK_UNAVAILABLE,
                reason=(
                    f"{ModelInstallErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )
        return await self._gated_apply(request, plan, lease, intent_id)

    # -- entry: model-weight pull + registration ------------------------------

    async def pull_model(self, request: ModelPullRequest) -> ModelPullResult:
        """Pull one model's weights into the installed service, verify the
        real endpoint serves it, probe tool-call support honestly and
        register the connection in the C02 model store.

        The approval is bound to THIS pull payload (model + license +
        recipe revision + identities), never to a plan hash: one decision
        approves exactly one acquisition (C03 rule 4).
        """
        sid, aid = request.server_id, request.application_id
        plan = build_deployment_plan(request.recipe, sid, aid, request.target)
        payload_sha256 = _pull_payload_sha256(request, plan.recipe_sha256)
        operations: list[AppliedOperation] = []
        notes: list[str] = []
        host_port = _published_port(plan)

        def result(**kw: object) -> ModelPullResult:
            base: dict[str, object] = dict(
                server_id=sid,
                application_id=aid,
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=None,
                model=request.model,
                license=request.license,
                operations=tuple(operations),
                notes=tuple(notes),
                payload_sha256=payload_sha256,
                published_port=None if host_port is None else int(host_port),
                lock_released=False,
            )
            base.update(kw)
            return ModelPullResult(**base)  # type: ignore[arg-type]

        # -- identity guards (no effect, no gate consumption) ---------------
        if any(s != sid for s in self._store.servers_storing(aid)):
            return result(reason=f"{ModelInstallErrorCode.CROSS_SERVER_CONFLICT.value}")
        try:
            stored = self._store.read(sid, aid)
        except AdoptionError as error:
            if error.code is not AdoptionErrorCode.NOT_FOUND:
                return result(
                    reason=(
                        f"{ModelInstallErrorCode.STATE_DIVERGED.value}:"
                        f"{error.code.value}"
                    ),
                )
            stored = None
        try:
            adopted_record = await self._adoption.read(sid, aid)
        except AdoptionError as error:
            return result(
                reason=(
                    f"{ModelInstallErrorCode.STATE_DIVERGED.value}:{error.code.value}"
                ),
            )
        if stored is None and adopted_record is None:
            return result(
                reason=f"{ModelInstallErrorCode.SERVICE_NOT_INSTALLED.value}",
            )
        project = getattr(adopted_record, "compose_project", None) or getattr(
            stored, "compose_project", None
        )
        if project is not None and project != plan.project_name:
            return result(
                reason=(f"{ModelInstallErrorCode.EXTERNAL_RECORD_CONFLICT.value}"),
            )

        # -- policy gate (first read; re-checked before the effect) ---------
        decision = self._decide(request.context)
        if not decision.allowed:
            return result(
                reason=(
                    f"{ModelInstallErrorCode.POLICY_DENIED.value}:"
                    f"{decision.reason.value}"
                ),
            )

        # -- exclusive lease (same key as every P08 path on this app) -------
        try:
            lease = self._locks.acquire(
                str(sid),
                f"{LOCK_RESOURCE_PREFIX}{aid}",
                owner_ref=f"model-pull:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return result(
                reason=(
                    f"{ModelInstallErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )

        def release() -> None:
            self._locks.release(lease)

        def result_locked(**kw: object) -> ModelPullResult:
            # Every post-lease return releases the lease unless the path
            # explicitly keeps it (outcome_unknown leaves it to lapse).
            kw.setdefault("lock_released", True)
            return result(**kw)

        # -- verified preconditions under the lease -------------------------
        state = self._discover_state(request, plan)
        if state is None:
            release()
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                lock_released=True,
            )
        if not state.compose_present or not state.project_running:
            release()
            return result(
                reason=f"{ModelInstallErrorCode.SERVICE_NOT_RUNNING.value}",
            )
        if state.compose_present and not state.compose_hash_matches:
            release()
            return result_locked(
                reason=f"{ModelInstallErrorCode.STATE_DIVERGED.value}",
            )
        if host_port is None:
            release()
            return result_locked(
                reason=f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}",
                notes=("recipe publishes no host port; endpoint cannot be probed",),
            )
        # The verification probes need curl on the target (the image has
        # none). Without it NOTHING can be verified honestly: fail closed.
        curl_ok = self._curl_present(request)
        if curl_ok is None:
            release()
            return result_locked(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not curl_ok:
            release()
            return result_locked(
                reason=f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}",
                notes=(
                    "curl is not installed on the target; verification "
                    "refused instead of guessed",
                ),
            )

        # -- pre-effect recheck + approval consumption (C03 rule 3/4) -------
        recheck = self._decide(request.context)
        if not recheck.allowed:
            release()
            return result_locked(
                reason=(
                    f"{ModelInstallErrorCode.POLICY_DENIED.value}:"
                    f"{recheck.reason.value}"
                ),
            )
        try:
            approval = await self._approvals.consume(
                request.approval_id,
                capability_id=CAPABILITY_ID,
                server_id=sid,
                payload_hash=payload_sha256,
                policy_revision=recheck.policy_revision,
                consumed_by=request.context.principal,
            )
        except ApprovalError as error:
            release()
            return result_locked(
                reason=(
                    f"{ModelInstallErrorCode.APPROVAL_REJECTED.value}:"
                    f"{error.code.value}"
                ),
            )

        # -- the bounded pull (lane cut = cancellation) ----------------------
        service = request.recipe.name
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            if heartbeat.lapsed:
                return result_locked(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                    notes=("resource lease lapsed before the pull",),
                )
            pull_out = self._transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=sid,
                    display="pull model weights into the managed service",
                    timeout=self._weights_pull_timeout,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=_compose_argv(
                        plan, "exec", "-T", service, "ollama", "pull", request.model
                    ),
                )
            )
            operations.append(
                AppliedOperation(
                    "pull_model_weights",
                    request.model,
                    _ACTION_APPLIED,
                    pull_out.status,
                    pull_out.exit_code,
                )
            )
            if pull_out.status is not CommandStatus.SUCCEEDED:
                if pull_out.status is CommandStatus.FAILED:
                    # Deterministic failure: record the partial-blob state
                    # honestly (the daemon keeps them for resume), register
                    # nothing, claim nothing.
                    partials = self._count_partial_blobs(request, plan, service)
                    if partials is not None:
                        notes.append(f"{partials} partial blob(s) remain for resume")
                    release()
                    return result_locked(
                        status=STATUS_FAILED,
                        effect=EFFECT_EXECUTED,
                        reason=f"{ModelInstallErrorCode.APPLY_FAILED.value}:pull",
                        approval_id=approval.approval_id,
                        lock_released=True,
                    )
                # Timeout cut or transport death: the payload may live.
                # Leave the lease to lapse into a fence (LOCK-FENCE).
                return result_locked(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                    lock_released=False,
                )
            if heartbeat.lapsed:
                # The lease lapsed mid-pull: the target may be fenced and
                # another worker may be resuming. Nothing is registered.
                return result_locked(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                    notes=("resource lease lapsed during the pull",),
                    lock_released=False,
                )

            # -- real response: the daemon must list the model ---------------
            listing = self._read_model_list(
                request, host_port, "/api/tags", operations, notes
            )
            if listing is None:
                release()
                return result_locked(
                    status=STATUS_FAILED,
                    effect=EFFECT_EXECUTED,
                    reason=(f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}:pull"),
                    approval_id=approval.approval_id,
                    lock_released=True,
                )
            entry = next((e for e in listing if e.get("name") == request.model), None)
            if entry is None:
                notes.append("pulled model absent from the daemon listing")
                release()
                return result_locked(
                    status=STATUS_FAILED,
                    effect=EFFECT_EXECUTED,
                    reason=(
                        f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}:"
                        "model-missing"
                    ),
                    approval_id=approval.approval_id,
                    lock_released=True,
                )
            digest = entry.get("digest")
            size = entry.get("size")
            digest_text = digest if isinstance(digest, str) and digest else None
            size_value = (
                size if isinstance(size, int) and not isinstance(size, bool) else None
            )

            # -- OpenAI-compatible endpoint must really answer ---------------
            openai_list = self._read_model_list(
                request, host_port, "/v1/models", operations, notes
            )
            if openai_list is None:
                release()
                return result_locked(
                    status=STATUS_FAILED,
                    effect=EFFECT_EXECUTED,
                    reason=(
                        f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}:openai-list"
                    ),
                    approval_id=approval.approval_id,
                    lock_released=True,
                )

            # -- honest tool-call probe (acceptance 2) -----------------------
            tool_calls, probe_note = self._probe_tool_calls(
                request, host_port, request.model
            )
            if tool_calls is None:
                release()
                return result_locked(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                )
            if probe_note:
                notes.append(probe_note)
            observed = tool_calls

            # -- C02/C03 registration (idempotent, never overwrites) ---------
            connection_name = _connection_name(request.recipe.name, sid)
            desired_text = _connection_document(
                name=connection_name,
                base_url=request.base_url,
                tool_calls=observed,
            )
            outcome = self._register_connection(connection_name, desired_text, notes)
            if outcome is None:
                release()
                return result_locked(
                    status=STATUS_FAILED,
                    effect=EFFECT_EXECUTED,
                    reason=(f"{ModelInstallErrorCode.REGISTRATION_REFUSED.value}"),
                    approval_id=approval.approval_id,
                    lock_released=True,
                )
            revision, sha256, performed = outcome
            if performed:
                operations.append(
                    AppliedOperation(
                        "register_model_connection",
                        connection_name,
                        _ACTION_APPLIED,
                        CommandStatus.SUCCEEDED,
                        0,
                    )
                )
            else:
                operations.append(
                    AppliedOperation(
                        "register_model_connection",
                        connection_name,
                        _ACTION_VERIFIED,
                        CommandStatus.SUCCEEDED,
                        0,
                    )
                )
            release()
            return result_locked(
                status=STATUS_SUCCEEDED,
                effect=EFFECT_EXECUTED,
                approval_id=approval.approval_id,
                digest=digest_text,
                size_bytes=size_value,
                tool_calls_observed=observed,
                connection_name=connection_name,
                connection_revision=revision,
                connection_sha256=sha256,
                lock_released=True,
            )

    # -- gate phases -----------------------------------------------------------

    def _refused(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        code: ModelInstallErrorCode,
        *,
        reason: str | None = None,
        notes: tuple[str, ...] = (),
        hardware_unsatisfied: tuple[str, ...] = (),
    ) -> ModelInstallResult:
        return ModelInstallResult(
            server_id=request.server_id,
            application_id=request.application_id,
            plan_sha256=plan.plan_sha256,
            status=STATUS_REFUSED,
            effect=EFFECT_NOT_ATTEMPTED,
            reason=reason if reason is not None else code.value,
            notes=notes,
            hardware_unsatisfied=hardware_unsatisfied,
        )

    def _decide(self, context: ExecutionContext) -> PolicyDecision:
        return evaluate(
            context=context,
            capability_id=CAPABILITY_ID,
            descriptor=self._descriptor,
            grants=self._grants(),
            policy=self._policy(),
            adopted=self._adopted(context.server_id),
        )

    async def _prelock_guards(
        self, request: ModelInstallRequest, plan: DeploymentPlan
    ) -> ModelInstallResult | None:
        """Identity/projection guards that need no lease and cause no effect."""
        sid, aid = request.server_id, request.application_id
        if any(s != sid for s in self._store.servers_storing(aid)):
            return self._refused(
                request, plan, ModelInstallErrorCode.CROSS_SERVER_CONFLICT
            )
        unknown = [
            token
            for token in request.recipe.prerequisites
            if token != "docker" and not _HARDWARE_TOKEN_RE.fullmatch(token)
        ]
        if unknown:
            return self._refused(
                request,
                plan,
                ModelInstallErrorCode.UNSUPPORTED_PREREQUISITE,
                notes=(f"uninterpretable tokens: {','.join(sorted(unknown))}",),
            )
        try:
            stored = self._store.read(sid, aid)
        except AdoptionError as error:
            if error.code is not AdoptionErrorCode.NOT_FOUND:
                return self._refused(
                    request,
                    plan,
                    ModelInstallErrorCode.STATE_DIVERGED,
                    notes=(f"canonical store: {error.code.value}",),
                )
            stored = None
        documents: list[object] = []
        if stored is not None:
            documents.append(parse_manifest_text(stored.text))
        existing = await self._adoption.read(sid, aid)
        if existing is not None:
            documents.append(existing)
        for document in documents:
            project = getattr(document, "compose_project", None)
            if project is not None and project != plan.project_name:
                return self._refused(
                    request, plan, ModelInstallErrorCode.EXTERNAL_RECORD_CONFLICT
                )
        return None

    async def _gated_apply(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        lease: LockLease,
        intent_id: uuid.UUID | None,
    ) -> ModelInstallResult:
        operations: list[AppliedOperation] = []
        notes: list[str] = []
        released = False

        def release() -> None:
            nonlocal released
            self._locks.release(lease)
            released = True

        def result(**kw: object) -> ModelInstallResult:
            base: dict[str, object] = dict(
                server_id=request.server_id,
                application_id=request.application_id,
                plan_sha256=plan.plan_sha256,
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=None,
                operations=tuple(operations),
                notes=tuple(notes),
                published_ports=tuple(sorted(p.host for p in plan.ports)),
                lock_released=released,
            )
            base.update(kw)
            return ModelInstallResult(**base)  # type: ignore[arg-type]

        # ---- policy gate (first read; re-checked before the effect) ----
        decision = self._decide(request.context)
        if not decision.allowed:
            release()
            return result(
                reason=(
                    f"{ModelInstallErrorCode.POLICY_DENIED.value}:"
                    f"{decision.reason.value}"
                ),
            )

        # ---- hardware prerequisites: real measurement, fail closed -------
        declared = frozenset(request.recipe.prerequisites)
        facts, unknown_facts, measurement_notes = collect_target_facts(
            self._transport,
            request.server_id,
            need_vram=any(t.startswith("hw.vram.") for t in declared),
        )
        for fact in unknown_facts:
            notes.append(f"prerequisite fact unmeasurable: {fact}")
        notes.extend(measurement_notes)
        unsatisfied = tuple(sorted(evaluate_hardware_checks(declared, facts)))
        operations.append(
            AppliedOperation(
                OP_VERIFY_PREREQUISITES,
                ",".join(request.recipe.prerequisites),
                _ACTION_APPLIED if not unsatisfied else _ACTION_VERIFIED,
                CommandStatus.SUCCEEDED if not unsatisfied else CommandStatus.FAILED,
                0 if not unsatisfied else 1,
            )
        )
        if unsatisfied:
            release()
            return result(
                reason=f"{ModelInstallErrorCode.HARDWARE_UNSATISFIED.value}",
                hardware_unsatisfied=unsatisfied,
            )

        state = self._discover_state(request, plan)
        if state is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not state.project_running:
            ports_ok = self._preflight_ports(request, plan, notes)
            if ports_ok is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if ports_ok is False:
                release()
                return result(
                    reason=f"{ModelInstallErrorCode.PORT_CONFLICT.value}",
                )

        if state.compose_present and not state.compose_hash_matches:
            release()
            return result(
                reason=f"{ModelInstallErrorCode.STATE_DIVERGED.value}",
                notes=("compose file hash differs from the bound plan",),
            )

        # Backup gate: pre-existing volume data needs the explicit operator
        # confirmation (C09 rule 4/5 discipline applied at install time).
        if request.recipe.backup.required and request.recipe.volumes:
            data_present = self._volume_data_present(request, plan)
            if data_present is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if data_present and not request.backup_confirmed:
                release()
                return result(
                    reason=f"{ModelInstallErrorCode.BACKUP_REQUIRED.value}",
                )

        # Credentials resolve BEFORE the approval is spent: a missing
        # reference must not consume the one-use decision.
        credential_env: bytes | None = None
        if plan.credentials_path is not None:
            try:
                credential_env = self._render_credentials(request.recipe)
            except EnvFileError:
                release()
                return result(
                    reason=f"{ModelInstallErrorCode.CREDENTIAL_UNAVAILABLE.value}",
                )

        host_port = _published_port(plan)
        if host_port is None:
            release()
            return result(
                reason=f"{ModelInstallErrorCode.ENDPOINT_UNVERIFIED.value}",
                notes=("recipe publishes no host port; endpoint cannot be probed",),
            )

        # Already-verified fast path: intact plan + running ⇒ verify the
        # real endpoint and converge the record, mutate nothing.
        if state.compose_present and state.project_running:
            verified = self._wait_for_endpoint(request, host_port, operations, notes)
            if verified is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if not verified:
                return self._finish_failed(
                    request,
                    plan,
                    notes,
                    failed_op=OP_WAIT_HEALTHY,
                    started=True,
                    result_fn=result,
                    release=release,
                )
            release()
            manifest = await self._update_manifest(request, plan, intent_id, notes)
            return result(
                status=STATUS_SUCCEEDED,
                effect=EFFECT_EXECUTED,
                manifest_revision=None if manifest is None else manifest[0],
                manifest_sha256=None if manifest is None else manifest[1],
                endpoint_verified=True,
            )

        # ---- pre-effect recheck + approval consumption (C03 rule 3/4) ----
        recheck = self._decide(request.context)
        if not recheck.allowed:
            release()
            return result(
                reason=(
                    f"{ModelInstallErrorCode.POLICY_DENIED.value}:"
                    f"{recheck.reason.value}"
                ),
            )
        try:
            approval = await self._approvals.consume(
                request.approval_id,
                capability_id=CAPABILITY_ID,
                server_id=request.server_id,
                payload_hash=plan.plan_sha256,
                policy_revision=recheck.policy_revision,
                consumed_by=request.context.principal,
            )
        except ApprovalError as error:
            release()
            return result(
                reason=(
                    f"{ModelInstallErrorCode.APPROVAL_REJECTED.value}:"
                    f"{error.code.value}"
                ),
                approval_id=request.approval_id,
            )

        # ---- the apply: plan operations in plan order, under heartbeat ----
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            started = False  # a start-class effect happened (pull onward)
            for op in plan.operations:
                if heartbeat.lapsed:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                        notes=("resource lease lapsed mid-apply",),
                        approval_id=approval.approval_id,
                    )
                if op.op == OP_VERIFY_PREREQUISITES:
                    continue  # measured and evaluated during pre-flight
                if op.op == OP_WAIT_HEALTHY:
                    continue  # replaced by the real endpoint gate below
                if op.op == OP_REQUIRE_BACKUP:
                    operations.append(
                        AppliedOperation(
                            op.op,
                            op.subject,
                            _ACTION_VERIFIED,
                            CommandStatus.SUCCEEDED,
                            0,
                        )
                    )
                    continue
                receipt = self._apply_operation(
                    request, plan, op, credential_env, operations
                )
                if op.op == OP_START:
                    started = True
                if receipt is None:
                    continue  # verified-present; receipt already recorded
                if receipt.status is CommandStatus.FAILED:
                    return self._finish_failed(
                        request,
                        plan,
                        notes,
                        failed_op=op.op,
                        started=started,
                        result_fn=result,
                        approval_id=approval.approval_id,
                        release=release,
                    )
                if receipt.status is not CommandStatus.SUCCEEDED:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                        approval_id=approval.approval_id,
                    )
            if heartbeat.lapsed:
                # The lease lapsed on the final op: no gate, no canonical
                # update — the target may be fenced (LOCK-FENCE).
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    notes=("resource lease lapsed mid-apply",),
                    approval_id=approval.approval_id,
                )

            # ---- the completion gate: the REAL endpoint --------------------
            verified = self._wait_for_endpoint(request, host_port, operations, notes)
            if verified is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=ModelInstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                    lock_released=False,
                )
            if not verified:
                return self._finish_failed(
                    request,
                    plan,
                    notes,
                    failed_op=OP_WAIT_HEALTHY,
                    started=True,
                    result_fn=result,
                    approval_id=approval.approval_id,
                    release=release,
                )

        # Verified success → canonical manifest/projection (C09).
        release()
        manifest = await self._update_manifest(request, plan, intent_id, notes)
        return result(
            status=STATUS_SUCCEEDED,
            effect=EFFECT_EXECUTED,
            approval_id=approval.approval_id,
            manifest_revision=None if manifest is None else manifest[0],
            manifest_sha256=None if manifest is None else manifest[1],
            endpoint_verified=True,
        )

    # -- target-state helpers (verify-then-mutate reads) ------------------------

    def _read(
        self,
        request: ModelInstallRequest | ModelPullRequest,
        *,
        display: str,
        argv: tuple[str, ...],
        timeout: float = _FACT_TIMEOUT_SECONDS,
    ) -> CommandOutcome | None:
        """One bounded read; ``None`` ⇒ malformed request (wiring bug)."""
        probe = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=request.server_id,
            display=display,
            timeout=timeout,
            output_budget=_OUTPUT_BUDGET_BYTES,
            argv=argv,
        )
        try:
            return self._transport(probe)
        except CommandRequestError:
            return None

    def _discover_state(
        self,
        request: ModelInstallRequest | ModelPullRequest,
        plan: DeploymentPlan,
    ) -> _TargetState | None:
        """Classify the target; ``None`` ⇒ a state read is unknown (C04.5)."""
        hash_out = self._read(
            request,
            display="read managed compose file hash",
            argv=("sha256sum", "--", plan.compose_path),
        )
        if hash_out is None:
            return None
        compose_present = hash_out.status is CommandStatus.SUCCEEDED
        compose_hash_matches = compose_present and hash_out.stdout.split()[:1] == [
            plan.compose_sha256
        ]
        ps_out = self._read(
            request,
            display="read managed compose project state",
            argv=_compose_argv(plan, "ps", "-q"),
        )
        if ps_out is None:
            return None
        if ps_out.status is CommandStatus.SUCCEEDED:
            project_running = bool(ps_out.stdout.strip())
        elif ps_out.exit_code is not None:
            project_running = False
        else:
            return None
        return _TargetState(
            compose_present=compose_present,
            compose_hash_matches=bool(compose_hash_matches),
            project_running=project_running,
        )

    def _preflight_ports(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        notes: list[str],
    ) -> bool | None:
        """Fresh-install port pre-flight (True/False/unknown)."""
        outcome = self._read(
            request,
            display="preflight check published ports",
            argv=("ss", "-ltnH"),
        )
        if outcome is None:
            return None
        if outcome.exit_code == 127:
            notes.append("port preflight unavailable: ss is not installed")
            return True
        if outcome.status is not CommandStatus.SUCCEEDED:
            return None
        listening: set[int] = set()
        for line in outcome.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or ":" not in parts[-2]:
                continue
            _addr, _, port = parts[-2].rpartition(":")
            if port.isdigit():
                listening.add(int(port))
        return not ({p.host for p in plan.ports} & listening)

    def _volume_data_present(
        self, request: ModelInstallRequest, plan: DeploymentPlan
    ) -> bool | None:
        """True when a planned volume host path already holds data."""
        for volume in plan.volumes:
            outcome = self._read(
                request,
                display="preflight check persistent data location",
                argv=("ls", "-A", "--", volume.host_path),
            )
            if outcome is None:
                return None
            if outcome.status is CommandStatus.SUCCEEDED and outcome.stdout.strip():
                return True
        return False

    def _render_credentials(self, recipe: ApplicationRecipe) -> bytes:
        """Resolve every declared reference from the configured .env source.

        Returns the exact ``credentials.env`` bytes (or raises a value-free
        :class:`EnvFileError`). Values exist only here and in the write
        channel; they never reach a receipt or result field.
        """
        if self._credentials is None:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "credentials provider is not wired; recipes declaring "
                "credential references require the EnvResolver provider",
            )
        resolver = self._credentials()
        return "".join(
            f"{name}={resolver.resolve(name).value}\n" for name in recipe.credentials
        ).encode("utf-8")

    def _curl_present(self, request: ModelPullRequest) -> bool | None:
        outcome = self._read(
            request,
            display="verify target curl presence",
            argv=("curl", "--version"),
        )
        if outcome is None:
            return None
        if outcome.status is CommandStatus.SUCCEEDED:
            return True
        if outcome.exit_code == 127:
            return False
        return None

    # -- endpoint verification -------------------------------------------------

    def _wait_for_endpoint(
        self,
        request: ModelInstallRequest,
        host_port: int,
        operations: list[AppliedOperation],
        notes: list[str],
    ) -> bool | None:
        """Real completion gate: ``GET /v1/models`` answers 200 with a
        parseable model list within the start budget.

        The model list may legitimately be EMPTY (a freshly installed
        service serves no weights yet); the response must be REAL.
        Returns ``None`` when a verification read is itself unknown.
        """
        deadline = monotonic() + _ENDPOINT_START_BUDGET_SECONDS
        while True:
            listing = self._read(
                request,
                display="verify model endpoint responds",
                argv=(
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    f"http://127.0.0.1:{host_port}/v1/models",
                ),
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
            if listing is None:
                return None
            if listing.status is CommandStatus.SUCCEEDED:
                parsed = _parse_model_list(listing.stdout)
                if parsed is not None:
                    operations.append(
                        AppliedOperation(
                            OP_WAIT_HEALTHY,
                            f"http://127.0.0.1:{host_port}/v1/models",
                            _ACTION_VERIFIED,
                            CommandStatus.SUCCEEDED,
                            0,
                        )
                    )
                    return True
                notes.append("endpoint answered but the body was not a model list")
                return False
            if listing.exit_code == 127:
                notes.append("curl is not installed; endpoint cannot be verified")
                return False
            if monotonic() >= deadline:
                notes.append("endpoint did not answer within the start budget")
                return False
            sleep(_ENDPOINT_POLL_INTERVAL_SECONDS)

    def _read_model_list(
        self,
        request: ModelPullRequest,
        host_port: int,
        path: str,
        operations: list[AppliedOperation],
        notes: list[str],
    ) -> list[dict[str, object]] | None:
        """One real model-list read; ``None`` ⇒ unverifiable (fail closed)."""
        outcome = self._read(
            request,
            display="read served model list",
            argv=(
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                f"http://127.0.0.1:{host_port}{path}",
            ),
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )
        receipt = AppliedOperation(
            "read_model_list",
            path,
            _ACTION_APPLIED,
            outcome.status if outcome is not None else CommandStatus.FAILED,
            outcome.exit_code if outcome is not None else None,
        )
        operations.append(receipt)
        if outcome is None or outcome.status is not CommandStatus.SUCCEEDED:
            return None
        parsed = _parse_model_list(outcome.stdout)
        if parsed is None:
            notes.append(f"{path} answered but the body was not a model list")
        return parsed

    def _probe_tool_calls(
        self,
        request: ModelPullRequest,
        host_port: int,
        model: str,
    ) -> tuple[bool | None, str | None]:
        """One bounded tool-call probe; verdict is honest by construction.

        ``(True, None)`` only when a REAL tool call came back. ``(False,
        note)`` for every other observable answer (refusal, plain answer,
        HTTP error). ``(None, None)`` when the read itself is unknown.
        """
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Call the ping tool now.",
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "ping",
                            "description": "Return pong.",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
            }
        )
        outcome = self._read(
            request,
            display="probe tool-call support",
            argv=(
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "-H",
                "Content-Type: application/json",
                "-d",
                body,
                f"http://127.0.0.1:{host_port}/v1/chat/completions",
            ),
            timeout=_TOOL_PROBE_TIMEOUT_SECONDS,
        )
        if outcome is None:
            return None, None
        if outcome.status is not CommandStatus.SUCCEEDED:
            return False, "tool-call probe: endpoint refused the tools request"
        observed = _tool_call_observed(outcome.stdout)
        if observed is None:
            return False, "tool-call probe: body was not a chat completion"
        if not observed:
            return False, "tool-call probe: model answered without a tool call"
        return True, None

    def _count_partial_blobs(
        self,
        request: ModelPullRequest,
        plan: DeploymentPlan,
        service: str,
    ) -> int | None:
        """Count the daemon's resumable partial blobs (never deleted here:
        deletion would turn a resumable download into a full re-download;
        the honest state is recorded instead)."""
        outcome = self._read(
            request,
            display="count resumable partial blobs",
            argv=_compose_argv(
                plan,
                "exec",
                "-T",
                service,
                "sh",
                "-c",
                "ls /root/.ollama/models/blobs/*-partial 2>/dev/null | wc -l",
            ),
        )
        if outcome is None or outcome.status is not CommandStatus.SUCCEEDED:
            return None
        try:
            return int(outcome.stdout.strip())
        except ValueError:
            return None

    # -- plan operation handlers -------------------------------------------------

    def _apply_operation(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        credential_env: bytes | None,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        """Apply one plan operation verify-first; never invents a status."""
        if op.op == OP_CREATE_DIRECTORIES:
            return self._op_mkdir(request, plan, op, operations)
        if op.op == OP_WRITE_COMPOSE:
            return self._op_write_compose(request, plan, op, operations)
        if op.op == OP_WRITE_CREDENTIALS:
            return self._op_write_credentials(
                request, plan, op, credential_env, operations
            )
        if op.op == OP_PULL_IMAGE:
            return self._op_command(
                request,
                op,
                argv=_compose_argv(plan, "pull", "--quiet"),
                timeout=_DEFAULT_OP_TIMEOUT_SECONDS * 10,
                display="pull pinned application image",
                operations=operations,
            )
        if op.op == OP_START:
            return self._op_command(
                request,
                op,
                argv=_compose_argv(plan, "up", "-d"),
                timeout=_UP_TIMEOUT_SECONDS,
                display="start managed compose project",
                operations=operations,
            )
        raise ModelInstallError(
            ModelInstallErrorCode.INVALID_PLAN,
            "plan operation outside the frozen vocabulary reached the apply loop",
        )

    def _op_mkdir(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        outcome = self._read(
            request,
            display="create managed application directories",
            argv=("mkdir", "-p", "--", *plan.directories),
            timeout=_DEFAULT_OP_TIMEOUT_SECONDS,
        )
        if outcome is None:
            return AppliedOperation(
                op.op, op.subject, _ACTION_APPLIED, CommandStatus.FAILED, None
            )
        receipt = AppliedOperation(
            op.op, op.subject, _ACTION_APPLIED, outcome.status, outcome.exit_code
        )
        operations.append(receipt)
        return receipt

    def _file_hash(self, request: ModelInstallRequest, path: str) -> str | None:
        """Observed target file hash, ``None`` when absent/unreadable."""
        outcome = self._read(
            request,
            display="read managed application file hash",
            argv=("sha256sum", "--", path),
        )
        if outcome is None or outcome.status is not CommandStatus.SUCCEEDED:
            return None
        parts = outcome.stdout.split()
        return parts[0] if parts else None

    def _op_write_compose(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        if self._file_hash(request, plan.compose_path) == plan.compose_sha256:
            operations.append(
                AppliedOperation(
                    op.op,
                    op.subject,
                    _ACTION_VERIFIED,
                    CommandStatus.SUCCEEDED,
                    0,
                )
            )
            return None
        try:
            outcome = self._transport(
                _write_file_command(
                    request.server_id,
                    plan.compose_path,
                    plan.compose_text.encode("utf-8"),
                )
            )
        except CommandRequestError:
            outcome = None
        receipt = AppliedOperation(
            op.op,
            op.subject,
            _ACTION_APPLIED,
            outcome.status if outcome is not None else CommandStatus.FAILED,
            outcome.exit_code if outcome is not None else None,
        )
        if (
            outcome is not None
            and outcome.status is CommandStatus.SUCCEEDED
            and self._file_hash(request, plan.compose_path) != plan.compose_sha256
        ):
            # Verify-then-trust: the write claims success but the bytes on
            # the target are not the plan's bytes — a deterministic failure.
            from dataclasses import replace

            receipt = replace(receipt, status=CommandStatus.FAILED, exit_code=None)
        operations.append(receipt)
        return receipt

    def _op_write_credentials(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        credential_env: bytes | None,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        if self._file_hash(request, plan.credentials_path or "") is not None:
            operations.append(
                AppliedOperation(
                    op.op,
                    op.subject,
                    _ACTION_VERIFIED,
                    CommandStatus.SUCCEEDED,
                    0,
                )
            )
            return None
        if credential_env is None:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_REQUEST,
                "credential bytes missing at write time",
            )
        assert plan.credentials_path is not None
        try:
            outcome = self._transport(
                _write_file_command(
                    request.server_id, plan.credentials_path, credential_env
                )
            )
        except CommandRequestError:
            outcome = None
        receipt = AppliedOperation(
            op.op,
            op.subject,
            _ACTION_APPLIED,
            outcome.status if outcome is not None else CommandStatus.FAILED,
            outcome.exit_code if outcome is not None else None,
        )
        operations.append(receipt)
        return receipt

    def _op_command(
        self,
        request: ModelInstallRequest,
        op: PlanOperation,
        *,
        argv: tuple[str, ...],
        timeout: float,
        display: str,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        try:
            outcome = self._transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=request.server_id,
                    display=display,
                    timeout=timeout,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=argv,
                )
            )
        except CommandRequestError:
            outcome = None
        receipt = AppliedOperation(
            op.op,
            op.subject,
            _ACTION_APPLIED,
            outcome.status if outcome is not None else CommandStatus.FAILED,
            outcome.exit_code if outcome is not None else None,
        )
        operations.append(receipt)
        return receipt

    # -- failure / canonical update ------------------------------------------------

    def _finish_failed(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        notes: list[str],
        *,
        failed_op: str,
        started: bool,
        result_fn: Callable[..., ModelInstallResult],
        approval_id: uuid.UUID | None = None,
        release: Callable[[], None] = lambda: None,
    ) -> ModelInstallResult:
        """Deterministic failure: verified rollback when an effect started."""
        rolled_back = False
        rollback_verified = False
        if started:
            notes.append(f"rollback after failed operation: {failed_op}")
            try:
                self._transport(
                    CommandRequest(
                        op_id=uuid.uuid4(),
                        server_id=request.server_id,
                        display="roll back managed compose project",
                        timeout=_DOWN_TIMEOUT_SECONDS,
                        output_budget=_OUTPUT_BUDGET_BYTES,
                        argv=_compose_argv(plan, "down", "--remove-orphans"),
                    )
                )
            except CommandRequestError:
                pass
            rolled_back = True
            verify = self._read(
                request,
                display="verify rollback removed project containers",
                argv=_compose_argv(plan, "ps", "-q"),
            )
            rollback_verified = (
                verify is not None
                and not verify.stdout.strip()
                and (
                    verify.status is CommandStatus.SUCCEEDED
                    or verify.exit_code is not None
                )
            )
            if not rollback_verified:
                notes.append("rollback could not be verified")
        release()
        return result_fn(
            status=STATUS_ROLLED_BACK if rolled_back else STATUS_FAILED,
            effect=EFFECT_EXECUTED if started else EFFECT_NOT_ATTEMPTED,
            reason=f"{ModelInstallErrorCode.APPLY_FAILED.value}",
            approval_id=approval_id,
            rollback_attempted=rolled_back,
            rollback_verified=rollback_verified,
            endpoint_verified=False,
        )

    async def _update_manifest(
        self,
        request: ModelInstallRequest,
        plan: DeploymentPlan,
        intent_id: uuid.UUID | None,
        notes: list[str],
    ) -> tuple[int, str] | None:
        """Canonical manifest + projection via the adoption pipeline.

        Deterministic intent id ⇒ an interrupted canonical update converges
        on retry instead of duplicating (C02 rule 5). A refusal here is a
        recorded note — the verified deployment is not destroyed for a
        bookkeeping failure.
        """
        sid, aid = request.server_id, request.application_id
        intent = intent_id or install_intent_id(sid, aid, plan.plan_sha256)
        compose_dir = plan.compose_path.rsplit("/", 1)[0]
        for _attempt in (0, 1):
            try:
                existing = await self._adoption.read(sid, aid)
            except AdoptionError:
                existing = None
            document = AdoptionRequest(
                server_id=sid,
                application_id=aid,
                name=request.recipe.name,
                origin=ApplicationOrigin.COMPOSE,
                compose_project=plan.project_name,
                working_dir=compose_dir,
                expected_revision=0 if existing is None else existing.revision,
            )
            try:
                outcome = await self._adoption.adopt(
                    self._store, document, intent_id=intent
                )
            except AdoptionError as error:
                if _attempt == 0 and error.code in _RECOVERABLE_ADOPTION_CODES:
                    await self._adoption.recover(self._store, server_id=sid)
                    continue
                notes.append(f"manifest update refused: {error.code.value}")
                return None
            return outcome.revision, outcome.sha256
        return None

    # -- C02/C03 model-store registration ---------------------------------------

    def _register_connection(
        self,
        name: str,
        desired_text: str,
        notes: list[str],
    ) -> tuple[int, str, bool] | None:
        """Write the connection through the store's validated pipeline.

        ``(revision, sha256, performed)``; ``None`` ⇒ refused (an existing
        record diverges — admin configuration is never overwritten here).
        """
        try:
            existing = self._model_store.read_connection(name)
        except ModelStoreError as error:
            if error.code is not ModelStoreErrorCode.NOT_FOUND:
                notes.append(f"model store read refused: {error.code.value}")
                return None
            try:
                stored = self._model_store.write_connection(
                    name, desired_text, expected_revision=0
                )
            except ModelStoreError as write_error:
                notes.append(f"model store write refused: {write_error.code.value}")
                return None
            return stored.revision, stored.sha256, True
        if existing.text == desired_text:
            return existing.revision, existing.sha256, False
        notes.append(
            "connection record exists with different content; registration "
            "refused (admin configuration is never overwritten)"
        )
        return None


# ---------------------------------------------------------------------------
# Pull payload binding + registration document (pure functions)
# ---------------------------------------------------------------------------


def _pull_payload_sha256(request: ModelPullRequest, recipe_sha256: str) -> str:
    """One-use approval payload for exactly one acquisition (C03 rule 4)."""
    import hashlib

    payload = json.dumps(
        {
            "capability": CAPABILITY_ID,
            "server_id": str(request.server_id),
            "application_id": str(request.application_id),
            "model": request.model,
            "license": request.license,
            "recipe_sha256": recipe_sha256,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connection_name(recipe_name: str, server_id: uuid.UUID) -> str:
    """Deterministic per-server connection name (store-name-grammar safe)."""
    name = f"{recipe_name}-{server_id.hex[:8]}".lower()
    if not _CONNECTION_NAME_RE.fullmatch(name):
        raise ModelInstallError(
            ModelInstallErrorCode.INVALID_REQUEST,
            "derived connection name does not satisfy the store name grammar",
        )
    return name


def _connection_document(*, name: str, base_url: str, tool_calls: bool) -> str:
    """The C02/C03-compliant connection document (canonical JSON).

    Value-free by construction: no credential material exists for a local
    endpoint (``credential_ref`` stays absent — credentials would resolve
    by reference only); ``capabilities.reasoning`` is never claimed.
    """
    document = {
        "schema_version": 1,
        "name": name,
        "base_url": base_url,
        "timeout_seconds": 120.0,
        "capabilities": {
            "tool_calls": bool(tool_calls),
            "reasoning": False,
        },
    }
    # Self-check: the exact document must validate through the frozen
    # connection schema before it can ever reach the store.
    parse_connection(document)
    return json.dumps(document, sort_keys=True) + "\n"


def _validate_plan_vocabulary(plan: DeploymentPlan) -> None:
    known = {
        OP_VERIFY_PREREQUISITES,
        OP_REQUIRE_BACKUP,
        OP_CREATE_DIRECTORIES,
        OP_WRITE_COMPOSE,
        OP_WRITE_CREDENTIALS,
        OP_PULL_IMAGE,
        OP_START,
        OP_WAIT_HEALTHY,
    }
    for op in plan.operations:
        if op.op not in known:
            raise ModelInstallError(
                ModelInstallErrorCode.INVALID_PLAN,
                "plan carries an operation outside the frozen vocabulary",
            )
