from pathlib import Path

p = Path("src/rework/cli/__init__.py")
s = p.read_text()

anchor = '''@app.command()
def complaints(
'''
new_cmd = '''@app.command("retry-pr")
def retry_pr_cmd(mission_id: str) -> None:
    """Retry the PR delivery of a mission that finished but whose push or
    `gh pr create` failed (a transient remote hiccup must not cost the work).

    Pushes the recorded branch and opens the missing PR with the persisted
    title/body; on success the pending marker is cleared, on failure the
    remote's FULL message is printed and nothing else is touched.
    """
    from rework.orchestrator.delivery import retry_pr

    async def go() -> tuple[bool, str]:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus

        store = await open_store_from_config(Config.from_env())
        try:
            return await retry_pr(store, InProcessEventBus(store), mission_id)
        finally:
            await store.aclose()

    ok, detail = asyncio.run(go())
    if ok:
        typer.echo(f"{mission_id}: PR opened: {detail}")
        return
    # the whole error, every line: the useful one is the LAST (git prints the
    # rejection reason after the remote banner and the forge's hints)
    typer.echo(f"{mission_id}: PR still not delivered:\\n{detail}", err=True)
    raise typer.Exit(1)


@app.command()
def complaints(
'''
assert anchor in s
s = s.replace(anchor, new_cmd, 1)
p.write_text(s)
print("ok")
