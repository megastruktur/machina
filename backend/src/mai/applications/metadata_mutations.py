"""Shared service-metadata mutation coordinator (ask_16, campaign D4, C4/C5).

ONE seam for the operator/agent-facing service-metadata mutations that
would otherwise each hand-roll their own serialization:

- ``update_entry_metadata`` — description / runbook slots /
  ``custom_instructions`` / ``installed`` updates on one Service Map
  entry through the slice-15 ``expected_revision`` compare-and-set
  (fields the caller omits are preserved, so operator notes are never
  wiped by a narrow update);
- ``unadopt`` — the D4 removal discipline: the auto-adopt TOMBSTONE is
  persisted and stale pending preservation is cancelled BEFORE the map
  entry is deleted (suppression-before-removal);
- ``readopt`` — the explicit operator readopt that starts a NEW identity
  generation and clears the tombstone;
- ``adopt`` — pure DELEGATION to the canonical adoption seam
  (:class:`mai.applications.adopt.ApplicationAdoption`). The coordinator
  NEVER writes map rows or manifest files as an adoption substitute
  (canonical adoption is not a map-only substitute) and NEVER converts a
  standalone container into a manual entry (no standalone-to-manual
  fallback).

Lock order (C4, frozen): the caller's already-held
``application:<application_id>`` lease is BORROWED (never reacquired,
never released by this coordinator); otherwise it is acquired together
with the coordinator-owned ``service-metadata`` server lease through
``ResourceLockService.acquire_all`` (canonical total order, deadlock
free, all-or-nothing). Only THEN does the short file/SQL section run:
the map mutation inside the slice-15 per-server file lock (pure sync
file I/O, nothing awaited inside), or the identity SQL through the
slice-14 store. A lease that cannot be acquired (held / fenced / lost)
refuses the whole operation before ANY effect — the supported-writer
guard: one writer per server's service metadata, no fence bypass.

Everything here is a composition of existing seams; this module adds no
storage of its own, no second lock authority and no scheduler.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mai.applications.adopt import (
    AdoptionRequest,
    AdoptionResult,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.learning_operations import (
    LearningOperationStore,
)
from mai.applications.service_map import (
    ServiceInstructions,
    ServiceMapEntry,
    ServiceMapStore,
)
from mai.jobs.resource_locks import (
    LockErrorCode,
    LockLease,
    LockRequest,
    ResourceLockError,
    ResourceLockService,
)

__all__ = [
    "APPLICATION_LEASE_PREFIX",
    "METADATA_LEASE_RESOURCE",
    "MetadataMutationCoordinator",
    "MetadataMutationError",
    "MetadataMutationErrorCode",
    "MutationOutcome",
]

#: The coordinator-owned per-server lease key (C4: the NEW server
#: ``service-metadata`` lease guarding every metadata mutation).
METADATA_LEASE_RESOURCE: Final[str] = "service-metadata"

#: The execution-side per-application lease namespace this coordinator
#: acquires (or borrows) ahead of the metadata lease.
APPLICATION_LEASE_PREFIX: Final[str] = "application:"


class MetadataMutationErrorCode(StrEnum):
    """Stable machine-readable coordinator rejection codes (C01)."""

    INVALID_REQUEST = "invalid_request"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_FENCED = "lease_fenced"
    LEASE_LOST = "lease_lost"


class MetadataMutationError(ValueError):
    """One structured, value-free coordinator rejection.

    Lease failures surface as the coordinator codes above; a
    compare-and-set mismatch keeps its AUTHORITATIVE store error
    (:class:`mai.applications.service_map.ServiceMapError` with
    ``REVISION_CONFLICT``) — never a duplicated second vocabulary.
    """

    def __init__(self, code: MetadataMutationErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    """The honest outcome of one coordinator mutation (only the fields
    the operation actually touched are populated)."""

    operation: str
    server_id: uuid.UUID
    application_id: uuid.UUID
    #: Map entry revision AFTER a successful metadata update (``None``
    #: when the operation wrote no map entry).
    map_entry_revision: int | None = None
    #: True when the unadopt deleted a stored map entry.
    map_entry_deleted: bool = False
    #: Identity generation after an identity-affecting operation.
    identity_generation: int | None = None
    #: Tombstone state after an identity-affecting operation.
    auto_adopt_suppressed: bool | None = None
    #: Entry observed before the mutation (``None`` when not read).
    previous_revision: int | None = None


def _application_resource(application_id: uuid.UUID) -> str:
    return f"{APPLICATION_LEASE_PREFIX}{application_id}"


def _coordinator_code(error: ResourceLockError) -> MetadataMutationErrorCode:
    """Map the lock vocabulary onto the coordinator codes (fail-closed:
    an unknown lock code is treated as a live conflict)."""
    if error.code is LockErrorCode.FENCED:
        return MetadataMutationErrorCode.LEASE_FENCED
    if error.code is LockErrorCode.LEASE_LOST:
        return MetadataMutationErrorCode.LEASE_LOST
    return MetadataMutationErrorCode.LEASE_CONFLICT


class MetadataMutationCoordinator:
    """ONE serialized seam for service-metadata mutations.

    Composes the PG lease service, the slice-15 locked map store and the
    slice-14 identity store; delegates canonical adoption. Single-writer
    per server's metadata is ENFORCED here via the ``service-metadata``
    lease — never assumed."""

    def __init__(
        self,
        *,
        locks: ResourceLockService,
        map_store: ServiceMapStore,
        learning: LearningOperationStore,
        adoption: ApplicationAdoption | None = None,
        owner_ref: str = "metadata-coordinator",
    ) -> None:
        self._locks = locks
        self._map_store = map_store
        self._learning = learning
        self._adoption = adoption
        self._owner_ref = owner_ref

    # -- lease plumbing (C4 order, borrowed-lease discipline) ---------------

    def _acquire_leases(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        application_lease: LockLease | None,
    ) -> tuple[LockLease | None, tuple[LockLease, ...]]:
        """Borrow-or-acquire the application lease, then take the
        coordinator-owned ``service-metadata`` lease.

        Returns ``(borrowed, acquired)``: ``borrowed`` is the caller's
        lease passed through untouched (never reacquired, never
        released here); ``acquired`` are the leases THIS call owns and
        must release. ``acquire_all`` orders the keys canonically
        (``application:<aid>`` sorts before ``service-metadata``) and is
        all-or-nothing."""
        sid = str(server_id)
        if application_lease is not None:
            if (
                not isinstance(application_lease, LockLease)
                or application_lease.server_id != sid
                or application_lease.resource != _application_resource(application_id)
            ):
                raise MetadataMutationError(
                    MetadataMutationErrorCode.INVALID_REQUEST,
                    "application_lease must be a live lease for "
                    f"application:{application_id} on this server",
                )
            requests = [
                LockRequest(
                    server_id=sid,
                    resource=METADATA_LEASE_RESOURCE,
                    owner_ref=self._owner_ref,
                )
            ]
        else:
            requests = [
                LockRequest(
                    server_id=sid,
                    resource=_application_resource(application_id),
                    owner_ref=self._owner_ref,
                ),
                LockRequest(
                    server_id=sid,
                    resource=METADATA_LEASE_RESOURCE,
                    owner_ref=self._owner_ref,
                ),
            ]
        try:
            acquired = self._locks.acquire_all(requests)
        except ResourceLockError as error:
            raise MetadataMutationError(
                _coordinator_code(error),
                f"the service-metadata lease set for server {server_id} "
                f"could not be acquired: {error.code.value}",
            ) from error
        return application_lease, acquired

    def _release_acquired(self, acquired: tuple[LockLease, ...]) -> None:
        """Release ONLY the leases this call acquired (reverse order);
        a borrowed lease is never released here."""
        for lease in reversed(acquired):
            try:
                self._locks.release(lease)
            except ResourceLockError as error:
                if error.code is not LockErrorCode.LEASE_LOST:
                    raise
                # A lease that lapsed mid-operation was already fenced by
                # the touch; the fenced successor path owns it now.

    # -- operations -----------------------------------------------------------

    async def update_entry_metadata(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        expected_revision: int,
        name: str | None = None,
        category: str | None = None,
        description: str | None = None,
        instructions: ServiceInstructions | dict[str, str] | None = None,
        custom_instructions: str | None = None,
        installed: bool | None = None,
        application_lease: LockLease | None = None,
    ) -> MutationOutcome:
        """Update the mutable metadata of ONE mapped service through the
        slice-15 compare-and-set.

        Fields left ``None`` are PRESERVED from the stored entry — a
        narrow description update never wipes operator notes, runbook
        slots or the installed flag (the common supported-writer guard
        for operator state). ``expected_revision`` is mandatory and is
        re-verified inside the per-server file lock by the store
        itself; a mismatch raises the authoritative
        ``ServiceMapError(REVISION_CONFLICT)`` with no effect."""
        previous = self._map_store.get_entry(server_id, application_id)
        if previous is None:
            raise MetadataMutationError(
                MetadataMutationErrorCode.INVALID_REQUEST,
                f"service {application_id} is not mapped on server "
                f"{server_id}; metadata updates CAS against a stored entry",
            )
        merged: dict[str, object] = {
            "name": previous.name,
            "category": previous.category,
            "description": previous.description,
            "instructions": previous.instructions,
            "custom_instructions": previous.custom_instructions,
            "installed": previous.installed,
        }
        overrides: dict[str, object] = {
            "name": name,
            "category": category,
            "description": description,
            "instructions": instructions,
            "custom_instructions": custom_instructions,
            "installed": installed,
        }
        for key, value in overrides.items():
            if value is not None:
                merged[key] = value
        borrowed, acquired = self._acquire_leases(
            server_id, application_id, application_lease
        )
        try:
            entry: ServiceMapEntry = self._map_store.upsert_entry(
                server_id,
                name=str(merged["name"]),
                category=str(merged["category"]),
                description=str(merged["description"]),
                instructions=merged["instructions"],  # type: ignore[arg-type]
                custom_instructions=str(merged["custom_instructions"]),
                installed=bool(merged["installed"]),
                application_id=application_id,
                expected_revision=expected_revision,
            )
        finally:
            self._release_acquired(acquired)
        return MutationOutcome(
            operation="update_entry_metadata",
            server_id=server_id,
            application_id=application_id,
            map_entry_revision=entry.revision,
            previous_revision=previous.revision,
        )

    async def unadopt(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        expected_revision: int | None = None,
        application_lease: LockLease | None = None,
    ) -> MutationOutcome:
        """Unadopt one service: suppression BEFORE removal (D4).

        Step 1 persists the auto-adopt tombstone and cancels stale
        pending preservation (slice-14 store — the identity SQL runs
        under the metadata lease but OUTSIDE any file lock). Step 2
        deletes the map entry inside the short per-server file lock
        (optional ``expected_revision`` CAS; a missing entry is an
        idempotent no-op). The tombstone outlives the deletion: no
        background admission can resurrect the service until an explicit
        readopt."""
        borrowed, acquired = self._acquire_leases(
            server_id, application_id, application_lease
        )
        try:
            identity = await self._learning.suppress_for_unadopt(
                server_id, application_id
            )
            deleted = self._map_store.delete_entry(
                server_id, application_id, expected_revision=expected_revision
            )
        finally:
            self._release_acquired(acquired)
        return MutationOutcome(
            operation="unadopt",
            server_id=server_id,
            application_id=application_id,
            map_entry_deleted=deleted,
            identity_generation=identity.generation,
            auto_adopt_suppressed=identity.auto_adopt_suppressed,
        )

    async def readopt(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        manual_identity: bool = False,
        application_lease: LockLease | None = None,
    ) -> MutationOutcome:
        """Explicit operator readopt: starts a NEW identity generation
        and clears the auto-adopt tombstone (slice-14 store). Prior
        facts and receipts stay intact as history; stale-generation work
        is fenced by the apply-time seams, never by deletion here."""
        borrowed, acquired = self._acquire_leases(
            server_id, application_id, application_lease
        )
        try:
            identity = await self._learning.readopt_service_identity(
                server_id, application_id, manual_identity=manual_identity
            )
        finally:
            self._release_acquired(acquired)
        return MutationOutcome(
            operation="readopt",
            server_id=server_id,
            application_id=application_id,
            identity_generation=identity.generation,
            auto_adopt_suppressed=identity.auto_adopt_suppressed,
        )

    async def adopt(
        self,
        manifest_store: ApplicationManifestStore,
        request: AdoptionRequest,
        *,
        application_lease: LockLease | None = None,
        intent_id: uuid.UUID | None = None,
    ) -> AdoptionResult:
        """Canonical adoption DELEGATION under the same lease set.

        The coordinator holds the leases and forwards the request
        untouched to :class:`ApplicationAdoption.adopt` — the canonical
        manifest+projection seam. It never writes Service Map rows as a
        substitute (canonical adoption is not a map-only substitute) and
        standalone-container requests are refused by the canonical seam
        itself (no standalone-to-manual fallback)."""
        if self._adoption is None:
            raise MetadataMutationError(
                MetadataMutationErrorCode.INVALID_REQUEST,
                "no canonical adoption seam was wired into this coordinator",
            )
        if not isinstance(request, AdoptionRequest):
            raise MetadataMutationError(
                MetadataMutationErrorCode.INVALID_REQUEST,
                "request must be an AdoptionRequest value",
            )
        borrowed, acquired = self._acquire_leases(
            request.server_id, request.application_id, application_lease
        )
        try:
            return await self._adoption.adopt(manifest_store, request, intent_id=intent_id)
        finally:
            self._release_acquired(acquired)

    # -- direct lease support for callers that compose further steps ---------

    def lease_set(
        self,
        server_id: uuid.UUID,
        application_ids: Sequence[uuid.UUID],
    ) -> tuple[LockLease, ...]:
        """Take the metadata lease plus the ``application:<aid>`` leases
        for every id in one canonical, all-or-nothing batch (the C4
        sorted-existing-leases-first order for callers that need several
        application fences around one metadata section)."""
        requests = [
            LockRequest(
                server_id=str(server_id),
                resource=_application_resource(application_id),
                owner_ref=self._owner_ref,
            )
            for application_id in application_ids
        ]
        requests.append(
            LockRequest(
                server_id=str(server_id),
                resource=METADATA_LEASE_RESOURCE,
                owner_ref=self._owner_ref,
            )
        )
        try:
            return self._locks.acquire_all(requests)
        except ResourceLockError as error:
            raise MetadataMutationError(
                _coordinator_code(error),
                f"the metadata lease set for server {server_id} could not "
                f"be acquired: {error.code.value}",
            ) from error
