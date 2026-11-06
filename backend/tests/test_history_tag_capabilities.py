"""Focused tests for the ``tags.*`` capability surface (W4A1..W4B).

Descriptor/contract claims are pure build-time tests (no database).
Store-backed claims (current-target wiring and the W4B catalog tools
create/list_catalog/delete) reuse the disposable real-Postgres harness
of ``test_task_capabilities.py``: set ``MAI_TEST_DATABASE_URL`` to a
fresh disposable pgvector/pg18 database (the harness applies the
linear Alembic chain from zero); without it those tests skip.
"""

from __future__ import annotations

import inspect
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import ValidationError

import sqlalchemy
from sqlalchemy import text

from mai.database import create_app_engine
from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.history_tags import (
    TAGS_ASSIGN_CAPABILITY,
    TAGS_CAPABILITY_IDS,
    TAGS_CREATE_CAPABILITY,
    TAGS_DELETE_CAPABILITY,
    TAGS_LIST_CATALOG_CAPABILITY,
    TAGS_LIST_FOR_CAPABILITY,
    TAGS_LIST_MAX_LIMIT,
    TAGS_READ_JOB_CAPABILITY,
    TAGS_REMOVE_CAPABILITY,
    TAGS_SEARCH_DEFAULT_LIMIT,
    TAGS_SEARCH_JOBS_CAPABILITY,
    TAGS_SEARCH_MAX_LIMIT,
    TAGS_SEARCH_MAX_TAG_IDS,
    TAGS_NAME_MAX_LENGTH,
    TagsAssignPayload,
    TagsCreatePayload,
    TagsListCatalogPayload,
    TagsReadJobPayload,
    TagsRemovePayload,
    TagsSearchJobsPayload,
    build_history_tag_capabilities,
)
from mai.capabilities.internal_api import (
    InternalApiComposition,
    assert_agent_ready_descriptor,
)
from mai.contracts.jobs import TERMINAL_RUN_STATES
from mai.jobs.lifecycle import RunLifecycle, RunState
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    aassign_job_tag,
    acreate_tag,
    alist_tags,
    atags_for_job,
)

