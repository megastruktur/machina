"""Focused tests for evidence-backed document generation (card 07-doc-generation).

Acceptance (BRIEF):

1. Completed/failed job evidence produces canonical server docs with
   verified/hypothesis/failed distinctions.
2. Automatic update is revision checked, secret filtered and reindexed;
   no fabricated success runbook after failure.

Boundary discipline (test policy): payload parsing, classification,
rendering, evidence-reference filtering and the revision-check negatives
run offline (pure functions + the real confined ``DocumentStore`` on
``tmp_path``). Every storage/reindex/retrieval claim runs against a real
disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) migrated through
the real Alembic chain, reconciled through the real ``07-chunk-index``
``ScopeIndexer`` and queried through the real ``07-retrieval``
``ScopeRetriever`` — citation resolution and stale-chunk removal are
proven by real SQL, not mocks. The CLI tests run the production module in
a real subprocess (fresh interpreter per invocation) against the fixture
database and a deterministic loopback OpenAI-compatible embedding server.

Feature-gate scenario (writer half): generated runbooks/incidents for two
fixture servers are committed, reindexed and retrieved per scope; the
failed run stays an incident, citations resolve to the exact source
revision/hash and a revised generation removes the stale chunks. The
assembled model-backed journey is owned by ``07-verify``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from mai.contracts.jobs import RunState, StepState
from mai.database import create_app_engine
from mai.knowledge import documents as knowledge_documents
from mai.knowledge import generate
from mai.knowledge.documents import (
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    SourceKind,
    VerificationStatus,
)
from mai.knowledge.generate import (
    DocClassification,
    DocGenerationError,
    DocGenerationErrorCode,
    DocGenerator,
    classify,
    doc_id_for_run,
    parse_job_evidence,
    render_document,
)
from mai.knowledge.index import (
    EMBEDDING_DIMENSIONS,
    ChunkEmbedder,
    IndexErrorCode,
    IndexingError,
    OutcomeStatus,
    ScopeIndexer,
)
from mai.knowledge.retrieve import ScopeRetriever

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Async storage tests run on the anyio pytest plugin (asyncio backend), the
# same discipline as tests/test_retrieval.py and test_index.py.
pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "6e4f5a1b-2c3d-4e5f-9a0b-1c2d3e4f5a60"
SERVER_B = "7a5b6c1d-8e2f-4a3b-9c4d-5e6f7a8b9c0d"
RUN_OK = "8b9c0d1e-2f3a-4b5c-8d7e-9f0a1b2c3d4e"
RUN_OK_B = "9c0d1e2f-3a4b-5c6d-9e8f-0a1b2c3d4e5f"
RUN_FAIL = "0d1e2f3a-4b5c-6d7e-8f9a-0b1c2d3e4f5a"
RUN_PARTIAL = "1e2f3a4b-5c6d-7e8f-9a0b-1c2d3e4f5a6b"

PRINCIPAL = "admin-fixture"

# Distinctive single-token markers for FTS-traceable retrieval assertions.
MARKER_A = "zetarestartomega"
MARKER_B = "bravorestoretheta"
MARKER_FAIL = "gammaincidentdelta"
MARKER_STEP = "epsilonevidenceeta"


def _evidence(
    *,
    run_id: str = RUN_OK,
    server_id: str = SERVER_A,
    app_id: str | None = None,
    status: str = "succeeded",
    host_verified: bool = True,
    summary: str = f"{MARKER_A} maintenance procedure completed.",
    error: str | None = None,
    captured_at: str | None = None,
    model: dict | None = None,
    steps: list[dict] | None = None,
) -> dict:
    if steps is None:
        steps = [
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "restart fixture service",
                "exit_code": 0,
                "evidence_refs": ["receipts/op-7d1c/state"],
            },
            {
                "seq": 2,
                "capability_id": "host.logs",
                "status": "succeeded",
                "verified": True,
                "display": "collect service logs",
                "exit_code": 0,
                "evidence_refs": [f"artifacts/{MARKER_STEP}.log"],
            },
        ]
    payload: dict = {
        "v": 1,
        "run_id": run_id,
        "server_id": server_id,
        "status": status,
        "host_verified": host_verified,
        "summary": summary,
        "steps": steps,
    }
    if app_id is not None:
        payload["app_id"] = app_id
    if error is not None:
        payload["error"] = error
    if captured_at is not None:
        payload["captured_at"] = captured_at
    if model is not None:
        payload["model"] = model
    return payload


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the reindex/retrieval claims need "
            "a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (D09): FK-safe and immune to future revisions
            # (the enumerated drop list went stale across migrations).
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
def sessions(migrated_url: str):
    """The async sessionmaker factory; cleanup runs on a throwaway loop."""
    engine, factory = create_app_engine(migrated_url)
    yield factory

    async def _clean() -> None:
        async with factory() as session:
            await session.execute(text("DELETE FROM knowledge_doc_chunks"))
            await session.execute(text("DELETE FROM knowledge_documents"))
            await session.commit()
        await engine.dispose()

    asyncio.run(_clean())


@pytest.fixture
def store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(tmp_path / "docs-root")


def _run(coro):
    """Run one coroutine on a throwaway loop (sync test helpers)."""
    return asyncio.run(coro)


class _HashedEmbedder(ChunkEmbedder):
    """Deterministic offline embedder: one unit vector per text content.

    Same fixture discipline as tests/test_retrieval.py: the basis dimension
    derives from the text hash, so vector neighborhoods are controlled
    exactly and offline.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE, "fixture endpoint down"
            )
        vectors = []
        for item in texts:
            dim = int.from_bytes(
                hashlib.sha256(item.encode("utf-8")).digest()[:8]
            ) % EMBEDDING_DIMENSIONS
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[dim] = 1.0
            vectors.append(vector)
        return vectors


