"""Focused tests for resource locks (card 05-resource-locks).

Two halves, mirroring the sibling suites:

- **Unit half** (no database): value-free validation, the exact
  reconciliation-verdict fence gate, canonical multi-acquire ordering
  with reverse rollback — the deadlock-freedom and envelope discipline.

- **Live half** (real disposable Postgres, ``MAI_TEST_DATABASE_URL``):
  exclusive acquisition, ownership-checked heartbeat/release (owner AND
  resource), lease-lapse FENCING instead of stealing, controller-boot
  restart identity, all-or-nothing batches — and the brief's feature
  scenario: two REAL processes contending for one target mutation while
  an independent resource proceeds, the owner killed while its effect is
  possibly live, the successor FENCED until a terminal reconciliation
  verdict clears the fence, then exactly one replacement effect.

The reconciliation verdicts used to clear fences here stand in for the
host-verified terminal outcomes that ``04-reconcile`` produces; the
host-side verification itself is not re-proven by this card (recorded
limitation, mirrored in the evidence).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from mai.execution.reconcile import ReconciliationClass
from mai.jobs.resource_locks import (
    CLEARABLE_VERDICTS,
    DEFAULT_LEASE_TTL,
    FENCE_REASON_LEASE_EXPIRED,
    LOCK_TABLE,
    LockErrorCode,
    LockLease,
    LockRequest,
    ResourceLockError,
    ResourceLockService,
    fence_may_be_cleared,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live scenario gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-05-resource-locks")

#: The controller boot identities pinned for this test run: one holder
#: boot, one contender/restart boot. Restart identity is the contrast.
BOOT_ID = str(uuid.uuid4())
OTHER_BOOT_ID = str(uuid.uuid4())

#: The expired-lease wait is bounded well above the shortest test TTLs.
EXPIRY_TIMEOUT = 15.0
READY_TIMEOUT = 15.0


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _expired(service: ResourceLockService, server_id: str, resource: str) -> bool:
    snapshot = service.inspect(server_id, resource)
    return snapshot is not None and snapshot.expired


# ---------------------------------------------------------------------------
# role script: REAL processes for the LOCK-FENCE scenario
# ---------------------------------------------------------------------------

ROLE_SCRIPT_NAME = "lock_scenario_worker.py"

ROLE_SCRIPT_SOURCE = '''
"""Role processes for the 05-resource-locks LOCK-FENCE fixture.

