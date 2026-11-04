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
from rework.kernel.state.records import Complaint, ComplaintKind
from rework.kernel.state.store import StateStore

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

COMPLAINT_SPEC = ToolSpec(
    name="file_complaint",
    description=(
        "File feedback on the WORKFLOW ITSELF (not the task): a tool you lacked,"
        " information the audit needed, a prompt that forced rework. Fire-and-forget,"
        " never pauses the mission. Args: {kind, summary, detail?, evidence?, suggestion?}"
        " - kind: workflow_gripe|needed_info|tool_missing|prompt_issue|bug_verified|process_issue"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [k.value for k in ComplaintKind],
            },
            "summary": {"type": "string"},
            "detail": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "suggestion": {"type": "string"},
        },
        "required": ["kind", "summary"],
    },
)


class ToolExecutor(Protocol):
    """The sandbox seam. LocalExecutor now, DockerExecutor at the sandboxing
    milestone (M7); roles never see the difference."""

    async def execute(self, name: str, args: dict[str, Any]) -> str: ...

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]: ...


class LazyExecutor:
    """Executor that binds its jail target at prepare time. The coder's
    worktree is created by its prepare node, but the loop node is wired at
    build_graph time; this wrapper forwards to a LocalExecutor once attached."""

    def __init__(self, fallback_specs: list[ToolSpec] | None = None) -> None:
        self._inner: LocalExecutor | None = None
        self._fallback = fallback_specs or list(BUILTIN_TOOLS)

    def attach(self, workdir: Path) -> None:
        self._inner = LocalExecutor(workdir)

    @property
    def attached(self) -> bool:
        return self._inner is not None

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if self._inner is None:
            raise RuntimeError("LazyExecutor used before attach()")
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        if self._inner is None:
            return [s for s in self._fallback if allowed is None or s.name in allowed]
        return self._inner.specs(allowed)


class LocalExecutor:
    """Path-jailed local tools. Every path arg is resolved against the workdir
    and rejected if it escapes (symlinks included).

    Known boundary (found live, first real eval run): the jail is ADVISORY for
    run_command. The shell starts inside the workdir, but a command may `cd`
    to an absolute path outside it and work there; inspecting the shell string
    is not a security boundary. The coder prompt pins cwd to the worktree, the
    eval harness seeds fixture repos outside the workroot, and the real fix is
    the Docker executor (M7)."""

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


class ComplaintAwareExecutor:
    """Wraps any ToolExecutor and answers `file_complaint` from the store
    (ADR 0004: typed, fire-and-forget feedback on the workflow itself).
    Everything else delegates untouched, so the jail (or the Docker sandbox,
    later) keeps doing the isolating."""

    def __init__(
        self,
        inner: ToolExecutor,
        store: StateStore,
        mission_id: str,
        run_id: str,
        role: str,
    ) -> None:
        self._inner = inner
        self._store = store
        self._mission_id = mission_id
        self._run_id = run_id
        self._role = role

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name == "file_complaint":
            try:
                complaint = Complaint(
                    kind=ComplaintKind(str(args.get("kind") or "")),
                    source_mission_id=self._mission_id,
                    source_run_id=self._run_id,
                    source_role=self._role,
                    summary=str(args.get("summary") or "")[:500],
                    detail=str(args.get("detail") or ""),
                    evidence=[str(e) for e in (args.get("evidence") or [])][:20],
                    suggestion=str(args.get("suggestion") or ""),
                )
            except ValueError:
                return "error: kind must be one of " + ", ".join(k.value for k in ComplaintKind)
            if not complaint.summary:
                return "error: summary is required"
            await self._store.file_complaint(complaint)
            return f"complaint filed: {complaint.id}"
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        base = self._inner.specs(allowed)
        if allowed is not None and "file_complaint" not in allowed:
            return base
        return [*base, COMPLAINT_SPEC]
