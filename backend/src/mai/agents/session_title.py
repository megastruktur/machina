"""LLM-generated chat session titles — pure generator + one bounded call.

Campaign ``machina-session-title`` (ST-PLAN §4.1). This module is the ONLY
title-text seam: it builds the bounded prompt, normalizes the model's raw
answer into the stored-title vocabulary (1..120 characters, matching
``arename_session``), and wraps ONE PydanticAI round under a hard deadline.
No database, no IO at import; the caller supplies an already-built model
(the ``build_model`` precedent — credentials are resolved upstream, never
here).

Governing rules:

- **Trust boundary (D8): the model's output is DATA, never authority.**
  The call carries no tools, no session history, no agent system prompt
  and no second turn, so the model is given nothing it could act on. Its
  text is treated as untrusted input end to end: redacted through
  :func:`mai.agents.output_safety.redact_text`, rejected outright when a
  redaction marker survives (the model echoed a secret), then normalized
  and bounded before anyone may store it. Nothing the model returns is
  ever interpreted as an instruction.
- **ONE bounded attempt, then degrade to ``None``** — unlike compaction's
  retry-then-fallback (``session_compaction._summarize_with_retry``), the
  title is cosmetic: a retry would only delay the chat run, and a missing
  title is honest (the drawer falls back to the session id). Any failure —
  provider error, deadline exceeded, empty or garbage output, echoed
  secret — degrades to ``None``. The function never raises on the model
  path (an external task cancellation still propagates, matching the
  ``_run_probe`` precedent in ``api/models.py``).
- **Minimal prompt by construction** — the title instruction plus the
  first user message, truncated to :data:`MAX_PROMPT_MESSAGE_CHARS`
  before it enters the prompt. Nothing else crosses the boundary.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from mai.agents.output_safety import REDACTED, redact_text
from mai.contracts.sessions import UNSAFE_TEXT_CLASS

__all__ = [
    "MAX_PROMPT_MESSAGE_CHARS",
    "MAX_SESSION_TITLE_CHARS",
    "TITLE_DEADLINE_SECONDS",
    "TITLE_MAX_OUTPUT_TOKENS",
    "build_title_prompt",
    "generate_session_title",
    "normalize_generated_title",
]

#: First-message bound applied BEFORE the text enters the prompt
#: (ST-PLAN §4.1): a longer message is cut, never echoed whole.
MAX_PROMPT_MESSAGE_CHARS: Final[int] = 1000

#: Hard wall-clock deadline for the single title round (``_run_probe``
#: precedent): a hung endpoint must degrade to ``None``, not pin the
#: worker past the visible answer.
TITLE_DEADLINE_SECONDS: Final[float] = 15.0

#: Output-token cap forwarded on the title request (a title is ONE short
#: phrase; the cap bounds cost and blocks essay-shaped output).
TITLE_MAX_OUTPUT_TOKENS: Final[int] = 32

#: Stored-title bound, matching ``arename_session`` (``session_store.py``):
#: the drawer's rename vocabulary is exactly this width.
MAX_SESSION_TITLE_CHARS: Final[int] = 120

#: The title instruction, carried BOTH as the agent's ``instructions`` and
#: at the head of the prompt so the prompt is self-contained (a bare
#: harness can exercise :func:`build_title_prompt` alone). The requested
#: shape — ONE short nominal phrase in the message's language, with the
#: exact hygiene rules :func:`normalize_generated_title` enforces — is
#: stated here once; the normalizer is the enforcement, not the model.
_TITLE_INSTRUCTIONS: Final[str] = (
    "You generate chat session titles. Reply with ONE short nominal "
    "phrase (at most 60 characters) in the same language as the user "
    "message. No quotes, no markdown, no trailing period, no "
    "explanations — reply with the title text only."
)

#: Leading list numbering (``1.``/``12)`` marker or a ``-``/``*``/``•``
#: bullet) the model sometimes wraps its answer in despite the
#: instruction.
_LEADING_NUMBERING: Final[re.Pattern[str]] = re.compile(
    r"^(?:\d{1,3}[.)]\s*|[-*•]\s+)"
)

#: Surrounding quote pairs, stripped outermost first so «"Title"» unwraps
#: fully. Straight quotes pair with themselves.
_QUOTE_PAIRS: Final[dict[str, str]] = {
    "«": "»",
    "“": "”",
    "‘": "’",
    "'": "'",
    '"': '"',
}

#: Horizontal whitespace runs collapse to ONE space; vertical whitespace
#: (never legal in a stored title) survives the collapse and is rejected.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"[^\S\n\r]+")

#: The control characters rejected verbatim from
#: ``mai.contracts.sessions`` (the safe-text class): a title is prose,
#: never binary or terminal noise.
_UNSAFE_TEXT: Final[re.Pattern[str]] = re.compile(f"[{UNSAFE_TEXT_CLASS}]")

#: Characters stripped from the tail AFTER the length cut — a hard cut can
#: strand a space, a dot or a dash mid-phrase. Meaningful ``!``/``?``
#: enders of an untouched title are deliberately kept.
_TRAILING_CUT_CHARS: Final[str] = " \t.。…:;,-–—"


def build_title_prompt(first_message: str) -> str:
    """Build the ONE bounded title prompt (ST-PLAN §4.1).

    The instruction asks for ONE short nominal phrase (at most 60
    characters) in the message's own language with the hygiene rules the
    normalizer enforces; the first message is truncated to
    :data:`MAX_PROMPT_MESSAGE_CHARS` BEFORE it enters the prompt. The
    prompt carries ONLY this text — no session history, no agent system
    prompt, no tool names (D8).
    """
    bounded = first_message[:MAX_PROMPT_MESSAGE_CHARS]
    return (
        f"{_TITLE_INSTRUCTIONS}\n"
        "\n"
        "User message:\n"
        f"{bounded}"
    )


def normalize_generated_title(raw: str) -> str | None:
    """Project ONE raw model answer into the stored-title vocabulary.

    Strips surrounding quotes, leading list numbering, markdown emphasis,
    trailing dots and stranded whitespace; collapses horizontal whitespace
    runs; enforces the :data:`MAX_SESSION_TITLE_CHARS` rename bound. The
    cut happens first, the stranded-cut tail cleanup after it, so a hard
    cut never leaves a trailing space or dot behind.

    Returns ``None`` — never raises — for non-string input, an empty
    result, control characters (the ``UNSAFE_TEXT_CLASS`` of
    ``mai.contracts.sessions``) or vertical whitespace surviving the
    collapse: all four are garbage the model was told not to produce.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # Surrounding quotes, outermost pair first (bounded unwrap so a
    # doubled «"Title"» comes off clean).
    for _ in range(4):
        if len(text) < 2:
            break
        closer = _QUOTE_PAIRS.get(text[0])
        if closer is None or text[-1] != closer:
            break
        text = text[1:-1].strip()
    # Leading list numbering ("1. ", "- ", "* ").
    text = _LEADING_NUMBERING.sub("", text)
    # Markdown emphasis: heavy markers anywhere (they are never meaningful
    # prose), edge markers only at the ends so an internal snake_case or
    # "a_b_c" underscore is never mangled.
    text = text.replace("**", "").replace("__", "").strip("*_").strip()
    # Internal horizontal whitespace series collapse to one space.
    text = _WHITESPACE_RUN.sub(" ", text)
    if not text:
        return None
    if _UNSAFE_TEXT.search(text):
        return None
    if "\n" in text or "\r" in text:
        return None
    # Length cut first, stranded-cut tail cleanup after it.
    text = text[:MAX_SESSION_TITLE_CHARS].rstrip(_TRAILING_CUT_CHARS)
    if not text:
        return None
    return text