def _indexer(sessions, store: DocumentStore, embedder: ChunkEmbedder) -> ScopeIndexer:
    return ScopeIndexer(sessions, store, embedder)


def _retriever(sessions, embedder: ChunkEmbedder) -> ScopeRetriever:
    return ScopeRetriever(sessions, embedder)


# ---------------------------------------------------------------------------
# Payload parsing: strict shape, frozen vocabulary, settled facts only
# ---------------------------------------------------------------------------


def test_parse_minimal_succeeded_payload() -> None:
    evidence = parse_job_evidence(_evidence())
    assert evidence.run_id == uuid.UUID(RUN_OK)
    assert evidence.server_id == uuid.UUID(SERVER_A)
    assert evidence.app_id is None
    assert evidence.status is RunState.SUCCEEDED
    assert evidence.host_verified is True
    assert len(evidence.steps) == 2
    assert evidence.steps[0].status is StepState.SUCCEEDED
    assert evidence.steps[0].evidence_refs == ("receipts/op-7d1c/state",)


def test_parse_full_failed_payload_with_model_pins() -> None:
    payload = _evidence(
        run_id=RUN_FAIL,
        status="failed",
        host_verified=True,
        summary=f"{MARKER_FAIL} restart loop detected.",
        error="exit code 1 on step 2",
        captured_at="2026-10-16T12:00:00Z",
        model={
            "connection": "local-gateway",
            "profile": "fast-operator",
            "connection_revision": 3,
            "profile_revision": 2,
        },
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "restart fixture service",
                "exit_code": 0,
                "evidence_refs": [],
            },
            {
                "seq": 2,
                "capability_id": "host.shell",
                "status": "failed",
                "verified": True,
                "display": "verify service health",
                "exit_code": 1,
                "evidence_refs": ["receipts/op-9e2f/state"],
            },
        ],
    )
    evidence = parse_job_evidence(payload)
    assert evidence.status is RunState.FAILED
    assert evidence.error == "exit code 1 on step 2"
    assert evidence.captured_at == "2026-10-16T12:00:00Z"
    assert evidence.model is not None
    assert evidence.model.connection_revision == 3
    assert evidence.steps[1].exit_code == 1


def test_parse_accepts_app_scope() -> None:
    app_id = "2f3a4b5c-6d7e-8f9a-0b1c-2d3e4f5a6b7c"
    evidence = parse_job_evidence(_evidence(app_id=app_id))
    assert evidence.app_id == uuid.UUID(app_id)


def test_parse_rejects_non_object_payload() -> None:
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(["not", "an", "object"])  # type: ignore[arg-type]
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


def test_parse_rejects_unknown_field() -> None:
    payload = _evidence()
    payload["server_label"] = "db-01"
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD
    assert "server_label" in excinfo.value.detail


def test_parse_rejects_missing_field() -> None:
    payload = _evidence()
    del payload["host_verified"]
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


def test_parse_rejects_wrong_version() -> None:
    payload = _evidence()
    payload["v"] = 2
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


def test_parse_rejects_non_canonical_uuid() -> None:
    payload = _evidence()
    payload["run_id"] = "not-a-uuid"
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "waiting_approval",
        "waiting_children",
        "reconciling",
        "cancelled",
    ],
)
def test_non_terminal_run_states_are_rejected(status: str) -> None:
    """Only settled completed/failed evidence may become a canonical doc."""
    payload = _evidence(status=status)
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.NON_TERMINAL_RUN


