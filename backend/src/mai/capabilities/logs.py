"""The target-log capability: bounded journal/container logs as evidence.

Card ``04-log-capability`` against contracts.md **C03** (rules 2, 5, 6,
7) and **C04** (rule 2), **C06** (rule 3). This module is the trusted
capability implementation (C03 rule 7) that turns a strictly validated
log request into bounded, secret-filtered, artifact-backed evidence —
and nothing else. Like every capability it is reachable only through the
common dispatcher (:class:`~mai.capabilities.dispatch.CapabilityDispatcher`),
which runs the grant/adoption/policy gates (and the immediate pre-effect
recheck) before this executor exists in the call graph.

Effect class and approval semantics (C03 rule 5)
------------------------------------------------

Reading logs changes no observable state, so the descriptor registers
effect class ``read``: the policy layer requires NO approval decision in
either prompting mode. Every authorization question — grant scope,
adoption, policy deny list, server scope — is still answered by the
dispatcher before invocation; this executor performs no policy decision
of its own and has no API that could bypass that order.

Request shape (acceptance 1)
----------------------------

The payload is bounded and value-validated *before any transport*:
``source`` (``journal`` | ``container``), ``service`` (a journal unit or
container name, strict charset — no path separators, no shell
metacharacters), optional ``since``/``until`` absolute ISO-8601
timestamps, ``lines`` (most-recent-line cap) and ``output_budget``
(per-stream retention budget in bytes) plus ``timeout``. The target is
NEVER a payload field: the :class:`~mai.policy.evaluate.ExecutionContext`
``server_id`` is the only target vocabulary (C03 rule 2), so a payload
cannot divert or widen a request. There is deliberately no path field —
arbitrary host file reads are not implied, supported or reachable.

Effect transport (C04 rule 2)
-----------------------------

The composed command is STRUCTURED argv (never a shell string):
``journalctl --unit <unit> --no-pager --utc --lines <n> [--since …]
[--until …]`` or ``docker logs [--since …] [--until …] --tail <n>
--timestamps <container>``, executed through the frozen bounded
synchronous lane (:func:`~mai.execution.commands.execute_command`) over
the pinned-identity SSH transport. A read writes no target-side state —
no supervision wrapper, no receipt directory; the bounded lane is the
contract lane for exactly this class of non-mutating retrieval. The
blocking system-ssh lane runs inside ``asyncio.to_thread`` (the
composition duty the password channel established).

Result shape and secret filtering (acceptance 2)
------------------------------------------------

Resolved credential values (C03 rule 6: resolved at effect time by the
dispatcher, handed to the executor as references→values) are replaced
with the shared ``REDACTED`` token BEFORE any bounding or persistence —
a budget cut can never split a secret around the marker. Output is then
stored as one SCOPED evidence artifact plus a provenance sidecar under
the host-composed artifacts root (``<server_id>/<op_id>.log`` + ``.json``,
paths derived only from opaque UUIDs and the approved root, atomic
temp+fsync+replace writes, 0600/0700 permissions). The executor result
document carries metadata, honest totals/truncation flags and only a
small redacted excerpt — large log bodies never enter the model context
(C06 rule 3). Artifact persistence is load-bearing: if the artifact
cannot be written the call FAILS (``artifact_unavailable``) rather than
returning unbounded log content without its evidence store.

Failure honesty
---------------

Pre-effect rejections (missing/mismatched profile, unenrolled host key,
missing credential reference, unusable key file, malformed payload)
raise :class:`LogsError` BEFORE any transport attempt; the dispatcher's
frozen seam records such an exception as ``effect="unknown"`` /
``executor_failed`` — never a claimed execution. Transport-level
failures after the attempt propagate unchanged for the same reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from mai.capabilities.dispatch import REDACTED, CapabilityCall, ExecutorResult
from mai.contracts.server import (
    ExecutionEnvironment,
    ServerProfile,
    SshIdentity,
)
from mai.credentials.env import SecretValue
from mai.execution.commands import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_OUTPUT_BUDGET_BYTES,
    CommandOutcome,
    CommandRequest,
    CommandStatus,
    execute_command,
)
from mai.policy.evaluate import ExecutionContext
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LOG_LINES",
    "DEFAULT_OUTPUT_BUDGET_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DISPLAY_LABEL",
    "LOG_SOURCES",
    "MAX_EXCERPT_BYTES",
    "MAX_LOG_LINES",
    "MAX_OUTPUT_BUDGET_BYTES",
    "ArtifactReference",
    "HostLogsCapability",
    "LogQuery",
    "LogSource",
    "LogsError",
    "LogsErrorCode",
    "compose_log_argv",
    "descriptor",
    "log_query_from_payload",
    "write_evidence_artifact",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "host.logs"

#: Safe one-line label carried by every request/outcome/artifact.
#: Deliberately value-free: the composed command is payload/transport
#: content, never a display label (C04 rule 3 discipline).
DISPLAY_LABEL = "host.logs"

#: Log producers this capability supports. Anything else is rejected
#: BEFORE transport — there is no generic "read this file" source.
LOG_SOURCES = frozenset({"journal", "container"})

#: Request defaults (per-call payload values override; all bounded).
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_OUTPUT_BUDGET_BYTES = 256 * 1024
DEFAULT_LOG_LINES = 1000

#: Caps accepted from one payload (fail-closed DoS bounds; the transport
#: enforces its own hard ceilings beyond these). The byte ceiling is the
#: commands lane's own cap, re-exported for descriptor/schema use.
MAX_LOG_LINES = 10_000

#: Cap on the excerpt returned in the executor document (the model-facing
#: window; C06 rule 3). The artifact holds the retained body regardless.
MAX_EXCERPT_BYTES = 8 * 1024

#: Service identifier discipline: journal units and container names are
#: validated against one strict charset. ``/`` is excluded, so a service
#: name can never denote a host path; shell metacharacters are excluded
#: even though the structured-argv transport would quote them anyway.
MAX_SERVICE_CHARS = 128
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")

#: ``since``/``until`` accept absolute ISO-8601-ish timestamps only —
#: both producers parse this grammar natively and the strict charset
#: leaves no injection surface. Relative durations ("10m") are a
#: deliberate non-feature: the caller computes an absolute bound.
MAX_TIMESTAMP_CHARS = 32
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$"
)

#: Payload vocabulary (frozen by this descriptor's input schema).
_PAYLOAD_FIELDS = frozenset(
    {"source", "service", "since", "until", "lines", "output_budget", "timeout"}
)


class LogsErrorCode(StrEnum):
    """Stable machine-readable pre-effect rejection codes (C01 style)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CONTEXT = "invalid_context"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_MISMATCH = "profile_mismatch"
    ENVIRONMENT_UNSUPPORTED = "environment_unsupported"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    KEY_FILE_UNAVAILABLE = "key_file_unavailable"
    HOST_KEY_UNENROLLED = "host_key_unenrolled"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"


