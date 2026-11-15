"""The contract every tab implements."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from textual.containers import Container
from textual.css.query import NoMatches

if TYPE_CHECKING:
    from rework.tui.app import ReworkApp
    from rework.tui.hub import Hub, Snapshot


class View(Container):
    """A tab. The app calls `on_snapshot` after every hub refresh (visible or
    not, so badges stay right), `shown` when it becomes the active tab, and
    `back` on esc (return True when handled, so esc doesn't arm quit)."""

    TAB: ClassVar[str] = ""
    LABEL: ClassVar[str] = ""

    def __init__(self) -> None:
        super().__init__(id=f"view-{self.TAB}", classes="view")

    @property
    def rw(self) -> ReworkApp:
        from rework.tui.app import ReworkApp

        assert isinstance(self.app, ReworkApp)
        return self.app

    @property
    def hub(self) -> Hub:
        return self.rw.hub

    @contextmanager
    def painting(self) -> Iterator[None]:
        """Guard a PAINT against a teardown race; let everything else through.

        A worker that awaits data and then paints can resume after the app
        started closing. Teardown removes the children first, so the paint's
        `query_one` raises `NoMatches` and the worker dies with a WorkerFailed
        traceback in tui.log (m7 findings #19: 2 of 8 runs, and the same shape
        as the hub's teardown race). Wrap ONLY the paint, never the data
        awaits: a failing query must still surface as it does today, and any
        other exception from the paint still propagates, so a live bug cannot
        hide behind this."""
        try:
            yield
        except NoMatches:
            return

    def hints(self) -> list[tuple[str, str]]:
        return []

    def on_snapshot(self, snap: Snapshot) -> None:
        pass

    def on_tick(self, snap: Snapshot) -> None:
        """A refresh found the data unchanged: no repaint, but a view that polls
        live detail (events, steps, a shared chat) keeps doing it here."""

    def shown(self) -> None:
        pass

    def back(self) -> bool:
        return False

    def busy_note(self, text: str) -> None:
        self.rw.set_note(text)

    def toast(self, text: str, *, severity: Any = "information", title: str = "") -> None:
        self.rw.notify(text, severity=severity, title=title, timeout=4)
