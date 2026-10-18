"""Focused Compose tests for the bundled SearXNG pair (card 07-searxng).

Acceptance covered (brief P07 / C07 rule 7 / knowledge-packaging ADR §3):

1. SearXNG runs the documented API/config with its required auxiliary service
   (valkey) and private networking; the pair is pinned, profile-gated and
   internal-only.
2. No secret in committed settings (`server.secret_key` is absent; the
   `SEARXNG_SECRET` .env variable is injected via the native upstream env
   override and the container refuses to start without it); upstream
   query/egress behavior is documented in deploy/searxng/settings.yml and
   auxiliary-service failure is observable through the readiness healthcheck.

Static lane (always runs, seconds): renders ``compose.yaml`` through the real
``docker compose config`` and asserts topology boundaries: pinned images,
profile gating, no host ports, internal-only networks, named-volume state,
committed-config mount, startup fail-closed secret, and the settings.yml
contract (json format enabled, limiter deliberately off with the documented
upstream API-window reason, valkey wired, no secret material).

Live lane (opt-in, ``MAI_COMPOSE_LIVE=1``): starts the pair under the
disposable fixture project ``mai-fxt-07-searxng`` (C10 D3 namespace) and
exercises the real entrypoints: healthy state, live published-port absence,
an actual JSON query returning structured results from an enabled upstream
engine, /config introspection, restart persistence of config, the valkey
failure flipping searxng unhealthy (and recovering), plus two real-boundary
negatives in throwaway projects: an unreachable engine surfacing explicitly
in ``unresponsive_engines`` (fixture engine pointed at RFC 5737 TEST-NET-1),
the documented 403 when ``format=json`` is requested from an instance
whose settings enable only html, and a paired rate-cap control: the
committed config (limiter off) answers six back-to-back JSON queries from
one IP, while an otherwise identical limiter-on fixture trips upstream's
hard-coded 4-json-requests/hour window at the fifth — the measured reason
the committed settings keep the limiter off. Teardown removes every
project resource, fixture file and /tmp artifact and asserts nothing was
left behind.

Stdlib only; runs under the backend venv like tests/runtime/test_compose.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
SETTINGS_FILE = REPO_ROOT / "deploy" / "searxng" / "settings.yml"
PROJECT = "mai-fxt-07-searxng"
DEAD_PROJECT = "mai-fxt-07-searxng-dead"
NOSECRET_PROJECT = "mai-fxt-07-searxng-nosecret"
NOJSON_PROJECT = "mai-fxt-07-searxng-nojson"
LIMITED_PROJECT = "mai-fxt-07-searxng-limited"
FIXTURE_DIR = Path("/tmp/mai-fxt-07-searxng")
FIXTURE_PASSWORD = "mai-fxt-throwaway-password"
FIXTURE_SECRET = "mai-fxt-throwaway-secret-4f2c9b1d"
NEEDED_ENV = {
    "MAI_POSTGRES_PASSWORD": FIXTURE_PASSWORD,
    "SEARXNG_SECRET": FIXTURE_SECRET,
}


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_args(
    *extra: str,
    project: str | None = None,
    profile: str | None = None,
    override: Path | None = None,
) -> list[str]:
    args = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if override is not None:
        args += ["-f", str(override)]
    if project is not None:
        args += ["-p", project]
    if profile is not None:
        args += ["--profile", profile]
    return args + list(extra)


def _compose(
    *extra: str,
    project: str | None = None,
    profile: str | None = None,
    override: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.pop("MAI_POSTGRES_PASSWORD", None)
    merged.pop("SEARXNG_SECRET", None)
    merged.update(NEEDED_ENV)
    if env is not None:
        merged.update(env)
    return subprocess.run(
        _compose_args(*extra, project=project, profile=profile, override=override),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=merged,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def rendered() -> dict:
    """Full rendered Compose model (search profile enabled, all services)."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = _compose("config", "--format", "json", profile="search")
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


@pytest.fixture(scope="module")
def settings() -> dict:
    """The committed SearXNG settings, parsed once."""
    assert SETTINGS_FILE.is_file(), SETTINGS_FILE
    return yaml.safe_load(SETTINGS_FILE.read_text())


