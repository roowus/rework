"""Live TUI end-to-end driver: a REAL daemon on a temp DB, one or two REAL TUI
apps (Textual pilot) against it, scripted steps, screenshots, and a findings log.

    uv run python scripts/e2e/tui_live.py <scenario> [out_dir]

Not a unit test: it starts a subprocess daemon, uses the fake model catalog, and
writes PNG/SVG shots plus findings.json to out_dir. Each scenario is a coroutine
below. Everything runs on throwaway paths and a free port; nothing touches
~/.rework or any shared database.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "tui"))

FAKE_MODELS = (
    '[{"id":"flash","provider":"fake","model":"f","tier":1},'
    '{"id":"pro","provider":"fake","model":"p","tier":2}]'
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class World:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.tmp = Path(tempfile.mkdtemp(prefix="rwtui-"))
        self.port = _free_port()
        self.findings: list[dict[str, Any]] = []
        self.daemon: subprocess.Popen[bytes] | None = None
        self.env = {
            "REWORK_DB": str(self.tmp / "state.sqlite3"),
            "REWORK_CHECKPOINTS_DB": str(self.tmp / "cp.sqlite3"),
            "REWORK_LIBRARY_DIR": str(self.tmp / "lib"),
            "REWORK_MODELS": FAKE_MODELS,
            "REWORK_HTTP_PORT": str(self.port),
            "REWORK_DAEMON_URL": f"http://127.0.0.1:{self.port}",
            "REWORK_LOCK_STALE_S": "6",
        }
        os.environ.update(self.env)

    def script(self, routes: dict[str, list[dict[str, Any]]]) -> None:
        """The daemon's scripted model turns (REWORK_FAKE_SCRIPT), per route."""
        path = self.tmp / "daemon-script.json"
        path.write_text(json.dumps(routes))
        self.env["REWORK_FAKE_SCRIPT"] = str(path)

    def start_daemon(self) -> None:
        log = open(self.tmp / "daemon.log", "wb")
        self.daemon = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "rework"), "serve"],
            env={**os.environ, **self.env},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"daemon did not come up; see {self.tmp / 'daemon.log'}")

    def stop(self) -> None:
        if self.daemon is not None:
            self.daemon.terminate()
            try:
                self.daemon.wait(10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()

    def finding(self, area: str, what: str, **detail: Any) -> None:
        print(f"FINDING [{area}] {what} {detail if detail else ''}", flush=True)
        self.findings.append({"area": area, "what": what, **detail})

    def write(self) -> None:
        (self.out / "findings.json").write_text(json.dumps(self.findings, indent=2))


async def shot(app: Any, out: Path, name: str) -> None:
    svg = out / f"{name}.svg"
    app.save_screenshot(svg.name, path=str(out))
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome).exists():  # noqa: ASYNC240 - dev driver, one stat
        size = app.size
        await asyncio.to_thread(
            subprocess.run,
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--window-size={size.width * 13},{size.height * 27}",
                f"--screenshot={svg.with_suffix('.png')}",
                f"file://{svg}",
            ],
            check=False,
            capture_output=True,
        )


async def until(pilot: Any, cond: Any, within_s: float = 15.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            if cond():
                return True
        except Exception:
            pass
        await pilot.pause(0.1)
    return False


SCENARIOS: dict[str, Any] = {}


def scenario(fn: Any) -> Any:
    SCENARIOS[fn.__name__] = fn
    return fn


async def main(name: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - dev driver
    world = World(out)
    try:
        await SCENARIOS[name](world)
    finally:
        world.stop()
        world.write()
    print(f"{name}: {len(world.findings)} finding(s); shots + findings in {out}")
    return 1 if world.findings else 0


if __name__ == "__main__":
    # run through the IMPORTED module, so the scenarios register into the same
    # SCENARIOS dict main() reads (not this __main__ copy's)
    sys.path.insert(0, str(Path(__file__).parent))
    import scenarios  # noqa: F401  (registers SCENARIOS)
    import tui_live

    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/rwtui")
    sys.exit(asyncio.run(tui_live.main(sys.argv[1], out_dir)))
