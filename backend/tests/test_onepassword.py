"""Focused tests for the optional 1Password credential provider.

The real 1Password CLI + an approved test vault is a separately approved,
owner-gated fixture (brief scenario; `op` is absent on this host, so the
real-provider journey stays BLOCKED for 11-verify — never claimed from a
mock). Everything here proves the **in-process contract** through a fake
`op` binary: reference validation, the token/argv/environment boundary,
bounded execution, fail-closed classification and value-free errors.

Sentinels below are synthetic non-disclosure markers, NOT secrets: absence
assertions against them prove the renderer/CLI/errors never emitted real
material.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mai.credentials.env import EnvResolver
from mai.credentials.onepassword import (
    MAX_LIST_OUTPUT_BYTES,
    MAX_OUTPUT_BYTES,
    OP_SERVICE_ACCOUNT_FLOOR,
    OnePasswordError,
    OnePasswordErrorCode,
    OnePasswordItemPage,
    OnePasswordProbeReport,
    OnePasswordResolver,
    StoredSecretReceipt,
)

SENTINEL = "mai-11-onepassword-value-sentinel-not-a-secret"
TOKEN_SENTINEL = "mai-11-onepassword-token-sentinel-not-a-secret"
DECOY_SENTINEL = "decoy-mai-11-onepassword-ambient-never-passed"
REFERENCE = "op://mai-dev/test-item/api-key"
ENV_TOKEN_KEY = "OP_SERVICE_ACCOUNT_TOKEN"
ENV_TOKEN_REF = "MAI_OP_SERVICE_TOKEN"

#: Version fixtures around the frozen service-account floor.
VERSION_OK = "2.31.0"
VERSION_FLOOR = ".".join(map(str, OP_SERVICE_ACCOUNT_FLOOR))
VERSION_BELOW = "2.17.9"

# ---------------------------------------------------------------------------
# Fake `op` binary: reads its behavior config from its own directory (the
# scrubbed child env must not be needed for fixture wiring).
# ---------------------------------------------------------------------------

FAKE_OP_SCRIPT = """#!/usr/bin/env python3
\"\"\"Fake `op` CLI fixture for the mai onepassword plugin tests (no secrets).\"\"\"
import json
import os
import pathlib
import sys
import time

here = pathlib.Path(__file__).resolve().parent
config = json.loads((here / "op-config.json").read_text(encoding="utf-8"))
store_path = here / "op-store.json"


def load_store():
    if store_path.exists():
        return json.loads(store_path.read_text(encoding="utf-8"))
    return {"next_id": 1, "items": []}


def save_store(store):
    store_path.write_text(json.dumps(store), encoding="utf-8")


def option_value(argv, flag, default=None):
    if flag in argv:
        return argv[argv.index(flag) + 1]
    return default


stdin_bytes = b""
if sys.stdin is not None and sys.stdin.buffer is not None:
    stdin_bytes = sys.stdin.buffer.read()

with (here / "invocations.jsonl").open("a", encoding="utf-8") as dump:
    dump.write(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "env_keys": sorted(os.environ),
                "env_token": os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"),
                "stdin": stdin_bytes.decode("utf-8", "replace"),
                "stdin_len": len(stdin_bytes),
            }
        )
        + "\\n"
    )

if sys.argv[1] == "--version":
    print(config["version"])
    raise SystemExit(0)

if sys.argv[1] == "read":
    behavior = config["behavior"]
    if behavior == "timeout":
        time.sleep(float(config["sleep"]))
        raise SystemExit(0)
    if behavior in ("fail", "stderr-leak"):
        sys.stderr.write(config["stderr"])
        raise SystemExit(int(config["rc"]))
    if behavior == "overlarge":
        sys.stdout.write(config["value"])
        sys.stdout.write("x" * int(config["pad"]))
        raise SystemExit(0)
    if behavior == "bad-utf8":
        sys.stdout.buffer.write(b"\\xff\\xfe\\xfd")
        raise SystemExit(0)
    if behavior == "empty":
        raise SystemExit(0)
    # Stored items win: read-back sees what item create/edit wrote.
    parts = sys.argv[2].split("/")
    if len(parts) == 5:
        for item in load_store()["items"]:
            if item["title"] == parts[3] and parts[4] in item.get("fields", {}):
                override = config.get("read_value_override")
                sys.stdout.write(override if override else item["fields"][parts[4]])
                raise SystemExit(0)
    if sys.argv[2] != config["reference"]:
        sys.stderr.write("unexpected reference")
        raise SystemExit(3)
    sys.stdout.write(config["value"])
    raise SystemExit(0)

