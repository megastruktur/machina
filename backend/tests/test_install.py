"""Focused tests for application installation (card 08-install).

Acceptance coverage (card mai-08-install):

- **AC1 (apply the bound plan under dispatcher/lock)** — the executor
  applies ONLY the rebuilt plan's frozen operation vocabulary, in plan
  order, after the full P03 gate (policy decision + one-use approval
  consumed with the plan hash binding) and under one exclusive
  ``application:<id>`` lease (P05). External paths are respected: a
  canonical record binding the identity to another project refuses
  (``external_record_conflict``), a manifest under another server refuses
  (``cross_server_conflict``), and credential references resolve from the
  configured ``.env`` source at effect time into the plan's
  ``credentials.env`` path — never into compose, receipts or results.
- **AC2 (real verification + interrupted install reconciles)** —
  completion requires the Docker health verdict AND (when published) a
  target-side loopback HTTP probe answering the expected status; failures
  after the start effect roll back with VERIFIED container removal; an
  interrupted apply is ``outcome_unknown`` and keeps the lease (lapse →
  fence); a resume verifies current target state and converges without a
  duplicate Compose project or a second create.

Test posture: the target boundary is a faithful in-memory simulator of
the exact command surface install.py drives (the same commands the real
sshd/docker lane would see, including the base64 file-write channel);
the approval/lock/adoption seams run BOTH as semantic stubs (pure tests)
and as the REAL Postgres-backed stores (live-DB section, disposable
fixture) — per the test policy, mocks never prove the concurrency/store
claims, the live section does. The catalogue fixtures (example-http,
example-stateful) are the frozen 08-fixture-recipes revision streams.
The ``real_target``-marked section additionally drives the executor over
the REAL transport: a disposable Arch sshd container mounting the host
docker socket (``mai-fxt-08-inst-*`` namespace, mirroring 08-discovery's
fixture), executing the real ``docker compose`` CLI against the host
daemon for example-http — happy path with BOTH health gates, an
interrupted → resumed apply with single-create idempotence, and a
deterministic health-failure rollback — while the canonical
manifest/projection commits go through the real Postgres stores.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mai.applications.install as install_module
from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    AdoptionResult,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.install import (
    CAPABILITY_ID,
    DEFAULT_LEASE_TTL_SECONDS,
    LOCK_RESOURCE_PREFIX,
    InstallError,
    InstallErrorCode,
    InstallRequest,
    InstallService,
    install_intent_id,
    ssh_transport,
)
from mai.applications.recipes import (
    ApplicationRecipe,
    TargetEnvironment,
    build_deployment_plan,
    parse_recipe_text,
    project_name,
)
from mai.credentials.env import EnvResolver
from mai.execution.commands import CommandRequest, CommandStatus
from mai.execution.ssh import (
    SshConnectError,
    SshIdentity,
    pin_host_key,
    resolve_identity_key,
    scan_host_keys,
)
from mai.jobs.resource_locks import LockLease, ResourceLockService
from mai.plugins.registry import parse_descriptor
from mai.policy.approvals import Approval, ApprovalError, ApprovalErrorCode, ApprovalStore
from mai.policy.evaluate import ExecutionContext, PolicyMode

try:  # live-DB section needs psycopg + the migration chain
    import psycopg

    from mai.database import create_app_engine
except ImportError:  # pragma: no cover - psycopg is a locked dependency
    psycopg = None  # type: ignore[assignment]

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
REPO_ROOT = BACKEND_ROOT.parent
CATALOGUE_DIR = REPO_ROOT / "catalogue"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
REVISION_IDS = ("0001", "0002", "0003", "0004", "0005", "0006")

HTTP_FILE = "example-http.yaml"
STATEFUL_FILE = "example-stateful.yaml"

TARGET = TargetEnvironment(os="linux", arch="x86_64")

SERVER_ID = uuid.UUID("08080808-0808-4808-8808-080808080808")
APP_ID = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c0c01")
OTHER_SERVER_ID = uuid.UUID("09090909-0909-4909-8909-090909090909")
RUN_ID = uuid.UUID("07070707-0707-4707-8707-070707070707")

POLICY_REVISION = 1
PRINCIPAL = "admin-test"

#: Synthetic non-disclosure marker (NOT a secret): planted as a credential
#: VALUE in the .env fixture; no receipt, note, error or display may carry it.
SECRET_SENTINEL = "SENTINEL-mai-08-install-not-a-secret"
CREDENTIAL_NAME = "APP_TOKEN"


# ---------------------------------------------------------------------------
# Catalogue fixtures (frozen 08-fixture-recipes revision streams)
# ---------------------------------------------------------------------------


def _document_texts(filename: str) -> list[str]:
    stream = (CATALOGUE_DIR / filename).read_text(encoding="utf-8")
    return [yaml.serialize(node) for node in yaml.compose_all(stream)]


def _recipe(filename: str = HTTP_FILE, revision: int = 0) -> ApplicationRecipe:
    return parse_recipe_text(_document_texts(filename)[revision])


def _mutant_recipe(filename: str, mutate) -> ApplicationRecipe:
    document = yaml.safe_load(_document_texts(filename)[0])
    mutate(document)
    return parse_recipe_text(yaml.safe_dump(document))


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


def _context(
    server_id: uuid.UUID = SERVER_ID,
    credentials: tuple[str, ...] = (),
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_ID,
        parent_scope=None,
        profile_revision="profile-r1",
        prompt_revision="prompt-r1",
        credential_references=credentials,
        allowed_capabilities=(CAPABILITY_ID,),
        mode=PolicyMode.ASK,
    )


def _grants(server_id: uuid.UUID = SERVER_ID, *, granted: bool = True):
    return {
        CAPABILITY_ID: frozenset({server_id} if granted else set()),
    }


#: Shared default approval id so tests that do not exercise approval
#: binding explicitly still wire service and request consistently.
_DEFAULT_APPROVAL_ID = uuid.UUID("0d0d0d0d-0d0d-4d0d-8d0d-0d0d0d0d0d0d")


def _request(
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    backup_confirmed: bool = False,
    context: ExecutionContext | None = None,
    application_id: uuid.UUID = APP_ID,
) -> InstallRequest:
    return InstallRequest(
        server_id=context.server_id if context else SERVER_ID,
        application_id=application_id,
        recipe=recipe,
        target=TARGET,
        context=context or _context(credentials=tuple(recipe.credentials)),
        approval_id=approval_id or _DEFAULT_APPROVAL_ID,
        backup_confirmed=backup_confirmed,
    )


# ---------------------------------------------------------------------------
# Semantic stubs (subclass the real types: wiring checks stay enforced)
# ---------------------------------------------------------------------------


class _StubLocks(ResourceLockService):
    """In-memory lease semantics: acquire/heartbeat/release bookkeeping."""

    def __init__(self, *, lapse_heartbeats: bool = False) -> None:
        self.acquired: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.heartbeat_calls = 0
        self._lapse = lapse_heartbeats

    def acquire(self, server_id, resource, *, owner_ref="", ttl=None):  # type: ignore[override]
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
            raise ApprovalError(
                ApprovalErrorCode.NOT_FOUND, "no approval record for id"
            )
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


class _StubAdoption(ApplicationAdoption):
    """In-memory canonical-update pipeline (read/adopt/recover)."""

    def __init__(self, existing_compose_project: str | None = None) -> None:
        self.existing_compose_project = existing_compose_project
        self.adopt_calls: list[tuple[AdoptionRequest, uuid.UUID | None]] = []
        self.recover_calls = 0

    def _existing(self, server_id, application_id):
        if self.existing_compose_project is None:
            return None

        class _Row:
            pass

        row = _Row()
        row.revision = 3
        row.compose_project = self.existing_compose_project
        return row

    async def read(self, server_id, application_id):  # type: ignore[override]
        return self._existing(server_id, application_id)

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
# The target simulator: the exact command surface install.py drives
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"umask 077 && mkdir -p (?P<dir>\S+) && "
    r"printf %s '(?P<b64>[A-Za-z0-9+/=]+)' \| base64 -d > "
    r"(?P<path>\S+)\.tmp && mv \S+\.tmp (?P=path) && sha256sum -- (?P=path)"
)


class _FakeTarget:
    """In-memory target executing exactly the commands install.py emits.

    Faithful where it matters for the acceptance claims: file writes go
    through the real base64 shell channel (decoded and stored byte-exact),
    hashes are recomputed from stored bytes, the Compose project state is
    a first-class mutable (running/healthy), ports are a listening set,
    and scripted failures can turn any op into ``failed`` (rc 1) or
    ``outcome_unknown`` (timeout cut).
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.listening: set[int] = set()
        self.volume_data: dict[str, tuple[str, ...]] = {}
        self.project: dict[str, bool] | None = None
        self.healthy_after_up = True
        self.curl_present = True
        #: When set, the published-port probe answers this status code
        #: regardless of container health (independent fault injection).
        self.curl_status: str | None = None
        self.ss_present = True
        self.up_calls = 0
        self.down_calls = 0
        self.fail: dict[str, str] = {}
        self.block_on: str | None = None
        self._gate = False
        self.calls: list[CommandRequest] = []

    # -- scripted control ---------------------------------------------------

    def unblock(self) -> None:
        self._gate = True

    # -- the transport callable ----------------------------------------------

    def __call__(self, request: CommandRequest):
        self.calls.append(request)
        scripted = next(
            (v for k, v in self.fail.items() if k in request.display), None
        )
        if scripted == "unknown":
            return _outcome(request, CommandStatus.OUTCOME_UNKNOWN, None, b"", b"",
                            timed_out=True)
        if scripted == "failed":
            return _outcome(request, CommandStatus.FAILED, 1, b"", b"scripted failure\n")
        argv = request.argv or ()
        if request.shell is not None:
            return self._write(request)
        if argv[:2] == ("docker", "version"):
            return _text(request, "24.0.7\n")
        if argv[:1] == ("ss",):
            if not self.ss_present:
                return _outcome(request, CommandStatus.FAILED, 127, b"", b"sh: ss: not found\n")
            lines = [
                f"LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:*\n" for port in sorted(self.listening)
            ]
            return _text(request, "".join(lines))
        if argv[:1] == ("sha256sum",):
            path = argv[-1]
            data = self.files.get(path)
            if data is None:
                return _outcome(request, CommandStatus.FAILED, 1, b"",
                                b"sha256sum: cannot open\n")
            return _text(request, f"{hashlib.sha256(data).hexdigest()}  {path}\n")
        if argv[:1] == ("ls",):
            path = argv[-1]
            entries = self.volume_data.get(path)
            if entries is None:
                return _outcome(request, CommandStatus.FAILED, 2, b"",
                                b"ls: cannot access\n")
            return _text(request, "".join(f"{e}\n" for e in entries))
        if argv[:1] == ("mkdir",):
            self.dirs.update(argv[3:])
            return _text(request, "")
        if argv[:1] == ("curl",):
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
        digest = hashlib.sha256(self.files[path]).hexdigest()
        return _text(request, f"{digest}  {path}\n")

    def _curl(self, request: CommandRequest, argv: tuple[str, ...]):
        if not self.curl_present:
            return _outcome(request, CommandStatus.FAILED, 127, b"", b"sh: curl: not found\n")
        url = argv[-1]
        match = re.search(r":(\d+)", url)
        assert match is not None, f"unparseable probe url: {url!r}"
        port = int(match.group(1))
        if self.project is None or not self.project["running"]:
            return _outcome(request, CommandStatus.FAILED, 7, b"", b"connection refused\n")
        assert f":{port}" in url
        if self.curl_status is not None:
            return _text(request, self.curl_status)
        if self.project["healthy"]:
            return _text(request, "200")
        return _text(request, "500")

    def _compose(self, request: CommandRequest, argv: tuple[str, ...]):
        rest = argv[6:]
        if self.block_on and self.block_on in request.display:
            import time

            deadline = time.monotonic() + 10.0
            while not self._gate and time.monotonic() < deadline:
                time.sleep(0.02)
        op = rest[0]
        if op == "ps":
            if self.project is None or not self.project["running"]:
                return _outcome(request, CommandStatus.FAILED, 1, b"",
                                b"no such project\n")
            if "-q" in rest:
                return _text(request, "abc123def456\n")
            healthy = "healthy" if self.project["healthy"] else "unhealthy"
            import json

            return _text(
                request,
                json.dumps(
                    [{"Name": "fixture-1", "State": "running", "Health": healthy}]
                )
                + "\n",
            )
        if op == "pull":
            return _text(request, "")
        if op == "up":
            if self.project is None or not self.project["running"]:
                self.up_calls += 1
            self.project = {"running": True, "healthy": self.healthy_after_up}
            return _text(request, "")
        if op == "down":
            self.down_calls += 1
            self.project = None
            return _text(request, "")
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
    from mai.execution.commands import CommandOutcome

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


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _plan_for(recipe: ApplicationRecipe, application_id: uuid.UUID = APP_ID):
    return build_deployment_plan(recipe, SERVER_ID, application_id, TARGET)


