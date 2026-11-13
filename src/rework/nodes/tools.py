"""ToolExecutor: how role tools reach the world.

LocalExecutor jails every path to the run's workdir (the v1 isolation line,
docs/design.md#isolation) and runs shell commands as subprocesses inside that
jail. The Docker milestone swaps the executor behind this same protocol; roles
never change.
"""

from __future__ import annotations

import asyncio
import re
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


# A leading `cd <abs path>` in a shell command. Agents write these from memory
# and get them wrong (found live: a coder invented
# .../<mission>/code_coder_2/worktree when the real dir is
# .../<mission>_code_coder_2/worktree), and the failure is INVISIBLE: /bin/sh
# exits 2 on the failed cd, every later command in the chain silently skips,
# and the model reads a truncated transcript as if the work happened.
_LEADING_CD = re.compile(r"^\s*cd\s+(?P<path>'[^']*'|\"[^\"]*\"|[^\s;&|]+)")


class LocalExecutor:
    """Path-jailed local tools. Every path arg is resolved against the workdir
    and rejected if it escapes (symlinks included).

    Known boundary (found live, first real eval run): the jail is ADVISORY for
    run_command. The shell starts inside the workdir, but a command may `cd`
    to an absolute path outside it and work there; inspecting the shell string
    is not a security boundary. The coder prompt pins cwd to the worktree, the
    eval harness seeds fixture repos outside the workroot, and the real fix is
    the Docker executor (M7).

    What the inspection DOES buy (found live, prod mission m-c8b72e7734b7): the
    shell is already inside the workdir, so a leading absolute `cd` is never
    necessary. When one points outside the workdir — or at a path that does not
    exist — we refuse it and say where it actually is, instead of handing back
    an opaque exit 2 the model misreads as success."""

    def __init__(self, workdir: Path, extra_specs: list[ToolSpec] | None = None) -> None:
        self._workdir = workdir.resolve()
        self._extra = extra_specs or []
        reap_orphaned_commands()

    def _jail(self, path_str: str) -> Path:
        candidate = (self._workdir / path_str).resolve()
        if candidate != self._workdir and self._workdir not in candidate.parents:
            raise ValueError(f"path escapes workspace: {path_str}")
        return candidate

    def _check_leading_cd(self, command: str) -> str | None:
        """Return an error message if the command starts with an absolute `cd`
        we should refuse, else None. The shell already runs in the workdir, so
        `cd .`, relative cd's and `cd $PWD` are all no-ops we leave alone."""
        match = _LEADING_CD.match(command)
        if not match:
            return None
        raw = match.group("path").strip().strip("'\"")
        if not raw.startswith(("/", "~")):
            return None
        target = Path(raw).expanduser()
        if target.resolve() == self._workdir:
            return None
        exists = target.exists()
        hint = (
            f"error: refusing `cd {raw}`."
            f" You are ALREADY inside your worktree: {self._workdir}."
            " Relative paths (and no cd at all) are correct here."
        )
        if not exists:
            hint += " That path does not exist."
        else:
            hint += " That path is outside the worktree; work done there is lost at handoff."
        return hint

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
                refusal = self._check_leading_cd(command)
                if refusal is not None:
                    return refusal
                timeout = float(args.get("timeout_seconds", 120))
                # own session/process group: on timeout the WHOLE tree dies.
                # Found live: terminate() killed only /bin/sh; the orphaned
                # `uv run pytest` kept the pipe open and communicate() blocked
                # forever, hanging the role and its mission with no log.
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(self._workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    env=role_env(self._workdir),
                )
                mark = _register_command(proc.pid, command)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except TimeoutError:
                    rest = await _kill_tree(proc)
                    mark.unlink(missing_ok=True)
                    return (
                        f"(timeout after {timeout:.0f}s; the command and its children were "
                        f"killed) partial output:\n{rest.decode(errors='replace')[:4000]}"
                    )
                mark.unlink(missing_ok=True)
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