if sys.argv[1] == "item" and sys.argv[2] == "list":
    behavior = config.get("list_behavior", "ok")
    if behavior == "timeout":
        time.sleep(float(config["sleep"]))
        raise SystemExit(0)
    if behavior == "fail":
        sys.stderr.write(config.get("stderr", "list failed"))
        raise SystemExit(int(config.get("rc", 4)))
    vault = option_value(sys.argv[1:], "--vault")
    category = option_value(sys.argv[1:], "--categories")
    rows = list(config.get("items", [])) + load_store()["items"]
    if vault is not None:
        rows = [row for row in rows if row.get("vault") == vault]
    if category:
        rows = [
            row
            for row in rows
            if str(row.get("category", "")).lower() == category.lower()
        ]
    sys.stdout.write(json.dumps(rows))
    if behavior == "overlarge":
        sys.stdout.write("x" * int(config.get("pad", 0)))
    raise SystemExit(0)

if sys.argv[1] == "item" and sys.argv[2] in ("create", "edit"):
    if config.get("create_behavior", "ok") == "fail":
        sys.stderr.write(config.get("create_stderr", "conflict"))
        raise SystemExit(int(config.get("create_rc", 5)))
    template = json.loads(stdin_bytes.decode("utf-8"))
    store = load_store()
    if sys.argv[2] == "create":
        item_id = "it-%03d" % store["next_id"]
        store["next_id"] += 1
    else:
        item_id = sys.argv[3]
    generated = None
    length = 0
    for arg in sys.argv[1:]:
        if arg.startswith("--generate-password="):
            generated = arg.split("=", 1)[1]
            length = int(generated.split(",")[0])
            break
    fields = {}
    for field in template.get("fields", []):
        if field.get("value") is not None:
            fields[field["label"]] = field["value"]
        elif generated and field.get("purpose") == "PASSWORD":
            fields[field["label"]] = ("Aa1!" + "x" * max(length - 4, 0))[:length]
    item = None
    for existing in store["items"]:
        if existing["id"] == item_id:
            item = existing
            break
    if item is None:
        item = {
            "id": item_id,
            "vault": option_value(sys.argv[1:], "--vault", ""),
            "category": template.get("category", "LOGIN"),
            "updated_at": "2026-01-01T00:00:00Z",
        }
        store["items"].append(item)
    if template.get("title"):
        item["title"] = template["title"]
    if fields:
        item["fields"] = fields
    if template.get("notes"):
        item["notes"] = template["notes"]
    save_store(store)
    print(
        json.dumps(
            {
                "id": item["id"],
                "title": item["title"],
                "category": item["category"],
                "vault": item["vault"],
                "updated_at": item["updated_at"],
            }
        )
    )
    raise SystemExit(0)

if sys.argv[1] == "vault":
    if config.get("vault_behavior", "ok") == "fail":
        sys.stderr.write(config.get("stderr", "vault unavailable"))
        raise SystemExit(int(config.get("rc", 4)))
    if sys.argv[2] == "get":
        print(json.dumps({"id": "vault-1", "name": sys.argv[3]}))
        raise SystemExit(0)
    if sys.argv[2] == "list":
        print(json.dumps(config.get("vaults", [{"id": "vault-1", "name": "mai-dev"}])))
        raise SystemExit(0)

sys.stderr.write("usage")
raise SystemExit(2)
"""


def _install_fake_op(
    tmp_path: Path,
    *,
    behavior: str = "ok",
    version: str = VERSION_OK,
    reference: str = REFERENCE,
    value: str = SENTINEL,
    rc: int = 4,
    stderr: str = "item not found",
    sleep: str = "30",
    pad: int = 0,
    items: list[dict[str, object]] | None = None,
    list_behavior: str = "ok",
    create_behavior: str = "ok",
    create_rc: int = 5,
    create_stderr: str = "conflict",
    read_value_override: str | None = None,
    vault_behavior: str = "ok",
    vaults: list[dict[str, object]] | None = None,
    store_items: list[dict[str, object]] | None = None,
) -> Path:
    """Write the fake `op` into ``tmp_path/bin``; return that directory.

    ``items`` seeds the ``op item list`` rows, ``store_items`` pre-seeds
    the fake's on-disk item store (what ``item create``/``item edit``
    write and ``read`` reads back), and the ``*_behavior`` knobs stage
    per-subcommand failures.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    config: dict[str, object] = {
        "behavior": behavior,
        "version": version,
        "reference": reference,
        "value": value,
        "rc": rc,
        "stderr": stderr,
        "sleep": sleep,
        "pad": pad,
        "items": items if items is not None else [],
        "list_behavior": list_behavior,
        "create_behavior": create_behavior,
        "create_rc": create_rc,
        "create_stderr": create_stderr,
        "vault_behavior": vault_behavior,
        "vaults": vaults
        if vaults is not None
        else [{"id": "vault-1", "name": "mai-dev"}],
    }
    if read_value_override is not None:
        config["read_value_override"] = read_value_override
    (bin_dir / "op-config.json").write_text(json.dumps(config), encoding="utf-8")
    if store_items is not None:
        (bin_dir / "op-store.json").write_text(
            json.dumps({"next_id": 100 + len(store_items), "items": store_items}),
            encoding="utf-8",
        )
    script = bin_dir / "op"
    script.write_text(FAKE_OP_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    return bin_dir


def _use_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    )


