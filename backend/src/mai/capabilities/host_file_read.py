"""The bounded host file reader: paged UTF-8 text over the pinned lane.

Capability ``host.file_read`` — a read-only companion to ``host.logs``
for DevOps evidence gathering (config files, unit drops, small state
files). Like every capability it is reachable only through the common
dispatcher (:class:`~mai.capabilities.dispatch.CapabilityDispatcher`),
which runs the grant/adoption/policy gates before this executor exists
in the call graph; ``effect_class="read"`` needs no approval decision,
every other gate still binds.

Request shape
-------------

The payload is strictly bounded before any transport: ``path`` (one
absolute target-side UTF-8 text file), ``offset`` (0-based line offset,
default 0) and ``limit`` (default 100, at most 200). The target SERVER
is never a payload field — it comes from the execution context.

Read scope: each enrolled server's profile carries an OPTIONAL
``file_read_roots`` list of absolute normalized target-side directory
prefixes (see :mod:`mai.contracts.server`). An empty or missing list
denies EVERY read (fail closed, ``path_denied``). The target must be
absolute and contained in an allowed root, and a hardcoded
deny-by-default list applies AFTER the allowlist check to keep the
reader away from credential material even on a misconfigured host:

- path prefixes: ``/etc/shadow``, ``/root/.ssh`` (the path itself or
  anything below it);
- basenames: exactly ``.env``; names starting ``id_`` (SSH private
  keys); names ending ``.pem``.

Effect transport
----------------

One fixed-argv remote helper per call through the frozen bounded
synchronous lane (:func:`~mai.execution.commands.execute_command`) over
the pinned-identity SSH transport — the same lane ``host.logs`` uses::

    python3 -c <fixed program> -- <path> <roots JSON> <offset> <limit> <budget>

Structured argv only (never a shell string): the lane quotes every
element element-wise (``shlex.quote``), so the target default shell
reconstructs identical argv without interpreting metacharacters. The
helper is Python stdlib only and performs the target-side safety
checks in ONE remote roundtrip:

- fd-pinned traversal (no TOCTOU): the walk opens every intermediate
  component ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` relative to the
  previous dirfd and the final component ``O_NOFOLLOW`` relative to the
  last pinned dirfd; the file is ``fstat``-checked (regular file) and
  read ONLY through that fd, so a concurrent symlink or directory swap
  can only fail the walk (ELOOP → ``path_denied``), never redirect it.
  Path components are lexically normalized before the walk, and the
  allowlist containment verdict (equal to a root or below
  ``root + "/"`` — sibling-prefix escapes such as ``/srv/datax`` under
  root ``/srv/data`` are refused) applies to exactly that normalized
  path;
- the page is read line-wise from the pinned fd: at most ``limit``
  lines and at most ``MAX_CONTENT_BYTES`` retained bytes are returned;
  ``total`` counts ALL lines of the file (the remainder is streamed and
  discarded, never retained), so ``has_more`` is computed exactly
  controller-side. A line that alone exceeds the byte budget is never
  truncated: when it is the page's FIRST candidate it is skipped and
  explicitly labeled (``oversized_line``, with ``next_offset`` moved
  past that single line) so the caller can progress; later oversized
  lines simply end the page (``has_more`` stays true);
- retained bytes are strictly UTF-8-decoded; invalid UTF-8 is a
  structured ``decode_error`` rejection, never replacement text.

There is no write, no sudo, no shell interpolation, no file search and
no credential discovery anywhere in this capability.

Failure honesty
---------------

Pre-effect rejections (payload shape, empty/outside allowlist, deny
list, missing/mismatched profile, unresolved SSH key, unenrolled host
key) raise :class:`FileReadError` BEFORE any transport attempt. Remote
rejections come back as one structured JSON line
(``{"error": code}``, exit 3) and map onto the same stable vocabulary;
transport failures and unreadable helper responses fail with the
honest outcome status — content is never fabricated.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.contracts.server import (
    ExecutionEnvironment,
    ServerProfile,
    SshIdentity,
)
from mai.credentials.env import SecretValue
from mai.execution.commands import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandOutcome,
    CommandRequest,
    CommandStatus,
    execute_command,
)
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "DISPLAY_LABEL",
    "MAX_CONTENT_BYTES",
    "MAX_LIMIT",
    "MAX_OFFSET",
    "MAX_PATH_CHARS",
    "FileReadError",
    "FileReadErrorCode",
    "FileReadQuery",
    "HostFileReadCapability",
    "compose_read_argv",
    "descriptor",
    "read_query_from_payload",
]

#: The capability slug this module owns (registry identifier shape).
#: Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "host.file_read"

#: Safe one-line label carried by every request/outcome.
DISPLAY_LABEL = "host.file_read"

#: Request defaults and caps (payload values override; all bounded).
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OFFSET = 1_000_000_000
MAX_PATH_CHARS = 4096

#: Retained-page byte budget. The serialized JSON document stays below
#: the dispatcher's 64 KiB output budget even in the worst case (JSON
#: ``\\uXXXX`` escaping expands UTF-8 at most 3x).
MAX_CONTENT_BYTES = 20 * 1024

#: Transport retention budget (per stream; the page budget above is the
#: binding cap — this only keeps honest stdout/stderr byte totals).
TRANSPORT_OUTPUT_BUDGET_BYTES = 64 * 1024

#: Payload vocabulary (frozen by this descriptor's input schema).
_PAYLOAD_FIELDS = frozenset({"path", "offset", "limit"})

#: Hardcoded deny-by-default rules, applied AFTER the allowlist check
#: (see module docstring). Path prefixes deny the path itself and
#: anything below it; basename rules deny the final component wherever
#: it appears under an allowed root.
_DENY_PATH_PREFIXES = ("/etc/shadow", "/root/.ssh")
_DENY_BASENAMES = (".env",)
_DENY_BASENAME_PREFIXES = ("id_",)
_DENY_BASENAME_SUFFIXES = (".pem",)

#: Remote helper rejection codes accepted from the helper's structured
#: JSON line (a subset of :class:`FileReadErrorCode`; anything else is
#: an unreadable helper response and fails honestly).
_REMOTE_REJECTION_CODES = frozenset(
    {"path_denied", "path_not_found", "not_regular_file", "decode_error"}
)


class FileReadErrorCode(StrEnum):
    """Stable machine-readable pre-effect rejection codes (C01 style)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CONTEXT = "invalid_context"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_MISMATCH = "profile_mismatch"
    ENVIRONMENT_UNSUPPORTED = "environment_unsupported"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    KEY_FILE_UNAVAILABLE = "key_file_unavailable"
    HOST_KEY_UNENROLLED = "host_key_unenrolled"
    PATH_DENIED = "path_denied"
    PATH_NOT_FOUND = "path_not_found"
    NOT_REGULAR_FILE = "not_regular_file"
    DECODE_ERROR = "decode_error"


