"""Focused tests for the durable web notification service (card 10-notifications).

C10 rule 3 + C03 rule 6 acceptance coverage:

- **Acceptance 1 (deduplicated persistence)** — updates/findings/job-result
  notices persist into the real ``notification_alerts`` table (real Alembic
  chain on ``MAI_TEST_DATABASE_URL``) with server/time/context references;
  repeated evaluation of the same condition dedups onto one row (the
  structural ``(server_id, dedup_key)`` UNIQUE is the audited backstop),
  cooldown gates repeat reminders, severity escalation re-delivers, quiet
  hours defer outbound delivery while the row stays honestly ``pending``,
  and every delivery attempt's outcome is durably stamped.
- **Acceptance 2 (LLM-free, delivery seam)** — the module's import surface
  contains no agent/model/durable-engine symbol (structural audit), and the
  only outbound transport is the C03 credential-REFERENCE-only webhook: the
  URL resolves at delivery time through the injected resolver, never
  persists, never enters an error string, never rides a redirect.

Test layers (the 10-update-check shape): pure draft/policy/envelope
evaluation, webhook channel boundaries on the mock transport, REAL loopback
HTTP webhook round trips, real-Postgres persistence/delivery lifecycle on
the migrated 0007 chain. Secret hygiene: fixture webhook URLs are loopback
literals with a fake token query; they never enter persisted rows, events
or error strings — asserted, not assumed.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.credentials.env import SecretValue
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID
from mai.database import create_app_engine
from mai.notifications.service import (
    DEFAULT_COOLDOWN,
    MAX_NOTIFICATION_BODY_CHARS,
    MAX_WEBHOOK_BODY_BYTES,
    MAX_WEBHOOK_RESPONSE_BYTES,
    RETENTION_HORIZON,
    ChannelDeliveryError,
    DeliveryPayload,
    NotificationError,
    NotificationErrorCode,
    NotificationEvent,
    QuietHours,
    RoutingPolicy,
    WebhookChannel,
    deliver_pending,
    envelope_ts,
    evaluate_findings,
    finding_alert_draft,
    finding_dedup_key,
    in_quiet_hours,
    ingest_external,
    job_result_alert_draft,
    notify_job_result,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "3a7d1c9e-5b2f-4a60-8c11-0d9e2f3a4b5c"
SERVER_B = "4b8e2d0f-6c3a-4b71-9d22-1e0f3a4b5c6d"

FINDING_1 = uuid.UUID("c1d2e3f4-0000-4001-8001-00000000000f")
FINDING_2 = uuid.UUID("c1d2e3f4-0000-4002-8002-00000000000f")
RUN_OK = uuid.UUID("d4e5f6a7-0000-4001-9001-00000000000a")
RUN_BAD = uuid.UUID("d4e5f6a7-0000-4002-9002-00000000000a")
RUN_OTHER_SERVER = uuid.UUID("d4e5f6a7-0000-4003-9003-00000000000a")

NOW = datetime(2026, 10, 17, 22, 0, 0, tzinfo=UTC)

#: Loopback webhook URL with a fake token — credential MATERIAL for the
#: resolver; the channel stores only the NAME.
WEBHOOK_SECRET_URL = "http://127.0.0.1:59876/hook?token=SECRET-TOKEN-X"
CREDENTIAL_REF = "MAI_FXT_WEBHOOK_URL"

INBOX_POLICY = RoutingPolicy()


# ---------------------------------------------------------------------------
# Live-database fixtures (the 10-update-check shape)
# ---------------------------------------------------------------------------


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip (same reconciliation list as
        # 10-observation-schema): order-independent zero-to-head fixture.
        # Whole-schema reset (D09): the enumerated drop list went stale
        # across migrations — the schema drop is FK-safe and immune to
        # future revisions (same discipline as the adoption suite).
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
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
        await session.execute(text("DELETE FROM job_events"))
        await session.execute(text("DELETE FROM job_steps"))
        await session.execute(text("DELETE FROM job_runs"))
        await session.commit()
    await engine.dispose()


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _insert_finding(
    sessions,
    finding_id: uuid.UUID,
    server_id: str,
    *,
    kind: str = "update.available",
    dedup_key: str = "app-1",
    severity: str = "warning",
    summary: str = "update available for fixture-app",
) -> None:
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO observation_findings
                    (finding_id, server_id, kind, dedup_key, severity,
                     summary, detail, opened_at, updated_at, resolved_at,
                     retain_until)
                VALUES
                    (:fid, :sid, :kind, :dkey, :sev, :summary, NULL,
                     :now, :now, NULL, :now)
                """
            ),
            {
                "fid": finding_id,
                "sid": uuid.UUID(server_id),
                "kind": kind,
                "dkey": dedup_key,
                "sev": severity,
                "summary": summary,
                "now": NOW,
            },
        )
        await session.commit()