# ---------------------------------------------------------------------------
# Static topology lane
# ---------------------------------------------------------------------------


def test_search_pair_is_pinned_and_profile_gated(
    rendered: dict, rendered_default: dict
) -> None:
    searxng = rendered["services"]["searxng"]
    valkey = rendered["services"]["valkey"]
    # Pinned, never a floating tag: full date+git-sha for searxng (the pin
    # equals the docs-verified upstream snapshot), minor-line pin for valkey.
    assert re.fullmatch(
        r"docker\.io/searxng/searxng:\d{4}\.\d+\.\d+-[0-9a-f]{9}", searxng["image"]
    ), searxng["image"]
    assert valkey["image"] == "docker.io/valkey/valkey:9-alpine"
    # Opt-in like mai-worker: starting search is an explicit stack-owner
    # decision (engine queries egress the host); the default start set is
    # egress-free.
    assert searxng.get("profiles") == ["search"]
    assert valkey.get("profiles") == ["search"]
    assert "searxng" not in rendered_default["services"]
    assert "valkey" not in rendered_default["services"]


def test_search_pair_is_internal_only_and_publishes_nothing(
    rendered: dict, rendered_default: dict
) -> None:
    for name in ("searxng", "valkey"):
        svc = rendered["services"][name]
        assert not svc.get("ports"), name
        assert set(svc["networks"]) == {"mai"}, name
    # The default-set guarantee from 01-compose keeps holding, now that the
    # topology ships mai-proxy (M-1): the ONLY publisher is mai-proxy itself
    # on the loopback interface; every other default service stays internal.
    for name, svc in rendered_default["services"].items():
        if name == "mai-proxy":
            ports = svc["ports"]
            assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1", name
            continue
        assert not svc.get("ports"), name


def test_searxng_requires_valkey_and_reports_real_readiness(rendered: dict) -> None:
    searxng = rendered["services"]["searxng"]
    valkey = rendered["services"]["valkey"]
    assert searxng["depends_on"]["valkey"]["condition"] == "service_healthy"
    check = " ".join(searxng["healthcheck"]["test"])
    assert "/healthz" in check
    # Readiness includes the limiter-state valkey, not just app liveness, so
    # auxiliary-service failure is observable through the compose health state.
    assert "valkey.Valkey.from_url" in check and ".ping()" in check
    assert " ".join(valkey["healthcheck"]["test"]) == "CMD valkey-cli ping"


def test_committed_settings_mount_is_the_single_config_source(rendered: dict) -> None:
    searxng = rendered["services"]["searxng"]
    mounts = {m["target"]: m for m in searxng.get("volumes", [])}
    assert set(mounts) == {"/var/cache/searxng"}
    assert mounts["/var/cache/searxng"]["type"] == "volume"
    assert mounts["/var/cache/searxng"]["source"] == "searxng-cache"
    cfg = {c["target"]: c for c in searxng.get("configs", [])}
    assert set(cfg) == {"/etc/searxng/settings.yml"}
    assert cfg["/etc/searxng/settings.yml"]["source"] == "searxng-settings"
    top = rendered["configs"]["searxng-settings"]
    assert (REPO_ROOT / top["file"]).resolve() == SETTINGS_FILE.resolve()


def test_settings_enable_json_and_document_limiter_choice_without_secrets(
    settings: dict,
) -> None:
    # C07 rule 7: formats MUST include json (a non-enabled format returns 403).
    assert "json" in settings["search"]["formats"]
    assert "html" in settings["search"]["formats"]
    # limiter is deliberately OFF: upstream's hard-coded ip_limit window caps
    # json requests at 4/hour/client-IP, which would throttle MAI's sole API
    # consumer (the backend, one container IP) to ~4 searches/hour. The
    # valkey pair stays provisioned and health-wired for a future
    # limiter/public switch. (Proven live by the rate-cap control tests.)
    assert settings["server"]["limiter"] is False
    assert settings["valkey"]["url"].startswith("redis://valkey:6379/")
    text = SETTINGS_FILE.read_text()
    # No secret material anywhere in the committed file: no secret_key SETTING
    # (comments may name the key; nothing assigns it), no upstream placeholder,
    # no fixture/test secret leaked in.
    assert "secret_key" not in settings.get("server", {})
    assert not re.search(r"^\s*secret_key\s*:", text, re.M)
    assert "ultrasecret" not in text
    assert FIXTURE_SECRET not in text