Every role journals one timestamped line per observable step; the journal
is the contention timeline the test (and the evidence) asserts against.
The database URL arrives via the environment, never argv.
"""

import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.environ["MAI_SRC_PATH"])

from mai.jobs.resource_locks import LockLease, ResourceLockError, ResourceLockService


def journal(path: str, marker: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with open(path, "a") as handle:
        handle.write(f"{now.replace('+00:00', 'Z')} {marker}\\n")


def _lease_from(payload: dict) -> LockLease:
    return LockLease(
        server_id=payload["server_id"],
        resource=payload["resource"],
        owner_token=payload["owner_token"],
        boot_id=payload["boot_id"],
        owner_ref=payload["owner_ref"],
        lease_ttl=payload["lease_ttl"],
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        acquired_at=datetime.fromisoformat(payload["acquired_at"]),
    )


def hold(argv: list[str]) -> int:
    """``hold <server> <resource> <journal> <ttl> <seconds> <export|->``

    Acquire, journal the (possibly-live) effect begin, export the lease,
    keep it alive by heartbeat. The test either kills this process
    mid-hold (the effect never completes) or lets it finish so another
    process can operate the exported token first.
    """
    _, server_id, resource, journal_path, ttl, seconds, export_path = argv
    service = ResourceLockService(os.environ["MAI_DATABASE_URL"])
    lease = service.acquire(server_id, resource, owner_ref="fixture-owner", ttl=float(ttl))
    journal(journal_path, "owner:acquired")
    journal(journal_path, "owner:effect_begin")
    if export_path != "-":
        payload = dataclasses.asdict(lease)
        payload["expires_at"] = lease.expires_at.isoformat()
        payload["acquired_at"] = lease.acquired_at.isoformat()
        with open(export_path, "w") as handle:
            json.dump(payload, handle)
    deadline = time.monotonic() + float(seconds)
    while time.monotonic() < deadline:
        time.sleep(float(ttl) / 4.0)
        if not service.heartbeat(lease):
            # A lost heartbeat is the signal to stop issuing further work.
            journal(journal_path, "owner:heartbeat_lost")
            break
    try:
        service.release(lease)
        journal(journal_path, "owner:released")
    except ResourceLockError as error:
        journal(journal_path, f"owner:late_release_rejected:{error.code.value}")
    return 0


def take_and_mutate(argv: list[str]) -> int:
    """``take_and_mutate <server> <resource> <journal> <marker> <ttl>``

    One acquisition attempt of the target mutation. The journal records
    the value-free outcome either way; on success the replacement effect
    runs and completes exactly once.
    """
    _, server_id, resource, journal_path, marker, ttl = argv
    service = ResourceLockService(os.environ["MAI_DATABASE_URL"])
    try:
        lease = service.acquire(server_id, resource, owner_ref="fixture-successor", ttl=float(ttl))
    except ResourceLockError as error:
        journal(journal_path, f"{marker}:{error.code.value}")
        return 0
    journal(journal_path, f"{marker}:granted")
    journal(journal_path, f"{marker}:mutation_done")
    service.release(lease)
    journal(journal_path, f"{marker}:released")
    return 0


def span(argv: list[str]) -> int:
    """``span <journal> <export>`` — operate the exported token elsewhere.

    A DIFFERENT process heartbeats and releases under the owner's token:
    locks span workers/parents/children by token, not by process.
    """
    _, journal_path, export_path = argv
    with open(export_path) as handle:
        lease = _lease_from(json.load(handle))
    service = ResourceLockService(os.environ["MAI_DATABASE_URL"], boot_id=lease.boot_id)
    renewed = service.heartbeat(lease)
    journal(journal_path, f"span:heartbeat_{'renewed' if renewed else 'lost'}")
    try:
        service.release(lease)
        journal(journal_path, "span:released")
    except ResourceLockError as error:
        journal(journal_path, f"span:release_rejected:{error.code.value}")
    return 0


def main() -> int:
    roles = {"hold": hold, "take_and_mutate": take_and_mutate, "span": span}
    argv = sys.argv[1:]
    if not argv or argv[0] not in roles:
        raise SystemExit("usage: lock_scenario_worker.py <role> ...")
    return roles[argv[0]](argv)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return (scenario / "journal.log").read_text().splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return sum(1 for line in _journal_lines(scenario) if line.endswith(f" {marker}"))


def _wait_journal(scenario: Path, marker: str, timeout: float) -> None:
    _wait_until(
        lambda: _journal_count(scenario, marker) > 0,
        timeout,
        f"journal marker {marker}",
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def lock_url() -> str:
    """Disposable fixture database (provisioned outside the suite)."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the lock claims need a real "
            "disposable Postgres fixture"
        )
    with psycopg.connect(url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {LOCK_TABLE}")
        conn.commit()
    yield url
    with psycopg.connect(url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {LOCK_TABLE}")
        conn.commit()


@pytest.fixture
def server_id() -> str:
    """Fresh canonical server identity per test (repeat-safe, C01 rule 1)."""
    return str(uuid.uuid4())


@pytest.fixture
def other_server_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def service(lock_url: str) -> ResourceLockService:
    return ResourceLockService(lock_url, boot_id=BOOT_ID)


@pytest.fixture
def successor(lock_url: str) -> ResourceLockService:
    """A second controller boot (the contending/restarter side)."""
    return ResourceLockService(lock_url, boot_id=OTHER_BOOT_ID)


@pytest.fixture
def scenario() -> Path:
    root = FIXTURE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True)
    if os.environ.get("MAI_KEEP_SCENARIO"):  # evidence capture; cleaned by hand
        yield root
        return
    yield root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# unit half: validation, verdict gate, ordering (no database)
# ---------------------------------------------------------------------------


class TestValueFreeValidation:
    def test_server_id_shapes_are_rejected(self) -> None:
        service = ResourceLockService("postgresql://x/db")
        for bad in (
            "",
            "not-a-uuid",
            str(uuid.uuid4()).upper(),
            f"{{{uuid.uuid4()}}}",
            5,
        ):
            with pytest.raises(ResourceLockError) as excinfo:
                service.acquire(bad, "resource")  # type: ignore[arg-type]
            assert excinfo.value.code is LockErrorCode.INVALID_REQUEST

    def test_resource_shapes_are_rejected(self) -> None:
        service = ResourceLockService("postgresql://x/db")
        for bad in ("", None, "x" * 201, "line\nbreak"):
            with pytest.raises(ResourceLockError) as excinfo:
                service.acquire(str(uuid.uuid4()), bad)  # type: ignore[arg-type]
            assert excinfo.value.code is LockErrorCode.INVALID_REQUEST

    def test_ttl_shapes_are_rejected(self) -> None:
        service = ResourceLockService("postgresql://x/db")
        for bad in (0, -1.0, float("inf"), float("nan"), True, "5", 8 * 24 * 3600.0):
            with pytest.raises(ResourceLockError) as excinfo:
                service.acquire(str(uuid.uuid4()), "resource", ttl=bad)
            assert excinfo.value.code is LockErrorCode.INVALID_REQUEST

    def test_lease_identity_shapes_are_rejected(self) -> None:
        service = ResourceLockService("postgresql://x/db")
        now = datetime.now(timezone.utc)
        lease = LockLease(
            server_id=str(uuid.uuid4()),
            resource="resource",
            owner_token=str(uuid.uuid4()).upper(),
            boot_id=BOOT_ID,
            owner_ref="",
            lease_ttl=DEFAULT_LEASE_TTL,
            expires_at=now,
            acquired_at=now,
        )
        with pytest.raises(ResourceLockError) as excinfo:
            service.heartbeat(lease)
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST

    def test_rejections_carry_the_safe_envelope(self) -> None:
        """No offending value ever appears in the message (C01 rule 3)."""
        service = ResourceLockService("postgresql://x/db")
        for bad in ("\nbreak", "x" * 201):
            with pytest.raises(ResourceLockError) as excinfo:
                service.acquire(str(uuid.uuid4()), bad)  # type: ignore[arg-type]
            assert bad not in str(excinfo.value)
        with pytest.raises(ResourceLockError) as excinfo:
            service.acquire("NOPE", "resource")
        assert "NOPE" not in str(excinfo.value)


class TestFenceVerdictGate:
    def test_exactly_the_terminal_verdicts_clear_a_fence(self) -> None:
        assert fence_may_be_cleared(ReconciliationClass.FINAL_SUCCEEDED)
        assert fence_may_be_cleared(ReconciliationClass.FINAL_FAILED)
        assert fence_may_be_cleared(ReconciliationClass.FINAL_CANCELLED)
        assert len(CLEARABLE_VERDICTS) == 3
        for open_verdict in (
            ReconciliationClass.RUNNING,
            ReconciliationClass.UNCERTAIN,
            ReconciliationClass.UNCERTAIN_AFTER_RESTART,
        ):
            assert not fence_may_be_cleared(open_verdict)

    def test_clear_fence_requires_verdict_and_note_shapes(self) -> None:
        service = ResourceLockService("postgresql://x/db")
        server = str(uuid.uuid4())
        with pytest.raises(ResourceLockError) as excinfo:
            service.clear_fence(server, "resource", verdict="running", note="n")
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST
        with pytest.raises(ResourceLockError) as excinfo:
            service.clear_fence(
                server,
                "resource",
                verdict=ReconciliationClass.FINAL_SUCCEEDED,
                note="  ",
            )
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST
        with pytest.raises(ResourceLockError) as excinfo:
            service.clear_fence(
                server,
                "resource",
                verdict=ReconciliationClass.FINAL_SUCCEEDED,
                note="x" * 501,
            )
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST


class _Spy:
    """Acquire/release recorder for the pure ordering half."""

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, float | None]] = []
        self.releases: list[str] = []

    def acquire(self, server_id, resource, *, owner_ref="", ttl=None) -> LockLease:
        self.calls.append((server_id, resource, ttl))
        if resource == self.fail_on:
            raise ResourceLockError(LockErrorCode.HELD, "lock is held by a live lease")
        now = datetime.now(timezone.utc)
        return LockLease(
            server_id=server_id,
            resource=resource,
            owner_token=f"token-{resource}",
            boot_id=BOOT_ID,
            owner_ref=owner_ref,
            lease_ttl=DEFAULT_LEASE_TTL if ttl is None else ttl,
            expires_at=now,
            acquired_at=now,
        )

    def release(self, lease: LockLease) -> None:
        self.releases.append(lease.resource)


