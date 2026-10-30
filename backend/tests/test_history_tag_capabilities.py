"""Focused tests for the ``tags.*`` capability surface (W4A1..W4B).

Descriptor/contract claims are pure build-time tests (no database).
Store-backed claims (current-target wiring and the W4B catalog tools
create/list_catalog/delete) reuse the disposable real-Postgres harness
of ``test_task_capabilities.py``: set ``MAI_TEST_DATABASE_URL`` to a
fresh disposable pgvector/pg18 database (the harness applies the
linear Alembic chain from zero); without it those tests skip.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest

from pydantic import ValidationError

from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    arelease_run,
)
from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.history_tags import (
    TAGS_ASSIGN_CAPABILITY,
    TAGS_CAPABILITY_IDS,
    TAGS_CREATE_CAPABILITY,
    TAGS_DELETE_CAPABILITY,
    TAGS_LIST_CATALOG_CAPABILITY,
    TAGS_LIST_DEFAULT_LIMIT,
    TAGS_LIST_FOR_CAPABILITY,
    TAGS_LIST_MAX_LIMIT,
    TAGS_READ_CONVERSATION_CAPABILITY,
    TAGS_READ_DEFAULT_LIMIT,
    TAGS_READ_DEFAULT_MAX_CHARS,
    TAGS_READ_MAX_LIMIT,
    TAGS_REMOVE_CAPABILITY,
    TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
    TAGS_SEARCH_DEFAULT_LIMIT,
    TAGS_SEARCH_JOBS_CAPABILITY,
    TAGS_SEARCH_MAX_LIMIT,
    TAGS_SEARCH_MAX_TAG_IDS,
    TAGS_NAME_MAX_LENGTH,
    TagsAssignPayload,
    TagsCreatePayload,
    TagsListCatalogPayload,
    TagsReadConversationPayload,
    TagsRemovePayload,
    TagsSearchConversationsPayload,
    TagsSearchJobsPayload,
    _aresolve_current_target,
    build_history_tag_capabilities,
)
from mai.capabilities.internal_api import (
    InternalApiComposition,
    InternalApiError,
    assert_agent_ready_descriptor,
)
from mai.history_store import MAX_CONTENT_CHARS
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    aassign_conversation_tag,
    aassign_job_tag,
    acreate_tag,
    alist_tags,
    atags_for_conversation,
    atags_for_job,
)

# Reused disposable-Postgres harness/fixtures (session-scoped migrated
# database + per-test cleaned sessions). Imported unused-on-purpose: the
# names must live in this module's namespace for fixture resolution.
from test_task_capabilities import (  # noqa: F401
    SERVER_A,
    SERVER_B,
    migrated_url,
    sessions,
)

# Fixed opaque fixture identities for local expectations.
SERVER_C = "c30d0e7a-3f4b-5c5d-9e6f-2a1b8c7d6e5f"


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
        TAGS_ASSIGN_CAPABILITY: {"target": "job", "tag_id": tag},
        TAGS_REMOVE_CAPABILITY: {"target": "job", "tag_id": tag},
        TAGS_LIST_FOR_CAPABILITY: {"target": "conversation"},
        TAGS_CREATE_CAPABILITY: {"name": f"w4b-min-{uuid.uuid4()}"},
        TAGS_LIST_CATALOG_CAPABILITY: {},
        TAGS_DELETE_CAPABILITY: {"tag_id": tag},
        TAGS_SEARCH_JOBS_CAPABILITY: {"tag_ids": [tag]},
        TAGS_SEARCH_CONVERSATIONS_CAPABILITY: {"tag_ids": [tag]},
        TAGS_READ_CONVERSATION_CAPABILITY: {
            "conversation_id": str(uuid.uuid4())
        },
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
        assert by_id[TAGS_SEARCH_CONVERSATIONS_CAPABILITY].effect_class == "read"
        assert by_id[TAGS_READ_CONVERSATION_CAPABILITY].effect_class == "read"
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
            assert "run_id" not in props
            assert "job_id" not in props
            if descriptor.identifier == TAGS_READ_CONVERSATION_CAPABILITY:
                # The one tags tool that legitimately names its subject:
                # the conversation_id of a same-server read result.
                assert "conversation_id" in props
                continue
            assert "conversation_id" not in props

    def test_target_is_the_job_or_conversation_literal(self) -> None:
        composition = _composition()
        for capability_id in (
            TAGS_ASSIGN_CAPABILITY,
            TAGS_REMOVE_CAPABILITY,
            TAGS_LIST_FOR_CAPABILITY,
        ):
            descriptor = {
                d.identifier: d for d in composition.descriptors
            }[capability_id]
            target = descriptor.input_schema["properties"]["target"]
            assert target["enum"] == ["job", "conversation"]
            assert "target" in descriptor.input_schema["required"]

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
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
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

    def test_search_conversations_declares_bounded_contract(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        assert by_id[TAGS_SEARCH_CONVERSATIONS_CAPABILITY].effect_class == "read"
        search = by_id[TAGS_SEARCH_CONVERSATIONS_CAPABILITY].input_schema
        assert search["required"] == ["tag_ids"]
        tag_ids = search["properties"]["tag_ids"]
        assert tag_ids["minItems"] == 1
        assert tag_ids["maxItems"] == TAGS_SEARCH_MAX_TAG_IDS
        assert search["properties"]["match"]["enum"] == ["any", "all"]
        assert search["properties"]["limit"]["minimum"] == 1
        assert search["properties"]["limit"]["maximum"] == TAGS_SEARCH_MAX_LIMIT
        assert search["properties"]["offset"]["minimum"] == 0
        assert TagsSearchConversationsPayload(
            tag_ids=[uuid.uuid4()]
        ).match == "any"


class TestReadConversationPayloadContract:
    def test_descriptor_declares_bounded_content_contract(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        read = by_id[TAGS_READ_CONVERSATION_CAPABILITY].input_schema
        assert read["required"] == ["conversation_id"]
        props = read["properties"]
        assert "server_id" not in props
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == TAGS_READ_MAX_LIMIT == 50
        assert props["offset"]["minimum"] == 0
        assert props["char_cursor"]["minimum"] == 0
        assert props["max_chars"]["minimum"] == 1
        assert props["max_chars"]["maximum"] == MAX_CONTENT_CHARS == 20_000

    def test_defaults_and_uuid_coercion(self) -> None:
        conv = str(uuid.uuid4())
        payload = TagsReadConversationPayload.model_validate(
            {"conversation_id": conv}
        )
        assert payload.conversation_id == uuid.UUID(conv)
        assert payload.limit == TAGS_READ_DEFAULT_LIMIT == 50
        assert payload.offset == 0
        assert payload.char_cursor == 0
        assert payload.max_chars == TAGS_READ_DEFAULT_MAX_CHARS == 4096

    def test_accepts_the_maximal_request(self) -> None:
        payload = TagsReadConversationPayload.model_validate(
            {
                "conversation_id": str(uuid.uuid4()),
                "limit": TAGS_READ_MAX_LIMIT,
                "offset": 7,
                "char_cursor": 12,
                "max_chars": MAX_CONTENT_CHARS,
            }
        )
        assert payload.limit == TAGS_READ_MAX_LIMIT
        assert payload.offset == 7
        assert payload.char_cursor == 12
        assert payload.max_chars == MAX_CONTENT_CHARS

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"conversation_id": "not-a-uuid"},
            {"conversation_id": str(uuid.uuid4()), "limit": 0},
            {"conversation_id": str(uuid.uuid4()), "limit": 51},
            {"conversation_id": str(uuid.uuid4()), "offset": -1},
            {"conversation_id": str(uuid.uuid4()), "char_cursor": -1},
            {"conversation_id": str(uuid.uuid4()), "max_chars": 0},
            {
                "conversation_id": str(uuid.uuid4()),
                "max_chars": MAX_CONTENT_CHARS + 1,
            },
        ],
    )
    def test_rejects_out_of_contract_payloads(self, payload: dict) -> None:
        with pytest.raises(ValidationError):
            TagsReadConversationPayload.model_validate(payload)


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
            {"target": "job", "tag_id": tag}
        )
        assert isinstance(payload.tag_id, uuid.UUID)
        assert payload.tag_id == uuid.UUID(tag)

    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_rejects_missing_tag_id(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        with pytest.raises(ValidationError):
            payload_model.model_validate({"target": "job"})

    @pytest.mark.parametrize("payload_model", SCALAR_PAYLOAD_MODELS)
    def test_rejects_legacy_tag_ids_list(
        self,
        payload_model: type[TagsAssignPayload] | type[TagsRemovePayload],
    ) -> None:
        tag = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            payload_model.model_validate(
                {"target": "job", "tag_ids": [tag]}
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
                    {"target": "job", "tag_id": tag_id}
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


class TestSearchConversationsPayloadContract:
    def test_defaults_and_uuid_coercion(self) -> None:
        tag = str(uuid.uuid4())
        payload = TagsSearchConversationsPayload.model_validate(
            {"tag_ids": [tag]}
        )
        assert payload.tag_ids == [uuid.UUID(tag)]
        assert isinstance(payload.tag_ids[0], uuid.UUID)
        assert payload.match == "any"
        assert payload.limit == TAGS_SEARCH_DEFAULT_LIMIT
        assert payload.offset == 0

    def test_accepts_the_maximal_request(self) -> None:
        tags = [str(uuid.uuid4()) for _ in range(TAGS_SEARCH_MAX_TAG_IDS)]
        payload = TagsSearchConversationsPayload.model_validate(
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
            TagsSearchConversationsPayload.model_validate(payload)


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


@pytest.mark.anyio
class TestResolveCurrentTarget:
    async def test_job_target_is_the_context_run_id(self, sessions) -> None:
        run_id = uuid.uuid4()
        resolved = await _aresolve_current_target(
            sessions, _context(SERVER_A, run_id), "job"
        )
        assert resolved == run_id

    async def test_conversation_target_resolves_the_active_link(
        self, sessions
    ) -> None:
        conversation_id = (
            await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        ).conversation_id
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        resolved = await _aresolve_current_target(
            sessions, _context(SERVER_A, run_id), "conversation"
        )
        assert resolved == conversation_id

    async def test_cross_server_conversation_is_invisible(self, sessions) -> None:
        conversation_id = (
            await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_B))
        ).conversation_id
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=uuid.UUID(SERVER_B),
            run_id=run_id,
        )
        with pytest.raises(InternalApiError) as excinfo:
            await _aresolve_current_target(
                sessions, _context(SERVER_A, run_id), "conversation"
            )
        assert excinfo.value.code == "not_found"

    async def test_released_link_does_not_resolve(self, sessions) -> None:
        conversation_id = (
            await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        ).conversation_id
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        await arelease_run(
            sessions,
            conversation_id=conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        with pytest.raises(InternalApiError) as excinfo:
            await _aresolve_current_target(
                sessions, _context(SERVER_A, run_id), "conversation"
            )
        assert excinfo.value.code == "not_found"

    async def test_unbound_run_reports_honest_not_found(self, sessions) -> None:
        with pytest.raises(InternalApiError) as excinfo:
            await _aresolve_current_target(
                sessions, _context(SERVER_C, uuid.uuid4()), "conversation"
            )
        assert excinfo.value.code == "not_found"


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
        payload = {"target": "job", "tag_id": str(tag.tag_id)}

        result, document = await _execute(
            composition, TAGS_ASSIGN_CAPABILITY, payload, context
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document == {
            "status": "ok",
            "ok": True,
            "target": "job",
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

    async def test_conversation_assign_targets_the_active_link(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_A)
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        tag = await acreate_tag(sessions, name=f"w4a2-conv-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"target": "conversation", "tag_id": str(tag.tag_id)},
            _context(SERVER_A, run_id),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["entity_id"] == str(conversation.conversation_id)
        assert document["target"] == "conversation"

        rows, total = await atags_for_conversation(
            sessions,
            conversation.conversation_id,
            server_id=uuid.UUID(SERVER_A),
        )
        assert total >= 1
        assigned = [row for row in rows if row.tag_id == tag.tag_id]
        assert len(assigned) == 1
        assert assigned[0].assignment_source == "agent"

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
                "target": "job",
                "tag_id": RESERVED_UNCLASSIFIED_TAG_ID,
                "replacement_tag_id": str(replacement.tag_id),
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document == {
            "status": "ok",
            "ok": True,
            "target": "job",
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
            {"target": "job", "tag_id": str(replacement.tag_id)},
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
        payload = {"target": "job", "tag_id": str(tag.tag_id)}
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
            {"target": "job", "tag_id": str(tag.tag_id)},
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
            {"target": "job", "tag_id": str(uuid.uuid4())},
            _context(SERVER_A, run_id),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "not_found"

    async def test_cross_server_conversation_assign_is_refused(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_B)
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=uuid.UUID(SERVER_B),
            run_id=run_id,
        )
        tag = await acreate_tag(sessions, name=f"w4a2-xconv-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"target": "conversation", "tag_id": str(tag.tag_id)},
            _context(SERVER_A, run_id),  # intruding from another server
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "not_found"

    async def test_conversation_remove_is_idempotent(self, sessions) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_A)
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        tag = await acreate_tag(sessions, name=f"w4a2-convrm-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)
        await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"target": "conversation", "tag_id": str(tag.tag_id)},
            context,
        )
        payload = {"target": "conversation", "tag_id": str(tag.tag_id)}
        for _ in range(2):
            result, document = await _execute(
                composition, TAGS_REMOVE_CAPABILITY, payload, context
            )
            assert result.ok is True  # type: ignore[attr-defined]
            assert document["entity_id"] == str(conversation.conversation_id)
            assert document["replacement_tag_id"] is None


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
            {"target": "job", "limit": 1, "offset": 0},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert first["status"] == "ok" and first["ok"] is True
        assert first["target"] == "job" and first["entity_id"] == str(run_id)
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
            {"target": "job", "limit": 1, "offset": first["next_offset"]},
            context,
        )
        assert [item["name"] for item in second["items"]] == [expected_order[1]]
        assert second["has_more"] is True and second["next_offset"] == 2

        # Final page ends the sequence: has_more False, next_offset None.
        _, final = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"target": "job", "limit": 1, "offset": second["next_offset"]},
            context,
        )
        assert [item["name"] for item in final["items"]] == [expected_order[2]]
        assert final["total"] == 3
        assert final["has_more"] is False and final["next_offset"] is None

        # Beyond the total: an honest empty page, not a refusal.
        _, beyond = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"target": "job", "limit": 1, "offset": 3},
            context,
        )
        assert beyond["items"] == []
        assert beyond["total"] == 3
        assert beyond["has_more"] is False and beyond["next_offset"] is None

    async def test_conversation_page_targets_active_link_with_typed_rows(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_A)
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        tag = await acreate_tag(sessions, name=f"w4a3-conv-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, run_id)
        assigned, _ = await _execute(
            composition,
            TAGS_ASSIGN_CAPABILITY,
            {"target": "conversation", "tag_id": str(tag.tag_id)},
            context,
        )
        assert assigned.ok is True  # type: ignore[attr-defined]

        result, document = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"target": "conversation"},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["status"] == "ok"
        assert document["target"] == "conversation"
        assert document["entity_id"] == str(conversation.conversation_id)
        assert document["limit"] == TAGS_LIST_DEFAULT_LIMIT
        assert document["offset"] == 0
        assert document["total"] >= 1
        assert document["has_more"] is False and document["next_offset"] is None
        mine = next(
            item
            for item in document["items"]
            if item["tag_id"] == str(tag.tag_id)
        )
        assert mine["name"] == tag.name
        assert mine["assigned_by"] == "principal"
        assert mine["assignment_source"] == "agent"
        datetime.fromisoformat(mine["created_at"])

    async def test_cross_server_conversation_list_refuses_not_found(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_B)
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=uuid.UUID(SERVER_B),
            run_id=run_id,
        )
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_LIST_FOR_CAPABILITY,
            {"target": "conversation"},
            _context(SERVER_A, run_id),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "not_found"


# ---------------------------------------------------------------------------
# W4B — catalog tools: create / list_catalog / delete (real Postgres)
# ---------------------------------------------------------------------------


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
            {"target": "job", "tag_id": tag_id},
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
# W4C-CONV — tags.search_conversations: same-server tag search (real PG)
# ---------------------------------------------------------------------------


def _insert_conversation(
    url: str,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID = uuid.UUID(SERVER_A),
    updated_at: datetime | None = None,
) -> None:
    """One real conversations row seeded with the reserved unclassified tag."""
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO conversations (conversation_id, server_id, updated_at)
            VALUES (%s, %s, COALESCE(%s, now()))
            """,
            (conversation_id, server_id, updated_at),
        )
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) "
            "VALUES (%s, %s)",
            (conversation_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


async def _assign_tag_to_conversation(
    sessions: object, conversation_id: uuid.UUID, tag_id: uuid.UUID
) -> None:
    await aassign_conversation_tag(
        sessions,  # type: ignore[arg-type]
        conversation_id,
        tag_id,
        server_id=uuid.UUID(SERVER_A),
        assigned_by="principal",
        assignment_source="agent",
    )


@pytest.mark.anyio
class TestSearchConversationsWired:
    async def test_any_and_all_match_same_server_newest_first(
        self, sessions, migrated_url
    ) -> None:
        base = datetime(2026, 10, 30, 12, 0, 0)
        c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        _insert_conversation(migrated_url, c1, updated_at=base)
        _insert_conversation(
            migrated_url, c2, updated_at=base + timedelta(minutes=1)
        )
        _insert_conversation(
            migrated_url, c3, updated_at=base + timedelta(minutes=2)
        )
        tag_a = await acreate_tag(sessions, name=f"w4cc-a-{uuid.uuid4()}")
        tag_b = await acreate_tag(sessions, name=f"w4cc-b-{uuid.uuid4()}")
        tag_c = await acreate_tag(sessions, name=f"w4cc-c-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        for conversation_id, tag_id in (
            (c1, tag_a.tag_id),
            (c2, tag_a.tag_id),
            (c2, tag_b.tag_id),
            (c3, tag_b.tag_id),
        ):
            await _assign_tag_to_conversation(sessions, conversation_id, tag_id)
        # Another server's conversation carrying tag_a is invisible to
        # this search.
        foreign = uuid.uuid4()
        _insert_conversation(
            migrated_url,
            foreign,
            server_id=uuid.UUID(SERVER_B),
            updated_at=base + timedelta(minutes=3),
        )
        await aassign_conversation_tag(
            sessions,
            foreign,
            tag_a.tag_id,
            server_id=uuid.UUID(SERVER_B),
            assigned_by="principal",
            assignment_source="agent",
        )

        result, any_doc = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {"tag_ids": [str(tag_a.tag_id), str(tag_b.tag_id)]},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert any_doc["status"] == "ok" and any_doc["ok"] is True
        assert any_doc["match"] == "any"
        assert any_doc["total"] == 3
        assert [item["conversation_id"] for item in any_doc["items"]] == [
            str(c3),
            str(c2),
            str(c1),
        ]
        assert any_doc["limit"] == TAGS_SEARCH_DEFAULT_LIMIT
        assert any_doc["offset"] == 0
        assert any_doc["has_more"] is False and any_doc["next_offset"] is None
        row = any_doc["items"][0]
        assert set(row) == {"conversation_id", "updated_at", "tag_preview"}
        assert str(uuid.UUID(row["conversation_id"])) == row["conversation_id"]
        datetime.fromisoformat(row["updated_at"])
        assert set(row["tag_preview"]) == {"preview", "total"}
        assert row["tag_preview"]["total"] == 2  # tag_b + reserved seed
        assert set(row["tag_preview"]["preview"]) == {tag_b.name, "unclassified"}
        c2_row = any_doc["items"][1]
        assert c2_row["tag_preview"]["total"] == 3
        assert set(c2_row["tag_preview"]["preview"]) == {
            tag_a.name,
            tag_b.name,
            "unclassified",
        }

        result, all_doc = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {
                "tag_ids": [str(tag_a.tag_id), str(tag_b.tag_id)],
                "match": "all",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert all_doc["match"] == "all"
        assert all_doc["total"] == 1
        assert [item["conversation_id"] for item in all_doc["items"]] == [str(c2)]

        # No conversation carries both a and c: an honest empty page,
        # not a refusal.
        result, empty = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {
                "tag_ids": [str(tag_a.tag_id), str(tag_c.tag_id)],
                "match": "all",
            },
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert empty["items"] == [] and empty["total"] == 0
        assert empty["has_more"] is False and empty["next_offset"] is None

    async def test_zero_uuid_general_consult_scope_is_hermetic(
        self, sessions, migrated_url
    ) -> None:
        base = datetime(2026, 10, 30, 15, 0, 0)
        consult = uuid.uuid4()
        host = uuid.uuid4()
        tag = await acreate_tag(sessions, name=f"w4cc-gc-{uuid.uuid4()}")
        _insert_conversation(
            migrated_url,
            consult,
            server_id=uuid.UUID(int=0),
            updated_at=base,
        )
        _insert_conversation(
            migrated_url,
            host,
            server_id=uuid.UUID(SERVER_A),
            updated_at=base + timedelta(minutes=1),
        )
        await aassign_conversation_tag(
            sessions,
            consult,
            tag.tag_id,
            server_id=uuid.UUID(int=0),
            assigned_by="principal",
            assignment_source="agent",
        )
        await aassign_conversation_tag(
            sessions,
            host,
            tag.tag_id,
            server_id=uuid.UUID(SERVER_A),
            assigned_by="principal",
            assignment_source="agent",
        )
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)]},
            _context("00000000-0000-0000-0000-000000000000", uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert document["match"] == "any"
        assert document["total"] == 1
        assert [item["conversation_id"] for item in document["items"]] == [
            str(consult)
        ]
        # The host server's conversation carrying the same tag never
        # leaks into the General Consult scope.
        assert str(host) not in [
            item["conversation_id"] for item in document["items"]
        ]

    async def test_pagination_slices_newest_first_with_honest_meta(
        self, sessions, migrated_url
    ) -> None:
        base = datetime(2026, 10, 30, 13, 0, 0)
        conversations = [uuid.uuid4() for _ in range(4)]
        for index, conversation_id in enumerate(conversations):
            _insert_conversation(
                migrated_url,
                conversation_id,
                updated_at=base + timedelta(minutes=index),
            )
        tag = await acreate_tag(sessions, name=f"w4cc-page-{uuid.uuid4()}")
        composition = build_history_tag_capabilities(session_factory=sessions)
        context = _context(SERVER_A, uuid.uuid4())
        for conversation_id in conversations:
            await _assign_tag_to_conversation(
                sessions, conversation_id, tag.tag_id
            )
        newest_first = [
            str(conversation_id) for conversation_id in reversed(conversations)
        ]

        result, first = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)], "limit": 2, "offset": 0},
            context,
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert first["total"] == 4
        assert [item["conversation_id"] for item in first["items"]] == (
            newest_first[:2]
        )
        assert first["limit"] == 2 and first["offset"] == 0
        assert first["has_more"] is True and first["next_offset"] == 2

        _, second = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
            {"tag_ids": [str(tag.tag_id)], "limit": 2, "offset": 2},
            context,
        )
        assert [item["conversation_id"] for item in second["items"]] == (
            newest_first[2:]
        )
        assert second["total"] == 4
        assert second["has_more"] is False and second["next_offset"] is None

        _, beyond = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
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
                composition,
                TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
                payload,
                context,
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "invalid_request"

    async def test_maximal_page_envelope_stays_under_60000_bytes(
        self, sessions, migrated_url
    ) -> None:
        stem = f"w4cc-{uuid.uuid4().hex[:8]}"
        tag_ids: list[str] = []
        for index in range(50):
            base = f"{stem}-{index:02d}-"
            name = base + "x" * (TAGS_NAME_MAX_LENGTH - len(base))
            assert len(name) == 64
            tag = await acreate_tag(sessions, name=name)
            tag_ids.append(str(tag.tag_id))
        for index, tag_id in enumerate(tag_ids):
            conversation_id = uuid.uuid4()
            _insert_conversation(
                migrated_url,
                conversation_id,
                updated_at=datetime(
                    2026, 10, 30, 16, index % 60, index // 60
                ),
            )
            await aassign_conversation_tag(
                sessions,
                conversation_id,
                uuid.UUID(tag_id),
                server_id=uuid.UUID(SERVER_A),
                assigned_by="principal",
                assignment_source="agent",
            )
        composition = build_history_tag_capabilities(session_factory=sessions)
        result, document = await _execute(
            composition,
            TAGS_SEARCH_CONVERSATIONS_CAPABILITY,
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
# W4C2-CONV — tags.read_conversation over the reviewed content reader
# ---------------------------------------------------------------------------


def _conv_insert(
    url: str,
    conv_id: uuid.UUID,
    server_id: uuid.UUID,
    title: str = "test conv",
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, server_id, title) "
            "VALUES (%s, %s, %s)",
            (conv_id, server_id, title),
        )
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) "
            "VALUES (%s, %s)",
            (conv_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


def _msg_insert(
    url: str,
    conv_id: uuid.UUID,
    msg_id: uuid.UUID,
    seq: int,
    content: str,
    role: str = "user",
    source_kind: str = "browser",
    redacted: bool = False,
    content_ref: str | None = None,
) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO conversation_messages "
            "(message_id, conversation_id, seq, role, source_kind, content, "
            "content_fingerprint, redacted, content_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                msg_id,
                conv_id,
                seq,
                role,
                source_kind,
                content,
                "x" * 64,
                redacted,
                content_ref,
            ),
        )


