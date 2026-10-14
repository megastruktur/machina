"""Durable supervision of long-running remote commands across SSH loss.

Card ``04-supervisor`` against contracts.md **C04** and the host-supervision
ADR (``docs/architecture/host-supervision.md``, P00 measured protocol):

- **Ownership** (host rule 1) — command ownership is a target-local
  supervision wrapper plus a durable receipt directory established over
  SSH *before* the effect; the SSH session is never the owner. Dispatch
  intent is persisted controller-side before submission (C04 rule 5), so a
  lost channel loses neither the operation nor its handle: the next
  session re-inspects from receipts alone (measured exp1b).
- **Scheme selection** (host rule 6) — the target capability is probed per
  operation: ``/proc/1/comm == systemd`` plus a resolvable ``systemctl``
  ⇒ the receipt discipline is carried by a ``systemd-run --user``
  transient unit; otherwise the POSIX-sh wrapper variant is detached via
  ``setsid`` — the scheme the P00 spike measured. Both variants run the
  SAME wrapper (one receipt contract); they differ only in how the wrapper
  is launched and kept alive. The systemd variant is boundary-verified
  against a disposable systemd-capable Arch target in
  ``backend/tests/test_supervisor.py`` (registry row SSH-SYSV1); on
  targets whose user manager is unavailable the launch fails BEFORE any
  effect (transient-unit start is synchronous), the intent stays persisted
  with no receipt, and inspection reports ``outcome_unknown`` — never an
  invented status. Redelivery under the wrapper scheme on such a target is
  a deliberate caller decision (engine policy), never an automatic retry
  (host rule 3).
- **Receipt contract** (frozen verbatim in C04) — per operation one
  directory under the profile's durable ``receipt_root``: ``state``
  (atomically replaced; keys ``op_id``, ``seq``, ``state ∈
  starting|running|exited``, ``ts``, ``boot_id``, ``wrapper_pid``,
  ``payload_pid``, ``payload_startticks``, ``exit_code`` on ``exited``
  only), ``events.log`` (append-only transitions, per-second
  ``hb payload_pid=…`` heartbeats carrying an ``hb_epoch`` wall-clock
  field for skew-free freshness, and ``note …`` lines for
  controller-recorded events such as cancellation requests),
  ``stdout.log``/``stderr.log``. A fourth, implementation-owned file
  ``identity`` (written by the payload session before the command runs)
  bootstraps the identity guard race-free; it feeds the frozen ``state``
  keys and nothing parses it downstream. The receipt never says
  ``cancelled`` — it records the actual exit (e.g. 143);
  requested-vs-verified cancellation is controller state.
- **Identity rule** — a receipt names a live process only if
  ``/proc/<payload_pid>/stat`` exists AND field-22 start ticks equal
  ``payload_startticks`` (PID-reuse guard; measured negative control
  exp2). Descendants are enumerated by ppid closure from the payload pid
  (host rule 7) for identification and post-cancel verification.
- **Status mapping** (C04 rule 4, frozen vocabulary from
  :class:`mai.execution.commands.CommandStatus`) — ``exited``+0 ⇒
  ``succeeded``; ``exited``+≠0 ⇒ ``failed``; ``running`` + fresh heartbeat
  + resolving identity ⇒ ``running``; ``running`` with a stale heartbeat
  or unresolved identity ⇒ ``outcome_unknown``; a missing or
  contract-violating receipt ⇒ ``outcome_unknown`` (an effect can precede
  its receipt — never retry-safe); a restart-suspected classification is
  DERIVED (receipt boot id ≠ observed boot id plus unresolved identity —
  host rule 4), never hardcoded. ``cancel_requested`` is recorded by the
  controller until host-verified ``cancelled``; this module reports
  ``cancelled`` only after the whole ppid closure is verified gone AND the
  receipt carries the actual exit record (C04 rule 6).
- **Cancellation** (host rule 2, SSH-CANCEL flow) — verify-then-signal:
  identity guard first, then ``kill -TERM -<payload pgid>`` against the
  payload's own session group (the payload is a ``setsid`` session leader
  in BOTH variants, so the signal path is identical and measured), bounded
  KILL escalation, ppid-closure emptiness verified, and only then the
  receipt's exit record (143) is reported as ``cancelled``. A refused
  guard (stale/forged ticks) raises :class:`CancelRefusedError` and
  signals nothing. A supervisor that died before recording the exit leaves
  the outcome ``outcome_unknown`` forever (measured exp3); repair is the
  explicit reconciliation operation (card ``04-reconcile``).
- **Explicit output retention** — the target-side logs are the record;
  inspection reads at most the caller's per-stream budget (first-N-bytes,
  UTF-8 with replacement) together with the true ``wc -c`` totals, and
  ``*_truncated`` marks a cut, so truncation is never silent — the same
  retention semantics as the bounded lane. Log sections travel behind
  markers keyed with a target-side random nonce, so payload output cannot
  re-anchor the parser's section boundaries.

Boundary: this module COMPOSES the frozen lanes without modifying them —
the supervised payload is exactly
:func:`mai.execution.privilege.privileged_remote_command` output for the
profile privilege mode (``none`` verbatim payload, ``sudo`` the
noninteractive escalation, ``root`` verbatim), so element-wise quoting,
the cwd guard, budgets and the transport vocabulary apply unchanged.
There is deliberately NO password input here: detached supervision cannot
carry an interactive sudo password without persisting secret material in
the wrapper or argv (C04 rule 2) — passworded escalation belongs to the
approval-gated synchronous lane (card ``04-password-channel``). Secret
material cannot enter requests, wrappers, receipts or outcomes of this
module. Known limitation (recorded in the evidence): the target-side logs
grow with payload output while the payload runs (bounded by the wall-clock
budget of the driver); bounding at the target is log policy, out of scope
here — the controller never reads more than its budget.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.execution.commands import (
    MAX_OUTPUT_BUDGET_BYTES,
    CommandRequest,
    CommandStatus,
    compose_remote_command,
)
from mai.execution.privilege import privileged_remote_command
from mai.execution.ssh import SshConnectError, SshErrorCode, ssh_arguments

__all__ = [
    "DEFAULT_INSPECT_BUDGET_BYTES",
    "DEFAULT_PROBE_TIMEOUT",
    "EVENTS_TAIL_BYTES",
    "HEARTBEAT_STALE_SECONDS",
    "CancelRefusedError",
    "CancelReport",
    "ProbeSupervisionScheme",
    "SupervisionErrorCode",
    "SupervisionHandle",
    "SupervisionIntent",
    "SupervisionScheme",
    "SupervisionReport",
    "cancel_supervised_command",
    "compose_install_command",
    "compose_inspect_command",
    "compose_launch_command",
    "compose_wrapper_script",
    "inspect_supervised_command",
    "load_intent",
    "map_receipt_status",
    "parse_inspect_output",
    "parse_state_file",
    "probe_supervision_scheme",
    "submit_supervised_command",
]


class SupervisionErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_REQUEST = "invalid_request"
    CANCEL_REFUSED = "cancel_refused"


class CancelRefusedError(ValueError):
    """Verify-then-signal refusal: the receipt's identity does not match.

    Raised BEFORE any signal when ``/proc/<payload_pid>`` is absent or its
    field-22 start ticks differ from ``payload_startticks`` — a stale
    receipt or a reused PID (measured exp2 negative control). ``detail``
    is value-free and bounded.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"{SupervisionErrorCode.CANCEL_REFUSED.value}: {detail}")
        self.code = SupervisionErrorCode.CANCEL_REFUSED
        self.detail = detail


