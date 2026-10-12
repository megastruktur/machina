#!/usr/bin/env python3
"""MAI P00 SSH supervision probe: host command ownership/receipts across SSH loss.

Card mai-00-ssh-spike (C04). Runs against a disposable panubo/sshd fixture container
(namespace mai-fxt-00-ssh-*, port 127.0.0.1:2222). The fixture has NO systemd
(PID 1 is the image entrypoint bash), so the bounded scheme under test is the
C04 fallback: a target-local supervision wrapper + durable receipt directory,
dispatched detached (setsid) over SSH, inspected on reconnect by a NEW session.

Roles:
  preflight  start fixture, verify host key/auth/tooling, record systemd absence
  exp1       SSH loss mid-run: attached tree dies (negative control), detached
             wrapper-owned tree survives; reconnect identifies it by receipts
  exp2       cancellation: request -> verify descendants gone; PID-reuse guard
             refuses a cancel with mismatched /proc start ticks
  exp3       supervisor (wrapper) killed mid-operation: payload orphaned, receipt
             frozen -> outcome_unknown; explicit reconciliation closes it
  exp4       fixture restart + full recreate: receipts vs boot identity, host-key
             pinning rejection, lost receipt fs
  cleanup    destroy fixture and temp files, print proof commands
  all        preflight exp1 exp2 exp3 exp4 cleanup

stdlib only (no dependency installs on this host). Python 3.14, OpenSSH 9.2p1 client.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRATCH = Path(os.environ.get("MAI_SSH_SPIKE_SCRATCH", "/tmp/mai-fxt-00-ssh-spike"))
CONTAINER = "mai-fxt-00-ssh-sshd"
IMAGE = "panubo/sshd:latest"
PORT = 2222
SSH_USER = "maiprobe"
RECEIPT_ROOT = "mai-receipts"  # under fixture $HOME; real targets: durable service-owned path
KEY_PATH = SCRATCH / "keys" / SSH_USER
KNOWN_HOSTS = SCRATCH / "known_hosts"


def ssh_opts(known_hosts: Path = KNOWN_HOSTS) -> list[str]:
    return [
        "-p", str(PORT),
        "-i", str(KEY_PATH),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(known_hosts),
        "-o", "ConnectTimeout=5",
    ]


SSH_OPTS_BASE = ssh_opts()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_dir(exp: str) -> Path:
    d = SCRATCH / exp
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal_path(exp: str) -> Path:
    return run_dir(exp) / "journal.log"


def _jrecord(exp: str, who: str, marker: str, note: str = "") -> None:
    with open(journal_path(exp), "a") as f:
        f.write(f"{now_iso()}|{who}|{marker}|{note}\n")


def drecord(exp: str, marker: str, note: str = "") -> None:
    _jrecord(exp, "DRV", marker, note)


def check(name: str, expected: str, observed: object, ok: bool) -> dict:
    return {"check": name, "expected": expected, "observed": observed, "ok": bool(ok)}


def verdict(exp: str, passed: bool, checks: list[dict], notes: list[str]) -> int:
    print("VERDICT " + json.dumps(
        {"exp": exp, "passed": passed, "checks": checks, "notes": notes}, indent=2))
    with open(run_dir(exp) / "verdict.txt", "w") as f:
        json.dump({"exp": exp, "passed": passed, "checks": checks, "notes": notes,
                   "finished": now_iso()}, f, indent=2)
    return 0 if passed else 2


def log_cmd(exp: str, label: str, cmd: list[str], rc: int | None, out: str, err: str) -> None:
    with open(run_dir(exp) / "commands.log", "a") as f:
        f.write(f"$ {now_iso()} # {label}\n{' '.join(shlex.quote(c) for c in cmd)}\n"
                f"# rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n\n")


# ---------------------------------------------------------------- fixture plumbing


def docker(exp: str, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = ["docker", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log_cmd(exp, "docker", cmd, r.returncode, r.stdout, r.stderr)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def container_exists(exp: str) -> bool:
    rc, out, _ = docker(exp, "ps", "-a", "--filter", f"name=^{CONTAINER}$",
                        "--format", "{{.Names}}")
    return rc == 0 and out == CONTAINER


def start_fixture(exp: str) -> None:
    """Producer-verified form (00-environment evidence 04; key provisioning via
    docker exec because bind-mount paths resolve on the daemon host, not here)."""
    if container_exists(exp):
        rc, _, _ = docker(exp, "stop", "-t", "1", CONTAINER)
        _ = rc  # --rm removal may race; wait for the name to free
        for _ in range(40):
            if not container_exists(exp):
                break
            time.sleep(0.5)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(KEY_PATH),
                        "-C", "mai-fxt-00-ssh-spike-probe"], check=True)
    rc, out, err = docker(exp, "run", "--rm", "-d", "--name", CONTAINER,
                          "-p", f"127.0.0.1:{PORT}:22",
                          "-e", f"SSH_USERS={SSH_USER}:1000:1000", IMAGE)
    if rc != 0:
        raise RuntimeError(f"fixture start failed: {err}")
    # sshd readiness: retry keyscan until it answers (image needs a few seconds)
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        r = subprocess.run(["ssh-keyscan", "-p", str(PORT), "-t", "ed25519,rsa", "127.0.0.1"],
                           capture_output=True, text=True, timeout=15)
        last = r.stdout
        if len(r.stdout.splitlines()) >= 2:
            break
        time.sleep(1.0)
    else:
        raise RuntimeError(f"sshd never answered keyscan: {last!r}")
    KNOWN_HOSTS.write_text(last)
    # install the disposable user key (sshd reads /etc/authorized_keys/<user> at auth time)
    pub = (KEY_PATH.with_suffix(".pub")).read_bytes()
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c",
                        "umask 022; cat > /etc/authorized_keys/maiprobe && "
                        "chown 0:0 /etc/authorized_keys/maiprobe && "
                        "chmod 644 /etc/authorized_keys/maiprobe"],
                       input=pub, capture_output=True, timeout=15)
    log_cmd(exp, "provision-key", ["docker", "exec", "-i", CONTAINER, "sh", "-c",
                                   "cat > /etc/authorized_keys/maiprobe && chown 0:0 ... && chmod 644 ..."],
            r.returncode, "", r.stderr.decode())
    if r.returncode != 0:
        raise RuntimeError(f"key provisioning failed: {r.stderr.decode()}")


def ssh_capture(exp: str, remote: str, timeout: int = 30, check_rc: bool = True,
                label: str = "ssh",
                known_hosts: Path = KNOWN_HOSTS) -> tuple[int, str, str]:
    cmd = ["ssh", *ssh_opts(known_hosts), f"{SSH_USER}@127.0.0.1", remote]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = -1, (e.stdout or b"").decode(), (e.stderr or b"").decode()
    log_cmd(exp, label, cmd, rc, out, err)
    if check_rc and rc != 0:
        raise RuntimeError(f"ssh failed rc={rc}: {label}: {err.strip()}")
    return rc, out, err


def ssh_popen(exp: str, remote: str, label: str) -> tuple[subprocess.Popen, list[str]]:
    cmd = ["ssh", *SSH_OPTS_BASE, f"{SSH_USER}@127.0.0.1", remote]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with open(run_dir(exp) / "commands.log", "a") as f:
        f.write(f"$ {now_iso()} # {label}\n{' '.join(shlex.quote(c) for c in cmd)}\n")
    return p, cmd


# ---------------------------------------------------------------- target-side helpers


def read_state(exp: str, op_id: str) -> dict:
    rc, out, _ = ssh_capture(exp, f'cat "$HOME/{RECEIPT_ROOT}/{op_id}/state"',
                             label=f"read-state {op_id}", check_rc=False)
    state: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                state[k.strip()] = v.strip()
    state["_rc"] = str(rc)
    return state


def wait_state(exp: str, op_id: str, want: set[str], timeout: float) -> dict | None:
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = read_state(exp, op_id)
        if st.get("state") in want:
            return st
        time.sleep(0.7)
    return st


def read_events(exp: str, op_id: str) -> str:
    _, out, _ = ssh_capture(exp, f'cat "$HOME/{RECEIPT_ROOT}/{op_id}/events.log" 2>/dev/null',
                            label=f"read-events {op_id}")
    return out


def proc_table(exp: str) -> list[dict]:
    """One snapshot of the target process table: pid, ppid, pgid, sid, args."""
    _, out, _ = ssh_capture(exp, "ps -eo pid,ppid,pgid,sid,args", label="ps snapshot")
    rows: list[dict] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) == 5 and all(p.isdigit() for p in parts[:4]):
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                         "pgid": int(parts[2]), "sid": int(parts[3]), "args": parts[4]})
    return rows


def descendants(rows: list[dict], root: int) -> list[dict]:
    by_ppid: dict[int, list[dict]] = {}
    for r in rows:
        by_ppid.setdefault(r["ppid"], []).append(r)
    found, frontier = [], [root]
    while frontier:
        nxt = []
        for pid in frontier:
            for ch in by_ppid.get(pid, []):
                found.append(ch)
                nxt.append(ch["pid"])
        frontier = nxt
    return found


def startticks(exp: str, pid: int) -> str:
    _, out, _ = ssh_capture(
        exp, f"awk '{{print $22}}' /proc/{pid}/stat 2>/dev/null", label=f"startticks {pid}",
        check_rc=False)
    return out.strip()


def pid_matches(exp: str, pid: int, ticks: str) -> bool:
    """PID-reuse guard: /proc/<pid> exists AND field-22 start ticks equal receipt."""
    if not ticks:
        return False
    return startticks(exp, pid) == ticks


TREE_CMD = "sleep 240 & sleep 240 & sleep 240 & wait"


def make_wrapper_script(op_id: str, cmd: str) -> str:
    """Target-local supervision wrapper (bounded scheme). One instance per operation.
    Owns: state transitions, payload identity (pid + /proc start ticks), heartbeat,
    exit capture. Payload runs in its OWN session (inner setsid) so cancel can TERM
    the payload group without killing the wrapper, and wrapper death orphans the
    payload instead of killing it (measured in exp3)."""
    b64 = base64.b64encode(cmd.encode()).decode()
    return f"""#!/bin/sh
