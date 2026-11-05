"""The rework TUI: `rework` with no arguments opens this.

The 2026-11-05 OS-shell redesign (owner-grilled): the app reads like a
familiar desktop. A MENU BAR sits at the top (File / Edit / View / Help with
real dropdown menus), TAB SECTIONS live under it (Work = type your work,
Dashboard = missions + projects, Gates = decisions with an unread badge,
Library = search what missions learned). One navigation rule everywhere:

    enter = deeper   esc = back   arrows = move   a/x = decide   f = fetch

The WORK tab is the typing surface: a big prompt, the missions you recently
touched, and the wizard one enter away. Gates get NOTIFICATIONS: a toast
when one opens, an unread-style badge on the Gates tab that clears when
everything is decided, and an optional terminal bell — all configurable via
REWORK_NOTIFY (toast|bell|both|none; default toast).

Visible artifacts land in ~/rework (results/, reports/); ~/.rework stays
hidden machinery. Bare `rework` opens blank: nothing auto-opens, nothing
auto-resumes; running missions are simply at the top of the table.

Quality rules this file tries to hold (owner bar: Claude-Code-grade):
- No blocking calls on the UI loop: store/IO via the async data layer.
- Polling never overlaps itself (one in-flight refresh guard per pane).
- Every destructive action (approve/reject) is explicit (a/x; never a bare
  Enter on a table).
- push_screen(wait_for_dismiss=True) only inside run_worker.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, HorizontalGroup, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Static,
)

from rework.tui.data import TuiData, human_age, status_rich

POLL_SECONDS = 2.0
EVENT_TAIL = 60


def notify_mode() -> str:
    """toast | bell | both | none (REWORK_NOTIFY; default toast)."""
    raw = (os.environ.get("REWORK_NOTIFY") or "toast").strip().lower()
    return raw if raw in ("toast", "bell", "both", "none") else "toast"


class SelectableLog(RichLog):
    """RichLog with working text selection.

    textual 8's RichLog inherits Widget.get_selection, which extracts text via
    self._render() — a method RichLog never defines (it draws through
    _render_line). So selecting a log in stock textual yields None and ctrl+c
    copies nothing; the log is exactly the pane people want to copy from
    (owner: 'allow copying text from the tui'). This override serializes the
    log's lines to plain text and lets Selection.extract() slice it."""

    def get_selection(self, selection: Any) -> tuple[str, str] | None:
        from textual.selection import Selection

        assert isinstance(selection, Selection)
        lines = [line.text for line in self.lines]
        if not lines:
            return None
        text = "\n".join(lines)
        return selection.extract(text), "\n"


# ---------------------------------------------------------------- modals