class SupervisionRequestError(ValueError):
    """One structured, value-free request rejection (C01 envelope shape)."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{SupervisionErrorCode.INVALID_REQUEST.value}: {detail}")
        self.code = SupervisionErrorCode.INVALID_REQUEST
        self.detail = detail


# ---------------------------------------------------------------------------
# Frozen protocol constants
# ---------------------------------------------------------------------------

#: Heartbeat freshness policy (adapter-owned per the ADR limitations):
#: the wrapper beats once per second; more than this many missed seconds
#: marks the heartbeat stale ⇒ ``outcome_unknown``.
HEARTBEAT_STALE_SECONDS = 5.0

#: Events-log tail read back by one inspection (bounded).
EVENTS_TAIL_BYTES = 4096

#: Default per-stream retention budget for inspection reads.
DEFAULT_INSPECT_BUDGET_BYTES = 64 * 1024

#: Bound on the raw inspect probe response before it is parsed (two
#: budgeted log reads plus bounded fixed sections; anything larger is a
#: hostile or broken target — the response is refused, never parsed).
MAX_INSPECT_RESPONSE_BYTES = 2 * MAX_OUTPUT_BUDGET_BYTES + 262144

#: Default wall-clock budget for one controller-side ssh round trip.
DEFAULT_PROBE_TIMEOUT = 30.0

#: Cancellation escalation timings (measured exp2 shape: TERM grace, then
#: KILL, then settle before the closure re-check).
TERM_GRACE_SECONDS = 6.0
KILL_SETTLE_SECONDS = 1.0

#: How long cancel waits for the wrapper to record the exit receipt after
#: the closure is verified empty (heartbeat granularity is 1 s).
EXIT_RECEIPT_WAIT_SECONDS = 10.0

#: ``state`` file keys frozen by contracts.md C04; anything else is a
#: receipt-contract violation and degrades the classification.
_STATE_KEYS = frozenset(
    {
        "op_id",
        "seq",
        "state",
        "ts",
        "boot_id",
        "wrapper_pid",
        "payload_pid",
        "payload_startticks",
        "exit_code",
    }
)
_RECEIPT_STATES = frozenset({"starting", "running", "exited"})

_INSPECT_MARKERS = (
    "BOOT",
    "STATE",
    "EVENTS",
    "SIZES",
    "STDOUT",
    "STDERR",
    "IDENT",
    "PROCS",
)


def _script_path(op_id: uuid.UUID) -> str:
    """Controller-local wrapper script path per operation (disposable)."""
    return f"/tmp/mai-supervisor-{op_id.hex}.sh"


def _unit_name(op_id: uuid.UUID) -> str:
    """systemd transient unit name per operation (user-manager scoped)."""
    return f"mai-op-{op_id.hex[:32]}"


def _reject(detail: str) -> None:
    raise SupervisionRequestError(detail)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Scheme probe (host rule 6: per-target capability, probed per operation)
# ---------------------------------------------------------------------------


class SupervisionScheme(StrEnum):
    """How the target carries the receipt discipline (host rule 6)."""

    SYSTEMD_UNIT = "systemd-unit"
    WRAPPER = "wrapper"


@dataclass(frozen=True, slots=True)
class ProbeSupervisionScheme:
    """Observed scheme capability plus the boot identity at probe time."""

    scheme: SupervisionScheme
    pid1_comm: str
    boot_id: str


SCHEME_PROBE_COMMAND = (
    "cat /proc/1/comm 2>/dev/null; "
    "printf '\\n---\\n'; "
    "command -v systemctl 2>/dev/null; "
    "printf '\\n---\\n'; "
    "cat /proc/sys/kernel/random/boot_id 2>/dev/null"
)


def parse_scheme_output(output: str) -> ProbeSupervisionScheme:
    """Parse the fixed scheme-probe output (pure; read-only facts only).

    Probe form: ``/proc/1/comm``, ``---``, ``command -v systemctl``,
    ``---``, ``/proc/sys/kernel/random/boot_id``. The transient-unit
    scheme is selected only when PID 1 IS systemd and ``systemctl``
    resolves; anything else (busybox init, bash entrypoint, no systemctl)
    selects the measured wrapper variant.
    """
    sections = output.split("\n---\n")
    comm = sections[0].strip() if sections else ""
    systemctl = sections[1].strip() if len(sections) > 1 else ""
    boot_id = sections[2].strip() if len(sections) > 2 else ""
    scheme = (
        SupervisionScheme.SYSTEMD_UNIT
        if comm == "systemd" and systemctl
        else SupervisionScheme.WRAPPER
    )
    return ProbeSupervisionScheme(scheme=scheme, pid1_comm=comm, boot_id=boot_id)


def probe_supervision_scheme(
    identity: SshIdentity,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ProbeSupervisionScheme:
    """One fixed, read-only ssh probe of the target's scheme capability."""
    proc = _run_ssh(
        identity,
        SCHEME_PROBE_COMMAND,
        key_path=os.fspath(key_path),
        known_hosts_path=os.fspath(known_hosts_path),
        timeout=timeout,
    )
    return parse_scheme_output(proc.stdout)