@pytest.mark.parametrize(
    "step_status", ["pending", "running", "outcome_unknown", "cancel_requested"]
)
def test_unsettled_step_states_are_rejected(step_status: str) -> None:
    payload = _evidence(
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": step_status,
                "verified": True,
                "display": "restart fixture service",
                "exit_code": None,
                "evidence_refs": [],
            }
        ]
    )
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.UNSETTLED_STEP


def test_parse_rejects_duplicate_step_sequence() -> None:
    steps = [
        {
            "seq": 1,
            "capability_id": "host.shell",
            "status": "succeeded",
            "verified": True,
            "display": "first",
            "exit_code": 0,
            "evidence_refs": [],
        },
        {
            "seq": 1,
            "capability_id": "host.shell",
            "status": "succeeded",
            "verified": True,
            "display": "duplicate sequence",
            "exit_code": 0,
            "evidence_refs": [],
        },
    ]
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(_evidence(steps=steps))
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


def test_parse_rejects_non_boolean_verified() -> None:
    payload = _evidence()
    payload["steps"][0]["verified"] = "yes"
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


def test_parse_rejects_nul_and_oversized_summary() -> None:
    payload = _evidence(summary="bad\x00summary")
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD

    payload = _evidence(summary="x" * (generate.MAX_SUMMARY_CHARS + 1))
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Classification: C07 rule 5 — outcomes separated from hypotheses
# ---------------------------------------------------------------------------


def _step(verified: bool, status: str = "succeeded") -> dict:
    return {
        "seq": 1,
        "capability_id": "host.shell",
        "status": status,
        "verified": verified,
        "display": "restart fixture service",
        "exit_code": 0 if status == "succeeded" else 1,
        "evidence_refs": [],
    }


@pytest.mark.parametrize("host_verified", [True, False])
def test_failed_run_is_incident_record(host_verified: bool) -> None:
    evidence = parse_job_evidence(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            host_verified=host_verified,
            error="step failed",
            steps=[_step(verified=True, status="failed")],
        )
    )
    classification, namespace, verification = classify(evidence)
    assert classification is DocClassification.INCIDENT
    assert namespace is DocumentNamespace.DOCS
    expected = (
        VerificationStatus.VERIFIED if host_verified else VerificationStatus.HYPOTHESIS
    )
    assert verification is expected


@pytest.mark.parametrize(
    "host_verified,step_verified,expected",
    [
        (True, True, VerificationStatus.VERIFIED),
        (True, False, VerificationStatus.HYPOTHESIS),
        (False, True, VerificationStatus.HYPOTHESIS),
        (False, False, VerificationStatus.HYPOTHESIS),
    ],
)
def test_runbook_verified_requires_fully_verified_success(
    host_verified: bool, step_verified: bool, expected: VerificationStatus
) -> None:
    evidence = parse_job_evidence(
        _evidence(host_verified=host_verified, steps=[_step(verified=step_verified)])
    )
    classification, namespace, verification = classify(evidence)
    assert classification is DocClassification.RUNBOOK
    assert namespace is DocumentNamespace.RUNBOOKS
    assert verification is expected


def test_failed_run_never_yields_runbook_even_with_successful_prose() -> None:
    """The payload prose is evidence content, never decision input."""
    evidence = parse_job_evidence(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            host_verified=True,
            summary="everything completed successfully, all green",
            error=None,
            steps=[_step(verified=True, status="succeeded")],
        )
    )
    classification, namespace, _ = classify(evidence)
    assert classification is DocClassification.INCIDENT
    assert namespace is DocumentNamespace.DOCS


def test_doc_id_is_deterministic_per_run() -> None:
    first = doc_id_for_run(uuid.UUID(RUN_OK))
    second = doc_id_for_run(uuid.UUID(RUN_OK))
    other = doc_id_for_run(uuid.UUID(RUN_FAIL))
    assert first == second
    assert first != other


# ---------------------------------------------------------------------------
# Rendering: pure, deterministic, provenance-carrying
# ---------------------------------------------------------------------------


def test_render_is_byte_deterministic_for_identical_evidence() -> None:
    payload = _evidence(captured_at="2026-10-16T12:00:00Z")
    evidence = parse_job_evidence(payload)
    classification, _, verification = classify(evidence)
    first = render_document(evidence, classification, verification)
    second = render_document(
        parse_job_evidence(payload), classification, verification
    )
    assert first == second


