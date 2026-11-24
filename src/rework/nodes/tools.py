"""ToolExecutor: how role tools reach the world.

LocalExecutor jails every path to the run's workdir (the v1 isolation line,
docs/design.md#isolation) and runs shell commands as subprocesses inside that
jail. The Docker milestone swaps the executor behind this same protocol; roles
never change.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable
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


# An executor factory takes the workdir a run will use and returns the executor
# for it. It is the one place a deployment chooses local vs container (ADR 0014,
# and ADR 0010 R1's requirement), and roles never see the choice.
ExecutorFactory = Callable[[Path], "ToolExecutor"]


def local_executor_factory(mission_id: str | None = None) -> ExecutorFactory:
    def make(workdir: Path) -> ToolExecutor:
        return LocalExecutor(workdir, mission_id=mission_id)

    return make


class LazyExecutor:
    """Executor whose jail target is known only at prepare time. The coder's
    worktree is created by its prepare node, but the loop node is wired at
    build_graph time; this wrapper builds the real executor once attached,
    through the injected factory (so a container swap needs no role change)."""

    def __init__(
        self,
        fallback_specs: list[ToolSpec] | None = None,
        mission_id: str | None = None,
        factory: ExecutorFactory | None = None,
    ) -> None:
        self._inner: ToolExecutor | None = None
        self._fallback = fallback_specs or list(BUILTIN_TOOLS)
        self._mission_id = mission_id
        self._factory = factory or local_executor_factory(mission_id)

    def attach(self, workdir: Path) -> None:
        self._inner = self._factory(workdir)

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

# The tools that only READ. They run on the host for every executor, including
# the container one: they cannot change anything, they work before a container
# has started, and the read is the same bytes either way. One jail, defined
# once, so a bug in it is fixed once (ADR 0014).
FILE_TOOLS = frozenset({"read_file", "list_dir"})


def jail_path(workdir: Path, path_str: str) -> Path:
    candidate = (workdir / path_str).resolve()
    if candidate != workdir and workdir not in candidate.parents:
        raise ValueError(f"path escapes workspace: {path_str}")
    return candidate


async def read_side(name: str, workdir: Path, args: dict[str, Any]) -> str:
    """`read_file` and `list_dir`, jailed to `workdir`."""
    if name == "read_file":
        path = jail_path(workdir, str(args["path"]))
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        lines = text.splitlines()
        head = lines[:2000]
        note = f"\n... ({len(lines) - 2000} more lines)" if len(lines) > 2000 else ""
        return "\n".join(head) + note
    path = jail_path(workdir, str(args.get("path", ".")))
    entries = await asyncio.to_thread(sorted, path.iterdir())
    rendered = []
    for entry in entries[:500]:
        kind = "d" if entry.is_dir() else "f"
        size = entry.stat().st_size if entry.is_file() else ""
        rendered.append(f"{kind} {entry.name} {size}".rstrip())
    return "\n".join(rendered) or "(empty)"


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

    def __init__(
        self,
        workdir: Path,
        extra_specs: list[ToolSpec] | None = None,
        mission_id: str | None = None,
    ) -> None:
        self._workdir = workdir.resolve()
        self._extra = extra_specs or []
        self._mission_id = mission_id  # names the scratch dir; gc reclaims it
        reap_orphaned_commands()

    def _jail(self, path_str: str) -> Path:
        return jail_path(self._workdir, path_str)

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
            if name in FILE_TOOLS:
                return await read_side(name, self._workdir, args)
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
                # rework must not overload its host (docs/ops.md, 2026-11-13):
                # deprioritize the command and cap its tree's memory so one
                # runaway pytest/mypy is killed alone instead of taking the box
                # down — via a systemd user/system scope when one is usable,
                # else a hard `ulimit -v` inside the wrapper shell. The wrappers
                # are plain argv (the shell command is one argument), so the
                # process group (start_new_session) and the timeout kill below
                # still cover the whole tree.
                from rework.nodes.hostlimits import command_prefix, configured_memory_max
                from rework.nodes.hostlimits import report_once as _report_wrapper

                memory_max = configured_memory_max()
                # the command's OWN environment: computed once and used both for
                # the prefix choice and the spawn, so the user-scope probe sees
                # exactly what the command will (ADR 0009 + CI's missing bus)
                proc_env = role_env(self._workdir, self._mission_id)
                prefix = command_prefix(command, memory_max=memory_max, env=proc_env)
                _report_wrapper(prefix, memory_max)
                proc = await asyncio.create_subprocess_exec(
                    *prefix,
                    cwd=str(self._workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    env=proc_env,
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


# Per-workdir scratch dirs, keyed by the resolved workdir. role_env runs once
# per jailed command, and every command of a run must see the SAME state dirs,
# so the dir is derived once and cached.
_scratch_dirs: dict[tuple[Path, str | None], Path] = {}


def _is_git_checkout(path: Path) -> bool:
    """True when `path` is the root of a git checkout: `.git` is a directory in
    a main checkout and a file (`gitdir: ...`) in a linked worktree. A missing
    or unreadable path is not a checkout."""
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def scratch_dir(workdir: Path, mission_id: str | None = None) -> Path:
    """The per-run scratch dir for rework state (targets, env file, TUI
    settings, logs, run registry), kept OUTSIDE any git checkout.

    Found live (follow-up to PR #22): this used to be `<workdir>/.rework-scratch`
    and a coder's workdir IS its git worktree, so the handoff's `git add -A`
    staged everything the role's own rework invocation wrote there into the PR.
    A checkout workdir is therefore treated as the coder's `<run_dir>/worktree`:
    the scratch dir goes NEXT TO it, in the run dir (`<run_dir>/.rework-scratch`,
    where loop.json lives), which no checkout covers. Any other workdir gets a
    per-run temporary directory.

    Stable for the same workdir: role_env is called once per jailed command, so
    repeated calls must hand back the same dir, not a fresh temp dir each time.

    `mission_id` goes INTO the temp dir's name (`rework-scratch-<id>-<random>`):
    found live (follow-up to PR #24), ~140 of these piled up in /tmp with no way
    to tell whose they were, so `rework gc` could not reclaim them. Legacy dirs
    without an id are unattributable and left alone by gc."""
    root = Path(workdir).resolve()
    key = (root, mission_id)
    cached = _scratch_dirs.get(key)
    if cached is not None:
        return cached
    if _is_git_checkout(root):
        scratch = root.parent / ".rework-scratch"
    else:
        tag = f"{mission_id}-" if mission_id else ""
        scratch = Path(tempfile.mkdtemp(prefix=f"rework-scratch-{tag}"))
    scratch.mkdir(parents=True, exist_ok=True)
    _scratch_dirs[key] = scratch
    return scratch


# -- the role environment allowlist (ADR 0009) -------------------------------
#
# A role command gets a BUILT environment, never the daemon's. Everything not
# named here or in ~/.rework/role_env.toml is dropped, including every
# REWORK_*_KEY and REWORK_DB. Deliberately absent: GIT_SSH_COMMAND (it picks
# which key git pushes as), GIT_CONFIG_* (can inject an auth header),
# SSH_AUTH_SOCK, proxy variables, PYTHONPATH/PYTHONHOME/VIRTUAL_ENV (the
# daemon's venv must not leak into a role, for every repo shape, not just ones
# with a pyproject.toml).

BASELINE_EXACT: frozenset[str] = frozenset(
    {
        # process basics
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TZ",
        # locale and terminal
        "LANG",
        "LANGUAGE",
        "TERM",
        # XDG directories (paths only)
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        # the session bus: `hostlimits.command_prefix` may run a role command
        # under `systemd-run --user`, which needs a reachable user bus. Without
        # these two it fails with "Failed to connect to user scope bus", which
        # on CI (no XDG_RUNTIME_DIR/DBUS in the job env) broke EVERY role
        # command. They are runtime paths, not secrets.
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        # uv and python behaviour (paths and switches, never URLs)
        "UV_CACHE_DIR",
        "UV_PYTHON",
        "UV_PYTHON_INSTALL_DIR",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    }
)

BASELINE_PREFIXES: tuple[str, ...] = ("LC_", "GIT_AUTHOR_", "GIT_COMMITTER_")

# The owner's additions. REWORK_ROLE_ENV_FILE is a test seam; the real path is
# ~/.rework/role_env.toml, the same directory targets.toml and rework.env live
# in.
MIN_PREFIX = 3  # a shorter prefix ("*", "A") would widen the list arbitrarily

_allow_cache: dict[tuple[str, float], tuple[frozenset[str], tuple[str, ...]]] = {}


def role_env_file() -> Path:
    import os

    from rework.config import DEFAULT_STATE_DIR

    return Path(
        os.environ.get("REWORK_ROLE_ENV_FILE", str(DEFAULT_STATE_DIR / "role_env.toml"))
    ).expanduser()


def _parse_allow(data: Any) -> tuple[frozenset[str], tuple[str, ...]]:
    """Exact names and `prefix*` entries from the file's `allow` list.

    Anything suspect rejects the WHOLE file (baseline only): a bad TOML, a
    non-list, a non-string entry, a bare `*`, an empty name, or a prefix
    shorter than MIN_PREFIX. Every failure moves toward fewer variables, never
    more, and there is deliberately no `inherit_all` switch."""
    exact: set[str] = set()
    prefixes: list[str] = []
    raw = data.get("allow", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raise ValueError("allow must be a list of strings")
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("allow entries must be non-empty strings")
        name = entry.strip()
        if name == "*":
            raise ValueError("a bare * would widen the allowlist to everything")
        if name.endswith("*"):
            prefix = name[:-1]
            if len(prefix) < MIN_PREFIX:
                raise ValueError(f"prefix {name!r} is shorter than {MIN_PREFIX} characters")
            prefixes.append(prefix)
        else:
            exact.add(name)
    return frozenset(exact), tuple(sorted(prefixes))


def load_allowlist(path: Path | None = None) -> tuple[frozenset[str], tuple[str, ...]]:
    """(extra exact names, extra prefixes) from the owner's file, cached on the
    file's mtime so an edit applies to the next command with no restart. A
    missing file is baseline only and is not an error."""
    import logging
    import tomllib

    target = Path(path) if path is not None else role_env_file()
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return frozenset(), ()
    key = (str(target), mtime)
    cached = _allow_cache.get(key)
    if cached is not None:
        return cached
    try:
        parsed = _parse_allow(tomllib.loads(target.read_text()))
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        # the whole file, not the bad entry: fewer variables, never more
        logging.getLogger("rework.roelenv").error(
            "ignoring %s as a whole (baseline only): %s", target, exc
        )
        parsed = (frozenset(), ())
    _allow_cache[key] = parsed
    return parsed


def allowed_names(env: dict[str, str] | None = None) -> set[str]:
    """The effective allowlist, plus which of `env`'s names it would keep. Used
    by `rework role-env show` and by the daemon's once-per-start log."""
    import os

    extra_exact, extra_prefixes = load_allowlist()
    exact = BASELINE_EXACT | extra_exact
    prefixes = BASELINE_PREFIXES + extra_prefixes
    source = os.environ if env is None else env
    return {
        name
        for name in source
        if name in exact or any(name.startswith(prefix) for prefix in prefixes)
    }


def role_env(workdir: Path | None = None, mission_id: str | None = None) -> dict[str, str]:
    """The environment a role command runs with: an allowlist, built from {}.

    ADR 0009. The daemon's environment is never inherited, so a role cannot read
    `REWORK_*_KEY`, `REWORK_DB`, `SSH_AUTH_SOCK`, or a proxy credential by
    running `env`. The owner adds names in ~/.rework/role_env.toml.

    The venv stripping stays (found live: the daemon runs from the main
    checkout's venv, so `pytest` in a worktree imported the main source and
    audits tested the wrong code). It now reads VIRTUAL_ENV and sys.prefix from
    the daemon to know which PATH entries to remove, but never copies
    VIRTUAL_ENV itself, and applies to every repo shape.

    Also redirects rework's own state paths (~/.rework targets, env file, TUI
    settings, logs, run registry) into a per-run scratch dir OUTSIDE the role's
    git worktree (see scratch_dir), so a role cannot touch or redirect the real
    state and nothing it writes can be staged by the handoff. These are plain
    assignments: with nothing inherited there is nothing to defer to."""
    import os
    import sys

    extra_exact, extra_prefixes = load_allowlist()
    exact = BASELINE_EXACT | extra_exact
    prefixes = BASELINE_PREFIXES + extra_prefixes
    env = {
        name: value
        for name, value in os.environ.items()
        if name in exact or any(name.startswith(prefix) for prefix in prefixes)
    }

    if workdir is not None:
        scratch = scratch_dir(workdir, mission_id)
        env["REWORK_TARGETS"] = str(scratch / "targets.toml")
        env["REWORK_ENV_FILE"] = str(scratch / "rework.env")
        env["REWORK_TUI_SETTINGS"] = str(scratch / "tui.json")
        env["REWORK_LOG_DIR"] = str(scratch / "logs")
        env["REWORK_RUN_DIR"] = str(scratch / "run")

    # The daemon's venv must not be importable from a worktree that has its own
    # Python project: found live, `pytest` in a rework worktree imported the
    # main checkout's source and audits tested the wrong code. That pruning is
    # scoped to a repo with its own pyproject.toml, exactly as before ADR 0009;
    # a toy repo (no pyproject) keeps the daemon's tools through PATH, and the
    # allowlist already guarantees VIRTUAL_ENV itself never arrives.
    own_python = workdir is None or (workdir / "pyproject.toml").exists()
    if own_python:
        venvs = {v for v in (os.environ.get("VIRTUAL_ENV"),) if v}
        if sys.prefix != sys.base_prefix:
            venvs.add(sys.prefix)
        roots = [os.path.realpath(v) for v in venvs]
        keep = [
            p
            for p in env.get("PATH", "").split(os.pathsep)
            if p and not any(os.path.realpath(p).startswith(r + os.sep) for r in roots)
        ]
        env["PATH"] = os.pathsep.join(keep)

    # The daemon's git identity is an owner setting (Config.git_author, from
    # REWORK_GIT_AUTHOR_*), not a stray variable, and a role command that runs
    # `git commit` without it fails on a machine with no global user config.
    # Copy it, and only it, from the daemon's own settings.
    from rework.config import Config

    author = Config.from_env().git_author
    env.setdefault("GIT_AUTHOR_NAME", author[0])
    env.setdefault("GIT_AUTHOR_EMAIL", author[1])
    env.setdefault("GIT_COMMITTER_NAME", author[0])
    env.setdefault("GIT_COMMITTER_EMAIL", author[1])
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
