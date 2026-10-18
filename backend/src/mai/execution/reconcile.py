"""Explicit reconciliation of uncertain command outcomes (card ``04-reconcile``).

Consumes the frozen receipt contract (contracts.md **C04**; host-supervision
ADR ``docs/architecture/host-supervision.md``) through the public API of
:mod:`mai.execution.supervisor` — never through its private helpers. This
module is the explicit reconciliation operation of host rule 3: an
``outcome_unknown`` is resolved by inspecting live host state, optionally
closing orphaned process trees, and appending the reconciliation's own
receipt note — never by re-running an effect and never by inventing an
exit.

- **Drift detection** (acceptance 1) — the persisted dispatch intent is the
  intended state; the read-only inspection probe is the actual host state.
  :func:`plan_reconciliation` derives the drift kinds (missing receipt,
  changed boot identity, unresolved payload identity, stale heartbeat) and
  classifies them. Boot identity comes from the supervisor: the intent's
  dispatch-time ``boot_id``, the receipt's ``boot_id`` and the observed
  boot id of the inspection. A restart classification is DERIVED from an
  observed identity change — never hardcoded (host rule 4: ``boot_id``
  does not move on container restarts, so a container restart with a
  frozen receipt stays plain ``uncertain``; the control-plane instance
  identity that C04 names for such targets enters as the caller-supplied
  ``instance_epoch_changed`` observation, never as an invention here).
- **No blind repeated effect** (C04 rules 5/7) — the plan's resubmission
  policy is ``forbidden`` whenever the effect's completion is unproven
  (missing receipt, unresolved identity, uncertain outcome): a duplicate
  mutation is never authorized while the prior effect may live.
  ``deliberate_only`` (a NEW explicit operation, never a retry) requires a
  recorded final outcome; ``not_applicable`` covers genuinely running or
  already-final operations. No code path in this module resubmits anything.
- **Repair** (host rule 3, measured exp3) — an uncertain outcome whose
  payload identity still resolves is repairable: the orphaned tree is
  closed through the supervisor's verify-then-signal cancellation
  (identity guard, TERM session group, KILL escalation, ppid-closure
  verification) and the outcome STAYS ``outcome_unknown`` unless the
  receipt records the actual exit — a supervisor that died before exit
  capture can never produce one. Repair is explicit: it runs only when the
  caller passes ``close_orphans=True``; a refused identity guard raises
  :class:`mai.execution.supervisor.CancelRefusedError` before any signal.
- **Cancellation reconciliation** (acceptance 2, registry row SSH-CANCEL)
  — :func:`reconcile_cancellation` runs the supervisor's cancellation flow
  and classifies the terminal receipt CONSISTENTLY, including the late
  result: a cancellation that loses a completion race reports what the
  receipt recorded (exit 0 ⇒ ``succeeded``, natural failure ⇒ ``failed``),
  while a termination-signal exit (143 = TERM, 137 = KILL — the only exits
  this flow produces) behind a verified-empty descendant closure reports
  ``cancelled``. Requested cancellation is never verified cancellation.
- **Receipt note** — every reconciliation of an uncertain operation appends
  one value-free ``note reconciliation …`` line to the receipt's
  ``events.log`` (controlled vocabulary tokens only); a missing receipt is
  never fabricated, and a failed note append is recorded on the verdict,
  never swallowed. The controller-side intent record is finalized
  (``final`` / ``unresolved``) so later reconciliations see the ledger
  state, never re-derived guesses.

Boundary: pure planning/classification functions are deterministic and
SSH-free; the two entrypoints compose supervisor inspections and
cancellation over NEW ssh sessions (channel liveness is never command
progress, C04 rule 7). Secret material cannot enter plans, notes, verdicts
or receipts. Known limitation (recorded in the evidence): a real-host
boot-id change was not measurable on the disposable container fixtures
(kernel-shared boot id, measured exp4a); boot-drift classification is
proven at the pure/fake-transport boundary plus an injected-drift
real-transport test, while container restarts are proven at the real
boundary as plain uncertainty with no blind resume.
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mai.contracts.server import SshIdentity
from mai.execution.commands import CommandStatus
from mai.execution.ssh import SshConnectError, SshErrorCode, ssh_arguments
from mai.execution.supervisor import (
    DEFAULT_INSPECT_BUDGET_BYTES,
    DEFAULT_PROBE_TIMEOUT,
    HEARTBEAT_STALE_SECONDS,
    CancelReport,
    SupervisionIntent,
    SupervisionReport,
    cancel_supervised_command,
    inspect_supervised_command,
    parse_state_file,
)

__all__ = [
    "DEFAULT_RECONCILE_TIMEOUT",
    "TERMINATION_SIGNAL_EXITS",
    "CancellationVerdict",
    "DriftKind",
    "ReconcileAction",
    "ReconcileActionTaken",
    "ReconciliationClass",
    "ReconciliationPlan",
    "ReconciliationRequestError",
    "ReconciliationVerdict",
    "ResubmissionPolicy",
    "classify_terminal_receipt",
    "compose_note_command",
    "plan_reconciliation",
    "reconcile_cancellation",
    "reconcile_operation",
    # re-exported so callers can classify raw receipt text through
    # classify_terminal_receipt without importing the supervisor module
    "parse_state_file",
]


class ReconciliationRequestError(ValueError):
    """One structured, value-free request rejection (C01 envelope shape)."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid_request: {detail}")
        self.code = "invalid_request"
        self.detail = detail


