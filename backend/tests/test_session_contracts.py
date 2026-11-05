"""Focused tests for the D01 session-first contracts (campaign
``machina-session-first-chat``, task D01).

Acceptance mapped to BRIEF/scenarios:

1. **Identity** — a ``run_id`` is never reusable as a ``session_id``
   (helper + model validators on entry/run/admission/delegation models);
   parent/child edges are distinct; cross-server scope fails closed with
   ``session_not_found`` and never leaks cross-server existence.
2. **Atomic tool groups** — matching call/result pairs validate; malformed,
   unmatched, duplicated, cross-session and cross-scope groups fail closed
   with stable value-free codes; ``partition_replayable_tool_groups`` keeps
   complete groups replayable, marks incomplete/pending groups honestly and
   excludes them from native replay without hiding safe assistant text.
3. **Bounded pagination** — history envelopes enforce count and byte bounds
   with strict sequence ordering and honest cursor/continuation metadata;
   the session list carries the canonical envelope with consistent
   ``has_more``.
4. **Neutral vocabularies** — the tag and tool-result contracts are owned by
   the neutral modules and every retained consumer/re-export is the same
   object; ``agents/tools.py`` has zero imports from the old chat/task/
   objective contracts while its phase gate still mirrors the ratified
   phase×action admission map exactly; approval/effect-certainty vocabulary
   values are unchanged.
5. **Contract hygiene** — every Pydantic model in the session contract
   forbids extra fields, describes every field (≥ 12 chars) and fails
   closed on schema-version drift; authority-shaped fields are
   unrepresentable on client request models.

Pure contract tests: no database, no network, no clock — fixed opaque UUIDs
and synthetic text only.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import uuid

import pytest
from pydantic import BaseModel, ValidationError

import mai.contracts.tags as tags_contract
import mai.contracts.tasks as tasks_contract
import mai.contracts.tool_results as tool_results_contract
from mai.agents import tools as agent_tools
from mai.contracts.sessions import (
    FORBIDDEN_AUTHORITY_FIELDS,
    HISTORY_PAGE_MAX_ENTRIES,
    MAX_ENTRY_TEXT_CHARS,
    MAX_PAGE_BYTES,
    MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    INITIAL_TAIL_MAX_ENTRIES,
    SESSION_LIST_DEFAULT_LIMIT,
    SESSION_LIST_MAX_LIMIT,
    SCHEMA_VERSION,
    AssistantMessageEntry,
    CompactionEntry,
    ControlActionKind,
    ControlEntry,
    DelegationEntry,
    DelegationResult,
    DelegationSpec,
    DocumentAttachAdmissionRequest,
    EntryCursor,
    EntryKind,
    RunStateEntry,
    RunbookRef,
    RunbookSessionExecuteRequest,
    SessionCompactionRecord,
    SessionContractError,
    SessionControlRequest,
    SessionControlResponse,
    SessionCreateRequest,
    SessionDetail,
    SessionEntryView,
    SessionErrorCode,
    SessionHistoryPage,
    SessionListPage,
    ProviderReasoningArtifact,
    SessionCompactionRecord,
    SessionRunRecord,
    SessionRunState,
    SessionSourceKind,
    SessionStatus,
    SessionSummary,
    SessionTurnSubmitRequest,
    SessionTurnSubmitResponse,
    ToolCallItem,
    ToolResultEntry,
    UserMessageEntry,
    assert_session_not_run,
    assert_session_scope,
    partition_replayable_tool_groups,
    validate_atomic_tool_group,
)
from mai.contracts.tool_results import DomainOutcome, EffectCertainty, ExecutionStatus

SESSION_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")

ENTRY_BASE_INT = 0x1000
REQUEST_BASE_INT = 0x2000


def _entry_id(n: int) -> uuid.UUID:
    return uuid.UUID(int=ENTRY_BASE_INT + n)


def _request_id(n: int) -> uuid.UUID:
    return uuid.UUID(int=REQUEST_BASE_INT + n)


def _user_entry(
    n: int,
    *,
    session_id: uuid.UUID = SESSION_A,
    run_id: uuid.UUID | None = None,
    seq: int,
) -> SessionEntryView:
    return SessionEntryView(
        entry_id=_entry_id(n),
        session_id=session_id,
        seq=seq,
        run_id=run_id,
        durable_source_id=f"user-src-{n}",
        created_at=0,
        payload=UserMessageEntry(
            kind=EntryKind.USER_MESSAGE,
            text=f"synthetic user turn {n}",
            client_request_id=_request_id(n),
            source=SessionSourceKind.INTERACTIVE,
        ),
    )


def _assistant_entry(
    n: int,
    *,
    call_ids: list[str],
    session_id: uuid.UUID = SESSION_A,
    run_id: uuid.UUID = RUN_1,
    seq: int,
    text: str = "",
) -> SessionEntryView:
    payload = AssistantMessageEntry(
        kind=EntryKind.ASSISTANT_MESSAGE,
        text=text,
        tool_calls=[
            ToolCallItem(
                call_id=call_id,
                tool_name="host.logs",
                capability_id="host.logs",
                args={"tail": 10},
            )
            for call_id in call_ids
        ],
        model_profile="default",
    )
    return SessionEntryView(
        entry_id=_entry_id(n),
        session_id=session_id,
        seq=seq,
        run_id=run_id,
        durable_source_id=f"assistant-src-{n}",
        created_at=0,
        payload=payload,
    )


def _tool_result_entry(
    n: int,
    *,
    call_id: str,
    session_id: uuid.UUID = SESSION_A,
    run_id: uuid.UUID = RUN_1,
    seq: int,
) -> SessionEntryView:
    return SessionEntryView(
        entry_id=_entry_id(n),
        session_id=session_id,
        seq=seq,
        run_id=run_id,
        durable_source_id=f"tool-src-{n}",
        created_at=0,
        payload=ToolResultEntry(
            kind=EntryKind.TOOL_RESULT,
            call_id=call_id,
            tool_name="host.logs",
            capability_id="host.logs",
            status="executed",
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.SUCCESS,
            effect_certainty=EffectCertainty.CERTAIN,
            payload_hash="a" * 64,
            output_preview="synthetic bounded preview",
        ),
    )


# ---------------------------------------------------------------------------
# 1. identity and scope
# ---------------------------------------------------------------------------


def test_session_as_run_identity_is_rejected_by_helper() -> None:
    with pytest.raises(SessionContractError) as err:
        assert_session_not_run(RUN_1, RUN_1)
    assert err.value.fields == {
        "session_id": SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT
    }
    assert_session_not_run(SESSION_A, RUN_1)  # distinct identities pass


def test_scope_guard_fails_closed_without_leaking() -> None:
    with pytest.raises(SessionContractError) as err:
        assert_session_scope(SERVER_B, SERVER_A)
    # Fail-closed, scope-blind: the SAME not-found code a missing session in
    # scope produces — no cross-server existence leak.
    assert err.value.fields == {"server_id": SessionErrorCode.SESSION_NOT_FOUND}
    assert_session_scope(SERVER_A, SERVER_A)


def test_entry_view_rejects_run_id_as_session_id() -> None:
    with pytest.raises(ValidationError):
        _user_entry(
            1,
            session_id=RUN_1,  # session identity reusing a run id
            run_id=RUN_1,
            seq=1,
        )
    entry = _user_entry(1, run_id=RUN_1, seq=1)
    assert entry.session_id == SESSION_A and entry.run_id == RUN_1


def test_run_record_rejects_identity_and_self_parent() -> None:
    with pytest.raises(ValidationError):
        SessionRunRecord(
            run_id=SESSION_A,
            session_id=SESSION_A,
            server_id=SERVER_A,
            state=SessionRunState.QUEUED,
            model_profile="default",
            client_request_id=_request_id(1),
            created_at=0,
            updated_at=0,
        )
    record = SessionRunRecord(
        run_id=RUN_1,
        session_id=SESSION_A,
        server_id=SERVER_A,
        parent_run_id=RUN_2,
        state=SessionRunState.QUEUED,
        model_profile="default",
        client_request_id=_request_id(1),
        created_at=0,
        updated_at=0,
    )
    assert record.parent_run_id == RUN_2
    with pytest.raises(ValidationError):  # a run is never its own parent
        SessionRunRecord(
            run_id=RUN_1,
            session_id=SESSION_A,
            server_id=SERVER_A,
            parent_run_id=RUN_1,
            state=SessionRunState.QUEUED,
            model_profile="default",
            client_request_id=_request_id(1),
            created_at=0,
            updated_at=0,
        )
    with pytest.raises(ValidationError):  # parent identity may not be the session
        SessionRunRecord(
            run_id=RUN_1,
            session_id=SESSION_A,
            server_id=SERVER_A,
            parent_run_id=SESSION_A,
            state=SessionRunState.QUEUED,
            model_profile="default",
            client_request_id=_request_id(1),
            created_at=0,
            updated_at=0,
        )


def test_admission_response_and_delegation_edges_reject_conflicts() -> None:
    with pytest.raises(ValidationError):
        SessionTurnSubmitResponse(
            session_id=RUN_1,
            run_id=RUN_1,
            entry_id=_entry_id(1),
            seq=1,
            run_state=SessionRunState.QUEUED,
        )
    with pytest.raises(ValidationError):
        DelegationResult(
            session_id=SESSION_A,
            parent_run_id=RUN_1,
            child_run_id=RUN_1,  # child == parent
            status=SessionRunState.SUCCEEDED,
        )
    with pytest.raises(ValidationError):
        DelegationResult(
            session_id=SESSION_A,
            parent_run_id=RUN_1,
            child_run_id=SESSION_A,  # child reuses the session identity
            status=SessionRunState.SUCCEEDED,
        )
    with pytest.raises(ValidationError):
        DelegationSpec(
            session_id=RUN_1,  # session identity reusing the parent run id
            parent_run_id=RUN_1,
            goal="synthetic bounded goal",
            allowed_capabilities=["host.logs"],
            max_rounds=1,
            max_tool_calls=1,
            timeout_seconds=10.0,
        )
    with pytest.raises(ValidationError):  # delegation entry: child as session
        DelegationEntry(
            kind=EntryKind.DELEGATION,
            child_run_id=RUN_2,
            child_session_id=RUN_2,
            goal="synthetic bounded goal",
            status=SessionRunState.RUNNING,
            allowed_capabilities=["host.logs"],
        )


def test_frozen_vocabularies_match_the_plan() -> None:
    assert [k.value for k in SessionSourceKind] == [
        "interactive",
        "runbook",
        "document_attach",
        "api",
    ]
    assert [k.value for k in SessionStatus] == [
        "idle",
        "running",
        "waiting_approval",
        "failed",
        "cancelled",
    ]
    assert [k.value for k in SessionRunState] == [
        "queued",
        "running",
        "waiting_approval",
        "waiting_child",
        "succeeded",
        "failed",
        "cancelled",
        "outcome_unknown",
    ]
    assert [k.value for k in EntryKind] == [
        "user_message",
        "assistant_message",
        "tool_result",
        "run_state",
        "delegation",
        "control",
        "compaction",
    ]
    assert [k.value for k in ControlActionKind] == [
        "steer",
        "pause",
        "cancel",
        "approve",
        "deny_approval",
        "reconcile",
    ]
    assert FORBIDDEN_AUTHORITY_FIELDS == frozenset(
        {"approved", "grants", "execute_now", "bypass_approval", "role", "seq", "is_system"}
    )


# ---------------------------------------------------------------------------
# 2. atomic tool groups
# ---------------------------------------------------------------------------


def test_complete_tool_group_validates_and_returns_call_order() -> None:
    assistant = _assistant_entry(1, call_ids=["call-b", "call-a"], seq=1)
    results = [
        _tool_result_entry(2, call_id="call-a", seq=2),
        _tool_result_entry(3, call_id="call-b", seq=3),
    ]
    matched = validate_atomic_tool_group(
        assistant, results, session_server_id=SERVER_A
    )
    assert matched == ("call-b", "call-a")  # assistant tool-call order


def test_tool_group_rejects_malformed_assistant() -> None:
    text_only = _assistant_entry(1, call_ids=[], text="final answer", seq=1)
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            text_only, [], session_server_id=SERVER_A
        )
    assert err.value.fields == {
        "assistant_entry": SessionErrorCode.INVALID_TOOL_GROUP
    }
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            _user_entry(1, seq=1), [], session_server_id=SERVER_A
        )
    assert err.value.fields == {
        "assistant_entry": SessionErrorCode.INVALID_TOOL_GROUP
    }


def test_tool_group_rejects_missing_and_unmatched_and_duplicate_results() -> None:
    assistant = _assistant_entry(1, call_ids=["call-1", "call-2"], seq=1)
    # missing result for call-2
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [_tool_result_entry(2, call_id="call-1", seq=2)],
            session_server_id=SERVER_A,
        )
    assert err.value.fields == {
        "call_id": SessionErrorCode.INVALID_TOOL_GROUP
    }
    # unmatched (orphan) result
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [
                _tool_result_entry(2, call_id="call-1", seq=2),
                _tool_result_entry(3, call_id="call-2", seq=3),
                _tool_result_entry(4, call_id="call-x", seq=4),
            ],
            session_server_id=SERVER_A,
        )
    assert err.value.fields == {
        "call_id": SessionErrorCode.INVALID_TOOL_GROUP
    }
    # duplicate result for the same call
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [
                _tool_result_entry(2, call_id="call-1", seq=2),
                _tool_result_entry(3, call_id="call-2", seq=3),
                _tool_result_entry(4, call_id="call-1", seq=4),
            ],
            session_server_id=SERVER_A,
        )
    assert err.value.fields == {
        "call_id": SessionErrorCode.INVALID_TOOL_GROUP
    }


def test_tool_group_rejects_run_mismatch_and_cross_session() -> None:
    assistant = _assistant_entry(1, call_ids=["call-1"], seq=1)
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [
                _tool_result_entry(
                    2, call_id="call-1", run_id=RUN_2, seq=2
                )
            ],
            session_server_id=SERVER_A,
        )
    assert err.value.fields == {"run_id": SessionErrorCode.INVALID_TOOL_GROUP}
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [
                _tool_result_entry(
                    2,
                    call_id="call-1",
                    session_id=uuid.UUID(int=99),
                    seq=2,
                )
            ],
            session_server_id=SERVER_A,
        )
    # The foreign-session result is skipped (cross_session_entry) and its call
    # consequently counts as unanswered (invalid_tool_group) — both facts are
    # reported value-free.
    assert err.value.fields == {
        "session_id": SessionErrorCode.CROSS_SESSION_ENTRY,
        "call_id": SessionErrorCode.INVALID_TOOL_GROUP,
    }


def test_tool_group_rejects_cross_scope_targets() -> None:
    payload = AssistantMessageEntry(
        kind=EntryKind.ASSISTANT_MESSAGE,
        tool_calls=[
            ToolCallItem(
                call_id="call-1",
                tool_name="host.shell",
                capability_id="host.shell",
                args={},
                target_server_id=SERVER_B,  # outside the session scope
            )
        ],
        model_profile="default",
    )
    assistant = SessionEntryView(
        entry_id=_entry_id(1),
        session_id=SESSION_A,
        seq=1,
        run_id=RUN_1,
        durable_source_id="assistant-src-1",
        created_at=0,
        payload=payload,
    )
    with pytest.raises(SessionContractError) as err:
        validate_atomic_tool_group(
            assistant,
            [_tool_result_entry(2, call_id="call-1", seq=2)],
            session_server_id=SERVER_A,
        )
    assert err.value.fields == {
        "tool_calls": SessionErrorCode.CROSS_SCOPE_TOOL_GROUP
    }


def test_partition_keeps_complete_groups_and_passes_other_traffic() -> None:
    run_state_view = SessionEntryView(
        entry_id=_entry_id(4),
        session_id=SESSION_A,
        seq=5,
        run_id=RUN_1,
        durable_source_id="run-src-1",
        created_at=0,
        payload=RunStateEntry(
            kind=EntryKind.RUN_STATE,
            state=SessionRunState.SUCCEEDED,
        ),
    )
    control_view = SessionEntryView(
        entry_id=_entry_id(5),
        session_id=SESSION_A,
        seq=6,
        run_id=RUN_1,
        durable_source_id="control-src-1",
        created_at=0,
        payload=ControlEntry(
            kind=EntryKind.CONTROL,
            action=ControlActionKind.CANCEL,
            client_request_id=_request_id(5),
        ),
    )
    window = [
        _user_entry(0, seq=1),
        _assistant_entry(1, call_ids=["call-1"], seq=2, text="checking logs"),
        _tool_result_entry(2, call_id="call-1", seq=3),
        _assistant_entry(3, call_ids=[], text="all done", seq=4),
        run_state_view,
        control_view,
    ]
    partition = partition_replayable_tool_groups(window, session_server_id=SERVER_A)
    assert partition.pending == ()
    assert [e.seq for e in partition.replayable] == [1, 2, 3, 4, 5, 6]
    # the complete group kept both members in chronological order
    assert isinstance(partition.replayable[1].payload, AssistantMessageEntry)
    assert isinstance(partition.replayable[2].payload, ToolResultEntry)


def test_partition_marks_incomplete_groups_and_excludes_partial_results() -> None:
    assistant = _assistant_entry(
        1, call_ids=["call-1", "call-2"], seq=2, text="working on it"
    )
    window = [
        _user_entry(0, seq=1),
        assistant,
        _tool_result_entry(2, call_id="call-1", seq=3),
        _user_entry(3, seq=4),  # a user turn SPLITS the atomic group
    ]
    partition = partition_replayable_tool_groups(window, session_server_id=SERVER_A)
    # the user entries stay replayable; the split group does not
    assert [e.seq for e in partition.replayable] == [1, 4]
    assert [e.seq for e in partition.pending] == [2, 3]
    marked = partition.pending[0].payload
    assert isinstance(marked, AssistantMessageEntry)
    assert marked.tool_group_state == "unresolved"
    assert marked.text == "working on it"  # safe text stays visible
    assert isinstance(partition.pending[1].payload, ToolResultEntry)


def test_partition_marks_resultless_assistant_pending() -> None:
    assistant = _assistant_entry(1, call_ids=["call-1"], seq=2)
    partition = partition_replayable_tool_groups(
        [assistant], session_server_id=SERVER_A
    )
    assert partition.replayable == ()
    assert [e.seq for e in partition.pending] == [2]
    marked = partition.pending[0].payload
    assert isinstance(marked, AssistantMessageEntry)
    assert marked.tool_group_state == "pending"


def test_orphan_tool_result_is_never_replayable_r01_o1() -> None:
    """R01 O1 pin: a ToolResultEntry with no preceding matching assistant
    call group in the window (window-straddling or corrupted) is classified
    into ``pending`` and NEVER enters the replayable native model history —
    an unmatched ToolReturnPart would poison the next provider request."""
    orphan = _tool_result_entry(1, call_id="call-orphan", seq=1)
    user = _user_entry(2, seq=2)
    partition = partition_replayable_tool_groups(
        [orphan, user], session_server_id=SERVER_A
    )
    assert [e.seq for e in partition.pending] == [1]
    assert [e.seq for e in partition.replayable] == [2]
    assert isinstance(partition.pending[0].payload, ToolResultEntry)
    # The orphan alone is excluded as well.
    alone = partition_replayable_tool_groups([orphan], session_server_id=SERVER_A)
    assert alone.replayable == ()
    assert [e.seq for e in alone.pending] == [1]


def test_partition_rejects_cross_session_windows_and_cross_scope() -> None:
    assistant = _assistant_entry(1, call_ids=["call-1"], seq=1)
    other = _user_entry(2, session_id=uuid.UUID(int=77), seq=2)
    with pytest.raises(SessionContractError) as err:
        partition_replayable_tool_groups([assistant, other], session_server_id=SERVER_A)
    assert err.value.fields == {
        "session_id": SessionErrorCode.CROSS_SESSION_ENTRY
    }
    cross_payload = AssistantMessageEntry(
        kind=EntryKind.ASSISTANT_MESSAGE,
        tool_calls=[
            ToolCallItem(
                call_id="call-1",
                tool_name="host.shell",
                capability_id="host.shell",
                args={},
                target_server_id=SERVER_B,
            )
        ],
        model_profile="default",
    )
    cross_entry = SessionEntryView(
        entry_id=_entry_id(1),
        session_id=SESSION_A,
        seq=1,
        run_id=RUN_1,
        durable_source_id="assistant-src-1",
        created_at=0,
        payload=cross_payload,
    )
    with pytest.raises(SessionContractError) as err:
        partition_replayable_tool_groups([cross_entry], session_server_id=SERVER_A)
    assert err.value.fields == {
        "tool_calls": SessionErrorCode.CROSS_SCOPE_TOOL_GROUP
    }


# ---------------------------------------------------------------------------
# 3. bounded pagination and entry bounds
# ---------------------------------------------------------------------------


def test_history_page_carries_honest_continuation_metadata() -> None:
    items = [
        _user_entry(1, run_id=RUN_1, seq=11),
        _assistant_entry(2, call_ids=[], text="done", run_id=RUN_1, seq=12),
    ]
    page = SessionHistoryPage(
        session_id=SESSION_A,
        items=items,
        limit=HISTORY_PAGE_MAX_ENTRIES,
        total=52,
        has_more=False,
        has_older=True,
        oldest_seq=11,
        latest_seq=12,
        next_before_seq=11,
        byte_size=1_024,
    )
    assert page.oldest_seq == 11 and page.next_before_seq == 11
    assert page.truncated_by_bytes is False


def test_history_page_enforces_count_order_and_cursor_consistency() -> None:
    items = [_user_entry(n, seq=n) for n in range(1, 4)]
    with pytest.raises(ValidationError):
        SessionHistoryPage(  # more items than the limit
            session_id=SESSION_A,
            items=items,
            limit=2,
            total=3,
            has_more=False,
            has_older=False,
            byte_size=10,
        )
    with pytest.raises(ValidationError):  # non-monotonic sequence order
        SessionHistoryPage(
            session_id=SESSION_A,
            items=[items[1], items[0]],
            limit=10,
            total=3,
            has_more=False,
            has_older=False,
            byte_size=10,
        )
    with pytest.raises(ValidationError):  # declared bounds must match items
        SessionHistoryPage(
            session_id=SESSION_A,
            items=items,
            limit=10,
            total=3,
            has_more=False,
            has_older=False,
            oldest_seq=2,
            latest_seq=3,
            byte_size=10,
        )
    with pytest.raises(ValidationError):  # cursor without has_older
        SessionHistoryPage(
            session_id=SESSION_A,
            items=items,
            limit=10,
            total=3,
            has_more=False,
            has_older=False,
            next_before_seq=1,
            byte_size=10,
        )
    with pytest.raises(ValidationError):  # has_older needs the cursor
        SessionHistoryPage(
            session_id=SESSION_A,
            items=items,
            limit=10,
            total=3,
            has_more=False,
            has_older=True,
            byte_size=10,
        )
    with pytest.raises(ValidationError):  # continuation on an empty page
        SessionHistoryPage(
            session_id=SESSION_A,
            items=[],
            limit=10,
            total=3,
            has_more=True,
            has_older=False,
            byte_size=0,
        )
    with pytest.raises(ValidationError):  # empty page may not declare bounds
        SessionHistoryPage(
            session_id=SESSION_A,
            items=[],
            limit=10,
            total=0,
            has_more=False,
            has_older=False,
            oldest_seq=1,
            latest_seq=1,
            byte_size=0,
        )


def test_history_page_enforces_byte_budget() -> None:
    items = [_user_entry(1, seq=1)]
    with pytest.raises(ValidationError):
        SessionHistoryPage(
            session_id=SESSION_A,
            items=items,
            limit=10,
            total=1,
            has_more=False,
            has_older=False,
            byte_size=MAX_PAGE_BYTES + 1,  # over the 64 KiB page budget
        )
    page = SessionHistoryPage(
        session_id=SESSION_A,
        items=items,
        limit=10,
        total=1,
        has_more=False,
        has_older=False,
        oldest_seq=1,
        latest_seq=1,
        byte_size=MAX_PAGE_BYTES,
        truncated_by_bytes=True,
    )
    assert page.byte_size == MAX_PAGE_BYTES
    assert page.truncated_by_bytes is True


def test_session_list_page_is_bounded_and_consistent() -> None:
    rows = [
        SessionSummary(
            session_id=uuid.UUID(int=n),
            server_id=SERVER_A,
            source=SessionSourceKind.INTERACTIVE,
            status=SessionStatus.IDLE,
            created_at=0,
            updated_at=0,
        )
        for n in range(1, 4)
    ]
    page = SessionListPage(
        items=rows,
        limit=SESSION_LIST_DEFAULT_LIMIT,
        offset=0,
        total=23,
        has_more=True,
    )
    assert page.has_more is True
    with pytest.raises(ValidationError):  # dishonest continuation
        SessionListPage(
            items=rows,
            limit=SESSION_LIST_DEFAULT_LIMIT,
            offset=0,
            total=3,
            has_more=True,
        )
    with pytest.raises(ValidationError):  # over the list page bound
        SessionListPage(
            items=rows,
            limit=SESSION_LIST_MAX_LIMIT + 1,
            offset=0,
            total=0,
            has_more=False,
        )
    detail = SessionDetail(
        session_id=SESSION_A,
        server_id=SERVER_A,
        source=SessionSourceKind.RUNBOOK,
        status=SessionStatus.IDLE,
        created_at=0,
        updated_at=0,
        latest_seq=42,
        total_entries=42,
        compaction_count=1,
    )
    assert detail.latest_seq == 42


def test_entry_and_payload_bounds_fail_closed() -> None:
    with pytest.raises(ValidationError):
        UserMessageEntry(
            kind=EntryKind.USER_MESSAGE,
            text="x" * (MAX_ENTRY_TEXT_CHARS + 1),
            client_request_id=_request_id(1),
            source=SessionSourceKind.INTERACTIVE,
        )
    with pytest.raises(ValidationError):  # oversized entry envelope rejects too
        SessionEntryView(
            entry_id=_entry_id(1),
            session_id=SESSION_A,
            seq=1,
            run_id=None,
            durable_source_id="user-src-1",
            created_at=0,
            payload=UserMessageEntry(
                kind=EntryKind.USER_MESSAGE,
                text="x" * (MAX_ENTRY_TEXT_CHARS + 1),
                client_request_id=_request_id(1),
                source=SessionSourceKind.INTERACTIVE,
            ),
        )
    with pytest.raises(ValidationError):  # schema-version drift fails closed
        SessionEntryView(
            entry_id=_entry_id(1),
            session_id=SESSION_A,
            seq=1,
            run_id=None,
            durable_source_id="user-src-1",
            created_at=0,
            schema_version=SCHEMA_VERSION + 1,
            payload=UserMessageEntry(
                kind=EntryKind.USER_MESSAGE,
                text="synthetic",
                client_request_id=_request_id(1),
                source=SessionSourceKind.INTERACTIVE,
            ),
        )
    with pytest.raises(ValidationError):
        ToolResultEntry(
            kind=EntryKind.TOOL_RESULT,
            call_id="call-1",
            tool_name="host.logs",
            capability_id="host.logs",
            status="executed",
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.SUCCESS,
            effect_certainty=EffectCertainty.CERTAIN,
            payload_hash="a" * 64,
            output_preview="x" * (MAX_TOOL_OUTPUT_PREVIEW_CHARS + 1),
        )
    with pytest.raises(ValidationError):  # oversized serialized args
        ToolCallItem(
            call_id="call-1",
            tool_name="host.shell",
            capability_id="host.shell",
            args={"blob": "x" * (MAX_ENTRY_TEXT_CHARS + 1)},
        )
    with pytest.raises(ValidationError):  # duplicate call ids
        AssistantMessageEntry(
            kind=EntryKind.ASSISTANT_MESSAGE,
            tool_calls=[
                ToolCallItem(
                    call_id="dup",
                    tool_name="host.logs",
                    capability_id="host.logs",
                    args={},
                ),
                ToolCallItem(
                    call_id="dup",
                    tool_name="host.logs",
                    capability_id="host.logs",
                    args={},
                ),
            ],
            model_profile="default",
        )
    with pytest.raises(ValidationError):  # group state without tool calls
        AssistantMessageEntry(
            kind=EntryKind.ASSISTANT_MESSAGE,
            text="text only",
            model_profile="default",
            tool_group_state="pending",
        )
    with pytest.raises(ValidationError):  # compaction range ordering
        CompactionEntry(
            kind=EntryKind.COMPACTION,
            compaction_id=_request_id(1),
            from_seq=5,
            to_seq=4,
            summary_text="synthetic summary",
            model_profile="default",
        )
    with pytest.raises(ValidationError):  # seq bounds
        CompactionEntry(
            kind=EntryKind.COMPACTION,
            compaction_id=_request_id(1),
            from_seq=0,
            to_seq=0,
            summary_text="synthetic summary",
            model_profile="default",
        )
    with pytest.raises(ValidationError):
        EntryCursor(seq=0)


def test_compaction_record_enforces_range_order() -> None:
    record = SessionCompactionRecord(
        compaction_id=_request_id(1),
        session_id=SESSION_A,
        from_seq=1,
        to_seq=30,
        summary_text="synthetic bounded summary",
        model_profile="default",
        source_usage_tokens=1234,
        created_at=0,
    )
    assert record.to_seq == 30
    with pytest.raises(ValidationError):
        SessionCompactionRecord(
            compaction_id=_request_id(1),
            session_id=SESSION_A,
            from_seq=31,
            to_seq=30,
            summary_text="synthetic bounded summary",
            model_profile="default",
            created_at=0,
        )


def test_provider_reasoning_artifacts_are_explicit_and_bounded() -> None:
    artifact = ProviderReasoningArtifact(
        provider="openai",
        artifact_kind="summary",
        content="synthetic bounded reasoning summary",
    )
    assert artifact.redacted is False
    with pytest.raises(ValidationError):
        ProviderReasoningArtifact(
            provider="openai",
            artifact_kind="raw_chain_of_thought",  # not a safe artifact class
            content="synthetic",
        )


def test_forbidden_authority_fields_are_unrepresentable() -> None:
    minimal_kwargs: dict[type, dict[str, object]] = {
        SessionCreateRequest: {
            "server_id": SERVER_A,
            "client_request_id": _request_id(1),
        },
        SessionTurnSubmitRequest: {
            "session_id": SESSION_A,
            "text": "synthetic turn",
            "client_request_id": _request_id(1),
        },
        SessionControlRequest: {
            "session_id": SESSION_A,
            "action": ControlActionKind.STEER,
            "client_request_id": _request_id(1),
        },
        RunbookSessionExecuteRequest: {
            "server_id": SERVER_A,
            "runbook_ref": RunbookRef(service_id="svc-1", runbook_id="rb-1"),
            "client_request_id": _request_id(1),
        },
        DocumentAttachAdmissionRequest: {
            "server_id": SERVER_A,
            "document_id": _request_id(1),
            "artifact_ref": "ref-1",
            "client_request_id": _request_id(1),
        },
    }
    for model, kwargs in minimal_kwargs.items():
        model(**kwargs)  # the minimal admission itself is valid
        for forbidden in sorted(FORBIDDEN_AUTHORITY_FIELDS):
            with pytest.raises(ValidationError):
                model(**kwargs, **{forbidden: True})


# ---------------------------------------------------------------------------
# 4. contract hygiene: forbid/describe/version
# ---------------------------------------------------------------------------


def _session_models() -> list[type[BaseModel]]:
    import inspect

    import mai.contracts.sessions as sessions_module

    return [
        obj
        for _, obj in inspect.getmembers(sessions_module, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == sessions_module.__name__
    ]


def test_every_session_model_forbids_extra_and_describes_fields() -> None:
    for model in _session_models():
        assert model.model_config.get("extra") == "forbid", model.__name__
        for name, field in model.model_fields.items():
            description = field.description or ""
            assert len(description) >= 12, (model.__name__, name, description)


# ---------------------------------------------------------------------------
# 5. neutral vocabularies and retained consumers
# ---------------------------------------------------------------------------


def test_tag_contract_bounds_and_previews() -> None:
    assert tags_contract.DEFAULT_PAGE_LIMIT == 50
    assert tags_contract.MAX_PAGE_LIMIT == 200
    assert tags_contract.compute_has_more(0, 50, 200) is True
    assert tags_contract.compute_has_more(150, 50, 200) is False
    preview = tags_contract.EntityTagPreview(preview=("a", "b"), total=2)
    assert preview.preview == ("a", "b")
    view = tags_contract.EntityTagPreviewView(preview=["a", "b"], total=5)
    assert view.total == 5
    with pytest.raises(ValidationError):
        tags_contract.EntityTagPreviewView(preview=["a"], total=0)
    with pytest.raises(ValidationError):
        tags_contract.EntityTagPreviewView(preview=["a", "b", "c", "d"], total=2)
    with pytest.raises(ValidationError):
        tags_contract.PageEnvelope(limit=0, offset=0, total=0, has_more=False)
    with pytest.raises(ValidationError):  # dishonest continuation
        tags_contract.PageEnvelope(limit=50, offset=200, total=100, has_more=True)
    envelope = tags_contract.PageEnvelope(limit=50, offset=0, total=100, has_more=True)
    assert envelope.has_more is True


def test_retained_consumers_reuse_the_neutral_tag_objects() -> None:
    import mai.api.history_tags as history_tags_api
    import mai.api.jobs as jobs_api
    import mai.history_store as history_store
    import mai.tag_store as tag_store

    assert jobs_api.EntityTagPreviewView is tags_contract.EntityTagPreviewView
    assert history_tags_api.compute_has_more is tags_contract.compute_has_more
    assert history_tags_api.DEFAULT_PAGE_LIMIT == tags_contract.DEFAULT_PAGE_LIMIT
    assert history_tags_api.MAX_PAGE_LIMIT == tags_contract.MAX_PAGE_LIMIT
    assert tag_store.EntityTagPreview is tags_contract.EntityTagPreview
    assert tag_store.compute_has_more is tags_contract.compute_has_more
    assert tag_store.DEFAULT_PAGE_LIMIT is tags_contract.DEFAULT_PAGE_LIMIT
    assert tag_store.MAX_PAGE_LIMIT is tags_contract.MAX_PAGE_LIMIT
    assert history_store.EntityTagPreview is tags_contract.EntityTagPreview
    assert history_store.DEFAULT_PAGE_LIMIT is tags_contract.DEFAULT_PAGE_LIMIT
    assert history_store.MAX_PAGE_LIMIT is tags_contract.MAX_PAGE_LIMIT
    # The deleted-at-D09 conversation contract keeps re-exporting the same
    # objects so its un-deleted consumers keep working until then.
    import mai.contracts.conversations as conversations_contract

    assert (
        conversations_contract.EntityTagPreviewView
        is tags_contract.EntityTagPreviewView
    )
    assert conversations_contract.compute_has_more is tags_contract.compute_has_more
    assert (
        conversations_contract.DEFAULT_PAGE_LIMIT is tags_contract.DEFAULT_PAGE_LIMIT
    )
    assert conversations_contract.MAX_PAGE_LIMIT is tags_contract.MAX_PAGE_LIMIT


def test_neutral_tool_result_vocabulary_is_frozen() -> None:
    assert tool_results_contract.MAX_EVIDENCE_REFS == 16
    assert tool_results_contract.RESULT_DATA_MAX_ENTRIES == 8
    assert tool_results_contract.RESULT_DATA_VALUE_MAX_CHARS == 200
    assert "token" in tool_results_contract.SECRET_DATA_KEYS
    assert "api_key" in tool_results_contract.SECRET_DATA_KEYS
    assert [k.value for k in tool_results_contract.EvidenceKind] == [
        "message",
        "observation",
        "run_event",
        "artifact",
        "document",
    ]
    assert [k.value for k in tool_results_contract.ExecutionStatus] == [
        "succeeded",
        "failed",
        "outcome_unknown",
        "cancelled",
    ]
    assert [k.value for k in tool_results_contract.DomainOutcome] == [
        "success",
        "failure",
        "unknown",
    ]
    assert [k.value for k in tool_results_contract.EffectCertainty] == [
        "certain",
        "uncertain",
        "unknown",
    ]
    # The task contract keeps re-exporting the same objects until D09.
    assert tasks_contract.DomainOutcome is DomainOutcome
    assert tasks_contract.ExecutionStatus is ExecutionStatus
    assert tasks_contract.EffectCertainty is EffectCertainty
    assert (
        tasks_contract.MAX_EVIDENCE_REFS is tool_results_contract.MAX_EVIDENCE_REFS
    )
    assert tasks_contract.SECRET_DATA_KEYS is tool_results_contract.SECRET_DATA_KEYS


def _forbidden_contract_imports(module_path: pathlib.Path) -> list[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    found: list[str] = []
    forbidden_prefixes = (
        "mai.contracts.tasks",
        "mai.contracts.conversations",
        "mai.contracts.objectives",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(forbidden_prefixes):
                found.append(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(forbidden_prefixes):
                    found.append(alias.name)
    return found


def test_agent_tools_has_zero_old_contract_imports() -> None:
    module_file = pathlib.Path(importlib.import_module("mai.agents.tools").__file__ or "")
    assert _forbidden_contract_imports(module_file) == []
    # The only task-contract symbol the bridge used to import was the phase
    # gate; it must not leak back as a module attribute.
    assert not hasattr(agent_tools, "validate_phase")
    assert not hasattr(agent_tools, "TaskContractError")


def test_agent_tools_phase_gate_mirrors_the_ratified_map() -> None:
    from mai.contracts.tasks import Phase, PhaseAction, validate_phase

    for phase in Phase:
        for action in PhaseAction:
            enforced_refuses = False
            try:
                validate_phase(phase, action)
            except ValueError:
                enforced_refuses = True
            offer_refuses = not agent_tools._phase_allows(phase, action)
            assert enforced_refuses == offer_refuses, (phase, action)
    # Neutral duck-typing: plain strings classify identically to the enums.
    assert agent_tools._phase_allows("act", "execute_steps") is True
    assert agent_tools._phase_allows("inspect", "execute_steps") is False
    assert agent_tools._phase_allows("plan", "unknown_action") is False
    assert agent_tools._phase_allows(None, None) is False