def _service(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    locks: ResourceLockService | None = None,
    adoption: ApplicationAdoption | None = None,
    store: ApplicationManifestStore | None = None,
    grants=None,
    policy=None,
    adopted=lambda server_id: True,
    credentials=None,
    descriptor=None,
    env_values: dict[str, str] | None = None,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
):
    plan = _plan_for(recipe)
    approvals = _StubApprovals(
        approval_id or _DEFAULT_APPROVAL_ID, payload_hash=plan.plan_sha256
    )
    resolver_tmp = None
    if credentials is None:
        resolver_tmp = _env_file(env_values if env_values is not None else {})
        credentials = lambda: EnvResolver(resolver_tmp)  # noqa: E731
    return (
        InstallService(
            transport=target,
            locks=locks or _StubLocks(),
            approvals=approvals,
            adoption=adoption or _StubAdoption(),
            store=store or _StubStore(),
            grants=grants if grants is not None else (lambda: _grants()),
            policy=policy if policy is not None else (lambda: _policy()),
            adopted=adopted,
            credentials=credentials,
            descriptor=descriptor or _descriptor(),
            lease_ttl=lease_ttl,
        ),
        approvals,
    )


def _policy(
    revision: int = POLICY_REVISION,
    denied: frozenset[str] = frozenset(),
):
    from mai.policy.evaluate import Policy

    return Policy(revision=revision, denied_capabilities=denied)


def _env_file(values: dict[str, str]) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".env", delete=False, encoding="utf-8"
    )
    for name, value in values.items():
        handle.write(f"{name}={value}\n")
    handle.close()
    os.chmod(handle.name, 0o600)
    return Path(handle.name)


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch, request):
    """The health wait must not actually sleep in tests (fake clock)."""
    if request.node.get_closest_marker("real_target") is not None:
        return None  # real-boundary tests drive the REAL clock and waits
    state = {"t": 0.0}

    monkeypatch.setattr(install_module, "monotonic", lambda: state["t"])

    def advance(seconds: float) -> None:
        state["t"] += seconds

    monkeypatch.setattr(install_module, "sleep", advance)
    return state


@pytest.fixture
def http_recipe() -> ApplicationRecipe:
    return _recipe(HTTP_FILE)


@pytest.fixture
def stateful_recipe() -> ApplicationRecipe:
    return _recipe(STATEFUL_FILE)


# ---------------------------------------------------------------------------
# AC1: gate order, plan-bound apply, identity and external-path guards
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_happy_path_applies_plan_operations_in_order_under_gates(
    http_recipe,
) -> None:
    target = _FakeTarget()
    service, approvals = _service(target, http_recipe)
    result = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )

    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert result.reason is None
    assert result.health_verified is True
    assert result.published_ports == (18080,)
    assert result.lock_released is True
    assert approvals.consume_calls == 1
    assert result.approval_id == approvals.approval_id

    # The applied operation receipts follow the plan's frozen vocabulary.
    assert [op.op for op in result.operations] == [
        "verify_prerequisites",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
        "wait_healthy",
    ]
    statuses = {op.action for op in result.operations}
    assert statuses == {"applied", "verified"}  # wait_healthy is verified

    # Only the bound plan's resource surface was touched.
    compose = _plan_for(http_recipe)
    assert target.files[compose.compose_path] == compose.compose_text.encode()
    assert result.manifest_revision == 1
    stub_adoption = service._adoption
    assert stub_adoption.adopt_calls, "canonical update must run on success"
    request, intent = stub_adoption.adopt_calls[0]
    assert request.origin is ApplicationOrigin.COMPOSE
    assert request.compose_project == compose.project_name
    assert intent == install_intent_id(
        SERVER_ID, APP_ID, compose.plan_sha256
    )


