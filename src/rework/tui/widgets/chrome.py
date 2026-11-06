"""App chrome: menu bar (+ dropdowns), tab bar, hint bar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from rework.tui.theme import ACCENT, FAINT, MUTED, OK, WARN, esc, money


@dataclass
class MenuItem:
    label: str
    action: str = ""  # an app action string, e.g. "app.new_chat"
    key: str = ""  # shortcut hint shown on the right
    separator: bool = False


SEPARATOR = MenuItem("", separator=True)


class _MenuTitle(Static):
    def __init__(self, name: str) -> None:
        super().__init__(f" {name} ", classes="menu-title")
        self.menu_name = name

    def on_click(self, event: events.Click) -> None:
        event.stop()
        bar = self.parent
        if isinstance(bar, MenuBar):
            bar.open_menu(self.menu_name)


class _Wordmark(Static):
    def on_click(self, event: events.Click) -> None:
        event.stop()
        bar = self.parent
        if isinstance(bar, MenuBar):
            bar.open_menu("rework")


class _Status(Static):
    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.call_later(self.app.run_action, "app.tab('gates')")


class MenuBar(Horizontal):
    """Row 1: wordmark, dropdown menus, global status on the right."""

    def __init__(self, menus: dict[str, list[MenuItem]]) -> None:
        super().__init__(id="menubar")
        self.menus = menus

    def compose(self) -> ComposeResult:
        yield _Wordmark(f"[{ACCENT}]◆[/] [b]rework[/b]", id="wordmark")
        for name in self.menus:
            if name != "rework":
                yield _MenuTitle(name)
        yield Static("", id="menubar-spacer")
        yield _Status("", id="menubar-status")

    def open_menu(self, name: str) -> None:
        if name not in self.menus:
            return
        x = 0
        if name != "rework":
            for title in self.query(_MenuTitle):
                if title.menu_name == name:
                    x = title.region.x
                    break
        self.app.push_screen(MenuScreen(self, name, x))

    def title_at(self, x: int) -> str | None:
        for title in self.query(_MenuTitle):
            if title.region.x <= x < title.region.right:
                return title.menu_name
        wordmark = self.query_one("#wordmark")
        if wordmark.region.x <= x < wordmark.region.right:
            return "rework"
        return None

    target = ""  # which database this TUI talks to ("local", "rewvis", a host)

    def set_status(self, *, online: bool, gates: int, spend_today: float, error: str = "") -> None:
        where = f" {esc(self.target)}" if self.target else " live"
        conn = f"[{OK}]●[/][{MUTED}]{where}[/]" if online else f"[{WARN}]○ offline{where}[/]"
        gate = f"[{WARN}]◆ {gates}[/]" if gates else f"[{FAINT}]◆ 0[/]"
        spend = f"[{MUTED}]{esc(money(spend_today))} today[/]"
        status = self.query_one("#menubar-status", _Status)
        status.update(f"{conn}  {gate}  {spend} ")
        status.tooltip = error or ("open gates: click to view" if gates else None)


class _MenuEntry(Static):
    def __init__(self, item: MenuItem, index: int) -> None:
        if item.separator:
            super().__init__("", classes="menu-sep")
        else:
            key = f"[{FAINT}]{esc(item.key)}[/]" if item.key else ""
            super().__init__(f" {esc(item.label)}", classes="menu-item")
            self._key_markup = key
        self.item = item
        self.index = index

    def on_mount(self) -> None:
        if not self.item.separator and self._key_markup:
            self.update(f" {esc(self.item.label)}")
            self.mount(Static(self._key_markup, classes="menu-key"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if not self.item.separator:
            screen = self.screen
            if isinstance(screen, MenuScreen):
                screen.choose(self.item)

    def on_enter(self, event: events.Enter) -> None:
        screen = self.screen
        if isinstance(screen, MenuScreen) and not self.item.separator:
            screen.highlight(self.index)


class MenuScreen(ModalScreen[None]):
    """A dropdown. Click outside or esc closes; left/right walk the menus."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "sibling(-1)", show=False),
        Binding("right", "sibling(1)", show=False),
        Binding("enter", "choose", show=False),
    ]

    def __init__(self, bar: MenuBar, name: str, x: int) -> None:
        super().__init__(classes="menu-screen")
        self.bar = bar
        self.menu_name = name
        self.x = x
        self.items = bar.menus[name]
        self.cursor = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            for i, item in enumerate(self.items):
                yield _MenuEntry(item, i)

    def on_mount(self) -> None:
        menu = self.query_one("#menu")
        menu.styles.offset = (self.x, 1)

    def highlight(self, index: int) -> None:
        self.cursor = index
        for entry in self.query(_MenuEntry):
            entry.set_class(entry.index == index, "-hover")

    def action_move(self, delta: int) -> None:
        selectable = [i for i, it in enumerate(self.items) if not it.separator]
        if not selectable:
            return
        if self.cursor not in selectable:
            self.highlight(selectable[0] if delta > 0 else selectable[-1])
            return
        pos = (selectable.index(self.cursor) + delta) % len(selectable)
        self.highlight(selectable[pos])

    def action_sibling(self, delta: int) -> None:
        names = list(self.bar.menus)
        target = names[(names.index(self.menu_name) + delta) % len(names)]
        self.dismiss()
        self.bar.open_menu(target)

    def action_choose(self) -> None:
        if 0 <= self.cursor < len(self.items):
            self.choose(self.items[self.cursor])

    def action_close(self) -> None:
        self.dismiss()

    def choose(self, item: MenuItem) -> None:
        self.dismiss()
        if item.action:
            self.app.call_later(self.app.run_action, item.action)

    def on_click(self, event: events.Click) -> None:
        # a click that reaches the screen itself landed outside the dropdown
        if event.widget is self or event.widget is None:
            self.dismiss()
            if event.screen_y == 0:
                other = self.bar.title_at(event.screen_x)
                if other and other != self.menu_name:
                    self.bar.open_menu(other)