def _invocations(bin_dir: Path) -> list[dict[str, object]]:
    dump = bin_dir / "invocations.jsonl"
    if not dump.exists():
        return []
    return [
        json.loads(line)
        for line in dump.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _env_lane(
    monkeypatch: pytest.MonkeyPatch, bin_dir: Path, **kwargs: object
) -> OnePasswordResolver:
    """Default lane provider: token through the confined env variable."""
    _use_path(monkeypatch, bin_dir)
    monkeypatch.setenv(ENV_TOKEN_KEY, TOKEN_SENTINEL)
    monkeypatch.setenv("MAI_DECOY_AMBIENT", DECOY_SENTINEL)
    return OnePasswordResolver(**kwargs)  # type: ignore[arg-type]


def _ref_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bin_dir: Path,
    extra_env_text: str = "",
    **kwargs: object,
) -> OnePasswordResolver:
    """credential_ref lane provider: token through a real 0600 .env file."""
    _use_path(monkeypatch, bin_dir)
    monkeypatch.delenv(ENV_TOKEN_KEY, raising=False)
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        f"{ENV_TOKEN_REF}={TOKEN_SENTINEL}\n{extra_env_text}", encoding="utf-8"
    )
    env_file.chmod(0o600)
    return OnePasswordResolver(
        credential_ref=ENV_TOKEN_REF,
        resolver=EnvResolver(env_file),
        token_env=None,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Happy path: the documented supported interface
# ---------------------------------------------------------------------------


def test_resolve_returns_exact_value_through_documented_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    secret = provider.resolve(REFERENCE)
    assert isinstance(secret, object) and secret.value == SENTINEL
    # Raw bytes pass through unchanged: no newline stripping.
    assert provider.resolve(REFERENCE).value == SENTINEL


def test_version_at_floor_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, version=VERSION_FLOOR)
    provider = _env_lane(monkeypatch, bin_dir)
    assert provider.availability().available
    assert provider.resolve(REFERENCE).value == SENTINEL


def test_valid_reference_boundaries_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_ref = "op://" + "v" * 128 + "/" + "i" * 128 + "/" + "f" * 128
    bin_dir = _install_fake_op(tmp_path, reference=long_ref)
    provider = _env_lane(monkeypatch, bin_dir)
    assert provider.resolve(long_ref).value == SENTINEL


def test_token_via_credential_ref_env_file_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    # A wrong ambient token proves the .env lane is the one used.
    monkeypatch.setenv(ENV_TOKEN_KEY, "wrong-ambient-not-used")
    provider = _ref_lane(tmp_path, monkeypatch, bin_dir)
    assert provider.resolve(REFERENCE).value == SENTINEL
    (record,) = [x for x in _invocations(bin_dir) if x["argv"] == ["read", REFERENCE]]
    assert record["env_token"] == TOKEN_SENTINEL


def test_token_via_confined_env_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    assert provider.resolve(REFERENCE).value == SENTINEL
    (record,) = [x for x in _invocations(bin_dir) if x["argv"] == ["read", REFERENCE]]
    assert record["env_token"] == TOKEN_SENTINEL


# ---------------------------------------------------------------------------
# Negative controls: argv / environment / representation boundary
# ---------------------------------------------------------------------------


def test_token_never_reaches_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    provider.resolve(REFERENCE)
    for record in _invocations(bin_dir):
        argv = json.dumps(record["argv"])
        assert TOKEN_SENTINEL not in argv
        if record["argv"] == ["--version"]:
            # The availability probe never carries the token.
            assert record["env_token"] is None
            assert ENV_TOKEN_KEY not in record["env_keys"]
        else:
            assert record["env_token"] == TOKEN_SENTINEL  # confined env key only