@pytest.mark.anyio
async def test_policy_denial_causes_no_effect_and_spends_nothing(http_recipe) -> None:
    target = _FakeTarget()
    service, approvals = _service(
        target, http_recipe, grants=lambda: {}  # capability not granted at all
    )
    result = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "policy_denied:capability_not_granted"
    assert target.calls == []  # not even a read reached the target
    assert approvals.consume_calls == 0
    assert result.lock_released is True


@pytest.mark.anyio
async def test_server_scope_mismatch_refuses(http_recipe) -> None:
    target = _FakeTarget()
    service, _ = _service(
        target,
        http_recipe,
        grants=lambda: {CAPABILITY_ID: frozenset({OTHER_SERVER_ID})},
    )
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "policy_denied:server_scope_mismatch"
    assert target.calls == []


@pytest.mark.anyio
async def test_policy_deny_list_refuses(http_recipe) -> None:
    target = _FakeTarget()
    service, _ = _service(
        target, http_recipe, policy=lambda: _policy(denied=frozenset({CAPABILITY_ID}))
    )
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "policy_denied:capability_denied_by_policy"
    assert target.calls == []


@pytest.mark.anyio
async def test_unenrolled_server_refused(http_recipe) -> None:
    target = _FakeTarget()
    service, _ = _service(target, http_recipe, adopted=lambda server_id: False)
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "policy_denied:server_not_adopted"
    assert target.calls == []


def test_context_target_mismatch_is_a_construction_rejection(http_recipe) -> None:
    with pytest.raises(InstallError) as error:
        InstallRequest(
            server_id=SERVER_ID,
            application_id=APP_ID,
            recipe=http_recipe,
            target=TARGET,
            context=_context(server_id=OTHER_SERVER_ID),
            approval_id=uuid.uuid4(),
        )
    assert error.value.code is InstallErrorCode.INVALID_REQUEST


@pytest.mark.anyio
async def test_wiring_checks_fail_closed(http_recipe) -> None:
    target = _FakeTarget()
    with pytest.raises(InstallError):
        InstallService(
            transport=target,
            locks=_StubLocks(),
            approvals=_StubApprovals(uuid.uuid4(), payload_hash="x"),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(identifier="other.capability"),
        )
    with pytest.raises(InstallError):
        InstallService(
            transport=target,
            locks=_StubLocks(),
            approvals=_StubApprovals(uuid.uuid4(), payload_hash="x"),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(effect_class="mutate"),
        )


@pytest.mark.anyio
async def test_altered_plan_hash_denies_the_approval_and_has_no_effect(
    http_recipe,
) -> None:
    # The approval was minted for a DIFFERENT plan hash (the recipe changed
    # after approval): C03 rule 4 altered-payload denial, nothing consumed.
    target = _FakeTarget()
    other_plan = _plan_for(_recipe(HTTP_FILE, revision=1))  # v2 digest
    approvals = _StubApprovals(uuid.uuid4(), payload_hash=other_plan.plan_sha256)
    service = InstallService(
        transport=target,
        locks=_StubLocks(),
        approvals=approvals,
        adoption=_StubAdoption(),
        store=_StubStore(),
        grants=lambda: _grants(),
        policy=lambda: _policy(),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(),
    )
    result = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )
    assert result.status == "refused"
    assert result.reason == "approval_rejected:payload_mismatch"
    assert approvals.consume_calls == 1  # the refusal WAS evaluated, once
    assert target.up_calls == 0
    writes = [c for c in target.calls if c.shell is not None]
    assert writes == []


@pytest.mark.anyio
async def test_cross_server_identity_conflict_refuses(http_recipe) -> None:
    target = _FakeTarget()
    service, _ = _service(
        target, http_recipe, store=_StubStore(foreign_servers=(OTHER_SERVER_ID,))
    )
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "cross_server_conflict"
    assert target.calls == []


@pytest.mark.anyio
async def test_external_record_conflict_refuses(http_recipe) -> None:
    target = _FakeTarget()
    service, _ = _service(
        target, http_recipe, adoption=_StubAdoption(existing_compose_project="someone-else")
    )
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "external_record_conflict"
    assert target.calls == []


@pytest.mark.anyio
async def test_unsupported_prerequisite_refuses(http_recipe) -> None:
    recipe = _mutant_recipe(
        HTTP_FILE, lambda d: d.update(prerequisites=["puppet"])
    )
    target = _FakeTarget()
    service, _ = _service(target, recipe)
    result = await service.install(_request(recipe))
    assert result.status == "refused"
    assert result.reason == "unsupported_prerequisite"
    assert target.calls == []


@pytest.mark.anyio
async def test_undeclared_health_refuses_before_any_effect(http_recipe) -> None:
    recipe = _mutant_recipe(HTTP_FILE, lambda d: d.pop("health"))
    target = _FakeTarget()
    service, approvals = _service(target, recipe)
    result = await service.install(_request(recipe, approval_id=approvals.approval_id))
    assert result.status == "refused"
    assert result.reason == "health_not_declared"
    assert target.calls == []  # refused before even the pre-flight read
    assert approvals.consume_calls == 0


@pytest.mark.anyio
async def test_port_conflict_refuses_a_fresh_install(http_recipe) -> None:
    target = _FakeTarget()
    target.listening.add(18080)
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "port_conflict"
    assert target.up_calls == 0
    assert target.files == {}


@pytest.mark.anyio
async def test_missing_ss_records_honest_limitation_and_proceeds(http_recipe) -> None:
    target = _FakeTarget()
    target.ss_present = False
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "succeeded"
    assert any("preflight unavailable" in n for n in result.notes)


@pytest.mark.anyio
async def test_diverged_compose_file_refuses(http_recipe) -> None:
    target = _FakeTarget()
    plan = _plan_for(http_recipe)
    target.files[plan.compose_path] = b"someone edited this"
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "refused"
    assert result.reason == "state_diverged"
    assert target.up_calls == 0


# ---------------------------------------------------------------------------
# AC1: credentials resolve by reference; values never leak into receipts
# ---------------------------------------------------------------------------


def _credential_recipe() -> ApplicationRecipe:
    return _mutant_recipe(
        HTTP_FILE,
        lambda d: d["deploy"].update(credentials=[CREDENTIAL_NAME]),
    )


@pytest.mark.anyio
async def test_missing_credential_refuses_before_spending_approval() -> None:
    recipe = _credential_recipe()
    target = _FakeTarget()
    service, approvals = _service(target, recipe, env_values={})
    result = await service.install(
        _request(recipe, approval_id=approvals.approval_id)
    )
    assert result.status == "refused"
    assert result.reason == "credential_unavailable"
    assert approvals.consume_calls == 0
    # Refusal before the effect: reads may have happened, nothing mutated.
    assert all(call.shell is None for call in target.calls)
    assert target.up_calls == 0
    assert target.files == {}


@pytest.mark.anyio
async def test_credentials_provision_without_value_leaks() -> None:
    recipe = _credential_recipe()
    target = _FakeTarget()
    service, _ = _service(
        target, recipe, env_values={CREDENTIAL_NAME: SECRET_SENTINEL}
    )
    result = await service.install(_request(recipe))
    assert result.status == "succeeded"

    plan = _plan_for(recipe)
    # The deliberate disclosure point: the target-side credentials.env.
    provisioned = target.files[plan.credentials_path]
    assert provisioned == f"{CREDENTIAL_NAME}={SECRET_SENTINEL}\n".encode()

    # Compose carries the env_file POINTER, never a value.
    assert any("env_file:" in line for line in plan.compose_text.splitlines())
    assert SECRET_SENTINEL not in plan.compose_text

    # No receipt, note, reason or transport display carries the value.
    rendered = repr(result)
    for operation in result.operations:
        rendered += repr(operation)
    assert SECRET_SENTINEL not in rendered
    for call in target.calls:
        assert SECRET_SENTINEL not in call.display