async def _insert_job_run(sessions, run_id: uuid.UUID, server_id: str) -> None:
    # The 0011 mandatory-tag constraint triggers are DEFERRABLE INITIALLY
    # DEFERRED: the run and its reserved classification share the ONE
    # committing transaction (the same discipline the real writers use).
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, parent_run_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     root_budget, error_ref, evidence_ref, cancel_requested_at,
                     submitted_at, updated_at)
                VALUES
                    (:rid, :sid, NULL, 0, 1, 1, 'codex-max',
                     '[]'::jsonb, NULL, NULL, NULL, NULL, :now, :now)
                """
            ),
            {"rid": run_id, "sid": uuid.UUID(server_id), "now": NOW},
        )
        await session.execute(
            text(
                """
                INSERT INTO job_tags (job_id, tag_id, assignment_source)
                VALUES (:jid, :tid, 'system')
                ON CONFLICT DO NOTHING
                """
            ),
            {"jid": run_id, "tid": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)},
        )
        await session.commit()


async def _alerts(sessions) -> list:
    return await _fetchall(
        sessions,
        "SELECT alert_id, server_id, origin, run_id, finding_id, dedup_key, "
        "severity, title, summary, body, source, context_ref, "
        "delivery_status, created_at, delivered_at, acknowledged_at, "
        "retain_until "
        "FROM notification_alerts ORDER BY created_at, dedup_key",
    )


async def _storage_dump(sessions) -> str:
    """Every stored observation-domain value, for write-scope sentinels."""
    parts = []
    for table in (
        "server_observations",
        "observation_findings",
    ):
        rows = await _fetchall(sessions, f"SELECT * FROM {table}")  # noqa: S608
        parts.extend(str(tuple(r)) for r in rows)
    return "\n".join(parts)


class RecordingSink:
    """EventSink double recording envelopes with its own cursor."""

    def __init__(self) -> None:
        self.envelopes: list[dict] = []
        self._seq = 0
        self.fail_first = 0

    async def emit(self, event: NotificationEvent) -> int:
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("sink down")
        self._seq += 1
        self.envelopes.append(event.envelope(self._seq))
        return self._seq


class CountingChannel:
    """NotificationChannel double counting deliveries and failing on cue."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.payloads: list[DeliveryPayload] = []

    async def deliver(self, payload: DeliveryPayload) -> None:
        if self.fail:
            raise ChannelDeliveryError(self.name, "http_500")
        self.payloads.append(payload)


def time_from(hour: int, minute: int) -> time:
    return time(hour, minute)


def _resolver_of(value: str):
    class _Resolver:
        def __init__(self) -> None:
            self.resolved: list[str] = []

        def resolve(self, name: str) -> SecretValue:
            self.resolved.append(name)
            return SecretValue(value)

    return _Resolver()


def _webhook_channel(**kw: object) -> tuple[WebhookChannel, object]:
    resolver = _resolver_of(WEBHOOK_SECRET_URL)
    channel = WebhookChannel(
        "webhook",
        CREDENTIAL_REF,
        credentials=resolver,
        transport=kw.pop("transport", None),
        **kw,  # type: ignore[arg-type]
    )
    return channel, resolver


def _quiet_policy(
    *,
    routes: dict[str, tuple[str, ...]] | None = None,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    quiet: QuietHours | None = None,
) -> RoutingPolicy:
    return RoutingPolicy(
        cooldown=cooldown,
        quiet_hours=quiet,
        channels_by_severity=routes
        if routes is not None
        else {
            "info": (),
            "warning": ("webhook",),
            "critical": ("webhook",),
        },
    )


