"""Modals: Picker (choose one), Form (a few fields), Help."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea

from rework.tui.theme import FAINT, MUTED, esc
from rework.tui.widgets.base import Btn, ChipGroup, RowData, RowList


@dataclass
class Option:
    value: str
    label: str
    meta: str = ""
    # per-row CSS classes, e.g. "-dim" for a model this process has no key for
    classes: str = ""


class Picker(ModalScreen[str | None]):
    """A filterable list. Dismisses with the chosen value, or None."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, options: list[Option], current: str | None = None) -> None:
        super().__init__(classes="modal")
        self.title_text = title
        self.options = options
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog picker"):
            yield Static(f"[b]{esc(self.title_text)}[/b]", classes="dialog-title")
            yield Input(placeholder="filter…", id="picker-filter", compact=True)
            yield RowList(id="picker-list", empty=f"[{FAINT}]no matches[/]")
            yield Static(f"[{FAINT}]enter pick · esc cancel[/]", classes="dialog-hint")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#picker-filter", Input).focus()

    def _fill(self, needle: str) -> None:
        needle = needle.lower().strip()
        rows = []
        for opt in self.options:
            if needle and needle not in f"{opt.label} {opt.meta} {opt.value}".lower():
                continue
            mark = "● " if opt.value == self.current else "  "
            meta = f"  [{MUTED}]{esc(opt.meta)}[/]" if opt.meta else ""
            rows.append(RowData(opt.value, f"{mark}{esc(opt.label)}{meta}", opt.classes))
        lst = self.query_one("#picker-list", RowList)
        lst.set_rows(rows)
        keys = lst.keys
        lst.select(self.current if self.current in keys else (keys[0] if keys else None))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        lst = self.query_one("#picker-list", RowList)
        if lst.selected:
            self.dismiss(lst.selected)

    def on_key(self, event: Any) -> None:
        if event.key in ("up", "down") and isinstance(self.focused, Input):
            event.stop()
            self.query_one("#picker-list", RowList).action_move(-1 if event.key == "up" else 1)

    def on_row_list_activated(self, event: RowList.Activated) -> None:
        if event.via_mouse or event.enter:
            self.dismiss(event.key)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text | area | choice
    value: str = ""
    placeholder: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)


class Form(ModalScreen[dict[str, str] | None]):
    """A small form. Dismisses with {name: value} or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+s", "submit", show=False),
    ]

    def __init__(self, title: str, fields: list[Field], *, submit: str = "Save") -> None:
        super().__init__(classes="modal")
        self.title_text = title
        self.fields = fields
        self.submit_label = submit

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog form"):
            yield Static(f"[b]{esc(self.title_text)}[/b]", classes="dialog-title")
            with VerticalScroll(classes="form-body"):
                for f in self.fields:
                    yield Static(f"[{MUTED}]{esc(f.label)}[/]", classes="form-label")
                    if f.kind == "area":
                        area = TextArea(
                            f.value, id=f"f-{f.name}", compact=True, classes="form-area"
                        )
                        yield area
                    elif f.kind == "choice":
                        yield ChipGroup(f.choices, f.value, id=f"f-{f.name}")
                    else:
                        yield Input(
                            f.value, placeholder=f.placeholder, id=f"f-{f.name}", compact=True
                        )
            with Horizontal(classes="dialog-actions"):
                yield Static(f"[{FAINT}]ctrl+s save · esc cancel[/]", classes="dialog-hint")
                yield Btn("Cancel", "cancel", variant="ghost")
                yield Btn(self.submit_label, "submit", variant="primary")

    def on_mount(self) -> None:
        first = self.query("Input, TextArea").first()
        if first is not None:
            first.focus()

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.fields:
            widget = self.query_one(f"#f-{f.name}")
            if isinstance(widget, TextArea):
                out[f.name] = widget.text
            elif isinstance(widget, ChipGroup):
                out[f.name] = widget.value
            elif isinstance(widget, Input):
                out[f.name] = widget.value
        return out

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        event.stop()
        if event.action == "submit":
            self.action_submit()
        else:
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.values())

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
[b]Navigation[/b]
  alt+1 … alt+5     Work · Dashboard · Gates · Library · Cost
  enter / click     open · go deeper
  esc               back
  esc esc           quit
  ctrl+p            command palette

[b]Work[/b]
  enter             send          shift+enter / alt+enter  newline
  ctrl+n            new chat      ctrl+b                   toggle chat list
  ↑ (empty prompt)  previous message
  paste image path  attach it

[b]Gates[/b]
  alt+a / alt+x     approve / reject the selected gate

[b]Anywhere[/b]
  ctrl+c            copy selection (drag to select)
  ctrl+r            refresh now
"""


class Help(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,question_mark", "close", show=False)]

    def __init__(self) -> None:
        super().__init__(classes="modal")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog help"):
            yield Static("[b]Keys[/b]", classes="dialog-title")
            yield Static(HELP_TEXT, classes="help-body")
            with Horizontal(classes="dialog-actions"):
                yield Static("", classes="dialog-hint")
                yield Btn("Close", "close", variant="primary")

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        event.stop()
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()