# Fixed opaque fixture identities for local expectations.
SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"
SERVER_C = "c30d0e7a-3f4b-5c5d-9e6f-2a1b8c7d6e5f"

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI_PATH = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI_PATH)


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (whole schema)."""
    with psycopg.connect(url) as conn:
        # Whole-schema reset (the sibling suites' approach): FK-safe and
        # immune to future migrations.
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
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
    # Each data test starts from an empty association domain (the
    # session-scoped fixture database). TRUNCATE bypasses row triggers,
    # so the 0011 must-keep-one-tag constraint triggers do not refuse the
    # reset (the same reset discipline the sibling tag suites use).
    engine_sync = sqlalchemy.create_engine(migrated_url)
    try:
        with engine_sync.begin() as conn:
            conn.exec_driver_sql(
                "TRUNCATE job_tags, job_steps, job_events, "
                "job_schedule_fires, job_runs CASCADE"
            )
    finally:
        engine_sync.dispose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _context(server: str, run_id: uuid.UUID) -> ExecutionContext:
    return ExecutionContext(
        principal="principal",
        server_id=uuid.UUID(server),
        run_id=run_id,
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=TAGS_CAPABILITY_IDS,
        mode=PolicyMode.DELEGATED,
    )


def _composition(
    sessions_factory: object | None = None,
) -> InternalApiComposition:
    return build_history_tag_capabilities(
        session_factory=sessions_factory,  # type: ignore[arg-type]
    )


def _call(
    composition: InternalApiComposition,
    capability_id: str,
    payload: dict[str, object],
    context: ExecutionContext,
) -> CapabilityCall:
    descriptor = {d.identifier: d for d in composition.descriptors}[capability_id]
    return CapabilityCall(
        descriptor=descriptor,
        payload=payload,
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )


async def _execute(
    composition: InternalApiComposition,
    capability_id: str,
    payload: dict[str, object],
    context: ExecutionContext,
) -> tuple[object, dict[str, object]]:
    result = await composition.executors[capability_id](
        _call(composition, capability_id, payload, context)
    )
    return result, json.loads(result.output)


def _minimal_payloads() -> dict[str, dict[str, object]]:
    tag = str(uuid.uuid4())
    return {
        TAGS_ASSIGN_CAPABILITY: {"tag_id": tag},
        TAGS_REMOVE_CAPABILITY: {"tag_id": tag},
        TAGS_LIST_FOR_CAPABILITY: {},
        TAGS_CREATE_CAPABILITY: {"name": f"w4b-min-{uuid.uuid4()}"},
        TAGS_LIST_CATALOG_CAPABILITY: {},
        TAGS_DELETE_CAPABILITY: {"tag_id": tag},
        TAGS_SEARCH_JOBS_CAPABILITY: {"tag_ids": [tag]},
        TAGS_READ_JOB_CAPABILITY: {"run_id": str(uuid.uuid4())},
    }


# ---------------------------------------------------------------------------
# AC 1 — agent-ready descriptors, bounded payloads, no scope selector
# ---------------------------------------------------------------------------


class TestDescriptors:
    def test_tags_tools_are_agent_ready_and_fail_closed_at_build(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        assert set(by_id) == set(TAGS_CAPABILITY_IDS)
        assert by_id[TAGS_ASSIGN_CAPABILITY].effect_class == "mutate"
        assert by_id[TAGS_REMOVE_CAPABILITY].effect_class == "mutate"
        assert by_id[TAGS_LIST_FOR_CAPABILITY].effect_class == "read"
        assert by_id[TAGS_SEARCH_JOBS_CAPABILITY].effect_class == "read"
        assert by_id[TAGS_READ_JOB_CAPABILITY].effect_class == "read"
        for descriptor in composition.descriptors:
            assert_agent_ready_descriptor(descriptor)
            assert len(str(descriptor.input_schema["description"])) >= 30
            assert tuple(descriptor.execution_environments) == ("controller",)
            assert tuple(descriptor.required_grants) == (descriptor.identifier,)

    def test_payloads_carry_no_scope_selector(self) -> None:
        composition = _composition()
        for descriptor in composition.descriptors:
            props = descriptor.input_schema["properties"]
            assert "server_id" not in props
            assert "job_id" not in props
            if descriptor.identifier == TAGS_READ_JOB_CAPABILITY:
                # The one tags tool that legitimately names the run:
                # the run_id of a same-server read result.
                assert "run_id" in props
            else:
                assert "run_id" not in props
            assert "conversation_id" not in props

    def test_current_target_tools_carry_no_target_selector(self) -> None:
        composition = _composition()
        for capability_id in (
            TAGS_ASSIGN_CAPABILITY,
            TAGS_REMOVE_CAPABILITY,
            TAGS_LIST_FOR_CAPABILITY,
        ):
            descriptor = {
                d.identifier: d for d in composition.descriptors
            }[capability_id]
            # D09: the operations address the execution context's own job
            # run — no client-selectable target field survives.
            assert "target" not in descriptor.input_schema["properties"]

    def test_tag_id_is_a_required_singular_uuid(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        for capability_id in (TAGS_ASSIGN_CAPABILITY, TAGS_REMOVE_CAPABILITY):
            props = by_id[capability_id].input_schema["properties"]
            assert props["tag_id"]["type"] == "string"
            assert "tag_id" in by_id[capability_id].input_schema["required"]
            assert "tag_ids" not in props

    def test_replacement_uuid_is_optional_only_on_remove(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        remove_props = by_id[TAGS_REMOVE_CAPABILITY].input_schema["properties"]
        assert "replacement_tag_id" in remove_props
        assert "replacement_tag_id" not in by_id[
            TAGS_REMOVE_CAPABILITY
        ].input_schema["required"]
        for capability_id in (
            TAGS_ASSIGN_CAPABILITY,
            TAGS_LIST_FOR_CAPABILITY,
            TAGS_CREATE_CAPABILITY,
            TAGS_LIST_CATALOG_CAPABILITY,
            TAGS_DELETE_CAPABILITY,
            TAGS_SEARCH_JOBS_CAPABILITY,
        ):
            props = by_id[capability_id].input_schema["properties"]
            assert "replacement_tag_id" not in props

    def test_catalog_tools_declare_bounded_contracts(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        assert by_id[TAGS_CREATE_CAPABILITY].effect_class == "mutate"
        assert by_id[TAGS_LIST_CATALOG_CAPABILITY].effect_class == "read"
        assert by_id[TAGS_DELETE_CAPABILITY].effect_class == "mutate"

        create = by_id[TAGS_CREATE_CAPABILITY].input_schema
        assert create["required"] == ["name"]
        assert (
            TagsCreatePayload(name="w4b", description="x" * 500).description
            == "x" * 500
        )
        with pytest.raises(ValidationError):
            TagsCreatePayload(name="w4b", description="x" * 501)
        assert "minLength" not in create["properties"]["description"]

        catalog = by_id[TAGS_LIST_CATALOG_CAPABILITY].input_schema
        assert "q" not in catalog["required"]
        assert "type" in catalog["properties"]["q"]
        assert TagsListCatalogPayload(q="x" * 64).q == "x" * 64
        with pytest.raises(ValidationError):
            TagsListCatalogPayload(q="x" * 65)
        with pytest.raises(ValidationError):
            TagsListCatalogPayload(q="")
        assert catalog["properties"]["limit"]["minimum"] == 1
        assert catalog["properties"]["limit"]["maximum"] == TAGS_LIST_MAX_LIMIT
        assert catalog["properties"]["offset"]["minimum"] == 0

        delete = by_id[TAGS_DELETE_CAPABILITY].input_schema
        assert delete["required"] == ["tag_id"]
        assert "force" not in delete["required"]

    def test_list_for_declares_bounded_pagination(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        props = by_id[TAGS_LIST_FOR_CAPABILITY].input_schema["properties"]
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == TAGS_LIST_MAX_LIMIT
        assert props["offset"]["minimum"] == 0

    def test_search_jobs_declares_bounded_contract(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        assert by_id[TAGS_SEARCH_JOBS_CAPABILITY].effect_class == "read"
        search = by_id[TAGS_SEARCH_JOBS_CAPABILITY].input_schema
        assert search["required"] == ["tag_ids"]
        tag_ids = search["properties"]["tag_ids"]
        assert tag_ids["minItems"] == 1
        assert tag_ids["maxItems"] == TAGS_SEARCH_MAX_TAG_IDS
        assert search["properties"]["match"]["enum"] == ["any", "all"]
        assert search["properties"]["limit"]["minimum"] == 1
        assert search["properties"]["limit"]["maximum"] == TAGS_SEARCH_MAX_LIMIT
        assert search["properties"]["offset"]["minimum"] == 0

class TestReadJobPayloadContract:
    def test_descriptor_declares_the_content_read_contract(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        read = by_id[TAGS_READ_JOB_CAPABILITY].input_schema
        # D09: the content reader takes exactly the run id — the link
        # pagination surface died with the conversation links.
        assert read["required"] == ["run_id"]
        props = read["properties"]
        assert "server_id" not in props
        assert "limit" not in props
        assert "offset" not in props

    def test_defaults_and_uuid_coercion(self) -> None:
        run = str(uuid.uuid4())
        payload = TagsReadJobPayload.model_validate({"run_id": run})
        assert payload.run_id == uuid.UUID(run)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"run_id": "not-a-uuid"},
        ],
    )
    def test_rejects_out_of_contract_payloads(self, payload: dict) -> None:
        with pytest.raises(ValidationError):
            TagsReadJobPayload.model_validate(payload)


# ---------------------------------------------------------------------------
# AC 1b — payload runtime contract: one required scalar UUID per call
# (the descriptor projection strips ``format`` by design, so UUID-ness
# of ``tag_id`` is proven against the Pydantic models themselves)
# ---------------------------------------------------------------------------


SCALAR_PAYLOAD_MODELS = (TagsAssignPayload, TagsRemovePayload)


class TestPayloadRuntimeContract:
    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_accepts_one_uuid_string_as_uuid(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        tag = str(uuid.uuid4())
        payload = payload_model.model_validate(
            {"tag_id": tag}
        )
        assert isinstance(payload.tag_id, uuid.UUID)
        assert payload.tag_id == uuid.UUID(tag)

    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_rejects_missing_tag_id(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        with pytest.raises(ValidationError):
            payload_model.model_validate({})

    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_rejects_legacy_tag_ids_list(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        tag = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            payload_model.model_validate(
                {"tag_ids": [tag]}
            )

    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_rejects_any_list_tag_id_including_singleton(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        tag = str(uuid.uuid4())
        for tag_id in ([tag], [tag, str(uuid.uuid4())]):
            with pytest.raises(ValidationError):
                payload_model.model_validate(
                    {"tag_id": tag_id}
                )


class TestCreatePayloadContract:
    def test_name_is_canonicalized_like_the_store(self) -> None:
        payload = TagsCreatePayload.model_validate(
            {"name": "  W4B   Alpha  "}
        )
        assert payload.name == "W4B Alpha"
        assert payload.description is None

    def test_description_is_optional_and_bounded(self) -> None:
        payload = TagsCreatePayload.model_validate(
            {"name": "w4b", "description": "d" * 500}
        )
        assert payload.description == "d" * 500
        with pytest.raises(ValidationError):
            TagsCreatePayload.model_validate(
                {"name": "w4b", "description": "d" * 501}
            )

    @pytest.mark.parametrize(
        ("raw_name", "reason"),
        [
            ("   ", "whitespace-only"),
            ("line1\nline2", "control character"),
            ("a" * 65, "over the canonical length"),
            ("\x1f hidden", "control character"),
        ],
    )
    def test_rejects_non_canonical_names(self, raw_name: str, reason: str) -> None:
        with pytest.raises(ValidationError):
            TagsCreatePayload.model_validate({"name": raw_name})


class TestSearchJobsPayloadContract:
    def test_defaults_and_uuid_coercion(self) -> None:
        tag = str(uuid.uuid4())
        payload = TagsSearchJobsPayload.model_validate({"tag_ids": [tag]})
        assert payload.tag_ids == [uuid.UUID(tag)]
        assert isinstance(payload.tag_ids[0], uuid.UUID)
        assert payload.match == "any"
        assert payload.limit == TAGS_SEARCH_DEFAULT_LIMIT
        assert payload.offset == 0

    def test_accepts_the_maximal_request(self) -> None:
        tags = [str(uuid.uuid4()) for _ in range(TAGS_SEARCH_MAX_TAG_IDS)]
        payload = TagsSearchJobsPayload.model_validate(
            {"tag_ids": tags, "match": "all", "limit": TAGS_SEARCH_MAX_LIMIT,
             "offset": 25}
        )
        assert payload.match == "all"
        assert len(payload.tag_ids) == TAGS_SEARCH_MAX_TAG_IDS
        assert payload.limit == TAGS_SEARCH_MAX_LIMIT
        assert payload.offset == 25

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"tag_ids": []},
            {"tag_ids": [str(uuid.uuid4())] * 51},
            {"tag_ids": ["not-a-uuid"]},
            {"tag_ids": [str(uuid.uuid4())], "match": "some"},
            {"tag_ids": [str(uuid.uuid4())], "limit": 0},
            {"tag_ids": [str(uuid.uuid4())], "limit": 51},
            {"tag_ids": [str(uuid.uuid4())], "offset": -1},
        ],
    )
    def test_rejects_out_of_contract_payloads(self, payload: dict) -> None:
        with pytest.raises(ValidationError):
            TagsSearchJobsPayload.model_validate(payload)



# ---------------------------------------------------------------------------
# AC 2 — fail-closed surfaces: no factory refuses everything (incl. list_for)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestUnavailableExecutors:
    async def test_unavailable_composition_refuses_fail_closed(self) -> None:
        composition = _composition()
        context = _context(SERVER_A, uuid.uuid4())
        for capability_id, payload in _minimal_payloads().items():
            result, document = await _execute(
                composition, capability_id, payload, context
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "unavailable"


# ---------------------------------------------------------------------------
# AC 3 — current-target resolution (real disposable Postgres)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# AC 4 — W4A2 store wiring: real job + active-conversation assign/remove
# (replacement/final-tag, scope invisibility, typed refusals, idempotence)
# ---------------------------------------------------------------------------


def _insert_job(
    url: str,
    job_id: uuid.UUID,
    server_id: uuid.UUID = uuid.UUID(SERVER_A),
    submitted_at: datetime | None = None,
) -> None:
    """One real job_runs row seeded with the reserved unclassified tag."""
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants, submitted_at)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb,
                    COALESCE(%s, now()))
            """,
            (job_id, server_id, submitted_at),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


