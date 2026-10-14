"""Durable supervision across SSH loss (card 04-supervisor).

Covers ``backend/src/mai/execution/supervisor.py`` against contracts.md
**C04** and the host-supervision ADR (P00 measured protocol):

- **Acceptance 1** — the frozen P00 supervisor protocol: dispatch intent
  persisted BEFORE submission (C04 rule 5, unit-proven at the transport
  boundary), an inspectable handle reloaded from disk, boot identity
  recorded at dispatch, the frozen receipt contract (``state``/
  ``events.log``/``stdout.log``/``stderr.log``; the good AND bad
  ``state`` files from the C04 frozen fixture set), scheme selection per
  host rule 6 (``systemd-run`` transient unit on systemd targets, the
  measured POSIX-sh ``setsid`` wrapper otherwise) and explicit
  first-N-bytes output retention with true totals and truncation flags.
- **Acceptance 2** — an SSH disconnect leaves an inspectable status, not
  an automatic success or cancellation: after the (by-design detached)
  dispatch channel is gone, a NEW session inspects ``running`` with a
  resolved payload identity and a live tree; a dead supervisor freezes
  the receipt at ``running`` with a stale heartbeat ⇒ ``outcome_unknown``
  (measured exp3 semantics); a missing receipt is ``outcome_unknown``;
  the frozen mapping never invents ``succeeded``/``cancelled``.
- **SSH-CANCEL flow** — verify-then-signal with the PID-reuse negative
  control at the REAL boundary: forged start ticks refuse cancellation
  and signal nothing; the true identity guards TERM → KILL escalation,
  the whole ppid closure is verified empty, and ``cancelled`` is reported
  only when the receipt carries the actual exit (143).
- **SSH-SYSV1** — the systemd transient-unit variant is boundary-verified
  against a disposable systemd-capable Arch target (systemd as PID 1,
  user manager over ssh with linger) and shows receipt-contract parity
  with the measured wrapper variant (same script, same keys, same
  transitions); cancellation uses the identical pgid signal path.

Layers (test policy: focused, real boundary where the claim depends on
it; mirrors the ``test_privilege.py`` producer pattern):

- pure composition: scheme probe parsing, wrapper script (one receipt
  discipline, base64 payload, inner ``setsid``, atomic state replace),
  install/launch command composition, intent round-trip;
- pure receipt contract: the C04 good sample and every documented bad
  ``state`` file, plus the full frozen status-mapping table;
- fake-transport unit tests over scripted ssh round trips (submit
  ordering, inspect parsing/report assembly, cancel verify-then-signal
  decision points) — determinism for protocol logic only;
- fixture-backed tests against TWO disposable Arch targets (runtime-
  environment ADR D1–D5, unique namespace ``mai-fxt-04-supervisor-*``):
  a plain sshd container (wrapper variant) and a systemd-as-PID-1
  container (transient-unit variant). These auto-skip only when docker
  is unavailable.

Secret hygiene: the supervised payload is composed by the frozen
privilege lane, which cannot carry a password; no secret material exists
in requests, wrappers, receipts, outcomes or this evidence. Fixture keys
are throwaway runtime material and never printed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.credentials.env import EnvResolver
from mai.execution import supervisor as sup
from mai.execution.commands import CommandRequest, CommandStatus
from mai.execution.ssh import (
    SshConnectError,
    pin_host_key,
    resolve_identity_key,
    ssh_arguments,
)
from mai.execution.supervisor import (
    CancelRefusedError,
    SupervisionIntent,
    compose_install_command,
    compose_inspect_command,
    compose_launch_command,
    compose_wrapper_script,
    inspect_supervised_command,
    load_intent,
    map_receipt_status,
    parse_inspect_output,
    parse_proc_table,
    parse_scheme_output,
    parse_state_file,
    probe_supervision_scheme,
    submit_supervised_command,
    SupervisionScheme,
)

TREE_SHELL = "sleep 60 & sleep 60 & sleep 60 & wait"
SLEEP_COUNT = 3

_GOOD_EXITED = """op_id=7d1c0a37-0000-4000-8000-000000000001
seq=4
state=exited
ts=2026-10-12T10:31:07Z
boot_id=3f2e1a6b9c8d4e2f
wrapper_pid=812
payload_pid=834
payload_startticks=1847
exit_code=143"""

_RUNNING_STATE = """op_id={op}
seq=3
state=running
ts=2026-10-14T00:00:01Z
boot_id=boot-at-dispatch
wrapper_pid=812
payload_pid=834
payload_startticks=1847
exit_code=
"""


def _request(**overrides: object) -> CommandRequest:
    """Valid default request; tests override one field at a time."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "supervised fixture tree",
        "timeout": 10.0,
        "output_budget": 4096,
        "shell": TREE_SHELL,
    }
    if "argv" in overrides:
        fields.pop("shell")  # exactly one of argv/shell (C04 rule 2)
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


def _intent(**overrides: object) -> SupervisionIntent:
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "pure intent",
        "scheme": SupervisionScheme.WRAPPER,
        "privilege_mode": "none",
        "receipt_root": "/srv/mai/receipts",
        "host": "203.0.113.10",
        "port": 22,
        "user": "mai-fxt",
        "payload_sha256": "a" * 64,
        "dispatch_ts": "2026-10-14T00:00:00Z",
        "boot_id": "boot-at-dispatch",
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


def _probe_output(
    nonce: str,
    *,
    boot: str = "boot-now",
    state: str = "",
    events: str = "",
    sizes: str = "",
    stdout: str = "",
    stderr: str = "",
    ident: str = "",
    procs: str = "",
) -> str:
    """One inspect-probe response in the exact compose_inspect_command form."""
    sections = [
        ("BOOT", boot),
        ("STATE", state),
        ("EVENTS", events),
        ("SIZES", sizes),
        ("STDOUT", stdout),
        ("STDERR", stderr),
        ("IDENT", ident),
        ("PROCS", procs),
    ]
    parts = [f"NONCE_LINE {nonce}\n"]
    for name, body in sections:
        parts.append(f"__MAI_{nonce}_{name}__\n{body}")
    return "".join(parts)