class FileReadError(ValueError):
    """One structured, value-free pre-effect rejection.

    Raised only before any transport attempt; the dispatcher's seam
    records it as ``executor_failed`` with ``effect="unknown"`` and
    quotes the exception *type* name only.
    """

    def __init__(self, code: FileReadErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: FileReadErrorCode, detail: str) -> FileReadError:
    return FileReadError(code, detail)


@dataclass(frozen=True, slots=True)
class FileReadQuery:
    """One validated, fully bounded file-read request (pure value)."""

    path: str
    offset: int
    limit: int
    file_read_roots: tuple[str, ...]


def _normalized_roots(roots: Sequence[object]) -> tuple[str, ...]:
    """Defensive normalization of validated allowlist roots."""
    return tuple(posixpath.normpath(str(root)) for root in roots)


def _allowed_target(normalized: str, roots: Sequence[str]) -> bool:
    """Exact-prefix containment: equal to a root or below ``root + /``."""
    return any(
        normalized == root or normalized.startswith(root + "/")
        for root in _normalized_roots(roots)
    )


def _denied_target(normalized: str) -> bool:
    """The hardcoded deny-by-default rules (after the allowlist check)."""
    for prefix in _DENY_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    name = posixpath.basename(normalized)
    return (
        name in _DENY_BASENAMES
        or name.startswith(_DENY_BASENAME_PREFIXES)
        or name.endswith(_DENY_BASENAME_SUFFIXES)
    )


