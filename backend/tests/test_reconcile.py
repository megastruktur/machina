"""Reconciliation of uncertain command outcomes (card ``04-reconcile``).

Layers, mirroring ``test_supervisor.py``:

- pure drift planning and terminal/late-result classification
  (deterministic, no transport);
- a routed fake ssh transport exercising :func:`reconcile_operation` and
  :func:`reconcile_cancellation` end to end (scripted receipt/host
  evolution, no docker);
- real-boundary tests on the disposable Arch sshd fixture (same image and
  fixture classes as ``test_supervisor.py`` — imported, not
  reimplemented — under THIS card's unique container namespace). These
  auto-skip only when docker is unavailable.

Acceptance mapping: missing receipt + changed boot identity ⇒ explicit
uncertainty/reconciliation with no blind repeated effect (plan policy,
repair flow, receipt note, ledger finalization); cancellation checks
descendant termination and the safe-boundary contract (identity guard
refusal, verified-empty ppid closure) with late results classified
consistently from the receipt's recorded exit.
"""

from __future__ import annotations

import shlex
import socket
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import SshIdentity
from mai.execution import reconcile as rec
from mai.execution import supervisor as sup
from mai.execution.commands import CommandRequest, CommandStatus
from mai.execution.ssh import SshConnectError, SshErrorCode
from mai.execution.supervisor import (
    CancelRefusedError,
    SupervisionIntent,
    SupervisionReport,
    SupervisionScheme,
    parse_state_file,
)

from test_supervisor import (  # sibling-card fixture, imported not copied
    TREE_SHELL,
    _ArchSshdFixture,
    _docker,
    _docker_available,
    _ensure_fixture_image,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-04-reconcile-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-reconcile-arch:sshd"
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_RECONCILE_FIXTURE"
RECEIPT_ROOT = "/home/mai-fxt/mai-receipts"

_BOOT_DISPATCH = "boot-at-dispatch"
_BOOT_NOW = "boot-now"

_RUNNING_FIELDS = (
    "op_id={op}\n"
    "seq=3\n"
    "state=running\n"
    "ts=2026-10-14T00:00:01Z\n"
    "boot_id={boot}\n"
    "wrapper_pid=812\n"
    "payload_pid=834\n"
    "payload_startticks=1847\n"
    "exit_code=\n"
)
_EXITED_FIELDS = (
    "op_id={op}\n"
    "seq=5\n"
    "state=exited\n"
    "ts=2026-10-14T00:00:09Z\n"
    "boot_id={boot}\n"
    "wrapper_pid=812\n"
    "payload_pid=834\n"
    "payload_startticks=1847\n"
    "exit_code={code}\n"
)
_NO_IDENTITY_RUNNING_FIELDS = (
    "op_id={op}\n"
    "seq=2\n"
    "state=running\n"
    "ts=2026-10-14T00:00:01Z\n"
    "boot_id={boot}\n"
    "wrapper_pid=812\n"
    "payload_pid=\n"
    "payload_startticks=\n"
    "exit_code=\n"
)

_PROCS_TREE = (
    "  834  812  834  834 sh -c sleep 60\n"
    "  835  834  834  834 sleep 60\n"
    "  836  834  834  834 sleep 60\n"
)


def _intent(**overrides: object) -> SupervisionIntent:
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "reconciled fixture operation",
        "scheme": SupervisionScheme.WRAPPER,
        "privilege_mode": "none",
        "receipt_root": "/srv/mai/receipts",
        "host": "203.0.113.10",
        "port": 22,
        "user": "mai-fxt",
        "payload_sha256": "b" * 64,
        "dispatch_ts": "2026-10-14T00:00:00Z",
        "boot_id": _BOOT_NOW,
        "status": "submitted",
    }
    fields.update(overrides)
    return SupervisionIntent(**fields)  # type: ignore[arg-type]


def _identity() -> SshIdentity:
    return SshIdentity(
        host="203.0.113.10",
        port=22,
        user="mai-fxt",
        host_key="SHA256:" + "a" * 43,
        credential_ref="MAI_SSH_KEY_UNIT",
    )


def _report(
    *,
    status: CommandStatus,
    receipt_missing: bool = False,
    boot_changed: bool = False,
    current_boot_id: str = _BOOT_NOW,
    receipt_boot_id: str = _BOOT_NOW,
    identity_resolved: bool = True,
    heartbeat_age: float | None = 0.5,
    exit_code: int | None = None,
    payload_pid: str = "834",
    notes: tuple[str, ...] = (),
) -> SupervisionReport:
    """One synthetic inspection report with only the planning inputs set."""
    return SupervisionReport(
        op_id=uuid.uuid4(),
        server_id=uuid.uuid4(),
        display="synthetic",
        scheme=SupervisionScheme.WRAPPER,
        status=status,
        receipt_missing=receipt_missing,
        state_seq="3",
        state_ts="2026-10-14T00:00:01Z",
        receipt_boot_id=receipt_boot_id,
        current_boot_id=current_boot_id,
        boot_changed=boot_changed,
        wrapper_pid="812",
        payload_pid=payload_pid,
        payload_startticks="1847",
        identity_resolved=identity_resolved,
        heartbeat_age=heartbeat_age,
        exit_code=exit_code,
        descendants=(),
        stdout="",
        stdout_bytes=0,
        stdout_truncated=False,
        stderr="",
        stderr_bytes=0,
        stderr_truncated=False,
        events_tail="",
        notes=notes,
    )


