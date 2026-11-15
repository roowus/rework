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


DEFAULT_CODE = "import sys; sys.argv=['rework']; from rework.cli import main; main()"


class Term:
    def __init__(self, env: dict[str, str], code: str = DEFAULT_CODE) -> None:
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
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

    def find(self, text: str) -> tuple[int, int] | None:
        """Where `text` is on screen now, as (row, col)."""
        for row, line in enumerate(self.screen.display):
            col = line.find(text)
            if col >= 0:
                return row, col
        return None

    def click_text(self, text: str) -> bool:
        """Click the first on-screen occurrence of `text` (real mouse bytes)."""
        hit = self.find(text)
        if hit is None:
            return False
        self.click(text, hit[0])
        return True

    def backspace(self, n: int = 24) -> None:
        """Clear a text field. Byte-at-a-time: a burst of backspaces can be
        coalesced/dropped by the pty and leaves the needle half-deleted."""
        for _ in range(n):
            os.write(self.fd, b"\x7f")
            self.pump(0.06)
        self.pump(0.5)

    def type_text(self, text: str) -> None:
        """Type character by character, the way a human does (one byte each)."""
        for ch in text:
            os.write(self.fd, ch.encode())
            self.pump(0.08)
        self.pump(0.5)

    def sidebar(self) -> str:
        """The left (Projects/Sessions) sidebar column, and only it: the word
        "rework" is also the app name in the chrome and a project label inside
        mission rows, so a project-filter check must not match those."""
        out = []
        for line in self.screen.display:
            head, sep, _rest = line.partition("│")
            if sep:
                out.append(head)
        return "\n".join(out)

    def screen_text(self) -> str:
        return "\n".join(self.screen.display)

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


# -- the three dashboard search features, in a real terminal -------------------
#
# Written after an audit passed a TUI feature that could not actually be used:
# Pilot tests drive widgets directly, so they cannot notice a row that renders
# Rich markup as literal text or a filter that only exists in widget state.
# These assertions are made against the CHARACTERS PYTE DECODED FROM THE PTY.

#: The app as a human starts it, except for the model provider: the ✦ path
#: (button -> worker -> request -> parse -> the one list) runs for real, only
#: the network call is a canned ranking. Everything else — store, filters,
#: rows, terminal — is the real thing.
STUBBED_MODEL_CODE = """
import sys
sys.path.insert(0, {root!r})
import rework.kernel.llm.adapters as adapters
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.types import ChatResult, Usage

STUB = FakeProvider([
    ChatResult(
        text='[{{"id": "m-gate000000b", "reason": "closest to the query"}}, '
             '{{"id": "m-run0000000a", "reason": "also about tests"}}]',
        usage=Usage(input_tokens=200, output_tokens=40),
    )
])
adapters.build_provider = lambda entry, extra=None: STUB
sys.argv = ["rework"]
from rework.cli import main
main()
"""


@pytest.fixture
async def seeded_terminal_env(tmp_path: Path) -> dict[str, str]:
    from seed import seed

    db = str(tmp_path / "tui.sqlite3")
    await seed(db, history_days=20)
    env = dict(os.environ)
    env.update(
        TERM="xterm-256color",
        REWORK_DB=db,
        REWORK_LOG_DIR=str(tmp_path / "logs"),
        REWORK_TUI_SETTINGS=str(tmp_path / "tui.json"),
        REWORK_LIBRARY_DIR=str(tmp_path / "lib"),
        REWORK_RESULTS_DIR=str(tmp_path / "results"),
        REWORK_REPORTS_DIR=str(tmp_path / "reports"),
        REWORK_NOTIFY="toast,bell",
        REWORK_MODELS='[{"id":"flash","provider":"fake","model":"f","tier":1}]',
    )
    return env


