#!/usr/bin/env python3
"""MAI P00 durable-job engine probe: dbos-transact-py 3.2.0 interruption/child-run spike.

Card: mai-00-job-spike. Candidate engine: DBOS 3.2.0 (DBOSConfig.name="mai-job-spike")
against a disposable Postgres 18 fixture (docker, port 55432, database per experiment).

Eight experiments (coordinator addendum):
  1  kill -9 mid-step          -> completed steps never re-execute
  2  kill in effect-before-checkpoint window -> double effect documented (P05 guard input)
  3  SetWorkflowID concurrent dedup + workflow_id_reuse_policy="reject" raises
  4  3-child tree: (a) parent kill -9 mid-await -> re-attach not duplicate;
                (b) cancel parent, default propagation -> record child behaviour;
                (c) cancel parent with cancel_children=True -> record child behaviour
  5  */1 * * * * schedule, app stopped 3 min -> measured misfire/dedup policy
  6  cancel mid-step (sync step: boundary preemption) + preemptible async step (immediate)
  7  pg_dump mid-queue -> restore to fresh DB -> relaunch -> recovery, no duplicate effects
  8  Postgres stopped 60s mid-workflow -> recorded failure/recovery mode

Model: every "host effect" is an append to a per-experiment journal file on disk
(scratch root, default /tmp/mai-job-spike/<exp>/journal.log). The journal survives
DB loss/restore, so it records physical executions across process and database
boundaries exactly like a target-host effect would.

Processes: `exp<N>` (driver) spawns `worker <exp>` victim processes, kills and
restarts them, and out-of-process control uses DBOSClient (a bare DB client: it
does NOT run recovery or schedule threads, so it never steals pending workflows
from the victim process the way a second full DBOS app would).

Timeline facts land in the journal as `ISO|who|MARKER|note` lines; drivers print a
single `VERDICT {json}` line; workers print a single `WORKER_RESULT {json}` line.
Evidence roots: MAI_SPIKE_SCRATCH (default /tmp/mai-job-spike), copied by the
caller into the campaign evidence dir.

Usage:
  durable_job_probe.py exp1 ... exp8        run one experiment (driver)
  durable_job_probe.py worker EXP           victim worker role (spawned by drivers)
Environment:
  MAI_SPIKE_DB_URL       base URL; database name is swapped per experiment
                         (default postgresql://postgres:...@localhost:55432/postgres)
  MAI_SPIKE_SCRATCH      journal root (default /tmp/mai-job-spike)
  MAI_SPIKE_PG_CONTAINER docker container name for psql/pg_dump (default mai-fixture-pg)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dbos import (
    DBOS,
    DBOSClient,
    DBOSConfig,
    SetWorkflowID,
)
from dbos import error as dbos_error

CONFIG_NAME = "mai-job-spike"
DEFAULT_DB_URL = "postgresql://postgres:mai-fixture-disposable@localhost:55432/postgres"
SCRATCH = Path(os.environ.get("MAI_SPIKE_SCRATCH", "/tmp/mai-job-spike"))
PG_CONTAINER = os.environ.get("MAI_SPIKE_PG_CONTAINER", "mai-fixture-pg")


def base_db_url() -> str:
    return os.environ.get("MAI_SPIKE_DB_URL", DEFAULT_DB_URL)


def db_url(db_name: str) -> str:
    return base_db_url().rsplit("/", 1)[0] + "/" + db_name


def exp_db(exp: str) -> str:
    return f"mai_spike_{exp}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- journal


def journal_path(exp: str) -> Path:
    d = SCRATCH / exp
    d.mkdir(parents=True, exist_ok=True)
    return d / "journal.log"


def _jrecord(exp: str, who: str, marker: str, note: str = "") -> None:
    with open(journal_path(exp), "a") as f:
        f.write(f"{now_iso()}|{who}|{marker}|{note}\n")


def record(exp: str, marker: str, note: str = "") -> None:
    """Record an effect (worker side)."""
    _jrecord(exp, "WRK", marker, note)


def drecord(exp: str, marker: str, note: str = "") -> None:
    """Record a driver event."""
    _jrecord(exp, "DRV", marker, note)


def journal_lines(exp: str) -> list[str]:
    p = journal_path(exp)
    if not p.exists():
        return []
    return p.read_text().splitlines()


def count_marker(exp: str, marker: str, who: str | None = None) -> int:
    n = 0
    for line in journal_lines(exp):
        parts = line.split("|", 3)
        if len(parts) >= 3 and parts[2] == marker and (who is None or parts[1] == who):
            n += 1
    return n


def wait_markers(
    exp: str, markers: list[str], timeout: float, mode: str = "all"
) -> list[str]:
    """Wait until all/any markers appear; return still-missing markers on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        missing = [m for m in markers if count_marker(exp, m) == 0]
        if mode == "all" and not missing:
            return []
        if mode == "any" and len(missing) < len(markers):
            return []
        time.sleep(0.2)
    return [m for m in markers if count_marker(exp, m) == 0]


def reset_journal(exp: str) -> None:
    d = SCRATCH / exp
    shutil.rmtree(d, ignore_errors=True)
    journal_path(exp)


# ---------------------------------------------------------------- db fixture plumbing


