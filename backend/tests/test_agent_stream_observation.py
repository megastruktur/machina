"""Focused live model/tool observation tests for the chat event stream."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import cast
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydantic_ai import Agent
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.models.function import (
    DeltaThinkingCalls, DeltaThinkingPart, DeltaToolCall,
    DeltaToolCalls,
)
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponsePart,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from mai.agents import run as agent_run
from pydantic_ai.tools import DeferredToolRequests, Tool
from mai.agents.output_safety import REDACTED
from mai.policy.evaluate import ExecutionContext, PolicyMode


def _intent() -> agent_run.AgentRunIntent:
    run_id = uuid.uuid4()
    server_id = uuid.uuid4()
    context = ExecutionContext(
        principal="hash:stream-test",
        server_id=server_id,
        run_id=run_id,
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=("web.read",),
        mode=PolicyMode.DELEGATED,
    )
    return agent_run.parse_agent_run_intent(json.dumps({
        "v": 1,
        "run_id": str(run_id),
        "server_id": str(server_id),
        "role": "operator",
        "task": "observe a live stream",
        "context": context.to_json(),
    }))


def _observer(intent):
    steps = SimpleNamespace(aemit_run_event=AsyncMock(return_value=1))
    bridge = SimpleNamespace(enabled_capabilities=["web.read"])
    executor = SimpleNamespace(
        _steps=steps,
        _bridge_of=lambda _intent: bridge,
    )
    return agent_run.ModelEventStreamObserver(
        cast(agent_run.AgentRunExecutor, executor), intent, 0
    ), steps


def test_text_thinking_and_tool_events_are_live_and_scrubbed():
    intent = _intent()
    observer, steps = _observer(intent)
    parts = [
        PartStartEvent(index=0, part=ThinkingPart(content="Plan ")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="first.")),
        PartStartEvent(index=1, part=TextPart(content="Answer ")),
        PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="now.")),
        PartStartEvent(index=2, part=ToolCallPart("web_read", '{"url": "https://example.test"}')),
        FunctionToolCallEvent(part=ToolCallPart("web_read", {
            "url": "https://user:password@example.test", "api_key": "synthetic-secret"
        })),
        FunctionToolResultEvent(part=ToolReturnPart(
            tool_name="web_read", tool_call_id="call-1", content="password=secret-value"
        )),
    ]
    asyncio.run(observer(None, _events(parts)))
    events = [call.args[1:] for call in steps.aemit_run_event.await_args_list]
    kinds = [(kind, data) for kind, data in events]
    thinking = "".join(data["delta"] for kind, data in kinds
                       if kind == "agent.output_delta" and data["kind"] == "thinking")
    text = "".join(data["delta"] for kind, data in kinds
                   if kind == "agent.output_delta" and data["kind"] == "text")
    assert thinking == "Plan first."
    assert text == "Answer now."
    requests = [data for kind, data in kinds if kind == "agent.tool_requested"]
    assert len(requests) == 2
    assert requests[-1]["args_partial"] is False
    assert requests[-1]["args"]["api_key"] == REDACTED
    assert "password" not in json.dumps(requests)
    results = [data for kind, data in kinds if kind == "agent.tool_result"]
    assert results[-1]["result"] == f"password={REDACTED}"
    assert requests[-1]["capability_id"] == "web.read"


async def _events(events):
    for event in events:
        yield event


async def _single_event(event):
    yield event


def test_real_agent_streams_deferred_tool_call_without_executing_it():
    intent = _intent()
    observer, steps = _observer(intent)

    async def streamed(
        messages: list[ModelResponsePart], info: AgentInfo
    ) -> AsyncGenerator[DeltaThinkingCalls | DeltaToolCalls, None]:
        yield DeltaThinkingCalls({0: DeltaThinkingPart(content="I should inspect the page.")})
        yield DeltaToolCalls({1: DeltaToolCall(name="web_read", json_args='{"url":' )})
        yield DeltaToolCalls({1: DeltaToolCall(json_args=' "https://example.test"}', tool_call_id="live-call")})

    async def scenario():
        async def record_observer(ctx, events):
            await observer(ctx, events)

        def deferred_read(url: str) -> str:
            raise CallDeferred

        tool = Tool.from_schema(
            deferred_read,
            name="web_read",
            description="Read a page",
            json_schema={
                "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]
            },
        )
        agent = Agent(FunctionModel(stream_function=streamed), output_type=[str, DeferredToolRequests], tools=[tool])
        return await agent.run("Inspect", event_stream_handler=record_observer)

    result = asyncio.run(scenario())
    assert isinstance(result.output, DeferredToolRequests)
    events = [call.args[1:] for call in steps.aemit_run_event.await_args_list]
    assert any(kind == "agent.output_delta" and data["kind"] == "thinking" for kind, data in events)
    requests = [data for kind, data in events if kind == "agent.tool_requested"]
    assert requests
    assert requests[-1]["tool_call_id"] == "live-call"
    assert requests[-1]["args"] == {"url": "https://example.test"}
    assert not any(kind == "agent.tool_result" for kind, data in events)
