"""Focused Compose-topology tests for the P01 packaging (card 01-compose).

Static lane (always runs, seconds): renders ``compose.yaml`` through the real
``docker compose config`` and asserts the frozen topology boundaries from the
brief acceptance criteria:

- nothing publishes a host port (postgres has no public port by default; the
  TLS proxy card owns the only published listener — auth ADR §6, C08);
- no host Docker socket, no privileged/pid grants, no root user overrides;
- state lives in named volumes (persistent config/data mounts), never in host
  bind mounts;
- ``mai-worker`` has an explicit command, is excluded from the default start
  set (profile ``worker``), and is not a fake healthy daemon;
- ``mai-api`` reaches Postgres over the internal network via the required
  ``MAI_POSTGRES_PASSWORD`` interpolation (missing ⇒ fail-closed render).

Live lane (opt-in, ``MAI_COMPOSE_LIVE=1``): builds the core images, starts the
stack under the disposable fixture project ``mai-fxt-01-compose`` (D3
namespace), and exercises the real entrypoints: liveness/readiness in-network,
mai-web HTML, non-root uids, published-port absence on live containers, the
worker real-entrypoint force-start (healthy), Postgres data persistence across a
restart, and an API restart. Teardown removes every project resource and
asserts nothing was left behind.

Stdlib only; runs under the backend venv like tests/runtime/test_runner.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
PROJECT = "mai-fxt-01-compose"
FIXTURE_PASSWORD = "mai-fxt-throwaway-password"
NEEDED_ENV = {"MAI_POSTGRES_PASSWORD": FIXTURE_PASSWORD}


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_args(*extra: str, project: str | None = None, profile: str | None = None) -> list[str]:
    args = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if project is not None:
        args += ["-p", project]
    if profile is not None:
        # --profile is a top-level compose flag; it precedes the subcommand.
        args += ["--profile", profile]
    return args + list(extra)


def _compose(
    *extra: str,
    project: str | None = None,
    profile: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.pop("MAI_POSTGRES_PASSWORD", None)
    merged.update(NEEDED_ENV)
    if env is not None:
        merged.update(env)
    return subprocess.run(
        _compose_args(*extra, project=project, profile=profile),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=merged,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def rendered() -> dict:
    """Full rendered Compose model (every profile enabled, all services)."""
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


# ---------------------------------------------------------------------------
# Static topology lane
# ---------------------------------------------------------------------------


def test_both_images_build_from_repo_root_contexts(rendered: dict) -> None:
    build_files = {
        svc["build"]["dockerfile"]
        for svc in rendered["services"].values()
        if "build" in svc
    }
    assert build_files == {"backend/Dockerfile", "frontend/Dockerfile"}
    for rel in sorted(build_files):
        assert (REPO_ROOT / rel).is_file(), rel


def test_no_service_publishes_a_host_port(rendered: dict) -> None:
    published = {
        name: svc.get("ports")
        for name, svc in rendered["services"].items()
        if svc.get("ports")
    }
    assert published == {}, f"services with host ports: {sorted(published)}"


def test_postgres_is_pinned_pgvector_with_persistent_volume(rendered: dict) -> None:
    pg = rendered["services"]["postgres"]
    image = pg["image"]
    assert image.startswith("pgvector/pgvector:")
    assert not image.endswith(":latest")
    major = image.rsplit(":", 1)[1]
    assert re.search(r"0\.(?:[8-9]|[1-9]\d+)\.\d+", major), image  # pgvector >= 0.8.0
    assert re.search(r"pg\d+", major), image  # postgres major line is explicit
    mounts = {m["target"]: m for m in pg["volumes"]}
    assert mounts["/var/lib/postgresql"]["type"] == "volume"
    assert mounts["/var/lib/postgresql"]["source"] == "postgres-data"


def test_state_lives_in_named_volumes_never_host_binds(rendered: dict) -> None:
    declared = set(rendered["volumes"])
    for name, svc in rendered["services"].items():
        for mount in svc.get("volumes", []):
            assert mount["type"] == "volume", f"{name}: host bind mount {mount}"
            assert mount["source"] in declared, f"{name}: undeclared volume {mount}"
    assert {"postgres-data", "mai-data"} <= declared
    api = rendered["services"]["mai-api"]
    targets = {m["target"]: m["source"] for m in api["volumes"]}
    assert api["environment"]["MAI_DATA_ROOT"] == "/var/lib/mai"
    assert targets["/var/lib/mai"] == "mai-data"


def test_no_docker_socket_and_no_privilege_grants(rendered: dict) -> None:
    for name, svc in rendered["services"].items():
        for mount in svc.get("volumes", []):
            assert "docker.sock" not in str(mount.get("source", "")), name
        assert not svc.get("privileged"), name
        assert "pid" not in svc, name
        assert not svc.get("cap_add"), name
        assert svc.get("user") not in ("root", "0", "0:0"), name


def test_worker_is_explicit_disabled_by_default_and_honest(
    rendered: dict, rendered_default: dict
) -> None:
    # Held disabled: the DEFAULT rendered start set does not contain it.
    assert "mai-worker" not in rendered_default["services"]
    assert {"postgres", "mai-api", "mai-web"} == set(rendered_default["services"])
    worker = rendered["services"]["mai-worker"]
    # Profile-gated: only `docker compose --profile worker …` may start it.
    assert worker.get("profiles") == ["worker"]
    # (05-worker-compose) Real C05 entrypoint — the durable engine worker —
    # same backend image/package, no HTTP server in the worker role.
    command = " ".join(worker["command"])
    assert command == "python -m mai.jobs.worker"
    assert "uvicorn" not in command
    assert worker["image"] == rendered["services"]["mai-api"]["image"]
    assert worker["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert not worker.get("ports")


def test_api_wires_internal_postgres_over_the_compose_network(rendered: dict) -> None:
    api = rendered["services"]["mai-api"]
    url = urlparse(api["environment"]["MAI_DATABASE_URL"])
    assert url.scheme == "postgresql"
    assert url.hostname == "postgres"  # service name on the compose network
    assert url.port == 5432
    assert url.username == "mai"
    assert url.path == "/mai"
    assert url.password == FIXTURE_PASSWORD  # interpolation actually resolves
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert set(api["networks"]) == {"mai"}
    assert set(rendered["services"]["postgres"]["networks"]) == {"mai"}


def test_missing_required_password_fails_the_render() -> None:
    """Negative control: without the .env credential the topology fails closed."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    env = {k: v for k, v in os.environ.items() if k != "MAI_POSTGRES_PASSWORD"}
    proc = subprocess.run(
        _compose_args("config", "--format", "json"),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "MAI_POSTGRES_PASSWORD" in proc.stderr + proc.stdout


def test_image_definitions_run_as_non_root() -> None:
    backend = (REPO_ROOT / "backend" / "Dockerfile").read_text()
    frontend = (REPO_ROOT / "frontend" / "Dockerfile").read_text()
    assert re.search(r"^USER 10001:10001$", backend, re.M), backend
    assert re.search(r"^USER node$", frontend, re.M), frontend
    assert "docker.sock" not in backend + frontend


def test_healthchecks_back_the_service_healthy_conditions(rendered: dict) -> None:
    pg_test = " ".join(rendered["services"]["postgres"]["healthcheck"]["test"])
    api_test = " ".join(rendered["services"]["mai-api"]["healthcheck"]["test"])
    web_test = " ".join(rendered["services"]["mai-web"]["healthcheck"]["test"])
    assert "pg_isready" in pg_test
    assert "/api/v1/health" in api_test  # liveness path, dependency-free
    assert "127.0.0.1:3000" in web_test


# ---------------------------------------------------------------------------
# Live lane (opt-in: MAI_COMPOSE_LIVE=1) — real build, real entrypoints.
# ---------------------------------------------------------------------------


def _live_enabled() -> bool:
    return os.environ.get("MAI_COMPOSE_LIVE") == "1"


def _json_rows(stdout: str) -> list[dict]:
    """Parse compose JSON output in either array or JSON-lines form (v5 quirk)."""
    text = stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, list):
        return payload
    return [payload]


