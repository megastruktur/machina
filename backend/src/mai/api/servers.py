"""Server profile HTTP API: authenticated create/get/edit through the
canonical service and projection (C01, C02, C08).

Ownership map (contracts.md C01/C02/C08; profile cards 02-profile-schema,
02-file-store, 02-profile-projection; SERVICES ``mai-api`` row):

- Identity is the opaque UUID only (C01 rule 1): ``POST /api/v1/servers``
  mints a fresh ``server_id`` server-side and every later operation is
  addressed by that UUID in the path. Display ``name``, host or labels are
  never identity, so a display rename retargets nothing.
- The principal comes from the validated session only (C01 rule 2); the
  request security boundary is owned by :mod:`mai.api.auth` and this router
  joins it — the same layer order (session → origin → CSRF, ADR §5) and the
  same ``BoundaryError`` rendering registered by ``register_auth``. GET is a
  safe method and applies layer 1 only (mirrors ``GET /api/v1/auth/csrf``).
- Every write goes through the canonical pipeline
  :meth:`mai.profiles.projection.ProfileProjection.apply_upsert`
  (journal ``prepared → file committed → reconciled``, C02 rule 5); the API
  never touches canonical files or SQL directly.
- Field validation has exactly one source: the frozen schema producer
  :func:`mai.contracts.server.parse_server_profile_text`, run by the file
  store before any filesystem effect. Request bodies are transported as
  plain JSON types and re-validated there; this layer maps the producer's
  structured rejections onto the C01 envelope (422 ``validation_failed``
  with field paths + stable codes) without duplicating any rule.
- Optimistic concurrency (C01 rule 6): ``PUT`` carries the caller's last
  observed ``expected_revision``. A stale caller is rejected with a
  structured ``409 conflict`` BEFORE any journal row is created (a
  read-only pre-check against the authoritative file), so the ordinary
  stale-client path leaves no live intent behind. A true concurrent race is
  still serialized by the journal's partial unique index
  (:class:`~mai.profiles.projection.ProjectionError` ``intent_pending`` →
  ``409 conflict``).
- The data root is explicit configuration (C02 rule 1):
  ``MAI_DATA_ROOT`` (Compose sets ``/var/lib/mai``), default ``~/.mai``,
  expanded against the host home in :func:`resolve_profiles_root`.
  The store is built once in the lifespan; if it cannot be constructed the
  routes fail closed with ``503 unavailable`` and never touch state.

Prompt note (C02 rule 6, C08 rule 5): the versioned prompt edition is the
canonical ``prompt_revision`` field of ``profile.yaml``; a prompt edit is a
profile edit that carries the new edition number through the same pipeline.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from typing import Final

import yaml
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.contracts.server import (
    SCHEMA_VERSION,
    ExecutionEnvironment,
    PrivilegeMode,
    ProfileValidationError,
    ServerProfile,
    parse_server_profile_text,
)
from mai.profiles.files import (
    SERVERS_DIR,
    FileStoreErrorCode,
    ProfileFileError,
    ProfileFileStore,
    StoredProfile,
)
from mai.profiles.projection import (
    ApplyResult,
    ProjectionErrorCode,
    ProjectionError,
    ProfileProjection,
    ProjectionState,
)

__all__ = [
    "DATA_ROOT_VAR",
    "DEFAULT_DATA_ROOT",
    "register_servers",
    "resolve_profiles_root",
]

#: Explicit data root (C02 rule 1); Compose sets ``MAI_DATA_ROOT``.
DATA_ROOT_VAR: Final = "MAI_DATA_ROOT"
#: Contract default for the controller host (C02 rule 1); ``~`` expands in
#: :func:`resolve_profiles_root`.
DEFAULT_DATA_ROOT: Final = "~/.mai"

#: Mutation rejection codes that are document-validation, not state (the
#: store owns the byte cap and alias/depth hardening; identity/revision
#: misuse cannot occur through the typed paths but is classified anyway).
_DOCUMENT_REJECTIONS: Final[frozenset[FileStoreErrorCode]] = frozenset(
    {
        FileStoreErrorCode.DOCUMENT_TOO_LARGE,
        FileStoreErrorCode.DOCUMENT_TOO_DEEP,
        FileStoreErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
        FileStoreErrorCode.INVALID_SERVER_ID,
        FileStoreErrorCode.INVALID_REVISION,
        FileStoreErrorCode.SERVER_ID_MISMATCH,
    }
)


def resolve_profiles_root(env: Mapping[str, str] | None = None) -> str:
    """Explicit data root (C02 rule 1): ``MAI_DATA_ROOT``, else ``~/.mai``.

    ``~``-prefixed values (the default included) expand against the
    controller host's home at this single boundary; absolute paths (the
    Compose-provided ``/var/lib/mai`` included) pass through unchanged.
    """
    source = os.environ if env is None else env
    return os.path.expanduser(source.get(DATA_ROOT_VAR) or DEFAULT_DATA_ROOT)


# ---------------------------------------------------------------------------
# Wire shapes (transport only; field truth is the schema producer)
# ---------------------------------------------------------------------------


class SshIdentityBody(BaseModel):
    """SSH target identity (C04 rule 1); ``credential_ref`` is a reference."""

    host: str
    port: int
    user: str
    host_key: str
    credential_ref: str


class ProfileCreate(BaseModel):
    """Creation payload; ``server_id`` is minted by the service (C01 rule 1)."""

    name: str
    prompt_revision: int = Field(ge=1)
    environment: ExecutionEnvironment
    ssh: SshIdentityBody
    privilege: PrivilegeMode
    capabilities: list[str]


class ProfileEdit(ProfileCreate):
    """Edit payload: carries the caller's last observed revision (C01 rule 6)."""

    expected_revision: int = Field(ge=0)


