"""Focused contract tests for the canonical document store (card 07-doc-store).

Acceptance criteria (card brief):
1. Canonical Markdown/revision/source/verification metadata scoped to
   server/app; outside docs import explicit.
2. Secret files/path escapes rejected; update/delete emits durable reindex
   intent.

Contract anchors: contracts.md C02 (rules 1-5, crash windows W1/W2/W4) and
C07 (rule 1 source record, rule 2 stale-chunk removal via the durable
reindex intent, rule 5 verification boundary). All tests run on a real
temporary filesystem (``tmp_path``) — confinement, atomicity and the secret
boundary are real-boundary claims and use no mocks.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mai.knowledge.documents import (
    ABSENT_REVISION,
    MAX_DOCUMENT_BYTES,
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    SourceKind,
    VerificationStatus,
)

SERVER = uuid.UUID("11111111-1111-1111-1111-111111111111")
SERVER_2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
APP = uuid.UUID("33333333-3333-3333-3333-333333333333")
DOC = uuid.UUID("44444444-4444-4444-4444-444444444444")
DOC_2 = uuid.UUID("55555555-5555-5555-5555-555555555555")

MARKDOWN_1 = "# Install\n\n1. apt update\n"
MARKDOWN_2 = "# Install\n\n1. apt update\n2. apt install nginx\n"


def _store(root: Path) -> DocumentStore:
    return DocumentStore(root)


def _write(
    store: DocumentStore,
    text: str = MARKDOWN_1,
    *,
    doc_id: uuid.UUID = DOC,
    server_id: uuid.UUID | None = SERVER,
    app_id: uuid.UUID | None = None,
    namespace: DocumentNamespace = DocumentNamespace.DOCS,
    expected_revision: int = ABSENT_REVISION,
    **kwargs: object,
):
    return store.write(
        doc_id,
        text,
        server_id=server_id,
        app_id=app_id,
        namespace=namespace,
        expected_revision=expected_revision,
        **kwargs,
    )


def _scope_paths(
    root: Path,
    doc_id: uuid.UUID,
    *,
    server_id: uuid.UUID | None = SERVER,
    app_id: uuid.UUID | None = None,
    namespace: str = "docs",
) -> tuple[Path, Path, Path]:
    if server_id is None:
        collection = root / "shared" / namespace
    elif app_id is not None:
        collection = (
            root / "servers" / str(server_id) / "applications" / str(app_id) / namespace
        )
    else:
        collection = root / "servers" / str(server_id) / namespace
    return (
        collection / f"{doc_id}.md",
        collection / f"{doc_id}.doc.meta.json",
        collection / "reindex" / f"{doc_id}.json",
    )


# ---------------------------------------------------------------------------
# AC1: canonical Markdown + revision + provenance, scoped; explicit import
# ---------------------------------------------------------------------------


def test_create_write_read_roundtrip_revision_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    assert stored.revision == 1
    assert stored.sha256 == hashlib.sha256(MARKDOWN_1.encode()).hexdigest()
    assert stored.namespace is DocumentNamespace.DOCS
    read_back = store.read(DOC, server_id=SERVER)
    assert read_back.text == MARKDOWN_1
    assert read_back == stored


def test_update_bumps_revision_and_replaces_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    updated = _write(store, MARKDOWN_2, expected_revision=1)
    assert updated.revision == 2
    assert updated.sha256 == hashlib.sha256(MARKDOWN_2.encode()).hexdigest()
    assert store.read(DOC, server_id=SERVER).text == MARKDOWN_2


def test_canonical_layout_and_permissions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    doc_path, meta_path, intent_path = _scope_paths(tmp_path, DOC)
    assert doc_path.is_file() and stat.S_IMODE(doc_path.stat().st_mode) == 0o600
    assert meta_path.is_file() and stat.S_IMODE(meta_path.stat().st_mode) == 0o600
    assert intent_path.is_file()
    for directory in (
        tmp_path,
        tmp_path / "servers",
        tmp_path / "servers" / str(SERVER),
        tmp_path / "servers" / str(SERVER) / "docs",
        tmp_path / "servers" / str(SERVER) / "docs" / "reindex",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_metadata_sidecar_carries_revision_hash_and_provenance(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write(
        store,
        source_kind=SourceKind.AUTHORED,
        origin="admin-console",
        origin_revision="v3",
        verification=VerificationStatus.VERIFIED,
    )
    _, meta_path, _ = _scope_paths(tmp_path, DOC)
    meta = json.loads(meta_path.read_text())
    assert meta["format"] == 1
    assert meta["doc_id"] == str(DOC)
    assert meta["server_id"] == str(SERVER)
    assert meta["app_id"] is None
    assert meta["namespace"] == "docs"
    assert meta["revision"] == 1
    assert meta["sha256"] == hashlib.sha256(MARKDOWN_1.encode()).hexdigest()
    assert meta["source_kind"] == "authored"
    assert meta["origin"] == "admin-console"
    assert meta["origin_revision"] == "v3"
    assert meta["verification"] == "verified"


def test_provenance_defaults_and_generated_hypothesis_boundary(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stored = _write(
        store,
        source_kind=SourceKind.GENERATED,
        origin="job:run-1",
        verification=VerificationStatus.HYPOTHESIS,
    )
    assert stored.source_kind is SourceKind.GENERATED
    assert stored.verification is VerificationStatus.HYPOTHESIS
    read_back = store.read(DOC, server_id=SERVER)
    assert read_back.origin == "job:run-1"
    assert read_back.verification is VerificationStatus.HYPOTHESIS


@pytest.mark.parametrize(
    "namespace,scope_kwargs",
    [
        (DocumentNamespace.DOCS, {}),
        (DocumentNamespace.RUNBOOKS, {}),
        (DocumentNamespace.DOCS, {"app_id": APP}),
        (DocumentNamespace.RUNBOOKS, {"app_id": APP}),
        (DocumentNamespace.DOCS, {"server_id": None}),
        (DocumentNamespace.RUNBOOKS, {"server_id": None}),
    ],
)
def test_scopes_server_app_shared_are_isolated_canonical_paths(
    tmp_path: Path,
    namespace: DocumentNamespace,
    scope_kwargs: dict[str, object],
) -> None:
    store = _store(tmp_path)
    server_id = scope_kwargs.get("server_id", SERVER)
    app_id = scope_kwargs.get("app_id")
    stored = _write(
        store, namespace=namespace, server_id=server_id, app_id=app_id
    )
    assert stored.namespace is namespace
    read_back = store.read(
        DOC, server_id=server_id, app_id=app_id, namespace=namespace
    )
    assert read_back.text == MARKDOWN_1
    # The same doc id in a different scope is a different document.
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER_2, namespace=namespace)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND


def test_app_scope_requires_server(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, app_id=APP, server_id=None)
    assert excinfo.value.code is DocumentStoreErrorCode.APP_REQUIRES_SERVER


def test_import_outside_file_is_explicit_and_provenance_marked(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-readme.md"
    outside.write_text("# External\n\nImported body.\n", encoding="utf-8")
    store = _store(tmp_path)
    stored = store.import_document(outside, server_id=SERVER, doc_id=DOC)
    assert stored.revision == 1
    assert stored.source_kind is SourceKind.IMPORTED
    assert stored.origin.startswith("import:")
    assert stored.origin.endswith("outside-readme.md")
    assert stored.text == "# External\n\nImported body.\n"
    assert store.read(DOC, server_id=SERVER).text == stored.text
    outside.unlink()


def test_import_generates_doc_id_when_absent(tmp_path: Path) -> None:
    outside = tmp_path.parent / "notes.md"
    outside.write_text("# Notes\n", encoding="utf-8")
    store = _store(tmp_path)
    stored = store.import_document(outside, server_id=SERVER)
    assert stored.doc_id is not None
    assert store.read(stored.doc_id, server_id=SERVER).text == "# Notes\n"
    outside.unlink()


def test_import_rejects_non_uuid_doc_id_and_bad_expected_revision(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "notes.md"
    outside.write_text("# Notes\n", encoding="utf-8")
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(
            outside, server_id=SERVER, doc_id="not-a-uuid"  # type: ignore[arg-type]
        )
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_DOC_ID
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(
            outside, server_id=SERVER, doc_id=DOC, expected_revision=-1
        )
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_REVISION
    outside.unlink()


def test_restart_reconstruction_reads_revision_and_provenance(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write(
        store,
        origin_revision="r7",
        verification=VerificationStatus.VERIFIED,
    )
    # Simulate process restart: a brand new store on the same root.
    restarted = _store(tmp_path)
    read_back = restarted.read(DOC, server_id=SERVER)
    assert read_back.revision == 1
    assert read_back.origin_revision == "r7"
    assert read_back.verification is VerificationStatus.VERIFIED
    updated = restarted.write(
        DOC, MARKDOWN_2, server_id=SERVER, expected_revision=1
    )
    assert updated.revision == 2


def test_invalid_identity_shapes_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read("44444444-4444-4444-4444-444444444444", server_id=SERVER)  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_DOC_ID
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=str(SERVER))  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_SERVER_ID
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER, app_id=str(APP))  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_APP_ID
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER, namespace="docs")  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_NAMESPACE


def test_invalid_documents_rejected_before_any_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, "bad\x00doc")
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_DOCUMENT
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, "x" * (MAX_DOCUMENT_BYTES + 1))
    assert excinfo.value.code is DocumentStoreErrorCode.DOCUMENT_TOO_LARGE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, "\udcff")  # lone surrogates do not encode as UTF-8
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_DOCUMENT
    _, meta_path, intent_path = _scope_paths(tmp_path, DOC)
    assert not meta_path.exists() and not intent_path.exists()


def test_invalid_provenance_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, origin="")
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_PROVENANCE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, origin_revision="")
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_PROVENANCE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, origin_revision="x" * 129)
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_PROVENANCE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, source_kind="leaked")  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_PROVENANCE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, verification="confirmed")  # type: ignore[arg-type]
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_PROVENANCE


# ---------------------------------------------------------------------------
# AC2: secret files / path escapes rejected; durable reindex intents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        ".env.local",
        "id_rsa",
        "id_ed25519",
        "server.key",
        "tls.pem",
        "bundle.p12",
        "keystore.pfx",
    ],
)
def test_import_secret_material_names_rejected_without_materializing(
    tmp_path: Path, name: str
) -> None:
    outside_dir = tmp_path.parent / "outside-secret-fixture"
    outside_dir.mkdir(exist_ok=True)
    secret_file = outside_dir / name
    secret_file.write_text("SENTINEL_SHOULD_NEVER_BE_STORED=1\n", encoding="utf-8")
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(secret_file, server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.SECRET_FILE_FORBIDDEN
    detail = excinfo.value.detail
    assert "SENTINEL" not in detail and "=" not in detail.split(name)[-1]
    # No indexed material: no canonical file, no metadata, no intent, and a
    # full-tree sweep finds no trace of the sentinel bytes.
    doc_path, meta_path, intent_path = _scope_paths(tmp_path, DOC)
    assert not doc_path.exists() and not meta_path.exists()
    assert not intent_path.exists()
    sweep = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert b"SENTINEL_SHOULD_NEVER_BE_STORED" not in sweep
    secret_file.unlink()


def test_import_normal_names_allowed(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside-normal-fixture"
    outside_dir.mkdir(exist_ok=True)
    for name in ("README.md", "deploy-notes.md", "CA-bundle.crt"):
        source = outside_dir / name
        source.write_text(f"# {name}\n", encoding="utf-8")
        store = _store(tmp_path)
        stored = store.import_document(
            source, server_id=SERVER, doc_id=uuid.uuid5(uuid.NAMESPACE_DNS, name)
        )
        assert stored.source_kind is SourceKind.IMPORTED
        source.unlink()


def test_import_symlink_and_non_file_rejected(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside-link-fixture"
    outside_dir.mkdir(exist_ok=True)
    real = outside_dir / "real.md"
    real.write_text("# Real\n", encoding="utf-8")
    link = outside_dir / "link.md"
    link.symlink_to(real)
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(link, server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.SYMLINK_FORBIDDEN
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(outside_dir, server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_A_FILE
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(outside_dir / "absent.md", server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_A_FILE
    _, meta_path, intent_path = _scope_paths(tmp_path, DOC)
    assert not meta_path.exists() and not intent_path.exists()
    link.unlink()
    real.unlink()


def test_import_rejects_unreadable_and_invalid_content(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside-invalid-fixture"
    outside_dir.mkdir(exist_ok=True)
    binary = outside_dir / "binary.md"
    binary.write_bytes(b"\xff\xfe\x00broken")
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(binary, server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.INVALID_DOCUMENT
    oversized = outside_dir / "oversized.md"
    oversized.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(DocumentStoreError) as excinfo:
        store.import_document(oversized, server_id=SERVER, doc_id=DOC)
    assert excinfo.value.code is DocumentStoreErrorCode.DOCUMENT_TOO_LARGE
    binary.unlink()
    oversized.unlink()


def test_write_emits_durable_upsert_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    _, _, intent_path = _scope_paths(tmp_path, DOC)
    payload = json.loads(intent_path.read_text())
    assert payload["format"] == 1
    assert payload["op"] == "upsert"
    assert payload["revision"] == stored.revision
    assert payload["sha256"] == stored.sha256
    assert payload["tombstone"] is False
    assert payload["server_id"] == str(SERVER)
    # Durability across reconstruction: the outbox survives a restart.
    restarted = _store(tmp_path)
    intents = restarted.read_intents(server_id=SERVER)
    assert len(intents) == 1
    assert intents[0].op == "upsert"
    assert intents[0].revision == 1


def test_update_replaces_intent_with_newer_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    updated = _write(store, MARKDOWN_2, expected_revision=1)
    intents = store.read_intents(server_id=SERVER)
    assert len(intents) == 1
    assert intents[0].revision == updated.revision == 2
    assert intents[0].sha256 == updated.sha256


def test_delete_emits_tombstone_intent_and_removes_pair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    intent = store.delete(DOC, server_id=SERVER, expected_revision=stored.revision)
    assert intent.tombstone and intent.op == "delete"
    assert intent.revision == stored.revision
    doc_path, meta_path, intent_path = _scope_paths(tmp_path, DOC)
    assert not doc_path.exists() and not meta_path.exists()
    # Tombstone stays durable until the indexer acknowledges (C02 W4).
    assert intent_path.is_file()
    payload = json.loads(intent_path.read_text())
    assert payload["op"] == "delete" and payload["tombstone"] is True
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND
    # Reconstruct (restart): the tombstone is still the durable truth.
    restarted = _store(tmp_path)
    intents = restarted.read_intents(server_id=SERVER)
    assert len(intents) == 1 and intents[0].tombstone


def test_purge_intent_is_the_indexer_acknowledgment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    store.delete(DOC, server_id=SERVER, expected_revision=stored.revision)
    store.purge_intent(DOC, server_id=SERVER)
    assert store.read_intents(server_id=SERVER) == []
    _, _, intent_path = _scope_paths(tmp_path, DOC)
    assert not intent_path.exists()


def test_recreate_after_delete_requires_exact_tombstone_revision(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    store.delete(DOC, server_id=SERVER, expected_revision=stored.revision)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store)  # expects ABSENT_REVISION but revision 1 was deleted
    assert excinfo.value.code is DocumentStoreErrorCode.STALE_REVISION
    recreated = _write(store, MARKDOWN_2, expected_revision=stored.revision)
    assert recreated.revision == stored.revision + 1
    assert store.read(DOC, server_id=SERVER).text == MARKDOWN_2


def test_resurrected_file_under_live_tombstone_is_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = _write(store)
    store.delete(DOC, server_id=SERVER, expected_revision=stored.revision)
    # External resurrection: file + metadata reappear while the tombstone
    # intent is still live (e.g. a restore raced the pending purge).
    doc_path, meta_path, _ = _scope_paths(tmp_path, DOC)
    doc_path.write_text(MARKDOWN_1, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "format": 1,
                "doc_id": str(DOC),
                "server_id": str(SERVER),
                "app_id": None,
                "namespace": "docs",
                "revision": stored.revision,
                "sha256": stored.sha256,
                "source_kind": "authored",
                "origin": "operator",
                "origin_revision": None,
                "verification": "unverified",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.TOMBSTONE_CONFLICT
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, MARKDOWN_2, expected_revision=stored.revision)
    assert excinfo.value.code is DocumentStoreErrorCode.TOMBSTONE_CONFLICT


def test_stale_write_and_delete_rejected_with_pair_intact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, MARKDOWN_2, expected_revision=0)
    assert excinfo.value.code is DocumentStoreErrorCode.STALE_REVISION
    with pytest.raises(DocumentStoreError) as excinfo:
        store.delete(DOC, server_id=SERVER, expected_revision=0)
    assert excinfo.value.code is DocumentStoreErrorCode.STALE_REVISION
    assert store.read(DOC, server_id=SERVER).text == MARKDOWN_1


def test_delete_absent_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.delete(DOC, server_id=SERVER, expected_revision=0)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND


def test_read_absent_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND


def test_servers_dir_symlink_escape_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path.parent / "escape-target"
    outside.mkdir(exist_ok=True)
    (tmp_path / "servers").symlink_to(outside)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store)
    assert excinfo.value.code is DocumentStoreErrorCode.PATH_ESCAPE
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.PATH_ESCAPE
    assert list(outside.iterdir()) == []  # nothing was written outside


def test_collection_dir_symlink_escape_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path.parent / "escape-target-2"
    outside.mkdir(exist_ok=True)
    docs_dir = tmp_path / "servers" / str(SERVER) / "docs"
    docs_dir.parent.mkdir(parents=True)
    docs_dir.symlink_to(outside)
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store)
    assert excinfo.value.code is DocumentStoreErrorCode.PATH_ESCAPE
    assert list(outside.iterdir()) == []


def test_canonical_file_symlink_escape_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    doc_path, _, _ = _scope_paths(tmp_path, DOC)
    doc_path.unlink()
    outside = tmp_path.parent / "escape-doc.md"
    outside.write_text("# outsider\n", encoding="utf-8")
    doc_path.symlink_to(outside)
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.PATH_ESCAPE
    with pytest.raises(DocumentStoreError) as excinfo:
        _write(store, MARKDOWN_2, expected_revision=1)
    assert excinfo.value.code is DocumentStoreErrorCode.PATH_ESCAPE
    outside.unlink()


def test_hash_mismatch_detected_not_reconciled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    doc_path, _, _ = _scope_paths(tmp_path, DOC)
    doc_path.write_text(MARKDOWN_2, encoding="utf-8")  # external modification
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.HASH_MISMATCH
    # A write with the matching observed revision is the repair path (the
    # file is authoritative, C02 W2 — same semantics as mai.profiles.files):
    # it replaces the tampered pair and bumps the revision.
    repaired = _write(store, MARKDOWN_2, expected_revision=1)
    assert repaired.revision == 2
    assert store.read(DOC, server_id=SERVER).text == MARKDOWN_2


def test_intent_outbox_is_scoped_and_ordered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store, doc_id=DOC)
    _write(store, MARKDOWN_2, doc_id=DOC, expected_revision=1)
    _write(store, doc_id=DOC_2)
    _write(store, doc_id=DOC, server_id=None)  # shared namespace, revision 1
    server_intents = store.read_intents(server_id=SERVER)
    assert [item.revision for item in server_intents] == [1, 2]
    assert {item.doc_id for item in server_intents} == {DOC, DOC_2}
    shared_intents = store.read_intents(server_id=None)
    assert len(shared_intents) == 1 and shared_intents[0].doc_id == DOC


def test_corrupt_intent_surfaces_intent_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    _, _, intent_path = _scope_paths(tmp_path, DOC)
    intent_path.write_text('{"format": 99}\n', encoding="utf-8")
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read_intents(server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.INTENT_INVALID


def test_invalid_metadata_surfaces_metadata_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store)
    _, meta_path, _ = _scope_paths(tmp_path, DOC)
    meta_path.write_text('{"format": 99}\n', encoding="utf-8")
    with pytest.raises(DocumentStoreError) as excinfo:
        store.read(DOC, server_id=SERVER)
    assert excinfo.value.code is DocumentStoreErrorCode.METADATA_INVALID


# ---------------------------------------------------------------------------
# Production entrypoint (fresh OS process per call; metadata-only output)
# ---------------------------------------------------------------------------


def _run_cli(
    root: Path, command: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mai.knowledge.documents", command, str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cli_roundtrip_emits_metadata_never_content(tmp_path: Path) -> None:
    root = tmp_path / "data"
    source = tmp_path / "cli-doc.md"
    source.write_text("# CLI\n\nbody\n", encoding="utf-8")

    written = _run_cli(
        root, "write", "--server", str(SERVER), "--namespace", "runbooks",
        str(DOC), str(source), "--expect-revision", "0",
        "--verification", "verified",
    )
    assert written.returncode == 0, written.stderr
    line = written.stdout.strip()
    payload = json.loads(line)
    assert payload["revision"] == 1
    assert payload["namespace"] == "runbooks"
    assert payload["verification"] == "verified"
    assert "body" not in line and "# CLI" not in line

    read = _run_cli(
        root, "read", "--server", str(SERVER), "--namespace", "runbooks", str(DOC)
    )
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout.strip())["sha256"] == payload["sha256"]

    intents = _run_cli(root, "intents", "--server", str(SERVER))
    assert intents.returncode == 0, intents.stderr
    intent_lines = [json.loads(item) for item in intents.stdout.splitlines()]
    assert intent_lines[0]["op"] == "upsert"

    deleted = _run_cli(
        root, "delete", "--server", str(SERVER), "--namespace", "runbooks",
        str(DOC), "--expect-revision", "1",
    )
    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout.strip())["tombstone"] is True

    missing = _run_cli(
        root, "read", "--server", str(SERVER), "--namespace", "runbooks", str(DOC)
    )
    assert missing.returncode == 1
    assert json.loads(missing.stderr.strip())["error"] == "not_found"


def test_cli_rejects_secret_import_with_clean_stderr(tmp_path: Path) -> None:
    root = tmp_path / "data"
    env_file = tmp_path / ".env"
    env_file.write_text("CLI_SENTINEL_TOKEN=abc\n", encoding="utf-8")
    result = _run_cli(
        root, "import", "--server", str(SERVER), "--doc-id", str(DOC),
        "--source", str(env_file),
    )
    assert result.returncode == 1
    error = json.loads(result.stderr.strip())
    assert error["error"] == "secret_file_forbidden"
    assert "CLI_SENTINEL_TOKEN" not in result.stderr
    assert "abc" not in result.stderr
    assert not (root / "servers").exists()  # nothing materialized at all


def test_cli_write_refuses_secret_name_without_materializing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    env_file = tmp_path / ".env"
    env_file.write_text("CLI_WRITE_SENTINEL=xyz\n", encoding="utf-8")
    result = _run_cli(
        root, "write", "--server", str(SERVER), str(DOC), str(env_file),
        "--expect-revision", "0",
    )
    assert result.returncode == 1
    error = json.loads(result.stderr.strip())
    assert error["error"] == "secret_file_forbidden"
    assert "CLI_WRITE_SENTINEL" not in result.stderr
    assert "xyz" not in result.stderr
    # The CLI write FILE door is an outside read: a refused source must
    # materialize nothing (no canonical file, no metadata, no intent).
    assert not (root / "servers").exists()


def test_cli_write_refuses_symlink_source_without_following(tmp_path: Path) -> None:
    root = tmp_path / "data"
    real = tmp_path / "real.md"
    real.write_text("LINK_SENTINEL_CONTENT\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    result = _run_cli(
        root, "write", "--server", str(SERVER), str(DOC), str(link),
        "--expect-revision", "0",
    )
    assert result.returncode == 1
    error = json.loads(result.stderr.strip())
    assert error["error"] == "symlink_forbidden"
    assert "LINK_SENTINEL_CONTENT" not in result.stderr
    assert not (root / "servers").exists()


def test_cli_root_creation_io_error_is_exit_two_not_traceback(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    root = blocker / "data"  # mkdir through a regular file: OSError
    result = _run_cli(root, "intents", "--server", str(SERVER))
    assert result.returncode == 2
    error = json.loads(result.stderr.strip())
    assert error["error"] == "usage_or_io"
    assert "Traceback" not in result.stderr
    assert not (root / "servers").exists()


def test_cli_shared_scope_and_import(tmp_path: Path) -> None:
    root = tmp_path / "data"
    source = tmp_path / "shared-note.md"
    source.write_text("# Shared\n", encoding="utf-8")
    imported = _run_cli(
        root, "import", "--shared", "--doc-id", str(DOC), "--source", str(source)
    )
    assert imported.returncode == 0, imported.stderr
    payload = json.loads(imported.stdout.strip())
    assert payload["server_id"] is None
    assert payload["source_kind"] == "imported"
    assert payload["origin"].startswith("import:")