class TestAcquireAllOrdering:
    """Canonical order + reverse rollback, observed through the spy."""

    # Fixed identities with known lexicographic order: FIRST < SECOND.
    FIRST = "00000000-0000-4000-8000-000000000001"
    SECOND = "00000000-0000-4000-8000-000000000002"

    def _service(
        self, monkeypatch: pytest.MonkeyPatch, fail_on: str
    ) -> tuple[ResourceLockService, _Spy]:
        service = ResourceLockService(
            "postgresql://fixture.invalid/db", boot_id=BOOT_ID
        )
        spy = _Spy(fail_on)
        monkeypatch.setattr(service, "acquire", spy.acquire)
        monkeypatch.setattr(service, "release", spy.release)
        return service, spy

    def test_canonical_order_ttl_forwarding_and_reverse_rollback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, spy = self._service(monkeypatch, fail_on="r-z")
        with pytest.raises(ResourceLockError) as excinfo:
            service.acquire_all(
                [
                    LockRequest(server_id=self.SECOND, resource="r-a", ttl=9.0),
                    LockRequest(server_id=self.FIRST, resource="r-z"),
                    LockRequest(
                        server_id=self.FIRST, resource="r-m", owner_ref="run-1"
                    ),
                ]
            )
        assert excinfo.value.code is LockErrorCode.HELD
        # Canonical total order over (server_id, resource), regardless of
        # input; the batch aborts at the first rejection.
        assert spy.calls == [
            (self.FIRST, "r-m", None),
            (self.FIRST, "r-z", None),
        ]
        # Only what was actually taken before the failure is released.
        assert spy.releases == ["r-m"]

    def test_all_taken_when_nothing_conflicts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, spy = self._service(monkeypatch, fail_on="")
        leases = service.acquire_all(
            [
                LockRequest(server_id=self.SECOND, resource="r-b"),
                LockRequest(server_id=self.FIRST, resource="r-a", ttl=9.0),
            ]
        )
        assert [lease.resource for lease in leases] == ["r-a", "r-b"]
        assert spy.calls == [
            (self.FIRST, "r-a", 9.0),
            (self.SECOND, "r-b", None),
        ]
        assert spy.releases == []

    def test_duplicate_key_and_bad_request_shapes_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, spy = self._service(monkeypatch, fail_on="")
        with pytest.raises(ResourceLockError) as excinfo:
            service.acquire_all(
                [
                    LockRequest(server_id=self.FIRST, resource="r"),
                    LockRequest(server_id=self.FIRST, resource="r"),
                ]
            )
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST
        assert spy.calls == []
        with pytest.raises(ResourceLockError) as excinfo:
            service.acquire_all(["not-a-request"])
        assert excinfo.value.code is LockErrorCode.INVALID_REQUEST
        assert spy.calls == []
        assert service.acquire_all([]) == ()