class ProfileMutationResult(BaseModel):
    """Post-commit version triple of one successful write (C02 rule 4)."""

    server_id: uuid.UUID
    revision: int
    schema_revision: int
    sha256: str


class ProjectionView(BaseModel):
    """Projected triple view (C08 rule 6: stale is shown, never hidden)."""

    revision: int | None
    stale: bool


class ProfileResponse(BaseModel):
    """Stored canonical profile plus the C02 rule-4 version triple."""

    schema_version: int
    server_id: uuid.UUID
    name: str
    prompt_revision: int
    environment: ExecutionEnvironment
    ssh: SshIdentityBody
    privilege: PrivilegeMode
    capabilities: list[str]
    revision: int
    sha256: str
    projection: ProjectionView


class ProfileListResponse(BaseModel):
    """Every stored server profile in deterministic ``(name, id)`` order."""

    servers: list[ProfileResponse]


# ---------------------------------------------------------------------------
# Canonical document assembly (single producer re-validates on write)
# ---------------------------------------------------------------------------


def _canonical_document(server_id: uuid.UUID, payload: ProfileCreate) -> str:
    """Render the canonical ``profile.yaml`` body in frozen field order."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "server_id": str(server_id),
        "name": payload.name,
        "prompt_revision": payload.prompt_revision,
        "environment": payload.environment.value,
        "ssh": {
            "host": payload.ssh.host,
            "port": payload.ssh.port,
            "user": payload.ssh.user,
            "host_key": payload.ssh.host_key,
            "credential_ref": payload.ssh.credential_ref,
        },
        "privilege": payload.privilege.value,
        "capabilities": list(payload.capabilities),
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _mutation_result(
    server_id: uuid.UUID, result: ApplyResult
) -> ProfileMutationResult:
    """Upsert outcomes always carry the committed triple; absence is a bug."""
    if result.revision is None or result.sha256 is None:
        raise ProjectionError(
            ProjectionErrorCode.INTENT_PHASE,
            "upsert outcome is missing its committed triple",
        )
    return ProfileMutationResult(
        server_id=server_id,
        revision=result.revision,
        schema_revision=SCHEMA_VERSION,
        sha256=result.sha256,
    )


def _profile_view(
    profile: ServerProfile, stored: StoredProfile, state: ProjectionState | None
) -> ProfileResponse:
    """One stored profile rendered as the wire view (single read == list row)."""
    return ProfileResponse(
        schema_version=profile.schema_version,
        server_id=profile.server_id,
        name=profile.name,
        prompt_revision=profile.prompt_revision,
        environment=profile.environment,
        ssh=SshIdentityBody(
            host=profile.ssh.host,
            port=profile.ssh.port,
            user=profile.ssh.user,
            host_key=profile.ssh.host_key,
            credential_ref=profile.ssh.credential_ref,
        ),
        privilege=profile.privilege,
        capabilities=list(profile.capabilities),
        revision=stored.revision,
        sha256=stored.sha256,
        projection=ProjectionView(
            revision=None if state is None else state.revision,
            stale=False if state is None else state.stale,
        ),
    )


# ---------------------------------------------------------------------------
# Guards and error mapping (C01 envelope; boundary owned by mai.api.auth)
# ---------------------------------------------------------------------------


def _canonical_service(request: Request) -> tuple[ProfileFileStore, ProfileProjection]:
    """The lifespan-built store+projection; without them fail closed."""
    store = getattr(request.app.state, "profiles", None)
    projection = getattr(request.app.state, "projection", None)
    if store is None or projection is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "canonical profile service is unavailable",
        )
    return store, projection


def _profile_error(request: Request, exc: Exception) -> JSONResponse:
    """Map producer/store/journal rejections onto the C01 envelope.

    Validation rejections carry the producer's stable field codes; state
    conflicts are value-free ``409 conflict`` bodies; everything unmapped
    bubbles to the app's generic ``internal_error`` handler.
    """
    if isinstance(exc, ProfileValidationError):
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "profile document failed validation",
            fields=exc.fields,
        )
    if isinstance(exc, ProfileFileError):
        if exc.code is FileStoreErrorCode.NOT_FOUND:
            return render_error_envelope(
                request,
                HTTP_404_NOT_FOUND,
                "not_found",
                "no profile stored for this server id",
            )
        if exc.code in _DOCUMENT_REJECTIONS:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "validation_failed",
                "profile document failed validation",
                fields={"document": exc.code.value},
            )
        if exc.code is FileStoreErrorCode.STALE_REVISION:
            return render_error_envelope(
                request,
                HTTP_409_CONFLICT,
                "conflict",
                "the profile changed since the last observed revision",
            )
        # path_escape, metadata_*, orphan_metadata, hash_mismatch: canonical
        # state needing recovery — surfaced, never silently repaired (C02).
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "canonical profile state requires recovery",
        )
    if isinstance(exc, ProjectionError):
        message = (
            "another profile write is in progress for this server"
            if exc.code is ProjectionErrorCode.INTENT_PENDING
            else "canonical profile state requires recovery"
        )
        return render_error_envelope(request, HTTP_409_CONFLICT, "conflict", message)
    raise exc  # unmapped failures take the app's generic internal_error path


# ---------------------------------------------------------------------------
# Routes (C01 prefix, envelope, identity; C02 pipeline; C08 boundary)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    response_model=ProfileMutationResult,
    summary="Create a server profile (mints the server identity)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Concurrent write in progress for the minted identity",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Profile document failed producer validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Canonical profile service unavailable",
        },
    },
)
async def create_profile(request: Request, payload: ProfileCreate) -> Response:
    """Mint an opaque ``server_id`` and store the initial profile (revision 1).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect; the
    write goes through the journal pipeline, so a crash mid-write is
    recovered by the projection card's sweep, never invented here.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store, projection = _canonical_service(request)
    server_id = uuid.uuid4()
    try:
        result = await projection.apply_upsert(
            store,
            server_id,
            _canonical_document(server_id, payload),
            expected_revision=0,
        )
    except (ProfileValidationError, ProfileFileError, ProjectionError) as exc:
        return _profile_error(request, exc)
    return _mutation_result(server_id, result)


