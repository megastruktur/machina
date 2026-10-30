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
from datetime import datetime

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
    TAGS_REMOVE_CAPABILITY,
    TagsAssignPayload,
    TagsCreatePayload,
    TagsListCatalogPayload,
    TagsRemovePayload,
    _aresolve_current_target,
    build_history_tag_capabilities,
)
from mai.capabilities.internal_api import (
    InternalApiComposition,
    InternalApiError,
    assert_agent_ready_descriptor,
)
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
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
            assert "conversation_id" not in props
            assert "run_id" not in props

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
    url: str, job_id: uuid.UUID, server_id: uuid.UUID = uuid.UUID(SERVER_A)
) -> None:
    """One real job_runs row seeded with the reserved unclassified tag."""
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
            """,
            (job_id, server_id),
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