# ---------------------------------------------------------------------------
# live half: real Postgres
# ---------------------------------------------------------------------------


class TestExclusiveAcquire:
    def test_conflicting_key_rejected_independent_keys_granted(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
        other_server_id: str,
    ) -> None:
        lease = service.acquire(server_id, "fixture-mutation", owner_ref="run-1")
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.HELD
        assert "fixture-mutation" not in str(excinfo.value)
        # Same server, different resource; same resource, different server.
        independent_resource = successor.acquire(server_id, "fixture-independent")
        independent_server = successor.acquire(other_server_id, "fixture-mutation")
        assert independent_resource.owner_token != lease.owner_token
        assert independent_server.owner_token != lease.owner_token

    def test_reacquire_after_release_is_a_new_generation(
        self, service: ResourceLockService, server_id: str
    ) -> None:
        first = service.acquire(server_id, "fixture-mutation")
        service.release(first)
        assert service.inspect(server_id, "fixture-mutation") is None
        second = service.acquire(server_id, "fixture-mutation")
        assert second.owner_token != first.owner_token

    def test_snapshot_records_holder_evidence(
        self, service: ResourceLockService, server_id: str
    ) -> None:
        service.acquire(server_id, "fixture-mutation", owner_ref="run-42")
        snapshot = service.inspect(server_id, "fixture-mutation")
        assert snapshot is not None
        assert snapshot.state == "held"
        assert snapshot.boot_id == BOOT_ID
        assert snapshot.owner_ref == "run-42"
        assert snapshot.fenced_at is None and snapshot.fence_reason is None
        assert not snapshot.expired
        assert snapshot.lease_expires_at > snapshot.acquired_at
        assert snapshot.lease_expires_at.tzinfo is not None


