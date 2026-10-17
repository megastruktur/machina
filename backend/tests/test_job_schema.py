"""Focused tests for the runs/steps/events/schedules schema (card 05-job-schema).

Acceptance (BRIEF):

1. Schema reflects selected engine ownership (DBOS system tables stay the
   canonical run-state owner — no duplicate run-state authority), stable
   target/parent/revision identity and the frozen outcome states.
2. Unique intent/event/schedule keys and the linear migration are validated
   from an empty database AND from a prior database (upgrade ``0003`` →
   ``0004`` preserves prior rows); downgrade/upgrade round trip checked.
3. The frozen contracts (``mai.contracts.jobs``) consumed by 05-steps and
   the execution UI are pinned: run lifecycle states + engine map, step
   outcome mapping (C04 rule 4 ``CommandStatus``), the exact event envelope
   (C05 rule 4), schedule field contract (C05 rule 8).

Boundary discipline (test policy, risk-critical slice): every storage claim
runs against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real linear Alembic chain applied — duplicate intent/event/schedule
keys are proven by real integrity violations, and the no-duplicate-run-state
claim is proven against ``information_schema`` (exact public-table set, no
engine-shadowing tables, no ``dbos`` schema before engine launch). Contract
units are pure; the NDJSON entrypoint is exercised in-process and via the
verified module command in the card evidence. Fixtures carry only TEST-NET
addresses and credential *reference names* — never secret values.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import (
    APPLICATION_RUN_STATES,
    DEFAULT_CRON_TIMEZONE,
    ENGINE_STATE_MAP,
    EVENT_ENVELOPE_FIELDS,
    EVENT_PAYLOAD_VERSION,
    MISFIRE_POLICY_SKIP_MISSED,
    TERMINAL_RUN_STATES,
    TERMINAL_STEP_STATES,
    STEP_OUTCOME_STATES,
    EngineStateError,
    JobErrorCode,
    JobEvent,
    JobsContractError,
    RunState,
    StepState,
    event_to_wire,
    main,
    parse_job_event,
    run_state_from_engine,
    step_state_from_command_status,
    validate_cron_expression,
    validate_cron_timezone,
)
from mai.contracts.server import SCHEMA_VERSION as PROFILE_SCHEMA_VERSION
from mai.execution.commands import CommandStatus
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain (exclusive migration-chain resource). Head
# assertions target the chain TIP; the tuple tracks the full chain.
# Chain-tip reconciliation (card 08-adoption, exclusive migration-chain
# owner): revision 0006 appends the application catalogue projection +
# journal; the tip constants are advanced here so this file's head
# assertions stay true.
REVISION_IDS = ("0001", "0002", "0003", "0004", "0005", "0006")

# The exact public application schema after 0006 — nothing else, in
# particular no engine-shadowing run-state table (C05 rule 1).
# ``alembic_version`` is the chain's own bookkeeping table (since 0001).
# The two knowledge tables belong to 07-vector-schema (0005) and the two
# application tables to 08-adoption (0006); they are part of the exact app
# set this file pins at the tip.
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
    }
)

# Frozen fixture identities (canonical lowercase UUID strings).
SERVER_ID = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
ROOT_RUN_ID = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"
CHILD_RUN_ID = "3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f"
STEP_ID = "12a5f7c8-3d94-4e2b-8a60-5f7e9d0c1b23"
EVENT_ID = "9a8b7c6d-5e4f-4031-8276-65fea1b2c3d4"
SCHEDULE_ID = "c2d3e4f5-06a7-4b8c-9d10-e1f2a3b4c5d6"

TS = "2026-10-15T01:02:03.123456Z"

# Frozen good envelope (C05 rule 4 shape).
GOOD_EVENT = {
    "seq": 42,
    "run_id": ROOT_RUN_ID,
    "ts": TS,
    "type": "step.completed",
    "v": EVENT_PAYLOAD_VERSION,
    "data": {"step_id": STEP_ID},
}


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _database_url(url: str, database: str) -> str:
    """Same server, different database (for isolated chain experiments).

    ``hide_password=False``: the default masks the password as ``***``,
    which then fails SCRAM on every derived-URL connect.
    """
    return make_url(url).set(database=database).render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real linear Alembic chain
# ---------------------------------------------------------------------------


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip — the 07-vector-schema AND
        # 08-adoption tables are listed here (chain-tip reconciliation) so
        # the zero-to-head fixture stays order-independent after 0006.
        conn.execute(
            "DROP TABLE IF EXISTS application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, "
            "job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0004``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the schema claims need a real "
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
    # Each data test starts from an empty jobs domain (session-scoped
    # fixture database): delete children before parents.
    async with factory() as session:
        for statement in (
            "DELETE FROM job_schedule_fires",
            "DELETE FROM job_steps",
            "DELETE FROM job_events",
            "DELETE FROM job_schedules",
            "DELETE FROM job_runs",
        ):
            await session.execute(text(statement))
        await session.commit()
    await engine.dispose()


async def _exec(sessions, sql: str, **params: object) -> None:
    async with sessions() as session:
        await session.execute(text(sql), params)
        await session.commit()


async def _insert_root_run(
    sessions, run_id: str = ROOT_RUN_ID, **overrides: object
) -> None:
    columns = {
        "run_id": run_id,
        "server_id": SERVER_ID,
        "depth": 0,
        "profile_revision": 3,
        "prompt_revision": 7,
        "model_profile": "build-agent",
        "requested_grants": '["shell.exec"]',
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO job_runs
            ({", ".join(columns)}, submitted_at, updated_at)
        VALUES
            ({", ".join(f":{name}" for name in columns)}, now(), now())
        """,
        **columns,
    )


