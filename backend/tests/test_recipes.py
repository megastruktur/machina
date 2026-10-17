"""Focused tests for the canonical application recipe schema (card 08-recipe-schema).

Acceptance coverage:

- **AC1** — the recipe declares prerequisites, persistent data, health
  verification, update/removal/recovery/backup semantics and the supported
  environment: strict-shape parse tests pin every frozen section, the
  vocabularies and the section requirements (missing semantics sections are
  rejected, never defaulted).
- **AC2** — validated inputs produce a policy-bound resource plan with no
  arbitrary hidden shell: golden-fixture plan comparison (exact target
  paths derived from opaque UUIDs, exact pinned image, exact operation
  list), unsupported-target refusals, and negative controls proving the
  schema has NO command/script field (unknown-field rejection at every
  executable-sounding position, argv-only healthcheck, digest-only image
  pinning, credential NAMES without values).

Pure module, pure tests: no fixtures, no database, no docker, no network —
``recipes.py`` performs no I/O by contract, so the parse → render → plan
pipeline is exercised directly (the "minimal actual entrypoint" evidence
run is the throwaway probe recorded in the card's evidence directory).

Boundaries follow the shared canonical-file precedents (adoption/profile):
duplicate keys, alias bombs, over-size and over-depth documents are
rejected before any semantic validation; every rejection is a value-free
:class:`RecipeError` with a stable code.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
import yaml

from mai.applications.recipes import (
    MAX_DOCUMENT_BYTES,
    OP_VERIFY_PREREQUISITES,
    RECIPE_SCHEMA_VERSION,
    TARGET_APPS_ROOT,
    ApplicationRecipe,
    DeploymentPlan,
    RecipeError,
    RecipeErrorCode,
    RemovalDataPolicy,
    SupportedEnvironment,
    TargetEnvironment,
    UpdateMigrations,
    build_deployment_plan,
    canonical_plan_json,
    parse_recipe_text,
    recipe_sha256,
    render_compose,
    render_recipe,
)

#: Fixed opaque identities for golden-fixture comparisons (C01 rule 1).
SERVER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
APPLICATION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROJECT_NAME = f"mai-app-{APPLICATION_ID.hex}"
APP_DIR = f"{TARGET_APPS_ROOT}/{APPLICATION_ID}"
DIGEST = "sha256:" + "ab" * 32
IMAGE = f"docker.io/library/nginx@{DIGEST}"
TARGET = TargetEnvironment(os="linux", arch="x86_64")


def _codes_of(excinfo: pytest.ExceptionInfo[RecipeError]) -> RecipeErrorCode:
    return excinfo.value.code


def _document() -> dict[str, object]:
    """The controlled catalogue recipe (frozen fixture for this suite)."""
    return {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "name": "webserver",
        "supported_environment": {"os": ["linux"], "arch": ["x86_64", "aarch64"]},
        "prerequisites": ["curl"],
        "required_grants": ["docker.compose"],
        "deploy": {
            "image": IMAGE,
            "ports": [
                {"host": 9090, "container": 443, "protocol": "tcp"},
                {"host": 8080, "container": 80, "protocol": "tcp"},
            ],
            "volumes": [
                {"name": "site-data", "container_path": "/usr/share/nginx/html"}
            ],
        },
        "health": {"kind": "http", "port": 80, "path": "/healthz"},
        "update": {"migrations": "none"},
        "removal": {"data_policy": "explicit_choice"},
        "recovery": {"image_rollback": True},
        "backup": {"required": True},
    }


def _text(document: dict[str, object] | None = None) -> str:
    return yaml.safe_dump(document if document is not None else _document())


def _parse(document: dict[str, object] | None = None) -> ApplicationRecipe:
    return parse_recipe_text(_text(document))


def _plan(
    recipe: ApplicationRecipe | None = None,
    target: TargetEnvironment = TARGET,
) -> DeploymentPlan:
    return build_deployment_plan(
        recipe if recipe is not None else _parse(),
        SERVER_ID,
        APPLICATION_ID,
        target,
    )


def _with(document: dict[str, object], **overrides: object) -> dict[str, object]:
    merged = dict(document)
    merged.update(overrides)
    return merged


# ---------------------------------------------------------------------------
# AC1: round trip, determinism, order normalization
# ---------------------------------------------------------------------------


def test_parse_render_round_trip_is_identity() -> None:
    recipe = _parse()
    assert parse_recipe_text(render_recipe(recipe)) == recipe


def test_render_is_byte_deterministic() -> None:
    recipe = _parse()
    assert render_recipe(recipe) == render_recipe(recipe)
    assert recipe_sha256(recipe) == recipe_sha256(recipe)


def test_collection_order_is_normalized_at_parse() -> None:
    document = _document()
    reversed_ports = list(reversed(document["deploy"]["ports"]))  # type: ignore[index]
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],  # type: ignore[arg-type]
        ports=sorted(reversed_ports, key=lambda p: p["host"], reverse=True),
    )
    document["supported_environment"] = {"os": ["linux"], "arch": ["aarch64", "x86_64"]}
    assert _parse(document) == _parse()


def test_full_recipe_round_trip_preserves_every_section() -> None:
    recipe = _parse()
    assert recipe.name == "webserver"
    assert recipe.supported_environment == SupportedEnvironment(
        os=("linux",), arch=("aarch64", "x86_64")
    )
    assert recipe.image == IMAGE
    assert [(p.host, p.container, p.protocol) for p in recipe.ports] == [
        (8080, 80, "tcp"),
        (9090, 443, "tcp"),
    ]
    assert [(v.name, v.container_path) for v in recipe.volumes] == [
        ("site-data", "/usr/share/nginx/html")
    ]
    assert recipe.prerequisites == ("curl",)
    assert recipe.required_grants == ("docker.compose",)
    assert recipe.health is not None
    assert (recipe.health.port, recipe.health.path) == (80, "/healthz")
    assert recipe.health.expected_status == 200
    assert recipe.update.migrations is UpdateMigrations.NONE
    assert recipe.removal.data_policy is RemovalDataPolicy.EXPLICIT_CHOICE
    assert recipe.recovery.image_rollback is True
    assert recipe.backup.required is True


# ---------------------------------------------------------------------------
# AC1: pinned image validation
# ---------------------------------------------------------------------------


def test_digest_pinned_image_is_accepted() -> None:
    assert _parse().image == IMAGE


@pytest.mark.parametrize(
    "image",
    [
        "nginx",
        "nginx:1.27",
        "nginx:latest",
        "docker.io/library/nginx:1.27",
    ],
)
def test_tag_or_bare_image_is_unpinned(image: str) -> None:
    document = _document()
    document["deploy"] = _with(document["deploy"], image=image)  # type: ignore[assignment,arg-type]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNPINNED_IMAGE


@pytest.mark.parametrize(
    "image",
    [
        "nginx@sha256:" + "ab" * 31,  # short digest
        "nginx@sha256:" + "ab" * 33,  # long digest
        "nginx@sha256:" + "XY" * 32,  # non-hex digest
        "nginx@sha256:" + "AB" * 32,  # uppercase digest
        "Nginx@sha256:" + "ab" * 32,  # uppercase repository
        "nginx extra@sha256:" + "ab" * 32,  # whitespace in repository
        "nginx$()@sha256:" + "ab" * 32,  # shell metacharacters
        "nginx@md5:" + "ab" * 32,  # wrong hash algorithm
        "",  # empty
    ],
)
def test_malformed_image_reference_is_invalid(image: str) -> None:
    document = _document()
    document["deploy"] = _with(document["deploy"], image=image)  # type: ignore[assignment,arg-type]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_IMAGE


def test_non_string_image_is_invalid() -> None:
    document = _document()
    document["deploy"] = _with(document["deploy"], image=123)  # type: ignore[assignment,arg-type]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_IMAGE


# ---------------------------------------------------------------------------
# AC1: port binding validation
# ---------------------------------------------------------------------------


def test_tcp_and_udp_bindings_are_accepted_sorted() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[
            {"host": 5353, "container": 53, "protocol": "udp"},
            {"host": 8080, "container": 80, "protocol": "tcp"},
        ],
    )
    recipe = _parse(document)
    assert [(p.host, p.container, p.protocol) for p in recipe.ports] == [
        (5353, 53, "udp"),
        (8080, 80, "tcp"),
    ]


@pytest.mark.parametrize(
    "port_field",
    ["host", "container"],
)
@pytest.mark.parametrize(
    "value",
    [0, -1, 65536, True, "8080", 1.5],
)
def test_out_of_bounds_or_non_int_ports_are_invalid(
    port_field: str, value: object
) -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 8080, "container": 80, "protocol": "tcp", port_field: value}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_PORT


def test_privileged_host_port_is_invalid() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 80, "container": 8080, "protocol": "tcp"}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_PORT


def test_duplicate_host_binding_is_refused() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[
            {"host": 8080, "container": 80, "protocol": "tcp"},
            {"host": 8080, "container": 81, "protocol": "tcp"},
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DUPLICATE_PORT


def test_duplicate_container_binding_is_refused() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[
            {"host": 8080, "container": 80, "protocol": "tcp"},
            {"host": 8081, "container": 80, "protocol": "tcp"},
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DUPLICATE_PORT


def test_unknown_protocol_is_invalid() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 8080, "container": 80, "protocol": "sctp"}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_PORT


def test_port_entry_with_unknown_field_is_rejected() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[
            {
                "host": 8080,
                "container": 80,
                "protocol": "tcp",
                "publish": True,
            }
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


def test_port_entry_missing_field_is_rejected() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 8080, "container": 80}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


# ---------------------------------------------------------------------------
# AC1: persistent volume validation (container paths only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "var/lib/data",  # relative
        "/",  # root
        "/var//lib",  # double slash
        "/var/lib/",  # trailing slash
        "/var/../lib",  # parent traversal
        "/var/./lib",  # current-dir segment
        "/var/lib/${SECRET}",  # Compose interpolation variable
        "/var/lib/$HOME",  # bare '$' interpolation character
        "/var/lib/ä",  # non-ascii
        "/var/li\tb",  # control character
        "/var/lib/" + "x" * 2000,  # over length
    ],
)
def test_unsafe_container_paths_are_invalid(path: str) -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        volumes=[{"name": "data", "container_path": path}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_VOLUME_PATH


@pytest.mark.parametrize(
    "name",
    ["Data", "1data", "data_site", "-data", "d" * 65, ""],
)
def test_malformed_volume_names_are_invalid(name: str) -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        volumes=[{"name": name, "container_path": "/var/lib/data"}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_VOLUME


def test_duplicate_volume_name_is_refused() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        volumes=[
            {"name": "data", "container_path": "/var/lib/a"},
            {"name": "data", "container_path": "/var/lib/b"},
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DUPLICATE_VOLUME


def test_duplicate_container_path_is_refused() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        volumes=[
            {"name": "a", "container_path": "/var/lib/data"},
            {"name": "b", "container_path": "/var/lib/data"},
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DUPLICATE_VOLUME


def test_volume_entry_with_unknown_field_is_rejected() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        volumes=[
            {
                "name": "data",
                "container_path": "/var/lib/data",
                "host_path": "/etc",  # host paths are planner-allocated, never declared
            }
        ],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


# ---------------------------------------------------------------------------
# AC1: health verification (argv HTTP probe only — no command fields)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["tcp", "cmd", "shell", "CMD", "exec"])
def test_non_http_health_kind_is_unsupported(kind: str) -> None:
    document = _document()
    document["health"] = {"kind": kind, "port": 80, "path": "/healthz"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_HEALTH_KIND


def test_health_port_must_reference_declared_port() -> None:
    document = _document()
    document["health"] = {"kind": "http", "port": 5555, "path": "/healthz"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.HEALTH_PORT_UNBOUND


def test_health_requires_a_declared_port_section() -> None:
    document = _document()
    document["deploy"] = _with(document["deploy"], ports=[])  # type: ignore[assignment,arg-type]
    document["health"] = {"kind": "http", "port": 80, "path": "/healthz"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.HEALTH_PORT_UNBOUND


def test_health_over_udp_binding_is_invalid() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 5353, "container": 53, "protocol": "udp"}],
    )
    document["health"] = {"kind": "http", "port": 53, "path": "/healthz"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_HEALTH_CHECK


@pytest.mark.parametrize(
    "path",
    [
        "healthz",
        "/health z",
        "/health\nz",
        "/heälth",
        "/health/$PATH",  # Compose interpolation character
        "/health/${SECRET}",  # Compose interpolation variable
    ],
)
def test_unsafe_health_paths_are_invalid(path: str) -> None:
    document = _document()
    document["health"] = {"kind": "http", "port": 80, "path": path}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_HEALTH_CHECK


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_status", 199),
        ("expected_status", 400),
        ("expected_status", "200"),
        ("interval_seconds", 4),
        ("interval_seconds", 3601),
        ("timeout_seconds", 0),
        ("timeout_seconds", 61),
        ("retries", 0),
        ("retries", 31),
        ("start_period_seconds", -1),
        ("start_period_seconds", 3601),
        ("port", 0),
        ("port", 65536),
        ("port", True),
    ],
)
def test_health_field_bounds_are_enforced(field: str, value: object) -> None:
    document = _document()
    document["health"] = _with(
        {"kind": "http", "port": 80, "path": "/healthz"}, **{field: value}
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_HEALTH_CHECK


@pytest.mark.parametrize(
    "test_value",
    [
        "curl -f http://localhost/healthz",
        ["CMD-SHELL", "curl -f http://localhost/healthz"],
        ["CMD", "curl", "-f", "http://localhost/healthz"],
    ],
)
def test_health_command_field_is_unknown_field(test_value: object) -> None:
    """The compose-style ``test`` field is not representable: health is a
    declarative probe. String (shell) AND argv forms are rejected — the
    schema has no command field at all."""
    document = _document()
    document["health"] = {"test": test_value, "kind": "http", "port": 80, "path": "/x"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


def test_absent_health_section_parses_to_none() -> None:
    document = _document()
    del document["health"]
    assert _parse(document).health is None


# ---------------------------------------------------------------------------
# AC1: credential REFERENCES (names only — C03 rules 2/6)
# ---------------------------------------------------------------------------


def test_credential_references_parse_sorted() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        credentials=["SMTP_PASSWORD", "API_KEY"],
    )
    assert _parse(document).credentials == ("API_KEY", "SMTP_PASSWORD")


@pytest.mark.parametrize("name", ["api_key", "Api-Key", "1KEY", "A" * 65, ""])
def test_malformed_credential_references_are_invalid(name: str) -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        credentials=[name],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE


def test_duplicate_credential_reference_is_refused() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        credentials=["API_KEY", "API_KEY"],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DUPLICATE_CREDENTIAL_REFERENCE


def test_credential_value_structures_are_unrepresentable() -> None:
    """A mapping-style credential (name + value) is not a valid reference —
    secret values have NO field in the schema (C03 rule 2)."""
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        credentials=[{"name": "API_KEY", "value": "hunter2"}],
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE


def test_credential_values_section_is_unknown_field() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        credentials=["API_KEY"],
        credentials_values={"API_KEY": "hunter2"},
    )
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


# ---------------------------------------------------------------------------
# AC1: prerequisites / grants / supported environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["-rf", "rm -rf", "RM", "a" * 65, "", "a;b"])
def test_shell_like_prerequisites_are_invalid(token: str) -> None:
    document = _with(_document(), prerequisites=[token])
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_PREREQUISITE


def test_duplicate_prerequisites_are_refused() -> None:
    document = _with(_document(), prerequisites=["curl", "curl"])
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_PREREQUISITE


@pytest.mark.parametrize("grant", ["Docker.Compose", "docker compose", "", "a" * 65])
def test_malformed_grants_are_invalid(grant: str) -> None:
    document = _with(_document(), required_grants=[grant])
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_GRANT


def test_duplicate_grants_are_refused() -> None:
    document = _with(_document(), required_grants=["docker.compose", "docker.compose"])
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_GRANT


@pytest.mark.parametrize("os_value", [["darwin"], ["windows"], ["linux", "darwin"], []])
def test_unsupported_os_vocabulary_is_rejected(os_value: list[str]) -> None:
    document = _document()
    document["supported_environment"] = {"os": os_value, "arch": ["x86_64"]}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_OS


@pytest.mark.parametrize("arch_value", [["armv7"], ["x86", "x86_64"], ["amd64"], []])
def test_unsupported_arch_vocabulary_is_rejected(arch_value: list[str]) -> None:
    document = _document()
    document["supported_environment"] = {"os": ["linux"], "arch": arch_value}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_ARCH


def test_environment_section_is_required() -> None:
    document = _document()
    del document["supported_environment"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


# ---------------------------------------------------------------------------
# AC1: update / removal / recovery / backup semantics are REQUIRED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("migrations", ["none", "reversible", "irreversible"])
def test_migration_vocabulary_is_accepted(migrations: str) -> None:
    document = _document()
    document["update"] = {"migrations": migrations}
    recipe = _parse(document)
    assert recipe.update.migrations == UpdateMigrations(migrations)


def test_unknown_migration_class_is_invalid() -> None:
    document = _document()
    document["update"] = {"migrations": "auto_rollback"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_UPDATE_SEMANTICS


def test_missing_update_section_is_rejected() -> None:
    document = _document()
    del document["update"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


def test_delete_data_policy_is_forbidden_in_recipes() -> None:
    """C09 rule 5: a recipe can never pre-authorize data deletion; the
    keep-or-delete decision belongs to the explicit removal action."""
    document = _document()
    document["removal"] = {"data_policy": "delete"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.DATA_DELETE_FORBIDDEN


def test_unknown_removal_data_policy_is_invalid() -> None:
    document = _document()
    document["removal"] = {"data_policy": "auto_delete_if_unused"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_REMOVAL_SEMANTICS


def test_missing_data_policy_is_rejected() -> None:
    """Feature-gate scenario fragment: a recipe without a declared data
    policy never reaches a plan."""
    document = _document()
    document["removal"] = {}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


def test_missing_removal_section_is_rejected() -> None:
    document = _document()
    del document["removal"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


def test_recovery_image_rollback_must_be_boolean() -> None:
    document = _document()
    document["recovery"] = {"image_rollback": "yes"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_FIELD


def test_missing_recovery_section_is_rejected() -> None:
    document = _document()
    del document["recovery"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


def test_backup_required_must_be_boolean() -> None:
    document = _document()
    document["backup"] = {"required": 1}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_FIELD


def test_missing_backup_section_is_rejected() -> None:
    document = _document()
    del document["backup"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


# ---------------------------------------------------------------------------
# Document-level strictness (name, version, unknown fields, hardening)
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_is_unsupported() -> None:
    document = _with(_document(), schema_version=2)
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.SCHEMA_VERSION_UNSUPPORTED


def test_missing_schema_version_is_rejected() -> None:
    document = _document()
    del document["schema_version"]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.MISSING_FIELD


@pytest.mark.parametrize("name", ["Webserver", "1web", "web server", "w" * 65, ""])
def test_malformed_names_are_invalid(name: str) -> None:
    document = _with(_document(), name=name)
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_NAME


@pytest.mark.parametrize(
    "field",
    ["hooks", "install_script", "pre_start", "post_install", "lifecycle"],
)
def test_top_level_command_fields_are_unknown(field: str) -> None:
    """Acceptance 2: every executable-sounding top-level field is rejected —
    there is no hook/script surface in the declarative schema."""
    document = _with(_document(), **{field: ["echo pwned"]})
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


@pytest.mark.parametrize(
    "field,value",
    [
        ("entrypoint", ["/bin/sh", "-c", "rm -rf /"]),
        ("command", "sh -c 'curl evil'"),
        ("pre_start_script", "do things"),
    ],
)
def test_deploy_command_fields_are_unknown(field: str, value: object) -> None:
    document = _document()
    document["deploy"] = _with(document["deploy"], **{field: value})  # type: ignore[assignment,arg-type]
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


def test_update_section_rejects_command_fields() -> None:
    document = _document()
    document["update"] = {"migrations": "none", "script": "migrate.sh"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


def test_removal_section_rejects_command_fields() -> None:
    document = _document()
    document["removal"] = {"data_policy": "keep", "purge_command": "rm -rf"}
    with pytest.raises(RecipeError) as excinfo:
        _parse(document)
    assert _codes_of(excinfo) is RecipeErrorCode.UNKNOWN_FIELD


def test_duplicate_keys_are_unparsable() -> None:
    text = (
        "schema_version: 1\n"
        "name: webserver\n"
        "name: other\n"
        "supported_environment: {os: [linux], arch: [x86_64]}\n"
        "deploy: {image: nginx}\n"
        "update: {migrations: none}\n"
        "removal: {data_policy: keep}\n"
        "recovery: {image_rollback: true}\n"
        "backup: {required: false}\n"
    )
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(text)
    assert _codes_of(excinfo) is RecipeErrorCode.UNPARSABLE_DOCUMENT


def test_non_mapping_document_is_invalid() -> None:
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text("- just\n- a\n- list\n")
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_FIELD


def test_unparsable_yaml_is_reported_value_free() -> None:
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text("name: [unclosed")
    assert _codes_of(excinfo) is RecipeErrorCode.UNPARSABLE_DOCUMENT


def test_alias_expansion_is_rejected_before_any_use() -> None:
    bomb = "a0: &a0 [" + ", ".join(["x"] * 10) + "]\n"
    for level in range(1, 6):
        bomb += f"a{level}: &a{level} [" + ", ".join([f"*a{level - 1}"] * 10) + "]\n"
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(bomb)
    assert _codes_of(excinfo) is RecipeErrorCode.DOCUMENT_ALIAS_FORBIDDEN


def test_oversized_document_is_rejected_before_parse() -> None:
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text("name: " + "x" * (MAX_DOCUMENT_BYTES + 1))
    assert _codes_of(excinfo) is RecipeErrorCode.DOCUMENT_TOO_LARGE


def test_deeply_nested_document_is_rejected_not_crashing() -> None:
    deep = "name: " + "[" * 30_000 + "]" * 30_000 + "\n"
    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(deep)
    assert _codes_of(excinfo) is RecipeErrorCode.DOCUMENT_TOO_DEEP


# ---------------------------------------------------------------------------
# AC2: deterministic Compose rendering
# ---------------------------------------------------------------------------


def test_compose_render_is_byte_deterministic() -> None:
    assert render_compose(_parse(), APPLICATION_ID) == render_compose(
        _parse(), APPLICATION_ID
    )


def test_minimal_compose_exact_fixture() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"], ports=[], volumes=[], credentials=[]
    )
    del document["health"]
    text = render_compose(_parse(document), APPLICATION_ID)
    assert text == (
        f"name: {PROJECT_NAME}\n"
        "services:\n"
        "  webserver:\n"
        f"    image: {IMAGE}\n"
        "    restart: unless-stopped\n"
    )


def test_compose_parse_back_matches_fixture() -> None:
    text = render_compose(_parse(), APPLICATION_ID)
    assert yaml.safe_load(text) == {
        "name": PROJECT_NAME,
        "services": {
            "webserver": {
                "image": IMAGE,
                "restart": "unless-stopped",
                "ports": ["8080:80/tcp", "9090:443/tcp"],
                "volumes": [f"{APP_DIR}/volumes/site-data:/usr/share/nginx/html"],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--output",
                        "/dev/null",
                        "http://127.0.0.1:80/healthz",
                    ],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                    "start_period": "30s",
                },
            }
        },
    }


def test_compose_carries_no_shell_and_no_interpolation() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"], credentials=["API_KEY"]
    )
    text = render_compose(_parse(document), APPLICATION_ID)
    assert "$" not in text  # no interpolation surface at all
    assert "CMD-SHELL" not in text
    assert "/bin/sh" not in text
    assert "bash" not in text
    # credentials appear ONLY as the env_file pointer, never as values
    assert yaml.safe_load(text)["services"]["webserver"]["env_file"] == [  # type: ignore[index]
        "credentials.env"
    ]


def test_compose_image_is_always_digest_pinned() -> None:
    text = render_compose(_parse(), APPLICATION_ID)
    assert DIGEST in text
    assert ":latest" not in text


# ---------------------------------------------------------------------------
# AC2: policy-bound deployment plan (golden fixture comparison)
# ---------------------------------------------------------------------------


def test_full_plan_matches_fixture() -> None:
    plan = _plan()
    assert plan.server_id == SERVER_ID
    assert plan.application_id == APPLICATION_ID
    assert plan.project_name == PROJECT_NAME
    assert (plan.target_os, plan.target_arch) == ("linux", "x86_64")
    assert plan.image == IMAGE
    assert plan.recipe_sha256 == recipe_sha256(_parse())
    assert plan.compose_path == f"{APP_DIR}/compose.yaml"
    assert plan.compose_sha256 == hashlib.sha256(plan.compose_text.encode()).hexdigest()
    assert plan.directories == (APP_DIR, f"{APP_DIR}/volumes/site-data")
    assert [(p.host, p.container, p.protocol) for p in plan.ports] == [
        (8080, 80, "tcp"),
        (9090, 443, "tcp"),
    ]
    assert [(v.name, v.container_path, v.host_path) for v in plan.volumes] == [
        ("site-data", "/usr/share/nginx/html", f"{APP_DIR}/volumes/site-data")
    ]
    assert plan.credentials == ()
    assert plan.credentials_path is None
    assert plan.health_probe is not None
    assert plan.health_probe.kind == "http"
    assert (plan.health_probe.port, plan.health_probe.path) == (80, "/healthz")
    assert plan.health_probe.expected_status == 200
    assert (plan.health_probe.interval_seconds, plan.health_probe.timeout_seconds) == (
        30,
        5,
    )
    assert (plan.health_probe.retries, plan.health_probe.start_period_seconds) == (
        3,
        30,
    )
    assert plan.prerequisites == ("curl",)
    assert plan.required_grants == ("docker.compose",)
    assert [(o.op, o.subject) for o in plan.operations] == [
        ("verify_prerequisites", "curl"),
        ("require_backup", f"{APP_DIR}/volumes/site-data"),
        (
            "create_directories",
            f"{APP_DIR},{APP_DIR}/volumes/site-data",
        ),
        ("write_compose", f"{APP_DIR}/compose.yaml"),
        ("pull_image", IMAGE),
        ("start", PROJECT_NAME),
        ("wait_healthy", "http://127.0.0.1:80/healthz"),
    ]
    assert list(plan.limitations) == [
        "declared update migrations are reversible",
        "recovery: the previous pinned image digest remains deployable",
        (
            "removal requires an explicit keep-or-delete data decision; "
            "absent intent refuses data deletion"
        ),
        "a backup of persistent data is required before data-affecting operations",
    ]
    assert hashlib.sha256(plan.compose_text.encode()).hexdigest() == plan.compose_sha256
    assert len(plan.plan_sha256) == 64


def test_plan_is_deterministic_across_rebuilds() -> None:
    assert _plan() == _plan()
    assert _plan().plan_sha256 == _plan().plan_sha256
    assert canonical_plan_json(_plan()) == canonical_plan_json(_plan())


def test_plan_sha256_binds_every_recipe_revision() -> None:
    base = _plan()
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"], image=f"docker.io/library/nginx@sha256:{'cd' * 32}"
    )
    other_image = _plan(_parse(document))
    assert other_image.recipe_sha256 != base.recipe_sha256
    assert other_image.compose_sha256 != base.compose_sha256
    assert other_image.plan_sha256 != base.plan_sha256

    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"],
        ports=[{"host": 8081, "container": 80, "protocol": "tcp"}],
    )
    other_port = _plan(_parse(document))
    assert other_port.plan_sha256 != base.plan_sha256

    document = _document()
    document["backup"] = {"required": False}
    other_backup = _plan(_parse(document))
    assert other_backup.plan_sha256 != base.plan_sha256
    assert not any(o.op == "require_backup" for o in other_backup.operations)


def test_plan_operations_come_from_frozen_vocabulary() -> None:
    allowed = {
        "verify_prerequisites",
        "require_backup",
        "create_directories",
        "write_compose",
        "write_credentials",
        "pull_image",
        "start",
        "wait_healthy",
    }
    plan = _plan()
    assert all(o.op in allowed for o in plan.operations)


def test_plan_with_credentials_binds_references_and_op() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"], credentials=["SMTP_PASSWORD", "API_KEY"]
    )
    plan = _plan(_parse(document))
    assert plan.credentials == ("API_KEY", "SMTP_PASSWORD")
    assert plan.credentials_path == f"{APP_DIR}/credentials.env"
    assert any(
        o.op == "write_credentials" and o.subject == plan.credentials_path
        for o in plan.operations
    )
    assert "credentials.env" in plan.compose_text


def test_plan_without_health_reports_unverified_limitation() -> None:
    document = _document()
    del document["health"]
    plan = _plan(_parse(document))
    assert plan.health_probe is None
    assert not any(o.op == "wait_healthy" for o in plan.operations)
    assert (
        "no declared health verification; deployment success is unverified"
        in plan.limitations
    )


def test_plan_without_volumes_skips_backup_and_reports_nothing_to_back_up() -> None:
    document = _document()
    document["deploy"] = _with(  # type: ignore[assignment]
        document["deploy"], volumes=[]
    )
    plan = _plan(_parse(document))
    assert not any(o.op == "require_backup" for o in plan.operations)
    assert "a backup of persistent data is required" not in "\n".join(plan.limitations)
    assert plan.directories == (APP_DIR,)


def test_plan_limitations_track_declared_semantics() -> None:
    document = _document()
    document["update"] = {"migrations": "irreversible"}
    document["recovery"] = {"image_rollback": False}
    document["removal"] = {"data_policy": "keep"}
    plan = _plan(_parse(document))
    assert (
        "declared update migrations may be irreversible; data rollback is not "
        "possible" in plan.limitations
    )
    assert (
        "recovery: no image rollback; recovery requires a new recipe revision"
        in plan.limitations
    )
    assert (
        "declared removal policy keeps all persistent data; no data deletion"
        in plan.limitations
    )


def test_plan_without_prerequisites_marks_subject_none() -> None:
    document = _with(_document(), prerequisites=[])
    plan = _plan(_parse(document))
    assert any(
        o.op == OP_VERIFY_PREREQUISITES and o.subject == "<none>"
        for o in plan.operations
    )


def test_plan_target_on_aarch64_is_accepted() -> None:
    plan = _plan(target=TargetEnvironment(os="linux", arch="aarch64"))
    assert (plan.target_os, plan.target_arch) == ("linux", "aarch64")


def test_plan_refuses_unsupported_target_os() -> None:
    with pytest.raises(RecipeError) as excinfo:
        _plan(target=TargetEnvironment(os="darwin", arch="x86_64"))
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_TARGET_OS


def test_plan_refuses_unsupported_target_arch() -> None:
    with pytest.raises(RecipeError) as excinfo:
        _plan(target=TargetEnvironment(os="linux", arch="armv7"))
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_TARGET_ARCH


def test_plan_refuses_narrowly_supported_environment() -> None:
    document = _document()
    document["supported_environment"] = {"os": ["linux"], "arch": ["aarch64"]}
    with pytest.raises(RecipeError) as excinfo:
        _plan(_parse(document), TARGET)
    assert _codes_of(excinfo) is RecipeErrorCode.UNSUPPORTED_TARGET_ARCH


def test_plan_requires_uuid_identities() -> None:
    recipe = _parse()
    with pytest.raises(RecipeError) as excinfo:
        build_deployment_plan(recipe, "srv", APPLICATION_ID, TARGET)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_SERVER_ID
    with pytest.raises(RecipeError) as excinfo:
        build_deployment_plan(recipe, SERVER_ID, "app", TARGET)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_APPLICATION_ID


def test_plan_requires_validated_recipe_and_typed_target() -> None:
    with pytest.raises(RecipeError) as excinfo:
        build_deployment_plan({"name": "x"}, SERVER_ID, APPLICATION_ID, TARGET)
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_FIELD
    with pytest.raises(RecipeError) as excinfo:
        build_deployment_plan(_parse(), SERVER_ID, APPLICATION_ID, {"os": "linux"})
    assert _codes_of(excinfo) is RecipeErrorCode.INVALID_TARGET_ENVIRONMENT


def test_plan_paths_stay_inside_the_frozen_application_scope() -> None:
    """C02 rule 3 pattern: every planned target path derives from the opaque
    application UUID below the frozen root — traversal is unrepresentable."""
    plan = _plan()
    prefix = f"{TARGET_APPS_ROOT}/{APPLICATION_ID}"
    assert plan.compose_path == f"{prefix}/compose.yaml"
    assert all(d.startswith(prefix) for d in plan.directories)
    assert all(v.host_path.startswith(prefix) for v in plan.volumes)
    assert ".." not in json.dumps(canonical_plan_json(plan))


def test_plan_canonical_json_is_valid_and_deterministic() -> None:
    plan = _plan()
    first = canonical_plan_json(plan)
    assert json.loads(first)["application_id"] == str(APPLICATION_ID)
    assert json.loads(first)["compose_text"] == plan.compose_text
    assert first == canonical_plan_json(_plan())