def test_child_environment_is_scrubbed_to_documented_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    provider.resolve(REFERENCE)
    for record in _invocations(bin_dir):
        # The fake op is a Python script: its own interpreter startup may
        # add LC_CTYPE (PEP 538 C-locale coercion). That artifact belongs
        # to the fixture, not to the resolver's scrubbed dict.
        env_keys = set(record["env_keys"]) - {"LC_CTYPE"}
        if record["argv"] == ["--version"]:
            assert env_keys == {"PATH", "HOME"}  # probe: never any token
        else:
            assert env_keys == {"PATH", "HOME", ENV_TOKEN_KEY}
        assert DECOY_SENTINEL not in json.dumps(record)


def test_hostile_stderr_token_echo_is_scrubbed_env_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaking_stderr = f"boom {TOKEN_SENTINEL} leaked"
    bin_dir = _install_fake_op(tmp_path, behavior="stderr-leak", stderr=leaking_stderr)
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert "<redacted>" in raised.value.detail
    assert TOKEN_SENTINEL not in str(raised.value)


def test_hostile_stderr_token_echo_is_scrubbed_ref_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaking_stderr = f"boom {TOKEN_SENTINEL} leaked"
    bin_dir = _install_fake_op(tmp_path, behavior="stderr-leak", stderr=leaking_stderr)
    provider = _ref_lane(tmp_path, monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert "<redacted>" in raised.value.detail
    assert TOKEN_SENTINEL not in str(raised.value)


def test_resolved_secret_representation_is_redacted_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    secret = provider.resolve(REFERENCE)
    assert repr(secret) == "SecretValue(<redacted>)"
    assert str(secret) == "SecretValue(<redacted>)"
    assert SENTINEL not in f"interpolated {secret}"
    stream = io.StringIO()
    logging.StreamHandler(stream).emit(
        logging.LogRecord("t", logging.INFO, __file__, 1, "%s", (secret,), None)
    )
    assert SENTINEL not in stream.getvalue()


# ---------------------------------------------------------------------------
# Negative controls: bounded execution, fail-closed outputs
# ---------------------------------------------------------------------------


def test_timeout_kills_child_and_classifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, behavior="timeout", sleep="30")
    provider = _env_lane(monkeypatch, bin_dir, timeout=0.5)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_TIMEOUT
    assert SENTINEL not in str(raised.value)


def test_oversized_output_fails_closed_without_partial_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, behavior="overlarge", pad=MAX_OUTPUT_BYTES)
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OUTPUT_TOO_LARGE
    assert SENTINEL not in str(raised.value)


def test_empty_output_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, behavior="empty")
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert SENTINEL not in str(raised.value)


def test_non_utf8_output_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, behavior="bad-utf8")
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.INVALID_ENCODING


def test_op_nonzero_exit_is_classified_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path, behavior="fail", rc=4, stderr="vault not found"
    )
    # Decoy: a fallback scanner would happily "resolve" from this .env.
    provider = _ref_lane(
        tmp_path,
        monkeypatch,
        bin_dir,
        extra_env_text=f"MAI_FALLBACK={DECOY_SENTINEL}\n",
    )
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert "rc=4" in raised.value.detail
    assert DECOY_SENTINEL not in str(raised.value)


# ---------------------------------------------------------------------------
# Negative controls: classification without fallback (AC2)
# ---------------------------------------------------------------------------


def test_plain_name_gets_no_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    # The .env lane is configured and CARRIES the plain name with a decoy
    # value: the provider must refuse the shape, never fall back to it.
    provider = _ref_lane(
        tmp_path, monkeypatch, bin_dir, extra_env_text=f"PLAIN_NAME={DECOY_SENTINEL}\n"
    )
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve("PLAIN_NAME")
    assert raised.value.code is OnePasswordErrorCode.INVALID_REFERENCE
    assert DECOY_SENTINEL not in str(raised.value)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "PLAIN_NAME",
        "op://two/segments",
        "op://a/b/c/d",
        "op:///a/b",
        "op://-v/x/y",
        "op://a b/c/d",
        "op://a/b/c ",
        "op://%41/b/c",
        "op://a/b/c\n",
        "OP://a/b/c",
        "op://./b/c",
        "op://../b/c",
        "op://a/b/c?x=1",
        "op://a/b/c#f",
        b"op://a/b/c",
        None,
        ["op://a/b/c"],
    ],
)
def test_invalid_references_rejected_before_any_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: object
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(reference)  # type: ignore[arg-type]
    assert raised.value.code is OnePasswordErrorCode.INVALID_REFERENCE
    assert _invocations(bin_dir) == []  # validated before the child runs