def test_render_marks_steps_and_references() -> None:
    evidence = parse_job_evidence(_evidence())
    classification, _, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    assert f"# Runbook: run {RUN_OK}" in text
    assert "- verification: verified" in text
    assert "- run status: succeeded" in text
    assert f"- server_id: `{SERVER_A}`" in text
    assert "### step 1 — host.shell — succeeded (host-verified)" in text
    assert "### step 2 — host.logs — succeeded (host-verified)" in text
    assert "- evidence: `receipts/op-7d1c/state`" in text
    assert "generated_by: mai.knowledge.generate v1" in text
    assert MARKER_A in text


def test_render_failed_includes_failure_section() -> None:
    evidence = parse_job_evidence(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            summary=f"{MARKER_FAIL} restart loop detected.",
            error="exit code 1 on step 2",
            steps=[_step(verified=True, status="failed")],
        )
    )
    classification, _, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    assert "# Incident: run" in text
    assert "## Failure" in text
    assert "exit code 1 on step 2" in text


def test_render_failed_without_error_uses_honest_default() -> None:
    evidence = parse_job_evidence(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            host_verified=False,
            steps=[_step(verified=False, status="failed")],
        )
    )
    classification, _, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    assert "no bounded error text was recorded" in text
    assert "- verification: hypothesis" in text


def test_render_records_model_pins_and_captured_at() -> None:
    evidence = parse_job_evidence(
        _evidence(
            captured_at="2026-10-16T12:00:00Z",
            model={
                "connection": "local-gateway",
                "profile": "fast-operator",
                "connection_revision": 3,
                "profile_revision": 2,
            },
        )
    )
    classification, _, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    assert "- model: local-gateway@3 / fast-operator@2" in text
    assert "- captured_at: 2026-10-16T12:00:00Z" in text


def test_render_without_captured_at_carries_no_clock_line() -> None:
    evidence = parse_job_evidence(_evidence())
    classification, _, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    assert "captured_at" not in text


# ---------------------------------------------------------------------------
# Evidence references: structural secret boundary (C03 rule 6 posture)
# ---------------------------------------------------------------------------


def test_secret_name_denylist_is_the_documents_authority() -> None:
    """The deny-list cannot drift: generate imports the doc-store guard."""
    assert generate._is_secret_name is knowledge_documents._is_secret_name


@pytest.mark.parametrize(
    "ref",
    [
        ".env",
        ".env.local",
        ".env.production",
        "receipts/id_rsa",
        "receipts/server.key",
        "artifacts/cert.pem",
        "backup/creds.p12",
        "backup/keystore.pfx",
        "keys/id_ed25519",
        "secrets/ID_RSA",
    ],
)
def test_evidence_refs_with_credential_material_names_rejected(ref: str) -> None:
    payload = _evidence(
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "collect",
                "exit_code": 0,
                "evidence_refs": [ref],
            }
        ]
    )
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.SECRET_REFERENCE


@pytest.mark.parametrize(
    "ref",
    [
        "..",
        "../receipts/op-1",
        "receipts/../op-1",
        "receipts//op-1",
        "./op-1",
        "receipts/./op-1",
        "receipts\\op-1",
        "receipts/op-\x001",
        "",
        "x" * (generate.MAX_EVIDENCE_REF_CHARS + 1),
    ],
)
def test_evidence_refs_with_escape_or_malformed_segments_rejected(ref: str) -> None:
    payload = _evidence(
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "collect",
                "exit_code": 0,
                "evidence_refs": [ref],
            }
        ]
    )
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code in (
        DocGenerationErrorCode.INVALID_EVIDENCE_REF,
        DocGenerationErrorCode.SECRET_REFERENCE,
    )


def test_evidence_refs_allow_absolute_host_paths_and_artifact_handles() -> None:
    """Real host-capability references (receipt dirs, artifacts) are opaque
    citations — validated structurally, never opened."""
    payload = _evidence(
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "restart",
                "exit_code": 0,
                "evidence_refs": [
                    "/var/lib/mai/receipts/7d1c",
                    "controller/intents/7d1c.json",
                    f"artifacts/{MARKER_STEP}.log",
                ],
            }
        ]
    )
    evidence = parse_job_evidence(payload)
    assert evidence.steps[0].evidence_refs[0] == "/var/lib/mai/receipts/7d1c"