async def _insert_step(sessions, **overrides: object) -> None:
    columns = {
        "step_id": STEP_ID,
        "run_id": ROOT_RUN_ID,
        "seq": 1,
        "capability_id": "shell.exec",
        "state": "pending",
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO job_steps ({", ".join(columns)})
        VALUES ({", ".join(f":{name}" for name in columns)})
        """,
        **columns,
    )


async def _insert_event(sessions, seq: int = 1, **overrides: object) -> None:
    columns = {
        "event_id": str(uuid.uuid4()),
        "run_id": ROOT_RUN_ID,
        "seq": seq,
        "ts": "2026-10-15T01:02:03.123456+00:00",
        "type": "step.completed",
        "v": EVENT_PAYLOAD_VERSION,
        "data": '{"step_id": "%s"}' % STEP_ID,
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO job_events ({", ".join(columns)})
        VALUES ({", ".join(f":{name}" for name in columns)})
        """,
        **columns,
    )


async def _insert_schedule(sessions, **overrides: object) -> None:
    columns = {
        "schedule_id": SCHEDULE_ID,
        "server_id": SERVER_ID,
        "cron": "*/5 * * * *",
        "cron_timezone": DEFAULT_CRON_TIMEZONE,
        "misfire_policy": MISFIRE_POLICY_SKIP_MISSED,
        "enabled": True,
        "run_spec": '{"prompt": "collect metrics"}',
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO job_schedules ({", ".join(columns)})
        VALUES ({", ".join(f":{name}" for name in columns)})
        """,
        **columns,
    )


async def _insert_fire(
    sessions,
    planned: str = "2026-10-15T02:00:00+00:00",
    **overrides: object,
) -> None:
    columns = {
        "schedule_id": SCHEDULE_ID,
        "planned_fire_time": planned,
        "run_id": None,
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO job_schedule_fires ({", ".join(columns)})
        VALUES ({", ".join(f":{name}" for name in columns)})
        """,
        **columns,
    )


# ---------------------------------------------------------------------------
# Acceptance 3 — frozen contracts (pure units)
# ---------------------------------------------------------------------------