# ---------------------------------------------------------------------------
# AC2: real verification, verified rollback, honest unknown outcomes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_failure_rolls_back_with_verified_removal(http_recipe) -> None:
    target = _FakeTarget()
    target.fail["start managed compose project"] = "failed"
    service, approvals = _service(target, http_recipe)
    result = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )
    assert result.status == "rolled_back"
    assert result.effect == "executed"
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert target.down_calls == 1
    assert target.project is None  # verified: ps -q returned empty
    stub_adoption = service._adoption
    assert stub_adoption.adopt_calls == []  # no canonical record for failure
    assert result.manifest_revision is None


@pytest.mark.anyio
async def test_pull_failure_before_start_fails_without_rollback(http_recipe) -> None:
    target = _FakeTarget()
    target.fail["pull pinned application image"] = "failed"
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "failed"
    assert result.effect == "not_attempted"
    assert result.rollback_attempted is False
    assert target.down_calls == 0
    assert target.up_calls == 0
    assert result.lock_released is True


@pytest.mark.anyio
async def test_unknown_outcome_keeps_the_lease_and_never_rolls_back(
    http_recipe,
) -> None:
    target = _FakeTarget()
    target.fail["pull pinned application image"] = "unknown"
    locks = _StubLocks()
    service, approvals = _service(target, http_recipe, locks=locks)
    result = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False
    assert locks.released == []  # the lease must lapse into a fence
    assert target.down_calls == 0  # no duplicate mutation on fenced ground
    stub_adoption = service._adoption
    assert stub_adoption.adopt_calls == []


@pytest.mark.anyio
async def test_lease_lapse_aborts_the_apply(http_recipe) -> None:
    target = _FakeTarget()
    target.block_on = "pull pinned application image"
    locks = _StubLocks(lapse_heartbeats=True)
    service, _ = _service(target, http_recipe, locks=locks, lease_ttl=1.0)
    import asyncio

    task = asyncio.ensure_future(service.install(_request(http_recipe)))
    await asyncio.sleep(1.3)  # let the heartbeat thread fire and lapse
    target.unblock()
    result = await task
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert any("lapsed" in note for note in result.notes)
    assert result.lock_released is False
    assert target.up_calls == 0  # aborted before the start effect
    assert locks.released == []


@pytest.mark.anyio
async def test_health_gate_failure_rolls_back(http_recipe) -> None:
    target = _FakeTarget()
    target.healthy_after_up = False
    target.curl_present = False
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "rolled_back"
    assert result.health_verified is False
    assert result.rollback_verified is True
    assert target.down_calls == 1


@pytest.mark.anyio
async def test_published_port_probe_must_answer_expected_status(http_recipe) -> None:
    target = _FakeTarget()
    target.curl_status = "500"  # container healthy, host probe answers 500
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "rolled_back"
    assert any("did not return the expected status" in n for n in result.notes)


@pytest.mark.anyio
async def test_unpublished_health_port_is_unrepresentable() -> None:
    """The recipe schema refuses a health port outside the published set
    (health_port_unbound), so a validated plan always carries a published
    probe target — the installer can never skip host-side verification
    for a recipe that parsed."""
    from mai.applications.recipes import RecipeErrorCode

    with pytest.raises(Exception) as error:
        _mutant_recipe(
            HTTP_FILE, lambda d: d["deploy"].update(ports=[])
        )
    assert error.value.code is RecipeErrorCode.HEALTH_PORT_UNBOUND


@pytest.mark.anyio
async def test_prerequisite_probe_failure_fails_without_effects(http_recipe) -> None:
    target = _FakeTarget()
    target.fail["verify declared prerequisites"] = "failed"
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "failed"
    assert result.effect == "not_attempted"
    assert target.up_calls == 0
    assert target.down_calls == 0


@pytest.mark.anyio
async def test_corrupted_write_is_a_detected_failure(http_recipe) -> None:
    target = _FakeTarget()
    plan = _plan_for(http_recipe)

    original = target._write

    def corrupting_write(request: CommandRequest):
        outcome = original(request)
        target.files[plan.compose_path] = b"corrupted"
        return outcome

    target._write = corrupting_write  # type: ignore[method-assign]
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "failed"
    receipts = [op for op in result.operations if op.op == "write_compose"]
    assert receipts[-1].status is CommandStatus.FAILED
    stub_adoption = service._adoption
    assert stub_adoption.adopt_calls == []


# ---------------------------------------------------------------------------
# AC2: interrupted install reconciles instead of duplicating
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resume_after_interruption_converges_without_duplicate(
    http_recipe,
) -> None:
    target = _FakeTarget()
    plan = _plan_for(http_recipe)

    # Attempt 1: killed mid-install (timeout cut at pull, AFTER the compose
    # write). Honest outcome; lease stays; no canonical record.
    target.fail["pull pinned application image"] = "unknown"
    locks = _StubLocks()
    first_approvals = _StubApprovals(uuid.uuid4(), payload_hash=plan.plan_sha256)
    adoption = _StubAdoption()
    service = _wire(target, http_recipe, locks, first_approvals, adoption)
    first = await service.install(
        _request(http_recipe, approval_id=first_approvals.approval_id)
    )
    assert first.status == "outcome_unknown"
    assert first.lock_released is False
    assert target.up_calls == 0

    # Attempt 2 (the resumed run): fresh approval, same deterministic
    # intent; the apply verifies the already-written compose file, pulls,
    # starts ONCE and converges the canonical record.
    target.fail.clear()
    second_approvals = _StubApprovals(uuid.uuid4(), payload_hash=plan.plan_sha256)
    resumed = _wire(target, http_recipe, locks, second_approvals, adoption)
    second = await resumed.install(
        _request(http_recipe, approval_id=second_approvals.approval_id)
    )
    assert second.status == "succeeded"
    assert second.health_verified is True
    assert target.up_calls == 1  # exactly one project create across both
    compose_receipt = [
        op for op in second.operations if op.op == "write_compose"
    ]
    assert len(compose_receipt) == 1
    assert compose_receipt[0].action == "verified"  # skipped as verified-present
    assert second.manifest_revision == 1
    # One canonical update, deterministic intent id shared by both attempts.
    assert len(adoption.adopt_calls) == 1
    _, intent = adoption.adopt_calls[0]
    assert intent == install_intent_id(SERVER_ID, APP_ID, plan.plan_sha256)


@pytest.mark.anyio
async def test_reinstall_of_a_verified_installation_mutates_nothing(
    http_recipe,
) -> None:
    target = _FakeTarget()
    plan = _plan_for(http_recipe)
    locks = _StubLocks()
    adoption = _StubAdoption()
    approvals = _StubApprovals(uuid.uuid4(), payload_hash=plan.plan_sha256)
    service = _wire(target, http_recipe, locks, approvals, adoption)
    first = await service.install(
        _request(http_recipe, approval_id=approvals.approval_id)
    )
    assert first.status == "succeeded"
    assert target.up_calls == 1

    second_approvals = _StubApprovals(uuid.uuid4(), payload_hash=plan.plan_sha256)
    again = _wire(target, http_recipe, locks, second_approvals, adoption)
    second = await again.install(
        _request(http_recipe, approval_id=second_approvals.approval_id)
    )
    assert second.status == "succeeded"
    assert target.up_calls == 1  # no duplicate create
    assert target.down_calls == 0
    # The converge path runs only the prerequisite + health verification
    # and the canonical-record update — no mutating operation.
    assert [op.op for op in second.operations] == [
        "verify_prerequisites",
        "wait_healthy",
    ]
    assert adoption.adopt_calls[1][1] == adoption.adopt_calls[0][1]


@pytest.mark.anyio
async def test_backup_gate_demands_confirmation_for_existing_data(
    stateful_recipe,
) -> None:
    target = _FakeTarget()
    plan = _plan_for(stateful_recipe)
    target.volume_data[plan.volumes[0].host_path] = ("sentinel.txt",)

    service, _ = _service(target, stateful_recipe)
    refused = await service.install(_request(stateful_recipe))
    assert refused.status == "refused"
    assert refused.reason == "backup_required"
    assert target.up_calls == 0

    confirmed = _FakeTarget()
    confirmed.volume_data[plan.volumes[0].host_path] = ("sentinel.txt",)
    service_ok, _ = _service(confirmed, stateful_recipe)
    result = await service_ok.install(
        _request(stateful_recipe, backup_confirmed=True)
    )
    assert result.status == "succeeded"
    # The verified no-op receipt is recorded for the backup operation.
    backup_receipts = [
        op for op in result.operations if op.op == "require_backup"
    ]
    assert backup_receipts and backup_receipts[0].action == "verified"


