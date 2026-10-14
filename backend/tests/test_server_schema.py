"""Focused tests for the frozen server profile schema (card 02-profile-schema).

Acceptance (BRIEF):
1. Profile contains stable ID, prompt revision, SSH identity, environment,
   privilege and capability refs per frozen schema.
2. Reject invalid IDs, unsupported execution environment and inline credential
   fields; rename does not change identity.

Fixtures are frozen as module constants (good/bad profile files) so the P02
gate can reuse the exact payloads through the profile HTTP path; the entrypoint
scenario writes them verbatim under the ``mai-fxt-02-profile-schema`` scratch
namespace. Error payloads are checked to be value-free (secret non-disclosure
is a risk-critical invariant, test policy). No DB, network or ambient
environment is touched — the schema module is pure.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import (
    SCHEMA_VERSION,
    ExecutionEnvironment,
    ProfileErrorCode,
    ProfileValidationError,
    PrivilegeMode,
    ServerProfile,
    SshIdentity,
    canonical_relative_path,
    main,
    parse_server_profile,
    parse_server_profile_text,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"
OTHER_SERVER_ID = "3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f"

# 43-char unpadded base64 body; format matches the P00 SSH spike measurement.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

GOOD_PROFILE = f"""\
schema_version: {SCHEMA_VERSION}
server_id: "{SERVER_ID}"
name: "build-host"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "{HOST_KEY}"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
  - shell.exec
  - files.read