def _reject(detail: str) -> None:
    raise ReconciliationRequestError(detail)


# ---------------------------------------------------------------------------
# Frozen vocabulary
# ---------------------------------------------------------------------------

#: Exits the verify-then-signal flow itself produces (128+SIGTERM,
#: 128+SIGKILL). A receipt exit outside this set behind a cancellation
#: request is a natural exit that won the race — the late result.
TERMINATION_SIGNAL_EXITS = (143, 137)

#: Default wall-clock budget for one controller-side ssh round trip.
DEFAULT_RECONCILE_TIMEOUT = DEFAULT_PROBE_TIMEOUT


class DriftKind(StrEnum):
    """Derived difference between intended and actual host state."""

    NONE = "none"
    #: No receipt at all: an effect can precede its receipt (C04 rule 5).
    RECEIPT_MISSING = "receipt_missing"
    #: Observed boot identity differs from the receipt's or the intent's.
    BOOT_CHANGED = "boot_changed"
    #: Control-plane restart observation (C04: container instance identity
    #: ``Id``+``StartedAt``); external evidence, never derived here.
    INSTANCE_EPOCH_CHANGED = "instance_epoch_changed"
    #: The pid + start-ticks identity guard does not resolve.
    IDENTITY_UNRESOLVED = "identity_unresolved"
    #: No fresh heartbeat: supervisor death or a target restart.
    HEARTBEAT_STALE = "heartbeat_stale"


class ReconciliationClass(StrEnum):
    """Explicit reconciliation verdict for one operation."""

    FINAL_SUCCEEDED = "final_succeeded"
    FINAL_FAILED = "final_failed"
    FINAL_CANCELLED = "final_cancelled"
    RUNNING = "running"
    UNCERTAIN = "uncertain"
    #: Uncertain AND an identity change was observed: the operation did not
    #: survive, its effect at the interruption is unknowable, and it must
    #: never be resumed or retried blindly (host rule 4).
    UNCERTAIN_AFTER_RESTART = "uncertain_after_restart"


class ResubmissionPolicy(StrEnum):
    """What may follow this reconciliation (C04 rules 5/7)."""

    #: Effect completion unproven — a duplicate mutation is never
    #: retry-safe while the prior effect may live.
    FORBIDDEN = "forbidden"
    #: Nothing to redo: the outcome is final or the operation genuinely runs.
    NOT_APPLICABLE = "not_applicable"
    #: The effect provably completed exactly once; a further execution is a
    #: NEW explicit operation decision, never an automatic retry.
    DELIBERATE_ONLY = "deliberate_only"