@pytest.mark.anyio
async def test_fresh_volume_locations_need_no_backup_confirmation(
    stateful_recipe,
) -> None:
    target = _FakeTarget()  # no data anywhere
    service, _ = _service(target, stateful_recipe)
    result = await service.install(_request(stateful_recipe))
    assert result.status == "succeeded"


@pytest.mark.anyio
async def test_unknown_state_read_is_outcome_unknown(http_recipe) -> None:
    from mai.execution.commands import CommandRequestError

    class _BrokenHashTarget(_FakeTarget):
        """Simulates a transport wiring bug on the compose-hash read."""

        def __call__(self, request: CommandRequest):
            if request.argv and request.argv[0] == "sha256sum":
                raise CommandRequestError("malformed read request (wiring bug)")
            return super().__call__(request)

    target = _BrokenHashTarget()
    service, _ = _service(target, http_recipe)
    result = await service.install(_request(http_recipe))
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False


def _wire(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    locks: ResourceLockService,
    approvals: ApprovalStore,
    adoption: ApplicationAdoption,
):
    return InstallService(
        transport=target,
        locks=locks,
        approvals=approvals,
        adoption=adoption,
        store=_StubStore(),
        grants=lambda: _grants(),
        policy=lambda: _policy(),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(),
    )


# ---------------------------------------------------------------------------
# Live-DB section: REAL approval store, REAL lock service, REAL adoption
# pipeline (disposable Postgres fixture; skipped without the URL)
# ---------------------------------------------------------------------------


def _alembic_config():
    from alembic.config import Config

    return Config(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    if psycopg is None:  # pragma: no cover
        pytest.skip("psycopg unavailable")
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the install integration claims need "
            "a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS application_projection_journal, "
                "application_projection, knowledge_doc_chunks, "
                "knowledge_documents, job_schedule_fires, job_schedules, "
                "job_events, job_steps, job_runs, approvals, "
                "profile_projection_journal, profile_projection, "
                "admin_sessions, admin_credential, alembic_version"
            )
            conn.commit()
        from alembic import command

        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await engine.dispose()


@pytest.fixture
def file_store(tmp_path: Path) -> ApplicationManifestStore:
    return ApplicationManifestStore(tmp_path / "data-root")


@pytest.fixture
def live_service_factory(sessions, file_store, migrated_url):
    def build(
        target: _FakeTarget, *, lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS
    ) -> tuple[InstallService, ApprovalStore, ResourceLockService, ApplicationAdoption]:
        approvals = ApprovalStore(sessions)
        locks = ResourceLockService(migrated_url)
        adoption = ApplicationAdoption(sessions)
        service = InstallService(
            transport=target,
            locks=locks,
            approvals=approvals,
            adoption=adoption,
            store=file_store,
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(),
            lease_ttl=lease_ttl,
        )
        return service, approvals, locks, adoption

    return build


@pytest.mark.anyio
async def test_live_full_gate_path_with_real_stores(
    http_recipe, sessions, file_store, live_service_factory, tmp_path
) -> None:
    target = _FakeTarget()
    service, approvals, locks, adoption = live_service_factory(target)
    plan = _plan_for(http_recipe)

    approval = await approvals.create(
        _approval_request(plan.plan_sha256, SERVER_ID)
    )
    result = await service.install(
        _request(http_recipe, approval_id=approval.approval_id)
    )
    assert result.status == "succeeded"
    assert result.health_verified is True
    assert result.manifest_revision == 1

    # REAL canonical manifest at the C02 path + REAL projection row.
    manifest_path = (
        file_store.root
        / "servers"
        / str(SERVER_ID)
        / "applications"
        / str(APP_ID)
        / "manifest.yaml"
    )
    assert manifest_path.is_file()
    record = await adoption.read(SERVER_ID, APP_ID)
    assert record is not None
    assert record.revision == 1
    assert record.compose_project == plan.project_name
    assert record.stale is False

    # REAL lease released (row gone).
    assert locks.inspect(str(SERVER_ID), f"{LOCK_RESOURCE_PREFIX}{APP_ID}") is None

    # REAL one-use decision consumed.
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"

    # The journal is fully reconciled (no live intents).
    assert await adoption.list_live_intents(server_id=SERVER_ID) == []


@pytest.mark.anyio
async def test_live_approval_is_one_use_across_replays(
    http_recipe, live_service_factory
) -> None:
    target = _FakeTarget()
    service, approvals, _locks, _adoption = live_service_factory(target)
    plan = _plan_for(http_recipe)
    approval = await approvals.create(
        _approval_request(plan.plan_sha256, SERVER_ID)
    )
    first = await service.install(
        _request(http_recipe, approval_id=approval.approval_id)
    )
    assert first.status == "succeeded"
    assert target.up_calls == 1

    # Later the project disappears (worker crash on the target); a replay
    # with the SAME decision must refuse: the one-use approval was spent.
    target.project = None
    replay = await service.install(
        _request(http_recipe, approval_id=approval.approval_id)
    )
    assert replay.status == "refused"
    assert replay.reason == "approval_rejected:already_consumed"
    assert target.up_calls == 1  # no second effect


@pytest.mark.anyio
async def test_live_interrupted_resume_deduplicates_the_canonical_record(
    http_recipe, live_service_factory
) -> None:
    target = _FakeTarget()
    target.fail["pull pinned application image"] = "unknown"
    # A short lease so the test can observe the REAL lapse→fence path.
    service, approvals, locks, adoption = live_service_factory(target, lease_ttl=1.0)
    plan = _plan_for(http_recipe)
    resource = f"{LOCK_RESOURCE_PREFIX}{APP_ID}"

    first_approval = await approvals.create(
        _approval_request(plan.plan_sha256, SERVER_ID)
    )
    first = await service.install(
        _request(http_recipe, approval_id=first_approval.approval_id)
    )
    assert first.status == "outcome_unknown"
    assert first.lock_released is False
    assert locks.inspect(str(SERVER_ID), resource) is not None  # still held

    # The killed worker stops heartbeating: the lease lapses and any
    # successor is FENCED until an explicit host-verified reconciliation.
    import asyncio

    await asyncio.sleep(1.3)
    second_approval = await approvals.create(
        _approval_request(plan.plan_sha256, SERVER_ID)
    )
    fenced = await service.install(
        _request(http_recipe, approval_id=second_approval.approval_id)
    )
    assert fenced.status == "refused"
    assert fenced.reason == "lock_unavailable:fenced"

    # Reconciliation verifies the interrupted effect is TERMINAL on the
    # target (compose ps proves no project containers) and clears the
    # fence; only then does the resumed run proceed.
    assert target.project is None  # host-verified: no containers running
    from mai.execution.reconcile import ReconciliationClass

    locks.clear_fence(
        str(SERVER_ID),
        resource,
        verdict=ReconciliationClass.FINAL_CANCELLED,
        note="install interrupted; no project containers verified running",
    )

    # Resume: the compose file from the interrupted attempt is
    # verified-present; the project is created exactly once.
    target.fail.clear()
    third_approval = await approvals.create(
        _approval_request(plan.plan_sha256, SERVER_ID)
    )
    resumed = await service.install(
        _request(http_recipe, approval_id=third_approval.approval_id)
    )
    assert resumed.status == "succeeded"
    assert target.up_calls == 1
    record = await adoption.read(SERVER_ID, APP_ID)
    assert record is not None and record.revision == 1
    assert await adoption.list_live_intents(server_id=SERVER_ID) == []
    assert locks.inspect(str(SERVER_ID), resource) is None


# ---------------------------------------------------------------------------
# Real-boundary section: disposable Arch sshd + docker-socket fixture
# (``mai-fxt-08-inst-*`` namespace, mirroring 08-discovery's fixture).
# The executor drives the REAL ``docker compose`` CLI on the HOST daemon
# through the REAL P04 SSH transport, and the canonical manifest/projection
# commits go through the REAL Postgres stores above. Skipped without a
# working docker daemon; the DB fixtures skip themselves without
# MAI_TEST_DATABASE_URL.
#
# Secret hygiene: the fixture key is a throwaway in /tmp/mai-fxt-08-inst-*
# deleted with the fixture; no credential material enters any record.
#
# Teardown contract: containers, compose projects (containers + networks,
# including leftovers of a crashed predecessor run under the same fixed
# identities), the scratch dirs AND the fixture image are removed, with
# the proof string printed to the record. MAI_KEEP_08_INST_FIXTURE_IMAGE=1
# is a documented DEV iteration speed-up that keeps the built image; the
# recorded evidence runs never set it.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-08-inst-sshd"
FIXTURE_IMAGE_TAG = "mai-fxt-08-inst-sshd:latest"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-08-inst")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_08_INST_FIXTURE"