def test_op_absent_is_classified_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty-bin"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    monkeypatch.setenv(ENV_TOKEN_KEY, TOKEN_SENTINEL)
    # Construction succeeds on hosts without the CLI (graceful optionality).
    provider = OnePasswordResolver()
    status = provider.availability()
    assert not status.available
    assert status.code is OnePasswordErrorCode.OP_NOT_FOUND
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_NOT_FOUND
    assert TOKEN_SENTINEL not in str(raised.value)


def test_version_below_floor_refuses_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, version=VERSION_BELOW)
    provider = _env_lane(monkeypatch, bin_dir)
    status = provider.availability()
    assert status.code is OnePasswordErrorCode.OP_UNSUPPORTED_VERSION
    assert VERSION_BELOW in status.detail and VERSION_FLOOR in status.detail
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.OP_UNSUPPORTED_VERSION
    # Classification happens before any secret read: only probes ran.
    assert all(x["argv"] != ["read", REFERENCE] for x in _invocations(bin_dir))


def test_token_absent_classified_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    _use_path(monkeypatch, bin_dir)
    monkeypatch.delenv(ENV_TOKEN_KEY, raising=False)
    provider = OnePasswordResolver()
    status = provider.availability()
    assert status.code is OnePasswordErrorCode.TOKEN_MISSING
    assert TOKEN_SENTINEL not in status.detail
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.TOKEN_MISSING
    assert all(x["argv"] != ["read", REFERENCE] for x in _invocations(bin_dir))


def test_empty_ambient_token_is_token_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    _use_path(monkeypatch, bin_dir)
    monkeypatch.setenv(ENV_TOKEN_KEY, "")
    provider = OnePasswordResolver()
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.TOKEN_MISSING


def test_env_file_token_missing_maps_to_token_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    _use_path(monkeypatch, bin_dir)
    monkeypatch.delenv(ENV_TOKEN_KEY, raising=False)
    env_file = tmp_path / "operator.env"
    env_file.write_text("MAI_UNRELATED=x\n", encoding="utf-8")
    env_file.chmod(0o600)
    provider = OnePasswordResolver(
        credential_ref=ENV_TOKEN_REF, resolver=EnvResolver(env_file), token_env=None
    )
    with pytest.raises(OnePasswordError) as raised:
        provider.resolve(REFERENCE)
    assert raised.value.code is OnePasswordErrorCode.TOKEN_MISSING
    assert ENV_TOKEN_REF in raised.value.detail  # the reference name, not a value


def test_unparsable_version_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, version="not-a-version")
    provider = _env_lane(monkeypatch, bin_dir)
    assert provider.availability().code is OnePasswordErrorCode.OP_FAILED


def test_invalid_token_lane_combinations_rejected() -> None:
    with pytest.raises(OnePasswordError) as both:
        OnePasswordResolver(
            credential_ref=ENV_TOKEN_REF, resolver=None, token_env=ENV_TOKEN_KEY
        )
    assert both.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION
    with pytest.raises(OnePasswordError) as no_resolver:
        OnePasswordResolver(credential_ref=ENV_TOKEN_REF, token_env=None)
    assert no_resolver.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION
    with pytest.raises(OnePasswordError) as bad_env:
        OnePasswordResolver(token_env="not a name")
    assert bad_env.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION
    with pytest.raises(OnePasswordError) as bad_binary:
        OnePasswordResolver(op_binary="  ")
    assert bad_binary.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION


@pytest.mark.parametrize("timeout", [0, -1, 0.0, 1000.0, "10", True])
def test_invalid_timeouts_rejected(timeout: object) -> None:
    with pytest.raises(OnePasswordError) as raised:
        OnePasswordResolver(timeout=timeout)  # type: ignore[arg-type]
    assert raised.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION


# ---------------------------------------------------------------------------
# Optional-ness: the plugin is additive, .env-only mode is untouched
# ---------------------------------------------------------------------------