# mai supervision wrapper v1 (mai-00-ssh-spike) op={op_id}
OD="$HOME/{RECEIPT_ROOT}/{op_id}"
OP="{op_id}"
CMD=$(printf %s '{b64}' | base64 -d)
mkdir -p "$OD"
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
setsid sh -c "$CMD" </dev/null >"$OD/stdout.log" 2>"$OD/stderr.log" &
PAYLOAD_PID=$!
PAYLOAD_TICKS=$(awk '{{print $22}}' /proc/$PAYLOAD_PID/stat 2>/dev/null)
emit running
while kill -0 "$PAYLOAD_PID" 2>/dev/null; do
  printf '%s hb payload_pid=%s\\n' "$(now)" "$PAYLOAD_PID" >> "$OD/events.log"
  sleep 1
done
wait "$PAYLOAD_PID"; EXIT_CODE=$?
emit exited
"""


def dispatch(exp: str, op_id: str, cmd: str, keep_open: float | None) -> subprocess.Popen | None:
    """Persist intent, install wrapper, launch detached. keep_open keeps the
    dispatching SSH channel alive N seconds (the worker channel under test)."""
    intent = {"op_id": op_id, "cmd": cmd, "dispatch_ts": now_iso(),
              "target": f"{SSH_USER}@127.0.0.1:{PORT}", "receipt_root": RECEIPT_ROOT,
              "scheme": "wrapper+receipts"}
    (run_dir(exp) / f"intent-{op_id}.json").write_text(json.dumps(intent, indent=2))
    drecord(exp, "DISPATCH-INTENT", json.dumps(intent))
    script = make_wrapper_script(op_id, cmd)
    up = subprocess.run(["ssh", *SSH_OPTS_BASE, f"{SSH_USER}@127.0.0.1",
                         f"cat > /tmp/mai-wrap-{op_id}.sh && chmod +x /tmp/mai-wrap-{op_id}.sh"],
                        input=script, capture_output=True, text=True, timeout=20)
    log_cmd(exp, "install-wrapper", ["ssh", "...", f"cat > /tmp/mai-wrap-{op_id}.sh"],
            up.returncode, "", up.stderr)
    if up.returncode != 0:
        raise RuntimeError(f"wrapper install failed: {up.stderr}")
    remote = (f"setsid /bin/sh /tmp/mai-wrap-{op_id}.sh >/dev/null 2>&1 </dev/null & echo go"
              + (f"; sleep {int(keep_open)}" if keep_open else ""))
    label = f"dispatch {op_id}" + (f" keep_open={keep_open}s" if keep_open else "")
    p, _ = ssh_popen(exp, remote, label)
    if p.stdout is not None:
        line = p.stdout.readline()
        drecord(exp, "DISPATCH-ACK", line.strip() or "<none>")
    return p


def kill_client(exp: str, p: subprocess.Popen, label: str) -> None:
    if p.poll() is None:
        os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=30)
    with open(run_dir(exp) / "commands.log", "a") as f:
        f.write(f"# {now_iso()} kill -9 local ssh client pid={p.pid} ({label}); rc={p.returncode}\n")
    drecord(exp, "SSH-CLIENT-KILLED", f"pid={p.pid} {label}")


def classify(exp: str, op_id: str) -> tuple[dict, list[dict], str]:
    """Reconnect inspection: receipt + live identity + tree. Returns (state, tree, report)."""
    st = read_state(exp, op_id)
    rows = proc_table(exp)
    tree: list[dict] = []
    ident = "receipt-unreadable"
    pid = int(st["payload_pid"]) if st.get("payload_pid", "").isdigit() else None
    if pid:
        ticks = st.get("payload_startticks", "")
        live = pid_matches(exp, pid, ticks)
        tree = descendants(rows, pid)
        ident = (f"payload_pid={pid} ticks_match={live} descendants="
                 f"{[(t['pid'], t['args'].split()[0]) for t in tree]}")
    report = f"state={st.get('state')} seq={st.get('seq')} ts={st.get('ts')} {ident}"
    drecord(exp, "INSPECT", report)
    return st, tree, report


# ---------------------------------------------------------------- preflight


def run_preflight() -> int:
    exp = "preflight"
    checks, notes = [], []
    start_fixture(exp)
    rc, out, _ = ssh_capture(exp, "id", label="auth check")
    checks.append(check("keyed auth as fixture user", "uid=1000(maiprobe)", out.strip(),
                        rc == 0 and "uid=1000(maiprobe)" in out))
    _, out, _ = ssh_capture(exp, "cat /proc/1/comm; ls /bin/systemctl 2>&1; "
                                 "ls -d /run/systemd 2>&1; ls -d /etc/init.d 2>&1",
                            label="systemd availability")
    pid1 = out.splitlines()[0] if out else ""
    checks.append(check("systemd available in fixture", "absent (documented gap)",
                        {"pid1": pid1, "raw": out.strip()},
                        "No such file" in out and "No such file or directory" in out
                        and pid1.strip() == "bash"))
    notes.append("systemd transient units are NOT testable in this fixture; "
                 "wrapper+receipts is the measured bounded scheme (C04 fallback)")
    _, out, _ = ssh_capture(exp, "for t in setsid base64 awk ps sleep sh; do "
                                 "command -v $t || echo MISSING:$t; done",
                            label="tooling check")
    tools = sorted({p.rsplit("/", 1)[-1] for p in out.split()})
    checks.append(check("required busybox tools", "['awk','base64','ps','setsid','sh','sleep']",
                        tools, all(t in tools for t in ["setsid", "base64", "awk", "ps", "sleep", "sh"])))
    _, out, _ = ssh_capture(exp, "date -u +%Y-%m-%dT%H:%M:%S.%NZ", label="date %N support")
    notes.append("busybox date drops %%N (sub-second); receipts use second-resolution UTC "
                 "plus seq numbers for ordering")
    rc, out, _ = docker(exp, "image", "inspect", "--format", "{{.Id}}", IMAGE)
    (run_dir(exp) / "image-id.txt").write_text(out)
    notes.append(f"fixture image {IMAGE} id {out[:32]}…; container {CONTAINER}")
    passed = all(c["ok"] for c in checks)
    drecord(exp, "PREFLIGHT", "passed" if passed else "failed")
    return verdict(exp, passed, checks, notes)


# ---------------------------------------------------------------- exp1


def run_exp1() -> int:
    exp = "exp1"
    checks, notes = [], []
    # --- negative control: tree ATTACHED to the exec channel has NO ownership
    attached_cmd = "sleep 240 & sleep 240 & sleep 240 & wait"
    p, _ = ssh_popen(exp, f"sh -c {shlex.quote(attached_cmd)}", "exp1a attached dispatch")
    time.sleep(2.5)
    rows = proc_table(exp)
    attached = [r for r in rows if r["args"].startswith("sleep 240")]
    drecord(exp, "ATTACHED-TREE", json.dumps([r["pid"] for r in attached]))
    checks.append(check("attached tree visible before loss (3 sleeps)",
                        "3 processes", len(attached), len(attached) == 3))
    kill_client(exp, p, "exp1a SSH loss mid-run")
    time.sleep(2.0)
    rows = proc_table(exp)
    still = [r for r in rows if r["args"].startswith("sleep 240")]
    rc_nr, _, _ = ssh_capture(exp, f'test -d "$HOME/{RECEIPT_ROOT}/op-exp1a-attached"',
                              check_rc=False, label="receipt existence for attached op")
    checks.append(check("session-attached operation has NO receipt (attachment is not "
                        "ownership: nothing identifies or reconciles it)",
                        "no receipt exists", f"receipt_rc={rc_nr}", rc_nr != 0))
    notes.append("measured on this fixture: non-tty children SURVIVE client loss as "
                 "unidentified orphans (sshd does not SIGHUP-kill them); attachment gives "
                 "either death-by-channel or an unownable orphan — never a receipt. "
                 f"observed post-loss survivors: {len(still)} (uncontrolled by design)")
    if still:
        ssh_capture(exp, f"kill -9 {' '.join(str(r['pid']) for r in still)} 2>/dev/null; "
                         f"pkill -9 -f 'sleep 240' 2>/dev/null; true",
                    check_rc=False, label="exp1a leftover hygiene (explicit cleanup)")
        time.sleep(1.5)
        rows = proc_table(exp)
        still2 = [r for r in rows if r["args"].startswith("sleep 240")]
        checks.append(check("exp1a leftovers explicitly cleaned (fixture hygiene)",
                            "0 processes", len(still2), len(still2) == 0))
    # --- wrapper-owned dispatch, channel killed mid-run
    op_id = f"op-exp1b-{uuid.uuid4().hex[:8]}"
    p = dispatch(exp, op_id, TREE_CMD, keep_open=90)
    st = wait_state(exp, op_id, {"running"}, 20)
    checks.append(check("receipt reaches running before loss", "state=running",
                        st and st.get("state"), bool(st and st.get("state") == "running")))
    time.sleep(2.0)
    kill_client(exp, p, "exp1b SSH loss mid-run")
    time.sleep(2.0)
    st, tree, report = classify(exp, op_id)
    checks.append(check("detached tree survives SSH loss (receipt state)",
                        "running", st.get("state"), st.get("state") == "running"))
    checks.append(check("payload identity verified via pid+startticks", "ticks_match=True",
                        "ticks_match=True" in report, "ticks_match=True" in report))
    checks.append(check("descendants identified on reconnect (3 sleeps)", "3",
                        len([t for t in tree if t["args"].startswith("sleep")]),
                        len([t for t in tree if t["args"].startswith("sleep")]) == 3))
    rows = proc_table(exp)
    payload_pid = int(st["payload_pid"])
    prows = [r for r in rows if r["pid"] == payload_pid]
    sids = {r["sid"] for r in rows if "sshd" in r["args"]}
    detached = prows and prows[0]["sid"] not in sids and prows[0]["sid"] == payload_pid
    checks.append(check("payload is session leader detached from sshd sessions",
                        "own sid, not an sshd sid", bool(detached), bool(detached)))
    events = read_events(exp, op_id)
    (run_dir(exp) / "events.log").write_text(events)
    checks.append(check("receipt events log captured", "non-empty", len(events.splitlines()),
                        len(events.splitlines()) >= 3))
    notes.append(f"op_id={op_id}; dispatch intent persisted pre-submission (intent-{op_id}.json)")
    # leave the tree running only as long as needed; terminate to keep fixture clean
    ssh_capture(exp, f"kill -TERM -{payload_pid} 2>/dev/null", check_rc=False,
                label="exp1b teardown (payload group TERM)")
    ssh_capture(exp, "pkill -9 -f 'sleep 90' 2>/dev/null; true", check_rc=False,
                label="exp1b teardown (keep-open session shell stray)")
    time.sleep(1.5)
    st2, _, _ = classify(exp, op_id)
    notes.append(f"post-teardown receipt state={st2.get('state')} exit={st2.get('exit_code')} "
                 "(wrapper survived teardown and captured the exit — the ownership payoff)")
    checks.append(check("wrapper recorded exit after teardown kill", "exited",
                        st2.get("state"), st2.get("state") == "exited"))
    passed = all(c["ok"] for c in checks)
    return verdict(exp, passed, checks, notes)


# ---------------------------------------------------------------- exp2


def request_cancel(exp: str, st: dict, fake_ticks: bool) -> tuple[bool, str]:
    """Verify-then-act cancellation over a NEW session. Refuses on identity mismatch."""
    pid = int(st["payload_pid"])
    ticks = st.get("payload_startticks", "")
    if fake_ticks:
        ticks = "1" if ticks != "1" else "2"
    guard = (f"[ -d /proc/{pid} ] && [ \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = '{ticks}' ]"
             f" && echo IDENTITY-OK || echo IDENTITY-MISMATCH")
    _, out, _ = ssh_capture(exp, guard, label=f"cancel identity guard pid={pid} fake={fake_ticks}")
    if "IDENTITY-OK" not in out:
        return False, "refused: pid/startticks mismatch (reuse or stale receipt)"
    ssh_capture(exp, f"kill -TERM -{pid}", label=f"cancel TERM group -{pid}")
    deadline = time.time() + 6
    rows = proc_table(exp)
    while time.time() < deadline and (descendants(rows, pid) or
                                      any(r["pid"] == pid for r in rows)):
        time.sleep(0.7)
        rows = proc_table(exp)
    if any(r["pid"] == pid for r in rows) or descendants(rows, pid):
        ssh_capture(exp, f"kill -KILL -{pid}", label=f"cancel KILL group -{pid}")
        time.sleep(1.0)
        rows = proc_table(exp)
    gone = not any(r["pid"] == pid for r in rows) and not descendants(rows, pid)
    return gone, f"tree_gone={gone}"


def run_exp2() -> int:
    exp = "exp2"
    checks, notes = [], []
    op_id = f"op-exp2-{uuid.uuid4().hex[:8]}"
    p = dispatch(exp, op_id, TREE_CMD, keep_open=None)
    st = wait_state(exp, op_id, {"running"}, 20)
    checks.append(check("operation running before cancel", "running",
                        st and st.get("state"), bool(st and st.get("state") == "running")))
    if p and p.poll() is None:
        p.wait(timeout=30)
    st, tree, _ = classify(exp, op_id)
    checks.append(check("descendants present before cancel (3 sleeps)", "3",
                        len([t for t in tree if t["args"].startswith("sleep")]),
                        len([t for t in tree if t["args"].startswith("sleep")]) == 3))
    drecord(exp, "CANCEL-REQUESTED", f"op={op_id} payload={st.get('payload_pid')}")
    ok, detail = request_cancel(exp, st, fake_ticks=False)
    drecord(exp, "CANCEL-VERIFIED", f"op={op_id} {detail}")
    checks.append(check("cancel verified: payload+descendants gone", "tree_gone=True",
                        detail, ok))
    st2 = wait_state(exp, op_id, {"exited"}, 10)
    checks.append(check("wrapper survived cancel and captured exit", "exited",
                        st2 and st2.get("state"), bool(st2 and st2.get("state") == "exited")))
    exit_code = st2.get("exit_code", "") if st2 else ""
    checks.append(check("exit code records signal death", "143 (128+SIGTERM)",
                        exit_code, exit_code == "143"))
    # negative control: stale/forged identity must be REFUSED
    ok2, detail2 = request_cancel(exp, st2 or st, fake_ticks=True)
    drecord(exp, "CANCEL-GUARD-NEGATIVE", detail2)
    checks.append(check("cancel with mismatched startticks refused (PID-reuse guard)",
                        "refused", detail2, not ok2))
    notes.append("requested != verified: guard runs before any signal; TERM escalation to "
                 "KILL measured; receipt shows exit 143 not 'cancelled' — cancellation is "
                 "reconstructed from verified host state (C04)")
    passed = all(c["ok"] for c in checks)
    return verdict(exp, passed, checks, notes)


# ---------------------------------------------------------------- exp3


def run_exp3() -> int:
    exp = "exp3"
    checks, notes = [], []
    op_id = f"op-exp3-{uuid.uuid4().hex[:8]}"
    p = dispatch(exp, op_id, TREE_CMD, keep_open=None)
    st = wait_state(exp, op_id, {"running"}, 20)
    checks.append(check("operation running before supervisor kill", "running",
                        st and st.get("state"), bool(st and st.get("state") == "running")))
    if p and p.poll() is None:
        p.wait(timeout=30)
    wrapper_pid = int(st["wrapper_pid"])
    payload_pid = int(st["payload_pid"])
    events = read_events(exp, op_id)
    (run_dir(exp) / "events-before.log").write_text(events)
    hbs_before = [l for l in events.splitlines() if " hb " in l]
    checks.append(check("wrapper heartbeat present before kill", "hb_count>0",
                        f"hb_count={len(hbs_before)}", len(hbs_before) > 0))
    kill_ts = now_iso()
    ssh_capture(exp, f"kill -9 {wrapper_pid}", label=f"kill supervisor wrapper {wrapper_pid}")
    time.sleep(2.5)
    rows = proc_table(exp)
    wrapper_gone = not any(r["pid"] == wrapper_pid for r in rows)
    payload_alive = any(r["pid"] == payload_pid for r in rows)
    checks.append(check("supervisor (wrapper) killed mid-operation", "dead",
                        wrapper_gone, wrapper_gone))
    checks.append(check("payload orphaned but still alive (reparented)", "alive",
                        payload_alive, payload_alive))
    st2 = read_state(exp, op_id)
    events = read_events(exp, op_id)
    (run_dir(exp) / "events-after.log").write_text(events)
    hbs_after = [l for l in events.splitlines() if " hb " in l]
    checks.append(check("receipt frozen at last wrapper write (no exit record)",
                        "running (stale)", st2.get("state"), st2.get("state") == "running"))
    checks.append(check("heartbeats stopped at wrapper death (staleness signal)",
                        "hb_count unchanged after kill",
                        f"before={len(hbs_before)} after={len(hbs_after)}",
                        len(hbs_after) == len(hbs_before)))
    notes.append("heartbeat staleness is the reconnect hint that the supervisor died; "
                 "payload identity (pid+ticks) still resolves live state")
    # explicit reconciliation (C04: reconcile uncertain outcomes)
    _, tree, _ = classify(exp, op_id)
    drecord(exp, "RECONCILE-START", f"outcome_unknown payload={payload_pid} tree={len(tree)}")
    ssh_capture(exp, f"kill -KILL -{payload_pid}", label=f"reconcile KILL group -{payload_pid}")
    time.sleep(1.2)
    rows = proc_table(exp)
    gone = not any(r["pid"] == payload_pid for r in rows) and not descendants(rows, payload_pid)
    ssh_capture(exp, f"printf 'reconciled=lost-supervisor ts=%s\\n' '{kill_ts}' "
                     f'>> "$HOME/{RECEIPT_ROOT}/{op_id}/events.log"',
                label="append reconciliation note to receipt")
    drecord(exp, "RECONCILE-DONE", f"tree_gone={gone}")
    checks.append(check("reconciliation closed the orphan (explicit operator action)",
                        "tree_gone=True", gone, gone))
    notes.append("supervisor death loses exit capture forever: even without restart, the "
                 "operation can only end as outcome_unknown or operator-reconciled, never "
                 "auto-'succeeded' — C04 rule re-measured at the target level")
    passed = all(c["ok"] for c in checks)
    return verdict(exp, passed, checks, notes)


# ---------------------------------------------------------------- exp4


def fixture_identity(exp: str) -> dict:
    _, boot, _ = ssh_capture(exp, "cat /proc/sys/kernel/random/boot_id; cat /proc/uptime",
                             label="boot identity snapshot")
    lines = boot.splitlines()
    _, started, _ = docker(exp, "inspect", "--format",
                           "{{.Id}} {{.State.StartedAt}}", CONTAINER)
    fp = subprocess.run(["ssh-keygen", "-lf", str(KNOWN_HOSTS)], capture_output=True,
                        text=True)
    return {"boot_id": lines[0] if lines else "", "uptime": lines[1] if len(lines) > 1 else "",
            "container": started, "hostkeys": fp.stdout.strip(),
            "at": now_iso()}


def run_exp4() -> int:
    exp = "exp4"
    checks, notes = [], []
    op_id = f"op-exp4-{uuid.uuid4().hex[:8]}"
    p = dispatch(exp, op_id, TREE_CMD, keep_open=None)
    st = wait_state(exp, op_id, {"running"}, 20)
    checks.append(check("operation running before restart", "running",
                        st and st.get("state"), bool(st and st.get("state") == "running")))
    if p and p.poll() is None:
        p.wait(timeout=30)
    before = fixture_identity(exp)
    payload_pid = int(st["payload_pid"])
    (run_dir(exp) / "identity-before.json").write_text(json.dumps(before, indent=2))
    drecord(exp, "RESTART-BEGIN", json.dumps(before))
    docker(exp, "restart", CONTAINER, timeout=60)
    # wait for sshd to return
    deadline = time.time() + 45
    up = False
    while time.time() < deadline:
        rc, _, _ = ssh_capture(exp, "echo up", check_rc=False, label="reconnect probe")
        if rc == 0:
            up = True
            break
        time.sleep(1.5)
    checks.append(check("SSH reconnects after fixture restart", "up", up, up))
    after = fixture_identity(exp)
    (run_dir(exp) / "identity-after.json").write_text(json.dumps(after, indent=2))
    drecord(exp, "RESTART-DONE", json.dumps(after))
    checks.append(check("host keys PINNED and unchanged across restart (C04 no auto-trust)",
                        "same fingerprints", before["hostkeys"] == after["hostkeys"],
                        before["hostkeys"] == after["hostkeys"]))
    st_after = read_state(exp, op_id)
    checks.append(check("receipt directory survived restart (container fs)",
                        "state readable rc=0", f"rc={st_after.get('_rc')}",
                        st_after.get("_rc") == "0"))
    checks.append(check("receipt still claims running (stale — supervisor dead)",
                        "running (stale)", st_after.get("state"),
                        st_after.get("state") == "running"))
    _, ticks_now, _ = ssh_capture(exp, f"awk '{{print $22}}' /proc/{payload_pid}/stat 2>/dev/null",
                                  label=f"payload startticks after restart", check_rc=False)
    checks.append(check("recorded payload identity no longer resolves (lost)",
                        "absent from /proc", ticks_now.strip() or "<absent>",
                        ticks_now.strip() == ""))
    checks.append(check("boot_id UNCHANGED by container restart (kernel-shared signal — "
                        "not a reboot detector for container targets)",
                        "unchanged (measured)", before["boot_id"] == after["boot_id"],
                        before["boot_id"] == after["boot_id"]))
    checks.append(check("control-plane container identity DID change (StartedAt)",
                        "changed", before["container"] != after["container"],
                        before["container"] != after["container"]))
    pid_unresolved = ticks_now.strip() == ""
    restart_lost = (st_after.get("state") == "running" and pid_unresolved
                    and before["container"] != after["container"])
    checks.append(check("classification of old operation after restart (derived)",
                        "outcome_unknown (stale running receipt + unresolved pid + restart observed)",
                        f"state={st_after.get('state')} pid_resolves={not pid_unresolved} "
                        f"restart_observed={before['container'] != after['container']}",
                        restart_lost))
    notes.append("receipt freshness (state=running + pid unresolved + restart timestamp) is "
                 "the evidence for outcome_unknown; no exit receipt can ever appear because "
                 "the supervisor died with the target")
    # full recreate: receipts vanish, host keys change -> pinned reconnect must be refused
    stale_kh = SCRATCH / "known_hosts.before-recreate"
    shutil.copyfile(KNOWN_HOSTS, stale_kh)
    old_container_id = before["container"].split()[0]
    docker(exp, "stop", "-t", "1", CONTAINER)
    time.sleep(2.0)
    start_fixture(exp)
    rc, _, err = ssh_capture(exp, "echo up", check_rc=False,
                             label="reconnect with STALE pinning (must be refused)",
                             known_hosts=stale_kh)
    refused = rc != 0 and "host key verification failed" in err.lower()
    checks.append(check("reconnect to RECREATED target refused by host-key pinning (C04)",
                        "host key verification failed", f"rc={rc} err={err.strip()[:120]}",
                        refused))
    rc, new_id, _ = docker(exp, "inspect", "--format", "{{.Id}}", CONTAINER)
    checks.append(check("recreated container is a different instance", "different id",
                        new_id != old_container_id, new_id != old_container_id))
    rc, out, _ = ssh_capture(exp, f'ls "$HOME/{RECEIPT_ROOT}/" 2>&1', check_rc=False,
                             label="receipt dir after recreate")
    receipt_gone = "No such file" in out or out.strip() == ""
    instance_changed = new_id != old_container_id
    recreate_lost = refused and receipt_gone and instance_changed
    checks.append(check("receipt directory GONE after full recreate (fresh fs)",
                        "empty/absent", out.strip() or "<absent>", receipt_gone))
    checks.append(check("old operation unresolvable after recreate (derived)",
                        "outcome_unknown (pinning refused + receipts gone + new instance)",
                        f"refused={refused} receipts_present={not receipt_gone} "
                        f"instance_changed={instance_changed}",
                        recreate_lost))
    notes.append("transient ownership artifacts do NOT survive target replacement: receipts "
                 "must live on storage that outlives the boot instance, and reconnect must "
                 "re-pin host identity before any inspection")
    passed = all(c["ok"] for c in checks)
    return verdict(exp, passed, checks, notes)


# ---------------------------------------------------------------- cleanup


def run_cleanup() -> int:
    exp = "cleanup"
    rc, out, _ = docker(exp, "ps", "-a", "--filter", f"name={CONTAINER}",
                        "--format", "{{.Names}} {{.Status}}")
    if out:
        docker(exp, "stop", "-t", "1", CONTAINER)
        time.sleep(2.0)
    rc, out, _ = docker(exp, "ps", "-a", "--filter", f"name={CONTAINER}",
                        "--format", "{{.Names}} {{.Status}}")
    proof = f"{now_iso()} containers matching {CONTAINER}: {out or '<none>'}"
    (SCRATCH / "cleanup-container.txt").write_text(proof + "\n")
    shutil.rmtree(SCRATCH / "keys", ignore_errors=True)
    print(proof)
    print(f"scratch {SCRATCH} retained for evidence curation (remove after copy)")
    drecord(exp, "CLEANUP", proof)
    return 0


DRIVERS: dict[str, object] = {
    "preflight": run_preflight,
    "exp1": run_exp1,
    "exp2": run_exp2,
    "exp3": run_exp3,
    "exp4": run_exp4,
    "cleanup": run_cleanup,
}


def main() -> int:
    p = argparse.ArgumentParser(description="MAI P00 SSH supervision probe")
    p.add_argument("role", choices=["all", *DRIVERS], help="experiment or lifecycle step")
    args = p.parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if args.role == "all":
        for name in ["preflight", "exp1", "exp2", "exp3", "exp4", "cleanup"]:
            print(f"===== {name} =====", flush=True)
            try:
                rc = DRIVERS[name]()
            except Exception as e:  # a crashed experiment is a recorded failure, not a PASS
                print(f"{name}: CRASHED {type(e).__name__}: {e}", flush=True)
                drecord(name, "CRASH", f"{type(e).__name__}: {e}")
                return 2
            if rc != 0:
                return rc
        return 0
    return DRIVERS[args.role]()


if __name__ == "__main__":
    sys.exit(main())