# ---------------------------------------------------------------------------
# Dispatch intent (C04 rule 5: persisted BEFORE submission)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SupervisionIntent:
    """Controller-side dispatch intent; the durable handle after loss.

    Persisted as JSON under the caller's ``intents_dir`` before any
    remote effect, then atomically re-replaced once the launch is
    acknowledged. Carries everything a NEW session needs to re-inspect
    the operation from target receipts alone: opaque ids, the safe
    display label, the selected scheme, the profile receipt root, the
    endpoint identity and the payload digest (SHA-256 of the composed
    remote payload — a tamper-evidence reference; the payload itself is
    secret-free by construction of the frozen lanes).
    ``boot_id`` is the observed kernel boot id at dispatch time —
    recorded, while restart CLASSIFICATION stays derived from receipts
    (host rule 4). An empty ``boot_id`` means the scheme was supplied by
    the caller and no probe round trip ran.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    scheme: SupervisionScheme
    privilege_mode: str
    receipt_root: str
    host: str
    port: int
    user: str
    payload_sha256: str
    dispatch_ts: str
    boot_id: str
    status: str  # "intent" before launch, "submitted" after the ack

    def to_json(self) -> str:
        return json.dumps(
            {
                "op_id": str(self.op_id),
                "server_id": str(self.server_id),
                "display": self.display,
                "scheme": self.scheme.value,
                "privilege_mode": self.privilege_mode,
                "receipt_root": self.receipt_root,
                "host": self.host,
                "port": self.port,
                "user": self.user,
                "payload_sha256": self.payload_sha256,
                "dispatch_ts": self.dispatch_ts,
                "boot_id": self.boot_id,
                "status": self.status,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> SupervisionIntent:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            _reject(f"intent file is not valid JSON ({error.msg})")
            raise  # pragma: no cover - _reject always raises
        try:
            return cls(
                op_id=uuid.UUID(raw["op_id"]),
                server_id=uuid.UUID(raw["server_id"]),
                display=str(raw["display"]),
                scheme=SupervisionScheme(raw["scheme"]),
                privilege_mode=str(raw["privilege_mode"]),
                receipt_root=str(raw["receipt_root"]),
                host=str(raw["host"]),
                port=int(raw["port"]),
                user=str(raw["user"]),
                payload_sha256=str(raw["payload_sha256"]),
                dispatch_ts=str(raw["dispatch_ts"]),
                boot_id=str(raw["boot_id"]),
                status=str(raw["status"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            _reject(f"intent file has an invalid shape ({error})")
            raise  # pragma: no cover - _reject always raises

    def save(self, intents_dir: os.PathLike[str] | str) -> Path:
        """Atomically persist (tmp file + ``os.replace``)."""
        directory = Path(intents_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"intent-{self.op_id.hex}.json"
        tmp = directory / f".intent-{self.op_id.hex}.tmp"
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)
        return path

    @property
    def script_path(self) -> str:
        return _script_path(self.op_id)

    @property
    def unit_name(self) -> str:
        return _unit_name(self.op_id)


@dataclass(frozen=True, slots=True)
class SupervisionHandle:
    """What the submitter keeps: the persisted intent plus its path."""

    intent: SupervisionIntent
    intent_path: str


def load_intent(
    intents_dir: os.PathLike[str] | str, op_id: uuid.UUID
) -> SupervisionIntent:
    """Reload a persisted intent for re-inspection from a new session."""
    path = Path(intents_dir) / f"intent-{op_id.hex}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _reject(f"no persisted intent for op {op_id} under {intents_dir}")
        raise  # pragma: no cover - _reject always raises
    return SupervisionIntent.from_json(text)


# ---------------------------------------------------------------------------
# Target-side wrapper — one receipt discipline, both schemes (host rule 6)
# ---------------------------------------------------------------------------


def compose_wrapper_script(intent: SupervisionIntent, payload: str) -> str:
    """The target-local supervision wrapper (pure; measured P00 form).

    Owns state transitions, payload identity (pid + /proc start ticks),
    the per-second heartbeat and exit capture. The payload runs in its
    OWN session (inner ``setsid``) in BOTH schemes, so cancellation can
    TERM the payload session group without killing the wrapper (measured
    exp2), and wrapper death orphans instead of signalling (measured
    exp3 ⇒ ``outcome_unknown``). The payload is embedded base64-encoded
    and decoded once on the target: no quoting round-trip can reinterpret
    it, and no secret material ever passes through this script by
    construction (see module docstring). The ``state`` file is atomically
    replaced (``state.tmp`` + ``mv``) with exactly the frozen C04 keys;
    ``exit_code`` carries its value only once ``state=exited``.

    Identity bootstrap: the payload session records its OWN ``pid`` and
    field-22 start ticks (``/proc/self/stat``) into ``$OD/identity``
    BEFORE the command runs, and the wrapper reads that file with a
    bounded retry. Reading ``/proc/<pid>/stat`` from the parent races a
    fast payload (measured in this card's evidence: a millisecond payload
    exited before the parent's awk ran, leaving the receipt without an
    identity guard); capturing from inside the session is race-free. If
    the identity file never appears, the receipt honestly carries empty
    identity values — which the frozen mapping treats as stale ⇒
    ``outcome_unknown`` (C04 bad-example rule), never an invented exit.
    """
    op = str(intent.op_id)
    root = intent.receipt_root
    inner = (
        "printf 'payload_pid=%s\\npayload_startticks=%s\\n' \"$$\" "
        '"$(awk \'{print $22}\' "/proc/$$/stat" 2>/dev/null)" '
        '> "$MAI_OD/identity" 2>/dev/null\n' + payload + "\n"
    )
    inner_b64 = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    return f"""#!/bin/sh
