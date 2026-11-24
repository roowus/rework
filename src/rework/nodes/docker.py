"""Sandboxed role commands: a container per run, behind the same executor seam.

`LocalExecutor` runs a role's `run_command` as the daemon's own user through a
shell that can `cd` anywhere (its own docstring says so). This module closes
that without teaching the roles anything: it satisfies the same two-method
`ToolExecutor` protocol, so a role's graph is unchanged (ADR 0014).

What runs where:

- `read_file` and `list_dir` stay on the HOST. They read the bind-mounted
  worktree, they cannot change anything, they work before a container exists,
  and the jail is exactly the one `LocalExecutor` uses (`tools.read_side`), so
  there is one implementation of it.
- `run_command` runs in the container.

The container is created lazily on the first command, named from the run id so a
restart recognizes its own, and removed by the run's closer (`RoleBuildCtx`
closers, called by `RunSupervisor`), or swept by `rework gc` if the daemon died
without running one. Docker is never required: `REWORK_EXECUTOR=docker` refuses
to start rather than silently degrading, and `auto` falls back to local.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from rework.kernel.llm.types import ToolSpec
from rework.nodes.tools import (
    BUILTIN_TOOLS,
    FILE_TOOLS,
    ToolExecutor,
    _kill_tree,
    read_side,
    role_env,
)

DEFAULT_IMAGE = "python:3.12-slim"
SANDBOX_LABEL = "rework.managed"
CONTAINER_PREFIX = "rework-"


class SandboxUnavailable(RuntimeError):
    """REWORK_EXECUTOR=docker but there is no usable docker.

    Deliberately fatal: an owner who asked for a sandbox must not silently get
    the unsandboxed path instead. `auto` is the mode that falls back.
    """


def docker_bin() -> str | None:
    return os.environ.get("REWORK_DOCKER_BIN") or shutil.which("docker")


def executor_mode() -> str:
    """`local` (default), `docker`, or `auto` (docker when it is installed)."""
    mode = (os.environ.get("REWORK_EXECUTOR") or "local").strip().lower()
    return mode if mode in ("local", "docker", "auto") else "local"


class DockerExecutor:
    """`run_command` in a per-run container; the rest on the host."""

    def __init__(
        self,
        workdir: Path,
        *,
        mission_id: str | None = None,
        run_id: str | None = None,
        image: str | None = None,
        docker: str | None = None,
        network: str = "none",
        memory: str | None = None,
        cpus: str | None = None,
    ) -> None:
        self._workdir = Path(workdir).resolve()
        self._mission_id = mission_id
        self._run_id = run_id or uuid.uuid4().hex[:12]
        self._image = image or os.environ.get("REWORK_SANDBOX_IMAGE") or DEFAULT_IMAGE
        self._docker = docker or docker_bin()
        self._network = network
        self._memory = memory or _memory_cap()
        self._cpus = cpus or os.environ.get("REWORK_SANDBOX_CPUS") or ""
        self._container: str | None = None

    # -- protocol ----------------------------------------------------------
    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        return [s for s in BUILTIN_TOOLS if allowed is None or s.name in allowed]

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name in FILE_TOOLS:
                return await read_side(name, self._workdir, args)
            if name == "run_command":
                return await self._run_in_container(
                    str(args["command"]), float(args.get("timeout_seconds", 120))
                )
            raise KeyError(f"unknown tool {name!r}")
        except KeyError as exc:
            return f"error: missing argument {exc}"
        except (ValueError, OSError) as exc:
            return f"error: {exc}"

    async def aclose(self) -> None:
        """Remove this run's container, if one was started. Never raises: a
        failed teardown must not fail a finished run."""
        name, self._container = self._container, None
        if not name or not self._docker:
            return
        try:
            await self._docker_exec("rm", "-f", name, limit_s=30)
        except Exception:
            pass

    # -- container lifecycle -----------------------------------------------
    @property
    def container_name(self) -> str:
        """Deterministic, so a restarted daemon recognizes its own container
        rather than starting a second one."""
        tag = self._run_id.replace(":", "-").replace("/", "-")[:60]
        return f"{CONTAINER_PREFIX}{tag}"

    async def _ensure_container(self) -> str:
        if self._container is not None:
            return self._container
        if not self._docker:
            raise SandboxUnavailable(
                "REWORK_EXECUTOR=docker but no docker binary was found; "
                "install docker, set REWORK_DOCKER_BIN, or use REWORK_EXECUTOR=local"
            )
        name = self.container_name
        # reuse a container left by an earlier attempt of this same run
        code, _ = await self._docker_exec(
            "inspect", "--format", "{{.State.Running}}", name, limit_s=30
        )
        if code == 0:
            self._container = name
            return name

        argv = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            SANDBOX_LABEL,
            "--label",
            f"rework.mission={self._mission_id or ''}",
            "--label",
            f"rework.run={self._run_id}",
            "--init",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            "--network",
            self._network,
            "--workdir",
            str(self._workdir),
            "--volume",
            f"{self._workdir}:{self._workdir}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
        ]
        if self._memory:
            argv += ["--memory", self._memory]
        if self._cpus:
            argv += ["--cpus", self._cpus]
        # only the allowlisted names, and names not values: the container reads
        # them from its own docker env, never from our argv or the daemon's env
        allowed = role_env(self._workdir, self._mission_id)
        for var in sorted(allowed):
            argv += ["-e", var]
        argv += [self._image, "sleep", "infinity"]

        code, out = await self._docker_exec(*argv, limit_s=120)
        if code != 0:
            raise SandboxUnavailable(f"could not start the sandbox container: {out.strip()[:400]}")
        self._container = name
        return name

    async def _run_in_container(self, command: str, limit_s: float) -> str:
        name = await self._ensure_container()
        # the container's own shell: no host command_prefix (that wrapper is a
        # HOST resource nanny; the container's cgroup limits replace it)
        proc = await asyncio.create_subprocess_exec(
            self._docker or "docker",
            "exec",
            name,
            "/bin/sh",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit_s)
        except TimeoutError:
            await _kill_tree(proc)
            # the exec client is dead, but the command may still be running in
            # the container: kill the container's processes too, and forget the
            # handle so the next call recreates it (the worktree is on the host,
            # so only container-layer state is lost)
            await self._docker_exec("kill", name, limit_s=30)
            self._container = None
            return f"(timeout after {limit_s:.0f}s; the sandboxed command was killed)"
        text = out.decode(errors="replace")
        if len(text) > 8000:
            text = text[:4000] + "\n... (truncated) ...\n" + text[-4000:]
        return f"(exit {proc.returncode})\n{text or '(no output)'}"

    async def _docker_exec(self, *args: str, limit_s: float = 60) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self._docker or "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_docker_client_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit_s)
        except TimeoutError:
            proc.kill()
            return -1, "docker command timed out"
        return proc.returncode or 0, out.decode(errors="replace")


def _docker_client_env() -> dict[str, str]:
    """What the docker CLIENT needs to reach its daemon. Deliberately NOT
    role_env: that is the environment INSIDE the container, and the client
    needs DOCKER_HOST, or it cannot find the socket."""
    keep = {
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "XDG_RUNTIME_DIR",
    }
    return {k: v for k, v in os.environ.items() if k in keep}


def _memory_cap() -> str:
    from rework.nodes.hostlimits import configured_memory_max

    return configured_memory_max()


# -- selection ---------------------------------------------------------------


class AutoExecutor:
    """`REWORK_EXECUTOR=auto`: docker when it is usable, local otherwise."""

    def __init__(self, executor: ToolExecutor) -> None:
        self._inner = executor

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        return self._inner.specs(allowed)


def make_executor_factory(mode: str | None = None) -> Any:
    """The daemon's one executor choice, made once per process.

    `local` (default) and `auto`-without-docker return the local jail. `docker`
    and `auto`-with-docker return a per-run container executor, which the run's
    closer removes.
    """
    from rework.nodes.tools import local_executor_factory

    chosen = (mode or executor_mode()).strip().lower()
    has_docker = docker_bin() is not None
    if chosen == "docker" and not has_docker:
        raise SandboxUnavailable(
            "REWORK_EXECUTOR=docker but no docker binary was found; install docker, "
            "set REWORK_DOCKER_BIN, or use REWORK_EXECUTOR=local"
        )
    if chosen == "docker" or (chosen == "auto" and has_docker):

        def make(workdir: Path) -> ToolExecutor:
            return DockerExecutor(workdir)

        return make
    return local_executor_factory()
