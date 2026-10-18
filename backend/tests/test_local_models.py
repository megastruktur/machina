"""Focused tests for local model service install + weight pull (card
11-model-install).

Acceptance coverage (card mai-11-model-install):

- **AC1 (download/install through granted lifecycle tools; cancellation /
  partial-file; checksum/license metadata)** — the model-service install
  rebuilds the policy-bound plan and applies ONLY its frozen vocabulary
  after the full P03 gate and under the SAME exclusive
  ``application:<id>`` lease every P08 path uses; the hardware
  prerequisite tokens are verified against REAL target measurements with
  the producer-frozen ``evaluate_hardware_checks`` semantics (fail-closed:
  unknown token, unmeasurable fact, unsatisfied bound ⇒ refused before any
  effect and before the one-use approval is spent). The weight pull is a
  separate explicit acquisition: one approval bound to the PULL payload
  hash (model + license + recipe revision), the bounded lane's timeout cut
  IS the cancellation (``outcome_unknown`` keeps the lease → fence), a
  deterministic failure records the daemon's resumable partial blobs and
  registers nothing, and the serving daemon's own listing supplies the
  checksum/size metadata while the admin's license metadata is carried
  value-free into the receipt.
- **AC2 (register endpoint only after a real response; tool-call support
  reported honestly)** — registration happens only after the daemon lists
  the pulled model (``GET /api/tags``) AND the OpenAI-compatible endpoint
  really answers (``GET /v1/models``); the registered connection document
  is written through the REAL C02 store pipeline (validated, revision-
  checked, atomic) with ``capabilities.tool_calls`` True ONLY when a real
  tool call came back — plain answers, HTTP refusals and non-chat bodies
  register False, and ``reasoning`` is never claimed. An existing
  connection record is converged only when byte-identical; a diverging
  record is refused, never overwritten.

Test posture (mirrors test_install.py): the target boundary is a faithful
in-memory simulator of the EXACT command surface local_models.py drives
(the same facts reads, the base64 file-write channel, compose project
state, curl probes); the approval/lock/adoption seams run as semantic
stubs subclassing the real types (wiring checks stay enforced), while
model-settings registration runs against the REAL
:class:`~mai.agents.model_store.ModelConfigStore` on a temporary root —
C02 store semantics are never stubbed. Time is virtualized (fake
monotonic/sleep) so endpoint budgets expire without wall-clock waits.
The ``real_target``-marked test additionally drives
:func:`mai.applications.local_models.collect_target_facts` over the REAL
local Docker daemon of the approved coordinator-class host (x86_64,
read-only measurements only) and evaluates the approved recipe's
prerequisites against the measured facts — the real Docker data root is
measured, never assumed.

Negative controls (``test_nt_*`` selection) exercise the real safety
boundaries: policy denial, altered approval payload (install plan hash /
pull payload), unsatisfied or unmeasurable hardware (fail-closed BEFORE
any effect), unknown prerequisite tokens, lock contention, lease lapse
(outcome_unknown keeps the fence), unreachable/garbage endpoint gates
(verified rollback), state divergence, partial-file honesty, missing
verification tooling, and registration that never overwrites admin
configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mai.applications.local_models as local_models
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import parse_connection
from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    AdoptionResult,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.install import install_intent_id
from mai.applications.local_models import (
    CAPABILITY_ID,
    LOCK_RESOURCE_PREFIX,
    ModelInstallError,
    ModelInstallErrorCode,
    ModelInstallRequest,
    LocalModelService,
    ModelPullRequest,
    collect_target_facts,
    evaluate_hardware_checks,
)
from mai.applications.recipes import (
    ApplicationRecipe,
    TargetEnvironment,
    build_deployment_plan,
    parse_recipe_text,
)
from mai.execution.commands import (
    CommandOutcome,
    CommandRequest,
    CommandStatus,
)
from mai.jobs.resource_locks import (
    LockErrorCode,
    LockLease,
    ResourceLockError,
    ResourceLockService,
)
from mai.plugins.registry import parse_descriptor
from mai.policy.approvals import (
    Approval,
    ApprovalError,
    ApprovalErrorCode,
    ApprovalStore,
)
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
CATALOGUE_DIR = REPO_ROOT / "catalogue"

INFERENCE_FILE = "local-inference.yaml"

TARGET = TargetEnvironment(os="linux", arch="x86_64")

SERVER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_SERVER_ID = uuid.UUID("12121212-1212-4212-8212-121212121212")
APP_ID = uuid.UUID("0bbbbbbb-0bbb-4bbb-8bbb-bbbbbbbbbbb3")
RUN_ID = uuid.UUID("0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a")

POLICY_REVISION = 1
PRINCIPAL = "admin-test"

#: Shared default approval id so tests that do not exercise approval
#: binding explicitly still wire service and request consistently.
_DEFAULT_APPROVAL_ID = uuid.UUID("0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e")

#: The acquisition used across pull tests (license metadata is REQUIRED
#: admin input; the recipe pre-authorizes no model and no license).
PULL_MODEL = "qwen2.5:7b-instruct-q4_K_M"
PULL_LICENSE = "apache-2.0 (operator acknowledgement: qwen2.5 research license)"
PULL_BASE_URL = "http://192.168.8.40:18114/v1"

#: Deterministic daemon-reported metadata the simulator serves after a
#: successful pull (checksum metadata, acceptance 1).
PULL_DIGEST = "sha256:" + ("1234abcd" * 8)
PULL_SIZE = 4683087569

#: Connection name the service derives (recipe name + server hex prefix).
EXPECTED_CONNECTION_NAME = f"local-inference-{SERVER_ID.hex[:8]}"


# ---------------------------------------------------------------------------
# Catalogue fixture (the frozen 11-model-recipe revision stream)
# ---------------------------------------------------------------------------


def _document_texts(filename: str) -> list[str]:
    stream = (CATALOGUE_DIR / filename).read_text(encoding="utf-8")
    return [yaml.serialize(node) for node in yaml.compose_all(stream)]


def _recipe(filename: str = INFERENCE_FILE) -> ApplicationRecipe:
    return parse_recipe_text(_document_texts(filename)[0])


def _plan_for(recipe: ApplicationRecipe):
    return build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET)


# ---------------------------------------------------------------------------
# Gate inputs (policy/grants/context/descriptor)
# ---------------------------------------------------------------------------


def _descriptor(**overrides: object):
    document: dict[str, object] = {
        "identifier": CAPABILITY_ID,
        "version": "1.0.0",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "execution_environments": ["linux_ssh"],
        "effect_class": "privileged",
        "resource_scope": ["application"],
        "required_grants": ["docker.compose"],
    }
    document.update(overrides)
    return parse_descriptor(document)


def _context(server_id: uuid.UUID = SERVER_ID) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_ID,
        parent_scope=None,
        profile_revision="profile-r1",
        prompt_revision="prompt-r1",
        credential_references=(),
        allowed_capabilities=(CAPABILITY_ID,),
        mode=PolicyMode.ASK,
    )


def _grants(server_id: uuid.UUID = SERVER_ID, *, granted: bool = True):
    return {CAPABILITY_ID: frozenset({server_id} if granted else set())}


def _policy(
    revision: int = POLICY_REVISION,
    denied: frozenset[str] = frozenset(),
) -> Policy:
    return Policy(revision=revision, denied_capabilities=denied)


def _install_request(
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID = _DEFAULT_APPROVAL_ID,
    server_id: uuid.UUID = SERVER_ID,
    application_id: uuid.UUID = APP_ID,
) -> ModelInstallRequest:
    return ModelInstallRequest(
        server_id=server_id,
        application_id=application_id,
        recipe=recipe,
        target=TARGET,
        context=_context(server_id),
        approval_id=approval_id,
    )


def _pull_request(
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID = _DEFAULT_APPROVAL_ID,
    server_id: uuid.UUID = SERVER_ID,
    model: str = PULL_MODEL,
    license_text: str | None = PULL_LICENSE,
    base_url: str = PULL_BASE_URL,
) -> ModelPullRequest:
    return ModelPullRequest(
        server_id=server_id,
        application_id=APP_ID if server_id is SERVER_ID else uuid.uuid4(),
        recipe=recipe,
        target=TARGET,
        context=_context(server_id),
        approval_id=approval_id,
        model=model,
        license=license_text,  # type: ignore[arg-type]
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# Semantic stubs (subclass the real types: wiring checks stay enforced)
# ---------------------------------------------------------------------------


class _StubLocks(ResourceLockService):
    """In-memory lease semantics: acquire/heartbeat/release bookkeeping."""

    def __init__(
        self,
        *,
        lapse_heartbeats: bool = False,
        refuse_acquire: bool = False,
    ) -> None:
        self.acquired: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.heartbeat_calls = 0
        self._lapse = lapse_heartbeats
        self._refuse = refuse_acquire

    def acquire(self, server_id, resource, *, owner_ref="", ttl=None):  # type: ignore[override]
        if self._refuse:
            raise ResourceLockError(LockErrorCode.HELD, "key held by another owner")
        self.acquired.append((server_id, resource, owner_ref))
        now = datetime.now(UTC)
        return LockLease(
            server_id=server_id,
            resource=resource,
            owner_token=str(uuid.uuid4()),
            boot_id="stub-boot",
            owner_ref=owner_ref,
            lease_ttl=ttl if ttl is not None else 3600.0,
            expires_at=now + timedelta(seconds=ttl if ttl is not None else 3600.0),
            acquired_at=now,
        )

    def heartbeat(self, lease: LockLease) -> bool:  # type: ignore[override]
        self.heartbeat_calls += 1
        return not self._lapse

    def release(self, lease: LockLease) -> None:  # type: ignore[override]
        self.released.append(lease.owner_token)


class _StubApprovals(ApprovalStore):
    """In-memory one-use decisions with the real consume() semantics."""

    def __init__(
        self,
        approval_id: uuid.UUID,
        *,
        payload_hash: str,
        policy_revision: int = POLICY_REVISION,
        server_id: uuid.UUID = SERVER_ID,
    ) -> None:
        self._records: dict[uuid.UUID, dict[str, object]] = {
            approval_id: {
                "capability_id": CAPABILITY_ID,
                "server_id": server_id,
                "payload_hash": payload_hash,
                "policy_revision": policy_revision,
                "consumed": False,
            }
        }
        self.consume_calls = 0
        self.approval_id = approval_id

    async def consume(  # type: ignore[override]
        self,
        approval_id: uuid.UUID,
        *,
        capability_id: str,
        server_id: uuid.UUID,
        payload_hash: str,
        policy_revision: int,
        consumed_by: str,
    ) -> Approval:
        self.consume_calls += 1
        record = self._records.get(approval_id)
        if record is None:
            raise ApprovalError(ApprovalErrorCode.NOT_FOUND, "no approval record")
        if record["consumed"]:
            raise ApprovalError(
                ApprovalErrorCode.ALREADY_CONSUMED,
                "decision already consumed (one-use, C03 rule 4)",
            )
        if record["capability_id"] != capability_id:
            raise ApprovalError(
                ApprovalErrorCode.CAPABILITY_MISMATCH,
                "presented capability_id differs from the approval binding",
            )
        if record["server_id"] != server_id:
            raise ApprovalError(
                ApprovalErrorCode.TARGET_MISMATCH,
                "presented server_id differs from the approval binding",
            )
        if record["payload_hash"] != payload_hash:
            raise ApprovalError(
                ApprovalErrorCode.PAYLOAD_MISMATCH,
                "presented payload_hash differs from the approval binding",
            )
        if record["policy_revision"] != policy_revision:
            raise ApprovalError(
                ApprovalErrorCode.POLICY_REVISION_MISMATCH,
                "presented policy_revision differs from the approval binding",
            )
        record["consumed"] = True
        now = datetime.now(UTC)
        return Approval(
            approval_id=approval_id,
            capability_id=capability_id,
            server_id=server_id,
            payload_hash=payload_hash,
            policy_revision=policy_revision,
            created_by=consumed_by,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            consumed_at=now,
            consumed_by=consumed_by,
        )

    def unconsumed(self) -> bool:
        return not any(r["consumed"] for r in self._records.values())


class _StubAdoption(ApplicationAdoption):
    """In-memory canonical-update pipeline (read/adopt/recover)."""

    def __init__(self, existing_compose_project: str | None = None) -> None:
        self.existing_compose_project = existing_compose_project
        self.adopt_calls: list[tuple[AdoptionRequest, uuid.UUID | None]] = []
        self.recover_calls = 0

    def _existing(self):
        if self.existing_compose_project is None:
            return None

        class _Row:
            pass

        row = _Row()
        row.revision = 3
        row.compose_project = self.existing_compose_project
        return row

    async def read(self, server_id, application_id):  # type: ignore[override]
        return self._existing()

    async def adopt(self, store, request, *, intent_id=None):  # type: ignore[override]
        self.adopt_calls.append((request, intent_id))
        return AdoptionResult(
            intent_id or uuid.uuid4(),
            request.server_id,
            request.application_id,
            "upsert",
            "applied",
            4 if self.existing_compose_project else 1,
            "ab" * 32,
        )

    async def recover(self, store, *, server_id=None, limit=None):  # type: ignore[override]
        self.recover_calls += 1
        return []


class _StubStore(ApplicationManifestStore):
    """File-store stub: no manifest stored, optional foreign-server holder."""

    def __init__(self, foreign_servers: tuple[uuid.UUID, ...] = ()) -> None:
        self._foreign = foreign_servers

    def servers_storing(self, application_id: uuid.UUID):  # type: ignore[override]
        return self._foreign

    def read(self, server_id: uuid.UUID, application_id: uuid.UUID):  # type: ignore[override]
        raise AdoptionError(AdoptionErrorCode.NOT_FOUND, "no manifest stored")


# ---------------------------------------------------------------------------
# The target simulator: the exact command surface local_models.py drives
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"umask 077 && mkdir -p (?P<dir>\S+) && "
    r"printf %s '(?P<b64>[A-Za-z0-9+/=]+)' \| base64 -d > "
    r"(?P<path>\S+)\.tmp && mv \S+\.tmp (?P=path) && sha256sum -- (?P=path)"
)


class _FakeTarget:
    """In-memory target executing exactly the commands local_models.py emits.

    Faithful where it matters for the acceptance claims: prerequisite facts
    come from configurable host attributes (arch, cpu flags, RAM, Docker
    data-root free bytes), file writes go through the real base64 shell
    channel, the Compose project state is a first-class mutable, the model
    service answers its endpoints only while the project runs, and pulled
    models appear in the daemon listing with deterministic checksum/size
    metadata. Scripted failures can turn any op into ``failed`` (rc 1) or
    ``outcome_unknown`` (timeout cut).
    """

    def __init__(self) -> None:
        # -- host facts (the approved coordinator-class target) ----------
        self.arch = "x86_64"
        self.cpu_flags = (
            "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov "
            "pat pse36 clflush mmx fxsr sse sse2 ht syscall nx rdtscp lm "
            "constant_tsc arch_perfmon avx2 bmi1 bmi2"
        )
        self.ram_kib = 122 * 1024 * 1024
        self.disk_free_bytes = 490 * 1024**3
        self.docker_ok = True
        self.compose_ok = True
        self.df_ok = True
        #: Paths df refuses (e.g. a root-only Docker data root).
        self.df_fail_paths: set[str] = set()
        # -- target state -------------------------------------------------
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.listening: set[int] = set()
        self.volume_data: dict[str, tuple[str, ...]] = {}
        self.project: dict[str, bool] | None = None
        self.up_calls = 0
        self.down_calls = 0
        self.pull_image_calls = 0
        # -- model service state ------------------------------------------
        self.pulled_models: dict[str, tuple[str, int]] = {}
        self.partial_blobs = 0
        self.curl_present = True
        #: Body served for GET /v1/models once the project runs.
        self.models_body: str | None = None
        #: chat-completion probe behavior: tool_call | plain | http_error |
        #: garbage
        self.tool_call_mode = "tool_call"
        # -- scripted control ----------------------------------------------
        self.fail: dict[str, str] = {}
        self.block_on: str | None = None
        self._gate = False
        self.calls: list[CommandRequest] = []

    def unblock(self) -> None:
        self._gate = True

    def wrote_compose(self) -> bool:
        return any(path.endswith("compose.yaml") for path in self.files)

    def __call__(self, request: CommandRequest):
        self.calls.append(request)
        scripted = next((v for k, v in self.fail.items() if k in request.display), None)
        if scripted == "unknown":
            return _outcome(
                request,
                CommandStatus.OUTCOME_UNKNOWN,
                None,
                b"",
                b"",
                timed_out=True,
            )
        if scripted == "failed":
            return _outcome(
                request, CommandStatus.FAILED, 1, b"", b"scripted failure\n"
            )
        if request.shell is not None:
            return self._write(request)
        argv = request.argv or ()
        head = argv[0] if argv else ""
        if head == "uname":
            return _text(request, f"{self.arch}\n")
        if argv[:2] == ("grep", "-m1"):
            if not self.cpu_flags:
                return _outcome(request, CommandStatus.FAILED, 1, b"", b"")
            return _text(request, f"flags\t\t: {self.cpu_flags}\n")
        if argv[:1] == ("awk",):
            return _text(request, f"{self.ram_kib}\n")
        if argv[:2] == ("docker", "version"):
            if not self.docker_ok:
                return _outcome(
                    request,
                    CommandStatus.FAILED,
                    1,
                    b"",
                    b"Cannot connect to the Docker daemon\n",
                )
            return _text(request, "24.0.7\n")
        if argv[:2] == ("docker", "compose") and "version" in argv:
            if not self.compose_ok:
                return _outcome(
                    request,
                    CommandStatus.FAILED,
                    1,
                    b"",
                    b"docker compose is not a docker CLI plugin\n",
                )
            return _text(request, "Docker Compose version v2.24.0\n")
        if argv[:2] == ("docker", "info"):
            if not self.docker_ok:
                return _outcome(request, CommandStatus.FAILED, 1, b"", b"daemon down\n")
            return _text(request, "/var/lib/docker\n")
        if argv[:1] == ("df",):
            if not self.df_ok or argv[-1] in self.df_fail_paths:
                return _outcome(request, CommandStatus.FAILED, 1, b"", b"df failed\n")
            return _text(request, f"Avail\n{self.disk_free_bytes}\n")
        if argv[:1] == ("ss",):
            lines = [
                f"LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:*\n"
                for port in sorted(self.listening)
            ]
            return _text(request, "".join(lines))
        if argv[:1] == ("sha256sum",):
            path = argv[-1]
            data = self.files.get(path)
            if data is None:
                return _outcome(
                    request,
                    CommandStatus.FAILED,
                    1,
                    b"",
                    b"sha256sum: cannot open\n",
                )
            return _text(request, f"{hashlib.sha256(data).hexdigest()}  {path}\n")
        if argv[:1] == ("ls",):
            path = argv[-1]
            entries = self.volume_data.get(path)
            if entries is None:
                return _outcome(
                    request, CommandStatus.FAILED, 2, b"", b"ls: cannot access\n"
                )
            return _text(request, "".join(f"{e}\n" for e in entries))
        if argv[:1] == ("mkdir",):
            self.dirs.update(argv[3:])
            return _text(request, "")
        if head == "curl":
            return self._curl(request, argv)
        if argv[:2] == ("docker", "compose"):
            return self._compose(request, argv)
        raise AssertionError(f"unexpected transport command: {argv!r}")

    # -- channel implementations ----------------------------------------------

    def _write(self, request: CommandRequest):
        match = _WRITE_RE.fullmatch(request.shell or "")
        assert match is not None, f"unexpected write shell: {request.shell!r}"
        data = base64.b64decode(match.group("b64"))
        path = match.group("path")
        self.dirs.add(match.group("dir"))
        self.files[path] = data
        digest = hashlib.sha256(data).hexdigest()
        return _text(request, f"{digest}  {path}\n")

    def _curl(self, request: CommandRequest, argv: tuple[str, ...]):
        if not self.curl_present:
            return _outcome(
                request, CommandStatus.FAILED, 127, b"", b"sh: curl: not found\n"
            )
        if argv[1] == "--version":
            return _text(request, "curl 8.11.0\n")
        url = argv[-1]
        if not (self.project is not None and self.project["running"]):
            return _outcome(
                request, CommandStatus.FAILED, 7, b"", b"connection refused\n"
            )
        if url.endswith("/v1/chat/completions"):
            return self._chat(request)
        if url.endswith("/api/tags"):
            models = [
                {"name": name, "model": name, "digest": digest, "size": size}
                for name, (digest, size) in sorted(self.pulled_models.items())
            ]
            return _text(request, json.dumps({"models": models}))
        if url.endswith("/v1/models"):
            if self.models_body is not None:
                return _text(request, self.models_body)
            data = [
                {"id": name, "object": "model"} for name in sorted(self.pulled_models)
            ]
            return _text(request, json.dumps({"object": "list", "data": data}))
        raise AssertionError(f"unexpected probe url: {url!r}")

    def _chat(self, request: CommandRequest):
        if self.tool_call_mode == "http_error":
            return _outcome(
                request,
                CommandStatus.FAILED,
                22,
                b"",
                b'{"error":{"message":"tools are not supported"}}\n',
            )
        if self.tool_call_mode == "garbage":
            return _text(request, "<html>not json</html>")
        message: dict[str, object] = {"role": "assistant", "content": "done"}
        if self.tool_call_mode == "tool_call":
            message["tool_calls"] = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "ping", "arguments": "{}"},
                }
            ]
        return _text(
            request,
            json.dumps({"choices": [{"index": 0, "message": message}]}) + "\n",
        )

    def _compose(self, request: CommandRequest, argv: tuple[str, ...]):
        if self.block_on and self.block_on in request.display:
            deadline = time.monotonic() + 10.0
            while not self._gate and time.monotonic() < deadline:
                time.sleep(0.02)
        rest = argv[6:]
        op = rest[0]
        if op == "ps":
            if self.project is None or not self.project["running"]:
                return _outcome(
                    request, CommandStatus.FAILED, 1, b"", b"no such project\n"
                )
            if "-q" in rest:
                return _text(request, "abc123def456\n")
            raise AssertionError("ps --format json is not part of this surface")
        if op == "pull":
            self.pull_image_calls += 1
            return _text(request, "")
        if op == "up":
            if self.project is None or not self.project["running"]:
                self.up_calls += 1
            self.project = {"running": True}
            return _text(request, "")
        if op == "down":
            self.down_calls += 1
            self.project = None
            return _text(request, "")
        if op == "exec":
            if self.project is None or not self.project["running"]:
                return _outcome(
                    request, CommandStatus.FAILED, 1, b"", b"no such project\n"
                )
            payload = " ".join(rest[3:])
            if payload.startswith("ollama pull "):
                model = payload.removeprefix("ollama pull ")
                self.pulled_models[model] = (PULL_DIGEST, PULL_SIZE)
                return _text(request, "")
            if "partial" in payload:
                return _text(request, f"{self.partial_blobs}\n")
            raise AssertionError(f"unexpected exec payload: {payload!r}")
        raise AssertionError(f"unexpected compose op: {rest!r}")


def _outcome(
    request: CommandRequest,
    status: CommandStatus,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    *,
    timed_out: bool = False,
):
    return CommandOutcome(
        op_id=request.op_id,
        server_id=request.server_id,
        display=request.display,
        status=status,
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", "replace"),
        stdout_bytes=len(stdout),
        stdout_truncated=False,
        stderr=stderr.decode("utf-8", "replace"),
        stderr_bytes=len(stderr),
        stderr_truncated=False,
        timed_out=timed_out,
        cwd_change_failed=False,
        duration_seconds=0.001,
    )


def _text(request: CommandRequest, stdout: str):
    return _outcome(request, CommandStatus.SUCCEEDED, 0, stdout.encode(), b"")


def _deployed_target(recipe: ApplicationRecipe) -> _FakeTarget:
    """A target where the model service is already installed and running.

    Pull tests start from the post-install state: the bound Compose file is
    on disk (hash-matching) and the project is running — the pull path's
    verified preconditions.
    """
    target = _FakeTarget()
    plan = _plan_for(recipe)
    target.files[plan.compose_path] = plan.compose_text.encode("utf-8")
    target.project = {"running": True}
    return target


class _Clock:
    """Virtual monotonic/sleep pair: endpoint budgets expire instantly."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _virtual_time(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(local_models, "monotonic", clock.monotonic)
    monkeypatch.setattr(local_models, "sleep", clock.sleep)
    return clock


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _service(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    *,
    model_root: Path,
    approval_id: uuid.UUID | None = None,
    locks: ResourceLockService | None = None,
    adoption: ApplicationAdoption | None = None,
    store: ApplicationManifestStore | None = None,
    grants=None,
    policy: Policy | None = None,
    adopted=lambda server_id: True,
    descriptor=None,
    payload_hash: str | None = None,
    lease_ttl: float = 3600.0,
):
    plan = _plan_for(recipe)
    approvals = _StubApprovals(
        approval_id or _DEFAULT_APPROVAL_ID,
        payload_hash=(payload_hash if payload_hash is not None else plan.plan_sha256),
    )
    service = LocalModelService(
        transport=target,
        locks=locks or _StubLocks(),
        approvals=approvals,
        adoption=adoption or _StubAdoption(),
        store=store or _StubStore(),
        model_store=ModelConfigStore(model_root),
        grants=grants if grants is not None else (lambda: _grants()),
        policy=policy if policy is not None else (lambda: _policy()),
        adopted=adopted,
        descriptor=descriptor or _descriptor(),
        lease_ttl=lease_ttl,
    )
    return service, approvals


