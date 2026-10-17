"""Focused tests for application removal (card 08-remove).

Acceptance coverage (card mai-08-remove):

- **AC1 (keep-data cannot silently become delete; bound deletion preview
  and grants)** — the tier-1 removal carries the EXPLICIT ``data_intent``
  (no default: an absent/inferred flag is unrepresentable) and NEVER
  deletes data (no ``rm`` in the entire remove flow, ``data_deleted`` is
  a constant False); tier-2 data deletion is a SEPARATE operation with
  its own capability (``app.remove-data``), its own grants, the literal
  ``delete_data=True`` intent, the recipe's declared ``explicit_choice``
  policy (a ``keep`` policy refuses, always) and a one-use approval bound
  to the hashed deletion manifest — the preview
  (:meth:`RemoveService.deletion_manifest`) hashes the EXACT locations,
  so preview/effect drift is the altered-payload denial (C03 rule 4).
- **AC2 (only adopted project resources; neighbor unaffected; accurate
  record)** — removal verifies the deployed Compose bytes against the
  rebuilt plan before any effect (``state_diverged``), tears down ONLY
  the plan's Compose project (``down`` without volume flags), and
  tombstone/purges the canonical manifest + projection through the
  adoption pipeline (C02 rule 5/W4, deterministic ``remove:`` intent).
  A neighboring application's project, volume data and canonical record
  are unaffected. Purge deletes ONLY the manifest locations, only after
  the deployment is VERIFIED absent, and "deleted" is claimed only when
  re-observed.

Test posture (mirrors test_install.py / test_update.py): the target
boundary is a faithful in-memory simulator of the exact command surface
remove.py drives (``sha256sum``, ``compose ps -q``, ``compose down
--remove-orphans``, ``ls -A``, ``rm -rf``); the approval/lock/adoption
seams run BOTH as semantic stubs (pure tests) and as the REAL
Postgres-backed stores (live-DB section, disposable fixture) — per the
test policy, mocks never prove the store/concurrency claims, the live
section does. The catalogue fixtures (example-http, example-stateful)
are the frozen 08-fixture-recipes revision streams. The
``real_target``-marked section additionally drives the executor over the
REAL transport: a disposable Arch sshd container mounting the host
docker socket (``mai-fxt-08-rem-*`` namespace, mirroring the 08-install
/ 08-update fixtures) — with one deliberate fixture extension: the
planner-allocated ``/var/lib/mai/apps`` subtree is bind-mounted into the
fixture, so the SSH-target view of the persistent-data locations IS the
daemon-side bind-source view. That makes the keep-data vs purge-data
acceptance REAL end to end: the sentinel is planted through the running
container's bind, observed over SSH AND through an independent
daemon-side mount, retained by the keep-policy removal, and really
deleted by the separately authorized purge — including the repeat
stale-approval denial.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mai.applications.remove as remove_module
from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    AdoptionResult,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.install import install_intent_id
from mai.applications.recipes import (
    ApplicationRecipe,
    TargetEnvironment,
    build_deployment_plan,
    parse_recipe_text,
    project_name,
    render_recipe,
)
from mai.applications.remove import (
    CAPABILITY_ID,
    DEFAULT_LEASE_TTL_SECONDS,
    LOCK_RESOURCE_PREFIX,
    PURGE_CAPABILITY_ID,
    DeletionManifest,
    PurgeDataRequest,
    RemoveDataIntent,
    RemoveError,
    RemoveErrorCode,
    RemoveRequest,
    RemoveResult,
    RemoveService,
    remove_intent_id,
)
from mai.applications.update import update_intent_id
from mai.credentials.env import EnvResolver
from mai.execution.commands import CommandRequest, CommandStatus
from mai.execution.ssh import (
    SshIdentity,
    pin_host_key,
    resolve_identity_key,
    scan_host_keys,
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
    ApprovalRequest,
    ApprovalStore,
)
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

SERVER_ID = uuid.UUID("080a080a-080a-480a-880a-080a080a080a")
APP_ID = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e0c0e")
NEIGHBOR_APP_ID = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e0c0f")
OTHER_SERVER_ID = uuid.UUID("09090909-0909-4909-8909-090909090909")
RUN_ID = uuid.UUID("07070707-0707-4707-8707-070707070707")

#: Live-DB section identities (distinct from install/update's; the
#: disposable DB is re-migrated per session but the lock table outlives
#: one run).
LIVE_SERVER_ID = uuid.UUID("080a080a-080a-480a-880a-080a080a00e1")
LIVE_APP_ID = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e00e1")

POLICY_REVISION = 1
PRINCIPAL = "admin-test"

#: Synthetic non-disclosure marker (NOT a secret): planted as volume DATA;
#: no receipt, note, error, display or command argument may carry it.
SENTINEL_NAME = "mai-fixture-sentinel.txt"
SENTINEL_PAYLOAD = "SENTINEL-mai-08-remove-not-a-secret\n"

_DEFAULT_APPROVAL_ID = uuid.UUID("0d0e0d0e-0d0e-4d0e-8d0e-0d0e0d0e0d0e")


# ---------------------------------------------------------------------------
# Catalogue fixtures (frozen 08-fixture-recipes revision streams)
# ---------------------------------------------------------------------------


def _document_texts(filename: str) -> list[str]:
    stream = (CATALOGUE_DIR / filename).read_text(encoding="utf-8")
    return [yaml.serialize(node) for node in yaml.compose_all(stream)]


def _recipe(filename: str = STATEFUL_FILE, revision: int = 0) -> ApplicationRecipe:
    return parse_recipe_text(_document_texts(filename)[revision])


def _mutant_recipe(filename: str, mutate) -> ApplicationRecipe:
    document = yaml.safe_load(_document_texts(filename)[0])
    mutate(document)
    return parse_recipe_text(yaml.safe_dump(document))


@pytest.fixture
def stateful_v1() -> ApplicationRecipe:
    return _recipe(STATEFUL_FILE, 0)


@pytest.fixture
def http_v1() -> ApplicationRecipe:
    return _recipe(HTTP_FILE, 0)


# ---------------------------------------------------------------------------
# Gate inputs (policy/grants/context/descriptors)
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


def _purge_descriptor(**overrides: object):
    overrides.setdefault("identifier", PURGE_CAPABILITY_ID)
    return _descriptor(**overrides)


def _context(
    server_id: uuid.UUID = SERVER_ID,
    capability: str = CAPABILITY_ID,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_ID,
        parent_scope=None,
        profile_revision="profile-r1",
        prompt_revision="prompt-r1",
        credential_references=(),
        allowed_capabilities=(capability,),
        mode=PolicyMode.ASK,
    )


def _grants(
    server_id: uuid.UUID = SERVER_ID,
    *,
    granted: bool = True,
    purge_granted: bool | None = None,
):
    if purge_granted is None:
        purge_granted = granted
    return {
        CAPABILITY_ID: frozenset({server_id} if granted else set()),
        PURGE_CAPABILITY_ID: frozenset({server_id} if purge_granted else set()),
    }


def _policy(
    revision: int = POLICY_REVISION,
    denied: frozenset[str] = frozenset(),
):
    from mai.policy.evaluate import Policy

    return Policy(revision=revision, denied_capabilities=denied)


def _remove_request(
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    data_intent: RemoveDataIntent = RemoveDataIntent.KEEP,
    context: ExecutionContext | None = None,
    application_id: uuid.UUID = APP_ID,
    server_id: uuid.UUID | None = None,
) -> RemoveRequest:
    sid = server_id or (context.server_id if context else SERVER_ID)
    return RemoveRequest(
        server_id=sid,
        application_id=application_id,
        recipe=recipe,
        target=TARGET,
        context=context or _context(server_id=sid),
        approval_id=approval_id or _DEFAULT_APPROVAL_ID,
        data_intent=data_intent,
    )


def _purge_request(
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    delete_data: bool = True,
    context: ExecutionContext | None = None,
    application_id: uuid.UUID = APP_ID,
    server_id: uuid.UUID | None = None,
) -> PurgeDataRequest:
    sid = server_id or (context.server_id if context else SERVER_ID)
    return PurgeDataRequest(
        server_id=sid,
        application_id=application_id,
        recipe=recipe,
        target=TARGET,
        context=context or _context(server_id=sid, capability=PURGE_CAPABILITY_ID),
        approval_id=approval_id or _DEFAULT_APPROVAL_ID,
        delete_data=delete_data,
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
        refuse_acquire: LockErrorCode | None = None,
    ) -> None:
        self.acquired: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.heartbeat_calls = 0
        self._lapse = lapse_heartbeats
        self._refuse = refuse_acquire

    def acquire(self, server_id, resource, *, owner_ref="", ttl=None):  # type: ignore[override]
        if self._refuse is not None:
            raise ResourceLockError(self._refuse, f"scripted {self._refuse.value}")
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
        capability_id: str = CAPABILITY_ID,
        payload_hash: str,
        policy_revision: int = POLICY_REVISION,
        server_id: uuid.UUID = SERVER_ID,
    ) -> None:
        self._records: dict[uuid.UUID, dict[str, object]] = {
            approval_id: {
                "capability_id": capability_id,
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
    ):
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


class _RecordRow:
    """Minimal adoption record shape for stub reads."""

    def __init__(self, revision: int, compose_project: str | None) -> None:
        self.revision = revision
        self.compose_project = compose_project


class _StubAdoption(ApplicationAdoption):
    """In-memory canonical-read/revoke/recover pipeline."""

    def __init__(
        self,
        existing: _RecordRow | None = None,
        revoke_failures: tuple[AdoptionErrorCode, ...] = (),
    ) -> None:
        self.existing = existing
        self._failures = list(revoke_failures)
        self.revoke_calls: list[tuple[uuid.UUID, uuid.UUID, int, uuid.UUID]] = []
        self.recover_calls = 0

    async def read(self, server_id, application_id):  # type: ignore[override]
        return self.existing

    async def revoke(  # type: ignore[override]
        self,
        store,
        server_id,
        application_id,
        *,
        expected_revision,
        intent_id=None,
    ):
        self.revoke_calls.append(
            (server_id, application_id, expected_revision, intent_id)
        )
        if self._failures:
            code = self._failures.pop(0)
            raise AdoptionError(code, f"scripted {code.value}")
        if self.existing is None:
            # mirror the real pipeline: nothing recorded, nothing resumed
            raise AdoptionError(AdoptionErrorCode.NOT_ADOPTED, "scripted")
        return AdoptionResult(
            intent_id or uuid.uuid4(),
            server_id,
            application_id,
            "delete",
            "applied",
            None,
            None,
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
# The target simulator: the exact command surface remove.py drives
# ---------------------------------------------------------------------------


class _FakeTarget:
    """In-memory target executing exactly the commands remove.py emits.

    Per-project Compose state (a removal must never touch a neighboring
    project), first-class volume data that ``compose down`` can NEVER
    delete (C09 rule 5 is a target property, not a promise), observed
    ``rm`` calls, and scripted failures that turn any op into ``failed``
    (rc 1) or ``outcome_unknown`` (timeout cut).
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.volume_data: dict[str, tuple[str, ...]] = {}
        self.projects: dict[str, dict[str, bool]] = {}
        self.rm_calls: list[str] = []
        self.down_calls = 0
        #: when True a successful down leaves the project running (a down
        #: that claimed success but did not remove — deterministic failure)
        self.down_leaves_containers = False
        #: when True a successful rm leaves the data in place
        self.rm_ineffective = False
        self.fail: dict[str, str] = {}
        self.block_on: str | None = None
        self._gate = threading.Event()
        self.calls: list[CommandRequest] = []

    def unblock(self) -> None:
        self._gate.set()

    def __call__(self, request: CommandRequest):
        self.calls.append(request)
        scripted = next(
            (v for k, v in self.fail.items() if k in request.display), None
        )
        if scripted == "unknown":
            return _outcome(
                request, CommandStatus.OUTCOME_UNKNOWN, None, b"", b"",
                timed_out=True,
            )
        if scripted == "failed":
            return _outcome(
                request, CommandStatus.FAILED, 1, b"", b"scripted failure\n"
            )
        if self.block_on and self.block_on in request.display:
            self._gate.wait(timeout=15.0)
        argv = request.argv or ()
        if argv[:1] == ("sha256sum",):
            path = argv[-1]
            data = self.files.get(path)
            if data is None:
                return _outcome(
                    request, CommandStatus.FAILED, 1, b"", b"sha256sum: cannot open\n"
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
        if argv[:1] == ("rm",):
            path = argv[-1]
            self.rm_calls.append(path)
            if not self.rm_ineffective:
                self.volume_data.pop(path, None)
            return _text(request, "")
        if argv[:2] == ("docker", "compose"):
            return self._compose(request, argv)
        raise AssertionError(f"unexpected transport command: {argv!r}")

    def _compose(self, request: CommandRequest, argv: tuple[str, ...]):
        project = argv[3]
        rest = argv[6:]
        op = rest[0]
        if op == "ps":
            state = self.projects.get(project)
            if state is None or not state["running"]:
                return _outcome(
                    request, CommandStatus.FAILED, 1, b"", b"no such project\n"
                )
            if "-q" in rest:
                return _text(request, "abc123def456\n")
            healthy = "healthy" if state["healthy"] else "unhealthy"
            return _text(
                request,
                json.dumps(
                    [{"Name": "fixture-1", "State": "running", "Health": healthy}]
                )
                + "\n",
            )
        if op == "down":
            self.down_calls += 1
            if self.projects.get(project) is None:
                # Real compose answers "no such project" (rc 1) on a down
                # of an absent project.
                return _outcome(
                    request, CommandStatus.FAILED, 1, b"", b"no such project\n"
                )
            if not self.down_leaves_containers:
                self.projects.pop(project, None)
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


def _plan_for(
    recipe: ApplicationRecipe,
    application_id: uuid.UUID = APP_ID,
    server_id: uuid.UUID = SERVER_ID,
):
    return build_deployment_plan(recipe, server_id, application_id, TARGET)


def _seed_deployed(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    application_id: uuid.UUID = APP_ID,
    *,
    data: bool = False,
) -> None:
    """Make the target look like a healthy deployment of recipe."""
    plan = _plan_for(recipe, application_id)
    target.files[plan.compose_path] = plan.compose_text.encode("utf-8")
    target.projects[plan.project_name] = {"running": True, "healthy": True}
    if data:
        for volume in plan.volumes:
            target.volume_data[volume.host_path] = (SENTINEL_NAME,)


def _seed_removed(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    application_id: uuid.UUID = APP_ID,
    *,
    data: bool = True,
) -> None:
    """Make the target look like a removed deployment (compose file
    retained as the plan anchor, project gone, volume data present)."""
    plan = _plan_for(recipe, application_id)
    target.files[plan.compose_path] = plan.compose_text.encode("utf-8")
    target.projects.pop(plan.project_name, None)
    if data:
        for volume in plan.volumes:
            target.volume_data[volume.host_path] = (SENTINEL_NAME,)


def _service(
    target: _FakeTarget,
    recipe: ApplicationRecipe,
    *,
    approval_id: uuid.UUID | None = None,
    application_id: uuid.UUID = APP_ID,
    capability_id: str = CAPABILITY_ID,
    locks: ResourceLockService | None = None,
    approvals: ApprovalStore | None = None,
    adoption: ApplicationAdoption | None = None,
    store: ApplicationManifestStore | None = None,
    grants=None,
    policy=None,
    adopted=lambda server_id: True,
    descriptor=None,
    purge_descriptor=None,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
):
    """Wire a RemoveService against the simulator; returns
    ``(service, approvals, locks, adoption, store)`` — the SAME stub
    instances the service holds, so tests observe its real effects."""
    plan = _plan_for(recipe, application_id)
    if locks is None:
        locks = _StubLocks()
    if approvals is None:
        payload = (
            plan.plan_sha256
            if capability_id == CAPABILITY_ID
            else _manifest_hash(recipe, application_id)
        )
        approvals = _StubApprovals(
            approval_id or _DEFAULT_APPROVAL_ID,
            capability_id=capability_id,
            payload_hash=payload,
        )
    if adoption is None:
        adoption = _StubAdoption()
    if store is None:
        store = _StubStore()
    service = RemoveService(
        transport=target,
        locks=locks,
        approvals=approvals,
        adoption=adoption,
        store=store,
        grants=grants if grants is not None else (lambda: _grants()),
        policy=policy if policy is not None else (lambda: _policy()),
        adopted=adopted,
        descriptor=descriptor or _descriptor(),
        purge_descriptor=purge_descriptor or _purge_descriptor(),
        lease_ttl=lease_ttl,
    )
    return service, approvals, locks, adoption, store


def _manifest_hash(recipe: ApplicationRecipe, application_id: uuid.UUID) -> str:
    from mai.applications.remove import purge_deletion_manifest

    request = _purge_request(recipe, application_id=application_id)
    return purge_deletion_manifest(request)[1].manifest_sha256


# ---------------------------------------------------------------------------
# Wiring/construction guards
# ---------------------------------------------------------------------------


def test_service_wiring_is_fail_closed(stateful_v1) -> None:
    target = _FakeTarget()
    with pytest.raises(RemoveError):
        RemoveService(
            transport="not-callable",
            locks=_StubLocks(),
            approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="x"),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda _sid: True,
            descriptor=_descriptor(),
            purge_descriptor=_purge_descriptor(),
        )
    with pytest.raises(RemoveError):
        RemoveService(
            transport=target,
            locks="not-locks",
            approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="x"),
            adoption=_StubAdoption(),
            store=_StubStore(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda _sid: True,
            descriptor=_descriptor(),
            purge_descriptor=_purge_descriptor(),
        )
    for bad in (
        _descriptor(identifier="app.other"),
        _descriptor(effect_class="mutate"),
        "not-a-descriptor",
    ):
        with pytest.raises(RemoveError):
            RemoveService(
                transport=target,
                locks=_StubLocks(),
                approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="x"),
                adoption=_StubAdoption(),
                store=_StubStore(),
                grants=lambda: _grants(),
                policy=lambda: _policy(),
                adopted=lambda _sid: True,
                descriptor=bad,
                purge_descriptor=_purge_descriptor(),
            )
    for bad_purge in (
        _purge_descriptor(identifier="app.other"),
        _purge_descriptor(effect_class="mutate"),
        "not-a-descriptor",
    ):
        with pytest.raises(RemoveError):
            RemoveService(
                transport=target,
                locks=_StubLocks(),
                approvals=_StubApprovals(_DEFAULT_APPROVAL_ID, payload_hash="x"),
                adoption=_StubAdoption(),
                store=_StubStore(),
                grants=lambda: _grants(),
                policy=lambda: _policy(),
                adopted=lambda _sid: True,
                descriptor=_descriptor(),
                purge_descriptor=bad_purge,
            )


