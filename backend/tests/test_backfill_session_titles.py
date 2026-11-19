"""Backfill session-titles script — real-Postgres acceptance tests (§4.4).

Every claim runs against the disposable fixture database (the same
``MAI_TEST_DATABASE_URL`` + linear Alembic chain discipline as the other
session suites). The generator is injected through the documented
``generate=`` seam of :func:`mai.scripts.backfill_session_titles.main` —
a scripted async callable — so NO live LLM provider, no model store and
no credential configuration is involved anywhere.

Proven here:

1. **Untitled session with a user message is titled** — the store row
   carries the generated title and the seam received the raw message.
2. **A session without user entries is never scanned** (the page
   predicate requires one) and keeps ``title IS NULL``.
3. **An already-titled session is never rescanned** and never rewritten.
4. **``--dry-run`` generates and reports but writes NOTHING** — the row
   keeps ``title IS NULL``.
5. **A failing generator leaves that session NULL and the run continues**
   to the next session (value-free ``failed`` counter).
6. **A second run is a no-op** (idempotence: titled sessions left the
   predicate set) — the generator is not even called again.
7. **``--limit`` bounds the run** and the kill-switch
   (``MAI_SESSION_TITLE=0``) disables the script entirely.
8. **The Service Map section never reaches the generator** — the prompt
   source is cut at ``SERVICE_MAP_SECTION_HEADER``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.sessions import (
    SERVICE_MAP_SECTION_HEADER,
    EntryKind,
    SessionSourceKind,
    UserMessageEntry,
)
from mai.database import create_app_engine
from mai.session_store import aappend_entry, acreate_session
from mai.scripts.backfill_session_titles import main as backfill_main

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
FIRST_MESSAGE = "How do I rotate the API key?"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the backfill claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
def factory(migrated_url: str) -> async_sessionmaker[AsyncSession]:
    eng, sessions = create_app_engine(migrated_url)
    yield sessions
    asyncio.run(eng.dispose())


@pytest.fixture(autouse=True)
def _clean_sessions(factory: async_sessionmaker[AsyncSession]):
    """Fresh session-first domain per test (generic tables untouched)."""

    async def _clean() -> None:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    sql_text(
                        "TRUNCATE session_tags, session_provider_artifacts, "
                        "session_admissions, session_runs, session_entries, "
                        "sessions CASCADE"
                    )
                )

    yield
    asyncio.run(_clean())


class _ScriptedGenerator:
    """The injected seam: scripted titles, one per call.

    A ``str`` entry answers that title, ``None`` degrades (failed
    generation) and an ``Exception`` entry RAISES (a generator crash) —
    both must leave the session untitled without aborting the run.
    """

    def __init__(self, script: list[str | None | Exception]) -> None:
        self.messages: list[str] = []
        self._script = list(script)

    async def __call__(self, first_message: str) -> str | None:
        self.messages.append(first_message)
        if not self._script:
            return None
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


async def _seed_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str | None = None,
    with_user: bool = True,
    text: str = FIRST_MESSAGE,
) -> uuid.UUID:
    row = await acreate_session(
        factory,
        server_id=SERVER,
        source=SessionSourceKind.INTERACTIVE,
        title=title,
    )
    if with_user:
        payload = UserMessageEntry(
            kind=EntryKind.USER_MESSAGE,
            text=text,
            client_request_id=uuid.uuid4(),
            source=SessionSourceKind.INTERACTIVE,
        ).model_dump(mode="json")
        await aappend_entry(
            factory,
            session_id=row.session_id,
            server_id=SERVER,
            kind=EntryKind.USER_MESSAGE,
            payload=payload,
            run_id=None,
            durable_source_id=f"turn:{uuid.uuid4()}",
        )
    return row.session_id


def _seed(factory: async_sessionmaker[AsyncSession], **kwargs: object) -> uuid.UUID:
    return asyncio.run(_seed_session(factory, **kwargs))  # type: ignore[arg-type]


def _read_title(url: str, session_id: uuid.UUID) -> str | None:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT title FROM sessions WHERE session_id = %s",
            (session_id,),
        ).fetchone()
    assert row is not None, "session row missing"
    return row["title"]


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    argv: list[str],
    generate: _ScriptedGenerator,
) -> dict[str, object]:
    monkeypatch.setenv("MAI_DATABASE_URL", url)
    assert backfill_main(argv, generate=generate) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_backfill_titles_untitled_session_with_user_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = _seed(factory)
    generator = _ScriptedGenerator(["Deploy the stack"])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], generator)
    assert summary["scanned"] == 1
    assert summary["titled"] == 1
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    assert summary["dry_run"] is False
    assert _read_title(migrated_url, session_id) == "Deploy the stack"
    # The seam received the RAW first user message (the only prompt input).
    assert generator.messages == [FIRST_MESSAGE]


def test_session_without_user_message_is_never_scanned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = _seed(factory, with_user=False)
    generator = _ScriptedGenerator([])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], generator)
    assert summary["scanned"] == 0
    assert summary["titled"] == 0
    assert generator.messages == []
    assert _read_title(migrated_url, session_id) is None


def test_already_titled_session_is_never_rescanned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = _seed(factory, title="Manual rename")
    generator = _ScriptedGenerator([])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], generator)
    assert summary["scanned"] == 0
    assert summary["titled"] == 0
    assert generator.messages == []
    assert _read_title(migrated_url, session_id) == "Manual rename"


def test_dry_run_reports_but_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = _seed(factory)
    generator = _ScriptedGenerator(["Deploy the stack"])
    summary = _invoke(monkeypatch, capsys, migrated_url, ["--dry-run"], generator)
    assert summary["dry_run"] is True
    assert summary["scanned"] == 1
    assert summary["titled"] == 1  # the WOULD-BE outcome
    # ...and NOTHING was written.
    assert _read_title(migrated_url, session_id) is None
    assert generator.messages == [FIRST_MESSAGE]


def test_failing_generator_leaves_null_and_run_continues(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed A strictly BEFORE B: oldest-first, the crash lands on A.
    session_a = _seed(factory)
    time.sleep(0.01)
    session_b = _seed(factory)
    generator = _ScriptedGenerator([RuntimeError("provider exploded"), "Recovered title"])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], generator)
    assert summary["scanned"] == 2
    assert summary["failed"] == 1
    assert summary["titled"] == 1
    assert _read_title(migrated_url, session_a) is None
    assert _read_title(migrated_url, session_b) == "Recovered title"


def test_second_run_is_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_one = _seed(factory)
    session_two = _seed(factory, text="Deploy the monitoring stack")
    first = _ScriptedGenerator(["Deploy the stack", "Monitoring stack"])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], first)
    assert summary["titled"] == 2
    # The re-run scans nothing and never calls the generator again.
    second = _ScriptedGenerator([])
    summary = _invoke(monkeypatch, capsys, migrated_url, [], second)
    assert summary["scanned"] == 0
    assert summary["titled"] == 0
    assert second.messages == []
    assert _read_title(migrated_url, session_one) == "Deploy the stack"
    assert _read_title(migrated_url, session_two) == "Monitoring stack"


def test_limit_bounds_processed_sessions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = [_seed(factory) for _ in range(3)]
    generator = _ScriptedGenerator(["Deploy the stack"])
    summary = _invoke(monkeypatch, capsys, migrated_url, ["--limit", "1"], generator)
    assert summary["scanned"] == 1
    assert summary["titled"] == 1
    titled_count = sum(
        1 for sid in seeded if _read_title(migrated_url, sid) is not None
    )
    assert titled_count == 1
    assert len(generator.messages) == 1


def test_kill_switch_disables_the_script(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = _seed(factory)
    generator = _ScriptedGenerator(["Never generated"])
    monkeypatch.setenv("MAI_SESSION_TITLE", "0")
    summary = _invoke(monkeypatch, capsys, migrated_url, [], generator)
    assert summary["disabled"] is True
    assert summary["scanned"] == 0
    assert generator.messages == []
    assert _read_title(migrated_url, session_id) is None


def test_service_map_section_never_reaches_the_generator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _seed(
        factory,
        text=(
            "Fix nginx\n\n"
            + SERVICE_MAP_SECTION_HEADER
            + "\n(@web, @db) full service material here"
        ),
    )
    generator = _ScriptedGenerator(["Fix nginx"])
    _invoke(monkeypatch, capsys, migrated_url, [], generator)
    # The prompt source is cut at the marker: only the user's own words.
    assert generator.messages == ["Fix nginx"]


def test_missing_database_url_is_a_value_free_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MAI_DATABASE_URL", raising=False)
    assert backfill_main([], generate=_ScriptedGenerator([])) == 2
    assert "st_fxt" not in capsys.readouterr().err  # value-free, no DSN echo
