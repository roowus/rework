"""The harness role: an external coding agent does the loop, rework keeps the wrapper.

A coding role is a worktree, a loop and a handoff. The `coder` runs rework's own
tool loop; this role runs someone else's whole coding agent (Claude Code, Codex)
as a subprocess and lets it own the loop. Everything either side of the loop is
the SAME code as the coder's (`coding.py`), so the artifact, the red-green check,
the auditor's auto-wiring and the delivery path cannot tell them apart.

Why a separate role rather than a flag on `coder`: the orchestrator picks roles by
name and description, and this one has different economics (no daemon model call
for the loop, its own token budget, its own session). Keeping it separate keeps
that choice visible in the plan rather than hidden in a brief.

A harness edits files and leaves a commit; it never merges, never opens a PR and
never opens a gate. Those are rails, and a rail is not something a worker decides
(ADR 0015).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType
from rework.nodes.tools import role_env
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.builtin.coder import CoderState
from rework.roles.builtin.coding import handoff_node, prepare_node

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

HARNESS_TIMEOUT_S = 45 * 60


class HarnessState(CoderState, total=False):
    """The coder's state plus what the harness run reports about itself."""

    harness: dict[str, Any]


def _binary(name: str) -> str | None:
    """Which harness CLI to run, and where. `REWORK_HARNESS_BIN_<NAME>` wins (a
    test sets it), then PATH."""
    override = os.environ.get(f"REWORK_HARNESS_BIN_{name.upper()}")
    if override:
        return override
    import shutil

    return shutil.which(name)


def harness_argv(name: str, prompt: str, workdir: Path) -> list[str]:
    """The CLI for one harness, as argv (never a shell string)."""
    binary = _binary(name)
    if binary is None:
        raise FileNotFoundError(
            f"the {name} harness is not installed (no `{name}` on PATH); set "
            f"REWORK_HARNESS_BIN_{name.upper()} or pick another harness"
        )
    if name == "claude":
        # non-interactive, streaming JSON so the trajectory can show progress.
        #
        # Permissions, found the hard way on a real run:
        #   - `dontAsk` denies Edit, Write AND Bash: the worker cannot change a
        #     single file, so the role is useless;
        #   - `acceptEdits` allows the edits but NOT Bash, so it can change the
        #     code and then never run the test: red-green is impossible and it
        #     honestly reports red_green_verified=false.
        # A coding worker needs BOTH. `--allowedTools` grants exactly the tools
        # the job needs (edit, write, run a command) and leaves the rest denied,
        # which is narrower than `bypassPermissions` and is the containment the
        # worktree already provides (plus the ADR 0014 container when enabled).
        return [
            binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Edit",
            "Write",
            "Read",
            "Bash",
        ]
    if name == "codex":
        return [binary, "exec", "--json", "--cd", str(workdir), prompt]
    raise ValueError(f"unknown harness {name!r}; known: claude, codex")


