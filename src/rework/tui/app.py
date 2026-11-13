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

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.notifications import SeverityLevel
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher
from textual.worker import WorkerFailed

from rework.runtime import CODE_CHANGED, CodeChangeWatcher
from rework.tui.hub import Hub, Snapshot
from rework.tui.settings import Settings
from rework.tui.theme import THEME, WARN, clip
from rework.tui.views.base import View
from rework.tui.widgets.chrome import SEPARATOR, HintBar, MenuBar, MenuItem, TabBar
from rework.tui.widgets.modals import Field, Form, Help

log = logging.getLogger("rework.tui")

TABS = ["work", "dashboard", "gates", "library", "cost"]

# Identical errors inside this window are counted, not re-toasted (see
# _swallow). Found live: a MarkupError storm toasted itself thousands of times.
ERROR_STORM_WINDOW = 5.0

# How often the shell asks whether the code on disk changed (finding 28: the
# owner's TUI ran code older than its checkout and the traceback lied). A stat
# walk of the package is cheap but not free, and a merge is rare, so this stays
# off the 2s heartbeat: its own slow interval.
CODE_CHECK_SECONDS = 60.0

# Found live: alt+N worked under Pilot and NEVER in a real macOS terminal.
# Option+digit reaches Textual as a character (¡ ™ £ ¢ ∞; Textual even maps
# ESC+digit to those), and while a text input has focus Textual deliberately
# drops bindings for printable characters so typing works. The Work prompt is
# focused nearly always, so these are intercepted in on_event instead.
OPTION_KEYS = {
    "inverted_exclamation_mark": "work",
    "trade_mark_sign": "dashboard",
    "pound_sign": "gates",
    "cent_sign": "library",
    "infinity": "cost",
}
# Option+a / Option+x type å / ≈ on macOS; they approve / reject on the Gates tab
GATE_KEYS = {"å": True, "almost_equal_to": False}


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
            # not action_mission(): previewing a PR must work with NO mission
            # focused (action_mission refuses early when there is none)
            MenuItem("Preview a PR…", "app.preview_pr"),
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
        # meta-mode terminals send real alt+N; the macOS spellings (¡ ™ £ ¢ ∞)
        # are handled in on_event, see OPTION_KEYS
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
        # error-storm guard: (type name, message) of the last toast plus when it
        # was shown, and how many identical repeats were collapsed into it
        self._last_error: tuple[str, str] | None = None
        self._last_error_at = 0.0
        self._suppressed_errors = 0
        # toast-log guard: the (severity, title, message) of the last toast
        # written to tui.log plus when, so a burst of identical toasts cannot
        # flood the log (the same shape as the storm guard above)
        self._last_toast: tuple[str, str, str] | None = None
        self._last_toast_at = 0.0
        self._suppressed_toasts = 0
        # "the code on disk moved under us": recorded here, said once (see
        # _check_code); None until the shell is mounted and can paint a note
        self._code_watcher: CodeChangeWatcher | None = None

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
        self.query_one(MenuBar).set_status(online=True, gates=0, spend_today=0.0, connecting=True)
        self._paint_hints()
        self._kick()
        self.set_interval(self.poll_seconds, self._kick)
        self._code_watcher = CodeChangeWatcher()
        self.set_interval(CODE_CHECK_SECONDS, self._check_code)

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

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key) and not event.is_forwarded:
            tab = OPTION_KEYS.get(event.key)
            if tab is not None:
                self.action_tab(tab)
                return
            if event.key in GATE_KEYS and self.active_tab == "gates":
                self.view("gates").action_decide(GATE_KEYS[event.key])
                return
        await super().on_event(event)

    # -- heartbeat -----------------------------------------------------------

    def _kick(self) -> None:
        """Start a refresh in a WORKER. Found live: the refresh used to be
        awaited on the App's own message queue, so a slow or unreachable
        database froze every key and click until it returned (rewvis offline =
        a dead TUI). Single-flight is the Hub's job; exclusive=False keeps a
        hung refresh from being cancelled mid-query by the next tick."""
        self.run_worker(self._tick(), group="hub", exclusive=False)

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

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = False,
        _logged: bool = False,
    ) -> None:
        """Every toast the owner would actually see is recorded here, so tests
        can assert on notifications (refusals included). Channels follow
        `settings.notify`, exactly like `announce`.

        Every toast also writes ONE line to tui.log (visible in `rework logs
        tui`): once a toast fades it is gone, and only errors that went through
        `_swallow` used to reach the log. Severity picks the level — warning,
        error, and information for anything else — and the line carries both
        title and message. Identical toasts inside ERROR_STORM_WINDOW collapse
        to debug, the way `_swallow` collapses its toasts.

        `_logged` marks a toast whose error `_swallow` already logged with a
        traceback: that one is logged at debug, never twice at error level. It
        is private and is NOT forwarded to Textual.

        `markup` defaults to False ON PURPOSE: nearly every toast here embeds
        runtime text (an exception message, a model name, mission text). A
        message containing `[` used to be parsed as markup, raising a
        MarkupError inside the toast, which `_handle_exception` swallowed and
        toasted again — an unbounded storm that took the TUI down. Callers who
        really want markup must ask for it, and must escape what they
        interpolate."""
        if "toast" in self.settings.notify:
            self.notifications_sent.append(("toast", message))
        self._log_toast(severity, title, message, logged=_logged)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)

    def _log_toast(
        self, severity: SeverityLevel, title: str, message: str, *, logged: bool
    ) -> None:
        """One log record per toast, a burst of identical ones counted not
        repeated (see notify). `logged` toasts are debug-only: `_swallow`
        already wrote the traceback at error level."""
        text = f"{title}: {message}" if title else message
        key = (severity, title or "", message)
        now = time.monotonic()
        same = key == self._last_toast
        if same and now - self._last_toast_at < ERROR_STORM_WINDOW:
            self._last_toast_at = now  # sliding window, as _swallow
            self._suppressed_toasts += 1
            return
        # a different message is its own burst: the count belongs to the last one
        suppressed = self._suppressed_toasts if same else 0
        self._suppressed_toasts = 0
        self._last_toast = key
        self._last_toast_at = now
        more = f" ({suppressed} more identical, suppressed)" if suppressed else ""
        if logged:
            log.debug("%s%s", text, more)
        elif severity == "error":
            log.error("%s%s", text, more)
        elif severity == "warning":
            log.warning("%s%s", text, more)
        else:
            log.info("%s%s", text, more)

    def announce(self, text: str, *, title: str = "") -> None:
        """A human is needed. Channels per settings: toast, bell, os."""
        channels = self.settings.notify
        if "toast" in channels:
            self.notify(text, title=title, severity="warning", timeout=8, markup=False)
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

    # -- code on disk ----------------------------------------------------------

    def _check_code(self) -> None:
        """A merge landed in the checkout this process was started from. The
        modules in memory are the OLD ones, so say so in the status bar and in
        tui.log, once. Never restart: that is the owner's call (finding 28)."""
        if self._code_watcher is None or not self._code_watcher.poll():
            return
        log.warning("%s", CODE_CHANGED)
        try:
            self.set_note(f"[{WARN}]{CODE_CHANGED}[/]")
        except Exception:  # a missing widget must not kill the tick
            log.debug("code-change note failed", exc_info=True)

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

    def action_refresh(self) -> None:
        self._kick()

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
            self.notify(f"Copied {len(text)} chars", timeout=1.5, markup=False)
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
            markup=False,
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

    def preview_repo_default(self) -> str:
        """What to prefill the repo-path field with: the newest mission (any
        status) in the project selected in the Work tab, else the newest
        mission overall. Missions are the only place the TUI records a LOCAL
        repo path (ADR 0008: never the process cwd, and a project's settings
        hold repo *references*, not paths)."""
        project_id = getattr(self.view("work"), "project_id", None)
        missions = list(self.hub.snapshot.missions)
        if project_id:
            for mission in missions:
                if mission.project_id == project_id and getattr(mission, "repo_path", None):
                    return str(mission.repo_path)
        for mission in missions:
            if getattr(mission, "repo_path", None):
                return str(mission.repo_path)
        return ""

    def action_preview_pr(self) -> None:
        """Preview ANY PR, focused mission or not. The card's Preview button
        only covers PRs a mission opened; this asks for the two things only the
        owner knows: which PR, and which local repo to fetch it into."""
        from rework.tui import actions

        def done(values: dict[str, str] | None) -> None:
            if not values:
                return
            pr = values.get("pr", "").strip()
            if not pr.isdigit():
                self.notify("PR number must be digits only", severity="error", timeout=4)
                return
            self.run_worker(actions.run_preview(self, pr, values.get("repo", "")), group="preview")

        self.push_screen(
            Form(
                "Preview a PR",
                [
                    Field("pr", "PR number", placeholder="e.g. 128"),
                    Field(
                        "repo",
                        "Repo path",
                        value=self.preview_repo_default(),
                        placeholder="/path/to/a/local/git/repo",
                    ),
                ],
                submit="Preview",
            ),
            done,
        )

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
            self.notify(f"Project {project.name} created", timeout=3, markup=False)
            await self.hub.refresh()
        except Exception as exc:
            self.notify(f"Could not create project: {exc}", severity="error", markup=False)

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
        """Never crash, never storm. Found live: a MarkupError whose own text
        was "auto closing tag ([/]) has nothing to close" was toasted with
        markup ON, the toast raised a second MarkupError, that was swallowed
        and toasted again — 1000+ toasts until the app fell apart. So: the
        toast is plain text, and an identical error repeating inside
        ERROR_STORM_WINDOW is counted, not re-toasted."""
        self.swallowed.append(exc)
        # WorkerFailed wraps the real exception; log the inner one with its traceback
        if isinstance(exc, WorkerFailed):
            inner = exc.error
            log.error("worker failed: %s", inner, exc_info=inner)
        else:
            log.exception("swallowed UI error", exc_info=exc)
        key = (type(exc).__name__, str(exc))
        now = time.monotonic()
        if key == self._last_error and now - self._last_error_at < ERROR_STORM_WINDOW:
            self._last_error_at = now
            self._suppressed_errors += 1
            return
        suppressed, self._suppressed_errors = self._suppressed_errors, 0
        self._last_error = key
        self._last_error_at = now
        more = f" ({suppressed} more identical, suppressed)" if suppressed else ""
        try:
            self.notify(
                f"{type(exc).__name__}: {clip(str(exc), 160)}{more}",
                severity="error",
                timeout=6,
                markup=False,  # exception text is untrusted, never parse it
                _logged=True,  # the traceback is already in tui.log, above
            )
        except Exception:
            pass

    def _handle_exception(self, error: Exception) -> None:
        """Turn surprises into a toast instead of Textual's full-screen
        traceback + exit (handoff §7 trap #1). Startup errors still exit: an app
        that never mounted has nothing to show a toast on.

        If the app is going to exit, the traceback goes to
        ~/.rework/logs/tui.log FIRST: the owner cannot copy a traceback off a
        dead full-screen terminal, and the toast (plain text, see notify) may
        not even fit. A swallowed error is logged once, by _swallow."""
        if not self.is_running or not self._exit_renderables_ok():
            log.error("fatal UI error: %s: %s", type(error).__name__, error, exc_info=error)
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
    app = ReworkApp()
    try:
        app.run()
    except BaseException:
        log.exception("tui crashed")  # traceback in tui.log before it escapes
        raise
    if app.return_code:
        log.error("tui exit with code %s", app.return_code)
    else:
        log.info("tui exit")
