"""The raw-shell host capability: policy-bound remote command execution.

Card ``04-shell-capability`` against contracts.md **C03** (rules 2, 4, 5,
7) and **C04** (rules 2, 3, 5). This module is the trusted capability
implementation (C03 rule 7) that the common dispatcher
(:class:`~mai.capabilities.dispatch.CapabilityDispatcher`) executes after
every gate — the ONE seam where a raw shell string may reach a remote
target (C04 rule 2: shell strings pass only through the explicit shell
capability). There is deliberately no other entrypoint — no ``python -m``
CLI, no direct-call helper — so every command carries its explicit
target/context (the :class:`~mai.policy.evaluate.ExecutionContext` is the
only target vocabulary; the payload names no target and could not divert
one) and passes the grant/adoption/policy/approval gates first
(acceptance 1). An unapproved or revoked call is denied by the dispatcher
before this executor exists in the call graph, so it cannot reach SSH
(acceptance 2; pinned end to end against a real disposable target in
``backend/tests/test_host_shell.py``).

Raw shell is the explicitly broad opt-in (C03 rule 5): the descriptor
registers effect class ``privileged``, so the policy layer routes every
call through an approval decision in BOTH prompting modes and stamps the
``raw_root_forfeits_os_containment`` advisory on every allowed decision —
enabling this capability forfeits app-level OS containment claims, and
the decision surface says so on every receipt.

Effect transport (C04 rules 3/5)
--------------------------------

The executor composes the frozen supervisor lane only:
:func:`~mai.execution.supervisor.submit_supervised_command` persists the
dispatch intent controller-side BEFORE submission, installs the
target-local supervision wrapper with its durable receipt directory, and
launches detached; :func:`~mai.execution.supervisor.inspect_supervised_command`
reads the receipt back through the frozen C04 status mapping. The SSH
session is never the owner; a lost channel loses neither the operation
nor its handle. The capability then settles for a bounded wall-clock
budget (quick commands usually finish inside it) and returns whatever is
proven — ``running``/``outcome_unknown`` are honest final answers at this
seam, never upgraded to a success (host rule 3). C05 consumers reconcile
uncertain operations through the reconcile card's public API.

Result shape (acceptance 2)
---------------------------

The :class:`~mai.capabilities.dispatch.ExecutorResult` output is one
JSON document with the frozen vocabulary of this module: the mapped
status, exit code where known, bounded stdout/stderr with true byte
totals and explicit truncation flags, the target receipt directory and
controller intent path as evidence references, identity/heartbeat/boot
facts and the supervisor's value-free notes — outcome AND evidence, not
only an exit code. ``ok`` is true exactly when the status is
``succeeded``; every other status keeps ``ok=False`` with the honest
status in the document. Resolved secret values are not part of the
document; the dispatcher's redaction-then-bound output boundary remains
the last line of defense (C03 rule 6).

Failure honesty
---------------

Pre-effect rejections (missing profile, mismatched profile identity,
unenrolled host key, missing credential reference, unusable key file,
malformed payload) raise :class:`HostShellError` BEFORE any transport
attempt. The dispatcher's frozen seam records such an exception as
``effect="unknown"``/``executor_failed`` — deliberately not
``ok=False``/``executed``, which would claim an external effect that
never happened. Transport-level failures after submission propagate
unchanged for the same reason. The blocking system-ssh lane runs inside
``asyncio.to_thread`` (the composition duty the password channel
established); ``asyncio`` cancellation cannot retract a submitted
effect — the durable receipt contract is what makes that survivable.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
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
    MAX_OUTPUT_BUDGET_BYTES,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
)
from mai.execution.supervisor import (
    DEFAULT_PROBE_TIMEOUT,
    SupervisionHandle,
    SupervisionReport,
    inspect_supervised_command,
    submit_supervised_command,
)
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_OUTPUT_BUDGET_BYTES",
    "DEFAULT_SETTLE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DISPLAY_LABEL",
    "MAX_SETTLE_SECONDS",
    "HostShellCapability",
    "HostShellError",
    "HostShellErrorCode",
    "descriptor",
    "request_from_payload",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "host.shell"

#: Safe one-line label carried by every CommandRequest/Outcome/receipt.
#: Deliberately value-free: the raw command is approval-payload content
#: and target-side log content, never a display label (C04 rule 3).
DISPLAY_LABEL = "host.shell"

#: Request defaults (per-call payload values override; all bounded).
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_OUTPUT_BUDGET_BYTES = 64 * 1024

#: Wall-clock budget the executor spends settling a submitted operation
#: before reporting the last proven state (bounded, never a retry).
DEFAULT_SETTLE_SECONDS = 15.0
MAX_SETTLE_SECONDS = 300.0
SETTLE_POLL_SECONDS = 0.5

#: Cap on the events-log tail copied into the result document (the
#: supervisor already reads at most its own tail bound; this keeps the
#: tool result small for model contexts, C06 rule 3).
MAX_EVENTS_TAIL_CHARS = 1024

#: Per-stream UTF-8 byte cap in the JSON result document so that the
#: serialized JSON (stdout + stderr + envelope) always fits comfortably
#: inside CapabilityDispatcher's 64 KiB output budget without slicing the
#: JSON mid-string (which would render the status unparseable).
MAX_RESULT_STREAM_BYTES = 24 * 1024

#: Payload vocabulary (frozen by this descriptor's input schema).
_PAYLOAD_FIELDS = frozenset(
    {"command", "timeout", "output_budget", "working_directory"}
)

#: Receipt states that end the settle loop; everything else (``running``,
#: ``starting``-derived ``outcome_unknown``) keeps polling until the
#: budget is spent, then reports the last proven state.
_TERMINAL_STATUSES = frozenset({CommandStatus.SUCCEEDED, CommandStatus.FAILED})


class HostShellErrorCode(StrEnum):
    """Stable machine-readable pre-effect rejection codes (C01 style)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CONTEXT = "invalid_context"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_MISMATCH = "profile_mismatch"
    ENVIRONMENT_UNSUPPORTED = "environment_unsupported"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    KEY_FILE_UNAVAILABLE = "key_file_unavailable"
    HOST_KEY_UNENROLLED = "host_key_unenrolled"