async def generate_session_title(model: Any, first_message: str) -> str | None:
    """ONE bounded title round: prompt → model → redact → normalize.

    The caller supplies an already-built PydanticAI model (the
    ``build_model`` precedent — credential resolution is upstream, never
    here). The agent carries NO tools, NO session history and NO agent
    system prompt (D8): the request is the bare title instruction plus
    the truncated first message, under :data:`TITLE_DEADLINE_SECONDS` and
    :data:`TITLE_MAX_OUTPUT_TOKENS`.

    The output is UNTRUSTED DATA (D8): it is redacted through
    :func:`mai.agents.output_safety.redact_text`, and the WHOLE answer is
    refused when a redaction marker survives — the model echoed a secret,
    and a title carrying ``[REDACTED]`` is worse than no title. The
    survivor is normalized; any failure — provider error, deadline,
    empty or garbage output — degrades to ``None``. The function never
    raises on the model path (an external task cancellation still
    propagates, matching the ``_run_probe`` precedent).
    """
    try:
        agent: Agent[None, str] = Agent(
            model,
            instructions=_TITLE_INSTRUCTIONS,
            tools=[],
            output_type=str,
        )
        settings: ModelSettings = {"max_tokens": TITLE_MAX_OUTPUT_TOKENS}
        async with asyncio.timeout(TITLE_DEADLINE_SECONDS):
            result = await agent.run(
                build_title_prompt(first_message),
                model_settings=settings,
            )
        output = result.output
    except Exception:  # noqa: BLE001 - ANY model-path failure degrades to None
        return None
    if not isinstance(output, str):
        return None
    redacted = redact_text(output)
    if REDACTED in redacted:
        # The model echoed secret-shaped text: refuse the whole answer.
        return None
    return normalize_generated_title(redacted)