@router.get(
    "/{server_id}",
    status_code=HTTP_200_OK,
    response_model=ProfileResponse,
    summary="Read one stored server profile",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for this server id",
        },
    },
)
async def read_profile(request: Request, server_id: uuid.UUID) -> Response:
    """Return the authoritative canonical file plus the projection view.

    Safe method: layer 1 only. The file is the authority (C02 W2); the
    projection's staleness flag is reported alongside (C08 rule 6), never
    hidden.
    """
    await _require_session(request)
    store, projection = _canonical_service(request)
    try:
        stored = store.read(server_id)
    except ProfileFileError as exc:
        return _profile_error(request, exc)
    profile = parse_server_profile_text(stored.text)
    state = await projection.read_projection(server_id)
    return _profile_view(profile, stored, state)


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=ProfileListResponse,
    summary="List every stored server profile",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Canonical profile state requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "A stored profile failed producer validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Canonical profile service unavailable",
        },
    },
)
async def list_profiles(request: Request) -> Response:
    """List every stored server profile in deterministic ``(name, id)`` order.

    Safe method: layer 1 only (no Origin/CSRF). Each ``servers/`` directory is
    read through the SAME canonical pipeline as the single read, so a list row
    and the single read of one server can never disagree. A directory whose
    name is not a canonical UUID is not a profile and is skipped; a missing
    canonical file (half-finished delete sweep state) is absent, not an error;
    any other store rejection or producer-validation failure is
    recovery-needed canonical state that surfaces (C02: shown, never silently
    repaired).
    """
    await _require_session(request)
    store, projection = _canonical_service(request)
    servers: list[ProfileResponse] = []
    servers_root = store.root / SERVERS_DIR
    if servers_root.is_dir():
        for directory in sorted(path for path in servers_root.iterdir() if path.is_dir()):
            try:
                server_id = uuid.UUID(directory.name)
            except ValueError:
                continue
            if str(server_id) != directory.name:
                continue  # non-canonical spelling is not a profile identity
            try:
                stored = store.read(server_id)
            except ProfileFileError as exc:
                if exc.code is FileStoreErrorCode.NOT_FOUND:
                    continue
                return _profile_error(request, exc)
            try:
                profile = parse_server_profile_text(stored.text)
            except ProfileValidationError as exc:
                return _profile_error(request, exc)
            state = await projection.read_projection(server_id)
            servers.append(_profile_view(profile, stored, state))
    servers.sort(key=lambda view: (view.name, str(view.server_id)))
    return ProfileListResponse(servers=servers)


