"""The TUI in a REAL pseudo-terminal, driven with real byte sequences.

Pilot tests simulate keys by name and could not catch two bugs that made the
owner's TUI unusable:
- a slow / unreachable database froze every key and click (the refresh ran on
  the App's message queue);
- alt+N never worked: Option+digit arrives as a character (¡ ™ £ ¢ ∞), and
  Textual drops printable-character bindings while a text input has focus.

This test spawns `rework` in a pty, emulates the terminal with pyte and reads
what is actually on screen. It runs against a database that HANGS (a socket
that accepts and never answers), the failure mode found live.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

pyte = pytest.importorskip("pyte")
if sys.platform == "win32":  # pragma: no cover
    pytest.skip("pty only", allow_module_level=True)

import fcntl  # noqa: E402
import pty  # noqa: E402
import termios  # noqa: E402

COLS, ROWS = 140, 40


def _black_hole() -> tuple[socket.socket, int]:
    """A TCP port that accepts connections and never says a word: psycopg
    connects, then waits on the server forever (like a stalled tailnet)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    return srv, srv.getsockname()[1]


class Term:
    def __init__(self, env: dict[str, str]) -> None:
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        code = "import sys; sys.argv=['rework']; from rework.cli import main; main()"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - child
            os.execvpe(sys.executable, [sys.executable, "-c", code], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                try:
                    self.stream.feed(os.read(self.fd, 65536))
                except OSError:
                    return

    def wait_for(self, text: str, seconds: float = 15.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            self.pump(0.3)
            if any(text in line for line in self.screen.display):
                return True
        return False

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def click(self, text: str, row: int) -> None:
        col = self.screen.display[row].index(text) + 2
        self.send(f"\x1b[<0;{col};{row + 1}M".encode())
        time.sleep(0.05)
        self.send(f"\x1b[<0;{col};{row + 1}m".encode())

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


@pytest.fixture
def hanging_db_env(tmp_path: Path) -> dict[str, str]:
    srv, port = _black_hole()
    env = dict(os.environ)
    env.update(
        TERM="xterm-256color",
        REWORK_DB=f"postgresql://u:p@127.0.0.1:{port}/rework",
        REWORK_LOG_DIR=str(tmp_path / "logs"),
        REWORK_TUI_SETTINGS=str(tmp_path / "tui.json"),
        REWORK_LIBRARY_DIR=str(tmp_path / "lib"),
        REWORK_MODELS='[{"id":"flash","provider":"fake","model":"f","tier":1}]',
    )
    yield env
    srv.close()


def test_keys_and_clicks_work_while_the_database_hangs(hanging_db_env: dict[str, str]) -> None:
    term = Term(hanging_db_env)
    try:
        assert term.wait_for("Talk to the orchestrator", 20), "the app rendered"
        term.send(b"hello")
        assert term.wait_for("hello", 5), "typing reaches the prompt"
        term.send("™".encode())  # what Option+2 sends on macOS
        assert term.wait_for("PROJECTS", 5), "Option+2 opens the Dashboard"
        term.send(b"\x1b2")  # ESC+2, what meta-mode terminals send
        assert term.wait_for("PROJECTS", 5)
        term.click("Gates", 1)
        assert term.wait_for("Decided", 5), "a click on the Gates tab switches to it"
        assert term.wait_for("connecting"), "status says it is still connecting"
    finally:
        term.close()