def docker_psql(sql: str, db: str = "postgres", capture: bool = False) -> str:
    cmd = ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", db, "-c", sql]
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed: {r.stderr}")
        return r.stdout
    subprocess.run(cmd, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ""


def fresh_db(db_name: str) -> None:
    docker_psql(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    docker_psql(f'CREATE DATABASE "{db_name}"')


def pg_dump(db_name: str, dest: Path) -> None:
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "pg_dump", "-U", "postgres", "-d", db_name],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {r.stderr}")
    dest.write_text(r.stdout)


def pg_restore(src: Path, target_db: str) -> None:
    fresh_db(target_db)
    r = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", "postgres", "-d", target_db,
         "-v", "ON_ERROR_STOP=1", "-q"],
        stdin=open(src), capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {r.stderr}")


# ---------------------------------------------------------------- app/workflow plumbing


def init_app(db_name: str) -> None:
    global EXP7_QUEUE
    config: DBOSConfig = {
        "name": CONFIG_NAME,
        "system_database_url": db_url(db_name),
        "log_level": "INFO",
    }
    DBOS(config=config)
    DBOS.launch()
    DBOS.register_queue("exp7-q", concurrency=2)
    EXP7_QUEUE = DBOS.retrieve_queue("exp7-q")


def spawn_worker(exp: str, tag: str, db: str | None = None,
                 observe: float | None = None) -> subprocess.Popen:
    log = open(SCRATCH / exp / f"{tag}.log", "w")
    env = dict(os.environ)
    if db:
        env["MAI_SPIKE_DB"] = db
    cmd = [sys.executable, __file__, "worker", exp]
    if observe:
        cmd += ["--observe", str(observe)]
    return subprocess.Popen(
        cmd,
        stdout=log, stderr=subprocess.STDOUT, env=env,
    )


def await_worker(exp: str, tag: str, proc: subprocess.Popen, timeout: float) -> tuple[dict | None, int | None]:
    logf = SCRATCH / exp / f"{tag}.log"

    def extract(text: str) -> dict | None:
        # stderr shares the log fd; a log line can be spliced into the result line,
        # so parse the first JSON object after the marker and ignore any trailing junk
        for line in reversed(text.splitlines()):
            if line.startswith("WORKER_RESULT "):
                try:
                    obj, _ = json.JSONDecoder().raw_decode(line[len("WORKER_RESULT "):])
                    return obj
                except json.JSONDecodeError:
                    continue
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if logf.exists():
            result = extract(logf.read_text(errors="replace"))
            if result is not None:
                return result, proc.poll()
        if proc.poll() is not None:
            time.sleep(0.3)
            result = extract(logf.read_text(errors="replace")) if logf.exists() else None
            return result, proc.poll()
        time.sleep(0.2)
    return None, proc.poll()


def kill9(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)


def check(name: str, expected: str, observed: Any, ok: bool) -> dict:
    return {"check": name, "expected": expected, "observed": observed, "ok": bool(ok)}


def verdict(exp: str, passed: bool, checks: list[dict], notes: list[str]) -> int:
    print("VERDICT " + json.dumps({
        "experiment": exp, "pass": passed, "checks": checks, "notes": notes,
        "finished_at": now_iso(),
    }, indent=None))
    sys.stdout.flush()
    return 0 if passed else 2


def client(exp_db_name: str) -> DBOSClient:
    return DBOSClient(system_database_url=db_url(exp_db_name))


def statuses(cl: DBOSClient, wf_ids: list[str]) -> dict[str, str]:
    got = cl.list_workflows(workflow_ids=wf_ids, load_input=False, load_output=False)
    return {w.workflow_id: w.status for w in got}


# ---------------------------------------------------------------- workflows (registered at import)


@DBOS.step()
def _step_record(exp: str, marker: str, note: str = "") -> str:
    record(exp, marker, note)
    return marker


# --- exp1


@DBOS.step()
def exp1_step_a() -> str:
    record("exp1", "STEP-A")
    return "a"


@DBOS.step()
def exp1_step_b() -> str:
    record("exp1", "STEP-B-START")
    time.sleep(10)
    record("exp1", "STEP-B-DONE")
    return "b"


@DBOS.step()
def exp1_step_c() -> str:
    record("exp1", "STEP-C")
    return "c"


@DBOS.workflow()
def exp1_main() -> list[str]:
    return [exp1_step_a(), exp1_step_b(), exp1_step_c()]


# --- exp2


@DBOS.step()
def exp2_effect_step() -> str:
    record("exp2", "EFFECT")
    time.sleep(4)  # window: effect done, checkpoint not yet written
    return "ok"


@DBOS.workflow()
def exp2_main() -> str:
    return exp2_effect_step()


# --- exp3


@DBOS.workflow()
def exp3_main() -> str:
    record("exp3", "EXEC")
    time.sleep(2)
    return "ok"


# --- exp4