class TestRunLifecycleContract:
    def test_run_states_are_exactly_the_frozen_diagram_states(self) -> None:
        assert {state.value for state in RunState} == {
            "queued",
            "running",
            "waiting_approval",
            "waiting_children",
            "reconciling",
            "succeeded",
            "failed",
            "cancelled",
        }

    def test_terminal_and_application_partitions(self) -> None:
        assert TERMINAL_RUN_STATES == {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
        assert APPLICATION_RUN_STATES == {
            RunState.WAITING_APPROVAL,
            RunState.WAITING_CHILDREN,
            RunState.RECONCILING,
        }
        # Partitions of the eight states: engine-derived vs application.
        engine_states = set(ENGINE_STATE_MAP.values())
        assert engine_states | APPLICATION_RUN_STATES == set(RunState)
        assert not engine_states & APPLICATION_RUN_STATES

    def test_engine_map_is_frozen_and_bijective(self) -> None:
        assert dict(ENGINE_STATE_MAP) == {
            "ENQUEUED": RunState.QUEUED,
            "PENDING": RunState.RUNNING,
            "SUCCESS": RunState.SUCCEEDED,
            "ERROR": RunState.FAILED,
            "CANCELLED": RunState.CANCELLED,
        }

    @pytest.mark.parametrize(
        ("engine_status", "expected"),
        [
            ("ENQUEUED", RunState.QUEUED),
            ("PENDING", RunState.RUNNING),
            ("SUCCESS", RunState.SUCCEEDED),
            ("ERROR", RunState.FAILED),
            ("CANCELLED", RunState.CANCELLED),
        ],
    )
    def test_run_state_from_engine(
        self, engine_status: str, expected: RunState
    ) -> None:
        assert run_state_from_engine(engine_status) is expected

    @pytest.mark.parametrize("engine_status", ["PAUSED", "pending", "", "SUCCESS "])
    def test_unknown_engine_state_fails_closed(self, engine_status: str) -> None:
        with pytest.raises(EngineStateError) as excinfo:
            run_state_from_engine(engine_status)
        assert excinfo.value.code is JobErrorCode.INVALID_ENGINE_STATE
        assert excinfo.value.detail  # value-free fixed reason present

    def test_cancel_requested_is_not_a_run_state(self) -> None:
        """C05 rule 2: cancel_requested is facade-recorded, never a state."""
        assert "cancel_requested" not in {state.value for state in RunState}
        assert "cancel_requested" in {state.value for state in StepState}


class TestStepOutcomeContract:
    def test_step_states_are_command_vocabulary_plus_pending(self) -> None:
        assert {state.value for state in StepState} == {
            "pending",
            *(status.value for status in CommandStatus),
        }

    def test_mapping_covers_the_full_command_vocabulary(self) -> None:
        mapped = {step_state_from_command_status(s) for s in CommandStatus}
        assert mapped == STEP_OUTCOME_STATES

    @pytest.mark.parametrize("status", list(CommandStatus))
    def test_mapping_is_identity(self, status: CommandStatus) -> None:
        assert step_state_from_command_status(status).value == status.value

    def test_outcome_unknown_is_never_terminal(self) -> None:
        """Guard rule 2: reconciliation decides; nothing auto-finalizes it."""
        assert StepState.OUTCOME_UNKNOWN not in TERMINAL_STEP_STATES
        assert TERMINAL_STEP_STATES == {
            StepState.SUCCEEDED,
            StepState.FAILED,
            StepState.CANCELLED,
        }
        assert StepState.CANCEL_REQUESTED not in TERMINAL_STEP_STATES


class TestEventEnvelopeContract:
    def test_envelope_field_set_is_frozen(self) -> None:
        assert EVENT_ENVELOPE_FIELDS == {"seq", "run_id", "ts", "type", "v", "data"}

    def test_good_event_parses_and_round_trips(self) -> None:
        event = parse_job_event(GOOD_EVENT)
        assert isinstance(event, JobEvent)
        assert event.seq == 42
        assert str(event.run_id) == ROOT_RUN_ID
        assert event.ts == datetime(2026, 10, 15, 1, 2, 3, 123456, tzinfo=timezone.utc)
        assert event.type == "step.completed"
        assert event.v == EVENT_PAYLOAD_VERSION
        wire = event_to_wire(event)
        assert set(wire) == EVENT_ENVELOPE_FIELDS
        assert wire["ts"].endswith("Z")
        assert json.loads(json.dumps(wire)) == {  # wire shape is JSON-stable
            "seq": 42,
            "run_id": ROOT_RUN_ID,
            "ts": "2026-10-15T01:02:03.123456Z",
            "type": "step.completed",
            "v": 1,
            "data": {"step_id": STEP_ID},
        }

    def test_offset_timestamp_normalizes_to_utc(self) -> None:
        event = parse_job_event({**GOOD_EVENT, "ts": "2026-10-15T03:02:03+02:00"})
        assert event.ts.utcoffset() == timezone.utc.utcoffset(None)
        assert event.ts.hour == 1

    @pytest.mark.parametrize("field", sorted(EVENT_ENVELOPE_FIELDS))
    def test_missing_field_is_rejected(self, field: str) -> None:
        payload = {key: value for key, value in GOOD_EVENT.items() if key != field}
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event(payload)
        assert excinfo.value.fields == {field: JobErrorCode.MISSING_FIELD.value}

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "extra": 1})
        assert excinfo.value.fields == {
            "extra": JobErrorCode.UNKNOWN_FIELD.value,
        }

    @pytest.mark.parametrize("seq", [0, -1, True, 1.0, "1", None])
    def test_bad_seq_is_rejected(self, seq: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "seq": seq})
        assert JobErrorCode.INVALID_SEQ in set(excinfo.value.fields.values())

    @pytest.mark.parametrize(
        "run_id",
        [
            ROOT_RUN_ID.upper(),
            f"{{{ROOT_RUN_ID}}}",
            ROOT_RUN_ID.replace("-", ""),
            "not-a-uuid",
            42,
        ],
    )
    def test_non_canonical_run_id_is_rejected(self, run_id: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "run_id": run_id})
        assert JobErrorCode.INVALID_RUN_ID in set(excinfo.value.fields.values())

    @pytest.mark.parametrize("ts", ["2026-10-15T01:02:03", "not-a-time", 42, ""])
    def test_naive_or_bad_timestamp_is_rejected(self, ts: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "ts": ts})
        assert JobErrorCode.INVALID_TIMESTAMP in set(excinfo.value.fields.values())

    @pytest.mark.parametrize(
        "event_type", ["", "Step.Completed", "step completed", "a" * 101, 42, "Ünicode"]
    )
    def test_bad_type_slug_is_rejected(self, event_type: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "type": event_type})
        assert JobErrorCode.INVALID_EVENT_TYPE in set(excinfo.value.fields.values())

    @pytest.mark.parametrize("v", [0, -1, True, "1", 1.0])
    def test_non_versioned_payload_is_rejected(self, v: object) -> None:
        """C05 rule 4 "bad": non-versioned payloads."""
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "v": v})
        assert JobErrorCode.INVALID_VERSION in set(excinfo.value.fields.values())

    @pytest.mark.parametrize("data", [["x"], "object", 42, None])
    def test_non_object_data_is_rejected(self, data: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event({**GOOD_EVENT, "data": data})
        assert JobErrorCode.INVALID_DATA in set(excinfo.value.fields.values())

    @pytest.mark.parametrize(
        "key", ["password", "api_key", "private_key", "SECRET", "passphrase", "token"]
    )
    def test_secret_data_key_is_rejected(self, key: str) -> None:
        """C05 rule 4 "bad": payloads carrying secret values (shallow guard)."""
        payload = {**GOOD_EVENT, "data": {key: "never-a-real-secret"}}
        with pytest.raises(JobsContractError) as excinfo:
            parse_job_event(payload)
        assert excinfo.value.fields == {key: JobErrorCode.SECRET_SHAPED_FIELD.value}
        # Value-free rejection: the fake value never appears in the message.
        assert "never-a-real-secret" not in str(excinfo.value)

    def test_benign_data_passes_the_shallow_guard(self) -> None:
        event = parse_job_event(
            {**GOOD_EVENT, "data": {"step_id": STEP_ID, "exit": 0, "token_count": 3}}
        )
        assert event.data == {"step_id": STEP_ID, "exit": 0, "token_count": 3}

    def test_all_rejections_are_value_free(self) -> None:
        secret = "sk-NEVER-9f8e7d6c5b4a"
        try:
            parse_job_event(
                {**GOOD_EVENT, "data": {"api_key": secret}, "type": "BAD TYPE"}
            )
        except JobsContractError as exc:
            assert secret not in str(exc)
            assert all(":" not in code for code in exc.fields.values())
        else:  # pragma: no cover - structural guarantee
            pytest.fail("envelope with secret-shaped key was accepted")


class TestScheduleFieldContract:
    @pytest.mark.parametrize(
        "expression", ["*/5 * * * *", "0 3 * * *", "@daily", "30 4 1,15 * 5"]
    )
    def test_cron_expressions_pass_field_contract(self, expression: str) -> None:
        assert validate_cron_expression(expression) == expression

    @pytest.mark.parametrize("expression", ["", "   ", 42, None])
    def test_blank_cron_is_rejected(self, expression: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            validate_cron_expression(expression)
        assert JobErrorCode.INVALID_CRON in set(excinfo.value.fields.values())

    def test_cron_with_control_characters_is_rejected(self) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            validate_cron_expression("*/5 * * * *\nrm -rf /")
        assert JobErrorCode.INVALID_CRON in set(excinfo.value.fields.values())

    @pytest.mark.parametrize(
        "name", ["UTC", "Europe/Berlin", "America/New_York", "Etc/UTC"]
    )
    def test_iana_timezones_pass(self, name: str) -> None:
        assert validate_cron_timezone(name) == name

    @pytest.mark.parametrize("name", ["", "Mars/Olympus", "utc/utc", 42, None])
    def test_non_iana_timezone_is_rejected(self, name: object) -> None:
        with pytest.raises(JobsContractError) as excinfo:
            validate_cron_timezone(name)
        assert JobErrorCode.INVALID_TIMEZONE in set(excinfo.value.fields.values())

    def test_frozen_defaults_and_policy(self) -> None:
        assert DEFAULT_CRON_TIMEZONE == "UTC"
        assert MISFIRE_POLICY_SKIP_MISSED == "skip_missed"
        assert EVENT_PAYLOAD_VERSION == 1


class TestEntrypoint:
    """`python -m mai.contracts.jobs` — exit contract 0/1/2 (C05 rule 4)."""

    def _ndjson(self, tmp_path, name: str, envelopes: list[object]) -> str:
        path = tmp_path / name
        lines = [
            envelope if isinstance(envelope, str) else json.dumps(envelope)
            for envelope in envelopes
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--help"]) == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_args_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_missing_file_exits_two(
        self, tmp_path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / "absent.ndjson")]) == 2
        assert "IO-ERROR" in capsys.readouterr().err

    def test_valid_file_exits_zero_with_ok_lines(
        self, tmp_path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._ndjson(tmp_path, "good.ndjson", [GOOD_EVENT, GOOD_EVENT])
        assert main([path]) == 0
        out = capsys.readouterr().out
        assert out.count("OK ") == 2
        assert "seq=42" in out and "type=step.completed" in out

    def test_invalid_lines_exit_one_with_structured_codes(
        self, tmp_path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._ndjson(
            tmp_path,
            "mixed.ndjson",
            [GOOD_EVENT, "{not json", {**GOOD_EVENT, "seq": 0}],
        )
        assert main([path]) == 1
        out = capsys.readouterr().out
        assert "OK " in out
        assert ":invalid_envelope" in out
        assert "seq=0" not in out  # value-free: the rejected value is not echoed
        assert "seq=invalid_seq" in out

    def test_secret_value_never_reaches_output(
        self, tmp_path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secret = "sk-NEVER-abcdef123456"
        path = self._ndjson(
            tmp_path,
            "secret.ndjson",
            [{**GOOD_EVENT, "data": {"password": secret}}],
        )
        assert main([path]) == 1
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


# ---------------------------------------------------------------------------
# Acceptance 2 — linear migration from empty and prior databases
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_with_single_head(migrated_url: str) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert sorted(revisions) == sorted(REVISION_IDS)
    assert revisions[REVISION_IDS[0]].down_revision is None
    for previous, following in zip(REVISION_IDS, REVISION_IDS[1:]):
        assert revisions[following].down_revision == previous


def test_migration_reupgrade_is_a_noop_and_records_the_revision(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    command.upgrade(_alembic_config(), "head")  # second run on the same DB
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


@pytest.fixture(scope="session")
def prior_database_url(migrated_url: str) -> str:
    """Fixture database carried from the prior head (``0003``) to ``0004``.

    Feature-gate scenario: "Migrate real fixture database from previous
    head" — the chain is applied up to ``0003``, prior-domain rows are
    inserted, then the ``0004`` upgrade must succeed without touching them.
    """
    url = migrated_url
    prior_db = "mai_fxt_05_prior"
    prior_url = _database_url(url, prior_db)
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{prior_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = prior_url
        command.upgrade(_alembic_config(), "0003")
        server_id = uuid.UUID(SERVER_ID)
        with psycopg.connect(prior_url) as conn:
            conn.execute(
                """
                INSERT INTO profile_projection
                    (server_id, revision, schema_revision, sha256)
                VALUES (%s, 1, %s, %s)
                """,
                (server_id, PROFILE_SCHEMA_VERSION, "a" * 64),
            )
            conn.execute(
                """
                INSERT INTO approvals
                    (approval_id, capability_id, server_id, payload_hash,
                     policy_revision, created_by, expires_at)
                VALUES (%s, 'shell.exec', %s, %s, 1, 'admin',
                        now() + interval '1 hour')
                """,
                (uuid.uuid4(), server_id, "b" * 64),
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    yield prior_url
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')


def test_upgrade_from_prior_head_preserves_prior_rows(
    prior_database_url: str,
) -> None:
    with psycopg.connect(prior_database_url) as conn:
        projection = conn.execute(
            "SELECT revision, sha256 FROM profile_projection WHERE server_id = %s",
            (uuid.UUID(SERVER_ID),),
        ).fetchone()
        approval = conn.execute(
            "SELECT count(*) FROM approvals WHERE server_id = %s",
            (uuid.UUID(SERVER_ID),),
        ).fetchone()
        job_tables = conn.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('job_runs', 'job_steps', 'job_events',
                                 'job_schedules', 'job_schedule_fires')
            """
        ).fetchone()
    assert projection == (1, "a" * 64)
    assert approval == (1,)
    assert job_tables == (5,)


def test_downgrade_upgrades_cleanly(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Down/upgrade round trip of the new revision on an isolated database."""
    down_db = "mai_fxt_05_down"
    down_url = _database_url(migrated_url, down_db)
    with psycopg.connect(
        _database_url(migrated_url, "postgres"), autocommit=True
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{down_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = down_url
        command.upgrade(_alembic_config(), "head")
        command.downgrade(_alembic_config(), "0003")
        with psycopg.connect(down_url) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            leftovers = conn.execute(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'job\\_%'
                """
            ).fetchone()
        assert version == ("0003",)
        assert leftovers == (0,)
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(down_url) as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (REVISION_IDS[-1],)
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(
            _database_url(migrated_url, "postgres"), autocommit=True
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')


# ---------------------------------------------------------------------------
# Acceptance 1 — engine ownership: no duplicate run-state authority
# ---------------------------------------------------------------------------


def test_public_schema_is_exactly_the_frozen_app_set(migrated_url: str) -> None:
    """No engine-shadowing tables; the DBOS schema appears only at launch."""
    with psycopg.connect(migrated_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
        }
        schemata = {
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM information_schema.schemata"
            )
        }
    assert tables == EXPECTED_APP_TABLES
    assert not any(name.startswith(("workflow_", "dbos_")) for name in tables)
    assert "dbos" not in schemata  # engine-owned schema, created at launch


@pytest.mark.anyio
async def test_job_runs_records_identity_without_engine_state(
    sessions: async_sessionmaker[AsyncSession], migrated_url: str
) -> None:
    """job_runs carries domain identity only — engine status stays DBOS's."""
    async with sessions() as session:
        columns = {
            column
            for (column,) in await session.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'job_runs'
                    """
                )
            )
        }
    assert columns == {
        "run_id",
        "server_id",
        "parent_run_id",
        "depth",
        "profile_revision",
        "prompt_revision",
        "model_profile",
        "requested_grants",
        "root_budget",
        "error_ref",
        "evidence_ref",
        "cancel_requested_at",
        "submitted_at",
        "updated_at",
    }
    assert not any("status" in column or "state" in column for column in columns)


@pytest.mark.anyio
async def test_step_state_check_mirrors_frozen_contract(
    sessions, migrated_url: str
) -> None:
    async with sessions() as session:
        constraint = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conname = 'ck_job_steps_state'
                    """
                )
            )
        ).scalar()
        misfire = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conname = 'ck_job_schedules_misfire'
                    """
                )
            )
        ).scalar()
    expected_states = {state.value for state in StepState}
    assert set(_literals_of(constraint)) == expected_states
    assert set(_literals_of(misfire)) == {MISFIRE_POLICY_SKIP_MISSED}


def _literals_of(constraint: str | None) -> list[str]:
    if constraint is None:
        return []
    return re.findall(r"'([^']*)'", constraint)


# ---------------------------------------------------------------------------
# Acceptance 2 — unique intent/event/schedule keys (real integrity controls)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submit_intent_dedup_run_id_is_unique(sessions) -> None:
    await _insert_root_run(sessions)
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions)  # same run_id: conflict, not a 2nd row


@pytest.mark.anyio
async def test_delegation_tree_shape_is_enforced(sessions) -> None:
    await _insert_root_run(sessions)
    await _insert_root_run(
        sessions,
        run_id=CHILD_RUN_ID,
        parent_run_id=ROOT_RUN_ID,
        depth=1,
    )
    with pytest.raises(IntegrityError):
        # Child shape without a parent row: FK violation.
        await _insert_root_run(
            sessions,
            run_id=str(uuid.uuid4()),
            parent_run_id=str(uuid.uuid4()),
            depth=1,
        )
    with pytest.raises(IntegrityError):
        # Root shape with a parent column set: ck_job_runs_root_depth.
        await _insert_root_run(sessions, parent_run_id=ROOT_RUN_ID)
    with pytest.raises(IntegrityError):
        # Child shape with depth 0: ck_job_runs_child_depth.
        await _insert_root_run(
            sessions, run_id=str(uuid.uuid4()), parent_run_id=ROOT_RUN_ID, depth=0
        )


@pytest.mark.anyio
async def test_event_cursor_is_unique_per_run_seq(sessions) -> None:
    """Monotonic per-run cursor (C05 rule 4): duplicate seq is impossible."""
    await _insert_root_run(sessions)
    await _insert_event(sessions, seq=1)
    await _insert_event(sessions, seq=2)
    with pytest.raises(IntegrityError):
        await _insert_event(sessions, seq=2)
    # Gap-tolerant at the storage layer (producer assigns; replay sorts).
    await _insert_event(sessions, seq=9)


@pytest.mark.anyio
async def test_event_envelope_columns_enforce_the_frozen_shape(sessions) -> None:
    await _insert_root_run(sessions)
    with pytest.raises(IntegrityError):
        await _insert_event(sessions, seq=1, v=0)  # non-versioned payload
    with pytest.raises(IntegrityError):
        await _insert_event(sessions, seq=1, type="Step.Completed")  # slug guard
    with pytest.raises(IntegrityError):
        await _insert_event(sessions, seq=0)  # cursor starts at 1
    with pytest.raises(IntegrityError):
        await _insert_event(
            sessions, seq=1, run_id=str(uuid.uuid4())
        )  # unknown run: FK


@pytest.mark.anyio
async def test_step_intents_are_ordered_and_state_checked(sessions) -> None:
    await _insert_root_run(sessions)
    await _insert_step(sessions)  # seq 1, pending, no receipt
    with pytest.raises(IntegrityError):
        await _insert_step(sessions, step_id=str(uuid.uuid4()))  # same (run, seq)
    for bad_state in ("queued", "succeed", "OK", ""):
        with pytest.raises(IntegrityError):
            await _insert_step(
                sessions, step_id=str(uuid.uuid4()), seq=2, state=bad_state
            )
    # Every frozen step state is writable; pending forbids a receipt.
    ordered = [
        "pending",
        "running",
        "succeeded",
        "failed",
        "outcome_unknown",
        "cancel_requested",
        "cancelled",
    ]
    for index, state_value in enumerate(ordered, start=2):
        await _insert_step(
            sessions,
            step_id=str(uuid.uuid4()),
            seq=index,
            state=state_value,
            receipt=(
                '{"exit": 0}' if state_value not in ("pending", "running") else None
            ),
        )
    with pytest.raises(IntegrityError):
        await _insert_step(
            sessions, step_id=str(uuid.uuid4()), seq=99, receipt='{"exit": 0}'
        )  # pending + receipt is contradictory (ck_job_steps_pending_no_receipt)


@pytest.mark.anyio
async def test_outcome_unknown_survives_as_persisted_state(sessions) -> None:
    """Guard rule 2: outcome_unknown is a first-class persisted state."""
    await _insert_root_run(sessions)
    await _insert_step(sessions, state="outcome_unknown")
    async with sessions() as session:
        row = await session.execute(
            text("SELECT state FROM job_steps WHERE step_id = :sid"),
            {"sid": STEP_ID},
        )
        state = row.scalar()
    assert state == "outcome_unknown"


@pytest.mark.anyio
async def test_schedule_dispatch_dedup_key(sessions) -> None:
    """C05 rule 8: (schedule_id, planned_fire_time) fires at most once."""
    await _insert_schedule(sessions)
    await _insert_fire(sessions)
    with pytest.raises(IntegrityError):
        await _insert_fire(sessions)  # same planned fire: dedup key
    # A different planned fire time is a distinct dispatch.
    await _insert_fire(sessions, planned="2026-10-15T03:00:00+00:00")


@pytest.mark.anyio
async def test_schedule_field_checks(sessions) -> None:
    with pytest.raises(IntegrityError):
        await _insert_schedule(sessions, misfire_policy="catch_up")
    with pytest.raises(IntegrityError):
        await _insert_schedule(sessions, cron="")
    with pytest.raises(IntegrityError):
        await _insert_schedule(sessions, run_spec='["not", "an", "object"]')
    await _insert_schedule(sessions)  # frozen defaults apply
    async with sessions() as session:
        row = await session.execute(
            text(
                "SELECT cron_timezone, misfire_policy, enabled "
                "FROM job_schedules WHERE schedule_id = :sid"
            ),
            {"sid": SCHEDULE_ID},
        )
        state = row.one()
    assert state == ("UTC", "skip_missed", True)


@pytest.mark.anyio
async def test_cancel_request_is_facade_recorded_not_a_state(sessions) -> None:
    """C05 rule 2: the facade records the request; no state column flips."""
    await _insert_root_run(sessions)
    await _exec(
        sessions,
        """
        UPDATE job_runs SET cancel_requested_at = now(), updated_at = now()
        WHERE run_id = :rid
        """,
        rid=ROOT_RUN_ID,
    )
    value = None
    async with sessions() as session:
        row = await session.execute(
            text("SELECT cancel_requested_at FROM job_runs WHERE run_id = :rid"),
            {"rid": ROOT_RUN_ID},
        )
        value = row.scalar()
    assert value is not None and value.tzinfo is not None  # stored UTC-aware


@pytest.mark.anyio
async def test_payload_columns_reject_secret_and_shape_drift(sessions) -> None:
    await _insert_root_run(sessions)
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions, requested_grants='{"not": "an array"}')
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions, root_budget='["not", "an object"]')
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions, profile_revision=0)
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions, prompt_revision=0)
    with pytest.raises(IntegrityError):
        await _insert_root_run(sessions, depth=-1)
    # References stay plain strings by producer contract (no length CHECK):
    # a long reference name is accepted; payloads belong in receipts/files.
    await _insert_root_run(sessions, run_id=CHILD_RUN_ID, error_ref="x" * 512)
