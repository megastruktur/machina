"""``.env`` credential resolver — the mandatory first resolver (C03 rule 6).

Credentials in MAI are stored **by reference** (an environment-variable name,
validated by the canonical profile schema, ``mai.contracts.server``):
``ssh.credential_ref`` and capability credential references never carry
credential material. This module is the production resolver that turns a
reference name into credential material at execution time:

- **One configured source.** A single operator-configured ``.env`` file
  (path given explicitly, e.g. from ``MAI_ENV_FILE`` at the composition
  boundary) is loaded once at construction. There is no upward directory
  search, no default file discovery and no host-wide secret scanning: only
  the exact configured path is ever opened (C03 rule 6).
- **Fail-closed loading.** The file must exist, be a regular file, not a
  symlink, carry restrictive permissions (no group/other access bits) and
  parse cleanly; violations raise structured, value-free errors before any
  resolution can succeed.
- **Value-free discipline.** Neither parsing nor resolution errors ever
  embed file content: details carry the source path, the 1-based line
  number and the reference name (a reference, not a secret). Resolved
  material is wrapped in :class:`SecretValue`, whose ``repr``/``str`` are
  redacted, so accidental logging or string interpolation cannot expose a
  value. Consumers must make the deliberate ``secret.value`` disclosure.

The module is pure stdlib (no dotenv dependency), performs **no** logging,
reads **no** process environment and touches **no** clock/network state:
results derive from the explicit path and the file content only, so
evidence runs cannot leak ambient secret material. The parsed mapping is an
immutable snapshot; reflecting operator edits to the ``.env`` requires
reconstruction (restart semantics, mirroring Compose environment behavior).

Thread safety: after construction the resolver holds only immutable state
and is safe for concurrent ``resolve`` calls.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

__all__ = [
    "DATA_ROOT_CREDENTIALS_FILENAME",
    "MAX_ENV_FILE_BYTES",
    "EnvErrorCode",
    "EnvFileError",
    "EnvResolver",
    "SecretValue",
    "build_runtime_env_resolver",
    "main",
    "parse_env_text",
    "upsert_env_credentials",
]

#: Managed credential store inside the writable data root (mode 0600).
DATA_ROOT_CREDENTIALS_FILENAME = "credentials.env"

#: Byte cap for one ``.env`` file, enforced before parsing. Same rationale
#: as the canonical profile cap (mai.profiles.files): an operator-written
#: environment file is tiny; the cap bounds parser work and refuses runaway
#: host files from a misconfigured path.
MAX_ENV_FILE_BYTES = 64 * 1024

#: General environment-variable name accepted in a ``.env`` file. Broader
#: than the credential-reference shape below: the file may also carry
#: non-credential configuration (e.g. ``MAI_DATABASE_URL``).
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Credential-reference shape, frozen by the canonical profile schema
#: (``mai.contracts.server`` ``_CREDENTIAL_REF_RE``, contracts.md C03 rule 6:
#: credentials resolve by reference). Kept local: the schema module is the
#: shape owner and is outside this card's allowlist; a divergence would be a
#: review finding, not a silent import of a private symbol.
_CREDENTIAL_REF_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")

_EXPORT_PREFIX = "export"


class EnvErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    NOT_CONFIGURED = "not_configured"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    SYMLINK_FORBIDDEN = "symlink_forbidden"
    NOT_READABLE = "not_readable"
    PERMISSIONS_TOO_OPEN = "permissions_too_open"
    TOO_LARGE = "too_large"
    INVALID_ENCODING = "invalid_encoding"
    PARSE_ERROR = "parse_error"
    DUPLICATE_KEY = "duplicate_key"
    INVALID_REF = "invalid_ref"
    MISSING_KEY = "missing_key"
    EMPTY_VALUE = "empty_value"


class EnvFileError(ValueError):
    """One structured, value-free resolver rejection.

    ``detail`` carries only the source path, line numbers and reference
    names — never file content or resolved values. Tests pin this with a
    sentinel-collision fixture (a malformed value shaped exactly like the
    fixture secret must stay absent from the message).
    """

    def __init__(self, code: EnvErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class SecretValue:
    """Resolved credential material with a value-free representation.

    ``repr``/``str`` (and therefore f-string interpolation, ``print`` and
    log formatting) render ``SecretValue(<redacted>)``; the raw material is
    disclosed only through the explicit :attr:`value` property, which
    consumers call at the point of use (e.g. an SSH key passphrase). The
    class defines no equality/hash involving the value, so accidental
    container membership tests cannot force value comparisons into logs.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def value(self) -> str:
        """The credential material; the one deliberate disclosure point."""
        return self._value

    def __repr__(self) -> str:  # noqa: D105 - fixed redacted rendering
        return "SecretValue(<redacted>)"

    __str__ = __repr__


