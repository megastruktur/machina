"""Focused tests for task-local no-progress signals (card 22-progress).

Acceptance (BRIEF / act_CONTRACTS §10 / act_RUNTIME rows I1, I2, P2):

1. **Equivalent repeated empty searches/errors yield a bounded strategy-
   change hint with source scope, never a false nonexistence claim (I1):**
   the third equivalent empty query (and the third identical refusal)
   emits a scoped advisory hint naming the capability and the query
   fingerprint and explicitly disclaiming absence; one and two repeats
   stay silent (no arbitrary hard failure on two tries).
2. **Async polling with operation identity and new evidence remains
   productive (I1/I2):** equivalent polls of one operation whose output
   changes are ``new_evidence`` (never a stall); unchanged non-empty
   repeats are ``expected_polling`` below the bounded note threshold;
   the note never claims failure. No chain-of-thought storage: the
   signal document carries only counts, short digests/fingerprints,
   capability ids and the hint sentence; output text is never retained.
   No unbounded cross-task memory: the derivation is pure over the
   deriving run's own artifacts and retains nothing across calls.
3. **The hook reaches the model round without changing any effect:** the
   ``run.py`` composition renders the labeled ``[Progress hint]`` round
   prefix after user steering, keeps hint-free rounds byte-identical,
   and the real checkpointed ``amodel_round`` seam delivers the rendered
   note through the composed executor (in-process, real disposable
   PostgreSQL — the proven 13-intake-runner harness).

Boundary discipline: the pure layer runs anywhere; the executor-seam
test needs ``MAI_TEST_DATABASE_URL`` (real Alembic chain, real engine
adapter) exactly like the sibling durable suites. Fixtures carry only
fixed opaque UUIDs and synthetic marker words — never secret values.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_agent_task_run as task_harness
from mai.agents import run as agent_run
from mai.agents import task_progress
from mai.agents.tools import TOOL_RESULT_SCHEMA_VERSION, ToolResult, ToolStatus

#: The suite runs async tests through the anyio pytest plugin (the repo's
#: async test convention); the marker is applied module-wide.
pytestmark = pytest.mark.anyio

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = str(BACKEND_ROOT / "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_ID = "3c7e5a91-8b2d-4f6a-9c1e-5d4b7a8f6e21"
RUN_ID = "8f4b2c6d-1a3e-4f5b-8c7d-9e2a4b6c8d10"

CAP_SEARCH = "kb.search"
CAP_POLL = "job.status"
FINGERPRINT_1 = "f" * 64
FINGERPRINT_2 = "e" * 64


# ---------------------------------------------------------------------------
# frozen ToolResult document builders (the real model, not a parallel shape)
# ---------------------------------------------------------------------------


def _result_doc(
    capability_id: str = CAP_SEARCH,
    payload_hash: str = FINGERPRINT_1,
    *,
    status: ToolStatus = ToolStatus.EXECUTED,
    output: str | None = "",
    reason: str = "ok",
) -> str:
    return ToolResult(
        schema_version=TOOL_RESULT_SCHEMA_VERSION,
        capability_id=capability_id,
        server_id=uuid.UUID(SERVER_ID),
        run_id=uuid.UUID(RUN_ID),
        status=status,
        allowed=status is ToolStatus.EXECUTED,
        reason=reason,
        effect="read" if status is ToolStatus.EXECUTED else "not_attempted",
        payload_hash=payload_hash,
        policy_revision=1,
        effect_class="read",
        approval_id=None,
        approval_spent=False,
        approval_code=None,
        executor_ok=status is ToolStatus.EXECUTED,
        output_text=output,
        output_truncated=False,
        output_omitted_bytes=0,
        error_code=None,
        error_detail=None,
    ).to_json_str()


def _results_json(**call_docs: str) -> str:
    return agent_run.canonical_json({"calls": call_docs})


def _history_json(*tool_docs: str) -> str:
    """One real PydanticAI request carrying tool returns (round replay)."""
    parts: list[object] = [UserPromptPart(content="investigate the marker")]
    for index, document in enumerate(tool_docs):
        parts.append(
            ToolReturnPart(
                tool_name="fixture_tool",
                content=document,
                tool_call_id=f"c{index}",
            )
        )
    seed = [ModelRequest(parts=parts)]  # type: ignore[list-item]
    return ModelMessagesTypeAdapter.dump_json(seed).decode("utf-8")


# ---------------------------------------------------------------------------
# the derivation layer (pure; scenario I1 row semantics)
# ---------------------------------------------------------------------------


class TestNoProgressDerivation:
    def test_one_and_two_equivalent_empty_searches_stay_silent(self) -> None:
        """I1/§10: no arbitrary hard failure on two tries — silence first."""
        for repeats in (1, 2):
            calls = {f"c{i}": _result_doc(output="") for i in range(repeats)}
            document = task_progress.progress_hint_document(
                _history_json(), _results_json(**calls)
            )
            assert document is None

    def test_third_equivalent_empty_search_emits_scoped_strategy_hint(
        self,
    ) -> None:
        """AC 1: bounded strategy-change hint WITH source scope, never a
        false nonexistence claim."""
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(output=""),
                c2=_result_doc(output=""),
                c3=_result_doc(output=""),
            ),
        )
        assert document is not None
        assert document["schema_version"] == task_progress.SCHEMA_VERSION
        assert document["kind"] == task_progress.SignalKind.REPEATED_EMPTY.value
        assert document["equivalent_repeats"] == 3
        assert document["scope"] == [CAP_SEARCH]
        fingerprint = document["fingerprint"]
        assert isinstance(fingerprint, str) and len(fingerprint) == 12
        hint = document["hint"]
        assert isinstance(hint, str)
        # The hint carries its source scope: WHICH capability and WHICH query.
        assert CAP_SEARCH in hint
        assert fingerprint in hint
        # The strategy change is named; absence is explicitly disclaimed.
        assert "widen the source scope" in hint
        assert "not proof of absence" in hint

    def test_whitespaced_and_marker_outputs_count_as_empty(self) -> None:
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(output="  \n"),
                c2=_result_doc(output="[]"),
                c3=_result_doc(output="{}"),
            ),
        )
        assert document is not None
        assert document["kind"] == task_progress.SignalKind.REPEATED_EMPTY.value

    def test_third_identical_refusal_emits_scoped_error_hint(self) -> None:
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="bridge_invalid_request",
                    output=None,
                ),
                c2=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="bridge_invalid_request",
                    output=None,
                ),
                c3=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="bridge_invalid_request",
                    output=None,
                ),
            ),
        )
        assert document is not None
        assert document["kind"] == task_progress.SignalKind.REPEATED_ERROR.value
        hint = document["hint"]
        assert isinstance(hint, str)
        assert CAP_SEARCH in hint
        assert "bridge_invalid_request" in hint
        assert "scoped to this exact query" in hint

    def test_changed_refusal_resets_the_error_streak(self) -> None:
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="bridge_invalid_request",
                    output=None,
                ),
                c2=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="bridge_invalid_request",
                    output=None,
                ),
                c3=_result_doc(
                    status=ToolStatus.DENIED,
                    reason="capability_not_enabled",
                    output=None,
                ),
            ),
        )
        assert document is None

    def test_repeated_unknown_effects_steer_to_reconciliation(self) -> None:
        """Guard rule 2: unknown effects are never re-requested; the hint
        says so instead of inventing a failure."""
        calls = {
            f"c{i}": _result_doc(
                status=ToolStatus.UNKNOWN_EFFECT,
                reason="crash_window_unproven",
                output=None,
            )
            for i in range(3)
        }
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is not None
        assert document["kind"] == task_progress.SignalKind.REPEATED_UNKNOWN.value
        hint = document["hint"]
        assert isinstance(hint, str)
        assert "Never re-request" in hint
        assert "failed" not in hint

    def test_alternating_distinct_queries_never_accumulate(self) -> None:
        calls = {
            "c1": _result_doc(payload_hash=FINGERPRINT_1, output=""),
            "c2": _result_doc(payload_hash=FINGERPRINT_2, output=""),
            "c3": _result_doc(payload_hash=FINGERPRINT_1, output=""),
        }
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is None

    def test_window_stays_bounded_under_a_long_repetition(self) -> None:
        calls = {f"c{i}": _result_doc(output="") for i in range(30)}
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is not None
        assert document["window"] == task_progress.OBSERVATION_WINDOW
        assert isinstance(document["equivalent_repeats"], int)
        assert document["equivalent_repeats"] <= task_progress.OBSERVATION_WINDOW


class TestPollingNotMisclassified:
    def test_polls_with_new_evidence_stay_productive(self) -> None:
        """AC 2 / I1: normal polling of an active asynchronous operation
        with operation identity (same payload hash) and changing output
        (new evidence) is never a stall."""
        calls = {
            "c1": _result_doc(capability_id=CAP_POLL, output="queued"),
            "c2": _result_doc(capability_id=CAP_POLL, output="running"),
            "c3": _result_doc(capability_id=CAP_POLL, output="done"),
            "c4": _result_doc(capability_id=CAP_POLL, output="result-payload"),
        }
        signal = task_progress.derive_progress_signal(
            task_progress.observations_from_round_results(_results_json(**calls))
        )
        assert signal.kind is task_progress.SignalKind.NEW_EVIDENCE
        assert signal.hint is None

    def test_bounded_identical_polling_below_threshold_is_expected(self) -> None:
        calls = {
            f"c{i}": _result_doc(capability_id=CAP_POLL, output="still running")
            for i in range(task_progress.FLAT_POLL_THRESHOLD - 1)
        }
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is None
        signal = task_progress.derive_progress_signal(
            task_progress.observations_from_round_results(_results_json(**calls))
        )
        assert signal.kind is task_progress.SignalKind.EXPECTED_POLLING

    def test_flat_poll_note_never_claims_failure(self) -> None:
        calls = {
            f"c{i}": _result_doc(capability_id=CAP_POLL, output="still running")
            for i in range(task_progress.FLAT_POLL_THRESHOLD)
        }
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is not None
        assert document["kind"] == task_progress.SignalKind.FLAT_POLL.value
        hint = document["hint"]
        assert isinstance(hint, str)
        assert CAP_POLL in hint
        assert "unchanged output" in hint
        lowered = hint.lower()
        for forbidden in ("stall", "failed", "does not exist", "give up"):
            assert forbidden not in lowered

    def test_new_evidence_resets_the_empty_streak(self) -> None:
        """I2: real new evidence between empty searches ends the streak."""
        calls = {
            "c1": _result_doc(output=""),
            "c2": _result_doc(output=""),
            "c3": _result_doc(output="found the marker document"),
            "c4": _result_doc(output=""),
        }
        document = task_progress.progress_hint_document(
            _history_json(), _results_json(**calls)
        )
        assert document is None


class TestObservationBoundaries:
    def test_receipt_only_documents_are_never_empty(self) -> None:
        """A value-free receipt rebuild (no output carrier) has an
        UNPROVEN output — it must never count as an empty result."""
        rebuilt = json.dumps(
            {
                "schema_version": TOOL_RESULT_SCHEMA_VERSION,
                "capability_id": CAP_SEARCH,
                "status": "executed",
                "allowed": True,
                "reason": "ok",
                "effect": "read",
                "payload_hash": FINGERPRINT_1,
                "executor_ok": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(c1=rebuilt, c2=rebuilt, c3=rebuilt),
        )
        assert document is None

    def test_boundary_wire_notes_and_junk_are_not_observations(self) -> None:
        not_executed = agent_run.canonical_json(
            {
                "status": "not_executed",
                "reason": "steering_pending",
                "detail": "no effect occurred",
            }
        )
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=not_executed,
                c2=not_executed,
                c3=not_executed,
                c4="plain text, not a document",
                c5="[1, 2, 3]",
            ),
        )
        assert document is None

    def test_history_extraction_reads_only_tool_returns(self) -> None:
        history = _history_json(
            _result_doc(output=""),
            _result_doc(output=""),
            _result_doc(output=""),
        )
        observations = task_progress.observations_from_history(history)
        assert len(observations) == 3
        assert {o.status for o in observations} == {"executed"}

    def test_malformed_inputs_fail_closed_inert(self) -> None:
        for history, results in (
            ("not json", _results_json(c1=_result_doc())),
            ("[]", "not json"),
            (None, None),
            ('{"calls": {"c1": 7}}', '{"calls": null}'),
        ):
            assert task_progress.progress_hint_document(history, results) is None

    def test_no_content_storage_and_bounded_document(self) -> None:
        """AC 2: no hidden chain-of-thought storage — huge outputs fold
        into short digests; the document carries counts, fingerprints,
        capability ids and the hint sentence, nothing else."""
        huge_a = "reasoning " + "x" * 5000
        document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(output=huge_a),
                c2=_result_doc(output=huge_a),
                c3=_result_doc(output=huge_a),
                c4=_result_doc(output=huge_a),
                c5=_result_doc(output=huge_a),
                c6=_result_doc(output=huge_a),
            ),
        )
        assert document is not None
        assert document["kind"] == task_progress.SignalKind.FLAT_POLL.value
        assert set(document) == {
            "schema_version",
            "kind",
            "equivalent_repeats",
            "window",
            "scope",
            "fingerprint",
            "hint",
        }
        serialized = json.dumps(document)
        assert len(serialized) < 2048
        hint = document["hint"]
        assert isinstance(hint, str)
        assert len(hint) <= task_progress.MAX_HINT_CHARS
        # No output text and no reasoning text survived anywhere.
        assert "x" * 64 not in serialized
        for value in document.values():
            if isinstance(value, str):
                assert len(value) <= task_progress.MAX_HINT_CHARS

    def test_render_hint_text_fail_closed_and_bounded(self) -> None:
        assert task_progress.render_hint_text(None) == ""
        assert task_progress.render_hint_text("junk") == ""
        assert task_progress.render_hint_text({"kind": "repeated_empty"}) == ""
        oversized = {"hint": "h" * (task_progress.MAX_HINT_CHARS + 500)}
        rendered = task_progress.render_hint_text(oversized)
        assert len(rendered) == task_progress.MAX_HINT_CHARS

    def test_task_local_derivation_is_stateless(self) -> None:
        """AC 2: one task's progress state never leaks into another's —
        the derivation is pure, so call order cannot change a signal."""
        task_a = _results_json(
            c1=_result_doc(output=""),
            c2=_result_doc(output=""),
            c3=_result_doc(output=""),
        )
        task_b = _results_json(c1=_result_doc(capability_id=CAP_POLL, output="queued"))
        assert task_progress.progress_hint_document(_history_json(), task_b) is None
        # Deriving task A (which emits) before task B changes nothing for B.
        assert task_progress.progress_hint_document(_history_json(), task_a) is not None
        assert task_progress.progress_hint_document(_history_json(), task_b) is None


# ---------------------------------------------------------------------------
# the run.py hook surface (pure composition)
# ---------------------------------------------------------------------------


class TestRunHookComposition:
    def test_compose_round_prompt_steering_first_hint_second(self) -> None:
        note = "[Progress hint]: widen the source scope"
        steered = "[User steering message]: prefer the staging target"
        assert agent_run.compose_round_prompt(None, None) is None
        assert agent_run.compose_round_prompt(steered, None) is steered
        assert agent_run.compose_round_prompt(None, note) == note
        assert agent_run.compose_round_prompt(steered, note) == (f"{steered}\n\n{note}")

    def test_progress_note_fail_closed(self) -> None:
        hint_document = {
            "schema_version": 1,
            "kind": "repeated_empty",
            "hint": "3 equivalent kb.search calls returned no new output.",
        }
        rendered = agent_run._progress_note_text(
            agent_run.canonical_json(hint_document)
        )
        assert rendered == f"[Progress hint]: {hint_document['hint']}"
        # Absent, null, malformed and hint-less documents render nothing.
        assert agent_run._progress_note_text(None) is None
        assert agent_run._progress_note_text("null") is None
        assert agent_run._progress_note_text("{not json") is None
        assert agent_run._progress_note_text('{"kind": "fresh"}') is None


# ---------------------------------------------------------------------------
# the checkpointed model-round seam (in-process, real disposable PG)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_all(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine too)."""
    table_list = ", ".join(
        name
        for name in (
            *task_harness.DOMAIN_TABLES,
            "profile_projection_journal",
            "profile_projection",
            "admin_sessions",
            "admin_credential",
            "notification_alerts",
            "observation_findings",
            "server_observations",
            "knowledge_doc_chunks",
            "knowledge_documents",
            "application_projection_journal",
            "application_projection",
            "alembic_version",
        )
    )
    with psycopg.connect(url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table_list}")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def progress_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the executor-seam claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        try:
            _drop_all(url)
            command.upgrade(_alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        from mai.jobs import engine as job_engine

        job_engine.launch_worker(url)  # creates the engine-owned dbos schema
        job_engine.shutdown()
        yield url
    finally:
        from mai.jobs import engine as job_engine

        job_engine.shutdown()
        _drop_all(url)


@pytest.fixture
async def sessions(
    progress_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """NullPool session factory; ONE truncated domain per test."""
    from mai.database import create_app_engine

    eng, factory = create_app_engine(progress_url)
    yield factory
    try:
        async with factory() as session:
            present = (
                (
                    await session.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                            "AND c.relname = ANY(:names)"
                        ),
                        {"names": list(task_harness.DOMAIN_TABLES)},
                    )
                )
                .scalars()
                .all()
            )
            if present:
                await session.execute(
                    text(f"TRUNCATE {', '.join(sorted(present))} CASCADE")
                )
            await session.commit()
    finally:
        await eng.dispose()


