"""The rework TUI: `rework` with no arguments opens this.

Full-screen dashboard (projects / missions / live detail), gates screen,
library browser, new-mission wizard, create-project modal, and a command
palette (ctrl+p) for finding every action. Mouse and keyboard both work;
layout is responsive and survives terminal resizes (Textual's layout engine
recomputes on Resize events; heights/widths are fractions with mins, never
absolute pixel counts).

Quality rules this file tries to hold (owner bar: Claude-Code-grade):
- No blocking calls on the UI loop: store/IO via the async data layer;
  long sync work (folder packing) already runs in threads below it.
- Polling never overlaps itself (one in-flight refresh guard).
- Every destructive action (approve/reject) is explicit (a/x on a visible
  gate; never a bare Enter).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, HorizontalGroup, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    TextArea,
)

from rework.tui.data import TuiData, human_age, status_rich

POLL_SECONDS = 2.0
EVENT_TAIL = 40


class ReworkScreen(Screen):
    """Every screen here runs under ReworkApp; this gives typed access."""

    @property
    def rw(self) -> ReworkApp:
        app = self.app
        assert isinstance(app, ReworkApp)
        return app


# ---------------------------------------------------------------- screens


class NewMissionModal(ModalScreen[dict[str, Any]]):
    """Guided new-mission form: project, repo (URL or local path) OR a local
    folder to upload, objective, plan."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Create", show=True),
    ]

    def __init__(self, projects: list[Any]) -> None:
        super().__init__()
        self._projects = projects

    def compose(self) -> ComposeResult:
        choices = [("(no project)", "__none__")] + [
            (f"{p.name} ({p.id})", p.id) for p in self._projects
        ]
        with Vertical(id="modal-form"):
            yield Label("[b]New mission[/b]")
            yield Label("Objective")
            yield TextArea(id="f-objective", soft_wrap=True)
            yield Label("Repo (git URL or local path; optional when a folder is given)")
            yield Input(id="f-repo", placeholder="https://github.com/you/repo.git")
            yield Label("Local folder of working files to upload (non-git)")
            yield Input(id="f-repodir", placeholder="~/Documents/some-folder")
            yield Label("Plan")
            yield Select(
                [
                    ("echo (no roles)", "echo"),
                    ("fix: investigate > code > audit + ship gate", "fix"),
                    ("fix-pr: like fix, opens a PR", "fix-pr"),
                ],
                value="fix",
                id="f-plan",
                allow_blank=False,
            )
            yield Label("Project")
            yield Select(choices, value="__none__", id="f-project", allow_blank=False)
            with HorizontalGroup(id="modal-actions"):
                yield Button("Create (ctrl+s)", id="btn-create", variant="success")
                yield Button("Cancel (esc)", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#f-objective", TextArea).focus()

    @on(Button.Pressed, "#btn-cancel")
    def cancel_btn(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-create")
    def create_btn(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        objective = self.query_one("#f-objective", TextArea).text.strip()
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
        self.dismiss(
            {
                "objective": objective,
                "repo": repo,
                "repo_dir": repo_dir,
                "project_id": project_id,
                "plan": str(plan),
            }
        )


class CreateProjectModal(ModalScreen[dict[str, Any]]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Create", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-form"):
            yield Label("[b]New project[/b] (a label that groups missions)")
            yield Label("Name")
            yield Input(id="p-name", placeholder="capstone")
            yield Label("Transport")
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
                yield Button("Create (ctrl+s)", id="btn-create", variant="success")
                yield Button("Cancel (esc)", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#p-name", Input).focus()

    @on(Button.Pressed, "#btn-cancel")
    def cancel_btn(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-create")
    def create_btn(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        name = self.query_one("#p-name", Input).value.strip()
        if not name:
            self.notify("Project name is required", severity="error")
            return
        transport = str(self.query_one("#p-transport", Select).value)
        self.dismiss({"name": name, "transport": transport})


class GatesScreen(ReworkScreen):
    """Every open gate across missions; a approves, x rejects the selected."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back", show=True),
        Binding("a", "approve", "Approve", show=True),
        Binding("x", "reject", "Reject", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("[b]Open gates[/b] — the human decisions rework is waiting on")
        yield DataTable(id="gates-table")
        yield Label(id="gates-empty")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#gates-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("gate", "mission", "kind", "question")
        self.call_after_refresh(self.reload)

    async def reload(self) -> None:
        table = self.query_one("#gates-table", DataTable)
        table.clear()
        gates = await self.rw.data.open_gates()
        for gate in gates:
            table.add_row(
                gate.id, gate.mission_id, gate.kind.value, gate.question[:70], key=gate.id
            )
        empty = self.query_one("#gates-empty", Label)
        empty.display = not gates
        if not gates:
            empty.update("Nothing waiting on you. Enjoy the silence.")

    def action_refresh(self) -> None:
        self.run_worker(self.reload(), exclusive=True)

    def _selected_gate_id(self) -> str | None:
        table = self.query_one("#gates-table", DataTable)
        key = table.cursor_row
        if key is None or table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(key)[0])
        except Exception:
            return None

    async def _decide(self, approved: bool) -> None:
        gate_id = self._selected_gate_id()
        if gate_id is None:
            return
        ok = await self.rw.data.decide_gate(gate_id, approved)
        if ok:
            self.notify(f"gate {gate_id} {'approved' if approved else 'rejected'}")
            await self.reload()
            self.rw.refresh_dashboard()

    def action_approve(self) -> None:
        self.run_worker(self._decide(True), exclusive=True)

    def action_reject(self) -> None:
        self.run_worker(self._decide(False), exclusive=True)

    def action_back(self) -> None:
        self.rw.refresh_dashboard()
        self.app.pop_screen()


class LibraryScreen(ReworkScreen):
    """Search the Library, read bodies, see what missions contributed."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back", show=True),
        Binding("enter", "read", "Read body", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("[b]Library[/b] — skills/reports/memories that missions learned")
        yield Input(
            placeholder="search (name, description, tags); empty = everything", id="lib-query"
        )
        yield DataTable(id="lib-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#lib-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "type", "status", "name", "description")
        self.query_one("#lib-query", Input).focus()
        self.run_worker(self.reload(""), exclusive=True)

    @on(Input.Submitted, "#lib-query")
    async def query_submitted(self, event: Input.Submitted) -> None:
        await self.reload(event.value)

    async def reload(self, query: str) -> None:
        table = self.query_one("#lib-table", DataTable)
        table.clear()
        for item in await self.rw.data.library_search(query):
            table.add_row(
                item.id,
                item.type.value,
                item.status.value,
                item.name,
                item.description[:60],
                key=item.id,
            )

    def _selected_item_id(self) -> str | None:
        table = self.query_one("#lib-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(table.cursor_row)[0])
        except Exception:
            return None

    async def action_read(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        item, body = await self.rw.data.library_body(item_id)
        if item is None:
            self.notify(f"no item {item_id}", severity="error")
            return
        text = body if body is not None else "(no body file on this machine)"
        header = f"{item.id}  [{item.type.value}/{item.status.value}]  {item.name}"
        self.app.push_screen(BodyViewerModal(header, text))

    def action_back(self) -> None:
        self.app.pop_screen()


class BodyViewerModal(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True)
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="body-viewer"):
            yield Label(f"[b]{self._title}[/b]")
            with VerticalScroll():
                yield Static(self._body, id="body-text")
            yield Label("esc to close")

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- dashboard


class Dashboard(ReworkScreen):
    """Projects sidebar + missions table + live detail pane for the selection."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("n", "new_mission", "New mission", show=True),
        Binding("p", "new_project", "New project", show=True),
        Binding("g", "gates", "Gates", show=True),
        Binding("l", "library", "Library", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selected_mission: str | None = None
        self._selected_project: str | None = None  # None = all
        self._last_event_seq: int = 0
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Label("[b]Projects[/b]")
                yield ListView(id="projects")
                yield Label("", id="sidebar-hint")
            with Vertical(id="mid"):
                yield DataTable(id="missions")
        with Vertical(id="detail"):
            yield Label("[b]Select a mission[/b]", id="mission-head")
            yield RichLog(id="events", markup=True, highlight=False, wrap=True, max_lines=600)
            yield Static("", id="gate-box")
        yield Footer()

    def on_mount(self) -> None:
        missions = self.query_one("#missions", DataTable)
        missions.cursor_type = "row"
        missions.add_columns("mission", "status", "plan", "project", "age")
        projects = self.query_one("#projects", ListView)
        projects.append(ListItem(Label("[b]all missions[/b]"), name="__all__"))
        self.set_interval(POLL_SECONDS, self.tick)
        self.run_worker(self.initial_load(), exclusive=True)

    async def initial_load(self) -> None:
        await self.rw.load_project_cache()
        await self.reload_projects()
        await self.reload_missions()

    # -- polling -----------------------------------------------------------

    async def tick(self) -> None:
        if self._refreshing or not self.is_current:
            return
        self._refreshing = True
        try:
            await self.reload_missions()
            await self.stream_events()
        except Exception as exc:  # store hiccups must not kill the UI
            self.notify(f"refresh failed: {exc}", severity="error")
        finally:
            self._refreshing = False

    # -- data ---------------------------------------------------------------

    async def reload_projects(self) -> None:
        projects_list = self.query_one("#projects", ListView)
        projects_list.clear()
        projects_list.append(ListItem(Label("[b]all missions[/b]"), name="__all__"))
        for project in self.rw._project_cache.values():
            projects_list.append(ListItem(Label(project.name), name=project.id))
        hint = self.query_one("#sidebar-hint", Label)
        count = len(self.rw._project_cache)
        hint.update(f"{count} project{'s' if count != 1 else ''} · p to create")

    @on(ListView.Selected, "#projects")
    async def project_selected(self, event: ListView.Selected) -> None:
        name = event.item.name or "__all__"
        self._selected_project = None if name == "__all__" else str(name)
        await self.reload_missions()

    async def reload_missions(self) -> None:
        table = self.query_one("#missions", DataTable)
        cursor_before = table.cursor_row
        selected_id = self._selected_mission
        table.clear()
        projects = {p.id: p.name for p in self.rw._project_cache.values()}
        missions = await self.rw.data.missions()
        self.rw._mission_cache = {m.id: m for m in missions}
        shown = 0
        for mission in missions:
            if self._selected_project and mission.project_id != self._selected_project:
                continue
            table.add_row(
                mission.id,
                status_rich(mission.status.value),
                (mission.plan or {}).get("kind", "echo"),
                projects.get(mission.project_id or "", "·"),
                human_age(mission.created_at),
                key=mission.id,
            )
            shown += 1
        if selected_id is not None:
            for row, key in enumerate(table.rows):
                if str(key.value) == selected_id:
                    table.move_cursor(row=row)
                    break
        _ = cursor_before
        self.rw.set_gates_badge(await self.rw.data.open_gates())

    @on(DataTable.RowSelected, "#missions")
    async def mission_selected(self, event: DataTable.RowSelected) -> None:
        await self.select_mission(str(event.row_key.value))

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
        log = self.query_one("#events", RichLog)
        log.clear()
        await self.stream_events(force=True)

    async def stream_events(self, force: bool = False) -> None:
        mission_id = self._selected_mission
        if mission_id is None:
            return
        events = await self.rw.data.events_tail(mission_id, 0 if force else self._last_event_seq)
        if not events:
            return
        runs = {r.id: r for r in await self.rw.data.mission_runs(mission_id)}
        log = self.query_one("#events", RichLog)
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
            log.write(f"[dim]{stamp}[/dim] {event.type.value:<16} {role} {detail}")
            self._last_event_seq = max(self._last_event_seq, event.seq or 0)
        m = self.rw._mission_cache.get(mission_id)
        if m is not None:
            spend = await self.rw.data.mission_spend(mission_id)
            head.update(
                f"[b]{m.id}[/b] {status_rich(m.status.value)}  "
                f"[dim]{(m.objective or '')[:90]}[/dim]  ${spend:.4f}"
            )
        await self.render_gate_box(mission_id)

    async def render_gate_box(self, mission_id: str) -> None:
        box = self.query_one("#gate-box", Static)
        gates = await self.rw.data.open_gates(mission_id)
        if not gates:
            box.update("")
            return
        lines = ["[bold yellow]WAITING ON YOU[/bold yellow]"]
        for gate in gates:
            lines.append(f"  gate {gate.id} [{gate.kind.value}]  {gate.question}")
        lines.append("  [a] approve    [x] reject")
        box.update("\n".join(lines))

    # -- actions -------------------------------------------------------------

    async def action_new_mission(self) -> None:
        result = await self.app.push_screen(
            NewMissionModal(list(self.rw._project_cache.values())), wait_for_dismiss=True
        )
        if not result:
            return
        self.notify("creating mission…")
        try:
            mission = await self.rw.data.create_mission(**result)
            self.notify(f"mission {mission.id} created")
            self._selected_mission = mission.id
            self._last_event_seq = 0
            await self.reload_projects()
            await self.reload_missions()
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    async def action_new_project(self) -> None:
        result = await self.app.push_screen(CreateProjectModal(), wait_for_dismiss=True)
        if not result:
            return
        try:
            project = await self.rw.data.create_project(**result)
            self.notify(f"project {project.name} created ({project.id})")
            await self.reload_projects()
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    def action_gates(self) -> None:
        self.app.push_screen(GatesScreen())

    def action_library(self) -> None:
        self.app.push_screen(LibraryScreen())

    def action_refresh(self) -> None:
        self.run_worker(self.tick(), exclusive=True)

    async def action_approve_gate(self) -> None:
        await self._decide_selected(True)

    async def action_reject_gate(self) -> None:
        await self._decide_selected(False)

    async def _decide_selected(self, approved: bool) -> None:
        mission_id = self._selected_mission
        if mission_id is None:
            self.notify("select a mission first", severity="warning")
            return
        gates = await self.rw.data.open_gates(mission_id)
        if not gates:
            self.notify("no open gate on this mission")
            return
        for gate in gates:
            ok = await self.rw.data.decide_gate(gate.id, approved)
            if ok:
                self.notify(f"gate {gate.id} {'approved' if approved else 'rejected'}")
        await self.stream_events(force=True)
        await self.reload_missions()


# ---------------------------------------------------------------- app


class ReworkCommands(Provider):
    """The palette (ctrl+p): every action findable by typing."""

    async def search(self, query: str) -> Hits:

        actions: list[tuple[str, str, Any]] = [
            ("new mission", "launch a mission (n)", "new_mission"),
            ("new project", "create a project label (p)", "new_project"),
            ("gates", "open the human-decision queue (g)", "gates"),
            ("library", "search what missions learned (l)", "library"),
            ("refresh", "poll the store right now (r)", "refresh"),
        ]
        matcher = self.matcher(query)
        dashboard = self.screen if isinstance(self.screen, Dashboard) else None
        if dashboard is None:
            return
        for title, sub, method in actions:
            score = matcher.match(title)
            if score > 0:
                yield Hit(
                    score,
                    f"[b]{title}[/b] — {sub}",
                    getattr(dashboard, f"action_{method}"),
                    help=sub,
                )


def rework_commands_provider(screen: Screen) -> Provider:
    return ReworkCommands(screen)


class ReworkApp(App[None]):
    TITLE = "rework"
    SUB_TITLE = "missions on the daemon"
    # textual's own default provider is also a (Screen) -> Provider callable;
    # the stub types COMMANDS more strictly than the runtime accepts
    COMMANDS = App.COMMANDS | {rework_commands_provider}  # type: ignore[arg-type]

    CSS = """
    #main { height: 3fr; }
    #sidebar { width: 32; min-width: 20; border-right: round $panel; padding: 0 1; }
    #mid { width: 1fr; }
    #missions { height: 1fr; }
    #detail { height: 2fr; border-top: round $panel; padding: 0 1; }
    #events { height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    #gate-box { height: auto; padding: 0 1; }
    #modal-form { width: 72; max-width: 90%; height: auto; max-height: 90%;
                  border: round $accent; padding: 1 2; background: $surface; }
    #modal-actions { height: auto; padding-top: 1; }
    #body-viewer { width: 90%; height: 90%; border: round $accent;
                   padding: 1 2; background: $surface; }
    #projects { height: 1fr; }
    .label-head { margin-top: 1; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("a", "approve_gate", "Approve", show=False),
        Binding("x", "reject_gate", "Reject", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data = TuiData()
        self._project_cache: dict[str, Any] = {}
        self._mission_cache: dict[str, Any] = {}
        self._open_gate_count = 0

    def on_mount(self) -> None:
        self.install_screen(Dashboard(), name="dashboard")
        self.push_screen("dashboard")

    async def on_unmount(self) -> None:
        await self.data.aclose()

    # -- cross-screen helpers ----------------------------------------------

    async def load_project_cache(self) -> None:
        self._project_cache = {p.id: p for p in await self.data.projects()}

    def set_gates_badge(self, gates: list[Any]) -> None:
        self._open_gate_count = len(gates)
        self.sub_title = f"{self._open_gate_count} gate(s) waiting on you" if gates else "all quiet"

    def refresh_dashboard(self) -> None:
        dashboard = self.get_screen("dashboard")
        dashboard.run_worker(dashboard.tick(), exclusive=True)  # type: ignore[attr-defined]

    # palette actions live on the Dashboard screen; the app delegates
    def call_method(self, method: str) -> Any:
        dashboard = (
            self.screen if isinstance(self.screen, Dashboard) else self.get_screen("dashboard")
        )

        async def run() -> None:
            await getattr(dashboard, f"action_{method}")()

        return run


def run_tui() -> None:
    """The `rework` entry (no subcommand)."""
    app = ReworkApp()
    app.run()