def test_requests_require_explicit_intents(stateful_v1) -> None:
    """C09 rule 5: the keep/delete decision cannot be inferred from an
    absent flag — construction demands the explicit member."""
    with pytest.raises(TypeError):
        RemoveRequest(  # type: ignore[call-arg]
            server_id=SERVER_ID,
            application_id=APP_ID,
            recipe=stateful_v1,
            target=TARGET,
            context=_context(),
            approval_id=_DEFAULT_APPROVAL_ID,
        )
    with pytest.raises(RemoveError) as excinfo:
        _remove_request(stateful_v1, data_intent="keep")  # type: ignore[arg-type]
    assert excinfo.value.code is RemoveErrorCode.INVALID_REQUEST
    with pytest.raises(TypeError):
        PurgeDataRequest(  # type: ignore[call-arg]
            server_id=SERVER_ID,
            application_id=APP_ID,
            recipe=stateful_v1,
            target=TARGET,
            context=_context(capability=PURGE_CAPABILITY_ID),
            approval_id=_DEFAULT_APPROVAL_ID,
        )
    with pytest.raises(RemoveError) as excinfo:
        _purge_request(stateful_v1, delete_data="yes")  # type: ignore[arg-type]
    assert excinfo.value.code is RemoveErrorCode.INVALID_REQUEST
    # context/target binding is construction-checked like install/update.
    with pytest.raises(RemoveError):
        _remove_request(
            stateful_v1,
            server_id=SERVER_ID,
            context=_context(server_id=OTHER_SERVER_ID),
        )