def test_too_many_evidence_refs_rejected() -> None:
    payload = _evidence(
        steps=[
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": "succeeded",
                "verified": True,
                "display": "collect",
                "exit_code": 0,
                "evidence_refs": [f"receipts/op-{index}" for index in range(17)],
            }
        ]
    )
    with pytest.raises(DocGenerationError) as excinfo:
        parse_job_evidence(payload)
    assert excinfo.value.code is DocGenerationErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Generation through the canonical store: revision-checked, provenance-carrying
# ---------------------------------------------------------------------------


async def test_generate_writes_verified_runbook(store) -> None:
    generator = DocGenerator(store)
    result = await generator.generate(_evidence())
    assert result.classification is DocClassification.RUNBOOK
    assert result.verification is VerificationStatus.VERIFIED
    assert result.doc.revision == 1
    assert result.doc.namespace is DocumentNamespace.RUNBOOKS
    assert result.doc.source_kind is SourceKind.GENERATED
    assert result.doc.origin == f"run:{RUN_OK}"
    assert result.doc.doc_id == doc_id_for_run(uuid.UUID(RUN_OK))
    assert result.reindex is None  # no reconcile hook: honest, not fabricated
    assert MARKER_A in result.doc.text


async def test_generate_failed_run_commits_incident_record(store) -> None:
    generator = DocGenerator(store)
    result = await generator.generate(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            summary=f"{MARKER_FAIL} restart loop detected.",
            error="exit code 1 on step 2",
            steps=[_step(verified=True, status="failed")],
        )
    )
    assert result.classification is DocClassification.INCIDENT
    assert result.doc.namespace is DocumentNamespace.DOCS
    assert "## Failure" in result.doc.text
    stored = store.read(
        result.doc.doc_id, server_id=uuid.UUID(SERVER_A), namespace=DocumentNamespace.DOCS
    )
    assert stored.revision == 1
    assert stored.verification is VerificationStatus.VERIFIED


async def test_second_generation_revises_the_same_canonical_document(store) -> None:
    generator = DocGenerator(store)
    first = await generator.generate(_evidence())
    second = await generator.generate(
        _evidence(summary=f"{MARKER_A} revised procedure, second pass.")
    )
    assert second.doc.doc_id == first.doc.doc_id
    assert second.doc.revision == 2
    assert first.doc.sha256 != second.doc.sha256
    current = store.read(
        first.doc.doc_id,
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
    )
    assert current.revision == 2
    assert "second pass" in current.text


async def test_stale_intervening_write_surfaces_conflict(store, monkeypatch) -> None:
    """An automatic update is revision checked: an intervening committed
    revision between the generator's read and write is a surfaced
    stale_revision conflict, never a silent clobber."""
    generator = DocGenerator(store)
    first = await generator.generate(_evidence())
    real_write = store.write

    def intervening(doc_id, text, **kwargs):
        current = store.read(
            doc_id,
            server_id=kwargs.get("server_id"),
            app_id=kwargs.get("app_id"),
            namespace=kwargs.get("namespace"),
        )
        real_write(
            doc_id,
            "intervening operator revision",
            server_id=kwargs.get("server_id"),
            app_id=kwargs.get("app_id"),
            namespace=kwargs.get("namespace"),
            expected_revision=current.revision,
            source_kind=SourceKind.AUTHORED,
            origin="operator",
        )
        return real_write(doc_id, text, **kwargs)

    monkeypatch.setattr(store, "write", intervening)
    with pytest.raises(DocumentStoreError) as excinfo:
        await generator.generate(
            _evidence(summary=f"{MARKER_A} automatic update attempt.")
        )
    assert excinfo.value.code is DocumentStoreErrorCode.STALE_REVISION
    current = store.read(
        first.doc.doc_id,
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
    )
    assert current.text == "intervening operator revision"


async def test_live_tombstone_conflict_is_surfaced(store) -> None:
    """C02 W4: a generation retry against a deleted document (live
    tombstone intent) is a surfaced conflict, never a silent resurrection.
    The doc-store read sees the absent file; its write then refuses the
    stale recreate against the tombstone revision."""
    generator = DocGenerator(store)
    first = await generator.generate(_evidence())
    store.delete(
        first.doc.doc_id,
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        expected_revision=first.doc.revision,
    )
    with pytest.raises(DocumentStoreError) as excinfo:
        await generator.generate(
            _evidence(summary=f"{MARKER_A} resurrection attempt.")
        )
    assert excinfo.value.code is DocumentStoreErrorCode.STALE_REVISION
    assert "tombstone" in str(excinfo.value)