class HostShellError(ValueError):
    """One structured, value-free pre-effect rejection.

    Raised only before any transport attempt; the dispatcher's seam
    records it as ``executor_failed`` with ``effect="unknown"`` (the
    receipt claims no executed effect and quotes the exception *type*
    name only — never this detail, which is for the host's structured
    logs and the operator).
    """

    def __init__(self, code: HostShellErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: HostShellErrorCode, detail: str) -> HostShellError:
    return HostShellError(code, detail)


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="privileged"`` is the load-bearing field: it is what
    makes the policy layer require an approval decision for every call
    in both modes and attach the OS-containment forfeiture advisory.
    The schemas describe the shapes this module actually produces;
    the registry validates structure, the executor enforces the bounds.
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Raw POSIX shell command string executed on the "
                            "target through the supervision wrapper — the "
                            "explicit broad opt-in (C04 rule 2)."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                        "description": "Controller-enforced wall clock in seconds.",
                    },
                    "output_budget": {
                        "type": "integer",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_OUTPUT_BUDGET_BYTES,
                        "description": "Per-stream retention budget in bytes.",
                    },
                    "working_directory": {
                        "type": "string",
                        "pattern": "^/",
                        "description": (
                            "Absolute target-side path; a failed cd aborts "
                            "the payload before it starts."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "exit_code", "stdout", "stderr"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(item.value for item in CommandStatus),
                    },
                    "exit_code": {"type": ["integer", "null"]},
                    "stdout": {"type": "string"},
                    "stdout_bytes": {"type": "integer"},
                    "stdout_truncated": {"type": "boolean"},
                    "stderr": {"type": "string"},
                    "stderr_bytes": {"type": "integer"},
                    "stderr_truncated": {"type": "boolean"},
                    "scheme": {"type": "string"},
                    "receipt_dir": {"type": "string"},
                    "intent_path": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": "privileged",
            "resource_scope": ["target.exec"],
            "required_grants": [CAPABILITY_ID],
        },
        source=f"<{CAPABILITY_ID} descriptor>",
    )