class _RoutedFake:
    """Scripted ssh transport routed by command shape; host state evolves.

    Installed on BOTH modules' private runners so supervisor inspections
    and cancellations plus the reconcile note append share one fake host:
    a mutable receipt state, a mutable process table and scripted
    post-kill state transitions.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.kills: list[str] = []
        self.reconcile_notes: list[str] = []
        self.boot = _BOOT_NOW
        self.state_text = ""
        self.procs_text = ""
        self.identity_ok = True
        self.identity_resolved = True
        self.hb_stale = False
        self.have_hb = True
        self.fail_reconcile_note = False
        self.post_kill_states: list[str] = []
        self._post_kill = False
        monkeypatch.setattr(sup, "_run_ssh", self)
        monkeypatch.setattr(sup, "_run_ssh_input", self)
        monkeypatch.setattr(rec, "_run_ssh", self)

    def __call__(
        self,
        identity: SshIdentity,
        remote_command: str,
        stdin_text: str | None = None,
        *,
        key_path: str,
        known_hosts_path: str,
        timeout: float,
    ) -> sup._SshResult:
        cmd = remote_command
        self.calls.append(cmd)
        if "note cancel_requested" in cmd:
            return sup._SshResult(0, "", "")
        if "note reconciliation" in cmd:
            if self.fail_reconcile_note:
                raise SshConnectError(
                    SshErrorCode.SSH_FAILED, "simulated note append failure"
                )
            self.reconcile_notes.append(cmd)
            return sup._SshResult(0, "", "")
        if cmd.startswith("NONCE="):
            return sup._SshResult(0, self._probe(), "")
        if "IDENTITY-OK" in cmd:
            verdict = "IDENTITY-OK\n" if self.identity_ok else "IDENTITY-MISMATCH\n"
            return sup._SshResult(0, verdict, "")
        if "kill -TERM" in cmd or "kill -KILL" in cmd:
            self.kills.append(cmd)
            self._post_kill = True
            self.procs_text = ""
            return sup._SshResult(0, "", "")
        if cmd.startswith("ps -o"):
            return sup._SshResult(0, self.procs_text, "")
        if cmd.startswith("cat ") and cmd.endswith("/state 2>/dev/null"):
            if self._post_kill and self.post_kill_states:
                return sup._SshResult(0, self.post_kill_states.pop(0), "")
            return sup._SshResult(0, self.state_text, "")
        raise AssertionError(f"routed fake has no handler for: {cmd[:120]}")

    def _probe(self) -> str:
        epoch = int(time.time()) - (3600 if self.hb_stale else 0)
        events = f"hb payload_pid=834 hb_epoch={epoch}\n" if self.have_hb else ""
        if self.identity_resolved:
            ident = "1847\nTICKS=1847\n"
        else:
            ident = "NO-PROC\nTICKS=1847\n"
        nonce = "cafebabe42"
        sections = [
            ("BOOT", self.boot + "\n"),
            ("STATE", self.state_text),
            ("EVENTS", events),
            ("SIZES", "OUT=0\nERR=0\n"),
            ("STDOUT", ""),
            ("STDERR", ""),
            ("IDENT", ident),
            ("PROCS", self.procs_text),
        ]
        parts: list[str] = [f"NONCE {nonce}\n"]
        for name, body in sections:
            parts.append(f"__MAI_{nonce}_{name}__\n{body}")
        return "".join(parts)


def _exited_state(op: uuid.UUID, code: int, boot: str = _BOOT_NOW) -> str:
    return _EXITED_FIELDS.format(op=op, boot=boot, code=code)


def _running_state(op: uuid.UUID, boot: str = _BOOT_NOW) -> str:
    return _RUNNING_FIELDS.format(op=op, boot=boot)


def _request(**overrides: object) -> CommandRequest:
    """Valid default request; tests override one field at a time."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "reconcile fixture tree",
        "timeout": 10.0,
        "output_budget": 4096,
        "shell": TREE_SHELL,
    }
    fields.update(overrides)
    if "argv" in fields:
        fields.pop("shell")  # exactly one of argv/shell (C04 rule 2)
    return CommandRequest(**fields)  # type: ignore[arg-type]