class ReconcileAction(StrEnum):
    """What the plan recommends the explicit operation do next."""

    NONE = "none"
    #: Identity-resolved frozen receipt (measured exp3): the orphaned tree
    #: can be closed — on the caller's explicit ``close_orphans`` opt-in.
    CLOSE_ORPHANS = "close_orphans"
    #: Record the reconciliation note and mark the record unresolved;
    #: nothing can be signalled safely (unresolved identity).
    RECORD_NOTE = "record_note"


class ReconcileActionTaken(StrEnum):
    """What the explicit operation actually did (audit trail)."""

    INSPECTED = "inspected"
    CANCEL_FLOW_RAN = "cancel_flow_ran"
    ORPHANS_CLOSED_VERIFIED = "orphans_closed_verified"
    NOTE_RECORDED = "note_recorded"
    NOTE_FAILED = "note_failed"
    #: A missing receipt is never fabricated: no note is attempted.
    NOTE_SKIPPED = "note_skipped"
    RECORD_FINALIZED = "record_finalized"
    RECORD_MARKED_UNRESOLVED = "record_marked_unresolved"
    RECORD_UNCHANGED = "record_unchanged"


def _classify_cancel_report(
    cancel: CancelReport,
) -> tuple[CommandStatus, bool, tuple[str, ...]]:
    """Consistent late-result classification of one cancellation flow (pure).

    The supervisor's flow-verified ``cancelled`` status is re-classified
    from the recorded exit so a completion that won the race against the
    termination signals is never reported as ``cancelled``; the flow's
    already-exited early mapping (``succeeded``/``failed``) is a late
    result by construction; every unverified boundary stays
    ``outcome_unknown``.
    """
    if cancel.status is CommandStatus.CANCELLED:
        fields = {
            "state": "exited",
            "exit_code": ("" if cancel.exit_code is None else str(cancel.exit_code)),
        }
        return classify_terminal_receipt(
            fields,
            (),
            cancel_requested=True,
            closure_empty=cancel.closure_empty,
        )
    if cancel.status in (CommandStatus.SUCCEEDED, CommandStatus.FAILED):
        return (
            cancel.status,
            True,
            ("late result: already exited before cancellation; nothing was cancelled",),
        )
    return cancel.status, False, ()


_FINAL_CLASSES = frozenset(
    {
        ReconciliationClass.FINAL_SUCCEEDED,
        ReconciliationClass.FINAL_FAILED,
        ReconciliationClass.FINAL_CANCELLED,
    }
)