"""


def _good_document() -> dict[str, object]:
    parsed = parse_server_profile_text(GOOD_PROFILE)
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "server_id": SERVER_ID,
        "name": parsed.name,
        "prompt_revision": parsed.prompt_revision,
        "environment": "linux_ssh",
        "ssh": {
            "host": "198.51.100.10",
            "port": 22,
            "user": "deploy",
            "host_key": HOST_KEY,
            "credential_ref": "MAI_SSH_KEY_BUILD_HOST",
        },
        "privilege": "none",
        "capabilities": ["shell.exec", "files.read"],
    }
    return document


def _rejected(
    mutations: dict[str, object] | None = None,
    remove: tuple[str, ...] = (),
) -> ProfileValidationError:
    document = _good_document()
    document.update(mutations or {})
    for key in remove:
        document.pop(key, None)
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile(document)
    return excinfo.value


def _codes_of(exc: ProfileValidationError) -> set[ProfileErrorCode]:
    return {error.code for error in exc.errors}


# ---------------------------------------------------------------------------
# AC1 — frozen schema carries every required reference
# ---------------------------------------------------------------------------


def test_good_profile_parses_with_all_frozen_fields() -> None:
    profile = parse_server_profile_text(GOOD_PROFILE)
    assert isinstance(profile, ServerProfile)
    assert profile.schema_version == SCHEMA_VERSION
    assert profile.server_id == uuid.UUID(SERVER_ID)
    assert profile.name == "build-host"
    assert profile.prompt_revision == 3
    assert profile.environment is ExecutionEnvironment.LINUX_SSH
    assert profile.ssh == SshIdentity(
        host="198.51.100.10",
        port=22,
        user="deploy",
        host_key=HOST_KEY,
        credential_ref="MAI_SSH_KEY_BUILD_HOST",
    )
    assert profile.privilege is PrivilegeMode.NONE
    assert profile.capabilities == ("shell.exec", "files.read")


def test_frozen_vocabularies_are_exactly_the_supported_set() -> None:
    # The vocabulary itself is part of the frozen schema; widening it is a
    # reconciliation, not a code edit.
    assert ExecutionEnvironment("linux_ssh") is ExecutionEnvironment.LINUX_SSH
    assert len(ExecutionEnvironment) == 1
    assert {mode.value for mode in PrivilegeMode} == {"none", "sudo", "root"}
    assert {code.value for code in ProfileErrorCode} == {
        "invalid_document",
        "unparsable_document",
        "schema_version_unsupported",
        "missing_field",
        "unknown_field",
        "invalid_field",
        "invalid_server_id",
        "unsupported_environment",
        "inline_credential_forbidden",
    }


def test_capability_refs_are_ordered_and_deduplicated_rejected() -> None:
    document = _good_document()
    document["capabilities"] = ["files.read", "shell.exec"]
    assert parse_server_profile(document).capabilities == ("files.read", "shell.exec")

    document["capabilities"] = ["shell.exec", "shell.exec"]
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile(document)
    assert "capabilities" in excinfo.value.fields


def test_two_server_ids_yield_distinct_identity_and_paths() -> None:
    # CROSS-SERVER fixture obligation: two distinct stable server IDs.
    other = _good_document()
    other["server_id"] = OTHER_SERVER_ID
    first = parse_server_profile(_good_document())
    second = parse_server_profile(other)
    assert first.server_id != second.server_id
    assert canonical_relative_path(first.server_id) != canonical_relative_path(
        second.server_id
    )


def test_canonical_relative_path_matches_contract_and_is_traversal_proof() -> None:
    path = canonical_relative_path(uuid.UUID(SERVER_ID))
    assert path == f"servers/{SERVER_ID}/profile.yaml"
    with pytest.raises(TypeError):
        canonical_relative_path("../../etc")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC2 — identity survives rename; invalid IDs rejected
# ---------------------------------------------------------------------------


def test_rename_keeps_identity() -> None:
    renamed = _good_document()
    renamed["name"] = "totally-different-label"
    before = parse_server_profile(_good_document())
    after = parse_server_profile(renamed)
    assert before.server_id == after.server_id
    assert canonical_relative_path(before.server_id) == canonical_relative_path(
        after.server_id
    )
    assert before.name != after.name


def test_endpoint_change_keeps_identity() -> None:
    moved = _good_document()
    moved["ssh"] = {**_good_document()["ssh"], "host": "203.0.113.77"}  # type: ignore[dict-item]
    before = parse_server_profile(_good_document())
    after = parse_server_profile(moved)
    assert before.server_id == after.server_id
    assert after.ssh.host == "203.0.113.77"


@pytest.mark.parametrize(
    "bad_id",
    [
        "web-prod",
        "192.168.1.10",
        "",
        SERVER_ID.upper(),
        f"{{{SERVER_ID}}}",
        f"urn:uuid:{SERVER_ID}",
        f" {SERVER_ID}",
        f"{SERVER_ID} ",
        "7e6c1a349a2b4c3d8e5f1029384756af",
        42,
        None,
    ],
)
def test_rejects_invalid_server_ids(bad_id: object) -> None:
    exc = _rejected({"server_id": bad_id})
    assert exc.fields == {"server_id": ProfileErrorCode.INVALID_SERVER_ID.value}
    # The rejected value itself is never echoed.
    rendered = str(bad_id)
    if rendered:
        assert rendered not in str(exc)


# ---------------------------------------------------------------------------
# AC2 — unsupported execution environment rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_env", ["docker", "windows", "kubernetes", "linux-ssh", "LINUX_SSH", "", 3, None]
)
def test_rejects_unsupported_environments(bad_env: object) -> None:
    exc = _rejected({"environment": bad_env})
    assert exc.fields == {
        "environment": ProfileErrorCode.UNSUPPORTED_ENVIRONMENT.value
    }


def test_rejects_missing_environment() -> None:
    exc = _rejected(remove=("environment",))
    assert exc.fields == {"environment": ProfileErrorCode.MISSING_FIELD.value}


# ---------------------------------------------------------------------------
# AC2 — inline credential fields rejected; errors never carry values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "passwd",
        "ssh_password",
        "secret",
        "session_token",
        "api_key",
        "access_key",
        "private_key",
        "passphrase",
        "credentials",
    ],
)
def test_rejects_inline_credential_fields_at_top_level(field: str) -> None:
    exc = _rejected({field: "hunter2"})
    assert exc.fields == {
        field: ProfileErrorCode.INLINE_CREDENTIAL_FORBIDDEN.value
    }
    assert "hunter2" not in str(exc)


def test_rejects_nested_private_key_in_ssh_block() -> None:
    document = _good_document()
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----"
    document["ssh"] = {**document["ssh"], "private_key": pem}  # type: ignore[dict-item]
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile(document)
    assert excinfo.value.fields == {
        "ssh.private_key": ProfileErrorCode.INLINE_CREDENTIAL_FORBIDDEN.value
    }
    assert pem not in str(excinfo.value)
    assert pem not in str(excinfo.value.fields)


def test_rejection_payload_is_value_free_across_multiple_errors() -> None:
    document = _good_document()
    document["server_id"] = "not-a-uuid"
    document["password"] = "hunter2"
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile(document)
    text = str(excinfo.value) + str(excinfo.value.fields)
    assert "hunter2" not in text
    assert "not-a-uuid" not in text
    assert excinfo.value.fields == {
        "server_id": ProfileErrorCode.INVALID_SERVER_ID.value,
        "password": ProfileErrorCode.INLINE_CREDENTIAL_FORBIDDEN.value,
    }


# ---------------------------------------------------------------------------
# Strictness — nothing is silently ignored or dropped
# ---------------------------------------------------------------------------


def test_unknown_fields_rejected_not_ignored() -> None:
    exc = _rejected({"extra": "value"})
    assert exc.fields == {"extra": ProfileErrorCode.UNKNOWN_FIELD.value}


def test_unknown_ssh_field_rejected() -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "jump_host": "bastion"}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields == {"ssh.jump_host": ProfileErrorCode.UNKNOWN_FIELD.value}


def test_known_field_in_wrong_position_rejected() -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "name": "misplaced"}  # type: ignore[dict-item]
    assert "ssh.name" in _rejected(document).fields


@pytest.mark.parametrize("version", [SCHEMA_VERSION + 1, 0, -1, "1", True, 1.0])
def test_rejects_unsupported_schema_versions(version: object) -> None:
    exc = _rejected({"schema_version": version})
    assert exc.fields == {
        "schema_version": ProfileErrorCode.SCHEMA_VERSION_UNSUPPORTED.value
    }


def test_missing_required_fields_reported() -> None:
    exc = _rejected(remove=("prompt_revision", "privilege"))
    assert exc.fields == {
        "prompt_revision": ProfileErrorCode.MISSING_FIELD.value,
        "privilege": ProfileErrorCode.MISSING_FIELD.value,
    }
    exc = _rejected(remove=("ssh",))
    assert exc.fields == {"ssh": ProfileErrorCode.MISSING_FIELD.value}
    document = _good_document()
    document["ssh"] = {"host": "198.51.100.10", "port": 22}  # type: ignore[assignment]
    exc = _rejected(document)
    assert exc.fields == {
        "ssh.user": ProfileErrorCode.MISSING_FIELD.value,
        "ssh.host_key": ProfileErrorCode.MISSING_FIELD.value,
        "ssh.credential_ref": ProfileErrorCode.MISSING_FIELD.value,
    }


def test_missing_schema_version_reported() -> None:
    exc = _rejected(remove=("schema_version",))
    assert exc.fields == {
        "schema_version": ProfileErrorCode.MISSING_FIELD.value
    }


# ---------------------------------------------------------------------------
# Field-level constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name", ["", "   ", "x" * 201, "line\nbreak", "tab\tstop", 42, None]
)
def test_rejects_invalid_names(bad_name: object) -> None:
    assert "name" in _rejected({"name": bad_name}).fields


@pytest.mark.parametrize(
    "bad_revision", [0, -1, "3", 3.0, True, None]
)
def test_rejects_invalid_prompt_revisions(bad_revision: object) -> None:
    assert "prompt_revision" in _rejected({"prompt_revision": bad_revision}).fields


@pytest.mark.parametrize(
    "bad_host",
    [
        "http://host.example.org",
        "user@host.example.org",
        "host.example.org:2222",
        "host example.org",
        "::1",
        "[2001:db8::1",
        "[2001:db8::1]:22",
        "host/../etc",
        "",
        1234,
    ],
)
def test_rejects_invalid_ssh_hosts(bad_host: object) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "host": bad_host}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields.get("ssh.host") == ProfileErrorCode.INVALID_FIELD.value


@pytest.mark.parametrize("bad_ipv6", ["[2001:db8::1]", "[::1]", "[fe80::1%eth0]"])
def test_accepts_bracketed_ipv6_hosts(bad_ipv6: str) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "host": bad_ipv6}  # type: ignore[dict-item]
    assert parse_server_profile(document).ssh.host == bad_ipv6


@pytest.mark.parametrize("bad_port", [0, 65536, -22, "22", 22.0, True, None])
def test_rejects_invalid_ports(bad_port: object) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "port": bad_port}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields.get("ssh.port") == ProfileErrorCode.INVALID_FIELD.value


@pytest.mark.parametrize(
    "bad_user", ["", "deploy deploy", "deploy@example", "deploy:x", "x" * 65, 7]
)
def test_rejects_invalid_ssh_users(bad_user: object) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "user": bad_user}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields.get("ssh.user") == ProfileErrorCode.INVALID_FIELD.value


@pytest.mark.parametrize(
    "bad_key",
    [
        "sha256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4",
        "md5:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4",
        "SHA256:short",
        "SHA256:" + "A" * 42,
        "SHA256:" + "A" * 44,
        "AAAAB3NzaC1yc2EAAAADAQABAAABgQD…",
        1234,
    ],
)
def test_rejects_invalid_host_key_fingerprints(bad_key: object) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "host_key": bad_key}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields.get("ssh.host_key") == ProfileErrorCode.INVALID_FIELD.value


@pytest.mark.parametrize(
    "bad_ref", ["mai_key", "MAI-KEY", "MAI KEY", "", "-----BEGIN", "MAI;KEY", 5]
)
def test_rejects_invalid_credential_refs(bad_ref: object) -> None:
    document = _good_document()
    document["ssh"] = {**document["ssh"], "credential_ref": bad_ref}  # type: ignore[dict-item]
    exc = _rejected(document)
    assert exc.fields.get("ssh.credential_ref") == ProfileErrorCode.INVALID_FIELD.value


@pytest.mark.parametrize("bad_privilege", ["auto", "NONE", "", "sudo ", 1, None])
def test_rejects_invalid_privilege_modes(bad_privilege: object) -> None:
    exc = _rejected({"privilege": bad_privilege})
    assert exc.fields == {"privilege": ProfileErrorCode.INVALID_FIELD.value}


@pytest.mark.parametrize(
    "bad_caps", ["shell.exec", ["SHELL.EXEC"], ["shell exec"], [1], ["", "x"], {"a": 1}]
)
def test_rejects_invalid_capability_lists(bad_caps: object) -> None:
    exc = _rejected({"capabilities": bad_caps})
    assert exc.fields == {"capabilities": ProfileErrorCode.INVALID_FIELD.value}


# ---------------------------------------------------------------------------
# Document-level parsing
# ---------------------------------------------------------------------------


def test_non_mapping_documents_rejected() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile(["a", "b"])
    assert excinfo.value.fields == {"": ProfileErrorCode.INVALID_DOCUMENT.value}
    with pytest.raises(ProfileValidationError):
        parse_server_profile(None)


def test_duplicate_yaml_keys_rejected_not_silently_resolved() -> None:
    tampered = GOOD_PROFILE.replace(
        "privilege: none", "privilege: none\nprivilege: root"
    )
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile_text(tampered)
    assert excinfo.value.fields == {
        "": ProfileErrorCode.UNPARSABLE_DOCUMENT.value
    }


def test_unparsable_yaml_rejected() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        parse_server_profile_text("ssh: [unclosed")
    assert excinfo.value.fields == {"": ProfileErrorCode.UNPARSABLE_DOCUMENT.value}


def test_parsed_profile_is_immutable() -> None:
    profile = parse_server_profile_text(GOOD_PROFILE)
    with pytest.raises(Exception):  # noqa: B017 — frozen_dataclass raise is attr-specific
        profile.server_id = uuid.UUID(OTHER_SERVER_ID)  # type: ignore[misc]


def test_document_mutation_does_not_alias_parsed_profile() -> None:
    document = _good_document()
    snapshot = copy.deepcopy(document)
    profile = parse_server_profile(document)
    document["name"] = "mutated-after-parse"
    assert profile.name == snapshot["name"]


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_main_help_exits_zero() -> None:
    assert main(["--help"]) == 0
    assert main(["-h"]) == 0


def test_main_without_args_is_usage_error() -> None:
    assert main([]) == 2


def test_main_accepts_good_rejects_bad_without_echoing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = GOOD_PROFILE.replace(
        f'server_id: "{SERVER_ID}"', 'server_id: "web-prod"'
    ).replace("privilege: none", "password: hunter2\nprivilege: none")
    good_path = _write(tmp_path, "good.yaml", GOOD_PROFILE)
    bad_path = _write(tmp_path, "bad.yaml", bad)

    assert main([str(good_path)]) == 0
    out = capsys.readouterr().out
    assert out == f"OK {good_path}\n"

    assert main([str(good_path), str(bad_path)]) == 1
    out = capsys.readouterr().out
    assert f"OK {good_path}" in out
    line = next(line for line in out.splitlines() if line.startswith("INVALID"))
    assert "server_id=invalid_server_id" in line
    assert "password=inline_credential_forbidden" in line
    assert "hunter2" not in out
    assert "web-prod" not in out


def test_main_missing_file_is_io_error(tmp_path: Path) -> None:
    assert main([str(tmp_path / "absent.yaml")]) == 2


def test_module_entrypoint_runs_under_python_m(
    tmp_path: Path,
) -> None:
    bad = GOOD_PROFILE.replace("linux_ssh", "docker")
    good_path = _write(tmp_path, "good.yaml", GOOD_PROFILE)
    bad_path = _write(tmp_path, "bad.yaml", bad)
    completed = subprocess.run(
        [sys.executable, "-m", "mai.contracts.server", str(good_path), str(bad_path)],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 1
    assert f"OK {good_path}" in completed.stdout
    assert "environment=unsupported_environment" in completed.stdout
    assert completed.stderr == ""
