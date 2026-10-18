"""Focused tests for the deterministic update-check collector (card 10-update-check).

C09 + C10 acceptance coverage:

- **Acceptance 1** — the installed pin of every MAI-managed application
  (the pinned ``image:`` line of the frozen planner layout on the target)
  is compared against the upstream source the pin itself declares (OCI
  Distribution API v2 on the reference's registry host), with provenance
  recorded in bounded observation rows and at most
  ``MAX_REGISTRY_CALLS_PER_APP`` byte-capped, timeout-bounded,
  credential-free GET requests per application.
- **Acceptance 2** — a check is a NOTICE, never a deployment: the probe
  command is a frozen read-only program (forbidden-verb audited, and proven
  read-only on the real fixture by an unchanged-tree hash) and the module's
  only write paths are the 0007 observation tables. Ambiguous or
  unavailable upstream answers are honest unknowns (metric-free
  ``quality='unknown'`` rows; findings untouched) — never "no updates".

Test layers (the 10-system shape): pure parse/resolution, mock-transport
upstream boundaries (protocol shapes, budgets, call caps), real-Postgres
persistence on the real Alembic chain (``MAI_TEST_DATABASE_URL``), and
``real_target`` tests against the disposable Arch sshd fixture over the
real transport plus a real loopback HTTP registry. Secret hygiene: fixture
keys are throwaway files under ``/tmp/mai-fxt-10-update-check``, destroyed
by the fixture teardown with proof; no credential material enters storage
or evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.contracts.observations import (
    ObservationFreshness,
    ObservationQuality,
    derive_freshness,
)
from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    pin_host_key,
    resolve_identity_key,
    scan_host_keys,
)
from mai.monitoring.updates import (
    FRESH_HORIZON,
    MAX_APPS_LISTED,
    MAX_PROBE_OUTPUT_BYTES,
    MAX_REGISTRY_CALLS_PER_APP,
    MAX_TAGS_BODY_BYTES,
    PROBE_COMMAND,
    RETENTION_HORIZON,
    SOURCE,
    UPSTREAM_TIMEOUT_SECONDS,
    UPSTREAM_USER_AGENT,
    AppCheck,
    AppPin,
    OsUpdates,
    UpdateSample,
    check_application_pin,
    collect,
    parse_image_pin,
    parse_probe_output,
    probe_updates,
    record_sample,
    record_unavailable,
    registry_endpoint,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "8f2d4e5c-6a7b-4c80-9d0e-1f2a3b4c5d6e"
SERVER_B = "9e3c5d6f-7b8c-4d91-0e1f-2a3b4c5d6e7f"

APP_1 = uuid.UUID("a1b2c3d4-0000-4111-8111-000000000001")
APP_2 = uuid.UUID("a1b2c3d4-0000-4222-8222-000000000002")

NOW = datetime(2026, 10, 17, 22, 0, 0, tzinfo=UTC)

PIN_1 = f"127.0.0.1:59999/fixture-app@sha256:{'c' * 64}"
PIN_2 = f"127.0.0.1:59999/other-app@sha256:{'b' * 64}"
DIGEST_UPSTREAM = f"sha256:{'b' * 64}"
DIGEST_INSTALLED_1 = f"sha256:{'c' * 64}"

# Real probe output captured verbatim from the disposable Arch fixture
# target (2026-10-17, mai-fxt-10-update-check-arch through the pinned-identity
# transport): the build-time sync db leaves two real upgradables (sqlite,
# zlib), two managed pins under /var/lib/mai/apps, and one NON-UUID directory
# that the frozen managed-root scheme must drop.
_ARCH_FIXTURE_OUTPUT = (
    "ts=2026-10-17T22:20:38\n"
    "os_pacman_status=ok\n"
    "os_pacman_count=2\n"
    "os_pacman=sqlite\n"
    "os_pacman=zlib\n"
    "os_apt_status=absent\n"
    "app=a1b2c3d4-0000-4111-8111-000000000001"
    f" image=127.0.0.1:59999/fixture-app@sha256:{'c' * 64}\n"
    "app=a1b2c3d4-0000-4222-8222-000000000002"
    f" image=127.0.0.1:59999/other-app@sha256:{'b' * 64}\n"
    f"app=not-managed-dir image=127.0.0.1:59999/junk-app@sha256:{'d' * 64}\n"
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip (same reconciliation list as
        # 10-observation-schema): order-independent zero-to-head fixture.
        conn.execute(
            "DROP TABLE IF EXISTS notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, "
            "job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0007``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the persistence claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    async with factory() as session:
        await session.execute(text("DELETE FROM notification_alerts"))
        await session.execute(text("DELETE FROM observation_findings"))
        await session.execute(text("DELETE FROM server_observations"))
        await session.commit()
    await engine.dispose()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _storage_dump(sessions) -> str:
    """Every stored value of the observation domain, for sentinel audits."""
    parts = []
    for table in ("server_observations", "observation_findings", "notification_alerts"):
        rows = await _fetchall(sessions, f"SELECT * FROM {table}")  # noqa: S608 (fixed names)
        parts.extend(str(tuple(r)) for r in rows)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sample builders (pure layer)
# ---------------------------------------------------------------------------


def _os(count: int = 2, source: str = "pacman", names: tuple[str, ...] | None = None):
    return OsUpdates(
        source=source,  # type: ignore[arg-type]
        count=count,
        names=names if names is not None else ("sqlite", "zlib"),
    )


def _pin_1(ref: str = PIN_1) -> AppPin:
    return AppPin(APP_1, ref)


def _pin_2(ref: str = PIN_2) -> AppPin:
    return AppPin(APP_2, ref)


def _check(app: uuid.UUID, status: str, **kw: object) -> AppCheck:
    return AppCheck(
        application_id=app,
        status=status,
        repository=kw.get("repository", "fixture-app"),
        registry_host=kw.get("registry_host", "127.0.0.1:59999"),
        endpoint=kw.get("endpoint", "http://127.0.0.1:59999"),
        upstream_tag=kw.get("upstream_tag"),
        upstream_digest=kw.get("upstream_digest"),
        detail=str(kw.get("detail", "")),
    )


def _sample(**overrides: object) -> UpdateSample:
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.MEASURED,
        "os_updates": _os(),
        "pins": (_pin_1(), _pin_2()),
        "checks": (
            _check(
                APP_1,
                "update_available",
                upstream_tag="v2.0.0",
                upstream_digest=DIGEST_UPSTREAM,
            ),
            _check(
                APP_2,
                "up_to_date",
                repository="other-app",
                upstream_tag="v2.0.0",
                upstream_digest=DIGEST_UPSTREAM,
            ),
        ),
    }
    fields.update(overrides)
    return UpdateSample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded probe output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_fixture_capture_parses_measured(self) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        # Target-reported timestamp, controller clock untouched.
        assert sample.observed_at == datetime(2026, 10, 17, 22, 20, 38, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        # The REAL fixture case: pacman answered (rc=0) with two upgradables.
        assert sample.os_updates is not None
        assert sample.os_updates.source == "pacman"
        assert sample.os_updates.count == 2
        assert sample.os_updates.names == ("sqlite", "zlib")
        # Two managed pins kept, the NON-UUID directory dropped.
        assert [pin.application_id for pin in sample.pins] == [APP_1, APP_2]
        assert sample.pins[0].image_ref.endswith(f"@sha256:{'c' * 64}")

    def test_empty_output_is_unknown_without_any_fact(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.os_updates is None
        assert sample.pins == ()
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"

    def test_count_line_is_authoritative_over_the_bounded_listing(self) -> None:
        output = (
            "os_pacman_status=ok\n"
            "os_pacman_count=500\n"
            "os_pacman=one\n"
            "os_pacman=two\n"  # listing capped by the probe's head -n
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.os_updates is not None
        assert sample.os_updates.count == 500  # exact target-side count
        assert sample.os_updates.names == ("one", "two")  # bounded provenance

    def test_failed_source_is_unknown_never_zero(self) -> None:
        output = "os_pacman_status=error_rc_1\nos_pacman_count=0\n"
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.os_updates is None
        assert sample.quality is ObservationQuality.UNKNOWN

    def test_absent_sources_are_unknown(self) -> None:
        output = "os_pacman_status=absent\nos_apt_status=absent\n"
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.os_updates is None

    def test_apt_source_used_when_pacman_absent(self) -> None:
        output = (
            "os_pacman_status=absent\nos_apt_status=ok\nos_apt_count=1\nos_apt=libc6\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.os_updates is not None
        assert sample.os_updates.source == "apt"
        assert sample.os_updates.names == ("libc6",)

    def test_pacman_takes_precedence_over_apt(self) -> None:
        output = (
            "os_pacman_status=ok\nos_pacman_count=1\nos_pacman=a\n"
            "os_apt_status=ok\nos_apt_count=7\nos_apt=b\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.os_updates is not None
        assert sample.os_updates.source == "pacman"
        assert sample.os_updates.count == 1

    def test_pins_dedupe_and_first_image_line_wins(self) -> None:
        output = (
            f"app={APP_1} image=first@sha256:{'a' * 64}\n"
            f"app={APP_1} image=second@sha256:{'b' * 64}\n"
            f"app={APP_2} image=only@sha256:{'c' * 64}\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert [pin.image_ref.split("@")[0] for pin in sample.pins] == [
            "first",
            "only",
        ]

    def test_non_uuid_and_junk_dirs_are_dropped(self) -> None:
        output = (
            "app=not-managed-dir image=x@sha256:" + "d" * 64 + "\n"
            f"app={APP_1} image=y@sha256:{'e' * 64}\n"
            "app=../etc image=escape@sha256:" + "f" * 64 + "\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert [pin.application_id for pin in sample.pins] == [APP_1]

    def test_app_cap_is_enforced_at_the_parser_too(self) -> None:
        lines = [
            f"app={uuid.UUID(int=index)} image=x@sha256:{'a' * 64}\n"
            for index in range(MAX_APPS_LISTED + 4)
        ]
        sample = parse_probe_output("".join(lines), controller_now=NOW)
        assert len(sample.pins) == MAX_APPS_LISTED

    def test_quality_ladder(self) -> None:
        measured = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert measured.quality is ObservationQuality.MEASURED
        pins_only = parse_probe_output(
            f"app={APP_1} image=x@sha256:{'a' * 64}\n", controller_now=NOW
        )
        assert pins_only.quality is ObservationQuality.DEGRADED
        nothing = parse_probe_output("os_pacman_status=absent\n", controller_now=NOW)
        assert nothing.quality is ObservationQuality.UNKNOWN

    def test_bad_timestamp_falls_back_to_controller_clock(self) -> None:
        sample = parse_probe_output("ts=nonsense\n", controller_now=NOW)
        assert sample.observed_at_source == "controller"
        assert sample.observed_at == NOW

    def test_junk_and_keyless_lines_are_ignored(self) -> None:
        sample = parse_probe_output(
            "garbage line without separator\n=novalue\n", controller_now=NOW
        )
        assert sample.os_updates is None
        assert sample.quality is ObservationQuality.UNKNOWN

    def test_ok_marker_without_a_count_line_is_not_trusted(self) -> None:
        # The probe always emits the exact count on the ok path; a missing
        # count is a malformed answer, so the source stays UNKNOWN.
        sample = parse_probe_output("os_pacman_status=ok\n", controller_now=NOW)
        assert sample.os_updates is None


# ---------------------------------------------------------------------------
# Pure pin/endpoint/tag resolution
# ---------------------------------------------------------------------------


class TestImagePinParsing:
    def test_registry_pin_parses_into_declared_upstream(self) -> None:
        pin = parse_image_pin(f"docker.io/library/nginx@sha256:{'a' * 64}")
        assert pin is not None
        assert pin.registry_host == "docker.io"
        assert pin.repository == "library/nginx"
        assert pin.digest == f"sha256:{'a' * 64}"

    def test_port_host_and_nested_repository(self) -> None:
        pin = parse_image_pin(f"127.0.0.1:5000/team/app@sha256:{'b' * 64}")
        assert pin is not None
        assert pin.registry_host == "127.0.0.1:5000"
        assert pin.repository == "team/app"

    def test_unpinned_or_hostless_references_declare_no_source(self) -> None:
        assert parse_image_pin(f"nginx:latest@sha256:{'a' * 64}") is None
        assert parse_image_pin(f"library/nginx@sha256:{'a' * 64}") is None
        assert parse_image_pin("nginx:1.27") is None
        assert parse_image_pin("") is None
        assert parse_image_pin(f"docker.io/nginx@sha256:{'z' * 64}") is None

    def test_registry_endpoint_scheme_rules(self) -> None:
        assert registry_endpoint("docker.io") == "https://registry-1.docker.io"
        assert registry_endpoint("ghcr.io") == "https://ghcr.io"
        assert registry_endpoint("example.com:5000") == "https://example.com:5000"
        # Loopback literals speak plain HTTP (the C10 fixture rule).
        assert registry_endpoint("127.0.0.1:59999") == "http://127.0.0.1:59999"
        assert registry_endpoint("localhost:5000") == "http://localhost:5000"


class TestVersionResolution:
    def test_numeric_ordering_with_v_prefix(self) -> None:
        from mai.monitoring.updates import _newest_version_tag

        assert _newest_version_tag(["v1.0.0", "v1.2.9", "v1.2.10"]) == "v1.2.10"
        assert _newest_version_tag(["2.0", "v1.9.9"]) == "2.0"
        assert _newest_version_tag(["v10"]) == "v10"

    def test_no_version_tags_is_ambiguous(self) -> None:
        from mai.monitoring.updates import _newest_version_tag

        assert _newest_version_tag(["latest", "main", "sha-abc123"]) is None

    def test_version_tie_between_distinct_tags_is_ambiguous(self) -> None:
        from mai.monitoring.updates import _newest_version_tag

        assert _newest_version_tag(["v1.0", "1.0", "v0.9"]) is None

    def test_zero_padding_makes_a_tie_not_a_winner(self) -> None:
        from mai.monitoring.updates import _newest_version_tag

        assert _newest_version_tag(["1.2", "1.2.0"]) is None


# ---------------------------------------------------------------------------
# Upstream resolution over the mock transport: protocol shapes and budgets
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """httpx2 MockTransport wrapper that logs method/path/auth per request."""

    def __init__(self, registry_state: dict[str, object]) -> None:
        import httpx2

        self.requests: list[tuple[str, str, bool]] = []
        self._state = registry_state

        def handler(request: httpx2.Request) -> httpx2.Response:
            path = request.url.path
            has_auth = "authorization" in request.headers
            self.requests.append((request.method, path, has_auth))
            return self._route(request.method, path)

        self.mock = httpx2.MockTransport(handler)

    def _route(self, method: str, path: str):
        import httpx2

        assert method == "GET", f"non-GET request: {method} {path}"
        assert path.startswith("/v2/")
        remainder = path[len("/v2/") :]
        if remainder.endswith("/tags/list"):
            repo = remainder[: -len("/tags/list")]
            state = self._state.get(repo)
            if state is None:
                return httpx2.Response(404, json={"errors": []})
            return httpx2.Response(
                200,
                json={"name": repo, "tags": state["tags"]},  # type: ignore[index]
            )
        if "/manifests/" in remainder:
            repo, _, tag = remainder.partition("/manifests/")
            state = self._state.get(repo)
            if state is None or tag not in state["digests"]:  # type: ignore[index]
                return httpx2.Response(404, json={"errors": []})
            digest = state["digests"][tag]  # type: ignore[index]
            body = json.dumps({"tag": tag}).encode()
            return httpx2.Response(
                200,
                content=body,
                headers={"Docker-Content-Digest": digest},
            )
        return httpx2.Response(404, json={"errors": []})


def _client_for(transport: object):
    import httpx2

    return httpx2.AsyncClient(
        timeout=UPSTREAM_TIMEOUT_SECONDS,
        transport=transport,  # type: ignore[arg-type]
        follow_redirects=False,
        headers={"User-Agent": UPSTREAM_USER_AGENT},
    )


def _registry_state() -> dict[str, object]:
    return {
        "fixture-app": {
            "tags": ["v1.0.0", "v2.0.0"],
            "digests": {
                "v1.0.0": f"sha256:{'1' * 64}",
                "v2.0.0": DIGEST_UPSTREAM,
            },
        },
        "other-app": {
            "tags": ["v2.0.0"],
            "digests": {"v2.0.0": DIGEST_UPSTREAM},
        },
        "ambiguous-app": {
            "tags": ["latest", "main"],
            "digests": {},
        },
        "tie-app": {
            "tags": ["v1.0", "1.0"],
            "digests": {"v1.0": f"sha256:{'1' * 64}", "1.0": f"sha256:{'2' * 64}"},
        },
        "broken-app": None,
    }


class TestUpstreamResolution:
    async def _check(self, transport: object, pin: AppPin) -> AppCheck:
        client = _client_for(transport)
        try:
            return await check_application_pin(client, pin)
        finally:
            await client.aclose()

    @pytest.mark.anyio
    async def test_update_available_when_newest_digest_differs(self) -> None:
        transport = _RecordingTransport(_registry_state())
        check = await self._check(transport.mock, _pin_1())
        assert check.status == "update_available"
        assert check.upstream_tag == "v2.0.0"
        assert check.upstream_digest == DIGEST_UPSTREAM
        assert check.repository == "fixture-app"

    @pytest.mark.anyio
    async def test_up_to_date_when_digests_match(self) -> None:
        transport = _RecordingTransport(_registry_state())
        check = await self._check(transport.mock, _pin_2())
        assert check.status == "up_to_date"
        assert check.upstream_digest == DIGEST_UPSTREAM

    @pytest.mark.anyio
    async def test_no_version_tags_is_ambiguous_and_stops_after_one_call(self) -> None:
        transport = _RecordingTransport(_registry_state())
        pin = AppPin(APP_1, f"127.0.0.1:59999/ambiguous-app@sha256:{'c' * 64}")
        check = await self._check(transport.mock, pin)
        assert check.status == "ambiguous"
        assert len(transport.requests) == 1  # no manifest call without a tag

    @pytest.mark.anyio
    async def test_version_tie_is_ambiguous(self) -> None:
        transport = _RecordingTransport(_registry_state())
        pin = AppPin(APP_1, f"127.0.0.1:59999/tie-app@sha256:{'c' * 64}")
        check = await self._check(transport.mock, pin)
        assert check.status == "ambiguous"

    @pytest.mark.anyio
    async def test_invalid_pin_makes_no_network_calls(self) -> None:
        transport = _RecordingTransport({})
        check = await self._check(transport.mock, AppPin(APP_1, "nginx:1.27"))
        assert check.status == "invalid_pin"
        assert transport.requests == []

    @pytest.mark.anyio
    async def test_http_errors_are_unavailable(self) -> None:
        transport = _RecordingTransport(_registry_state())
        missing = AppPin(APP_1, f"127.0.0.1:59999/absent-repo@sha256:{'c' * 64}")
        check = await self._check(transport.mock, missing)
        assert check.status == "unavailable"
        assert check.detail == "tags http_404"

    @pytest.mark.anyio
    async def test_manifest_http_error_is_unavailable(self) -> None:
        state = {
            "half-app": {
                "tags": ["v1.0"],
                "digests": {},  # manifest 404s while tags 200s
            }
        }
        transport = _RecordingTransport(state)
        pin = AppPin(APP_1, f"127.0.0.1:59999/half-app@sha256:{'c' * 64}")
        check = await self._check(transport.mock, pin)
        assert check.status == "unavailable"
        assert check.detail == "manifest http_404"

    @pytest.mark.anyio
    async def test_unparsable_tags_body_is_unavailable(self) -> None:
        import httpx2

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>not json</html>")

        client = _client_for(httpx2.MockTransport(handler))
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        assert check.status == "unavailable"
        assert check.detail == "tags body unparsable"

    @pytest.mark.anyio
    async def test_digest_header_preferred_over_body_hash(self) -> None:
        import httpx2

        body = b'{"tag": "v2.0.0"}'
        header_digest = f"sha256:{'a' * 64}"

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/tags/list"):
                return httpx2.Response(200, json={"name": "x", "tags": ["v2.0.0"]})
            return httpx2.Response(
                200, content=body, headers={"Docker-Content-Digest": header_digest}
            )

        client = _client_for(httpx2.MockTransport(handler))
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        assert check.upstream_digest == header_digest

    @pytest.mark.anyio
    async def test_body_hash_fallback_without_digest_header(self) -> None:
        import httpx2

        body = b"manifest-bytes"

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/tags/list"):
                return httpx2.Response(200, json={"name": "x", "tags": ["v2.0.0"]})
            return httpx2.Response(200, content=body)

        client = _client_for(httpx2.MockTransport(handler))
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        expected = "sha256:" + hashlib.sha256(body).hexdigest()
        assert check.upstream_digest == expected

    @pytest.mark.anyio
    async def test_call_budget_is_two_with_exact_declared_paths(self) -> None:
        transport = _RecordingTransport(_registry_state())
        await self._check(transport.mock, _pin_1())
        assert len(transport.requests) <= MAX_REGISTRY_CALLS_PER_APP
        assert [path for _, path, _ in transport.requests] == [
            "/v2/fixture-app/tags/list",
            "/v2/fixture-app/manifests/v2.0.0",
        ]
        assert all(method == "GET" for method, _, _ in transport.requests)

    @pytest.mark.anyio
    async def test_no_authorization_header_is_ever_sent(self) -> None:
        transport = _RecordingTransport(_registry_state())
        await self._check(transport.mock, _pin_1())
        assert transport.requests
        assert all(not has_auth for _, _, has_auth in transport.requests)

    @pytest.mark.anyio
    async def test_redirects_are_not_followed(self) -> None:
        import httpx2

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                302, headers={"Location": "http://127.0.0.1:1/elsewhere"}
            )

        client = _client_for(httpx2.MockTransport(handler))
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        assert check.status == "unavailable"

    @pytest.mark.anyio
    async def test_oversized_tags_body_is_aborted_at_the_budget(self) -> None:
        import httpx2

        seen_chunks = {"n": 0}
        blob = b"x" * 4096

        def handler(request: httpx2.Request) -> httpx2.Response:
            async def stream() -> object:
                for _ in range((MAX_TAGS_BODY_BYTES // 4096) + 8):
                    seen_chunks["n"] += 1
                    yield blob

            return httpx2.Response(200, content=stream())

        client = _client_for(httpx2.MockTransport(handler))
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        assert check.status == "unavailable"
        assert check.detail == "tags body over budget"
        # The transfer stopped at the budget: at most cap/4096 + 1 chunks.
        assert seen_chunks["n"] <= MAX_TAGS_BODY_BYTES // 4096 + 1


class TestUpstreamTransportFailures:
    async def _check_with(self, handler: object) -> AppCheck:
        import httpx2

        client = _client_for(httpx2.MockTransport(handler))
        try:
            return await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()

    @pytest.mark.anyio
    async def test_timeout_maps_to_timeout_status(self) -> None:
        import httpx2

        def handler(request: object) -> object:
            raise httpx2.ReadTimeout("slow upstream")

        check = await self._check_with(handler)
        assert check.status == "unavailable"
        assert check.detail == "timeout"

    @pytest.mark.anyio
    async def test_transport_error_maps_to_unreachable(self) -> None:
        import httpx2

        def handler(request: object) -> object:
            raise httpx2.ConnectError("refused")

        check = await self._check_with(handler)
        assert check.status == "unavailable"
        assert check.detail == "unreachable"

    @pytest.mark.anyio
    async def test_real_loopback_refusal_is_unreachable(self) -> None:
        """REAL network boundary: a closed loopback port refuses the GET."""
        import httpx2

        client = httpx2.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
        try:
            check = await check_application_pin(client, _pin_1())
        finally:
            await client.aclose()
        assert check.status == "unavailable"
        assert check.detail == "unreachable"


# ---------------------------------------------------------------------------
# Probe transport boundary (no docker): budgets, frozen surface, codes
# ---------------------------------------------------------------------------


class TestProbeTransport:
    def _identity(self) -> SshIdentity:
        return SshIdentity(
            host="203.0.113.10",
            port=2222,
            user="root",
            host_key="SHA256:" + "A" * 43,
            credential_ref="MAI_SSH_KEY_TEST",
        )

    def test_probe_command_is_a_frozen_read_only_program(self) -> None:
        # Forbidden-verb audit over the ONE remote command: the check must
        # not carry any mutating path (acceptance 2: notice, not deployment).
        forbidden = (
            "-Sy",
            "-S ",
            "-R ",
            "--remove",
            "install",
            "docker",
            "pacman -U",
            "apt-get install",
            " apt ",
            "rm ",
            "rmdir",
            "mv ",
            "truncate",
            ">>",
            "tee ",
            "chmod",
            "chown",
            "kill",
            "pkill",
            "systemctl start",
            "systemctl restart",
            "reboot",
            "shutdown",
            "mkfs",
            "dd ",
            "curl",
            "wget",
            "sh -c",
            "bash -c",
        )
        for token in forbidden:
            assert token not in PROBE_COMMAND, token
        # Reading the managed compose files is the probe's PURPOSE; the
        # compose tool must never be invoked.
        assert "compose" not in PROBE_COMMAND.replace("${d}compose.yaml", "")
        # The only redirections are the stdout/stderr discards; nothing
        # writes to the target filesystem or state.
        audited = (
            PROBE_COMMAND.replace(">/dev/null", "")
            .replace("2>/dev/null", "")
            .replace("2>&1", "")
        )
        assert ">" not in audited
        # The only package invocations are the two read-only queries.
        assert "pacman -Quq" in PROBE_COMMAND
        assert "apt-get -s dist-upgrade" in PROBE_COMMAND

    def test_missing_known_hosts_file_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            probe_updates(
                self._identity(),
                key_path=tmp_path / "key",
                known_hosts_path=tmp_path / "absent_known_hosts",
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION

    def test_output_budget_raises_bounded_output_exceeded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text("")
        import mai.monitoring.updates as updates

        def fake(*args: object, **kwargs: object):
            return subprocess.CompletedProcess(
                args, 0, stdout="x" * (MAX_PROBE_OUTPUT_BYTES + 1), stderr=""
            )

        monkeypatch.setattr(updates.subprocess, "run", fake)
        with pytest.raises(SshConnectError) as excinfo:
            probe_updates(
                self._identity(),
                key_path=tmp_path / "key",
                known_hosts_path=known_hosts,
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.BOUNDED_OUTPUT_EXCEEDED

    def test_timeout_maps_to_the_timeout_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text("")
        import mai.monitoring.updates as updates

        def fake(*args: object, **kwargs: object):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

        monkeypatch.setattr(updates.subprocess, "run", fake)
        with pytest.raises(SshConnectError) as excinfo:
            probe_updates(
                self._identity(),
                key_path=tmp_path / "key",
                known_hosts_path=known_hosts,
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.TIMEOUT

    def test_stderr_classification_matches_the_p04_vocabulary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text("")
        import mai.monitoring.updates as updates

        cases = {
            "Host key verification failed.": SshErrorCode.HOST_KEY_MISMATCH,
            "Permission denied (publickey).": SshErrorCode.AUTH_FAILED,
            "Connection refused": SshErrorCode.UNREACHABLE,
        }
        for stderr, expected in cases.items():

            def fake(*args: object, **kwargs: object):
                return subprocess.CompletedProcess(args, 255, stdout="", stderr=stderr)

            monkeypatch.setattr(updates.subprocess, "run", fake)
            with pytest.raises(SshConnectError) as excinfo:
                probe_updates(
                    self._identity(),
                    key_path=tmp_path / "key",
                    known_hosts_path=known_hosts,
                    controller_now=NOW,
                )
            assert excinfo.value.code is expected, stderr


# ---------------------------------------------------------------------------
# Persistence into the 0007 observation schema (real disposable Postgres)
# ---------------------------------------------------------------------------


class TestPersistence:
    @pytest.mark.anyio
    async def test_measured_round_persists_rows_provenance_and_findings(
        self, sessions
    ) -> None:
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        # os.count + os.source + one measured row per resolved app check.
        assert written == 4
        rows = await _fetchall(
            sessions,
            "SELECT metric, value, method, quality FROM server_observations "
            "WHERE server_id = :sid ORDER BY metric, value",
            sid=uuid.UUID(SERVER_A),
        )
        metrics = sorted(row.metric for row in rows)
        assert metrics == [
            "update.app",
            "update.app",
            "update.os.count",
            "update.os.source",
        ]
        lookup = {row.metric: row for row in rows}
        assert lookup["update.os.count"].value == "2"
        assert lookup["update.os.source"].value == "pacman"
        assert all(row.quality == "measured" for row in rows)
        methods = {row.metric: row.method for row in rows}
        assert methods["update.os.count"] == "command"
        # The app rows are DERIVED (target command + upstream network answer)
        # and carry the full bounded provenance.
        app_rows = [row for row in rows if row.metric == "update.app"]
        assert {row.method for row in app_rows} == {"derived"}
        assert any(
            "upstream_tag=v2.0.0" in row.value
            and "upstream_digest=sha256:" in row.value
            and "registry=127.0.0.1:59999" in row.value
            and f"app={APP_1}" in row.value
            for row in app_rows
        )
        assert all(len(row.value) <= 512 for row in app_rows)

        findings = await _fetchall(
            sessions,
            "SELECT kind, dedup_key, severity, summary, resolved_at "
            "FROM observation_findings WHERE server_id = :sid ORDER BY kind, dedup_key",
            sid=uuid.UUID(SERVER_A),
        )
        assert len(findings) == 2
        os_finding = next(f for f in findings if f.kind == "update.os_packages")
        assert os_finding.dedup_key == "os"
        assert os_finding.severity == "info"
        assert os_finding.resolved_at is None
        assert "2 OS package updates pending" in os_finding.summary
        assert "sqlite" in os_finding.summary
        app_finding = next(f for f in findings if f.kind == "update.available")
        assert app_finding.dedup_key == str(APP_1)
        assert app_finding.resolved_at is None
        assert "update available for fixture-app" in app_finding.summary

    @pytest.mark.anyio
    async def test_update_finding_lifecycle_open_clear_reopen(self, sessions) -> None:
        active = _sample()
        await record_sample(sessions, uuid.UUID(SERVER_A), active, controller_now=NOW)
        cleared = replace(
            active,
            checks=(
                _check(
                    APP_1,
                    "up_to_date",
                    upstream_tag="v2.0.0",
                    upstream_digest=DIGEST_UPSTREAM,
                ),
                _check(
                    APP_2,
                    "up_to_date",
                    repository="other-app",
                    upstream_tag="v2.0.0",
                    upstream_digest=DIGEST_UPSTREAM,
                ),
            ),
        )
        await record_sample(sessions, uuid.UUID(SERVER_A), cleared, controller_now=NOW)
        row = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings WHERE kind = "
            "'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert row is not None and row.resolved_at is not None
        reopened = replace(active)
        later = NOW + timedelta(hours=1)
        await record_sample(
            sessions, uuid.UUID(SERVER_A), reopened, controller_now=later
        )
        row = await _fetchone(
            sessions,
            "SELECT resolved_at, updated_at FROM observation_findings WHERE kind = "
            "'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert row.resolved_at is None  # reopened onto one finding, no stacking
        assert row.updated_at == later

    @pytest.mark.anyio
    async def test_unknown_check_never_creates_closes_or_updates_a_finding(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        before = await _fetchone(
            sessions,
            "SELECT summary, updated_at, resolved_at FROM observation_findings "
            "WHERE kind = 'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert before is not None and before.resolved_at is None
        degraded = replace(
            _sample(quality=ObservationQuality.DEGRADED),
            checks=(
                _check(APP_1, "unavailable", detail="tags http_401"),
                _check(
                    APP_2,
                    "up_to_date",
                    repository="other-app",
                    upstream_tag="v2.0.0",
                    upstream_digest=DIGEST_UPSTREAM,
                ),
            ),
        )
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), degraded, controller_now=NOW
        )
        after = await _fetchone(
            sessions,
            "SELECT summary, updated_at, resolved_at FROM observation_findings "
            "WHERE kind = 'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert after.summary == before.summary  # untouched, not closed/rewritten
        assert after.updated_at == before.updated_at
        assert after.resolved_at is None
        # The unknown check persisted as ONE metric-free row.
        unknown_rows = await _fetchall(
            sessions,
            "SELECT metric, value, observed_at FROM server_observations "
            "WHERE quality = 'unknown' AND server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        assert len(unknown_rows) == 1
        assert unknown_rows[0].metric is None
        assert unknown_rows[0].value is None
        assert unknown_rows[0].observed_at is None
        assert written == 4  # os.count, os.source, app2 row, 1 unknown row

    @pytest.mark.anyio
    async def test_unknown_with_a_metric_is_a_real_integrity_violation(
        self, sessions
    ) -> None:
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, "
                        "server_id, source, method, quality, metric, value, "
                        "collected_at, retain_until) VALUES (:oid, :sid, :src, "
                        "'derived', 'unknown', 'update.app', 'x', :now, :retain)"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "src": SOURCE,
                        "now": NOW,
                        "retain": NOW + RETENTION_HORIZON,
                    },
                )
            await session.rollback()

    @pytest.mark.anyio
    async def test_target_unavailable_persists_one_metric_free_row(
        self, sessions
    ) -> None:
        assert (
            await record_unavailable(sessions, uuid.UUID(SERVER_A), controller_now=NOW)
            == 1
        )
        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at, fresh_until, collected_at, "
            "retain_until FROM server_observations",
        )
        assert row is not None
        assert row.metric is None and row.value is None
        assert row.observed_at is None and row.fresh_until is None
        assert row.collected_at == NOW
        assert row.retain_until == NOW + RETENTION_HORIZON
        # Findings from earlier rounds are deliberately untouched.
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        await record_unavailable(sessions, uuid.UUID(SERVER_A), controller_now=NOW)
        finding = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings WHERE kind = "
            "'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert finding is not None and finding.resolved_at is None

    @pytest.mark.anyio
    async def test_nothing_measured_round_persists_the_unknown_row(
        self, sessions
    ) -> None:
        empty = _sample(
            quality=ObservationQuality.UNKNOWN,
            os_updates=None,
            pins=(),
            checks=(),
        )
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), empty, controller_now=NOW
        )
        assert written == 1
        row = await _fetchone(
            sessions, "SELECT quality, metric FROM server_observations"
        )
        assert row.quality == "unknown" and row.metric is None

    @pytest.mark.anyio
    async def test_os_zero_is_only_zero_when_a_source_was_ok(self, sessions) -> None:
        # Round 1 opens the notice (two real pending updates).
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        up_to_date = replace(
            _sample(os_updates=_os(count=0)),
            checks=(
                _check(
                    APP_1,
                    "up_to_date",
                    upstream_tag="v2.0.0",
                    upstream_digest=DIGEST_UPSTREAM,
                ),
                _check(
                    APP_2,
                    "up_to_date",
                    repository="other-app",
                    upstream_tag="v2.0.0",
                    upstream_digest=DIGEST_UPSTREAM,
                ),
            ),
        )
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            up_to_date,
            controller_now=NOW + timedelta(hours=1),
        )
        row = await _fetchone(
            sessions,
            "SELECT value FROM server_observations WHERE metric = 'update.os.count' "
            "ORDER BY collected_at DESC LIMIT 1",
        )
        assert row is not None and row.value == "0"
        finding = await _fetchone(
            sessions,
            "SELECT resolved_at, summary FROM observation_findings "
            "WHERE kind = 'update.os_packages'",
        )
        assert finding is not None and finding.resolved_at is not None
        # A close resolves but never rewrites history: the summary keeps the
        # last ACTIVE state ("zero" is a fact row, not a rewritten notice).
        assert "2 OS package updates pending" in finding.summary

    @pytest.mark.anyio
    async def test_rows_carry_freshness_and_retention_horizons(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        row = await _fetchone(
            sessions,
            "SELECT observed_at, fresh_until, retain_until FROM server_observations "
            "WHERE metric = 'update.os.count'",
        )
        assert row.observed_at == NOW
        assert row.fresh_until == NOW + FRESH_HORIZON
        assert row.retain_until == NOW + RETENTION_HORIZON
        # Consumers derive staleness with the shared derivation.
        assert (
            derive_freshness("measured", row.observed_at, row.fresh_until, NOW)
            is ObservationFreshness.FRESH
        )
        assert (
            derive_freshness(
                "measured",
                row.observed_at,
                row.fresh_until,
                NOW + FRESH_HORIZON + timedelta(seconds=1),
            )
            is ObservationFreshness.STALE
        )
        assert (
            derive_freshness("unknown", None, None, NOW) is ObservationFreshness.UNKNOWN
        )

    @pytest.mark.anyio
    async def test_two_servers_are_a_storage_boundary(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        await record_unavailable(sessions, uuid.UUID(SERVER_B), controller_now=NOW)
        counts = await _fetchall(
            sessions,
            "SELECT server_id, quality FROM server_observations ORDER BY server_id",
        )
        by_server: dict[str, int] = {}
        for row in counts:
            by_server[str(row.server_id)] = by_server.get(str(row.server_id), 0) + 1
        assert by_server[SERVER_A] == 4
        assert by_server[SERVER_B] == 1


# ---------------------------------------------------------------------------
# Real target: disposable Arch sshd + real loopback registry + real Postgres
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-update-check-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-update-check-arch:sshd"
FALLBACK_IMAGE_TAG = "mai-fxt-04-ssh-arch:sshd"  # identical reviewed recipe
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-update-check/tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_UPDATE_CHECK_FIXTURE"
SENTINEL = "mai-10-update-check-tree-sentinel-not-a-secret"


class _RegistryServer:
    """One real loopback HTTP registry (ThreadingHTTPServer, ephemeral port).

    A REAL socket + real HTTP client exercise the actual bounded-network
    boundary (connect, status, headers, streaming caps, refusal). It speaks
    the two OCI Distribution v2 endpoints this collector uses; full registry
    semantics (token auth, manifest lists) remain the real fixture-registry
    scenario's territory (10-verify) and are a recorded limitation.
    """

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, object]] = {}
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> int:
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                registry.requests.append(self.path)
                path = self.path
                remainder = path[len("/v2/") :] if path.startswith("/v2/") else ""
                if remainder.endswith("/tags/list"):
                    repo = remainder[: -len("/tags/list")]
                    state = registry.repos.get(repo)
                    if state is None:
                        self._reply(404, b'{"errors": []}')
                        return
                    payload = json.dumps({"name": repo, "tags": state["tags"]}).encode()
                    self._reply(200, payload)
                    return
                if "/manifests/" in remainder:
                    repo, _, tag = remainder.partition("/manifests/")
                    state = registry.repos.get(repo)
                    digests = state["digests"] if state else {}
                    if tag not in digests:  # type: ignore[operator]
                        self._reply(404, b'{"errors": []}')
                        return
                    body = json.dumps({"tag": tag}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Docker-Content-Digest", digests[tag])  # type: ignore[index]
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._reply(404, b'{"errors": []}')

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image() -> str:
    """Own tag first; identical reviewed 04 recipe reused; else built."""
    if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return "own tag present"
    if _docker("image", "inspect", FALLBACK_IMAGE_TAG).returncode == 0:
        tag = _docker("tag", FALLBACK_IMAGE_TAG, FIXTURE_IMAGE_TAG)
        assert tag.returncode == 0, tag.stderr
        return f"tagged from {FALLBACK_IMAGE_TAG} (identical reviewed recipe)"
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh && ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    return f"built from archlinux:latest ({FALLBACK_IMAGE_TAG} absent)"


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.identity: SshIdentity | None = None
        self.image_provenance = "<untouched>"
        self.cleanup_proof = "<not cleaned>"

    def start(self) -> SshIdentity:
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
                f"{FIXTURE_CONTAINER}-test",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        self.image_provenance = _ensure_fixture_image()
        run = _docker(
            "run",
            "--rm",
            "-d",
            "--name",
            FIXTURE_CONTAINER,
            "-p",
            "127.0.0.1::22",
            FIXTURE_IMAGE_TAG,
            "sh",
            "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd && exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._provision_authorized_key()
        scanned: tuple = ()
        for _ in range(30):
            try:
                scanned = scan_host_keys(self._identity("unscanned"))
            except SshConnectError:
                scanned = ()  # sshd not ready yet; poll
            if scanned:
                break
            time.sleep(1.0)
        self.identity = self._identity(scanned[0].fingerprint)
        pin_host_key(self.identity, self.known_hosts_path)
        return self.identity

    def _identity(self, fingerprint: str) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user="root",
            host_key=fingerprint,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def _refresh_port(self) -> None:
        port_out = _docker(
            "inspect",
            "--format",
            '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}',
            FIXTURE_CONTAINER,
        )
        assert port_out.returncode == 0 and port_out.stdout.strip().isdigit(), (
            f"fixture port inspect failed: {port_out.stderr[-400:]}"
        )
        self.port = int(port_out.stdout.strip())

    def _provision_authorized_key(self) -> None:
        pub = Path(f"{self.key_path}.pub").read_text(encoding="utf-8").strip()
        provision = _docker(
            "exec",
            "-e",
            f"KEY={pub}",
            FIXTURE_CONTAINER,
            "sh",
            "-c",
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
            "&& printf '%s\\n' \"$KEY\" > /root/.ssh/authorized_keys "
            "&& chmod 600 /root/.ssh/authorized_keys",
        )
        assert provision.returncode == 0, provision.stderr

    @property
    def known_hosts_path(self) -> Path:
        return self.scratch / "known_hosts"

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def provision_managed_apps(self, registry_port: int) -> None:
        """Two MAI-managed pins + one non-UUID dir + the read-only sentinel."""
        app1 = f"127.0.0.1:{registry_port}/fixture-app@{DIGEST_INSTALLED_1}"
        app2 = f"127.0.0.1:{registry_port}/other-app@{DIGEST_UPSTREAM}"
        provision = _docker(
            "exec",
            FIXTURE_CONTAINER,
            "sh",
            "-c",
            f"mkdir -p /var/lib/mai/apps/{APP_1} /var/lib/mai/apps/{APP_2} "
            "/var/lib/mai/apps/not-managed-dir && "
            f"printf 'services:\\n  app:\\n    image: {app1}\\n' "
            f"> /var/lib/mai/apps/{APP_1}/compose.yaml && "
            f"printf 'services:\\n  app:\\n    image: {app2}\\n' "
            f"> /var/lib/mai/apps/{APP_2}/compose.yaml && "
            f"printf 'services:\\n  app:\\n    image: 127.0.0.1:{registry_port}"
            f"/junk-app@sha256:{'d' * 64}\\n' "
            "> /var/lib/mai/apps/not-managed-dir/compose.yaml && "
            f"printf '{SENTINEL}\\n' > /var/lib/mai/apps/SENTINEL.txt",
        )
        assert provision.returncode == 0, provision.stderr[-800:]

    def tree_hash(self) -> str:
        out = _docker(
            "exec",
            FIXTURE_CONTAINER,
            "sh",
            "-c",
            "cd /var/lib/mai/apps && find . -type f -print0 | sort -z | "
            "xargs -0 sha256sum | sha256sum",
        )
        assert out.returncode == 0, out.stderr[-400:]
        return out.stdout.strip()

    def stop_container(self) -> None:
        _docker("stop", "-t", "1", FIXTURE_CONTAINER)

    def cleanup(self) -> str:
        proof = _docker("rm", "-f", FIXTURE_CONTAINER)
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.cleanup_proof = (
            f"container rm rc={proof.returncode}"
            + (f" ({proof.stdout.strip()})" if proof.stdout.strip() else "")
            + f"; scratch {self.scratch} removed"
        )
        return self.cleanup_proof


@pytest.fixture(scope="session")
def real_target():
    if not _docker_available():
        pytest.skip("docker is not available; real-target claims need the fixture")
    fixture = ArchSshdFixture()
    try:
        fixture.start()
    except Exception as error:
        fixture.cleanup()
        raise AssertionError(f"fixture failed to start: {error}") from error
    yield fixture
    print(f"\nfixture cleanup: {fixture.cleanup()}")


class TestRealTarget:
    """Real-boundary proofs: real sshd target, real loopback HTTP registry,
    real Postgres, the real transport — no stub re-implements any invariant
    under test (the 0007 CHECKs and the real refusal paths are the
    enforcement)."""

    async def _collect(
        self,
        sessions,
        fixture: ArchSshdFixture,
        *,
        transport: object | None = None,
        now: datetime | None = None,
    ):
        resolver = fixture.env_resolver()
        key_path = resolve_identity_key(resolver, FIXTURE_CREDENTIAL_REF)
        assert fixture.identity is not None
        return await collect(
            sessions,
            fixture.identity,
            key_path=key_path,
            known_hosts_path=fixture.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=now or NOW,
            transport=transport,
        )

    @pytest.mark.real_target
    @pytest.mark.anyio
    async def test_real_probe_records_update_notice_and_is_read_only(
        self, sessions, real_target
    ) -> None:
        fixture: ArchSshdFixture = real_target
        registry = _RegistryServer()
        registry.repos["fixture-app"] = {
            "tags": ["v1.0.0", "v2.0.0"],
            "digests": {
                "v1.0.0": f"sha256:{'1' * 64}",
                "v2.0.0": DIGEST_UPSTREAM,
            },
        }
        registry.repos["other-app"] = {
            "tags": ["v2.0.0"],
            "digests": {"v2.0.0": DIGEST_UPSTREAM},
        }
        registry.start()
        try:
            fixture.provision_managed_apps(registry.port)
            before = fixture.tree_hash()
            result = await self._collect(sessions, fixture)
        finally:
            registry.stop()

        # One real update notice, one real up-to-date, real OS candidates.
        assert result.error is None
        assert result.quality is ObservationQuality.MEASURED
        by_app = {check.application_id: check for check in result.apps}
        assert by_app[APP_1].status == "update_available"
        assert by_app[APP_1].upstream_tag == "v2.0.0"
        assert by_app[APP_1].upstream_digest == DIGEST_UPSTREAM
        assert by_app[APP_2].status == "up_to_date"
        # Exactly two bounded calls per application, exact declared paths,
        # and the non-UUID directory never produced a pin or a call.
        assert len(registry.requests) == 4
        assert registry.requests.count("/v2/fixture-app/tags/list") == 1
        assert registry.requests.count("/v2/fixture-app/manifests/v2.0.0") == 1
        assert not any("/junk-app/" in path for path in registry.requests)

        rows = await _fetchall(
            sessions,
            "SELECT metric, value, quality FROM server_observations "
            "WHERE server_id = :sid ORDER BY metric",
            sid=uuid.UUID(SERVER_A),
        )
        metrics = [row.metric for row in rows]
        assert metrics.count("update.os.count") == 1
        assert metrics.count("update.app") == 2
        os_row = next(row for row in rows if row.metric == "update.os.count")
        assert os_row.value == "2"  # the fixture's real pacman answer
        assert all(row.quality == "measured" for row in rows)
        finding = await _fetchone(
            sessions,
            "SELECT summary, resolved_at FROM observation_findings "
            "WHERE kind = 'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert finding is not None and finding.resolved_at is None
        assert "update available for fixture-app" in finding.summary

        # Read-only proof: the managed tree + sentinel hash is UNCHANGED.
        assert fixture.tree_hash() == before

    @pytest.mark.real_target
    @pytest.mark.anyio
    async def test_real_registry_outage_is_unknown_not_no_updates(
        self, sessions, real_target
    ) -> None:
        fixture: ArchSshdFixture = real_target
        registry = _RegistryServer()
        registry.repos["fixture-app"] = {
            "tags": ["v2.0.0"],
            "digests": {"v2.0.0": DIGEST_UPSTREAM},
        }
        registry.repos["other-app"] = {
            "tags": ["v2.0.0"],
            "digests": {"v2.0.0": DIGEST_UPSTREAM},
        }
        registry.start()
        fixture.provision_managed_apps(registry.port)
        first = await self._collect(sessions, fixture)
        by_app = {check.application_id: check for check in first.apps}
        assert by_app[APP_1].status == "update_available"

        # Outage: the registry listener goes away (real connection refusal).
        registry.stop()
        result = await self._collect(sessions, fixture)
        by_app = {check.application_id: check for check in result.apps}
        assert by_app[APP_1].status == "unavailable"
        assert by_app[APP_1].detail == "unreachable"
        assert by_app[APP_2].status == "unavailable"
        assert result.quality is ObservationQuality.DEGRADED

        # The prior notice STAYS OPEN (never silently closed/re-evaluated),
        # and nothing claims the application is up to date.
        finding = await _fetchone(
            sessions,
            "SELECT summary, resolved_at FROM observation_findings "
            "WHERE kind = 'update.available' AND dedup_key = :key",
            key=str(APP_1),
        )
        assert finding is not None
        assert finding.resolved_at is None
        assert "update available" in finding.summary
        # The outage round persisted metric-free unknown rows, not zeroes
        # and not "up to date" values.
        unknown_rows = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations "
            "WHERE quality = 'unknown' AND server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        assert len(unknown_rows) == 2
        assert all(row.metric is None and row.value is None for row in unknown_rows)

    @pytest.mark.real_target
    @pytest.mark.anyio
    async def test_real_disconnect_records_unknown_never_zero(
        self, sessions, real_target
    ) -> None:
        fixture: ArchSshdFixture = real_target
        before = await _fetchall(
            sessions,
            "SELECT COUNT(*) FROM observation_findings",
        )
        fixture.stop_container()
        result = await self._collect(sessions, fixture)
        assert result.quality is ObservationQuality.UNKNOWN
        assert result.error in ("unreachable", "ssh_failed")
        assert result.observations == 1
        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at, quality FROM server_observations "
            "WHERE server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        assert row is not None
        assert row.quality == "unknown"
        assert row.metric is None and row.value is None
        assert row.observed_at is None
        # No finding was created or touched by the failed round.
        after = await _fetchall(sessions, "SELECT COUNT(*) FROM observation_findings")
        assert after == before