# mai supervision wrapper v1 (mai-04-supervisor) op={op}
OD="{root}/{op}"
OP="{op}"
INNER=$(printf %s '{inner_b64}' | base64 -d)
mkdir -p "$OD" || exit 97
now() {{ date -u +%Y-%m-%dT%H:%M:%SZ; }}
SEQ=0
emit() {{
  SEQ=$((SEQ+1))
  {{
    printf 'op_id=%s\\n' "$OP"
    printf 'seq=%s\\n' "$SEQ"
    printf 'state=%s\\n' "$1"
    printf 'ts=%s\\n' "$(now)"
    printf 'boot_id=%s\\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
    printf 'wrapper_pid=%s\\n' "$$"
    printf 'payload_pid=%s\\n' "${{PAYLOAD_PID:-}}"
    printf 'payload_startticks=%s\\n' "${{PAYLOAD_TICKS:-}}"
    printf 'exit_code=%s\\n' "${{EXIT_CODE:-}}"
  }} > "$OD/state.tmp" && mv "$OD/state.tmp" "$OD/state"
  printf '%s seq=%s state=%s\\n' "$(now)" "$SEQ" "$1" >> "$OD/events.log"
}}
emit starting
MAI_OD="$OD" setsid sh -c "$INNER" </dev/null >"$OD/stdout.log" 2>"$OD/stderr.log" &
PAYLOAD_PID=$!
PAYLOAD_TICKS=""
i=0
while [ $i -lt 100 ]; do
  if [ -s "$OD/identity" ]; then
    FILE_PID=$(awk -F= '$1=="payload_pid"{{print $2}}' "$OD/identity" 2>/dev/null)
    PAYLOAD_TICKS=$(awk -F= '$1=="payload_startticks"{{print $2}}' "$OD/identity" 2>/dev/null)
    if [ -n "$PAYLOAD_TICKS" ]; then
      # the self-reported pair is ONE consistent /proc/self read: trust it
      # for BOTH values ($! is only the spawned pid and can diverge from
      # the session under managers that fork the setsid chain)
      [ -n "$FILE_PID" ] && PAYLOAD_PID=$FILE_PID
      break
    fi
  fi
  kill -0 "$PAYLOAD_PID" 2>/dev/null || break
  sleep 0.05
  i=$((i+1))
done
emit running
while kill -0 "$PAYLOAD_PID" 2>/dev/null; do
  printf '%s hb payload_pid=%s hb_epoch=%s\\n' \\
    "$(now)" "$PAYLOAD_PID" "$(date -u +%s)" >> "$OD/events.log"
  sleep 1
done
wait "$PAYLOAD_PID"; EXIT_CODE=$?
emit exited
"""


# ---------------------------------------------------------------------------
# Transport composition (pure) — install + launch per scheme
# ---------------------------------------------------------------------------


def compose_install_command(intent: SupervisionIntent) -> str:
    """Synchronous, rc-checked wrapper install over one ssh session.

    Creates the receipt directory first and proves it writable, so an
    unusable receipt root is a structured pre-effect rejection rather
    than an unownable effect.
    """
    op_dir = shlex.quote(f"{intent.receipt_root}/{intent.op_id}")
    script = shlex.quote(intent.script_path)
    return (
        f"mkdir -p {op_dir} "
        f"&& [ -w {op_dir} ] "
        f"&& cat > {script} "
        f"&& chmod 700 {script} "
        f"&& echo MAI-INSTALLED"
    )


def compose_launch_command(intent: SupervisionIntent) -> str:
    """Launch the installed wrapper detached from the SSH session.

    ``wrapper`` — the measured exp1b dispatch: ``setsid`` detaches the
    wrapper into its own session; the ack line proves the shell reached
    the launch before the channel could die.

    ``systemd-unit`` — a ``systemd-run --user`` transient unit owns the
    wrapper; the user manager (linger or an active session) outlives the
    ssh channel. ``XDG_RUNTIME_DIR`` is defaulted the way PAM would set
    it, so the launch also works on targets whose sshd does not export
    it.
    """
    script = shlex.quote(intent.script_path)
    if intent.scheme is SupervisionScheme.SYSTEMD_UNIT:
        unit = shlex.quote(intent.unit_name)
        return (
            'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
            "export XDG_RUNTIME_DIR; "
            f"systemd-run --user --unit={unit} --collect --quiet "
            f"/bin/sh {script} && echo MAI-LAUNCHED"
        )
    return f"setsid /bin/sh {script} >/dev/null 2>&1 </dev/null & echo MAI-LAUNCHED"


# ---------------------------------------------------------------------------
# Receipt parsing and the frozen C04 status mapping
# ---------------------------------------------------------------------------


def parse_state_file(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse one receipt ``state`` file (pure).

    Returns ``(fields, problems)``: strict ``key=value`` lines, unknown
    keys and contract violations (a valued ``exit_code`` outside
    ``exited``, the forbidden ``cancelled`` state, an exited receipt
    without an exit record or identity) are reported as value-free
    problems — a violating receipt degrades to ``outcome_unknown``, never
    to an invented status. The wrapper emits EMPTY values for the
    not-yet-known keys (``payload_pid`` before identification,
    ``exit_code`` before exit); an empty value is absent data, matching
    the C04 good example, not the ``exit_code``-present violation.
    """
    fields: dict[str, str] = {}
    problems: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or not key:
            problems.append("malformed state line")
            continue
        if key not in _STATE_KEYS:
            problems.append("unknown state key")
            continue
        fields[key] = value
    state = fields.get("state", "")
    if state and state not in _RECEIPT_STATES:
        problems.append("state value outside the frozen vocabulary")
    if fields.get("exit_code", "") and state != "exited":
        problems.append("exit_code present without state=exited")
    if state == "exited" and not fields.get("exit_code", ""):
        problems.append("exited receipt without exit_code")
    if state == "exited" and not fields.get("payload_startticks", ""):
        problems.append("exited receipt without payload identity")
    return fields, tuple(problems)


@dataclass(frozen=True, slots=True)
class _ProcsRow:
    """One ``ps -o pid,ppid,pgid,sid,args`` row (pgid/sid unused today)."""

    pid: int
    ppid: int
    args: str


def parse_proc_table(text: str) -> tuple[_ProcsRow, ...]:
    """Parse ``ps -o pid,ppid,pgid,sid,args`` output (pure)."""
    rows: list[_ProcsRow] = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append(_ProcsRow(pid=int(parts[0]), ppid=int(parts[1]), args=parts[4]))
    return tuple(rows)


def descendants(rows: tuple[_ProcsRow, ...], root: int) -> tuple[_ProcsRow, ...]:
    """ppid closure strictly below ``root`` (host rule 7; measured)."""
    by_ppid: dict[int, list[_ProcsRow]] = {}
    for row in rows:
        by_ppid.setdefault(row.ppid, []).append(row)
    found: list[_ProcsRow] = []
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for child in by_ppid.get(current, []):
            found.append(child)
            frontier.append(child.pid)
    return tuple(found)


def _last_heartbeat_epoch(events_tail: str) -> float | None:
    matches = re.findall(r"hb payload_pid=\S+ hb_epoch=(\d+)", events_tail)
    return float(matches[-1]) if matches else None