@router.put(
    "/{server_id}",
    status_code=HTTP_200_OK,
    response_model=ProfileMutationResult,
    summary="Edit a server profile (optimistic revision, C01 rule 6)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for this server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or concurrent write",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Profile document failed producer validation",
        },
    },
)
async def edit_profile(
    request: Request, server_id: uuid.UUID, payload: ProfileEdit
) -> Response:
    """Revision-checked edit through the same canonical pipeline.

    A stale ``expected_revision`` is rejected BEFORE any journal row is
    created (read-only pre-check against the authoritative file), so the
    ordinary stale-client path leaves no live intent; a genuine concurrent
    race is still serialized by the journal (``409`` ``intent_pending``).
    Identity is the path UUID: renaming the display name retargets nothing.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store, projection = _canonical_service(request)
    try:
        current = store.read(server_id)
    except ProfileFileError as exc:
        return _profile_error(request, exc)
    if current.revision != payload.expected_revision:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "the profile changed since the last observed revision",
        )
    try:
        result = await projection.apply_upsert(
            store,
            server_id,
            _canonical_document(server_id, payload),
            expected_revision=payload.expected_revision,
        )
    except (ProfileValidationError, ProfileFileError, ProjectionError) as exc:
        return _profile_error(request, exc)
    return _mutation_result(server_id, result)


def register_servers(app: FastAPI) -> None:
    """Register the profile surface under the shared boundary (api-wiring)."""
    app.include_router(router)