class _Tab(Static):
    def __init__(self, name: str, label: str) -> None:
        super().__init__(classes="tab")
        self.tab_name = name
        self.label = label
        self.badge = 0

    def on_mount(self) -> None:
        self.paint()

    def paint(self) -> None:
        badge = f" [{WARN}]{self.badge}[/]" if self.badge else ""
        self.update(f" {esc(self.label)}{badge} ")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        bar = self.parent
        if isinstance(bar, TabBar):
            bar.post_message(TabBar.Changed(bar, self.tab_name))


class TabBar(Horizontal):
    """Row 2: section switcher."""

    class Changed(Message):
        def __init__(self, bar: TabBar, name: str) -> None:
            super().__init__()
            self.bar = bar
            self.name = name

        @property
        def control(self) -> TabBar:
            return self.bar

    def __init__(self, tabs: list[tuple[str, str]]) -> None:
        super().__init__(id="tabbar")
        self.tabs = tabs

    def compose(self) -> ComposeResult:
        for name, label in self.tabs:
            yield _Tab(name, label)
        yield Static("", id="tabbar-spacer")
        yield Static("", id="tabbar-note")

    def set_active(self, name: str) -> None:
        for tab in self.query(_Tab):
            tab.set_class(tab.tab_name == name, "-active")

    def set_badge(self, name: str, count: int) -> None:
        for tab in self.query(_Tab):
            if tab.tab_name == name and tab.badge != count:
                tab.badge = count
                tab.paint()

    def set_note(self, markup: str) -> None:
        self.query_one("#tabbar-note", Static).update(markup)


class HintBar(Static):
    """Last row: context key hints."""

    def __init__(self) -> None:
        super().__init__("", id="hintbar")

    def set_hints(self, hints: list[tuple[str, str]]) -> None:
        parts = [f"[{MUTED}]{esc(k)}[/] [{FAINT}]{esc(v)}[/]" for k, v in hints]
        self.update(f"[{FAINT}]  ·  [/]".join(parts))