@pytest.mark.anyio
class TestAssignRemoveWired:
    async def test_job_assign_persists_agent_principal_and_is_idempotent(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)
        tag = await acreate_tag(sessions, name=f"w4a2-job-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)
        payload = {"tag_id": str(tag.tag_id)}

        result, document = await _execute(
            composition, TAGS_ASSIGN_CAPABILITY, payload, context
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document == {
            "status": "ok",
            "ok": True,
            "entity_id": str(run_id),
            "tag_id": str(tag.tag_id),
        }

        rows, total = await atags_for_job(
            sessions, run_id, server_id=uuid.UUID(SERVER_A)
        )
        assert total == 2  # reserved seed + the agent assignment
        assigned = [row for row in rows if row.tag_id == tag.tag_id]
        assert len(assigned) == 1
        assert assigned[0].assigned_by == "principal"
        assert assigned[0].assignment_source == "agent"

        # Re-assignment is an idempotent no-op with the same page.
        result, _ = await _execute(
            composition, TAGS_ASSIGN_CAPABILITY, payload, context
        )
        assert result.ok is True  # type: ignore[attr-defined]
        _, total_again = await atags_for_job(
            sessions, run_id, server_id=uuid.UUID(SERVER_A)
        )
        assert total_again == 2

    async def test_remove_swaps_final_tag_with_atomic_replacement(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)  # exactly one tag: reserved seed
        replacement = await acreate_tag(sessions, name=f"w4a2-repl-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)

        result, document = await _execute(
            composition,
            TAGS_REMOVE_CAPABILITY,
            {
                "tag_id": RESERVED_UNCLASSIFIED_TAG_ID,
                "replacement_tag_id": str(replacement.tag_id),
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document == {
            "status": "ok",
            "ok": True,
            "entity_id": str(run_id),
            "tag_id": RESERVED_UNCLASSIFIED_TAG_ID,
            "replacement_tag_id": str(replacement.tag_id),
        }
        rows, total = await atags_for_job(
            sessions, run_id, server_id=uuid.UUID(SERVER_A)
        )
        assert total == 1
        assert rows[0].tag_id == replacement.tag_id

        # Final-tag removal without replacement is refused typed, not raw.
        result, document = await _execute(
            composition,
            TAGS_REMOVE_CAPABILITY,
            {"tag_id": str(replacement.tag_id)},
            context,
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "conflict"

    async def test_remove_absent_association_is_idempotent_noop(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)
        tag = await acreate_tag(sessions, name=f"w4a2-absent-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        payload = {"tag_id": str(tag.tag_id)}
        context = _context(SERVER_A, run_id)

        for _ in range(2):
            result, document = await _execute(
                composition, TAGS_REMOVE_CAPABILITY, payload, context
            )
            assert result.ok is True  # type: ignore[attr-defined]
            assert document["replacement_tag_id"] is None
        _, total = await atags_for_job(
            sessions, run_id, server_id=uuid.UUID(SERVER_A)
        )
        assert total == 1  # untouched reserved seed

    async def test_cross_server_job_is_invisible(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)  # lives on SERVER_A
        tag = await acreate_tag(sessions, name=f"w4a2-scope-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"tag_id": str(tag.tag_id)},
            _context(SERVER_B, run_id),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "not_found"

    async def test_unknown_tag_refuses_typed_not_found(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"tag_id": str(uuid.uuid4())},
            _context(SERVER_A, run_id),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "not_found"


# ---------------------------------------------------------------------------
# AC 5 — W4A3 list_for wiring: deterministic bounded real pages
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestListForWired:
    async def test_job_pages_are_deterministic_first_next_final_beyond(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)
        extras = [
            await acreate_tag(sessions, name=f"w4a3-job-{uuid.uuid4()}")
            for _ in range(2)
        ]
        for tag in extras:
            await aassign_job_tag(
                sessions,
                run_id,
                tag.tag_id,
                server_id=uuid.UUID(SERVER_A),
                assigned_by="seeder",
                assignment_source="system",
            )
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)
        expected_order = sorted(
            ["unclassified", extras[0].name, extras[1].name], key=str.lower
        )

        # First page (limit=1): lowest name, honest totals, advancing page.
        result, first = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"limit": 1, "offset": 0},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert first["status"] == "ok" and first["ok"] is True
        assert first["entity_id"] == str(run_id)
        assert first["limit"] == 1 and first["offset"] == 0
        assert first["total"] == 3  # reserved seed + two assignments
        assert first["has_more"] is True and first["next_offset"] == 1
        assert [item["name"] for item in first["items"]] == [expected_order[0]]
        row = first["items"][0]
        assert set(row) == {
            "tag_id",
            "name",
            "assigned_by",
            "assignment_source",
            "created_at",
        }
        assert str(uuid.UUID(row["tag_id"])) == row["tag_id"]
        datetime.fromisoformat(row["created_at"])

        # Next page continues the deterministic order.
        _, second = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"limit": 1, "offset": first["next_offset"]},
            context,
        )
        assert [item["name"] for item in second["items"]] == [expected_order[1]]
        assert second["has_more"] is True and second["next_offset"] == 2

        # Final page ends the sequence: has_more False, next_offset None.
        _, final = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"limit": 1, "offset": second["next_offset"]},
            context,
        )
        assert [item["name"] for item in final["items"]] == [expected_order[2]]
        assert final["total"] == 3
        assert final["has_more"] is False and final["next_offset"] is None

        # Beyond the total: an honest empty page, not a refusal.
        _, beyond = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"limit": 1, "offset": 3},
            context,
        )
        assert beyond["items"] == []
        assert beyond["total"] == 3
        assert beyond["has_more"] is False and beyond["next_offset"] is None