def read_query_from_payload(
    payload: object,
    *,
    file_read_roots: Sequence[str],
    default_limit: int = DEFAULT_LIMIT,
) -> FileReadQuery:
    """Validate one operation payload into a bounded query (pure).

    Every rejection happens here, BEFORE any transport attempt. The
    allowlist check runs first (empty list denies everything), then the
    hardcoded deny list on the normalized target.

    Raises:
        FileReadError: ``invalid_request`` for shape violations,
            ``path_denied`` for every read-scope refusal.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            FileReadErrorCode.INVALID_REQUEST, "payload must be a JSON object"
        )
    unknown = sorted(str(key) for key in payload if key not in _PAYLOAD_FIELDS)
    if unknown:
        raise _reject(
            FileReadErrorCode.INVALID_REQUEST,
            f"unknown payload field {unknown[0]!r}",
        )
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > MAX_PATH_CHARS
        or not path.startswith("/")
        or not path.isprintable()
        or "\x00" in path
        or "\n" in path
        or "\r" in path
    ):
        raise _reject(
            FileReadErrorCode.INVALID_REQUEST,
            "path must be an absolute printable single-line path of at "
            f"most {MAX_PATH_CHARS} characters without NUL",
        )
    normalized = posixpath.normpath(path)
    if not file_read_roots:
        raise _reject(
            FileReadErrorCode.PATH_DENIED,
            "the server profile declares no file_read_roots; reads are denied",
        )
    if not _allowed_target(normalized, file_read_roots):
        raise _reject(
            FileReadErrorCode.PATH_DENIED,
            "path is outside every allowed file_read_roots prefix",
        )
    if _denied_target(normalized):
        raise _reject(
            FileReadErrorCode.PATH_DENIED,
            "path matches the deny-by-default credential material list",
        )
    offset = payload.get("offset", 0)
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_OFFSET
    ):
        raise _reject(
            FileReadErrorCode.INVALID_REQUEST,
            f"offset must be an integer in [0, {MAX_OFFSET}]",
        )
    limit = payload.get("limit", default_limit)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise _reject(
            FileReadErrorCode.INVALID_REQUEST,
            f"limit must be an integer in [1, {MAX_LIMIT}]",
        )
    return FileReadQuery(
        path=path,
        offset=offset,
        limit=limit,
        file_read_roots=_normalized_roots(file_read_roots),
    )


#: The fixed remote helper (Python stdlib only). It receives path,
#: roots JSON, offset, limit and the retained-byte budget as separate
#: structured argv elements after ``--`` and answers with ONE compact
#: JSON line on stdout: exit 0 success
#: ``{"content": str, "total": int, "returned": int}`` or exit 3
#: structured rejection ``{"error": code}``. Any other exit is a
#: helper crash and fails the call honestly.
_HELPER_PROGRAM = """\
import errno, json, os, stat, sys


def _fail(code):
    sys.stdout.write(json.dumps({"error": code}, sort_keys=True) + "\\n")
    sys.exit(3)