class TestModelRoundHook:
    @staticmethod
    def _final_text_model() -> FunctionModel:
        return FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart(content="done")])
        )

    async def test_hint_rides_the_next_round_after_steering_and_alone(
        self, sessions, progress_url, tmp_path
    ) -> None:
        """The rendered ``[Progress hint]`` prefix reaches the round after
        user steering (steering keeps authority and comes first) — the
        exact composition the durable loop feeds ``amodel_round``."""
        run_id = str(uuid.uuid4())
        await task_harness._ainsert_run(sessions, run_id)
        composed, _journal = task_harness._compose(
            sessions, progress_url, tmp_path, self._final_text_model()
        )
        intent = task_harness._intent_of(run_id)
        pins = task_harness._pins_of(composed)
        seed = agent_run.initial_history_json("You administer the host.", "go")

        hint_document = task_progress.progress_hint_document(
            _history_json(),
            _results_json(
                c1=_result_doc(output=""),
                c2=_result_doc(output=""),
                c3=_result_doc(output=""),
            ),
        )
        assert hint_document is not None
        hint_json = agent_run.canonical_json(hint_document)

        steered_and_hinted = await composed.amodel_round(
            intent,
            pins,
            seed,
            None,
            0,
            agent_run.canonical_json(["prefer the staging target"]),
            hint_json,
        )
        history = str(steered_and_hinted["history_json"])
        assert "[User steering message]: prefer the staging target" in history
        assert "[Progress hint]:" in history
        hint_text = str(hint_document["hint"])
        assert hint_text in history
        assert history.index("[User steering message]:") < history.index(
            "[Progress hint]:"
        )

        hinted_only = await composed.amodel_round(
            intent, pins, seed, None, 1, None, hint_json
        )
        hinted_history = str(hinted_only["history_json"])
        assert "[Progress hint]:" in hinted_history
        assert hint_text in hinted_history

    async def test_hint_free_rounds_stay_legacy_identical(
        self, sessions, progress_url, tmp_path
    ) -> None:
        """The default (no derivation output) keeps the round byte-
        identical: no ``[Progress hint]`` marker anywhere in history."""
        run_id = str(uuid.uuid4())
        await task_harness._ainsert_run(sessions, run_id)
        composed, _journal = task_harness._compose(
            sessions, progress_url, tmp_path, self._final_text_model()
        )
        intent = task_harness._intent_of(run_id)
        pins = task_harness._pins_of(composed)
        seed = agent_run.initial_history_json("You administer the host.", "go")

        legacy = await composed.amodel_round(intent, pins, seed, None, 0, None)
        assert "[Progress hint]" not in str(legacy["history_json"])
        assert legacy["status"] == "final"

        explicit_null = await composed.amodel_round(
            intent, pins, seed, None, 1, None, "null"
        )
        assert "[Progress hint]" not in str(explicit_null["history_json"])
