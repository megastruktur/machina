"""Focused direct-agent-default acceptance (backend slice).

A canonical chat turn with NO ``objective_id`` runs ``agent.run_v2``:
the admission transaction starts the objective episode itself (the
tx-scoped objective-store seams), binds the durable run to it, and a
failure rolls the episode back together with the run — no orphan, no
second request, no intake planner. Terminal-episode conversations roll
over into a new episode on their next run; historical legacy
conversations (no binding) stay legacy; General Consult (zero UUID)
gets exactly the zero-UUID scope — no host authority expansion.

Harness: the proven ``test_conversation_api_v2_http`` composition (real
ASGI app + real disposable PostgreSQL + real Alembic chain) through its
imported helpers and fixture functions. Legacy history is seeded through
the internal store seams (``_record_submit_intent`` /
``_record_chat_submission`` with the frozen legacy handler) — there is
deliberately NO public legacy selector.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from mai.api import chat as chat_surface
from mai.contracts.jobs import RunState
from mai.contracts.objectives import (
    ActorKind,
    ObjectiveContractError,
    ObjectiveErrorCode,
    ObjectiveFieldError,
    ObjectiveStatus,
)
from mai.database import create_app_engine
from mai.jobs.lifecycle import RunLifecycle

import test_conversation_api_v2_http as harness

pytestmark = pytest.mark.anyio

GENERAL_CONSULT = uuid.UUID(int=0)


# ---------------------------------------------------------------------------
# fixtures delegating to the reviewed v2 harness composition
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_url() -> str:
    return harness.migrated_url.__wrapped__()  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    return harness.bootstrapped_url.__wrapped__(migrated_url)  # type: ignore[no-any-return]


@pytest.fixture
async def app(bootstrapped_url: str, tmp_path: Path):
    agen = harness.v2_app.__wrapped__(bootstrapped_url, tmp_path)
    try:
        async for fastapi_app in agen:
            yield fastapi_app
    finally:
        await agen.aclose()


@pytest.fixture
async def jar(app) -> dict[str, str]:
    return await harness.jar.__wrapped__(app)  # type: ignore[no-any-return]


@pytest.fixture
async def server_a(app, jar) -> uuid.UUID:
    return await harness.server_a.__wrapped__(app, jar)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# local helpers (DB truth reads + internal legacy seed)
# ---------------------------------------------------------------------------


def _submit_create(
    app,
    jar: dict[str, str],
    server: uuid.UUID,
    crid: uuid.UUID,
    *,
    message: str = "Deploy the fixture service and verify health.",
    **extra: Any,
):
    body: dict[str, Any] = {
        "server_id": str(server),
        "client_request_id": str(crid),
        "message": message,
        "profile": "fast",
    }
    body.update(extra)
    return harness.call_app(
        app,
        "POST",
        "/api/v1/chat/conversations",
        json_body=body,
        headers=harness._mutation_headers(jar),
        cookies=jar,
    )


def _submit_followup(
    app,
    jar: dict[str, str],
    server: uuid.UUID,
    conversation_id: uuid.UUID,
    crid: uuid.UUID,
    *,
    message: str = "Continue the episode.",
    **extra: Any,
):
    body: dict[str, Any] = {
        "server_id": str(server),
        "client_request_id": str(crid),
        "message": message,
        "profile": "fast",
    }
    body.update(extra)
    return harness.call_app(
        app,
        "POST",
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        json_body=body,
        headers=harness._mutation_headers(jar),
        cookies=jar,
    )


def _db(url: str):
    return psycopg.connect(url, row_factory=dict_row)


def _objective_rows(url: str) -> list[dict]:
    with _db(url) as conn:
        return conn.execute(
            "SELECT o.objective_id::text AS objective_id, "
            "o.server_id::text AS server_id, o.status, o.revision, "
            "o.original_request, o.client_request_id::text AS client_request_id, "
            "(SELECT array_agg(s.server_id::text ORDER BY s.server_id::text) "
            " FROM objective_servers s WHERE s.objective_id = o.objective_id "
            ") AS scope "
            "FROM objectives o ORDER BY o.created_at, o.objective_id"
        ).fetchall()


def _bindings(url: str) -> list[dict]:
    with _db(url) as conn:
        return conn.execute(
            "SELECT objective_id::text AS objective_id, "
            "run_id::text AS run_id, handler "
            "FROM objective_runs ORDER BY created_at, run_id"
        ).fetchall()


def _submitted_handler(url: str, run_id: uuid.UUID) -> str | None:
    with _db(url) as conn:
        row = conn.execute(
            "SELECT data->>'handler' AS handler FROM job_events "
            "WHERE run_id = %s AND type = 'chat.submitted' "
            "ORDER BY seq LIMIT 1",
            (run_id,),
        ).fetchone()
    return None if row is None else row["handler"]  # type: ignore[no-any-return]


def _event_types(url: str, run_id: uuid.UUID) -> list[str]:
    with _db(url) as conn:
        rows = conn.execute(
            "SELECT type FROM job_events WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [str(row["type"]) for row in rows]


def _run_state(url: str, run_id: uuid.UUID) -> str | None:
    async def _read() -> str | None:
        engine, sessions = create_app_engine(url)
        try:
            state = await RunLifecycle(sessions).current_state(str(run_id))
            return None if state is None else state.value
        finally:
            await engine.dispose()

    value = harness._run(_read())
    return None if value is None else str(value)


def _settle(url: str, run_id: uuid.UUID) -> None:
    """Settle one queued run through the ONE lifecycle writer."""

    async def _cancel() -> None:
        engine, sessions = create_app_engine(url)
        try:
            lifecycle = RunLifecycle(sessions)
            state = await lifecycle.current_state(str(run_id))
            if state is None or state is RunState.QUEUED:
                if state is None:
                    await lifecycle.record_transition(str(run_id), RunState.QUEUED)
            await lifecycle.record_transition(str(run_id), RunState.CANCELLED)
        finally:
            await engine.dispose()

    harness._run(_cancel())


def _message_rows(url: str, conversation_id: uuid.UUID) -> list[dict]:
    with _db(url) as conn:
        return conn.execute(
            "SELECT seq, role, content, client_request_id::text AS crid "
            "FROM conversation_messages WHERE conversation_id = %s "
            "ORDER BY seq",
            (conversation_id,),
        ).fetchall()


def _detail(app, jar: dict[str, str], server: uuid.UUID, crid: uuid.UUID) -> dict:
    response = harness._run(
        harness.call_app(
            app,
            "GET",
            f"/api/v1/chat/conversations/{crid}?server_id={server}",
            headers=harness._cookies(jar),
        )
    )
    assert response.status == 200, response.body
    return response.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


async def test_create_without_objective_runs_v2_with_automatic_episode(
    app, jar, server_a, bootstrapped_url
) -> None:
    """No-objective create → handler ``agent.run_v2``, ONE objective +
    binding, correct projected original_request, no intake, no legacy
    drain."""
    crid = uuid.uuid4()
    message = "Deploy the fixture service and verify health."
    created = await _submit_create(app, jar, server_a, crid, message=message)
    assert created.status == 201, created.body
    body = created.json()
    assert body["run_id"] == str(crid)
    assert body["disposition"] == "created"

    assert _submitted_handler(bootstrapped_url, crid) == "agent.run_v2"

    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 1
    episode = objectives[0]
    assert episode["server_id"] == str(server_a)
    assert episode["status"] == "active"
    assert episode["scope"] == [str(server_a)]
    assert episode["client_request_id"] == str(crid)
    # The original request is the PROJECTED PERSISTED user content.
    stored = _message_rows(bootstrapped_url, crid)
    assert len(stored) == 1
    assert episode["original_request"] == stored[0]["content"] == message

    bindings = _bindings(bootstrapped_url)
    assert [(b["objective_id"], b["run_id"], b["handler"]) for b in bindings] == [
        (str(episode["objective_id"]), str(crid), "agent.run_v2")
    ]

    types = _event_types(bootstrapped_url, crid)
    assert not [t for t in types if t.startswith("intake")]

    drain = await harness.call_app(
        app, "GET", "/api/v1/objectives/drain-status",
        headers=harness._cookies(jar),
    )
    assert drain.status == 200, drain.body
    doc = drain.json()
    assert doc["legacy"]["total"] == 0
    assert doc["v2"]["total"] == 1

    detail = _detail(app, jar, server_a, crid)
    assert detail["is_legacy"] is False
    assert detail["handler_name"] == "agent.run_v2"
    assert detail["objective_ids"] == [str(episode["objective_id"])]


async def test_replay_dedups_same_episode_and_changed_body_conflicts(
    app, jar, server_a, bootstrapped_url
) -> None:
    """Retry yields the SAME conversation/objective/run; a changed body
    under the same key fails closed."""
    crid = uuid.uuid4()
    first = await _submit_create(app, jar, server_a, crid)
    assert first.status == 201, first.body
    episode_id = _objective_rows(bootstrapped_url)[0]["objective_id"]

    replay = await _submit_create(app, jar, server_a, crid)
    assert replay.status == 200, replay.body
    assert replay.json()["disposition"] == "deduplicated"
    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 1
    assert objectives[0]["objective_id"] == episode_id
    assert len(_bindings(bootstrapped_url)) == 1

    conflict = await _submit_create(
        app, jar, server_a, crid, message="a different body entirely"
    )
    assert conflict.status == 409, conflict.body
    assert harness._error(conflict)["code"] == "client_request_conflict"
    assert len(_objective_rows(bootstrapped_url)) == 1
    assert len(_message_rows(bootstrapped_url, crid)) == 1

    # The mode rides the submission digest: the same key with a changed
    # mode is a different submission (ask/delegated stay explicit, C4).
    mode_conflict = await _submit_create(
        app, jar, server_a, crid, mode="ask"
    )
    assert mode_conflict.status == 409, mode_conflict.body


async def test_admission_fault_rolls_back_objective_and_run(
    app, jar, server_a, bootstrapped_url, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in the in-admission episode creation rolls the objective,
    its binding, the run intent and the submission record back together —
    and the retry heals to the exact same triple (no orphan)."""
    crid = uuid.uuid4()

    def _boom(*args: object, **kwargs: object) -> object:
        raise ObjectiveContractError(
            [
                ObjectiveFieldError(
                    ObjectiveErrorCode.CLIENT_REQUEST_CONFLICT.value,
                    "client_request_id",
                    "injected admission fault",
                )
            ]
        )

    monkeypatch.setattr(chat_surface, "acreate_objective_tx", _boom)
    faulted = await _submit_create(app, jar, server_a, crid)
    assert faulted.status == 409, faulted.body
    assert harness._error(faulted)["code"] == "client_request_conflict"
    monkeypatch.undo()

    with _db(bootstrapped_url) as conn:
        assert conn.execute("SELECT 1 FROM objectives").fetchone() is None
        assert conn.execute("SELECT 1 FROM objective_runs").fetchone() is None
        assert (
            conn.execute(
                "SELECT 1 FROM job_runs WHERE run_id = %s", (crid,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM job_events WHERE run_id = %s", (crid,)
            ).fetchone()
            is None
        )
    # The canonical message is durable store content (C4 re-anchor
    # semantics); the retry deduplicates onto it — never a second row.
    assert len(_message_rows(bootstrapped_url, crid)) == 1

    healed = await _submit_create(app, jar, server_a, crid)
    assert healed.status == 201, healed.body
    assert healed.json()["disposition"] == "created"
    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 1
    assert objectives[0]["client_request_id"] == str(crid)
    assert [
        (b["objective_id"], b["run_id"]) for b in _bindings(bootstrapped_url)
    ] == [(str(objectives[0]["objective_id"]), str(crid))]
    assert len(_message_rows(bootstrapped_url, crid)) == 1
    assert _submitted_handler(bootstrapped_url, crid) == "agent.run_v2"


async def test_active_episode_reuse_and_terminal_rollover(
    app, jar, server_a, bootstrapped_url
) -> None:
    """A conversation bound to an ACTIVE objective reuses it; once the
    episode is terminal the next run creates a NEW objective — old
    history untouched."""
    crid = uuid.uuid4()
    created = await _submit_create(app, jar, server_a, crid)
    assert created.status == 201, created.body
    episode_a = _objective_rows(bootstrapped_url)[0]["objective_id"]

    _settle(bootstrapped_url, crid)

    key2 = uuid.uuid4()
    followup = await _submit_followup(
        app, jar, server_a, crid, key2, message="Continue the episode."
    )
    assert followup.status == 201, followup.body
    assert followup.json()["run_id"] == str(key2)
    assert _submitted_handler(bootstrapped_url, key2) == "agent.run_v2"
    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 1  # ACTIVE episode reused, not duplicated
    assert objectives[0]["objective_id"] == episode_a
    assert sorted(b["run_id"] for b in _bindings(bootstrapped_url)) == sorted(
        [str(crid), str(key2)]
    )

    _settle(bootstrapped_url, key2)

    async def _cancel_episode() -> None:
        engine, sessions = create_app_engine(bootstrapped_url)
        try:
            from mai.agents.objective_store import achange_status

            await achange_status(
                sessions,
                uuid.UUID(str(episode_a)),
                server_id=server_a,
                expected_revision=1,
                to_status=ObjectiveStatus.CANCELLED,
                actor_kind=ActorKind.OPERATOR,
            )
        finally:
            await engine.dispose()

    harness._run(_cancel_episode())

    key3 = uuid.uuid4()
    rollover = await _submit_followup(
        app, jar, server_a, crid, key3, message="Start the next episode."
    )
    assert rollover.status == 201, rollover.body
    assert _submitted_handler(bootstrapped_url, key3) == "agent.run_v2"
    objectives = _objective_rows(bootstrapped_url)
    assert [row["status"] for row in objectives] == ["cancelled", "active"]
    new_id = objectives[1]["objective_id"]
    assert new_id != episode_a
    assert [
        (b["objective_id"], b["run_id"]) for b in _bindings(bootstrapped_url)
    ] == [
        (str(episode_a), str(crid)),
        (str(episode_a), str(key2)),
        (str(new_id), str(key3)),
    ]
    # Old history is untouched: three durable turns, original content intact.
    messages = _message_rows(bootstrapped_url, crid)
    assert [row["content"] for row in messages] == [
        "Deploy the fixture service and verify health.",
        "Continue the episode.",
        "Start the next episode.",
    ]
    detail = _detail(app, jar, server_a, crid)
    assert detail["handler_name"] == "agent.run_v2"
    # The §10.3 read probe lists bindings in objective-id order.
    assert detail["objective_ids"] == sorted([str(episode_a), str(new_id)])

    # Per-conversation ACTIVE episode: after the rollover, the conversation's
    # LIVE episode is the one reused — the earliest (terminal) binding must
    # never hijack selection into re-rolling a fresh episode per turn.
    _settle(bootstrapped_url, key3)
    key4 = uuid.uuid4()
    fourth = await _submit_followup(
        app, jar, server_a, crid, key4, message="Continue the live episode."
    )
    assert fourth.status == 201, fourth.body
    assert _submitted_handler(bootstrapped_url, key4) == "agent.run_v2"
    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 2, "the live episode is reused, not re-rolled"
    assert [(b["objective_id"], b["run_id"]) for b in _bindings(bootstrapped_url)] == [
        (str(episode_a), str(crid)),
        (str(episode_a), str(key2)),
        (str(new_id), str(key3)),
        (str(new_id), str(key4)),
    ]


async def test_replay_after_rollover_replays_run_binding(
    app, jar, server_a, bootstrapped_url
) -> None:
    """A retransmission reconstructs its OWN run's binding — exact under
    any rollover history (the digest must match the RECORDED admission),
    never a false conflict."""
    crid = uuid.uuid4()
    created = await _submit_create(app, jar, server_a, crid)
    assert created.status == 201, created.body
    episode_a = _objective_rows(bootstrapped_url)[0]["objective_id"]

    _settle(bootstrapped_url, crid)
    key2 = uuid.uuid4()
    followup = await _submit_followup(
        app, jar, server_a, crid, key2, message="Second turn."
    )
    assert followup.status == 201, followup.body
    _settle(bootstrapped_url, key2)

    async def _cancel() -> None:
        from mai.agents.objective_store import achange_status

        engine, sessions = create_app_engine(bootstrapped_url)
        try:
            await achange_status(
                sessions,
                uuid.UUID(str(episode_a)),
                server_id=server_a,
                expected_revision=1,
                to_status=ObjectiveStatus.CANCELLED,
                actor_kind=ActorKind.OPERATOR,
            )
        finally:
            await engine.dispose()

    harness._run(_cancel())

    key3 = uuid.uuid4()
    rollover = await _submit_followup(
        app, jar, server_a, crid, key3, message="Rollover turn."
    )
    assert rollover.status == 201, rollover.body
    assert len(_objective_rows(bootstrapped_url)) == 2
    # Replay the ROLLOVER turn itself under the same key: the recorded
    # digest named the NEW episode (this run's own binding) — the replay
    # must deduplicate, never falsely conflict and never re-roll.
    replay = await _submit_followup(
        app, jar, server_a, crid, key3, message="Rollover turn."
    )
    assert replay.status == 200, replay.body
    assert replay.json()["disposition"] == "deduplicated"
    assert len(_objective_rows(bootstrapped_url)) == 2
    assert sorted(b["run_id"] for b in _bindings(bootstrapped_url)) == sorted(
        [str(crid), str(key2), str(key3)]
    )


async def test_explicit_objective_rejections_unchanged(
    app, jar, server_a, bootstrapped_url
) -> None:
    """Explicit-request failure mapping is untouched by the default: a
    foreign-scope reference is the scope-blind 404; a settled episode
    rejects new runs (invalid_status_transition)."""
    missing = await _submit_create(
        app, jar, server_a, uuid.uuid4(),
        objective_id=str(uuid.uuid4()),
    )
    assert missing.status == 404, missing.body
    assert harness._error(missing)["code"] == "objective_not_found"

    created = await _submit_create(app, jar, server_a, uuid.uuid4())
    assert created.status == 201, created.body
    episode = _objective_rows(bootstrapped_url)[0]

    async def _cancel() -> None:
        from mai.agents.objective_store import achange_status

        engine, sessions = create_app_engine(bootstrapped_url)
        try:
            await achange_status(
                sessions,
                uuid.UUID(str(episode["objective_id"])),
                server_id=server_a,
                expected_revision=1,
                to_status=ObjectiveStatus.CANCELLED,
                actor_kind=ActorKind.OPERATOR,
            )
        finally:
            await engine.dispose()

    harness._run(_cancel())

    settled = await _submit_create(
        app, jar, server_a, uuid.uuid4(),
        objective_id=str(episode["objective_id"]),
    )
    assert settled.status == 409, settled.body
    assert harness._error(settled)["code"] == "invalid_status_transition"


async def test_general_consult_zero_scope_runs_v2_without_host_expansion(
    app, jar, bootstrapped_url
) -> None:
    """General Consult (zero UUID) gets the v2 runtime with EXACTLY the
    zero-UUID scope — no host authority expansion."""
    crid = uuid.uuid4()
    created = await _submit_create(app, jar, GENERAL_CONSULT, crid)
    assert created.status == 201, created.body
    assert _submitted_handler(bootstrapped_url, crid) == "agent.run_v2"
    objectives = _objective_rows(bootstrapped_url)
    assert len(objectives) == 1
    assert objectives[0]["server_id"] == str(GENERAL_CONSULT)
    assert objectives[0]["scope"] == [str(GENERAL_CONSULT)]
    with _db(bootstrapped_url) as conn:
        run = conn.execute(
            "SELECT server_id::text AS sid FROM job_runs WHERE run_id = %s",
            (crid,),
        ).fetchone()
    assert run is not None and run["sid"] == str(GENERAL_CONSULT)
    bindings = _bindings(bootstrapped_url)
    assert [(b["objective_id"], b["run_id"]) for b in bindings] == [
        (str(objectives[0]["objective_id"]), str(crid))
    ]


async def test_legacy_seeded_history_stays_legacy(
    app, jar, server_a, bootstrapped_url
) -> None:
    """A historical legacy conversation (no binding) keeps running the
    frozen legacy handler on follow-ups — the default never upgrades
    history."""
    key = uuid.uuid4()
    seeded_run = await harness.seed_legacy_conversation(
        bootstrapped_url,
        server_a,
        client_request_id=key,
    )
    assert seeded_run == str(key)
    assert _submitted_handler(bootstrapped_url, key) == "agent.run"
    detail = _detail(app, jar, server_a, key)
    assert detail["is_legacy"] is True
    assert detail["handler_name"] == "agent.run"
    assert detail["objective_ids"] == []

    _settle(bootstrapped_url, key)

    followup = await _submit_followup(
        app, jar, server_a, key, uuid.uuid4(), message="Legacy follow-up."
    )
    assert followup.status == 201, followup.body
    followup_run = uuid.UUID(followup.json()["run_id"])
    assert _submitted_handler(bootstrapped_url, followup_run) == "agent.run"
    # Not a single objective row appeared anywhere.
    assert _objective_rows(bootstrapped_url) == []
    assert _bindings(bootstrapped_url) == []
    detail = _detail(app, jar, server_a, key)
    assert detail["is_legacy"] is True
    assert detail["handler_name"] == "agent.run"

    drain = await harness.call_app(
        app, "GET", "/api/v1/objectives/drain-status",
        headers=harness._cookies(jar),
    )
    doc = drain.json()
    assert doc["legacy"]["total"] >= 1
    assert doc["v2"]["total"] == 0