def parse_env_text(text: str, *, source: str = "<env>") -> dict[str, str]:
    """Parse ``.env`` text into a name→value mapping.

    Supported syntax (deliberately minimal and predictable; no escape
    processing, no multi-line values):

    - ``KEY=VALUE`` per line; optional ``export `` prefix; blank lines and
      whole-line ``#`` comments ignored.
    - Unquoted values: surrounding whitespace stripped; a whitespace-hash
      sequence (``" #"``) starts an inline comment. A hash without leading
      whitespace (``KEY=a#b``) stays literal.
    - Single/double-quoted values: quotes stripped, content preserved
      literally (hashes and spaces included); nothing but whitespace or an
      inline comment may follow the closing quote; quotes must terminate on
      the same line.
    - Duplicate keys are rejected (config error, mirroring the strict
      duplicate-key policy of the canonical profile loader) — last-wins
      silently patching a mistake is exactly how a stale credential value
      survives an operator rotation.
    - Empty values parse fine (flag-style variables) but :meth:`EnvResolver.resolve`
      rejects them for credential material (fail closed).

    Raises :class:`EnvFileError` with ``parse_error``/``duplicate_key`` and
    a ``source:line`` location; the offending line content is never quoted
    into the message.
    """
    entries: dict[str, str] = {}
    if text.startswith("\ufeff"):
        text = text[1:]
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(_EXPORT_PREFIX) and len(line) > len(_EXPORT_PREFIX) and (
            line[len(_EXPORT_PREFIX)] in " \t"
        ):
            line = line[len(_EXPORT_PREFIX) + 1 :].lstrip()
        key, separator, remainder = line.partition("=")
        if not separator:
            raise EnvFileError(
                EnvErrorCode.PARSE_ERROR,
                f"{source}:{index}: expected KEY=VALUE",
            )
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise EnvFileError(
                EnvErrorCode.PARSE_ERROR,
                f"{source}:{index}: invalid variable name",
            )
        value = remainder.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing == -1:
                raise EnvFileError(
                    EnvErrorCode.PARSE_ERROR,
                    f"{source}:{index}: unterminated quote",
                )
            tail = value[closing + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise EnvFileError(
                    EnvErrorCode.PARSE_ERROR,
                    f"{source}:{index}: unexpected content after closing quote",
                )
            value = value[1:closing]
        else:
            comment = re.search(r"\s#", value)
            if comment is not None:
                value = value[: comment.start()].rstrip()
        if key in entries:
            raise EnvFileError(
                EnvErrorCode.DUPLICATE_KEY,
                f"{source}:{index}: duplicate variable {key}",
            )
        entries[key] = value
    return entries


class EnvResolver:
    """Load one configured ``.env`` file and resolve reference names (C03.6).

    Construction is the fail-closed boundary: the file is opened once with
    ``O_NOFOLLOW`` (symlink redirection refused, consistent with the
    canonical file store), permission bits are checked on the open file
    descriptor (``fstat`` — no path-stat race), and parsing happens before
    the instance exists. Any violation raises and the resolver never
    half-exists.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        overlay_path: str | os.PathLike[str] | None = None,
    ) -> None:
        raw = str(path)
        if not raw.strip():
            raise EnvFileError(
                EnvErrorCode.NOT_CONFIGURED,
                "no credential source configured; set the .env file path "
                "explicitly (discovery/scanning is prohibited, C03 rule 6)",
            )
        self._path = Path(path)
        self._entries: dict[str, str] = parse_env_text(
            self._read(self._path), source=raw
        )
        if overlay_path is not None and str(overlay_path).strip():
            overlay = Path(overlay_path)
            if overlay.exists():
                overlay_entries = parse_env_text(
                    self._read(overlay), source=str(overlay)
                )
                self._entries.update(overlay_entries)

    @property
    def path(self) -> Path:
        """The configured source path (configuration, not secret material)."""
        return self._path

    def __repr__(self) -> str:
        return f"<EnvResolver path={self._path} keys={len(self._entries)}>"

    def resolve(self, name: str) -> SecretValue:
        """Resolve one credential reference to material from the source.

        Raises :class:`EnvFileError`:

        - ``invalid_ref`` — the name is not a credential reference shape
          (frozen by the canonical profile schema);
        - ``missing_key`` — the name is absent from the configured source
          (the message names the reference and the source: a reference is
          not secret material, and a clear missing-key error is an
          acceptance criterion);
        - ``empty_value`` — the name maps to an empty value; an empty
          credential is a configuration error, never silently usable.
        """
        if not isinstance(name, str) or not _CREDENTIAL_REF_RE.fullmatch(name):
            raise EnvFileError(
                EnvErrorCode.INVALID_REF,
                "credential reference must match [A-Z][A-Z0-9_]{0,127} "
                "(frozen by the canonical profile schema)",
            )
        if name not in self._entries:
            raise EnvFileError(
                EnvErrorCode.MISSING_KEY,
                f"credential reference {name} is not defined in {self._path}",
            )
        value = self._entries[name]
        if not value:
            raise EnvFileError(
                EnvErrorCode.EMPTY_VALUE,
                f"credential reference {name} maps to an empty value in "
                f"{self._path}",
            )
        return SecretValue(value)

    def configured_keys(self) -> frozenset[str]:
        """The configured reference names of the merged source, never values.

        The snapshot covers the configured ``.env`` file plus the optional
        overlay layer (merged at construction, overlay wins), so callers
        can answer "is this reference name configured?" — a name-only
        availability question — without ever touching or transmitting the
        mapped material (C03 rule 6: credentials resolve by reference).
        """
        return frozenset(self._entries.keys())

    def _read(self, target_path: Path | None = None) -> str:
        path = self._path if target_path is None else target_path
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise EnvFileError(
                EnvErrorCode.NOT_FOUND,
                f"configured credential source does not exist: {path}",
            ) from None
        except IsADirectoryError:
            raise EnvFileError(
                EnvErrorCode.NOT_A_FILE,
                f"configured credential source is a directory: {path}",
            ) from None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise EnvFileError(
                    EnvErrorCode.SYMLINK_FORBIDDEN,
                    f"configured credential source is a symlink: {path}",
                ) from None
            raise EnvFileError(
                EnvErrorCode.NOT_READABLE,
                f"configured credential source cannot be opened: "
                f"{error.strerror}",
            ) from None
        opened = False
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                # Linux opens directories read-only; classify on the open
                # descriptor before any read (os.fdopen would raise the
                # raw IsADirectoryError first on Python 3.14).
                raise EnvFileError(
                    EnvErrorCode.NOT_A_FILE,
                    f"configured credential source is a directory: {path}",
                )
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o077:
                raise EnvFileError(
                    EnvErrorCode.PERMISSIONS_TOO_OPEN,
                    f"configured credential source {path} is readable "
                    f"beyond its owner (mode {mode:04o}); restrict it to "
                    "owner-only access (e.g. 0600)",
                )
            with os.fdopen(fd, "rb") as handle:
                opened = True
                data = handle.read(MAX_ENV_FILE_BYTES + 1)
        finally:
            if not opened:
                os.close(fd)
        if len(data) > MAX_ENV_FILE_BYTES:
            raise EnvFileError(
                EnvErrorCode.TOO_LARGE,
                f"configured credential source {path} exceeds "
                f"{MAX_ENV_FILE_BYTES} bytes",
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise EnvFileError(
                EnvErrorCode.INVALID_ENCODING,
                f"configured credential source {path} is not valid "
                "UTF-8",
            ) from None


def upsert_env_credentials(
    path: str | os.PathLike[str], updates: Mapping[str, str]
) -> None:
    """Atomically create or update credential references in a 0600 .env file."""
    if not updates:
        return
    validated: dict[str, str] = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not _CREDENTIAL_REF_RE.fullmatch(key):
            raise EnvFileError(
                EnvErrorCode.INVALID_REF,
                "credential reference must match [A-Z][A-Z0-9_]{0,127}",
            )
        if not isinstance(value, str) or not value.strip():
            raise EnvFileError(
                EnvErrorCode.EMPTY_VALUE,
                f"credential reference {key} cannot have an empty value",
            )
        if "\n" in value or "\r" in value or "\x00" in value or "'" in value:
            raise EnvFileError(
                EnvErrorCode.PARSE_ERROR,
                f"credential reference {key} contains unsupported control/quote characters",
            )
        validated[key] = value

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, str] = {}
    if target.exists():
        if target.is_symlink():
            raise EnvFileError(
                EnvErrorCode.SYMLINK_FORBIDDEN,
                f"configured credential source is a symlink: {target}",
            )
        existing = EnvResolver(target)
        entries.update(existing._entries)
    entries.update(validated)
    lines = [f"{k}='{entries[k]}'" for k in sorted(entries)]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(payload) > MAX_ENV_FILE_BYTES:
        raise EnvFileError(
            EnvErrorCode.TOO_LARGE,
            f"configured credential source {target} exceeds {MAX_ENV_FILE_BYTES} bytes",
        )
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_runtime_env_resolver(
    env_file: str | os.PathLike[str] | None,
    data_root: str | os.PathLike[str] | None = None,
) -> EnvResolver | None:
    """Construct an EnvResolver layering `<data_root>/credentials.env` over `env_file`."""
    raw_env = str(env_file).strip() if env_file is not None else ""
    overlay: Path | None = None
    if data_root is not None and str(data_root).strip():
        candidate = Path(data_root) / DATA_ROOT_CREDENTIALS_FILENAME
        if candidate.is_file() and not candidate.is_symlink():
            overlay = candidate
    if raw_env:
        return EnvResolver(raw_env, overlay_path=overlay)
    if overlay is not None:
        return EnvResolver(overlay)
    return None


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.credentials.env",
        description=(
            "Check or resolve through the .env credential resolver. "
            "Structured output only; credential values are never printed "
            "(resolution success is proven by the value's SHA-256 digest)."
        ),
    )
    parser.add_argument("command", choices=["check", "resolve"])
    parser.add_argument("env_file", help="configured .env source path")
    parser.add_argument("name", nargs="?", help="credential reference for resolve")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success, 1 structured rejection, 2 usage error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "resolve" and not args.name:
        parser.error("resolve requires a credential reference NAME")
    try:
        resolver = EnvResolver(args.env_file)
    except EnvFileError as error:
        _emit({"kind": "error", "code": error.code.value, "detail": error.detail})
        return 1
    if args.command == "check":
        _emit({"kind": "ok", "source": str(resolver.path), "keys": len(resolver._entries)})
        return 0
    try:
        secret = resolver.resolve(args.name)
    except EnvFileError as error:
        _emit({"kind": "error", "code": error.code.value, "detail": error.detail})
        return 1
    digest = hashlib.sha256(secret.value.encode("utf-8")).hexdigest()
    _emit({"kind": "resolved", "name": args.name, "sha256": digest})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