def _reconcile(
    fake: _RoutedFake,
    intent: SupervisionIntent,
    *,
    intents_dir: Path | None = None,
    **kwargs: object,
) -> rec.ReconciliationVerdict:
    params: dict[str, object] = {
        "key_path": "/tmp/k",
        "known_hosts_path": "/tmp/kh",
    }
    if intents_dir is not None:
        params["intents_dir"] = intents_dir
    params.update(kwargs)
    return rec.reconcile_operation(
        _identity(),
        intent,
        **params,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Pure: drift planning (acceptance 1)
# ---------------------------------------------------------------------------


class TestPlanReconciliation:
    def test_healthy_running_has_no_drift_and_no_action(self) -> None:
        plan = rec.plan_reconciliation(_intent(), _report(status=CommandStatus.RUNNING))
        assert plan.status is CommandStatus.RUNNING
        assert plan.drift == (rec.DriftKind.NONE,)
        assert plan.classification is rec.ReconciliationClass.RUNNING
        assert plan.resubmission is rec.ResubmissionPolicy.NOT_APPLICABLE
        assert plan.recommended_action is rec.ReconcileAction.NONE
        assert plan.notes == ()

    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (CommandStatus.SUCCEEDED, 0, rec.ReconciliationClass.FINAL_SUCCEEDED),
            (CommandStatus.FAILED, 7, rec.ReconciliationClass.FINAL_FAILED),
        ],
    )
    def test_final_outcomes_are_deliberate_only(
        self, status: CommandStatus, code: int, expected: rec.ReconciliationClass
    ) -> None:
        plan = rec.plan_reconciliation(
            _intent(), _report(status=status, exit_code=code)
        )
        assert plan.classification is expected
        assert plan.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert plan.recommended_action is rec.ReconcileAction.NONE

    def test_recorded_exit_survives_an_observed_restart(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(
                status=CommandStatus.FAILED,
                exit_code=7,
                boot_changed=True,
                receipt_boot_id=_BOOT_DISPATCH,
            ),
        )
        assert plan.classification is rec.ReconciliationClass.FINAL_FAILED
        assert plan.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert rec.DriftKind.BOOT_CHANGED in plan.drift
        assert any("recorded before the observed restart" in n for n in plan.notes)

    def test_missing_receipt_is_uncertain_and_never_retry_safe(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                receipt_missing=True,
                identity_resolved=False,
                heartbeat_age=None,
                payload_pid="",
            ),
        )
        assert plan.drift == (rec.DriftKind.RECEIPT_MISSING,)
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN
        assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        # no identity, nothing to signal: note-only
        assert plan.recommended_action is rec.ReconcileAction.RECORD_NOTE

    def test_supervisor_death_form_is_repairable(self) -> None:
        # exp3: wrapper dead, payload alive, identity resolves, heartbeat stale
        plan = rec.plan_reconciliation(
            _intent(),
            _report(status=CommandStatus.OUTCOME_UNKNOWN, heartbeat_age=99.0),
        )
        assert plan.drift == (rec.DriftKind.HEARTBEAT_STALE,)
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN
        assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert plan.recommended_action is rec.ReconcileAction.CLOSE_ORPHANS

    def test_unresolved_identity_cannot_be_signalled(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                identity_resolved=False,
                heartbeat_age=99.0,
            ),
        )
        assert rec.DriftKind.IDENTITY_UNRESOLVED in plan.drift
        assert rec.DriftKind.HEARTBEAT_STALE in plan.drift
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN
        assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert plan.recommended_action is rec.ReconcileAction.RECORD_NOTE

    def test_boot_change_on_uncertain_receipt_is_restart_class(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                identity_resolved=False,
                heartbeat_age=None,
                boot_changed=True,
                receipt_boot_id=_BOOT_DISPATCH,
            ),
        )
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART
        assert rec.DriftKind.BOOT_CHANGED in plan.drift
        assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN

    def test_intent_boot_drift_is_detected_without_receipt_drift(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(boot_id="boot-before"),
            _report(status=CommandStatus.OUTCOME_UNKNOWN, identity_resolved=False),
        )
        assert rec.DriftKind.BOOT_CHANGED in plan.drift
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART
        assert any("dispatch boot id" in n for n in plan.notes)

    def test_instance_epoch_observation_yields_restart_class(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                identity_resolved=False,
                heartbeat_age=None,
            ),
            instance_epoch_changed=True,
        )
        assert rec.DriftKind.INSTANCE_EPOCH_CHANGED in plan.drift
        assert rec.DriftKind.BOOT_CHANGED not in plan.drift
        assert plan.classification is rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART

    def test_epoch_hint_cannot_override_healthy_identity(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(),
            _report(status=CommandStatus.RUNNING),
            instance_epoch_changed=True,
        )
        assert plan.classification is rec.ReconciliationClass.RUNNING
        assert plan.resubmission is rec.ResubmissionPolicy.NOT_APPLICABLE
        assert any("receipt identity wins" in n for n in plan.notes)

    def test_empty_intent_boot_id_skips_intent_comparison(self) -> None:
        plan = rec.plan_reconciliation(
            _intent(boot_id=""),
            _report(status=CommandStatus.OUTCOME_UNKNOWN, heartbeat_age=99.0),
        )
        assert rec.DriftKind.BOOT_CHANGED not in plan.drift

    def test_resubmission_policy_invariant(self) -> None:
        """FORBIDDEN ⟺ uncertain; DELIBERATE_ONLY ⟺ final; N/A ⟺ running."""
        cases = [
            _report(status=CommandStatus.RUNNING),
            _report(status=CommandStatus.SUCCEEDED, exit_code=0),
            _report(status=CommandStatus.FAILED, exit_code=3),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                receipt_missing=True,
                identity_resolved=False,
                heartbeat_age=None,
                payload_pid="",
            ),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                identity_resolved=False,
                heartbeat_age=99.0,
            ),
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                identity_resolved=False,
                heartbeat_age=None,
                boot_changed=True,
            ),
        ]
        for report in cases:
            plan = rec.plan_reconciliation(_intent(), report)
            if plan.status is CommandStatus.OUTCOME_UNKNOWN:
                assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN
            elif plan.status is CommandStatus.RUNNING:
                assert plan.resubmission is rec.ResubmissionPolicy.NOT_APPLICABLE
            else:
                assert plan.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
            if plan.classification in {
                rec.ReconciliationClass.UNCERTAIN,
                rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART,
            }:
                assert plan.resubmission is rec.ResubmissionPolicy.FORBIDDEN

    def test_non_bool_epoch_hint_is_rejected(self) -> None:
        with pytest.raises(rec.ReconciliationRequestError):
            rec.plan_reconciliation(
                _intent(),
                _report(status=CommandStatus.RUNNING),
                instance_epoch_changed=1,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Pure: terminal receipt classification incl. the late result (acceptance 2)
# ---------------------------------------------------------------------------


class TestClassifyTerminalReceipt:
    def test_exit_zero_without_request_is_plain_success(self) -> None:
        status, late, notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": "0"},
            (),
            cancel_requested=False,
            closure_empty=None,
        )
        assert (status, late, notes) == (CommandStatus.SUCCEEDED, False, ())

    def test_exit_zero_after_request_is_a_late_result(self) -> None:
        status, late, notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": "0"},
            (),
            cancel_requested=True,
            closure_empty=True,
        )
        assert status is CommandStatus.SUCCEEDED
        assert late is True
        assert any("completed before termination" in n for n in notes)

    @pytest.mark.parametrize("code", [143, 137])
    def test_signal_exits_behind_a_request_are_cancelled(self, code: int) -> None:
        status, late, _notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": str(code)},
            (),
            cancel_requested=True,
            closure_empty=True,
        )
        assert status is CommandStatus.CANCELLED
        assert late is False

    def test_natural_failure_wins_the_race(self) -> None:
        status, late, notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": "1"},
            (),
            cancel_requested=True,
            closure_empty=True,
        )
        assert status is CommandStatus.FAILED
        assert late is True
        assert any("termination-signal exit" in n for n in notes)

    def test_signal_exit_without_a_request_is_just_a_failure(self) -> None:
        status, late, _notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": "143"},
            (),
            cancel_requested=False,
            closure_empty=None,
        )
        assert status is CommandStatus.FAILED
        assert late is False

    def test_empty_closure_without_exit_is_supervisor_death(self) -> None:
        status, late, notes = rec.classify_terminal_receipt(
            {"state": "running", "exit_code": ""},
            (),
            cancel_requested=True,
            closure_empty=True,
        )
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert late is False
        assert any("supervisor recorded no exit" in n for n in notes)

    def test_unverified_termination_stays_unknown(self) -> None:
        status, late, notes = rec.classify_terminal_receipt(
            {"state": "running", "exit_code": ""},
            (),
            cancel_requested=True,
            closure_empty=False,
        )
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("termination not verified" in n for n in notes)

    def test_contract_violation_never_invents_an_exit(self) -> None:
        fields, problems = parse_state_file("state=cancelled\nexit_code=0\n")
        status, late, notes = rec.classify_terminal_receipt(
            fields,
            problems,
            cancel_requested=True,
            closure_empty=True,
        )
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert late is False
        assert problems

    def test_unreadable_exit_degrades_to_unknown(self) -> None:
        status, late, _notes = rec.classify_terminal_receipt(
            {"state": "exited", "exit_code": ""},
            (),
            cancel_requested=False,
            closure_empty=None,
        )
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert late is False


