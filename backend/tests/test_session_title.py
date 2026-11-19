"""Session-title generator — pure seam acceptance tests (ST-PLAN §4.1/§6).

No provider, no database: the LLM round runs against a scripted
``FunctionModel`` (the ``test_session_runner`` precedent) and every other
claim is a pure call into :mod:`mai.agents.session_title`.

Proven here:

1. **Prompt construction** — the first message enters truncated (the tail
   of an over-long message never appears), and the prompt carries no
   session history, no agent system prompt vocabulary and no capability
   or tool names (D8).
2. **Wire honesty of the single round** — exactly ONE model request, its
   only part is the user prompt, the agent instructions are the title
   instruction, no tools, ``max_tokens`` is the title cap.
3. **Normalization** — quotes, numbering, markdown emphasis, trailing
   dots, whitespace collapse; rejections: empty, control characters,
   vertical whitespace; the 120-character rename bound with the
   stranded-cut tail cleanup.
4. **Degradation contract** — provider error, deadline, garbage output
   and secret-shaped output all yield ``None``; the function never
   raises on the model path.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from mai.agents import session_title
from mai.agents.session_title import (
    MAX_PROMPT_MESSAGE_CHARS,
    MAX_SESSION_TITLE_CHARS,
    TITLE_MAX_OUTPUT_TOKENS,
    _TITLE_INSTRUCTIONS,
    build_title_prompt,
    generate_session_title,
    normalize_generated_title,
)

pytestmark = pytest.mark.anyio


def _static_model(text: str, hook=None) -> FunctionModel:
    """One scripted FunctionModel answering ``text`` (runner precedent)."""

    def _respond(messages, info):
        if hook is not None:
            hook(messages, info)
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(_respond)


# --- prompt construction (pure) ------------------------------------------


def test_prompt_carries_bounded_message_never_the_tail():
    message = "A" * MAX_PROMPT_MESSAGE_CHARS + "NEVER-PROMPTED-TAIL"
    prompt = build_title_prompt(message)
    assert message[:MAX_PROMPT_MESSAGE_CHARS] in prompt
    assert "NEVER-PROMPTED-TAIL" not in prompt


def test_prompt_has_no_history_no_system_prompt_no_tool_names():
    prompt = build_title_prompt("Как обновить docker compose на сервере?")
    # The prompt's whole vocabulary is the title instruction + the
    # message itself: no capability/tool names, no history or policy
    # markers from the runner's composed instructions.
    for leak in (
        "app.discover",
        "app.resolve_service",
        "secret.store",
        "assistant",
        "history",
        "Binding rules:",
        "Run role:",
        "SERVICE MAP",
    ):
        assert leak not in prompt
    assert "docker compose" in prompt


# --- wire honesty of the single round ------------------------------------


async def test_generate_single_round_is_bare_and_bounded():
    calls: list[int] = []
    captured: dict[str, object] = {}

    def _hook(messages, info):
        calls.append(1)
        captured["messages"] = messages
        captured["info"] = info

    model = _static_model('"Deploy the stack"', hook=_hook)
    out = await generate_session_title(model, "How do I rotate the API key?")
    assert out == "Deploy the stack"
    assert sum(calls) == 1  # exactly ONE model request, no second turn

    (request,) = captured["messages"]
    assert [part.part_kind for part in request.parts] == ["user-prompt"]
    prompt = str(request.parts[0].content)
    assert prompt == build_title_prompt("How do I rotate the API key?")
    assert "rotate the API key" in prompt

    info = captured["info"]
    assert info.instructions == _TITLE_INSTRUCTIONS
    assert list(info.function_tools) == []
    assert info.model_settings == {"max_tokens": TITLE_MAX_OUTPUT_TOKENS}


# --- normalization: accepted shapes ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Deploy nginx", "Deploy nginx"),
        ("Обновить docker compose", "Обновить docker compose"),
        ('"Deploy nginx"', "Deploy nginx"),
        ("'Deploy nginx'", "Deploy nginx"),
        ("«Deploy nginx»", "Deploy nginx"),
        ("“Deploy nginx”", "Deploy nginx"),
        ("«\"Deploy nginx\"»", "Deploy nginx"),
        ("**Deploy nginx**", "Deploy nginx"),
        ("__Deploy nginx__", "Deploy nginx"),
        ("*Deploy nginx*", "Deploy nginx"),
        ("Deploy **nginx** fast", "Deploy nginx fast"),
        ("1. Deploy nginx", "Deploy nginx"),
        ("12) Deploy nginx", "Deploy nginx"),
        ("- Deploy nginx", "Deploy nginx"),
        ("* Deploy nginx", "Deploy nginx"),
        ("1. **Deploy** nginx", "Deploy nginx"),
        ("Deploy nginx.", "Deploy nginx"),
        ("Deploy nginx。", "Deploy nginx"),
        ("Deploy   the\tstack", "Deploy the stack"),
        ("  Deploy nginx  ", "Deploy nginx"),
    ],
)
def test_normalize_accepts(raw, expected):
    assert normalize_generated_title(raw) == expected


def test_normalize_preserves_leading_decimal_semver_ipv4_numbers():
    # A leading decimal/semver/IPv4 number is CONTENT, not list numbering:
    # the marker must never swallow the digits after its dot.
    for raw, expected in (
        ("1.5x speed improvement", "1.5x speed improvement"),
        ("0.18.0 migration failure", "0.18.0 migration failure"),
        ("192.168.3.23 unreachable", "192.168.3.23 unreachable"),
    ):
        assert normalize_generated_title(raw) == expected


def test_normalize_strips_dot_numbering_without_space():
    # The marker itself still clears without a following space, as long as
    # it is not a digit (that shape is a number, see the test above).
    assert normalize_generated_title("1.Title") == "Title"
    assert normalize_generated_title("12) Title") == "Title"


# --- normalization: rejections --------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "...", "。", '"   "'])
def test_normalize_rejects_empty_results(raw):
    assert normalize_generated_title(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "Deploy\x00nginx",
        "Deploy\x07nginx",
        "Deploy\x1b[31mnginx",  # ANSI escape, realistic terminal noise
        "Deploy\nnginx",
        "Deploy\r\nnginx",
    ],
)
def test_normalize_rejects_control_and_vertical_whitespace(raw):
    assert normalize_generated_title(raw) is None


@pytest.mark.parametrize("raw", [None, 42, b"Deploy nginx"])
def test_normalize_rejects_non_string_input(raw):
    assert normalize_generated_title(raw) is None


def test_normalize_enforces_rename_bound_and_cleans_cut_tail():
    # A hard cut strands the dot family only after the bound, never before.
    out = normalize_generated_title("A" * 119 + "....")
    assert out == "A" * 119
    assert len(out) <= MAX_SESSION_TITLE_CHARS
    # A cut that lands on whitespace leaves no stranded trailing space.
    assert normalize_generated_title("A" * 119 + " tail words") == "A" * 119


def test_normalize_keeps_exact_bound_title_untouched():
    exact = "B" * MAX_SESSION_TITLE_CHARS
    assert normalize_generated_title(exact) == exact


def test_normalize_keeps_meaningful_question_enders():
    # ``!``/``?`` are real sentence enders of an untouched title, not cut
    # artifacts; only the stranded-cut dot/dash family is stripped.
    assert normalize_generated_title("Что такое Docker?") == "Что такое Docker?"


# --- degradation contract --------------------------------------------------


async def test_generate_none_on_provider_error():
    def _boom(messages, info):
        raise RuntimeError("provider down")

    assert await generate_session_title(FunctionModel(_boom), "m") is None


async def test_generate_none_on_deadline(monkeypatch):
    monkeypatch.setattr(session_title, "TITLE_DEADLINE_SECONDS", 0.2)

    async def _stall(messages, info):
        await asyncio.sleep(30)

    started = time.monotonic()
    assert await generate_session_title(FunctionModel(_stall), "m") is None
    assert time.monotonic() - started < 5  # the hard deadline fired early


@pytest.mark.parametrize("raw", ["", "   ", "..."])
async def test_generate_none_on_garbage_output(raw):
    assert await generate_session_title(_static_model(raw), "m") is None


@pytest.mark.parametrize(
    "raw",
    [
        "sk-abcdefghijklmnopqrstuvwx",
        "The key is sk-abcdefghijklmnopqrstuvwx — use it",
    ],
)
async def test_generate_rejects_secret_shaped_output(raw):
    assert await generate_session_title(_static_model(raw), "m") is None