class LogsError(ValueError):
    """One structured, value-free pre-effect rejection.

    Raised only before any transport attempt (or for a failed evidence
    write after the read — see the module docstring); the dispatcher's
    seam records it as ``executor_failed`` with ``effect="unknown"``
    (the receipt claims no proven effect and quotes the exception *type*
    name only — never this detail, which is for the host's structured
    logs and the operator).
    """

    def __init__(self, code: LogsErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class LogSource(StrEnum):
    """Supported log producers (frozen set; anything else denies)."""

    JOURNAL = "journal"
    CONTAINER = "container"


def _reject(code: LogsErrorCode, detail: str) -> LogsError:
    return LogsError(code, detail)


@dataclass(frozen=True, slots=True)
class LogQuery:
    """One validated, fully bounded log request (pure value object).

    Every field was checked by :func:`log_query_from_payload`; the
    target is not part of the query — it comes from the execution
    context at dispatch time.
    """

    source: LogSource
    service: str
    lines: int
    output_budget: int
    timeout: float
    since: str | None = None
    until: str | None = None


def log_query_from_payload(
    payload: object,
    *,
    default_lines: int = DEFAULT_LOG_LINES,
    default_output_budget: int = DEFAULT_OUTPUT_BUDGET_BYTES,
    default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> LogQuery:
    """Validate one operation payload into a bounded query (pure).

    ``service`` must match the strict charset (no path separators, no
    shell metacharacters); ``since``/``until`` must be absolute
    ISO-8601-ish timestamps; ``lines``/``output_budget``/``timeout``
    are bounded. Every rejection happens here, BEFORE any state read or
    transport attempt.

    Raises:
        LogsError: ``invalid_request`` for any shape violation.
    """
    if not isinstance(payload, Mapping):
        raise _reject(LogsErrorCode.INVALID_REQUEST, "payload must be a JSON object")
    unknown = sorted(str(key) for key in payload if key not in _PAYLOAD_FIELDS)
    if unknown:
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            f"unknown payload field {unknown[0]!r}",
        )
    raw_source = payload.get("source")
    if not isinstance(raw_source, str) or raw_source not in LOG_SOURCES:
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            "source must be one of " + ", ".join(sorted(LOG_SOURCES)),
        )
    service = payload.get("service")
    if not isinstance(service, str) or not _SERVICE_RE.fullmatch(service):
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            "service must be a journal unit or container name (at most "
            f"{MAX_SERVICE_CHARS} characters of [A-Za-z0-9_.@-], never a path)",
        )
    since = _validated_timestamp(payload, "since")
    until = _validated_timestamp(payload, "until")
    lines = payload.get("lines", default_lines)
    if (
        isinstance(lines, bool)
        or not isinstance(lines, int)
        or not 1 <= lines <= MAX_LOG_LINES
    ):
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            f"lines must be an integer in [1, {MAX_LOG_LINES}]",
        )
    budget = payload.get("output_budget", default_output_budget)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int)
        or not 0 < budget <= MAX_OUTPUT_BUDGET_BYTES
    ):
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            f"output_budget must be an integer in (0, {MAX_OUTPUT_BUDGET_BYTES}] bytes",
        )
    timeout = payload.get("timeout", default_timeout)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            f"timeout must be a number in (0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds",
        )
    return LogQuery(
        source=LogSource(raw_source),
        service=service,
        lines=lines,
        output_budget=budget,
        timeout=timeout,
        since=since,
        until=until,
    )


