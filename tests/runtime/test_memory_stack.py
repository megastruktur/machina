"""Focused Compose tests for the bundled Hindsight memory lane (card 11-hindsight-compose).

Static lane (always runs, seconds): renders ``compose.yaml`` through the real
``docker compose config`` and asserts the frozen memory-topology boundaries
(contracts.md C07 rule 7, knowledge-packaging ADR §2/§4):

- ``hindsight`` is pinned to the integration-time image tag and excluded from
  the DEFAULT start set (profile ``memory``, like ``--profile search``);
- the memory lane publishes no host port (API 8888 / UI 9999 stay internal),
  binds no host paths, grants no privileges, adds no new image (the one-shot
  ``hindsight-db-init`` reuses the pinned pgvector Postgres image);
- Hindsight's whole persistence story is the dedicated ``hindsight`` database
  of the stack Postgres (its own pg_dump namespace — C07 rule 3), provisioned
  idempotently before the service starts;
- the healthcheck probes upstream readiness (``/health`` fails when the
  database is unreachable — KB-H1), not a fake liveness;
- the frozen deployment decisions are in the render: embeddings provider
  ``openai`` / ``text-embedding-3-small`` (upstream ``local`` default is
  rejected by ADR §4), LLM credentials interpolated from the Compose ``.env``
  only — no baked-in secret anywhere;
- negative control: without ``MAI_POSTGRES_PASSWORD`` the render fails closed
  and names the variable;
- ``deploy/hindsight/README.md`` records the license and the restart/backup
  data ownership (acceptance criteria 1–2).

Live lane (opt-in, ``MAI_COMPOSE_LIVE=1``): under the disposable fixture
project ``mai-fxt-11-memory`` (D3 namespace) it proves the two behaviors this
card exists for — the DEFAULT start set boots healthy without any memory
container, and with ``--profile memory`` the real pinned image reaches
``healthy`` against the stack Postgres (health + version answered in-network,
state isolated in the dedicated database, health restored after a container
restart). The LLM/embeddings endpoint upstream verifies at startup is served
by a throwaway in-network stub (created from the hindsight image itself, so
no additional image and no host bind mount — the daemon here cannot see host
/tmp); the stub satisfies boot verification only and proves nothing about
retain/recall, which stays owned by ``11-memory-adapter``/``11-verify``
(KB-H2). A second live negative control starts the memory profile WITHOUT an
LLM key and requires the container to refuse to boot.

Stdlib only; runs under the backend venv like tests/runtime/test_compose.py.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
MEMORY_DOCS = REPO_ROOT / "deploy" / "hindsight" / "README.md"
PROJECT = "mai-fxt-11-memory"
NOKEY_PROJECT = "mai-fxt-11-memory-nokey"
HINDSIGHT_IMAGE = "ghcr.io/vectorize-io/hindsight:1.0.0"
FIXTURE_PASSWORD = "mai-fxt-throwaway-password"
FIXTURE_LLM_KEY = "mai-fxt-throwaway-llm-key"
STUB_CONTAINER = f"{PROJECT}-llm-stub"
STUB_URL = f"http://{STUB_CONTAINER}:8080/v1"
NEEDED_ENV = {
    "MAI_POSTGRES_PASSWORD": FIXTURE_PASSWORD,
    "HINDSIGHT_API_LLM_PROVIDER": "openai",
    "HINDSIGHT_API_LLM_MODEL": "stub-chat-model",
    "HINDSIGHT_API_LLM_BASE_URL": STUB_URL,
    "HINDSIGHT_API_LLM_API_KEY": FIXTURE_LLM_KEY,
    "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": STUB_URL,
    "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": FIXTURE_LLM_KEY,
}

# Throwaway OpenAI-compatible stub: GET /v1/models for upstream's boot
# verification, POST /v1/chat/completions and /v1/embeddings for anything that
# probes further. Embeddings answer with the contract dimension (1536) so a
# boot against this stub also proves the pinned `openai` provider initializes
# without downloading a local model. It implements nothing of retain/recall.
STUB_PY = """\
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