def test_missing_search_secret_renders_empty_and_refuses_to_start() -> None:
    """Negative control: no default secret anywhere; startup fails closed.

    Unlike MAI_POSTGRES_PASSWORD (render-time `:?`, global interpolation),
    the search profile must not break plain `docker compose config` for
    consumers that never start it — so the variable renders empty and the
    container entrypoint guard (live lane) is what stops the service.
    """
    if not _docker_available():
        pytest.skip("docker CLI not available")
    env = {k: v for k, v in os.environ.items() if k != "SEARXNG_SECRET"}
    env["MAI_POSTGRES_PASSWORD"] = FIXTURE_PASSWORD
    # Empty override outranks the repo .env (present since M-1) so this
    # negative control stays deterministic.
    env["SEARXNG_SECRET"] = ""
    proc = subprocess.run(
        _compose_args("config", "--format", "json", profile="search"),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    model = json.loads(proc.stdout)
    # … but nothing invents a secret: the value is the empty default.
    assert model["services"]["searxng"]["environment"]["SEARXNG_SECRET"] == ""
    # The startup guard is part of the topology definition.
    entrypoint = " ".join(model["services"]["searxng"]["entrypoint"])
    assert "SEARXNG_SECRET must be set in the Compose .env" in entrypoint


def test_search_secret_comes_from_env_only(rendered: dict) -> None:
    searxng = rendered["services"]["searxng"]
    value = searxng["environment"]["SEARXNG_SECRET"]
    # Interpolation resolves to the .env-provided value at render time …
    assert value == FIXTURE_SECRET
    # … and the raw compose file carries no literal secret, only the
    # empty-default reference plus the startup guard (previous test).
    raw = COMPOSE_FILE.read_text()
    assert "${SEARXNG_SECRET:-}" in raw
    assert FIXTURE_SECRET not in raw


def test_search_pair_has_no_privilege_or_socket_grants(rendered: dict) -> None:
    for name in ("searxng", "valkey"):
        svc = rendered["services"][name]
        assert not svc.get("privileged"), name
        assert not svc.get("cap_add"), name
        assert "pid" not in svc, name
        for mount in svc.get("volumes", []):
            assert "docker.sock" not in str(mount.get("source", "")), name


# ---------------------------------------------------------------------------
# Live lane (opt-in: MAI_COMPOSE_LIVE=1) — real pinned images, real entrypoints.
# ---------------------------------------------------------------------------


def _live_enabled() -> bool:
    return os.environ.get("MAI_COMPOSE_LIVE") == "1"


def _json_rows(stdout: str) -> list[dict]:
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


def _http(network: str, url: str, timeout: int = 60) -> tuple[int, str]:
    """GET from a throwaway curl container attached to a compose network.

    Sends the headers upstream's default-active bot detection requires on a
    limiter-enabled instance (descriptive non-bot User-Agent, Accept with
    text/html, Accept-Encoding gzip, Accept-Language) — default library UAs
    (curl/wget/python-requests) are answered 429 by design.
    """
    proc = subprocess.run(
        [
            "docker", "run", "--rm", f"--network={network}",
            "curlimages/curl:latest", "-sS", "-m", "60",
            "-H", "User-Agent: mai-fxt-07-searxng-test/1.0 (internal smoke client)",
            "-H", "Accept: text/html,application/json;q=0.9,*/*;q=0.8",
            "-H", "Accept-Encoding: gzip",
            "-H", "Accept-Language: en",
            "-w", "\n%{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    body, _, status = proc.stdout.rpartition("\n")
    if not status.strip().isdigit() or status.strip() == "000":
        raise AssertionError(
            f"curl failed (rc={proc.returncode}): stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return int(status.strip()), body


def _search_url(host: str, q: str, extra: str = "") -> str:
    return f"http://{host}:8080/search?q={q}&format=json{extra}"


def _http_loop(network: str, url: str, count: int, timeout: int = 300) -> list[int]:
    """N sequential GETs from ONE throwaway curl container (one client IP).

    Keeps the source IP constant so upstream per-IP windows are measured
    honestly; sends the same bot-detection-satisfying headers as _http.
    """
    loop = (
        f"for i in $(seq 1 {count}); do "
        "curl -sS -m 60 -o /dev/null -w '%{http_code} ' "
        "-H 'User-Agent: mai-fxt-07-searxng-test/1.0 (internal smoke client)' "
        "-H 'Accept: text/html,application/json;q=0.9,*/*;q=0.8' "
        "-H 'Accept-Encoding: gzip' "
        "-H 'Accept-Language: en' "
        f"'{url}'; sleep 1; done"
    )
    proc = subprocess.run(
        [
            "docker", "run", "--rm", f"--network={network}",
            "curlimages/curl:latest", "sh", "-c", loop,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    return [int(s) for s in proc.stdout.split()]


def _has_content(payload: dict) -> bool:
    """A search answer with organic results, infoboxes or answers counts as
    content — some engines answer certain queries with only an infobox."""
    return bool(payload["results"] or payload["infoboxes"] or payload["answers"])


class Stack:
    def __init__(self, project: str, override: Path | None = None) -> None:
        self.project = project
        self.override = override

    def run(self, *extra: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return _compose(
            *extra, project=self.project, profile="search",
            override=self.override, timeout=timeout,
        )

    def exec(self, service: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "-T", service, *args, timeout=timeout)

    def ps(self, all: bool = False) -> dict[str, dict]:
        extra = ["--all"] if all else []
        proc = self.run("ps", "--format", "json", *extra)
        assert proc.returncode == 0, proc.stderr
        rows = _json_rows(proc.stdout)
        return {row["Service"]: row for row in rows}

    def wait_health(self, service: str, want: str, deadline_s: int = 150) -> dict:
        end = time.monotonic() + deadline_s
        last: dict = {}
        while time.monotonic() < end:
            last = self.ps(all=True).get(service, {})
            if last.get("Health") == want:
                return last
            time.sleep(2)
        raise AssertionError(f"{service} never became {want}: {last}")

    def wait_healthy(self, service: str, deadline_s: int = 240) -> dict:
        return self.wait_health(service, "healthy", deadline_s)

    def inspect(self, service: str) -> dict:
        rows = self.ps(all=True)
        name = rows[service]["Name"]
        inspect = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, timeout=60
        )
        assert inspect.returncode == 0, inspect.stderr
        return json.loads(inspect.stdout)[0]


DEAD_SETTINGS = """\
# Fixture-only settings (never committed): mirrors deploy/searxng/settings.yml
# but points the bing engine at RFC 5737 TEST-NET-1 (guaranteed unroutable;
# bing is disabled by default upstream and officially supports base_url
# overrides), proving SearXNG surfaces an unreachable engine explicitly.
use_default_settings: true

general:
  instance_name: "MAI internal search"

search:
  formats:
    - html
    - json

server:
  limiter: true

valkey:
  url: "redis://valkey:6379/0"

engines:
  - name: bing
    base_url: "http://192.0.2.1"
    disabled: false
"""

NOJSON_SETTINGS = """\
# Fixture-only settings (never committed): json format deliberately NOT
# enabled, proving the documented upstream boundary that requesting a
# non-enabled format returns HTTP 403 (C07 rule 7).
use_default_settings: true

general:
  instance_name: "MAI internal search"

search:
  formats:
    - html

server:
  limiter: false

valkey:
  url: "redis://valkey:6379/0"
"""

LIMITED_SETTINGS = """\
# Fixture-only settings (never committed): identical to the committed
# deploy/searxng/settings.yml EXCEPT limiter: true. Paired with the main
# stack it measures upstream's hard-coded ip_limit API window
# (API_MAX=4 json requests per API_WINDOW=3600s per client IP,
# searx/botdetection/ip_limit.py) — the reason the committed config keeps
# limiter off for MAI's internal JSON consumer.
use_default_settings: true

general:
  instance_name: "MAI internal search"

search:
  formats:
    - html
    - json

server:
  limiter: true

valkey:
  url: "redis://valkey:6379/0"
"""


class _OverrideList(list):
    """List dumped with the compose ``!override`` merge tag."""


def _override_representer(dumper: yaml.SafeDumper, data: list) -> yaml.Node:
    return dumper.represent_sequence("!override", data)


yaml.SafeDumper.add_representer(_OverrideList, _override_representer)


@pytest.fixture(scope="module")
def fixture_overrides() -> dict[str, Path]:
    """Throwaway compose overrides for the negative-control variants.

    The variant settings are embedded via the compose `configs: content:`
    form, so the docker daemon never bind-mounts a host file (the daemon runs
    with a private /tmp on this host and cannot see it). Only these two small
    override files touch disk, under the C10 fixture namespace. Each override
    introduces a NEW config name and ``!override``-replaces the service's
    configs list — merging `content` onto the base entry's `file` is rejected
    by compose ("attributes are mutually exclusive").
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    overrides = {}
    for name, settings in (
        ("dead", DEAD_SETTINGS),
        ("nojson", NOJSON_SETTINGS),
        ("limited", LIMITED_SETTINGS),
    ):
        path = FIXTURE_DIR / f"{name}-override.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "searxng": {
                            "configs": _OverrideList(
                                [
                                    {
                                        "source": f"searxng-settings-{name}",
                                        "target": "/etc/searxng/settings.yml",
                                    }
                                ]
                            )
                        }
                    },
                    "configs": {
                        f"searxng-settings-{name}": {"content": settings},
                    },
                },
                default_flow_style=False,
            )
        )
        overrides[name] = path
    return overrides


@pytest.fixture(scope="module")
def stack() -> Stack:
    if not _docker_available():
        pytest.skip("docker CLI not available")
    assert _live_enabled(), "live lane requires MAI_COMPOSE_LIVE=1"
    s = Stack(PROJECT)
    up = s.run("up", "-d", "--wait", "--wait-timeout", "300", "searxng", "valkey")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        yield s
    finally:
        s.run("down", "-v", "--remove-orphans", "--timeout", "10", timeout=300)
        shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
        _assert_no_leftovers(PROJECT)


def _assert_no_leftovers(project: str) -> None:
    # Label-scoped to the exact compose project: sibling fixture projects
    # share the mai-fxt-07-searxng name prefix and must not match.
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


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_search_pair_runs_healthy(stack: Stack) -> None:
    rows = stack.ps()
    assert rows["searxng"]["Health"] == "healthy"
    assert rows["valkey"]["Health"] == "healthy"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_stack_refuses_to_start_without_secret() -> None:
    # Live half of the fail-closed negative control: with no SEARXNG_SECRET in
    # the environment the searxng container must exit immediately with the
    # explicit guard message — never launch with an empty/placeholder secret.
    env = {k: v for k, v in os.environ.items() if k != "SEARXNG_SECRET"}
    env["MAI_POSTGRES_PASSWORD"] = FIXTURE_PASSWORD

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            _compose_args(*extra, project=NOSECRET_PROJECT, profile="search"),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=300,
        )

    run("up", "-d", "--no-deps", "searxng")
    try:
        state = ""
        row: dict = {}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ps = run("ps", "--all", "--format", "json")
            assert ps.returncode == 0, ps.stderr
            row = next(
                (r for r in _json_rows(ps.stdout) if r["Service"] == "searxng"), {}
            )
            state = row.get("State", "")
            if state == "exited":
                break
            time.sleep(2)
        assert state == "exited", f"searxng never exited: {row}"
        logs = run("logs", "searxng")
        assert "SEARXNG_SECRET must be set in the Compose .env" in logs.stdout + logs.stderr
    finally:
        down = run("down", "-v", "--remove-orphans", "--timeout", "10")
        assert down.returncode == 0, down.stderr
        _assert_no_leftovers(NOSECRET_PROJECT)


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_no_live_container_publishes_a_host_port(stack: Stack) -> None:
    for svc in ("searxng", "valkey"):
        info = stack.inspect(svc)
        bindings = info["HostConfig"].get("PortBindings") or {}
        assert bindings == {}, f"{svc} published {bindings}"


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_json_query_returns_structured_results(stack: Stack) -> None:
    # KB-S1: actual Compose SearXNG query returns structured JSON from an
    # enabled upstream engine (wikipedia/duckduckgo are default-on, keyless).
    status, body = _http(
        f"{PROJECT}_mai",
        _search_url("searxng", "searxng", "&engines=wikipedia,duckduckgo"),
    )
    assert status == 200, body[:500]
    payload = json.loads(body)
    assert payload["query"] == "searxng"
    results = payload["results"]
    assert isinstance(results, list) and results, body[:500]
    for hit in results[:3]:
        assert hit.get("title") and hit.get("url"), hit
    # Engine health is explicit in the payload, never silent.
    assert isinstance(payload.get("unresponsive_engines"), list)


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_config_introspection_reflects_committed_settings(stack: Stack) -> None:
    status, body = _http(f"{PROJECT}_mai", "http://searxng:8080/config")
    assert status == 200, body[:500]
    payload = json.loads(body)
    # Top-level introspection of the running instance: the committed config
    # is what actually loaded (name and limiter state both come from it).
    assert payload["instance_name"] == "MAI internal search"
    assert payload["limiter"]["enabled"] is False


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_secret_key_absent_from_settings_and_present_via_env(stack: Stack) -> None:
    grep = stack.exec(
        "searxng", "sh", "-c",
        "grep -cE '^[[:space:]]*secret_key[[:space:]]*:' /etc/searxng/settings.yml; "
        "printenv SEARXNG_SECRET",
    )
    assert grep.returncode == 0, grep.stderr
    no_key, via_env = grep.stdout.strip().splitlines()
    assert no_key == "0", "committed settings must carry no secret_key setting"
    assert via_env == FIXTURE_SECRET, "SEARXNG_SECRET env override must feed the container"
    # The override actually feeds the app settings (not just the environment):
    # the loaded server.secret_key must be the injected value.
    loaded = stack.exec(
        "searxng", "sh", "-c",
        "cd /usr/local/searxng && .venv/bin/python -c "
        "\"from searx import settings; print(settings['server']['secret_key'])\"",
    )
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == FIXTURE_SECRET


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_restart_keeps_config_and_search_working(stack: Stack) -> None:
    before = _http(f"{PROJECT}_mai", "http://searxng:8080/config")
    restarted = stack.run("restart", "--timeout", "30", "searxng", timeout=180)
    assert restarted.returncode == 0, restarted.stderr
    stack.wait_healthy("searxng")
    after = _http(f"{PROJECT}_mai", "http://searxng:8080/config")
    assert after[0] == 200 and after[1] == before[1], "config must survive restart"
    # A fresh curl container reconnects right after the restart; allow a few
    # transient connection refusals before judging the search broken.
    last: tuple[int, str] = (0, "")
    for _attempt in range(4):
        try:
            last = _http(
                f"{PROJECT}_mai",
                _search_url("searxng", "wikipedia", "&engines=wikipedia"),
            )
        except AssertionError:
            time.sleep(3)
            continue
        if last[0] == 200:
            break
        time.sleep(3)
    assert last[0] == 200 and _has_content(json.loads(last[1])), last[1][:500]


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_valkey_failure_is_observable_and_recovers(stack: Stack) -> None:
    # AC2: auxiliary-service failure is observable — the readiness healthcheck
    # (app alive AND limiter-state valkey pingable) flips searxng unhealthy.
    stopped = stack.run("stop", "valkey", timeout=120)
    assert stopped.returncode == 0, stopped.stderr
    try:
        stack.wait_health("searxng", "unhealthy")
    finally:
        started = stack.run("start", "valkey", timeout=120)
        assert started.returncode == 0, started.stderr
    stack.wait_healthy("valkey")
    stack.wait_healthy("searxng")
    status, body = _http(
        f"{PROJECT}_mai", _search_url("searxng", "wikipedia", "&engines=wikipedia")
    )
    assert status == 200 and _has_content(json.loads(body)), body[:500]


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_unreachable_engine_error_is_explicit(
    stack: Stack, fixture_overrides: dict[str, Path]
) -> None:
    # Real-boundary negative: the fixture instance's duckduckgo engine points
    # at RFC 5737 TEST-NET-1. The query still answers HTTP 200 JSON, results
    # are empty, and the failure is named in unresponsive_engines.
    dead = Stack(DEAD_PROJECT, override=fixture_overrides["dead"])
    up = dead.run("up", "-d", "--wait", "--wait-timeout", "300", "searxng", "valkey")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        status, body = _http(
            f"{DEAD_PROJECT}_mai", _search_url("searxng", "test", "&engines=bing")
        )
        assert status == 200, body[:500]
        payload = json.loads(body)
        assert payload["results"] == [], payload
        assert any("bing" in e for e in payload["unresponsive_engines"]), payload
        # The instance itself is healthy: another enabled engine still answers.
        ok_status, ok_body = _http(
            f"{DEAD_PROJECT}_mai",
            _search_url("searxng", "wikipedia", "&engines=wikipedia"),
        )
        assert ok_status == 200 and _has_content(json.loads(ok_body)), ok_body[:500]
    finally:
        dead.run("down", "-v", "--remove-orphans", "--timeout", "10", timeout=300)
        _assert_no_leftovers(DEAD_PROJECT)


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_json_format_not_enabled_returns_403(
    stack: Stack, fixture_overrides: dict[str, Path]
) -> None:
    # Documented upstream boundary (C07 rule 7): requesting a format that is
    # not enabled in settings returns 403, never silent degradation.
    nojson = Stack(NOJSON_PROJECT, override=fixture_overrides["nojson"])
    up = nojson.run("up", "-d", "--wait", "--wait-timeout", "300", "searxng", "valkey")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        status, body = _http(
            f"{NOJSON_PROJECT}_mai", _search_url("searxng", "test")
        )
        assert status == 403, f"expected 403, got {status}: {body[:300]}"
        # The html format the fixture enables still serves.
        html_status, html_body = _http(
            f"{NOJSON_PROJECT}_mai", "http://searxng:8080/search?q=test"
        )
        assert html_status == 200 and "<" in html_body, html_body[:300]
    finally:
        nojson.run("down", "-v", "--remove-orphans", "--timeout", "10", timeout=300)
        _assert_no_leftovers(NOJSON_PROJECT)


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_committed_config_has_no_json_rate_cap(stack: Stack) -> None:
    # Paired control (main stack, limiter off): six JSON queries in a row
    # from ONE client IP must all answer 200 — the committed configuration
    # does not throttle its sole API consumer.
    url = _search_url("searxng", "searxng", "&engines=wikipedia")
    assert _http_loop(f"{PROJECT}_mai", url, 6) == [200] * 6


@pytest.mark.skipif(not _live_enabled(), reason="opt-in live lane: MAI_COMPOSE_LIVE=1")
def test_limiter_enabled_instance_caps_json_requests(
    stack: Stack, fixture_overrides: dict[str, Path]
) -> None:
    # Measured proof of WHY the committed settings keep limiter off: with
    # limiter true, upstream's hard-coded ip_limit API window
    # (API_MAX=4 per API_WINDOW=3600s per client IP) trips at the 5th JSON
    # request from one IP. The fixture is identical to the committed
    # settings except limiter: true.
    limited = Stack(LIMITED_PROJECT, override=fixture_overrides["limited"])
    up = limited.run("up", "-d", "--wait", "--wait-timeout", "300", "searxng", "valkey")
    assert up.returncode == 0, up.stderr[-4000:]
    try:
        url = _search_url("searxng", "searxng", "&engines=wikipedia")
        statuses = _http_loop(f"{LIMITED_PROJECT}_mai", url, 5)
        assert statuses[:4] == [200] * 4, statuses
        assert statuses[4] == 429, statuses
    finally:
        limited.run("down", "-v", "--remove-orphans", "--timeout", "10", timeout=300)
        _assert_no_leftovers(LIMITED_PROJECT)