def _open_final(parts):
    \"\"\"Open the final component relative to a chain of pinned dirfds.

    Every intermediate component is opened O_DIRECTORY|O_NOFOLLOW
    relative to the previous fd, the final component O_NOFOLLOW relative
    to the last pinned dirfd; the returned fd IS the file (fstat- and
    read-only), so a concurrent symlink or directory swap can only fail
    the walk (ELOOP) — it can never redirect it. Returns the fd, with
    the caller owning every close.
    \"\"\"
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            parent = fd
            fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            os.close(parent)
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd
        )
    finally:
        os.close(fd)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if len(argv) != 5:
        _fail("invalid_request")
    path, roots_json, raw_offset, raw_limit, raw_budget = argv
    try:
        roots = json.loads(roots_json)
        offset = int(raw_offset)
        limit = int(raw_limit)
        budget = int(raw_budget)
    except (TypeError, ValueError):
        _fail("invalid_request")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(root, str) and root.startswith("/") for root in roots)
    ):
        _fail("path_denied")
    if offset < 0 or limit < 1 or budget < 1:
        _fail("invalid_request")
    if (
        not path.startswith("/")
        or "\\x00" in path
        or "\\n" in path
        or "\\r" in path
        or len(path) > 4096
    ):
        _fail("invalid_request")
    # Lexical normalization before the walk: no "."/".." component is
    # ever opened, and the containment verdict below is the exact path
    # the dirfd chain then descends.
    resolved = os.path.normpath(path)
    parts = [part for part in resolved.split("/") if part]
    if not parts:
        _fail("not_regular_file")
    if not any(
        resolved == root or resolved.startswith(root + "/")
        for root in (os.path.normpath(root) for root in roots)
    ):
        _fail("path_denied")
    try:
        fd = _open_final(parts)
    except (FileNotFoundError, NotADirectoryError):
        _fail("path_not_found")
    except OSError as open_error:
        if open_error.errno == errno.ELOOP:
            # A component raced to a symlink mid-walk: O_NOFOLLOW
            # refused it — fail closed, never follow.
            _fail("path_denied")
        raise
    total = 0
    kept = []
    kept_bytes = 0
    index = 0
    retaining = True
    oversized = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail("not_regular_file")
        handle = os.fdopen(fd, "rb")
        fd = -1  # ownership moved to the handle (closed by the with)
        with handle:
            for raw_line in handle:
                total += 1
                if index < offset:
                    index += 1
                    continue
                if retaining:
                    if len(kept) >= limit:
                        retaining = False
                    elif kept_bytes + len(raw_line) > budget:
                        retaining = False
                        if not kept:
                            # The FIRST candidate line alone exceeds the
                            # byte budget: it is skipped and explicitly
                            # labeled (oversized_line), never returned
                            # truncated and never swallowed.
                            oversized = True
                    else:
                        kept.append(raw_line)
                        kept_bytes += len(raw_line)
                index += 1
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        text = b"".join(kept).decode("utf-8")
    except UnicodeDecodeError:
        _fail("decode_error")
    returned = len(kept)
    sys.stdout.write(
        json.dumps(
            {
                "content": text,
                "total": total,
                "returned": returned,
                "next_offset": offset + returned + (1 if oversized else 0),
                "oversized_line": oversized,
            },
            sort_keys=True,
        )
        + "\\n"
    )


main()
"""


def compose_read_argv(query: FileReadQuery) -> tuple[str, ...]:
    """Compose the STRUCTURED argv for one query (pure; C04 rule 2).

    The fixed helper program travels as ONE argv element; path, roots,
    offset, limit and the byte budget travel as separate elements. The
    transport quotes every element, so the target shell reconstructs
    identical argv without interpreting metacharacters.
    """
    roots_json = json.dumps(list(query.file_read_roots), ensure_ascii=True)
    return (
        "python3",
        "-c",
        _HELPER_PROGRAM,
        "--",
        query.path,
        roots_json,
        str(query.offset),
        str(query.limit),
        str(MAX_CONTENT_BYTES),
    )


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary)."""
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.1.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PATH_CHARS,
                        "description": (
                            "Absolute target-side path of one UTF-8 text "
                            "file inside the server profile's "
                            "file_read_roots allowlist; directories and "
                            "symlinks are rejected."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_OFFSET,
                        "description": (
                            "0-based line offset where the page starts "
                            "(default 0)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "description": (
                            f"Maximum lines in the page (default "
                            f"{DEFAULT_LIMIT}, at most {MAX_LIMIT})."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": [
                    "path",
                    "content",
                    "offset",
                    "limit",
                    "total",
                    "has_more",
                    "returned",
                    "next_offset",
                    "oversized_line",
                ],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1},
                    "total": {"type": "integer", "minimum": 0},
                    "has_more": {"type": "boolean"},
                    "returned": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Lines actually contained in this page's "
                            "content (0..limit); a page is never padded "
                            "or cut mid-line."
                        ),
                    },
                    "next_offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Resume offset for the next page: the window "
                            "end, plus one when the first candidate line "
                            "was oversized (labeled by oversized_line) so "
                            "the caller can skip that single line."
                        ),
                    },
                    "oversized_line": {
                        "type": "boolean",
                        "description": (
                            "True when the first candidate line alone "
                            "exceeded the byte budget: it was skipped, "
                            "never truncated and never fabricated; page "
                            "content stays 0 lines in that case."
                        ),
                    },
                    "status": {"type": "string"},
                    "code": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": "read",
            "resource_scope": [],
            "required_grants": [CAPABILITY_ID],
        },
        source=f"<{CAPABILITY_ID} descriptor>",
    )


