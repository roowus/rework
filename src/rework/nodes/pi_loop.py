"""A role's agent loop run by pi (ADR 0022): the agent is pi, the role is rework.

A role is rigid nodes around ONE agent loop. The native loop (nodes/toolloop.py)
is rework's own; this node hands the same job to pi, a tuned coding agent, and
keeps everything around it identical: it takes the role's opening messages and
system prompt, returns `{"messages", "loop_turns", "final_text"}` exactly like
`make_toolloop_node`, so the report writer, the handoff and every rail downstream
cannot tell which loop ran.

What rework keeps doing, around pi:
- ledger: every assistant message's usage is charged as it streams (model, tokens,
  pi's own cost estimate), so mission spend and the burn gate see it live;
- budget: past the role's cap the process is stopped and the stop is reported, the
  same way the native loop stops at its cap;
- trajectory: turns and tool calls are published as PROGRESS events, so the TUI
  shows pi's work like any other role's;
- containment: pi runs in the run's workspace with the role environment allowlist
  (ADR 0009), its tools limited to what the role may use (read-only roles get
  `read` only), and an isolated agent dir so no user extension loads.

Mid-loop owner/orchestrator questions and Library tools are NOT available inside
pi yet (they need an MCP bridge); a pi-run role finishes with what it has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from rework.kernel.budget.ledger import Budget
from rework.kernel.state.records import Charge, EventRecord, EventType
from rework.kernel.state.store import StateStore

log = logging.getLogger("rework.nodes")

PI_TIMEOUT_S = 30 * 60
# tools a read-only role (investigator, auditor, surveyor) may hand to pi; a
# writing role adds edit/write/bash
READ_ONLY_TOOLS = ("read",)
WRITE_TOOLS = ("read", "edit", "write", "bash")


def pi_binary() -> str | None:
    return os.environ.get("REWORK_PI_BIN") or shutil.which("pi")


def pi_target(model_id: str) -> tuple[str, str]:
    """(pi provider, pi model) for a rework catalog id.

    REWORK_PI_PROVIDER names pi's provider (default `9router`); the model is the
    catalog entry's provider-side id. Kept explicit and env-only: pi and rework
    keep their own model catalogs, and this is the one mapping between them."""
    return os.environ.get("REWORK_PI_PROVIDER", "9router"), model_id


def _agent_dir() -> Path:
    """An isolated pi agent dir: the owner's models/auth, none of their
    extensions (an extension flag hangs `pi -p`, and a role must not run
    someone's editor plugins)."""
    src = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    dest = Path(tempfile.mkdtemp(prefix="rework-pi-"))
    for name in ("models.json", "auth.json", "settings.json"):
        if (src / name).is_file():
            shutil.copy2(src / name, dest / name)
    return dest


def argv(
    binary: str,
    prompt: str,
    system_prompt: str,
    provider: str,
    model: str,
    tools: tuple[str, ...],
    thinking: str | None,
) -> list[str]:
    out = [
        binary,
        "-p",
        prompt,
        "--mode",
        "json",
        "--no-session",
        "--provider",
        provider,
        "--model",
        model,
        "--system-prompt",
        system_prompt,
        "--tools",
        ",".join(tools),
    ]
    if thinking:
        out += ["--thinking", thinking]
    return out


def make_pi_loop_node(
    *,
    store: StateStore,
    bus: Any | None,
    mission_id: str,
    run_id: str,
    model_id: str,
    system_prompt: str,
    workdir: Path,
    budget: Budget,
    env: dict[str, str],
    tools: tuple[str, ...] = READ_ONLY_TOOLS,
    effort: str | None = None,
    price: Any | None = None,
) -> Any:
    """`price(input_tokens, output_tokens, cached_tokens)` is the catalog
    entry's estimate_cost; None falls back to pi's own cost figure."""

    async def emit(payload: dict[str, Any]) -> None:
        if bus is None:
            return
        await bus.publish(
            EventRecord(
                mission_id=mission_id, run_id=run_id, type=EventType.PROGRESS, payload=payload
            )
        )

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        binary = pi_binary()
        if binary is None:
            raise FileNotFoundError("pi is not installed (no `pi` on PATH, REWORK_PI_BIN unset)")
        messages: list[dict[str, Any]] = list(state.get("messages") or [])
        prompt = "\n\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "user"
        )
        provider, model = pi_target(model_id)
        agent_dir = await asyncio.to_thread(_agent_dir)
        run_env = {**env, "PI_CODING_AGENT_DIR": str(agent_dir)}
        proc = await asyncio.create_subprocess_exec(
            *argv(binary, prompt, system_prompt, provider, model, tools, effort),
            cwd=str(workdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
            start_new_session=True,
        )
        turns = 0
        spent = 0.0
        final_text = ""
        stop_note: str | None = None
        error = ""

        async def read() -> None:
            nonlocal turns, spent, final_text, stop_note, error
            assert proc.stdout is not None
            async for raw in proc.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "tool_execution_start":
                    args = json.dumps(event.get("args") or {})[:160]
                    await emit(
                        {
                            "trajectory": "tool_call",
                            "name": event.get("toolName"),
                            "arguments": args,
                        }
                    )
                if kind != "message_end":
                    continue
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                turns += 1
                usage = message.get("usage") or {}
                in_tok = int(usage.get("input") or 0)
                cached = int(usage.get("cacheRead") or 0)
                out_tok = int(usage.get("output") or 0)
                # priced by REWORK's catalog, like the native loop: pi's own
                # price list misses models it does not know (found live: three
                # of four 9router calls came back at $0, so spend undercounted)
                cost = (
                    price(in_tok + cached, out_tok, cached)
                    if price is not None
                    else float((usage.get("cost") or {}).get("total") or 0.0)
                )
                spent += cost
                await store.charge(
                    Charge(
                        mission_id=mission_id,
                        run_id=run_id,
                        model=str(message.get("model") or model),
                        input_tokens=in_tok + cached,
                        output_tokens=out_tok,
                        cached_tokens=cached,
                        cost_usd=cost,
                    )
                )
                text = "".join(
                    str(part.get("text") or "")
                    for part in message.get("content") or []
                    if isinstance(part, dict) and part.get("type") == "text"
                )
                if text:
                    final_text = text
                await emit(
                    {
                        "trajectory": "turn",
                        "turn": turns,
                        "text": text[:400],
                        "tokens": int(usage.get("totalTokens") or 0),
                    }
                )
                if message.get("stopReason") == "error":
                    error = str(message.get("errorMessage") or "pi reported an error")
                if budget.max_usd is not None and spent > budget.max_usd:
                    stop_note = f"stopped: budget ${budget.max_usd:.2f} reached (pi loop)"
                    from rework.nodes.tools import _kill_tree

                    await _kill_tree(proc)
                    return

        try:
            await asyncio.wait_for(asyncio.gather(read(), proc.wait()), PI_TIMEOUT_S)
        except TimeoutError:
            from rework.nodes.tools import _kill_tree

            await _kill_tree(proc)
            stop_note = f"stopped: the pi loop ran past {PI_TIMEOUT_S}s"
        finally:
            await asyncio.to_thread(shutil.rmtree, agent_dir, True)
        if error and not final_text:
            raise RuntimeError(f"pi loop failed: {error}"[:500])
        if final_text:
            messages.append({"role": "assistant", "content": final_text})
        return {
            "messages": messages,
            "loop_turns": turns,
            "final_text": final_text if stop_note is None else f"{stop_note}\n\n{final_text}",
        }

    return node
