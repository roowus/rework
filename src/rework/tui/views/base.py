"""The contract every tab implements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from textual.containers import Container

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

    def hints(self) -> list[tuple[str, str]]:
        return []

    def on_snapshot(self, snap: Snapshot) -> None:
        pass

    def shown(self) -> None:
        pass

    def back(self) -> bool:
        return False

    def busy_note(self, text: str) -> None:
        self.rw.set_note(text)

    def toast(self, text: str, *, severity: Any = "information", title: str = "") -> None:
        self.rw.notify(text, severity=severity, title=title, timeout=4)