def _validated_timestamp(payload: Mapping[str, object], field: str) -> str | None:
    """One optional absolute-timestamp payload field (pure)."""
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise _reject(
            LogsErrorCode.INVALID_REQUEST,
            f"{field} must be an absolute ISO-8601 timestamp "
            f"(at most {MAX_TIMESTAMP_CHARS} characters); relative "
            "durations are not accepted",
        )
    return value


def compose_log_argv(query: LogQuery) -> tuple[str, ...]:
    """Compose the STRUCTURED argv for one query (pure; C04 rule 2).

    No shell string is ever built: every element is a separate argv
    entry, so the target default shell reconstructs identical argv
    without interpreting metacharacters (the transport quotes each
    element). Both producers use their native most-recent-N-lines flag;
    the time window and line/byte bounds travel with the command.
    """
    if query.source is LogSource.JOURNAL:
        argv: list[str] = [
            "journalctl",
            "--unit",
            query.service,
            "--no-pager",
            "--utc",
            "--lines",
            str(query.lines),
        ]
    else:
        argv = ["docker", "logs"]
        if query.since is not None:
            argv += ["--since", query.since]
        if query.until is not None:
            argv += ["--until", query.until]
        argv += ["--tail", str(query.lines), "--timestamps", query.service]
        return tuple(argv)
    if query.since is not None:
        argv += ["--since", query.since]
    if query.until is not None:
        argv += ["--until", query.until]
    return tuple(argv)


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="read"`` is the load-bearing field: reading logs
    changes no observable state, so the policy layer requires no
    approval decision in either mode — while every grant/scope/policy
    gate still applies unchanged (pinned by tests against the real
    ``evaluate()``. No resource scope: concurrent log reads share no
    lockable resource.
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "service"],
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": sorted(LOG_SOURCES),
                        "description": (
                            "Target log producer; there is no generic "
                            "file-read source (arbitrary host file reads "
                            "are not implied)."
                        ),
                    },
                    "service": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SERVICE_CHARS,
                        "pattern": _SERVICE_RE.pattern,
                        "description": (
                            "Journal unit (journal source) or container "
                            "name (container source); path separators and "
                            "shell metacharacters are rejected."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "maxLength": MAX_TIMESTAMP_CHARS,
                        "pattern": _TIMESTAMP_RE.pattern,
                        "description": "Absolute window start (ISO-8601).",
                    },
                    "until": {
                        "type": "string",
                        "maxLength": MAX_TIMESTAMP_CHARS,
                        "pattern": _TIMESTAMP_RE.pattern,
                        "description": "Absolute window end (ISO-8601).",
                    },
                    "lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LOG_LINES,
                        "description": "Most-recent-line cap.",
                    },
                    "output_budget": {
                        "type": "integer",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_OUTPUT_BUDGET_BYTES,
                        "description": (
                            "Per-stream retention budget in bytes; the "
                            "artifact holds the retained bytes, the model "
                            "context only a small excerpt."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                        "description": "Controller-enforced wall clock in seconds.",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": [
                    "status",
                    "source",
                    "service",
                    "artifact",
                    "excerpt",
                ],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(item.value for item in CommandStatus),
                    },
                    "exit_code": {"type": ["integer", "null"]},
                    "source": {"type": "string", "enum": sorted(LOG_SOURCES)},
                    "service": {"type": "string"},
                    "since": {"type": ["string", "null"]},
                    "until": {"type": ["string", "null"]},
                    "lines_requested": {"type": "integer"},
                    "output_budget_bytes": {"type": "integer"},
                    "timeout_seconds": {"type": "number"},
                    "stdout_bytes": {"type": "integer"},
                    "stdout_truncated": {"type": "boolean"},
                    "stderr_bytes": {"type": "integer"},
                    "stderr_truncated": {"type": "boolean"},
                    "secrets_redacted": {"type": "boolean"},
                    "artifact": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "provenance": {"type": "string"},
                            "sha256": {"type": "string"},
                            "bytes": {"type": "integer"},
                        },
                    },
                    "excerpt": {"type": "string"},
                    "excerpt_bytes": {"type": "integer"},
                    "excerpt_truncated": {"type": "boolean"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                    "captured_at": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": "read",
            "resource_scope": [],
            "required_grants": [CAPABILITY_ID],
        },
        source=f"<{CAPABILITY_ID} descriptor>",
    )


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Where one retrieved log body and its provenance live."""

    path: str
    provenance: str
    sha256: str
    bytes: int


#: Current canonical profile source (host-composed; re-read per effect).
type ProfileProvider = Callable[[uuid.UUID], ServerProfile]
#: The enrolled known-hosts store for one server (host-composed; the
#: pinned host key lives there — an unenrolled target refuses here).
type KnownHostsProvider = Callable[[uuid.UUID], os.PathLike[str] | str]


def write_evidence_artifact(
    root: os.PathLike[str] | str,
    server_id: uuid.UUID,
    op_id: uuid.UUID,
    content: bytes,
    provenance: Mapping[str, object],
) -> ArtifactReference:
    """Persist one scoped log artifact plus its provenance sidecar.

    Layout: ``<root>/<server_id>/<op_id>.log`` + ``<op_id>.json`` —
    paths derive ONLY from opaque UUIDs below the approved root (C02
    rule 3). Writes are atomic (temp file + fsync + ``os.replace``) and
    restrictive (0700 directories, 0600 files), mirroring the canonical
    file store's crash discipline. The sidecar is the provenance record:
    where the bytes came from, how they were bounded and filtered.

    Raises:
        LogsError: ``artifact_unavailable`` when the evidence store
            cannot be written — callers must fail the call rather than
            return log bodies without their artifact.
    """
    try:
        directory = Path(root) / str(server_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
        log_path = directory / f"{op_id}.log"
        _atomic_write(log_path, content)
        document = dict(provenance)
        document["artifact_sha256"] = hashlib.sha256(content).hexdigest()
        document["artifact_bytes"] = len(content)
        document["artifact_path"] = str(log_path)
        sidecar = directory / f"{op_id}.json"
        _atomic_write(
            sidecar, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
        )
    except OSError as error:
        raise _reject(
            LogsErrorCode.ARTIFACT_UNAVAILABLE,
            f"evidence artifact could not be written ({type(error).__name__})",
        ) from None
    return ArtifactReference(
        path=str(log_path),
        provenance=str(sidecar),
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via temp file + fsync + atomic replace."""
    directory = path.parent
    fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _redact(text: str, secrets: list[str]) -> str:
    """Replace every resolved secret value in ``text`` (C03 rule 6).

    Local copy of the dispatcher's redaction discipline (that module is
    outside this card's allowlist and its helper is private; a
    divergence would be a review finding). Runs BEFORE bounding so a
    budget cut can never split a secret around the marker.
    """
    for value in secrets:
        if value:
            text = text.replace(value, REDACTED)
    return text


