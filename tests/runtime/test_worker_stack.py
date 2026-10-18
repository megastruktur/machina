"""Focused worker-stack tests for the P05 Compose wiring (card 05-worker-compose).

Static lane (always runs, seconds): renders ``compose.yaml`` through the real
``docker compose config`` and asserts the worker topology boundaries:

- ``mai-worker`` runs the REAL C05 entrypoint (``python -m mai.jobs.worker``,
  05-engine-adapter) — never a placeholder, never an HTTP server — and it is
  the ONLY service in the topology allowed to be a durable engine: the API
  command never launches one, so API replicas can never become a second
  scheduler (C05 rule 1);
- the worker is profile-gated (absent from the default start set), reaches
  Postgres through the required fail-closed ``MAI_POSTGRES_PASSWORD``
  interpolation, mounts only the scoped ``mai-data`` data root, and carries
  no secret beyond the one database URL (no admin credential, no origin
  allowlist — C03 rule 6 scoping);
- restart policy (``unless-stopped``), SIGTERM shutdown budget, and the
  healthcheck exist and probe ACTUAL worker readiness: the real entrypoint
  as the container's main process AND the engine-owned ``dbos`` schema (a
  full engine launch is its only creator — the EngineClient ordering
  contract in ``mai.jobs.engine``);
- no ``depends_on`` edge in either direction between ``mai-api`` and
  ``mai-worker`` (independent recreate is the SERVICES end-to-end route).

Live lane (opt-in, ``MAI_WORKER_STACK_LIVE=1``): builds the backend image,
starts postgres + mai-api + mai-worker (profile) under the disposable
fixture project ``mai-fxt-05-worker-compose`` (D3 namespace), and walks the
brief's feature-gate scenario with real observables:

1. the worker reaches HEALTHY via its own readiness healthcheck (real
   daemon, distinct command identity vs the API);
2. the engine-owned ``dbos`` schema exists only AFTER the worker launched
   (Alembic ran first — negative + positive control for the ordering
   contract);
3. with TWO ``mai-api`` replicas, one every-5-seconds schedule produces
   exactly one ledger row per distinct planned fire and exactly that many
   SUCCESS ``schedule_fire_workflow`` workflows — no duplicate schedule
   leader from API replicas;
4. a run enqueued while the worker is down stays durably queued while the
   API is fully recreated (both replicas) — the stopped worker container is
   not touched by the API recreation;
5. the recreated worker recovers the pending run and executes it exactly
   once (single workflow row, registered production handler
   ``capability.steps`` — value-free ``executor_not_configured`` because
   run composition is a later card's surface, disclosed limitation);
6. restart policy is real: a worker that self-exits (SIGTERM drain, exit 0)
   is restarted by Docker (``unless-stopped``) and returns healthy; a manual
   stop exits 0 (clean engine drain on SIGTERM) and stays stopped. (Docker
   treats a docker-API ``kill`` as a manual stop that disables the policy —
   recorded, so the policy proof uses the deliverable in-container SIGTERM.)

Fixtures: the real stack only. The auth/HTTP boundary is deliberately NOT
exercised here (producer cards own it); control-plane actions use the
sanctioned ``EngineClient``/``ScheduleManager`` seams ("tests acting as the
API"). Teardown removes every project resource and asserts nothing was left
behind.

Stdlib only; runs under the backend venv like tests/runtime/test_compose.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
PROJECT = "mai-fxt-05-worker-compose"
FIXTURE_PASSWORD = "mai-fxt-throwaway-password"
NEEDED_ENV = {"MAI_POSTGRES_PASSWORD": FIXTURE_PASSWORD}

#: The real production handler registered by ``mai.jobs.steps`` (imported by
#: the worker through ``MAI_WORKER_HANDLERS``).
STEPS_HANDLER = "capability.steps"

#: The C03-slug capability id used inside the fixture run intent (the strict
#: 05-steps schema accepts it; the composition check rejects the attempt
#: before any effect).
STEPS_CAPABILITY = "fixture.capability"

_STATE: dict = {}


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _live_enabled() -> bool:
    return os.environ.get("MAI_WORKER_STACK_LIVE") == "1"


def _compose_args(*extra: str, profile: str | None = None) -> list[str]:
    args = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT]
    if profile is not None:
        # --profile is a top-level compose flag; it precedes the subcommand.
        args += ["--profile", profile]
    return args + list(extra)


def _compose(
    *extra: str,
    profile: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.pop("MAI_POSTGRES_PASSWORD", None)
    merged.update(NEEDED_ENV)
    return subprocess.run(
        _compose_args(*extra, profile=profile),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=merged,
        timeout=timeout,
    )


def _json_rows(stdout: str) -> list[dict]:
    """Parse compose JSON output in either array or JSON-lines form (v5 quirk)."""
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Static lane
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rendered() -> dict:
    """Full rendered Compose model (worker profile enabled, all services)."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = _compose("config", "--format", "json", profile="worker")
    assert proc.returncode == 0, proc.stderr
    model = json.loads(proc.stdout)
    assert model["services"], "render produced no services"
    return model