# ---------------------------------------------------------------------------
# AC1/AC2 tier-1 removal
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_remove_tears_down_only_own_project_and_keeps_data(
    stateful_v1,
) -> None:
    """AC2: the plan's project is removed (containers/networks), the
    compose file and the volume data are RETAINED, the canonical record is
    tombstoned, and a neighboring application is untouched. No ``rm``
    exists anywhere in the tier-1 flow (C09 rule 5)."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    _seed_deployed(target, stateful_v1, NEIGHBOR_APP_ID, data=True)
    locks = _StubLocks()
    adoption = _StubAdoption(existing=_RecordRow(1, project_name(APP_ID)))
    service, approvals, locks, adoption, _ = _service(
        target, stateful_v1, locks=locks, adoption=adoption
    )

    result = await service.remove(_remove_request(stateful_v1))

    assert isinstance(result, RemoveResult)
    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert result.reason is None
    assert result.data_intent == "keep"
    assert result.data_deleted is False
    assert result.manifest_removed is True
    assert result.manifest_revision == 1
    assert result.lock_released is True
    assert result.approval_id == _DEFAULT_APPROVAL_ID
    assert [op.op for op in result.operations] == ["compose_down"]
    assert result.operations[0].action == "applied"
    # one-use decision spent exactly once
    assert approvals.consume_calls == 1
    # the deterministic tombstone intent (C02 rule 5 resume safety)
    plan = _plan_for(stateful_v1)
    assert adoption.revoke_calls == [
        (SERVER_ID, APP_ID, 1, remove_intent_id(SERVER_ID, APP_ID, plan.plan_sha256))
    ]
    # target truth: own project gone, data + compose file retained
    assert target.down_calls == 1
    assert plan.project_name not in target.projects
    assert plan.compose_path in target.files  # retained plan anchor
    assert all(
        request.argv[:1] != ("rm",) for request in target.calls
    ), "tier-1 removal must never issue a data deletion"
    # neighbor untouched: project alive, volume data intact; own data also
    # retained (keep semantics: down never touches volume data)
    neighbor = _plan_for(stateful_v1, NEIGHBOR_APP_ID)
    assert target.projects[neighbor.project_name]["running"] is True
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)
    assert target.volume_data[neighbor.volumes[0].host_path] == (SENTINEL_NAME,)
    # the exclusive lease was taken and released (C05 rule 6)
    assert locks.acquired == [(str(SERVER_ID), f"{LOCK_RESOURCE_PREFIX}{APP_ID}", f"remove:{RUN_ID}")]
    assert len(locks.released) == 1


@pytest.mark.anyio
async def test_remove_records_delete_intent_without_deleting(stateful_v1) -> None:
    """C09 rule 5: a declared delete-data intent is RECORDED on the
    receipt; the data survives until the separately authorized purge."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    service, _, _, adoption, _ = _service(
        target, stateful_v1, adoption=_StubAdoption(existing=_RecordRow(1, None))
    )

    result = await service.remove(
        _remove_request(stateful_v1, data_intent=RemoveDataIntent.DELETE)
    )

    assert result.status == "succeeded"
    assert result.data_intent == "delete"
    assert result.data_deleted is False
    plan = _plan_for(stateful_v1)
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)
    assert all(request.argv[:1] != ("rm",) for request in target.calls)
    assert any(PURGE_CAPABILITY_ID in note for note in result.notes)


@pytest.mark.anyio
async def test_remove_refuses_state_diverged_and_not_installed(
    stateful_v1,
) -> None:
    target = _FakeTarget()
    plan = _plan_for(stateful_v1)

    # (a) deployed bytes differ from the bound plan: refuse before any effect.
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    target.files[plan.compose_path] += b"# drifted\n"
    service, approvals, locks, _, _ = _service(target, stateful_v1)
    result = await service.remove(_remove_request(stateful_v1))
    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == RemoveErrorCode.STATE_DIVERGED.value
    assert result.lock_released is True  # terminal refusal inside the lease
    assert approvals.consume_calls == 0
    assert target.down_calls == 0
    assert target.projects[plan.project_name]["running"] is True

    # (b) compose file gone while the project still runs: honest divergence.
    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    del target2.files[plan.compose_path]
    service2, _, _, _, _ = _service(target2, stateful_v1)
    result2 = await service2.remove(_remove_request(stateful_v1))
    assert result2.reason == RemoveErrorCode.STATE_DIVERGED.value
    assert target2.down_calls == 0

    # (c) nothing anywhere: not_installed (never a silent no-op success).
    target3 = _FakeTarget()
    service3, _, _, _, _ = _service(target3, stateful_v1)
    result3 = await service3.remove(_remove_request(stateful_v1))
    assert result3.status == "refused"
    assert result3.reason == RemoveErrorCode.NOT_INSTALLED.value
    assert result3.effect == "not_attempted"


@pytest.mark.anyio
async def test_remove_identity_guards(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    # a canonical record under ANOTHER server refuses (C09 rule 1)
    service, _, _, _, _ = _service(
        target, stateful_v1, store=_StubStore(foreign_servers=(OTHER_SERVER_ID,))
    )
    result = await service.remove(_remove_request(stateful_v1))
    assert result.reason == RemoveErrorCode.CROSS_SERVER_CONFLICT.value
    assert result.effect == "not_attempted"

    # a record binding the identity to a different project refuses
    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    service2, _, _, _, _ = _service(
        target2,
        stateful_v1,
        adoption=_StubAdoption(existing=_RecordRow(2, "someone-elses-project")),
    )
    result2 = await service2.remove(_remove_request(stateful_v1))
    assert result2.reason == RemoveErrorCode.EXTERNAL_RECORD_CONFLICT.value
    assert target2.down_calls == 0


@pytest.mark.anyio
async def test_remove_policy_denial_consumes_nothing(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    # no app.remove grant at all (the key is absent from the scope set)
    service, approvals, _, _, _ = _service(target, stateful_v1, grants=lambda: {})
    result = await service.remove(_remove_request(stateful_v1))
    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == (
        f"{RemoveErrorCode.POLICY_DENIED.value}:capability_not_granted"
    )
    assert approvals.consume_calls == 0
    assert target.down_calls == 0

    # a grant key present with an EMPTY server scope is a scope mismatch
    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    service2, approvals2, _, _, _ = _service(
        target2, stateful_v1, grants=lambda: _grants(granted=False)
    )
    result2 = await service2.remove(_remove_request(stateful_v1))
    assert result2.reason == (
        f"{RemoveErrorCode.POLICY_DENIED.value}:server_scope_mismatch"
    )
    assert approvals2.consume_calls == 0

    # policy-level denial (deny takes precedence everywhere, C03 rule 5)
    target3 = _FakeTarget()
    _seed_deployed(target3, stateful_v1, APP_ID, data=True)
    service3, approvals3, _, _, _ = _service(
        target3,
        stateful_v1,
        policy=lambda: _policy(denied=frozenset({CAPABILITY_ID})),
    )
    result3 = await service3.remove(_remove_request(stateful_v1))
    assert result3.reason == (
        f"{RemoveErrorCode.POLICY_DENIED.value}:capability_denied_by_policy"
    )
    assert approvals3.consume_calls == 0


@pytest.mark.anyio
async def test_remove_approval_binding_denials(stateful_v1) -> None:
    """C03 rule 4: the one-use decision binds the plan hash; a stale
    recipe revision (different plan) or a spent decision is denied."""
    v2 = _recipe(STATEFUL_FILE, 1)
    target = _FakeTarget()
    _seed_deployed(target, v2, APP_ID, data=True)  # v2 actually deployed
    service, approvals, _, _, _ = _service(target, v2)
    # the approval was created for the v1 plan (stale recipe revision)
    stale = _StubApprovals(
        _DEFAULT_APPROVAL_ID,
        payload_hash=_plan_for(stateful_v1).plan_sha256,
    )
    service2, _, _, _, _ = _service(
        target, v2, approvals=stale, capability_id=CAPABILITY_ID
    )
    result = await service2.remove(_remove_request(v2, approval_id=_DEFAULT_APPROVAL_ID))
    assert result.status == "refused"
    assert result.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:payload_mismatch"
    )
    assert result.effect == "not_attempted"
    assert stale.consume_calls == 1
    assert target.down_calls == 0

    # repeating the request with the SAME (now consumed) approval is denied
    service3, approvals3, _, _, _ = _service(target, v2)
    first = await service3.remove(_remove_request(v2))
    assert first.status == "succeeded"
    second = await service3.remove(_remove_request(v2))
    assert second.status == "refused"
    assert second.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:already_consumed"
    )
    assert approvals3.consume_calls == 2
    assert target.down_calls == 1  # no second teardown effect