def map_receipt_status(
    fields: dict[str, str],
    problems: tuple[str, ...],
    *,
    receipt_missing: bool,
    identity_resolved: bool,
    heartbeat_age: float | None,
    heartbeat_ttl: float,
    boot_changed: bool,
) -> tuple[CommandStatus, tuple[str, ...]]:
    """The frozen C04 mapping from receipt facts to outcome status (pure).

    Never invents an exit: every unresolved boundary lands on
    ``outcome_unknown`` with value-free reasons; ``cancelled`` is never
    produced here (the receipt records the actual exit; cancellation
    verification lives in :func:`cancel_supervised_command`).
    """
    notes: list[str] = []
    if receipt_missing:
        return CommandStatus.OUTCOME_UNKNOWN, (
            "receipt_missing: an effect can precede its receipt",
        )
    if problems:
        return CommandStatus.OUTCOME_UNKNOWN, (
            *problems,
            "receipt violates the frozen contract",
        )
    state = fields.get("state", "")
    if state == "exited":
        exit_text = fields.get("exit_code", "")
        if not exit_text.lstrip("-").isdigit():
            return CommandStatus.OUTCOME_UNKNOWN, ("exit_code unreadable",)
        return (
            CommandStatus.SUCCEEDED if int(exit_text) == 0 else CommandStatus.FAILED
        ), ()
    if boot_changed:
        notes.append("boot identity changed since dispatch")
    if state == "starting":
        notes.append(
            "pre-identification state; supervisor death is not distinguishable"
        )
        return CommandStatus.OUTCOME_UNKNOWN, tuple(notes)
    # state == running
    if not identity_resolved:
        notes.append("payload identity unresolved (dead or reused pid)")
    if heartbeat_age is None:
        notes.append("no heartbeat observed")
    elif heartbeat_age > heartbeat_ttl:
        notes.append(f"heartbeat stale ({heartbeat_age:.0f}s)")
    if identity_resolved and not notes:
        return CommandStatus.RUNNING, ()
    if identity_resolved and notes == ["boot identity changed since dispatch"]:
        # Measured exp4a semantics: a live-looking pid across a boot-id
        # change cannot be trusted as progress — the restart classification
        # is derived, and the boot change alone forces unknown.
        return CommandStatus.OUTCOME_UNKNOWN, tuple(notes)
    return CommandStatus.OUTCOME_UNKNOWN, tuple(notes)


# ---------------------------------------------------------------------------
# Inspection probe — one read-only round trip, nonce-keyed sections
# ---------------------------------------------------------------------------


def compose_inspect_command(intent: SupervisionIntent, budget: int) -> str:
    """One bounded read-only probe of receipt, logs, identity and tree.

    Sections are delimited by markers keyed with a per-probe random
    nonce minted target-side from /dev/urandom, so payload output cannot
    re-anchor the parser's section boundaries. Identity is resolved
    REMOTELY with the frozen guard form (``/proc/<pid>/stat`` exists AND
    field-22 start ticks equal the receipt's), so identity, heartbeat
    freshness (``hb_epoch``), tree closure and output travel in ONE round
    trip. Every path is quoted; every read is bounded.
    """
    root = intent.receipt_root
    op = str(intent.op_id)
    state = shlex.quote(f"{root}/{op}/state")
    events = shlex.quote(f"{root}/{op}/events.log")
    stdout_log = shlex.quote(f"{root}/{op}/stdout.log")
    stderr_log = shlex.quote(f"{root}/{op}/stderr.log")
    return (
        "NONCE=$(dd if=/dev/urandom bs=8 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n'); "
        'm() { printf "__MAI_%s_%s__\\n" "$NONCE" "$1"; }; '
        "m BOOT; cat /proc/sys/kernel/random/boot_id 2>/dev/null; "
        f"m STATE; cat {state} 2>/dev/null; "
        f"m EVENTS; tail -c {EVENTS_TAIL_BYTES} {events} 2>/dev/null; "
        "m SIZES; "
        f"printf 'OUT=%s\\n' \"$(wc -c < {stdout_log} 2>/dev/null)\"; "
        f"printf 'ERR=%s\\n' \"$(wc -c < {stderr_log} 2>/dev/null)\"; "
        f"m STDOUT; head -c {int(budget)} {stdout_log} 2>/dev/null; "
        f"m STDERR; head -c {int(budget)} {stderr_log} 2>/dev/null; "
        "m IDENT; "
        f"PID=$(awk -F= '$1==\"payload_pid\"{{print $2}}' {state} 2>/dev/null); "
        f"TICKS=$(awk -F= '$1==\"payload_startticks\"{{print $2}}' {state} 2>/dev/null); "
        'if [ -n "$PID" ] && [ -r "/proc/$PID/stat" ]; then '
        "awk '{print $22}' \"/proc/$PID/stat\" 2>/dev/null; else echo NO-PROC; fi; "
        'printf "TICKS=%s\\n" "$TICKS"; '
        "m PROCS; ps -o pid,ppid,pgid,sid,args 2>/dev/null"
    )


def parse_inspect_output(output: str) -> dict[str, str]:
    """Split one probe response into its nonce-keyed sections (pure)."""
    match = re.search(r"__MAI_([0-9a-f]{8,64})_BOOT__", output)
    if match is None:
        return {}
    nonce = match.group(1)
    sections: dict[str, str] = {}
    pattern = re.compile(rf"__MAI_{nonce}_({'|'.join(_INSPECT_MARKERS)})__\n?")
    parts = pattern.split(output)
    # parts: [preamble, NAME, body, NAME, body, ...]
    for index in range(1, len(parts) - 1, 2):
        sections[parts[index]] = parts[index + 1]
    return sections


