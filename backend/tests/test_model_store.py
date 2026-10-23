"""Focused tests for the confined atomic model-configuration store.

Cards/contracts: ``06-model-store`` consuming the frozen producer
``mai.agents.models`` (``06-model-config``) and the storage semantics proven
by ``mai.profiles.files`` (``02-file-store``), against contracts.md
**C02** (canonical storage semantics: explicit root, version triple, atomic
replace, crash windows W1/W2), **C03 rule 6** (credential references, never
values) and **C06 rule 1** (endpoint config and model selection are separate
records; selected pair validated at selection/bind time).

Acceptance coverage:

- AC1: canonical shared model config uses secret references, version
  conflicts (``stale_revision``) and selected endpoint/profile separation
  (the selection is its own versioned record naming both documents and is
  validated with the producer's ``validate_selection``).
- AC2: saved settings survive restart (fresh store instance over the same
  root); an active run's binding retains the explicit model profile revision
  while credentials re-resolve at call time; inline token output is rejected
  before any filesystem effect and never echoed.

Boundary discipline (test policy): confinement, atomicity and
crash-preservation claims are exercised on a real temporary filesystem. The
pre-replace interruption is proven twice — deterministically in-process
(``os.replace`` interrupted) and with a **real SIGKILL** delivered to a
child process instrumented to die exactly before the replace call; the disk
state after the kill is inspected by a fresh store instance (restart
semantics). Every "credential" below is the synthetic sentinel
``…not-a-secret…`` marker; no network, database or ambient environment is
touched and error payloads are checked value-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mai.agents.model_store import (
    MAX_DOCUMENT_BYTES,
    META_FORMAT,
    ModelConfigStore,
    ModelStoreError,
    ModelStoreErrorCode,
    main as store_main,
)
from mai.agents.models import (
    SCHEMA_VERSION,
    ModelConfigError,
    ModelConfigErrorCode,
    model_settings,
)
from mai.credentials.env import EnvResolver

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Synthetic credential material: safe to commit, designed to collide with
#: any accidental rendering path (see module docstring).
_SENTINEL = "sk-sentinel-not-a-secret-0123456789abcdef"

CONNECTION_NAME = "local-gateway"
PROFILE_NAME = "fast-chat"
OTHER_CONNECTION_NAME = "other-gateway"
OTHER_PROFILE_NAME = "other-chat"


def _connection_text(
    name: str = CONNECTION_NAME,
    *,
    credential_ref: str | None = "GATEWAY_KEY",
    reasoning: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "base_url": "http://127.0.0.1:4000/v1",
        "timeout_seconds": 30,
        "headers": {"X-Deployment": "tests"},
        "capabilities": {"tool_calls": True, "reasoning": reasoning},
    }
    if credential_ref is not None:
        document["credential_ref"] = credential_ref
    if extra:
        document.update(extra)
    return json.dumps(document, indent=2) + "\n"


def _profile_text(
    name: str = PROFILE_NAME,
    connection: str = CONNECTION_NAME,
    *,
    temperature: float | None = 0.2,
    extra: dict[str, Any] | None = None,
) -> str:
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "connection": connection,
        "model": "test-model",
        "max_tokens": 256,
    }
    if temperature is not None:
        document["temperature"] = temperature
    if extra:
        document.update(extra)
    return json.dumps(document, indent=2) + "\n"


def _write_pair(
    store: ModelConfigStore,
    *,
    connection: str = CONNECTION_NAME,
    profile: str = PROFILE_NAME,
    reasoning: bool = False,
) -> tuple[int, int]:
    """Store one valid (connection, profile) pair; return both revisions."""
    stored_connection = store.write_connection(
        connection, _connection_text(connection, reasoning=reasoning),
        expected_revision=0,
    )
    stored_profile = store.write_profile(
        profile, _profile_text(profile, connection), expected_revision=0
    )
    return stored_connection.revision, stored_profile.revision


def _codes_of(exc: ModelStoreError) -> ModelStoreErrorCode:
    return exc.code


# ---------------------------------------------------------------------------
# AC1 — canonical shared config: write/read by identity, version triple
# ---------------------------------------------------------------------------


def test_connection_roundtrip_revision_one(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    text = _connection_text()
    stored = store.write_connection(CONNECTION_NAME, text, expected_revision=0)
    assert stored.revision == 1
    assert stored.schema_revision == SCHEMA_VERSION
    assert stored.kind == "connection"
    assert stored.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert store.read_connection(CONNECTION_NAME) == stored


def test_profile_roundtrip_and_update_bumps_revision(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    first = store.write_profile(
        PROFILE_NAME, _profile_text(), expected_revision=0
    )
    assert first.revision == 1
    second = store.write_profile(
        PROFILE_NAME, _profile_text(temperature=0.7), expected_revision=1
    )
    assert (first.revision, second.revision) == (1, 2)
    reread = store.read_profile(PROFILE_NAME)
    assert reread.revision == 2
    assert reread.text == _profile_text(temperature=0.7)


def test_stale_write_rejected_and_previous_pair_intact(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    original = _connection_text()
    store.write_connection(CONNECTION_NAME, original, expected_revision=0)
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, _connection_text(), expected_revision=0
        )
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.STALE_REVISION
    reread = store.read_connection(CONNECTION_NAME)
    assert (reread.revision, reread.text) == (1, original)


def test_stale_delete_rejected_and_content_intact(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    store.write_profile(PROFILE_NAME, _profile_text(), expected_revision=0)
    with pytest.raises(ModelStoreError) as excinfo:
        store.delete_profile(PROFILE_NAME, expected_revision=5)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.STALE_REVISION
    assert store.read_profile(PROFILE_NAME).revision == 1


def test_delete_with_matching_revision_then_recreate(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    store.write_connection(CONNECTION_NAME, _connection_text(), expected_revision=0)
    store.delete_connection(CONNECTION_NAME, expected_revision=1)
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(CONNECTION_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    recreated = store.write_connection(
        CONNECTION_NAME, _connection_text(), expected_revision=0
    )
    assert recreated.revision == 1


def test_read_and_delete_absent_are_not_found(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_profile(PROFILE_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    with pytest.raises(ModelStoreError) as excinfo:
        store.delete_connection(CONNECTION_NAME, expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND


def test_invalid_document_rejected_before_any_filesystem_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data-root"
    store = ModelConfigStore(root)
    invalid = _profile_text().replace('"test-model"', '"bad model"')
    with pytest.raises(ModelConfigError) as excinfo:
        store.write_profile(PROFILE_NAME, invalid, expected_revision=0)
    assert "model" in excinfo.value.fields
    assert not (root / "models").exists()
    with pytest.raises(ModelStoreError) as store_error:
        store.read_profile(PROFILE_NAME)
    assert _codes_of(store_error.value) is ModelStoreErrorCode.NOT_FOUND


def test_embedded_name_must_match_storage_identity(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, _connection_text(OTHER_CONNECTION_NAME),
            expected_revision=0,
        )
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NAME_MISMATCH
    with pytest.raises(ModelStoreError):
        store.read_connection(CONNECTION_NAME)


@pytest.mark.parametrize("bad_name", ["Bad-Name", "UPPER", "", "-lead", 5, None])
def test_invalid_name_identity_rejected(
    tmp_path: Path, bad_name: object
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(bad_name)  # type: ignore[arg-type]
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.INVALID_NAME


@pytest.mark.parametrize("bad_revision", [-1, True, "1", 1.5, None])
def test_invalid_expected_revision_rejected(
    tmp_path: Path, bad_revision: object
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, _connection_text(), expected_revision=bad_revision  # type: ignore[arg-type]
        )
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.INVALID_REVISION


def test_two_records_of_one_kind_are_isolated(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    mine = store.write_connection(
        CONNECTION_NAME, _connection_text(), expected_revision=0
    )
    other = store.write_connection(
        OTHER_CONNECTION_NAME,
        _connection_text(OTHER_CONNECTION_NAME, credential_ref=None),
        expected_revision=0,
    )
    assert mine.sha256 != other.sha256
    assert store.read_connection(CONNECTION_NAME).text == mine.text
    assert store.read_connection(OTHER_CONNECTION_NAME).text == other.text
    assert "credential_ref" not in json.loads(other.text)


def test_list_returns_sorted_names(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    assert store.list_connections() == ()
    assert store.list_profiles() == ()
    for name in ("zebra", "alpha"):
        store.write_connection(name, _connection_text(name), expected_revision=0)
        store.write_profile(name, _profile_text(name), expected_revision=0)
    assert store.list_connections() == ("alpha", "zebra")
    assert store.list_profiles() == ("alpha", "zebra")


# ---------------------------------------------------------------------------
# AC1 — secret references only; inline tokens rejected, never echoed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("inline_key", ["api_key", "token", "X-Api-Key"])
def test_inline_token_rejected_before_any_filesystem_effect(
    tmp_path: Path, inline_key: str
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    document = json.loads(_connection_text())
    document[inline_key] = _SENTINEL
    with pytest.raises(ModelConfigError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, json.dumps(document), expected_revision=0
        )
    assert (
        excinfo.value.fields[inline_key]
        == ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN.value
    )
    with pytest.raises(ModelStoreError):
        store.read_connection(CONNECTION_NAME)
    assert not list((store.root / "models").rglob("*.json"))


def test_store_metadata_and_errors_are_value_free(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    store.write_connection(CONNECTION_NAME, _connection_text(), expected_revision=0)
    canonical = (
        store.root / "models" / "connections" / f"{CONNECTION_NAME}.json"
    )
    canonical.write_bytes(json.dumps({"spoofed": _SENTINEL}).encode())
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(CONNECTION_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.HASH_MISMATCH
    rendered = f"{excinfo.value} {excinfo.value.detail}"
    assert _SENTINEL not in rendered
    assert "GATEWAY_KEY" not in rendered


# ---------------------------------------------------------------------------
# AC1 — restrictive permissions and metadata shape (C02 rules 1 and 4)
# ---------------------------------------------------------------------------


def test_permissions_are_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = ModelConfigStore(root)
    _write_pair(store)
    assert (root.stat().st_mode & 0o777) == 0o700
    models_dir = root / "models"
    assert (models_dir.stat().st_mode & 0o777) == 0o700
    for kind_dir in ("connections", "profiles"):
        assert ((models_dir / kind_dir).stat().st_mode & 0o777) == 0o700
    stored_files = list(models_dir.rglob("*.json"))
    assert len(stored_files) == 4  # two documents + two metadata sidecars
    for stored in stored_files:
        assert (stored.stat().st_mode & 0o777) == 0o600


def test_metadata_carries_frozen_version_triple(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    stored = store.write_connection(
        CONNECTION_NAME, _connection_text(), expected_revision=0
    )
    meta = json.loads(
        (store.root / "models" / "connections" / f"{CONNECTION_NAME}.meta.json")
        .read_bytes()
    )
    assert meta == {
        "format": META_FORMAT,
        "kind": "connection",
        "name": CONNECTION_NAME,
        "revision": stored.revision,
        "schema_revision": SCHEMA_VERSION,
        "sha256": stored.sha256,
    }


# ---------------------------------------------------------------------------
# AC1 — selected endpoint/profile separation
# ---------------------------------------------------------------------------


def test_selection_is_a_separate_versioned_record(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    with pytest.raises(ModelStoreError) as excinfo:
        store.get_selection()
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    selected = store.set_selection(
        CONNECTION_NAME, PROFILE_NAME, expected_revision=0
    )
    assert (selected.revision, selected.connection, selected.profile) == (
        1,
        CONNECTION_NAME,
        PROFILE_NAME,
    )
    assert store.get_selection() == selected
    # The selection is a record of its own: re-selecting bumps only its
    # revision, never the documents'.
    store.write_profile(
        OTHER_PROFILE_NAME, _profile_text(OTHER_PROFILE_NAME), expected_revision=0
    )
    reselected = store.set_selection(
        CONNECTION_NAME, OTHER_PROFILE_NAME, expected_revision=1
    )
    assert reselected.revision == 2
    assert store.read_profile(PROFILE_NAME).revision == 1


def test_selection_requires_a_pairing_valid_pair(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.write_connection(
        OTHER_CONNECTION_NAME,
        _connection_text(OTHER_CONNECTION_NAME, credential_ref=None),
        expected_revision=0,
    )
    store.write_profile(
        OTHER_PROFILE_NAME,
        _profile_text(OTHER_PROFILE_NAME, OTHER_CONNECTION_NAME),
        expected_revision=0,
    )
    # Both documents are valid on their own; the *selection* is where the
    # pairing is enforced (C06 rule 1).
    with pytest.raises(ModelConfigError) as excinfo:
        store.set_selection(
            CONNECTION_NAME, OTHER_PROFILE_NAME, expected_revision=0
        )
    assert excinfo.value.fields["connection"] == (
        ModelConfigErrorCode.INVALID_SELECTION.value
    )
    with pytest.raises(ModelStoreError):
        store.get_selection()


def test_selection_enforces_endpoint_capability_gate(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    reasoning_profile = _profile_text(
        PROFILE_NAME, extra={"reasoning_effort": "high"}
    )
    store.write_profile(
        PROFILE_NAME, reasoning_profile, expected_revision=1
    )
    with pytest.raises(ModelConfigError) as excinfo:
        store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    assert excinfo.value.fields["reasoning_effort"] == (
        ModelConfigErrorCode.UNSUPPORTED_OPTION.value
    )


def test_set_selection_requires_stored_documents(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND


def test_stale_selection_update_rejected(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    with pytest.raises(ModelStoreError) as excinfo:
        store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.STALE_REVISION


def test_selection_survives_document_edits(tmp_path: Path) -> None:
    # The selection names records; editing a named document does not move it.
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    store.write_profile(
        PROFILE_NAME, _profile_text(temperature=0.9), expected_revision=1
    )
    selected = store.get_selection()
    assert (selected.connection, selected.profile) == (
        CONNECTION_NAME,
        PROFILE_NAME,
    )


def test_delete_of_selected_record_refused_until_repointed(
    tmp_path: Path,
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    for delete in (
        lambda: store.delete_profile(PROFILE_NAME, expected_revision=1),
        lambda: store.delete_connection(CONNECTION_NAME, expected_revision=1),
    ):
        with pytest.raises(ModelStoreError) as excinfo:
            delete()
        assert _codes_of(excinfo.value) is ModelStoreErrorCode.SELECTION_CONFLICT
        assert store.read_profile(PROFILE_NAME).revision == 1
    store.clear_selection(expected_revision=1)
    store.delete_profile(PROFILE_NAME, expected_revision=1)
    with pytest.raises(ModelStoreError):
        store.read_profile(PROFILE_NAME)


def test_clear_selection_revision_checked(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.clear_selection(expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    _write_pair(store)
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    with pytest.raises(ModelStoreError) as excinfo:
        store.clear_selection(expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.STALE_REVISION
    store.clear_selection(expected_revision=1)
    with pytest.raises(ModelStoreError):
        store.get_selection()
    # A cleared selection restarts its revision at the create baseline.
    fresh = store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    assert fresh.revision == 1


# ---------------------------------------------------------------------------
# AC2 — restart survival and explicit revision binding
# ---------------------------------------------------------------------------


def test_state_survives_store_reopen(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    first = ModelConfigStore(root)
    connection_revision, profile_revision = _write_pair(first)
    first.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    reopened = ModelConfigStore(root)
    connection = reopened.read_connection(CONNECTION_NAME)
    assert (connection.revision, connection.text) == (
        connection_revision,
        _connection_text(),
    )
    assert reopened.read_profile(PROFILE_NAME).revision == profile_revision
    assert reopened.get_selection().profile == PROFILE_NAME


def test_bind_defaults_to_selection_and_accepts_explicit_names(
    tmp_path: Path,
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    with pytest.raises(ModelStoreError) as excinfo:
        store.bind()
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    from_selection = store.bind()
    assert from_selection.connection.name == CONNECTION_NAME
    assert from_selection.profile.name == PROFILE_NAME
    explicit = store.bind(CONNECTION_NAME, PROFILE_NAME)
    assert explicit.profile_revision == from_selection.profile_revision


def test_bind_rejects_invalid_explicit_pair(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.write_connection(
        OTHER_CONNECTION_NAME,
        _connection_text(OTHER_CONNECTION_NAME),
        expected_revision=0,
    )
    store.write_profile(
        OTHER_PROFILE_NAME,
        _profile_text(OTHER_PROFILE_NAME, OTHER_CONNECTION_NAME),
        expected_revision=0,
    )
    with pytest.raises(ModelConfigError) as excinfo:
        store.bind(CONNECTION_NAME, OTHER_PROFILE_NAME)
    assert excinfo.value.fields["connection"] == (
        ModelConfigErrorCode.INVALID_SELECTION.value
    )


def test_bind_resolves_paired_connection_for_a_profile_alone(
    tmp_path: Path,
) -> None:
    """`bind(profile=…)` without a connection resolves the profile's OWN
    paired connection — never the stored selection's (a chat profile
    selection binds cleanly without naming the connection)."""
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    store.write_connection(
        OTHER_CONNECTION_NAME,
        _connection_text(OTHER_CONNECTION_NAME),
        expected_revision=0,
    )
    store.write_profile(
        OTHER_PROFILE_NAME,
        _profile_text(OTHER_PROFILE_NAME, OTHER_CONNECTION_NAME),
        expected_revision=0,
    )
    # The stored selection names the OTHER pair: a profile-only bind must
    # still resolve the profile's own stored pairing.
    store.set_selection(
        OTHER_CONNECTION_NAME, OTHER_PROFILE_NAME, expected_revision=0
    )
    binding = store.bind(profile=PROFILE_NAME)
    assert binding.connection.name == CONNECTION_NAME
    assert binding.profile.name == PROFILE_NAME
    other = store.bind(profile=OTHER_PROFILE_NAME)
    assert other.connection.name == OTHER_CONNECTION_NAME
    assert other.profile.name == OTHER_PROFILE_NAME


def test_bind_profile_alone_still_rejects_pairing_drift(tmp_path: Path) -> None:
    """A stored profile whose pairing drifted from its stored connection
    document is a classified rejection, never a silent retarget."""
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    # Edit the profile to name a connection that is not stored at all.
    store.write_profile(
        PROFILE_NAME,
        _profile_text(PROFILE_NAME, OTHER_CONNECTION_NAME),
        expected_revision=1,
    )
    with pytest.raises(ModelStoreError) as excinfo:
        store.bind(profile=PROFILE_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND


def test_binding_retains_profile_revision_across_edit(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    binding = store.bind(CONNECTION_NAME, PROFILE_NAME)
    assert binding.profile_revision == 1
    assert binding.profile.temperature == 0.2

    # Administrator edits the defaults: new revision stored.
    store.write_profile(
        PROFILE_NAME, _profile_text(temperature=0.9), expected_revision=1
    )

    status = store.revalidate(binding)
    assert status.profile_changed is True
    assert status.connection_changed is False
    assert status.stored_profile_revision == 2
    assert status.profile_missing is False

    # The active run keeps the pinned revision: the binding's profile is the
    # captured revision-1 document, and its settings payload obeys it.
    assert binding.profile.temperature == 0.2
    assert binding.profile_revision == 1
    assert model_settings(binding.profile)["temperature"] == 0.2
    assert binding.selection().profile.connection == CONNECTION_NAME


def test_revalidate_reports_missing_records(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    binding = store.bind(CONNECTION_NAME, PROFILE_NAME)
    store.delete_profile(PROFILE_NAME, expected_revision=1)
    status = store.revalidate(binding)
    assert status.profile_missing is True
    assert status.stored_profile_revision is None
    assert status.connection_missing is False
    assert status.stored_connection_revision == 1


def test_binding_rebuild_revalidates_credentials_per_call(
    tmp_path: Path,
) -> None:
    """Credentials re-resolve at call time; the pinned documents never move."""
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    binding = store.bind(CONNECTION_NAME, PROFILE_NAME)
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    env_file = env_dir / "agents.env"
    env_file.write_text(f"GATEWAY_KEY={_SENTINEL}\n", encoding="utf-8")
    os.chmod(env_file, stat.S_IRUSR | stat.S_IWUSR)

    first = binding.rebuild(resolver=EnvResolver(env_file))
    first_digest = hashlib.sha256(
        first.provider.client.api_key.encode("utf-8")
    ).hexdigest()
    assert first_digest == hashlib.sha256(_SENTINEL.encode()).hexdigest()

    # Rotate the credential value: the next call re-resolves it — the run
    # keeps its pinned profile revision, credentials/grants revalidate.
    rotated = _SENTINEL.replace("sentinel", "rotated")
    env_file.write_text(f"GATEWAY_KEY={rotated}\n", encoding="utf-8")
    second = binding.rebuild(resolver=EnvResolver(env_file))
    second_digest = hashlib.sha256(
        second.provider.client.api_key.encode("utf-8")
    ).hexdigest()
    assert second_digest == hashlib.sha256(rotated.encode()).hexdigest()
    assert second_digest != first_digest
    # The pinned documents are unchanged across both resolutions.
    assert second.profile == first.profile
    assert second.profile.temperature == 0.2


# ---------------------------------------------------------------------------
# Boundaries — symlink escape, integrity metadata, crash windows
# ---------------------------------------------------------------------------


def _outside_fixture(tmp_path: Path) -> tuple[Path, bytes]:
    outside = tmp_path / "outside-fixture"
    outside.mkdir()
    target = outside / "escaped.json"
    target.write_bytes(b'{"escaped": true}\n')
    return target, b'{"escaped": true}\n'


def test_symlinked_canonical_file_rejected(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    canonical = (
        store.root / "models" / "connections" / f"{CONNECTION_NAME}.json"
    )
    canonical.unlink()
    canonical.symlink_to(outside_target)
    for operation in (
        lambda: store.read_connection(CONNECTION_NAME),
        lambda: store.write_connection(
            CONNECTION_NAME, _connection_text(), expected_revision=1
        ),
        lambda: store.delete_connection(CONNECTION_NAME, expected_revision=1),
    ):
        with pytest.raises(ModelStoreError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is ModelStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_kind_directory_rejected(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    links = store.root / "models"
    links.mkdir(parents=True)
    (links / "profiles").symlink_to(outside_target, target_is_directory=True)
    for operation in (
        lambda: store.read_profile(PROFILE_NAME),
        lambda: store.write_profile(
            PROFILE_NAME, _profile_text(), expected_revision=0
        ),
        lambda: store.delete_profile(PROFILE_NAME, expected_revision=0),
    ):
        with pytest.raises(ModelStoreError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is ModelStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_metadata_file_rejected(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    outside_target, outside_bytes = _outside_fixture(tmp_path)
    meta = (
        store.root / "models" / "profiles" / f"{PROFILE_NAME}.meta.json"
    )
    meta.unlink()
    meta.symlink_to(outside_target)
    for operation in (
        lambda: store.read_profile(PROFILE_NAME),
        lambda: store.delete_profile(PROFILE_NAME, expected_revision=1),
    ):
        with pytest.raises(ModelStoreError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is ModelStoreErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_missing_metadata_reported_not_reconstructed(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    (
        store.root / "models" / "connections" / f"{CONNECTION_NAME}.meta.json"
    ).unlink()
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(CONNECTION_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.METADATA_MISSING


def test_invalid_metadata_shape_reported(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    meta_path = (
        store.root / "models" / "connections" / f"{CONNECTION_NAME}.meta.json"
    )
    meta_path.write_bytes(b"{not json")
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(CONNECTION_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.METADATA_INVALID
    # Shape-valid JSON with a foreign identity is equally invalid metadata.
    meta_path.write_bytes(
        json.dumps(
            {
                "format": META_FORMAT,
                "kind": "connection",
                "name": OTHER_CONNECTION_NAME,
                "revision": 1,
                "schema_revision": SCHEMA_VERSION,
                "sha256": "0" * 64,
            }
        ).encode()
    )
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_connection(CONNECTION_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.METADATA_INVALID


def test_orphan_metadata_blocks_write_until_delete_completes(
    tmp_path: Path,
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    (
        store.root / "models" / "connections" / f"{CONNECTION_NAME}.json"
    ).unlink()
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, _connection_text(), expected_revision=1
        )
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.ORPHAN_METADATA
    store.delete_connection(CONNECTION_NAME, expected_revision=1)
    recreated = store.write_connection(
        CONNECTION_NAME, _connection_text(), expected_revision=0
    )
    assert recreated.revision == 1


def _kind_dir(store: ModelConfigStore, kind: str) -> Path:
    return store.root / "models" / (
        "connections" if kind == "connection" else "profiles"
    )


def test_interrupted_before_canonical_replace_preserves_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    original = _connection_text()
    store.write_connection(CONNECTION_NAME, original, expected_revision=0)

    def _interrupted(src: object, dst: object) -> None:
        raise OSError("interrupted before replace")

    monkeypatch.setattr("mai.agents.model_store.os.replace", _interrupted)
    with pytest.raises(OSError):
        store.write_connection(
            CONNECTION_NAME, _connection_text(credential_ref=None),
            expected_revision=1,
        )
    monkeypatch.undo()

    reread = store.read_connection(CONNECTION_NAME)
    assert (reread.revision, reread.text) == (1, original)
    recovered = store.write_connection(
        CONNECTION_NAME, _connection_text(credential_ref=None), expected_revision=1
    )
    assert recovered.revision == 2
    assert not list(_kind_dir(store, "connection").glob("*.tmp"))


def test_interrupted_after_canonical_replace_is_detectable_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    original = _profile_text()
    store.write_profile(PROFILE_NAME, original, expected_revision=0)
    real_replace = os.replace
    calls = {"count": 0}

    def _half_committed(src: object, dst: object) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("interrupted between replaces")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("mai.agents.model_store.os.replace", _half_committed)
    with pytest.raises(OSError):
        store.write_profile(
            PROFILE_NAME, _profile_text(temperature=0.5), expected_revision=1
        )
    monkeypatch.undo()

    canonical = store.root / "models" / "profiles" / f"{PROFILE_NAME}.json"
    assert b"0.5" in canonical.read_bytes()  # file is authoritative
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_profile(PROFILE_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.HASH_MISMATCH


def test_real_sigkill_before_replace_preserves_previous_content(
    tmp_path: Path,
) -> None:
    """Real-boundary proof: SIGKILL at the replace boundary, then restart."""
    store = ModelConfigStore(tmp_path / "data-root")
    original = _connection_text()
    store.write_connection(CONNECTION_NAME, original, expected_revision=0)

    child_script = """