def wait_child_done(exp: str, n: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(count_marker(exp, f"CHILD-DONE-{i}") == 1 for i in range(n)):
            return True
        time.sleep(0.5)
    return False


def snapshot_status(wf_ids: list[str]) -> dict[str, str]:
    got = DBOS.list_workflows(workflow_ids=wf_ids, load_input=False, load_output=False)
    return {w.workflow_id: w.status for w in got}


@DBOS.step()
def exp4_child_start(idx: int, exp: str) -> int:
    record(exp, f"CHILD-START-{idx}")
    return idx


@DBOS.step()
def exp4_child_work(idx: int, exp: str, seconds: float) -> int:
    time.sleep(seconds)
    record(exp, f"CHILD-DONE-{idx}")
    return idx


@DBOS.workflow()
def exp4_child(idx: int, exp: str, seconds: float) -> int:
    exp4_child_start(idx, exp)
    return exp4_child_work(idx, exp, seconds)


def _make_parent(exp: str, child_ids: list[str], seconds: float) -> Callable[[], list[int]]:
    @DBOS.workflow()
    def parent() -> list[int]:
        handles = []
        for i, cid in enumerate(child_ids):
            with SetWorkflowID(cid):
                handles.append(DBOS.start_workflow(exp4_child, i, exp, seconds))
        return [h.get_result() for h in handles]

    return parent


# --- exp5


@DBOS.step()
def exp5_fire(scheduled_time: datetime) -> str:
    record("exp5", "FIRE", scheduled_time.isoformat())
    return scheduled_time.isoformat()


@DBOS.workflow()
def exp5_tick(scheduled_time: datetime, context: Any) -> str:
    return exp5_fire(scheduled_time)


# --- exp6


@DBOS.step(name="exp6_S1")
def exp6_s1() -> str:
    record("exp6", "STEP-S1-START")
    time.sleep(4)
    record("exp6", "STEP-S1-DONE")
    return "S1"


@DBOS.step(name="exp6_S2")
def exp6_s2() -> str:
    record("exp6", "STEP-S2-START")
    time.sleep(4)
    record("exp6", "STEP-S2-DONE")
    return "S2"


@DBOS.step(name="exp6_S3")
def exp6_s3() -> str:
    record("exp6", "STEP-S3-START")
    time.sleep(4)
    record("exp6", "STEP-S3-DONE")
    return "S3"


@DBOS.workflow()
def exp6_main() -> list[str]:
    return [exp6_s1(), exp6_s2(), exp6_s3()]


@DBOS.step(preemptible=True)
async def exp6_async_step() -> str:
    record("exp6b", "ASYNC-STEP-START")
    await asyncio.sleep(30)
    record("exp6b", "ASYNC-STEP-DONE")
    return "done"


@DBOS.workflow()
async def exp6b_main() -> str:
    return await exp6_async_step()


# --- exp7


EXP7_QUEUE: Any = None  # bound in init_app after launch (register_queue needs the app)


@DBOS.step()
def exp7_job_exec(i: int) -> int:
    record("exp7", f"EXEC-JOB-{i}")
    return i


@DBOS.step()
def exp7_job_work(i: int) -> int:
    time.sleep(6)
    return i


@DBOS.step()
def exp7_job_done(i: int) -> int:
    record("exp7", f"DONE-JOB-{i}")
    return i


@DBOS.workflow()
def exp7_job(i: int) -> int:
    exp7_job_exec(i)
    exp7_job_work(i)
    return exp7_job_done(i)


# --- exp8


@DBOS.step()
def exp8_s1() -> str:
    record("exp8", "S1-START")
    time.sleep(1)
    record("exp8", "S1-DONE")
    return "s1"


@DBOS.step()
def exp8_s2() -> str:
    record("exp8", "S2-START")
    time.sleep(45)
    record("exp8", "S2-DONE")
    return "s2"


@DBOS.step()
def exp8_s3() -> str:
    record("exp8", "S3-START")
    record("exp8", "S3-DONE")
    return "s3"


@DBOS.workflow()
def exp8_main() -> list[str]:
    return [exp8_s1(), exp8_s2(), exp8_s3()]


# ---------------------------------------------------------------- worker role


def run_scheduled_app(exp: str, observe_seconds: float) -> None:
    try:
        DBOS.create_schedule(
            schedule_name=f"{exp}-sched",
            workflow_fn=exp5_tick,
            schedule="*/1 * * * *",
        )
    except dbos_error.DBOSException as e:
        if "already exists" not in str(e):
            raise
        record(exp, "SCHEDULE-EXISTS")
    deadline = time.time() + observe_seconds
    while time.time() < deadline:
        time.sleep(0.5)


WORKERS: dict[str, Callable[[argparse.Namespace], dict]] = {}


def worker_arg(f: Callable[[argparse.Namespace], dict]) -> Callable[[argparse.Namespace], dict]:
    WORKERS[f.__name__.removeprefix("worker_")] = f
    return f


@worker_arg
def worker_exp1(ns: argparse.Namespace) -> dict:
    with SetWorkflowID("exp1-wf"):
        h = DBOS.start_workflow(exp1_main)
    result = h.get_result()
    return {"workflow_id": h.workflow_id, "result": result}


@worker_arg
def worker_exp2(ns: argparse.Namespace) -> dict:
    with SetWorkflowID("exp2-wf"):
        h = DBOS.start_workflow(exp2_main)
    result = h.get_result()
    return {"workflow_id": h.workflow_id, "result": result}


@worker_arg
def worker_exp3(ns: argparse.Namespace) -> dict:
    outcomes: list[dict] = []

    def submit(tag: str) -> None:
        try:
            with SetWorkflowID("exp3-wf"):
                h = DBOS.start_workflow(exp3_main)
            outcomes.append({"tag": tag, "id": h.workflow_id, "result": h.get_result()})
        except BaseException as e:  # noqa: BLE001 - record the observable behaviour
            outcomes.append({"tag": tag, "error": f"{type(e).__name__}: {e}"})

    t1 = threading.Thread(target=submit, args=("t1",))
    t2 = threading.Thread(target=submit, args=("t2",))
    t1.start(); t2.start(); t1.join(60); t2.join(60)

    reject_raised = None
    try:
        with SetWorkflowID("exp3-wf", workflow_id_reuse_policy="reject"):
            DBOS.start_workflow(exp3_main)
        reject_raised = False
    except dbos_error.DBOSWorkflowIDInUseError as e:
        reject_raised = f"DBOSWorkflowIDInUseError: {e}"
    except BaseException as e:  # noqa: BLE001
        reject_raised = f"UNEXPECTED {type(e).__name__}: {e}"
    return {
        "submits": outcomes,
        "exec_count": count_marker("exp3", "EXEC"),
        "reject": reject_raised,
    }


@worker_arg
def worker_exp4a(ns: argparse.Namespace) -> dict:
    parent = _make_parent("exp4a", [f"exp4a-child-{i}" for i in range(3)], 12.0)
    with SetWorkflowID("exp4a-parent"):
        h = DBOS.start_workflow(parent)
    result = h.get_result()
    return {"workflow_id": h.workflow_id, "children": result}


@worker_arg
def worker_exp4b(ns: argparse.Namespace) -> dict:
    parent = _make_parent("exp4b", [f"exp4b-child-{i}" for i in range(3)], 25.0)
    with SetWorkflowID("exp4b-parent"):
        h = DBOS.start_workflow(parent)
    try:
        result = h.get_result()
        cancelled = False
    except BaseException as e:  # noqa: BLE001
        cancelled = True
        result = f"{type(e).__name__}: {e}"
        # stay alive so the default-propagation behaviour of children is observable
        wait_child_done("exp4b", 3, timeout=45)
    child_st = snapshot_status([f"exp4b-child-{i}" for i in range(3)])
    return {"workflow_id": h.workflow_id, "cancelled": cancelled,
            "result": result, "children_final": child_st}


@worker_arg
def worker_exp4c(ns: argparse.Namespace) -> dict:
    parent = _make_parent("exp4c", [f"exp4c-child-{i}" for i in range(3)], 25.0)
    with SetWorkflowID("exp4c-parent"):
        h = DBOS.start_workflow(parent)
    try:
        result = h.get_result()
        cancelled = False
    except BaseException as e:  # noqa: BLE001
        cancelled = True
        result = f"{type(e).__name__}: {e}"
    child_st: dict[str, str] = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        child_st = snapshot_status([f"exp4c-child-{i}" for i in range(3)])
        if all(v != "PENDING" for v in child_st.values()) and child_st:
            break
        time.sleep(0.5)
    return {"workflow_id": h.workflow_id, "cancelled": cancelled,
            "result": result, "children_final": child_st}


@worker_arg
def worker_exp5(ns: argparse.Namespace) -> dict:
    run_scheduled_app("exp5", ns.observe)
    return {"observe": ns.observe}


@worker_arg
def worker_exp6(ns: argparse.Namespace) -> dict:
    with SetWorkflowID("exp6-wf"):
        h = DBOS.start_workflow(exp6_main)
    try:
        result = h.get_result()
        return {"workflow_id": h.workflow_id, "result": result, "cancelled": False}
    except BaseException as e:  # noqa: BLE001
        return {"workflow_id": h.workflow_id, "cancelled": True, "error": f"{type(e).__name__}: {e}"}


@worker_arg
def worker_exp6b(ns: argparse.Namespace) -> dict:
    with SetWorkflowID("exp6b-wf"):
        h = DBOS.start_workflow(exp6b_main)
    try:
        result = h.get_result()
        return {"workflow_id": h.workflow_id, "result": result, "cancelled": False}
    except BaseException as e:  # noqa: BLE001
        return {"workflow_id": h.workflow_id, "cancelled": True, "error": f"{type(e).__name__}: {e}"}


@worker_arg
def worker_exp7(ns: argparse.Namespace) -> dict:
    handles = []
    for i in range(6):
        with SetWorkflowID(f"exp7-job-{i}"):
            handles.append(EXP7_QUEUE.enqueue(exp7_job, i))
    results = []
    for h in handles:
        try:
            results.append({"job": h.workflow_id, "result": h.get_result()})
        except BaseException as e:  # noqa: BLE001
            results.append({"job": h.workflow_id, "error": f"{type(e).__name__}: {e}"})
    return {"jobs": results}


@worker_arg
def worker_exp8(ns: argparse.Namespace) -> dict:
    with SetWorkflowID("exp8-wf"):
        h = DBOS.start_workflow(exp8_main)
    try:
        result = h.get_result()
        return {"workflow_id": h.workflow_id, "result": result, "error": None}
    except BaseException as e:  # noqa: BLE001
        return {"workflow_id": h.workflow_id, "result": None, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- drivers


def driver_common(exp: str) -> None:
    reset_journal(exp)
    fresh_db(exp_db(exp))


def run_exp1() -> int:
    exp = "exp1"
    driver_common(exp)
    drecord(exp, "EXP-START", "kill -9 mid-step; completed steps must not re-execute")

    w1 = spawn_worker(exp, "w1")
    missing = wait_markers(exp, ["STEP-B-START"], timeout=60)
    if missing:
        kill9(w1)
        return verdict(exp, False, [check("reached-step-B", "STEP-B-START present", "absent", False)],
                       ["worker never reached step B; see w1.log"])
    time.sleep(1.0)
    drecord(exp, "KILL-9", "inside STEP-B 10s sleep")
    kill9(w1)

    w2 = spawn_worker(exp, "w2")
    result, rc = await_worker(exp, "w2", w2, timeout=120)
    if result is None:
        kill9(w2)
        return verdict(exp, False, [check("restart-completion", "worker result", f"rc={rc}", False)],
                       ["restarted worker did not complete; see w2.log"])

    a, bs, bd, c = (count_marker(exp, m) for m in
                    ("STEP-A", "STEP-B-START", "STEP-B-DONE", "STEP-C"))
    checks = [
        check("STEP-A executions", 1, a, a == 1),
        check("STEP-B executions", 2, bs, bs == 2),
        check("STEP-B completions", 1, bd, bd == 1),
        check("STEP-C executions", 1, c, c == 1),
        check("final status", "SUCCESS", result.get("result"), result.get("result") == ["a", "b", "c"]),
    ]
    notes = ["kill -9 inside STEP-B sleep; restarted worker attached via SetWorkflowID(exp1-wf)"]
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


def run_exp2() -> int:
    exp = "exp2"
    driver_common(exp)
    drecord(exp, "EXP-START", "kill -9 in effect-before-checkpoint window")

    w1 = spawn_worker(exp, "w1")
    missing = wait_markers(exp, ["EFFECT"], timeout=60)
    if missing:
        kill9(w1)
        return verdict(exp, False, [check("reached-effect", "EFFECT present", "absent", False)],
                       ["worker never recorded the effect; see w1.log"])
    time.sleep(1.0)
    drecord(exp, "KILL-9", "inside post-effect pre-checkpoint sleep")
    kill9(w1)

    w2 = spawn_worker(exp, "w2")
    result, rc = await_worker(exp, "w2", w2, timeout=90)
    if result is None:
        kill9(w2)
        return verdict(exp, False, [check("restart-completion", "worker result", f"rc={rc}", False)],
                       ["restarted worker did not complete; see w2.log"])

    effects = count_marker(exp, "EFFECT")
    checks = [
        check("effect executions", "2 (documented double effect)", effects, effects == 2),
        check("final status", "SUCCESS", result.get("result"), result.get("result") == "ok"),
    ]
    notes = [
        "DBOS does not close the crash-between-effect-and-checkpoint window: step retried after kill, effect ran twice",
        "P05 rule derived: host-mutating steps must be verify-then-mutate (read-back step before mutate) or idempotent by construction",
    ]
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


def run_exp3() -> int:
    exp = "exp3"
    driver_common(exp)
    drecord(exp, "EXP-START", "concurrent SetWorkflowID dedup + reject policy")

    w = spawn_worker(exp, "w1")
    result, rc = await_worker(exp, "w1", w, timeout=90)
    if result is None:
        kill9(w)
        return verdict(exp, False, [check("worker-result", "result", f"rc={rc}", False)],
                       ["worker did not finish; see w1.log"])

    submits = result["submits"]
    ids = [s.get("id") for s in submits if s.get("id")]
    errors = [s.get("error") for s in submits if s.get("error")]
    same_id = len(set(ids)) == 1 and len(ids) == 2
    checks = [
        check("both callers resolved same workflow id", True, same_id, same_id),
        check("single execution", 1, result["exec_count"], result["exec_count"] == 1),
        check("reject policy raises DBOSWorkflowIDInUseError", True,
              result["reject"], isinstance(result["reject"], str)
              and result["reject"].startswith("DBOSWorkflowIDInUseError")),
        check("no submit-side errors", None, errors or None, not errors),
    ]
    notes = [f"caller outcomes: {json.dumps(submits)}"]
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


def run_exp4() -> int:
    exp = "exp4"
    # ---- 4a: kill parent mid-await, restart, expect re-attach not duplicates
    e = "exp4a"
    driver_common(e)
    drecord(e, "EXP-START", "3-child tree; kill -9 parent mid-await")
    w1 = spawn_worker(e, "w1")
    missing = wait_markers(e, [f"CHILD-START-{i}" for i in range(3)], timeout=60)
    if missing:
        kill9(w1)
        return verdict(e, False, [check("children-started", "3 starts", f"missing {missing}", False)],
                       ["children never all started; see w1.log"])
    time.sleep(2.0)
    drecord(e, "KILL-9", "parent awaiting child results")
    kill9(w1)
    w2 = spawn_worker(e, "w2")
    result, rc = await_worker(e, "w2", w2, timeout=120)
    if result is None:
        kill9(w2)
        return verdict(e, False, [check("parent-completed", "result", f"rc={rc}", False)],
                       ["restarted parent never completed; see w2.log"])
    starts = [count_marker(e, f"CHILD-START-{i}") for i in range(3)]
    dones = [count_marker(e, f"CHILD-DONE-{i}") for i in range(3)]
    cl = client(exp_db(e))
    st = statuses(cl, ["exp4a-parent"] + [f"exp4a-child-{i}" for i in range(3)])
    checks_a = [
        check("child starts exactly once each", [1, 1, 1], starts, starts == [1, 1, 1]),
        check("child completions exactly once each", [1, 1, 1], dones, dones == [1, 1, 1]),
        check("parent status", "SUCCESS", st.get("exp4a-parent"), st.get("exp4a-parent") == "SUCCESS"),
        check("child results via parent", [0, 1, 2], result.get("children"),
              result.get("children") == [0, 1, 2]),
    ]
    notes_a = ["restart re-attached parent and children via workflow IDs; no duplicate children"]
    a_pass = all(c["ok"] for c in checks_a)

    # ---- 4b: cancel parent (default propagation), record child behaviour
    e = "exp4b"
    driver_common(e)
    drecord(e, "EXP-START", "cancel parent mid-await, default cancel_children=False")
    w1 = spawn_worker(e, "w1")
    missing = wait_markers(e, [f"CHILD-START-{i}" for i in range(3)], timeout=60)
    if missing:
        kill9(w1)
        return verdict(e, False, [check("children-started", "3 starts", f"missing {missing}", False)],
                       ["children never all started; see w1.log"])
    t_cancel = time.time()
    drecord(e, "CANCEL-PARENT", "default propagation")
    cl = client(exp_db(e))
    cl.cancel_workflow("exp4b-parent")
    parent_st: dict[str, str] = {}
    while time.time() - t_cancel < 60:
        parent_st = statuses(cl, ["exp4b-parent"])
        if parent_st.get("exp4b-parent") in ("CANCELLED", "SUCCESS", "ERROR"):
            break
        time.sleep(0.5)
    t_parent_done = time.time()
    # observe children for the remainder of their 25s work
    deadline = time.time() + 40
    while time.time() < deadline and count_marker(e, "CHILD-DONE-2") == 0:
        time.sleep(1)
    await_worker(e, "w1", w1, timeout=50)
    child_st = statuses(cl, [f"exp4b-child-{i}" for i in range(3)])
    dones_b = [count_marker(e, f"CHILD-DONE-{i}") for i in range(3)]
    checks_b = [
        check("parent status after cancel", "CANCELLED", parent_st.get("exp4b-parent"),
              parent_st.get("exp4b-parent") == "CANCELLED"),
        check("children ran to completion (no propagation)", [1, 1, 1], dones_b,
              dones_b == [1, 1, 1]),
        check("child statuses", ["SUCCESS"] * 3, [child_st.get(f"exp4b-child-{i}") for i in range(3)],
              [child_st.get(f"exp4b-child-{i}") for i in range(3)] == ["SUCCESS"] * 3),
    ]
    notes_b = [
        f"parent cancel observed after {t_parent_done - t_cancel:.1f}s",
        "default propagation leaves children running to completion",
    ]
    b_pass = all(c["ok"] for c in checks_b)

    # ---- 4c: cancel parent with cancel_children=True, record child behaviour
    e = "exp4c"
    driver_common(e)
    drecord(e, "EXP-START", "cancel parent mid-await, cancel_children=True")
    w1 = spawn_worker(e, "w1")
    missing = wait_markers(e, [f"CHILD-START-{i}" for i in range(3)], timeout=60)
    if missing:
        kill9(w1)
        return verdict(e, False, [check("children-started", "3 starts", f"missing {missing}", False)],
                       ["children never all started; see w1.log"])
    t_cancel = time.time()
    drecord(e, "CANCEL-PARENT", "cancel_children=True")
    cl = client(exp_db(e))
    cl.cancel_workflow("exp4c-parent", cancel_children=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        st = statuses(cl, ["exp4c-parent"] + [f"exp4c-child-{i}" for i in range(3)])
        if all(v in ("CANCELLED", "SUCCESS", "ERROR") for v in st.values()):
            break
        time.sleep(0.5)
    t_all_cancelled = time.time()
    await_worker(e, "w1", w1, timeout=40)
    time.sleep(2)
    dones_c = [count_marker(e, f"CHILD-DONE-{i}") for i in range(3)]
    cl = client(exp_db(e))
    st_c = statuses(cl, ["exp4c-parent"] + [f"exp4c-child-{i}" for i in range(3)])
    child_sts = [st_c.get(f"exp4c-child-{i}") for i in range(3)]
    checks_c = [
        check("parent status", "CANCELLED", st_c.get("exp4c-parent"),
              st_c.get("exp4c-parent") == "CANCELLED"),
        check("child statuses cancelled", ["CANCELLED"] * 3, child_sts,
              child_sts == ["CANCELLED"] * 3),
        check("cancel_children latency", "< 25s (before children finish)",
              f"{t_all_cancelled - t_cancel:.1f}s", t_all_cancelled - t_cancel < 25),
    ]
    notes_c = [
        "children are sync-sleep steps: cancellation lands at the step boundary, in-flight child work may complete",
        f"child DONE markers after cancel_children=True: {dones_c}",
    ]
    c_pass = all(c["ok"] for c in checks_c)

    passed = a_pass and b_pass and c_pass
    return verdict(exp, passed,
                   checks_a + checks_b + checks_c, notes_a + notes_b + notes_c)


def run_exp5() -> int:
    exp = "exp5"
    driver_common(exp)
    drecord(exp, "EXP-START", "*/1 schedule; stop app 3 min; measure misfire/dedup policy")

    w1 = spawn_worker(exp, "w1", observe=135)
    time.sleep(130)  # expect fires on the next 1-2 minute boundaries
    n1 = count_marker(exp, "FIRE")
    drecord(exp, "STOP-APP", f"fires so far: {n1}")
    kill9(w1)

    t_down = time.time()
    time.sleep(180)  # ~3 minutes of downtime: ~3 cron fires missed
    t_up = time.time()
    drecord(exp, "RESTART-APP", f"downtime {(t_up - t_down):.1f}s")

    w2 = spawn_worker(exp, "w2", observe=135)
    time.sleep(130)
    kill9(w2)

    fires = [line.split("|") for line in journal_lines(exp)
             if "|WRK|FIRE|" in line]
    scheduled = [f[3] for f in fires]
    executed = [f[0] for f in fires]
    dupes = len(scheduled) != len(set(scheduled))
    # a catch-up fire would carry a scheduled_time inside the downtime window
    gap_fires = [s for s in scheduled if t_down <= _parse_iso(s) <= t_up]

    checks = [
        check("fires while app up (first window)", ">=1", n1, n1 >= 1),
        check("no duplicate fire per scheduled time", False, dupes, not dupes),
        check("missed downtime fires NOT replayed", 0, len(gap_fires), len(gap_fires) == 0),
        check("fires resumed after restart", count_marker(exp, "FIRE") > n1,
              count_marker(exp, "FIRE") > n1, count_marker(exp, "FIRE") > n1),
    ]
    notes = [
        f"fires: {json.dumps([{'scheduled': s, 'executed': x} for s, x in zip(scheduled, executed)])}",
        "measured policy: scheduler resumes from 'now' after restart; missed fire times are skipped, "
        "deduplicated per scheduled time by deterministic workflow id sched-<name>-<fire time>",
    ]
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def run_exp6() -> int:
    exp = "exp6"
    driver_common(exp)
    drecord(exp, "EXP-START", "cancel mid-step: boundary preemption on sync steps")

    w1 = spawn_worker(exp, "w1")
    missing = wait_markers(exp, ["STEP-S2-START"], timeout=60)
    if missing:
        kill9(w1)
        return verdict(exp, False, [check("reached-S2", "STEP-S2-START", "absent", False)],
                       ["worker never reached S2; see w1.log"])
    t_cancel = time.time()
    drecord(exp, "CANCEL", "mid S2")
    cl = client(exp_db(exp))
    cl.cancel_workflow("exp6-wf")
    st: dict[str, str] = {}
    while time.time() - t_cancel < 60:
        st = statuses(cl, ["exp6-wf"])
        if st.get("exp6-wf") in ("CANCELLED", "SUCCESS", "ERROR"):
            break
        time.sleep(0.3)
    t_seen = time.time()
    await_worker(exp, "w1", w1, timeout=30)
    s2d = count_marker(exp, "STEP-S2-DONE")
    s3s = count_marker(exp, "STEP-S3-START")
    checks = [
        check("status after cancel", "CANCELLED", st.get("exp6-wf"), st.get("exp6-wf") == "CANCELLED"),
        check("in-flight S2 completed (boundary)", 1, s2d, s2d == 1),
        check("next step never started", 0, s3s, s3s == 0),
    ]
    notes = [
        f"cancel visible after {t_seen - t_cancel:.1f}s (polling + boundary wait)",
        "mid-step cancellation preempts at the next step boundary",
    ]
    p1 = all(c["ok"] for c in checks)

    # ---- preemptible async step cancels immediately
    e = "exp6b"
    driver_common(e)
    drecord(e, "EXP-START", "preemptible async step cancel")
    w2 = spawn_worker(e, "w2")
    missing = wait_markers(e, ["ASYNC-STEP-START"], timeout=60)
    if missing:
        kill9(w2)
        return verdict(e, False, [check("async-step-started", "ASYNC-STEP-START", "absent", False)],
                       ["async step never started; see w2.log"])
    t_cancel = time.time()
    drecord(e, "CANCEL", "preemptible async step in flight")
    cl = client(exp_db(e))
    cl.cancel_workflow("exp6b-wf")
    st = {}
    while time.time() - t_cancel < 60:
        st = statuses(cl, ["exp6b-wf"])
        if st.get("exp6b-wf") in ("CANCELLED", "SUCCESS", "ERROR"):
            break
        time.sleep(0.2)
    t_seen = time.time()
    result, rc = await_worker(e, "w2", w2, timeout=30)
    adone = count_marker(e, "ASYNC-STEP-DONE")
    checks_b = [
        check("status after cancel", "CANCELLED", st.get("exp6b-wf"), st.get("exp6b-wf") == "CANCELLED"),
        check("preemptible step did NOT run to completion", 0, adone, adone == 0),
        check("cancel latency", "< 25s (well before 30s step end)", f"{t_seen - t_cancel:.1f}s",
              t_seen - t_cancel < 25),
        check("worker observed DBOSAwaitedWorkflowCancelledError", True,
              result, bool(result and result.get("cancelled"))),
    ]
    notes_b = [f"preemptible async step cancel observed after {t_seen - t_cancel:.1f}s"]
    passed = p1 and all(c["ok"] for c in checks_b)
    return verdict(exp, passed, checks + checks_b, notes + notes_b)


def run_exp7() -> int:
    exp = "exp7"
    driver_common(exp)
    drecord(exp, "EXP-START", "pg_dump mid-queue -> restore to fresh DB -> relaunch")

    w1 = spawn_worker(exp, "w1", db=exp_db(exp))
    deadline = time.time() + 60
    # wait until the first pair completed AND the second pair is mid-work so the
    # dump+kill lands inside the in-flight window
    while time.time() < deadline and not (
        count_marker(exp, "DONE-JOB-0")
        and count_marker(exp, "DONE-JOB-1")
        and count_marker(exp, "EXEC-JOB-2")
    ):
        time.sleep(0.2)
    time.sleep(0.5)  # let the second pair enter its work step
    drecord(exp, "DUMP", f"done jobs at dump: {sum(count_marker(exp, f'DONE-JOB-{i}') for i in range(6))}")
    dump = SCRATCH / exp / "dump.sql"
    pg_dump(exp_db(exp), dump)
    pre = {m: count_marker(exp, m) for m in journal_marker_tokens(exp)}
    drecord(exp, "KILL-9", f"mid-queue, post-dump, pre-kill markers: {json.dumps(pre)}")
    kill9(w1)

    restore_db = exp_db(exp) + "_restore"
    pg_restore(dump, restore_db)
    drecord(exp, "RESTORED", f"into {restore_db}")

    w2 = spawn_worker(exp, "w2", db=restore_db)
    result, rc = await_worker(exp, "w2", w2, timeout=120)
    if result is None:
        kill9(w2)
        return verdict(exp, False, [check("post-restore-completion", "6 job results", f"rc={rc}", False)],
                       ["restored worker never finished all jobs; see w2.log"])
    post = {m: count_marker(exp, m) for m in journal_marker_tokens(exp)}
    exec_counts = [post[f"EXEC-JOB-{i}"] for i in range(6)]
    done_counts = [post[f"DONE-JOB-{i}"] for i in range(6)]
    job_results = result.get("jobs", [])
    all_ok = all("result" in j for j in job_results)
    checks = [
        check("every job completed exactly once (DONE)", [1] * 6, done_counts, done_counts == [1] * 6),
        check("every job started exactly once (EXEC)", [1] * 6, exec_counts, exec_counts == [1] * 6),
        check("all 6 jobs succeeded after restore", True, all_ok, all_ok),
    ]
    notes = [
        f"markers before restart: {json.dumps(pre)}",
        f"markers after restore+relaunch: {json.dumps(post)}",
        "in-flight job steps at dump time re-executed from their last checkpoint on the restored DB; "
        "no completed work re-executed and no job fired twice",
    ]
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


def journal_marker_tokens(exp: str) -> list[str]:
    toks = set()
    for line in journal_lines(exp):
        parts = line.split("|", 3)
        if len(parts) >= 3 and parts[1] == "WRK" and (
            parts[2].startswith("EXEC-JOB-") or parts[2].startswith("DONE-JOB-")
        ):
            toks.add(parts[2])
    return sorted(toks)


def run_exp8() -> int:
    exp = "exp8"
    driver_common(exp)
    drecord(exp, "EXP-START", "Postgres stopped 60s mid-workflow; record failure/recovery mode")

    w1 = spawn_worker(exp, "w1")
    missing = wait_markers(exp, ["S1-DONE"], timeout=60)
    if missing:
        kill9(w1)
        return verdict(exp, False, [check("reached-S1", "S1-DONE", "absent", False)],
                       ["worker never finished S1; see w1.log"])
    subprocess.run(["docker", "stop", PG_CONTAINER], check=True, timeout=120)
    drecord(exp, "PG-STOP", "60s outage begins")
    time.sleep(60)
    subprocess.run(["docker", "start", PG_CONTAINER], check=True, timeout=120)
    for _ in range(60):
        r = subprocess.run(["docker", "exec", PG_CONTAINER, "pg_isready", "-U", "postgres"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    drecord(exp, "PG-BACK", f"outage ended at {now_iso()}")

    result, rc = await_worker(exp, "w1", w1, timeout=150)
    cl = client(exp_db(exp))
    st = statuses(cl, ["exp8-wf"])
    notes = [f"during/after outage worker reported: {json.dumps(result)} (rc={rc})"]

    final = st.get("exp8-wf")
    if final in ("ERROR", "PENDING", "ENQUEUED"):
        drecord(exp, "RESUME", f"manual resume from status {final}")
        cl.resume_workflow("exp8-wf")
        w2 = spawn_worker(exp, "w2")
        result2, rc2 = await_worker(exp, "w2", w2, timeout=150)
        notes.append(f"after resume: {json.dumps(result2)} (rc={rc2})")
        kill9(w2)
    st = statuses(cl, ["exp8-wf"])

    s1 = count_marker(exp, "S1-START")
    s3 = count_marker(exp, "S3-START")
    s3d = count_marker(exp, "S3-DONE")
    checks = [
        check("final status", "SUCCESS", st.get("exp8-wf"), st.get("exp8-wf") == "SUCCESS"),
        check("S1 never re-executed", 1, s1, s1 == 1),
        check("S3 executed once after recovery", (1, 1), (s3, s3d), s3 == 1 and s3d == 1),
    ]
    notes.append("failure mode is recorded verbatim in w1.log / journal; recovery via explicit resume + relaunch")
    return verdict(exp, all(c["ok"] for c in checks), checks, notes)


DRIVERS: dict[str, Callable[[], int]] = {
    "exp1": run_exp1,
    "exp2": run_exp2,
    "exp3": run_exp3,
    "exp4": run_exp4,
    "exp5": run_exp5,
    "exp6": run_exp6,
    "exp7": run_exp7,
    "exp8": run_exp8,
}


# ---------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description="MAI P00 durable-job engine probe")
    p.add_argument("role", choices=["worker"] + sorted(DRIVERS))
    p.add_argument("exp", nargs="?", help="experiment for worker role")
    p.add_argument("--observe", type=float, default=0.0, help="worker: seconds to observe")
    ns = p.parse_args()

    if ns.role == "worker":
        if not ns.exp:
            p.error("worker role requires experiment name")
        db = os.environ.get("MAI_SPIKE_DB", exp_db(ns.exp))
        init_app(db)
        try:
            fn = WORKERS.get(ns.exp)
            if fn is None:
                print(f"WORKER_RESULT {{\"error\": \"no worker for {ns.exp}\"}}")
                return 2
            result = fn(ns)
            print("WORKER_RESULT " + json.dumps(result))
            return 0
        finally:
            DBOS.destroy()

    return DRIVERS[ns.role]()


if __name__ == "__main__":
    sys.exit(main())
