"""Focused tests for application update and rollback (card 08-update).

Acceptance coverage (card mai-08-update):

- **AC1 (record prior/new version + data/recovery constraints, apply only
  the current approved plan)** — the executor VERIFIES the declared
  baseline against the deployed Compose bytes before any effect
  (``state_diverged`` on mismatch; ``not_installed`` on absence), records
  the verified prior digest and the requested new digest on the result
  together with the declared migration class and image-rollback support,
  takes a verified pre-update snapshot of every declared persistent-data
  location, and applies ONLY the rebuilt plan's frozen operation
  vocabulary, in plan order, under the full P03 gate (policy decision +
  one-use approval bound to the NEW plan hash) and the SAME exclusive
  ``application:<id>`` lease namespace install uses (P05).
- **AC2 (health regression visible with supported compensation or blocked
  recovery; no universal rollback claim)** — completion requires the
  Docker health verdict AND the target-side loopback probe; a health
  regression triggers the automatic rollback to the PRIOR pinned digest
  (baseline Compose restore + ``up -d`` + full baseline health gate) ONLY
  when the recipe declares ``recovery.image_rollback``; a recipe without
  image-rollback support gets honest blocked recovery (failure surfaced,
  candidate left visible, nothing restored, no rollback claim); an
  interrupted apply is ``outcome_unknown`` and NEVER restores (the prior
  process may live); irreversible migrations keep the data-rollback
  limitation on every receipt. Only fully verified success updates the
  canonical manifest/projection — a failed or rolled-back update commits
  nothing.

Test posture (mirrors test_install.py): the target boundary is the same
faithful in-memory simulator, extended with the ``tar`` snapshot and
``test -s`` verification commands update.py emits; the approval/lock/
adoption seams run BOTH as semantic stubs (pure tests) and as the REAL
Postgres-backed stores (live-DB section, disposable fixture). The
catalogue fixtures are the frozen 08-fixture-recipes revision streams
(example-http, example-stateful — each a v1/v2 digest stream). The
``real_target``-marked section drives the executor over the REAL
transport: a disposable Arch sshd container mounting the host docker
socket (``mai-fxt-08-upd-*`` namespace, mirroring the 08-install
fixture), executing the real ``docker compose`` CLI against the host
daemon — (a) a LIVE v1→v2 update of example-stateful preserving a
planted sentinel through the digest change, (b) an injected unhealthy
candidate that rolls back to the prior pinned digest with the sentinel
intact and no canonical commit, and (c) blocked recovery on a recipe
that declares no image rollback.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mai.applications.update as update_module
from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    AdoptionResult,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.recipes import (
    ApplicationRecipe,
    TargetEnvironment,
    build_deployment_plan,
    parse_recipe_text,
    project_name,
)
from mai.applications.update import (
    CAPABILITY_ID,
    DEFAULT_LEASE_TTL_SECONDS,
    LOCK_RESOURCE_PREFIX,
    UpdateError,
    UpdateErrorCode,
    UpdateRequest,
    UpdateService,
    update_intent_id,
)
from mai.credentials.env import EnvResolver
from mai.execution.commands import CommandRequest, CommandStatus
from mai.execution.ssh import (
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

HTTP_FILE = "example-http.yaml"
STATEFUL_FILE = "example-stateful.yaml"

TARGET = TargetEnvironment(os="linux", arch="x86_64")

SERVER_ID = uuid.UUID("08090809-0809-4809-8809-080908090809")
APP_ID = uuid.UUID("0c0d0c0d-0c0d-4c0d-8c0d-0c0d0c0d0c0d")
OTHER_SERVER_ID = uuid.UUID("09090909-0909-4909-8909-090909090909")
RUN_ID = uuid.UUID("07070707-0707-4707-8707-070707070707")

#: Live-DB section identities (distinct from install's; the disposable DB
#: is re-migrated per session but the lock table outlives one run).
LIVE_SERVER_ID = uuid.UUID("08090809-0809-4809-8809-0809080900e1")
LIVE_APP_ID = uuid.UUID("0c0d0c0d-0c0d-4c0d-8c0d-0c0d0c0d00e1")

POLICY_REVISION = 1
PRINCIPAL = "admin-test"

#: Synthetic non-disclosure marker (NOT a secret): planted as a credential
#: VALUE in the .env fixture; no receipt, note, error or display may carry it.
SECRET_SENTINEL = "SENTINEL-mai-08-update-not-a-secret"
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


def _mutant_revision(filename: str, revision: int, mutate) -> ApplicationRecipe:
    """Mutate one revision of a stream (e.g. build a v2-derived candidate)."""
    document = yaml.safe_load(_document_texts(filename)[revision])
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
    capability: str = CAPABILITY_ID,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_ID,
        parent_scope=None,
        profile_revision="profile-r1",
        prompt_revision="prompt-r1",
        credential_references=credentials,
        allowed_capabilities=(capability,),
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
    current_recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    backup_confirmed: bool = False,
    context: ExecutionContext | None = None,
    application_id: uuid.UUID = APP_ID,
) -> UpdateRequest:
    return UpdateRequest(
        server_id=context.server_id if context else SERVER_ID,
        application_id=application_id,
        recipe=recipe,
        current_recipe=current_recipe,
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
            4 if self.existing_compose_project else 2,
            "cd" * 32,
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
# The target simulator: the exact command surface update.py drives
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"umask 077 && mkdir -p (?P<dir>\S+) && "
    r"printf %s '(?P<b64>[A-Za-z0-9+/=]+)' \| base64 -d > "
    r"(?P<path>\S+)\.tmp && mv \S+\.tmp (?P=path) && sha256sum -- (?P=path)"
)


class _FakeTarget:
    """In-memory target executing exactly the commands update.py emits.

    Same fidelity contract as the install simulator: file writes go
    through the real base64 shell channel (decoded and stored
    byte-exact), hashes are recomputed from stored bytes, the Compose
    project state is a first-class mutable (running/healthy with a
    per-up health FIFO for update/rollback sequences), snapshots are
    recorded as ``tar`` receipts, and scripted failures can turn any op
    into ``failed`` (rc 1) or ``outcome_unknown`` (timeout cut).
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.listening: set[int] = set()
        self.volume_data: dict[str, tuple[str, ...]] = {}
        self.project: dict[str, bool] | None = None
        self.healthy_after_up = True
        #: Per-up health outcomes consumed FIFO (update then rollback).
        self.up_health: list[bool] = []
        self.curl_present = True
        #: When set, the published-port probe answers this status code
        #: regardless of container health (independent fault injection).
        self.curl_status: str | None = None
        self.up_calls = 0
        self.tars: list[tuple[str, str, tuple[str, ...]]] = []
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
        if argv[:1] == ("test",):
            path = argv[-1]
            data = self.files.get(path)
            if data:
                return _text(request, "")
            return _outcome(request, CommandStatus.FAILED, 1, b"", b"no file\n")
        if argv[:1] == ("tar",):
            path = argv[argv.index("--file") + 1]
            directory = argv[argv.index("--directory") + 1]
            names = tuple(argv[argv.index("--directory") + 2:])
            self.tars.append((path, directory, names))
            self.files[path] = f"TAR {directory} {' '.join(names)}".encode()
            return _text(request, "")
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
            healthy = (
                self.up_health.pop(0) if self.up_health else self.healthy_after_up
            )
            self.project = {"running": True, "healthy": healthy}
            return _text(request, "")
        if op == "down":
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