def _policy(**kw: object) -> RoutingPolicy:
    return RoutingPolicy(
        channels_by_severity={
            "info": (),
            "warning": (),
            "critical": (),
        },
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Pure layer: drafts, dedup keys
# ---------------------------------------------------------------------------


class TestDraftsAndDedupKeys:
    def test_finding_dedup_key_is_stable_and_bounded(self):
        first = finding_dedup_key("update.available", "app-1")
        second = finding_dedup_key("update.available", "app-1")
        assert first == second
        assert first.startswith("finding:")
        assert len(first) <= 64

    def test_finding_dedup_keys_never_collide_on_long_inputs(self):
        long_a = "k" * 200 + "a"
        long_b = "k" * 200 + "b"
        assert finding_dedup_key("kind", long_a) != finding_dedup_key("kind", long_b)
        assert finding_dedup_key("kind.a", "x") != finding_dedup_key("kind", "a\x1fx")

    def test_job_result_dedup_key_is_the_run(self):
        assert (
            job_result_alert_draft(  # outcome severity mapping sanity
                RUN_OK, "succeeded", model_profile="codex"
            ).dedup_key
            == f"job_result:{RUN_OK}"
        )

    def test_finding_draft_carries_frozen_fields(self):
        draft = finding_alert_draft(
            {
                "finding_id": FINDING_1,
                "kind": "update.available",
                "dedup_key": "app-1",
                "severity": "warning",
                "summary": "update available for fixture-app",
            }
        )
        assert draft.origin == "finding"
        assert draft.finding_id == FINDING_1
        assert draft.run_id is None
        assert draft.severity == "warning"
        assert draft.context_ref == f"finding:{FINDING_1}"
        assert draft.dedup_key == finding_dedup_key("update.available", "app-1")
        assert "update available" in draft.title
        assert draft.summary == "update available for fixture-app"

    def test_finding_draft_truncates_to_column_bounds(self):
        draft = finding_alert_draft(
            {
                "finding_id": FINDING_1,
                "kind": "k",
                "dedup_key": "d",
                "severity": "info",
                "summary": "x" * 10_000,
            }
        )
        assert len(draft.title) <= 200
        assert len(draft.summary) <= 512
        assert draft.summary.startswith("x") and draft.summary.endswith("…")

    def test_finding_draft_rejects_unknown_severity(self):
        with pytest.raises(NotificationError):
            finding_alert_draft(
                {
                    "finding_id": FINDING_1,
                    "kind": "k",
                    "dedup_key": "d",
                    "severity": "fatal",
                    "summary": "s",
                }
            )

    def test_job_result_draft_maps_outcomes(self):
        ok = job_result_alert_draft(RUN_OK, "succeeded", model_profile="codex")
        bad = job_result_alert_draft(
            RUN_BAD, "failed", model_profile="codex", error_ref="err:1"
        )
        assert (ok.severity, bad.severity) == ("info", "warning")
        assert ok.origin == "job_result" and bad.origin == "job_result"
        assert ok.run_id == RUN_OK and bad.run_id == RUN_BAD
        assert ok.context_ref == f"run:{RUN_OK}"
        assert "error_ref=err:1" in bad.summary and "error_ref" not in ok.summary

    def test_job_result_draft_rejects_unknown_outcome(self):
        with pytest.raises(NotificationError):
            job_result_alert_draft(RUN_OK, "timed_out", model_profile="codex")


# ---------------------------------------------------------------------------
# Pure layer: policy, quiet hours, envelope
# ---------------------------------------------------------------------------


class TestPolicyAndQuietHours:
    def test_policy_rejects_non_positive_cooldown(self):
        with pytest.raises(NotificationError):
            RoutingPolicy(cooldown=timedelta(0))
        with pytest.raises(NotificationError):
            RoutingPolicy(cooldown=timedelta(hours=-1))

    def test_policy_rejects_unknown_severity_key(self):
        with pytest.raises(NotificationError):
            RoutingPolicy(channels_by_severity={"fatal": ("webhook",)})

    def test_policy_rejects_empty_channel_name(self):
        with pytest.raises(NotificationError):
            RoutingPolicy(channels_by_severity={"info": ("",)})

    def test_policy_rejects_bad_quiet_hours_object(self):
        with pytest.raises(NotificationError):
            RoutingPolicy(quiet_hours="23:00-07:00")  # type: ignore[arg-type]

    def test_quiet_hours_reject_degenerate_window(self):
        with pytest.raises(NotificationError):
            QuietHours(start=time_from(9, 0), end=time_from(9, 0), tzinfo=timezone.utc)

    def test_channels_for_defaults_to_inbox_only(self):
        assert _policy().channels_for("critical") == ()
        assert _policy().channels_for("unknown-severity") == ()

    def test_quiet_hours_inside_and_outside(self):
        window = QuietHours(
            start=time_from(23, 0), end=time_from(7, 0), tzinfo=timezone.utc
        )
        assert in_quiet_hours(datetime(2026, 10, 17, 23, 30, tzinfo=UTC), window)
        assert in_quiet_hours(datetime(2026, 10, 17, 3, 0, tzinfo=UTC), window)
        assert not in_quiet_hours(datetime(2026, 10, 17, 12, 0, tzinfo=UTC), window)
        day = QuietHours(
            start=time_from(9, 0), end=time_from(17, 0), tzinfo=timezone.utc
        )
        assert in_quiet_hours(datetime(2026, 10, 17, 12, 0, tzinfo=UTC), day)
        assert not in_quiet_hours(datetime(2026, 10, 17, 18, 0, tzinfo=UTC), day)

    def test_quiet_hours_respects_the_window_timezone(self):
        tokyo = timezone(timedelta(hours=9))
        window = QuietHours(start=time_from(23, 0), end=time_from(7, 0), tzinfo=tokyo)
        # 23:30 JST == 14:30 UTC → quiet in Tokyo.
        assert in_quiet_hours(datetime(2026, 10, 17, 14, 30, tzinfo=UTC), window)

    def test_quiet_hours_reject_naive_instant(self):
        window = QuietHours(
            start=time_from(23, 0), end=time_from(7, 0), tzinfo=timezone.utc
        )
        with pytest.raises(ValueError):
            in_quiet_hours(datetime(2026, 10, 17, 23, 30), window)


class TestEventEnvelope:
    def test_envelope_has_exactly_the_frozen_field_set(self):
        event = NotificationEvent(
            type="notification.created",
            ts=NOW,
            run_id=None,
            data={"alert_id": str(uuid.uuid4()), "severity": "info"},
        )
        envelope = event.envelope(7)
        assert set(envelope) == {"seq", "run_id", "ts", "type", "v", "data"}
        assert envelope["seq"] == 7 and envelope["v"] == 1
        assert envelope["run_id"] is None
        assert envelope["type"] == "notification.created"
        assert envelope["ts"] == "2026-10-17T22:00:00.000Z"

    def test_envelope_run_id_is_canonical_uuid_string(self):
        event = NotificationEvent(
            type="notification.delivered", ts=NOW, run_id=RUN_OK, data={}
        )
        assert event.envelope(1)["run_id"] == str(RUN_OK)

    def test_envelope_rejects_non_positive_or_boolean_seq(self):
        event = NotificationEvent(
            type="notification.created", ts=NOW, run_id=None, data={}
        )
        for bad in (0, -1, True, 1.5):
            with pytest.raises(NotificationError):
                event.envelope(bad)  # type: ignore[arg-type]

    def test_envelope_ts_rejects_naive_timestamps(self):
        with pytest.raises(ValueError):
            envelope_ts(datetime(2026, 10, 17, 22, 0, 0))


class TestModuleBoundaries:
    def test_module_imports_no_llm_or_engine_symbols(self):
        # Brief acceptance 2: collectors/notifications work when the model
        # endpoint is unavailable — structurally, by import audit. Top-level
        # imports only; the lazy in-function import is asserted separately.
        source_path = os.path.join(
            BACKEND_ROOT, "src", "mai", "notifications", "service.py"
        )
        tree = ast.parse(open(source_path, encoding="utf-8").read())
        top_level: set[str] = set()
        lazy: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                lazy.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                lazy.add(node.module)
        for module in top_level:
            top = module.split(".")[0]
            assert top not in {"dbos", "openai", "requests", "aiohttp", "httpx"}, module
            assert not module.startswith(("mai.agents", "mai.jobs", "mai.execution")), (
                module
            )
        allowed_app_modules = {
            "mai.contracts.observations",
            "mai.credentials.env",
            "mai.database",
        }
        assert top_level & allowed_app_modules, top_level
        # The outbound HTTP client is the locked httpx2 — and ONLY lazily
        # inside deliver(), never at module import time.
        assert "httpx2" in lazy and "httpx2" not in top_level


# ---------------------------------------------------------------------------
# Webhook channel on the mock transport (protocol shapes + budgets)
# ---------------------------------------------------------------------------


def _recording_transport(
    responder,  # (request) -> httpx2.Response
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    """MockTransport whose handler records every request (sibling pattern)."""
    import httpx2

    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return responder(request)

    return httpx2.MockTransport(handler), requests


def _mock_channel(
    transport: httpx2.MockTransport,
    *,
    url: str = WEBHOOK_SECRET_URL,
) -> tuple[WebhookChannel, object]:
    resolver = _resolver_of(url)
    return (
        WebhookChannel(
            "webhook", CREDENTIAL_REF, credentials=resolver, transport=transport
        ),
        resolver,
    )


def _payload(**kw: object) -> DeliveryPayload:
    fields: dict[str, object] = {
        "alert_id": uuid.uuid4(),
        "server_id": uuid.UUID(SERVER_A),
        "origin": "finding",
        "dedup_key": "finding:abc",
        "severity": "warning",
        "title": "update.available: update available",
        "summary": "update available for fixture-app",
        "context_ref": f"finding:{FINDING_1}",
        "created_at": NOW,
        "event_type": "notification.delivered",
        "decided_at": NOW,
    }
    fields.update(kw)
    return DeliveryPayload(**fields)  # type: ignore[arg-type]


@pytest.mark.anyio
class TestWebhookChannelMock:
    async def test_posts_bounded_json_with_no_credentials(self):
        transport, requests = _recording_transport(
            lambda request: httpx2.Response(200, content=b"ok")
        )
        channel, resolver = _mock_channel(transport)
        await channel.deliver(_payload())
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert "Authorization" not in request.headers
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        assert body["event"] == "notification.delivered"
        assert body["severity"] == "warning"
        assert body["ts"].endswith("Z")
        # The secret URL never appears in the payload or its rendering.
        assert WEBHOOK_SECRET_URL not in request.content.decode()
        assert resolver.resolved == [CREDENTIAL_REF]

    async def test_non_2xx_is_a_failed_attempt(self):
        transport, requests = _recording_transport(
            lambda request: httpx2.Response(503, content=b"down")
        )
        channel, _ = _mock_channel(transport)
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "http_503"

    async def test_redirect_status_is_never_success(self):
        transport, requests = _recording_transport(
            lambda request: httpx2.Response(302, headers={"Location": "/"})
        )
        channel, _ = _mock_channel(transport)
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "http_302"
        assert len(requests) == 1  # NOT followed

    async def test_transport_failures_map_value_free(self):
        def timeout_handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectTimeout(f"timed out contacting {WEBHOOK_SECRET_URL}")

        channel, _ = _mock_channel(_recording_transport(timeout_handler)[0])
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "timeout"
        assert WEBHOOK_SECRET_URL not in str(err.value)

        def refused_handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError(f"refused {WEBHOOK_SECRET_URL}")

        channel, _ = _mock_channel(_recording_transport(refused_handler)[0])
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "unreachable"
        assert WEBHOOK_SECRET_URL not in str(err.value)

    async def test_resolver_failure_is_credential_unresolved(self):
        class BrokenResolver:
            def resolve(self, name: str) -> SecretValue:
                raise RuntimeError(f"missing {name}")

        channel = WebhookChannel(
            "webhook", CREDENTIAL_REF, credentials=BrokenResolver()
        )
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "credential_unresolved"

    async def test_oversized_response_aborts_at_the_budget(self):
        transport, _ = _recording_transport(
            lambda request: httpx2.Response(
                200, content=b"x" * (MAX_WEBHOOK_RESPONSE_BYTES + 1)
            )
        )
        channel, _ = _mock_channel(transport)
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "oversized_response"

    async def test_oversized_body_is_refused_before_sending(self):
        transport, requests = _recording_transport(
            lambda request: httpx2.Response(200, content=b"ok")
        )
        channel, _ = _mock_channel(transport)
        huge = _payload(summary="x" * (MAX_WEBHOOK_BODY_BYTES + 10))
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(huge)
        assert err.value.code == "oversized_body"
        assert requests == []  # nothing left the controller

    def test_channel_holds_only_the_credential_reference(self):
        transport, _ = _recording_transport(lambda request: httpx2.Response(200))
        channel, resolver = _mock_channel(transport)
        assert channel.name == "webhook"
        assert channel.credential_ref == CREDENTIAL_REF
        assert WEBHOOK_SECRET_URL not in repr(channel)
        assert isinstance(resolver.resolved, list) and not resolver.resolved

    def test_secret_value_redacts_its_material(self):
        secret = SecretValue(WEBHOOK_SECRET_URL)
        assert secret.value == WEBHOOK_SECRET_URL  # delivery-time use only
        assert WEBHOOK_SECRET_URL not in str(secret)
        assert WEBHOOK_SECRET_URL not in repr(secret)


# ---------------------------------------------------------------------------
# Webhook channel against a REAL loopback HTTP server
# ---------------------------------------------------------------------------


class _HookServer:
    """Minimal real HTTP receiver: records bodies, answers a fixed code."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[bytes] = []
        lock = threading.Lock()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with lock:
                    outer.bodies.append(body)
                self.send_response(outer.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Shutdown + close; only valid after :meth:`start`."""
        self._server.shutdown()
        self._server.server_close()

    def close(self) -> None:
        """Release the port WITHOUT running the loop (refusal fixture)."""
        self._server.server_close()


@pytest.mark.anyio
class TestWebhookRealBoundary:
    async def test_real_post_round_trip_delivers_the_notice(self):
        server = _HookServer()
        server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/hook?token=SECRET"
            # The REAL async client talks to the REAL loopback socket — no
            # transport indirection anywhere in this test.
            channel = WebhookChannel(
                "webhook",
                CREDENTIAL_REF,
                credentials=_resolver_of(url),
            )
            await channel.deliver(_payload())
            assert len(server.bodies) == 1
            body = json.loads(server.bodies[0])
            assert body["title"].startswith("update.available")
            assert body["dedup_key"].startswith("finding:")
            assert body["severity"] == "warning"
        finally:
            server.stop()

    async def test_real_refusal_maps_to_unreachable(self):
        server = _HookServer()
        port = server.port  # bound then closed: guaranteed refusal
        server.close()
        channel = WebhookChannel(
            "webhook",
            CREDENTIAL_REF,
            credentials=_resolver_of(f"http://127.0.0.1:{port}/hook"),
            timeout=2.0,
        )
        with pytest.raises(ChannelDeliveryError) as err:
            await channel.deliver(_payload())
        assert err.value.code == "unreachable"

    async def test_real_http_500_maps_to_http_code(self):
        server = _HookServer(status=500)
        server.start()
        try:
            channel = WebhookChannel(
                "webhook",
                CREDENTIAL_REF,
                credentials=_resolver_of(f"http://127.0.0.1:{server.port}/hook"),
            )
            with pytest.raises(ChannelDeliveryError) as err:
                await channel.deliver(_payload())
            assert err.value.code == "http_500"
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# Persistence + delivery lifecycle on the real 0007 chain
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestPersistence:
    async def test_finding_notice_persists_with_references(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        sink = RecordingSink()
        outcomes = await evaluate_findings(
            sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy(), sink=sink
        )
        assert len(outcomes) == 1
        assert outcomes[0].disposition == "created"
        assert outcomes[0].delivery == "delivered"
        rows = await _alerts(sessions)
        assert len(rows) == 1
        row = rows[0]
        assert row.server_id == uuid.UUID(SERVER_A)
        assert row.origin == "finding"
        assert row.severity == "warning"  # the real column, no summary tag
        assert row.finding_id == FINDING_1
        assert row.run_id is None
        assert row.context_ref == f"finding:{FINDING_1}"
        assert row.delivery_status == "delivered"
        assert row.delivered_at == NOW
        assert row.acknowledged_at is None
        assert row.retain_until == NOW + RETENTION_HORIZON
        # Exactly one created event, frozen shape, bounded refs only.
        assert len(sink.envelopes) == 2  # created + delivered
        created, delivered = sink.envelopes
        assert created["type"] == "notification.created"
        assert delivered["type"] == "notification.delivered"
        assert created["data"]["dedup_key"] == row.dedup_key
        assert created["data"]["severity"] == "warning"
        assert set(created["data"]) == {
            "alert_id",
            "server_id",
            "dedup_key",
            "severity",
            "origin",
        }

    async def test_repeated_poll_dedups_onto_one_notice(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        first = await evaluate_findings(
            sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy()
        )
        second = await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(minutes=5),
            policy=_policy(),
        )
        assert first[0].disposition == "created"
        assert second[0].disposition == "deduped"
        assert len(await _alerts(sessions)) == 1

    async def test_cooldown_gates_reminders_then_releases(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        policy = _policy(cooldown=timedelta(hours=1))
        await evaluate_findings(sessions, uuid.UUID(SERVER_A), now=NOW, policy=policy)
        inside = await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(minutes=30),
            policy=policy,
        )
        assert inside[0].disposition == "deduped"
        after = await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(hours=2),
            policy=policy,
        )
        assert after[0].disposition == "reminder"
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "delivered"
        assert row.delivered_at == NOW + timedelta(hours=2)

    async def test_acknowledged_notice_is_never_nagged(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        policy = _policy(cooldown=timedelta(minutes=1))
        await evaluate_findings(sessions, uuid.UUID(SERVER_A), now=NOW, policy=policy)
        async with sessions() as session:
            await session.execute(
                text("UPDATE notification_alerts SET acknowledged_at = :ack"),
                {"ack": NOW + timedelta(minutes=5)},
            )
            await session.commit()
        later = await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(hours=5),
            policy=policy,
        )
        assert later[0].disposition == "unchanged"
        assert later[0].delivery == "skipped"

    async def test_escalation_redelivers_within_cooldown(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        routes = {"info": (), "warning": (), "critical": ("webhook",)}
        channel = CountingChannel("webhook")
        policy = _quiet_policy(routes=routes, cooldown=timedelta(hours=6))
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": channel},
        )
        # Collector re-upserts the same condition as critical.
        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE observation_findings SET severity = 'critical' "
                    "WHERE finding_id = :f"
                ),
                {"f": FINDING_1},
            )
            await session.commit()
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW + timedelta(minutes=10),
                policy=policy,
                channels={"webhook": channel},
            )
        )[0]
        assert outcome.disposition == "escalated"
        assert outcome.delivery == "delivered"
        assert len(channel.payloads) == 1
        assert channel.payloads[0].severity == "critical"
        # The column mirrors what the escalation last told the human.
        row = (await _alerts(sessions))[0]
        assert row.severity == "critical"

    async def test_de_escalation_rescores_without_redelivery(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="critical")
        routes = {"info": (), "warning": (), "critical": ("webhook",)}
        channel = CountingChannel("webhook")
        policy = _quiet_policy(routes=routes, cooldown=timedelta(hours=6))
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": channel},
        )
        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE observation_findings SET severity = 'info' "
                    "WHERE finding_id = :f"
                ),
                {"f": FINDING_1},
            )
            await session.commit()
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW + timedelta(minutes=10),
                policy=policy,
                channels={"webhook": channel},
            )
        )[0]
        assert outcome.disposition == "rescored"
        assert outcome.delivery == "skipped"
        assert len(channel.payloads) == 1  # no second delivery
        row = (await _alerts(sessions))[0]
        assert row.severity == "info"  # rescored on the column

    async def test_unknown_channel_name_fails_closed(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        routes = {"info": (), "warning": ("missing",), "critical": ()}
        with pytest.raises(NotificationError) as err:
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,
                policy=RoutingPolicy(channels_by_severity=routes),
            )
        assert err.value.code == "unknown_channel"
        assert await _alerts(sessions) == []  # nothing persisted

    async def test_duplicate_key_is_a_real_integrity_violation(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        await evaluate_findings(
            sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy()
        )
        row = (await _alerts(sessions))[0]
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO notification_alerts (alert_id, server_id, "
                        "origin, dedup_key, title, delivery_status, created_at, "
                        "retain_until) VALUES (:a, :s, 'finding', :d, 't', "
                        "'pending', :now, :now)"
                    ),
                    {
                        "a": uuid.uuid4(),
                        "s": uuid.UUID(SERVER_A),
                        "d": row.dedup_key,
                        "now": NOW,
                    },
                )
            await session.rollback()

    async def test_evaluation_never_writes_observation_tables(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        before = await _storage_dump(sessions)
        await evaluate_findings(
            sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy()
        )
        assert await _storage_dump(sessions) == before


@pytest.mark.anyio
class TestDeliveryLifecycle:
    async def test_quiet_hours_defer_webhook_and_sweep_flushes(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        channel = CountingChannel("webhook")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        policy = _quiet_policy(quiet=quiet)
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,  # 22:00 UTC → inside the window
                policy=policy,
                channels={"webhook": channel},
            )
        )[0]
        assert outcome.delivery == "pending"
        assert channel.payloads == []
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "pending"
        assert row.delivered_at is None
        sweep = await deliver_pending(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(hours=2),  # 00:00 UTC → after the window
            policy=policy,
            channels={"webhook": channel},
        )
        assert (sweep.delivered, sweep.failed, sweep.deferred) == (1, 0, 0)
        assert len(channel.payloads) == 1
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "delivered"
        assert row.delivered_at == NOW + timedelta(hours=2)

    async def test_sweep_inside_quiet_hours_defers_again(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        channel = CountingChannel("webhook")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        policy = _quiet_policy(quiet=quiet)
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": channel},
        )
        sweep = await deliver_pending(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(minutes=30),  # still inside
            policy=policy,
            channels={"webhook": channel},
        )
        assert (sweep.delivered, sweep.deferred) == (0, 1)
        assert channel.payloads == []

    async def test_critical_bypasses_quiet_hours(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="critical")
        channel = CountingChannel("webhook")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        policy = _quiet_policy(quiet=quiet)
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,
                policy=policy,
                channels={"webhook": channel},
            )
        )[0]
        assert outcome.delivery == "delivered"
        assert len(channel.payloads) == 1

    async def test_webhook_failure_marks_failed_and_inbox_stays(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        failing = CountingChannel("webhook", fail=True)
        policy = _quiet_policy()
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,
                policy=policy,
                channels={"webhook": failing},
            )
        )[0]
        assert outcome.delivery == "failed"
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "failed"
        assert row.delivered_at is None
        # The inbox row still exists with its full content (ack/audit
        # evidence is never deleted by a delivery failure).
        assert row.title.startswith("update.available")

    async def test_sweep_does_not_retry_failed_rows(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        failing = CountingChannel("webhook", fail=True)
        policy = _quiet_policy()
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": failing},
        )
        healthy = CountingChannel("webhook")
        sweep = await deliver_pending(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(hours=1),
            policy=policy,
            channels={"webhook": healthy},
        )
        assert (sweep.delivered, sweep.failed) == (0, 0)
        assert healthy.payloads == []
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "failed"

    async def test_all_routed_channels_attempted_despite_one_failure(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="critical")
        bad = CountingChannel("bad", fail=True)
        good = CountingChannel("good")
        policy = RoutingPolicy(
            channels_by_severity={
                "info": (),
                "warning": (),
                "critical": ("bad", "good"),
            }
        )
        outcome = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,
                policy=policy,
                channels={"bad": bad, "good": good},
            )
        )[0]
        assert outcome.delivery == "failed"
        # Both routes were attempted despite the first failing.
        assert outcome.channels_attempted == ("bad", "good")
        assert len(good.payloads) == 1
        assert bad.payloads == []  # the failing double records nothing

    async def test_sweep_never_touches_another_server(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        channel_a = CountingChannel("webhook")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        policy = _quiet_policy(quiet=quiet)
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": channel_a},
        )
        # Server B's sweep must not touch server A's pending notice.
        sweep = await deliver_pending(
            sessions,
            uuid.UUID(SERVER_B),
            now=NOW + timedelta(hours=2),
            policy=policy,
            channels={"webhook": CountingChannel("webhook")},
        )
        assert (sweep.delivered, sweep.deferred) == (0, 0)
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "pending"

    async def test_severed_anchor_routes_by_the_stored_severity(self, sessions):
        # After the retention sweep severs the finding (ON DELETE SET NULL),
        # a pending notice must still route by its recorded severity — the
        # real column, no anchor join.
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        policy = _quiet_policy(quiet=quiet)
        await evaluate_findings(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW,
            policy=policy,
            channels={"webhook": CountingChannel("webhook")},
        )
        async with sessions() as session:
            await session.execute(
                text("DELETE FROM observation_findings WHERE finding_id = :f"),
                {"f": FINDING_1},
            )
            await session.commit()
        row = (await _alerts(sessions))[0]
        assert row.finding_id is None  # anchor legally severed
        channel = CountingChannel("webhook")
        sweep = await deliver_pending(
            sessions,
            uuid.UUID(SERVER_A),
            now=NOW + timedelta(hours=2),
            policy=policy,
            channels={"webhook": channel},
        )
        assert sweep.delivered == 1
        assert len(channel.payloads) == 1
        assert channel.payloads[0].severity == "warning"  # from the column

    async def test_sink_failure_never_rolls_back_the_notice(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        sink = RecordingSink()
        sink.fail_first = 1  # the created event dies
        outcome = (
            await evaluate_findings(
                sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy(), sink=sink
            )
        )[0]
        assert outcome.delivery == "delivered"
        assert outcome.event_error == "RuntimeError"
        assert len(await _alerts(sessions)) == 1
        assert len(sink.envelopes) == 1  # the delivered event got through
        assert sink.envelopes[0]["seq"] == 1

    async def test_inbox_only_policy_stamps_delivered_once(self, sessions):
        await _insert_finding(sessions, FINDING_1, SERVER_A)
        await evaluate_findings(
            sessions, uuid.UUID(SERVER_A), now=NOW, policy=_policy()
        )
        row = (await _alerts(sessions))[0]
        assert row.delivery_status == "delivered"
        assert row.delivered_at == NOW


@pytest.mark.anyio
class TestJobResultNotices:
    async def test_completed_and_failed_runs_notify_with_references(self, sessions):
        await _insert_job_run(sessions, RUN_OK, SERVER_A)
        await _insert_job_run(sessions, RUN_BAD, SERVER_A)
        sink = RecordingSink()
        ok = await notify_job_result(
            sessions,
            uuid.UUID(SERVER_A),
            RUN_OK,
            "succeeded",
            model_profile="codex-max",
            now=NOW,
            policy=_policy(),
            sink=sink,
        )
        bad = await notify_job_result(
            sessions,
            uuid.UUID(SERVER_A),
            RUN_BAD,
            "failed",
            model_profile="codex-max",
            error_ref="err:step-3",
            now=NOW + timedelta(seconds=1),
            policy=_policy(),
            sink=sink,
        )
        assert (ok.disposition, bad.disposition) == ("created", "created")
        rows = {str(r.run_id): r for r in await _alerts(sessions)}
        assert rows[str(RUN_OK)].origin == "job_result"
        assert rows[str(RUN_OK)].finding_id is None
        assert rows[str(RUN_BAD)].context_ref == f"run:{RUN_BAD}"
        assert rows[str(RUN_BAD)].severity == "warning"  # the real column
        assert "outcome=failed" in rows[str(RUN_BAD)].summary
        # Envelope carries the run anchor for job-result notices.
        created = [e for e in sink.envelopes if e["type"] == "notification.created"]
        assert {e["run_id"] for e in created} == {str(RUN_OK), str(RUN_BAD)}

    async def test_duplicate_job_result_call_is_a_noop(self, sessions):
        await _insert_job_run(sessions, RUN_OK, SERVER_A)
        first = await notify_job_result(
            sessions,
            uuid.UUID(SERVER_A),
            RUN_OK,
            "succeeded",
            model_profile="codex-max",
            now=NOW,
            policy=_policy(),
        )
        second = await notify_job_result(
            sessions,
            uuid.UUID(SERVER_A),
            RUN_OK,
            "succeeded",
            model_profile="codex-max",
            now=NOW + timedelta(minutes=1),
            policy=_policy(),
        )
        assert (first.disposition, second.disposition) == ("created", "deduped")
        assert len(await _alerts(sessions)) == 1

    async def test_unknown_run_fails_closed_before_insert(self, sessions):
        with pytest.raises(NotificationError):
            await notify_job_result(
                sessions,
                uuid.UUID(SERVER_A),
                RUN_OK,  # no job_runs row
                "failed",
                model_profile="codex-max",
                now=NOW,
                policy=_policy(),
            )
        assert await _alerts(sessions) == []

    async def test_cross_server_run_is_refused(self, sessions):
        await _insert_job_run(sessions, RUN_OTHER_SERVER, SERVER_B)
        with pytest.raises(NotificationError) as err:
            await notify_job_result(
                sessions,
                uuid.UUID(SERVER_A),  # caller claims server A...
                RUN_OTHER_SERVER,  # ...but the run belongs to server B
                "failed",
                model_profile="codex-max",
                now=NOW,
                policy=_policy(),
            )
        assert "different server" in err.value.detail
        assert await _alerts(sessions) == []  # nothing re-attributed

    async def test_failed_run_notice_routes_as_warning(self, sessions):
        await _insert_job_run(sessions, RUN_BAD, SERVER_A)
        channel = CountingChannel("webhook")
        routes = {"info": (), "warning": ("webhook",), "critical": ("webhook",)}
        outcome = await notify_job_result(
            sessions,
            uuid.UUID(SERVER_A),
            RUN_BAD,
            "failed",
            model_profile="codex-max",
            now=NOW,
            policy=RoutingPolicy(channels_by_severity=routes),
            channels={"webhook": channel},
        )
        assert outcome.delivery == "delivered"
        assert channel.payloads[0].severity == "warning"
        assert channel.payloads[0].origin == "job_result"


# ---------------------------------------------------------------------------
# External notices: the machine ingest lane's writer (unified inbox, 0017)
# ---------------------------------------------------------------------------


def _external_key(source: str | None, dedup_key: str) -> str:
    """The frozen stored-key namespace the service derives (test oracle)."""
    digest = hashlib.sha256(f"{source or ''}|{dedup_key}".encode()).hexdigest()[:32]
    return f"external:{digest}"


@pytest.mark.anyio
class TestExternalNotices:
    async def test_global_notice_persists_delivered_with_identity(self, sessions):
        outcome = await ingest_external(
            sessions,
            title="disk almost full",
            body="/var usage 94% on db-1",
            severity="warning",
            source="host-monitor",
            dedup_key="disk-/var-db-1",
            context_ref="https://monitoring.example/host/db-1",
            now=NOW,
        )
        assert (outcome.disposition, outcome.delivery) == ("created", "delivered")
        assert outcome.dedup_key == _external_key("host-monitor", "disk-/var-db-1")
        rows = await _alerts(sessions)
        assert len(rows) == 1
        row = rows[0]
        assert row.server_id is None  # global: NULL server, no sentinel UUID
        assert row.origin == "external"
        assert row.severity == "warning"  # the real column
        assert row.title == "disk almost full"
        assert row.body == "/var usage 94% on db-1"
        assert row.source == "host-monitor"
        assert row.summary is None  # body carries the external text
        assert row.context_ref == "https://monitoring.example/host/db-1"
        assert row.delivery_status == "delivered"  # inbox-only stamp on insert
        assert row.delivered_at == NOW
        assert row.acknowledged_at is None
        assert row.retain_until == NOW + RETENTION_HORIZON

    async def test_namespaced_key_hashes_source_and_key(self, sessions):
        keyed = await ingest_external(
            sessions, title="t", source="a", dedup_key="k", now=NOW
        )
        sourceless = await ingest_external(
            sessions, title="t", dedup_key="k", now=NOW
        )
        assert keyed.dedup_key == _external_key("a", "k")
        assert sourceless.dedup_key == _external_key(None, "k")  # '' source
        assert keyed.dedup_key != sourceless.dedup_key

    async def test_replay_is_idempotent_per_scope(self, sessions):
        first = await ingest_external(
            sessions, title="t1", source="s", dedup_key="k", now=NOW
        )
        second = await ingest_external(
            sessions,
            title="t2",
            source="s",
            dedup_key="k",
            now=NOW + timedelta(minutes=5),
        )
        scoped_first = await ingest_external(
            sessions,
            server_id=SERVER_A,
            title="t1",
            source="s",
            dedup_key="k",
            now=NOW,
        )
        scoped_second = await ingest_external(
            sessions,
            server_id=SERVER_A,
            title="t2",
            source="s",
            dedup_key="k",
            now=NOW + timedelta(minutes=5),
        )
        assert (first.disposition, second.disposition) == ("created", "deduped")
        assert (scoped_first.disposition, scoped_second.disposition) == (
            "created",
            "deduped",
        )
        assert second.alert_id == first.alert_id  # the committed row
        assert scoped_second.alert_id == scoped_first.alert_id
        assert second.delivery == "skipped"  # nothing re-delivered
        rows = await _alerts(sessions)
        assert len(rows) == 2  # one global + one server-scoped

    async def test_global_and_server_rows_of_same_key_never_collide(self, sessions):
        global_outcome = await ingest_external(
            sessions, title="global", source="s", dedup_key="k", now=NOW
        )
        scoped_outcome = await ingest_external(
            sessions,
            server_id=SERVER_A,
            title="scoped",
            source="s",
            dedup_key="k",
            now=NOW,
        )
        assert (global_outcome.disposition, scoped_outcome.disposition) == (
            "created",
            "created",
        )
        assert global_outcome.dedup_key == scoped_outcome.dedup_key
        rows = await _alerts(sessions)
        assert len(rows) == 2  # composite arbiter + partial index both allow it
        assert {r.server_id for r in rows} == {None, uuid.UUID(SERVER_A)}

    async def test_omitted_dedup_key_is_unique_per_message(self, sessions):
        first = await ingest_external(sessions, title="one", now=NOW)
        second = await ingest_external(
            sessions, title="two", now=NOW + timedelta(seconds=1)
        )
        assert (first.disposition, second.disposition) == ("created", "created")
        assert first.dedup_key != second.dedup_key
        assert all(
            key.startswith("external:") and len(key) == len("external:") + 32
            for key in (first.dedup_key, second.dedup_key)
        )
        assert len(await _alerts(sessions)) == 2

    async def test_external_bypasses_quiet_hours_and_cooldown(self, sessions):
        # The writer takes no policy/channels at all (message semantics, not
        # condition semantics): a finding notice at the same instant defers
        # inside the quiet window while the external push lands delivered.
        await _insert_finding(sessions, FINDING_1, SERVER_A, severity="warning")
        quiet = QuietHours(
            start=time_from(21, 0), end=time_from(23, 0), tzinfo=timezone.utc
        )
        deferred = (
            await evaluate_findings(
                sessions,
                uuid.UUID(SERVER_A),
                now=NOW,  # 22:00 UTC → inside the window
                policy=_quiet_policy(quiet=quiet),
                channels={"webhook": CountingChannel("webhook")},
            )
        )[0]
        assert deferred.delivery == "pending"
        pushed = await ingest_external(
            sessions,
            title="push during quiet hours",
            severity="warning",
            source="s",
            dedup_key="q1",
            now=NOW,
        )
        assert pushed.delivery == "delivered"
        rows = {r.origin: r for r in await _alerts(sessions)}
        assert rows["external"].delivery_status == "delivered"
        assert rows["external"].delivered_at == NOW
        assert rows["finding"].delivery_status == "pending"  # unchanged

    async def test_bounds_fail_closed_value_free_before_any_write(self, sessions):
        good = dict(title="ok", source="s", dedup_key="k", body="fine")
        bad_cases = [
            dict(good, title="   "),  # blank title
            dict(good, title="OVERBOUNDS-TITLE" * 20),  # > 200 chars
            dict(good, body=""),  # empty body violates the CHECK bound
            dict(good, body="OVERBOUNDS-BODY" * 400),  # > 4096 chars
            dict(good, source="   "),  # blank source
            dict(good, source="OVERBOUNDS-SRC" * 8),  # > 64 chars
            dict(good, dedup_key="   "),  # blank dedup key
            dict(good, dedup_key="OVERBOUNDS-KEY" * 20),  # > 256 chars
            dict(good, severity="fatal"),  # outside the frozen vocabulary
            dict(good, title=None),
        ]
        for case in bad_cases:
            with pytest.raises(NotificationError) as err:
                await ingest_external(sessions, **case, now=NOW)
            assert err.value.code == NotificationErrorCode.INVALID_DRAFT
            # Value-free: none of the rejected content leaks into the error.
            for marker in ("OVERBOUNDS", "fatal"):
                assert marker not in str(err.value)
        assert await _alerts(sessions) == []  # fail-closed before any write

    async def test_invalid_server_id_is_rejected_value_free(self, sessions):
        with pytest.raises(NotificationError):
            await ingest_external(
                sessions, server_id="not-a-uuid", title="t", now=NOW
            )
        assert await _alerts(sessions) == []

    async def test_oversized_context_ref_is_truncated_not_rejected(self, sessions):
        await ingest_external(
            sessions, title="t", context_ref="r" * 600, now=NOW
        )
        row = (await _alerts(sessions))[0]
        assert row.context_ref == "r" * 511 + "…"
        assert len(row.context_ref) == 512

    async def test_concurrent_global_replay_yields_one_row(self, sessions):
        # Two concurrent writers race the same global logical key: the
        # partial unique index arbitrates — exactly one committed row, one
        # created + one deduped outcome pointing at it, never a duplicate.
        outcomes = await asyncio.gather(
            ingest_external(
                sessions,
                title="first",
                source="race-src",
                dedup_key="race-key",
                now=NOW,
            ),
            ingest_external(
                sessions,
                title="second",
                source="race-src",
                dedup_key="race-key",
                now=NOW,
            ),
        )
        assert sorted(o.disposition for o in outcomes) == ["created", "deduped"]
        created, deduped = outcomes
        assert created.alert_id == deduped.alert_id  # the one committed row
        rows = await _alerts(sessions)
        assert len(rows) == 1
        assert rows[0].dedup_key == _external_key("race-src", "race-key")
        assert rows[0].delivery_status == "delivered"