import os, signal, sys
from mai.agents.model_store import ModelConfigStore
root, name, text = sys.argv[1], sys.argv[2], sys.argv[3]
def _die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = _die
ModelConfigStore(root).write_connection(name, text, expected_revision=1)
raise SystemExit("write returned; kill hook did not fire")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_script,
            str(store.root),
            CONNECTION_NAME,
            _connection_text(credential_ref=None),
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    # Restart semantics: a fresh store observes the untouched previous pair.
    restarted = ModelConfigStore(store.root)
    reread = restarted.read_connection(CONNECTION_NAME)
    assert (reread.revision, reread.text) == (1, original)
    # Recovery: the next successful write cleans the orphaned temp file.
    recovered = restarted.write_connection(
        CONNECTION_NAME, _connection_text(credential_ref=None), expected_revision=1
    )
    assert recovered.revision == 2
    assert not list(_kind_dir(restarted, "connection").glob("*.tmp"))


def test_interrupted_delete_completes_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    _write_pair(store)
    real_unlink = Path.unlink
    calls = {"count": 0}

    def _half_deleted(self: Path, missing_ok: bool = False) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("interrupted between unlinks")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _half_deleted)
    with pytest.raises(OSError):
        store.delete_profile(PROFILE_NAME, expected_revision=1)
    monkeypatch.undo()

    meta_path = store.root / "models" / "profiles" / f"{PROFILE_NAME}.meta.json"
    assert meta_path.exists()
    with pytest.raises(ModelStoreError) as excinfo:
        store.read_profile(PROFILE_NAME)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.NOT_FOUND
    store.delete_profile(PROFILE_NAME, expected_revision=1)  # completes it
    assert not meta_path.exists()
    assert (
        store.write_profile(PROFILE_NAME, _profile_text(), expected_revision=0)
    ).revision == 1