def test_core_import_does_not_load_the_plugin() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mai, sys; print('mai.credentials.onepassword' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_env_module_is_unaffected_by_the_plugin() -> None:
    import mai.credentials.env as env_module

    assert "onepassword" not in Path(env_module.__file__).read_text(encoding="utf-8")


def test_resolver_repr_carries_names_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    rendered = repr(provider)
    assert "op='op'" in rendered and ENV_TOKEN_KEY in rendered
    assert TOKEN_SENTINEL not in rendered
    lane_provider = _ref_lane(tmp_path, monkeypatch, bin_dir)
    assert ENV_TOKEN_REF in repr(lane_provider)
    assert TOKEN_SENTINEL not in repr(lane_provider)


def test_availability_detail_is_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    status = provider.availability()
    assert status.available
    assert TOKEN_SENTINEL not in status.detail + repr(status)


# ---------------------------------------------------------------------------
# Collection + write surface: list_items / store_secret / probe
# ---------------------------------------------------------------------------


def _listing_items() -> list[dict[str, object]]:
    """Fixture rows for pagination/ordering proof (deliberately unsorted)."""
    return [
        {
            "id": "it-b",
            "title": "Bravo",
            "category": "LOGIN",
            "vault": "mai-dev",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            "id": "it-a",
            "title": "alpha",
            "category": "PASSWORD",
            "vault": "mai-dev",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "it-c",
            "title": "charlie",
            "category": "LOGIN",
            "vault": "other-vault",
            "updated_at": None,
        },
        {
            "id": "it-d",
            "title": "ALPHA-two",
            "category": "LOGIN",
            "vault": "mai-dev",
            "updated_at": "2026-01-03T00:00:00Z",
        },
    ]


def test_list_items_paginates_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, items=_listing_items())
    provider = _env_lane(monkeypatch, bin_dir)
    page = provider.list_items("mai-dev", limit=2, offset=0)
    assert isinstance(page, OnePasswordItemPage)
    # Deterministic (title.lower(), id) order across case variants; the
    # --vault filter bounds the result (it-c lives in other-vault).
    assert [item.id for item in page.items] == ["it-a", "it-d"]
    assert page.items[0].title == "alpha"
    assert page.items[1].title == "ALPHA-two"
    assert page.items[0].vault == "mai-dev"
    assert page.items[0].updated_at == "2026-01-01T00:00:00Z"
    assert (page.limit, page.offset, page.total, page.has_more) == (2, 0, 3, True)
    second = provider.list_items("mai-dev", limit=2, offset=2)
    assert [item.id for item in second.items] == ["it-b"]
    assert (second.total, second.has_more) == (3, False)
    beyond = provider.list_items("mai-dev", limit=2, offset=3)
    assert beyond.items == () and (beyond.total, beyond.has_more) == (3, False)


def test_list_items_filters_by_query_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, items=_listing_items())
    provider = _env_lane(monkeypatch, bin_dir)
    by_title = provider.list_items("mai-dev", query="AlPhA")
    assert [item.id for item in by_title.items] == ["it-a", "it-d"]
    assert by_title.total == 2
    by_id = provider.list_items("mai-dev", query="IT-B")
    assert [item.id for item in by_id.items] == ["it-b"]
    categorized = provider.list_items("mai-dev", category="login")
    assert [item.id for item in categorized.items] == ["it-d", "it-b"]
    records = [x for x in _invocations(bin_dir) if x["argv"][:2] == ["item", "list"]]
    assert len(records) == 3
    assert records[-1]["argv"] == [
        "item",
        "list",
        "--vault",
        "mai-dev",
        "--format",
        "json",
        "--categories",
        "login",
    ]


def test_list_items_rejects_invalid_arguments_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path, items=_listing_items())
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as bad_vault:
        provider.list_items("mai/dev")
    assert bad_vault.value.code is OnePasswordErrorCode.INVALID_REFERENCE
    for kwargs in (
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"offset": -1},
        {"category": "-v"},
        {"category": "a/b"},
    ):
        with pytest.raises(OnePasswordError) as raised:
            provider.list_items("mai-dev", **kwargs)  # type: ignore[arg-type]
        assert raised.value.code is OnePasswordErrorCode.INVALID_CONFIGURATION
    assert _invocations(bin_dir) == []  # validated before any child ran


def test_list_items_overlarge_output_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path, list_behavior="overlarge", pad=MAX_LIST_OUTPUT_BYTES
    )
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.list_items("mai-dev")
    assert raised.value.code is OnePasswordErrorCode.OUTPUT_TOO_LARGE
    assert SENTINEL not in str(raised.value)


def test_list_items_op_failure_is_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path, list_behavior="fail", rc=7, stderr="vault locked"
    )
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.list_items("mai-dev")
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert "rc=7" in raised.value.detail
    assert "vault locked" in raised.value.detail