def role_env(workdir: Path | None = None) -> dict[str, str]:
    """The daemon's environment, minus its own Python environment when the
    worktree is a Python project of its own (pyproject.toml). Found live: the
    daemon runs from the main checkout's venv (`uv run rework serve`), every role
    command inherited VIRTUAL_ENV and that venv's bin first on PATH, so a bare
    `python`/`pytest` in a rework worktree imported the MAIN checkout's source
    and audits tested the wrong code. A plain repo (no pyproject, e.g. a toy
    repo whose tests run with `python3 -m pytest`) keeps the daemon's tools.

    Also redirects any rework-state paths that rework reads/writes under
    ~/.rework (targets, env file, TUI settings, logs) to a per-run scratch
    directory inside the role's own workdir, so a role command cannot silently
    touch or redirect from the real ~/.rework on a dev machine."""
    import os
    import sys

    env = dict(os.environ)

    # Always redirect rework-state paths to a per-run scratch dir under the
    # role's workdir (if one was given), regardless of whether the worktree has
    # a pyproject.toml.  This prevents role commands from reading/writing the
    # real ~/.rework (targets.toml, rework.env, tui.json, logs) and
    # accidentally redirecting which target/db rework talks to.
    if workdir is not None:
        scratch = workdir / ".rework-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        # Only set if not already overridden by the caller's environment.
        env.setdefault("REWORK_TARGETS", str(scratch / "targets.toml"))
        env.setdefault("REWORK_ENV_FILE", str(scratch / "rework.env"))
        env.setdefault("REWORK_TUI_SETTINGS", str(scratch / "tui.json"))
        env.setdefault("REWORK_LOG_DIR", str(scratch / "logs"))
        env.setdefault("REWORK_RUN_DIR", str(scratch / "run"))

    if workdir is not None and not (workdir / "pyproject.toml").exists():
        return env
    venvs = {v for v in (env.pop("VIRTUAL_ENV", None),) if v}
    if sys.prefix != sys.base_prefix:
        venvs.add(sys.prefix)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV_PROMPT"):
        env.pop(key, None)
    roots = [os.path.realpath(v) for v in venvs]
    keep = [
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p and not any(os.path.realpath(p).startswith(r + os.sep) for r in roots)
    ]
    env["PATH"] = os.pathsep.join(keep)
    return env


def _command_registry() -> Path:
    import os

    from rework.config import DEFAULT_STATE_DIR

    root = Path(os.environ.get("REWORK_RUN_DIR", str(DEFAULT_STATE_DIR / "run"))) / "commands"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _register_command(pgid: int, command: str) -> Path:
    """Record a running command's process group and the daemon that owns it,
    so a later daemon can reap it if this one dies mid-command."""
    import json
    import os

    mark = _command_registry() / f"{pgid}.json"
    mark.write_text(json.dumps({"pgid": pgid, "owner": os.getpid(), "command": command}))
    return mark


_reaped = False


def reap_orphaned_commands(force: bool = False) -> list[int]:
    """Kill command trees left by a daemon that died mid-command (found live: a
    restart left two `uv run pytest` trees running). Only groups whose owner
    daemon is gone, and only if the group still runs that exact shell command,
    so a reused pid is never killed. Runs once per process."""
    import json
    import os
    import signal
    import subprocess

    global _reaped
    if _reaped and not force:
        return []
    _reaped = True
    killed: list[int] = []
    try:
        marks = list(_command_registry().glob("*.json"))
    except OSError:
        return killed
    for mark in marks:
        try:
            info = json.loads(mark.read_text())
            pgid, owner, command = int(info["pgid"]), int(info["owner"]), str(info["command"])
        except (OSError, ValueError, KeyError):
            mark.unlink(missing_ok=True)
            continue
        if owner == os.getpid() or _alive(owner):
            continue  # its daemon (or this one) is still running it
        try:
            rows = subprocess.run(
                ["ps", "-ax", "-o", "pgid=,command="], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return killed
        leader = f"-c {command}"
        if any(r.split(None, 1)[0] == str(pgid) and leader in r for r in rows if r.strip()):
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed.append(pgid)
            except (ProcessLookupError, PermissionError):
                pass
        mark.unlink(missing_ok=True)
    return killed


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _kill_tree(proc: Any) -> bytes:
    """TERM, then KILL, the command's whole process group; read what is left
    without ever waiting forever."""
    import os
    import signal

    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            rest, _ = await asyncio.wait_for(proc.communicate(), timeout=grace)
            return rest or b""
        except TimeoutError:
            continue
    return b""


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