def _catalog_prefix() -> str:
    return f"w4b-cat-{uuid.uuid4().hex[:8]}-"


@pytest.mark.anyio
class TestCatalogWired:
    async def test_create_persists_one_canonical_typed_row(
        self, sessions
    ) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())

        result, document = await _execute(
            composition,
            TAGS_CREATE_CAPABILITY,
            {
                "name": f"  {_catalog_prefix()}Alpha   Beta  ",
                "description": "created by the W4B capability test",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["status"] == "ok" and document["ok"] is True
        canonical = document["name"]
        assert canonical.startswith("w4b-cat-") and canonical.endswith("Alpha Beta")
        assert document["description"] == "created by the W4B capability test"
        assert str(uuid.UUID(document["tag_id"])) == document["tag_id"]
        datetime.fromisoformat(document["created_at"])
        datetime.fromisoformat(document["updated_at"])

        rows, total = await alist_tags(
            sessions, name_filter=canonical.removeprefix("w4b-cat-")
        )
        assert total == 1
        assert rows[0].tag_id == uuid.UUID(document["tag_id"])
        assert rows[0].name == canonical  # the stored name IS the canonical form

    async def test_create_duplicate_canonical_name_returns_existing_row(
        self, sessions
    ) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        original_name = f"{_catalog_prefix()}Dup Case"

        _, first = await _execute(
            composition,
            TAGS_CREATE_CAPABILITY,
            {"name": original_name, "description": "the original purpose"},
            context,
        )
        assert first["status"] == "ok"

        # Same canonical name (case + whitespace differ), different
        # description: idempotent re-create returns the winner's row
        # untouched — the stored description is never overwritten.
        result, second = await _execute(
            composition,
            TAGS_CREATE_CAPABILITY,
            {
                "name": f"  {original_name.upper()}  ",
                "description": "a different purpose that must be ignored",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert second["tag_id"] == first["tag_id"]
        assert second["name"] == first["name"]
        assert second["description"] == "the original purpose"

    async def test_create_refuses_invalid_names_fail_closed(
        self, sessions
    ) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        # Catalog rows from sibling tests are never truncated, so the
        # "nothing was created" claim is a delta, not an absolute count.
        _, total_before = await alist_tags(sessions, limit=1)
        for raw_name in ("   ", "line1\nline2", "a" * 65):
            result, document = await _execute(
                composition,
                TAGS_CREATE_CAPABILITY,
                {"name": raw_name},
                context,
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "invalid_request"
        _, total_after = await alist_tags(sessions, limit=1)
        assert total_after == total_before

    async def test_list_catalog_pages_deterministically_with_q_filter(
        self, sessions
    ) -> None:
        prefix = _catalog_prefix()
        names = [f"{prefix}b-row", f"{prefix}a-row", f"{prefix}c-row"]
        for name in names:
            await acreate_tag(sessions, name=name)
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        expected_order = sorted(names, key=str.lower)

        result, first = await _execute(
            composition,
            TAGS_LIST_CATALOG_CAPABILITY,
            {"q": prefix, "limit": 2, "offset": 0},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert first["status"] == "ok" and first["ok"] is True
        assert first["total"] == 3  # pre-slice count under the same filter
        assert first["limit"] == 2 and first["offset"] == 0
        assert first["has_more"] is True and first["next_offset"] == 2
        assert [item["name"] for item in first["items"]] == expected_order[:2]
        row = first["items"][0]
        assert set(row) == {
            "tag_id",
            "name",
            "description",
            "created_at",
            "updated_at",
        }
        assert str(uuid.UUID(row["tag_id"])) == row["tag_id"]
        datetime.fromisoformat(row["created_at"])
        datetime.fromisoformat(row["updated_at"])

        # Final page ends the sequence: has_more False, next_offset None.
        _, final = await _execute(
            composition,
            TAGS_LIST_CATALOG_CAPABILITY,
            {"q": prefix, "limit": 2, "offset": first["next_offset"]},
            context,
        )
        assert [item["name"] for item in final["items"]] == expected_order[2:]
        assert final["total"] == 3
        assert final["has_more"] is False and final["next_offset"] is None

        # The filter is a case-insensitive substring on the tag name.
        _, narrowed = await _execute(
            composition,
            TAGS_LIST_CATALOG_CAPABILITY,
            {"q": prefix.upper() + "B-ROW"},
            context,
        )
        assert narrowed["total"] == 1
        assert narrowed["items"][0]["name"] == f"{prefix}b-row"

        # A filter matching nothing is an honest empty page, not a refusal.
        _, absent = await _execute(
            composition,
            TAGS_LIST_CATALOG_CAPABILITY,
            {"q": f"{prefix}absent"},
            context,
        )
        assert absent["items"] == [] and absent["total"] == 0
        assert absent["has_more"] is False and absent["next_offset"] is None

    async def test_delete_refuses_used_tag_until_forced_and_keeps_final_tag(
        self, sessions, migrated_url
    ) -> None:
        run_id = uuid.uuid4()
        _insert_job(migrated_url, run_id)  # seeds the reserved unclassified tag
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)
        _, created = await _execute(
            composition,
            TAGS_CREATE_CAPABILITY,
            {"name": f"{_catalog_prefix()}used-tag"},
            context,
        )
        tag_id = created["tag_id"]
        result, _ = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"tag_id": tag_id},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]

        # Default refusal: the tag is used, force is absent (False).
        result, refused = await _execute(
            composition,
            TAGS_DELETE_CAPABILITY,
            {"tag_id": tag_id},
            context,
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert refused["status"] == "refused"
        assert refused["code"] == "conflict"
        _, total = await alist_tags(sessions, name_filter="used-tag")
        assert total == 1  # the refused delete changed nothing

        # Forced delete succeeds only because the job keeps its final
        # (reserved) tag; the association row is removed with the tag.
        result, forced = await _execute(
            composition,
            TAGS_DELETE_CAPABILITY,
            {"tag_id": tag_id, "force": True},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert forced["status"] == "ok"
        assert forced["tag_id"] == tag_id
        assert forced["force"] is True
        assert forced["removed_associations"] == 1

        _, total_after = await alist_tags(sessions, name_filter="used-tag")
        assert total_after == 0
        remaining, remaining_total = await atags_for_job(
            sessions, run_id, server_id=uuid.UUID(SERVER_A)
        )
        assert remaining_total == 1  # final-tag preservation
        assert remaining[0].name == "unclassified"

    async def test_delete_reserved_tag_refused_even_forced(self, sessions) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_DELETE_CAPABILITY,
            {
                "tag_id": str(uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
                "force": True,
            },
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "conflict"
        _, total = await alist_tags(sessions, name_filter="unclassified")
        assert total == 1

    async def test_full_catalog_page_envelope_stays_under_60000_bytes(
        self, sessions
    ) -> None:
        prefix = _catalog_prefix()
        for index in range(105):
            await acreate_tag(sessions, name=f"{prefix}{index:03d}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_LIST_CATALOG_CAPABILITY,
            {"q": prefix, "limit": TAGS_LIST_MAX_LIMIT, "offset": 0},
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["total"] == 105
        assert len(document["items"]) == TAGS_LIST_MAX_LIMIT
        assert document["has_more"] is True
        assert document["next_offset"] == TAGS_LIST_MAX_LIMIT
        assert len(json.dumps(document, sort_keys=True)) < 60000


# ---------------------------------------------------------------------------
# W4C — tags.search_jobs: same-server tag search (real Postgres)
# ---------------------------------------------------------------------------


async def _assign_tag_to_job(
    sessions: object, job_id: uuid.UUID, tag_id: uuid.UUID
) -> None:
    await aassign_job_tag(
        sessions,  # type: ignore[arg-type]
        job_id,
        tag_id,
        server_id=uuid.UUID(SERVER_A),
        assigned_by="principal",
        assignment_source="agent",
    )


@pytest.mark.anyio
class TestSearchJobsWired:
    async def test_any_and_all_match_same_server_newest_first(
        self, sessions, migrated_url
    ) -> None:
        base = datetime(2026, 10, 30, 12, 0, 0)
        j1, j2, j3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        _insert_job(migrated_url, j1, submitted_at=base)
        _insert_job(migrated_url, j2, submitted_at=base + timedelta(minutes=1))
        _insert_job(migrated_url, j3, submitted_at=base + timedelta(minutes=2))
        tag_a = await acreate_tag(sessions, name=f"w4c-a-{uuid.uuid4()}")
        tag_b = await acreate_tag(sessions, name=f"w4c-b-{uuid.uuid4()}")
        tag_c = await acreate_tag(sessions, name=f"w4c-c-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        for job_id, tag_id in (
            (j1, tag_a.tag_id),
            (j2, tag_a.tag_id),
            (j2, tag_b.tag_id),
            (j3, tag_b.tag_id),
        ):
            await _assign_tag_to_job(sessions, job_id, tag_id)
        # Another server's run carrying tag_a is invisible to this search.
        foreign = uuid.uuid4()
        _insert_job(
            migrated_url,
            foreign,
            server_id=uuid.UUID(SERVER_B),
            submitted_at=base + timedelta(minutes=3),
        )
        await aassign_job_tag(
            sessions,
            foreign,
            tag_a.tag_id,
            server_id=uuid.UUID(SERVER_B),
            assigned_by="principal",
            assignment_source="agent",
        )

        result, any_doc = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {"tag_ids": [str(tag_a.tag_id), str(tag_b.tag_id)]},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert any_doc["status"] == "ok" and any_doc["ok"] is True
        assert any_doc["match"] == "any"
        assert any_doc["total"] == 3
        assert [item["run_id"] for item in any_doc["items"]] == [
            str(j3),
            str(j2),
            str(j1),
        ]
        assert any_doc["limit"] == TAGS_SEARCH_DEFAULT_LIMIT
        assert any_doc["offset"] == 0
        assert any_doc["has_more"] is False and any_doc["next_offset"] is None
        row = any_doc["items"][0]
        assert set(row) == {"run_id", "submitted_at", "tag_preview"}
        assert str(uuid.UUID(row["run_id"])) == row["run_id"]
        datetime.fromisoformat(row["submitted_at"])
        assert set(row["tag_preview"]) == {"preview", "total"}
        assert row["tag_preview"]["total"] == 2  # tag_b + reserved seed
        assert set(row["tag_preview"]["preview"]) == {tag_b.name, "unclassified"}
        j2_row = any_doc["items"][1]
        assert j2_row["tag_preview"]["total"] == 3
        assert set(j2_row["tag_preview"]["preview"]) == {
            tag_a.name,
            tag_b.name,
            "unclassified",
        }

        result, all_doc = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {
                "tag_ids": [str(tag_a.tag_id), str(tag_b.tag_id)],
                "match": "all",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert all_doc["match"] == "all"
        assert all_doc["total"] == 1
        assert [item["run_id"] for item in all_doc["items"]] == [str(j2)]

        # No run carries both a and c: an honest empty page, not a refusal.
        result, empty = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {
                "tag_ids": [str(tag_a.tag_id), str(tag_c.tag_id)],
                "match": "all",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert empty["items"] == [] and empty["total"] == 0
        assert empty["has_more"] is False and empty["next_offset"] is None

    async def test_pagination_slices_newest_first_with_honest_meta(
        self, sessions, migrated_url
    ) -> None:
        base = datetime(2026, 10, 30, 13, 0, 0)
        jobs = [uuid.uuid4() for _ in range(4)]
        for index, job_id in enumerate(jobs):
            _insert_job(
                migrated_url, job_id,
                submitted_at=base + timedelta(minutes=index),
            )
        tag = await acreate_tag(sessions, name=f"w4c-page-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        for job_id in jobs:
            await _assign_tag_to_job(sessions, job_id, tag.tag_id)
        newest_first = [str(job_id) for job_id in reversed(jobs)]

        result, first = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)], "limit": 2, "offset": 0},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert first["total"] == 4
        assert [item["run_id"] for item in first["items"]] == newest_first[:2]
        assert first["limit"] == 2 and first["offset"] == 0
        assert first["has_more"] is True and first["next_offset"] == 2

        _, second = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)], "limit": 2, "offset": 2},
            context,
        )
        assert [item["run_id"] for item in second["items"]] == newest_first[2:]
        assert second["total"] == 4
        assert second["has_more"] is False and second["next_offset"] is None

        _, beyond = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)], "limit": 2, "offset": 99},
            context,
        )
        assert beyond["items"] == [] and beyond["total"] == 4
        assert beyond["has_more"] is False and beyond["next_offset"] is None

    async def test_invalid_payloads_refuse_fail_closed(self, sessions) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        tag = str(uuid.uuid4())
        for payload in (
            {},
            {"tag_ids": []},
            {"tag_ids": [tag] * 51},
            {"tag_ids": ["not-a-uuid"]},
            {"tag_ids": [tag], "match": "some"},
            {"tag_ids": [tag], "limit": 0},
            {"tag_ids": [tag], "limit": 51},
            {"tag_ids": [tag], "offset": -1},
        ):
            result, document = await _execute(
                composition, TAGS_SEARCH_JOBS_CAPABILITY, payload, context
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "invalid_request"

    async def test_maximal_page_envelope_stays_under_60000_bytes(
        self, sessions, migrated_url
    ) -> None:
        stem = f"w4c-{uuid.uuid4().hex[:8]}"
        tag_ids: list[str] = []
        for index in range(50):
            base = f"{stem}-{index:02d}-"
            name = base + "x" * (TAGS_NAME_MAX_LENGTH - len(base))
            assert len(name) == 64
            tag = await acreate_tag(sessions, name=name)
            tag_ids.append(str(tag.tag_id))
        for index, tag_id in enumerate(tag_ids):
            job_id = uuid.uuid4()
            _insert_job(
                migrated_url, job_id,
                submitted_at=datetime(2026, 10, 30, 14, index % 60, index // 60),
            )
            await aassign_job_tag(
                sessions,
                job_id,
                uuid.UUID(tag_id),
                server_id=uuid.UUID(SERVER_A),
                assigned_by="principal",
                assignment_source="agent",
            )
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_SEARCH_JOBS_CAPABILITY,
            {"tag_ids": tag_ids},
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["match"] == "any"
        assert document["total"] == 50
        assert len(document["items"]) == 50
        assert document["limit"] == TAGS_SEARCH_DEFAULT_LIMIT == 50
        assert document["offset"] == 0
        assert document["has_more"] is False and document["next_offset"] is None
        for item in document["items"]:
            preview = item["tag_preview"]
            assert preview["total"] == 2  # the maximal 64-char name + reserved
            assert len(preview["preview"]) == 2
        assert len(json.dumps(document, sort_keys=True)) < 60000


# ---------------------------------------------------------------------------
# W4C2-JOB — tags.read_job over the reviewed job content reader
# ---------------------------------------------------------------------------
_TS_A = datetime(2026, 3, 1, 12, 0, 0, 111111, tzinfo=timezone.utc)
_TS_B = datetime(2026, 3, 2, 12, 0, 0, 222222, tzinfo=timezone.utc)
_TS_C = datetime(2026, 3, 3, 12, 0, 0, 333333, tzinfo=timezone.utc)


def _job_row_insert(
    url: str,
    job_id: uuid.UUID,
    server_id: uuid.UUID = uuid.UUID(SERVER_A),
    *,
    parent_run_id: uuid.UUID | None = None,
    label: str = "",
    error_ref: str | None = None,
    evidence_ref: str | None = None,
    submitted_at: datetime | None = None,
) -> None:
    """One real job_runs row with explicit content columns."""
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, parent_run_id, depth, profile_revision,
                 prompt_revision, model_profile, requested_grants, label,
                 error_ref, evidence_ref, submitted_at)
            VALUES (%s, %s, %s, %s, 1, 1, 'profile', '[]'::jsonb,
                    COALESCE(%s, ''), %s, %s, COALESCE(%s, now()))
            """,
            (
                job_id,
                server_id,
                parent_run_id,
                1 if parent_run_id is not None else 0,
                label,
                error_ref,
                evidence_ref,
                submitted_at,
            ),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


async def _record(
    sessions: object, run_id: uuid.UUID, *targets: RunState
) -> None:
    # Terminal transitions swap to catalog tags named after the target
    # state; a fresh 0011 catalog only seeds ``unclassified``, so seed the
    # terminal rows first (idempotent — the same harness contract as
    # ``test_chat_http._seed_terminal_catalog``).
    async with sessions() as session:  # type: ignore[union-attr]
        for state in TERMINAL_RUN_STATES:
            await session.execute(
                text(
                    "INSERT INTO tags (tag_id, name, description) "
                    "VALUES (:tag_id, :name, 'terminal run classification') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"tag_id": uuid.uuid4(), "name": state.value},
            )
        await session.commit()
    lifecycle = RunLifecycle(sessions)  # type: ignore[arg-type]
    for target in targets:
        await lifecycle.record_transition(str(run_id), target)


def _read_job(
    sessions: object,
    payload: dict[str, object],
    context: ExecutionContext,
) -> tuple[object, dict[str, object]]:
    return _execute(
        build_history_tag_capabilities(session_factory=sessions),  # type: ignore[arg-type]
        TAGS_READ_JOB_CAPABILITY,
        payload,
        context,
    )


@pytest.mark.anyio
class TestReadJobContent:
    async def test_scalar_fields_are_returned_verbatim(
        self, sessions, migrated_url
    ) -> None:
        parent, job = uuid.uuid4(), uuid.uuid4()
        _job_row_insert(migrated_url, parent)
        _job_row_insert(
            migrated_url,
            job,
            parent_run_id=parent,
            label="w4c2 job label",
            error_ref="runs/abc/error",
            evidence_ref="runs/abc/evidence",
            submitted_at=_TS_B,
        )

        result, page = await _read_job(
            sessions, {"run_id": str(job)}, _context(SERVER_A, uuid.uuid4())
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert page["status"] == "ok"
        assert page["run_id"] == str(job)
        assert page["parent_run_id"] == str(parent)
        assert page["label"] == "w4c2 job label"
        assert page["error_ref"] == "runs/abc/error"
        assert page["evidence_ref"] == "runs/abc/evidence"
        assert page["state"] is None  # no recorded transitions yet
        assert datetime.fromisoformat(page["submitted_at"]) == _TS_B
        assert datetime.fromisoformat(page["updated_at"]).tzinfo is not None
        # The accepted envelope verbatim: nothing invented, no raw events
        # and no conversation links (D09 dropped the link surface).
        assert set(page) == {
            "ok",
            "status",
            "run_id",
            "parent_run_id",
            "label",
            "state",
            "submitted_at",
            "updated_at",
            "error_ref",
            "evidence_ref",
        }

    async def test_lifecycle_state_replays_without_raw_events(
        self, sessions, migrated_url
    ) -> None:
        quiet, done = uuid.uuid4(), uuid.uuid4()
        _job_row_insert(migrated_url, quiet)
        _job_row_insert(migrated_url, done)
        await _record(
            sessions, done, RunState.QUEUED, RunState.RUNNING,
            RunState.SUCCEEDED,
        )

        result, page = await _read_job(
            sessions, {"run_id": str(done)}, _context(SERVER_A, uuid.uuid4())
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert page["state"] == "succeeded"
        assert "events" not in page

        _, pre_submit = await _read_job(
            sessions, {"run_id": str(quiet)}, _context(SERVER_A, uuid.uuid4())
        )
        assert pre_submit["state"] is None

    async def test_empty_label_is_verbatim_not_null(
        self, sessions, migrated_url
    ) -> None:
        job = uuid.uuid4()
        _job_row_insert(migrated_url, job, label="")

        result, page = await _read_job(
            sessions, {"run_id": str(job)}, _context(SERVER_A, uuid.uuid4())
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert isinstance(page["label"], str)
        assert page["label"] == ""

    async def test_cross_server_and_missing_are_identically_not_found(
        self, sessions, migrated_url
    ) -> None:
        job_b = uuid.uuid4()
        _job_row_insert(migrated_url, job_b, server_id=uuid.UUID(SERVER_B))

        documents = []
        for run_id in (job_b, uuid.uuid4()):
            result, document = await _read_job(
                sessions,
                {"run_id": str(run_id)},
                _context(SERVER_A, uuid.uuid4()),
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            documents.append(document)
        assert all(document["code"] == "not_found" for document in documents)
        # A real other-server run and a missing one are indistinguishable.
        assert len({document["message"] for document in documents}) == 1
        assert documents[0]["message"] == "job not found"


@pytest.mark.anyio
class TestW4DCompositionRegistration:
    def test_builder_maps_exactly_the_eight_unique_tag_ids(self) -> None:
        """The builder surface is exactly the 8 unique ``tags.*`` IDs."""
        composition = _composition()
        assert len(TAGS_CAPABILITY_IDS) == 8
        assert len(set(TAGS_CAPABILITY_IDS)) == 8
        assert len(composition.descriptors) == 8
        by_id = {d.identifier: d for d in composition.descriptors}
        assert set(by_id) == set(TAGS_CAPABILITY_IDS)
        assert set(composition.executors) == set(TAGS_CAPABILITY_IDS)
        for capability_id in TAGS_CAPABILITY_IDS:
            assert by_id[capability_id].identifier == capability_id
            assert callable(composition.executors[capability_id])

    def test_api_merges_history_tags_into_the_same_composition(self) -> None:
        """create_app builds with app.state.session_factory and merges the
        tags descriptors/executors into the ONE InternalApiComposition
        alongside internal_api (source policy; no runtime)."""
        from mai.api import app as app_module

        src = inspect.getsource(app_module._lifespan)
        assert (
            "_history_tags = build_history_tag_capabilities(\n"
            "            session_factory=app.state.session_factory,\n"
            "        )"
        ) in src
        assert (
            "descriptors=(\n"
            "                *app.state.internal_api.descriptors,\n"
            "                *_history_tags.descriptors,\n"
            "            ),"
        ) in src
        assert (
            "executors={\n"
            "                **app.state.internal_api.executors,\n"
            "                **_history_tags.executors,\n"
            "            },"
        ) in src

    def test_worker_registers_executes_and_grants_history_tags(self) -> None:
        """_configure_default_agent_runtime builds with sessions, registers
        every descriptor, merges every executor, and auto-grants the tags
        IDs to enrolled_servers incl. UUID-zero General Consult."""
        from mai.jobs import worker as worker_module

        src = inspect.getsource(
            worker_module._configure_default_agent_runtime
        )
        assert (
            "history_tags = "
            "build_history_tag_capabilities(session_factory=sessions)"
        ) in src
        assert "for desc in history_tags.descriptors:" in src
        assert "registry.register(desc)" in src
        assert "**history_tags.executors," in src
        assert "*TAGS_CAPABILITY_IDS," in src
        assert (
            "grants.setdefault(cap_id, set()).update(enrolled_servers)"
        ) in src