@dataclass(frozen=True, slots=True)
class SupervisionReport:
    """One reconnection inspection: backend view of a supervised operation.

    ``status`` follows the frozen C04 mapping; ``notes`` carry the
    value-free reasons whenever the status is ``outcome_unknown``.
    ``identity_resolved`` is the receipt-vs-/proc guard result,
    ``descendants`` the ppid closure below the payload, ``heartbeat_age``
    the seconds since the last receipt heartbeat, and the output fields
    use the bounded-lane retention semantics (first-N bytes kept, true
    totals, explicit truncation flags). ``boot_changed`` derives the
    restart signal (receipt boot id ≠ observed boot id — host rule 4).
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    scheme: SupervisionScheme
    status: CommandStatus
    receipt_missing: bool
    state_seq: str
    state_ts: str
    receipt_boot_id: str
    current_boot_id: str
    boot_changed: bool
    wrapper_pid: str
    payload_pid: str
    payload_startticks: str
    identity_resolved: bool
    heartbeat_age: float | None
    exit_code: int | None
    descendants: tuple[tuple[int, str], ...]
    stdout: str
    stdout_bytes: int
    stdout_truncated: bool
    stderr: str
    stderr_bytes: int
    stderr_truncated: bool
    events_tail: str
    notes: tuple[str, ...]


def _parse_sections_to_report(
    intent: SupervisionIntent,
    sections: dict[str, str],
    *,
    heartbeat_ttl: float,
) -> SupervisionReport:
    state_text = sections.get("STATE", "")
    receipt_missing = not state_text.strip()
    fields, problems = parse_state_file(state_text)
    current_boot = sections.get("BOOT", "").strip()
    receipt_boot = fields.get("boot_id", "")
    boot_changed = bool(current_boot and receipt_boot and current_boot != receipt_boot)

    ident_lines = sections.get("IDENT", "").splitlines()
    ticks_line = next((ln for ln in ident_lines if ln.startswith("TICKS=")), "")
    receipt_ticks = ticks_line.split("=", 1)[1].strip() if "=" in ticks_line else ""
    observed_ticks = next(
        (ln.strip() for ln in ident_lines if ln.strip().isdigit()), ""
    )
    identity_resolved = bool(
        observed_ticks and receipt_ticks and observed_ticks == receipt_ticks
    )

    events = sections.get("EVENTS", "")
    hb_epoch = _last_heartbeat_epoch(events)
    heartbeat_age = max(0.0, time.time() - hb_epoch) if hb_epoch is not None else None

    sizes_text = sections.get("SIZES", "")
    out_match = re.search(r"^OUT=(\d*)$", sizes_text, re.MULTILINE)
    err_match = re.search(r"^ERR=(\d*)$", sizes_text, re.MULTILINE)
    stdout_total = int(out_match.group(1)) if out_match and out_match.group(1) else 0
    stderr_total = int(err_match.group(1)) if err_match and err_match.group(1) else 0
    stdout_text = sections.get("STDOUT", "")
    stderr_text = sections.get("STDERR", "")

    exit_text = fields.get("exit_code", "")
    exit_code = int(exit_text) if exit_text.lstrip("-").isdigit() else None

    payload_pid = fields.get("payload_pid", "")
    tree: tuple[tuple[int, str], ...] = ()
    if payload_pid.isdigit():
        procs = parse_proc_table(sections.get("PROCS", ""))
        tree = tuple(
            (row.pid, row.args.split()[0] if row.args.split() else row.args)
            for row in descendants(procs, int(payload_pid))
        )

    status, notes = map_receipt_status(
        fields,
        problems,
        receipt_missing=receipt_missing,
        identity_resolved=identity_resolved,
        heartbeat_age=heartbeat_age,
        heartbeat_ttl=heartbeat_ttl,
        boot_changed=boot_changed,
    )

    return SupervisionReport(
        op_id=intent.op_id,
        server_id=intent.server_id,
        display=intent.display,
        scheme=intent.scheme,
        status=status,
        receipt_missing=receipt_missing,
        state_seq=fields.get("seq", ""),
        state_ts=fields.get("ts", ""),
        receipt_boot_id=receipt_boot,
        current_boot_id=current_boot,
        boot_changed=boot_changed,
        wrapper_pid=fields.get("wrapper_pid", ""),
        payload_pid=payload_pid,
        payload_startticks=receipt_ticks,
        identity_resolved=identity_resolved,
        heartbeat_age=heartbeat_age,
        exit_code=exit_code,
        descendants=tree,
        stdout=stdout_text,
        stdout_bytes=stdout_total,
        stdout_truncated=stdout_total > len(stdout_text.encode("utf-8")),
        stderr=stderr_text,
        stderr_bytes=stderr_total,
        stderr_truncated=stderr_total > len(stderr_text.encode("utf-8")),
        events_tail=events[-EVENTS_TAIL_BYTES:],
        notes=notes,
    )


def inspect_supervised_command(
    identity: SshIdentity,
    intent: SupervisionIntent,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    output_budget: int = DEFAULT_INSPECT_BUDGET_BYTES,
    heartbeat_ttl: float = HEARTBEAT_STALE_SECONDS,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> SupervisionReport:
    """Inspect a supervised operation from a NEW session (acceptance 2).

    Never raises for a lost or contract-violating receipt: both are
    ``outcome_unknown`` with value-free notes (C04 rules 5/6) — an SSH
    disconnect must leave an inspectable status, not an automatic success
    or cancellation. Only transport-level failures raise
    :class:`SshConnectError` (the inspection itself could not run).
    """
    if isinstance(output_budget, bool) or not isinstance(output_budget, int):
        _reject("output_budget must be an integer byte count")
    if not 0 < output_budget <= MAX_OUTPUT_BUDGET_BYTES:
        _reject(f"output_budget must be in (0, {MAX_OUTPUT_BUDGET_BYTES}] bytes")
    proc = _run_ssh(
        identity,
        compose_inspect_command(intent, output_budget),
        key_path=os.fspath(key_path),
        known_hosts_path=os.fspath(known_hosts_path),
        timeout=timeout,
    )
    if len(proc.stdout.encode("utf-8")) > MAX_INSPECT_RESPONSE_BYTES:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"{identity.host}:{identity.port} inspect response exceeded the "
            "hard cap; refusing to parse a hostile target response",
        )
    return _parse_sections_to_report(
        intent,
        parse_inspect_output(proc.stdout),
        heartbeat_ttl=heartbeat_ttl,
    )


# ---------------------------------------------------------------------------
# Cancellation — verify-then-signal (host rule 2, SSH-CANCEL flow)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancelReport:
    """Host truth of one cancellation attempt (C04 rule 6).

    ``status`` is ``cancelled`` ONLY when the whole ppid closure is
    verified gone AND the receipt carries the actual exit (e.g. 143).
    An already-``exited`` receipt maps through the frozen outcome
    vocabulary (nothing was cancelled — it had already finished). Any
    unverified boundary stays ``outcome_unknown``; a refused identity
    guard raises :class:`CancelRefusedError` instead of signalling.
    """

    op_id: uuid.UUID
    status: CommandStatus
    requested_recorded: bool
    verified: bool
    closure_empty: bool
    receipt_state: str
    exit_code: int | None
    notes: tuple[str, ...]


_CANCEL_NOTE_COMMAND = (
    "printf '%s note cancel_requested\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" "
    ">> {events}"
)

_IDENTITY_GUARD_COMMAND = (
    "PID=$(awk -F= '$1==\"payload_pid\"{{print $2}}' {state} 2>/dev/null); "
    "TICKS=$(awk -F= '$1==\"payload_startticks\"{{print $2}}' {state} 2>/dev/null); "
    'if [ -n "$PID" ] && [ -r "/proc/$PID/stat" ] && '
    '[ "$(awk \'{{print $22}}\' "/proc/$PID/stat" 2>/dev/null)" = "$TICKS" ]; then '
    "echo IDENTITY-OK; else echo IDENTITY-MISMATCH; fi"
)

_PROCS_COMMAND = "ps -o pid,ppid,pgid,sid,args 2>/dev/null"

_READ_STATE_COMMAND = "cat {state} 2>/dev/null"


def _closure_empty(rows: tuple[_ProcsRow, ...], payload_pid: int) -> bool:
    if any(row.pid == payload_pid for row in rows):
        return False
    return not descendants(rows, payload_pid)


def cancel_supervised_command(
    identity: SshIdentity,
    intent: SupervisionIntent,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> CancelReport:
    """Verify-then-signal cancellation over a NEW session (measured exp2).

    Records the request in the receipt events, verifies the payload
    identity (pid + start ticks; refusal raises
    :class:`CancelRefusedError` — the PID-reuse negative control), TERMs
    the payload session group, waits a bounded grace, escalates to KILL,
    verifies the ppid closure is empty and waits for the wrapper's exit
    receipt. The receipt's exit code — not the cancel request — is the
    host truth; the report says ``cancelled`` only when that exit record
    exists behind a verified-empty closure.
    """
    key_path = os.fspath(key_path)
    known_hosts_path = os.fspath(known_hosts_path)
    state = shlex.quote(f"{intent.receipt_root}/{intent.op_id}/state")
    events = shlex.quote(f"{intent.receipt_root}/{intent.op_id}/events.log")

    note = _run_ssh(
        identity,
        _CANCEL_NOTE_COMMAND.format(events=events),
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
    )
    requested_recorded = note.returncode == 0

    def read_fields() -> dict[str, str]:
        proc = _run_ssh(
            identity,
            _READ_STATE_COMMAND.format(state=state),
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            timeout=timeout,
        )
        fields, _problems = parse_state_file(proc.stdout)
        return fields

    fields = read_fields()
    state_value = fields.get("state", "")
    pid_text = fields.get("payload_pid", "")
    payload_pid = int(pid_text) if pid_text.isdigit() else None
    if state_value == "exited":
        exit_text = fields.get("exit_code", "")
        exit_code = int(exit_text) if exit_text.lstrip("-").isdigit() else None
        return CancelReport(
            op_id=intent.op_id,
            status=CommandStatus.SUCCEEDED if exit_code == 0 else CommandStatus.FAILED,
            requested_recorded=requested_recorded,
            verified=True,
            closure_empty=True,
            receipt_state=state_value,
            exit_code=exit_code,
            notes=("already exited before cancellation; nothing was cancelled",),
        )
    if payload_pid is None:
        return CancelReport(
            op_id=intent.op_id,
            status=CommandStatus.OUTCOME_UNKNOWN,
            requested_recorded=requested_recorded,
            verified=False,
            closure_empty=False,
            receipt_state=state_value,
            exit_code=None,
            notes=("receipt carries no usable payload identity",),
        )

    def remote(text: str) -> _SshResult:
        return _run_ssh(
            identity,
            text,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            timeout=timeout,
        )

    guard = remote(_IDENTITY_GUARD_COMMAND.format(state=state))
    if "IDENTITY-OK" not in guard.stdout:
        raise CancelRefusedError(
            f"{identity.host}:{identity.port} refused cancellation: pid/start "
            "ticks mismatch (stale receipt or reused pid); nothing signalled"
        )

    def procs() -> tuple[_ProcsRow, ...]:
        return parse_proc_table(remote(_PROCS_COMMAND).stdout)

    def signal_group(sig: str) -> None:
        # Best-effort by design: a group that vanished between the poll
        # and the signal makes kill(1) fail; the closure re-check — not
        # the signal's exit code — is the verifier.
        try:
            remote(f"kill -{sig} -{payload_pid}")
        except SshConnectError:
            pass

    signal_group("TERM")
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    rows = procs()
    while time.monotonic() < deadline and not _closure_empty(rows, payload_pid):
        time.sleep(0.7)
        rows = procs()
    if not _closure_empty(rows, payload_pid):
        signal_group("KILL")
        time.sleep(KILL_SETTLE_SECONDS)
        rows = procs()
    closure_empty = _closure_empty(rows, payload_pid)
    if not closure_empty:
        return CancelReport(
            op_id=intent.op_id,
            status=CommandStatus.OUTCOME_UNKNOWN,
            requested_recorded=requested_recorded,
            verified=False,
            closure_empty=False,
            receipt_state=state_value,
            exit_code=None,
            notes=("descendant closure still present after KILL escalation",),
        )

    exit_deadline = time.monotonic() + EXIT_RECEIPT_WAIT_SECONDS
    current = fields
    while time.monotonic() < exit_deadline and current.get("state") != "exited":
        time.sleep(0.5)
        current = read_fields()
    if current.get("state") != "exited":
        return CancelReport(
            op_id=intent.op_id,
            status=CommandStatus.OUTCOME_UNKNOWN,
            requested_recorded=requested_recorded,
            verified=False,
            closure_empty=True,
            receipt_state=current.get("state", state_value),
            exit_code=None,
            notes=(
                "closure verified empty but the supervisor recorded no exit "
                "(supervisor death ⇒ outcome_unknown; explicit reconciliation)",
            ),
        )
    exit_text = current.get("exit_code", "")
    exit_code = int(exit_text) if exit_text.lstrip("-").isdigit() else None
    return CancelReport(
        op_id=intent.op_id,
        status=CommandStatus.CANCELLED,
        requested_recorded=requested_recorded,
        verified=True,
        closure_empty=True,
        receipt_state="exited",
        exit_code=exit_code,
        notes=(),
    )


# ---------------------------------------------------------------------------
# Submission — intent persisted before submission (C04 rule 5)
# ---------------------------------------------------------------------------


def submit_supervised_command(
    identity: SshIdentity,
    request: CommandRequest,
    mode: PrivilegeMode,
    *,
    scheme: SupervisionScheme | None = None,
    receipt_root: str,
    intents_dir: os.PathLike[str] | str,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> SupervisionHandle:
    """Persist intent, then install and launch the supervision wrapper.

    Composition order is the safety order: the payload is composed through
    the frozen lanes (``none`` ⇒ the bounded lane's composition,
    ``sudo``/``root`` ⇒ :func:`mai.execution.privilege.privileged_remote_command`),
    which validates the request and the mode BEFORE any effect; the scheme
    capability is probed (or taken from the caller's per-target profile
    knowledge), the intent is persisted controller-side, the wrapper is
    installed with a synchronous rc check (receipt directory proven
    writable), and only then is the wrapper launched detached from the SSH
    session. A launch failure leaves the intent persisted with
    ``status="intent"`` and no receipt — ``outcome_unknown`` on
    inspection, never a retry.

    Raises:
        SupervisionRequestError: ``request`` is not a CommandRequest.
        TypeError: ``mode`` is not a :class:`PrivilegeMode`.
        CommandRequestError: malformed request / oversized payload.
        PrivilegeRequestError: the profile forbids the escalation.
        SshConnectError: transport refusal at any step, or an install /
            launch failure.
    """
    if not isinstance(request, CommandRequest):
        _reject("request must be a CommandRequest from mai.execution.commands")
    if not isinstance(mode, PrivilegeMode):
        raise TypeError("mode must be a PrivilegeMode from mai.contracts.server")
    if mode is PrivilegeMode.NONE:
        # The profile forbids escalation: the supervised payload is the
        # bounded lane's composition itself (privileged_remote_command
        # would refuse a `none` request — its refusal semantics are the
        # escalation lane's, not a payload shape).
        payload = compose_remote_command(request)
    else:
        payload = privileged_remote_command(request, mode)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    if scheme is None:
        probed = probe_supervision_scheme(
            identity,
            key_path=os.fspath(key_path),
            known_hosts_path=os.fspath(known_hosts_path),
            timeout=timeout,
        )
        scheme_value, boot_id = probed.scheme, probed.boot_id
    else:
        scheme_value, boot_id = scheme, ""

    intent = SupervisionIntent(
        op_id=request.op_id,
        server_id=request.server_id,
        display=request.display,
        scheme=scheme_value,
        privilege_mode=mode.value,
        receipt_root=receipt_root,
        host=identity.host,
        port=identity.port,
        user=identity.user,
        payload_sha256=digest,
        dispatch_ts=_now_iso(),
        boot_id=boot_id,
        status="intent",
    )
    intent_path = intent.save(intents_dir)

    script = compose_wrapper_script(intent, payload)
    installed = _run_ssh_input(
        identity,
        compose_install_command(intent),
        script,
        key_path=os.fspath(key_path),
        known_hosts_path=os.fspath(known_hosts_path),
        timeout=timeout,
    )
    if "MAI-INSTALLED" not in installed.stdout:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"{identity.host}:{identity.port} refused the supervision wrapper "
            f"install (receipt root unusable or script write failed; stderr: "
            f"{_bounded_tail(installed.stderr)})",
        )
    launched = _run_ssh(
        identity,
        compose_launch_command(intent),
        key_path=os.fspath(key_path),
        known_hosts_path=os.fspath(known_hosts_path),
        timeout=timeout,
    )
    if "MAI-LAUNCHED" not in launched.stdout:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"{identity.host}:{identity.port} did not acknowledge the "
            f"supervision launch (stderr: {_bounded_tail(launched.stderr)})",
        )
    submitted = dataclasses.replace(intent, status="submitted")
    intent_path = submitted.save(intents_dir)
    return SupervisionHandle(intent=submitted, intent_path=str(intent_path))


# ---------------------------------------------------------------------------
# Local ssh helpers — local copies: the sibling modules' helpers are private
# to their allowlisted files; a divergence would be a review finding, not a
# silent import of a private symbol.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SshResult:
    returncode: int
    stdout: str
    stderr: str


def _bounded_tail(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_transport_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the frozen transport rejection vocabulary."""
    flat = " ".join(stderr.split())
    if (
        "Host key verification failed" in flat
        or "HOST IDENTIFICATION HAS CHANGED" in flat
    ):
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning "
            f"(stderr: {_bounded_tail(stderr)}); re-enrollment is an "
            "explicit admin action, never automatic",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key "
            f"(stderr: {_bounded_tail(stderr)})",
        )
    if any(
        marker in flat
        for marker in (
            "Could not resolve hostname",
            "Connection refused",
            "Connection timed out",
            "Connection reset",
            "Network is unreachable",
        )
    ):
        return SshConnectError(
            SshErrorCode.UNREACHABLE,
            f"{endpoint} unreachable (stderr: {_bounded_tail(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_bounded_tail(stderr)})",
    )