# ---------------------------------------------------------------------------
# AC1a: hardware prerequisite semantics (producer parity + fail-closed)
# ---------------------------------------------------------------------------


_APPROVED_FACTS: dict[str, object] = {
    "docker_compose_ready": True,
    "arch": "x86_64",
    "cpu_flags": "fpu sse sse2 avx avx2 bmi2",
    "ram_bytes": 122 * 1024**3,
    "disk_free_bytes": 490 * 1024**3,
}


@pytest.mark.parametrize(
    ("facts", "expected_unsatisfied"),
    [
        (_APPROVED_FACTS, set()),
        ({**_APPROVED_FACTS, "arch": "aarch64"}, {"hw.arch.x86_64"}),
        ({**_APPROVED_FACTS, "cpu_flags": "fpu sse sse2"}, {"hw.cpu.avx2"}),
        ({**_APPROVED_FACTS, "ram_bytes": 2 * 1024**3}, {"hw.ram.16gb"}),
        ({**_APPROVED_FACTS, "disk_free_bytes": 10 * 1024**3}, {"hw.disk.30gb"}),
        ({**_APPROVED_FACTS, "docker_compose_ready": False}, {"docker"}),
        # Exact budget boundaries are satisfied (>= semantics).
        ({**_APPROVED_FACTS, "ram_bytes": 16 * 1024**3}, set()),
        ({**_APPROVED_FACTS, "disk_free_bytes": 30 * 1024**3}, set()),
        # Fail-closed: wrong fact types never pass.
        ({**_APPROVED_FACTS, "ram_bytes": "122"}, {"hw.ram.16gb"}),
        ({**_APPROVED_FACTS, "ram_bytes": True}, {"hw.ram.16gb"}),
        ({**_APPROVED_FACTS, "cpu_flags": None}, {"hw.cpu.avx2"}),
    ],
)
def test_evaluate_hardware_checks_matches_producer_semantics(
    facts, expected_unsatisfied
) -> None:
    declared = {
        "docker",
        "hw.arch.x86_64",
        "hw.cpu.avx2",
        "hw.ram.16gb",
        "hw.disk.30gb",
    }
    assert evaluate_hardware_checks(declared, dict(facts)) == frozenset(
        expected_unsatisfied
    )


