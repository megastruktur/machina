"""Focused tests for the ``tags.*`` capability scaffold (card W4A1).

Descriptor/contract claims are pure build-time tests (no database). The
current-target resolver claims reuse the disposable real-Postgres
harness of ``test_task_capabilities.py``: set ``MAI_TEST_DATABASE_URL``
to a fresh disposable pgvector/pg18 database (the harness applies the
linear Alembic chain from zero); without it those tests skip.
"""

from __future__ import annotations

import json
import uuid

import pytest

from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    arelease_run,
)
from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.history_tags import (
    TAGS_ASSIGN_CAPABILITY,
    TAGS_CAPABILITY_IDS,
    TAGS_LIST_FOR_CAPABILITY,
    TAGS_LIST_MAX_LIMIT,
    TAGS_REMOVE_CAPABILITY,
    _aresolve_current_target,
    build_history_tag_capabilities,
)
from mai.capabilities.internal_api import (
    InternalApiComposition,
    InternalApiError,
    assert_agent_ready_descriptor,
)
from mai.policy.evaluate import ExecutionContext, PolicyMode

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
        for descriptor in composition.descriptors:
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
        ):
            props = by_id[capability_id].input_schema["properties"]
            assert "replacement_tag_id" not in props

    def test_list_for_declares_bounded_pagination(self) -> None:
        composition = _composition()
        by_id = {d.identifier: d for d in composition.descriptors}
        props = by_id[TAGS_LIST_FOR_CAPABILITY].input_schema["properties"]
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == TAGS_LIST_MAX_LIMIT
        assert props["offset"]["minimum"] == 0


# ---------------------------------------------------------------------------
# AC 2 — W4A1 executors are honest placeholders: every operation call is
# refused ``unavailable``, with or without a valid store available
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

    async def test_refuses_unavailable_even_with_store_available(
        self, sessions
    ) -> None:
        composition = build_history_tag_capabilities(session_factory=sessions)
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