class TestOwnershipChecks:
    def test_release_and_heartbeat_check_owner_and_resource(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
    ) -> None:
        lease_r1 = service.acquire(server_id, "resource-1")
        lease_r2 = service.acquire(server_id, "resource-2")
        # Token possession IS the authorization (locks span boots/processes):
        # the same lease presented by another controller boot still renews.
        assert successor.heartbeat(lease_r1) is True
        # A foreign token: rejected, live leases untouched.
        foreign = dataclasses.replace(lease_r1, owner_token=str(uuid.uuid4()))
        assert successor.heartbeat(foreign) is False
        with pytest.raises(ResourceLockError) as excinfo:
            successor.release(foreign)
        assert excinfo.value.code is LockErrorCode.NOT_OWNER
        # Right token, wrong resource: rejected, both leases untouched.
        retargeted = dataclasses.replace(lease_r1, resource="resource-2")
        with pytest.raises(ResourceLockError) as excinfo:
            service.release(retargeted)
        assert excinfo.value.code is LockErrorCode.NOT_OWNER
        assert (
            service.inspect(server_id, "resource-2").owner_token == lease_r2.owner_token
        )
        assert (
            service.inspect(server_id, "resource-1").owner_token == lease_r1.owner_token
        )
        # Correct identity: the lease is gone; later touches see nothing.
        service.release(lease_r1)
        assert service.heartbeat(lease_r1) is False
        with pytest.raises(ResourceLockError) as excinfo:
            service.release(lease_r1)
        assert excinfo.value.code is LockErrorCode.NOT_OWNER
        service.release(lease_r2)
        assert service.inspect(server_id, "resource-2") is None

    def test_heartbeat_renews_on_the_database_clock(
        self, service: ResourceLockService, server_id: str
    ) -> None:
        lease = service.acquire(server_id, "fixture-mutation", ttl=5.0)
        before = service.inspect(server_id, "fixture-mutation")
        assert before is not None
        time.sleep(0.2)
        assert service.heartbeat(lease) is True
        after = service.inspect(server_id, "fixture-mutation")
        assert after is not None
        assert after.lease_expires_at > before.lease_expires_at
        assert after.heartbeat_at > before.heartbeat_at


