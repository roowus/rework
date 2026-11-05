"""The rework TUI: `rework` with no arguments opens this.

The 2026-11-05 redesign (owner-grilled) turns the dashboard into a COCKPIT:
a persistent prompt bar along the bottom drives everything. Free text +
enter opens the pre-filled mission wizard; `/` offers commands. Enter on a
mission opens a full-screen deep view (Feed / Findings / Diff / Report tabs).
The library is search-first: cursor lands in the search box, list left,
rendered body right. One navigation rule everywhere:

    enter = deeper   esc = back   arrows = move   a/x = gate   f = fetch

Visible artifacts land in ~/rework (results/, reports/); ~/.rework stays
hidden machinery. Bare `rework` opens blank: nothing auto-opens, nothing
auto-resumes (owner decision); running missions are simply at the top of
the table and enter resumes.

Quality rules this file tries to hold (owner bar: Claude-Code-grade):
- No blocking calls on the UI loop: store/IO via the async data layer;
  long sync work (folder packing, tar extraction) runs in threads.
- Polling never overlaps itself (one in-flight refresh guard).
- Every destructive action (approve/reject) is explicit (a/x on a visible
  gate; never a bare Enter).
- push_screen(wait_for_dismiss=True) only inside run_worker (a keypress
  handler is not a worker; found live as a crash).
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
)

from rework.tui.data import TuiData, human_age, status_rich

POLL_SECONDS = 2.0
EVENT_TAIL = 60


class ReworkScreen(Screen):
    """Every screen here runs under ReworkApp; this gives typed access."""

    @property
    def rw(self) -> ReworkApp:
        app = self.app
        assert isinstance(app, ReworkApp)
        return app


# ---------------------------------------------------------------- modals


class NewMissionModal(ModalScreen[dict[str, Any]]):
    """Guided new-mission form: project, repo (URL or local path) OR a local
    folder to upload, objective, plan. Opened by the prompt bar with the
    objective pre-filled and last-used repo/project/plan remembered."""

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
        self.dismiss(None)

    @on(Button.Pressed, "#btn-create")
    def create_btn(self) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

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


# ---------------------------------------------------------------- gates


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


# ---------------------------------------------------------------- library


class LibraryScreen(ReworkScreen):
    """Search-first mail-client split: the cursor lands in the search box;
    typing filters live (hybrid when embeddings are configured, keyword
    otherwise); list grouped by type on the left, rendered body on the right.
    Built to stack up: the search box is the primary surface, not the list."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back", show=True),
        Binding("enter", "read", "Read", show=True),
        Binding("a", "activate", "Activate", show=True),
        Binding("d", "retire", "Retire", show=True),
        Binding("down", "focus_list", "List", show=False),
        Binding("up", "focus_search", "Search", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(
            placeholder="search the library… (type to filter, enter to read, esc back)",
            id="lib-query",
        )
        with Horizontal(id="lib-split"):
            with Vertical(id="lib-list-pane"):
                yield Static("", id="lib-counts")
                yield DataTable(id="lib-table")
            with VerticalScroll(id="lib-body-pane"):
                yield Static("", id="lib-body", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#lib-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("type", "status", "name")
        self.query_one("#lib-query", Input).focus()
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
            items = await self.rw.data.library_hybrid_search(query)
        else:
            items = await self.rw.data.library_search(query)
        self._items = items
        table = self.query_one("#lib-table", DataTable)
        cursor_before = table.cursor_row
        table.clear()
        self._row_ids = []
        for item in items:
            table.add_row(item.type.value, item.status.value, item.name, key=item.id)
            self._row_ids.append(item.id)
        # counts by type, the one-line orientation
        counts: dict[str, int] = {}
        for item in items:
            counts[item.type.value] = counts.get(item.type.value, 0) + 1
        summary = "  ".join(f"{k}s {v}" for k, v in sorted(counts.items())) or "empty"
        self.query_one("#lib-counts", Static).update(f"[dim]{summary}[/dim]")
        if items and 0 <= cursor_before < len(items):
            table.move_cursor(row=cursor_before)
            await self.render_body(self._row_ids[cursor_before])

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
        item, body = await self.rw.data.library_body(item_id)
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
        # the split already shows the body; enter just proves the path and
        # refreshes it (body may have changed on disk)
        item_id = self._selected_item_id()
        if item_id is not None:
            await self.render_body(item_id)

    async def _status_change(self, retire: bool) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        ok = await self.rw.data.library_set_status(item_id, retire)
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

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------- deep view


class MissionDeepView(ReworkScreen):
    """Full-screen deep view of one mission (enter from the dashboard): tabs
    Feed / Findings / Diff / Report. Gates are actionable here; f fetches
    the result to ~/rework/results/<id>/. esc returns to the dashboard."""

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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("", id="deep-head")
        with Horizontal(id="deep-tabs"):
            for name in self.TABS:
                yield Label(f" {name} ", id=f"tab-{name}", classes="deep-tab")
        with VerticalScroll(id="deep-content"):
            yield RichLog(id="deep-feed", markup=True, highlight=False, wrap=True, max_lines=900)
            yield Static("", id="deep-findings", markup=True)
            yield Static("", id="deep-diff", markup=False)
            yield Static("", id="deep-report", markup=False)
        yield Static("", id="deep-gates")
        yield Footer()

    def on_mount(self) -> None:
        self.show_tab(0)
        self.run_worker(self.refresh_all(force=True), exclusive=True)

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

    # -- data ---------------------------------------------------------------

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
                f"[b]{m.id}[/b] {status_rich(m.status.value)}  {m.objective}  "
                f"[dim]${spend:.4f}[/dim]"
            )
            await self._stream_feed(force=force)
            await self._render_findings()
            await self._render_diff()
            await self._render_report()
            await self._render_gates()
        except Exception as exc:
            self.notify(f"refresh failed: {exc}", severity="error")
        finally:
            self._refreshing = False

    async def _stream_feed(self, force: bool = False) -> None:
        log = self.query_one("#deep-feed", RichLog)
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
        """Every run artifact's findings, newest attempt last, severity-tagged."""
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
        """The last diff artifact in the mission (auditor sees diff-only; so
        does this pane: the diff is what shipped)."""
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

    # -- actions ------------------------------------------------------------

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
        self.rw.refresh_dashboard()

    async def action_fetch(self) -> None:
        self.notify("fetching result…")
        # blocking network + tar extraction off the UI loop (asyncio.to_thread)
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
        self.rw.refresh_dashboard()
        self.app.pop_screen()


# ---------------------------------------------------------------- dashboard


class PromptBar(Input):
    """The cockpit prompt: free text becomes a mission, /commands drive the
    app. Lives at the bottom of the dashboard, always reachable."""

    class CommandHint(Static):
        pass

    def __init__(self) -> None:
        super().__init__(
            placeholder="what should rework do?  (type / for commands, enter to launch)",
            id="prompt",
        )


SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/gates", "review and decide open gates"),
    ("/library", "search what missions learned"),
    ("/fetch", "fetch a finished mission's result: /fetch <mission-id>"),
    ("/new", "start a mission wizard: /new <objective>"),
    ("/projects", "create a project label"),
)