def request_from_payload(
    payload: object,
    *,
    op_id: uuid.UUID,
    server_id: uuid.UUID,
    default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    default_output_budget: int = DEFAULT_OUTPUT_BUDGET_BYTES,
) -> CommandRequest:
    """Build the bounded-lane request from one operation payload (pure).

    The payload is exactly the approval-bound object (C03 rule 4): the
    canonical hash the decision was created for covers every field here,
    so an altered command can never ride on an old decision. ``command``
    is the raw shell string (the explicit raw-shell request); the target
    comes from the context, never from the payload. Deeper field bounds
    are the CommandRequest constructor's rejections, wrapped into this
    module's value-free error.

    Raises:
        HostShellError: ``invalid_request`` for any shape violation —
            before any state read or transport attempt.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            HostShellErrorCode.INVALID_REQUEST, "payload must be a JSON object"
        )
    unknown = sorted(str(key) for key in payload if key not in _PAYLOAD_FIELDS)
    if unknown:
        raise _reject(
            HostShellErrorCode.INVALID_REQUEST,
            f"unknown payload field {unknown[0]!r}",
        )
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise _reject(
            HostShellErrorCode.INVALID_REQUEST,
            "command must be a non-empty shell string",
        )
    try:
        return CommandRequest(
            op_id=op_id,
            server_id=server_id,
            display=DISPLAY_LABEL,
            timeout=payload.get("timeout", default_timeout),
            output_budget=payload.get("output_budget", default_output_budget),
            shell=command,
            working_directory=payload.get("working_directory"),
        )
    except CommandRequestError as error:
        raise _reject(
            HostShellErrorCode.INVALID_REQUEST,
            f"command request rejected ({error.detail})",
        ) from None


#: Current canonical profile source (host-composed; re-read per effect).
type ProfileProvider = Callable[[uuid.UUID], ServerProfile]
#: The enrolled known-hosts store for one server (host-composed; G2's
#: pinned host key lives there — an unenrolled target refuses here).
type KnownHostsProvider = Callable[[uuid.UUID], os.PathLike[str] | str]


class HostShellCapability:
    """The dispatcher-facing raw-shell executor over the supervisor lane.

    Composition is constructor-checked (fail-closed wiring, the sibling
    pattern): the host supplies the current profile source, the enrolled
    known-hosts store, the target receipt root, the controller intent
    directory and the bounded defaults. Nothing is cached — profile and
    enrollment store are consulted per effect, so a revoked enrollment
    cannot ride on a warm capability object.

    The instance is the dispatcher's ``executor`` (an ``async`` callable
    of :class:`~mai.capabilities.dispatch.CapabilityCall` returning
    :class:`~mai.capabilities.dispatch.ExecutorResult`); it performs no
    policy decision of its own — every grant/approval question was
    answered before invocation, and this class deliberately has no API
    that could bypass that order.
    """

    def __init__(
        self,
        *,
        profiles: ProfileProvider,
        known_hosts: KnownHostsProvider,
        receipt_root: str,
        intents_dir: os.PathLike[str] | str,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_output_budget: int = DEFAULT_OUTPUT_BUDGET_BYTES,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        if not callable(profiles):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT, "profiles must be a callable"
            )
        if not callable(known_hosts):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT, "known_hosts must be a callable"
            )
        if (
            not isinstance(receipt_root, str)
            or not receipt_root.startswith("/")
            or not receipt_root.isprintable()
            or "\x00" in receipt_root
            or len(receipt_root) > 4096
        ):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                "receipt_root must be an absolute target-side path "
                "(printable single line, at most 4096 characters)",
            )
        try:
            intents = os.fspath(intents_dir)
        except TypeError:
            intents = ""
        if not intents:
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                "intents_dir must be a non-empty path",
            )
        if (
            isinstance(default_timeout, bool)
            or not isinstance(default_timeout, (int, float))
            or not 0 < default_timeout <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                f"default_timeout must be a number in "
                f"(0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds",
            )
        if (
            isinstance(default_output_budget, bool)
            or not isinstance(default_output_budget, int)
            or not 0 < default_output_budget <= MAX_OUTPUT_BUDGET_BYTES
        ):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                f"default_output_budget must be an int in "
                f"(0, {MAX_OUTPUT_BUDGET_BYTES}] bytes",
            )
        if (
            isinstance(settle_seconds, bool)
            or not isinstance(settle_seconds, (int, float))
            or not 0 <= settle_seconds <= MAX_SETTLE_SECONDS
        ):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                f"settle_seconds must be a number in [0, {MAX_SETTLE_SECONDS}] seconds",
            )
        self._profiles = profiles
        self._known_hosts = known_hosts
        self._receipt_root = receipt_root
        self._intents_dir = intents
        self._timeout = default_timeout
        self._budget = default_output_budget
        self._settle = settle_seconds

    def __repr__(self) -> str:
        return f"<HostShellCapability {CAPABILITY_ID} settle={self._settle}>"

    # -- the dispatcher seam -------------------------------------------------

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see module docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                HostShellErrorCode.INVALID_CONTEXT,
                "call context must be an ExecutionContext",
            )
        profile = self._profile_for(context.server_id)
        key_path = self._key_path_for(call, profile)
        known_hosts = self._known_hosts_for(context.server_id)
        request = request_from_payload(
            call.payload,
            op_id=call.op_id,
            server_id=context.server_id,
            default_timeout=self._timeout,
            default_output_budget=self._budget,
        )
        # The frozen system-ssh lane blocks; the password channel card
        # fixed the composition duty: run it off the host event loop.
        return await asyncio.to_thread(
            self._supervised_roundtrip, profile, request, key_path, known_hosts
        )

    # -- pre-effect wiring checks (value-free, fail-closed) -------------------

    def _profile_for(self, server_id: uuid.UUID) -> ServerProfile:
        """Current canonical profile for the context's target."""
        try:
            profile = self._profiles(server_id)
        except HostShellError:
            raise
        except Exception as error:
            raise _reject(
                HostShellErrorCode.PROFILE_UNAVAILABLE,
                f"profile provider failed ({type(error).__name__})",
            ) from None
        if not isinstance(profile, ServerProfile):
            raise _reject(
                HostShellErrorCode.PROFILE_UNAVAILABLE,
                "profile provider returned no ServerProfile",
            )
        if profile.server_id != server_id:
            raise _reject(
                HostShellErrorCode.PROFILE_MISMATCH,
                "profile identity does not match the execution context server_id",
            )
        if profile.environment is not ExecutionEnvironment.LINUX_SSH:
            # Tripwire for the frozen environment vocabulary: unreachable
            # while ``linux_ssh`` is its only member; a second member must
            # route through a different capability, never through here.
            raise _reject(
                HostShellErrorCode.ENVIRONMENT_UNSUPPORTED,
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
                HostShellErrorCode.CREDENTIAL_UNAVAILABLE,
                f"execution context carries no resolved value for "
                f"credential reference {reference!r}",
            )
        key_path = Path(secret.value)
        if not key_path.is_file():
            raise _reject(
                HostShellErrorCode.KEY_FILE_UNAVAILABLE,
                f"credential reference {reference!r} does not resolve to "
                "an existing key file",
            )
        return key_path

    def _known_hosts_for(self, server_id: uuid.UUID) -> Path:
        """Enrolled known-hosts file for the target (G2 pinned host key)."""
        try:
            path = Path(os.fspath(self._known_hosts(server_id)))
        except HostShellError:
            raise
        except Exception as error:
            raise _reject(
                HostShellErrorCode.HOST_KEY_UNENROLLED,
                f"known-hosts provider failed ({type(error).__name__})",
            ) from None
        if not path.is_file():
            raise _reject(
                HostShellErrorCode.HOST_KEY_UNENROLLED,
                "no enrolled known-hosts file for this server; pin the "
                "host key explicitly before dispatch (never auto-trust)",
            )
        return path

    # -- effect: the frozen supervisor lane -----------------------------------

    def _supervised_roundtrip(
        self,
        profile: ServerProfile,
        request: CommandRequest,
        key_path: Path,
        known_hosts: Path,
    ) -> ExecutorResult:
        """Submit, settle, report — the blocking body of one capability
        call (runs in a worker thread). Raises from the lane propagate:
        a transport refusal before the launch is an unproven effect, and
        the receipt contract owns everything after it."""
        identity = profile.ssh
        handle = submit_supervised_command(
            identity,
            request,
            profile.privilege,
            receipt_root=self._receipt_root,
            intents_dir=self._intents_dir,
            key_path=key_path,
            known_hosts_path=known_hosts,
            timeout=DEFAULT_PROBE_TIMEOUT,
        )
        report = self._inspect(identity, handle, key_path, known_hosts)
        deadline = time.monotonic() + self._settle
        while report.status not in _TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(SETTLE_POLL_SECONDS)
            report = self._inspect(identity, handle, key_path, known_hosts)
        document = self._result_document(handle, report, profile)
        return ExecutorResult(
            ok=report.status is CommandStatus.SUCCEEDED,
            output=json.dumps(document, sort_keys=True),
        )

    def _inspect(
        self,
        identity: SshIdentity,
        handle: SupervisionHandle,
        key_path: Path,
        known_hosts: Path,
    ) -> SupervisionReport:
        return inspect_supervised_command(
            identity,
            handle.intent,
            key_path=key_path,
            known_hosts_path=known_hosts,
            output_budget=self._budget,
            timeout=DEFAULT_PROBE_TIMEOUT,
        )

    def _result_document(
        self,
        handle: SupervisionHandle,
        report: SupervisionReport,
        profile: ServerProfile,
    ) -> dict[str, object]:
        """The safe result document: outcome AND evidence (acceptance 2)."""
        op_id = str(report.op_id)
        stdout = report.stdout
        stdout_truncated = report.stdout_truncated
        raw_stdout = stdout.encode("utf-8")
        if len(raw_stdout) > MAX_RESULT_STREAM_BYTES:
            stdout = raw_stdout[:MAX_RESULT_STREAM_BYTES].decode(
                "utf-8", errors="ignore"
            )
            stdout_truncated = True
        stderr = report.stderr
        stderr_truncated = report.stderr_truncated
        raw_stderr = stderr.encode("utf-8")
        if len(raw_stderr) > MAX_RESULT_STREAM_BYTES:
            stderr = raw_stderr[:MAX_RESULT_STREAM_BYTES].decode(
                "utf-8", errors="ignore"
            )
            stderr_truncated = True
        doc = {
            "capability": CAPABILITY_ID,
            "op_id": op_id,
            "server_id": str(report.server_id),
            "display": report.display,
            "privilege_mode": profile.privilege.value,
            "scheme": report.scheme.value,
            "status": report.status.value,
            "exit_code": report.exit_code,
            "stdout": stdout,
            "stdout_bytes": report.stdout_bytes,
            "stdout_truncated": stdout_truncated,
            "stderr": stderr,
            "stderr_bytes": report.stderr_bytes,
            "stderr_truncated": stderr_truncated,
            "receipt_dir": f"{self._receipt_root}/{op_id}",
            "receipt_seq": report.state_seq,
            "receipt_ts": report.state_ts,
            "receipt_boot_id": report.receipt_boot_id,
            "observed_boot_id": report.current_boot_id,
            "boot_changed": report.boot_changed,
            "identity_resolved": report.identity_resolved,
            "heartbeat_age_seconds": report.heartbeat_age,
            "notes": list(report.notes),
            "events_tail": report.events_tail[-MAX_EVENTS_TAIL_CHARS:],
            "intent_path": str(handle.intent_path),
        }
        # Guarantee that the JSON-escaped document stays strictly under the
        # dispatcher's 64 KiB output budget even when control characters,
        # quotes, or non-ASCII bytes expand under json.dumps.
        max_json_bytes = DEFAULT_OUTPUT_BUDGET_BYTES - 2048
        while len(json.dumps(doc, sort_keys=True).encode("utf-8")) > max_json_bytes:
            cur_out = str(doc["stdout"]).encode("utf-8")
            cur_err = str(doc["stderr"]).encode("utf-8")
            if len(cur_out) >= len(cur_err) and len(cur_out) > 0:
                doc["stdout"] = cur_out[: max(0, len(cur_out) // 2)].decode(
                    "utf-8", errors="ignore"
                )
                doc["stdout_truncated"] = True
            elif len(cur_err) > 0:
                doc["stderr"] = cur_err[: max(0, len(cur_err) // 2)].decode(
                    "utf-8", errors="ignore"
                )
                doc["stderr_truncated"] = True
            else:
                break
        return doc