# ---------------------------------------------------------------------------
# Pure: receipt note composition
# ---------------------------------------------------------------------------


class TestNoteComposition:
    def test_note_command_embeds_tokens_and_quoted_path(self) -> None:
        cmd = rec.compose_note_command(
            "/srv/r/abc/events.log",
            "reconciliation class=uncertain drift=receipt_missing "
            "resubmission=forbidden action=note_skipped",
        )
        assert "note reconciliation class=uncertain" in cmd
        assert cmd.index("date -u") < cmd.index("events.log")

    def test_note_command_quotes_hostile_paths(self) -> None:
        cmd = rec.compose_note_command(
            "/srv/receipts/x y/events.log", "reconciliation class=uncertain"
        )
        assert "'/srv/receipts/x y/events.log'" in cmd
        nasty = "/srv/receipts/$(rm -rf)/events.log"
        assert shlex.quote(nasty) in rec.compose_note_command(
            nasty, "reconciliation class=uncertain"
        )

    def test_note_body_rejects_non_vocabulary_payload(self) -> None:
        with pytest.raises(rec.ReconciliationRequestError):
            rec.compose_note_command("/x/events.log", "class=ok; rm -rf /")
        with pytest.raises(rec.ReconciliationRequestError):
            rec.compose_note_command("/x/events.log", "class='quoted'")

    def test_note_body_from_verdict_is_always_in_vocabulary(self) -> None:
        body = rec._note_body(
            rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART,
            (rec.DriftKind.BOOT_CHANGED, rec.DriftKind.HEARTBEAT_STALE),
            rec.ResubmissionPolicy.FORBIDDEN,
            (
                rec.ReconcileActionTaken.INSPECTED,
                rec.ReconcileActionTaken.NOTE_RECORDED,
            ),
        )
        composed = rec.compose_note_command("/x/events.log", body)
        assert "class=uncertain_after_restart" in composed
        assert "drift=boot_changed,heartbeat_stale" in composed


# ---------------------------------------------------------------------------
# Fake transport: reconcile_operation
# ---------------------------------------------------------------------------