class HostLogsCapability:
    """The dispatcher-facing bounded log reader over the pinned lane.

    Composition is constructor-checked (fail-closed wiring, the sibling
    pattern): the host supplies the current profile source, the enrolled
    known-hosts store, the controller-side evidence artifacts root and
    the bounded defaults. Nothing is cached — profile and enrollment
    store are consulted per call, so a revoked enrollment cannot ride on
    a warm capability object.

    The instance is the dispatcher's ``executor`` (an ``async`` callable
    of :class:`~mai.capabilities.dispatch.CapabilityCall` returning
    :class:`~mai.capabilities.dispatch.ExecutorResult`); it performs no
    policy decision of its own — every grant/adoption/policy question
    was answered before invocation.
    """

    def __init__(
        self,
        *,
        profiles: ProfileProvider,
        known_hosts: KnownHostsProvider,
        artifacts_root: os.PathLike[str] | str,
        default_lines: int = DEFAULT_LOG_LINES,
        default_output_budget: int = DEFAULT_OUTPUT_BUDGET_BYTES,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(profiles):
            raise _reject(LogsErrorCode.INVALID_CONTEXT, "profiles must be a callable")
        if not callable(known_hosts):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT, "known_hosts must be a callable"
            )
        root = os.fspath(artifacts_root) if artifacts_root else ""
        if (
            not isinstance(root, str)
            or not root.startswith("/")
            or not root.isprintable()
            or "\x00" in root
            or len(root) > 4096
        ):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                "artifacts_root must be an absolute controller-side path "
                "(printable single line, at most 4096 characters)",
            )
        if (
            isinstance(default_lines, bool)
            or not isinstance(default_lines, int)
            or not 1 <= default_lines <= MAX_LOG_LINES
        ):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                f"default_lines must be an int in [1, {MAX_LOG_LINES}]",
            )
        if (
            isinstance(default_output_budget, bool)
            or not isinstance(default_output_budget, int)
            or not 0 < default_output_budget <= MAX_OUTPUT_BUDGET_BYTES
        ):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                f"default_output_budget must be an int in "
                f"(0, {MAX_OUTPUT_BUDGET_BYTES}] bytes",
            )
        if (
            isinstance(default_timeout, bool)
            or not isinstance(default_timeout, (int, float))
            or not 0 < default_timeout <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                f"default_timeout must be a number in "
                f"(0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds",
            )
        self._profiles = profiles
        self._known_hosts = known_hosts
        self._root = root
        self._lines = default_lines
        self._budget = default_output_budget
        self._timeout = default_timeout

    def __repr__(self) -> str:
        return f"<HostLogsCapability {CAPABILITY_ID} root={self._root!r}>"

    # -- the dispatcher seam -------------------------------------------------

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see module docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                LogsErrorCode.INVALID_CONTEXT,
                "call context must be an ExecutionContext",
            )
        profile = self._profile_for(context.server_id)
        key_path = self._key_path_for(call, profile)
        known_hosts = self._known_hosts_for(context.server_id)
        query = log_query_from_payload(
            call.payload,
            default_lines=self._lines,
            default_output_budget=self._budget,
            default_timeout=self._timeout,
        )
        request = CommandRequest(
            op_id=call.op_id,
            server_id=context.server_id,
            display=DISPLAY_LABEL,
            timeout=query.timeout,
            output_budget=query.output_budget,
            argv=compose_log_argv(query),
        )
        secrets = [
            secret.value
            for secret in call.credentials.values()
            if isinstance(secret, SecretValue)
        ]
        # The frozen system-ssh lane blocks; the password channel card
        # fixed the composition duty: run it off the host event loop.
        return await asyncio.to_thread(
            self._read_roundtrip,
            profile.ssh,
            request,
            key_path,
            known_hosts,
            query,
            secrets,
            context,
        )

    # -- pre-effect wiring checks (value-free, fail-closed) -------------------

    def _profile_for(self, server_id: uuid.UUID) -> ServerProfile:
        """Current canonical profile for the context's target."""
        try:
            profile = self._profiles(server_id)
        except LogsError:
            raise
        except Exception as error:
            raise _reject(
                LogsErrorCode.PROFILE_UNAVAILABLE,
                f"profile provider failed ({type(error).__name__})",
            ) from None
        if not isinstance(profile, ServerProfile):
            raise _reject(
                LogsErrorCode.PROFILE_UNAVAILABLE,
                "profile provider returned no ServerProfile",
            )
        if profile.server_id != server_id:
            raise _reject(
                LogsErrorCode.PROFILE_MISMATCH,
                "profile identity does not match the execution context server_id",
            )
        if profile.environment is not ExecutionEnvironment.LINUX_SSH:
            # Tripwire for the frozen environment vocabulary: unreachable
            # while ``linux_ssh`` is its only member; a second member must
            # route through a different capability, never through here.
            raise _reject(
                LogsErrorCode.ENVIRONMENT_UNSUPPORTED,
                "target environment is outside the frozen linux_ssh vocabulary",
            )
        return profile

    def _key_path_for(self, call: CapabilityCall, profile: ServerProfile) -> Path:
        """Controller-local identity key path from the resolved credential.

        The context's credential REFERENCES were resolved by the
        dispatcher at effect time (C03 rules 2/6); the profile names the
        reference and this module never resolves anything itself — a
        context that does not carry the profile's reference refuses
        instead of falling back to any ambient source.
        """
        reference = profile.ssh.credential_ref
        secret = call.credentials.get(reference)
        if not isinstance(secret, SecretValue):
            raise _reject(
                LogsErrorCode.CREDENTIAL_UNAVAILABLE,
                f"execution context carries no resolved value for "
                f"credential reference {reference!r}",
            )
        key_path = Path(secret.value)
        if not key_path.is_file():
            raise _reject(
                LogsErrorCode.KEY_FILE_UNAVAILABLE,
                f"credential reference {reference!r} does not resolve to "
                "an existing key file",
            )
        return key_path

    def _known_hosts_for(self, server_id: uuid.UUID) -> Path:
        """Enrolled known-hosts file for the target (G2 pinned host key)."""
        try:
            path = Path(os.fspath(self._known_hosts(server_id)))
        except LogsError:
            raise
        except Exception as error:
            raise _reject(
                LogsErrorCode.HOST_KEY_UNENROLLED,
                f"known-hosts provider failed ({type(error).__name__})",
            ) from None
        if not path.is_file():
            raise _reject(
                LogsErrorCode.HOST_KEY_UNENROLLED,
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
        query: LogQuery,
        secrets: list[str],
        context: ExecutionContext,
    ) -> ExecutorResult:
        """Read, redact, persist evidence, excerpt — the blocking body of
        one capability call (runs in a worker thread). Raises from the
        lane propagate: a transport refusal before the command ran is an
        unproven read, and the dispatcher's seam owns that honesty."""
        outcome = execute_command(
            identity,
            request,
            key_path=os.fspath(key_path),
            known_hosts_path=os.fspath(known_hosts),
        )
        stdout = _redact(outcome.stdout, secrets)
        stderr = _redact(outcome.stderr, secrets)
        secrets_redacted = stdout != outcome.stdout or stderr != outcome.stderr
        artifact = self._persist_evidence(
            request, query, outcome, stdout, stderr, secrets_redacted, context
        )

        excerpt, excerpt_truncated, notes = self._excerpt(stdout, stderr, outcome)
        document: dict[str, object] = {
            "capability": CAPABILITY_ID,
            "op_id": str(outcome.op_id),
            "server_id": str(outcome.server_id),
            "source": query.source.value,
            "service": query.service,
            "since": query.since,
            "until": query.until,
            "lines_requested": query.lines,
            "output_budget_bytes": query.output_budget,
            "timeout_seconds": query.timeout,
            "status": outcome.status.value,
            "exit_code": outcome.exit_code,
            "stdout_bytes": outcome.stdout_bytes,
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_bytes": outcome.stderr_bytes,
            "stderr_truncated": outcome.stderr_truncated,
            "secrets_redacted": secrets_redacted,
            "artifact": {
                "path": artifact.path,
                "provenance": artifact.provenance,
                "sha256": artifact.sha256,
                "bytes": artifact.bytes,
            },
            "excerpt": excerpt,
            "excerpt_bytes": len(excerpt.encode("utf-8")),
            "excerpt_truncated": excerpt_truncated,
            "notes": notes,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": outcome.duration_seconds,
        }
        return ExecutorResult(
            ok=outcome.status is CommandStatus.SUCCEEDED,
            output=json.dumps(document, sort_keys=True),
        )

    def _persist_evidence(
        self,
        request: CommandRequest,
        query: LogQuery,
        outcome: CommandOutcome,
        stdout: str,
        stderr: str,
        secrets_redacted: bool,
        context: ExecutionContext,
    ) -> ArtifactReference:
        """One artifact body (both streams, explicitly separated) plus
        its provenance sidecar. Load-bearing for acceptance 2: a failed
        evidence write fails the whole call."""
        body = bytearray(stdout.encode("utf-8"))
        if stderr:
            body += b"\n--- stderr ---\n"
            body += stderr.encode("utf-8")
        content = bytes(body)
        provenance: dict[str, object] = {
            "capability": CAPABILITY_ID,
            "descriptor_version": "1.0.0",
            "op_id": str(outcome.op_id),
            "server_id": str(outcome.server_id),
            "run_id": str(context.run_id),
            "principal": context.principal,
            "source": query.source.value,
            "service": query.service,
            "since": query.since,
            "until": query.until,
            "lines_requested": query.lines,
            "output_budget_bytes": query.output_budget,
            "timeout_seconds": query.timeout,
            "status": outcome.status.value,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "stdout_bytes": outcome.stdout_bytes,
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_bytes": outcome.stderr_bytes,
            "stderr_truncated": outcome.stderr_truncated,
            "secrets_redacted": secrets_redacted,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": outcome.duration_seconds,
        }
        try:
            return write_evidence_artifact(
                self._root,
                request.server_id,
                request.op_id,
                content,
                provenance,
            )
        except LogsError:
            raise
        except OSError as error:
            # The evidence store is load-bearing (acceptance 2): a call
            # never returns log bodies without their persisted artifact.
            raise _reject(
                LogsErrorCode.ARTIFACT_UNAVAILABLE,
                f"evidence artifact could not be written ({type(error).__name__})",
            ) from None

    def _excerpt(
        self, stdout: str, stderr: str, outcome: CommandOutcome
    ) -> tuple[str, bool, list[str]]:
        """The model-facing window: the most recent tail of the redacted
        retained stdout (or stderr diagnostics when stdout is empty),
        never the whole retained body (C06 rule 3)."""
        notes: list[str] = []
        source_text = stdout
        if not source_text and stderr:
            source_text = stderr
            notes.append("excerpt source: stderr (stdout was empty)")
        elif stderr and source_text.strip() == "-- No entries --":
            source_text = f"{source_text.rstrip()}\n{stderr}"
            notes.append(
                "excerpt includes stderr diagnostics (journalctl reported no entries)"
            )
        raw = source_text.encode("utf-8")
        if outcome.stdout_truncated:
            # Explicit truncation notice (C06 rule 3): the artifact is a
            # bounded head window of the true log body, never the body.
            notes.append(
                "log body exceeded the retention budget: the artifact "
                "carries the first retained bytes (head window); narrow "
                "the time window or raise output_budget for more"
            )
        if len(raw) <= MAX_EXCERPT_BYTES:
            return source_text, False, notes
        kept = raw[-MAX_EXCERPT_BYTES:].decode("utf-8", errors="ignore")
        return kept, True, notes
