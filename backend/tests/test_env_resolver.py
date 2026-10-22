"""Focused tests for the ``.env`` credential resolver.

Cards/contracts: ``03-credentials`` against contracts.md **C03 rule 6**
(``.env`` is the mandatory first resolver; names resolve only from the
configured source; host-wide secret scanning is prohibited) and the C01
envelope vocabulary. The reference-name shape is frozen by the canonical
profile schema (``mai.contracts.server``, ``ssh.credential_ref``).

Acceptance coverage:

- AC1: configured ``.env`` is the sole source with reference lookup,
  restrictive-permission enforcement and clear missing-key errors;
- AC2: no arbitrary host-file scanning (decoy files in parent directories
  and the ambient process environment are never consulted) and no
  representation/logging path can expose a value (redacted
  ``SecretValue`` representation, value-free error details, structured
  CLI output proven value-free in a real subprocess).

Secret hygiene: every "credential" below is the synthetic sentinel
``...not-a-secret`` marker, never a real secret. The sentinel is designed
to collide with every rendering path (values, malformed lines, error
messages, subprocess stdout/stderr) so the absence assertions are
meaningful non-disclosure controls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from mai.credentials.env import (
    MAX_ENV_FILE_BYTES,
    EnvErrorCode,
    EnvFileError,
    EnvResolver,
    SecretValue,
    build_runtime_env_resolver,
    parse_env_text,
)

#: Synthetic non-disclosure marker (NOT a secret): every test that could
#: surface a value uses THIS string as the value, so absence assertions
#: prove the renderer never emitted it.
SENTINEL = "mai-03-credentials-sentinel-not-a-secret"
#: Marker for files a scanning resolver would wrongly discover.
DECOY_SENTINEL = "decoy-mai-03-credentials-parent-file-never-read"


def _write_env(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


# ---------------------------------------------------------------------------
# Parsing (pure function)
# ---------------------------------------------------------------------------


def test_parse_basic_key_value_and_blank_comment_lines() -> None:
    assert parse_env_text("A=b\n\n# comment\n   # indented comment\n") == {"A": "b"}


def test_parse_export_prefix_stripped() -> None:
    assert parse_env_text("export A=b\nexport\tC=d\nE=f\n") == {
        "A": "b",
        "C": "d",
        "E": "f",
    }


def test_parse_quotes_preserve_hash_spaces_and_equals() -> None:
    text = 'A="x # y"\nB=\'a"b\'\nC=" leading and trailing "\nD=e=f=g\n'
    parsed = parse_env_text(text)
    assert parsed == {
        "A": "x # y",
        "B": 'a"b',
        "C": " leading and trailing ",
        "D": "e=f=g",
    }


def test_parse_inline_comment_only_after_whitespace_hash() -> None:
    assert parse_env_text("A=v # comment\nB=v#not-comment\n") == {
        "A": "v",
        "B": "v#not-comment",
    }


def test_parse_empty_value_is_legal_in_the_mapping() -> None:
    assert parse_env_text("EMPTY=\n") == {"EMPTY": ""}


def test_parse_handles_crlf_and_utf8_bom() -> None:
    assert parse_env_text("\ufeffA=b\r\nC=d\r\n") == {"A": "b", "C": "d"}


def test_parse_error_reports_line_number_without_content() -> None:
    with pytest.raises(EnvFileError) as raised:
        parse_env_text(f"A=b\nnot a mapping line value={SENTINEL}\n")
    assert raised.value.code is EnvErrorCode.PARSE_ERROR
    assert ":2:" in raised.value.detail
    assert SENTINEL not in str(raised.value)


@pytest.mark.parametrize(
    "line",
    [
        "=value",
        "1BAD=value",
        "has space=value",
        "KEY",
    ],
)
def test_parse_rejects_invalid_lines(line: str) -> None:
    with pytest.raises(EnvFileError) as raised:
        parse_env_text(line)
    assert raised.value.code is EnvErrorCode.PARSE_ERROR


def test_parse_rejects_duplicate_keys_with_line_number() -> None:
    with pytest.raises(EnvFileError) as raised:
        parse_env_text("A=first\nA=second\n")
    assert raised.value.code is EnvErrorCode.DUPLICATE_KEY
    assert ":2:" in raised.value.detail


def test_parse_rejects_unterminated_and_trailing_quote_content() -> None:
    with pytest.raises(EnvFileError) as unterminated:
        parse_env_text('A="no close\n')
    assert unterminated.value.code is EnvErrorCode.PARSE_ERROR
    with pytest.raises(EnvFileError) as trailing:
        parse_env_text('A="v" junk\n')
    assert trailing.value.code is EnvErrorCode.PARSE_ERROR


# ---------------------------------------------------------------------------
# Loading boundary: existence, type, symlink, permissions, size, encoding
# ---------------------------------------------------------------------------


def test_load_owner_only_file_resolves(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_KEY={SENTINEL}\n")
    resolver = EnvResolver(env_file)
    assert resolver.resolve("MAI_KEY").value == SENTINEL


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_load_rejects_group_or_other_access_bits(tmp_path: Path, mode: int) -> None:
    env_file = _write_env(tmp_path / "open.env", f"MAI_KEY={SENTINEL}\n", mode=mode)
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file)
    assert raised.value.code is EnvErrorCode.PERMISSIONS_TOO_OPEN
    assert f"mode {mode:04o}" in raised.value.detail
    assert SENTINEL not in str(raised.value)


def test_load_accepts_read_only_owner_file(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "ro.env", "MAI_KEY=v\n", mode=0o400)
    assert EnvResolver(env_file).resolve("MAI_KEY").value == "v"


def test_load_missing_file_is_structured_not_found(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(tmp_path / "absent.env")
    assert raised.value.code is EnvErrorCode.NOT_FOUND


def test_load_directory_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(tmp_path)
    assert raised.value.code is EnvErrorCode.NOT_A_FILE


def test_load_rejects_symlink_even_to_owner_only_file(tmp_path: Path) -> None:
    target = _write_env(tmp_path / "target.env", f"MAI_KEY={SENTINEL}\n")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(link)
    assert raised.value.code is EnvErrorCode.SYMLINK_FORBIDDEN
    assert SENTINEL not in str(raised.value)


def test_load_rejects_dangling_symlink(tmp_path: Path) -> None:
    link = tmp_path / "dangling.env"
    link.symlink_to(tmp_path / "never-existed.env")
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(link)
    assert raised.value.code is EnvErrorCode.SYMLINK_FORBIDDEN


def test_load_rejects_oversized_file(tmp_path: Path) -> None:
    padding = "x" * 128
    lines = "".join(f"KEY_{index:05d}={padding}\n" for index in range(600))
    assert len(lines.encode()) > MAX_ENV_FILE_BYTES
    env_file = _write_env(tmp_path / "big.env", lines)
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file)
    assert raised.value.code is EnvErrorCode.TOO_LARGE


def test_load_rejects_invalid_utf8(tmp_path: Path) -> None:
    env_file = tmp_path / "binary.env"
    env_file.write_bytes(b"KEY=\xff\xfe\xff\n")
    os.chmod(env_file, 0o600)
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file)
    assert raised.value.code is EnvErrorCode.INVALID_ENCODING


def test_load_empty_path_is_not_configured() -> None:
    with pytest.raises(EnvFileError) as raised:
        EnvResolver("  ")
    assert raised.value.code is EnvErrorCode.NOT_CONFIGURED


# ---------------------------------------------------------------------------
# Resolution semantics
# ---------------------------------------------------------------------------


def test_resolve_returns_secret_with_exact_value(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_KEY={SENTINEL}\n")
    secret = EnvResolver(env_file).resolve("MAI_KEY")
    assert isinstance(secret, SecretValue)
    assert secret.value == SENTINEL
    assert (
        hashlib.sha256(secret.value.encode()).hexdigest()
        == hashlib.sha256(SENTINEL.encode()).hexdigest()
    )


def test_resolve_missing_key_names_reference_never_other_values(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_OTHER_KEY={SENTINEL}\n")
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file).resolve("MAI_MISSING_KEY")
    assert raised.value.code is EnvErrorCode.MISSING_KEY
    assert "MAI_MISSING_KEY" in raised.value.detail
    assert SENTINEL not in str(raised.value)


@pytest.mark.parametrize("name", ["lower_case", "", "1LEADING", "HAS SPACE", "A" * 129])
def test_resolve_rejects_non_reference_shapes(tmp_path: Path, name: str) -> None:
    env_file = _write_env(tmp_path / "real.env", "MAI_KEY=v\n")
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file).resolve(name)
    assert raised.value.code is EnvErrorCode.INVALID_REF


def test_resolve_accepts_maximum_length_reference(tmp_path: Path) -> None:
    name = "A" * 128
    env_file = _write_env(tmp_path / "real.env", f"{name}={SENTINEL}\n")
    assert EnvResolver(env_file).resolve(name).value == SENTINEL


def test_resolve_rejects_empty_value_fail_closed(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_OTHER={SENTINEL}\nEMPTY=\n")
    with pytest.raises(EnvFileError) as raised:
        EnvResolver(env_file).resolve("EMPTY")
    assert raised.value.code is EnvErrorCode.EMPTY_VALUE
    assert SENTINEL not in str(raised.value)


# ---------------------------------------------------------------------------
# AC2: no host scanning; the configured source is the only source
# ---------------------------------------------------------------------------


def test_no_upward_search_no_cwd_default_no_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    _write_env(
        outer / ".env",
        f"MAI_DECOY={DECOY_SENTINEL}\nMAI_KEY={DECOY_SENTINEL}\n",
    )
    _write_env(inner / "real.env", f"MAI_KEY={SENTINEL}\n")
    monkeypatch.chdir(inner)
    monkeypatch.setenv("MAI_AMBIENT", SENTINEL)
    resolver = EnvResolver("real.env")
    assert resolver.resolve("MAI_KEY").value == SENTINEL
    for missing in ("MAI_DECOY", "MAI_AMBIENT"):
        with pytest.raises(EnvFileError) as raised:
            resolver.resolve(missing)
        assert raised.value.code is EnvErrorCode.MISSING_KEY
        assert DECOY_SENTINEL not in str(raised.value)
        assert SENTINEL not in str(raised.value)


def test_only_the_configured_file_is_read(tmp_path: Path) -> None:
    _write_env(tmp_path / "neighbor.env", f"MAI_NEIGHBOR={DECOY_SENTINEL}\n")
    resolver = EnvResolver(_write_env(tmp_path / "real.env", "MAI_KEY=v\n"))
    with pytest.raises(EnvFileError) as raised:
        resolver.resolve("MAI_NEIGHBOR")
    assert DECOY_SENTINEL not in str(raised.value)


# ---------------------------------------------------------------------------
# AC2: representation and logging cannot expose values
# ---------------------------------------------------------------------------


def test_secret_representation_is_redacted() -> None:
    secret = SecretValue(SENTINEL)
    assert SENTINEL not in repr(secret)
    assert SENTINEL not in str(secret)
    assert SENTINEL not in f"interpolated={secret}"
    assert repr(secret) == "SecretValue(<redacted>)"


def test_logging_a_secret_never_emits_the_value(tmp_path: Path) -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("mai.credentials.env.test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    secret = EnvResolver(
        _write_env(tmp_path / "real.env", f"MAI_KEY={SENTINEL}\n")
    ).resolve("MAI_KEY")
    try:
        logger.debug("resolved %r", secret)
        logger.info("resolved %s", secret)
        logger.warning("resolved %s", secret.value)  # deliberate disclosure path
    finally:
        logger.removeHandler(handler)
    emitted = stream.getvalue()
    assert "SecretValue(<redacted>)" in emitted
    # Only the explicit .value disclosure contains the material; the value
    # passed via logging infrastructure (repr/str paths) stays redacted.
    assert emitted.count(SENTINEL) == 1


def test_every_error_code_is_value_free(tmp_path: Path) -> None:
    target = _write_env(tmp_path / "target.env", f"MAI_KEY={SENTINEL}\n")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    binary = tmp_path / "binary.env"
    binary.write_bytes(b"KEY=\xff\n")
    os.chmod(binary, 0o600)
    oversized = _write_env(
        tmp_path / "big.env", ("K=v\n" * (MAX_ENV_FILE_BYTES // 3 + 8))
    )
    cases: list[tuple[EnvErrorCode, EnvFileError]] = []
    for resolver_error in [
        lambda: EnvResolver(tmp_path / "absent.env"),
        lambda: EnvResolver(tmp_path),
        lambda: EnvResolver(link),
        lambda: EnvResolver(_write_env(tmp_path / "open.env", f"K={SENTINEL}\n", mode=0o644)),
        lambda: EnvResolver(oversized),
        lambda: EnvResolver(binary),
        lambda: EnvResolver(_write_env(tmp_path / "bad.env", f"junk line {SENTINEL}\n")),
        lambda: EnvResolver(_write_env(tmp_path / "dup.env", f"K={SENTINEL}\nK=again\n")),
    ]:
        with pytest.raises(EnvFileError) as raised:
            resolver_error()
        cases.append((raised.value.code, raised.value))
    loaded = EnvResolver(target)
    with pytest.raises(EnvFileError) as missing:
        loaded.resolve("MAI_MISSING")
    cases.append((EnvErrorCode.MISSING_KEY, missing.value))
    with pytest.raises(EnvFileError) as shape:
        loaded.resolve("not-a-ref")
    cases.append((EnvErrorCode.INVALID_REF, shape.value))
    empty_file = _write_env(tmp_path / "empty.env", "MAI_EMPTY=\n")
    with pytest.raises(EnvFileError) as empty:
        EnvResolver(empty_file).resolve("MAI_EMPTY")
    cases.append((EnvErrorCode.EMPTY_VALUE, empty.value))
    expected = {
        EnvErrorCode.NOT_FOUND,
        EnvErrorCode.NOT_A_FILE,
        EnvErrorCode.SYMLINK_FORBIDDEN,
        EnvErrorCode.PERMISSIONS_TOO_OPEN,
        EnvErrorCode.TOO_LARGE,
        EnvErrorCode.INVALID_ENCODING,
        EnvErrorCode.PARSE_ERROR,
        EnvErrorCode.DUPLICATE_KEY,
        EnvErrorCode.MISSING_KEY,
        EnvErrorCode.INVALID_REF,
        EnvErrorCode.EMPTY_VALUE,
    }
    assert {code for code, _ in cases} == expected
    for _, error in cases:
        assert SENTINEL not in str(error)
        assert DECOY_SENTINEL not in str(error)


def test_resolver_repr_carries_only_path_and_count(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_KEY={SENTINEL}\n")
    rendered = repr(EnvResolver(env_file))
    assert SENTINEL not in rendered
    assert "real.env" in rendered


# ---------------------------------------------------------------------------
# configured_keys: reference-name availability, never values
# ---------------------------------------------------------------------------


def test_configured_keys_reports_names_never_values(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "real.env", f"MAI_KEY={SENTINEL}\nMAI_OTHER=v\n"
    )
    keys = EnvResolver(env_file).configured_keys()
    assert isinstance(keys, frozenset)
    assert keys == frozenset({"MAI_KEY", "MAI_OTHER"})
    assert SENTINEL not in repr(keys)
    assert SENTINEL not in str(keys)


def test_configured_keys_covers_the_overlay_layer(tmp_path: Path) -> None:
    base = _write_env(tmp_path / "base.env", "MAI_BASE=v\n")
    overlay = _write_env(tmp_path / "overlay.env", f"MAI_OVERLAY={SENTINEL}\n")
    resolver = EnvResolver(base, overlay_path=overlay)
    assert resolver.configured_keys() == frozenset(
        {"MAI_BASE", "MAI_OVERLAY"}
    )


def test_configured_keys_on_an_empty_source_is_empty(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "empty.env", "")
    assert EnvResolver(env_file).configured_keys() == frozenset()


def test_build_runtime_env_resolver_configured_keys_use_data_root_store(
    tmp_path: Path,
) -> None:
    """The data-root credentials.env overlay is the runtime key source."""
    _write_env(
        tmp_path / "credentials.env", f"MAI_RUNTIME_KEY={SENTINEL}\n"
    )
    resolver = build_runtime_env_resolver(None, tmp_path)
    assert resolver is not None
    assert resolver.configured_keys() == frozenset({"MAI_RUNTIME_KEY"})
    # No configured source and no store: no resolver, no keys.
    assert build_runtime_env_resolver(None, tmp_path / "absent-root") is None


# ---------------------------------------------------------------------------
# AC2 boundary: the production CLI is value-free in a real subprocess
# ---------------------------------------------------------------------------


def _run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mai.credentials.env", *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_check_reports_structured_ok(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "real.env", f"MAI_KEY={SENTINEL}\nMAI_OTHER=v\n"
    )
    done = _run_cli("check", str(env_file))
    assert done.returncode == 0
    payload = json.loads(done.stdout.strip())
    assert payload == {"kind": "ok", "source": str(env_file), "keys": 2}


def test_cli_resolve_proves_success_by_digest_never_by_value(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_KEY={SENTINEL}\n")
    done = _run_cli("resolve", str(env_file), "MAI_KEY")
    assert done.returncode == 0
    payload = json.loads(done.stdout.strip())
    assert payload["kind"] == "resolved"
    assert payload["name"] == "MAI_KEY"
    assert payload["sha256"] == hashlib.sha256(SENTINEL.encode()).hexdigest()
    assert SENTINEL not in done.stdout + done.stderr


def test_cli_error_is_structured_and_value_free(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", f"MAI_OTHER={SENTINEL}\n")
    done = _run_cli("resolve", str(env_file), "MAI_MISSING")
    assert done.returncode == 1
    payload = json.loads(done.stdout.strip())
    assert payload["kind"] == "error"
    assert payload["code"] == "missing_key"
    assert SENTINEL not in done.stdout + done.stderr


def test_cli_permissions_rejection_is_value_free(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "open.env", f"MAI_KEY={SENTINEL}\n", mode=0o666)
    done = _run_cli("check", str(env_file))
    assert done.returncode == 1
    payload = json.loads(done.stdout.strip())
    assert payload["code"] == "permissions_too_open"
    assert SENTINEL not in done.stdout + done.stderr


def test_cli_usage_error_exits_two(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "real.env", "MAI_KEY=v\n")
    assert _run_cli("resolve", str(env_file)).returncode == 2
    assert _run_cli("frobnicate", str(env_file)).returncode == 2
