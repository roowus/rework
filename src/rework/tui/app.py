"""ReworkApp: the shell. Menu bar, tab bar, one view per tab, hint bar.

The app owns cross-cutting behaviour only: the hub heartbeat, gate
notifications, copy, quit, and the never-crash exception policy. Everything
about a section lives in its view (src/rework/tui/views/)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher

from rework.tui.hub import Hub, Snapshot
from rework.tui.settings import Settings
from rework.tui.theme import THEME, clip
from rework.tui.views.base import View
from rework.tui.widgets.chrome import SEPARATOR, HintBar, MenuBar, MenuItem, TabBar
from rework.tui.widgets.modals import Field, Form, Help

log = logging.getLogger("rework.tui")

TABS = ["work", "dashboard", "gates", "library", "cost"]


def _menus() -> dict[str, list[MenuItem]]:
    return {
        "rework": [
            MenuItem("About rework", "app.about"),
            MenuItem("Keyboard shortcuts", "app.help", "f1"),
            SEPARATOR,
            MenuItem("Settings…", "app.settings"),
            SEPARATOR,
            MenuItem("Quit", "app.quit", "esc esc"),
        ],
        "File": [
            MenuItem("New chat", "app.new_chat", "ctrl+n"),
            MenuItem("New project…", "app.new_project"),
            MenuItem("New library item…", "app.new_library_item"),
            SEPARATOR,
            MenuItem("Refresh", "app.refresh", "ctrl+r"),
            MenuItem("Settings…", "app.settings"),
            SEPARATOR,
            MenuItem("Quit", "app.quit", "esc esc"),
        ],
        "Edit": [
            MenuItem("Copy", "app.copy", "ctrl+c"),
            MenuItem("Focus prompt", "app.focus_prompt", "ctrl+l"),
        ],
        "View": [
            MenuItem("Work", "app.tab('work')", "alt+1"),
            MenuItem("Dashboard", "app.tab('dashboard')", "alt+2"),
            MenuItem("Gates", "app.tab('gates')", "alt+3"),
            MenuItem("Library", "app.tab('library')", "alt+4"),
            MenuItem("Cost", "app.tab('cost')", "alt+5"),
            SEPARATOR,
            MenuItem("Toggle sidebar", "app.toggle_sidebar", "ctrl+b"),
        ],
        "Mission": [
            MenuItem("Open in dashboard", "app.mission('open')"),
            SEPARATOR,
            MenuItem("Pause", "app.mission('pause')"),
            MenuItem("Resume", "app.mission('resume')"),
            MenuItem("Cancel", "app.mission('cancel')"),
            SEPARATOR,
            MenuItem("Fetch result", "app.mission('fetch')"),
        ],
        "Help": [
            MenuItem("Keyboard shortcuts", "app.help", "f1"),
            MenuItem("About rework", "app.about"),
        ],
    }


class ReworkApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "rework"
    ENABLE_COMMAND_PALETTE = True
    ESCAPE_TO_MINIMIZE = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("alt+1", "tab('work')", show=False, priority=True),
        Binding("alt+2", "tab('dashboard')", show=False, priority=True),
        Binding("alt+3", "tab('gates')", show=False, priority=True),
        Binding("alt+4", "tab('library')", show=False, priority=True),
        Binding("alt+5", "tab('cost')", show=False, priority=True),
        Binding("ctrl+n", "new_chat", show=False, priority=True),
        Binding("ctrl+r", "refresh", show=False, priority=True),
        Binding("ctrl+b", "toggle_sidebar", show=False, priority=True),
        Binding("ctrl+l", "focus_prompt", show=False),
        Binding("f1", "help", show=False),
        Binding("escape", "go_back", show=False),
        Binding("ctrl+c", "copy", show=False),
        Binding("ctrl+q", "quit", show=False),
    ]

    def __init__(self, *, poll_seconds: float | None = None, hub: Hub | None = None) -> None:
        super().__init__()
        self.register_theme(THEME)
        self.theme = "rework"
        self.settings = Settings.load()
        self.hub = hub or Hub()
        self.poll_seconds = poll_seconds or float(self.settings.poll_seconds or 2.0)
        self.active_tab = "work"
        self._esc_armed_at = 0.0
        self._seen_gates: set[str] | None = None  # None until the first load
        self.swallowed: list[BaseException] = []  # surprises turned into toasts
        self.notifications_sent: list[tuple[str, str]] = []  # (channel, text), for tests

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        from rework.tui.views.cost import CostView
        from rework.tui.views.dashboard import DashboardView
        from rework.tui.views.gates import GatesView
        from rework.tui.views.library import LibraryView
        from rework.tui.views.work import WorkView

        yield MenuBar(_menus())
        yield TabBar(
            [
                ("work", "Work"),
                ("dashboard", "Dashboard"),
                ("gates", "Gates"),
                ("library", "Library"),
                ("cost", "Cost"),
            ]
        )
        with ContentSwitcher(initial="view-work", id="views"):
            yield WorkView()
            yield DashboardView()
            yield GatesView()
            yield LibraryView()
            yield CostView()
        yield HintBar()

    def on_mount(self) -> None:
        from rework.logs import asyncio_exception_handler

        self.query_one(MenuBar).target = target_label()

        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
        self.hub.subscribe(self._on_snapshot)
        self.query_one(TabBar).set_active("work")
        self.query_one(MenuBar).set_status(online=True, gates=0, spend_today=0.0)
        self._paint_hints()
        self.call_later(self._tick)
        self.set_interval(self.poll_seconds, self._tick)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # the stylesheet references $bg/$fg/… before any theme switch happens
        return {**super().get_theme_variable_defaults(), **THEME.variables}

    def views(self) -> list[View]:
        return list(self.query(View))

    def view(self, tab: str) -> Any:
        return self.query_one(f"#view-{tab}", View)

    @property
    def current(self) -> View:
        return self.view(self.active_tab)

    # -- heartbeat -----------------------------------------------------------

    async def _tick(self) -> None:
        try:
            await self.hub.refresh()
        except Exception as exc:  # belt and braces: refresh already guards
            log.warning("tick failed: %s", exc)

    def _on_snapshot(self, snap: Snapshot) -> None:
        self.query_one(MenuBar).set_status(
            online=snap.online,
            gates=len(snap.gates),
            spend_today=snap.spend_today,
            error=snap.error,
        )
        self.query_one(TabBar).set_badge("gates", len(snap.gates))
        self._notify_new_gates(snap)
        for view in self.views():
            try:
                view.on_snapshot(snap)
            except Exception as exc:
                self._swallow(exc)

    def _notify_new_gates(self, snap: Snapshot) -> None:
        if not snap.loaded:
            return
        ids = {g.id for g in snap.gates}
        if self._seen_gates is None:  # first load: existing gates are not news
            self._seen_gates = ids
            return
        fresh = [g for g in snap.gates if g.id not in self._seen_gates]
        self._seen_gates = ids
        for gate in fresh:
            mission = snap.mission(gate.mission_id)
            where = clip(mission.objective, 60) if mission else gate.mission_id
            self.announce(f"{clip(gate.question, 120)}", title=f"Gate · {where}")

    def announce(self, text: str, *, title: str = "") -> None:
        """A human is needed. Channels per settings: toast, bell, os."""
        channels = self.settings.notify
        if "toast" in channels:
            self.notify(text, title=title, severity="warning", timeout=8)
            self.notifications_sent.append(("toast", text))
        if "bell" in channels:
            self.bell()
            self.notifications_sent.append(("bell", text))
        if "os" in channels:
            self.notifications_sent.append(("os", text))
            self.run_worker(self._os_notify(title or "rework", text), group="notify")

    async def _os_notify(self, title: str, text: str) -> None:
        if sys.platform != "darwin":
            return
        script = f"display notification {_osa(text)} with title {_osa(title)}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            log.debug("osascript failed", exc_info=True)

    # -- chrome helpers --------------------------------------------------------

    def set_note(self, markup: str) -> None:
        self.query_one(TabBar).set_note(markup)

    def _paint_hints(self) -> None:
        base = [("alt+1-5", "tabs"), ("f1", "keys"), ("esc esc", "quit")]
        try:
            hints = self.current.hints()
        except Exception:
            hints = []
        self.query_one(HintBar).set_hints([*hints, *base])

    def refresh_hints(self) -> None:
        self._paint_hints()

    # -- actions -----------------------------------------------------------------

    def action_tab(self, name: str) -> None:
        if name not in TABS:
            return
        if isinstance(self.screen, ModalScreen):
            self.pop_screen()
        self.active_tab = name
        self.query_one("#views", ContentSwitcher).current = f"view-{name}"
        self.query_one(TabBar).set_active(name)
        self._esc_armed_at = 0.0
        try:
            self.current.shown()
        except Exception as exc:
            self._swallow(exc)
        self._paint_hints()

    def on_tab_bar_changed(self, event: TabBar.Changed) -> None:
        self.action_tab(event.name)

    async def action_refresh(self) -> None:
        await self._tick()

    def action_go_back(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return
        try:
            if self.current.back():
                self._esc_armed_at = 0.0
                return
        except Exception as exc:
            self._swallow(exc)
        now = time.monotonic()
        if now - self._esc_armed_at < 1.0:
            self.exit()
            return
        self._esc_armed_at = now
        self.notify("Press esc again to quit", timeout=1.5)

    def action_copy(self) -> None:
        text = None
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        focused = self.focused
        if not text and focused is not None and hasattr(focused, "selected_text"):
            text = str(focused.selected_text or "")
        if not text and focused is not None and hasattr(focused, "copy_value"):
            text = focused.copy_value()
        if text:
            self.copy_to_clipboard(text)
            self.notify(f"Copied {len(text)} chars", timeout=1.5)
        else:
            self.notify("Select text with the mouse, then ctrl+c", timeout=2)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)  # OSC 52 (most terminals)
        if sys.platform == "darwin" and self.is_running and not self.is_headless:
            self.run_worker(_pbcopy(text), group="clipboard")

    def action_new_chat(self) -> None:
        self.action_tab("work")
        self.view("work").new_chat()

    def action_focus_prompt(self) -> None:
        self.action_tab("work")
        self.view("work").focus_prompt()

    def action_toggle_sidebar(self) -> None:
        for sidebar in self.current.query(".sidebar"):
            sidebar.toggle_class("-hidden")

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_about(self) -> None:
        from rework import __version__

        self.notify(
            f"rework {__version__}. Talk to the orchestrator; it runs the roles.",
            title="About",
            timeout=5,
        )

    def action_mission(self, verb: str) -> None:
        dash = self.view("dashboard")
        mission_id = dash.focus_mission_id()
        if not mission_id and self.active_tab == "work":
            mission_id = self.view("work").last_mission_id()
        if not mission_id:
            self.notify("Open a mission first (Dashboard)", timeout=3)
            return
        if verb == "open":
            self.open_mission(mission_id)
        else:
            self.run_worker(dash.control(mission_id, verb), group="control")

    def open_mission(self, mission_id: str, run_id: str | None = None) -> None:
        self.action_tab("dashboard")
        self.view("dashboard").open_mission(mission_id, run_id)

    def action_new_project(self) -> None:
        def done(values: dict[str, str] | None) -> None:
            if values and values.get("name", "").strip():
                self.run_worker(self._create_project(values), group="write")

        self.push_screen(
            Form(
                "New project",
                [
                    Field("name", "Name", placeholder="e.g. polystats"),
                    Field(
                        "repo",
                        "Repo URL (optional)",
                        placeholder="https://github.com/you/repo.git",
                    ),
                ],
                submit="Create",
            ),
            done,
        )

    async def _create_project(self, values: dict[str, str]) -> None:
        try:
            project = await self.hub.data.create_project(values["name"].strip())
            repo = values.get("repo", "").strip()
            if repo:
                store = await self.hub.data.store()
                settings = dict(project.settings)
                name = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repo"
                settings["repos"] = [{"name": name, "kind": "git", "url": repo}]
                await store.update_project(project.id, settings=settings)
            self.notify(f"Project {project.name} created", timeout=3)
            await self.hub.refresh()
        except Exception as exc:
            self.notify(f"Could not create project: {exc}", severity="error")

    def action_new_library_item(self) -> None:
        self.action_tab("library")
        self.view("library").new_item()

    def action_settings(self) -> None:
        current = ",".join(self.settings.notify) or "none"

        def done(values: dict[str, str] | None) -> None:
            if not values:
                return
            from rework.tui.settings import parse_notify

            self.settings.notify = parse_notify(values.get("notify", ""))
            self.settings.save()
            self.notify("Settings saved", timeout=2)

        self.push_screen(
            Form(
                "Settings",
                [
                    Field(
                        "notify",
                        "When a gate opens, notify by",
                        kind="choice",
                        value=current,
                        choices=[
                            ("toast + bell", "toast,bell"),
                            ("toast", "toast"),
                            ("toast + bell + macOS", "toast,bell,os"),
                            ("macOS only", "os"),
                            ("none", "none"),
                        ],
                    )
                ],
            ),
            done,
        )

    # -- resilience ------------------------------------------------------------------

    def _swallow(self, exc: BaseException) -> None:
        self.swallowed.append(exc)
        log.exception("swallowed UI error", exc_info=exc)
        try:
            self.notify(f"{type(exc).__name__}: {clip(str(exc), 160)}", severity="error", timeout=6)
        except Exception:
            pass

    def _handle_exception(self, error: Exception) -> None:
        """Turn surprises into a toast instead of Textual's full-screen
        traceback + exit (handoff §7 trap #1). Startup errors still exit: an app
        that never mounted has nothing to show a toast on."""
        if not self.is_running or not self._exit_renderables_ok():
            super()._handle_exception(error)
            return
        self._swallow(error)

    def _exit_renderables_ok(self) -> bool:
        try:
            self.query_one(MenuBar)
            return True
        except Exception:
            return False

    async def on_unmount(self) -> None:
        await self.hub.aclose()


def target_label() -> str:
    """Which database this TUI is connected to, for the status bar (found
    live: the owner's TUI pointed at production while they meant local).
    REWORK_TARGET_NAME overrides; SQLite is 'local'; Postgres shows its host."""
    import os
    from urllib.parse import urlparse

    named = os.environ.get("REWORK_TARGET_NAME")
    if named:
        return named
    db = os.environ.get("REWORK_DB", "")
    if not db.startswith(("postgres://", "postgresql://")):
        return "local"
    host = urlparse(db).hostname or "postgres"
    return {"100.107.125.118": "rewvis"}.get(host, host)


def _osa(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _pbcopy(text: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode())
    except Exception:
        log.debug("pbcopy failed", exc_info=True)


def run_tui() -> None:
    from rework.logs import setup_logging

    setup_logging("tui")  # file only: the TUI owns the terminal
    log.info("tui start")
    ReworkApp().run()
    log.info("tui exit")