class Stack:
    def run(
        self, *extra: str, timeout: int = 600, profile: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _compose(*extra, project=PROJECT, profile=profile, timeout=timeout)

    def exec(self, service: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "-T", service, *args, timeout=timeout)

    def ps(self, all: bool = False) -> dict[str, dict]:
        extra = ["--all"] if all else []
        proc = self.run("ps", "--format", "json", *extra)
        assert proc.returncode == 0, proc.stderr
        rows = _json_rows(proc.stdout)
        return {row["Service"]: row for row in rows}

    def wait_status(self, service: str, want: str, deadline_s: int = 120) -> dict:
        end = time.monotonic() + deadline_s
        last: dict = {}
        while time.monotonic() < end:
            last = self.ps(all=True).get(service, {})
            state = last.get("State", "")
            if state == want:
                return last
            time.sleep(1)
        raise AssertionError(f"{service} never reached {want}: {last}")

    def wait_healthy(self, service: str, deadline_s: int = 120) -> dict:
        """Poll until the compose healthcheck reports healthy (running is not enough)."""
        end = time.monotonic() + deadline_s
        last: dict = {}
        while time.monotonic() < end:
            last = self.ps().get(service, {})
            if last.get("Health") == "healthy":
                return last
            time.sleep(1)
        raise AssertionError(f"{service} never became healthy: {last}")

    def inspect(self, service: str) -> dict:
        rows = self.ps(all=True)
        name = rows[service]["Name"]
        inspect = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, timeout=60
        )
        assert inspect.returncode == 0, inspect.stderr
        return json.loads(inspect.stdout)[0]