class TestFencing:
    def test_lapsed_lease_fences_and_reconciliation_gates_the_successor(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
    ) -> None:
        lapsed = service.acquire(server_id, "fixture-mutation", ttl=0.5)
        _wait_until(
            lambda: _expired(service, server_id, "fixture-mutation"),
            EXPIRY_TIMEOUT,
            "lease lapse",
        )
        # The lapse fences; it never hands the key over (acceptance 2).
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.FENCED
        fenced = service.inspect(server_id, "fixture-mutation")
        assert fenced is not None
        assert fenced.state == "fenced"
        assert fenced.fence_reason == FENCE_REASON_LEASE_EXPIRED
        assert fenced.fenced_boot_id == BOOT_ID
        # The fence is stable: repeated attempts stay rejected.
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.FENCED
        # A live/uncertain verdict does NOT clear the fence.
        for open_verdict in (
            ReconciliationClass.RUNNING,
            ReconciliationClass.UNCERTAIN,
            ReconciliationClass.UNCERTAIN_AFTER_RESTART,
        ):
            with pytest.raises(ResourceLockError) as excinfo:
                successor.clear_fence(
                    server_id,
                    "fixture-mutation",
                    verdict=open_verdict,
                    note="not terminal",
                )
            assert excinfo.value.code is LockErrorCode.FENCE_UNRESOLVED
        assert service.inspect(server_id, "fixture-mutation") is not None
        # A terminal verdict clears it; the fresh acquisition is a new generation.
        cleared = successor.clear_fence(
            server_id,
            "fixture-mutation",
            verdict=ReconciliationClass.FINAL_SUCCEEDED,
            note="fixture: host-verified terminal outcome",
        )
        assert cleared.owner_token == lapsed.owner_token
        assert cleared.state == "fenced"
        assert service.inspect(server_id, "fixture-mutation") is None
        fresh = successor.acquire(server_id, "fixture-mutation")
        assert fresh.owner_token != lapsed.owner_token

    def test_clear_fence_without_a_fence_is_rejected(
        self, service: ResourceLockService, server_id: str
    ) -> None:
        with pytest.raises(ResourceLockError) as excinfo:
            service.clear_fence(
                server_id,
                "fixture-mutation",
                verdict=ReconciliationClass.FINAL_SUCCEEDED,
                note="nothing fenced",
            )
        assert excinfo.value.code is LockErrorCode.NOT_FENCED

    def test_late_heartbeat_self_fences(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
    ) -> None:
        lease = service.acquire(server_id, "fixture-mutation", ttl=0.5)
        _wait_until(
            lambda: _expired(service, server_id, "fixture-mutation"),
            EXPIRY_TIMEOUT,
            "lease lapse",
        )
        # The owner's own late touch performs the same fence transition.
        assert service.heartbeat(lease) is False
        fenced = service.inspect(server_id, "fixture-mutation")
        assert fenced is not None
        assert fenced.state == "fenced"
        assert fenced.fence_reason == FENCE_REASON_LEASE_EXPIRED
        # And the fenced key blocks the successor as usual.
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.FENCED

    def test_late_release_reports_the_lost_lease(
        self,
        service: ResourceLockService,
        server_id: str,
        successor: ResourceLockService,
    ) -> None:
        lease = service.acquire(server_id, "fixture-mutation", ttl=0.5)
        _wait_until(
            lambda: _expired(service, server_id, "fixture-mutation"),
            EXPIRY_TIMEOUT,
            "lease lapse",
        )
        with pytest.raises(ResourceLockError) as excinfo:
            service.release(lease)
        assert excinfo.value.code is LockErrorCode.LEASE_LOST
        fenced = service.inspect(server_id, "fixture-mutation")
        assert fenced is not None and fenced.state == "fenced"
        # After reconciliation the stale identity is simply not the owner.
        successor.clear_fence(
            server_id,
            "fixture-mutation",
            verdict=ReconciliationClass.FINAL_FAILED,
            note="fixture: terminal",
        )
        with pytest.raises(ResourceLockError) as excinfo:
            service.release(lease)
        assert excinfo.value.code is LockErrorCode.NOT_OWNER