@pytest.mark.anyio
async def test_remove_lock_refusals(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    for code in (LockErrorCode.HELD, LockErrorCode.FENCED):
        locks = _StubLocks(refuse_acquire=code)
        service, _, _, _, _ = _service(target, stateful_v1, locks=locks)
        result = await service.remove(_remove_request(stateful_v1))
        assert result.status == "refused"
        assert result.reason == (
            f"{RemoveErrorCode.LOCK_UNAVAILABLE.value}:{code.value}"
        )
        assert result.effect == "not_attempted"
        assert target.down_calls == 0


@pytest.mark.anyio
async def test_remove_down_unknown_keeps_lease(stateful_v1) -> None:
    """C04 rules 5–7: a timeout cut during the teardown is
    outcome_unknown; the lease is left to lapse into a fence."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    target.fail["remove managed compose project"] = "unknown"
    service, approvals, locks, _, _ = _service(target, stateful_v1)

    result = await service.remove(_remove_request(stateful_v1))

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.reason == RemoveErrorCode.OUTCOME_UNKNOWN.value
    assert result.approval_id == _DEFAULT_APPROVAL_ID  # decision was spent
    assert result.lock_released is False
    assert locks.released == []
    plan = _plan_for(stateful_v1)
    assert plan.project_name in target.projects  # state unresolved, honest
    assert target.volume_data  # data untouched either way

    # a post-effect verification read that is unknown is equally honest
    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    target2.fail["verify managed compose project removal"] = "unknown"
    service2, _, locks2, _, _ = _service(target2, stateful_v1)
    result2 = await service2.remove(_remove_request(stateful_v1))
    assert result2.status == "outcome_unknown"
    assert result2.lock_released is False
    assert locks2.released == []


@pytest.mark.anyio
async def test_remove_state_read_unknown_keeps_approval(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    target.fail["read managed compose file hash"] = "unknown"
    service, approvals, locks, _, _ = _service(target, stateful_v1)

    result = await service.remove(_remove_request(stateful_v1))

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert approvals.consume_calls == 0  # nothing gated past a dead read
    assert locks.released == []


@pytest.mark.anyio
async def test_remove_down_failure_leaving_containers_fails(stateful_v1) -> None:
    """"Removed" is claimed only when observed (C04.6): a down that leaves
    the project running is a deterministic failure, not a success."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    target.down_leaves_containers = True
    service, approvals, locks, _, _ = _service(target, stateful_v1)

    result = await service.remove(_remove_request(stateful_v1))

    assert result.status == "failed"
    assert result.effect == "executed"  # the down effect really happened
    assert result.reason == RemoveErrorCode.APPLY_FAILED.value
    assert any("did not verify project removal" in note for note in result.notes)
    assert result.lock_released is True  # deterministic failure: safe to retry
    plan = _plan_for(stateful_v1)
    assert target.projects[plan.project_name]["running"] is True


@pytest.mark.anyio
async def test_remove_converges_when_project_already_gone(stateful_v1) -> None:
    """Compose answers "no such project" (rc 1) on down of an absent
    project: the VERIFIED absence is the removal's goal — succeed with
    the honest note, never a fake failure."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    service, _, _, adoption, _ = _service(
        target, stateful_v1, adoption=_StubAdoption(existing=_RecordRow(1, None))
    )

    result = await service.remove(_remove_request(stateful_v1))

    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert any("verified absent" in note for note in result.notes)
    assert result.manifest_removed is True
    assert target.down_calls == 1


@pytest.mark.anyio
async def test_remove_lease_lapse_aborts_the_apply(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    target.block_on = "remove managed compose project"
    locks = _StubLocks(lapse_heartbeats=True)
    service, _, _, adoption, _ = _service(
        target, stateful_v1, locks=locks, lease_ttl=1.0
    )
    import asyncio

    task = asyncio.ensure_future(service.remove(_remove_request(stateful_v1)))
    await asyncio.sleep(1.3)  # the lease lapses while the down is in flight
    target.unblock()
    result = await task
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert any("lapsed" in note for note in result.notes)
    assert result.lock_released is False
    assert target.down_calls == 1  # the effect ran, but nothing is claimed
    assert adoption.revoke_calls == []  # no canonical touch on fenced ground


@pytest.mark.anyio
async def test_remove_tombstone_recovery_then_honest_refusal(stateful_v1) -> None:
    """C02 rule 5/W4: an interrupted canonical deletion gets ONE recovery
    sweep + retry; a real conflict is recorded, never healed silently and
    never undoes the verified teardown."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    adoption = _StubAdoption(
        existing=_RecordRow(1, None),
        revoke_failures=(AdoptionErrorCode.STALE_REVISION,),
    )
    service, _, _, adoption, _ = _service(target, stateful_v1, adoption=adoption)

    result = await service.remove(_remove_request(stateful_v1))

    assert result.status == "succeeded"
    assert result.manifest_removed is True
    assert adoption.recover_calls == 1
    assert len(adoption.revoke_calls) == 2  # sweep + retry converged

    # a hard tombstone conflict: teardown stands, refusal recorded
    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    adoption2 = _StubAdoption(
        existing=_RecordRow(1, None),
        revoke_failures=(
            AdoptionErrorCode.TOMBSTONE_CONFLICT,
            AdoptionErrorCode.TOMBSTONE_CONFLICT,
        ),
    )
    service2, _, _, adoption2, _ = _service(target2, stateful_v1, adoption=adoption2)
    result2 = await service2.remove(_remove_request(stateful_v1))
    assert result2.status == "succeeded"  # the verified teardown stands
    assert result2.manifest_removed is False
    assert any(
        "manifest tombstone refused: tombstone_conflict" in note
        for note in result2.notes
    )

    # never recorded: the honest "nothing to tombstone" note
    target3 = _FakeTarget()
    _seed_deployed(target3, stateful_v1, APP_ID, data=True)
    service3, _, _, _, _ = _service(target3, stateful_v1)
    result3 = await service3.remove(_remove_request(stateful_v1))
    assert result3.status == "succeeded"
    assert result3.manifest_removed is False
    assert any("no canonical record" in note for note in result3.notes)


def test_remove_intent_id_is_deterministic_and_namespaced() -> None:
    plan_hash = "ab" * 32
    first = remove_intent_id(SERVER_ID, APP_ID, plan_hash)
    assert first == remove_intent_id(SERVER_ID, APP_ID, plan_hash)
    assert first != remove_intent_id(SERVER_ID, APP_ID, "cd" * 32)
    assert first != remove_intent_id(SERVER_ID, NEIGHBOR_APP_ID, plan_hash)
    # distinct canonical intent namespaces across the sibling cards
    assert len({
        first,
        install_intent_id(SERVER_ID, APP_ID, plan_hash),
        update_intent_id(SERVER_ID, APP_ID, plan_hash),
    }) == 3


# ---------------------------------------------------------------------------
# AC1 tier 2: the bound deletion preview and the separately authorized purge
# ---------------------------------------------------------------------------


def test_deletion_manifest_preview_is_deterministic_and_binding(
    stateful_v1,
) -> None:
    """The preview hashes the EXACT planner-allocated locations; any
    identity/revision change re-binds the approval payload (C03 rule 4)."""
    request = _purge_request(stateful_v1)
    service, _, _, _, _ = _service(_FakeTarget(), stateful_v1)
    manifest = service.deletion_manifest(request)
    assert isinstance(manifest, DeletionManifest)
    plan = _plan_for(stateful_v1)
    assert manifest.locations == tuple(
        sorted(v.host_path for v in plan.volumes)
    )
    assert manifest.locations == (f"/var/lib/mai/apps/{APP_ID}/volumes/srv-files",)
    assert manifest.manifest_sha256 == service.deletion_manifest(
        _purge_request(stateful_v1)
    ).manifest_sha256
    assert re.fullmatch(r"[0-9a-f]{64}", manifest.manifest_sha256)

    # a different application identity re-binds the manifest
    other = service.deletion_manifest(
        _purge_request(stateful_v1, application_id=NEIGHBOR_APP_ID)
    )
    assert other.locations != manifest.locations
    assert other.manifest_sha256 != manifest.manifest_sha256
    # a different recipe revision (v2 digest) re-binds through plan_sha256
    v2 = _recipe(STATEFUL_FILE, 1)
    revised = service.deletion_manifest(_purge_request(v2))
    assert revised.locations == manifest.locations  # same volume name/path
    assert revised.manifest_sha256 != manifest.manifest_sha256


@pytest.mark.anyio
async def test_purge_deletes_only_manifest_locations_after_verified_absence(
    stateful_v1,
) -> None:
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    manifest_hash = _manifest_hash(stateful_v1, APP_ID)
    service, approvals, locks, _, _ = _service(
        target,
        stateful_v1,
        capability_id=PURGE_CAPABILITY_ID,
        approvals=_StubApprovals(
            _DEFAULT_APPROVAL_ID,
            capability_id=PURGE_CAPABILITY_ID,
            payload_hash=manifest_hash,
        ),
    )

    result = await service.purge_data(
        _purge_request(stateful_v1, approval_id=_DEFAULT_APPROVAL_ID)
    )

    assert result.status == "succeeded"
    assert result.effect == "executed"
    assert result.verified is True
    assert result.locations == (plan.volumes[0].host_path,)
    assert result.approval_id == _DEFAULT_APPROVAL_ID
    assert result.lock_released is True
    assert target.rm_calls == [plan.volumes[0].host_path]
    assert plan.volumes[0].host_path not in target.volume_data
    assert plan.compose_path in target.files  # the record anchor is NOT data
    assert approvals.consume_calls == 1
    assert locks.acquired[0][1] == f"{LOCK_RESOURCE_PREFIX}{APP_ID}"