def test_nt_declared_vram_without_vram_fact_fails_closed() -> None:
    """``hw.vram.<N>gb`` is reserved for a future GPU-target revision: a
    recipe that declares it against a target with no measurable VRAM fails
    closed, and a measured budget satisfies the exact bound."""
    facts = dict(_APPROVED_FACTS)  # no vram_bytes fact
    assert evaluate_hardware_checks({"hw.vram.4gb"}, facts) == frozenset(
        {"hw.vram.4gb"}
    )
    measured = {**facts, "vram_bytes": 4 * 1024**3}
    assert evaluate_hardware_checks({"hw.vram.4gb"}, measured) == frozenset()


@pytest.mark.parametrize(
    "token",
    ["hw.ram.16GB", "hw.gpu.big", "hw.disk.", "rm -rf", "hw.vram.4gb.extra!"],
)
def test_nt_out_of_grammar_tokens_fail_closed(token) -> None:
    facts = dict(_APPROVED_FACTS)
    assert token in evaluate_hardware_checks({token}, facts)


def test_nt_unmeasurable_fact_fails_closed(tmp_path: Path) -> None:
    """A fact read that dies (transport error) satisfies nothing."""
    recipe = _recipe()

    class _DeadTransport:
        def __call__(self, request: CommandRequest):
            raise local_models.CommandRequestError("transport broken")

    facts, unknown, _notes = collect_target_facts(_DeadTransport(), SERVER_ID)
    unsatisfied = evaluate_hardware_checks(frozenset(recipe.prerequisites), facts)
    assert unsatisfied == frozenset(recipe.prerequisites)
    assert set(unknown) >= {
        "arch",
        "cpu_flags",
        "ram_bytes",
        "docker_root",
        "disk_free_bytes",
    }