def test_store_secret_explicit_value_rides_stdin_never_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    receipt = provider.store_secret(
        "mai-dev", title="deploy-key", field="password", value=SENTINEL
    )
    assert isinstance(receipt, StoredSecretReceipt)
    assert receipt.vault == "mai-dev"
    assert receipt.title == "deploy-key" and receipt.field == "password"
    assert receipt.reference == "op://mai-dev/deploy-key/password"
    assert receipt.item_id  # id from the op item create output
    assert receipt.value.value == SENTINEL  # read-back material
    assert receipt.sha256 is None  # L3: no digest oracle for chosen values
    assert receipt.generated is False
    records = _invocations(bin_dir)
    (create,) = [x for x in records if x["argv"][:2] == ["item", "create"]]
    template = json.loads(str(create["stdin"]))
    assert template["title"] == "deploy-key"
    assert template["category"] == "PASSWORD"
    assert template["fields"][0]["label"] == "password"
    assert template["fields"][0]["value"] == SENTINEL  # the secret rides stdin
    # Zero-argv rule: the value NEVER appears in any child argv.
    for record in records:
        assert SENTINEL not in json.dumps(record["argv"])
    # Read-back verification ran through op read on the resolved reference.
    (read,) = [
        x for x in records if x["argv"] == ["read", "op://mai-dev/deploy-key/password"]
    ]
    assert read["env_token"] == TOKEN_SENTINEL


def test_store_secret_generated_uses_flag_and_populates_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    receipt = provider.store_secret(
        "mai-dev", title="gen-key", generate=True, length=32
    )
    assert receipt.generated is True
    assert len(receipt.value.value) == 32
    assert (
        receipt.sha256
        == hashlib.sha256(receipt.value.value.encode("utf-8")).hexdigest()
    )
    records = _invocations(bin_dir)
    (create,) = [x for x in records if x["argv"][:2] == ["item", "create"]]
    assert "--generate-password=32,letters,digits,symbols" in create["argv"]
    # Generated material is known only after the read-back: never on argv.
    assert receipt.value.value not in json.dumps([x["argv"] for x in records])


def test_store_secret_updates_existing_item_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = [
        {
            "id": "it-042",
            "title": "deploy-key",
            "category": "PASSWORD",
            "vault": "mai-dev",
            "updated_at": "2025-01-01T00:00:00Z",
            "fields": {"password": "old-value-not-a-secret"},
        }
    ]
    bin_dir = _install_fake_op(tmp_path, store_items=seeded)
    provider = _env_lane(monkeypatch, bin_dir)
    receipt = provider.store_secret("mai-dev", title="deploy-key", value=SENTINEL)
    assert receipt.item_id == "it-042"  # updated in place, not duplicated
    records = _invocations(bin_dir)
    assert [x for x in records if x["argv"][:2] == ["item", "create"]] == []
    (edit,) = [x for x in records if x["argv"][:2] == ["item", "edit"]]
    assert edit["argv"][2] == "it-042"
    assert SENTINEL in str(edit["stdin"])
    assert SENTINEL not in json.dumps(edit["argv"])
    assert provider.resolve("op://mai-dev/deploy-key/password").value == SENTINEL


def test_store_secret_read_back_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path, read_value_override="decoy-mismatch-not-stored"
    )
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.store_secret("mai-dev", title="mismatch-key", value=SENTINEL)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert "read-back verification mismatch" in raised.value.detail
    assert SENTINEL not in str(raised.value)


def test_store_secret_scrubs_value_and_token_from_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path,
        create_behavior="fail",
        create_rc=9,
        create_stderr=f"conflict echo {SENTINEL} {TOKEN_SENTINEL}",
    )
    provider = _env_lane(monkeypatch, bin_dir)
    with pytest.raises(OnePasswordError) as raised:
        provider.store_secret("mai-dev", title="doomed-key", value=SENTINEL)
    assert raised.value.code is OnePasswordErrorCode.OP_FAILED
    assert raised.value.detail.count("<redacted>") >= 2
    assert SENTINEL not in str(raised.value)
    assert TOKEN_SENTINEL not in str(raised.value)


def test_store_secret_validation_errors_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    cases: list[dict[str, object]] = [
        {"vault": "mai/dev", "title": "t"},
        {"vault": "mai-dev", "title": "-t"},
        {"vault": "mai-dev", "title": "t", "field": "a/b"},
        {"vault": "mai-dev", "title": "t", "category": "bank-card"},
        {"vault": "mai-dev", "title": "t", "generate": True, "length": 15},
        {"vault": "mai-dev", "title": "t", "generate": True, "length": 129},
        {"vault": "mai-dev", "title": "t", "value": ""},
        {"vault": "mai-dev", "title": "t", "generate": True, "value": SENTINEL},
    ]
    for kwargs in cases:
        with pytest.raises(OnePasswordError) as raised:
            provider.store_secret(**kwargs)  # type: ignore[arg-type]
        assert raised.value.code in (
            OnePasswordErrorCode.INVALID_REFERENCE,
            OnePasswordErrorCode.INVALID_CONFIGURATION,
        )
    assert _invocations(bin_dir) == []  # nothing executed