def _read(
    sessions: object,
    payload: dict[str, object],
    context: ExecutionContext,
) -> tuple[object, dict[str, object]]:
    return _execute(
        build_history_tag_capabilities(session_factory=sessions),  # type: ignore[arg-type]
        TAGS_READ_CONVERSATION_CAPABILITY,
        payload,
        context,
    )


@pytest.mark.anyio
class TestReadConversationContent:
    async def test_exact_continuation_within_one_message(
        self, sessions, migrated_url
    ) -> None:
        conv, msg = uuid.uuid4(), uuid.uuid4()
        text = "Hello 世界 🌍 " * 40
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        _msg_insert(migrated_url, conv, msg, 1, text)

        collected: list[str] = []
        offset, cursor, pages = 0, 0, 0
        while True:
            result, page = await _read(
                sessions,
                {
                    "conversation_id": str(conv),
                    "max_chars": 7,
                    "offset": offset,
                    "char_cursor": cursor,
                },
                _context(SERVER_A, uuid.uuid4()),
            )
            assert result.ok is True  # type: ignore[attr-defined]
            assert page["conversation_id"] == str(conv)
            assert page["max_chars"] == 7
            collected.extend(window["content"] for window in page["items"])
            pages += 1
            if not page["has_more"]:
                assert page["next_offset"] is None
                assert page["next_char_cursor"] is None
                break
            offset, cursor = page["next_offset"], page["next_char_cursor"]
        assert pages > 1
        assert page["total"] == 1
        assert "".join(collected) == text

    async def test_exact_continuation_across_messages(
        self, sessions, migrated_url
    ) -> None:
        conv = uuid.uuid4()
        texts = ["alpha-0123456789", "", "beta-" + "x" * 30, "gamma"]
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        for index, text in enumerate(texts, start=1):
            _msg_insert(migrated_url, conv, uuid.uuid4(), index * 3, text)

        result, full = await _read(
            sessions,
            {"conversation_id": str(conv)},
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert [window["content"] for window in full["items"]] == texts
        assert full["total"] == 4
        assert full["has_more"] is False
        assert full["next_offset"] is None
        assert full["next_char_cursor"] is None

        collected: list[str] = []
        visited: list[uuid.UUID] = []
        offset, cursor, pages = 0, 0, 0
        while True:
            _, page = await _read(
                sessions,
                {
                    "conversation_id": str(conv),
                    "limit": 2,
                    "max_chars": 10,
                    "offset": offset,
                    "char_cursor": cursor,
                },
                _context(SERVER_A, uuid.uuid4()),
            )
            collected.extend(window["content"] for window in page["items"])
            visited.extend(
                uuid.UUID(window["message_id"]) for window in page["items"]
            )
            # Each page stays within the requested message bound.
            assert len(page["items"]) <= 2
            pages += 1
            if not page["has_more"]:
                break
            offset, cursor = page["next_offset"], page["next_char_cursor"]
        assert "".join(collected) == "".join(texts)
        assert pages > 1
        # The walk touches every stored message and nothing else (the
        # empty message surfaces as its own zero-char window, per the
        # reader contract); a message split by a page boundary recurs
        # on the resuming page, so set equality — not list equality —
        # is the no-skip/no-foreign-id invariant.
        with psycopg.connect(migrated_url) as conn:
            stored_ids = [
                row[0]
                for row in conn.cursor()
                .execute(
                    "SELECT message_id FROM conversation_messages "
                    "WHERE conversation_id = %s ORDER BY seq, message_id",
                    (conv,),
                )
                .fetchall()
            ]
        assert set(visited) == set(stored_ids)

    async def test_redacted_and_content_ref_preserved_verbatim(
        self, sessions, migrated_url
    ) -> None:
        conv, msg = uuid.uuid4(), uuid.uuid4()
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        _msg_insert(
            migrated_url,
            conv,
            msg,
            1,
            "[redacted] s3cret",
            role="assistant",
            source_kind="api",
            redacted=True,
            content_ref="runs/abc/turn-1",
        )

        result, page = await _read(
            sessions,
            {"conversation_id": str(conv)},
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        window = page["items"][0]
        assert window["message_id"] == str(msg)
        assert window["seq"] == 1
        assert window["role"] == "assistant"
        assert window["source_kind"] == "api"
        assert window["content"] == "[redacted] s3cret"
        assert window["redacted"] is True
        assert window["content_ref"] == "runs/abc/turn-1"
        # The accepted page model verbatim: nothing invented.
        assert "char_cursor" not in window
        assert "created_at" not in window
        assert "client_request_id" not in window

    async def test_zero_uuid_server_reads_its_real_conversation(
        self, sessions, migrated_url
    ) -> None:
        zero = uuid.UUID(int=0)
        conv = uuid.uuid4()
        _conv_insert(migrated_url, conv, zero, title="General Consult")
        _msg_insert(migrated_url, conv, uuid.uuid4(), 1, "consult body")

        result, page = await _read(
            sessions,
            {"conversation_id": str(conv)},
            _context(str(zero), uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert page["conversation_id"] == str(conv)
        assert page["items"][0]["content"] == "consult body"
        assert page["total"] == 1
        assert page["has_more"] is False

    async def test_cross_server_and_missing_are_identically_not_found(
        self, sessions, migrated_url
    ) -> None:
        conv_b = uuid.uuid4()
        _conv_insert(
            migrated_url, conv_b, uuid.UUID(SERVER_B), title="other server"
        )
        conv_zero = uuid.uuid4()
        _conv_insert(
            migrated_url, conv_zero, uuid.UUID(int=0), title="General Consult"
        )

        documents = []
        for conversation_id in (conv_b, conv_zero, uuid.uuid4()):
            result, document = await _read(
                sessions,
                {"conversation_id": str(conversation_id)},
                _context(SERVER_A, uuid.uuid4()),
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            documents.append(document)
        assert all(document["code"] == "not_found" for document in documents)
        # A real other-server conversation and a missing one are
        # indistinguishable on the wire.
        assert len({document["message"] for document in documents}) == 1
        assert documents[0]["message"] == "conversation not found"

    async def test_char_cursor_beyond_message_is_invalid_request(
        self, sessions, migrated_url
    ) -> None:
        """Same-server cursor past message length refuses invalid_request,
        while cross-server/missing conversations stay not_found."""
        conv, msg = uuid.uuid4(), uuid.uuid4()
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        _msg_insert(migrated_url, conv, msg, 1, "short")

        result, document = await _read(
            sessions,
            {"conversation_id": str(conv), "char_cursor": 100},
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "invalid_request"
        assert document["message"] == (
            "char_cursor is beyond the stored message content; "
            "retry with a returned continuation cursor"
        )

        conv_b = uuid.uuid4()
        _conv_insert(migrated_url, conv_b, uuid.UUID(SERVER_B))
        for other_id in (conv_b, uuid.uuid4()):
            result, document = await _read(
                sessions,
                {"conversation_id": str(other_id), "char_cursor": 100},
                _context(SERVER_A, uuid.uuid4()),
            )
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "not_found"
            assert document["message"] == "conversation not found"

    async def test_invalid_bounds_refuse_invalid_request(
        self, sessions
    ) -> None:
        context = _context(SERVER_A, uuid.uuid4())
        for payload in (
            {"conversation_id": str(uuid.uuid4()), "limit": 51},
            {"conversation_id": str(uuid.uuid4()), "limit": 0},
            {"conversation_id": str(uuid.uuid4()), "offset": -1},
            {"conversation_id": str(uuid.uuid4()), "char_cursor": -1},
            {"conversation_id": str(uuid.uuid4()), "max_chars": 0},
            {
                "conversation_id": str(uuid.uuid4()),
                "max_chars": MAX_CONTENT_CHARS + 1,
            },
            {"conversation_id": "not-a-uuid"},
        ):
            result, document = await _read(sessions, payload, context)
            assert result.ok is False  # type: ignore[attr-defined]
            assert document["status"] == "refused"
            assert document["code"] == "invalid_request"

    async def test_max_size_ascii_page_stays_under_envelope_budget(
        self, sessions, migrated_url
    ) -> None:
        conv = uuid.uuid4()
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        _msg_insert(
            migrated_url, conv, uuid.uuid4(), 1, "a" * MAX_CONTENT_CHARS
        )

        result, page = await _read(
            sessions,
            {
                "conversation_id": str(conv),
                "max_chars": MAX_CONTENT_CHARS,
            },
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is True  # type: ignore[attr-defined]
        assert page["items"][0]["content"] == "a" * MAX_CONTENT_CHARS
        assert len(result.output.encode("utf-8")) < 60_000  # type: ignore[attr-defined]

    async def test_oversize_unicode_page_refuses_typed_not_clamped(
        self, sessions, migrated_url
    ) -> None:
        conv = uuid.uuid4()
        _conv_insert(migrated_url, conv, uuid.UUID(SERVER_A))
        _msg_insert(
            migrated_url, conv, uuid.uuid4(), 1, "🌍" * MAX_CONTENT_CHARS
        )

        result, document = await _read(
            sessions,
            {
                "conversation_id": str(conv),
                "max_chars": MAX_CONTENT_CHARS,
            },
            _context(SERVER_A, uuid.uuid4()),
        )
        assert result.ok is False  # type: ignore[attr-defined]
        assert document["status"] == "refused"
        assert document["code"] == "invalid_request"
        # No silent clamping and no content leak through the refusal.
        assert "items" not in document