class HarnessSpec(RoleSpec):
    name = "harness"
    description = (
        "Implements a change by running an external coding agent (Claude Code or Codex)"
        " inside the run's worktree + branch, then mechanically commits and packages"
        " the diff. Use it when a whole harness does the work better than rework's loop."
    )
    # the same contract as the coder: the artifact is a coder_handoff
    goal = (
        "Fulfill the task completely and make the code work reliably and well:"
        " implement it properly, prove it with real test runs, and report"
        " exactly what passed and what failed."
    )
    default_model = "fake"  # no daemon LLM call: the harness brings its own
    context_manifest: ClassVar[set[str]] = {"brief", "investigation", "requested_scope"}

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        if not ctx.mission.repo_path:
            raise ValueError("harness role requires mission.repo_path (a git repo)")
        harness_name = (os.environ.get("REWORK_HARNESS") or "claude").strip().lower()
        prepare = prepare_node(ctx, executor=None)
        handoff = handoff_node(ctx)

        async def run_node(state: HarnessState) -> dict[str, Any]:
            """One subprocess, its stream fed to the trajectory, its final text
            the coder's `final_text` so the same tests block is parsed."""
            worktree = Path(state["worktree"])
            prompt = "\n\n".join(
                m["content"] for m in state.get("messages") or [] if m.get("role") == "user"
            )
            # The SAME contract the coder is held to, in the harness's own
            # prompt: the tests block is what the red-green rail reads, so a
            # worker that never emits it fails the rail for a reason it could
            # have avoided. The harness does not know rework's conventions; say
            # them.
            prompt += (
                "\n\nIMPORTANT (rework): when you are done, end your final message with a "
                "fenced json block recording the tests you ran, exactly:\n"
                '```json\n{"tests": {"added": [], "changed": [], '
                '"red_green_verified": true, "commands": ["the command you ran"], '
                '"result": "red then green: ...", "not_testable_reason": ""}}\n```\n'
                "Work only inside this directory. Do not leave scratch scripts or probe "
                "files behind: this directory is committed as-is."
            )
            argv = harness_argv(harness_name, prompt, worktree)
            timeout = float((ctx.run.context or {}).get("max_seconds") or HARNESS_TIMEOUT_S)
            # a fresh session per run, under the run's own dir: two runs must
            # never share a harness session
            session_dir = ctx.workdir / "harness-session"
            session_dir.mkdir(parents=True, exist_ok=True)
            env = role_env(worktree, ctx.mission.id)
            env["REWORK_HARNESS_SESSION"] = str(session_dir)

            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(worktree),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            final_text = ""
            turns = 0
            events: list[dict[str, Any]] = []
            # the CLI's own verdict on the run. `claude -p` exits 1 when it stops
            # short of its end (a tool it could not use, a turn limit), and 0 when
            # it reached `end_turn`: its `result` event carries `is_error`.
            self_ok = False

            result_text: str = ""  # the CLI's own final `result` field wins

            async def read_stream() -> None:
                nonlocal final_text, turns, result_text, self_ok
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    note = line
                    try:
                        data = json.loads(line)
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        # a TURN is one assistant message, not one stream line:
                        # thinking-token deltas alone run into the thousands
                        if data.get("type") == "assistant":
                            turns += 1
                        if isinstance(data.get("result"), str) and data.get("type") in (
                            "result",
                            "turn.completed",
                        ):
                            result_text = str(data["result"])
                            if not data.get("is_error"):
                                self_ok = True
                        text = _final_text_of(data)
                        if text:
                            final_text = text
                        note = _progress_note(data)
                        events.append({k: v for k, v in data.items() if k != "content"})
                    await ctx.bus.publish(
                        EventRecord(
                            mission_id=ctx.mission.id,
                            run_id=ctx.run.id,
                            type=EventType.PROGRESS,
                            payload={"note": f"harness: {note[:400]}"},
                        )
                    )

            try:
                await asyncio.wait_for(_drain(proc, read_stream), timeout=timeout)
            except TimeoutError:
                from rework.nodes.tools import _kill_tree

                await _kill_tree(proc)
                raise RuntimeError(
                    f"the {harness_name} harness ran past {timeout:.0f}s and was killed"
                ) from None
            # What "the harness succeeded" means: it reached its own end
            # (self_ok), not that the process exited 0. A harness can exit
            # non-zero after a perfectly good edit (found live: it used a
            # shell probe that a sandbox refused, then finished the work anyway
            # and reported success in its result event). It FAILED only when
            # neither its own verdict nor any text survived.
            if not self_ok and not final_text:
                raise RuntimeError(
                    f"the {harness_name} harness exited {proc.returncode} without reaching "
                    "its end and produced no output"
                )
            return {
                "final_text": result_text or final_text or (state.get("final_text") or ""),
                "harness": {
                    "name": harness_name,
                    "turns": turns,
                    "exit_code": proc.returncode,
                    "session": str(session_dir),
                    "last_events": events[-20:],
                },
            }

        graph = StateGraph(HarnessState)
        graph.add_node("prepare", prepare)
        graph.add_node("run", run_node)
        graph.add_node("handoff", handoff)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "run")
        graph.add_edge("run", "handoff")
        graph.add_edge("handoff", END)
        return graph.compile()


async def _drain(proc: Any, reader: Any) -> None:
    await asyncio.gather(reader(), proc.wait())


def _final_text_of(data: dict[str, Any]) -> str:
    """The harness's final assistant message, across the stream shapes the two
    CLIs use. Empty for every intermediate event."""
    if data.get("type") in ("result", "turn.completed") and isinstance(data.get("result"), str):
        return str(data["result"])
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("role") == "assistant":
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
    return ""


def _progress_note(data: dict[str, Any]) -> str:
    kind = str(data.get("type") or data.get("event") or "event")
    summary = data.get("summary") or data.get("subtype") or data.get("name") or ""
    return f"{kind} {summary}".strip()