@pytest.mark.anyio
async def test_purge_is_idempotent_on_absent_locations(stateful_v1) -> None:
    """A re-run after an interrupted purge converges: absent locations are
    VERIFIED no-ops, never invented deletions."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    manifest_hash = _manifest_hash(stateful_v1, APP_ID)
    approvals = _StubApprovals(
        _DEFAULT_APPROVAL_ID,
        capability_id=PURGE_CAPABILITY_ID,
        payload_hash=manifest_hash,
    )
    service, _, _, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID, approvals=approvals
    )
    first = await service.purge_data(_purge_request(stateful_v1))
    assert first.status == "succeeded" and first.verified is True

    # a fresh one-use decision for the converged re-run
    second_id = uuid.uuid4()
    second_approvals = _StubApprovals(
        second_id,
        capability_id=PURGE_CAPABILITY_ID,
        payload_hash=manifest_hash,
    )
    service2, _, _, _, _ = _service(
        target,
        stateful_v1,
        capability_id=PURGE_CAPABILITY_ID,
        approvals=second_approvals,
    )
    second = await service2.purge_data(
        _purge_request(stateful_v1, approval_id=second_id)
    )
    assert second.status == "succeeded"
    assert second.verified is True
    assert [op.action for op in second.operations] == ["verified"]
    assert len(target.rm_calls) == 1  # the second run issued NO rm


@pytest.mark.anyio
async def test_purge_requires_explicit_delete_intent(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    service, approvals, _, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(
        _purge_request(stateful_v1, delete_data=False)
    )

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == RemoveErrorCode.DATA_INTENT_REQUIRED.value
    assert approvals.consume_calls == 0
    assert target.rm_calls == []
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_refuses_declared_keep_policy(stateful_v1) -> None:
    """A recipe whose DECLARED policy keeps data can never be purged —
    the explicit operator intent does not override the recipe (C09 rule 5)."""
    keep_stateful = _mutant_recipe(
        STATEFUL_FILE,
        lambda d: d["removal"].__setitem__("data_policy", "keep"),
    )
    target = _FakeTarget()
    _seed_removed(target, keep_stateful, APP_ID, data=True)
    plan = _plan_for(keep_stateful)
    service, approvals, _, _, _ = _service(
        target, keep_stateful, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(_purge_request(keep_stateful))

    assert result.status == "refused"
    assert result.reason == RemoveErrorCode.DATA_POLICY_KEEP.value
    assert result.effect == "not_attempted"
    assert result.locations == (plan.volumes[0].host_path,)
    assert approvals.consume_calls == 0
    assert target.rm_calls == []
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_refuses_without_data_locations(http_v1) -> None:
    target = _FakeTarget()
    locks = _StubLocks()
    service, approvals, locks, _, _ = _service(
        target, http_v1, capability_id=PURGE_CAPABILITY_ID, locks=locks
    )

    result = await service.purge_data(_purge_request(http_v1))

    assert result.status == "refused"
    assert result.reason == RemoveErrorCode.NO_DATA_LOCATIONS.value
    assert result.locations == ()
    assert approvals.consume_calls == 0
    assert locks.acquired == []  # refused before any lock/effect


@pytest.mark.anyio
async def test_purge_refuses_while_deployed(stateful_v1) -> None:
    """Data deletion requires the VERIFIED-absent deployment: tier 2
    refuses while the application still runs (never delete under a live
    service)."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    service, approvals, _, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(_purge_request(stateful_v1))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == RemoveErrorCode.APP_STILL_DEPLOYED.value
    assert approvals.consume_calls == 0
    assert target.rm_calls == []
    assert target.projects[plan.project_name]["running"] is True
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_stale_preview_payload_is_denied(stateful_v1) -> None:
    """Preview/effect drift is the altered-payload denial: the approval
    was created for the v1 manifest; the request now carries v2."""
    v2 = _recipe(STATEFUL_FILE, 1)
    target = _FakeTarget()
    _seed_removed(target, v2, APP_ID, data=True)
    stale_approvals = _StubApprovals(
        _DEFAULT_APPROVAL_ID,
        capability_id=PURGE_CAPABILITY_ID,
        payload_hash=_manifest_hash(stateful_v1, APP_ID),  # v1 manifest
    )
    service, _, _, _, _ = _service(
        target, v2, capability_id=PURGE_CAPABILITY_ID, approvals=stale_approvals
    )

    result = await service.purge_data(_purge_request(v2))

    assert result.status == "refused"
    assert result.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:payload_mismatch"
    )
    assert target.rm_calls == []
    plan = _plan_for(v2)
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_repeat_with_spent_approval_is_denied(stateful_v1) -> None:
    """The registry's repeat stale-approval denial: the second purge with
    the SAME one-use decision is refused and changes nothing."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    service, approvals, _, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID
    )
    request = _purge_request(stateful_v1)

    first = await service.purge_data(request)
    assert first.status == "succeeded"
    second = await service.purge_data(request)
    assert second.status == "refused"
    assert second.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:already_consumed"
    )
    assert second.effect == "not_attempted"
    assert approvals.consume_calls == 2
    assert len(target.rm_calls) == 1


@pytest.mark.anyio
async def test_purge_grants_are_separate_from_removal(stateful_v1) -> None:
    """Acceptance 1: data deletion carries its OWN grant — a principal
    granted app.remove but NOT app.remove-data is denied the purge."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    service, approvals, _, _, _ = _service(
        target,
        stateful_v1,
        capability_id=PURGE_CAPABILITY_ID,
        grants=lambda: {CAPABILITY_ID: frozenset({SERVER_ID})},  # purge absent
    )

    result = await service.purge_data(_purge_request(stateful_v1))

    assert result.status == "refused"
    assert result.effect == "not_attempted"
    assert result.reason == (
        f"{RemoveErrorCode.POLICY_DENIED.value}:capability_not_granted"
    )
    assert approvals.consume_calls == 0
    assert target.rm_calls == []
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_state_diverged_on_compose_drift(stateful_v1) -> None:
    """The deletion manifest binds to the DEPLOYED bytes: drift refuses
    (different bytes may mean different volume paths)."""
    v2 = _recipe(STATEFUL_FILE, 1)
    target = _FakeTarget()
    plan = _plan_for(v2)
    target.files[plan.compose_path] = (
        _plan_for(stateful_v1).compose_text.encode("utf-8")  # v1 bytes on disk
    )
    service, approvals, _, _, _ = _service(
        target, v2, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(_purge_request(v2))

    assert result.status == "refused"
    assert result.reason == RemoveErrorCode.STATE_DIVERGED.value
    assert result.effect == "not_attempted"
    assert approvals.consume_calls == 0
    assert target.rm_calls == []


@pytest.mark.anyio
async def test_purge_rm_unknown_keeps_lease(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    target.fail["delete application persistent data"] = "unknown"
    service, approvals, locks, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(_purge_request(stateful_v1))

    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert result.approval_id == _DEFAULT_APPROVAL_ID
    assert result.lock_released is False
    assert locks.released == []
    # the simulated cut landed before the backend ran: the receipt is the
    # honest record (unknown status, no exit), the data unverified
    assert [op.status for op in result.operations] == ["outcome_unknown"]
    assert result.operations[0].exit_code is None
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)


@pytest.mark.anyio
async def test_purge_unverifiable_deletion_fails(stateful_v1) -> None:
    """A rm that claims success while the data is still observed is a
    deterministic failure — "deleted" is claimed only when re-observed."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    target.rm_ineffective = True
    service, approvals, locks, _, _ = _service(
        target, stateful_v1, capability_id=PURGE_CAPABILITY_ID
    )

    result = await service.purge_data(_purge_request(stateful_v1))

    assert result.status == "failed"
    assert result.effect == "executed"
    assert result.verified is False
    assert result.reason == RemoveErrorCode.APPLY_FAILED.value
    assert any("could not be verified" in note for note in result.notes)
    assert result.lock_released is True
    assert target.volume_data[plan.volumes[0].host_path] == (SENTINEL_NAME,)
    assert approvals.consume_calls == 1


@pytest.mark.anyio
async def test_purge_lease_lapse_aborts_the_apply(stateful_v1) -> None:
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    plan = _plan_for(stateful_v1)
    target.block_on = "delete application persistent data"
    locks = _StubLocks(lapse_heartbeats=True)
    service, _, _, _, _ = _service(
        target,
        stateful_v1,
        capability_id=PURGE_CAPABILITY_ID,
        locks=locks,
        lease_ttl=1.0,
    )
    import asyncio

    task = asyncio.ensure_future(service.purge_data(_purge_request(stateful_v1)))
    await asyncio.sleep(1.3)  # the lease lapses while the rm is in flight
    target.unblock()
    result = await task
    assert result.status == "outcome_unknown"
    assert result.effect == "unknown"
    assert any("lapsed" in note for note in result.notes)
    assert result.lock_released is False
    assert target.rm_calls == [plan.volumes[0].host_path]  # ran, unclaimed
    # the rm DID complete after the unblock — but the lapsed lease means
    # the executor claims nothing and the engine must reconcile
    assert plan.volumes[0].host_path not in target.volume_data


@pytest.mark.anyio
async def test_no_sentinel_payload_leaks_into_receipts_or_commands(
    stateful_v1,
) -> None:
    """Value-free discipline: the planted data payload is never a receipt
    field, note, display string or command argument."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, APP_ID, data=True)
    service, _, _, _, _ = _service(target, stateful_v1, capability_id=PURGE_CAPABILITY_ID)
    purge_result = await service.purge_data(_purge_request(stateful_v1))
    assert purge_result.status == "succeeded"

    target2 = _FakeTarget()
    _seed_deployed(target2, stateful_v1, APP_ID, data=True)
    service2, _, _, _, _ = _service(target2, stateful_v1)
    remove_result = await service2.remove(_remove_request(stateful_v1))
    assert remove_result.status == "succeeded"

    for result in (purge_result, remove_result):
        rendered = repr(result) + "".join(repr(op) for op in result.operations)
        assert SENTINEL_PAYLOAD.strip() not in rendered
    for request in target.calls + target2.calls:
        assert SENTINEL_PAYLOAD.strip() not in request.display
        if request.argv:
            assert SENTINEL_PAYLOAD.strip() not in " ".join(request.argv)


# ---------------------------------------------------------------------------
# Live-DB section: REAL Postgres-backed canonical pipeline + approvals/locks
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
            f"{LIVE_URL_VAR} is not set; the remove integration claims need "
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
def _clean_live_leases(migrated_url: str):
    if psycopg is None:  # pragma: no cover - migrated_url already skipped
        pytest.skip("psycopg unavailable")
    # The lock table is producer-created (ensure_schema), not a migration:
    # guarantee it before the DELETE (fixture order independence).
    ResourceLockService(migrated_url).ensure_schema()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_resource_locks WHERE server_id = ANY(%s)",
            ([LIVE_SERVER_ID],),
        )
        conn.commit()
    yield