# ---------------------------------------------------------------------------
# Resource-exhaustion and document-format boundaries
# ---------------------------------------------------------------------------


def test_oversized_document_rejected_before_parse(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    oversized = '{"name": "' + "x" * MAX_DOCUMENT_BYTES + '"}'
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_profile(PROFILE_NAME, oversized, expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.DOCUMENT_TOO_LARGE
    with pytest.raises(ModelStoreError):
        store.read_profile(PROFILE_NAME)


def test_unparsable_json_classified(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "data-root")
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_connection(
            CONNECTION_NAME, '{"name": truncated', expected_revision=0
        )
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.UNPARSABLE_DOCUMENT
    # A parsable non-mapping document is rejected by the producer's parser.
    with pytest.raises(ModelConfigError) as config_error:
        store.write_connection(
            CONNECTION_NAME,
            json.dumps(["not", "a", "mapping"]),
            expected_revision=0,
        )
    assert config_error.value.fields == {
        "": ModelConfigErrorCode.INVALID_DOCUMENT.value
    }


def test_deep_nesting_maps_recursion_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The RecursionError → document_too_deep mapping is pinned directly.

    Within the byte cap the pinned runtime's JSON parser does not recurse to
    the interpreter limit (measured 3.14.6: 32768 levels parse), so the
    defensive mapping is exercised at the unit boundary.
    """
    store = ModelConfigStore(tmp_path / "data-root")

    def _recursion(_text: str) -> object:
        raise RecursionError

    monkeypatch.setattr("mai.agents.model_store.json.loads", _recursion)
    with pytest.raises(ModelStoreError) as excinfo:
        store.write_profile(PROFILE_NAME, _profile_text(), expected_revision=0)
    assert _codes_of(excinfo.value) is ModelStoreErrorCode.DOCUMENT_TOO_DEEP


# ---------------------------------------------------------------------------
# Production entrypoint (exit contract 0 / 1 / 2)
# ---------------------------------------------------------------------------


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "mai.agents.model_store", *args]


def test_cli_document_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    document = tmp_path / "connection.json"
    document.write_text(_connection_text(), encoding="utf-8")
    write = subprocess.run(
        _cli(
            "write-connection", str(root), CONNECTION_NAME, str(document),
            "--expect-revision", "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert write.returncode == 0, write.stderr
    written = json.loads(write.stdout)
    assert written == {
        "kind": "connection",
        "name": CONNECTION_NAME,
        "revision": 1,
        "schema_revision": SCHEMA_VERSION,
        "sha256": hashlib.sha256(_connection_text().encode()).hexdigest(),
    }
    read = subprocess.run(
        _cli("read-connection", str(root), CONNECTION_NAME),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout) == written


def test_cli_selection_and_list_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    connection_file = tmp_path / "connection.json"
    connection_file.write_text(_connection_text(), encoding="utf-8")
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(_profile_text(), encoding="utf-8")
    assert (
        subprocess.run(
            _cli(
                "write-connection", str(root), CONNECTION_NAME,
                str(connection_file), "--expect-revision", "0",
            ),
            capture_output=True,
            cwd=BACKEND_DIR,
            timeout=60,
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            _cli(
                "write-profile", str(root), PROFILE_NAME, str(profile_file),
                "--expect-revision", "0",
            ),
            capture_output=True,
            cwd=BACKEND_DIR,
            timeout=60,
            check=False,
        ).returncode
        == 0
    )
    listed = subprocess.run(
        _cli("list", str(root)),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert json.loads(listed.stdout) == {
        "connections": [CONNECTION_NAME],
        "profiles": [PROFILE_NAME],
    }
    selected = subprocess.run(
        _cli(
            "set-selection", str(root), CONNECTION_NAME, PROFILE_NAME,
            "--expect-revision", "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout)["connection"] == CONNECTION_NAME
    fetched = subprocess.run(
        _cli("get-selection", str(root)),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert json.loads(fetched.stdout) == json.loads(selected.stdout)


def test_cli_output_is_value_free(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    document = tmp_path / "connection.json"
    document.write_text(_connection_text(), encoding="utf-8")
    completed = subprocess.run(
        _cli(
            "write-connection", str(root), CONNECTION_NAME, str(document),
            "--expect-revision", "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    captured = completed.stdout + completed.stderr
    assert _SENTINEL not in captured
    assert "127.0.0.1:4000" not in captured
    assert "GATEWAY_KEY" not in captured


def test_cli_stale_write_exits_one_with_stable_code(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    document = tmp_path / "profile.json"
    document.write_text(_profile_text(), encoding="utf-8")
    assert (
        subprocess.run(
            _cli(
                "write-profile", str(root), PROFILE_NAME, str(document),
                "--expect-revision", "0",
            ),
            capture_output=True,
            cwd=BACKEND_DIR,
            timeout=60,
            check=False,
        ).returncode
        == 0
    )
    stale = subprocess.run(
        _cli(
            "write-profile", str(root), PROFILE_NAME, str(document),
            "--expect-revision", "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert stale.returncode == 1
    error = json.loads(stale.stderr)
    assert error["error"]["code"] == ModelStoreErrorCode.STALE_REVISION.value


def test_cli_invalid_document_exits_one_with_schema_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data-root"
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"schema_version": 1, "name": CONNECTION_NAME, "api_key": _SENTINEL}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        _cli(
            "write-connection", str(root), CONNECTION_NAME, str(bad),
            "--expect-revision", "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "model_config_rejected"
    assert error["error"]["fields"]["api_key"] == (
        ModelConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN.value
    )
    assert _SENTINEL not in completed.stderr


def test_cli_delete_and_selection_conflict(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = ModelConfigStore(root)
    _write_pair(store)
    store.set_selection(CONNECTION_NAME, PROFILE_NAME, expected_revision=0)
    completed = subprocess.run(
        _cli("delete-profile", str(root), PROFILE_NAME, "--expect-revision", "1"),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == ModelStoreErrorCode.SELECTION_CONFLICT.value
    cleared = subprocess.run(
        _cli("clear-selection", str(root), "--expect-revision", "1"),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert cleared.returncode == 0, cleared.stderr
    assert json.loads(cleared.stdout)["deleted"] is True
    deleted = subprocess.run(
        _cli("delete-profile", str(root), PROFILE_NAME, "--expect-revision", "1"),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout)["name"] == PROFILE_NAME


def test_cli_missing_document_file_exits_two(tmp_path: Path) -> None:
    completed = subprocess.run(
        _cli(
            "write-profile",
            str(tmp_path / "data-root"),
            PROFILE_NAME,
            str(tmp_path / "absent.json"),
            "--expect-revision",
            "0",
        ),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stderr.startswith("io error:")


def test_cli_usage_error_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        store_main([])
    assert excinfo.value.code == 2


def test_module_entrypoint_help_exits_zero() -> None:
    completed = subprocess.run(
        _cli("--help"),
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    assert "set-selection" in completed.stdout