class TestRestartIdentity:
    def test_new_boot_never_double_holds_and_never_steals(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
    ) -> None:
        """Restart = new boot id; a live lease blocks it, a lapsed one fences it."""
        service.acquire(server_id, "fixture-mutation", ttl=0.8)
        # While the lease is live: the restarted controller holds nothing.
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.HELD
        _wait_until(
            lambda: _expired(service, server_id, "fixture-mutation"),
            EXPIRY_TIMEOUT,
            "lease lapse",
        )
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire(server_id, "fixture-mutation")
        assert excinfo.value.code is LockErrorCode.FENCED
        fenced = service.inspect(server_id, "fixture-mutation")
        assert fenced is not None
        assert fenced.fenced_boot_id == BOOT_ID
        successor.clear_fence(
            server_id,
            "fixture-mutation",
            verdict=ReconciliationClass.FINAL_CANCELLED,
            note="fixture: post-restart reconciliation",
        )
        assert successor.acquire(server_id, "fixture-mutation").boot_id == OTHER_BOOT_ID


class TestAcquireAllLive:
    def test_all_or_nothing_on_the_real_database(
        self,
        service: ResourceLockService,
        successor: ResourceLockService,
        server_id: str,
    ) -> None:
        conflicting = service.acquire(server_id, "resource-held")
        with pytest.raises(ResourceLockError) as excinfo:
            successor.acquire_all(
                [
                    LockRequest(server_id=server_id, resource="resource-z"),
                    LockRequest(server_id=server_id, resource="resource-held"),
                ]
            )
        assert excinfo.value.code is LockErrorCode.HELD
        # The failed batch left nothing behind.
        assert successor.inspect(server_id, "resource-z") is None
        service.release(conflicting)
        leases = successor.acquire_all(
            [
                LockRequest(server_id=server_id, resource="resource-held"),
                LockRequest(
                    server_id=server_id, resource="resource-z", owner_ref="run-7"
                ),
            ]
        )
        assert [lease.owner_ref for lease in leases] == ["", "run-7"]
        for lease in leases:
            successor.release(lease)


# ---------------------------------------------------------------------------
# live half: the brief's LOCK-FENCE scenario with REAL processes
# ---------------------------------------------------------------------------


