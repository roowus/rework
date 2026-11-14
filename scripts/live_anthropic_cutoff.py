"""Live check: a reply cut off while thinking can be replayed through the adapter.

    uv run python scripts/live_anthropic_cutoff.py

Needs REWORK_ANTHROPIC_KEY (skips with exit 0 when it is unset). Spends a few
thousand tokens on claude-sonnet-5-5:

1. a real call with thinking on and a tiny max_tokens, which stops inside the
   thinking and yields a real thinking block with its signature;
2. the history the cut-off retry persists: user, assistant content "" with that
   thinking block, user "continue", sent with a session_id (cache marking on)
   through the project's adapter. Anthropic used to answer 400 "cache_control
   cannot be set for empty text blocks" and every later turn failed too;
3. a follow-up turn on top of the reply.

Exits 1 when any step is rejected.
"""

from __future__ import annotations

import asyncio
import os
import sys

from rework.kernel.llm.adapters.anthropic import AnthropicProvider, translate_request
from rework.kernel.llm.types import ChatRequest

MODEL = "claude-sonnet-5-5"
PROMPT = (
    "Find every integer solution (x, y) with x, y >= 0 of x^2 + y^2 = 2025^2 and "
    "explain your method. Think it through carefully before you answer."
)


def check(request: ChatRequest) -> None:
    """The invariants the API enforces, checked before the request is sent."""
    marked = 0
    for msg in translate_request(request)["messages"]:
        blocks = msg["content"] if isinstance(msg["content"], list) else [msg["content"]]
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                assert b["text"].strip(), f"empty text block in {msg['role']} turn"
            marked += isinstance(b, dict) and "cache_control" in b
    print(f"  payload ok: no empty text block, {marked} breakpoint(s) in messages")


async def main() -> int:
    key = os.environ.get("REWORK_ANTHROPIC_KEY")
    if not key:
        print("skipped: REWORK_ANTHROPIC_KEY is not set")
        return 0
    llm = AnthropicProvider(api_key=key)
    first = [{"role": "user", "content": PROMPT}]

    print(f"1. thinking call on {MODEL}, max_tokens=1024 (expect a cut-off in the thinking)")
    cut = await llm.complete(
        ChatRequest(model=MODEL, messages=first, max_tokens=1024, effort="high", session_id="live")
    )
    signed = [b for b in cut.thinking_blocks if b.get("signature") or b.get("data")]
    print(
        f"  stop_reason={cut.stop_reason} text={len(cut.text)} chars thinking_blocks={len(signed)}"
    )
    if not signed:
        print("FAIL: no signed thinking block to replay (the model did not think)")
        return 1

    history = [
        *first,
        # exactly what toolloop.py / agent.py / chat.py persist for a cut-off reply
        {"role": "assistant", "content": cut.text, "thinking_blocks": signed},
        {"role": "user", "content": "continue"},
    ]
    if cut.text.strip():
        print("  note: the reply had text, so this is not the thinking-only shape; blanking it")
        history[1]["content"] = ""
    print("2. replay the cut-off history (session_id set, so cache marking is on)")
    request = ChatRequest(
        model=MODEL, messages=history, max_tokens=4096, effort="low", session_id="live"
    )
    check(request)
    reply = await llm.complete(request)
    print(f"  OK stop_reason={reply.stop_reason} text={reply.text[:80]!r}")

    print("3. follow-up turn on top of it")
    history += [
        {"role": "assistant", "content": reply.text, "thinking_blocks": reply.thinking_blocks},
        {"role": "user", "content": "Now state the answer in one sentence."},
    ]
    request = request.model_copy(update={"messages": history})
    check(request)
    again = await llm.complete(request)
    print(f"  OK stop_reason={again.stop_reason} text={again.text[:80]!r}")
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # a 400 from the API lands here
        print(f"FAIL: {type(exc).__name__}: {str(exc)[:400]}")
        sys.exit(1)