def _run_ssh(
    identity: SshIdentity,
    remote_command: str,
    *,
    key_path: str,
    known_hosts_path: str,
    timeout: float,
    stdin_text: str | None = None,
) -> _SshResult:
    """One bounded ssh round trip; output decoded with replacement.

    Byte capture with UTF-8 replacement decoding: a payload's binary
    stdout.log must corrupt neither the parse nor the round trip.
    ssh(1) reports its own failures as exit 255 and maps to the frozen
    transport vocabulary; any OTHER non-zero remote exit is a structured
    ``ssh_failed`` (callers treat signal steps as best-effort, the
    closure check is the verifier).
    """
    endpoint = f"{identity.host}:{identity.port}"
    argv = ssh_arguments(
        identity,
        key_path=Path(key_path),
        known_hosts_path=Path(known_hosts_path),
        remote_command=remote_command,
    )
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"{endpoint} ssh round trip exceeded {timeout:.0f}s",
        ) from None
    except OSError as error:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"ssh client could not be started ({type(error).__name__})",
        ) from None
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode == 255:
        raise _map_transport_failure(proc.returncode, stderr, endpoint)
    if proc.returncode != 0:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"{endpoint} remote step exited rc={proc.returncode} "
            f"(stderr: {_bounded_tail(stderr)})",
        )
    return _SshResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)


def _run_ssh_input(
    identity: SshIdentity,
    remote_command: str,
    stdin_text: str,
    *,
    key_path: str,
    known_hosts_path: str,
    timeout: float,
) -> _SshResult:
    """One bounded ssh round trip carrying ``stdin_text`` (wrapper install)."""
    return _run_ssh(
        identity,
        remote_command,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
        stdin_text=stdin_text,
    )