#: Fixed identity pairs, one per real test, so target dirs, compose
#: projects, locks and canonical records never collide across tests — and
#: a crashed predecessor run's leftovers are findable under the SAME names.
REAL_SERVER_A = uuid.UUID("08080808-0808-4808-8808-0808080800a1")
REAL_SERVER_B = uuid.UUID("08080808-0808-4808-8808-0808080800b2")
REAL_SERVER_C = uuid.UUID("08080808-0808-4808-8808-0808080800c3")
REAL_APP_A = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00a1")
REAL_APP_B = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00b2")
REAL_APP_C = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00c3")

#: Published host ports: test (a) keeps the canonical recipe port; (b)/(c)
#: mutate their recipe's host port so concurrently-live deployments of the
#: other tests never trip the honest port pre-flight.
REAL_PORT_A = 18080
REAL_PORT_B = 18081
REAL_PORT_C = 18082


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _project_container_ids(project: str) -> list[str]:
    out = _docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
        "--format", "{{.ID}}",
    )
    assert out.returncode == 0, f"docker ps failed: {out.stderr[-400:]}"
    return out.stdout.split()


def _project_network_ids(project: str) -> list[str]:
    out = _docker(
        "network", "ls", "-q", "--filter",
        f"label=com.docker.compose.project={project}",
    )
    assert out.returncode == 0, f"docker network ls failed: {out.stderr[-400:]}"
    return out.stdout.split()


def _container_health(container_id: str) -> str:
    out = _docker(
        "inspect", "--format", "{{.State.Health.Status}}", container_id
    )
    assert out.returncode == 0, f"docker inspect failed: {out.stderr[-400:]}"
    return out.stdout.strip()


class InstallFixture:
    """Arch sshd + docker-CLI container on the HOST network namespace.

    Host networking is the production topology this card's health gate
    assumes: the published port answers on the TARGET's own loopback, and
    the target-side ``curl`` probe observes exactly what a real host
    would. NOTE: this workstation's docker daemon lives in a foreign
    mount namespace (shared socket + network, NOT the client's
    filesystem — bind mounts of client paths do not propagate), so all
    target-side file observations go through ``docker exec``.
    """

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.identity: SshIdentity | None = None
        self.cleanup_proof = "<not cleaned>"
        self._projects: set[str] = set()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> SshIdentity:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_path),
             "-C", f"{FIXTURE_CONTAINER}-test"],
            check=True, capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        self._sweep_leftovers()
        # A predecessor run killed before its teardown leaves the named
        # container behind; the namespace is producer-owned — reclaim it.
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        self._ensure_image()
        self.port = self._free_port()
        run = _docker(
            "run", "--rm", "-d", "--name", FIXTURE_CONTAINER,
            "--network", "host",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            FIXTURE_IMAGE_TAG,
            "sh", "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            # sshd must NOT be PID 1: a PID-namespace init is immune to
            # signals from inside (even SIGKILL — pkill still reports rc 0),
            # so a PID-1 sshd listener could never be deterministically
            # killed for the interruption test. As a plain child it dies,
            # and its parent-side bind makes `sshd` rc 0 a REAL rebind proof
            # (with -D the bind failure would be invisible behind rc 0).
            "&& /usr/bin/sshd -o ListenAddress=127.0.0.1 "
            f"-p {self.port} && exec sleep infinity",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._provision_authorized_key()
        self._wait_keyscan()
        compose = _docker("exec", FIXTURE_CONTAINER, "docker", "compose", "version")
        assert compose.returncode == 0, (
            "fixture docker compose plugin unavailable: "
            f"{compose.stderr[-400:]}{compose.stdout[-200:]}"
        )
        self.identity = SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user="root",
            host_key=self._current_fingerprint(),
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )
        return self.identity

    def _ensure_image(self) -> None:
        if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
            return
        context = self.scratch / "build-context"
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text(
            "FROM archlinux:latest\n"
            "# openssh: the target sshd. docker: the CLI only — the daemon\n"
            "# is the HOST's, reached through the mounted socket.\n"
            "# docker-compose: the compose plugin binary (Arch ships the\n"
            "# standalone v2 binary; the symlink registers it as a\n"
            "# `docker compose` CLI plugin). curl: the target-side\n"
            "# published-port probe. iproute2: the ss port pre-flight.\n"
            "RUN pacman-key --init "
            "&& pacman -Sy --noconfirm openssh docker docker-compose curl "
            "iproute2 && ssh-keygen -A "
            "&& mkdir -p /usr/local/lib/docker/cli-plugins "
            "&& ln -sf /usr/bin/docker-compose "
            "/usr/local/lib/docker/cli-plugins/docker-compose\n",
            encoding="utf-8",
        )
        build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1800)
        assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _sweep_leftovers() -> None:
        """Remove compose leftovers of a crashed predecessor run (same
        fixed identities ⇒ same deterministic project names)."""
        for app_id in (REAL_APP_A, REAL_APP_B, REAL_APP_C):
            for cid in _project_container_ids(project_name(app_id)):
                _docker("rm", "-f", cid, timeout=60)
            for nid in _project_network_ids(project_name(app_id)):
                _docker("network", "rm", nid)

    def _provision_authorized_key(self) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
             "&& cat > /root/.ssh/authorized_keys "
             "&& chmod 600 /root/.ssh/authorized_keys"],
            input=pub, capture_output=True, timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519", "127.0.0.1"],
            capture_output=True, text=True, timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        (ed25519,) = [line for line in lines if " ssh-ed25519 " in line] or [lines[0]]
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        return keygen.stdout.split()[1]

    def _wait_keyscan(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self._scan_now():
                    return
                last_error = "empty keyscan"
            except subprocess.TimeoutExpired as error:
                last_error = str(error)
            time.sleep(0.5)
        raise AssertionError(f"fixture sshd never became ready: {last_error}")

    # -- test-facing helpers ---------------------------------------------------

    def enroll(self) -> tuple[str, str]:
        """Resolve the key + pin the host key; returns (key_path, known_hosts)."""
        assert self.identity is not None
        env_file = self.scratch / "fixture.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        key_path = str(resolve_identity_key(EnvResolver(env_file), FIXTURE_CREDENTIAL_REF))
        assert scan_host_keys(self.identity), "keyscan produced no host keys"
        known_hosts = self.scratch / "known_hosts"
        pin_host_key(self.identity, known_hosts)
        return key_path, str(known_hosts)

    def kill_sshd(self) -> int:
        """Deterministic worker-death: kill the sshd session handlers
        FIRST (modern OpenSSH runs per-connection ``sshd-session``
        processes — killing only the listener would leave live sessions
        intact), then the listener so the executor's next connection is
        refused. Live SSH transports die (rc 255 → SshConnectError)."""
        first = _docker(
            "exec", FIXTURE_CONTAINER,
            "pkill", "-9", "-x", "sshd-session", timeout=30,
        )
        second = _docker(
            "exec", FIXTURE_CONTAINER, "pkill", "-9", "-x", "sshd", timeout=30,
        )
        return 0 if 0 in (first.returncode, second.returncode) else first.returncode

    def restart_sshd(self) -> None:
        """Restart sshd in place: same port, same host keys (they live in
        the container FS and were not regenerated), same pinned identity."""
        assert self.identity is not None
        out = _docker(
            "exec", FIXTURE_CONTAINER, "sh", "-c",
            "mkdir -p /run/sshd && exec /usr/bin/sshd -o ListenAddress=127.0.0.1 "
            f"-p {self.port}",
            timeout=30,
        )
        assert out.returncode == 0, f"sshd restart failed: {out.stderr[-400:]}"
        self._wait_keyscan()

    def track(self, project: str) -> None:
        self._projects.add(project)

    def compose_file_present(self, compose_path: str) -> bool:
        """Target-side observation through the daemon (NOT the client FS):
        did the executor's write channel create the plan's compose file?"""
        out = _docker(
            "exec", FIXTURE_CONTAINER, "sh", "-c", f"test -f {compose_path}",
            timeout=30,
        )
        assert out.returncode in (0, 1), f"exec probe failed: {out.stderr[-300:]}"
        return out.returncode == 0

    def cleanup(self) -> str:
        parts: list[str] = []
        projects = self._projects | {
            project_name(a) for a in (REAL_APP_A, REAL_APP_B, REAL_APP_C)
        }
        for name in sorted(projects):
            ids = _project_container_ids(name)
            if ids:
                rm = _docker("rm", "-f", *ids, timeout=120)
                parts.append(f"{name}: {len(ids)} container(s) rm rc={rm.returncode}")
            else:
                parts.append(f"{name}: no containers")
            nets = _project_network_ids(name)
            if nets:
                _docker("network", "rm", *nets, timeout=60)
                parts.append(f"{name}: {len(nets)} network(s) removed")
        rm = _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        parts.append(f"fixture container rm rc={rm.returncode}")
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$",
            "--format", "{{.Names}}",
        )
        parts.append(f"fixture container after rm: '{listing.stdout.strip() or '<none>'}'")
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        parts.append(f"scratch {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}")
        if os.environ.get("MAI_KEEP_08_INST_FIXTURE_IMAGE"):
            parts.append("fixture image KEPT (MAI_KEEP_08_INST_FIXTURE_IMAGE set)")
        else:
            rmi = _docker("rmi", "-f", FIXTURE_IMAGE_TAG, timeout=120)
            parts.append(f"fixture image rmi rc={rmi.returncode}")
        self.cleanup_proof = "; ".join(parts)
        return self.cleanup_proof