def test_nt_disk_measurement_fails_closed(tmp_path: Path) -> None:
    """A daemon that answers but NO statable df target leaves the disk check
    unsatisfied — the budget is never guessed."""
    recipe = _recipe()
    target = _FakeTarget()
    target.df_ok = False
    facts, unknown, _notes = collect_target_facts(target, SERVER_ID)
    assert facts["docker_compose_ready"] is True
    assert "disk_free_bytes" in unknown
    assert "hw.disk.30gb" in evaluate_hardware_checks(
        frozenset(recipe.prerequisites), facts
    )


def test_disk_falls_back_to_statable_ancestor_with_note() -> None:
    """A root-only Docker data root (df refuses) is measured at the nearest
    statable ancestor — the filesystem that will hold the data — and the
    fallback is recorded as a measurement note (the producer precedent:
    'docker data root not statable unprivileged')."""
    target = _FakeTarget()
    target.df_fail_paths = {"/var/lib/docker"}
    facts, unknown, notes = collect_target_facts(target, SERVER_ID)
    assert facts["disk_free_bytes"] == target.disk_free_bytes
    assert "disk_free_bytes" not in unknown
    assert len(notes) == 1
    assert "measured at /" in notes[0]
    assert "not statable" in notes[0]


# ---------------------------------------------------------------------------
# AC1b: model-service install through the granted lifecycle tools
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_install_happy_path_verifies_real_endpoint_and_converges(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, approvals = _service(target, recipe, model_root=tmp_path)
    request = _install_request(recipe)

    result = await service.install_service(request)

    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert result.endpoint_verified is True
    assert result.reason is None
    assert result.lock_released is True
    assert approvals.consume_calls == 1
    # The receipts carry the full plan vocabulary, endpoint gate included.
    receipt_ops = [op.op for op in result.operations]
    assert receipt_ops == [
        "verify_prerequisites",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
        "wait_healthy",
    ]
    # The canonical update converged with the deterministic intent id.
    plan = _plan_for(recipe)
    adoption = service._adoption
    assert isinstance(adoption, _StubAdoption)
    assert len(adoption.adopt_calls) == 1
    assert adoption.adopt_calls[0][1] == install_intent_id(
        SERVER_ID, APP_ID, plan.plan_sha256
    )
    assert result.manifest_revision == 1
    # The lease went through the shared application key.
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert locks.acquired == [
        (str(SERVER_ID), f"{LOCK_RESOURCE_PREFIX}{APP_ID}", f"model-install:{RUN_ID}")
    ]
    assert len(locks.released) == 1


@pytest.mark.anyio
async def test_install_rerun_converges_without_second_create(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, approvals = _service(target, recipe, model_root=tmp_path)
    request = _install_request(recipe)

    first = await service.install_service(request)
    second = await service.install_service(request)

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert target.up_calls == 1  # no duplicate Compose project
    assert approvals.consume_calls == 1  # fast path consumes nothing
    adoption = service._adoption
    assert isinstance(adoption, _StubAdoption)
    assert len(adoption.adopt_calls) == 2
    assert adoption.adopt_calls[0][1] == adoption.adopt_calls[1][1]


@pytest.mark.anyio
async def test_install_records_digest_pinned_compose(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, _approvals = _service(target, recipe, model_root=tmp_path)
    plan = _plan_for(recipe)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "succeeded"
    written = target.files[plan.compose_path]
    assert written.decode("utf-8") == plan.compose_text
    document = yaml.safe_load(plan.compose_text)
    service_document = document["services"][recipe.name]
    assert service_document["image"] == recipe.image
    assert service_document["image"].startswith("docker.io/ollama/ollama@sha256:")
    # Honest absence: the no-curl recipe renders NO impossible healthcheck.
    assert "healthcheck" not in service_document
    assert service_document["volumes"] == [f"{plan.volumes[0].host_path}:/root/.ollama"]
    assert document["name"] == f"mai-app-{APP_ID.hex}"


@pytest.mark.anyio
async def test_install_pull_timeout_cut_is_outcome_unknown(tmp_path: Path) -> None:
    """The bounded lane's cut during the IMAGE pull (plan op) is honest."""
    recipe = _recipe()
    target = _FakeTarget()
    target.fail["pull pinned application image"] = "unknown"
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert locks.released == []  # lease kept: lapse fences the key


# ---------------------------------------------------------------------------
# AC1c: weight pull — granted, bounded, checksum/license metadata
# ---------------------------------------------------------------------------


def _pull_payload(recipe: ApplicationRecipe) -> str:
    request = _pull_request(recipe)
    plan = _plan_for(recipe)
    return local_models._pull_payload_sha256(request, plan.recipe_sha256)


@pytest.mark.anyio
async def test_pull_happy_path_registers_connection_with_observed_tools(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )
    request = _pull_request(recipe)

    result = await service.pull_model(request)

    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert result.approval_id == _DEFAULT_APPROVAL_ID
    assert result.lock_released is True
    assert approvals.consume_calls == 1
    # Checksum/size metadata straight from the serving daemon.
    assert result.digest == PULL_DIGEST
    assert result.size_bytes == PULL_SIZE
    # License metadata carried value-free into the receipt.
    assert result.license == PULL_LICENSE
    # Honest capability verdict from the real probe round.
    assert result.tool_calls_observed is True
    # The connection lives in the REAL C02 store, fully validated.
    assert result.connection_name == EXPECTED_CONNECTION_NAME
    store: ModelConfigStore = service._model_store
    stored = store.read_connection(EXPECTED_CONNECTION_NAME)
    assert stored.revision == 1
    assert result.connection_revision == 1
    assert result.connection_sha256 == stored.sha256
    parsed = parse_connection(json.loads(stored.text))
    assert parsed.base_url == PULL_BASE_URL
    assert parsed.capabilities.tool_calls is True
    assert parsed.capabilities.reasoning is False
    assert parsed.credential_ref is None


@pytest.mark.anyio
async def test_pull_registration_rerun_is_a_verified_noop(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    payload = _pull_payload(recipe)
    service, _approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=payload,
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    first = await service.pull_model(_pull_request(recipe))
    # A second acquisition is a NEW decision (one-use approvals): re-wire a
    # fresh approval bound to the same pull payload, same target and store.
    second_service, _second_approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=payload,
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )
    second = await second_service.pull_model(_pull_request(recipe))

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    store: ModelConfigStore = second_service._model_store
    stored = store.read_connection(EXPECTED_CONNECTION_NAME)
    assert stored.revision == 1  # not rewritten
    assert second.connection_revision == 1
    noop = [op for op in second.operations if op.op == "register_model_connection"]
    assert [op.action for op in noop] == ["verified"]


@pytest.mark.anyio
async def test_pull_records_checksum_and_license_metadata(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )
    request = _pull_request(recipe)

    result = await service.pull_model(request)

    assert result.payload_sha256 == _pull_payload(recipe)
    operations = {op.op: op for op in result.operations}
    assert operations["pull_model_weights"].subject == PULL_MODEL
    assert operations["pull_model_weights"].status is CommandStatus.SUCCEEDED
    # Both real listings were read: the daemon's own inventory and the
    # OpenAI-compatible endpoint.
    assert [op.subject for op in result.operations if op.op == "read_model_list"] == [
        "/api/tags",
        "/v1/models",
    ]
    # The pull payload binding is NOT the deployment plan hash: one
    # decision approves exactly one acquisition (C03 rule 4).
    plan = _plan_for(recipe)
    assert plan.plan_sha256 != result.payload_sha256


@pytest.mark.anyio
async def test_pull_without_tool_support_reports_honestly(tmp_path: Path) -> None:
    """Plain answers and HTTP refusals register tool_calls=False — never a
    claim that every model is capable."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    target.tool_call_mode = "plain"
    service, _approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "succeeded"
    assert result.tool_calls_observed is False
    store: ModelConfigStore = service._model_store
    parsed = parse_connection(
        json.loads(store.read_connection(EXPECTED_CONNECTION_NAME).text)
    )
    assert parsed.capabilities.tool_calls is False
    assert parsed.capabilities.reasoning is False

    # An endpoint that REFUSES the tools request registers the same honesty.
    target2 = _deployed_target(recipe)
    target2.tool_call_mode = "http_error"
    service2, _approvals2 = _service(
        target2,
        recipe,
        model_root=tmp_path / "second",
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )
    result2 = await service2.pull_model(_pull_request(recipe))
    assert result2.status == "succeeded"
    assert result2.tool_calls_observed is False
    assert any("tool-call probe" in note for note in result2.notes)
    store2: ModelConfigStore = service2._model_store
    parsed2 = parse_connection(
        json.loads(store2.read_connection(EXPECTED_CONNECTION_NAME).text)
    )
    assert parsed2.capabilities.tool_calls is False


# ---------------------------------------------------------------------------
# Real boundary: facts measured on the approved coordinator-class host
# ---------------------------------------------------------------------------


@pytest.mark.real_target
@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not installed on this host"
)
@pytest.mark.skipif(
    platform.machine() != "x86_64", reason="approved v1 target is x86_64"
)
def test_real_target_facts_satisfy_the_approved_recipe() -> None:
    """The fact collectors measure the REAL local Docker daemon (read-only)
    and the approved recipe's prerequisites evaluate satisfied on the
    approved host class — the data-root budget is measured, never assumed."""
    recipe = _recipe()

    def local_transport(request: CommandRequest) -> CommandOutcome:
        assert request.shell is None
        try:
            proc = subprocess.run(
                request.argv,
                capture_output=True,
                timeout=max(5, int(request.timeout)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _outcome(
                request,
                CommandStatus.OUTCOME_UNKNOWN,
                None,
                b"",
                b"",
                timed_out=True,
            )
        except FileNotFoundError:
            return _outcome(request, CommandStatus.FAILED, 127, b"", b"not found\n")
        status = (
            CommandStatus.SUCCEEDED if proc.returncode == 0 else CommandStatus.FAILED
        )
        return _outcome(request, status, proc.returncode, proc.stdout, proc.stderr)

    facts, unknown, measurement_notes = collect_target_facts(local_transport, SERVER_ID)
    assert facts["docker_compose_ready"] is True
    assert facts["arch"] == platform.machine()
    free = facts["disk_free_bytes"]
    assert isinstance(free, int) and free > 0
    assert "disk_free_bytes" not in unknown
    unsatisfied = evaluate_hardware_checks(frozenset(recipe.prerequisites), facts)
    assert unsatisfied == frozenset(), (
        f"approved host no longer satisfies: {sorted(unsatisfied)}; facts={facts}"
    )
    # The measurement caveat (if the data root was not statable) is honest
    # and surfaced, never swallowed.
    assert all("measured at" in note for note in measurement_notes)


# ---------------------------------------------------------------------------
# Negative controls: the safety boundaries are load-bearing (C03/C05/C09)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_nt_wiring_rejects_wrong_descriptor(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    with pytest.raises(ModelInstallError) as foreign:
        _service(
            target,
            recipe,
            model_root=tmp_path,
            descriptor=_descriptor(identifier="app.install"),
        )
    assert foreign.value.code is ModelInstallErrorCode.INVALID_REQUEST
    with pytest.raises(ModelInstallError) as weak:
        _service(
            target,
            recipe,
            model_root=tmp_path,
            descriptor=_descriptor(effect_class="mutate"),
        )
    assert weak.value.code is ModelInstallErrorCode.INVALID_REQUEST


@pytest.mark.anyio
async def test_nt_policy_denied_install_never_touches_the_target(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        policy=lambda: _policy(denied=frozenset({CAPABILITY_ID})),
    )

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "policy_denied:capability_denied_by_policy"
    assert approvals.consume_calls == 0
    assert approvals.unconsumed()
    assert target.calls == []
    # P08 parity: the lease is taken for the gated apply and released on
    # the denial — acquired once, never held.
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert len(locks.acquired) == 1
    assert len(locks.released) == 1


@pytest.mark.anyio
async def test_nt_policy_denied_pull_never_touches_the_target(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        policy=lambda: _policy(denied=frozenset({CAPABILITY_ID})),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "policy_denied:capability_denied_by_policy"
    assert approvals.consume_calls == 0
    assert target.pulled_models == {}
    assert store_connections(service) == ()


@pytest.mark.anyio
async def test_nt_install_approval_payload_mismatch_denies_and_preserves(
    tmp_path: Path,
) -> None:
    """An approval bound to a different payload can never approve this plan
    (C03 rule 4): denied, effect not attempted, decision not consumed."""
    recipe = _recipe()
    target = _FakeTarget()
    service, approvals = _service(
        target, recipe, model_root=tmp_path, payload_hash="deadbeef" * 8
    )

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "approval_rejected:payload_mismatch"
    assert result.approval_id == _DEFAULT_APPROVAL_ID
    assert target.up_calls == 0
    assert not target.wrote_compose()
    assert approvals.unconsumed()


@pytest.mark.anyio
async def test_nt_pull_approval_bound_to_plan_hash_is_rejected(
    tmp_path: Path,
) -> None:
    """The pull consumes only a decision bound to the PULL payload — the
    install plan hash approves the deployment, never a weight acquisition."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    plan = _plan_for(recipe)
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=plan.plan_sha256,
        adoption=_StubAdoption(existing_compose_project=plan.project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "approval_rejected:payload_mismatch"
    assert target.pulled_models == {}
    assert store_connections(service) == ()


@pytest.mark.anyio
async def test_nt_unsatisfied_hardware_refuses_before_any_effect(
    tmp_path: Path,
) -> None:
    """No AVX2 ⇒ refused with the exact unsatisfied token, before any write
    or start, and before the one-use decision is spent."""
    recipe = _recipe()
    target = _FakeTarget()
    target.cpu_flags = "fpu sse sse2"  # no avx2
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "hardware_unsatisfied"
    assert result.hardware_unsatisfied == ("hw.cpu.avx2",)
    assert approvals.consume_calls == 0
    assert target.up_calls == 0
    assert not target.wrote_compose()
    # Only the read-only fact measurements ever reached the target.
    assert {call.display for call in target.calls} == {
        "measure model-install prerequisite fact"
    }
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert len(locks.released) == 1


@pytest.mark.anyio
async def test_nt_underbudget_disk_refuses_before_any_effect(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    target.disk_free_bytes = 10 * 1024**3  # under the hw.disk.30gb budget
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.hardware_unsatisfied == ("hw.disk.30gb",)
    assert approvals.consume_calls == 0
    assert target.up_calls == 0


def _mutant_recipe(mutate) -> ApplicationRecipe:
    document = yaml.safe_load(_document_texts(INFERENCE_FILE)[0])
    mutate(document)
    return parse_recipe_text(yaml.safe_dump(document))


@pytest.mark.anyio
async def test_nt_unknown_prerequisite_token_refuses_before_contact(
    tmp_path: Path,
) -> None:
    """The recipe schema accepts any lowercase token shape; the executor
    refuses the families it cannot verify — before any target contact."""

    def add_token(document: dict[str, object]) -> None:
        document["prerequisites"] = [*document["prerequisites"], "hw.gpu.big"]

    recipe = _mutant_recipe(add_token)
    target = _FakeTarget()
    service, _approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.reason == "unsupported_prerequisite"
    assert "hw.gpu.big" in result.notes[0]
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_lock_unavailable_refuses(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, approvals = _service(
        target, recipe, model_root=tmp_path, locks=_StubLocks(refuse_acquire=True)
    )

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "lock_unavailable:held"
    assert approvals.consume_calls == 0
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_lease_lapse_mid_apply_is_outcome_unknown_and_keeps_fence(
    tmp_path: Path,
) -> None:
    """A lapsed lease aborts the apply as outcome_unknown: no rollback (the
    prior process may live), no manifest, and the lease is left to lapse
    into a fence (LOCK-FENCE)."""
    recipe = _recipe()
    target = _FakeTarget()
    target.block_on = "start managed compose project"
    locks = _StubLocks(lapse_heartbeats=True)
    service, approvals = _service(
        target, recipe, model_root=tmp_path, locks=locks, lease_ttl=2.0
    )

    def _unblock_after_heartbeat() -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if locks.heartbeat_calls >= 1:
                target.unblock()
                return
            time.sleep(0.01)
        target.unblock()

    unblocker = threading.Thread(target=_unblock_after_heartbeat, daemon=True)
    unblocker.start()
    result = await service.install_service(_install_request(recipe))
    unblocker.join(timeout=5.0)

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False
    assert locks.released == []
    assert target.down_calls == 0  # never rolled back on unknown
    adoption = service._adoption
    assert isinstance(adoption, _StubAdoption)
    assert adoption.adopt_calls == []
    assert approvals.consume_calls == 1


@pytest.mark.anyio
async def test_nt_endpoint_never_answering_rolls_back(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    target.fail["verify model endpoint responds"] = "failed"  # rc 1, never 200
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "rolled_back"
    assert result.effect == "executed"
    assert result.endpoint_verified is False
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert target.down_calls == 1
    assert any(
        "endpoint did not answer within the start budget" in note
        for note in result.notes
    )
    adoption = service._adoption
    assert isinstance(adoption, _StubAdoption)
    assert adoption.adopt_calls == []  # no canonical record for an unverified gate
    assert result.manifest_revision is None


@pytest.mark.anyio
async def test_nt_garbage_endpoint_body_rolls_back(tmp_path: Path) -> None:
    """A 200 that is not a real model list is a failure, never a pass."""
    recipe = _recipe()
    target = _FakeTarget()
    target.models_body = "<html>hello</html>"
    service, _approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "rolled_back"
    assert result.endpoint_verified is False
    assert target.down_calls == 1
    assert any("body was not a model list" in note for note in result.notes)


def test_real_fresh_daemon_null_data_body_is_an_empty_model_list() -> None:
    """The REAL pinned ollama daemon (v0.40.6) answers a fresh install with
    ``{"object":"list","data":null}`` — present-but-null is the valid empty
    list, never garbage (11-verify fix-1 regression)."""
    assert local_models._parse_model_list('{"object":"list","data":null}') == []
    # The native shape carries the same present-null convention.
    assert local_models._parse_model_list('{"models":null}') == []
    # A genuinely missing list key stays unparseable (fail closed).
    assert local_models._parse_model_list('{"object":"list"}') is None


@pytest.mark.anyio
async def test_install_accepts_real_fresh_daemon_null_data_endpoint(
    tmp_path: Path,
) -> None:
    """The full completion gate verifies against the REAL fresh daemon
    answer: a 200 ``{"object":"list","data":null}`` body is a served (empty)
    model list — the install completes instead of rolling back."""
    recipe = _recipe()
    target = _FakeTarget()
    target.models_body = '{"object":"list","data":null}'
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "succeeded"
    assert result.endpoint_verified is True
    assert result.reason is None
    assert target.down_calls == 0  # no rollback of a genuinely healthy install
    assert approvals.consume_calls == 1


@pytest.mark.anyio
async def test_nt_port_conflict_refuses(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    target.listening = {18114}
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "port_conflict"
    assert target.up_calls == 0
    assert approvals.consume_calls == 0


@pytest.mark.anyio
async def test_nt_cross_server_conflict_refuses_without_contact(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, _approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        store=_StubStore(foreign_servers=(OTHER_SERVER_ID,)),
    )

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "cross_server_conflict"
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_external_record_conflict_refuses(tmp_path: Path) -> None:
    """The identity is bound to an adopted EXTERNAL project elsewhere — the
    install never touches another deployment's files (C09 rule 6)."""
    recipe = _recipe()
    target = _FakeTarget()
    service, _approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        adoption=_StubAdoption(existing_compose_project="someone-elses-project"),
    )

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.reason == "external_record_conflict"
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_state_diverged_install_refuses(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    target.files[_plan_for(recipe).compose_path] = b"hand-edited compose\n"
    service, approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.install_service(_install_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "state_diverged"
    assert any("compose file hash differs" in note for note in result.notes)
    assert approvals.consume_calls == 0
    assert target.up_calls == 0


@pytest.mark.anyio
async def test_nt_pull_not_installed_refuses_without_contact(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    service, _approvals = _service(target, recipe, model_root=tmp_path)

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "service_not_installed"
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_pull_service_not_running_refuses(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _FakeTarget()
    target.files[_plan_for(recipe).compose_path] = _plan_for(
        recipe
    ).compose_text.encode("utf-8")
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.reason == "service_not_running"
    assert approvals.consume_calls == 0
    assert target.pulled_models == {}


@pytest.mark.anyio
async def test_nt_pull_cross_server_conflict_refuses(tmp_path: Path) -> None:
    recipe = _recipe()
    target = _deployed_target(recipe)
    service, _approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        store=_StubStore(foreign_servers=(OTHER_SERVER_ID,)),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.reason == "cross_server_conflict"
    assert target.calls == []


@pytest.mark.anyio
async def test_nt_pull_curl_absent_fails_closed(tmp_path: Path) -> None:
    """Without curl NOTHING can be verified honestly: no pull, no probe, no
    registration — a refused, not a guess."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    target.curl_present = False
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "endpoint_unverified"
    assert any("curl is not installed" in note for note in result.notes)
    assert result.lock_released is True
    assert approvals.consume_calls == 0
    assert target.pulled_models == {}
    assert store_connections(service) == ()
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert len(locks.released) == 1


@pytest.mark.anyio
async def test_nt_pull_failure_records_partials_and_registers_nothing(
    tmp_path: Path,
) -> None:
    """A deterministic pull failure is honest: resumable partial blobs are
    counted and recorded, nothing is registered, success is never claimed."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    target.partial_blobs = 2
    target.fail["pull model weights"] = "failed"
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "failed"
    assert result.effect == "executed"  # the pull attempt really happened
    assert result.digest is None
    assert result.size_bytes is None
    assert any("2 partial blob(s) remain for resume" in note for note in result.notes)
    assert result.tool_calls_observed is False
    assert approvals.consume_calls == 1
    assert store_connections(service) == ()
    assert result.lock_released is True


@pytest.mark.anyio
async def test_nt_pull_outcome_unknown_keeps_lease_and_registers_nothing(
    tmp_path: Path,
) -> None:
    """The lane's cancellation cut leaves the pull outcome unknowable: the
    lease is left to lapse into a fence, nothing is registered."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    target.fail["pull model weights"] = "unknown"
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False
    locks = service._locks
    assert isinstance(locks, _StubLocks)
    assert locks.released == []
    assert approvals.consume_calls == 1
    assert store_connections(service) == ()
    assert result.digest is None


@pytest.mark.anyio
async def test_nt_pull_registration_never_overwrites_diverged_connection(
    tmp_path: Path,
) -> None:
    """An existing connection with different content is admin configuration:
    the pull lands, the registration refuses, the record is untouched."""
    recipe = _recipe()
    target = _deployed_target(recipe)
    service, approvals = _service(
        target,
        recipe,
        model_root=tmp_path,
        payload_hash=_pull_payload(recipe),
        adoption=_StubAdoption(existing_compose_project=_plan_for(recipe).project_name),
    )
    store: ModelConfigStore = service._model_store
    seed = (
        json.dumps(
            {
                "schema_version": 1,
                "name": EXPECTED_CONNECTION_NAME,
                "base_url": "http://10.0.0.9:9999/v1",
                "timeout_seconds": 60.0,
                "capabilities": {"tool_calls": False, "reasoning": False},
            },
            sort_keys=True,
        )
        + "\n"
    )
    store.write_connection(EXPECTED_CONNECTION_NAME, seed, expected_revision=0)

    result = await service.pull_model(_pull_request(recipe))

    assert result.status == "failed"
    assert result.effect == "executed"  # the weights landed
    assert result.reason == "registration_refused"
    assert any("never overwritten" in note for note in result.notes)
    assert result.connection_revision is None
    stored = store.read_connection(EXPECTED_CONNECTION_NAME)
    assert stored.text == seed  # byte-identical: not rewritten
    assert stored.revision == 1


@pytest.mark.anyio
async def test_nt_pull_model_reference_grammar() -> None:
    recipe = _recipe()
    with pytest.raises(ModelInstallError) as shell:
        _pull_request(recipe, model="qwen2.5; rm -rf /")
    assert shell.value.code is ModelInstallErrorCode.INVALID_REQUEST
    with pytest.raises(ModelInstallError) as space:
        _pull_request(recipe, model="qwen 2.5 7b")
    assert space.value.code is ModelInstallErrorCode.INVALID_REQUEST


@pytest.mark.anyio
async def test_nt_pull_license_metadata_is_required() -> None:
    recipe = _recipe()
    for missing in ("", "   ", None):
        with pytest.raises(ModelInstallError) as exc:
            _pull_request(recipe, license_text=missing)  # type: ignore[arg-type]
        assert exc.value.code is ModelInstallErrorCode.INVALID_REQUEST


@pytest.mark.anyio
async def test_nt_pull_base_url_cannot_carry_credentials() -> None:
    recipe = _recipe()
    with pytest.raises(ModelInstallError) as userinfo:
        _pull_request(recipe, base_url="http://admin:hunter2@host:18114/v1")
    assert userinfo.value.code is ModelInstallErrorCode.INVALID_REQUEST
    with pytest.raises(ModelInstallError) as query:
        _pull_request(recipe, base_url="http://host:18114/v1?x=1")
    assert query.value.code is ModelInstallErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# helpers used by the negative controls
# ---------------------------------------------------------------------------


def store_connections(service: LocalModelService) -> tuple[str, ...]:
    model_store: ModelConfigStore = service._model_store
    return model_store.list_connections()