def test_dashboard_searches_work_in_a_real_terminal(seeded_terminal_env: dict[str, str]) -> None:
    root = str(Path(__file__).resolve().parents[2])
    term = Term(seeded_terminal_env, STUBBED_MODEL_CODE.format(root=root))
    try:
        assert term.wait_for("Talk to the orchestrator", 30), "the app rendered"
        term.send("™".encode())  # Option+2, exactly what macOS sends
        assert term.wait_for("PROJECTS", 20), "◊ opens the Dashboard"
        assert term.wait_for("Search missions", 10)
        term.pump(1.5)
        assert "All projects" in term.sidebar(), term.screen_text()
        assert "rework" in term.sidebar(), "the unfiltered sidebar lists every project"

        # -- (a) the session (mission) filter ---------------------------------
        assert term.click_text("Search missions"), "the search bar is clickable"
        term.type_text("flaky")
        assert term.wait_for("fix the flaky test in test_rolling_mean", 15)
        for gone in (
            "add dark mode to the settings page",
            "migrate the CI to uv",
            "audit the auth module for token leaks",
            "document the workspace API",
            "rewrite the parser in rust",
        ):
            assert gone not in term.screen_text(), f"{gone!r} must not survive 'flaky'"

        term.backspace()
        term.type_text("zzz")
        assert term.wait_for("No missions match “zzz”.", 15), (
            "the empty state says why: " + term.screen_text()
        )
        assert "fix the flaky test" not in term.screen_text()
        term.backspace()

        # -- (b) one search flow: ✦ reorders the SAME filtered list -----------
        term.type_text("the")
        assert term.wait_for("add dark mode to the settings page", 15)
        plain = term.screen_text()
        assert "add dark mode" in plain and "fix the flaky test" in plain
        assert "from AI" not in plain, "the plain filter says nothing about ranking"
        assert "Search" in plain and "✦" in plain, "both submit buttons on the one bar"

        assert term.click_text("✦"), "the ✦ button is clickable"
        # the model returns at most MAX_RESULTS ids, so the one status line says
        # how many of the matches it picked: 'the' matches 6 seeded missions
        assert term.wait_for("✦ top 2 of 6 from AI", 25), (
            "the ranking finished and reports on one line: " + term.screen_text()
        )
        term.pump(1.0)
        ranked = term.screen_text()
        assert ranked.count("from AI") == 1, "one status line, not a second one"
        assert "closest to the query" in ranked and "also about tests" in ranked
        # the reason is OUR markup: it must paint, never print as literal tags
        assert "[#a394f7]" not in ranked, "the reason's colour is markup, not text"
        assert ranked.count("add dark mode to the settings page") == 1, "no duplicate row"
        assert ranked.count("fix the flaky test in test_rolling_mean") == 1

        # Enter means "filter" both before and after ✦: it never re-ranks and
        # never opens a mission
        term.send(b"\r")
        term.pump(2.0)
        after = term.screen_text()
        assert "✦ top 2 of 6 from AI" in after, "enter left the ranking alone"
        assert "also about tests" in after, "enter kept the same list"
        assert "PROJECTS" in after, "enter did not open a mission page"
        term.backspace()
        term.send(b"\r")
        term.pump(1.5)

        # -- (c) the projects sidebar filter ----------------------------------
        before_clear = term.sidebar()
        assert "Filter projects…" in before_clear
        assert term.click_text("Filter projects"), "the project filter is clickable"
        term.type_text("poly")
        assert term.wait_for("polystats  4", 15)
        filtered = term.sidebar()
        assert "poly" in filtered, "the typed needle reaches the box"
        assert "polystats" in filtered, "the matching project row stays"
        assert "rework  2" not in filtered, "the non-matching project row is gone"
        assert "All projects" in filtered, "'All projects' is never filtered away"
        assert term.find("No project"), "pinned rows stay (a loose mission exists)"

        term.backspace()  # clear the project needle
        assert "rework  2" in term.sidebar(), "clearing restores the full project list"
        assert term.find("All projects")
    finally:
        term.close()