@pytest.fixture(scope="module")
def install_fixture() -> object:
    info = _docker("info", timeout=60)
    if info.returncode != 0:
        pytest.skip("docker unavailable: real-boundary install tests require it")
    fixture = InstallFixture()
    fixture.start()
    yield fixture
    print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture
def _clean_real_leases(migrated_url: str):
    """A killed worker's lease lives up to its TTL (1h default) in the
    disposable DB, which outlives one pytest invocation; the fixed real
    identities must start each run unfenced and unheld."""
    import psycopg

    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_resource_locks WHERE server_id = ANY(%s)",
            ([REAL_SERVER_A, REAL_SERVER_B, REAL_SERVER_C],),
        )
        conn.commit()
    yield


# -- real-boundary wiring -----------------------------------------------------


def test_compose_health_verdict_accepts_single_object_form() -> None:
    """Pure pin of the REAL-daemon shape discovered by the live fixture:
    compose emits ONE JSON object (not an array, not a stream) when the
    project has exactly one container."""
    healthy = '{"Name":"p-svc-1","State":"running","Health":"healthy"}\n'
    assert install_module._compose_health_verdict(healthy) is True
    starting = '{"Name":"p-svc-1","State":"running","Health":"starting"}\n'
    assert install_module._compose_health_verdict(starting) is False
    exited = '{"Name":"p-svc-1","State":"exited"}\n'
    assert install_module._compose_health_verdict(exited) is False
    # A bare non-container JSON value is still unparseable, never guessed.
    assert install_module._compose_health_verdict("null\n") is None


def _real_transport(fixture: InstallFixture):
    key_path, known_hosts = fixture.enroll()
    assert fixture.identity is not None
    return ssh_transport(
        fixture.identity, key_path=key_path, known_hosts_path=known_hosts
    )


