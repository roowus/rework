"""ToolExecutor: how role tools reach the world.

LocalExecutor jails every path to the run's workdir (the v1 isolation line,
docs/design.md#isolation) and runs shell commands as subprocesses inside that
jail. The Docker milestone swaps the executor behind this same protocol; roles
never change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from rework.kernel.llm.types import ToolSpec

# tools every role gets; RoleSpec.tool_pack narrows via `allowed`
BUILTIN_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="read_file",
        description="Read a file inside the workspace. Args: {path}",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="list_dir",
        description="List a directory inside the workspace. Args: {path}",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    ),
    ToolSpec(
        name="run_command",
        description=(
            "Run a shell command inside the workspace (grep/rg, git, tests, builds)."
            " Args: {command, timeout_seconds?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    ),
]


class ToolExecutor(Protocol):
    """The sandbox seam. LocalExecutor now, DockerExecutor at the sandboxing
    milestone (M7); roles never see the difference."""

    async def execute(self, name: str, args: dict[str, Any]) -> str: ...

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]: ...


class LocalExecutor:
    """Path-jailed local tools. Every path arg is resolved against the workdir
    and rejected if it escapes (symlinks included)."""

    def __init__(self, workdir: Path, extra_specs: list[ToolSpec] | None = None) -> None:
        self._workdir = workdir.resolve()
        self._extra = extra_specs or []

    def _jail(self, path_str: str) -> Path:
        candidate = (self._workdir / path_str).resolve()
        if candidate != self._workdir and self._workdir not in candidate.parents:
            raise ValueError(f"path escapes workspace: {path_str}")
        return candidate

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "read_file":
                path = self._jail(str(args["path"]))
                text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
                lines = text.splitlines()
                head = lines[:2000]
                note = f"\n... ({len(lines) - 2000} more lines)" if len(lines) > 2000 else ""
                return "\n".join(head) + note
            if name == "list_dir":
                path = self._jail(str(args.get("path", ".")))
                entries = await asyncio.to_thread(sorted, path.iterdir())
                rendered = []
                for entry in entries[:500]:
                    kind = "d" if entry.is_dir() else "f"
                    size = entry.stat().st_size if entry.is_file() else ""
                    rendered.append(f"{kind} {entry.name} {size}".rstrip())
                return "\n".join(rendered) or "(empty)"
            if name == "run_command":
                command = str(args["command"])
                timeout = float(args.get("timeout_seconds", 120))
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(self._workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except TimeoutError:
                    proc.terminate()
                    rest, _ = await proc.communicate()
                    return (
                        f"(timeout after {timeout:.0f}s) partial output:\n"
                        f"{rest.decode(errors='replace')[:4000]}"
                    )
                text = out.decode(errors="replace")
                if len(text) > 8000:
                    text = (
                        text[:4000]
                        + f"\n... ({len(text) - 8000} chars truncated) ...\n"
                        + text[-4000:]
                    )
                return f"(exit {proc.returncode})\n{text or '(no output)'}"
            raise KeyError(f"unknown tool {name!r}")
        except KeyError as exc:
            return f"error: missing argument {exc}"
        except (ValueError, OSError) as exc:
            return f"error: {exc}"

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        all_specs = BUILTIN_TOOLS + self._extra
        if allowed is None:
            return list(all_specs)
        return [s for s in all_specs if s.name in allowed]