# ---------------------------------------------------------------------------
# Pure: drift planning (acceptance 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """Deterministic drift verdict for one inspection (pure).

    ``status`` is the frozen C04 mapping result of the inspection;
    ``classification`` the reconciliation verdict; ``drift`` the derived
    intended-vs-actual differences; ``resubmission`` the never-automatic
    follow-up policy; ``recommended_action`` what the explicit operation
    may do next (``close_orphans`` still requires the caller's explicit
    opt-in at execution time).
    """

    op_id: uuid.UUID
    status: CommandStatus
    drift: tuple[DriftKind, ...]
    classification: ReconciliationClass
    resubmission: ResubmissionPolicy
    recommended_action: ReconcileAction
    notes: tuple[str, ...]


def plan_reconciliation(
    intent: SupervisionIntent,
    report: SupervisionReport,
    *,
    instance_epoch_changed: bool = False,
    heartbeat_ttl: float = HEARTBEAT_STALE_SECONDS,
) -> ReconciliationPlan:
    """Derive drift between the dispatch intent and the inspected host state.

    Pure. Every unresolved boundary lands on an explicit uncertain class
    with a ``forbidden`` resubmission policy — never on a repeatable
    effect, never on an invented exit. A restart classification requires
    an OBSERVED identity change (boot id or the control-plane epoch
    observation); it is never inferred from staleness alone (host rule 4).
    """
    if not isinstance(instance_epoch_changed, bool):
        _reject("instance_epoch_changed must be a boolean")
    drift: list[DriftKind] = []
    notes: list[str] = list(report.notes)

    if report.receipt_missing:
        drift.append(DriftKind.RECEIPT_MISSING)
    if report.boot_changed:
        drift.append(DriftKind.BOOT_CHANGED)
        notes.append("receipt boot id differs from the observed boot id")
    if (
        intent.boot_id
        and report.current_boot_id
        and intent.boot_id != report.current_boot_id
        and not report.boot_changed
    ):
        drift.append(DriftKind.BOOT_CHANGED)
        notes.append("dispatch boot id differs from the observed boot id")
    if instance_epoch_changed:
        drift.append(DriftKind.INSTANCE_EPOCH_CHANGED)
        notes.append("control-plane instance epoch changed (external observation)")

    status = report.status
    if status is CommandStatus.OUTCOME_UNKNOWN and not report.receipt_missing:
        if not report.identity_resolved:
            drift.append(DriftKind.IDENTITY_UNRESOLVED)
        if report.heartbeat_age is None:
            drift.append(DriftKind.HEARTBEAT_STALE)
            notes.append("no heartbeat observed")
        elif report.heartbeat_age > heartbeat_ttl:
            drift.append(DriftKind.HEARTBEAT_STALE)
            notes.append(f"heartbeat stale ({report.heartbeat_age:.0f}s)")

    boot_drift = (
        DriftKind.BOOT_CHANGED in drift or DriftKind.INSTANCE_EPOCH_CHANGED in drift
    )

    if status is CommandStatus.SUCCEEDED:
        classification = ReconciliationClass.FINAL_SUCCEEDED
        resubmission = ResubmissionPolicy.DELIBERATE_ONLY
        if boot_drift:
            notes.append("outcome was recorded before the observed restart")
    elif status is CommandStatus.FAILED:
        classification = ReconciliationClass.FINAL_FAILED
        resubmission = ResubmissionPolicy.DELIBERATE_ONLY
        if boot_drift:
            notes.append("outcome was recorded before the observed restart")
    elif status is CommandStatus.RUNNING:
        # The frozen mapping emits running only behind a resolved identity
        # and a fresh heartbeat with no boot change; the external epoch
        # hint cannot override that measured identity (noted, not acted on).
        classification = ReconciliationClass.RUNNING
        resubmission = ResubmissionPolicy.NOT_APPLICABLE
        if boot_drift:
            notes.append(
                "identity change observed while identity and heartbeat resolve; "
                "the receipt identity wins, drift recorded"
            )
    else:
        resubmission = ResubmissionPolicy.FORBIDDEN
        classification = (
            ReconciliationClass.UNCERTAIN_AFTER_RESTART
            if boot_drift
            else ReconciliationClass.UNCERTAIN
        )

    if (
        classification in _FINAL_CLASSES
        or classification is ReconciliationClass.RUNNING
    ):
        action = ReconcileAction.NONE
    elif report.identity_resolved and report.payload_pid:
        action = ReconcileAction.CLOSE_ORPHANS
    else:
        action = ReconcileAction.RECORD_NOTE

    return ReconciliationPlan(
        op_id=intent.op_id,
        status=status,
        drift=tuple(drift) if drift else (DriftKind.NONE,),
        classification=classification,
        resubmission=resubmission,
        recommended_action=action,
        notes=tuple(dict.fromkeys(notes)),
    )


# ---------------------------------------------------------------------------
# Pure: terminal receipt classification incl. the late result (acceptance 2)
# ---------------------------------------------------------------------------


def classify_terminal_receipt(
    fields: dict[str, str],
    problems: tuple[str, ...],
    *,
    cancel_requested: bool,
    closure_empty: bool | None,
) -> tuple[CommandStatus, bool, tuple[str, ...]]:
    """Classify one terminal receipt consistently (pure).

    The receipt's exit record is the host truth. Behind a cancellation
    request: exit 0 or a natural non-zero exit is a LATE result — the
    operation completed before the termination signals took effect and is
    classified by the frozen mapping (``succeeded``/``failed``), never as
    ``cancelled``; a termination-signal exit (:data:`TERMINATION_SIGNAL_EXITS`)
    behind a verified-empty closure is host-verified ``cancelled``.
    Without an exit record, a verified-empty closure is the supervisor-death
    form: permanently ``outcome_unknown`` pending explicit reconciliation.
    Returns ``(status, late_result, notes)``.
    """
    if problems:
        return (
            CommandStatus.OUTCOME_UNKNOWN,
            False,
            (
                *problems,
                "receipt violates the frozen contract",
            ),
        )
    exit_text = fields.get("exit_code", "")
    exit_code = int(exit_text) if exit_text.lstrip("-").isdigit() else None
    if fields.get("state", "") == "exited":
        if exit_code is None:
            return CommandStatus.OUTCOME_UNKNOWN, False, ("exit_code unreadable",)
        if exit_code == 0:
            if cancel_requested:
                return (
                    CommandStatus.SUCCEEDED,
                    True,
                    ("recorded exit 0: completed before termination took effect",),
                )
            return CommandStatus.SUCCEEDED, False, ()
        if cancel_requested and exit_code in TERMINATION_SIGNAL_EXITS:
            return CommandStatus.CANCELLED, False, ()
        if cancel_requested:
            return (
                CommandStatus.FAILED,
                True,
                (
                    f"recorded exit {exit_code} is not a termination-signal exit; "
                    "the natural exit preceded the signals",
                ),
            )
        return CommandStatus.FAILED, False, ()
    if closure_empty is True:
        return (
            CommandStatus.OUTCOME_UNKNOWN,
            False,
            (
                "closure verified empty but the supervisor recorded no exit "
                "(supervisor death ⇒ outcome_unknown; explicit reconciliation)",
            ),
        )
    if closure_empty is False:
        return (
            CommandStatus.OUTCOME_UNKNOWN,
            False,
            ("termination not verified; descendant closure still present",),
        )
    return (
        CommandStatus.OUTCOME_UNKNOWN,
        False,
        ("no exit record and no closure verification",),
    )


# ---------------------------------------------------------------------------
# Receipt note — value-free, controlled vocabulary only
# ---------------------------------------------------------------------------

_NOTE_TOKEN_GUARD = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_=,- "
)


def compose_note_command(events_path: str, body: str) -> str:
    """One append-only ``note`` line for ``events.log`` (pure).

    ``body`` must be built from controlled vocabulary tokens (class, drift
    kinds, policies, actions); anything outside the token set is rejected
    before composition, so payload or user data can never ride into the
    receipt through this path.
    """
    if not body or any(ch not in _NOTE_TOKEN_GUARD for ch in body):
        _reject("note body must be controlled-vocabulary tokens only")
    return (
        f"printf '%s note {body}\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" "
        f">> {shlex.quote(events_path)}"
    )


def _note_body(
    classification: ReconciliationClass,
    drift: tuple[DriftKind, ...],
    resubmission: ResubmissionPolicy,
    actions: tuple[ReconcileActionTaken, ...],
) -> str:
    parts = [
        f"class={classification.value}",
        f"drift={','.join(kind.value for kind in drift)}",
        f"resubmission={resubmission.value}",
        f"action={','.join(a.value for a in actions) or 'none'}",
    ]
    return "reconciliation " + " ".join(parts)


# ---------------------------------------------------------------------------
# Explicit reconciliation operation (host rule 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationVerdict:
    """Host truth of one explicit reconciliation (audit record).

    ``inspected_status`` is the pre-repair frozen-mapping result;
    ``status`` the post-reconciliation outcome in the frozen vocabulary;
    ``classification``/``drift``/``resubmission`` the explicit verdict;
    ``actions``/``note_recorded``/``record_status`` the audit of what this
    operation did (``record_status`` is the controller-side intent record's
    post-reconciliation status, or ``None`` when no record was written).
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    inspected_status: CommandStatus
    status: CommandStatus
    classification: ReconciliationClass
    drift: tuple[DriftKind, ...]
    resubmission: ResubmissionPolicy
    actions: tuple[ReconcileActionTaken, ...]
    note_recorded: bool
    record_status: str | None
    closure_empty: bool | None
    exit_code: int | None
    notes: tuple[str, ...]