def _real_service(
    *,
    transport,
    migrated_url: str,
    sessions,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[InstallService, ApprovalStore]:
    approvals = ApprovalStore(sessions)
    service = InstallService(
        transport=transport,
        locks=ResourceLockService(migrated_url),
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: _grants(server_id),
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(),
        lease_ttl=lease_ttl,
    )
    return service, approvals


def _real_request(
    recipe: ApplicationRecipe,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    approval_id: uuid.UUID,
) -> InstallRequest:
    return InstallRequest(
        server_id=server_id,
        application_id=application_id,
        recipe=recipe,
        target=TARGET,
        context=_context(server_id=server_id, credentials=tuple(recipe.credentials)),
        approval_id=approval_id,
        backup_confirmed=False,
    )


def _manifest_path(store: ApplicationManifestStore, sid: uuid.UUID, aid: uuid.UUID) -> Path:
    return store.root / "servers" / str(sid) / "applications" / str(aid) / "manifest.yaml"


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_happy_path_health_gates_and_canonical_commit(
    install_fixture, _clean_real_leases, migrated_url, sessions, file_store, http_recipe
) -> None:
    """(a) Real compose install: BOTH health gates pass (Docker health
    verdict + target-side loopback probe on the REALLY published port) and
    the canonical manifest/projection is committed through the real
    Postgres-backed adoption pipeline."""
    fixture: InstallFixture = install_fixture
    sid, aid = REAL_SERVER_A, REAL_APP_A
    plan = build_deployment_plan(http_recipe, sid, aid, TARGET)
    fixture.track(plan.project_name)
    service, approvals = _real_service(
        transport=_real_transport(fixture),
        migrated_url=migrated_url,
        sessions=sessions,
        store=file_store,
        server_id=sid,
    )
    approval = await approvals.create(_approval_request(plan.plan_sha256, sid))

    result = await service.install(
        _real_request(http_recipe, sid, aid, approval.approval_id)
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.reason is None
    assert result.health_verified is True
    assert result.published_ports == (REAL_PORT_A,)
    assert result.lock_released is True
    assert result.manifest_revision == 1
    assert [op.op for op in result.operations] == [
        "verify_prerequisites",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
        "wait_healthy",
    ]
    assert result.operations[-1].action == "verified"
    assert result.notes == ()  # no curl-absence note: the published-port gate RAN

    # One-use decision spent; real lease released.
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"
    locks = ResourceLockService(migrated_url)
    assert locks.inspect(str(sid), f"{LOCK_RESOURCE_PREFIX}{aid}") is None

    # Daemon truth: exactly one healthy container for the project.
    ids = _project_container_ids(plan.project_name)
    assert len(ids) == 1, f"expected one project container, saw {len(ids)}"
    assert _container_health(ids[0]) == "healthy"

    # Independent HOST-side proof the port is really published and answering
    # (the executor's own probe ran inside the fixture's host netns).
    with urllib.request.urlopen(
        f"http://127.0.0.1:{REAL_PORT_A}/health", timeout=10
    ) as response:
        assert response.status == 200

    # REAL compose bytes on the target match the plan hash (independent read).
    hash_out = _docker("exec", FIXTURE_CONTAINER, "sha256sum", "--", plan.compose_path)
    assert hash_out.returncode == 0
    assert hash_out.stdout.split()[:1] == [plan.compose_sha256]

    # REAL canonical manifest at the C02 path + REAL projection row.
    assert _manifest_path(file_store, sid, aid).is_file()
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(sid, aid)
    assert record is not None
    assert record.revision == 1
    assert record.compose_project == plan.project_name
    assert record.stale is False
    assert await adoption.list_live_intents(server_id=sid) == []


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_interrupted_apply_resumes_without_duplicate_create(
    install_fixture, _clean_real_leases, migrated_url, sessions, file_store, http_recipe
) -> None:
    """(b) The apply is REALLY interrupted (sshd killed mid-apply after
    the compose write, transport death ⇒ the worker dies like a killed
    process, no result object at all); the held lease lapses into a fence,
    a successor is refused until a host-verified reconciliation clears it,
    and the resumed install converges with EXACTLY ONE project create and
    one canonical record."""
    import asyncio

    from mai.execution.reconcile import ReconciliationClass

    fixture: InstallFixture = install_fixture
    sid, aid = REAL_SERVER_B, REAL_APP_B
    recipe = _mutant_recipe(
        HTTP_FILE,
        lambda d: d["deploy"]["ports"][0].__setitem__("host", REAL_PORT_B),
    )
    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    fixture.track(plan.project_name)
    transport = _real_transport(fixture)
    resource = f"{LOCK_RESOURCE_PREFIX}{aid}"

    approval = await ApprovalStore(sessions).create(
        _approval_request(plan.plan_sha256, sid)
    )

    # The worker runs on its OWN engine/loop: a killed worker holds state
    # the successor must reconcile, not share.
    raised: list[BaseException] = []
    results: list[object] = []

    async def _worker_main() -> None:
        engine, factory = create_app_engine(migrated_url)
        try:
            service, _ = _real_service(
                transport=transport,
                migrated_url=migrated_url,
                sessions=factory,
                store=file_store,
                server_id=sid,
                lease_ttl=3.0,
            )
            results.append(
                await service.install(
                    _real_request(recipe, sid, aid, approval.approval_id)
                )
            )
        finally:
            await engine.dispose()

    def _worker_entry() -> None:
        try:
            asyncio.run(_worker_main())
        except BaseException as exc:  # the killed worker's transport death
            raised.append(exc)

    thread = threading.Thread(target=_worker_entry, name="mai-install-worker")
    thread.start()
    deadline = time.monotonic() + 120.0
    while not fixture.compose_file_present(plan.compose_path):
        assert time.monotonic() < deadline, "apply never wrote the compose file"
        assert not raised, f"worker died before the write: {raised!r}"
        assert not results, f"worker completed before the write: {results!r}"
        time.sleep(0.02)
    # The compose bytes are on the target; the next op loses its transport
    # mid-flight (deterministic worker death BEFORE any start effect).
    assert fixture.kill_sshd() == 0
    thread.join(timeout=90.0)
    assert not thread.is_alive(), "worker survived the transport kill"
    assert raised, f"worker completed instead of dying: {results!r}"
    assert isinstance(raised[0], SshConnectError)

    # The one-use decision WAS spent (consumed before the effect) and the
    # lease is left held, exactly as a killed worker leaves it.
    spent = await ApprovalStore(sessions).get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"
    locks = ResourceLockService(migrated_url)
    assert locks.inspect(str(sid), resource) is not None

    before = set(_project_container_ids(plan.project_name))
    assert len(before) <= 1  # the kill landed before/at most one create

    # The killed worker stops heartbeating: the lease lapses into a fence
    # and a successor is REFUSED until reconciliation.
    await asyncio.sleep(3.6)
    second = await ApprovalStore(sessions).create(
        _approval_request(plan.plan_sha256, sid)
    )
    successor_service, _ = _real_service(
        transport=transport,
        migrated_url=migrated_url,
        sessions=sessions,
        store=file_store,
        server_id=sid,
    )
    fenced = await successor_service.install(
        _real_request(recipe, sid, aid, second.approval_id)
    )
    assert fenced.status == "refused"
    assert fenced.reason == "lock_unavailable:fenced"
    assert fenced.effect == "not_attempted"

    # Host-verified reconciliation: the prior worker is terminal; the
    # observed target state (0 or 1 containers) is recorded, the fence
    # clears, and the resume reconciles FROM the observed state.
    locks.clear_fence(
        str(sid),
        resource,
        verdict=ReconciliationClass.FINAL_CANCELLED,
        note="install worker died mid-apply (transport death); host observes "
        f"{len(before)} project container(s); resume reconciles from state",
    )
    fixture.restart_sshd()

    third = await ApprovalStore(sessions).create(
        _approval_request(plan.plan_sha256, sid)
    )
    resumed = await successor_service.install(
        _real_request(recipe, sid, aid, third.approval_id)
    )
    assert resumed.status == "succeeded", resumed.notes
    assert resumed.health_verified is True
    assert resumed.manifest_revision == 1

    # Single-create idempotence: across the killed attempt and the resume,
    # EXACTLY ONE container ever existed for the project.
    after = set(_project_container_ids(plan.project_name))
    assert len(before | after) == 1, f"create duplicated: {sorted(before | after)}"
    assert after, "resume must leave the project deployed"
    # A resume that rewrote compose would bypass verify-then-mutate: any
    # recorded write must be the VERIFIED (already-present) receipt.
    writes = [op for op in resumed.operations if op.op == "write_compose"]
    assert all(op.action == "verified" for op in writes)

    # One canonical record, no live intents, lease released.
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(sid, aid)
    assert record is not None and record.revision == 1
    assert record.compose_project == plan.project_name
    assert await adoption.list_live_intents(server_id=sid) == []
    assert locks.inspect(str(sid), resource) is None


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_health_failure_rolls_back_and_commits_nothing(
    install_fixture, _clean_real_leases, migrated_url, sessions, file_store, http_recipe
) -> None:
    """(c) Deterministic health failure (the rendered healthcheck probes
    a container port nothing serves: connection refused, forever): the
    executor rolls back with verified container removal, the compose
    project leaves NO daemon resources (containers + network), and no
    canonical manifest/projection is committed. The compose FILE is
    deliberately retained on the target: it is the plan-hash anchor the
    resume path verifies (verified-present), and directory deletion would
    be data deletion, which is unrepresentable here (C09 rule 5)."""
    fixture: InstallFixture = install_fixture
    sid, aid = REAL_SERVER_C, REAL_APP_C

    def _never_healthy(document: dict[str, object]) -> None:
        # Distinct host port so a concurrently-live deployment of another
        # test never trips the honest port pre-flight.
        document["deploy"]["ports"][0]["host"] = REAL_PORT_C
        # filebrowser is a SPA: it answers 200 even on garbage paths, so a
        # path mutation stays healthy. A healthcheck against a container
        # port nothing listens on fails DETERMINISTICALLY (connection
        # refused) while satisfying the schema (health port declared).
        document["deploy"]["ports"].append(
            {"host": REAL_PORT_C + 1, "container": 81, "protocol": "tcp"}
        )
        document["health"]["port"] = 81
        document["health"]["interval_seconds"] = 5
        document["health"]["retries"] = 1
        document["health"]["start_period_seconds"] = 0

    recipe = _mutant_recipe(HTTP_FILE, _never_healthy)
    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    fixture.track(plan.project_name)
    service, approvals = _real_service(
        transport=_real_transport(fixture),
        migrated_url=migrated_url,
        sessions=sessions,
        store=file_store,
        server_id=sid,
    )
    approval = await approvals.create(_approval_request(plan.plan_sha256, sid))

    result = await service.install(
        _real_request(recipe, sid, aid, approval.approval_id)
    )

    assert result.status == "rolled_back", (result.reason, result.notes)
    assert result.effect == "executed"  # the start effect really happened
    assert result.published_ports == (REAL_PORT_C, REAL_PORT_C + 1)
    assert result.health_verified is False
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert result.lock_released is True
    assert "rollback after failed operation: wait_healthy" in result.notes
    # The gate ran to its real budget AFTER a real start: no wait_healthy
    # receipt exists (it is only recorded on success).
    assert [op.op for op in result.operations] == [
        "verify_prerequisites",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
    ]
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"

    # Daemon truth: NO containers and NO compose network remain.
    assert _project_container_ids(plan.project_name) == []
    assert _project_network_ids(plan.project_name) == []

    # No canonical anything: no manifest file, no projection row, no
    # journal intents.
    assert not _manifest_path(file_store, sid, aid).exists()
    adoption = ApplicationAdoption(sessions)
    assert await adoption.read(sid, aid) is None
    assert await adoption.list_live_intents(server_id=sid) == []

    # The retained compose file still anchors the resume path (verified by
    # hash; the rollback removed the deployment, never the plan record).
    hash_out = _docker("exec", FIXTURE_CONTAINER, "sha256sum", "--", plan.compose_path)
    assert hash_out.returncode == 0
    assert hash_out.stdout.split()[:1] == [plan.compose_sha256]


# ---------------------------------------------------------------------------
# Value-free discipline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_secret_value_or_store_path_leaks_into_receipts() -> None:
    recipe = _credential_recipe()
    target = _FakeTarget()
    service, _ = _service(
        target, recipe, env_values={CREDENTIAL_NAME: SECRET_SENTINEL}
    )
    result = await service.install(_request(recipe))
    assert result.status == "succeeded"
    rendered = repr(result) + "".join(repr(op) for op in result.operations)
    assert SECRET_SENTINEL not in rendered
    for call in target.calls:
        assert SECRET_SENTINEL not in call.display
        if call.argv:
            assert SECRET_SENTINEL not in " ".join(call.argv)


def _approval_request(payload_hash: str, server_id: uuid.UUID):
    from mai.policy.approvals import ApprovalRequest

    return ApprovalRequest(
        capability_id=CAPABILITY_ID,
        server_id=server_id,
        payload_hash=payload_hash,
        policy_revision=POLICY_REVISION,
        created_by=PRINCIPAL,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