async def test_app_scoped_generation_uses_app_collection(store) -> None:
    app_id = "2f3a4b5c-6d7e-8f9a-0b1c-2d3e4f5a6b7c"
    generator = DocGenerator(store)
    result = await generator.generate(_evidence(app_id=app_id))
    assert result.doc.app_id == uuid.UUID(app_id)
    canonical = (
        store.root
        / "servers"
        / SERVER_A
        / "applications"
        / app_id
        / "runbooks"
        / f"{result.doc.doc_id}.md"
    )
    assert canonical.is_file()
    stored = store.read(
        result.doc.doc_id,
        server_id=uuid.UUID(SERVER_A),
        app_id=uuid.UUID(app_id),
        namespace=DocumentNamespace.RUNBOOKS,
    )
    assert stored.app_id == uuid.UUID(app_id)


# ---------------------------------------------------------------------------
# Durable reindex + retrieval integration (real disposable Postgres fixture)
# ---------------------------------------------------------------------------


async def test_generate_then_reconcile_indexes_the_document(store, sessions) -> None:
    generator = DocGenerator(store)
    result = await generator.generate(_evidence())
    embedder = _HashedEmbedder()
    outcomes = await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.doc_id == result.doc.doc_id
    assert outcome.status is OutcomeStatus.INDEXED
    assert outcome.revision == 1
    assert outcome.chunks >= 1
    # The indexer acked: the durable intent is purged (C02 W4 ordering).
    assert store.read_intents(server_id=uuid.UUID(SERVER_A)) == []


async def test_generated_document_citations_resolve(store, sessions) -> None:
    """References resolve: retrieval citations name the exact generated doc
    id, source revision and content hash."""
    generator = DocGenerator(store)
    result = await generator.generate(_evidence())
    embedder = _HashedEmbedder()
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    retrieval = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        query=MARKER_A,
        limit=5,
    )
    assert retrieval.status == "ok"
    matching = [
        excerpt for excerpt in retrieval.excerpts if excerpt.doc_id == result.doc.doc_id
    ]
    assert matching, "generated runbook must be retrievable by its marker"
    for excerpt in matching:
        assert excerpt.revision == result.doc.revision
        assert excerpt.sha256 == result.doc.sha256
        assert excerpt.namespace == DocumentNamespace.RUNBOOKS.value
        assert excerpt.source_kind == SourceKind.GENERATED.value
        assert excerpt.origin == f"run:{RUN_OK}"


async def test_revision_update_removes_stale_chunks(store, sessions) -> None:
    """C07 rule 2: after a revised generation is reconciled, retrieval sees
    only the current revision."""
    generator = DocGenerator(store)
    embedder = _HashedEmbedder()
    first = await generator.generate(_evidence())
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    second = await generator.generate(
        _evidence(summary=f"{MARKER_A} revised procedure, second pass.")
    )
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    retrieval = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        query=MARKER_A,
        limit=10,
    )
    assert retrieval.status == "ok"
    assert retrieval.excerpts
    for excerpt in retrieval.excerpts:
        if excerpt.doc_id == second.doc.doc_id:
            assert excerpt.revision == 2
            assert excerpt.sha256 == second.doc.sha256
    assert all(
        excerpt.doc_id != first.doc.doc_id or excerpt.revision == 2
        for excerpt in retrieval.excerpts
    )
    assert first.doc.sha256 != second.doc.sha256


async def test_failed_incident_is_retrievable_in_docs_scope(store, sessions) -> None:
    generator = DocGenerator(store)
    result = await generator.generate(
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            summary=f"{MARKER_FAIL} restart loop detected on the fixture host.",
            error="exit code 1 on step 2",
            steps=[_step(verified=True, status="failed")],
        )
    )
    assert result.classification is DocClassification.INCIDENT
    embedder = _HashedEmbedder()
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    retrieval = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.DOCS,
        query=MARKER_FAIL,
        limit=5,
    )
    assert retrieval.status == "ok"
    assert any(excerpt.doc_id == result.doc.doc_id for excerpt in retrieval.excerpts)
    # The runbooks collection of the same scope never carries the incident.
    runbooks = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        query=MARKER_FAIL,
        limit=5,
    )
    assert all(
        excerpt.doc_id != result.doc.doc_id for excerpt in runbooks.excerpts
    )