def _spawn(scenario: Path, url: str, name: str, *args: str) -> subprocess.Popen:
    script = scenario / ROLE_SCRIPT_NAME
    if not script.exists():
        script.write_text(ROLE_SCRIPT_SOURCE)
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_SRC_PATH"] = str(BACKEND_ROOT / "src")
    log = open(scenario / f"{name}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _read_export(path: Path) -> dict:
    _wait_until(path.exists, READY_TIMEOUT, f"lease export {path.name}")
    return json.loads(path.read_text())


class TestLiveTwoProcessLockFence:
    def test_contention_fencing_and_single_effect(
        self, lock_url: str, scenario: Path, service: ResourceLockService
    ) -> None:
        """The brief's scenario: two real processes, one target mutation.

        Kill the owner while its effect is possibly live; the successor
        must be fenced, and exactly one replacement effect runs after a
        terminal reconciliation verdict — while an independent resource
        proceeds the whole time.
        """
        server = str(uuid.uuid4())
        export = scenario / "owner-lease.json"
        owner = _spawn(
            scenario,
            lock_url,
            "owner",
            "hold",
            server,
            "fixture-target-mutation",
            str(scenario / "journal.log"),
            "2.0",
            "60",
            str(export),
        )
        try:
            _wait_journal(scenario, "owner:effect_begin", READY_TIMEOUT)
            payload = _read_export(export)

            # Live contention: rejected; an independent resource proceeds.
            _spawn(
                scenario,
                lock_url,
                "conflict",
                "take_and_mutate",
                server,
                "fixture-target-mutation",
                str(scenario / "journal.log"),
                "successor:conflict",
                "30",
            ).wait(timeout=30)
            _spawn(
                scenario,
                lock_url,
                "independent",
                "take_and_mutate",
                server,
                "fixture-independent-resource",
                str(scenario / "journal.log"),
                "successor:independent",
                "30",
            ).wait(timeout=30)
            assert _journal_count(scenario, "successor:conflict:held") == 1
            assert _journal_count(scenario, "successor:independent:granted") == 1
            assert _journal_count(scenario, "successor:independent:mutation_done") == 1

            # Kill the owner mid-hold: the effect is possibly live.
            owner.send_signal(signal.SIGKILL)
            assert owner.wait(timeout=30) == -signal.SIGKILL

            _wait_until(
                lambda: _expired(service, server, "fixture-target-mutation"),
                EXPIRY_TIMEOUT,
                "orphaned lease lapse",
            )

            # The successor is FENCED, not handed the key (acceptance 2).
            _spawn(
                scenario,
                lock_url,
                "fenced",
                "take_and_mutate",
                server,
                "fixture-target-mutation",
                str(scenario / "journal.log"),
                "successor:after_kill",
                "30",
            ).wait(timeout=30)
            assert _journal_count(scenario, "successor:after_kill:fenced") == 1
            fenced = service.inspect(server, "fixture-target-mutation")
            assert fenced is not None
            assert fenced.state == "fenced"
            assert fenced.fenced_boot_id == payload["boot_id"]

            # A live verdict keeps the fence up (negative control).
            with pytest.raises(ResourceLockError) as excinfo:
                service.clear_fence(
                    server,
                    "fixture-target-mutation",
                    verdict=ReconciliationClass.RUNNING,
                    note="fixture: verdict stands in for an unresolved host truth",
                )
            assert excinfo.value.code is LockErrorCode.FENCE_UNRESOLVED
            _spawn(
                scenario,
                lock_url,
                "still-fenced",
                "take_and_mutate",
                server,
                "fixture-target-mutation",
                str(scenario / "journal.log"),
                "successor:after_running_verdict",
                "30",
            ).wait(timeout=30)
            assert (
                _journal_count(scenario, "successor:after_running_verdict:fenced") == 1
            )

            # Terminal reconciliation verdict: exactly one replacement effect.
            service.clear_fence(
                server,
                "fixture-target-mutation",
                verdict=ReconciliationClass.FINAL_CANCELLED,
                note="fixture: verdict stands in for the host-verified terminal outcome",
            )
            _spawn(
                scenario,
                lock_url,
                "successor",
                "take_and_mutate",
                server,
                "fixture-target-mutation",
                str(scenario / "journal.log"),
                "successor:after_reconcile",
                "30",
            ).wait(timeout=30)
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=30)

        # The single-effect proof: the owner's effect began exactly once
        # and was never completed; the replacement effect ran exactly once,
        # only AFTER the fence was cleared.
        assert _journal_count(scenario, "owner:effect_begin") == 1
        assert _journal_count(scenario, "successor:after_reconcile:granted") == 1
        assert _journal_count(scenario, "successor:after_reconcile:mutation_done") == 1
        lines = _journal_lines(scenario)
        fenced_index = next(
            i
            for i, line in enumerate(lines)
            if line.endswith(" successor:after_kill:fenced")
        )
        granted_index = next(
            i
            for i, line in enumerate(lines)
            if line.endswith(" successor:after_reconcile:granted")
        )
        assert fenced_index < granted_index

    def test_token_spans_processes_with_ownership_checked_release(
        self, lock_url: str, scenario: Path, service: ResourceLockService
    ) -> None:
        """Locks span workers: another process operates the shared token."""
        server = str(uuid.uuid4())
        export = scenario / "owner-lease.json"
        owner = _spawn(
            scenario,
            lock_url,
            "owner",
            "hold",
            server,
            "fixture-spanned-mutation",
            str(scenario / "journal.log"),
            "2.0",
            "3",
            str(export),
        )
        _wait_journal(scenario, "owner:effect_begin", READY_TIMEOUT)
        payload = _read_export(export)
        span = _spawn(
            scenario,
            lock_url,
            "span",
            "span",
            str(scenario / "journal.log"),
            str(export),
        )
        assert span.wait(timeout=30) == 0
        assert owner.wait(timeout=30) == 0
        assert _journal_count(scenario, "span:heartbeat_renewed") == 1
        assert _journal_count(scenario, "span:released") == 1
        # The lease is gone; the original holder's late touches say so.
        assert _journal_count(scenario, "owner:heartbeat_lost") == 1
        assert _journal_count(scenario, "owner:late_release_rejected:not_owner") == 1
        assert service.inspect(server, "fixture-spanned-mutation") is None
        assert payload["owner_ref"] == "fixture-owner"
