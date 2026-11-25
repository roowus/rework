"""The container executor, tested without docker (ADR 0014).

A fake `docker` binary on PATH (the `REWORK_GH_BIN` pattern) records argv and
delegates `exec` to the host shell, so the real subprocess seam and the real
environment are exercised. The point of these tests is the CONTRACT: what is
mounted, what environment reaches the container, that reads never start one, and
that a failure refuses rather than silently degrading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.nodes.docker import DockerExecutor, SandboxUnavailable, make_executor_factory


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `docker` that records every invocation and runs `exec` for real."""
    state = tmp_path / "docker-state"
    state.mkdir()
    log = state / "calls.jsonl"
    script = tmp_path / "docker"
    script.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {log}
case "$1" in
  inspect) exit 1 ;;
  exec)
    shift 2   # exec <name>
    shift 2   # sh -c   (the rest is the command string)
    exec /bin/sh -c "$*"
    ;;
  run)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
    )
    script.chmod(0o755)
    monkeypatch.setenv("REWORK_DOCKER_BIN", str(script))
    return log


def _calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line]


async def test_reads_never_start_a_container(tmp_path: Path, fake_docker: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    ex = DockerExecutor(tmp_path, run_id="m:code:coder:1")

    assert "x = 1" in await ex.execute("read_file", {"path": "a.py"})
    assert "(empty)" not in await ex.execute("list_dir", {"path": "."})
    assert _calls(fake_docker) == [], "a read must not touch docker"


async def test_a_read_outside_the_worktree_is_refused(tmp_path: Path, fake_docker: Path) -> None:
    ex = DockerExecutor(tmp_path, run_id="r")
    out = await ex.execute("read_file", {"path": "../../etc/passwd"})
    assert out.startswith("error: path escapes workspace")


async def test_run_command_goes_through_docker(tmp_path: Path, fake_docker: Path) -> None:
    ex = DockerExecutor(tmp_path, run_id="m:code:coder:1")
    out = await ex.execute("run_command", {"command": "echo sandboxed"})

    assert "sandboxed" in out
    calls = _calls(fake_docker)
    assert any(c.startswith("run ") for c in calls), "a container must be created"
    assert any(c.startswith("exec ") for c in calls), "the command must run in it"


async def test_the_container_name_is_deterministic_and_per_run(tmp_path: Path) -> None:
    a = DockerExecutor(tmp_path, run_id="m:code:coder:1")
    b = DockerExecutor(tmp_path, run_id="m:code:coder:2")
    assert a.container_name != b.container_name
    assert a.container_name.startswith("rework-")
    assert ":" not in a.container_name and "/" not in a.container_name
    # the same run id is the same name, so a restart recognizes its container
    assert a.container_name == DockerExecutor(tmp_path, run_id="m:code:coder:1").container_name


async def test_the_container_mounts_only_the_worktree(tmp_path: Path, fake_docker: Path) -> None:
    ex = DockerExecutor(tmp_path, run_id="r1")
    await ex.execute("run_command", {"command": "true"})
    run_line = next(c for c in _calls(fake_docker) if c.startswith("run "))
    mounts = [part for part in run_line.split() if part == "--volume"]
    assert mounts, "the worktree must be mounted"
    assert str(tmp_path) in run_line
    for forbidden in ("/var/run/docker.sock", str(Path.home() / ".rework")):
        assert forbidden not in run_line, f"{forbidden} must never be mounted"


async def test_the_container_gets_allowlisted_names_not_values(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container's environment is EXPLICIT and minimal.

    Found live on the first real container: `-e NAME` alone forwards the
    DAEMON's value, so the sandbox inherited the host's HOME, its venv PATH and
    its locale. A sandbox must start clean; only an owner-granted variable is
    forwarded, and only by the value the daemon holds."""
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "fw-SECRET")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/host/venv/bin:/usr/bin")
    ex = DockerExecutor(tmp_path, run_id="r1")
    await ex.execute("run_command", {"command": "true"})

    run_line = next(c for c in _calls(fake_docker) if c.startswith("run "))
    assert "fw-SECRET" not in run_line, "no secret value may appear in argv"
    assert "HOME=/home/tester" not in run_line, "the host HOME must not leak in"
    assert "/host/venv/bin" not in run_line, "the daemon venv must not leak in"
    assert "HOME=/tmp" in run_line, "the container gets its own writable HOME"
    assert "--network none" in run_line
    assert "--cap-drop=ALL" in run_line
    assert "no-new-privileges" in run_line


async def test_an_owner_granted_variable_is_forwarded(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `role-env allow` IS honoured: that is the owner asking for it."""
    allow = tmp_path / "role_env.toml"
    allow.write_text('allow = ["MY_TOOL_TOKEN"]\n')
    monkeypatch.setenv("REWORK_ROLE_ENV_FILE", str(allow))
    monkeypatch.setenv("MY_TOOL_TOKEN", "t-123")
    import rework.nodes.tools as tools

    tools._allow_cache.clear()

    ex = DockerExecutor(tmp_path, run_id="r1")
    await ex.execute("run_command", {"command": "true"})

    run_line = next(c for c in _calls(fake_docker) if c.startswith("run "))
    assert "MY_TOOL_TOKEN=t-123" in run_line


async def test_a_timeout_kills_the_container_and_forgets_it(
    tmp_path: Path, fake_docker: Path
) -> None:
    ex = DockerExecutor(tmp_path, run_id="r1")
    out = await ex.execute("run_command", {"command": "sleep 30", "timeout_seconds": 1})

    assert "timeout" in out
    assert ex._container is None, "the next call must recreate the container"
    assert any(c.startswith("kill ") for c in _calls(fake_docker))


async def test_aclose_removes_the_container_and_never_raises(
    tmp_path: Path, fake_docker: Path
) -> None:
    ex = DockerExecutor(tmp_path, run_id="r1")
    await ex.execute("run_command", {"command": "true"})
    await ex.aclose()
    assert any(c.startswith("rm -f ") for c in _calls(fake_docker))
    await ex.aclose()  # second close is a no-op, not an error


def test_missing_docker_refuses_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner who asked for a sandbox must not silently get the bare shell."""
    monkeypatch.setenv("REWORK_EXECUTOR", "docker")
    monkeypatch.setenv("REWORK_DOCKER_BIN", str(tmp_path / "nope"))
    monkeypatch.setattr("rework.nodes.docker.docker_bin", lambda: None)
    with pytest.raises(SandboxUnavailable):
        make_executor_factory("docker")


def test_auto_falls_back_to_local_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rework.nodes.docker.docker_bin", lambda: None)
    factory = make_executor_factory("auto")
    from rework.nodes.tools import LocalExecutor

    assert isinstance(factory(Path("/tmp")), LocalExecutor)


def test_the_default_mode_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REWORK_EXECUTOR", raising=False)
    from rework.nodes.tools import LocalExecutor

    assert isinstance(make_executor_factory()(Path("/tmp")), LocalExecutor)


async def test_the_run_closer_removes_the_container(tmp_path: Path, fake_docker: Path) -> None:
    """`RoleBuildCtx.closers` is what the supervisor runs at end of run; the
    container must come off through it."""
    from rework.roles.base import RoleBuildCtx  # noqa: F401  (shape check below)

    ex = DockerExecutor(tmp_path, run_id="r1")
    await ex.execute("run_command", {"command": "true"})

    # the executor registers itself the way a role would
    from contextlib import suppress

    from rework.kernel.budget.ledger import Budget
    from rework.kernel.state.records import Mission, Run

    # a minimal ctx: the closers machinery is what matters here
    from rework.roles.base import RoleBuildCtx as Ctx

    ctx = Ctx.model_construct(
        run=Run(id="m:coder:1", mission_id="m", role="coder", step="code"),
        mission=Mission(id="m", objective="x"),
        budget=Budget(),
        workdir=tmp_path,
        closers=[ex.aclose],
    )
    with suppress(Exception):
        await ctx.aclose()
    assert any(c.startswith("rm -f ") for c in _calls(fake_docker))


def test_env_for_the_docker_client_is_not_the_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client needs DOCKER_HOST; the container needs neither it nor the
    daemon's keys."""
    from rework.nodes.docker import _docker_client_env

    monkeypatch.setenv("DOCKER_HOST", "unix:///run/docker.sock")
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "secret")
    env = _docker_client_env()
    assert env["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert "REWORK_FIREWORKS_KEY" not in env