@pytest.fixture(scope="module")
def stack() -> Stack:
    if not _docker_available():
        pytest.skip("docker CLI not available")
    assert _live_enabled(), "live lane requires MAI_COMPOSE_LIVE=1"
    s = Stack()
    built = s.run("build")
    assert built.returncode == 0, built.stderr[-4000:]
    up = s.run("up", "-d", "--wait", "--wait-timeout", "300")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        yield s
    finally:
        # v5 `down` respects profiles: without the worker profile the exited
        # placeholder container (and its attached volume) would survive.
        s.run(
            "down", "-v", "--remove-orphans", "--timeout", "10",
            profile="worker", timeout=300,
        )
        leftovers = {}
        for fmt, args in (
            ("containers", ["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]),
            ("volumes", ["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"]),
            ("networks", ["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"]),
        ):
            out = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=60
            )
            if out.returncode == 0 and out.stdout.strip():
                leftovers[fmt] = out.stdout.split()
        assert not leftovers, f"fixture left docker resources: {leftovers}"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_core_services_run_healthy_and_worker_is_absent(stack: Stack) -> None:
    rows = stack.ps()
    for svc in ("postgres", "mai-api", "mai-web"):
        assert svc in rows, f"{svc} did not start"
        assert rows[svc]["State"] == "running"
        assert rows[svc]["Health"] == "healthy"
    assert "mai-worker" not in rows, "worker must not start with the default profile set"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_api_health_readiness_and_web_html_inside_the_network(stack: Stack) -> None:
    health = stack.exec(
        "mai-api",
        "python",
        "-c",
        "import urllib.request; r=urllib.request.urlopen('http://mai-api:8000/api/v1/health', timeout=5); print(r.status, r.read().decode())",
    )
    assert health.returncode == 0 and health.stdout.startswith("200"), health.stderr
    ready = stack.exec(
        "mai-api",
        "python",
        "-c",
        "import urllib.request; r=urllib.request.urlopen('http://mai-api:8000/api/v1/health/ready', timeout=5); print(r.status)",
    )
    assert ready.returncode == 0 and ready.stdout.startswith("200"), ready.stderr
    web = stack.exec(
        "mai-api",
        "python",
        "-c",
        "import urllib.request; r=urllib.request.urlopen('http://mai-web:3000/', timeout=5); print(r.status, r.headers.get('content-type'), r.read(256))",
    )
    assert web.returncode == 0, web.stderr
    assert web.stdout.startswith("200") and "text/html" in web.stdout


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_no_container_binds_a_host_port(stack: Stack) -> None:
    for svc, private in (("postgres", "5432"), ("mai-api", "8000"), ("mai-web", "3000")):
        port_cmd = stack.run("port", svc, private)
        # v5 quirk: unpublished ports print a placeholder like "invalid IP:0"
        # with exit 0. A real publication always renders "<host-ip>:<port>".
        bound = [
            line
            for line in port_cmd.stdout.split()
            if re.fullmatch(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\[::\]):\d+", line)
        ]
        assert not bound, f"{svc} unexpectedly published: {bound}"
        info = stack.inspect(svc)
        assert info["HostConfig"]["PortBindings"] in (None, {})
        for bindings in info["NetworkSettings"]["Ports"].values():
            assert not bindings, f"{svc} published {bindings}"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_app_containers_run_as_non_root(stack: Stack) -> None:
    assert stack.exec("mai-api", "id", "-u").stdout.strip() == "10001"
    assert stack.exec("mai-web", "id", "-u").stdout.strip() == "1000"
    for svc in ("mai-api", "mai-web", "postgres"):
        info = stack.inspect(svc)
        assert not info["HostConfig"]["Privileged"], svc


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_worker_force_start_runs_the_real_entrypoint(stack: Stack) -> None:
    # (05-worker-compose) The worker is no longer a placeholder: force-start
    # with the profile must reach a HEALTHY real daemon (real readiness
    # healthcheck), then be removed cleanly.
    up = stack.run("--profile", "worker", "up", "-d", "--wait", "mai-worker", "--wait-timeout", "120")
    assert up.returncode == 0, up.stdout + up.stderr
    row = stack.wait_status("mai-worker", "running", deadline_s=90)
    assert row["ExitCode"] in (None, 0)
    logs = stack.run("logs", "mai-worker", profile="worker")
    assert "mai worker: ready" in logs.stdout + logs.stderr
    removed = stack.run("--profile", "worker", "rm", "-sf", "mai-worker")
    assert removed.returncode == 0, removed.stderr


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_postgres_data_persists_across_restart(stack: Stack) -> None:
    write = stack.exec(
        "postgres",
        "psql",
        "-U",
        "mai",
        "-d",
        "mai",
        "-c",
        "CREATE TABLE IF NOT EXISTS mai_compose_persistence (id int);"
        " TRUNCATE mai_compose_persistence; INSERT INTO mai_compose_persistence VALUES (42);",
    )
    assert write.returncode == 0, write.stderr
    restarted = stack.run("restart", "postgres", timeout=180)
    assert restarted.returncode == 0, restarted.stderr
    stack.wait_healthy("postgres", deadline_s=120)
    read = stack.exec(
        "postgres",
        "psql",
        "-U",
        "mai",
        "-d",
        "mai",
        "-t",
        "-A",
        "-c",
        "SELECT id FROM mai_compose_persistence",
    )
    assert read.stdout.strip() == "42", read.stdout + read.stderr


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_api_restarts_and_stays_healthy(stack: Stack) -> None:
    restarted = stack.run("restart", "mai-api", timeout=180)
    assert restarted.returncode == 0, restarted.stderr
    stack.wait_healthy("mai-api", deadline_s=120)
    health = stack.exec(
        "mai-api",
        "python",
        "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status)",
    )
    assert health.stdout.strip() == "200", health.stderr
