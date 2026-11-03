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


@app.command()
def new(objective: str, repo: str | None = None) -> None:
    """Create a mission; the daemon picks it up."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.state.records import (
            EventRecord,
            EventType,
            Mission,
            new_id,
        )

        store = await open_store_from_config(Config.from_env())
        try:
            bus = InProcessEventBus(store)
            mission = await store.create_mission(
                Mission(id=new_id("m"), objective=objective, repo_path=repo)
            )
            await bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.MISSION_CREATED,
                    payload={"objective": objective},
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