@pytest.fixture(scope="module")
def rendered_default() -> dict:
    """Rendered model of the DEFAULT start set (no profiles enabled)."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = _compose("config", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_worker_runs_the_real_c05_entrypoint(rendered: dict) -> None:
    worker = rendered["services"]["mai-worker"]
    assert " ".join(worker["command"]) == "python -m mai.jobs.worker"
    # The registration modules the real executor and the schedule fire
    # workflow need, imported before engine launch (mai.jobs.worker contract).
    assert worker["environment"]["MAI_WORKER_HANDLERS"] == (
        "mai.jobs.steps,mai.jobs.schedules"
    )
    # Same backend image/package as the API (SERVICES row); no listener.
    assert worker["image"] == rendered["services"]["mai-api"]["image"]
    assert not worker.get("ports")


def test_worker_is_profile_gated_absent_from_default_start_set(
    rendered: dict, rendered_default: dict
) -> None:
    assert rendered["services"]["mai-worker"].get("profiles") == ["worker"]
    assert "mai-worker" not in rendered_default["services"]


def test_worker_identity_is_distinct_from_the_api(rendered: dict) -> None:
    worker_cmd = " ".join(rendered["services"]["mai-worker"]["command"])
    # Only the worker role may be a durable engine (C05 rule 1). The API
    # does not override its image command: the reviewed uvicorn factory line
    # (backend/Dockerfile CMD) is the API identity — no engine process in
    # any API replica, so API replicas can never become a second scheduler.
    assert not rendered["services"]["mai-api"].get("command")
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text()
    cmd_line = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "uvicorn" in cmd_line
    assert "mai.jobs.worker" not in cmd_line
    assert worker_cmd != cmd_line
    assert "MAI_WORKER_HANDLERS" not in rendered["services"]["mai-api"]["environment"]


def test_worker_secrets_and_mounts_are_scoped(rendered: dict) -> None:
    worker = rendered["services"]["mai-worker"]
    # Exactly the one database URL (fail-closed interpolation — the rendered
    # value is resolved, the requirement is proven by the negative render
    # below), the explicit data root, and the handler registration list.
    assert set(worker["environment"]) == {
        "MAI_DATABASE_URL",
        "MAI_DATA_ROOT",
        "MAI_WORKER_HANDLERS",
    }
    # Scoped data mount only; no Docker socket, no host binds.
    assert {m["type"] for m in worker["volumes"]} == {"volume"}
    mounts = {m["target"]: m["source"] for m in worker["volumes"]}
    assert set(mounts) == {"/var/lib/mai"}
    assert mounts["/var/lib/mai"] == "mai-data"
    assert set(worker["networks"]) == {"mai"}


def test_worker_readiness_restart_and_shutdown_are_real(rendered: dict) -> None:
    worker = rendered["services"]["mai-worker"]
    # Docker restarts a dead worker; a manual stop is honored.
    assert worker["restart"] == "unless-stopped"
    # SIGTERM drains the engine cleanly within the grace budget.
    assert worker["stop_grace_period"] == "30s"
    # Healthcheck probes ACTUAL worker readiness: the real entrypoint as
    # PID 1 AND the engine-owned dbos schema (created only by a full engine
    # launch) — never a fake exit-0 probe.
    probe = " ".join(worker["healthcheck"]["test"])
    assert "mai.jobs.worker" in probe and "/proc/1/cmdline" in probe
    assert "dbos" in probe and "workflow_status" in probe
    assert worker["healthcheck"]["start_period"]


def test_worker_and_api_are_independently_recreatable(rendered: dict) -> None:
    worker_deps = set(rendered["services"]["mai-worker"].get("depends_on", {}))
    api_deps = set(rendered["services"]["mai-api"].get("depends_on", {}))
    assert worker_deps == {"postgres"}
    assert "mai-worker" not in api_deps
    assert "mai-api" not in worker_deps


def test_worker_without_db_credential_fails_closed() -> None:
    """Negative control: the worker render needs the .env credential too."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    env = {k: v for k, v in os.environ.items() if k != "MAI_POSTGRES_PASSWORD"}
    proc = subprocess.run(
        _compose_args("config", "--format", "json", profile="worker"),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "MAI_POSTGRES_PASSWORD" in proc.stderr + proc.stdout


# ---------------------------------------------------------------------------
# Live lane (opt-in: MAI_WORKER_STACK_LIVE=1) — real stack, real engine.
# ---------------------------------------------------------------------------


class Stack:
    def run(
        self, *extra: str, timeout: int = 600, profile: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _compose(*extra, profile=profile, timeout=timeout)

    def exec(
        self, service: str, *args: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "-T", service, *args, timeout=timeout)

    def py(self, service: str, script: str, timeout: int = 120) -> str:
        """Run a control-plane python script inside a service container."""
        proc = self.exec(service, "python", "-c", script, timeout=timeout)
        assert proc.returncode == 0, proc.stderr[-4000:]
        return proc.stdout

    def psql(self, sql: str, timeout: int = 60) -> str:
        proc = self.exec(
            "postgres", "psql", "-U", "mai", "-d", "mai", "-tAc", sql, timeout=timeout
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def rows(self) -> list[dict]:
        proc = self.run("ps", "--format", "json", "--all")
        assert proc.returncode == 0, proc.stderr
        return _json_rows(proc.stdout)

    def wait_healthy(self, service: str, deadline_s: int = 180) -> dict:
        """Poll until the compose healthcheck reports healthy."""
        end = time.monotonic() + deadline_s
        last: dict = {}
        while time.monotonic() < end:
            matches = [row for row in self.rows() if row.get("Service") == service]
            if matches:
                last = matches[0]
                if last.get("Health") == "healthy":
                    return last
            time.sleep(2)
        raise AssertionError(f"{service} never became healthy: {last}")

    def cid(self, service: str) -> str:
        return sorted(self.cids(service))[0]

    def cids(self, service: str) -> set[str]:
        proc = self.run("ps", "-q", "-a", service)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.strip()
        assert out, f"{service} has no container"
        return set(out.splitlines())

    def inspect(self, cid: str) -> dict:
        out = subprocess.run(
            ["docker", "inspect", cid], capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)[0]


@pytest.fixture(scope="module")
def wstack() -> Stack:
    if not _docker_available():
        pytest.skip("docker CLI not available")
    if not _live_enabled():
        pytest.skip("opt-in live lane: MAI_WORKER_STACK_LIVE=1")
    s = Stack()
    built = s.run("build", "mai-api", timeout=900)
    assert built.returncode == 0, built.stderr[-4000:]
    up = s.run("up", "-d", "--wait", "postgres", "mai-api", "--wait-timeout", "300")
    assert up.returncode == 0, up.stderr[-4000:]
    # The migration harness rides in the image and never auto-runs; the
    # fixture applies the domain chain explicitly (card 01-db ownership).
    # Packaging bug (recorded, out of scope): the image COPY flattens
    # backend/migrations' CONTENTS into /app, so alembic.ini's
    # %(here)s/migrations does not exist in the image as shipped. The
    # fixture restores the directory into the disposable container (compose
    # cp — the image and the Dockerfile are not this card's surface).
    cp = subprocess.run(
        _compose_args(
            "cp", str(REPO_ROOT / "backend" / "migrations"), "mai-api:/app/migrations"
        ),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, **NEEDED_ENV},
        timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    mig = s.exec("mai-api", "alembic", "upgrade", "head", timeout=300)
    assert mig.returncode == 0, mig.stderr[-4000:]
    # Negative control (ordering contract): the engine-owned dbos schema is
    # created ONLY by a full engine launch — it must not exist yet, even
    # though the API (a bare control-plane client) and Alembic already ran.
    before = s.psql(
        "select count(*) from information_schema.tables where table_schema='dbos'"
    )
    assert before == "0", f"dbos schema exists before any worker launch: {before}"
    try:
        yield s
    finally:
        s.run(
            "down",
            "-v",
            "--remove-orphans",
            "--timeout",
            "10",
            profile="worker",
            timeout=300,
        )
        leftovers = {}
        for fmt, args in (
            (
                "containers",
                [
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.docker.compose.project={PROJECT}",
                ],
            ),
            (
                "volumes",
                [
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={PROJECT}",
                ],
            ),
            (
                "networks",
                [
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={PROJECT}",
                ],
            ),
        ):
            out = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=60
            )
            if out.returncode == 0 and out.stdout.strip():
                leftovers[fmt] = out.stdout.split()
        assert not leftovers, f"fixture left docker resources: {leftovers}"


_ENQUEUE_RUN = """
import json, uuid
from mai.database import resolve_database_url
from mai.jobs.engine import EngineClient

# A REAL (minimal) run intent document: the strict 05-steps schema is the
# worker's first gate; the composition check fires right after it.
run_id = str(uuid.uuid4())
server_id = str(uuid.uuid4())
intent = {
    "v": 1,
    "run_id": run_id,
    "server_id": server_id,
    "context": {
        "principal": "hash:fixture-principal",
        "server_id": server_id,
        "run_id": run_id,
        "parent_scope": None,
        "profile_revision": "profile-1",
        "prompt_revision": "prompt-1",
        "credential_references": [],
        "allowed_capabilities": ["@CAP@"],
        "mode": "delegated",
    },
    "steps": [{"seq": 1, "capability_id": "@CAP@", "payload": {}}],
}
with EngineClient(resolve_database_url()) as client:
    client.enqueue_run(run_id, "@HANDLER@", json.dumps(intent, sort_keys=True))
    state = client.run_state(run_id)
print(run_id)
print(state.value if state is not None else "none")
""".replace("@HANDLER@", STEPS_HANDLER).replace("@CAP@", STEPS_CAPABILITY)

_RUN_OUTCOME = """
from mai.database import resolve_database_url
from mai.jobs.engine import EngineClient

with EngineClient(resolve_database_url()) as client:
    state = client.run_state("@RUN_ID@")
    detail = ""
    if state is not None and state.value in {"succeeded", "failed", "cancelled"}:
        try:
            client.await_run("@RUN_ID@")
        except Exception as exc:  # the failed run's own value-free exception
            detail = f"{type(exc).__name__}: {exc}"
print(state.value if state is not None else "none")
print(detail)
"""

_SCHEDULE_CREATE = """
import asyncio, uuid
from mai.database import resolve_database_url, create_app_engine
from mai.jobs.schedules import ScheduleManager

async def main():
    url = resolve_database_url()
    engine, sessions = create_app_engine(url)
    manager = ScheduleManager(url, sessions)
    record = await manager.create(
        server_id=str(uuid.uuid4()),
        cron="*/5 * * * * *",
        run_spec={{"handler_name": {handler!r}, "payload": {{"fixture": "worker-stack"}}}},
    )
    print(record.schedule_id)
    manager.close()
    await engine.dispose()

asyncio.run(main())
""".format(handler=STEPS_HANDLER)

_SCHEDULE_SET_ENABLED = """
import asyncio
from mai.database import resolve_database_url, create_app_engine
from mai.jobs.schedules import ScheduleManager

async def main():
    url = resolve_database_url()
    engine, sessions = create_app_engine(url)
    manager = ScheduleManager(url, sessions)
    await manager.set_enabled({schedule_id!r}, enabled={enabled!r})
    manager.close()
    await engine.dispose()

asyncio.run(main())
"""

_SCHEDULE_DELETE = """
import asyncio
from mai.database import resolve_database_url, create_app_engine
from mai.jobs.schedules import ScheduleManager

async def main():
    url = resolve_database_url()
    engine, sessions = create_app_engine(url)
    manager = ScheduleManager(url, sessions)
    await manager.delete({schedule_id!r})
    manager.close()
    await engine.dispose()

asyncio.run(main())
"""


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_01_worker_starts_healthy_with_distinct_identity(wstack: Stack) -> None:
    up = wstack.run(
        "up",
        "-d",
        "--wait",
        "mai-worker",
        "--wait-timeout",
        "180",
        profile="worker",
    )
    assert up.returncode == 0, up.stdout + up.stderr
    worker = wstack.wait_healthy("mai-worker")
    assert worker["State"] == "running"
    # The readiness was REAL: the worker printed its launch-ready line.
    logs = wstack.run("logs", "mai-worker", profile="worker")
    assert "mai worker: ready" in logs.stdout + logs.stderr
    # Service identity distinct from the API: different container command.
    worker_cid = wstack.cid("mai-worker")
    api_cid = wstack.cid("mai-api")
    worker_cmd = wstack.inspect(worker_cid)["Config"]["Cmd"]
    api_cmd = wstack.inspect(api_cid)["Config"]["Cmd"]
    assert worker_cmd == ["python", "-m", "mai.jobs.worker"]
    assert "mai.jobs.worker" not in api_cmd
    assert api_cmd != worker_cmd
    _STATE["worker_cid"] = worker_cid


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_02_engine_schema_exists_only_after_worker_launch(wstack: Stack) -> None:
    # Positive half of the ordering contract (the fixture proved the negative
    # half): the dbos schema exists now — created by this worker's launch.
    tables = wstack.psql(
        "select count(*) from information_schema.tables where table_schema='dbos'"
    )
    assert int(tables) > 0
    # The worker is the only full engine in the topology: exactly one
    # mai-worker replica regardless of how the API scales.
    workers = [row for row in wstack.rows() if row.get("Service") == "mai-worker"]
    assert len(workers) == 1


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_03_two_api_replicas_and_no_duplicate_schedule_leader(wstack: Stack) -> None:
    # ONE schedule created through the control-plane facade.
    schedule_id = wstack.py("mai-api", _SCHEDULE_CREATE).strip().splitlines()[-1]
    assert schedule_id
    _STATE["schedule_id"] = schedule_id

    # Two API replicas: more control plane, never more schedulers.
    scaled = wstack.run(
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "240",
        "--scale",
        "mai-api=2",
        profile="worker",
    )
    assert scaled.returncode == 0, scaled.stdout + scaled.stderr
    api_rows = [row for row in wstack.rows() if row.get("Service") == "mai-api"]
    assert len(api_rows) == 2, api_rows
    for row in api_rows:
        assert row["State"] == "running" and row["Health"] == "healthy"
    for row in api_rows:
        info = wstack.inspect(row["Name"])
        assert "mai.jobs.worker" not in " ".join(info["Config"]["Cmd"])

    # Wait for the first fire (every-5-seconds cron, seconds-allowed grammar).
    end = time.monotonic() + 120
    while time.monotonic() < end:
        if int(wstack.psql("select count(*) from job_schedule_fires") or "0") >= 1:
            break
        time.sleep(2)
    wstack.py(
        "mai-api", _SCHEDULE_SET_ENABLED.format(schedule_id=schedule_id, enabled=False)
    )
    enabled = wstack.psql(
        f"select enabled from job_schedules where schedule_id='{schedule_id}'"
    )
    assert enabled in ("f", "false"), f"schedule pause did not persist: {enabled}"
    time.sleep(3)  # let an in-flight fire land, then freeze the counts

    # Wait until no fire workflow is mid-flight, then freeze the counts.
    end = time.monotonic() + 30
    while time.monotonic() < end:
        fire_success = int(
            wstack.psql(
                "select count(*) from dbos.workflow_status "
                "where name='schedule_fire_workflow' and status='SUCCESS'"
            )
        )
        fire_total = int(
            wstack.psql(
                "select count(*) from dbos.workflow_status where name='schedule_fire_workflow'"
            )
        )
        if fire_total == fire_success:
            break
        time.sleep(2)

    ledger_rows = int(wstack.psql("select count(*) from job_schedule_fires"))
    ledger_distinct = int(
        wstack.psql("select count(distinct planned_fire_time) from job_schedule_fires")
    )
    # No duplicate schedule leader: one ledger row per distinct planned fire
    # time, and exactly that many fire workflows — not one per API replica.
    assert ledger_rows >= 1, "schedule never fired"
    assert ledger_rows == ledger_distinct, (ledger_rows, ledger_distinct)
    assert fire_success == ledger_rows, (fire_success, ledger_rows)
    assert fire_total == fire_success, (fire_total, fire_success)

    # Owned-resource cleanup: the fixture schedule is removed again.
    wstack.py("mai-api", _SCHEDULE_DELETE.format(schedule_id=schedule_id))
    remaining = wstack.psql(
        f"select count(*) from job_schedules where schedule_id='{schedule_id}'"
    )
    assert remaining == "0"


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_04_pending_run_survives_full_api_recreation(wstack: Stack) -> None:
    # Clean worker shutdown first: SIGTERM must drain the engine to exit 0
    # (entrypoint shutdown contract) and the container stays as exited.
    stopped = wstack.run("stop", "mai-worker", timeout=120, profile="worker")
    assert stopped.returncode == 0, stopped.stderr
    worker_cid = wstack.cid("mai-worker")
    info = wstack.inspect(worker_cid)
    assert info["State"]["Status"] == "exited"
    assert info["State"]["ExitCode"] == 0, info["State"]
    _STATE["stopped_worker_cid"] = worker_cid

    # Enqueue a run while NO worker exists: it must be durably queued.
    out = wstack.py("mai-api", _ENQUEUE_RUN)
    run_id, state = out.strip().splitlines()[-2:]
    assert state == "queued", out
    _STATE["run_id"] = run_id

    # Recreate BOTH API replicas while the work is pending: stop, observe,
    # then force-recreate (compose would otherwise just restart the same
    # stopped containers, which is not a recreation).
    api_cids_before = wstack.cids("mai-api")
    down = wstack.run("stop", "mai-api", timeout=120, profile="worker")
    assert down.returncode == 0, down.stderr
    # Observation plane independent of the API: the engine DB still holds
    # the run, untouched, in ENQUEUED state while the API is down.
    status = wstack.psql(
        f"select status from dbos.workflow_status where workflow_uuid='{run_id}'"
    )
    assert status == "ENQUEUED", status
    up = wstack.run(
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "240",
        "--force-recreate",
        "--scale",
        "mai-api=2",
        "mai-api",
        profile="worker",
    )
    assert up.returncode == 0, up.stdout + up.stderr
    api_rows = [row for row in wstack.rows() if row.get("Service") == "mai-api"]
    assert len(api_rows) == 2 and all(
        r["Health"] == "healthy" and r["State"] == "running" for r in api_rows
    )
    # The API really was recreated: fresh container IDs ...
    api_cids_after = wstack.cids("mai-api")
    assert len(api_cids_after) == 2
    assert api_cids_after.isdisjoint(api_cids_before), (
        api_cids_before,
        api_cids_after,
    )
    # ... and the work is STILL durably queued after the recreation ...
    status = wstack.psql(
        f"select status from dbos.workflow_status where workflow_uuid='{run_id}'"
    )
    assert status == "ENQUEUED", status
    # ... while the stopped worker container was not touched (same ID, still
    # exited 0, not restarted by the API recreation).
    assert wstack.cids("mai-worker") == {worker_cid}
    info = wstack.inspect(worker_cid)
    assert info["State"]["Status"] == "exited"
    assert info["State"]["ExitCode"] == 0


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_05_recreated_worker_recovers_pending_run_exactly_once(wstack: Stack) -> None:
    run_id = _STATE["run_id"]
    old_cid = wstack.cid("mai-worker")
    # True recreation: remove the exited container, start a fresh one.
    removed = wstack.run("rm", "-sf", "mai-worker", profile="worker")
    assert removed.returncode == 0, removed.stderr
    up = wstack.run(
        "up",
        "-d",
        "--wait",
        "mai-worker",
        "--wait-timeout",
        "180",
        profile="worker",
    )
    assert up.returncode == 0, up.stdout + up.stderr
    worker = wstack.wait_healthy("mai-worker")
    assert worker["State"] == "running"
    new_cid = wstack.cid("mai-worker")
    assert new_cid != old_cid, "worker was not actually recreated"
    logs = wstack.run("logs", "mai-worker", profile="worker")
    assert "mai worker: ready" in logs.stdout + logs.stderr

    # The recovered run reaches a terminal state (the run executor's
    # composition is a later card's surface: value-free
    # executor_not_configured — the run was claimed and executed, never
    # silently dropped and never fabricated as a success).
    end = time.monotonic() + 180
    outcome = ""
    while time.monotonic() < end:
        outcome = wstack.py("mai-api", _RUN_OUTCOME.replace("@RUN_ID@", run_id))
        lines = outcome.strip().splitlines()
        if lines and lines[0] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(3)
    lines = outcome.strip().splitlines()
    assert lines[0] == "failed", (
        f"recovered run never reached a terminal state: {outcome}"
    )

    # Exactly once: one workflow row, executed by the registered production
    # handler (proves the MAI_WORKER_HANDLERS wiring), no duplicate run.
    count = wstack.psql(
        f"select count(*) from dbos.workflow_status where workflow_uuid='{run_id}'"
    )
    assert count == "1", count
    row = wstack.psql(
        "select status, recovery_attempts from dbos.workflow_status "
        f"where workflow_uuid='{run_id}'"
    ).split("|")
    assert row[0] == "ERROR", row
    assert int(row[1]) <= 1, row
    # The terminal reason lives in the serialized workflow_output record
    # (base64 pickle of the run's own value-free StepsError). Client-side
    # deserialization of StepsError is broken (its __init__ needs `code` and
    # `detail`; reconstruction gets one arg) — recorded as an out-of-scope
    # 05-steps/DBOS interop finding; the DURABLE record is read instead.
    present = wstack.psql(
        "select exists(select 1 from dbos.workflow_output where workflow_uuid="
        f"'{run_id}' and position(convert_to('executor_not_configured','UTF8') in "
        "decode(error,'base64')) > 0)"
    )
    assert present == "t", (row, outcome[:400])


@pytest.mark.skipif(
    not _live_enabled(), reason="opt-in live lane: MAI_WORKER_STACK_LIVE=1"
)
def test_06_restart_policy_recovers_self_exit_and_honors_manual_stop(
    wstack: Stack,
) -> None:
    cid = wstack.cid("mai-worker")
    # Deliverable crash trigger: PID 1 in a PID namespace IGNORES signals
    # with default dispositions (even SIGKILL) sent from inside, but the
    # worker has a SIGTERM handler — the container drains the engine and
    # self-exits (exit 0). `unless-stopped` must then restart it by itself.
    # (Observed docker behavior, recorded in evidence: a docker-API `kill`
    # counts as a manual stop and DISABLES the restart policy, so the
    # docker-API path is not a crash simulation.)
    wstack.exec(
        "mai-worker",
        "python",
        "-c",
        "import os, signal; os.kill(1, signal.SIGTERM)",
        timeout=60,
    )
    # unless-stopped: Docker restarts the exited worker itself (RestartCount
    # is a TOP-LEVEL inspect field, not under State; a fresh run resets
    # State.ExitCode to 0, so only RestartCount is asserted).
    end = time.monotonic() + 60
    restarted = False
    while time.monotonic() < end:
        info = wstack.inspect(cid)
        if info["RestartCount"] >= 1 and info["State"]["Status"] == "running":
            restarted = True
            break
        time.sleep(2)
    assert restarted, wstack.inspect(cid)
    healthy = wstack.wait_healthy("mai-worker", deadline_s=180)
    assert healthy["Health"] == "healthy"

    # Manual stop is honored: SIGTERM exit 0, and it STAYS stopped.
    stopped = wstack.run("stop", "mai-worker", timeout=120, profile="worker")
    assert stopped.returncode == 0, stopped.stderr
    info = wstack.inspect(cid)
    assert info["State"]["Status"] == "exited"
    assert info["State"]["ExitCode"] == 0, info["State"]
    time.sleep(5)
    assert wstack.inspect(cid)["State"]["Status"] == "exited", (
        "manual stop must not be undone by the restart policy"
    )