class Dashboard(ReworkScreen):
    """Projects sidebar + missions table + live detail pane + prompt bar."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("n", "new_mission", "New mission", show=True),
        Binding("p", "new_project", "New project", show=True),
        Binding("g", "gates", "Gates", show=True),
        Binding("l", "library", "Library", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("enter", "open_deep", "Open", show=True),
        Binding("f", "fetch", "Fetch", show=True),
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
                yield ListView(id="projects")
                yield Label("[dim]p new project[/dim]", id="sidebar-hint")
            with Vertical(id="mid"):
                yield Label("[b]all missions[/b]", id="mid-title")
                yield DataTable(id="missions")
        with Vertical(id="detail"):
            yield Label("Select a mission", id="mission-head")
            yield RichLog(id="events", markup=True, highlight=False, wrap=True, max_lines=600)
            yield Static("", id="gate-box")
        with Horizontal(id="prompt-row"):
            yield PromptBar()
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
        # focus follows into the missions table: arrows work immediately
        self.query_one("#missions", DataTable).focus()

    async def reload_missions(self) -> None:
        title = self.query_one("#mid-title", Label)
        if self._selected_project:
            pname = self.rw._project_cache.get(self._selected_project)
            title.update(f"[b]{pname.name if pname else self._selected_project}[/b]")
        else:
            title.update("[b]all missions[/b]")
        table = self.query_one("#missions", DataTable)
        selected_id = self._selected_mission
        table.clear()
        projects = {p.id: p.name for p in self.rw._project_cache.values()}
        missions = await self.rw.data.missions()
        self.rw._mission_cache = {m.id: m for m in missions}
        # running/awaiting-gate first (the cockpit shows live work up top),
        # then newest first within each group
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
        if selected_id is not None:
            for row, key in enumerate(table.rows):
                if str(key.value) == selected_id:
                    table.move_cursor(row=row)
                    break
        self.rw.set_gates_badge(await self.rw.data.open_gates())

    @on(DataTable.RowSelected, "#missions")
    async def mission_selected(self, event: DataTable.RowSelected) -> None:
        await self.open_deep(str(event.row_key.value))

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

    async def open_deep(self, mission_id: str) -> None:
        await self.select_mission(mission_id)
        self.app.push_screen(MissionDeepView(mission_id))

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
            log.write(f"[dim]{stamp}[/dim] {event.type.value:<15} {role} {detail}".rstrip())
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

    # -- prompt bar -----------------------------------------------------------

    @on(Input.Submitted, "#prompt")
    async def prompt_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.rw.run_slash(text)
            return
        # free text = a mission: wizard pre-filled with the objective and
        # last-used repo/project/plan (remembered on the app)
        self.run_worker(self._new_mission_flow(objective=text), exclusive=True)

    # -- actions -------------------------------------------------------------

    async def action_new_mission(self) -> None:
        # push_screen(wait_for_dismiss=True) is only legal inside a Textual
        # worker (a keypress handler is not one; found live: pressing n
        # crashed). The modal flow becomes a worker; UI stays interactive.
        self.run_worker(self._new_mission_flow(), exclusive=True)

    async def _new_mission_flow(self, objective: str = "") -> None:
        defaults = self.rw._mission_defaults
        result = await self.rw.push_screen(
            NewMissionModal(
                list(self.rw._project_cache.values()),
                objective=objective,
                defaults=defaults,
            ),
            wait_for_dismiss=True,
        )
        if not result:
            return
        # remember for the next wizard (one-keypress launches afterwards)
        self.rw._mission_defaults = {
            "repo": result.get("repo"),
            "repo_dir": result.get("repo_dir"),
            "project_id": result.get("project_id"),
            "plan": result.get("plan"),
        }
        self.notify("creating mission…")
        try:
            mission = await self.rw.data.create_mission(**result)
            self.notify(f"mission {mission.id} created")
            self._selected_mission = mission.id
            self._last_event_seq = 0
            await self.rw.load_project_cache()
            await self.reload_projects()
            await self.reload_missions()
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    async def action_new_project(self) -> None:
        self.run_worker(self._new_project_flow(), exclusive=True)

    async def _new_project_flow(self) -> None:
        result = await self.rw.push_screen(CreateProjectModal(), wait_for_dismiss=True)
        if not result:
            return
        try:
            project = await self.rw.data.create_project(**result)
            self.notify(f"project {project.name} created ({project.id})")
            await self.rw.load_project_cache()
            await self.reload_projects()
        except Exception as exc:
            self.notify(f"create failed: {exc}", severity="error")

    def action_gates(self) -> None:
        self.app.push_screen(GatesScreen())

    def action_library(self) -> None:
        self.app.push_screen(LibraryScreen())

    def action_refresh(self) -> None:
        self.run_worker(self.tick(), exclusive=True)

    async def action_open_deep(self) -> None:
        if self._selected_mission:
            await self.open_deep(self._selected_mission)

    async def action_fetch(self) -> None:
        mission_id = self._selected_mission
        if mission_id is None:
            self.notify("select a mission first", severity="warning")
            return
        self.notify("fetching result…")
        try:
            dest, count = await self.rw.data.fetch_result(mission_id)
            self.notify(f"fetched {count} files → {dest}")
        except FileNotFoundError as exc:
            self.notify(str(exc), severity="warning")
        except Exception as exc:
            self.notify(f"fetch failed: {exc}", severity="error")

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


class ReworkApp(App[None]):
    TITLE = "rework"
    SUB_TITLE = "missions on the daemon"
    # providers are CLASSES here (textual instantiates with screen + match
    # style itself); a factory function gets called with zero args and crashes
    # the palette on open (found live)
    COMMANDS = App.COMMANDS | {ReworkCommands}

    CSS = """
    #main { height: 3fr; }
    #sidebar { width: 32; min-width: 20; border-right: round $panel; padding: 0 1; }
    #mid { width: 1fr; }
    #missions { height: 1fr; }
    #detail { height: 2fr; border-top: round $panel; padding: 0 1; }
    #events { height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    #gate-box { height: auto; padding: 0 1; }
    #prompt-row { height: 3; border-top: round $accent; padding: 0 1; }
    #prompt { width: 1fr; border: none; background: transparent; }
    #modal-form { width: 72; max-width: 90%; height: auto; max-height: 90%;
                  border: round $accent; padding: 1 2; background: $surface; }
    #modal-actions { height: auto; padding-top: 1; }
    #body-viewer { width: 90%; height: 90%; border: round $accent;
                   padding: 1 2; background: $surface; }
    #projects { height: 1fr; }
    #lib-split { height: 1fr; }
    #lib-list-pane { width: 2fr; min-width: 30; border-right: round $panel; padding: 0 1; }
    #lib-table { height: 1fr; }
    #lib-counts { height: 1; }
    #lib-body-pane { width: 3fr; padding: 1 2; }
    #lib-body { padding: 0 1; }
    #lib-query { border: none; background: transparent; border-bottom: round $accent; }
    #deep-tabs { height: 1; }
    .deep-tab { color: $text-muted; padding: 0 2; }
    .deep-tab.active { color: $text; background: $panel; text-style: bold; }
    #deep-content { height: 1fr; border-top: round $panel; padding: 0 1; }
    #deep-feed { height: auto; max-height: 100%; }
    #deep-gates { height: auto; padding: 0 1; }
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
        # the wizard remembers last-used repo/project/plan (owner: after the
        # first mission, launching is one keypress)
        self._mission_defaults: dict[str, Any] = {}

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

    # -- slash commands ------------------------------------------------------

    async def run_slash(self, text: str) -> None:
        """`/cmd args` from the prompt bar. Unknown commands say so; no
        syntax is required anywhere else."""
        cmd, _, rest = text.partition(" ")
        rest = rest.strip()
        screen = self.screen if self.screen is not None else self.get_screen("dashboard")
        dash = screen if isinstance(screen, Dashboard) else None
        if cmd == "/gates":
            if dash:
                dash.action_gates()
        elif cmd == "/library":
            if dash:
                dash.action_library()
        elif cmd == "/projects":
            if dash:
                await dash.action_new_project()
        elif cmd == "/new":
            if dash:
                dash.run_worker(dash._new_mission_flow(objective=rest), exclusive=True)
        elif cmd == "/fetch":
            if dash and rest:
                dash._selected_mission = rest
                await dash.action_fetch()
            else:
                self.notify("usage: /fetch <mission-id>", severity="warning")
        else:
            known = "  ".join(c for c, _ in SLASH_COMMANDS)
            self.notify(f"unknown command {cmd}. try: {known}", severity="warning")

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