class _FakeSsh:
    """Scripted ssh transport: records round trips, plays queued results."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.script: list[tuple[int, str, str]] = []
        monkeypatch.setattr(sup, "_run_ssh", self)
        monkeypatch.setattr(sup, "_run_ssh_input", self)

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
        self.calls.append((remote_command, stdin_text))
        assert self.script, f"unexpected ssh call: {remote_command[:80]}"
        returncode, stdout, stderr = self.script.pop(0)
        return sup._SshResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def push(self, *results: tuple[int, str, str]) -> None:
        self.script.extend(results)


_NOW = time.time()  # fixed reference for heartbeat freshness math


def _events_fresh() -> str:
    return (
        "2026-10-14T00:00:00Z seq=2 state=running\n"
        f"2026-10-14T00:00:01Z hb payload_pid=834 hb_epoch={int(_NOW)}\n"
    )


# ---------------------------------------------------------------------------
# Pure: scheme probe
# ---------------------------------------------------------------------------


class TestSchemeProbeParsing:
    def test_systemd_pid1_with_systemctl_selects_transient_unit(self) -> None:
        probed = parse_scheme_output(
            "systemd\n\n---\n/usr/bin/systemctl\n\n---\n3f2e-boot\n"
        )
        assert probed.scheme is SupervisionScheme.SYSTEMD_UNIT
        assert probed.pid1_comm == "systemd"
        assert probed.boot_id == "3f2e-boot"

    def test_bash_pid1_selects_measured_wrapper(self) -> None:
        assert parse_scheme_output("bash\n\n---\n\n\n---\nboot-x\n").scheme is (
            SupervisionScheme.WRAPPER
        )

    def test_systemctl_absent_selects_wrapper(self) -> None:
        probed = parse_scheme_output("systemd\n\n---\n\n\n---\nboot-x\n")
        assert probed.scheme is SupervisionScheme.WRAPPER

    def test_empty_output_selects_wrapper(self) -> None:
        assert parse_scheme_output("").scheme is SupervisionScheme.WRAPPER


# ---------------------------------------------------------------------------
# Pure: wrapper script composition (one receipt discipline, both schemes)
# ---------------------------------------------------------------------------


class TestWrapperComposition:
    def test_state_file_emits_exactly_the_frozen_keys(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        for key in (
            "op_id",
            "seq",
            "state",
            "ts",
            "boot_id",
            "wrapper_pid",
            "payload_pid",
            "payload_startticks",
            "exit_code",
        ):
            assert f"printf '{key}=%s\\n'" in script

    def test_payload_embedded_base64_round_trips(self) -> None:
        payload = "sudo -n -- /bin/sh -c 'payload; exit 7'"
        script = compose_wrapper_script(_intent(), payload)
        inner_b64 = script.split("INNER=$(printf %s '", 1)[1].split("' | base64 -d", 1)[
            0
        ]
        inner = base64.b64decode(inner_b64).decode("utf-8")
        # the payload session records its own identity BEFORE the command
        assert inner.startswith(
            "printf 'payload_pid=%s\\npayload_startticks=%s\\n' \"$$\""
        )
        assert inner.split("\n", 1)[1].rstrip("\n") == payload

    def test_payload_session_is_detached_and_wrapper_survives(self) -> None:
        script = compose_wrapper_script(_intent(), TREE_SHELL)
        # inner setsid: the payload owns its session; the wrapper's monitor
        # loop is what survives and records the exit (measured exp2/exp3)
        assert 'MAI_OD="$OD" setsid sh -c "$INNER"' in script
        assert "while kill -0" in script
        assert 'wait "$PAYLOAD_PID"' in script

    def test_identity_bootstrap_reads_with_bounded_retry(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        # the wrapper retries the identity file while the payload lives and
        # gives up (honest empty ticks) once it is gone — never blocks long
        assert '[ -s "$OD/identity" ]' in script
        assert 'awk -F= \'$1=="payload_startticks"{print $2}\' "$OD/identity"' in script
        assert 'kill -0 "$PAYLOAD_PID" 2>/dev/null || break' in script
        assert "sleep 0.05" in script

    def test_state_file_is_replaced_atomically(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        assert '> "$OD/state.tmp" && mv "$OD/state.tmp" "$OD/state"' in script

    def test_heartbeat_carries_epoch_for_freshness(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        assert "hb payload_pid=" in script
        assert "hb_epoch=" in script

    def test_unwritable_receipt_root_exits_before_the_effect(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        assert 'mkdir -p "$OD" || exit 97' in script

    def test_exit_capture_runs_only_after_the_payload_exits(self) -> None:
        script = compose_wrapper_script(_intent(), "true")
        assert "EXIT_CODE=$?" in script
        assert script.index("emit exited") > script.index("EXIT_CODE=$?")

    def test_wrapper_script_is_identical_across_variants(self) -> None:
        # host rule 6: both schemes run the SAME receipt discipline; only
        # the launcher differs. Modulo the op id and receipt root, the
        # emit/monitor logic must be byte-identical.
        unit = compose_wrapper_script(
            _intent(
                scheme=SupervisionScheme.SYSTEMD_UNIT,
                receipt_root="/var/lib/mai/receipts",
            ),
            "true",
        )
        wrap = compose_wrapper_script(_intent(), "true")

        def logic(script: str) -> str:
            return script[script.index("now() {") :]

        assert logic(unit) == logic(wrap)


# ---------------------------------------------------------------------------
# Pure: install / launch composition
# ---------------------------------------------------------------------------


class TestTransportComposition:
    def test_install_proves_receipt_root_before_the_script(self) -> None:
        intent = _intent(receipt_root="/var/lib/mai/receipts")
        command = compose_install_command(intent)
        mkdir, rest = command.split(" && ", 1)
        assert mkdir.startswith("mkdir -p ")
        assert "[ -w " in rest
        assert "cat > " in rest
        assert "chmod 700 " in rest
        assert "echo MAI-INSTALLED" in command

    def test_install_quotes_hostile_paths(self) -> None:
        command = compose_install_command(_intent(receipt_root="/var/lib/my receipts"))
        assert shlex.quote("/var/lib/my receipts/")[1:-1] in command

    def test_wrapper_launch_detaches_with_setsid_and_ack(self) -> None:
        command = compose_launch_command(_intent(scheme=SupervisionScheme.WRAPPER))
        assert command.startswith("setsid /bin/sh ")
        assert "</dev/null & echo MAI-LAUNCHED" in command

    def test_systemd_launch_runs_a_collected_user_transient_unit(self) -> None:
        command = compose_launch_command(_intent(scheme=SupervisionScheme.SYSTEMD_UNIT))
        assert 'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"' in command
        assert "systemd-run --user --unit=" in command
        assert "--collect --quiet" in command
        assert "/bin/sh " in command
        assert "echo MAI-LAUNCHED" in command

    def test_unit_name_is_op_scoped_hex(self) -> None:
        intent = _intent()
        assert intent.unit_name == f"mai-op-{intent.op_id.hex[:32]}"
        assert intent.script_path == f"/tmp/mai-supervisor-{intent.op_id.hex}.sh"


# ---------------------------------------------------------------------------
# Pure: intent persistence
# ---------------------------------------------------------------------------


class TestIntentPersistence:
    def test_json_round_trip_preserves_every_field(self, tmp_path: Path) -> None:
        intent = _intent()
        path = intent.save(tmp_path)
        assert path.name == f"intent-{intent.op_id.hex}.json"
        assert load_intent(tmp_path, intent.op_id) == intent

    def test_save_is_atomic_no_tmp_leftover(self, tmp_path: Path) -> None:
        _intent().save(tmp_path)
        assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []

    def test_load_missing_intent_is_a_structured_rejection(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="invalid_request"):
            load_intent(tmp_path, uuid.uuid4())

    def test_invalid_json_shape_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / f"intent-{uuid.UUID(int=1).hex}.json").write_text("{}")
        with pytest.raises(ValueError, match="invalid_request"):
            load_intent(tmp_path, uuid.UUID(int=1))


# ---------------------------------------------------------------------------
# Pure: the frozen receipt contract (C04 good/bad fixture set)
# ---------------------------------------------------------------------------


class TestStateFileContract:
    def _map(
        self,
        fields: dict[str, str],
        problems: tuple[str, ...],
        **overrides: object,
    ) -> tuple[CommandStatus, tuple[str, ...]]:
        kwargs: dict[str, object] = {
            "receipt_missing": False,
            "identity_resolved": True,
            "heartbeat_age": 1.0,
            "heartbeat_ttl": 5.0,
            "boot_changed": False,
        }
        kwargs.update(overrides)
        return map_receipt_status(fields, problems, **kwargs)  # type: ignore[arg-type]

    def test_c04_good_exited_sample_maps_to_failed_143(self) -> None:
        fields, problems = parse_state_file(_GOOD_EXITED)
        assert problems == ()
        assert fields["exit_code"] == "143"
        status, notes = self._map(fields, problems)
        assert status is CommandStatus.FAILED
        assert notes == ()

    def test_success_is_exit_zero(self) -> None:
        fields, problems = parse_state_file(_GOOD_EXITED.replace("143", "0"))
        assert problems == ()
        assert self._map(fields, problems)[0] is CommandStatus.SUCCEEDED

    def test_running_with_valued_exit_code_violates_the_contract(self) -> None:
        fields, problems = parse_state_file(
            _GOOD_EXITED.replace("state=exited", "state=running")
        )
        assert any("exit_code present" in problem for problem in problems)
        status, notes = self._map(fields, problems)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert "receipt violates the frozen contract" in notes

    def test_exited_without_exit_record_violates_the_contract(self) -> None:
        _fields, problems = parse_state_file(
            _GOOD_EXITED.replace("exit_code=143", "exit_code=")
        )
        assert any("without exit_code" in problem for problem in problems)

    def test_exited_without_identity_guard_violates_the_contract(self) -> None:
        _fields, problems = parse_state_file(
            _GOOD_EXITED.replace("payload_startticks=1847\n", "")
        )
        assert any("without payload identity" in problem for problem in problems)

    def test_cancelled_state_is_always_a_violation(self) -> None:
        _fields, problems = parse_state_file(
            _GOOD_EXITED.replace("state=exited", "state=cancelled")
        )
        assert any("outside the frozen vocabulary" in problem for problem in problems)

    def test_garbage_exit_code_degrades_to_unknown_never_raises(self) -> None:
        # C04 bad-receipt direction: an unreadable exit record must never
        # crash the inspection nor invent an exit — outcome_unknown.
        fields, problems = parse_state_file(_GOOD_EXITED.replace("143", "abc"))
        assert problems == ()  # valued and present: shape-wise legal
        status, notes = map_receipt_status(
            fields,
            problems,
            receipt_missing=False,
            identity_resolved=True,
            heartbeat_age=1.0,
            heartbeat_ttl=5.0,
            boot_changed=False,
        )
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert "exit_code unreadable" in notes

    def test_unknown_key_and_malformed_line_are_violations(self) -> None:
        _fields, problems = parse_state_file(_GOOD_EXITED + "\ncolor=red\nnoseparator")
        assert "unknown state key" in problems
        assert "malformed state line" in problems

    def test_empty_pre_identification_values_are_not_violations(self) -> None:
        text = """op_id=x