async def test_generated_docs_are_isolated_per_server(store, sessions) -> None:
    """KB-FILTER writer half: conflicting generated docs for two servers
    resolve disjointly per scope."""
    generator = DocGenerator(store)
    embedder = _HashedEmbedder()
    result_a = await generator.generate(
        _evidence(summary=f"{MARKER_A} restart procedure for server A.")
    )
    result_b = await generator.generate(
        _evidence(
            run_id=RUN_OK_B,
            server_id=SERVER_B,
            summary=f"{MARKER_B} restart procedure for server B.",
            steps=[
                {
                    "seq": 1,
                    "capability_id": "host.shell",
                    "status": "succeeded",
                    "verified": True,
                    "display": "restart fixture service",
                    "exit_code": 0,
                    "evidence_refs": [f"receipts/{MARKER_B}/state"],
                }
            ],
        )
    )
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    await _indexer(sessions, store, embedder).reconcile_scope(
        server_id=uuid.UUID(SERVER_B)
    )
    retrieval_a = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        query=MARKER_A,
        limit=10,
    )
    ids_a = {excerpt.doc_id for excerpt in retrieval_a.excerpts}
    assert result_a.doc.doc_id in ids_a
    assert result_b.doc.doc_id not in ids_a
    retrieval_b = await _retriever(sessions, embedder).retrieve(
        server_id=uuid.UUID(SERVER_B),
        namespace=DocumentNamespace.RUNBOOKS,
        query=MARKER_B,
        limit=10,
    )
    ids_b = {excerpt.doc_id for excerpt in retrieval_b.excerpts}
    assert result_b.doc.doc_id in ids_b
    assert result_a.doc.doc_id not in ids_b


async def test_reconcile_failure_keeps_pending_intent_honest(store, sessions) -> None:
    """An embedding-seam outage leaves the durable intent pending; the
    generation result says so instead of fabricating a reindex."""
    generator = DocGenerator(store)
    result = await generator.generate(_evidence())
    failing = _HashedEmbedder(fail=True)
    outcomes = await _indexer(sessions, store, failing).reconcile_scope(
        server_id=uuid.UUID(SERVER_A)
    )
    assert len(outcomes) == 1
    assert outcomes[0].status is OutcomeStatus.PENDING
    pending = store.read_intents(server_id=uuid.UUID(SERVER_A))
    assert len(pending) == 1
    assert pending[0].doc_id == result.doc.doc_id


async def test_reindex_hook_reports_real_outcomes(store, sessions) -> None:
    async def _reconcile():
        return await _indexer(sessions, store, _HashedEmbedder()).reconcile_scope(
            server_id=uuid.UUID(SERVER_A)
        )

    generator = DocGenerator(store, reconcile=_reconcile)
    result = await generator.generate(_evidence())
    assert result.reindex is not None
    outcome = result.reindex[0]
    assert outcome.status is OutcomeStatus.INDEXED  # type: ignore[attr-defined]
    assert store.read_intents(server_id=uuid.UUID(SERVER_A)) == []


# ---------------------------------------------------------------------------
# Production entrypoint: real subprocesses
# ---------------------------------------------------------------------------


