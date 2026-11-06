"""The component kit (docs/tui/DESIGN.md §5). Every view builds from these;
none of them hard-code colours (TCSS in app.tcss uses the theme tokens)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from rework.tui.theme import FAINT, MUTED, esc


class Btn(Static, can_focus=True):
    """A flat one-line button. variant: "" | "primary" | "danger" | "ghost".

    Posts `Btn.Pressed` on click, enter or space. `action` is a free string the
    owner view switches on, so views don't need an id per button."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter,space", "press", show=False)]

    class Pressed(Message):
        def __init__(self, btn: Btn) -> None:
            super().__init__()
            self.btn = btn

        @property
        def action(self) -> str:
            return self.btn.action

        @property
        def control(self) -> Btn:
            return self.btn

    def __init__(
        self,
        label: str,
        action: str = "",
        *,
        variant: str = "",
        id: str | None = None,
        classes: str = "",
        tooltip: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(f" {label} ", id=id, classes=f"btn {variant} {classes}".strip())
        self.action = action or (id or "")
        self.payload = payload
        if tooltip:
            self.tooltip = tooltip

    def set_label(self, label: str) -> None:
        self.update(f" {label} ")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_press()

    def action_press(self) -> None:
        if not self.disabled:
            self.post_message(self.Pressed(self))


class Chip(Static, can_focus=False):
    """A small toggle. Selected chips get the accent-dim background."""

    class Toggled(Message):
        def __init__(self, chip: Chip) -> None:
            super().__init__()
            self.chip = chip

        @property
        def control(self) -> Chip:
            return self.chip

    def __init__(self, label: str, value: str, *, group: str = "", selected: bool = False) -> None:
        super().__init__(f" {label} ", classes="chip")
        self.value = value
        self.group = group
        self.set_class(selected, "-selected")

    @property
    def selected(self) -> bool:
        return self.has_class("-selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Toggled(self))


class ChipGroup(Horizontal):
    """Single-select chips. Posts `ChipGroup.Changed(group, value)`."""

    class Changed(Message):
        def __init__(self, group: ChipGroup, value: str) -> None:
            super().__init__()
            self.group = group
            self.value = value

        @property
        def control(self) -> ChipGroup:
            return self.group

    def __init__(
        self,
        options: list[tuple[str, str]],
        value: str,
        *,
        id: str | None = None,
        label: str = "",
    ) -> None:
        super().__init__(id=id, classes="chips")
        self._options = options
        self.value = value
        self._label = label

    def compose(self) -> Any:
        if self._label:
            yield Static(self._label, classes="chips-label")
        for label, value in self._options:
            yield Chip(label, value, selected=value == self.value)

    def on_chip_toggled(self, event: Chip.Toggled) -> None:
        event.stop()
        self.select(event.chip.value)
        self.post_message(self.Changed(self, self.value))

    def select(self, value: str) -> None:
        self.value = value
        for chip in self.query(Chip):
            chip.set_class(chip.value == value, "-selected")


HEADER = "§"  # RowData keys starting with this are section headers (not selectable)


@dataclass
class RowData:
    key: str
    markup: str
    classes: str = ""


class Row(Static):
    """One clickable list row. A click selects AND activates (DESIGN §6.2)."""

    def __init__(self, data: RowData) -> None:
        super().__init__(data.markup, classes=f"row {data.classes}".strip())
        self.key = data.key
        self._markup = data.markup
        self._extra = data.classes

    def refresh_data(self, data: RowData) -> None:
        if data.markup != self._markup:
            self._markup = data.markup
            self.update(data.markup)
        if data.classes != self._extra:
            for cls in self._extra.split():
                self.remove_class(cls)
            for cls in data.classes.split():
                self.add_class(cls)
            self._extra = data.classes

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self.key.startswith(HEADER):
            return
        parent = self.parent
        while parent is not None and not isinstance(parent, RowList):
            parent = parent.parent
        if isinstance(parent, RowList):
            parent.activate(self.key, via_mouse=True)


class RowList(VerticalScroll, can_focus=True):
    """A list of `Row`s keyed by id, updated in place (no flicker on polls).

    Posts `RowList.Activated(key)` on click or enter; arrow keys move the
    selection and also activate (so the detail pane follows the cursor)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("enter", "enter", show=False),
        Binding("home", "jump(0)", show=False),
        Binding("end", "jump(-1)", show=False),
    ]

    class Activated(Message):
        def __init__(self, rows: RowList, key: str, via_mouse: bool, enter: bool) -> None:
            super().__init__()
            self.rows = rows
            self.key = key
            self.via_mouse = via_mouse
            self.enter = enter

        @property
        def control(self) -> RowList:
            return self.rows

    def __init__(self, *, id: str | None = None, classes: str = "", empty: str = "") -> None:
        super().__init__(id=id, classes=f"rowlist {classes}".strip())
        self.selected: str | None = None
        self._empty = empty
        self._keys: list[str] = []

    def compose(self) -> Any:
        if self._empty:
            yield Static(self._empty, classes="rowlist-empty")

    def set_rows(self, rows: list[RowData]) -> None:
        keys = [r.key for r in rows]
        existing = {row.key: row for row in self.query(Row)}
        if keys == self._keys:
            for data in rows:
                existing[data.key].refresh_data(data)
        else:
            for row in existing.values():
                row.remove()
            if rows:
                self.mount_all([Row(d) for d in rows])
            self._keys = keys
        for empty in self.query(".rowlist-empty"):
            empty.display = not rows
        if self.selected not in keys:
            self.selected = None
        self._paint_selection()

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def select(self, key: str | None) -> None:
        self.selected = key
        self._paint_selection()

    def _paint_selection(self) -> None:
        for row in self.query(Row):
            row.set_class(row.key == self.selected, "-selected")

    def activate(self, key: str, *, via_mouse: bool = False, enter: bool = False) -> None:
        self.select(key)
        for row in self.query(Row):
            if row.key == key:
                self.scroll_to_widget(row, animate=False)
        self.post_message(self.Activated(self, key, via_mouse, enter))

    @property
    def selectable(self) -> list[str]:
        return [k for k in self._keys if not k.startswith(HEADER)]

    def action_move(self, delta: int) -> None:
        keys = self.selectable
        if not keys:
            return
        if self.selected not in keys:
            index = 0 if delta > 0 else len(keys) - 1
        else:
            index = max(0, min(len(keys) - 1, keys.index(self.selected) + delta))
        self.activate(keys[index])

    def action_jump(self, index: int) -> None:
        keys = self.selectable
        if keys:
            self.activate(keys[index])

    def action_enter(self) -> None:
        if self.selected:
            self.activate(self.selected, enter=True)


class Label(Static):
    """Section label: UPPERCASE, faint, bold."""

    def __init__(self, text: str, *, id: str | None = None) -> None:
        super().__init__(esc(text.upper()), id=id, classes="label")


class Empty(Static):
    """Centred faint explanation."""

    def __init__(self, text: str, *, id: str | None = None) -> None:
        super().__init__(text, id=id, classes="empty")


class Crumb(Static):
    def __init__(self, text: str, target: str) -> None:
        super().__init__(text, classes="crumb")
        self.target = target

    def on_click(self, event: events.Click) -> None:
        event.stop()
        bar = self.parent
        if isinstance(bar, Breadcrumb):
            bar.post_message(Breadcrumb.Navigate(bar, self.target))


class Breadcrumb(Horizontal):
    """`Dashboard › polystats › m-1a2b › coder #2`; every segment is clickable."""

    class Navigate(Message):
        def __init__(self, bar: Breadcrumb, target: str) -> None:
            super().__init__()
            self.bar = bar
            self.target = target

        @property
        def control(self) -> Breadcrumb:
            return self.bar

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id, classes="breadcrumb")
        self._parts: list[tuple[str, str]] = []

    def set_parts(self, parts: list[tuple[str, str]]) -> None:
        """[(label, target)] — the last part is the current location."""
        if parts == self._parts:
            return
        self._parts = parts
        self.remove_children()
        widgets: list[Widget] = []
        for i, (label, target) in enumerate(parts):
            if i:
                widgets.append(Static(f"[{FAINT}] › [/]", classes="crumb-sep"))
            crumb = Crumb(esc(label), target)
            crumb.set_class(i == len(parts) - 1, "-current")
            widgets.append(crumb)
        self.mount_all(widgets)


class Stat(Static):
    """A big number with a label underneath (dashboard header, cost tab)."""

    def __init__(self, label: str, value: str = "—", *, id: str | None = None) -> None:
        super().__init__(id=id, classes="stat")
        self._label = label
        self._value = value
        self._sub = ""

    def on_mount(self) -> None:
        self._paint()

    def set(self, value: str, sub: str = "") -> None:
        if (value, sub) != (self._value, self._sub):
            self._value, self._sub = value, sub
            self._paint()

    def _paint(self) -> None:
        sub = f"\n[{FAINT}]{esc(self._sub)}[/]" if self._sub else ""
        self.update(f"[b]{esc(self._value)}[/b]\n[{MUTED}]{esc(self._label)}[/]{sub}")