class NewMissionModal(ModalScreen[dict[str, Any]]):
    """Guided new-mission form: project, repo (URL or local path) OR a local
    folder to upload, objective, plan. Opened from Work / the prompt bar with
    the objective pre-filled and last-used repo/project/plan remembered."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Create", show=True),
    ]

    def __init__(
        self,
        projects: list[Any],
        objective: str = "",
        defaults: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._projects = projects
        self._objective = objective
        self._defaults = defaults or {}

    def compose(self) -> ComposeResult:
        choices = [("(no project)", "__none__")] + [
            (f"{p.name} ({p.id})", p.id) for p in self._projects
        ]
        default_project = self._defaults.get("project_id") or "__none__"
        # a remembered project may have been deleted; fall back cleanly
        if default_project != "__none__" and not any(v == default_project for _, v in choices):
            default_project = "__none__"
        with Vertical(id="modal-form"):
            yield Label("[b]New mission[/b]  [dim]ctrl+s create · esc cancel[/dim]")
            yield Input(
                id="f-objective",
                placeholder="Objective (what should happen)",
                value=self._objective,
            )
            yield Input(
                id="f-repo",
                placeholder="Repo: git URL or local path",
                value=self._defaults.get("repo") or "",
            )
            yield Input(
                id="f-repodir",
                placeholder="Folder to upload (non-git files), optional",
                value=self._defaults.get("repo_dir") or "",
            )
            yield Select(
                [
                    ("echo (no roles)", "echo"),
                    ("fix: investigate > code > audit + ship gate", "fix"),
                    ("fix-pr: like fix, opens a PR", "fix-pr"),
                ],
                value=self._defaults.get("plan") or "fix",
                id="f-plan",
                allow_blank=False,
            )
            yield Select(choices, value=default_project, id="f-project", allow_blank=False)
            with HorizontalGroup(id="modal-actions"):
                yield Button("Create", id="btn-create", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#f-objective", Input).focus()

    @on(Button.Pressed, "#btn-cancel")
    def cancel_btn(self) -> None:
        self._safe_dismiss()

    def _safe_dismiss(self) -> None:
        self._safe_dismiss_with(None)

    def _safe_dismiss_with(self, result: Any) -> None:
        """Dismiss without blowing up when the awaiting future was already
        cancelled (found live: InvalidStateError in ResultCallback.__call__
        when another exclusive worker cancelled the modal flow)."""
        import contextlib

        with contextlib.suppress(Exception):
            self.dismiss(result)

    @on(Button.Pressed, "#btn-create")
    def create_btn(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self._safe_dismiss()

    def action_submit(self) -> None:
        objective = self.query_one("#f-objective", Input).value.strip()
        if not objective:
            self.notify("Objective is required", severity="error")
            return
        repo = self.query_one("#f-repo", Input).value.strip() or None
        repo_dir = self.query_one("#f-repodir", Input).value.strip() or None
        if not repo and not repo_dir:
            self.notify("Give a repo (URL or path) or a folder to upload", severity="warning")
            return
        plan = self.query_one("#f-plan", Select).value
        project = self.query_one("#f-project", Select).value
        project_id = None if project == "__none__" else str(project)
        self._safe_dismiss_with(
            {
                "objective": objective,
                "repo": repo,
                "repo_dir": repo_dir,
                "project_id": project_id,
                "plan": str(plan),
            }
        )


CHOICE_CANCELLED = object()  # sentinel: esc, not "I picked the None row"


class ChoiceModal(ModalScreen[Any]):
    """A small selection screen: a title, a list of labelled choices, and the
    current one marked. Click (or arrow + enter) to pick; esc cancels.

    Owner: the Work knobs 'shouldnt be a toggle... it should be a selection
    screen menu'. Cycling a value on click is a toy; seeing the options and
    choosing is the real interaction."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        title: str,
        choices: list[tuple[str, Any]],  # (label, value)
        current: Any = None,
        *,
        footer: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._current = current
        self._footer = footer
        self._values = [value for _label, value in choices]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-form"):
            yield Label(f"[b]{self._title}[/b]  [dim]enter pick · esc cancel[/dim]")
            from textual.widgets.option_list import Option

            def marked(label: str, value: Any) -> str:
                # Rich needs balanced tags; keep the mark self-contained
                return (
                    f"[bold green]●[/bold green] {label}"
                    if value == self._current
                    else (f"[dim]○[/dim] {label}")
                )

            options = [Option(marked(label, value)) for label, value in self._choices]
            yield OptionList(*options, id="choice-list")
            if self._footer:
                yield Label(f"[dim]{self._footer}[/dim]")

    def on_mount(self) -> None:
        listing = self.query_one("#choice-list", OptionList)
        # land on the current choice, not always the first row
        if self._current is not None and self._current in self._values:
            listing.highlighted = self._values.index(self._current)
        listing.focus()

    @on(OptionList.OptionSelected, "#choice-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self._values):
            with contextlib.suppress(Exception):
                self.dismiss(self._values[index])

    def action_cancel(self) -> None:
        with contextlib.suppress(Exception):
            self.dismiss(CHOICE_CANCELLED)


class CreateProjectModal(ModalScreen[dict[str, Any]]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Create", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-form"):
            yield Label("[b]New project[/b]  [dim]ctrl+s create · esc cancel[/dim]")
            yield Input(id="p-name", placeholder="Project name")
            yield Select(
                [
                    ("git (daemon clones origins)", "git"),
                    ("upload (non-git working files)", "upload"),
                ],
                value="git",
                id="p-transport",
                allow_blank=False,
            )
            with HorizontalGroup(id="modal-actions"):
                yield Button("Create", id="btn-create", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#p-name", Input).focus()

    @on(Button.Pressed, "#btn-cancel")
    def cancel_btn(self) -> None:
        self._safe_dismiss()

    def _safe_dismiss(self) -> None:
        self._safe_dismiss_with(None)

    def _safe_dismiss_with(self, result: Any) -> None:
        """Dismiss without blowing up when the awaiting future was already
        cancelled (found live: InvalidStateError in ResultCallback.__call__
        when another exclusive worker cancelled the modal flow)."""
        import contextlib

        with contextlib.suppress(Exception):
            self.dismiss(result)

    @on(Button.Pressed, "#btn-create")
    def create_btn(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self._safe_dismiss()

    def action_submit(self) -> None:
        name = self.query_one("#p-name", Input).value.strip()
        if not name:
            self.notify("Project name is required", severity="error")
            return
        transport = str(self.query_one("#p-transport", Select).value)
        self._safe_dismiss_with({"name": name, "transport": transport})


# ---------------------------------------------------------------- panes


class Pane(Static):
    """A tab section inside the shell. Panes are WIDGETS (not screens): the
    shell keeps one live instance of each so polling and state survive tab
    switches. `rw` reaches the app; `shell` the shell screen."""

    @property
    def shell(self) -> Shell:
        node = self.parent
        while node is not None and not isinstance(node, Shell):
            node = node.parent
        assert isinstance(node, Shell)
        return node

    @property
    def rw(self) -> ReworkApp:
        app = self.app
        assert isinstance(app, ReworkApp)
        return app


class WorkPane(Pane):
    """The typing surface: a prompt with command autocomplete, and THREE
    buttons (project / model / effort) for the knobs a mission runs with.

    Owner asks folded in: 'commands should have autocomplete'; the pickers
    should be buttons, not hidden key bindings ('make this stuff buttons');
    and the old action-button row plus the recent-missions list were clutter
    and were removed ('these buttons are redundant', 'remove them also')."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        # ctrl+m IS enter (CR) and ctrl+e is end-of-line inside Input —
        # both were eaten. alt keys do not collide with editing.
        Binding("alt+m", "cycle_model", "Model", show=False),
        Binding("alt+e", "cycle_effort", "Effort", show=False),
        Binding("alt+p", "cycle_project", "Project", show=False),
    ]

    EFFORTS: ClassVar[tuple[str, ...]] = ("default", "low", "medium", "high")

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="what should rework do?  (type / for commands, enter to launch)",
            id="prompt",
        )
        with Horizontal(id="work-settings"):
            yield Button("", id="btn-project", classes="work-knob")
            yield Button("", id="btn-model", classes="work-knob")
            yield Button("", id="btn-effort", classes="work-knob")
        yield Label(
            "[dim]tab completes a command · click a button to change it[/dim]",
            id="work-hint",
        )

    def on_mount(self) -> None:
        self._model_ids: list[str] = []
        self._effort_index = 0
        self._project_ids: list[str] = []
        self._load_models()
        self._install_suggestions()
        self.query_one("#prompt", Input).focus()
        # ONE worker per mount: two exclusive run_worker calls share the
        # default worker GROUP, so the second cancels the first and leaves an
        # unawaited coroutine (the pane froze on mount — found live).
        self.run_worker(self._initial_load(), exclusive=True, group="work-load")

    async def _initial_load(self) -> None:
        try:
            await self.reload_projects()
        except Exception as exc:  # a bad project list must not kill the tab
            self.notify(f"projects unavailable: {exc}", severity="warning")

    # -- the three knob buttons: each opens a SELECTION screen ----------------

    @on(Button.Pressed, "#btn-project")
    def _project_clicked(self) -> None:
        self.run_worker(self._pick_project(), exclusive=True, group="knob")

    @on(Button.Pressed, "#btn-model")
    def _model_clicked(self) -> None:
        self.run_worker(self._pick_model(), exclusive=True, group="knob")

    @on(Button.Pressed, "#btn-effort")
    def _effort_clicked(self) -> None:
        self.run_worker(self._pick_effort(), exclusive=True, group="knob")

    async def _pick_project(self) -> None:
        choices: list[tuple[str, Any]] = [("(no project)", None)]
        choices += [(p.name, str(p.id)) for p in self.shell.rw._project_cache.values()]
        chosen = await self.app.push_screen(
            ChoiceModal("project", choices, self._current_project()),
            wait_for_dismiss=True,
        )
        if chosen is CHOICE_CANCELLED:
            return
        self.shell.rw._mission_defaults["project_id"] = chosen
        self._update_setting_labels()
        self.notify(f"project: {self._project_name(chosen)}")

    async def _pick_model(self) -> None:
        if not self._model_ids:
            self.notify("no models in the catalog (REWORK_MODELS)", severity="warning")
            return
        current = self._current_model() or (self._model_ids[0] if self._model_ids else None)
        choices = [(mid, mid) for mid in self._model_ids]
        chosen = await self.app.push_screen(
            ChoiceModal("model", choices, current, footer="from REWORK_MODELS"),
            wait_for_dismiss=True,
        )
        if chosen is CHOICE_CANCELLED or not chosen:
            return
        self.shell.rw._mission_defaults["model"] = chosen
        self._update_setting_labels()

    async def _pick_effort(self) -> None:
        current = self._current_effort() or "default"
        choices: list[tuple[str, Any]] = [
            ("default (provider decides)", None),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
        ]
        chosen = await self.app.push_screen(
            ChoiceModal("effort", choices, current), wait_for_dismiss=True
        )
        if chosen is CHOICE_CANCELLED:
            return
        self.shell.rw._mission_defaults["effort"] = chosen
        self._update_setting_labels()

    # -- command autocomplete ------------------------------------------------

    def _install_suggestions(self) -> None:
        from textual.suggester import SuggestFromList

        prompt = self.query_one("#prompt", Input)
        # Suggestions are recomputed when this is called again; the command set
        # is static, project ids change so reload_projects refreshes it.
        prompt.suggester = SuggestFromList(self._suggestions(), case_sensitive=False)

    def _suggestions(self) -> list[str]:
        from rework.tui.app import SLASH_COMMANDS

        out = [c for c, _ in SLASH_COMMANDS]
        out.extend(getattr(self, "_project_names", []))
        return out

    def _refresh_suggestions(self) -> None:
        from textual.suggester import SuggestFromList

        prompt = self.query_one("#prompt", Input)
        prompt.suggester = SuggestFromList(self._suggestions(), case_sensitive=False)

    # -- project selection ---------------------------------------------------

    def _current_project(self) -> str | None:
        return self.shell.rw._mission_defaults.get("project_id")

    def _project_name(self, project_id: str | None) -> str:
        if not project_id:
            return "no project"
        project = self.shell.rw._project_cache.get(project_id)
        return project.name if project else project_id

    def _update_project_label(self) -> None:
        if self.is_mounted:
            self.query_one(
                "#btn-project", Button
            ).label = f"project: {self._project_name(self._current_project())}"

    async def reload_projects(self) -> None:
        await self.shell.rw.load_project_cache()
        projects = list(self.shell.rw._project_cache.values())
        self._project_ids = [str(p.id) for p in projects]
        self._project_names = [p.name for p in projects]
        current = self._current_project()
        if current in self._project_ids:
            self._project_index = self._project_ids.index(current)
        self._update_project_label()
        self._refresh_suggestions()

    def action_cycle_project(self) -> None:
        """Cycle through: no project -> each project. The choice is visible in
        the settings row and prefills the mission wizard."""
        options: list[str | None] = [None, *self._project_ids]
        current = self._current_project()
        index = options.index(current) if current in options else 0
        chosen = options[(index + 1) % len(options)]
        self.shell.rw._mission_defaults["project_id"] = chosen
        if chosen and chosen in self._project_ids:
            self._project_index = self._project_ids.index(chosen)
        self._update_project_label()
        self.notify(f"project: {self._project_name(chosen)}")

    def _load_models(self) -> None:
        from rework.config import load_catalog

        try:
            entries = load_catalog().all()
        except Exception:  # a broken catalog must not kill the tab
            entries = []
        self._model_ids = [e.id for e in entries]
        self._update_setting_labels()

    def _model_index(self) -> int:
        current = self.shell.rw._mission_defaults.get("model")
        if current in self._model_ids:
            return self._model_ids.index(str(current))
        return 0

    def _current_model(self) -> str | None:
        return self.shell.rw._mission_defaults.get("model")

    def _current_effort(self) -> str | None:
        return self.shell.rw._mission_defaults.get("effort")

    def _update_setting_labels(self) -> None:
        if not self.is_mounted:
            return
        model = self._current_model()
        if not model and self._model_ids:
            model = self._model_ids[0]
        effort = self._current_effort() or "default"
        self.query_one("#btn-model", Button).label = f"model: {model or 'catalog default'}"
        self.query_one("#btn-effort", Button).label = f"effort: {effort}"
        self._update_project_label()

    def action_cycle_model(self) -> None:
        if not self._model_ids:
            self.notify("no models in the catalog (REWORK_MODELS)", severity="warning")
            return
        index = (self._model_index() + 1) % len(self._model_ids)
        self.shell.rw._mission_defaults["model"] = self._model_ids[index]
        self._update_setting_labels()
        self.notify(f"model: {self._model_ids[index]}")

    def action_cycle_effort(self) -> None:
        nxt = self.EFFORTS[(self._effort_index + 1) % len(self.EFFORTS)]
        self._effort_index = self.EFFORTS.index(nxt)
        self.shell.rw._mission_defaults["effort"] = None if nxt == "default" else nxt
        self._update_setting_labels()
        self.notify(f"effort: {nxt}")

    def on_show(self) -> None:
        self._update_setting_labels()

    @on(Input.Submitted, "#prompt")
    async def prompt_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.shell.rw.run_slash(text)
            return
        # free text = a mission: wizard pre-filled with the objective and
        # last-used repo/project/plan (remembered on the app)
        self.run_worker(self.shell.new_mission_flow(objective=text), exclusive=True)


class ProjectBand(Static, can_focus=True):
    """Projects chips — all, then each project with its mission counts —
    navigable with arrows, enter (or a click) filters the missions table
    below. `p` on it creates a project."""

    class ProjectPicked(Message):
        def __init__(self, project_id: str | None) -> None:
            super().__init__()
            self.project_id = project_id

    class NewProjectRequested(Message):
        pass

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("left", "prev", "◀", show=False),
        Binding("right", "next", "▶", show=False),
        Binding("enter", "pick", "Open", show=False),
        Binding("p", "new", "New project", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="project-band")
        self._projects: list[Any] = []
        self._counts: dict[str, tuple[int, int]] = {}
        self._index = 0
        self._chip_cols: list[tuple[int, int, str | None]] = []

    def update_projects(self, projects: list[Any], counts: dict[str, tuple[int, int]]) -> None:
        self._projects = projects
        self._counts = counts
        self._index = min(self._index, len(projects))
        self.refresh(layout=True)

    def current_project_id(self) -> str | None:
        if self._index == 0:
            return None
        if self._index <= len(self._projects):
            return str(self._projects[self._index - 1].id)
        return None

    def render(self) -> Any:
        def chip_text(label: str, detail: str) -> tuple[str, str]:
            plain = f" {label} {detail} ".rstrip() + " "
            hi = (
                f"[reverse] {label} [/reverse] [b]{detail}[/b]"
                if detail
                else f"[reverse] {label} [/reverse]"
            )
            return plain, hi

        texts: list[tuple[str, str, str | None]] = []
        texts.append((*chip_text("all", ""), None))
        for project in self._projects:
            total, running = self._counts.get(project.id, (0, 0))
            detail = f"{running}●/{total}" if running else str(total)
            texts.append((*chip_text(project.name, detail), str(project.id)))
        texts.append((*chip_text("+ new", ""), "__new__"))

        self._chip_cols = []
        out: list[str] = []
        col = 1
        for plain, hi, pid in texts:
            width = len(plain)
            self._chip_cols.append((col, col + width, pid))
            out.append(hi if pid == self.current_project_id() and pid != "__new__" else plain)
            col += width + 1
        return " " + " ".join(out)

    def action_prev(self) -> None:
        self._index = max(0, self._index - 1)
        self.refresh(layout=True)
        self.post_message(self.ProjectPicked(self.current_project_id()))

    def action_next(self) -> None:
        self._index = min(len(self._projects), self._index + 1)
        self.refresh(layout=True)
        self.post_message(self.ProjectPicked(self.current_project_id()))

    def action_pick(self) -> None:
        self.post_message(self.ProjectPicked(self.current_project_id()))

    def action_new(self) -> None:
        self.post_message(self.NewProjectRequested())

    def on_click(self, event: Any) -> None:
        for start, end, pid in self._chip_cols:
            if start <= event.x < end:
                if pid == "__new__":
                    self.action_new()
                    return
                self._index = (
                    0
                    if pid is None
                    else next(
                        (i for i, p in enumerate(self._projects, 1) if str(p.id) == pid),
                        0,
                    )
                )
                self.refresh(layout=True)
                self.post_message(self.ProjectPicked(self.current_project_id()))
                return


# ---------------------------------------------------------------- menu bar


class DashboardPane(Pane):
    """PROJECTS band on top (the primary axis), missions full-width below for
    the selection, live detail strip."""

    class ProjectPicked(Message):
        def __init__(self, project_id: str | None) -> None:
            super().__init__()
            self.project_id = project_id

    class NewProjectRequested(Message):
        pass

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._selected_mission: str | None = None
        self._selected_project: str | None = None  # None = all
        self._last_event_seq: int = 0
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield ProjectBand()
        yield Label("[b]missions[/b]", id="mid-title")
        yield DataTable(id="missions")
        with Vertical(id="detail"):
            yield Label("enter opens a mission", id="mission-head")
            yield SelectableLog(id="events", markup=True, highlight=False, wrap=True, max_lines=600)
            yield Static("", id="gate-box")

    def on_mount(self) -> None:
        missions = self.query_one("#missions", DataTable)
        missions.cursor_type = "row"
        missions.add_columns("mission", "status", "plan", "age")
        self.set_interval(POLL_SECONDS, self.tick)
        self.run_worker(self.reload_missions(), exclusive=True)

    async def tick(self) -> None:
        if self._refreshing or not self.display:
            return
        self._refreshing = True
        try:
            await self.reload_missions()
            await self.stream_events()
        except Exception as exc:  # store hiccups must not kill the UI
            self.notify(f"refresh failed: {exc}", severity="error")
        finally:
            self._refreshing = False

    async def reload_missions(self) -> None:
        table = self.query_one("#missions", DataTable)
        selected_id = self._selected_mission
        table.clear()
        missions = await self.shell.rw.data.missions()
        self.shell.rw._mission_cache = {m.id: m for m in missions}

        # per-project counts feed the band: total + running per project
        counts: dict[str, tuple[int, int]] = {}
        for m in missions:
            total, running = counts.get(m.project_id or "", (0, 0))
            counts[m.project_id or ""] = (total + 1, running + (m.status.value == "running"))

        # running/awaiting-gate first (live work on top), newest within group
        rank = {
            "running": 0,
            "awaiting_gate": 0,
            "on_hold": 1,
            "pending": 2,
            "done": 3,
            "failed": 3,
            "cancelled": 4,
        }
        missions.sort(key=lambda m: (rank.get(m.status.value, 5), -m.created_at))
        shown = 0
        running_shown = 0
        for mission in missions:
            if self._selected_project and mission.project_id != self._selected_project:
                continue
            table.add_row(
                mission.id,
                status_rich(mission.status.value),
                (mission.plan or {}).get("kind", "echo"),
                human_age(mission.created_at),
                key=mission.id,
            )
            shown += 1
            running_shown += mission.status.value == "running"

        # ONE context line + the project chips re-render from the same data
        band = self.query_one("#project-band", ProjectBand)
        band.update_projects(list(self.shell.rw._project_cache.values()), counts)
        name = "all projects"
        if self._selected_project:
            p = self.shell.rw._project_cache.get(self._selected_project)
            name = p.name if p else self._selected_project
        open_gates = await self.shell.rw.data.open_gates()
        gate_note = ""
        if open_gates:
            n = len(open_gates)
            gate_note = f" · [bold yellow]{n} gate{'s' if n != 1 else ''} waiting[/bold yellow]"
        title = self.query_one("#mid-title", Label)
        title.update(
            f"[b]{name}[/b] · {shown} mission{'s' if shown != 1 else ''}"
            + (f" · {running_shown} running" if running_shown else "")
            + gate_note
        )
        if selected_id is not None:
            for row, key in enumerate(table.rows):
                if str(key.value) == selected_id:
                    table.move_cursor(row=row)
                    break
        self.shell.rw.set_gates_badge(open_gates)

    @on(ProjectBand.ProjectPicked)
    async def project_picked(self, event: ProjectBand.ProjectPicked) -> None:
        self._selected_project = event.project_id
        await self.reload_missions()
        # projects are the entry point: choosing one hands control to missions
        self.query_one("#missions", DataTable).focus()

    @on(ProjectBand.NewProjectRequested)
    async def project_new_requested(self) -> None:
        self.run_worker(self.shell.new_project_flow(), exclusive=True)

    @on(DataTable.RowSelected, "#missions")
    async def mission_selected(self, event: DataTable.RowSelected) -> None:
        await self.shell.open_mission(str(event.row_key.value))

    @on(DataTable.RowHighlighted, "#missions")
    async def mission_cursor_moved(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key and str(key) != self._selected_mission:
            await self.select_mission(str(key))

    async def select_mission(self, mission_id: str) -> None:
        if mission_id == self._selected_mission:
            return
        self._selected_mission = mission_id
        self._last_event_seq = 0
        log = self.query_one("#events", SelectableLog)
        log.clear()
        await self.stream_events(force=True)

    async def stream_events(self, force: bool = False) -> None:
        mission_id = self._selected_mission
        if mission_id is None:
            return
        events = await self.shell.rw.data.events_tail(
            mission_id, 0 if force else self._last_event_seq
        )
        if not events:
            return
        runs = {r.id: r for r in await self.shell.rw.data.mission_runs(mission_id)}
        log = self.query_one("#events", SelectableLog)
        head = self.query_one("#mission-head", Label)
        if force:
            log.clear()
            events = events[-EVENT_TAIL:]
        for event in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
            role = ""
            if event.run_id and event.run_id in runs:
                role = runs[event.run_id].step
            detail = ""
            payload = event.payload or {}
            if payload.get("note"):
                detail = str(payload["note"])
            elif payload.get("error"):
                detail = f"[red]{payload['error']}[/red]"
            elif payload.get("question"):
                detail = str(payload["question"])
            elif event.type.value == "mission_created":
                detail = str(payload.get("objective", ""))[:100]
            log.write(f"[dim]{stamp}[/dim] {event.type.value:<15} {role} {detail}".rstrip())
            self._last_event_seq = max(self._last_event_seq, event.seq or 0)
        m = self.shell.rw._mission_cache.get(mission_id)
        if m is not None:
            spend = await self.shell.rw.data.mission_spend(mission_id)
            head.update(
                f"[b]{m.id}[/b] {status_rich(m.status.value)}  "
                f"[dim]{(m.objective or '')[:80]}[/dim]  ${spend:.4f}  [f] fetch"
            )
        await self.render_gate_box(mission_id)

    async def render_gate_box(self, mission_id: str) -> None:
        box = self.query_one("#gate-box", Static)
        gates = await self.shell.rw.data.open_gates(mission_id)
        if not gates:
            box.update("")
            return
        lines = ["[bold yellow]WAITING ON YOU[/bold yellow]"]
        for gate in gates:
            lines.append(f"  gate {gate.id} [{gate.kind.value}]  {gate.question}")
        lines.append("  [a] approve    [x] reject")
        box.update("\n".join(lines))

    async def selected_mission_id(self) -> str | None:
        return self._selected_mission

    async def action_approve_gate(self) -> None:
        await self._decide_selected(True)

    async def action_reject_gate(self) -> None:
        await self._decide_selected(False)

    async def _decide_selected(self, approved: bool) -> None:
        mission_id = self._selected_mission
        if mission_id is None:
            self.notify("select a mission first", severity="warning")
            return
        gates = await self.shell.rw.data.open_gates(mission_id)
        if not gates:
            self.notify("no open gate on this mission")
            return
        for gate in gates:
            ok = await self.shell.rw.data.decide_gate(gate.id, approved)
            if ok:
                self.notify(f"gate {gate.id} {'approved' if approved else 'rejected'}")
        await self.stream_events(force=True)
        await self.reload_missions()


class GatesPane(Pane):
    """Every open gate across missions; a approves, x rejects the selected.
    Lives in its own tab with an unread badge on the tab label."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("a", "approve", "Approve", show=True),
        Binding("x", "reject", "Reject", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Label("[b]Open gates[/b] — the human decisions rework is waiting on", id="gates-head")
        yield DataTable(id="gates-table")
        yield Label(id="gates-empty")

    def on_mount(self) -> None:
        table = self.query_one("#gates-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("gate", "mission", "kind", "question")
        self.set_interval(POLL_SECONDS, self.refresh_quiet)
        self.call_after_refresh(self.refresh_quiet)

    async def refresh_quiet(self) -> None:
        """Reload without toasts; polls while the tab is mounted."""
        if not self.display:
            return
        table = self.query_one("#gates-table", DataTable)
        gates = await self.shell.rw.data.open_gates()
        selected = self._selected_gate_id()
        table.clear()
        self._row_ids = []
        for gate in gates:
            table.add_row(
                gate.id, gate.mission_id, gate.kind.value, gate.question[:70], key=gate.id
            )
            self._row_ids.append(gate.id)
        empty = self.query_one("#gates-empty", Label)
        empty.display = not gates
        if not gates:
            empty.update("Nothing waiting on you. Enjoy the silence.")
        if selected and selected in self._row_ids:
            table.move_cursor(row=self._row_ids.index(selected))

    def _selected_gate_id(self) -> str | None:
        table = self.query_one("#gates-table", DataTable)
        if not getattr(self, "_row_ids", []) or table.cursor_row >= len(self._row_ids):
            return None
        return self._row_ids[table.cursor_row]

    async def _decide(self, approved: bool) -> None:
        gate_id = self._selected_gate_id()
        if gate_id is None:
            return
        ok = await self.shell.rw.data.decide_gate(gate_id, approved)
        if ok:
            self.notify(f"gate {gate_id} {'approved' if approved else 'rejected'}")
            await self.refresh_quiet()
            self.shell.rw.refresh_dashboard()
            self.shell.update_gate_badge()


class LibraryPane(Pane):
    """Search-first mail-client split: the cursor lands in the search box;
    typing filters live (hybrid when embeddings are configured); list left,
    rendered body right."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("enter", "read", "Read", show=True),
        Binding("a", "activate", "Activate", show=True),
        Binding("d", "retire", "Retire", show=True),
        Binding("down", "focus_list", "List", show=False),
        Binding("up", "focus_search", "Search", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="search the library… (type to filter, enter to read)",
            id="lib-query",
        )
        with Horizontal(id="lib-split"):
            with Vertical(id="lib-list-pane"):
                yield Static("", id="lib-counts")
                yield DataTable(id="lib-table")
            with VerticalScroll(id="lib-body-pane"):
                yield Static("", id="lib-body", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#lib-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("type", "status", "name")
        self._items: list[Any] = []
        self._row_ids: list[str] = []
        self.run_worker(self.reload(""), exclusive=True)

    @on(Input.Changed, "#lib-query")
    async def query_changed(self, event: Input.Changed) -> None:
        self.run_worker(self.reload(event.value), exclusive=True)

    async def reload(self, query: str) -> None:
        # hybrid when embeddings configured (already degrades to keyword on
        # embedder failure); plain keyword search otherwise
        from rework.library.embeddings import embeddings_from_env

        if embeddings_from_env() is not None:
            items = await self.shell.rw.data.library_hybrid_search(query)
        else:
            items = await self.shell.rw.data.library_search(query)
        self._items = items
        table = self.query_one("#lib-table", DataTable)
        cursor_before = table.cursor_row
        table.clear()
        self._row_ids = []
        for item in items:
            table.add_row(item.type.value, item.status.value, item.name, key=item.id)
            self._row_ids.append(item.id)
        counts: dict[str, int] = {}
        for item in items:
            counts[item.type.value] = counts.get(item.type.value, 0) + 1
        summary = "  ".join(f"{k}s {v}" for k, v in sorted(counts.items())) or "empty"
        self.query_one("#lib-counts", Static).update(f"[dim]{summary}[/dim]")
        if items and 0 <= cursor_before < len(items):
            table.move_cursor(row=cursor_before)
            await self.render_body(self._row_ids[cursor_before])
        elif items:
            await self.render_body(self._row_ids[0])

    @on(DataTable.RowHighlighted, "#lib-table")
    async def cursor_moved(self, event: DataTable.RowHighlighted) -> None:
        await self.render_body(str(event.row_key.value))

    def _selected_item_id(self) -> str | None:
        table = self.query_one("#lib-table", DataTable)
        if not self._row_ids or table.cursor_row >= len(self._row_ids):
            return None
        return self._row_ids[table.cursor_row]

    async def render_body(self, item_id: str) -> None:
        pane = self.query_one("#lib-body", Static)
        item, body = await self.shell.rw.data.library_body(item_id)
        if item is None:
            pane.update(f"(no item {item_id})")
            return
        text = body if body is not None else "(no body file on this machine)"
        meta = (
            f"[b]{item.name}[/b]  [dim]{item.type.value}/{item.status.value}"
            f" · uses {item.uses} · {item.id}[/dim]\n"
            f"[dim]{item.description}[/dim]\n"
            f"{'=' * max(20, min(60, len(item.name)))}\n\n"
        )
        pane.update(meta + text)

    async def action_read(self) -> None:
        item_id = self._selected_item_id()
        if item_id is not None:
            await self.render_body(item_id)

    async def _status_change(self, retire: bool) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        ok = await self.shell.rw.data.library_set_status(item_id, retire)
        if ok:
            verb = "retired" if retire else "activated"
            self.notify(f"{item_id} {verb}")
            await self.reload(self.query_one("#lib-query", Input).value)

    async def action_activate(self) -> None:
        await self._status_change(retire=False)

    async def action_retire(self) -> None:
        await self._status_change(retire=True)

    def action_focus_list(self) -> None:
        self.query_one("#lib-table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#lib-query", Input).focus()


# ---------------------------------------------------------------- band


WORDMARK = "rework"
WORDMARK_PAD = 2  # spaces after the wordmark before the first menu


class MenuBar(Static, can_focus=True):
    """A desktop-style menu bar: rework wordmark first, then File / Edit /
    View / Help. Click a menu (or focus the bar and press enter) to open a
    real dropdown; moving the mouse across the labels highlights the hovered
    one (owner: mouse should highlight as it goes over things). The far side
    carries the Gates badge (unread-style)."""

    class MenuAction(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    MENUS: ClassVar[dict[str, list[tuple[str, str, str]]]] = {
        "File": [
            ("New mission…", "n", "new_mission"),
            ("New project…", "p", "new_project"),
            ("Fetch result…", "f", "fetch"),
            ("", "", ""),  # separator
            ("Quit", "esc esc", "quit"),
        ],
        "Edit": [
            ("Copy selection", "ctrl+c", "copy"),
            ("Select all (pane)", "ctrl+a", "select_all"),
        ],
        "View": [
            ("Work", "alt+1", "tab_work"),
            ("Dashboard", "alt+2", "tab_dashboard"),
            ("Gates", "alt+3", "tab_gates"),
            ("Library", "alt+4", "tab_library"),
        ],
        "Help": [
            ("Keys", "", "keys"),
            ("Copying text", "", "copy_help"),
            ("About rework", "", "about"),
        ],
    }

    def __init__(self) -> None:
        super().__init__(id="menu-bar")
        self._open_menu: str | None = None
        self._dropdown: Any | None = None
        self._gate_count = 0
        self._hover: str | None = None

    def set_gate_count(self, count: int) -> None:
        if count != self._gate_count:
            self._gate_count = count
            self.refresh(layout=True)

    def _menu_spans(self) -> list[tuple[str, int, int]]:
        """(name, start_col, width) for each menu label, wordmark included in
        the offset so hover and click share one geometry."""
        spans: list[tuple[str, int, int]] = []
        col = len(WORDMARK) + WORDMARK_PAD
        for name in self.MENUS:
            width = len(name) + 2  # padded label
            spans.append((name, col, width))
            col += width + 1
        return spans

    def _hover_markup(self, label: str) -> str:
        """Rich markup for a hovered label. Rich knows no theme variables
        (`[on $boost]` renders as literal nothing), so resolve a real colour
        off the app theme and fall back to a fixed one."""
        color = "#3a5a7a"
        try:
            theme = self.app.current_theme
            for attr in ("panel", "surface", "primary"):
                value = getattr(theme, attr, None)
                if value:
                    color = str(value)
                    break
        except Exception:  # pragma: no cover - theme shape varies
            pass
        return f"[on {color}]{label}[/on {color}]"

    def render(self) -> Any:
        out = f"[bold]{WORDMARK}[/bold]" + " " * WORDMARK_PAD
        for name, _start, _width in self._menu_spans():
            label = f" {name} "
            active = self._open_menu == name
            hover = self._hover == name
            if active:
                out += f"[reverse]{label}[/reverse]"
            elif hover:
                out += self._hover_markup(label)
            else:
                out += label
            out += " "
        badge = ""
        if self._gate_count:
            badge = f"[bold yellow][ Gates ●{self._gate_count} ][/bold yellow]"
        return out + badge

    def _menu_items(self, name: str) -> list[tuple[str, str, str]]:
        return self.MENUS.get(name, [])

    def _menu_at(self, x: int) -> str | None:
        for name, start, width in self._menu_spans():
            if start <= x < start + width:
                return name
        return None

    async def open_dropdown(self, menu: str) -> None:
        await self.close_dropdown()
        self._open_menu = menu
        self.refresh(layout=True)
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        options = []
        self._actions: list[str] = []
        for label, key, action in self._menu_items(menu):
            if not label:  # separator
                options.append(Option("[dim]" + "─" * 24 + "[/dim]", disabled=True))
                self._actions.append("")
                continue
            key_part = f"[dim]{key:>9}[/dim]" if key else ""
            options.append(Option(f"{label:<22}{key_part}"))
            self._actions.append(action)
        dropdown = OptionList(*options, id="menu-dropdown")
        start = next(s for n, s, _w in self._menu_spans() if n == menu)
        dropdown.styles.offset = (start + 1, 1)
        dropdown.styles.width = 36
        dropdown.styles.layer = "overlay"
        self._dropdown = dropdown
        # mount on the SCREEN (absolute coordinates); a child of the bar with
        # an undeclared layer doesn't paint
        await self.app.screen.mount(dropdown)
        dropdown.focus()

    async def close_dropdown(self) -> None:
        if self._dropdown is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await self._dropdown.remove()
            self._dropdown = None
        if self._open_menu is not None:
            self._open_menu = None
            self.refresh(layout=True)

    async def handle_selected(self, event: OptionList.OptionSelected) -> None:
        """Route a choice made in OUR dropdown.

        The dropdown mounts on the SCREEN (it must, to paint over other
        widgets), so its OptionSelected bubbles up through the screen and
        never through the MenuBar's own DOM branch — the @on handler that
        used to live here never fired (the owner's 'dropdowns dont work').
        The Shell, which IS the screen, catches the message and calls this."""
        if event.option_list is not self._dropdown:
            return
        action = self._actions[event.option_index]
        await self.close_dropdown()
        if action:
            self.post_message(self.MenuAction(action))

    async def on_key(self, event: Any) -> None:
        if event.key == "escape":
            await self.close_dropdown()
            self.focus()

    def on_blur(self, event: Any) -> None:
        # focusing the dropdown blurs the bar — that is NOT a dismissal (the
        # owner's 'dropdowns dont work': this closed every menu the instant it
        # opened). Only close when focus left for something else.
        focused = self.app.focused
        if focused is not None and focused is self._dropdown:
            return
        if self._dropdown is not None and self._dropdown.has_focus_within:
            return
        self.run_worker(self.close_dropdown(), exclusive=True)

    def on_mouse_move(self, event: Any) -> None:
        """Hover highlight (owner ask): light the label under the pointer."""
        hover = self._menu_at(event.x)
        if hover != self._hover:
            self._hover = hover
            self.refresh()

    def on_leave(self, event: Any) -> None:
        if self._hover is not None:
            self._hover = None
            self.refresh()

    def on_click(self, event: Any) -> None:
        name = self._menu_at(event.x)
        if name is None:
            return
        if self._open_menu == name:
            self.run_worker(self.close_dropdown(), exclusive=True)
        else:
            self.run_worker(self.open_dropdown(name), exclusive=True)


class TabBar(Static, can_focus=True):
    """The visible tab strip under the menu bar (owner: 'idk where to
    navigate/switch tabs did u add that at all'). Four labeled tabs, the
    active one raised, hover highlight, the Gates tab carrying its badge.
    Click a tab, or focus the strip and use left/right/enter."""

    class TabPicked(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    TABS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("work", "Work"),
        ("dashboard", "Dashboard"),
        ("gates", "Gates"),
        ("library", "Library"),
    )

    def __init__(self) -> None:
        super().__init__(id="tab-bar")
        self._active = "work"
        self._hover: str | None = None
        self._gate_count = 0

    def set_active(self, name: str) -> None:
        if name != self._active:
            self._active = name
            self.refresh()

    def set_gate_count(self, count: int) -> None:
        if count != self._gate_count:
            self._gate_count = count
            self.refresh()

    def _spans(self) -> list[tuple[str, int, int]]:
        spans: list[tuple[str, int, int]] = []
        col = 1
        for name, label in self.TABS:
            width = len(label) + 2 + (4 if name == "gates" and self._gate_count else 0)
            spans.append((name, col, width))
            col += width + 1
        return spans

    def render(self) -> Any:
        out = " "
        for name, label in self.TABS:
            badge = f" ●{self._gate_count}" if (name == "gates" and self._gate_count) else ""
            text = f" {label}{badge} "
            if name == self._active:
                out += f"[reverse]{text}[/reverse]"
            elif name == self._hover:
                color = "#0178D4"
                try:
                    color = str(self.app.current_theme.primary or color)
                except Exception:  # pragma: no cover - theme shape varies
                    pass
                out += f"[on {color}]{text}[/on {color}]"
            else:
                out += f"[dim]{text}[/dim]"
            out += " "
        # a persistent way out: escape twice, or File > Quit
        out += " [dim]esc esc to quit[/dim]"
        return out

    def _tab_at(self, x: int) -> str | None:
        for name, start, width in self._spans():
            if start <= x < start + width:
                return name
        return None

    def on_mouse_move(self, event: Any) -> None:
        hover = self._tab_at(event.x)
        if hover != self._hover:
            self._hover = hover
            self.refresh()

    def on_leave(self, event: Any) -> None:
        if self._hover is not None:
            self._hover = None
            self.refresh()

    def on_click(self, event: Any) -> None:
        name = self._tab_at(event.x)
        if name is not None:
            self.post_message(self.TabPicked(name))

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("left", "prev_tab", "◀", show=False),
        Binding("right", "next_tab", "▶", show=False),
        Binding("enter", "pick", "Open", show=False),
    ]

    def _index(self) -> int:
        return [n for n, _ in self.TABS].index(self._active)

    def action_prev_tab(self) -> None:
        names = [n for n, _ in self.TABS]
        self.post_message(self.TabPicked(names[max(0, self._index() - 1)]))

    def action_next_tab(self) -> None:
        names = [n for n, _ in self.TABS]
        self.post_message(self.TabPicked(names[min(len(names) - 1, self._index() + 1)]))

    def action_pick(self) -> None:
        self.post_message(self.TabPicked(self._active))


# ---------------------------------------------------------------- shell


class Shell(Screen):
    """The whole app surface: menu bar on top, tabs under it, one live pane
    per section. Owns cross-pane actions (new mission flow, fetch, opening a
    mission deep view) and the gate badge/notifications."""

    TABS = ("work", "dashboard", "gates", "library")

    @property
    def rw(self) -> ReworkApp:
        app = self.app
        assert isinstance(app, ReworkApp)
        return app

    def __init__(self) -> None:
        super().__init__()
        self._seen_gates: set[str] = set()

    def compose(self) -> ComposeResult:
        yield MenuBar()
        yield TabBar()
        yield WorkPane(id="pane-work")
        yield DashboardPane(id="pane-dashboard")
        yield GatesPane(id="pane-gates")
        yield LibraryPane(id="pane-library")

    def on_mount(self) -> None:
        self.show_tab("work")
        # the badge poll: one place, drives badge + notifications
        self.set_interval(POLL_SECONDS, self.poll_badge)

    @on(OptionList.OptionSelected)
    async def dropdown_selected(self, event: OptionList.OptionSelected) -> None:
        """The menu dropdown mounts on this screen, so its messages land
        here (not on the MenuBar branch). Hand off when it is ours."""
        bar = self.query_one("#menu-bar", MenuBar)
        if event.option_list is not bar._dropdown:
            return
        event.stop()
        await bar.handle_selected(event)

    @on(MenuBar.MenuAction)
    async def menu_action(self, event: MenuBar.MenuAction) -> None:
        """Menu choices arrive at the SCREEN (custom messages bubble up the
        DOM and stop there; an App-level @on never sees them — the reason
        'dropdowns dont work' even once they opened)."""
        event.stop()
        result = self.rw.run_menu_action(event.action)
        if result is not None:
            self.run_worker(result, exclusive=True)

    @on(TabBar.TabPicked)
    async def tab_picked(self, event: TabBar.TabPicked) -> None:
        self.show_tab(event.name)

    def on_key(self, event: Any) -> None:
        """escape on the HOME screen quits, in two steps.

        There was no discoverable way out (owner got stuck): ctrl+q is eaten
        by macOS terminal flow control and the menus were broken. Escape is
        the key people try; the first press explains, the second exits.

        ONLY on the home screen: with a modal open (the mission deep view),
        escape must close the modal, not arm a quit (owner: 'i cant go back')."""
        if event.key != "escape":
            return
        if len(self.app.screen_stack) > 1:
            return  # a modal owns escape
        if getattr(self, "_quit_armed", False):
            self.app.exit()
            return
        self._quit_armed = True
        self.notify(
            "press escape again to quit (missions keep running) · or File > Quit",
            title="quit?",
            severity="warning",
            timeout=4,
        )
        self.set_timer(4.0, self._disarm_quit)

    def _disarm_quit(self) -> None:
        self._quit_armed = False

    def show_tab(self, name: str) -> None:
        for tab in self.TABS:
            pane = self.query_one(f"#pane-{tab}")
            pane.display = tab == name
        self.query_one("#tab-bar", TabBar).set_active(name)
        if name == "work":
            self.query_one("#prompt", Input).focus()
        elif name == "gates":
            self.query_one("#pane-gates", GatesPane).run_worker(
                self.query_one("#pane-gates", GatesPane).refresh_quiet(), exclusive=True
            )
        elif name == "dashboard":
            # a hidden pane has zero size and the log drops writes: replay the
            # selected mission's tail the moment the tab becomes visible
            dash = self.query_one("#pane-dashboard", DashboardPane)
            dash._last_event_seq = 0
            dash.run_worker(dash.stream_events(force=True), exclusive=True)

    def switch_tab(self, name: str) -> None:
        self.show_tab(name)

    # -- cross-pane actions -------------------------------------------------

    async def open_mission(self, mission_id: str) -> None:
        dash = self.query_one("#pane-dashboard", DashboardPane)
        await dash.select_mission(mission_id)
        self.show_tab("dashboard")
        self.app.push_screen(MissionDeepView(mission_id))

    async def new_mission_flow(self, objective: str = "") -> None:
        rw = self.rw
        defaults = rw._mission_defaults
        result = await self.app.push_screen(
            NewMissionModal(
                list(rw._project_cache.values()),
                objective=objective,
                defaults=defaults,
            ),
            wait_for_dismiss=True,
        )
        if not result:
            return
        rw._mission_defaults = {
            "repo": result.get("repo"),
            "repo_dir": result.get("repo_dir"),
            "project_id": result.get("project_id"),
            "plan": result.get("plan"),
            # the Work tab's pickers survive the round trip
            "model": rw._mission_defaults.get("model"),
            "effort": rw._mission_defaults.get("effort"),
        }
        self.notify("creating mission…")
        try:
            if rw._mission_defaults.get("model"):
                result["model"] = rw._mission_defaults["model"]
            if rw._mission_defaults.get("effort"):
                result["effort"] = rw._mission_defaults["effort"]
            mission = await rw.data.create_mission(**result)
            self.notify(f"mission {mission.id} created")
            dash = self.query_one("#pane-dashboard", DashboardPane)
            dash._selected_mission = mission.id
            dash._last_event_seq = 0
            await rw.load_project_cache()
            await dash.reload_missions()
            work = self.query_one("#pane-work", WorkPane)
            work.run_worker(work.reload_projects(), exclusive=True, group="work-reload")
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    async def new_project_flow(self) -> None:
        result = await self.app.push_screen(CreateProjectModal(), wait_for_dismiss=True)
        if not result:
            return
        try:
            project = await self.rw.data.create_project(**result)
            self.notify(f"project {project.name} created ({project.id})")
            await self.rw.load_project_cache()
            await self.query_one("#pane-dashboard", DashboardPane).reload_missions()
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    async def fetch_selected(self, mission_id: str | None = None) -> None:
        mid = (
            mission_id
            or await self.query_one("#pane-dashboard", DashboardPane).selected_mission_id()
        )
        if not mid:
            self.notify("select a mission first (Dashboard tab)", severity="warning")
            return
        self.notify("fetching result…")
        import asyncio

        try:
            dest, count = await asyncio.to_thread(self._fetch_sync, mid)
            self.notify(f"fetched {count} files → {dest}")
        except FileNotFoundError as exc:
            self.notify(str(exc), severity="warning")
        except Exception as exc:
            self.notify(f"fetch failed: {exc}", severity="error")

    def _fetch_sync(self, mission_id: str) -> tuple[str, int]:
        import asyncio

        return asyncio.run(self.rw.data.fetch_result(mission_id))

    # -- gate badge + notifications -------------------------------------------

    async def poll_badge(self) -> None:
        try:
            gates = await self.rw.data.open_gates()
        except Exception:
            return
        self.update_gate_badge(gates)
        # notifications: NEW gate ids since we last looked
        current = {g.id for g in gates}
        new = current - self._seen_gates
        mode = notify_mode()
        if new and mode != "none":
            for g in gates:
                if g.id in new:
                    text = f"gate {g.id} [{g.kind.value}] — {g.question[:50]}"
                    if mode in ("toast", "both"):
                        self.notify(text, title="rework needs you", severity="warning")
                    if mode in ("bell", "both"):
                        self.app.bell()
        self._seen_gates = current

    def update_gate_badge(self, gates: list[Any] | None = None) -> None:
        count = len(gates) if gates is not None else self.rw._open_gate_count
        self.query_one("#menu-bar", MenuBar).set_gate_count(count)
        tab_bar = self.query_one("#tab-bar", TabBar)
        tab_bar.set_gate_count(count)
        self.rw._open_gate_count = count


class MissionDeepView(ModalScreen[None]):
    """Full-screen deep view of one mission: a live flow strip (which role the
    mission is at), then tabs Feed / Findings / Diff / Report. Gates are
    actionable here; f fetches; esc goes back. Polls while open so the flow
    strip advances without pressing r."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back", show=True),
        Binding("tab", "next_tab", "Next tab", show=True),
        Binding("shift+tab", "prev_tab", "Previous tab", show=False),
        Binding("left", "prev_tab", "◀", show=False),
        Binding("right", "next_tab", "▶", show=False),
        Binding("a", "approve", "Approve gate", show=True),
        Binding("x", "reject", "Reject gate", show=True),
        Binding("f", "fetch", "Fetch result", show=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    TABS = ("feed", "findings", "diff", "report")

    def __init__(self, mission_id: str) -> None:
        super().__init__()
        self._mission_id = mission_id
        self._tab = 0
        self._last_event_seq = 0
        self._refreshing = False

    @property
    def rw(self) -> ReworkApp:
        app = self.app
        assert isinstance(app, ReworkApp)
        return app

    def compose(self) -> ComposeResult:
        with Vertical(id="deep-frame"):
            yield Label("", id="deep-head")
            yield Label("", id="deep-flow")
            with Horizontal(id="deep-tabs"):
                for name in self.TABS:
                    yield Label(f" {name} ", id=f"tab-{name}", classes="deep-tab")
            with VerticalScroll(id="deep-content"):
                yield SelectableLog(
                    id="deep-feed", markup=True, highlight=False, wrap=True, max_lines=900
                )
                yield Static("", id="deep-findings", markup=True)
                yield Static("", id="deep-diff", markup=False)
                yield Static("", id="deep-report", markup=False)
            yield Static("", id="deep-gates")
            yield Label(
                "[dim]esc back[/dim]  ·  tab/←→ switch tabs  ·  "
                "[dim]a[/dim] approve  [dim]x[/dim] reject  ·  [dim]f[/dim] fetch  "
                "· [dim]r[/dim] refresh",
                id="deep-foot",
            )

    def on_mount(self) -> None:
        self.show_tab(0)
        self.run_worker(self.refresh_all(force=True), exclusive=True)
        # live: the flow strip and feed advance while the view is open
        self.set_interval(POLL_SECONDS, self._poll)

    def show_tab(self, index: int) -> None:
        self._tab = index % len(self.TABS)
        content = self.query_one("#deep-content", VerticalScroll)
        for i, name in enumerate(self.TABS):
            tab = self.query_one(f"#tab-{name}", Label)
            tab.add_class("active" if i == self._tab else "-active")
            widget = content.query_one(f"#deep-{name}")
            widget.display = i == self._tab

    def action_next_tab(self) -> None:
        self.show_tab(self._tab + 1)

    def action_prev_tab(self) -> None:
        self.show_tab(self._tab - 1)

    async def _poll(self) -> None:
        if self.is_current:
            self.run_worker(self.refresh_all(), exclusive=True)

    async def refresh_all(self, force: bool = False) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            m = await self.rw.data.get_mission(self._mission_id)
            if m is None:
                self.notify(f"mission {self._mission_id} vanished", severity="error")
                self.action_back()
                return
            spend = await self.rw.data.mission_spend(self._mission_id)
            head = self.query_one("#deep-head", Label)
            head.update(
                f"[b]{m.id}[/b]  {status_rich(m.status.value)}  "
                f"[dim]${spend:.4f}[/dim]\n{m.objective}"
            )
            await self._render_flow(m)
            await self._stream_feed(force=force)
            await self._render_findings()
            await self._render_diff()
            await self._render_report()
            await self._render_gates()
        except Exception as exc:
            self.notify(f"refresh failed: {exc}", severity="error")
        finally:
            self._refreshing = False

    async def _render_flow(self, mission: Any) -> None:
        """The mission's pipeline as a left-to-right strip: one box per plan
        step with its role, mark, and attempt; the live one highlighted.

        Owner ask: 'make it easier to see/visualize and track the flow/state
        of a mission, which role it's at'. Everything here is derived from
        run rows — the store is the truth, never a local guess."""
        flow = self.query_one("#deep-flow", Label)
        runs = await self.rw.data.mission_runs(self._mission_id)
        plan = mission.plan or {}
        steps: list[tuple[str, str]] = []
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and step.get("step"):
                steps.append((str(step["step"]), str(step.get("role", ""))))
        if not steps:
            # no explicit plan steps (echo, or pre-plan missions): fall back
            # to the steps that actually ran, in first-seen order
            seen: dict[str, str] = {}
            for run in runs:
                seen.setdefault(run.step, run.role)
            steps = list(seen.items())
        if not steps:
            flow.update("[dim]no plan steps yet[/dim]")
            return

        by_step: dict[str, list[Any]] = {}
        for run in runs:
            by_step.setdefault(run.step, []).append(run)
        for group in by_step.values():
            group.sort(key=lambda r: r.attempt)

        parts: list[str] = []
        for index, (step, role) in enumerate(steps, start=1):
            group = by_step.get(step, [])
            latest = group[-1] if group else None
            status = latest.status.value if latest is not None else "pending"
            tries = f"#{latest.attempt}" if latest is not None else ""
            icon = {
                "completed": "[green]✔[/green]",
                "running": "[bold cyan]▶[/bold cyan]",
                "failed": "[red]✘[/red]",
                "pending": "[dim]·[/dim]",
                "awaiting_gate": "[yellow]‖[/yellow]",
                "cancelled": "[dim]-[/dim]",
            }.get(status, "·")
            label = f"{role or step} {tries}".strip()
            cell = f"{icon} {label}"
            if latest is not None and latest.status.value == "running":
                cell = f"[reverse]{icon} {label}[/reverse]"
            elif status == "failed":
                cell = f"[red]{icon} {label}[/red]"
            parts.append(f"[dim]{index}[/dim] {cell}")
        flow.update("  [dim]→[/dim]  ".join(parts))

    async def _stream_feed(self, force: bool = False) -> None:
        log = self.query_one("#deep-feed", SelectableLog)
        events = await self.rw.data.events_tail(
            self._mission_id, 0 if force else self._last_event_seq
        )
        if not events:
            return
        runs = {r.id: r for r in await self.rw.data.mission_runs(self._mission_id)}
        if force:
            log.clear()
            events = events[-EVENT_TAIL:]
        for event in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
            role = runs[event.run_id].step if event.run_id in runs else ""
            detail = ""
            payload = event.payload or {}
            if payload.get("note"):
                detail = str(payload["note"])
            elif payload.get("error"):
                detail = f"[red]{payload['error']}[/red]"
            elif payload.get("question"):
                detail = str(payload["question"])
            elif event.type.value == "mission_created":
                detail = str(payload.get("objective", ""))[:100]
            log.write(f"[dim]{stamp}[/dim] {event.type.value:<15} {role} {detail}".rstrip())
            self._last_event_seq = max(self._last_event_seq, event.seq or 0)

    async def _render_findings(self) -> None:
        runs = await self.rw.data.mission_runs(self._mission_id)
        out: list[str] = []
        for run in runs:
            artifact = run.artifact or {}
            findings = artifact.get("findings") or []
            if not findings:
                continue
            out.append(f"[b]{run.step}[/b] [dim](attempt {run.attempt})[/dim]")
            for finding in findings:
                if isinstance(finding, dict):
                    sev = str(finding.get("severity", "info"))
                    title = finding.get("title") or finding.get("summary") or "?"
                    detail = finding.get("detail") or finding.get("description") or ""
                    color = {"high": "red", "medium": "yellow"}.get(sev, "cyan")
                    out.append(f"  [{color}]({sev})[/{color}] {title}")
                    if detail:
                        out.append(f"      {detail}")
                else:
                    out.append(f"  {finding}")
            out.append("")
        widget = self.query_one("#deep-findings", Static)
        widget.update("\n".join(out) if out else "[dim]no findings recorded[/dim]")

    async def _render_diff(self) -> None:
        diff = ""
        for run in await self.rw.data.mission_runs(self._mission_id):
            artifact = run.artifact or {}
            if artifact.get("diff"):
                diff = str(artifact["diff"])
        widget = self.query_one("#deep-diff", Static)
        widget.update(
            diff if diff else "[dim]no diff (echo plans and pure investigations have none)[/dim]"
        )

    async def _render_report(self) -> None:
        runs = await self.rw.data.mission_runs(self._mission_id)
        from rework.cli import _render_md

        prepared = []
        for run in runs:
            artifact = dict(run.artifact or {})
            if artifact and "run_stats" not in artifact:
                from rework.nodes.run_stats import attach_run_stats

                artifact = await attach_run_stats(await self.rw.data.store(), run.id, artifact)
            prepared.append((run, artifact))
        spend = await self.rw.data.mission_spend(self._mission_id)
        mission = await self.rw.data.get_mission(self._mission_id)
        text = _render_md(prepared, spend, mission) if mission else "(mission vanished)"
        self.query_one("#deep-report", Static).update(text)

    async def _render_gates(self) -> None:
        box = self.query_one("#deep-gates", Static)
        gates = await self.rw.data.open_gates(self._mission_id)
        if not gates:
            box.update("")
            return
        lines = ["[bold yellow]WAITING ON YOU[/bold yellow]"]
        for gate in gates:
            lines.append(f"  {gate.id} [{gate.kind.value}]  {gate.question}")
        lines.append("  [a] approve    [x] reject")
        box.update("\n".join(lines))

    async def action_approve(self) -> None:
        await self._decide(True)

    async def action_reject(self) -> None:
        await self._decide(False)

    async def _decide(self, approved: bool) -> None:
        gates = await self.rw.data.open_gates(self._mission_id)
        if not gates:
            self.notify("no open gate on this mission")
            return
        for gate in gates:
            ok = await self.rw.data.decide_gate(gate.id, approved)
            if ok:
                self.notify(f"gate {gate.id} {'approved' if approved else 'rejected'}")
        self.run_worker(self.refresh_all(), exclusive=True)

    async def action_fetch(self) -> None:
        self.notify("fetching result…")
        import asyncio

        try:
            dest, count = await asyncio.to_thread(self._fetch_sync)
            self.notify(f"fetched {count} files → {dest}")
        except FileNotFoundError as exc:
            self.notify(str(exc), severity="warning")
        except Exception as exc:
            self.notify(f"fetch failed: {exc}", severity="error")

    def _fetch_sync(self) -> tuple[str, int]:
        import asyncio

        return asyncio.run(self.rw.data.fetch_result(self._mission_id))

    async def action_refresh(self) -> None:
        self.run_worker(self.refresh_all(force=True), exclusive=True)

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------- app


class ReworkCommands(Provider):
    """The palette (ctrl+p): every action findable by typing."""

    async def search(self, query: str) -> Hits:
        actions: list[tuple[str, str, str]] = [
            ("work tab", "type your work (alt+1)", "work"),
            ("dashboard", "missions + projects (alt+2)", "dashboard"),
            ("gates", "human decisions (alt+3)", "gates"),
            ("library", "what missions learned (alt+4)", "library"),
            ("new mission", "launch a mission", "new_mission"),
            ("new project", "create a project label", "new_project"),
        ]
        matcher = self.matcher(query)
        node: Any = self.screen
        while node is not None and not isinstance(node, Shell):
            node = getattr(node, "parent", None)
        shell = node if isinstance(node, Shell) else None
        if shell is None:
            return
        for title, sub, action in actions:
            score = matcher.match(title)
            if score > 0:

                async def run(action: str = action) -> None:
                    if action in ("work", "dashboard", "gates", "library"):
                        shell.switch_tab(action)
                    elif action == "new_mission":
                        shell.run_worker(shell.new_mission_flow(), exclusive=True)
                    elif action == "new_project":
                        shell.run_worker(shell.new_project_flow(), exclusive=True)

                yield Hit(score, f"[b]{title}[/b] — {sub}", run, help=sub)


class ReworkApp(App[None]):
    TITLE = "rework"
    SUB_TITLE = "missions on the daemon"
    COMMANDS = App.COMMANDS | {ReworkCommands}

    CSS = """
    #menu-bar { height: 1; background: $surface-darken-1; color: $text; padding: 0 1; }
    #tab-bar { height: 1; background: $surface; padding: 0; }
    #menu-bar > .menu-label { }
    #menu-dropdown { border: round $accent; background: $surface;
                     max-height: 12; width: 34; }
    #pane-work { height: 1fr; padding: 1 2; }
    #pane-work #prompt { border: none; background: transparent;
                         border-bottom: round $accent; height: 3; }
    #work-settings { height: 3; }
    .work-knob { min-width: 16; margin-right: 1; height: 3; }
    #work-hint { height: 1; color: $text-muted; }
    #pane-dashboard { height: 1fr; padding: 0 1; }
    #pane-gates { height: 1fr; padding: 0 1; }
    #pane-library { height: 1fr; padding: 0 1; }
    #project-band { height: 3; border-bottom: round $panel; padding: 0 1;
                    background: $surface-darken-1; }
    #mid-title { height: 1; padding: 0 1; color: $text-muted; }
    #missions { height: 1fr; }
    #detail { height: 8; border-top: round $panel; padding: 0 1; }
    #detail #events { height: 1fr; border: none; }
    #mission-head { height: 1; color: $text-muted; }
    #gate-box { height: auto; padding: 0 1; }
    #modal-form { width: 72; max-width: 90%; height: auto; max-height: 90%;
                  border: round $accent; padding: 1 2; background: $surface; }
    #modal-actions { height: auto; padding-top: 1; }
    #lib-split { height: 1fr; }
    #lib-list-pane { width: 2fr; min-width: 30; border-right: round $panel; padding: 0 1; }
    #lib-table { height: 1fr; }
    #lib-counts { height: 1; }
    #lib-body-pane { width: 3fr; padding: 1 2; }
    #lib-body { padding: 0 1; }
    #lib-query { border: none; background: transparent; border-bottom: round $accent; }
    /* the deep view is a full SCREEN: it must paint its own opaque
       background, or the pane underneath shows through and the window
       'falls apart' (owner's screenshot) */
    MissionDeepView { background: $surface; }
    #deep-frame { height: 1fr; background: $surface; padding: 0 1; }
    #deep-head { height: auto; padding: 0 1; background: $surface; }
    #deep-flow { height: auto; padding: 0 1; background: $surface; }
    #deep-tabs { height: 1; background: $surface; }
    .deep-tab { color: $text-muted; padding: 0 2; }
    .deep-tab.active { color: $text; background: $panel; text-style: bold; }
    #deep-content { height: 1fr; border-top: round $panel; padding: 0 1;
                    background: $surface; }
    #deep-feed { height: auto; max-height: 100%; }
    #deep-gates { height: auto; padding: 0 1; background: $surface; }
    #deep-foot { height: 1; color: $text-muted; background: $surface; padding: 0 1; }
    #gates-table { height: 1fr; }
    """

    # ctrl+c is COPY (textual's selection clipboard). Quit has to work from
    # anywhere without stealing copy: File > Quit, or pressing escape twice on
    # the home screen. ctrl+q is kept as an alias but ISN'T relied on — macOS
    # terminals treat it as XON flow control and swallow it (owner got stuck
    # with no visible way out).
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("f10", "quit", "Quit", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+c", "copy_text", "Copy", show=False, priority=True),
        Binding("alt+1", "tab_work", "Work", show=False),
        Binding("alt+2", "tab_dashboard", "Dashboard", show=False),
        Binding("alt+3", "tab_gates", "Gates", show=False),
        Binding("alt+4", "tab_library", "Library", show=False),
        Binding("a", "approve_gate", "Approve", show=False),
        Binding("x", "reject_gate", "Reject", show=False),
    ]

    def action_quit(self) -> Any:
        """Quit from anywhere. Missions live in the store, not this process:
        anything running keeps running on the daemon."""
        self.exit()

    def action_copy_text(self) -> None:
        """Copy the current pane's selection (textual tracks it); fall back to
        a helpful hint when nothing is selected."""
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.notify(f"copied {len(selected)} chars to the clipboard")
        else:
            self.notify("drag over text to select it first, then ctrl+c", severity="warning")

    def action_select_all_text(self) -> None:
        """Select everything in the focused widget (double-click also works)."""
        focused = self.focused
        if focused is not None and hasattr(focused, "text_select_all"):
            focused.text_select_all()
            self.notify("selected the visible pane text — ctrl+c to copy")
        else:
            self.notify("nothing selectable here", severity="warning")

    def __init__(self) -> None:
        super().__init__()
        self.data = TuiData()
        self._project_cache: dict[str, Any] = {}
        self._mission_cache: dict[str, Any] = {}
        self._open_gate_count = 0
        self._mission_defaults: dict[str, Any] = {}

    def get_default_screen(self) -> Any:
        return Shell()

    async def on_unmount(self) -> None:
        await self.data.aclose()

    # -- tab bindings (app-level so they work from any pane) -----------------

    def action_tab_work(self) -> None:
        self._switch("work")

    def action_tab_dashboard(self) -> None:
        self._switch("dashboard")

    def action_tab_gates(self) -> None:
        self._switch("gates")

    def action_tab_library(self) -> None:
        self._switch("library")

    def _switch(self, name: str) -> None:
        screen = self.screen
        node: Any = screen
        while node is not None and not isinstance(node, Shell):
            node = getattr(node, "parent", None)
        if isinstance(node, Shell):
            node.switch_tab(name)

    # -- cross-screen helpers ----------------------------------------------

    async def load_project_cache(self) -> None:
        self._project_cache = {p.id: p for p in await self.data.projects()}

    def set_gates_badge(self, gates: list[Any]) -> None:
        self._open_gate_count = len(gates)
        self.sub_title = f"{self._open_gate_count} gate(s) waiting on you" if gates else "all quiet"
        shell = self._find_shell()
        if shell is not None:
            shell.update_gate_badge(gates)

    def _find_shell(self) -> Shell | None:
        node: Any = self.screen
        while node is not None and not isinstance(node, Shell):
            node = getattr(node, "parent", None)
        return node if isinstance(node, Shell) else None

    def refresh_dashboard(self) -> None:
        shell = self._find_shell()
        if shell is not None:
            dash = shell.query_one("#pane-dashboard", DashboardPane)
            dash.run_worker(dash.tick(), exclusive=True)

    # -- menu actions (from MenuBar.MenuAction) ------------------------------

    def run_menu_action(self, action: str) -> Any:
        """Dispatch a MenuBar choice. Returns the coroutine for async work so
        the Shell (which owns the widgets) can schedule it as a worker."""
        shell = self._find_shell()
        if action == "quit":
            self.exit()
        elif action == "new_mission" and shell:
            return shell.new_mission_flow()
        elif action == "new_project" and shell:
            return shell.new_project_flow()
        elif action == "fetch" and shell:
            return shell.fetch_selected()
        elif action.startswith("tab_") and shell:
            shell.switch_tab(action[4:])
        elif action == "keys":
            self.notify(
                "drag to select · ctrl+c copies · alt+1..4 tabs · a/x decide · f fetch"
                " · quit: escape twice, F10, or File > Quit",
                title="keys",
            )
        elif action == "copy_help":
            self.notify(
                "drag over any text to select it (shift+drag if a table has focus), "
                "then ctrl+c to copy. Or double-click a line, triple-click a pane.",
                title="copying text",
            )
        elif action == "about":
            from rework import __version__

            self.notify(
                f"rework {__version__} — portable multi-agent workflow engine", title="about"
            )
        elif action == "copy":
            self.action_copy_text()
        elif action == "select_all":
            self.action_select_all_text()
        return None

    # -- slash commands ------------------------------------------------------

    async def run_slash(self, text: str) -> None:
        """`/cmd args` from the prompt. Unknown commands say so."""
        cmd, _, rest = text.partition(" ")
        rest = rest.strip()
        shell = self._find_shell()
        if cmd == "/gates":
            self._switch("gates")
        elif cmd == "/library":
            self._switch("library")
        elif cmd == "/projects":
            if shell:
                shell.run_worker(shell.new_project_flow(), exclusive=True)
        elif cmd == "/new":
            if shell:
                shell.run_worker(shell.new_mission_flow(objective=rest), exclusive=True)
        elif cmd == "/fetch":
            if shell and rest:
                await shell.fetch_selected(rest)
            else:
                self.notify("usage: /fetch <mission-id>", severity="warning")
        else:
            from rework.tui.app import SLASH_COMMANDS

            known = "  ".join(c for c, _ in SLASH_COMMANDS)
            self.notify(f"unknown command {cmd}. try: {known}", severity="warning")

    def run_command(self, cmd: str) -> None:  # pragma: no cover - unused
        return


# ProjectBand is referenced by DashboardPane above; defined here at module end
# would be too late — see the class defined before Shell instead.


SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/gates", "review and decide open gates"),
    ("/library", "search what missions learned"),
    ("/fetch", "fetch a finished mission's result: /fetch <mission-id>"),
    ("/new", "start a mission wizard: /new <objective>"),
    ("/projects", "create a project label"),
)


def run_tui() -> None:
    """The `rework` entry (no subcommand)."""
    app = ReworkApp()
    app.run()