def _validate_common(
    intent: SupervisionIntent,
    *,
    close_orphans: bool,
    instance_epoch_changed: bool,
    finalize_record: bool,
    heartbeat_ttl: float,
    timeout: float,
    intents_dir: os.PathLike[str] | str | None,
) -> None:
    if not isinstance(intent, SupervisionIntent):
        _reject("intent must be a SupervisionIntent from mai.execution.supervisor")
    for name, value in (
        ("close_orphans", close_orphans),
        ("instance_epoch_changed", instance_epoch_changed),
        ("finalize_record", finalize_record),
    ):
        if not isinstance(value, bool):
            _reject(f"{name} must be a boolean")
    if isinstance(heartbeat_ttl, bool) or not isinstance(heartbeat_ttl, (int, float)):
        _reject("heartbeat_ttl must be a number of seconds")
    if heartbeat_ttl <= 0:
        _reject("heartbeat_ttl must be positive")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        _reject("timeout must be a positive number of seconds")
    if finalize_record and intents_dir is None:
        _reject("finalize_record requires the intents_dir of the persisted record")


def _finalize_record(
    intent: SupervisionIntent, intents_dir: os.PathLike[str] | str, status: str
) -> None:
    dataclasses.replace(intent, status=status).save(intents_dir)


def reconcile_operation(
    identity: SshIdentity,
    intent: SupervisionIntent,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    close_orphans: bool = False,
    instance_epoch_changed: bool = False,
    finalize_record: bool = True,
    intents_dir: os.PathLike[str] | str | None = None,
    output_budget: int = DEFAULT_INSPECT_BUDGET_BYTES,
    heartbeat_ttl: float = HEARTBEAT_STALE_SECONDS,
    timeout: float = DEFAULT_RECONCILE_TIMEOUT,
) -> ReconciliationVerdict:
    """The explicit reconciliation of one uncertain operation (host rule 3).

    One read-only inspection derives the drift plan; an uncertain verdict
    appends the reconciliation note to the receipt (a missing receipt is
    never fabricated) and marks the controller record ``unresolved``; with
    ``close_orphans=True`` an identity-resolved frozen receipt is repaired
    through the supervisor's verify-then-signal flow — the outcome stays
    ``outcome_unknown`` unless the receipt records the actual exit, and a
    refused identity guard raises
    :class:`mai.execution.supervisor.CancelRefusedError` before any signal.
    A final outcome is never re-litigated: the record is finalized and
    nothing is signalled or written on the target.

    Raises:
        ReconciliationRequestError: malformed arguments.
        CancelRefusedError: the identity guard refused repair (nothing was
            signalled — the PID-reuse negative control).
        SshConnectError: transport refusal (inspection, repair or note
            append could not run; note-append failure alone is recorded on
            the verdict instead).
    """
    _validate_common(
        intent,
        close_orphans=close_orphans,
        instance_epoch_changed=instance_epoch_changed,
        finalize_record=finalize_record,
        heartbeat_ttl=heartbeat_ttl,
        timeout=timeout,
        intents_dir=intents_dir,
    )
    key_path = os.fspath(key_path)
    known_hosts_path = os.fspath(known_hosts_path)

    report = inspect_supervised_command(
        identity,
        intent,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        output_budget=output_budget,
        heartbeat_ttl=heartbeat_ttl,
        timeout=timeout,
    )
    plan = plan_reconciliation(
        intent,
        report,
        instance_epoch_changed=instance_epoch_changed,
        heartbeat_ttl=heartbeat_ttl,
    )
    actions: list[ReconcileActionTaken] = [ReconcileActionTaken.INSPECTED]
    notes: list[str] = list(plan.notes)
    status = plan.status
    classification = plan.classification
    resubmission = plan.resubmission
    closure_empty: bool | None = None
    exit_code = report.exit_code

    if plan.recommended_action is ReconcileAction.CLOSE_ORPHANS and close_orphans:
        cancel = cancel_supervised_command(
            identity,
            intent,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            timeout=timeout,
        )
        actions.append(ReconcileActionTaken.CANCEL_FLOW_RAN)
        closure_empty = cancel.closure_empty
        exit_code = cancel.exit_code
        status, _late_result, classify_notes = _classify_cancel_report(cancel)
        notes.extend(classify_notes)
        if status is CommandStatus.OUTCOME_UNKNOWN:
            classification = (
                ReconciliationClass.UNCERTAIN_AFTER_RESTART
                if plan.classification is ReconciliationClass.UNCERTAIN_AFTER_RESTART
                else ReconciliationClass.UNCERTAIN
            )
            resubmission = ResubmissionPolicy.FORBIDDEN
            if cancel.closure_empty:
                actions.append(ReconcileActionTaken.ORPHANS_CLOSED_VERIFIED)
                notes.append(
                    "orphans closed and verified gone; the receipt records "
                    "no exit, so the outcome stays unknown (host rule 3)"
                )
        else:
            classification = {
                CommandStatus.SUCCEEDED: ReconciliationClass.FINAL_SUCCEEDED,
                CommandStatus.FAILED: ReconciliationClass.FINAL_FAILED,
                CommandStatus.CANCELLED: ReconciliationClass.FINAL_CANCELLED,
            }[status]
            resubmission = ResubmissionPolicy.DELIBERATE_ONLY

    record_status: str | None = None
    note_recorded = False
    if classification in _FINAL_CLASSES:
        if intents_dir is not None:
            _finalize_record(intent, intents_dir, "final")
            record_status = "final"
            actions.append(ReconcileActionTaken.RECORD_FINALIZED)
        else:
            actions.append(ReconcileActionTaken.RECORD_UNCHANGED)
    elif classification is ReconciliationClass.RUNNING:
        # Healthy operation: read-only reconciliation, no note, no
        # ledger change — inspecting progress is not reconciling drift.
        actions.append(ReconcileActionTaken.RECORD_UNCHANGED)
    else:
        if DriftKind.RECEIPT_MISSING in plan.drift:
            # No receipt, no annotation: a receipt that never existed is
            # never fabricated (C04 rule 5); the ledger records it here.
            note_recorded = False
            actions.append(ReconcileActionTaken.NOTE_SKIPPED)
        else:
            note_recorded = _append_note(
                identity,
                intent,
                _note_body(classification, plan.drift, resubmission, tuple(actions)),
                key_path=key_path,
                known_hosts_path=known_hosts_path,
                timeout=timeout,
            )
            actions.append(
                ReconcileActionTaken.NOTE_RECORDED
                if note_recorded
                else ReconcileActionTaken.NOTE_FAILED
            )
        if intents_dir is not None:
            _finalize_record(intent, intents_dir, "unresolved")
            record_status = "unresolved"
            actions.append(ReconcileActionTaken.RECORD_MARKED_UNRESOLVED)
        else:
            actions.append(ReconcileActionTaken.RECORD_UNCHANGED)
        if not note_recorded:
            notes.append("the reconciliation note could not be recorded on the target")

    return ReconciliationVerdict(
        op_id=intent.op_id,
        server_id=intent.server_id,
        display=intent.display,
        inspected_status=report.status,
        status=status,
        classification=classification,
        drift=plan.drift,
        resubmission=resubmission,
        actions=tuple(actions),
        note_recorded=note_recorded,
        record_status=record_status,
        closure_empty=closure_empty,
        exit_code=exit_code,
        notes=tuple(notes),
    )


