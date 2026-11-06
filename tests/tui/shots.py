"""Dev helper: seed a tmp DB, drive the app, save PNG screenshots.

    uv run python tests/tui/shots.py [out_dir] [script]

Not a test (no test_ prefix). Uses rsvg-convert when available."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main(out: Path, steps: list[str], size: tuple[int, int]) -> None:
    tmp = Path(tempfile.mkdtemp())
    os.environ["REWORK_DB"] = str(tmp / "s.sqlite3")
    os.environ["REWORK_LIBRARY_DIR"] = str(tmp / "lib")
    os.environ["REWORK_TUI_SETTINGS"] = str(tmp / "tui.json")
    os.environ["REWORK_MODELS"] = os.environ.get(
        "SHOTS_MODELS",
        '[{"id":"flash","provider":"fake","model":"f","tier":1},'
        '{"id":"pro","provider":"fake","model":"p","tier":2}]',
    )
    from seed import seed, seed_library

    await seed(os.environ["REWORK_DB"])
    await seed_library(os.environ["REWORK_DB"], os.environ["REWORK_LIBRARY_DIR"])
    from rework.tui.app import ReworkApp

    app = ReworkApp(poll_seconds=0.5)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.6)
        n = 0
        for step in steps:
            if step.startswith("shot:"):
                name = step[5:]
                svg = out / f"{n:02d}-{name}.svg"
                app.save_screenshot(str(svg.name), path=str(out))
                chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                if Path(chrome).exists():  # noqa: ASYNC240 - dev helper
                    subprocess.run(  # noqa: ASYNC221 - dev helper
                        [
                            chrome,
                            "--headless=new",
                            "--disable-gpu",
                            "--hide-scrollbars",
                            f"--window-size={size[0] * 13},{size[1] * 27}",
                            f"--screenshot={svg.with_suffix('.png')}",
                            f"file://{svg}",
                        ],
                        check=False,
                        capture_output=True,
                    )
                n += 1
            elif step.startswith("key:"):
                await pilot.press(*step[4:].split(","))
            elif step.startswith("click:"):
                await pilot.click(step[6:])
            elif step.startswith("wait:"):
                await pilot.pause(float(step[5:]))
            elif step.startswith("type:"):
                for ch in step[5:]:
                    await pilot.press(ch if ch != " " else "space")
            elif step.startswith("do:"):
                exec(step[3:], {"app": app})
            await pilot.pause(0.3)
        if app.swallowed:
            print("SWALLOWED:", [repr(e) for e in app.swallowed])


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rwshots")
    out.mkdir(parents=True, exist_ok=True)
    script = sys.argv[2] if len(sys.argv) > 2 else "shot:work"
    w, h = (int(x) for x in os.environ.get("SHOTS_SIZE", "140x40").split("x"))
    asyncio.run(main(out, script.split("|"), (w, h)))