#: Current canonical profile source (host-composed; re-read per effect).
type ProfileProvider = Callable[[uuid.UUID], ServerProfile]
#: The enrolled known-hosts store for one server (pinned host key).
type KnownHostsProvider = Callable[[uuid.UUID], os.PathLike[str] | str]


class HostFileReadCapability:
    """The dispatcher-facing bounded file reader over the pinned lane.

    Composition is constructor-checked (fail-closed wiring): the host
    supplies the current profile source and the enrolled known-hosts
    store; nothing is cached — both are consulted per call. The
    instance is the dispatcher's ``executor`` (an ``async`` callable of
    :class:`~mai.capabilities.dispatch.CapabilityCall` returning
    :class:`~mai.capabilities.dispatch.ExecutorResult`); it performs no
    policy decision of its own.
    """

    def __init__(
        self,
        *,
        profiles: ProfileProvider,
        known_hosts: KnownHostsProvider,
        default_limit: int = DEFAULT_LIMIT,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(profiles):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT, "profiles must be a callable"
            )
        if not callable(known_hosts):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT, "known_hosts must be a callable"
            )
        if (
            isinstance(default_limit, bool)
            or not isinstance(default_limit, int)
            or not 1 <= default_limit <= MAX_LIMIT
        ):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT,
                f"default_limit must be an int in [1, {MAX_LIMIT}]",
            )
        if (
            isinstance(default_timeout, bool)
            or not isinstance(default_timeout, (int, float))
            or not 0 < default_timeout <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT,
                f"default_timeout must be a number in "
                f"(0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds",
            )
        self._profiles = profiles
        self._known_hosts = known_hosts
        self._limit = default_limit
        self._timeout = default_timeout

    def __repr__(self) -> str:
        return f"<HostFileReadCapability {CAPABILITY_ID}>"

    # -- the dispatcher seam -------------------------------------------------

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see module docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                FileReadErrorCode.INVALID_CONTEXT,
                "call context must be an ExecutionContext",
            )
        profile = self._profile_for(context.server_id)
        key_path = self._key_path_for(call, profile)
        known_hosts = self._known_hosts_for(context.server_id)
        query = read_query_from_payload(
            call.payload,
            file_read_roots=profile.file_read_roots,
            default_limit=self._limit,
        )
        request = CommandRequest(
            op_id=call.op_id,
            server_id=context.server_id,
            display=DISPLAY_LABEL,
            timeout=self._timeout,
            output_budget=TRANSPORT_OUTPUT_BUDGET_BYTES,
            argv=compose_read_argv(query),
        )
        # The frozen system-ssh lane blocks; run it off the event loop.
        return await asyncio.to_thread(
            self._read_roundtrip,
            profile.ssh,
            request,
            key_path,
            known_hosts,
            query,
        )

    # -- pre-effect wiring checks (value-free, fail-closed) -------------------

    def _profile_for(self, server_id: uuid.UUID) -> ServerProfile:
        """Current canonical profile for the context's target."""
        try:
            profile = self._profiles(server_id)
        except FileReadError:
            raise
        except Exception as error:  # noqa: BLE001 - provider failures refuse fail-closed
            raise _reject(
                FileReadErrorCode.PROFILE_UNAVAILABLE,
                f"profile provider failed ({type(error).__name__})",
            ) from None
        if not isinstance(profile, ServerProfile):
            raise _reject(
                FileReadErrorCode.PROFILE_UNAVAILABLE,
                "profile provider returned no ServerProfile",
            )
        if profile.server_id != server_id:
            raise _reject(
                FileReadErrorCode.PROFILE_MISMATCH,
                "profile identity does not match the execution context server_id",
            )
        if profile.environment is not ExecutionEnvironment.LINUX_SSH:
            # Tripwire for the frozen environment vocabulary.
            raise _reject(
                FileReadErrorCode.ENVIRONMENT_UNSUPPORTED,
                "target environment is outside the frozen linux_ssh vocabulary",
            )
        return profile

    def _key_path_for(self, call: CapabilityCall, profile: ServerProfile) -> Path:
        """Controller-local identity key path from the resolved credential."""
        reference = profile.ssh.credential_ref
        secret = call.credentials.get(reference)
        if not isinstance(secret, SecretValue):
            raise _reject(
                FileReadErrorCode.CREDENTIAL_UNAVAILABLE,
                f"execution context carries no resolved value for "
                f"credential reference {reference!r}",
            )
        key_path = Path(secret.value)
        if not key_path.is_file():
            raise _reject(
                FileReadErrorCode.KEY_FILE_UNAVAILABLE,
                f"credential reference {reference!r} does not resolve to "
                "an existing key file",
            )
        return key_path

    def _known_hosts_for(self, server_id: uuid.UUID) -> Path:
        """Enrolled known-hosts file for the target (pinned host key)."""
        try:
            path = Path(os.fspath(self._known_hosts(server_id)))
        except FileReadError:
            raise
        except Exception as error:  # noqa: BLE001 - provider failures refuse fail-closed
            raise _reject(
                FileReadErrorCode.HOST_KEY_UNENROLLED,
                f"known-hosts provider failed ({type(error).__name__})",
            ) from None
        if not path.is_file():
            raise _reject(
                FileReadErrorCode.HOST_KEY_UNENROLLED,
                "no enrolled known-hosts file for this server; pin the "
                "host key explicitly before dispatch (never auto-trust)",
            )
        return path

    # -- effect: the frozen bounded read lane ---------------------------------

    def _read_roundtrip(
        self,
        identity: SshIdentity,
        request: CommandRequest,
        key_path: Path,
        known_hosts: Path,
        query: FileReadQuery,
    ) -> ExecutorResult:
        """One blocking capability call body (runs in a worker thread).

        Transport refusals before the command ran propagate: the
        dispatcher's seam owns that honesty.
        """
        outcome = execute_command(
            identity,
            request,
            key_path=os.fspath(key_path),
            known_hosts_path=os.fspath(known_hosts),
        )
        if outcome.status is CommandStatus.FAILED:
            # The helper's own structured rejection (exit 3, one JSON
            # line) is authoritative for a known non-zero exit; a
            # killed or crashed payload has no trustworthy stdout.
            try:
                failed_response = json.loads(outcome.stdout)
            except ValueError:
                failed_response = None
            if isinstance(failed_response, dict):
                error = failed_response.get("error")
                if error in _REMOTE_REJECTION_CODES:
                    return _refusal_result(query, str(error))
        if outcome.status is not CommandStatus.SUCCEEDED:
            return _failure_result(outcome, query, None)
        try:
            response = json.loads(outcome.stdout)
        except ValueError:
            return _failure_result(outcome, query, None)
        if not isinstance(response, dict):
            return _failure_result(outcome, query, None)
        error = response.get("error")
        if error is not None:
            code = error if error in _REMOTE_REJECTION_CODES else None
            return _failure_result(outcome, query, code)
        content = response.get("content")
        total = response.get("total")
        returned = response.get("returned")
        next_offset = response.get("next_offset")
        oversized_line = response.get("oversized_line")
        if (
            not isinstance(content, str)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or isinstance(returned, bool)
            or not isinstance(returned, int)
            or not 0 <= returned <= query.limit
            or not isinstance(oversized_line, bool)
            or isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            # next_offset is the honest resume point: the window end,
            # plus exactly one skipped line when the first candidate
            # was oversized. Anything else is an untrustworthy helper.
            or next_offset != query.offset + returned + (1 if oversized_line else 0)
        ):
            return _failure_result(outcome, query, None)
        document: dict[str, object] = {
            "path": query.path,
            "content": content,
            "offset": query.offset,
            "limit": query.limit,
            "total": total,
            "has_more": total > next_offset,
            "returned": returned,
            "next_offset": next_offset,
            "oversized_line": oversized_line,
        }
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))


def _refusal_result(query: FileReadQuery, code: str) -> ExecutorResult:
    """One structured helper rejection: stable code, honest status."""
    document: dict[str, object] = {
        "path": query.path,
        "status": "refused",
        "code": code,
    }
    return ExecutorResult(ok=False, error_code=code, output=json.dumps(document))


def _failure_result(
    outcome: CommandOutcome,
    query: FileReadQuery,
    code: str | None,
) -> ExecutorResult:
    """One honest non-success answer: status, bounded code, no content."""
    document: dict[str, object] = {
        "path": query.path,
        "status": outcome.status.value,
        "exit_code": outcome.exit_code,
    }
    if code is not None:
        document["code"] = code
    return ExecutorResult(ok=False, error_code=code, output=json.dumps(document))
