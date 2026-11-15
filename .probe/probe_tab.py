import asyncio, os, sys, tempfile
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "tests" / "tui"))

tmp = Path(tempfile.mkdtemp())
os.environ["REWORK_DB"] = str(tmp / "t.sqlite3")
os.environ["REWORK_LIBRARY_DIR"] = str(tmp / "lib")
os.environ["REWORK_RESULTS_DIR"] = str(tmp / "results")
os.environ["REWORK_REPORTS_DIR"] = str(tmp / "reports")
os.environ["REWORK_TUI_SETTINGS"] = str(tmp / "tui.json")
os.environ["REWORK_NOTIFY"] = "toast,bell"
os.environ["REWORK_MODELS"] = '[{"id":"flash","provider":"fake","model":"f","tier":1}]'

from seed import seed
from rework.tui.app import ReworkApp
from textual.widgets import Input


async def main():
    await seed(os.environ["REWORK_DB"], history_days=20)
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("alt+2")
        await pilot.pause(1.5)
        for i in range(6):
            f = app.screen.focused
            print("focus", i, type(f).__name__, getattr(f, "id", None))
            await pilot.press("tab")
            await pilot.pause(0.15)
        for i in range(4):
            f = app.screen.focused
            print("shift-tab", i, type(f).__name__, getattr(f, "id", None))
            await pilot.press("shift+tab")
            await pilot.pause(0.15)
        # type into whatever is focused and see if it is the proj bar
        app.query_one("#proj-search", Input).focus()
        await pilot.pause(0.2)
        print("proj value before:", app.query_one("#proj-search", Input).value)


asyncio.run(main())