class _EmbeddingHandler(BaseHTTPRequestHandler):
    """Local OpenAI-compatible /v1/embeddings fixture (deterministic)."""

    embedder = _HashedEmbedder()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if self.path != "/v1/embeddings":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        vectors = _run(self.embedder.embed(list(body["input"])))
        payload = json.dumps(
            {
                "object": "list",
                "data": [
                    {"index": index, "embedding": vector}
                    for index, vector in enumerate(vectors)
                ],
                "model": body["model"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


@pytest.fixture
def embedding_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def _write_evidence(tmp_path: Path, payload: dict, name: str = "evidence.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_cli(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mai.knowledge.generate", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND_ROOT,
        timeout=120,
    )


def test_cli_preview_valid_evidence(tmp_path: Path) -> None:
    path = _write_evidence(tmp_path, _evidence())
    proc = _run_cli(["preview", str(path)])
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "preview"
    assert document["classification"] == "runbook"
    assert document["namespace"] == "runbooks"
    assert document["verification"] == "verified"
    assert document["doc_id"] == str(doc_id_for_run(uuid.UUID(RUN_OK)))
    assert document["sha256"] != ""
    assert proc.stderr == ""


def test_cli_preview_rejected_evidence_exits_1(tmp_path: Path) -> None:
    path = _write_evidence(tmp_path, _evidence(status="running"))
    proc = _run_cli(["preview", str(path)])
    assert proc.returncode == 1
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "error"
    assert document["code"] == "non_terminal_run"


@pytest.mark.parametrize(
    "name",
    [".env.json", "id_rsa", "leaked.pem"],
)
def test_cli_preview_secret_named_evidence_file_rejected(tmp_path: Path, name: str) -> None:
    path = _write_evidence(tmp_path, _evidence(), name=name)
    proc = _run_cli(["preview", str(path)])
    assert proc.returncode == 1
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "error"
    assert document["code"] == "secret_reference"


def test_cli_preview_symlink_evidence_file_rejected(tmp_path: Path) -> None:
    real = _write_evidence(tmp_path, _evidence(), name="real-evidence.json")
    link = tmp_path / "linked-evidence.json"
    link.symlink_to(real)
    proc = _run_cli(["preview", str(link)])
    assert proc.returncode == 1
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "error"


def test_cli_preview_non_json_evidence_rejected(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"\xff\xfe not json")
    proc = _run_cli(["preview", str(path)])
    assert proc.returncode == 1
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "error"
    assert document["code"] == "invalid_payload"


def test_cli_commit_without_reindex_leaves_durable_intent(
    tmp_path: Path, store: DocumentStore
) -> None:
    path = _write_evidence(tmp_path, _evidence())
    proc = _run_cli(["commit", str(store.root), str(path)])
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "committed"
    assert document["classification"] == "runbook"
    assert document["namespace"] == "runbooks"
    assert document["verification"] == "verified"
    assert document["revision"] == 1
    assert document["source_kind"] == "generated"
    assert document["reindex"] is None
    intents = store.read_intents(server_id=uuid.UUID(SERVER_A))
    assert len(intents) == 1
    assert intents[0].doc_id == uuid.UUID(document["doc_id"])


def test_cli_commit_with_reindex_real_subprocess(
    tmp_path: Path,
    store: DocumentStore,
    migrated_url: str,
    embedding_server: str,
) -> None:
    """``commit --base-url`` writes the canonical doc and reconciles the
    scope's intent outbox through the real chunk indexer."""
    path = _write_evidence(tmp_path, _evidence())
    env = dict(os.environ, MAI_DATABASE_URL=migrated_url)
    proc = _run_cli(
        ["commit", str(store.root), str(path), "--base-url", embedding_server],
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip())
    assert document["kind"] == "committed"
    assert document["revision"] == 1
    assert document["reindex"] is not None
    assert document["reindex"][0]["status"] == "indexed"
    assert document["reindex"][0]["chunks"] >= 1
    # The projection now answers retrieval for the generated runbook.
    engine, sessions = create_app_engine(migrated_url)

    async def _check() -> None:
        embedder = _HashedEmbedder()
        retrieval = await _retriever(sessions, embedder).retrieve(
            server_id=uuid.UUID(SERVER_A),
            namespace=DocumentNamespace.RUNBOOKS,
            query=MARKER_A,
            limit=5,
        )
        assert retrieval.status == "ok"
        assert any(
            excerpt.doc_id == uuid.UUID(document["doc_id"])
            and excerpt.sha256 == document["sha256"]
            for excerpt in retrieval.excerpts
        )

    try:
        _run(_check())
    finally:
        _run(engine.dispose())


def test_cli_commit_failed_run_produces_incident_not_runbook(
    tmp_path: Path, store: DocumentStore
) -> None:
    path = _write_evidence(
        tmp_path,
        _evidence(
            run_id=RUN_FAIL,
            status="failed",
            summary=f"{MARKER_FAIL} restart loop detected.",
            error="exit code 1 on step 2",
            steps=[_step(verified=True, status="failed")],
        ),
    )
    proc = _run_cli(["commit", str(store.root), str(path)])
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip())
    assert document["classification"] == "incident"
    assert document["namespace"] == "docs"
    assert document["verification"] == "verified"


def test_cli_commit_stale_evidence_revision_conflicts(
    tmp_path: Path, store: DocumentStore
) -> None:
    """A second commit of evolved evidence revises; an evidence file whose
    derived write no longer matches current state fails closed."""
    first = _write_evidence(tmp_path, _evidence(), name="first.json")
    proc = _run_cli(["commit", str(store.root), str(first)])
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip())
    assert document["revision"] == 1
    # Tamper the canonical content out-of-band: the next automatic update
    # must surface the hash/revision conflict instead of clobbering.
    doc_id = uuid.UUID(document["doc_id"])
    target = (
        store.root
        / "servers"
        / SERVER_A
        / "runbooks"
        / f"{doc_id}.md"
    )
    target.write_text("# externally modified\n", encoding="utf-8")
    second = _write_evidence(
        tmp_path,
        _evidence(summary=f"{MARKER_A} revised procedure, second pass."),
        name="second.json",
    )
    proc = _run_cli(["commit", str(store.root), str(second)])
    assert proc.returncode == 1
    error = json.loads(proc.stdout.strip())
    assert error["kind"] == "error"
    assert error["code"] == "hash_mismatch"