seq=1
state=starting
ts=2026-10-14T00:00:00Z
boot_id=b
wrapper_pid=9
payload_pid=
payload_startticks=
exit_code="""
        fields, problems = parse_state_file(text)
        assert problems == ()
        assert fields["state"] == "starting"


# ---------------------------------------------------------------------------
# Pure: the frozen C04 status mapping table
# ---------------------------------------------------------------------------


def _mapped(
    state: str,
    *,
    identity: bool = True,
    hb: float | None = 1.0,
    boot_changed: bool = False,
    receipt_missing: bool = False,
    problems: tuple[str, ...] = (),
) -> tuple[CommandStatus, tuple[str, ...]]:
    fields: dict[str, str] = {"state": state}
    if state == "exited":
        fields["exit_code"] = "0"
    return map_receipt_status(
        fields,
        problems,
        receipt_missing=receipt_missing,
        identity_resolved=identity,
        heartbeat_age=hb,
        heartbeat_ttl=5.0,
        boot_changed=boot_changed,
    )


class TestStatusMapping:
    def test_exited_zero_maps_succeeded(self) -> None:
        assert _mapped("exited")[0] is CommandStatus.SUCCEEDED

    def test_exited_nonzero_maps_failed(self) -> None:
        fields = {"state": "exited", "exit_code": "7"}
        status, _ = map_receipt_status(
            fields,
            (),
            receipt_missing=False,
            identity_resolved=True,
            heartbeat_age=1.0,
            heartbeat_ttl=5.0,
            boot_changed=False,
        )
        assert status is CommandStatus.FAILED

    def test_running_fresh_resolved_is_running(self) -> None:
        assert _mapped("running")[0] is CommandStatus.RUNNING

    def test_running_stale_heartbeat_is_unknown(self) -> None:
        status, notes = _mapped("running", hb=9.0)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("heartbeat stale" in note for note in notes)

    def test_running_without_heartbeat_is_unknown(self) -> None:
        status, notes = _mapped("running", hb=None)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("no heartbeat observed" in note for note in notes)

    def test_running_unresolved_identity_is_unknown(self) -> None:
        status, notes = _mapped("running", identity=False)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("identity unresolved" in note for note in notes)

    def test_running_boot_change_is_derived_unknown(self) -> None:
        status, notes = _mapped("running", boot_changed=True)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("boot identity changed" in note for note in notes)

    def test_starting_is_unknown_supervisor_death_indistinguishable(self) -> None:
        status, notes = _mapped("starting")
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("pre-identification" in note for note in notes)

    def test_missing_receipt_is_unknown(self) -> None:
        status, notes = _mapped("running", receipt_missing=True)
        assert status is CommandStatus.OUTCOME_UNKNOWN
        assert any("receipt_missing" in note for note in notes)

    def test_cancelled_is_never_produced_by_the_mapping(self) -> None:
        for state in ("running", "starting", "exited"):
            assert _mapped(state)[0] is not CommandStatus.CANCELLED
            assert _mapped(state)[0] is not CommandStatus.CANCEL_REQUESTED


# ---------------------------------------------------------------------------
# Pure: inspect response parsing
# ---------------------------------------------------------------------------


class TestInspectParsing:
    NONCE = "cafe1234deadbeef"

    def test_sections_split_by_nonce_keyed_markers(self) -> None:
        output = _probe_output(
            self.NONCE,
            boot="boot-1",
            state=_RUNNING_STATE.format(op="op"),
            sizes="OUT=10\nERR=2\n",
        )
        sections = parse_inspect_output(output)
        assert set(sections) == {
            "BOOT",
            "STATE",
            "EVENTS",
            "SIZES",
            "STDOUT",
            "STDERR",
            "IDENT",
            "PROCS",
        }
        assert sections["BOOT"].strip() == "boot-1"
        assert "OUT=10" in sections["SIZES"]
        assert "ERR=2" in sections["SIZES"]

    def test_missing_nonce_yields_no_sections(self) -> None:
        assert parse_inspect_output("garbage without markers") == {}

    def test_payload_output_cannot_reanchor_the_parser(self) -> None:
        hostile_stdout = f"__MAI_{'f' * 16}_STDERR__\nfake stderr\n"
        output = _probe_output(self.NONCE, stdout=hostile_stdout, stderr="real\n")
        sections = parse_inspect_output(output)
        assert hostile_stdout in sections["STDOUT"]
        assert sections["STDERR"] == "real\n"

    def test_proc_table_skips_headers_and_junk(self) -> None:
        rows = parse_proc_table(
            "  PID  PPID  PGID   SID COMMAND\n"
            "    1     0     1     1 /sbin/init\n"
            "junk line\n"
            "  834   812   834   834 sh -c sleep 60 & sleep 60 & sleep 60 & wait\n"
        )
        assert [row.pid for row in rows] == [1, 834]
        assert rows[1].ppid == 812

    def test_descendants_is_a_ppid_closure(self) -> None:
        rows = parse_proc_table(
            "P P P P header\n"
            "10 1 10 10 a\n"
            "20 10 20 20 b\n"
            "30 20 30 30 c\n"
            "40 5 40 40 unrelated\n"
        )
        tree = sup.descendants(rows, 1)
        assert sorted(row.pid for row in tree) == [10, 20, 30]


# ---------------------------------------------------------------------------
# Fake-transport: inspection report assembly
# ---------------------------------------------------------------------------


class TestInspectUnit:
    IDENT_RESOLVED = "1844674407370955100\nTICKS=1844674407370955100\n"

    def _running_response(self, intent: SupervisionIntent) -> str:
        return _probe_output(
            "ab12cd34",
            boot="boot-at-dispatch",
            state=_RUNNING_STATE.format(op=intent.op_id),
            events=_events_fresh(),
            sizes="OUT=12\nERR=4\n",
            stdout="tree out\n",
            stderr="err\n",
            ident=self.IDENT_RESOLVED,
            procs=(
                "  PID  PPID  PGID   SID COMMAND\n"
                "  812     1   812   812 /bin/sh /tmp/mai-supervisor-x.sh\n"
                "  834   812   834   834 sh -c sleep 60 & sleep 60 & sleep 60 & wait\n"
                "  900   834   834   834 sleep 60\n"
            ),
        )

    def test_running_report_with_identity_tree_and_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent()
        fake = _FakeSsh(monkeypatch)
        fake.push((0, self._running_response(intent), ""))
        report = inspect_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert report.status is CommandStatus.RUNNING
        assert report.notes == ()
        assert report.identity_resolved is True
        assert report.heartbeat_age is not None and report.heartbeat_age < 3.0
        assert report.boot_changed is False
        assert report.payload_pid == "834"
        assert report.payload_startticks == "1844674407370955100"
        assert report.stdout == "tree out\n"
        assert report.stdout_bytes == 12
        assert report.stderr_truncated is False
        assert {pid for pid, _args in report.descendants} == {900}

    def test_exited_report_maps_exit_and_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent()
        fake = _FakeSsh(monkeypatch)
        exited = _GOOD_EXITED.replace(
            "op_id=7d1c0a37-0000-4000-8000-000000000001", f"op_id={intent.op_id}"
        )
        fake.push(
            (
                0,
                _probe_output(
                    "ab12cd34",
                    boot="boot-at-dispatch",
                    state=exited,
                    events="exited line\n",
                    sizes="OUT=200000\nERR=0\n",
                    stdout="x" * 64,
                    stderr="",
                    ident="NO-PROC\nTICKS=1847\n",
                    procs="",
                ),
                "",
            )
        )
        report = inspect_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert report.status is CommandStatus.FAILED
        assert report.exit_code == 143
        assert report.identity_resolved is False  # process gone
        assert report.stdout_truncated is True
        assert report.stdout_bytes == 200000

    def test_missing_receipt_is_unknown_not_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeSsh(monkeypatch)
        fake.push((0, _probe_output("ab12cd34", boot="boot"), ""))
        report = inspect_supervised_command(
            _identity(), _intent(), key_path="/k", known_hosts_path="/kh"
        )
        assert report.status is CommandStatus.OUTCOME_UNKNOWN
        assert report.receipt_missing is True
        assert any("receipt_missing" in note for note in report.notes)
        assert report.exit_code is None

    def test_unparseable_response_is_unknown_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeSsh(monkeypatch)
        fake.push((0, "total garbage", ""))
        report = inspect_supervised_command(
            _identity(), _intent(), key_path="/k", known_hosts_path="/kh"
        )
        assert report.status is CommandStatus.OUTCOME_UNKNOWN
        assert report.receipt_missing is True

    def test_hostile_oversized_response_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeSsh(monkeypatch)
        fake.push((0, "x" * (sup.MAX_INSPECT_RESPONSE_BYTES + 1), ""))
        with pytest.raises(SshConnectError) as excinfo:
            inspect_supervised_command(
                _identity(), _intent(), key_path="/k", known_hosts_path="/kh"
            )
        assert "hard cap" in str(excinfo.value)

    def test_budget_validation_rejects_before_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_spawn(*_a: object, **_k: object) -> sup._SshResult:
            raise AssertionError("ssh must not run for an invalid budget")

        monkeypatch.setattr(sup, "_run_ssh", _no_spawn)
        with pytest.raises(ValueError, match="invalid_request"):
            inspect_supervised_command(
                _identity(),
                _intent(),
                key_path="/k",
                known_hosts_path="/kh",
                output_budget=0,
            )

    def test_inspect_command_is_bounded_and_quoted(self) -> None:
        intent = _intent(receipt_root="/srv/mai receipts")
        command = compose_inspect_command(intent, 4096)
        assert "head -c 4096" in command
        assert f"tail -c {sup.EVENTS_TAIL_BYTES}" in command
        assert shlex.quote("/srv/mai receipts/")[1:-1] in command
        assert "awk '{print $22}'" in command  # the frozen field-22 guard
        assert "OUT=" in command and "ERR=" in command


# ---------------------------------------------------------------------------
# Fake-transport: cancellation decision points
# ---------------------------------------------------------------------------


class TestCancelUnit:
    def _running_state(self, intent: SupervisionIntent) -> str:
        return _RUNNING_STATE.format(op=intent.op_id)

    def _exited_state(self, intent: SupervisionIntent, code: str) -> str:
        return (
            _RUNNING_STATE.format(op=intent.op_id)
            .replace("seq=3", "seq=4")
            .replace("state=running", "state=exited")
            .replace("exit_code=", f"exit_code={code}")
        )

    def _patch(self, monkeypatch: pytest.MonkeyPatch, route) -> None:
        monkeypatch.setattr(sup, "_run_ssh", route)
        monkeypatch.setattr(sup, "_run_ssh_input", route)

    def test_verified_cancel_reports_receipt_exit_not_the_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")
        states = [
            self._running_state(intent),
            self._running_state(intent),
            self._exited_state(intent, "143"),
        ]
        commands: list[str] = []

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            commands.append(command)
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                return sup._SshResult(0, states.pop(0), "")
            if command.startswith("PID=$(awk"):
                return sup._SshResult(0, "IDENTITY-OK", "")
            if command.startswith("ps "):
                return sup._SshResult(0, "", "")  # closure empty after TERM
            if command.startswith("kill -TERM"):
                return sup._SshResult(0, "", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        report = sup.cancel_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert report.status is CommandStatus.CANCELLED
        assert report.verified is True
        assert report.closure_empty is True
        assert report.exit_code == 143
        assert report.receipt_state == "exited"
        assert report.requested_recorded is True
        assert any(command.startswith("kill -TERM") for command in commands)
        assert not any(command.startswith("kill -KILL") for command in commands)

    def test_guard_mismatch_signals_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")
        commands: list[str] = []

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            commands.append(command)
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                return sup._SshResult(0, self._running_state(intent), "")
            if command.startswith("PID=$(awk"):
                return sup._SshResult(0, "IDENTITY-MISMATCH", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        with pytest.raises(CancelRefusedError) as excinfo:
            sup.cancel_supervised_command(
                _identity(), intent, key_path="/k", known_hosts_path="/kh"
            )
        assert "nothing signalled" in excinfo.value.detail
        assert not any(command.startswith("kill") for command in commands)

    def test_already_exited_maps_through_the_outcome_vocabulary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")
        commands: list[str] = []

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            commands.append(command)
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                return sup._SshResult(0, self._exited_state(intent, "0"), "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        report = sup.cancel_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert not any(command.startswith("kill") for command in commands)
        assert report.status is CommandStatus.SUCCEEDED
        assert report.verified is True
        assert any("nothing was cancelled" in note for note in report.notes)

    def test_closure_still_present_after_kill_stays_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")
        kills: list[str] = []

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                return sup._SshResult(0, self._running_state(intent), "")
            if command.startswith("PID=$(awk"):
                return sup._SshResult(0, "IDENTITY-OK", "")
            if command.startswith("ps "):
                return sup._SshResult(0, " 999 834 834 834 sleep 60\n", "")
            if command.startswith("kill "):
                kills.append(command)
                return sup._SshResult(0, "", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        monkeypatch.setattr(sup, "TERM_GRACE_SECONDS", 0.2)
        monkeypatch.setattr(sup, "KILL_SETTLE_SECONDS", 0.0)
        monkeypatch.setattr(sup, "EXIT_RECEIPT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
        report = sup.cancel_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert any(command.startswith("kill -KILL") for command in kills)
        assert report.status is CommandStatus.OUTCOME_UNKNOWN
        assert report.verified is False
        assert report.closure_empty is False

    def test_closure_empty_but_no_exit_record_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                return sup._SshResult(0, self._running_state(intent), "")
            if command.startswith("PID=$(awk"):
                return sup._SshResult(0, "IDENTITY-OK", "")
            if command.startswith("ps "):
                return sup._SshResult(0, "", "")
            if command.startswith("kill "):
                return sup._SshResult(0, "", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        monkeypatch.setattr(sup, "EXIT_RECEIPT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
        report = sup.cancel_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert report.closure_empty is True
        assert report.status is CommandStatus.OUTCOME_UNKNOWN
        assert any("recorded no exit" in note for note in report.notes)

    def test_receipt_without_payload_identity_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intent = _intent(receipt_root="/srv/mai/receipts")
        guard_ran = False

        def route(_identity, command: str, _stdin: str | None = None, **_kw: object):
            nonlocal guard_ran
            if "note cancel_requested" in command:
                return sup._SshResult(0, "", "")
            if command.startswith("cat "):
                starting = (
                    _RUNNING_STATE.format(op=intent.op_id)
                    .replace("state=running", "state=starting")
                    .replace("seq=3", "seq=1")
                    .replace("payload_pid=834", "payload_pid=")
                    .replace("payload_startticks=1847", "payload_startticks=")
                )
                return sup._SshResult(0, starting, "")
            if command.startswith("PID=$(awk"):
                guard_ran = True
                return sup._SshResult(0, "IDENTITY-OK", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        self._patch(monkeypatch, route)
        report = sup.cancel_supervised_command(
            _identity(), intent, key_path="/k", known_hosts_path="/kh"
        )
        assert guard_ran is False
        assert report.status is CommandStatus.OUTCOME_UNKNOWN
        assert any("no usable payload identity" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Fake-transport: submit ordering (C04 rule 5)
# ---------------------------------------------------------------------------


class TestSubmitUnit:
    def _route(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        calls: dict[str, list[str]] = {"probe": [], "install": [], "launch": []}

        def route(_identity, command: str, stdin: str | None = None, **_kw: object):
            if command.startswith("cat /proc/1/comm"):
                calls["probe"].append(command)
                return sup._SshResult(0, "bash\n\n---\n\n\n---\nboot-x\n", "")
            if "MAI-INSTALLED" in command:
                calls["install"].append(command)
                assert stdin is not None and stdin.startswith("#!/bin/sh")
                return sup._SshResult(0, "MAI-INSTALLED\n", "")
            if "MAI-LAUNCHED" in command:
                calls["launch"].append(command)
                return sup._SshResult(0, "MAI-LAUNCHED\n", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        monkeypatch.setattr(sup, "_run_ssh", route)
        monkeypatch.setattr(sup, "_run_ssh_input", route)
        return calls

    def _submit(self, tmp_path: Path, **kwargs: object) -> sup.SupervisionHandle:
        params: dict[str, object] = {
            "scheme": SupervisionScheme.WRAPPER,
            "receipt_root": "/srv/mai/receipts",
            "intents_dir": tmp_path,
            "key_path": "/k",
            "known_hosts_path": "/kh",
        }
        params.update(kwargs)
        return submit_supervised_command(
            _identity(),
            _request(),
            PrivilegeMode.NONE,
            **params,  # type: ignore[arg-type]
        )

    def test_intent_is_persisted_before_any_transport(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._route(monkeypatch)
        real_input = sup._run_ssh_input
        seen_at_install: list[str] = []

        def spy(_identity, command: str, stdin: str | None = None, **kw: object):
            files = list(tmp_path.glob("intent-*.json"))
            seen_at_install.extend(file.name for file in files)
            assert len(files) == 1
            assert '"status": "intent"' in files[0].read_text()
            return real_input(_identity, command, stdin, **kw)  # type: ignore[misc]

        monkeypatch.setattr(sup, "_run_ssh_input", spy)
        handle = self._submit(tmp_path)
        assert len(seen_at_install) == 1
        final = Path(handle.intent_path).read_text()
        assert '"status": "submitted"' in final

    def test_probe_runs_when_scheme_not_supplied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._route(monkeypatch)
        handle = self._submit(tmp_path, scheme=None)
        assert len(calls["probe"]) == 1
        assert handle.intent.scheme is SupervisionScheme.WRAPPER
        assert handle.intent.boot_id == "boot-x"

    def test_caller_supplied_scheme_skips_the_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._route(monkeypatch)
        handle = self._submit(tmp_path)
        assert calls["probe"] == []
        assert handle.intent.scheme is SupervisionScheme.WRAPPER
        assert handle.intent.boot_id == ""

    def test_install_failure_keeps_intent_and_never_launches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._route(monkeypatch)

        def failing_install(
            _identity, command: str, stdin: str | None = None, **_kw: object
        ):
            if "MAI-INSTALLED" in command:
                return sup._SshResult(1, "", "sh: cannot create: read-only fs")
            raise AssertionError(f"unexpected command: {command[:60]}")

        monkeypatch.setattr(sup, "_run_ssh_input", failing_install)
        with pytest.raises(SshConnectError) as excinfo:
            self._submit(tmp_path)
        assert "receipt root unusable" in str(excinfo.value)
        assert calls["launch"] == []
        persisted = list(tmp_path.glob("intent-*.json"))
        assert len(persisted) == 1
        assert '"status": "intent"' in persisted[0].read_text()

    def test_launch_ack_absent_is_a_structured_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def route(_identity, command: str, stdin: str | None = None, **_kw: object):
            if "MAI-INSTALLED" in command:
                return sup._SshResult(0, "MAI-INSTALLED\n", "")
            return sup._SshResult(0, "", "")  # launch ack missing

        monkeypatch.setattr(sup, "_run_ssh", route)
        monkeypatch.setattr(sup, "_run_ssh_input", route)
        with pytest.raises(SshConnectError, match="did not acknowledge"):
            self._submit(tmp_path)

    def test_oversized_escalation_rejected_before_any_transport_or_intent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Pre-effect negative control: the frozen privilege lane refuses the
        # oversized escalation BEFORE the probe, the intent persistence or
        # any transport — the supervisor adds no effect of its own.
        from mai.execution.commands import MAX_REMOTE_COMMAND_BYTES, CommandRequestError

        def _no_spawn(*_a: object, **_k: object):
            raise AssertionError("nothing may be transported for an invalid payload")

        monkeypatch.setattr(sup, "_run_ssh", _no_spawn)
        monkeypatch.setattr(sup, "_run_ssh_input", _no_spawn)
        with pytest.raises(CommandRequestError):
            submit_supervised_command(
                _identity(),
                _request(shell="x" * (MAX_REMOTE_COMMAND_BYTES + 1)),
                PrivilegeMode.SUDO,
                scheme=SupervisionScheme.WRAPPER,
                receipt_root="/srv/mai/receipts",
                intents_dir=tmp_path,
                key_path="/k",
                known_hosts_path="/kh",
            )
        assert list(tmp_path.iterdir()) == []

    def test_none_mode_supervises_the_bounded_payload_without_escalation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The unprivileged mode composes the bounded lane's payload verbatim;
        # the installed wrapper embeds exactly that payload (no sudo prefix).
        installed_scripts: list[str] = []

        def route(_identity, command: str, stdin: str | None = None, **_kw: object):
            if command.startswith("cat /proc/1/comm"):
                return sup._SshResult(0, "bash\n\n---\n\n\n---\nboot-x\n", "")
            if "MAI-INSTALLED" in command:
                assert stdin is not None
                installed_scripts.append(stdin)
                return sup._SshResult(0, "MAI-INSTALLED\n", "")
            if "MAI-LAUNCHED" in command:
                return sup._SshResult(0, "MAI-LAUNCHED\n", "")
            raise AssertionError(f"unexpected command: {command[:60]}")

        monkeypatch.setattr(sup, "_run_ssh", route)
        monkeypatch.setattr(sup, "_run_ssh_input", route)
        request = _request(shell="echo plain-payload")
        self._submit_with(monkeypatch, tmp_path, request, PrivilegeMode.NONE)
        import base64 as _base64
        from mai.execution.commands import compose_remote_command

        inner_b64 = (
            installed_scripts[0]
            .split("INNER=$(printf %s '", 1)[1]
            .split("' | base64 -d", 1)[0]
        )
        inner = _base64.b64decode(inner_b64).decode("utf-8")
        embedded = inner.split("\n", 1)[1].rstrip("\n")
        assert embedded == compose_remote_command(request)
        assert "sudo" not in embedded

    def _submit_with(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        request: CommandRequest,
        mode: PrivilegeMode,
    ) -> sup.SupervisionHandle:
        params: dict[str, object] = {
            "scheme": SupervisionScheme.WRAPPER,
            "receipt_root": "/srv/mai/receipts",
            "intents_dir": tmp_path,
            "key_path": "/k",
            "known_hosts_path": "/kh",
        }
        return submit_supervised_command(
            _identity(),
            request,
            mode,
            **params,  # type: ignore[arg-type]
        )

    def test_non_command_request_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid_request"):
            submit_supervised_command(
                _identity(),
                "not a request",  # type: ignore[arg-type]
                PrivilegeMode.NONE,
                scheme=SupervisionScheme.WRAPPER,
                receipt_root="/srv/mai/receipts",
                intents_dir=tmp_path,
                key_path="/k",
                known_hosts_path="/kh",
            )

    def test_payload_digest_binds_the_composed_payload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._route(monkeypatch)
        request = _request(shell="echo bound")
        params: dict[str, object] = {
            "scheme": SupervisionScheme.WRAPPER,
            "receipt_root": "/srv/mai/receipts",
            "intents_dir": tmp_path,
            "key_path": "/k",
            "known_hosts_path": "/kh",
        }
        handle = submit_supervised_command(
            _identity(),
            request,
            PrivilegeMode.NONE,
            **params,  # type: ignore[arg-type]
        )
        assert handle.intent.payload_sha256 == hashlib.sha256(b"echo bound").hexdigest()


# ---------------------------------------------------------------------------
# Disposable fixtures (runtime-environment ADR D1–D5; unique namespace
# mai-fxt-04-supervisor-*). Pattern producer: test_privilege.py fixtures.
# ---------------------------------------------------------------------------

FIXTURE_IMAGE_TAG = "mai-fxt-04-supervisor-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-supervisor-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_SUPERVISOR_FIXTURE"
FIXTURE_USER = "mai-fxt"
PLAIN_CONTAINER = "mai-fxt-04-supervisor-arch"
SYSTEMD_CONTAINER = "mai-fxt-04-supervisor-sysd"
RECEIPT_ROOT = f"/home/{FIXTURE_USER}/mai-receipts"
SUDOERS_DROPIN = f"{FIXTURE_USER}-supervisor-nopasswd"
FIXTURE_SUDOERS_RULE = f"{FIXTURE_USER} ALL=(root) NOPASSWD: /bin/sh"

_image_ensured = False


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image() -> None:
    """Build the disposable Arch+openssh+sudo fixture image once."""
    global _image_ensured
    if (
        _image_ensured
        and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0
    ):
        return
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
        "&& ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True


class _ArchSshdFixture:
    """One disposable Arch target with key auth; scheme variant by subclass."""

    container = PLAIN_CONTAINER

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.host_key_fingerprint = ""
        self.cleanup_proof = "<not cleaned>"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.key_path),
                "-C",
                f"{self.container}-test",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        _ensure_fixture_image()
        run = _docker("run", "--rm", "-d", "--name", self.container, *self._run_args())
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._wait_ready()
        self._provision()
        self._wait_keyscan()
        self.host_key_fingerprint = self._current_fingerprint()
        pinned = pin_host_key(self.identity(), self.scratch / "known_hosts")
        assert pinned.fingerprint == self.host_key_fingerprint

    def _run_args(self) -> list[str]:
        return [
            "-p",
            "127.0.0.1::22",
            FIXTURE_IMAGE_TAG,
            "sh",
            "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            f"&& useradd -m -s /bin/sh {FIXTURE_USER} "
            "&& exec /usr/bin/sshd -D",
        ]

    def _wait_ready(self) -> None:
        """Plain: the start chain created the account; wait for it."""
        deadline = time.monotonic() + 60.0
        last = ""
        while time.monotonic() < deadline:
            check = _docker("exec", self.container, "id", "-u", FIXTURE_USER)
            if check.returncode == 0:
                return
            last = check.stderr[-200:]
            time.sleep(0.3)
        raise AssertionError(f"fixture account never appeared: {last}")

    def _provision(self) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "sh",
                "-c",
                f"mkdir -p /home/{FIXTURE_USER}/.ssh "
                f"&& cat > /home/{FIXTURE_USER}/.ssh/authorized_keys "
                f"&& chmod 700 /home/{FIXTURE_USER}/.ssh "
                f"&& chmod 600 /home/{FIXTURE_USER}/.ssh/authorized_keys "
                f"&& chown -R {FIXTURE_USER}:{FIXTURE_USER} /home/{FIXTURE_USER}/.ssh",
            ],
            input=pub,
            capture_output=True,
            timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    def write_sudo_grant(self) -> None:
        script = (
            "grep -q '^@includedir /etc/sudoers.d$' /etc/sudoers "
            "|| echo '@includedir /etc/sudoers.d' >> /etc/sudoers; "
            f"echo '{FIXTURE_SUDOERS_RULE}' > /etc/sudoers.d/{SUDOERS_DROPIN}; "
            f"chmod 440 /etc/sudoers.d/{SUDOERS_DROPIN}; "
            f"visudo -cf /etc/sudoers.d/{SUDOERS_DROPIN}"
        )
        result = _docker("exec", self.container, "sh", "-c", script)
        assert result.returncode == 0, (
            f"sudoers provisioning failed: {result.stdout[-400:]}{result.stderr[-400:]}"
        )

    def remove_sudo_grant(self) -> None:
        result = _docker(
            "exec", self.container, "rm", "-f", f"/etc/sudoers.d/{SUDOERS_DROPIN}"
        )
        assert result.returncode == 0, result.stderr[-400:]

    def _refresh_port(self) -> None:
        port_out = _docker(
            "inspect",
            "--format",
            '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}',
            self.container,
        )
        assert port_out.returncode == 0 and port_out.stdout.strip().isdigit(), (
            f"fixture port inspect failed: {port_out.stderr[-400:]}"
        )
        self.port = int(port_out.stdout.strip())

    # -- identity / transport ------------------------------------------------

    def identity(self) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user=FIXTURE_USER,
            host_key=self.host_key_fingerprint,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def transport(self) -> tuple[Path, Path]:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        identity = self.identity()
        key_path = resolve_identity_key(EnvResolver(env_file), identity.credential_ref)
        return key_path, self.scratch / "known_hosts"

    def ssh(self, remote: str, *, timeout: float = 30.0) -> tuple[int, str, str]:
        """Raw pinned-identity ssh round trip (fixture-side probes only)."""
        key_path, known_hosts = self.transport()
        argv = ssh_arguments(
            self.identity(),
            key_path=key_path,
            known_hosts_path=known_hosts,
            remote_command=remote,
        )
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        """Enrollment fixture value: cross-checked against real ssh-keygen."""
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        ed25519 = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        from mai.execution.ssh import target_fingerprint

        fingerprint = keygen.stdout.split()[1]
        computed = target_fingerprint(ed25519.split()[1], ed25519.split()[2])
        assert computed == fingerprint
        return fingerprint

    def _wait_keyscan(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                if self._scan_now():
                    return
                last = "empty keyscan"
            except subprocess.TimeoutExpired as error:
                last = str(error)
            time.sleep(1.0)
        raise AssertionError(f"fixture sshd never became ready: {last}")

    # -- supervisor op helpers -------------------------------------------------

    def submit(
        self,
        request: CommandRequest,
        mode: PrivilegeMode = PrivilegeMode.NONE,
        **kwargs: object,
    ) -> sup.SupervisionHandle:
        key_path, known_hosts = self.transport()
        params: dict[str, object] = {
            "receipt_root": RECEIPT_ROOT,
            "intents_dir": self.scratch / "intents",
            "key_path": key_path,
            "known_hosts_path": known_hosts,
        }
        params.update(kwargs)
        return submit_supervised_command(
            self.identity(),
            request,
            mode,
            **params,  # type: ignore[arg-type]
        )

    def inspect(
        self, intent: SupervisionIntent, **kwargs: object
    ) -> sup.SupervisionReport:
        key_path, known_hosts = self.transport()
        return inspect_supervised_command(
            self.identity(),
            intent,
            key_path=key_path,
            known_hosts_path=known_hosts,
            **kwargs,
        )

    def cancel(self, intent: SupervisionIntent) -> sup.CancelReport:
        key_path, known_hosts = self.transport()
        return sup.cancel_supervised_command(
            self.identity(),
            intent,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

    def wait_status(
        self,
        intent: SupervisionIntent,
        wanted: set[CommandStatus],
        timeout: float = 40.0,
    ) -> sup.SupervisionReport:
        deadline = time.monotonic() + timeout
        report: sup.SupervisionReport | None = None
        while time.monotonic() < deadline:
            report = self.inspect(intent)
            if report.status in wanted:
                return report
            time.sleep(0.4)
        details = f"status={report.status} notes={report.notes}" if report else "none"
        pytest.fail(f"receipt never reached {wanted}: {details}")

    def receipt_state(self, intent: SupervisionIntent) -> str:
        _rc, out, _err = self.ssh(
            f"cat {shlex.quote(RECEIPT_ROOT)}/{intent.op_id}/state 2>/dev/null"
        )
        return out

    def kill_tree(self, payload_pid: str) -> None:
        if payload_pid.isdigit():
            self.ssh(f"kill -KILL -{payload_pid} 2>/dev/null; true", timeout=15.0)
            time.sleep(0.5)

    def cleanup(self) -> str:
        rm = _docker("rm", "-f", self.container, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{self.container}$", "--format", "{{.Names}}"
        )
        strays = [
            name
            for name in _docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
            if name.startswith("mai-fxt-04-supervisor")
        ]
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.cleanup_proof = (
            f"container {self.container} after rm: '{listing.stdout.strip() or '<none>'}'; "
            f"stray supervisor fixtures: {strays or 'none'}; "
            f"scratch exists: {self.scratch.exists()}"
        )
        assert rm.returncode == 0
        return self.cleanup_proof


class _PlainArchFixture(_ArchSshdFixture):
    """Plain sshd-as-PID1 container: the measured wrapper variant target."""


class _SystemdArchFixture(_ArchSshdFixture):
    """systemd-as-PID1 container: the transient-unit variant (SSH-SYSV1).

    Privileged with a PRIVATE cgroup namespace (no host cgroup bind mount)
    and tmpfs /run — the contained form boundary-verified before these
    tests were written; sshd and the fixture user are provisioned as a
    system unit under the nested init.
    """

    container = SYSTEMD_CONTAINER

    def _run_args(self) -> list[str]:
        return [
            "--privileged",
            "--cgroupns=private",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            "--tmpfs",
            "/tmp",
            "-p",
            "127.0.0.1::22",
            FIXTURE_IMAGE_TAG,
            "/sbin/init",
        ]

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 120.0
        last = ""
        while time.monotonic() < deadline:
            check = _docker("exec", self.container, "systemctl", "is-system-running")
            state = check.stdout.strip()
            if state in ("running", "degraded"):
                return
            last = state or check.stderr[-200:]
            time.sleep(0.5)
        raise AssertionError(f"fixture systemd never became ready: {last}")

    def _provision(self) -> None:
        steps = (
            "systemctl start sshd && "
            f"useradd -m -s /bin/sh {FIXTURE_USER} && "
            f"loginctl enable-linger {FIXTURE_USER}"
        )
        result = _docker("exec", self.container, "sh", "-c", steps)
        assert result.returncode == 0, result.stderr[-800:]
        super()._provision()


def _class_fixture(fixture_cls: type[_ArchSshdFixture]):
    if not _docker_available():
        pytest.skip("docker unavailable: Arch supervisor fixture tests require it")
    fixture = fixture_cls()
    try:
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture(scope="class")
def plain_arch() -> _PlainArchFixture:
    yield from _class_fixture(_PlainArchFixture)


@pytest.fixture(scope="class")
def systemd_arch() -> _SystemdArchFixture:
    yield from _class_fixture(_SystemdArchFixture)


# ---------------------------------------------------------------------------
# Real boundary: wrapper variant on the plain Arch target
# ---------------------------------------------------------------------------


class TestAgainstPlainArch:
    """Wrapper variant end-to-end on a real disposable target."""

    def test_scheme_probe_selects_the_measured_wrapper(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        key_path, known_hosts = plain_arch.transport()
        probed = probe_supervision_scheme(
            plain_arch.identity(), key_path=key_path, known_hosts_path=known_hosts
        )
        assert probed.scheme is SupervisionScheme.WRAPPER
        assert probed.pid1_comm  # sshd in this fixture
        assert probed.boot_id  # boot identity recorded at probe time

    def test_submit_persists_intent_and_inspects_running(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        handle = plain_arch.submit(_request())
        intent = handle.intent
        assert intent.scheme is SupervisionScheme.WRAPPER
        assert intent.status == "submitted"
        assert load_intent(Path(handle.intent_path).parent, intent.op_id) == intent
        report = plain_arch.wait_status(intent, {CommandStatus.RUNNING})
        assert report.identity_resolved is True
        assert report.heartbeat_age is not None and report.heartbeat_age < 5.0
        assert len(report.descendants) == SLEEP_COUNT
        assert all("sleep" in args for _pid, args in report.descendants)
        assert report.receipt_boot_id  # boot identity in the receipt
        assert report.notes == ()
        plain_arch.cancel(intent)  # leave the fixture clean

    def test_completion_yields_true_exit_and_bounded_output(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        request = _request(shell="echo supervised-out; echo supervised-err >&2; exit 5")
        intent = plain_arch.submit(request).intent
        report = plain_arch.wait_status(intent, {CommandStatus.FAILED})
        assert report.status is CommandStatus.FAILED
        assert report.exit_code == 5
        assert "supervised-out" in report.stdout
        assert "supervised-err" in report.stderr
        assert report.stdout_truncated is False
        fields, problems = parse_state_file(plain_arch.receipt_state(intent))
        assert problems == ()
        assert fields["state"] == "exited"
        assert fields["exit_code"] == "5"
        assert fields["op_id"] == str(intent.op_id)

    def test_disconnect_leaves_running_status_never_success_or_cancel(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        # The dispatch channel is closed by design at the ack; the detached
        # tree (measured exp1b) keeps running and stays inspectable from a
        # NEW session — no automatic success, no automatic cancellation.
        intent = plain_arch.submit(_request()).intent
        report = plain_arch.wait_status(intent, {CommandStatus.RUNNING})
        assert report.status is CommandStatus.RUNNING
        fields, _problems = parse_state_file(plain_arch.receipt_state(intent))
        assert fields["state"] == "running"
        assert fields.get("exit_code", "") == ""
        cancel_report = plain_arch.cancel(intent)
        assert cancel_report.status is CommandStatus.CANCELLED
        assert cancel_report.exit_code == 143
        assert cancel_report.closure_empty is True
        # the receipt records the ACTUAL exit; the frozen mapping follows it
        after = plain_arch.inspect(intent)
        assert after.status is CommandStatus.FAILED
        assert after.exit_code == 143
        _rc, events, _err = plain_arch.ssh(
            f"cat {shlex.quote(RECEIPT_ROOT)}/{intent.op_id}/events.log"
        )
        assert "note cancel_requested" in events
        assert "state=cancelled" not in events  # receipts never say cancelled

    def test_cancel_refuses_forged_ticks_and_signals_nothing(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        intent = plain_arch.submit(_request()).intent
        report = plain_arch.wait_status(intent, {CommandStatus.RUNNING})
        original_ticks = report.payload_startticks
        assert original_ticks.isdigit()
        state_path = f"{shlex.quote(RECEIPT_ROOT)}/{intent.op_id}/state"
        # PID-reuse negative control (measured exp2): forged start ticks
        plain_arch.ssh(
            f"sed -i 's/payload_startticks={original_ticks}/payload_startticks=1/' {state_path}"
        )
        with pytest.raises(CancelRefusedError) as excinfo:
            plain_arch.cancel(intent)
        assert "nothing signalled" in excinfo.value.detail
        # nothing was signalled: the tree is still alive
        _rc, procs, _err = plain_arch.ssh("ps -o pid,ppid,args 2>/dev/null")
        assert procs.count("sleep 60") >= SLEEP_COUNT
        # restore the true receipt and cancel for real
        plain_arch.ssh(
            f"sed -i 's/payload_startticks=1/payload_startticks={original_ticks}/' {state_path}"
        )
        cancel_report = plain_arch.cancel(intent)
        assert cancel_report.status is CommandStatus.CANCELLED
        assert cancel_report.exit_code == 143
        _rc, procs_after, _err = plain_arch.ssh("ps -o pid,ppid,args 2>/dev/null")
        assert "sleep 60" not in procs_after

    def test_supervisor_death_stays_unknown_and_freezes_the_receipt(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        # measured exp3: kill -9 the WRAPPER mid-run — the payload orphans
        # and lives, heartbeats stop, the receipt freezes at running with no
        # exit record; the outcome is outcome_unknown forever, never an
        # automatic success and never a cancellation (acceptance 2).
        intent = plain_arch.submit(_request()).intent
        report = plain_arch.wait_status(intent, {CommandStatus.RUNNING})
        assert report.wrapper_pid.isdigit()
        plain_arch.ssh(f"kill -9 {report.wrapper_pid}")
        deadline = time.monotonic() + 20.0
        last = report
        while time.monotonic() < deadline:
            last = plain_arch.inspect(intent)
            if any(
                "heartbeat stale" in note or "no heartbeat" in note
                for note in last.notes
            ):
                break
            time.sleep(0.5)
        assert last.status is CommandStatus.OUTCOME_UNKNOWN
        assert last.exit_code is None
        fields, _problems = parse_state_file(plain_arch.receipt_state(intent))
        assert fields["state"] == "running"  # frozen, no exit record
        assert fields.get("exit_code", "") == ""
        # the orphaned tree is still alive (unknown ≠ dead) — explicit cleanup
        plain_arch.kill_tree(fields["payload_pid"])
        fields_after, _problems = parse_state_file(plain_arch.receipt_state(intent))
        assert fields_after["state"] == "running"  # still no exit: wrapper dead

    def test_missing_receipt_is_unknown_never_retry_safe(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        intent = plain_arch.submit(_request()).intent
        report = plain_arch.wait_status(intent, {CommandStatus.RUNNING})
        plain_arch.ssh(f"rm -rf {shlex.quote(RECEIPT_ROOT)}/{intent.op_id}")
        inspected = plain_arch.inspect(intent)
        assert inspected.status is CommandStatus.OUTCOME_UNKNOWN
        assert inspected.receipt_missing is True
        assert inspected.exit_code is None
        plain_arch.kill_tree(report.payload_pid)

    def test_binary_output_decodes_with_replacement_not_fatal(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        request = _request(
            shell="head -c 3000 /dev/urandom; head -c 1500 /dev/urandom >&2"
        )
        intent = plain_arch.submit(request).intent
        report = plain_arch.wait_status(intent, {CommandStatus.SUCCEEDED})
        assert report.exit_code == 0
        assert report.stdout_bytes >= 3000
        assert report.stderr_bytes >= 1500
        assert report.stdout_truncated is False

    def test_output_retention_is_explicit_first_n_with_totals(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        request = _request(shell="head -c 200000 /dev/zero | tr '\\0' 'x'")
        intent = plain_arch.submit(request).intent
        report = plain_arch.wait_status(intent, {CommandStatus.SUCCEEDED})
        assert report.stdout_bytes >= 200000
        bounded = plain_arch.inspect(intent, output_budget=1024)
        assert bounded.stdout_truncated is True
        assert len(bounded.stdout) <= 2048  # budget-bound retention

    def test_privileged_payload_supervises_under_the_sudo_grant(
        self, plain_arch: _PlainArchFixture
    ) -> None:
        # Privilege-mode composition at the real boundary: with a
        # command-scoped NOPASSWD grant for the escalation shell, the
        # supervised payload REALLY runs as uid 0 and the receipt records
        # the true exit; without touching the grant the same supervised
        # escalation fails as a payload failure (no invented success).
        plain_arch.write_sudo_grant()
        try:
            request = _request(argv=("/usr/bin/id", "-u"), timeout=30.0)
            intent = plain_arch.submit(request, PrivilegeMode.SUDO).intent
            report = plain_arch.wait_status(intent, {CommandStatus.SUCCEEDED})
            assert report.exit_code == 0
            assert report.stdout.strip() == "0"  # root on the disposable target
        finally:
            plain_arch.remove_sudo_grant()


# ---------------------------------------------------------------------------
# Real boundary: systemd transient-unit variant (SSH-SYSV1)
# ---------------------------------------------------------------------------


class TestAgainstSystemdArch:
    """Transient-unit variant on a real systemd-capable disposable target."""

    def test_scheme_probe_selects_the_transient_unit(
        self, systemd_arch: _SystemdArchFixture
    ) -> None:
        key_path, known_hosts = systemd_arch.transport()
        probed = probe_supervision_scheme(
            systemd_arch.identity(), key_path=key_path, known_hosts_path=known_hosts
        )
        assert probed.scheme is SupervisionScheme.SYSTEMD_UNIT
        assert probed.pid1_comm == "systemd"
        assert probed.boot_id

    def test_submit_complete_and_receipt_contract_parity(
        self, systemd_arch: _SystemdArchFixture
    ) -> None:
        request = _request(shell="sleep 5; echo systemd-out; exit 0")
        intent = systemd_arch.submit(request).intent
        assert intent.scheme is SupervisionScheme.SYSTEMD_UNIT
        assert intent.boot_id  # boot identity persisted at dispatch
        running = systemd_arch.wait_status(intent, {CommandStatus.RUNNING})
        assert running.identity_resolved is True
        assert [args for _pid, args in running.descendants] == ["sleep"]
        final = systemd_arch.wait_status(intent, {CommandStatus.SUCCEEDED})
        assert final.exit_code == 0
        assert "systemd-out" in final.stdout
        fields, problems = parse_state_file(systemd_arch.receipt_state(intent))
        assert problems == ()
        # receipt-contract parity with the wrapper variant: the SAME frozen
        # keys and transitions (the SAME script writes both variants' receipts)
        assert set(fields) == {
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
        assert fields["boot_id"] == intent.boot_id
        _rc, events, _err = systemd_arch.ssh(
            f"cat {shlex.quote(RECEIPT_ROOT)}/{intent.op_id}/events.log"
        )
        transitions = [line for line in events.splitlines() if " state=" in line]
        states = [line.split("state=")[1].split()[0] for line in transitions]
        assert states == ["starting", "running", "exited"]

    def test_disconnect_survival_and_verified_cancel(
        self, systemd_arch: _SystemdArchFixture
    ) -> None:
        # The user manager (linger) owns the transient unit across the ssh
        # channel loss; cancellation uses the same measured pgid signal path.
        intent = systemd_arch.submit(_request()).intent
        report = systemd_arch.wait_status(intent, {CommandStatus.RUNNING})
        assert report.status is CommandStatus.RUNNING
        cancel_report = systemd_arch.cancel(intent)
        assert cancel_report.status is CommandStatus.CANCELLED
        assert cancel_report.exit_code == 143
        assert cancel_report.closure_empty is True
        final = systemd_arch.inspect(intent)
        assert final.exit_code == 143
        # the collected transient unit is gone after the wrapper's exit
        _rc, active, _err = systemd_arch.ssh(
            f"systemctl --user is-active {intent.unit_name}; true"
        )
        assert active.strip() in ("inactive", "failed", "unknown")