def _append_note(
    identity: SshIdentity,
    intent: SupervisionIntent,
    body: str,
    *,
    key_path: str,
    known_hosts_path: str,
    timeout: float,
) -> bool:
    """Best-effort reconciliation note; a failed append is never fatal."""
    events = f"{intent.receipt_root}/{intent.op_id}/events.log"
    try:
        _run_ssh(
            identity,
            compose_note_command(events, body),
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            timeout=timeout,
        )
    except SshConnectError:
        return False
    return True


# ---------------------------------------------------------------------------
# Cancellation reconciliation: requested → verified, late result classified
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancellationVerdict:
    """Host truth of one cancellation reconciliation (C04 rule 6).

    ``status`` is ``cancelled`` only behind a verified-empty descendant
    closure AND a recorded termination exit; a late result (the operation
    completed before the signals took effect) is classified by the frozen
    mapping with ``late_result=True``; every unverified boundary stays
    ``outcome_unknown``. A refused identity guard raises
    :class:`mai.execution.supervisor.CancelRefusedError` — nothing is ever
    signalled behind a stale or reused identity.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    status: CommandStatus
    requested_recorded: bool
    verified: bool
    closure_empty: bool | None
    receipt_state: str
    exit_code: int | None
    late_result: bool
    notes: tuple[str, ...]


def reconcile_cancellation(
    identity: SshIdentity,
    intent: SupervisionIntent,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
    timeout: float = DEFAULT_RECONCILE_TIMEOUT,
) -> CancellationVerdict:
    """Run the verify-then-signal flow and classify the terminal receipt.

    Descendant termination is checked by the supervisor's ppid-closure
    verification; the safe-boundary contract (identity guard before any
    signal, requested ≠ verified) is enforced there and surfaced here
    unchanged. The late result — a completion that wins the race against
    the termination signals — is classified consistently with the frozen
    mapping from the receipt's recorded exit, never reported as
    ``cancelled``.

    Raises:
        ReconciliationRequestError: malformed arguments.
        CancelRefusedError: the identity guard refused (nothing signalled).
        SshConnectError: transport refusal.
    """
    if not isinstance(intent, SupervisionIntent):
        _reject("intent must be a SupervisionIntent from mai.execution.supervisor")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        _reject("timeout must be a positive number of seconds")
    key_path = os.fspath(key_path)
    known_hosts_path = os.fspath(known_hosts_path)

    cancel: CancelReport = cancel_supervised_command(
        identity,
        intent,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
    )
    status, late_result, classification_notes = _classify_cancel_report(cancel)
    notes = list(classification_notes) + list(cancel.notes)
    return CancellationVerdict(
        op_id=cancel.op_id,
        server_id=intent.server_id,
        display=intent.display,
        status=status,
        requested_recorded=cancel.requested_recorded,
        verified=cancel.verified,
        closure_empty=cancel.closure_empty,
        receipt_state=cancel.receipt_state,
        exit_code=cancel.exit_code,
        late_result=late_result,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Local ssh helper — the sibling modules' helpers are private to their
# allowlisted files; a divergence would be a review finding, not a silent
# import of a private symbol (same convention as supervisor.py).
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
) -> _SshResult:
    """One bounded ssh round trip; output decoded with replacement."""
    endpoint = f"{identity.host}:{identity.port}"
    argv = ssh_arguments(
        identity,
        key_path=Path(key_path),
        known_hosts_path=Path(known_hosts_path),
        remote_command=remote_command,
    )
    try:
        proc = subprocess.run(
            argv,
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
