"""Focused tests for the capability descriptor registry.

Cards/contracts: ``03-capability-registry`` against contracts.md
**C03 rules 1 and 7** — the frozen descriptor vocabulary, duplicate/
incompatible-version rejection, and the trust boundary that registration
is declarative metadata only (never an execution grant, never a stored
code reference). Identifier charset is frozen by the canonical profile
schema (``mai.contracts.server`` ``_CAPABILITY_RE``).

Acceptance coverage:

- AC1: identifiers/versions/schemas/environment/effect metadata are
  validated against the frozen vocabulary; duplicates and incompatible
  versions (same major, different call contract) are rejected; every
  rejected registration is atomic — the registry never keeps a partially
  enabled tool.
- AC2: the registry exposes no execution surface; descriptors carry no
  code references or credential-shaped values (strict vocabulary), list
  output is an immutable, JSON-safe snapshot safe for UI/model tool
  selection.
- Feature-gate scenario half owned here: a *real* fixture Python module
  (executed at import, proven by a filesystem marker) is loaded through
  the production loader and registered; duplicate/invalid registration
  fails without changing the registry.

Secret hygiene: the synthetic marker ``...not-a-secret`` plays any
content that must never reappear in error details; identifiers, versions
and slugs in this file are inert declarative strings.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mai.plugins.registry import (
    CAPABILITY_DECLARATION,
    MAX_SCHEMA_JSON_BYTES,
    VERSION_RE,
    CapabilityDescriptor,
    CapabilityError,
    CapabilityErrorCode,
    CapabilityRegistry,
    descriptor_from_module,
    parse_descriptor,
)

#: Synthetic non-disclosure marker (NOT a secret): embedded into schema
#: documents and raised module messages; assertions require its absence
#: from every rejection detail.
SENTINEL = "mai-03-registry-sentinel-not-a-secret"

CLI = [sys.executable, "-m", "mai.plugins.registry"]


def _descriptor(**overrides: object) -> dict[str, object]:
    """One valid descriptor document, overridden per test."""
    document: dict[str, object] = {
        "identifier": "deploy.app",
        "version": "1.0.0",
        "config_schema": None,
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
        },
        "output_schema": {"type": "object"},
        "execution_environments": ["linux_ssh"],
        "effect_class": "mutate",
        "resource_scope": ["target.fs"],
        "required_grants": ["deploy.app"],
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# parse_descriptor: the frozen vocabulary (AC1)
# ---------------------------------------------------------------------------


def test_parse_minimal_read_descriptor() -> None:
    descriptor = parse_descriptor(
        _descriptor(
            effect_class="read",
            resource_scope=[],
            required_grants=[],
        )
    )
    assert descriptor.identifier == "deploy.app"
    assert descriptor.version == "1.0.0"
    assert descriptor.config_schema is None
    assert descriptor.effect_class == "read"
    assert descriptor.resource_scope == ()
    assert descriptor.required_grants == ()
    assert descriptor.version_tuple == (1, 0, 0)
    assert descriptor.major == 1


def test_parse_full_descriptor_with_config_schema() -> None:
    config = {"type": "object", "properties": {"retries": {"type": "integer"}}}
    descriptor = parse_descriptor(_descriptor(config_schema=config))
    assert descriptor.config_schema == config
    assert descriptor.execution_environments == ("linux_ssh",)
    assert descriptor.to_json()["config_schema"] == config


def test_to_json_emits_exact_frozen_vocabulary_in_order() -> None:
    document = parse_descriptor(_descriptor()).to_json()
    assert tuple(document) == (
        "identifier",
        "version",
        "config_schema",
        "input_schema",
        "output_schema",
        "execution_environments",
        "effect_class",
        "resource_scope",
        "required_grants",
    )
    # Declarative output is JSON-serializable as-is: no code references,
    # no opaque objects (AC2 list output).
    assert json.loads(json.dumps(document)) == document


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"identifier": ""}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "Deploy.App"}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": ".hidden"}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "has space"}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "a/b"}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "a:b"}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "a" * 129}, CapabilityErrorCode.INVALID_IDENTIFIER),
        ({"identifier": "a" * 128}, None),
    ],
)
def test_identifier_charset_frozen_by_profile_schema(
    overrides: dict[str, object], code: CapabilityErrorCode | None
) -> None:
    if code is None:
        assert parse_descriptor(_descriptor(**overrides)).identifier == "a" * 128
        return
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(**overrides))
    assert raised.value.code is code


@pytest.mark.parametrize(
    "version",
    ["1", "1.2", "v1.2.3", "01.2.3", "1.02.3", "1.2.3-rc1", "1.2.3+build", "1.2.3.4", ""],
)
def test_version_requires_semver_core(version: str) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(version=version))
    assert raised.value.code is CapabilityErrorCode.INVALID_VERSION


def test_version_boundaries_accepted() -> None:
    assert VERSION_RE.fullmatch("0.0.0")
    assert parse_descriptor(_descriptor(version="10.20.30")).version_tuple == (10, 20, 30)


@pytest.mark.parametrize("field", ["input_schema", "output_schema"])
@pytest.mark.parametrize("bad", ["string", 42, True, [], None])
def test_call_schemas_required_json_objects(field: str, bad: object) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(**{field: bad}))
    assert raised.value.code is CapabilityErrorCode.INVALID_SCHEMA


def test_schema_rejects_non_string_keys_silently_coerced_by_json() -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(input_schema={"type": "object", 1: "x"}))
    assert raised.value.code is CapabilityErrorCode.INVALID_SCHEMA
    assert "keys must be strings" in raised.value.detail


def test_schema_rejects_non_json_safe_values() -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(output_schema={"type": float("nan")}))
    assert raised.value.code is CapabilityErrorCode.INVALID_SCHEMA


def test_oversized_schema_rejected_without_quoting_content() -> None:
    padding = "x" * (MAX_SCHEMA_JSON_BYTES + 1)
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(input_schema={"type": "object", "pad": padding}))
    assert raised.value.code is CapabilityErrorCode.INVALID_SCHEMA
    assert "exceeds" in raised.value.detail
    assert padding[:64] not in raised.value.detail


@pytest.mark.parametrize(
    ("environments", "valid"),
    [
        (["linux_ssh"], True),
        (["controller"], True),
        (["controller", "linux_ssh"], True),
        ([], False),
        (["windows_rdp"], False),
        (["linux_ssh", "linux_ssh"], False),
        ([1], False),
        ("linux_ssh", False),
    ],
)
def test_execution_environments_frozen_vocabulary(
    environments: object, valid: bool
) -> None:
    if valid:
        descriptor = parse_descriptor(
            _descriptor(execution_environments=environments)
        )
        assert descriptor.execution_environments == tuple(environments)
        return
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(execution_environments=environments))
    assert raised.value.code is CapabilityErrorCode.INVALID_ENVIRONMENTS


@pytest.mark.parametrize(
    "effect", ["read", "mutate", "privileged"]
)
def test_effect_class_frozen_vocabulary(effect: str) -> None:
    assert parse_descriptor(_descriptor(effect_class=effect)).effect_class == effect


@pytest.mark.parametrize("effect", ["READ", "write", "", "delete", None, 1])
def test_effect_class_rejects_unknown(effect: object) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(effect_class=effect))
    assert raised.value.code is CapabilityErrorCode.INVALID_EFFECT_CLASS


def test_effect_without_scope_rejected_for_mutate_and_privileged() -> None:
    # Cross-field frozen rule: an effect that can change state must
    # declare the resources it touches (lock/planning surface).
    for effect in ("mutate", "privileged"):
        with pytest.raises(CapabilityError) as raised:
            parse_descriptor(_descriptor(effect_class=effect, resource_scope=[]))
        assert raised.value.code is CapabilityErrorCode.INVALID_RESOURCE_SCOPE


@pytest.mark.parametrize(
    "scope",
    [[], ["target.fs"], ["target.fs", "target.proc"], ["controller.db"]],
)
def test_resource_scope_slugs_for_read_capabilities(scope: list[str]) -> None:
    descriptor = parse_descriptor(
        _descriptor(effect_class="read", resource_scope=scope)
    )
    assert descriptor.resource_scope == tuple(scope)


@pytest.mark.parametrize(
    "scope",
    [["Target.Fs"], ["has space"], ["a" * 65], ["ok", "ok"], "target.fs", [1]],
)
def test_resource_scope_rejects_bad_slugs(scope: object) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(resource_scope=scope))
    assert raised.value.code is CapabilityErrorCode.INVALID_RESOURCE_SCOPE


@pytest.mark.parametrize(
    "grants", [[], ["deploy.app"], ["deploy.app", "logs.read"], ["a" * 128]]
)
def test_required_grants_accept_identifier_slugs(grants: list[str]) -> None:
    descriptor = parse_descriptor(_descriptor(required_grants=grants))
    assert descriptor.required_grants == tuple(grants)


@pytest.mark.parametrize("grants", [["Deploy"], ["ok", "ok"], ["a b"], "deploy", [1]])
def test_required_grants_reject_bad_names(grants: object) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(required_grants=grants))
    assert raised.value.code is CapabilityErrorCode.INVALID_GRANTS


def test_unknown_field_rejected_and_content_not_leaked() -> None:
    # A "handler" key is exactly how a code reference would try to enter
    # a descriptor; the strict vocabulary rejects it and never echoes the
    # value back.
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(_descriptor(handler=f"evil_mod:run({SENTINEL})"))
    assert raised.value.code is CapabilityErrorCode.UNKNOWN_FIELD
    assert "handler" in raised.value.detail
    assert SENTINEL not in raised.value.detail


@pytest.mark.parametrize(
    "field",
    [
        "identifier",
        "version",
        "input_schema",
        "output_schema",
        "execution_environments",
        "effect_class",
        "resource_scope",
        "required_grants",
    ],
)
def test_missing_required_field_named(field: str) -> None:
    document = _descriptor()
    del document[field]
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(document)
    assert raised.value.code is CapabilityErrorCode.MISSING_FIELD
    assert field in raised.value.detail


@pytest.mark.parametrize("document", ["{}", "[]", '"x"', 42, None])
def test_descriptor_must_be_mapping(document: object) -> None:
    with pytest.raises(CapabilityError) as raised:
        parse_descriptor(document)
    assert raised.value.code is CapabilityErrorCode.NOT_A_MAPPING


# ---------------------------------------------------------------------------
# Registry: duplicates, incompatible versions, atomicity (AC1)
# ---------------------------------------------------------------------------


def test_register_and_list_roundtrip() -> None:
    registry = CapabilityRegistry()
    stored = registry.register(_descriptor())
    assert isinstance(stored, CapabilityDescriptor)
    assert registry.get("deploy.app", "1.0.0") is stored
    assert registry.versions("deploy.app") == ("1.0.0",)
    assert [d.identifier for d in registry.list_capabilities()] == ["deploy.app"]


def test_register_accepts_parsed_descriptor_instance() -> None:
    registry = CapabilityRegistry()
    stored = registry.register(parse_descriptor(_descriptor()))
    assert registry.get("deploy.app", "1.0.0") is stored


def test_duplicate_registration_rejected_even_if_identical() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor())
    with pytest.raises(CapabilityError) as raised:
        registry.register(_descriptor())
    assert raised.value.code is CapabilityErrorCode.DUPLICATE_REGISTRATION
    assert len(registry) == 1


def test_incompatible_same_major_input_schema_rejected() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="1.0.0"))
    broken = _descriptor(
        version="1.1.0",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    with pytest.raises(CapabilityError) as raised:
        registry.register(broken)
    assert raised.value.code is CapabilityErrorCode.INCOMPATIBLE_VERSION
    assert registry.versions("deploy.app") == ("1.0.0",)


def test_incompatible_same_major_output_schema_rejected() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="2.0.0"))
    with pytest.raises(CapabilityError) as raised:
        registry.register(_descriptor(version="2.1.0", output_schema={"type": "string"}))
    assert raised.value.code is CapabilityErrorCode.INCOMPATIBLE_VERSION


def test_compatible_minor_evolution_accepted() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="1.0.0", config_schema=None))
    evolved = registry.register(
        _descriptor(
            version="1.1.0",
            config_schema={"type": "object", "properties": {"retries": {}}},
        )
    )
    assert registry.versions("deploy.app") == ("1.0.0", "1.1.0")
    assert evolved.config_schema is not None


def test_new_major_may_change_call_contract() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="1.0.0"))
    successor = registry.register(
        _descriptor(version="2.0.0", input_schema={"type": "object"})
    )
    assert successor.major == 2
    assert registry.versions("deploy.app") == ("1.0.0", "2.0.0")


def test_first_registration_in_major_pins_the_line() -> None:
    # Order-independence is not promised: whoever registers first in a
    # major pins its call contract (documented decision).
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="1.1.0"))
    with pytest.raises(CapabilityError) as raised:
        registry.register(
            _descriptor(
                version="1.0.0",
                input_schema={"type": "object", "properties": {"x": {}}},
            )
        )
    assert raised.value.code is CapabilityErrorCode.INCOMPATIBLE_VERSION


def test_rejected_registration_leaves_no_partially_enabled_tool() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor(version="1.0.0"))
    before = registry.list_capabilities()
    rejections = [
        _descriptor(version="1.0.0"),  # duplicate
        _descriptor(version="1.1.0", output_schema={"type": "string"}),  # incompatible
        _descriptor(identifier="BROKEN"),  # invalid identifier
        _descriptor(execution_environments=[]),  # invalid metadata
        _descriptor(required_grants=["X Y"]),  # invalid grants
    ]
    for candidate in rejections:
        with pytest.raises(CapabilityError):
            registry.register(candidate)
        assert registry.list_capabilities() == before
        assert len(registry) == 1
        assert registry.get("deploy.app", "1.1.0") is None


def test_versions_sorted_semver_not_lexicographic() -> None:
    registry = CapabilityRegistry()
    for version in ("1.10.0", "1.2.0", "0.9.0"):
        registry.register(_descriptor(version=version))
    assert registry.versions("deploy.app") == ("0.9.0", "1.2.0", "1.10.0")
    assert [d.version for d in registry.list_capabilities()] == [
        "0.9.0",
        "1.2.0",
        "1.10.0",
    ]


def test_unknown_identifier_empty_versions_and_none_get() -> None:
    registry = CapabilityRegistry()
    assert registry.get("nope", "1.0.0") is None
    assert registry.versions("nope") == ()


# ---------------------------------------------------------------------------
# Registry: list output safety (AC2)
# ---------------------------------------------------------------------------


def test_list_output_is_snapshot_isolated_from_callers() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor())
    listed = registry.list_capabilities()
    document = listed[0].to_json()
    document["input_schema"]["properties"]["app_id"]["type"] = SENTINEL
    document["identifier"] = "hijacked"
    again = registry.list_capabilities()
    assert again[0].identifier == "deploy.app"
    assert again[0].to_json()["input_schema"]["properties"]["app_id"]["type"] == "string"
    # Distinct copies per call: no shared mutable state between consumers.
    assert again[0].to_json() is not again[0].to_json()


def test_descriptors_carry_no_code_references_or_executable_surface() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor())
    for descriptor in registry.list_capabilities():
        document = descriptor.to_json()
        # The strict vocabulary is the whole surface: nothing in a
        # descriptor can name a module, callable or entry point.
        assert set(document) == {
            "identifier",
            "version",
            "config_schema",
            "input_schema",
            "output_schema",
            "execution_environments",
            "effect_class",
            "resource_scope",
            "required_grants",
        }
        assert json.loads(json.dumps(document)) == document
    registry_methods = {
        name for name in vars(CapabilityRegistry) if not name.startswith("_")
    }
    assert registry_methods == {"register", "get", "versions", "list_capabilities"}


def test_registry_repr_is_count_only() -> None:
    registry = CapabilityRegistry()
    registry.register(_descriptor())
    assert repr(registry) == "<CapabilityRegistry descriptors=1>"


# ---------------------------------------------------------------------------
# Fixture Python capability: real module loading (feature-gate scenario)
# ---------------------------------------------------------------------------


def _write_capability_module(
    path: Path, document: dict[str, object] | None = None, *, marker: Path | None = None
) -> Path:
    declaration = json.dumps(document if document is not None else _descriptor())
    lines: list[str] = ["import json"]
    if marker is not None:
        # Executed at import: proves the loader really loaded this module.
        lines.append(f"Path({str(marker)!r}).write_text('imported')")
        lines.insert(0, "from pathlib import Path")
    lines.append(f"{CAPABILITY_DECLARATION} = json.loads({declaration!r})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_real_fixture_module_loaded_registration_executes_nothing(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "import-marker.txt"
    module = _write_capability_module(tmp_path / "fixture_cap.py", marker=marker)
    registry = CapabilityRegistry()

    descriptor = descriptor_from_module(str(module))
    assert descriptor.identifier == "deploy.app"
    # The module genuinely executed at import (trusted-code loading).
    assert marker.read_text() == "imported"

    stored = registry.register(descriptor)
    assert registry.versions("deploy.app") == ("1.0.0",)
    # Registration and selection never execute the capability: the import
    # marker is written exactly once (module import), not on register/list.
    registry.list_capabilities()
    assert marker.read_text() == "imported"

    # Loading leaves no import-system residue and the registry stores no
    # code reference: only the validated declaration survives.
    assert not [name for name in sys.modules if name.startswith("_mai_capability_")]
    assert json.dumps(stored.to_json())  # declarative JSON, no handles


def test_duplicate_module_registration_fails_without_partial_enable(
    tmp_path: Path,
) -> None:
    module = _write_capability_module(tmp_path / "fixture_cap.py")
    registry = CapabilityRegistry()
    registry.register(descriptor_from_module(str(module)))
    before = registry.list_capabilities()
    with pytest.raises(CapabilityError) as raised:
        registry.register(descriptor_from_module(str(module)))
    assert raised.value.code is CapabilityErrorCode.DUPLICATE_REGISTRATION
    assert registry.list_capabilities() == before


def test_invalid_module_declaration_rejected_registry_unchanged(
    tmp_path: Path,
) -> None:
    module = _write_capability_module(tmp_path / "bad_cap.py", _descriptor(version="1"))
    registry = CapabilityRegistry()
    with pytest.raises(CapabilityError) as raised:
        registry.register(descriptor_from_module(str(module)))
    assert raised.value.code is CapabilityErrorCode.INVALID_VERSION
    assert len(registry) == 0


def test_module_without_declaration_rejected(tmp_path: Path) -> None:
    module = tmp_path / "no_decl.py"
    module.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(module))
    assert raised.value.code is CapabilityErrorCode.DECLARATION_MISSING
    assert CAPABILITY_DECLARATION in raised.value.detail


def test_raising_module_classified_without_message_leak(tmp_path: Path) -> None:
    module = tmp_path / "boom.py"
    module.write_text(f'raise RuntimeError("{SENTINEL}")\n', encoding="utf-8")
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(module))
    assert raised.value.code is CapabilityErrorCode.MODULE_LOAD_ERROR
    assert "RuntimeError" in raised.value.detail
    assert SENTINEL not in raised.value.detail


def test_module_syntax_error_classified(tmp_path: Path) -> None:
    module = tmp_path / "syntax.py"
    module.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(module))
    assert raised.value.code is CapabilityErrorCode.MODULE_LOAD_ERROR


def test_missing_or_oversized_module_rejected(tmp_path: Path) -> None:
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(tmp_path / "absent.py"))
    assert raised.value.code is CapabilityErrorCode.MODULE_LOAD_ERROR

    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" * (MAX_SCHEMA_JSON_BYTES * 20))
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(big))
    assert raised.value.code is CapabilityErrorCode.MODULE_LOAD_ERROR
    assert "exceeds" in raised.value.detail


def test_module_declaration_cannot_smuggle_code_reference(
    tmp_path: Path,
) -> None:
    smuggled = _descriptor()
    smuggled["entry_point"] = "fixture_cap:run"
    module = _write_capability_module(tmp_path / "smuggler.py", smuggled)
    with pytest.raises(CapabilityError) as raised:
        descriptor_from_module(str(module))
    assert raised.value.code is CapabilityErrorCode.UNKNOWN_FIELD


# ---------------------------------------------------------------------------
# CLI entrypoint (real subprocess)
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args], capture_output=True, text=True, check=False
    )


def test_cli_validate_ok_and_rejections(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_descriptor()), encoding="utf-8")
    done = _run_cli("validate", str(good))
    assert done.returncode == 0, done.stderr
    line = json.loads(done.stdout)
    assert line == {
        "kind": "ok",
        "file": str(good),
        "identifier": "deploy.app",
        "version": "1.0.0",
        "effect_class": "mutate",
    }

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_descriptor(effect_class="root")), encoding="utf-8")
    done = _run_cli("validate", str(good), str(bad))
    assert done.returncode == 1
    lines = [json.loads(l) for l in done.stdout.splitlines()]
    assert [l["kind"] for l in lines] == ["ok", "error"]
    assert lines[1]["code"] == CapabilityErrorCode.INVALID_EFFECT_CLASS.value

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    done = _run_cli("validate", str(malformed))
    assert done.returncode == 1
    assert json.loads(done.stdout)["code"] == CapabilityErrorCode.INVALID_JSON.value

    missing = tmp_path / "absent.json"
    done = _run_cli("validate", str(missing))
    assert done.returncode == 1
    assert json.loads(done.stdout)["code"] == CapabilityErrorCode.NOT_READABLE.value


def test_cli_load_module(tmp_path: Path) -> None:
    module = _write_capability_module(tmp_path / "fixture_cap.py")
    done = _run_cli("load-module", str(module))
    assert done.returncode == 0, done.stderr
    line = json.loads(done.stdout)
    assert line["kind"] == "ok"
    assert line["identifier"] == "deploy.app"
    assert line["effect_class"] == "mutate"

    done = _run_cli("load-module", str(tmp_path / "absent.py"))
    assert done.returncode == 1
    assert json.loads(done.stdout)["code"] == CapabilityErrorCode.MODULE_LOAD_ERROR.value


def test_cli_usage_error_exits_two() -> None:
    done = _run_cli()
    assert done.returncode == 2
    done = _run_cli("validate")
    assert done.returncode == 2