def test_probe_ready_with_and_without_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    report = provider.probe("mai-dev")
    assert isinstance(report, OnePasswordProbeReport)
    assert report.available and report.code == "ready"
    assert report.checked_vault == "mai-dev"
    assert report.detail  # value-free availability detail
    assert TOKEN_SENTINEL not in report.detail
    listing = provider.probe()
    assert listing.available and listing.code == "ready"
    assert listing.checked_vault is None
    argvs = [x["argv"] for x in _invocations(bin_dir)]
    assert ["vault", "get", "mai-dev", "--format", "json"] in argvs
    assert ["vault", "list", "--format", "json"] in argvs


def test_probe_vault_failure_is_scrubbed_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(
        tmp_path, vault_behavior="fail", rc=6, stderr=f"vault locked {TOKEN_SENTINEL}"
    )
    provider = _env_lane(monkeypatch, bin_dir)
    report = provider.probe("mai-dev")
    assert not report.available
    assert report.code == "op_failed"
    assert report.checked_vault == "mai-dev"
    assert "<redacted>" in report.detail
    assert TOKEN_SENTINEL not in report.detail + repr(report)


def test_probe_unavailable_provider_reports_without_vault_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty-bin"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    monkeypatch.setenv(ENV_TOKEN_KEY, TOKEN_SENTINEL)
    provider = OnePasswordResolver()
    report = provider.probe("mai-dev")
    assert not report.available
    assert report.code == "op_not_found"
    assert report.checked_vault == "mai-dev"


def test_probe_invalid_vault_segment_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    provider = _env_lane(monkeypatch, bin_dir)
    report = provider.probe("mai/dev")
    assert not report.available
    assert report.code == "invalid_reference"
    assert report.checked_vault == "mai/dev"
    # Only the availability probe ran; no vault call was attempted.
    assert all(x["argv"] == ["--version"] for x in _invocations(bin_dir))


# ---------------------------------------------------------------------------
# Production entrypoint (real subprocess, value-free output)
# ---------------------------------------------------------------------------


def _run_cli(
    *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mai.credentials.onepassword", *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_check_unavailable_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty-bin"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    done = _run_cli("check", env=dict(os.environ))
    assert done.returncode == 1
    payload = json.loads(done.stdout)
    assert payload["kind"] == "unavailable"
    assert payload["code"] == "op_not_found"
    assert TOKEN_SENTINEL not in done.stdout + done.stderr


def test_cli_check_available_then_resolve_proves_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    env[ENV_TOKEN_KEY] = TOKEN_SENTINEL
    available = _run_cli("check", env=env)
    assert available.returncode == 0
    assert json.loads(available.stdout) == {
        "kind": "available",
        "code": "available",
        "detail": json.loads(available.stdout)["detail"],
    }
    resolved = _run_cli("resolve", REFERENCE, env=env)
    assert resolved.returncode == 0
    payload = json.loads(resolved.stdout)
    assert payload["kind"] == "resolved"
    assert payload["reference"] == REFERENCE
    assert payload["sha256"] == hashlib.sha256(SENTINEL.encode("utf-8")).hexdigest()
    assert SENTINEL not in resolved.stdout + resolved.stderr
    assert TOKEN_SENTINEL not in resolved.stdout + resolved.stderr


def test_cli_resolve_through_env_file_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_fake_op(tmp_path)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    monkeypatch.delenv(ENV_TOKEN_KEY, raising=False)
    env.pop(ENV_TOKEN_KEY, None)
    env_file = tmp_path / "operator.env"
    env_file.write_text(f"{ENV_TOKEN_REF}={TOKEN_SENTINEL}\n", encoding="utf-8")
    env_file.chmod(0o600)
    resolved = _run_cli(
        "resolve",
        REFERENCE,
        "--env-file",
        str(env_file),
        "--credential-ref",
        ENV_TOKEN_REF,
        env=env,
    )
    assert resolved.returncode == 0
    assert json.loads(resolved.stdout)["kind"] == "resolved"
    assert TOKEN_SENTINEL not in resolved.stdout + resolved.stderr


def test_cli_usage_errors_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    assert _run_cli("resolve", env=dict(os.environ)).returncode == 2
    assert _run_cli("frobnicate", env=dict(os.environ)).returncode == 2
    assert (
        _run_cli(
            "resolve", REFERENCE, "--env-file", "/tmp/x", env=dict(os.environ)
        ).returncode
        == 2
    )
