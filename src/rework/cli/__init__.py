"""rework CLI: serve/new/ls/attach/gates/approve. The CLI is a client: every
command opens its own store connection, so it works from any machine sharing
the database (that is the cross-device design, docs/design.md)."""

from __future__ import annotations

import asyncio
import time

import typer

from rework import __version__

app = typer.Typer(
    name="rework",
    help=(
        "Portable multi-agent workflow engine: an orchestrator supervising role "
        "workflows, with a compounding cross-harness library."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def root() -> None:
    """rework: see `rework --help` for commands."""


@app.command()
def version() -> None:
    """Print the rework version."""
    typer.echo(f"rework {__version__}")


PLAN_FIX: dict = {
    "steps": [
        {
            "step": "investigate",
            "role": "investigator",
            "model": "flash",
            "context": {
                "brief": "$OBJECTIVE",
                "depth": "quick",
                "requested_sections": [
                    "summary",
                    "findings",
                    "suggested_fix",
                    "proposed_tests",
                    "blockers",
                ],
            },
        },
        {
            "step": "code",
            "role": "coder",
            "model": "flash",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {"investigation": {"step": "investigate"}},
        },
        {
            "step": "audit",
            "role": "auditor",
            "model": "pro",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {
                "diff": {"step": "code", "field": "diff"},
                "worktree_path": {"step": "code", "field": "worktree"},
                "investigation": {"step": "investigate"},
            },
            "on_request_changes": {"revise": "code", "max_rounds": 2},
        },
    ],
    "gates": ["ship"],
}


def _prepare_repo(repo_arg: str, mission_id: str) -> str:
    """A git repo is used in place; a plain directory is copied into the
    state workspace and git-initialized, so missions stay self-contained
    and the example repos are never mutated."""
    import shutil as _shutil
    import subprocess as _sp
    from pathlib import Path as _Path

    src = _Path(repo_arg).expanduser().resolve()
    if not src.is_dir():
        raise typer.BadParameter(f"repo not found: {src}")
    if (src / ".git").exists():
        return str(src)
    from rework.config import DEFAULT_STATE_DIR

    dest = DEFAULT_STATE_DIR / "work" / mission_id / "repo"
    if dest.exists():
        _shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(
        src,
        dest,
        ignore=_shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules"),
    )

    def _git(*args: str) -> None:
        _sp.run(["git", *args], cwd=dest, check=True, capture_output=True)

    _git("init", "-b", "main")
    _git("add", "-A")
    _git("-c", "user.name=rework", "-c", "user.email=rework@rework.local", "commit", "-m", "init")
    return str(dest)


@app.command()
def new(
    objective: str,
    repo: str | None = None,
    plan: str = typer.Option("echo", help="echo | fix"),
) -> None:
    """Create a mission; the daemon picks it up. `--plan fix` runs the
    investigate -> code -> audit pipeline with a ship gate."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.state.records import (
            EventRecord,
            EventType,
            Mission,
            new_id,
        )

        mission_id = new_id("m")
        repo_path = _prepare_repo(repo, mission_id) if repo else None
        store = await open_store_from_config(Config.from_env())
        try:
            bus = InProcessEventBus(store)
            plan_spec: dict | None = None
            if plan == "fix":
                if not repo_path:
                    raise typer.BadParameter("--plan fix requires --repo")
                import json as _json

                plan_spec = _json.loads(_json.dumps(PLAN_FIX).replace("$OBJECTIVE", objective))
            mission = await store.create_mission(
                Mission(
                    id=mission_id,
                    objective=objective,
                    repo_path=repo_path,
                    plan=plan_spec,
                )
            )
            await bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.MISSION_CREATED,
                    payload={"objective": objective, "plan": plan},
                )
            )
            return mission.id
        finally:
            await store.aclose()

    typer.echo(asyncio.run(go()))


@app.command()
def ls(limit: int = 20) -> None:
    """List missions, newest first."""

    async def go() -> list[tuple[str, str, str]]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            missions = await store.list_missions(limit=limit)
            return [(m.id, m.status.value, m.objective) for m in missions]
        finally:
            await store.aclose()

    for mid, status, objective in asyncio.run(go()):
        typer.echo(f"{mid}  {status:<14}  {objective}")


@app.command()
def gates(mission_id: str | None = None) -> None:
    """List open approval gates (optionally for one mission)."""

    async def go() -> list[tuple[str, str, str]]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            open_gates = await store.list_open_gates(mission_id)
            return [(g.id, g.kind.value, g.question) for g in open_gates]
        finally:
            await store.aclose()

    rows = asyncio.run(go())
    if not rows:
        typer.echo("no open gates")
        return
    for gid, kind, question in rows:
        typer.echo(f"{gid}  [{kind}]  {question}")


@app.command()
def approve(
    gate_id: str,
    note: str | None = None,
    reject: bool = typer.Option(False, help="reject instead of approve"),
) -> None:
    """Decide an open gate. Works from any machine sharing the database."""

    async def go() -> tuple[bool, str]:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.gates import GateManager

        store = await open_store_from_config(Config.from_env())
        try:
            gates = GateManager(store, InProcessEventBus(store))
            gate = await gates.decide(gate_id, approved=not reject, note=note)
            if gate is None:
                return False, "gate not found"
            return True, gate.status.value
        finally:
            await store.aclose()

    ok, status = asyncio.run(go())
    if not ok:
        typer.secho(status, fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"gate {gate_id}: {status}")


@app.command()
def attach(mission_id: str, follow: bool = True) -> None:
    """Stream a mission's events (replay from the beginning, then follow)."""

    async def go() -> None:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            cursor = 0
            last_status = ""
            while True:
                mission = await store.get_mission(mission_id)
                if mission is None:
                    typer.secho(f"no such mission: {mission_id}", fg=typer.colors.RED)
                    return
                if mission.status.value != last_status:
                    typer.secho(f"== {mission.status.value.upper()} ==", fg=typer.colors.CYAN)
                    last_status = mission.status.value
                for event in await store.events_since(cursor, mission_id=mission_id):
                    assert event.seq is not None
                    cursor = event.seq
                    stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
                    who = event.run_id.rsplit(":", 2)[0].split(":")[-1] if event.run_id else ""
                    note = event.payload.get("note") or event.payload.get("question") or ""
                    typer.echo(f"{stamp}  {event.type.value:<16} {who:<12} {note}".rstrip())
                if not follow or mission.status.value in {"done", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.5)
        finally:
            await store.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        typer.echo("\ndetached")


@app.command()
def serve() -> None:
    """Run the headless daemon (one per database; leader-locked)."""
    import logging
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from rework.config import Config, load_catalog, open_store_from_config

    config = Config.from_env()
    workroot = Path(config.db).expanduser().parent / "work"

    async def go() -> None:
        from rework.kernel.events.bus import InProcessEventBus
        from rework.orchestrator.daemon import Daemon, DaemonLocked, sqlite_checkpointer_factory
        from rework.roles.registry import autodiscover

        store = await open_store_from_config(config)
        bus = InProcessEventBus(store)
        daemon = Daemon(
            store=store,
            bus=bus,
            registry=autodiscover(),
            catalog=load_catalog(),
            make_checkpointer=sqlite_checkpointer_factory(config.checkpoints_db),
            workroot=workroot,
            config=config,
        )
        try:
            await daemon.run_forever()
        except DaemonLocked as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc

    asyncio.run(go())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