def _live_service(
    target: _FakeTarget, migrated_url: str, sessions, store, locks
) -> tuple[RemoveService, ApprovalStore]:
    approvals = ApprovalStore(sessions)
    service = RemoveService(
        transport=target,
        locks=locks,
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: _grants(LIVE_SERVER_ID),
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        descriptor=_descriptor(),
        purge_descriptor=_purge_descriptor(),
    )
    return service, approvals


def _approval_request(
    capability_id: str, payload_hash: str, server_id: uuid.UUID
) -> ApprovalRequest:
    return ApprovalRequest(
        capability_id=capability_id,
        server_id=server_id,
        payload_hash=payload_hash,
        policy_revision=POLICY_REVISION,
        created_by=PRINCIPAL,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _seed_live_adoption(
    sessions, store: ApplicationManifestStore, recipe: ApplicationRecipe
):
    """Create the canonical record through the REAL adoption pipeline
    (the state a real install leaves behind)."""
    from mai.applications.discover import ApplicationOrigin

    adoption = ApplicationAdoption(sessions)
    document = AdoptionRequest(
        server_id=LIVE_SERVER_ID,
        application_id=LIVE_APP_ID,
        name=recipe.name,
        origin=ApplicationOrigin.COMPOSE,
        compose_project=project_name(LIVE_APP_ID),
        working_dir=f"/var/lib/mai/apps/{LIVE_APP_ID}",
        expected_revision=0,
    )
    outcome = await adoption.adopt(store, document)
    return adoption, outcome


def _manifest_path(store: ApplicationManifestStore, sid: uuid.UUID, aid: uuid.UUID) -> Path:
    return store.root / "servers" / str(sid) / "applications" / str(aid) / "manifest.yaml"


@pytest.mark.anyio
async def test_live_remove_tombstones_real_manifest_and_projection(
    _clean_live_leases, migrated_url, sessions, file_store, stateful_v1
) -> None:
    """REAL canonical pipeline: the removal's tombstone deletes the
    manifest file AND the projection row and leaves NO live intent."""
    target = _FakeTarget()
    _seed_deployed(target, stateful_v1, LIVE_APP_ID, data=True)
    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    adoption, created = await _seed_live_adoption(
        sessions, file_store, stateful_v1
    )
    assert created.revision == 1
    assert _manifest_path(file_store, LIVE_SERVER_ID, LIVE_APP_ID).is_file()

    service, approvals = _live_service(
        target, migrated_url, sessions, file_store, locks
    )
    plan = _plan_for(stateful_v1, LIVE_APP_ID, LIVE_SERVER_ID)
    approval = await approvals.create(
        _approval_request(CAPABILITY_ID, plan.plan_sha256, LIVE_SERVER_ID)
    )

    result = await service.remove(
        _remove_request(
            stateful_v1,
            approval_id=approval.approval_id,
            application_id=LIVE_APP_ID,
            server_id=LIVE_SERVER_ID,
        )
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.manifest_removed is True
    assert result.manifest_revision == 1
    assert result.data_deleted is False
    assert result.lock_released is True
    # REAL tombstone: file gone, projection row gone, no live intents.
    assert not _manifest_path(file_store, LIVE_SERVER_ID, LIVE_APP_ID).exists()
    assert await adoption.read(LIVE_SERVER_ID, LIVE_APP_ID) is None
    assert await adoption.list_live_intents(server_id=LIVE_SERVER_ID) == []
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"
    assert locks.inspect(str(LIVE_SERVER_ID), f"{LOCK_RESOURCE_PREFIX}{LIVE_APP_ID}") is None

    # Idempotent canonical convergence: a retry of the SAME removal finds
    # the deterministic intent already reconciled — no resurrected record,
    # no duplicate tombstone (C02 rule 5 resume semantics).
    second = await approvals.create(
        _approval_request(CAPABILITY_ID, plan.plan_sha256, LIVE_SERVER_ID)
    )
    retry = await service.remove(
        _remove_request(
            stateful_v1,
            approval_id=second.approval_id,
            application_id=LIVE_APP_ID,
            server_id=LIVE_SERVER_ID,
        )
    )
    assert retry.status == "succeeded"
    assert retry.manifest_removed is True
    assert retry.manifest_revision is None  # nothing left to consume
    assert not _manifest_path(file_store, LIVE_SERVER_ID, LIVE_APP_ID).exists()
    assert await adoption.read(LIVE_SERVER_ID, LIVE_APP_ID) is None
    assert await adoption.list_live_intents(server_id=LIVE_SERVER_ID) == []


@pytest.mark.anyio
async def test_live_purge_with_real_stores_denies_spent_approval(
    _clean_live_leases, migrated_url, sessions, file_store, stateful_v1
) -> None:
    """REAL approvals + locks: the purge consumes its manifest-bound
    decision; repeating it with the SAME decision is denied and deletes
    nothing (the registry's repeat stale-approval denial)."""
    target = _FakeTarget()
    _seed_removed(target, stateful_v1, LIVE_APP_ID, data=True)
    plan = _plan_for(stateful_v1, LIVE_APP_ID, LIVE_SERVER_ID)
    locks = ResourceLockService(migrated_url)
    locks.ensure_schema()
    service, approvals = _live_service(
        target, migrated_url, sessions, file_store, locks
    )
    manifest = service.deletion_manifest(
        _purge_request(
            stateful_v1,
            application_id=LIVE_APP_ID,
            server_id=LIVE_SERVER_ID,
        )
    )
    assert manifest.locations == (f"/var/lib/mai/apps/{LIVE_APP_ID}/volumes/srv-files",)
    approval = await approvals.create(
        _approval_request(PURGE_CAPABILITY_ID, manifest.manifest_sha256, LIVE_SERVER_ID)
    )
    assert (await approvals.get(approval.approval_id)).status == "pending"

    request = _purge_request(
        stateful_v1,
        approval_id=approval.approval_id,
        application_id=LIVE_APP_ID,
        server_id=LIVE_SERVER_ID,
    )
    result = await service.purge_data(request)

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.verified is True
    assert plan.volumes[0].host_path not in target.volume_data
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"

    repeat = await service.purge_data(request)
    assert repeat.status == "refused"
    assert repeat.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:already_consumed"
    )
    assert repeat.effect == "not_attempted"
    assert len(target.rm_calls) == 1


# ---------------------------------------------------------------------------
# Real-boundary section: disposable Arch sshd + docker-socket fixture
# (``mai-fxt-08-rem-*`` namespace, mirroring the 08-install/08-update
# fixtures). The executor drives the REAL ``docker compose`` CLI on the
# HOST daemon through the REAL P04 SSH transport, and the canonical
# manifest/projection commits go through the REAL Postgres stores above.
# Skipped without a working docker daemon; the DB fixtures skip themselves
# without MAI_TEST_DATABASE_URL.
#
# Fixture extension (what makes keep-data vs purge-data REAL here): the
# planner-allocated ``/var/lib/mai/apps`` subtree is bind-mounted into the
# fixture at start (the source resolves DAEMON-side, wherever the daemon
# lives). The SSH-target view of the persistent-data locations is
# therefore the SAME directory tree the daemon binds into the application
# containers — the sentinel planted through the running container is
# observed over SSH AND through an independent daemon-side container
# mount, retained by the keep-policy removal, and REALLY deleted by the
# executor's ``rm -rf`` (verified from both views). Recorded honestly in
# the evidence: the mounted subtree is the fixture's owned scratch under
# /var/lib/mai/apps/<owned application ids> only.
#
# Secret hygiene: the fixture key is a throwaway in /tmp/mai-fxt-08-rem-*
# deleted with the fixture; the sentinel is a synthetic non-secret.
#
# Teardown contract: containers, compose projects (containers + networks,
# including leftovers of a crashed predecessor run under the same fixed
# identities), the daemon-side app dirs of the owned identities, the
# scratch dirs AND the fixture image are removed, with the proof string
# printed to the record.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-08-rem-sshd"
FIXTURE_IMAGE_TAG = "mai-fxt-08-rem-sshd:latest"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-08-rem")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_08_REM_FIXTURE"

#: Fixed identity pairs, one per real test, so target dirs, compose
#: projects, locks and canonical records never collide across tests (and
#: never with the 08-install/08-update fixture identities).
REAL_SERVER_G = uuid.UUID("080a080a-080a-480a-880a-080a080a00a7")
REAL_SERVER_H = uuid.UUID("080a080a-080a-480a-880a-080a080a00b8")
REAL_SERVER_I = uuid.UUID("080a080a-080a-480a-880a-080a080a00c9")
REAL_SERVER_J = uuid.UUID("080a080a-080a-480a-880a-080a080a00da")
REAL_APP_G = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e00a7")
REAL_APP_H = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e00b8")
REAL_APP_I = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e00c9")
REAL_APP_J = uuid.UUID("0c0e0c0e-0c0e-4c0e-8c0e-0c0e0c0e00da")

#: Published host ports: distinct per real test so concurrently-live
#: deployments never trip the honest port pre-flight.
REAL_PORT_G = 18087
REAL_PORT_H = 18088
REAL_PORT_I = 18089
REAL_PORT_J = 18090


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


def _volume_host_path(application_id: uuid.UUID, name: str) -> str:
    return f"/var/lib/mai/apps/{application_id}/volumes/{name}"


class RemoveFixture:
    """Arch sshd + docker-CLI container on the HOST network namespace,
    with the planner-allocated /var/lib/mai/apps subtree bound in so the
    SSH-target volume view == the daemon bind-source view (module doc)."""

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
            # The remove card's data-realism mount: the bind SOURCE is
            # resolved daemon-side, so this exposes the daemon's
            # /var/lib/mai/apps tree at the same path inside the fixture.
            "-v", "/var/lib/mai/apps:/var/lib/mai/apps",
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
            "# openssh: the target sshd. docker: the CLI only — the daemon\n"
            "# is the HOST's, reached through the mounted socket.\n"
            "# docker-compose: the compose plugin (symlinked as a CLI\n"
            "# plugin). curl + iproute2: the 08-install health gates the\n"
            "# removal tests install through first.\n"
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
        fixed identities ⇒ same deterministic project names) and the
        daemon-side app dirs of the owned identities."""
        for app_id in (REAL_APP_G, REAL_APP_H, REAL_APP_I, REAL_APP_J):
            for cid in _project_container_ids(project_name(app_id)):
                _docker("rm", "-f", cid, timeout=60)
            for nid in _project_network_ids(project_name(app_id)):
                _docker("network", "rm", nid)
            RemoveFixture.daemon_rm_path(f"/var/lib/mai/apps/{app_id}")

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

    def compose_file_present(self, compose_path: str) -> bool:
        out = self.exec_sh(f"test -f {compose_path}")
        assert out.returncode in (0, 1), f"exec probe failed: {out.stderr[-300:]}"
        return out.returncode == 0

    def ssh_listing(self, path: str) -> str | None:
        """`ls -A` through the SSH target view; None when absent."""
        out = self.exec_sh(f"ls -A -- {path}")
        assert out.returncode in (0, 2), f"ls probe failed: {out.stderr[-300:]}"
        return out.stdout if out.returncode == 0 else None

    def plant_sentinel(self, container_id: str, payload: str) -> None:
        """Plant the sentinel through the RUNNING container's bind (the
        data lands in the daemon-side bind source = the shared tree)."""
        out = _docker(
            "exec", container_id, "sh", "-c",
            f"printf '%s' '{payload}' > /srv/{SENTINEL_NAME}",
            timeout=30,
        )
        assert out.returncode == 0, f"sentinel plant failed: {out.stderr[-400:]}"

    @staticmethod
    def daemon_probe(path: str, script: str) -> subprocess.CompletedProcess[str]:
        """Observe a persistent-data location from an INDEPENDENT
        daemon-side container mounting the same bind source."""
        return _docker(
            "run", "--rm", "-v", f"{path}:/probe", FIXTURE_IMAGE_TAG,
            "sh", "-c", script, timeout=120,
        )

    @staticmethod
    def daemon_rm_path(path: str) -> None:
        """Delete one daemon-side path (fixture-owned identity dir only)."""
        parent, _, name = path.rpartition("/")
        _docker(
            "run", "--rm", "-v", f"{parent}:/purge", FIXTURE_IMAGE_TAG,
            "rm", "-rf", "--", f"/purge/{name}", timeout=120,
        )

    def cleanup(self) -> str:
        parts: list[str] = []
        projects = self._projects | {
            project_name(a) for a in (REAL_APP_G, REAL_APP_H, REAL_APP_I, REAL_APP_J)
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
        for app_id in (REAL_APP_G, REAL_APP_H, REAL_APP_I, REAL_APP_J):
            self.daemon_rm_path(f"/var/lib/mai/apps/{app_id}")
        parts.append("daemon-side app dirs of owned identities removed")
        rm = _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        parts.append(f"fixture container rm rc={rm.returncode}")
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$",
            "--format", "{{.Names}}",
        )
        parts.append(f"fixture container after rm: '{listing.stdout.strip() or '<none>'}'")
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        parts.append(f"scratch {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}")
        if os.environ.get("MAI_KEEP_08_REM_FIXTURE_IMAGE"):
            parts.append("fixture image KEPT (MAI_KEEP_08_REM_FIXTURE_IMAGE set)")
        else:
            rmi = _docker("rmi", "-f", FIXTURE_IMAGE_TAG, timeout=120)
            parts.append(f"fixture image rmi rc={rmi.returncode}")
        self.cleanup_proof = "; ".join(parts)
        return self.cleanup_proof


@pytest.fixture(scope="module")
def remove_fixture() -> object:
    info = _docker("info", timeout=60)
    if info.returncode != 0:
        pytest.skip("docker unavailable: real-boundary remove tests require it")
    fixture = RemoveFixture()
    fixture.start()
    yield fixture
    print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture
def _clean_real_leases(migrated_url: str):
    """A killed worker's lease lives up to its TTL in the disposable DB,
    which outlives one pytest invocation; the fixed real identities must
    start each run unfenced and unheld."""
    import psycopg

    # Producer-created table (not a migration): guarantee it first.
    ResourceLockService(migrated_url).ensure_schema()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_resource_locks WHERE server_id = ANY(%s)",
            ([REAL_SERVER_G, REAL_SERVER_H, REAL_SERVER_I, REAL_SERVER_J],),
        )
        conn.commit()
    yield


# -- real-boundary wiring -----------------------------------------------------


def _real_transport(fixture: RemoveFixture):
    key_path, known_hosts = fixture.enroll()
    assert fixture.identity is not None
    return remove_module.ssh_transport(
        fixture.identity, key_path=key_path, known_hosts_path=known_hosts
    )


def _real_remove_service(
    *,
    transport,
    migrated_url: str,
    sessions,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
) -> tuple[RemoveService, ApprovalStore]:
    approvals = ApprovalStore(sessions)
    service = RemoveService(
        transport=transport,
        locks=ResourceLockService(migrated_url),
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: _grants(server_id),
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        descriptor=_descriptor(),
        purge_descriptor=_purge_descriptor(),
    )
    return service, approvals


def _real_install_service(
    *, transport, migrated_url: str, sessions, store, server_id: uuid.UUID
):
    from mai.applications.install import InstallService

    approvals = ApprovalStore(sessions)
    service = InstallService(
        transport=transport,
        locks=ResourceLockService(migrated_url),
        approvals=approvals,
        adoption=ApplicationAdoption(sessions),
        store=store,
        grants=lambda: {"app.install": frozenset({server_id})},
        policy=lambda: _policy(),
        adopted=lambda _sid: True,
        credentials=lambda: EnvResolver(_env_file({})),
        descriptor=_descriptor(identifier="app.install"),
    )
    return service, approvals


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


def _port_mutant(recipe: ApplicationRecipe, port: int) -> ApplicationRecipe:
    document = yaml.safe_load(render_recipe(recipe))
    document["deploy"]["ports"][0]["host"] = port
    return parse_recipe_text(yaml.safe_dump(document))


async def _install_real(
    recipe: ApplicationRecipe,
    sid: uuid.UUID,
    aid: uuid.UUID,
    *,
    transport,
    migrated_url: str,
    sessions,
    store,
) -> None:
    from mai.applications.install import InstallRequest

    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    install_service, install_approvals = _real_install_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=store, server_id=sid,
    )
    approval = await install_approvals.create(
        _approval_request("app.install", plan.plan_sha256, sid)
    )
    installed = await install_service.install(
        InstallRequest(
            server_id=sid,
            application_id=aid,
            recipe=recipe,
            target=TARGET,
            context=_context(server_id=sid, capability="app.install"),
            approval_id=approval.approval_id,
        )
    )
    assert installed.status == "succeeded", (installed.reason, installed.notes)
    assert installed.manifest_revision == 1


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_keep_data_removal_retains_sentinel_and_neighbor(
    remove_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1,
) -> None:
    """(a) LIVE keep-data removal over REAL sshd + REAL docker daemon:
    the planted sentinel REALLY survives (SSH view AND independent
    daemon-side mount), the plan's containers/networks are gone, the
    canonical manifest/projection are tombstoned, and the neighboring
    application (container, sentinel, canonical record) is unaffected."""
    fixture: RemoveFixture = remove_fixture
    sid, aid = REAL_SERVER_G, REAL_APP_G
    neighbor_sid, neighbor_aid = REAL_SERVER_H, REAL_APP_H
    recipe = _port_mutant(stateful_v1, REAL_PORT_G)
    neighbor_recipe = _port_mutant(stateful_v1, REAL_PORT_H)
    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    neighbor_plan = build_deployment_plan(neighbor_recipe, neighbor_sid, neighbor_aid, TARGET)
    fixture.track(plan.project_name)
    fixture.track(neighbor_plan.project_name)
    volume_path = _volume_host_path(aid, "srv-files")
    neighbor_volume_path = _volume_host_path(neighbor_aid, "srv-files")

    transport = _real_transport(fixture)
    await _install_real(
        recipe, sid, aid,
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store,
    )
    await _install_real(
        neighbor_recipe, neighbor_sid, neighbor_aid,
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store,
    )

    # Plant sentinels through the RUNNING containers (bind source view).
    containers = _project_container_ids(plan.project_name)
    assert len(containers) == 1
    fixture.plant_sentinel(containers[0], SENTINEL_PAYLOAD)
    neighbor_containers = _project_container_ids(neighbor_plan.project_name)
    assert len(neighbor_containers) == 1
    fixture.plant_sentinel(neighbor_containers[0], "neighbor-payload\n")

    # Topology proof: the SSH-target view of the volume path IS the bind
    # source (the mounted subtree) — the executor's rm will hit REAL data.
    listing = fixture.ssh_listing(volume_path)
    assert listing is not None and SENTINEL_NAME in listing
    neighbor_listing = fixture.ssh_listing(neighbor_volume_path)
    assert neighbor_listing is not None and SENTINEL_NAME in neighbor_listing

    service, approvals = _real_remove_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    approval = await approvals.create(
        _approval_request(CAPABILITY_ID, plan.plan_sha256, sid)
    )
    result = await service.remove(
        _remove_request(
            recipe, approval_id=approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.data_intent == "keep"
    assert result.data_deleted is False
    assert result.manifest_removed is True
    assert result.manifest_revision == 1
    assert result.lock_released is True
    assert [op.op for op in result.operations] == ["compose_down"]

    # Daemon truth: the plan's containers AND network are gone; the
    # neighbor still runs healthy.
    assert _project_container_ids(plan.project_name) == []
    assert _project_network_ids(plan.project_name) == []
    neighbor_after = _project_container_ids(neighbor_plan.project_name)
    assert len(neighbor_after) == 1
    assert _container_health(neighbor_after[0]) == "healthy"

    # Keep-data REALLY kept: sentinel observed over SSH and through an
    # independent daemon-side mount, byte-identical.
    ssh_after = fixture.ssh_listing(volume_path)
    assert ssh_after is not None and SENTINEL_NAME in ssh_after
    cat = fixture.exec_sh(f"cat {volume_path}/{SENTINEL_NAME}")
    assert cat.returncode == 0 and cat.stdout == SENTINEL_PAYLOAD
    probe = fixture.daemon_probe(
        volume_path, f"cat /probe/{SENTINEL_NAME}"
    )
    assert probe.returncode == 0 and probe.stdout == SENTINEL_PAYLOAD

    # Canonical record: G tombstoned (file + projection + no intents);
    # neighbor's record untouched at revision 1.
    assert not (
        file_store.root / "servers" / str(sid) / "applications" / str(aid)
        / "manifest.yaml"
    ).exists()
    adoption = ApplicationAdoption(sessions)
    assert await adoption.read(sid, aid) is None
    assert await adoption.list_live_intents(server_id=sid) == []
    neighbor_record = await adoption.read(neighbor_sid, neighbor_aid)
    assert neighbor_record is not None and neighbor_record.revision == 1
    # neighbor sentinel untouched (daemon-side read)
    neighbor_cat = fixture.exec_sh(
        f"cat {neighbor_volume_path}/{SENTINEL_NAME}"
    )
    assert neighbor_cat.returncode == 0
    assert neighbor_cat.stdout == "neighbor-payload\n"
    # the retained compose file still anchors the plan path
    assert fixture.compose_file_present(plan.compose_path)
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_purge_data_really_deletes_then_denies_stale_approval(
    remove_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1,
) -> None:
    """(b) The separately authorized delete-data operation REALLY deletes
    the persistent data (SSH view AND independent daemon-side mount), and
    repeating it with the SAME (spent) approval is denied — the registry's
    repeat stale-approval denial."""
    fixture: RemoveFixture = remove_fixture
    sid, aid = REAL_SERVER_I, REAL_APP_I
    recipe = _port_mutant(stateful_v1, REAL_PORT_I)
    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    fixture.track(plan.project_name)
    volume_path = _volume_host_path(aid, "srv-files")

    transport = _real_transport(fixture)
    await _install_real(
        recipe, sid, aid,
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store,
    )
    containers = _project_container_ids(plan.project_name)
    assert len(containers) == 1
    fixture.plant_sentinel(containers[0], SENTINEL_PAYLOAD)
    assert SENTINEL_NAME in (fixture.ssh_listing(volume_path) or "")

    service, approvals = _real_remove_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    remove_approval = await approvals.create(
        _approval_request(CAPABILITY_ID, plan.plan_sha256, sid)
    )
    removed = await service.remove(
        _remove_request(
            recipe, approval_id=remove_approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )
    assert removed.status == "succeeded", (removed.reason, removed.notes)

    # The bound deletion preview, then the SEPARATELY created approval.
    purge_request = _purge_request(
        recipe, application_id=aid, server_id=sid
    )
    manifest = service.deletion_manifest(purge_request)
    assert manifest.locations == (volume_path,)
    purge_approval = await approvals.create(
        _approval_request(PURGE_CAPABILITY_ID, manifest.manifest_sha256, sid)
    )

    result = await service.purge_data(
        _purge_request(
            recipe, approval_id=purge_approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )

    assert result.status == "succeeded", (result.reason, result.notes)
    assert result.effect == "executed"
    assert result.verified is True
    assert result.locations == (volume_path,)
    # Data REALLY gone from the SSH view (asserted BEFORE the daemon-side
    # probe: a `docker run -v` on a missing path auto-creates it empty).
    assert fixture.ssh_listing(volume_path) is None
    # Daemon-side truth from an independent container mounting the same
    # bind source the executor's rm deleted through SSH.
    probe = fixture.daemon_probe(volume_path, "ls -A /probe")
    assert probe.returncode == 0 and probe.stdout.strip() == ""
    spent = await approvals.get(purge_approval.approval_id)
    assert spent is not None and spent.status == "consumed"

    # Repeat stale-approval denial: same request, same (spent) decision.
    repeat = await service.purge_data(
        _purge_request(
            recipe, approval_id=purge_approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )
    assert repeat.status == "refused"
    assert repeat.reason == (
        f"{RemoveErrorCode.APPROVAL_REJECTED.value}:already_consumed"
    )
    assert repeat.effect == "not_attempted"
    # nothing resurrected (the dir the probe auto-created stays empty)
    assert fixture.ssh_listing(volume_path) == ""


@pytest.mark.real_target
@pytest.mark.anyio
async def test_real_fixture_refuses_purge_while_deployed_and_diverged_removal(
    remove_fixture, _clean_real_leases, migrated_url, sessions, file_store,
    stateful_v1,
) -> None:
    """(c) Real safety boundaries: the purge refuses while the deployment
    still runs (sentinel + container untouched, approval unspent), and a
    removal with drifted target bytes refuses state_diverged BEFORE any
    effect (the deployment keeps running)."""
    fixture: RemoveFixture = remove_fixture
    sid, aid = REAL_SERVER_J, REAL_APP_J
    recipe = _port_mutant(stateful_v1, REAL_PORT_J)
    plan = build_deployment_plan(recipe, sid, aid, TARGET)
    fixture.track(plan.project_name)
    volume_path = _volume_host_path(aid, "srv-files")

    transport = _real_transport(fixture)
    await _install_real(
        recipe, sid, aid,
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store,
    )
    containers = _project_container_ids(plan.project_name)
    assert len(containers) == 1
    fixture.plant_sentinel(containers[0], SENTINEL_PAYLOAD)

    service, approvals = _real_remove_service(
        transport=transport, migrated_url=migrated_url, sessions=sessions,
        store=file_store, server_id=sid,
    )
    manifest = service.deletion_manifest(
        _purge_request(recipe, application_id=aid, server_id=sid)
    )
    purge_approval = await approvals.create(
        _approval_request(PURGE_CAPABILITY_ID, manifest.manifest_sha256, sid)
    )
    early_purge = await service.purge_data(
        _purge_request(
            recipe, approval_id=purge_approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )
    assert early_purge.status == "refused"
    assert early_purge.reason == RemoveErrorCode.APP_STILL_DEPLOYED.value
    assert early_purge.effect == "not_attempted"
    unspent = await approvals.get(purge_approval.approval_id)
    assert unspent is not None and unspent.status == "pending"
    assert _container_health(_project_container_ids(plan.project_name)[0]) == "healthy"
    assert SENTINEL_NAME in (fixture.ssh_listing(volume_path) or "")

    # Drift the deployed compose bytes on the target (real mutation).
    drift = fixture.exec_sh(f"printf '# drifted\\n' >> {plan.compose_path}")
    assert drift.returncode == 0
    remove_approval = await approvals.create(
        _approval_request(CAPABILITY_ID, plan.plan_sha256, sid)
    )
    diverged = await service.remove(
        _remove_request(
            recipe, approval_id=remove_approval.approval_id,
            application_id=aid, server_id=sid,
        )
    )
    assert diverged.status == "refused"
    assert diverged.reason == RemoveErrorCode.STATE_DIVERGED.value
    assert diverged.effect == "not_attempted"
    assert any("hash differs" in note for note in diverged.notes)
    remove_spent = await approvals.get(remove_approval.approval_id)
    assert remove_spent is not None and remove_spent.status == "pending"
    # The deployment is UNTOUCHED by the refusal.
    still = _project_container_ids(plan.project_name)
    assert len(still) == 1
    assert _container_health(still[0]) == "healthy"
    assert SENTINEL_NAME in (fixture.ssh_listing(volume_path) or "")