EMB = [0.001] * 1536

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send({"object": "list", "data": [
                {"id": "stub-chat-model", "object": "model", "owned_by": "fixture"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.rstrip("/")
        n = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(n)
        if p.endswith("/chat/completions"):
            self._send({"id": "stub-1", "object": "chat.completion", "created": 0,
                        "model": "stub-chat-model",
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": "stub"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}})
        elif p.endswith("/embeddings"):
            self._send({"object": "list",
                        "data": [{"object": "embedding", "index": 0, "embedding": EMB}],
                        "model": "text-embedding-3-small",
                        "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass

HTTPServer(("0.0.0.0", 8080), H).serve_forever()
"""


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
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    for var in ("MAI_POSTGRES_PASSWORD", *NEEDED_ENV):
        merged.pop(var, None)
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


def _json_rows(stdout: str) -> list[dict]:
    """Parse compose JSON output in either array or JSON-lines form (v5 quirk)."""
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _live_enabled() -> bool:
    return os.environ.get("MAI_COMPOSE_LIVE") == "1"


@pytest.fixture(scope="module")
def rendered() -> dict:
    """Full rendered Compose model (memory profile enabled, all services)."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = _compose("config", "--format", "json", profile="memory")
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


def test_memory_service_is_pinned_and_profile_gated(
    rendered: dict, rendered_default: dict
) -> None:
    hindsight = rendered["services"]["hindsight"]
    assert hindsight["image"] == HINDSIGHT_IMAGE
    assert "memory" in hindsight["profiles"], hindsight["profiles"]
    # Held disabled: the DEFAULT rendered start set contains no memory lane.
    assert "hindsight" not in rendered_default["services"]
    assert "hindsight-db-init" not in rendered_default["services"]
    init = rendered["services"]["hindsight-db-init"]
    assert "memory" in init["profiles"], init["profiles"]


def test_memory_lane_stays_internal_and_grants_nothing(rendered: dict) -> None:
    for name in ("hindsight", "hindsight-db-init"):
        svc = rendered["services"][name]
        assert not svc.get("ports"), f"{name} publishes {svc.get('ports')}"
        assert set(svc.get("networks", {})) == {"mai"}, name
        assert svc.get("privileged") in (None, False), name
        assert not svc.get("pid"), name
        assert svc.get("user") not in ("root", "0", "0:0"), name
        for mount in svc.get("volumes", []) or []:
            source = str(mount.get("source", ""))
            assert not source.startswith("/"), f"{name} bind-mounts {source}"
            assert "docker.sock" not in source, name
    # The service itself owns no storage: everything lives in Postgres.
    assert not rendered["services"]["hindsight"].get("volumes")


def test_hindsight_persistence_is_the_dedicated_database_namespace(
    rendered: dict,
) -> None:
    url = rendered["services"]["hindsight"]["environment"]["HINDSIGHT_API_DATABASE_URL"]
    parsed = urlparse(url)
    assert parsed.hostname == "postgres"
    assert parsed.port == 5432
    assert parsed.path == "/hindsight"
    assert parsed.username == "mai"
    # The credential is interpolated from the required Compose .env variable,
    # never baked in.
    assert parsed.password == FIXTURE_PASSWORD
    depends = rendered["services"]["hindsight"]["depends_on"]
    assert depends["hindsight-db-init"]["condition"] == "service_completed_successfully"
    assert depends["postgres"]["condition"] == "service_healthy"
    # The provisioner reuses the stack's pinned pgvector image — no new image.
    init = rendered["services"]["hindsight-db-init"]
    assert init["image"] == rendered["services"]["postgres"]["image"]
    script = " ".join(init["command"])
    assert "pg_database" in script and "datname='hindsight'" in script
    assert "CREATE DATABASE hindsight" in script


def test_hindsight_healthcheck_probes_real_readiness(rendered: dict) -> None:
    health = rendered["services"]["hindsight"]["healthcheck"]
    probe = " ".join(health["test"])
    assert "127.0.0.1:8888" in probe
    # /health is upstream readiness (fails when the DB is unreachable, KB-H1);
    # a liveness-only /health/live probe would badge a dead DB as healthy.
    assert "/health" in probe
    assert health.get("start_period"), health


def test_hindsight_env_pins_the_frozen_deployment_decisions(rendered: dict) -> None:
    env = rendered["services"]["hindsight"]["environment"]
    # ADR §4: same OpenAI-compatible gateway and model as the doc index,
    # native 1536 dims — no dimensions override anywhere in the render.
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openai"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"] == "text-embedding-3-small"
    assert "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS" not in env
    # LLM/embeddings credentials come from the Compose .env only: the source
    # interpolates empty defaults (never baked-in values), so the render of a
    # consumer that never enables this profile carries no secret material.
    source = COMPOSE_FILE.read_text()
    # Provider falls back to the explicit upstream default, every credential
    # and endpoint to an EMPTY default.
    assert "${HINDSIGHT_API_LLM_PROVIDER:-openai}" in source
    for var in (
        "HINDSIGHT_API_LLM_MODEL",
        "HINDSIGHT_API_LLM_BASE_URL",
        "HINDSIGHT_API_LLM_API_KEY",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY",
    ):
        assert "${%s:-}" % var in source, var
    assert FIXTURE_LLM_KEY not in source
    # Same restart posture as mai-worker; upstream's start script asks for a
    # 30s SIGTERM grace period to drain API + control plane.
    assert rendered["services"]["hindsight"]["restart"] == "unless-stopped"
    assert rendered["services"]["hindsight"]["stop_grace_period"] == "30s"


def test_missing_postgres_password_fails_the_render() -> None:
    """Negative control: without the .env credential the topology fails closed
    even for consumers that only asked for the memory profile."""
    env = {k: v for k, v in os.environ.items() if k != "MAI_POSTGRES_PASSWORD"}
    proc = subprocess.run(
        _compose_args("config", "--format", "json", profile="memory"),
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=120,
    )
    assert proc.returncode != 0
    assert "MAI_POSTGRES_PASSWORD" in proc.stderr + proc.stdout


def test_deployment_docs_record_license_and_data_ownership() -> None:
    text = MEMORY_DOCS.read_text()
    assert "MIT" in text, "license must be documented (acceptance 1)"
    assert "pg_dump" in text, "backup ownership must be documented (acceptance 2)"
    assert "restart" in text.lower(), "restart ownership must be documented"
    assert "hindsight" in text and "postgres" in text.lower()


# ---------------------------------------------------------------------------
# Live lane (opt-in: MAI_COMPOSE_LIVE=1) — real pinned image, real startup.
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, project: str) -> None:
        self.project = project

    def run(self, *extra: str, profile: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        return _compose(*extra, project=self.project, profile=profile, timeout=timeout)

    def exec(self, service: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "-T", service, *args, timeout=timeout)

    def ps(self, all: bool = False) -> dict[str, dict]:
        extra = ["--all"] if all else []
        proc = self.run("ps", "--format", "json", *extra)
        assert proc.returncode == 0, proc.stderr
        rows = _json_rows(proc.stdout)
        return {row["Service"]: row for row in rows}

    def wait_health(self, service: str, want: str, deadline_s: int = 240) -> dict:
        end = time.monotonic() + deadline_s
        last: dict = {}
        while time.monotonic() < end:
            last = self.ps(all=True).get(service, {})
            if last.get("Health") == want:
                return last
            time.sleep(2)
        raise AssertionError(f"{service} never became {want}: {last}")

    def wait_healthy(self, service: str, deadline_s: int = 300) -> dict:
        return self.wait_health(service, "healthy", deadline_s)

    def inspect(self, service: str) -> dict:
        rows = self.ps(all=True)
        name = rows[service]["Name"]
        inspect = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, timeout=60
        )
        assert inspect.returncode == 0, inspect.stderr
        return json.loads(inspect.stdout)[0]


def _psql(stack: Stack, database: str, sql: str) -> str:
    proc = stack.exec(
        "postgres", "psql", "-U", "mai", "-d", database, "-tAc", sql, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _start_llm_stub() -> None:
    """Throwaway in-network OpenAI-compatible stub, served from the hindsight
    image itself (no extra image, no host bind mount — the daemon on this host
    runs with a private /tmp)."""
    subprocess.run(["docker", "rm", "-f", STUB_CONTAINER], capture_output=True, timeout=60)
    b64 = base64.b64encode(STUB_PY.encode()).decode()
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--name", STUB_CONTAINER,
            "--network", f"{PROJECT}_mai",
            "--entrypoint", "/bin/sh", HINDSIGHT_IMAGE,
            "-c", f"echo {b64} | base64 -d > /tmp/mai-llm-stub.py && exec python /tmp/mai-llm-stub.py",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", STUB_CONTAINER,
             "python", "-c",
             "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/v1/models', timeout=3).status)"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "200":
            return
        time.sleep(1)
    raise AssertionError(f"LLM stub never became ready: {probe.stderr}")


def _assert_no_leftovers(project: str) -> None:
    # Label-scoped to the exact compose project: sibling fixture projects
    # share the mai-fxt-11-memory name prefix and must not match.
    label = f"com.docker.compose.project={project}"
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert not out.stdout.strip(), f"fixture left containers: {out.stdout}"
    vols = subprocess.run(
        ["docker", "volume", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"],
        capture_output=True, text=True, timeout=60,
    )
    assert vols.returncode == 0, vols.stderr
    assert not vols.stdout.strip(), f"fixture left volumes: {vols.stdout}"
    nets = subprocess.run(
        ["docker", "network", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"],
        capture_output=True, text=True, timeout=60,
    )
    assert nets.returncode == 0, nets.stderr
    assert not nets.stdout.strip(), f"fixture left networks: {nets.stdout}"


@pytest.fixture(scope="module")
def core() -> Stack:
    """Phase 1: the DEFAULT start set boots — without any memory service."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    assert _live_enabled(), "live lane requires MAI_COMPOSE_LIVE=1"
    s = Stack(PROJECT)
    # Precondition: a clean project namespace. A previous crashed run must
    # not leak profile containers into this module's observations (compose ps
    # lists them regardless of the currently enabled profiles).
    subprocess.run(["docker", "rm", "-f", STUB_CONTAINER], capture_output=True, timeout=60)
    s.run("down", "-v", "--remove-orphans", "--timeout", "1", profile="memory", timeout=120)
    _assert_no_leftovers(PROJECT)
    up = s.run("up", "-d", "--wait", "--wait-timeout", "900")
    assert up.returncode == 0, up.stderr[-4000:]
    rows = s.ps()
    s.default_rows = rows
    names = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=60,
    )
    s.default_container_names = sorted(names.stdout.split())
    try:
        yield s
    finally:
        # Runs after the `stack` fixture teardown: removes everything the
        # module created (idempotent) and proves the project left nothing.
        # --profile memory is required: without it compose down ignores the
        # profile-gated containers the module started.
        subprocess.run(["docker", "rm", "-f", STUB_CONTAINER], capture_output=True, timeout=60)
        down = s.run("down", "-v", "--remove-orphans", "--timeout", "10", profile="memory", timeout=300)
        assert down.returncode == 0, down.stderr
        _assert_no_leftovers(PROJECT)


@pytest.fixture(scope="module")
def stack(core: Stack) -> Stack:
    """Phase 2: the memory profile joins the RUNNING core stack (the real
    'enabled later' case) and hindsight reaches healthy against the stack
    Postgres, with the in-network LLM stub satisfying upstream's boot
    verification."""
    _start_llm_stub()
    up = core.run(
        "up", "-d", "--wait", "--wait-timeout", "600",
        profile="memory",
    )
    assert up.returncode == 0, up.stderr[-4000:]
    return core


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_default_start_set_boots_healthy_without_memory(core: Stack) -> None:
    rows = core.default_rows
    assert set(rows) == {"postgres", "mai-api", "mai-web"}, sorted(rows)
    for svc, row in rows.items():
        assert row["Health"] == "healthy", f"{svc}: {row}"
    joined = " ".join(core.default_container_names)
    assert "hindsight" not in joined, core.default_container_names


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_memory_profile_joins_running_core_and_reaches_healthy(stack: Stack) -> None:
    rows = stack.ps()
    assert rows["hindsight"]["Health"] == "healthy", rows.get("hindsight")
    assert rows["mai-api"]["Health"] == "healthy"
    assert rows["mai-web"]["Health"] == "healthy"
    assert rows["postgres"]["Health"] == "healthy"
    # The one-shot provisioner did its job and exited.
    init = stack.ps(all=True)["hindsight-db-init"]
    assert init["State"] == "exited" and init["ExitCode"] == 0, init


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_health_and_version_answer_inside_the_network(stack: Stack) -> None:
    health = stack.exec(
        "hindsight", "python", "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=5).status)",
    )
    assert health.stdout.strip() == "200", health.stdout + health.stderr
    version = stack.exec(
        "hindsight", "python", "-c",
        "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8888/version', timeout=5))['api_version'])",
    )
    assert version.stdout.strip() == "1.0.0", version.stdout + version.stderr


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_memory_state_is_isolated_in_the_dedicated_database(stack: Stack) -> None:
    databases = _psql(stack, "postgres", "SELECT datname FROM pg_database")
    assert "hindsight" in databases.split()

    def hindsight_tables() -> set[str]:
        return {
            row.strip()
            for row in _psql(
                stack, "hindsight",
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
            ).splitlines()
            if row.strip()
        }

    # Migrations are part of boot; give them a bounded window after the first
    # green health probe before judging the namespace.
    tables: set[str] = set()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        tables = hindsight_tables()
        if tables:
            break
        time.sleep(2)
    assert tables, "hindsight migrations created no tables in the dedicated database"
    mai_tables = {
        row.strip()
        for row in _psql(
            stack, "mai",
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        ).splitlines()
        if row.strip()
    }
    # The dedicated namespace is isolated: Hindsight's tables exist only in
    # the hindsight database, never in the controller's own database.
    assert not (tables & mai_tables), sorted(tables & mai_tables)
    extensions = _psql(stack, "hindsight", "SELECT extname FROM pg_extension")
    assert "vector" in extensions.split()
    assert "pg_trgm" in extensions.split()


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_no_memory_container_publishes_a_host_port(stack: Stack) -> None:
    for svc in ("hindsight", "hindsight-db-init"):
        bindings = stack.inspect(svc)["HostConfig"].get("PortBindings") or {}
        assert bindings == {}, f"{svc} published {bindings}"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_hindsight_restores_health_after_container_restart(stack: Stack) -> None:
    # Restart ownership proof: the container is disposable, the data is not —
    # state stays in the stack Postgres and the service comes back healthy.
    restarted = stack.run("restart", "hindsight", timeout=180)
    assert restarted.returncode == 0, restarted.stderr
    row = stack.wait_healthy("hindsight", deadline_s=300)
    assert row["Health"] == "healthy", row
    version = stack.exec(
        "hindsight", "python", "-c",
        "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8888/version', timeout=5))['api_version'])",
    )
    assert version.stdout.strip() == "1.0.0", version.stdout + version.stderr


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_hindsight_refuses_to_boot_without_llm_key() -> None:
    """Live negative control: without the LLM key in the environment the
    memory service must fail closed at startup — never launch degraded and
    never badge itself healthy."""
    env = {k: v for k, v in os.environ.items() if k not in NEEDED_ENV}
    env["MAI_POSTGRES_PASSWORD"] = FIXTURE_PASSWORD

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            _compose_args(*extra, project=NOKEY_PROJECT, profile="memory"),
            capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=300,
        )

    # `--wait` on the one-shot itself always fails by design (a completed
    # container is not "running"), so bootstrap the dependencies in steps:
    # postgres healthy → init completed → then start hindsight alone.
    deps = run("up", "-d", "--wait", "--wait-timeout", "300", "postgres")
    assert deps.returncode == 0, deps.stderr[-4000:]
    init = run("up", "-d", "hindsight-db-init")
    assert init.returncode == 0, init.stderr[-4000:]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ps = run("ps", "--all", "--format", "json")
        assert ps.returncode == 0, ps.stderr
        init_row = next(
            (r for r in _json_rows(ps.stdout) if r["Service"] == "hindsight-db-init"), {}
        )
        if init_row.get("State") == "exited" and init_row.get("ExitCode") == 0:
            break
        time.sleep(2)
    else:
        raise AssertionError(f"db-init never completed: {init_row}")
    up = run("up", "-d", "--no-deps", "hindsight")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        # The service holds restart: unless-stopped (same posture as
        # mai-worker), so a missing key is a visible crash-restart loop, not
        # a quiet terminal exit. Fail-closed means: it NEVER serves healthy
        # and the cause is named in the logs.
        rows: list[dict] = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ps = run("ps", "--all", "--format", "json")
            assert ps.returncode == 0, ps.stderr
            rows = [r for r in _json_rows(ps.stdout) if r["Service"] == "hindsight"]
            if rows and rows[0].get("Health") == "healthy":
                break
            time.sleep(2)
        assert not (rows and rows[0].get("Health") == "healthy"), (
            f"hindsight turned healthy without an LLM key: {rows}"
        )
        inspect = subprocess.run(
            ["docker", "inspect", rows[0]["Name"]],
            capture_output=True, text=True, timeout=60,
        )
        assert inspect.returncode == 0, inspect.stderr
        info = json.loads(inspect.stdout)[0]
        state = info["State"]
        assert state["Status"] in ("restarting", "exited"), state
        # RestartCount is a top-level inspect field (not under State).
        assert info["RestartCount"] >= 1 or state["Status"] == "exited", info
        assert state["ExitCode"] != 0, state
        logs = run("logs", "hindsight")
        assert "LLM API key is required" in logs.stdout + logs.stderr, (
            logs.stdout[-2000:] + logs.stderr[-2000:]
        )
    finally:
        down = run("down", "-v", "--remove-orphans", "--timeout", "10")
        assert down.returncode == 0, down.stderr
        _assert_no_leftovers(NOKEY_PROJECT)