@pytest.fixture(name="fake")
def _fake(monkeypatch: pytest.MonkeyPatch) -> _RoutedFake:
    monkeypatch.setattr(sup, "TERM_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(sup, "KILL_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(sup, "EXIT_RECEIPT_WAIT_SECONDS", 0.3)
    return _RoutedFake(monkeypatch)


class TestReconcileOperationFake:
    def test_missing_receipt_skips_note_and_marks_record_unresolved(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        intent.save(tmp_path)
        fake.state_text = ""  # no receipt on the target
        verdict = _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.inspected_status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN
        assert verdict.drift == (rec.DriftKind.RECEIPT_MISSING,)
        assert verdict.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert verdict.note_recorded is False
        assert rec.ReconcileActionTaken.NOTE_SKIPPED in verdict.actions
        assert verdict.record_status == "unresolved"
        # negative control: not even close_orphans=True reaches a signal —
        # one probe round trip, no kill, no fabricated receipt
        assert len(fake.calls) == 1
        assert fake.kills == []
        assert fake.reconcile_notes == []
        assert "kill" not in fake.calls[0]
        assert sup.load_intent(tmp_path, intent.op_id).status == "unresolved"

    def test_healthy_running_is_a_read_only_no_op(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        intent.save(tmp_path)
        fake.state_text = _running_state(intent.op_id)
        verdict = _reconcile(fake, intent, intents_dir=tmp_path)
        assert verdict.status is CommandStatus.RUNNING
        assert verdict.classification is rec.ReconciliationClass.RUNNING
        assert verdict.resubmission is rec.ResubmissionPolicy.NOT_APPLICABLE
        assert verdict.actions == (
            rec.ReconcileActionTaken.INSPECTED,
            rec.ReconcileActionTaken.RECORD_UNCHANGED,
        )
        assert len(fake.calls) == 1
        assert fake.kills == []
        assert sup.load_intent(tmp_path, intent.op_id).status == "submitted"

    def test_orphan_repair_closes_tree_and_stays_unknown(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        intent.save(tmp_path)
        fake.state_text = _running_state(intent.op_id)
        fake.identity_resolved = True
        fake.hb_stale = True
        fake.procs_text = _PROCS_TREE
        fake.post_kill_states = [
            _running_state(intent.op_id)  # closure check keeps reading running
        ]
        verdict = _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN
        assert verdict.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert verdict.closure_empty is True
        assert verdict.record_status == "unresolved"
        assert rec.ReconcileActionTaken.CANCEL_FLOW_RAN in verdict.actions
        assert rec.ReconcileActionTaken.ORPHANS_CLOSED_VERIFIED in verdict.actions
        assert rec.ReconcileActionTaken.NOTE_RECORDED in verdict.actions
        # the flow: guard BEFORE any signal; the note records the truth
        guard_index = next(i for i, c in enumerate(fake.calls) if "IDENTITY-OK" in c)
        assert fake.kills
        assert all(fake.calls.index(k) > guard_index for k in fake.kills)
        assert any("note reconciliation" in c for c in fake.calls)
        assert any("class=uncertain" in c for c in fake.reconcile_notes)
        assert any("resubmission=forbidden" in c for c in fake.reconcile_notes)
        # unknown is never upgraded by the repair
        assert not any(
            a in (rec.ReconcileActionTaken.RECORD_FINALIZED,) for a in verdict.actions
        )

    def test_repair_sees_late_exit_143_and_reports_cancelled(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.hb_stale = True
        fake.procs_text = _PROCS_TREE
        fake.post_kill_states = [_exited_state(intent.op_id, 143)]
        verdict = _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert verdict.status is CommandStatus.CANCELLED
        assert verdict.classification is rec.ReconciliationClass.FINAL_CANCELLED
        assert verdict.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert verdict.exit_code == 143
        assert verdict.record_status == "final"
        assert rec.ReconcileActionTaken.RECORD_FINALIZED in verdict.actions
        assert fake.reconcile_notes == []  # final outcomes are not annotated

    def test_repair_late_exit_zero_is_succeeded_never_cancelled(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.hb_stale = True
        fake.procs_text = _PROCS_TREE
        fake.post_kill_states = [_exited_state(intent.op_id, 0)]
        verdict = _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert verdict.status is CommandStatus.SUCCEEDED
        assert verdict.classification is rec.ReconciliationClass.FINAL_SUCCEEDED
        assert verdict.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert verdict.exit_code == 0
        assert any("termination" in n for n in verdict.notes)

    def test_guard_refusal_propagates_and_signals_nothing(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        intent.save(tmp_path)
        fake.state_text = _running_state(intent.op_id)
        fake.hb_stale = True
        fake.procs_text = _PROCS_TREE
        fake.identity_ok = False  # stale/forged ticks (exp2 negative control)
        with pytest.raises(CancelRefusedError):
            _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert fake.kills == []
        assert fake.reconcile_notes == []  # nothing recorded, nothing signalled
        assert sup.load_intent(tmp_path, intent.op_id).status == "submitted"

    def test_final_outcome_is_never_touched(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        intent.save(tmp_path)
        fake.state_text = _exited_state(intent.op_id, 0)
        verdict = _reconcile(fake, intent, intents_dir=tmp_path, close_orphans=True)
        assert verdict.status is CommandStatus.SUCCEEDED
        assert verdict.classification is rec.ReconciliationClass.FINAL_SUCCEEDED
        assert verdict.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert verdict.exit_code == 0
        assert verdict.record_status == "final"
        assert len(fake.calls) == 1  # one read-only probe; no writes, no signals
        assert fake.kills == []
        assert sup.load_intent(tmp_path, intent.op_id).status == "final"

    def test_note_failure_is_recorded_never_fatal(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.identity_resolved = False  # nothing to signal
        fake.hb_stale = True
        fake.fail_reconcile_note = True
        verdict = _reconcile(fake, intent, intents_dir=tmp_path)
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN
        assert verdict.note_recorded is False
        assert rec.ReconcileActionTaken.NOTE_FAILED in verdict.actions
        assert verdict.record_status == "unresolved"
        assert any("could not be recorded" in n for n in verdict.notes)

    def test_boot_drift_note_records_restart_class(
        self, fake: _RoutedFake, tmp_path: Path
    ) -> None:
        intent = _intent(boot_id=_BOOT_DISPATCH)
        fake.state_text = _running_state(intent.op_id, boot=_BOOT_DISPATCH)
        fake.identity_resolved = False
        fake.hb_stale = True
        verdict = _reconcile(fake, intent, intents_dir=tmp_path)
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN_AFTER_RESTART
        assert verdict.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert rec.DriftKind.BOOT_CHANGED in verdict.drift
        assert any("class=uncertain_after_restart" in c for c in fake.reconcile_notes)
        assert sup.load_intent(tmp_path, intent.op_id).status == "unresolved"

    def test_validation_rejects_malformed_calls(self, fake: _RoutedFake) -> None:
        with pytest.raises(rec.ReconciliationRequestError):
            _reconcile(fake, "not-an-intent")  # type: ignore[arg-type]
        intent = _intent()
        with pytest.raises(rec.ReconciliationRequestError):
            rec.reconcile_operation(
                _identity(),
                intent,
                key_path="/tmp/k",
                known_hosts_path="/tmp/kh",
                finalize_record=True,
                intents_dir=None,
            )
        with pytest.raises(rec.ReconciliationRequestError):
            rec.reconcile_operation(
                _identity(),
                intent,
                key_path="/tmp/k",
                known_hosts_path="/tmp/kh",
                close_orphans=1,  # type: ignore[arg-type]
            )
        with pytest.raises(rec.ReconciliationRequestError):
            rec.reconcile_operation(
                _identity(),
                intent,
                key_path="/tmp/k",
                known_hosts_path="/tmp/kh",
                heartbeat_ttl=0,
            )


# ---------------------------------------------------------------------------
# Fake transport: reconcile_cancellation (late result consistency)
# ---------------------------------------------------------------------------


class TestReconcileCancellationFake:
    def _cancel(
        self, fake: _RoutedFake, intent: SupervisionIntent
    ) -> rec.CancellationVerdict:
        return rec.reconcile_cancellation(
            _identity(),
            intent,
            key_path="/tmp/k",
            known_hosts_path="/tmp/kh",
        )

    def test_verified_termination_reports_cancelled(self, fake: _RoutedFake) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.procs_text = _PROCS_TREE
        fake.post_kill_states = [_exited_state(intent.op_id, 143)]
        verdict = self._cancel(fake, intent)
        assert verdict.status is CommandStatus.CANCELLED
        assert verdict.verified is True
        assert verdict.late_result is False
        assert verdict.closure_empty is True
        assert verdict.requested_recorded is True
        assert verdict.exit_code == 143
        assert fake.kills  # the tree was signalled behind a passed guard

    def test_late_exit_zero_is_reported_succeeded(self, fake: _RoutedFake) -> None:
        intent = _intent()
        fake.state_text = _exited_state(intent.op_id, 0)
        verdict = self._cancel(fake, intent)
        assert verdict.status is CommandStatus.SUCCEEDED
        assert verdict.late_result is True
        assert fake.kills == []  # early exit: nothing was ever signalled

    def test_natural_failure_wins_over_the_request(self, fake: _RoutedFake) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.procs_text = _PROCS_TREE
        fake.post_kill_states = [_exited_state(intent.op_id, 1)]
        verdict = self._cancel(fake, intent)
        assert verdict.status is CommandStatus.FAILED
        assert verdict.late_result is True
        assert any("termination-signal exit" in n for n in verdict.notes)

    def test_supervisor_death_after_closure_stays_unknown(
        self, fake: _RoutedFake
    ) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.procs_text = _PROCS_TREE
        verdict = self._cancel(fake, intent)
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.late_result is False
        assert verdict.closure_empty is True
        assert any("no exit" in n for n in verdict.notes)

    def test_unusable_identity_is_unknown_without_signalling(
        self, fake: _RoutedFake
    ) -> None:
        intent = _intent()
        fake.state_text = _NO_IDENTITY_RUNNING_FIELDS.format(
            op=intent.op_id, boot=_BOOT_NOW
        )
        verdict = self._cancel(fake, intent)
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.verified is False
        assert fake.kills == []

    def test_guard_refusal_propagates_from_cancellation(
        self, fake: _RoutedFake
    ) -> None:
        intent = _intent()
        fake.state_text = _running_state(intent.op_id)
        fake.procs_text = _PROCS_TREE
        fake.identity_ok = False
        with pytest.raises(CancelRefusedError):
            self._cancel(fake, intent)
        assert fake.kills == []


# ---------------------------------------------------------------------------
# Real boundary: disposable Arch target (unique card namespace)
# ---------------------------------------------------------------------------


class _ReconcileArchFixture(_ArchSshdFixture):
    """Same disposable image/fixture as 04-supervisor; restart-safe start
    chain (idempotent provisioning so ``docker restart`` preserves host
    keys and the account — the measured reboot analogue needs reconnect).
    """

    container = FIXTURE_CONTAINER

    def _run_args(self) -> list[str]:
        # A STABLE host port across `docker restart`: a dynamic port rebinds
        # to a new value and the port-scoped pinned known_hosts entry would
        # refuse the (unchanged) host key — a fixture-mechanics artifact,
        # not a product behavior.
        self._host_port = _pick_free_port()
        return [
            "-p",
            f"127.0.0.1:{self._host_port}:22",
            FIXTURE_IMAGE_TAG,
            "sh",
            "-c",
            "if [ ! -e /var/lib/mai-fxt-initialized ]; then "
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            "&& useradd -m -s /bin/sh mai-fxt "
            "&& touch /var/lib/mai-fxt-initialized; fi; "
            "exec /usr/bin/sshd -D",
        ]

    def ensure_image(self) -> None:
        global _image_ensured_reconcile
        if (
            _image_ensured_reconcile
            and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0
        ):
            return
        _ensure_fixture_image()  # same recipe builds the base tag
        build = _docker("tag", "mai-fxt-04-supervisor-arch:sshd", FIXTURE_IMAGE_TAG)
        if build.returncode != 0:
            context = self.scratch / "build-context"
            context.mkdir(parents=True, exist_ok=True)
            (context / "Dockerfile").write_text(
                "FROM archlinux:latest\n"
                "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
                "&& ssh-keygen -A\n",
                encoding="utf-8",
            )
            build = _docker(
                "build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200
            )
        assert build.returncode == 0, (
            f"fixture image build failed: {build.stderr[-800:]}"
        )
        _image_ensured_reconcile = True

    def restart(self) -> None:
        """The measured reboot analogue: container restart keeps the fs
        (receipts survive) and the kernel boot id, resets processes and
        reassigns the port (measured exp4a semantics)."""
        restart = _docker("restart", "-t", "2", self.container, timeout=120)
        assert restart.returncode == 0, restart.stderr[-400:]
        self._refresh_port()
        assert self.port == self._host_port, (
            "restart rebound the fixture port; the stable-port fixture rule broke"
        )
        self._wait_keyscan()

    def cleanup(self) -> str:
        proof = super().cleanup()
        strays = [
            name
            for name in _docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
            if name.startswith("mai-fxt-04-reconcile")
        ]
        return f"{proof}; reconcile-fixture strays: {strays or 'none'}"


_image_ensured_reconcile = False


def _pick_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _class_reconcile_fixture():
    if not _docker_available():
        pytest.skip("docker unavailable: Arch reconcile fixture tests require it")
    fixture = _ReconcileArchFixture()
    try:
        fixture.ensure_image()
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture(scope="class")
def reconcile_arch() -> _ReconcileArchFixture:
    yield from _class_reconcile_fixture()


class TestAgainstReconcileArch:
    """Real-boundary acceptance on the disposable Arch target."""

    def _reconcile(
        self,
        fixture: _ReconcileArchFixture,
        intent: SupervisionIntent,
        *,
        intents_dir: Path | None = None,
        **kwargs: object,
    ) -> rec.ReconciliationVerdict:
        key_path, known_hosts = fixture.transport()
        params: dict[str, object] = {
            "key_path": key_path,
            "known_hosts_path": known_hosts,
            **kwargs,
        }
        if intents_dir is not None:
            params["intents_dir"] = intents_dir
        return rec.reconcile_operation(fixture.identity(), intent, **params)

    def _cancel_reconcile(
        self, fixture: _ReconcileArchFixture, intent: SupervisionIntent
    ) -> rec.CancellationVerdict:
        key_path, known_hosts = fixture.transport()
        return rec.reconcile_cancellation(
            fixture.identity(),
            intent,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

    def _events_log(
        self, fixture: _ReconcileArchFixture, intent: SupervisionIntent
    ) -> str:
        _rc, out, _err = fixture.ssh(
            f"cat {RECEIPT_ROOT}/{intent.op_id}/events.log 2>/dev/null"
        )
        return out

    def _state_file(
        self, fixture: _ReconcileArchFixture, intent: SupervisionIntent
    ) -> str:
        return fixture.receipt_state(intent)

    def test_running_reconciliation_is_a_read_only_no_op(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell=TREE_SHELL))
        intent = handle.intent
        intents_dir = Path(handle.intent_path).parent
        report = fixture.wait_status(intent, {CommandStatus.RUNNING})
        assert report.boot_changed is False

        verdict = self._reconcile(fixture, intent, intents_dir=intents_dir)
        assert verdict.status is CommandStatus.RUNNING
        assert verdict.classification is rec.ReconciliationClass.RUNNING
        assert verdict.resubmission is rec.ResubmissionPolicy.NOT_APPLICABLE
        assert verdict.drift == (rec.DriftKind.NONE,)
        assert verdict.actions == (
            rec.ReconcileActionTaken.INSPECTED,
            rec.ReconcileActionTaken.RECORD_UNCHANGED,
        )
        assert verdict.record_status is None  # healthy: ledger untouched
        assert sup.load_intent(intents_dir, intent.op_id).status == "submitted"
        # the healthy operation was not touched: no reconciliation note
        assert "note reconciliation" not in self._events_log(fixture, intent)

        cancelled = fixture.cancel(intent)
        assert cancelled.status is CommandStatus.CANCELLED

    def test_orphan_repair_closes_descendants_and_stays_unknown(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell=TREE_SHELL))
        intent = handle.intent
        intents_dir = Path(handle.intent_path).parent
        report = fixture.wait_status(intent, {CommandStatus.RUNNING})
        payload_pid = report.payload_pid
        assert payload_pid.isdigit()
        _rc, before, _err = fixture.ssh("ps -o pid,args")
        assert "sleep 60" in before

        # exp3 disruption: kill ONLY the supervisor wrapper
        assert report.wrapper_pid.isdigit()
        kill = fixture.ssh(f"kill -KILL {report.wrapper_pid}; true", timeout=15.0)
        assert kill[0] == 0
        time.sleep(6.5)  # heartbeat staleness threshold is 5 s

        verdict = self._reconcile(
            fixture, intent, intents_dir=intents_dir, close_orphans=True
        )
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.inspected_status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN
        assert verdict.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert verdict.closure_empty is True
        assert rec.ReconcileActionTaken.ORPHANS_CLOSED_VERIFIED in verdict.actions
        assert rec.ReconcileActionTaken.NOTE_RECORDED in verdict.actions
        assert rec.DriftKind.HEARTBEAT_STALE in verdict.drift
        assert verdict.record_status == "unresolved"
        assert sup.load_intent(intents_dir, intent.op_id).status == "unresolved"

        # the tree is really gone on the target; the receipt stays frozen
        _rc, after, _err = fixture.ssh("ps -o pid,args")
        assert "sleep 60" not in after
        state = self._state_file(fixture, intent)
        assert "state=running" in state  # no exit record — host rule 3
        assert "exit_code=" in state
        events = self._events_log(fixture, intent)
        assert "note cancel_requested" in events  # the flow's own record
        assert "note reconciliation class=uncertain" in events
        assert "resubmission=forbidden" in events

        # second pass: uncertainty is stable, never auto-upgraded
        second = self._reconcile(fixture, intent, intents_dir=intents_dir)
        assert second.status is CommandStatus.OUTCOME_UNKNOWN
        assert second.classification is rec.ReconciliationClass.UNCERTAIN
        assert second.resubmission is rec.ResubmissionPolicy.FORBIDDEN

    def test_verified_cancellation_reports_cancelled_from_receipt_143(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell=TREE_SHELL))
        intent = handle.intent
        fixture.wait_status(intent, {CommandStatus.RUNNING})

        verdict = self._cancel_reconcile(fixture, intent)
        assert verdict.status is CommandStatus.CANCELLED
        assert verdict.late_result is False
        assert verdict.verified is True
        assert verdict.closure_empty is True
        assert verdict.exit_code == 143
        assert verdict.requested_recorded is True
        state = self._state_file(fixture, intent)
        assert "exit_code=143" in state

    def test_late_completion_wins_over_cancellation(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell="sleep 1"))
        intent = handle.intent
        fixture.wait_status(intent, {CommandStatus.SUCCEEDED}, timeout=30.0)

        verdict = self._cancel_reconcile(fixture, intent)
        assert verdict.status is CommandStatus.SUCCEEDED
        assert verdict.late_result is True
        assert verdict.exit_code == 0
        assert any("nothing was cancelled" in n for n in verdict.notes)

    def test_forged_ticks_refuse_reconciliation_and_signal_nothing(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell=TREE_SHELL))
        intent = handle.intent
        report = fixture.wait_status(intent, {CommandStatus.RUNNING})
        payload_pid = report.payload_pid
        assert report.payload_startticks.isdigit()
        original_ticks = report.payload_startticks

        # forge the identity: the receipt no longer names the live payload
        forge = fixture.ssh(
            f"sed -i 's/^payload_startticks=.*/payload_startticks=999999/' "
            f"{RECEIPT_ROOT}/{intent.op_id}/state",
            timeout=15.0,
        )
        assert forge[0] == 0

        with pytest.raises(CancelRefusedError):
            self._cancel_reconcile(fixture, intent)

        _rc, procs, _err = fixture.ssh("ps -o pid,args")
        assert f" {payload_pid} " in procs  # nothing was signalled

        # cleanup: restore the true ticks, then the verified flow can land
        restore = fixture.ssh(
            f"sed -i 's/^payload_startticks=.*/payload_startticks={original_ticks}/' "
            f"{RECEIPT_ROOT}/{intent.op_id}/state",
            timeout=15.0,
        )
        assert restore[0] == 0
        fixture.kill_tree(payload_pid)

    def test_container_restart_yields_uncertainty_never_blind_resume(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        """Gate-scenario half: reboot the fixture while an operation is
        frozen running; reconnect and verify the old operation is neither
        resumed nor retried and classification is honestly derived."""
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell=TREE_SHELL))
        intent = handle.intent
        intents_dir = Path(handle.intent_path).parent
        fixture.wait_status(intent, {CommandStatus.RUNNING})
        state_before = self._state_file(fixture, intent)
        seq_before = next(
            line for line in state_before.splitlines() if line.startswith("seq=")
        )

        fixture.restart()  # measured reboot analogue (fs + boot id survive)
        time.sleep(6.5)  # frozen heartbeat goes stale

        verdict = self._reconcile(fixture, intent, intents_dir=intents_dir)
        # boot id is kernel-shared and did NOT move (measured exp4a): the
        # restart must NOT be claimed — plain derived uncertainty instead
        assert rec.DriftKind.BOOT_CHANGED not in verdict.drift
        assert rec.DriftKind.IDENTITY_UNRESOLVED in verdict.drift
        assert rec.DriftKind.HEARTBEAT_STALE in verdict.drift
        assert verdict.status is CommandStatus.OUTCOME_UNKNOWN
        assert verdict.classification is rec.ReconciliationClass.UNCERTAIN
        assert verdict.resubmission is rec.ResubmissionPolicy.FORBIDDEN
        assert verdict.note_recorded is True
        assert verdict.record_status == "unresolved"

        # the receipt is untouched by the reconciliation (no resume, no
        # re-effect, no invented exit)
        state_after = self._state_file(fixture, intent)
        assert seq_before in state_after.splitlines()
        assert "state=running" in state_after
        _rc, procs, _err = fixture.ssh("ps -o pid,args")
        assert "sleep 60" not in procs  # old operation did not come back
        events = self._events_log(fixture, intent)
        assert "note reconciliation class=uncertain" in events

        # safe boundary: the post-restart receipt resolves no identity, so
        # even an explicit close_orphans request never reaches a signal —
        # the plan routes note-only and nothing on the target is touched
        second = self._reconcile(
            fixture, intent, intents_dir=intents_dir, close_orphans=True
        )
        assert rec.ReconcileActionTaken.CANCEL_FLOW_RAN not in second.actions
        assert second.closure_empty is None
        assert self._state_file(fixture, intent) == state_after
        _rc2, procs2, _err2 = fixture.ssh("ps -o pid,args")
        assert "sleep 60" not in procs2

    def test_injected_boot_drift_keeps_a_recorded_outcome_final(
        self, reconcile_arch: _ReconcileArchFixture
    ) -> None:
        """Boot-identity drift at the real transport boundary: the receipt's
        boot id is moved fixture-side (a container fixture cannot change the
        kernel boot id — measured exp4a); a recorded exit must stay final."""
        fixture = reconcile_arch
        handle = fixture.submit(_request(shell="true"))
        intent = handle.intent
        intents_dir = Path(handle.intent_path).parent
        fixture.wait_status(intent, {CommandStatus.SUCCEEDED}, timeout=30.0)

        inject = fixture.ssh(
            f"sed -i 's/^boot_id=.*/boot_id=boot-before-reboot/' "
            f"{RECEIPT_ROOT}/{intent.op_id}/state",
            timeout=15.0,
        )
        assert inject[0] == 0

        verdict = self._reconcile(fixture, intent, intents_dir=intents_dir)
        assert verdict.status is CommandStatus.SUCCEEDED
        assert verdict.classification is rec.ReconciliationClass.FINAL_SUCCEEDED
        assert verdict.resubmission is rec.ResubmissionPolicy.DELIBERATE_ONLY
        assert rec.DriftKind.BOOT_CHANGED in verdict.drift
        assert any("recorded before the observed restart" in n for n in verdict.notes)
        assert verdict.note_recorded is False  # final outcomes are not annotated
        assert verdict.record_status == "final"
        assert sup.load_intent(intents_dir, intent.op_id).status == "final"
        events = self._events_log(fixture, intent)
        assert "note reconciliation" not in events