def _seed_deployed(
    target: _FakeTarget,
    current_recipe: ApplicationRecipe,
    application_id: uuid.UUID = APP_ID,
    *,
    data: bool = False,
) -> None:
    """Make the target look like a healthy deployment of current_recipe."""
    baseline = build_deployment_plan(current_recipe, SERVER_ID, application_id, TARGET)
    target.files[baseline.compose_path] = baseline.compose_text.encode("utf-8")
    target.project = {"running": True, "healthy": True}
    if data:
        for volume in baseline.volumes:
            target.volume_data[volume.host_path] = ("sentinel.txt",)


def _service(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    current_recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    application_id: uuid.UUID = APP_ID,
    locks: ResourceLockService | None = None,
    approvals: ApprovalStore | None = None,
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
    plan = _plan_for(recipe, application_id)
    if approvals is None:
        approvals = _StubApprovals(
            approval_id or _DEFAULT_APPROVAL_ID, payload_hash=plan.plan_sha256
        )
    if credentials is None:
        resolver_tmp = _env_file(env_values if env_values is not None else {})
        credentials = lambda: EnvResolver(resolver_tmp)  # noqa: E731
    service = UpdateService(
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
    )
    return service, approvals


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

    monkeypatch.setattr(update_module, "monotonic", lambda: state["t"])

    def advance(seconds: float) -> None:
        state["t"] += seconds

    monkeypatch.setattr(update_module, "sleep", advance)
    return state


@pytest.fixture
def http_v1() -> ApplicationRecipe:
    return _recipe(HTTP_FILE, 0)


@pytest.fixture
def http_v2() -> ApplicationRecipe:
    return _recipe(HTTP_FILE, 1)


@pytest.fixture
def stateful_v1() -> ApplicationRecipe:
    return _recipe(STATEFUL_FILE, 0)


@pytest.fixture
def stateful_v2() -> ApplicationRecipe:
    return _recipe(STATEFUL_FILE, 1)


# ---------------------------------------------------------------------------
# AC1: baseline verification, plan-bound apply, identity guards
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_happy_update_records_versions_snapshot_and_canonical_commit(
    stateful_v1, stateful_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, data=True)
    service, approvals = _service(target, stateful_v2, stateful_v1)

    result = await service.update(_request(stateful_v2, stateful_v1,
                                           backup_confirmed=True))

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.reason is None
    assert result.health_verified is True
    assert result.prior_image == stateful_v1.image
    assert result.new_image == stateful_v2.image
    assert result.prior_healthy is True
    assert result.migrations == "none"
    assert result.image_rollback_supported is True
    assert result.manifest_revision == 2
    assert result.lock_released is True
    assert result.snapshot_paths == (
        update_module._snapshot_path(_plan_for(stateful_v2)),
    )
    assert target.tars and target.tars[0][0] == result.snapshot_paths[0]
    assert "baseline deployment verified healthy (prior version)" in result.notes
    assert any(n.startswith("pre-update snapshot recorded:") for n in result.notes)
    # Receipt order: prerequisite probe, snapshot (executor-owned), then the
    # plan operations in plan order, ending with the verified health gate.
    assert [op.op for op in result.operations] == [
        "verify_prerequisites",
        "pre_update_snapshot",
        "require_backup",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
        "wait_healthy",
    ]
    assert result.operations[-1].action == "verified"
    assert result.operations[1].op == "pre_update_snapshot"
    # One-use decision spent exactly once.
    assert approvals.consume_calls == 1


@pytest.mark.anyio
async def test_stateless_update_has_no_snapshot_and_applies_plan_in_order(
    http_v1, http_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(target, http_v2, http_v1)

    result = await service.update(_request(http_v2, http_v1))

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.snapshot_paths == ()  # no declared volumes, no snapshot
    assert [op.op for op in result.operations] == [
        "verify_prerequisites",
        "create_directories",
        "write_compose",
        "pull_image",
        "start",
        "wait_healthy",
    ]
    write = next(op for op in result.operations if op.op == "write_compose")
    assert write.action == "applied"  # the digest change really was written


@pytest.mark.anyio
async def test_same_digest_request_converges_without_rewrites(http_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    adoption = _StubAdoption()
    service, _ = _service(target, http_v1, http_v1, adoption=adoption)

    result = await service.update(_request(http_v1, http_v1))

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.prior_image == result.new_image == http_v1.image
    write = next(op for op in result.operations if op.op == "write_compose")
    assert write.action == "verified"  # bytes already on the target
    # Deterministic intent: the canonical update is deduplicated.
    (_, intent) = adoption.adopt_calls[0]
    assert intent == update_intent_id(SERVER_ID, APP_ID, _plan_for(http_v1).plan_sha256)


@pytest.mark.anyio
async def test_policy_denial_causes_no_effect_and_spends_nothing(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, approvals = _service(target, http_v2, http_v1, grants=lambda: {})
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == "policy_denied:capability_not_granted"
    assert approvals.consume_calls == 0
    assert target.project == {"running": True, "healthy": True}  # untouched
    baseline = _plan_for(http_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()


@pytest.mark.anyio
async def test_server_scope_mismatch_refuses(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(target, http_v2, http_v1)
    context = _context(server_id=OTHER_SERVER_ID)
    result = await service.update(_request(http_v2, http_v1, context=context))
    assert result.status == "refused"
    assert result.reason == "policy_denied:server_scope_mismatch"
    assert result.effect == "not_attempted"


@pytest.mark.anyio
async def test_unenrolled_server_refused(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(target, http_v2, http_v1, adopted=lambda server_id: False)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "policy_denied:server_not_adopted"


def test_context_target_mismatch_is_a_construction_rejection(http_v1, http_v2) -> None:
    with pytest.raises(UpdateError) as error:
        UpdateRequest(
            server_id=SERVER_ID,  # request target...
            application_id=APP_ID,
            recipe=http_v2,
            current_recipe=http_v1,
            target=TARGET,
            context=_context(server_id=OTHER_SERVER_ID),  # ...vs run binding
            approval_id=_DEFAULT_APPROVAL_ID,
        )
    assert error.value.code is UpdateErrorCode.INVALID_REQUEST


def test_request_type_checks_fail_closed(http_v1, http_v2) -> None:
    def _build(**overrides: object) -> None:
        fields: dict[str, object] = {
            "server_id": SERVER_ID,
            "application_id": APP_ID,
            "recipe": http_v2,
            "current_recipe": http_v1,
            "target": TARGET,
            "context": _context(),
            "approval_id": _DEFAULT_APPROVAL_ID,
        }
        fields.update(overrides)
        UpdateRequest(**fields)  # type: ignore[arg-type]

    with pytest.raises(UpdateError):
        _build(recipe="not-a-recipe")
    with pytest.raises(UpdateError):
        _build(current_recipe="not-a-recipe")
    with pytest.raises(UpdateError):
        _build(target="not-a-target")
    with pytest.raises(UpdateError):
        _build(approval_id="not-a-uuid")


@pytest.mark.anyio
async def test_wiring_checks_fail_closed(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    with pytest.raises(UpdateError):
        UpdateService(  # wrong capability identifier
            transport=target,
            locks=_StubLocks(),
            approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="0" * 64),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(identifier="app.install"),
        )
    with pytest.raises(UpdateError):
        UpdateService(  # not declared privileged
            transport=target,
            locks=_StubLocks(),
            approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="0" * 64),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(effect_class="read"),
        )
    with pytest.raises(UpdateError):
        UpdateService(  # transport must be callable
            transport="not-callable",  # type: ignore[arg-type]
            locks=_StubLocks(),
            approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="0" * 64),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file({})),
            descriptor=_descriptor(),
        )


@pytest.mark.anyio
async def test_altered_plan_hash_denies_the_approval_and_has_no_effect(
    http_v1, http_v2,
) -> None:
    # The approval was minted for a DIFFERENT plan hash (the recipe changed
    # after the decision): consumption must refuse with payload_mismatch and
    # the target must be untouched.
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    other_hash = "e" * 64
    approvals = _StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash=other_hash)
    service, _ = _service(target, http_v2, http_v1, approvals=approvals)

    result = await service.update(_request(http_v2, http_v1))

    assert result.status == "refused"
    assert result.reason == "approval_rejected:payload_mismatch"
    assert result.effect == "not_attempted"
    assert approvals.consume_calls == 1  # the (refused) consumption attempt
    baseline = _plan_for(http_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()
    assert target.project == {"running": True, "healthy": True}


@pytest.mark.anyio
async def test_cross_server_identity_conflict_refuses(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(
        target, http_v2, http_v1,
        store=_StubStore(foreign_servers=(OTHER_SERVER_ID,)),
    )
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "cross_server_conflict"


@pytest.mark.anyio
async def test_external_record_conflict_refuses(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(
        target, http_v2, http_v1,
        adoption=_StubAdoption(existing_compose_project="someone-else"),
    )
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "external_record_conflict"


@pytest.mark.anyio
async def test_unsupported_prerequisite_refuses(http_v1) -> None:
    recipe = _mutant_recipe(
        HTTP_FILE, lambda d: d.update(prerequisites=["puppet"])
    )
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(target, recipe, http_v1)
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "refused"
    assert result.reason == "unsupported_prerequisite"


@pytest.mark.anyio
async def test_undeclared_health_refuses_before_any_effect(http_v1, http_v2) -> None:
    recipe = _mutant_recipe(HTTP_FILE, lambda d: d.pop("health"))
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, approvals = _service(target, recipe, http_v1)
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "refused"
    assert result.reason == "health_not_declared"
    assert approvals.consume_calls == 0


@pytest.mark.anyio
async def test_undeclared_baseline_health_refuses(http_v1) -> None:
    # The BASELINE recipe declares no health check: the rollback gate could
    # never be verified, so the update refuses before any effect.
    baseline = _mutant_recipe(HTTP_FILE, lambda d: d.pop("health"))
    target = _FakeTarget()
    _seed_deployed(target, baseline)
    service, _ = _service(target, http_v1, baseline)
    result = await service.update(_request(http_v1, baseline))
    assert result.status == "refused"
    assert result.reason == "health_not_declared"


# ---------------------------------------------------------------------------
# AC1: baseline verification is load-bearing (prior version observed)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_not_installed_refuses_when_no_compose_document(http_v1, http_v2) -> None:
    target = _FakeTarget()  # nothing deployed
    service, approvals = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "not_installed"
    assert approvals.consume_calls == 0
    assert result.prior_image is None  # never recorded: never verified


@pytest.mark.anyio
async def test_diverged_compose_refuses(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    baseline = _plan_for(http_v1)
    target.files[baseline.compose_path] = b"someone edited this"
    service, approvals = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "state_diverged"
    assert any("matches neither" in note for note in result.notes)
    assert approvals.consume_calls == 0
    assert target.project == {"running": True, "healthy": True}  # untouched


@pytest.mark.anyio
async def test_compose_already_matching_requested_plan_requires_reconciliation(
    http_v1, http_v2,
) -> None:
    # An interrupted earlier attempt left the NEW bytes deployed: the prior
    # version can no longer be verified from here. The executor must refuse
    # (explicit reconciliation), never silently converge an unrecorded
    # prior version.
    target = _FakeTarget()
    new_plan = _plan_for(http_v2)
    target.files[new_plan.compose_path] = new_plan.compose_text.encode("utf-8")
    target.project = {"running": True, "healthy": True}
    service, approvals = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "state_diverged"
    assert any("requested update plan" in note for note in result.notes)
    assert approvals.consume_calls == 0
    assert result.prior_image is None


@pytest.mark.anyio
async def test_stopped_baseline_refuses_as_not_installed(http_v1, http_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.project = None  # compose present but project down
    service, _ = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "not_installed"
    assert any("not running" in note for note in result.notes)


@pytest.mark.anyio
async def test_prior_unhealthy_blocks_update(http_v1, http_v2) -> None:
    # A health regression is only attributable against a verified healthy
    # prior state: updating an UNHEALTHY deployment is blocked.
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.project = {"running": True, "healthy": False}
    service, approvals = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "refused"
    assert result.reason == "prior_state_unhealthy"
    assert approvals.consume_calls == 0
    baseline = _plan_for(http_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()


@pytest.mark.anyio
async def test_backup_gate_demands_confirmation_for_existing_data(
    stateful_v1, stateful_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, data=True)
    service, approvals = _service(target, stateful_v2, stateful_v1)
    result = await service.update(
        _request(stateful_v2, stateful_v1, backup_confirmed=False)
    )
    assert result.status == "refused"
    assert result.reason == "backup_required"
    assert result.effect == "not_attempted"
    assert approvals.consume_calls == 0
    assert result.snapshot_paths == ()  # refused before any snapshot


# ---------------------------------------------------------------------------
# AC2: snapshot, verified rollback, blocked recovery, honest unknowns
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_snapshot_failure_fails_before_any_mutation(
    stateful_v1, stateful_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, data=True)
    target.fail["snapshot persistent data"] = "failed"
    service, approvals = _service(target, stateful_v2, stateful_v1)
    result = await service.update(
        _request(stateful_v2, stateful_v1, backup_confirmed=True)
    )
    assert result.status == "failed"
    assert result.effect == "not_attempted"  # no start-class effect happened
    assert result.reason == "apply_failed"
    assert result.rollback_attempted is False  # nothing to roll back
    assert result.snapshot_paths == ()
    assert result.lock_released is True
    assert approvals.consume_calls == 1  # consumed before the failed step
    baseline = _plan_for(stateful_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()
    assert target.project == {"running": True, "healthy": True}  # untouched


@pytest.mark.anyio
async def test_unknown_snapshot_keeps_the_lease(stateful_v1, stateful_v2) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, data=True)
    target.fail["snapshot persistent data"] = "unknown"
    service, _ = _service(target, stateful_v2, stateful_v1)
    result = await service.update(
        _request(stateful_v2, stateful_v1, backup_confirmed=True)
    )
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False  # lapse into a fence
    assert result.rollback_attempted is False


@pytest.mark.anyio
async def test_pull_failure_restores_the_baseline_compose(http_v1, http_v2) -> None:
    # The new compose bytes were written, then the pull failed: the
    # executor restores the VERIFIED baseline (same bytes that were
    # confirmed pre-update) and re-verifies it healthy.
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.fail["pull pinned application image"] = "failed"
    adoption = _StubAdoption()
    service, _ = _service(target, http_v2, http_v1, adoption=adoption)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "rolled_back", (result.reason, result.notes)
    assert result.effect == "executed"  # the compose write really happened
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert result.health_verified is False
    assert result.prior_image == http_v1.image
    assert any("rolling back to the prior pinned digest" in n for n in result.notes)
    baseline = _plan_for(http_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()
    assert target.project == {"running": True, "healthy": True}
    assert adoption.adopt_calls == []  # failed update commits nothing


@pytest.mark.anyio
async def test_health_regression_rolls_back_to_the_prior_digest(
    http_v1, http_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.up_health = [False, True]  # candidate unhealthy, restore healthy
    adoption = _StubAdoption()
    service, approvals = _service(target, http_v2, http_v1, adoption=adoption)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "rolled_back", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert result.health_verified is False
    assert result.prior_image == http_v1.image
    assert result.new_image == http_v2.image
    assert "health regression after update" in result.notes
    assert any("rolling back to the prior pinned digest" in n for n in result.notes)
    baseline = _plan_for(http_v1)
    assert target.files[baseline.compose_path] == baseline.compose_text.encode()
    assert target.project == {"running": True, "healthy": True}
    assert adoption.adopt_calls == []
    spent = approvals.consume_calls
    assert spent == 1


@pytest.mark.anyio
async def test_blocked_recovery_when_recipe_declares_no_image_rollback(
    http_v1, http_v2,
) -> None:
    recipe = _mutant_revision(
        HTTP_FILE, 1, lambda d: d["recovery"].__setitem__("image_rollback", False)
    )
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.up_health = [False]  # candidate unhealthy, NO restore follows
    adoption = _StubAdoption()
    service, _ = _service(target, recipe, http_v1, adoption=adoption)
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "failed"
    assert result.effect == "executed"  # the start effect really happened
    assert result.rollback_attempted is False  # declared limit: no rollback
    assert result.rollback_verified is False
    assert result.health_verified is False
    assert result.image_rollback_supported is False
    assert any("recovery: no image rollback" in n for n in result.notes)
    new_plan = _plan_for(recipe)
    assert target.files[new_plan.compose_path] == new_plan.compose_text.encode()
    assert target.project == {"running": True, "healthy": False}  # left visible
    assert adoption.adopt_calls == []


@pytest.mark.anyio
async def test_irreversible_migrations_keep_the_data_rollback_limitation(
    http_v1, http_v2,
) -> None:
    recipe = _mutant_revision(
        HTTP_FILE, 1, lambda d: d["update"].__setitem__("migrations", "irreversible")
    )
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.up_health = [False, True]
    service, _ = _service(target, recipe, http_v1)
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "rolled_back"
    assert result.migrations == "irreversible"
    assert result.prior_image == http_v1.image
    assert result.new_image == recipe.image
    assert any("data rollback is not possible" in n for n in result.notes)


@pytest.mark.anyio
async def test_unknown_outcome_keeps_the_lease_and_never_restores(
    http_v1, http_v2,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.fail["start managed compose project"] = "unknown"
    service, _ = _service(target, http_v2, http_v1)
    result = await service.update(_request(http_v2, http_v1))
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.lock_released is False  # the lease lapses into a fence
    assert result.rollback_attempted is False  # NEVER restore a maybe-live
    new_plan = _plan_for(http_v2)
    assert target.files[new_plan.compose_path] == new_plan.compose_text.encode()


@pytest.mark.anyio
async def test_lease_lapse_aborts_the_apply(http_v1, http_v2) -> None:
    import asyncio

    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    target.block_on = "pull pinned application image"
    locks = _StubLocks(lapse_heartbeats=True)
    service, _ = _service(target, http_v2, http_v1, locks=locks, lease_ttl=3.0)

    task = asyncio.ensure_future(service.update(_request(http_v2, http_v1)))
    deadline = time.monotonic() + 10.0
    while locks.heartbeat_calls == 0:
        assert time.monotonic() < deadline, "heartbeat never fired"
        await asyncio.sleep(0.02)
    target.unblock()
    result = await task

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert any("lease lapsed" in n for n in result.notes)
    assert locks.released == []  # left to lapse into a fence


# ---------------------------------------------------------------------------
# Value-free discipline
# ---------------------------------------------------------------------------


def _credential_recipe() -> ApplicationRecipe:
    return _mutant_recipe(
        HTTP_FILE,
        lambda d: d["deploy"].update(credentials=[CREDENTIAL_NAME]),
    )


@pytest.mark.anyio
async def test_missing_credential_refuses_before_spending_approval(http_v1) -> None:
    recipe = _credential_recipe()
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, approvals = _service(target, recipe, http_v1, env_values={})
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "refused"
    assert result.reason == "credential_unavailable"
    assert approvals.consume_calls == 0


@pytest.mark.anyio
async def test_no_secret_value_or_store_path_leaks_into_receipts(http_v1) -> None:
    recipe = _credential_recipe()
    target = _FakeTarget()
    _seed_deployed(target, http_v1)
    service, _ = _service(
        target, recipe, http_v1, env_values={CREDENTIAL_NAME: SECRET_SENTINEL}
    )
    result = await service.update(_request(recipe, http_v1))
    assert result.status == "succeeded"
    rendered = repr(result) + "".join(repr(op) for op in result.operations)
    assert SECRET_SENTINEL not in rendered
    for call in target.calls:
        assert SECRET_SENTINEL not in call.display
        if call.argv:
            assert SECRET_SENTINEL not in " ".join(call.argv)


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
            f"{LIVE_URL_VAR} is not set; the update integration claims need "
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
def locks_service(migrated_url: str) -> ResourceLockService:
    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    return locks


@pytest.fixture
def _clean_live_leases(migrated_url: str):
    if psycopg is None:  # pragma: no cover - migrated_url already skipped
        pytest.skip("psycopg unavailable")
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_resource_locks WHERE server_id = ANY(%s)",
            ([LIVE_SERVER_ID],),
        )
        conn.commit()
    yield


def _live_update_service(
    target: _FakeTarget, migrated_url: str, sessions, store, locks
) -> tuple[UpdateService, ApprovalStore]:
    approvals = ApprovalStore(sessions)
    service = UpdateService(
        transport=target,
        locks=locks,
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: _grants(LIVE_SERVER_ID),
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(),
    )
    return service, approvals


async def _seed_canonical_record(sessions, store, recipe: ApplicationRecipe) -> None:
    """Revision 1 baseline through the REAL adoption pipeline (what a
    completed install leaves behind). Idempotent: the disposable DB is
    re-migrated once per session, so records persist across tests."""
    adoption = ApplicationAdoption(sessions)
    if await adoption.read(LIVE_SERVER_ID, LIVE_APP_ID) is not None:
        return
    await adoption.adopt(
        store,
        AdoptionRequest(
            server_id=LIVE_SERVER_ID,
            application_id=LIVE_APP_ID,
            name=recipe.name,
            origin=ApplicationOrigin.COMPOSE,
            compose_project=project_name(LIVE_APP_ID),
            working_dir=f"/var/lib/mai/apps/{LIVE_APP_ID}",
            expected_revision=0,
        ),
        intent_id=uuid.uuid4(),
    )


@pytest.mark.anyio
async def test_live_update_bumps_canonical_revision_with_real_stores(
    stateful_v1, stateful_v2, migrated_url, sessions, file_store, locks_service,
    _clean_live_leases,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, application_id=LIVE_APP_ID, data=True)
    await _seed_canonical_record(sessions, file_store, stateful_v1)
    service, approvals = _live_update_service(
        target, migrated_url, sessions, file_store, locks_service
    )
    plan = build_deployment_plan(stateful_v2, LIVE_SERVER_ID, LIVE_APP_ID, TARGET)
    approval = await approvals.create(
        _approval_request(plan.plan_sha256, LIVE_SERVER_ID)
    )

    result = await service.update(
        _request(
            stateful_v2, stateful_v1,
            application_id=LIVE_APP_ID,
            context=_context(server_id=LIVE_SERVER_ID),
            approval_id=approval.approval_id,
            backup_confirmed=True,
        )
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.prior_image == stateful_v1.image
    assert result.new_image == stateful_v2.image
    assert result.manifest_revision == 2
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(LIVE_SERVER_ID, LIVE_APP_ID)
    assert record is not None and record.revision == 2
    assert record.compose_project == plan.project_name
    assert record.stale is False
    assert await adoption.list_live_intents(server_id=LIVE_SERVER_ID) == []
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"
    assert locks_service.inspect(
        str(LIVE_SERVER_ID), f"{LOCK_RESOURCE_PREFIX}{LIVE_APP_ID}"
    ) is None


@pytest.mark.anyio
async def test_live_rolled_back_update_commits_nothing(
    stateful_v1, stateful_v2, migrated_url, sessions, file_store, locks_service,
    _clean_live_leases,
) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, application_id=LIVE_APP_ID, data=True)
    await _seed_canonical_record(sessions, file_store, stateful_v1)
    adoption_probe = ApplicationAdoption(sessions)
    revision_before = (await adoption_probe.read(LIVE_SERVER_ID, LIVE_APP_ID)).revision
    target.up_health = [False, True]  # candidate unhealthy, restore healthy
    service, approvals = _live_update_service(
        target, migrated_url, sessions, file_store, locks_service
    )
    plan = build_deployment_plan(stateful_v2, LIVE_SERVER_ID, LIVE_APP_ID, TARGET)
    approval = await approvals.create(
        _approval_request(plan.plan_sha256, LIVE_SERVER_ID)
    )

    result = await service.update(
        _request(
            stateful_v2, stateful_v1,
            application_id=LIVE_APP_ID,
            context=_context(server_id=LIVE_SERVER_ID),
            approval_id=approval.approval_id,
            backup_confirmed=True,
        )
    )

    assert result.status == "rolled_back", (result.reason, result.notes)
    assert result.rollback_verified is True
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(LIVE_SERVER_ID, LIVE_APP_ID)
    assert record is not None and record.revision == revision_before  # unchanged
    assert await adoption.list_live_intents(server_id=LIVE_SERVER_ID) == []
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


# ---------------------------------------------------------------------------
# Real-boundary section: disposable Arch sshd + docker-socket fixture
# (``mai-fxt-08-upd-*`` namespace, mirroring the 08-install fixture).
# The executor drives the REAL ``docker compose`` CLI on the HOST daemon
# through the REAL P04 SSH transport, and the canonical manifest/projection
# commits go through the REAL Postgres stores above. Skipped without a
# working docker daemon; the DB fixtures skip themselves without
# MAI_TEST_DATABASE_URL.
#
# Topology note (recorded honestly): this workstation's docker daemon lives
# in a foreign mount namespace, so the fixture-side volume paths (where the
# executor's SSH snapshot lands) are NOT the daemon-side bind sources (where
# container data lands). The sentinel therefore travels through
# daemon-mediated ``docker cp``; the snapshot op is proven as an executed,
# verified bounded command on the target, its byte-level capture of
# daemon-side data is a recorded fixture limitation (production topology:
# SSH target == docker host, same filesystem).
#
# Teardown contract: containers, compose projects (containers + networks,
# including leftovers of a crashed predecessor run under the same fixed
# identities), the scratch dirs AND the fixture image are removed, with
# the proof string printed to the record.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-08-upd-sshd"
FIXTURE_IMAGE_TAG = "mai-fxt-08-upd-sshd:latest"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-08-upd")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_08_UPD_FIXTURE"

#: Fixed identity pairs, one per real test, so target dirs, compose
#: projects, locks and canonical records never collide across tests (and
#: never with the 08-install fixture identities).
REAL_SERVER_D = uuid.UUID("08080808-0808-4808-8808-0808080800d4")
REAL_SERVER_E = uuid.UUID("08080808-0808-4808-8808-0808080800e5")
REAL_SERVER_F = uuid.UUID("08080808-0808-4808-8808-0808080800f6")
REAL_APP_D = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00d4")
REAL_APP_E = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00e5")
REAL_APP_F = uuid.UUID("0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c00f6")

#: Published host ports: distinct per real test so concurrently-live
#: deployments never trip the honest port checks or collide.
REAL_PORT_D = 18083
REAL_PORT_E = 18084
REAL_PORT_F = 18086

SENTINEL_NAME = "mai-fixture-sentinel.txt"


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


def _container_image(container_id: str) -> str:
    out = _docker("inspect", "--format", "{{.Image}}", container_id)
    assert out.returncode == 0, f"docker inspect failed: {out.stderr[-400:]}"
    return out.stdout.strip()


def _image_id(ref: str) -> str:
    out = _docker("image", "inspect", "--format", "{{.Id}}", ref)
    assert out.returncode == 0, f"image inspect failed for the pinned ref: {out.stderr[-400:]}"
    return out.stdout.strip()


class UpdateFixture:
    """Arch sshd + docker-CLI container on the HOST network namespace
    (the 08-install fixture shape, under the mai-fxt-08-upd-* namespace)."""

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
            "RUN pacman-key --init "
            "&& pacman -Sy --noconfirm openssh docker docker-compose curl "
            "iproute2 tar && ssh-keygen -A "
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
        for app_id in (REAL_APP_D, REAL_APP_E, REAL_APP_F):
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

    def track(self, project: str) -> None:
        self._projects.add(project)

    def exec_sh(self, script: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        """Run one shell command INSIDE the fixture (SSH-target view)."""
        return _docker("exec", FIXTURE_CONTAINER, "sh", "-c", script, timeout=timeout)

    def file_present(self, path: str) -> bool:
        out = self.exec_sh(f"test -f {path}")
        assert out.returncode in (0, 1), f"exec probe failed: {out.stderr[-300:]}"
        return out.returncode == 0

    def file_nonempty(self, path: str) -> bool:
        out = self.exec_sh(f"test -s {path}")
        assert out.returncode in (0, 1), f"exec probe failed: {out.stderr[-300:]}"
        return out.returncode == 0

    def compose_file_present(self, compose_path: str) -> bool:
        return self.file_present(compose_path)

    def cp_into_container(self, container_id: str, container_path: str,
                          source: Path) -> None:
        out = _docker("cp", str(source), f"{container_id}:{container_path}", timeout=60)
        assert out.returncode == 0, f"docker cp in failed: {out.stderr[-400:]}"

    def cp_out_of_container(self, container_id: str, container_path: str,
                            target: Path) -> None:
        out = _docker("cp", f"{container_id}:{container_path}", str(target), timeout=60)
        assert out.returncode == 0, f"docker cp out failed: {out.stderr[-400:]}"

    def plant_sentinel(self, container_id: str, payload: str) -> None:
        source = self.scratch / SENTINEL_NAME
        source.write_text(payload, encoding="utf-8")
        self.cp_into_container(container_id, f"/srv/{SENTINEL_NAME}", source)

    def read_sentinel(self, container_id: str) -> str:
        target = self.scratch / f"sentinel-out-{uuid.uuid4().hex}"
        self.cp_out_of_container(container_id, f"/srv/{SENTINEL_NAME}", target)
        return target.read_text(encoding="utf-8")

    def cleanup(self) -> str:
        parts: list[str] = []
        projects = self._projects | {
            project_name(a) for a in (REAL_APP_D, REAL_APP_E, REAL_APP_F)
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
        if os.environ.get("MAI_KEEP_08_UPD_FIXTURE_IMAGE"):
            parts.append("fixture image KEPT (MAI_KEEP_08_UPD_FIXTURE_IMAGE set)")
        else:
            rmi = _docker("rmi", "-f", FIXTURE_IMAGE_TAG, timeout=120)
            parts.append(f"fixture image rmi rc={rmi.returncode}")
        self.cleanup_proof = "; ".join(parts)
        return self.cleanup_proof


@pytest.fixture(scope="module")
def update_fixture() -> object:
    info = _docker("info", timeout=60)
    if info.returncode != 0:
        pytest.skip("docker unavailable: real-boundary update tests require it")
    fixture = UpdateFixture()
    fixture.start()
    yield fixture
    print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture
def _clean_real_leases(migrated_url: str):
    """A killed worker's lease lives up to its TTL in the disposable DB,
    which outlives one pytest invocation; the fixed real identities must
    start each run unfenced and unheld."""
    import psycopg

    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_resource_locks WHERE server_id = ANY(%s)",
            ([REAL_SERVER_D, REAL_SERVER_E, REAL_SERVER_F],),
        )
        conn.commit()
    yield


# -- real-boundary wiring -----------------------------------------------------


def _real_transport(fixture: UpdateFixture):
    key_path, known_hosts = fixture.enroll()
    assert fixture.identity is not None
    return update_module.ssh_transport(
        fixture.identity, key_path=key_path, known_hosts_path=known_hosts
    )


def _real_update_service(
    *,
    transport,
    migrated_url: str,
    sessions,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    locks: ResourceLockService,
) -> tuple[UpdateService, ApprovalStore]:
    approvals = ApprovalStore(sessions)
    service = UpdateService(
        transport=transport,
        locks=locks,
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: _grants(server_id),
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(),
    )
    return service, approvals


def _real_install_service(
    *, transport, migrated_url: str, sessions, store, server_id: uuid.UUID
):
    from mai.applications.install import InstallService
    from mai.plugins.registry import parse_descriptor as _pd

    install_descriptor = _pd(
        {
            "identifier": "app.install",
            "version": "1.0.0",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": "privileged",
            "resource_scope": ["application"],
            "required_grants": ["docker.compose"],
        }
    )
    approvals = ApprovalStore(sessions)
    service = InstallService(
        transport=transport,
        locks=ResourceLockService(migrated_url),
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: {
            "app.install": frozenset({server_id}),
        },
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=install_descriptor,
    )
    return service, approvals


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


def _install_approval_request(payload_hash: str, server_id: uuid.UUID):
    from mai.policy.approvals import ApprovalRequest

    return ApprovalRequest(
        capability_id="app.install",
        server_id=server_id,
        payload_hash=payload_hash,
        policy_revision=POLICY_REVISION,
        created_by=PRINCIPAL,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _manifest_path(store: ApplicationManifestStore, sid: uuid.UUID, aid: uuid.UUID) -> Path:
    return store.root / "servers" / str(sid) / "applications" / str(aid) / "manifest.yaml"


def _port_mutant(recipe: ApplicationRecipe, port: int) -> ApplicationRecipe:
    document = yaml.safe_load(recipe_text_of(recipe))
    document["deploy"]["ports"][0]["host"] = port
    return parse_recipe_text(yaml.safe_dump(document))


def recipe_text_of(recipe: ApplicationRecipe) -> str:
    from mai.applications.recipes import render_recipe

    return render_recipe(recipe)


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_live_update_preserves_stateful_data(
    update_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1, stateful_v2,
) -> None:
    """(a) LIVE v1→v2 update over REAL sshd + REAL docker daemon: the
    planted sentinel survives the digest change, BOTH health gates pass on
    both versions, the daemon really runs the v2 pinned digest afterwards,
    the pre-update snapshot exists on the target, and the canonical
    manifest/projection advances to revision 2."""
    import urllib.request

    fixture: UpdateFixture = update_fixture
    sid, aid = REAL_SERVER_D, REAL_APP_D
    v1 = _port_mutant(stateful_v1, REAL_PORT_D)
    v2 = _port_mutant(stateful_v2, REAL_PORT_D)
    plan_v1 = build_deployment_plan(v1, sid, aid, TARGET)
    plan_v2 = build_deployment_plan(v2, sid, aid, TARGET)
    fixture.track(plan_v1.project_name)
    assert plan_v1.project_name == plan_v2.project_name  # same application

    transport = _real_transport(fixture)
    install_service, install_approvals = _real_install_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    install_approval = await install_approvals.create(
        _install_approval_request(plan_v1.plan_sha256, sid)
    )
    from mai.applications.install import InstallRequest

    installed = await install_service.install(
        InstallRequest(
            server_id=sid,
            application_id=aid,
            recipe=v1,
            target=TARGET,
            context=_context(server_id=sid, capability="app.install"),
            approval_id=install_approval.approval_id,
        )
    )
    assert installed.status == "succeeded", (installed.reason, installed.notes)
    assert installed.manifest_revision == 1

    # Plant the stateful sentinel THROUGH the daemon (see topology note).
    containers = _project_container_ids(plan_v1.project_name)
    assert len(containers) == 1
    sentinel_payload = f"mai-fxt-08-upd sentinel {aid}\n"
    fixture.plant_sentinel(containers[0], sentinel_payload)
    v1_image_id = _image_id(v1.image)

    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    service, approvals = _real_update_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid, locks=locks,
    )
    approval = await approvals.create(_approval_request(plan_v2.plan_sha256, sid))
    result = await service.update(
        UpdateRequest(
            server_id=sid,
            application_id=aid,
            recipe=v2,
            current_recipe=v1,
            target=TARGET,
            context=_context(server_id=sid),
            approval_id=approval.approval_id,
            backup_confirmed=True,
        )
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.health_verified is True
    assert result.prior_image == v1.image
    assert result.new_image == v2.image
    assert result.prior_healthy is True
    assert result.rollback_attempted is False
    assert result.snapshot_paths == (update_module._snapshot_path(plan_v2),)
    assert result.manifest_revision == 2
    assert result.lock_released is True
    assert "baseline deployment verified healthy (prior version)" in result.notes

    # The snapshot really exists (non-empty) on the SSH target.
    assert fixture.file_nonempty(result.snapshot_paths[0])

    # Daemon truth: exactly one healthy container running the V2 digest.
    after = _project_container_ids(plan_v2.project_name)
    assert len(after) == 1, f"expected one project container, saw {len(after)}"
    assert _container_health(after[0]) == "healthy"
    assert _container_image(after[0]) == _image_id(v2.image)
    assert _container_image(after[0]) != v1_image_id

    # Independent HOST-side proof the port is really published and answering.
    with urllib.request.urlopen(
        f"http://127.0.0.1:{REAL_PORT_D}/health", timeout=10
    ) as response:
        assert response.status == 200

    # The sentinel SURVIVED the digest change (stateful data preserved).
    assert fixture.read_sentinel(after[0]) == sentinel_payload

    # Canonical record: revision 2, no live intents, one-use decision spent.
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(sid, aid)
    assert record is not None and record.revision == 2
    assert record.compose_project == plan_v2.project_name
    assert await adoption.list_live_intents(server_id=sid) == []
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_unhealthy_candidate_rolls_back_to_prior_digest(
    update_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1, stateful_v2,
) -> None:
    """(b) Injected unhealthy candidate (healthcheck against a container
    port nothing serves): the executor rolls back to the PRIOR pinned
    digest, the daemon really runs the v1 image again and answers healthy,
    the sentinel survives BOTH the failed update and the rollback, the
    baseline compose bytes are restored on the target, and NO canonical
    commit happens (revision stays at the install's 1)."""
    fixture: UpdateFixture = update_fixture
    sid, aid = REAL_SERVER_E, REAL_APP_E
    v1 = _port_mutant(stateful_v1, REAL_PORT_E)

    def _never_healthy(document: dict) -> None:
        document["deploy"]["ports"][0]["host"] = REAL_PORT_E
        # filebrowser is a SPA (200 on garbage paths): a healthcheck against
        # a container port nothing listens on fails DETERMINISTICALLY.
        document["deploy"]["ports"].append(
            {"host": REAL_PORT_E + 1, "container": 81, "protocol": "tcp"}
        )
        document["health"]["port"] = 81
        document["health"]["interval_seconds"] = 5
        document["health"]["retries"] = 1
        document["health"]["start_period_seconds"] = 0

    document = yaml.safe_load(recipe_text_of(stateful_v2))
    _never_healthy(document)
    v2_bad = parse_recipe_text(yaml.safe_dump(document))

    plan_v1 = build_deployment_plan(v1, sid, aid, TARGET)
    plan_bad = build_deployment_plan(v2_bad, sid, aid, TARGET)
    fixture.track(plan_v1.project_name)

    transport = _real_transport(fixture)
    install_service, install_approvals = _real_install_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    install_approval = await install_approvals.create(
        _install_approval_request(plan_v1.plan_sha256, sid)
    )
    from mai.applications.install import InstallRequest

    installed = await install_service.install(
        InstallRequest(
            server_id=sid,
            application_id=aid,
            recipe=v1,
            target=TARGET,
            context=_context(server_id=sid, capability="app.install"),
            approval_id=install_approval.approval_id,
        )
    )
    assert installed.status == "succeeded", (installed.reason, installed.notes)

    containers = _project_container_ids(plan_v1.project_name)
    assert len(containers) == 1
    sentinel_payload = f"mai-fxt-08-upd sentinel {aid}\n"
    fixture.plant_sentinel(containers[0], sentinel_payload)
    v1_image_id = _image_id(v1.image)
    v2_image_id = _image_id(v2_bad.image)
    assert v1_image_id != v2_image_id

    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    service, approvals = _real_update_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid, locks=locks,
    )
    approval = await approvals.create(_approval_request(plan_bad.plan_sha256, sid))
    result = await service.update(
        UpdateRequest(
            server_id=sid,
            application_id=aid,
            recipe=v2_bad,
            current_recipe=v1,
            target=TARGET,
            context=_context(server_id=sid),
            approval_id=approval.approval_id,
            backup_confirmed=True,
        )
    )

    assert result.status == "rolled_back", (result.reason, result.notes)
    assert result.effect == "executed"  # the start effect really happened
    assert result.health_verified is False
    assert result.prior_image == v1.image
    assert result.new_image == v2_bad.image
    assert result.image_rollback_supported is True
    assert result.rollback_attempted is True
    assert result.rollback_verified is True
    assert result.lock_released is True
    assert "health regression after update" in result.notes
    assert any(
        "rolling back to the prior pinned digest" in n for n in result.notes
    )

    # Daemon truth: the PRIOR digest is running and healthy again.
    after = _project_container_ids(plan_v1.project_name)
    assert len(after) == 1
    assert _container_health(after[0]) == "healthy"
    assert _container_image(after[0]) == v1_image_id
    assert _container_image(after[0]) != v2_image_id

    # The sentinel survived BOTH the failed candidate and the rollback.
    assert fixture.read_sentinel(after[0]) == sentinel_payload

    # The baseline compose bytes were restored on the target (the rollback
    # anchor the next update would verify).
    hash_out = _docker(
        "exec", FIXTURE_CONTAINER, "sha256sum", "--", plan_v1.compose_path
    )
    assert hash_out.returncode == 0
    assert hash_out.stdout.split()[:1] == [plan_v1.compose_sha256]

    # NO canonical commit: the record still describes the baseline.
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(sid, aid)
    assert record is not None and record.revision == 1
    assert await adoption.list_live_intents(server_id=sid) == []
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_blocked_recovery_leaves_candidate_visible(
    update_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1, stateful_v2,
) -> None:
    """(c) A recipe that declares NO image rollback gets honest BLOCKED
    recovery: the health regression is surfaced, the unhealthy candidate is
    left running and visible, nothing is restored, no rollback is claimed,
    and the canonical record is untouched."""
    fixture: UpdateFixture = update_fixture
    sid, aid = REAL_SERVER_F, REAL_APP_F
    v1 = _port_mutant(stateful_v1, REAL_PORT_F)

    def _no_rollback_never_healthy(document: dict) -> None:
        document["deploy"]["ports"][0]["host"] = REAL_PORT_F
        document["deploy"]["ports"].append(
            {"host": REAL_PORT_F + 1, "container": 81, "protocol": "tcp"}
        )
        document["health"]["port"] = 81
        document["health"]["interval_seconds"] = 5
        document["health"]["retries"] = 1
        document["health"]["start_period_seconds"] = 0
        document["recovery"]["image_rollback"] = False

    base_doc = yaml.safe_load(recipe_text_of(stateful_v2))
    _no_rollback_never_healthy(base_doc)
    v2_bad = parse_recipe_text(yaml.safe_dump(base_doc))
    assert v2_bad.recovery.image_rollback is False

    plan_v1 = build_deployment_plan(v1, sid, aid, TARGET)
    plan_bad = build_deployment_plan(v2_bad, sid, aid, TARGET)
    fixture.track(plan_v1.project_name)

    transport = _real_transport(fixture)
    install_service, install_approvals = _real_install_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    install_approval = await install_approvals.create(
        _install_approval_request(plan_v1.plan_sha256, sid)
    )
    from mai.applications.install import InstallRequest

    installed = await install_service.install(
        InstallRequest(
            server_id=sid,
            application_id=aid,
            recipe=v1,
            target=TARGET,
            context=_context(server_id=sid, capability="app.install"),
            approval_id=install_approval.approval_id,
        )
    )
    assert installed.status == "succeeded", (installed.reason, installed.notes)
    v2_image_id = _image_id(v2_bad.image)

    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    service, approvals = _real_update_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid, locks=locks,
    )
    approval = await approvals.create(_approval_request(plan_bad.plan_sha256, sid))
    result = await service.update(
        UpdateRequest(
            server_id=sid,
            application_id=aid,
            recipe=v2_bad,
            current_recipe=v1,
            target=TARGET,
            context=_context(server_id=sid),
            approval_id=approval.approval_id,
            backup_confirmed=True,
        )
    )

    assert result.status == "failed", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.health_verified is False
    assert result.image_rollback_supported is False
    assert result.rollback_attempted is False  # declared limit honored
    assert result.rollback_verified is False
    assert any("recovery: no image rollback" in n for n in result.notes)
    assert result.lock_released is True

    # Daemon truth: the UNHEALTHY CANDIDATE is still what runs (visible,
    # honestly reported) — no restore was attempted.
    after = _project_container_ids(plan_bad.project_name)
    assert len(after) == 1
    assert _container_image(after[0]) == v2_image_id
    assert _container_health(after[0]) == "unhealthy"

    # The candidate compose bytes stayed deployed (nothing restored).
    hash_out = _docker(
        "exec", FIXTURE_CONTAINER, "sha256sum", "--", plan_bad.compose_path
    )
    assert hash_out.returncode == 0
    assert hash_out.stdout.split()[:1] == [plan_bad.compose_sha256]

    # NO canonical commit.
    adoption = ApplicationAdoption(sessions)
    record = await adoption.read(sid, aid)
    assert record is not None and record.revision == 1
    assert await adoption.list_live_intents(server_id=sid) == []
