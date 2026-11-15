"""Dashboard: every project and mission at a glance, then drill down.

Overview: stats strip · projects (left) · missions grouped by what matters
(needs you → queued → running → paused → recent). Click a mission to open it.

Mission: header + flow strip · runs (left: one row per role session) · tabs
for the live Trajectory, the LangGraph-style Graph, each role's Output, the
Diff, the full Report and the mission's Gates. Click a run to focus everything
on that session; esc steps back out."""

from __future__ import annotations

import asyncio
import time
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import ContentSwitcher, Input, Markdown, Static

from rework.tui import actions, search, spinner
from rework.tui.hub import Snapshot
from rework.tui.theme import (
    ACCENT,
    FAINT,
    MUTED,
    WARN,
    ago,
    clip,
    duration,
    esc,
    glyph,
    money,
    plan_label,
    run_mark,
    status_text,
    status_value,
    tokens,
)
from rework.tui.views.base import View
from rework.tui.widgets.artifacts import artifact_markup, diff_markup
from rework.tui.widgets.base import (
    HEADER,
    Breadcrumb,
    Btn,
    ChipGroup,
    Label,
    RowData,
    RowList,
    Stat,
)
from rework.tui.widgets.graph import (
    GraphView,
    mission_flow_summary,
    mission_layers,
    mission_nodes,
)
from rework.tui.widgets.modals import Field, Form
from rework.tui.widgets.trajectory import Trajectory, role_of

ALL = "*"
NONE = "-"
#: the smallest slice of the AI-search budget we still hand to the model, so a
#: blown deadline comes back as a clean "timed out" rather than an instant 0s call
_MIN_BUDGET = 0.5

#: The overview's mission sections, in display order. This list only fixes
#: ORDER (and "needs you" is the amber one); which section a mission belongs to
#: is `section_for`'s decision alone, so grouping, counts and the project
#: badges below can never disagree.
SECTIONS = ["needs you", "queued", "running", "paused", "recent"]


def section_for(mission: Any, snap: Snapshot) -> str:
    """The ONE rule that puts a mission in a Dashboard section.

    "needs you" comes from the snapshot's OPEN gates, never from the stored
    status label: an `awaiting_gate` mission whose gate was already decided is
    not waiting for the owner (it is running), so the Dashboard can no longer
    list a mission under "needs you" while the Gates tab is empty (found live
    2026-11-14). `Snapshot.gates` holds open gates only, so this needs no I/O.

    The stats strip, the project badges and the section headers all call this,
    so one gate cannot be counted twice differently."""
    if snap.gates_for(mission.id):
        return "needs you"
    st = status_value(mission.status)
    if st == "pending":
        return "queued"  # waiting for a runner slot, not for the owner
    if st in ("running", "awaiting_gate"):
        return "running"
    if st == "on_hold":
        return "paused"
    return "recent"  # done / failed / cancelled (an unknown status stays visible)


#: The status whose glyph a section paints when the stored label would paint
#: another one: a stale `awaiting_gate` (its gate is already decided) is listed
#: under "running" and must look running, or an amber diamond under a faint
#: "running" header re-creates the confusion section_for just removed.
SECTION_GLYPH = {"running": "running", "queued": "pending"}


def row_glyph(mission: Any, section: str) -> str:
    """The overview row's leading icon: the SECTION's glyph, not the raw status.

    `awaiting_gate` is amber (a human is needed) in theme.STATUS, and it stays
    that way everywhere else: the drill-down header still prints the stored
    status as diagnostic detail, and the ◆ question note below the row still
    marks the open gate. What must not happen is an amber diamond on a row filed
    under "running"/"queued"/"paused"/"recent", because then `$warn` ("a human is
    needed", DESIGN.md §1) would paint for a mission that needs nobody."""
    st = status_value(mission.status)
    if section == "needs you":
        return glyph(st)  # the row note below carries the ◆ question too
    return glyph(SECTION_GLYPH.get(section, st))


MTABS = [
    ("Trajectory", "traj"),
    ("Graph", "graph"),
    ("Output", "output"),
    ("Diff", "diff"),
    ("Report", "report"),
    ("Gates", "gates"),
]


def parse_paths(raw: str) -> list[str]:
    """The Add-files form's value -> paths ready to hand to `add-files`.

    ONE PATH PER LINE is the primary form, so a path containing spaces
    (`/Users/me/My Papers`) can finally be entered at all; a line that is a
    shell-ish list is split like a shell (`shlex`), and the historical
    comma/space-separated single line still works. `~` is expanded.

    Splitting the whole input on spaces was the bug: a path with a space in it
    was unenterable. A line that DOES exist on disk is never split, so the two
    readings cannot disagree about a real path."""
    import shlex

    paths: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if Path(text).expanduser().exists():
            paths.append(str(Path(text).expanduser()))
            continue  # the line IS one real path, spaces and all
        try:
            parts = shlex.split(text)
        except ValueError:  # unbalanced quote: take the line literally
            parts = [text]
        for part in parts:
            paths.extend(str(Path(p).expanduser()) for p in part.replace(",", " ").split())
    return [p for p in paths if p.strip()]


class DashboardView(View):
    TAB = "dashboard"
    LABEL = "Dashboard"

    BINDINGS: ClassVar[list[BindingType]] = [
        # "/" jumps to the search bar, the way every list UI does. Modal-free:
        # the Input keeps its own keys (a printable "/" typed there is text).
        Binding("slash", "focus_search", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.snap = Snapshot()
        self.project: str = ALL
        self.filter = "all"
        # mission search: one query, one list. `_filtered()` is the only way a
        # mission reaches the list; the ✦ ranking orders the same list instead of
        # forking a second render path.
        self.query_text = ""
        self.ai_results: list[dict[str, str]] | None = None  # None = plain filter
        self.ai_query = ""  # the query the current ✦ search/ranking is for
        self.ai_note = ""  # AI status/error, shown on the one search status line
        self._ai_busy = False
        # bumped when a ranking is dismissed or the query moves on, so an
        # in-flight worker's result is dropped instead of landing on a bar the
        # user has already moved past
        self._ai_gen = 0
        # projects sidebar: plain name filter (no ranking, no closeness)
        self.project_query = ""
        self._debounce: Timer | None = None
        self.mission_id: str | None = None
        # the open mission's FULL row (plan, result): the snapshot only carries
        # light rows, so _poll_mission fetches this once per open and again only
        # when the mission's updated_at moves
        self.full_mission: Any = None
        self.full_failed = False  # the last full-row fetch came back empty
        # backoff for a failing full-row fetch (the Hub._next_try idiom): a row
        # that cannot be read must not be re-SELECTed on every tick
        self._full_failures = 0
        self._full_next_try = 0.0
        # what the open mission's detail was last painted from; an unchanged
        # poll (same key, cadence not due) repaints nothing
        self._painted_key: tuple[Any, ...] | None = None
        self.run_id: str | None = None
        self.mtab = "traj"
        # mission page state
        self.events: list[Any] = []
        self.last_seq = 0
        self.runs: list[Any] = []
        self.budget: Any = None
        self.run_cost: dict[str, float] = {}
        self.mission_gates: list[Any] = []
        self.report_loaded_for: str | None = None
        # overview enrichment (throttled)
        self.mission_cost: dict[str, float] = {}
        self.mission_role: dict[str, str] = {}
        self.spend_week = 0.0
        self._enriched_at = 0.0

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-head", id="dash-head"):
            yield Breadcrumb(id="dash-crumbs")
            with Horizontal(id="dash-controls", classes="toolbar"):
                yield Btn("Pause", "pause", variant="ghost")
                yield Btn("Resume", "resume", variant="ghost")
                yield Btn("Cancel", "cancel", variant="ghost")
                yield Btn("Priority", "priority", variant="ghost")
                yield Btn("Fetch result", "fetch", variant="ghost")
                yield Btn("Copy id", "copy-id", variant="ghost")
        with ContentSwitcher(initial="dash-overview", id="dash-pages"):
            with Vertical(id="dash-overview"):
                with Horizontal(id="dash-stats"):
                    yield Stat("running", id="st-running")
                    yield Stat("queued", id="st-queued")
                    yield Stat("need you", id="st-gates")
                    yield Stat("paused", id="st-paused")
                    yield Stat("done · 7d", id="st-done")
                    yield Stat("spend today", id="st-today")
                    yield Stat("spend · 7d", id="st-week")
                with Horizontal():
                    with Vertical(classes="sidebar", id="dash-side"):
                        with Horizontal(classes="side-actions"):
                            # only an upload-transport (repo-less) project has a
                            # files area to add to; a git project is told to push
                            yield Btn("Add files", "add-files", variant="ghost")
                        yield Label("Projects")
                        yield Input(placeholder="Filter projects…  (tab ⇥)", id="proj-search")
                        yield RowList(id="proj-list")
                    with Vertical(classes="main"):
                        with Horizontal(classes="subbar"):
                            yield ChipGroup(
                                [
                                    ("All", "all"),
                                    ("Active", "active"),
                                    ("Done", "done"),
                                    ("Failed", "failed"),
                                ],
                                "all",
                                id="dash-filter",
                            )
                        with Horizontal(classes="mission-search"):
                            yield Input(
                                placeholder="Search missions…  (/ to focus)",
                                id="mission-search",
                                # focus must never select-all: after ✦ the query
                                # is the ranking's, and one keystroke has to
                                # extend it, not wipe it
                                select_on_focus=False,
                            )
                            yield Btn("Search", "search", variant="ghost")
                            yield Btn("✦", "ai-search", variant="ghost", tooltip="AI search")
                        yield Static("", id="mission-note", classes="subbar-note")
                        yield RowList(
                            id="mission-list",
                            empty=f"[{FAINT}]No missions here yet. Start one from Work.[/]",
                        )
            with Vertical(id="dash-mission"):
                yield Static("", id="m-header")
                yield Static("", id="m-flow")
                with Horizontal():
                    with Vertical(classes="sidebar", id="m-side"):
                        yield Label("Sessions")
                        yield RowList(id="run-list")
                    with Vertical(classes="main"):
                        with Horizontal(classes="subbar"):
                            yield ChipGroup(MTABS, "traj", id="m-tabs")
                            yield Static("", classes="spacer")
                            yield Static("", id="m-follow", classes="subbar-note")
                        with ContentSwitcher(initial="m-traj", id="m-pages"):
                            yield Trajectory(id="m-traj")
                            yield GraphView(id="m-graph")
                            with VerticalScroll(id="m-output", classes="doc"):
                                yield Static("", id="m-output-body")
                            with VerticalScroll(id="m-diff", classes="doc"):
                                yield Static("", id="m-diff-body")
                            with VerticalScroll(id="m-report", classes="doc"):
                                yield Markdown("", id="m-report-body")
                            with VerticalScroll(id="m-gates", classes="doc"):
                                yield Vertical(id="m-gates-body")
                with Horizontal(id="m-msgbar"):
                    yield Input(
                        placeholder="Message this mission's orchestrator…  (enter to send)",
                        id="m-msg",
                    )
                    yield Btn("Send", "msg-send", variant="primary")

    def on_mount(self) -> None:
        self._paint_head()
        self._paint_search_note()

    def hints(self) -> list[tuple[str, str]]:
        if self.mission_id:
            return [("click", "focus a session"), ("esc", "back"), ("ctrl+c", "copy")]
        return [
            ("/", "search"),
            ("tab ⇥", "cycle focus"),
            ("click", "open mission"),
            ("↑↓", "move"),
            ("enter", "open"),
        ]

    def shown(self) -> None:
        target = "#run-list" if self.mission_id else "#mission-list"
        self.query_one(target, RowList).focus()

    def focus_mission_id(self) -> str | None:
        return self.mission_id

    async def control(self, mission_id: str, verb: str) -> bool:
        return await actions.control(self.rw, mission_id, verb)

    # -- snapshot ------------------------------------------------------------------

    def on_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        self.mark_time_painted()
        self._paint_overview()
        self._paint_head()
        if time.time() - self._enriched_at > 15:
            self._enriched_at = time.time()
            self._enrich()
        if self.mission_id:
            self._paint_mission_static()
            self._poll_mission()

    def on_tick(self, snap: Snapshot) -> None:
        self.snap = snap
        if self.time_repaint_due():
            # the data is unchanged but `ago(...)` text is not: repaint only what
            # carries time (the list rows, the open mission's header)
            self._paint_overview()
            self._painted_key = None  # the open mission repaints below
        if time.time() - self._enriched_at > 15:
            self._enriched_at = time.time()
            self._enrich()
        if self.mission_id:
            self._poll_mission()

    @work(exclusive=True, group="dash-enrich")
    async def _enrich(self) -> None:
        """Cost + active role for the missions people look at (throttled)."""
        data = self.hub.data
        week = await self.hub.guard(
            lambda: data.spend_history(bucket="total", since=time.time() - 7 * 86400), []
        )
        self.spend_week = sum(float(r.get("cost_usd") or 0) for r in week)
        # NOTE: this selection still keys off `actions.ACTIVE` while the
        # sections key off `section_for`, so the one divergent case below has no
        # role/cost chip: a TERMINAL mission (cancelled/failed/done) that still
        # holds an open gate lands under "needs you" but is not ACTIVE, so
        # neither `visible` nor the role branch reaches it. Aligning it with
        # `section_for` would also drop the [:12] cap for every non-recent row
        # (cost chips would appear on rows that never had one), so the chips
        # are deliberately left alone.
        visible = [m for m in self._filtered() if status_value(m.status) in actions.ACTIVE]
        visible += [m for m in self._filtered() if m not in visible][:12]
        for mission in visible:
            self.mission_cost[mission.id] = await self.hub.guard(
                partial(data.mission_spend, mission.id), self.mission_cost.get(mission.id, 0.0)
            )
            if status_value(mission.status) in actions.ACTIVE:
                runs = await self.hub.guard(partial(data.mission_runs, mission.id), [])
                live = [
                    r for r in runs if status_value(r.status) in ("running", "halted_escalated")
                ]
                self.mission_role[mission.id] = role_of(live[-1].id) if live else ""
        # the app can close while this worker awaits (the awaits above): the
        # paint then queries '#st-running' on a view whose children teardown
        # already removed, and NoMatches killed the worker mid-test. Same guard
        # as _poll_mission (m7 findings #19): only the paint, never the awaits.
        try:
            self._paint_overview()
        except NoMatches:
            return

    # -- overview -------------------------------------------------------------------

    def _filtered(self) -> list[Any]:
        out = [m for m in self.snap.missions if self._passes(m)]
        ranked = self._ranking()
        if ranked is not None:
            return self._in_rank_order(out, ranked)
        return search.search_missions(out, self.query_text)

    def _passes(self, mission: Any) -> bool:
        """Does the project/status filter let this mission into the list?"""
        if self.project == NONE and mission.project_id:
            return False
        if self.project not in (ALL, NONE) and mission.project_id != self.project:
            return False
        st = status_value(mission.status)
        # Active is exactly "everything the Dashboard does not file under
        # recent": needs you + queued + running + paused.
        if self.filter == "active" and section_for(mission, self.snap) == "recent":
            return False
        if self.filter == "done" and st != "done":
            return False
        if self.filter == "failed" and st not in ("failed", "cancelled"):
            return False
        return True

    def _ranking(self) -> list[dict[str, str]] | None:
        """The ✦ ranking, but only while it still answers the query in the box:
        typing a new query (or clearing it) expires the old ordering."""
        if self.ai_results is None:
            return None
        if self.ai_query != self.query_text.strip():
            return None
        return self.ai_results

    def _in_rank_order(self, missions: list[Any], ranked: list[dict[str, str]]) -> list[Any]:
        """The ranked missions that survive the project/status filters, best
        first. Same list object as the plain search: the model decides the ORDER
        of the hits only. The query still decides who is in the list, so a pick
        that is not a strict match for it is dropped, never resurrected (a
        zero-match query must never grow rows because ✦ was pressed)."""
        order = {hit["id"]: i for i, hit in enumerate(ranked)}
        hits = [m for m in search.search_missions(missions, self.query_text) if m.id in order]
        hits.sort(key=lambda m: order[m.id])
        return hits

    def _reason(self, mission_id: str) -> str:
        ranking = self._ranking() or []
        for hit in ranking:
            if hit["id"] == mission_id:
                return str(hit.get("reason") or "")
        return ""

    def _paint_overview(self) -> None:
        snap = self.snap
        # the strip counts SECTIONS over the WHOLE snapshot, ignoring the
        # current filter and project selection, so a number here need NOT match
        # the rows painted below it (filter="done" shows #st-running = 2 and no
        # running rows); the rows are the filtered view of these same buckets
        by_section: dict[str, int] = dict.fromkeys(SECTIONS, 0)
        for m in snap.missions:
            title = section_for(m, snap)
            by_section[title] = by_section.get(title, 0) + 1
        week_ago = time.time() - 7 * 86400
        done_week = sum(
            1 for m in snap.missions if status_value(m.status) == "done" and m.updated_at > week_ago
        )
        if not snap.loaded:
            # owner screenshot: zeros everywhere looked like real data while the
            # first refresh was still pending (or the database was unreachable)
            note = "offline" if not snap.online else "connecting…"
            for sid in (
                "st-running",
                "st-queued",
                "st-gates",
                "st-paused",
                "st-done",
                "st-today",
                "st-week",
            ):
                self.query_one(f"#{sid}", Stat).set("—", note)
            return
        self._paint_running_tile()
        self.query_one("#st-queued", Stat).set(str(by_section["queued"]))
        # this counts OPEN GATES, deliberately: it is the same number as the
        # Gates tab badge (app.py's `TabBar.set_badge("gates", len(snap.gates))`),
        # and two open gates on one mission really are two decisions waiting.
        # The "needs you" section HEADER counts missions instead, so a mission
        # with two open gates shows 2 here and 1 there: gates vs missions.
        self.query_one("#st-gates", Stat).set(str(len(snap.gates)))
        self.query_one("#st-paused", Stat).set(str(by_section["paused"]))
        self.query_one("#st-done", Stat).set(str(done_week))
        self.query_one("#st-today", Stat).set(money(snap.spend_today))
        self.query_one("#st-week", Stat).set(
            money(self.spend_week) if self._enriched_at else "—", "" if self._enriched_at else "…"
        )
        self.query_one("#st-gates").set_class(bool(snap.gates), "-warn")

        # projects (built by the same helper the spinner tick uses): a plain
        # case-insensitive name filter, with the pinned rows (All projects / No
        # project) always there so a filter never strands the owner without a
        # way back to everything.
        plist = self.query_one("#proj-list", RowList)
        plist.set_rows(self._proj_rows())
        # the selection lives in self.project, not in the row list, so a project
        # filtered out of view keeps filtering missions and comes back selected
        # when the filter clears
        plist.select(self.project if self.project in plist.keys else None)

        # missions, grouped
        missions = self._filtered()
        groups: dict[str, list[Any]] = {title: [] for title in SECTIONS}
        for m in missions:
            groups.setdefault(section_for(m, snap), []).append(m)
        mrows: list[RowData] = []
        for title, group in groups.items():
            if not group:
                continue
            colour = WARN if title == "needs you" else FAINT
            mrows.append(
                RowData(
                    f"{HEADER}{title}",
                    f"[{colour}][b]{title.upper()}[/b]  {len(group)}[/]",
                    classes="-header",
                )
            )
            for m in group:
                mrows.append(RowData(m.id, self._mission_row(m, title)))
        mlist = self.query_one("#mission-list", RowList)
        # an empty list must say WHY it is empty: a search miss, a project/status
        # filter that left nothing, or a store with no missions yet
        query = esc(self.query_text.strip())
        cause = self._filter_label()  # "" when only the text search narrows
        if missions or not snap.missions:
            mlist.set_empty(f"[{FAINT}]No missions here yet. Start one from Work.[/]")
        elif query and cause:
            mlist.set_empty(f"[{FAINT}]No missions match “{query}” in {cause}.[/]")
        elif query:
            mlist.set_empty(f"[{FAINT}]No missions match “{query}”.[/]")
        else:
            mlist.set_empty(f"[{FAINT}]No missions in {cause}.[/]")
        mlist.set_rows(mrows)
        self._paint_search_note()

    def _scope(self) -> list[Any]:
        """The missions the project/status chips let through, BEFORE the text
        search or the ✦ ranking: what a query is answered against."""
        return [m for m in self.snap.missions if self._passes(m)]

    def _filter_label(self) -> str:
        """The project/status filter that narrows the list, as words ('project
        “rework”', 'the failed filter'), or "" when only the text search does."""
        project = ""
        if self.project == NONE:
            project = "the no-project filter"
        elif self.project != ALL:
            project = f"project “{esc(self.snap.project_name(self.project))}”"
        status = f"the {self.filter} filter" if self.filter != "all" else ""
        if project and status:
            return f"{status} in {project}"
        return status or project

    # -- spinner ---------------------------------------------------------------------

    def _stamp(self, key: str | None) -> float | None:
        """Newest event epoch for ONE entity (a mission id, a run id, the
        orchestrator pseudo id, or "*" for the aggregate), from the snapshot.

        Events are the only live signal a role gives: a run row's `updated_at`
        moves when its status changes and then stays put for the whole session,
        so it cannot tell "working" from "wedged".

        Strictly that entity's own stamp. It is never a max over several keys
        (every run event also stamps its mission, so a max would let one live
        run make a wedged sibling look busy) and never falls back to another
        entity's stamp. No key means UNKNOWN, and unknown animates. "*" is the
        newest across all running missions: use it only where an aggregate is
        genuinely meant (the stats tile, the project badge)."""
        return self.snap.last_event.get(key) if key else None

    def _mark(
        self, status: Any, last: float | None, frame: str | None = None, now: float | None = None
    ) -> str:
        """The marker for a status: a frame when it is genuinely running, the
        static idle glyph when it is a zombie. Everything else keeps the
        existing status glyph (pending/on_hold/gate never animate).

        `frame`/`now` of None mean "compute it now": the store poll paints
        without a tick, and it must still render a real one-cell frame (never
        an empty span), also while the terminal is unfocused and no tick runs."""
        if status_value(status) != "running":
            return glyph(status)
        clock = time.time() if now is None else now
        return run_mark(spinner.is_stalled(clock, last), frame)

    def _row_mark(
        self, mission: Any, section: str, frame: str | None = None, now: float | None = None
    ) -> str:
        """A mission row's leading icon: the spinner (or the idle glyph) for a
        mission whose STORED status is running, wherever the section filed it (a
        running mission with an open gate sits under "needs you" and is still
        working, so it keeps animating); every other row takes the SECTION's
        glyph (a stale awaiting_gate filed under running must not paint amber)."""
        status = status_value(mission.status)
        if status == "running":
            return self._mark(status, self._stamp(mission.id), frame, now)
        return row_glyph(mission, section)

    def _paint_running_tile(self, frame: str | None = None, now: float | None = None) -> None:
        """The dashboard "running" tile. ONE rendering shared by the poll and the
        tick, so the tile cannot alternate between two texts."""
        stat = self.query_one("#st-running", Stat)
        running = sum(1 for m in self.snap.missions if section_for(m, self.snap) == "running")
        if not running:
            stat.set("0")
            return
        clock = time.time() if now is None else now
        last = self._stamp("*")
        if spinner.is_stalled(clock, last):
            human = spinner.human_duration(spinner.silence(clock, last))
            stat.set(str(running), f"idle {human}")
        else:
            stat.set(f"{run_mark(False, frame)} {running}", "working", markup=True)

    def tick(self, frame: str, now: float) -> None:
        """Repaint only the rows that carry a running marker (10fps).

        Rebuilding the markup string with the next frame IS the repaint:
        RowList.set_rows updates in place (Row.refresh_data only calls
        update() when the markup changed) and Stat.set does the same, so this
        mounts nothing and touches the store not at all."""
        with self.painting():
            if self.mission_id is None:
                self._tick_overview(frame, now)
            else:
                self._tick_mission(frame, now)

    def _tick_overview(self, frame: str, now: float) -> None:
        missions = self._filtered()
        mrows: list[RowData] = []
        for title in SECTIONS:
            group = [m for m in missions if section_for(m, self.snap) == title]
            if not group:
                continue
            colour = WARN if title == "needs you" else FAINT
            mrows.append(
                RowData(
                    f"{HEADER}{title}",
                    f"[{colour}][b]{title.upper()}[/b]  {len(group)}[/]",
                    classes="-header",
                )
            )
            for m in group:
                mrows.append(RowData(m.id, self._mission_row(m, title, frame=frame, now=now)))
        if self.ai_results is not None:
            mrows = self._ai_rows(frame=frame, now=now)
        # the same key list as the poll's paint, so set_rows takes the cheap
        # in-place path instead of remounting the list
        self.query_one("#mission-list", RowList).set_rows(mrows)
        self.query_one("#proj-list", RowList).set_rows(self._proj_rows(frame, now))
        self._paint_running_tile(frame, now)

    def _tick_mission(self, frame: str, now: float) -> None:
        mission = self.snap.mission(self.mission_id)
        if mission is None:
            return
        if status_value(mission.status) == "running":
            self._paint_mission_static(frame=frame, now=now)
        self.query_one("#run-list", RowList).set_rows(self._run_rows(frame, now))
        if self.mtab == "traj":
            self._paint_traj_tail(frame, now)

    def _proj_rows(self, frame: str | None = None, now: float | None = None) -> list[RowData]:
        """The project pane's rows. Split out so the spinner tick and the poll
        build them the same way (and with the same keys)."""
        snap = self.snap
        rows = [RowData(ALL, self._proj_row("All projects", snap.missions, frame=frame, now=now))]
        needle = self.project_query.strip().lower()
        for p in snap.projects:
            if needle and needle not in str(p.name).lower():
                continue
            ms = [m for m in snap.missions if m.project_id == p.id]
            rows.append(RowData(p.id, self._proj_row(p.name, ms, frame=frame, now=now)))
        loose = [m for m in snap.missions if not m.project_id]
        if loose:
            rows.append(
                RowData(NONE, self._proj_row("No project", loose, dim=True, frame=frame, now=now))
            )
        return rows

    def _proj_row(
        self,
        name: str,
        missions: list[Any],
        dim: bool = False,
        *,
        frame: str | None = None,
        now: float | None = None,
    ) -> str:
        # the chips use section_for exactly like the sections, or a project
        # could show ◆ while none of its missions sits under "needs you"
        sections = [section_for(m, self.snap) for m in missions]
        running = sections.count("running")
        queued = sections.count("queued")
        waiting = sections.count("needs you")
        badges = []
        if running:
            # the project badge is the second running glyph on the overview
            last = self._stamp("*")
            mark = self._mark("running", last, frame, now)
            badges.append(f"{mark} {running}")
        if queued:
            badges.append(f"[{MUTED}]○ {queued}[/]")
        if waiting:
            badges.append(f"[{WARN}]◆ {waiting}[/]")
        label = f"[{MUTED}]{esc(name)}[/]" if dim else esc(name)
        return f"{label}  [{FAINT}]{len(missions)}[/]  " + " ".join(badges)

    def _mission_row(
        self,
        m: Any,
        section: str | None = None,
        *,
        frame: str | None = None,
        now: float | None = None,
    ) -> str:
        # callers that already grouped pass the section; a lone row works it out
        section = section or section_for(m, self.snap)
        st = status_value(m.status)
        bits = []
        if self.project == ALL and m.project_id:
            bits.append(self.snap.project_name(m.project_id))
        role = self.mission_role.get(m.id)
        if role and st in actions.ACTIVE:
            bits.append(role)
        if m.id in self.mission_cost:
            bits.append(money(self.mission_cost[m.id]))
        bits.append(ago(m.created_at))
        bits.append(m.id)
        gate_note = ""
        gates = self.snap.gates_for(m.id)
        if gates:  # same predicate as "needs you": show what needs deciding
            gate_note = f"\n  [{WARN}]◆ {esc(clip(gates[0].question, 90))}[/]"
        meta = f"[{FAINT}]{esc('  ·  '.join(bits))}[/]"
        # the ✦ reason is OUR markup, not mission text: escaping it with the
        # rest of the line printed "[#a394f7]closest to the query[/]" literally
        reason = esc(clip(self._reason(m.id), 120))
        if reason:
            meta += f"  [{ACCENT}]{reason}[/]"
        mark = self._row_mark(m, section, frame, now)
        return f"{mark} [b]{esc(clip(m.objective, 100))}[/b]\n  {meta}{gate_note}"

    def _ai_rows(self, *, frame: str | None = None, now: float | None = None) -> list[RowData]:
        """The ✦ ranking: one row per ranked mission, objective + its reason.
        Built from the snapshot so a mission that vanished mid-search drops."""
        rows: list[RowData] = []
        for hit in self.ai_results or []:
            mission = self.snap.mission(hit["id"])
            if mission is None:
                continue
            section = section_for(mission, self.snap)
            reason = esc(hit.get("reason") or "")
            mark = self._row_mark(mission, section, frame, now)
            rows.append(
                RowData(
                    mission.id,
                    f"{mark} [b]{esc(clip(mission.objective, 100))}[/b]\n"
                    f"  [{FAINT}]{esc(clip(mission.id, 60))}[/]"
                    + (f"  [{ACCENT}]{reason}[/]" if reason else ""),
                )
            )
        return rows

    def on_row_list_activated(self, event: RowList.Activated) -> None:
        event.stop()
        lid = event.rows.id
        if lid == "proj-list":
            self.project = event.key
            self._paint_overview()
            self._paint_head()
        elif lid == "mission-list":
            if event.via_mouse or event.enter:
                self.open_mission(event.key)
        elif lid == "run-list":
            self.run_id = None if event.key == ALL else event.key
            self._paint_head()
            self._paint_mission_static()
            self._rerender_traj()
            self._paint_detail_tabs()

    def on_chip_group_changed(self, event: ChipGroup.Changed) -> None:
        event.stop()
        if event.group.id == "dash-filter":
            self.filter = event.value
            self._paint_overview()
        elif event.group.id == "m-tabs":
            self.mtab = event.value
            self.query_one("#m-pages", ContentSwitcher).current = f"m-{event.value}"
            self._paint_detail_tabs()

    # -- search -----------------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#mission-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "proj-search":
            event.stop()
            self.project_query = event.value
            with self.painting():
                self._paint_overview()
            return
        if event.input.id != "mission-search":
            return
        event.stop()
        self.query_text = event.value
        self._expire_ranking()  # typing means the plain filter again
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(0.25, self._apply_search)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the bar always means the same thing: filter the list on the
        query (the ✦ ranking, when it is there, stays as the list's order).
        Down moves into the results, as in the Library."""
        if event.input.id == "m-msg":
            event.stop()
            self._send_message()
            return
        if event.input.id == "proj-search":
            event.stop()
            self._focus_projects()
            return
        if event.input.id != "mission-search":
            return
        event.stop()
        self.query_text = event.value
        if self.query_text.strip() != self.ai_query:
            # a different query: whatever ✦ produced no longer answers it
            self._expire_ranking()
        self._apply_search()

    def on_key(self, event: Any) -> None:
        """↓ out of either search bar lands on that bar's list."""
        if event.key != "down":
            return
        focused = self.screen.focused
        if not isinstance(focused, Input):
            return
        rows = self.query_one(
            "#proj-list" if focused.id == "proj-search" else "#mission-list", RowList
        )
        if not rows.selectable:
            return
        event.stop()
        rows.focus()
        if focused.id == "proj-search":
            rows.select(self.project if self.project in rows.selectable else None)
        else:
            rows.select(rows.selectable[0])

    def _focus_projects(self) -> None:
        rows = self.query_one("#proj-list", RowList)
        rows.focus()
        rows.select(self.project if self.project in rows.selectable else None)

    def _expire_ranking(self) -> None:
        """The ✦ order belongs to the query it was asked for. Any change to the
        query drops it (and any in-flight search), so AI-ordered rows can never
        impersonate the plain filter's result."""
        if self.ai_results is None and not self.ai_note and not self._ai_busy:
            return
        self._ai_gen += 1  # an in-flight worker's answer is stale from here on
        self.ai_results = None
        self.ai_query = ""
        self.ai_note = ""
        if self._ai_busy:
            self._ai_busy = False
            self.workers.cancel_group(self, "dash-ai-search")

    def _apply_search(self) -> None:
        self._paint_overview()

    def _paint_search_note(self) -> None:
        """The ONE status line under the search bar: the ✦ state (searching…, a
        result count, a failure) or the plain filter's count / no-match state."""
        plain = self._filtered()
        note = ""
        if self._ai_busy:
            note = f"[{ACCENT}]✦ searching…[/]"
        elif self.ai_note:
            note = f"[{WARN}]{esc(self.ai_note)}[/]"
        elif self._ranking() is not None:
            # the model returns at most MAX_RESULTS ids, so this list is its
            # picks out of the matches, not the whole match set: say so
            count = len(plain)
            matched = len(search.search_missions(self._scope(), self.query_text))
            scope = f"{count} of {matched}" if matched > count else f"{count}"
            note = f"[{ACCENT}]✦ top {scope} from AI[/]  [{FAINT}]esc clears[/]"
        with self.painting():
            widget = self.query_one("#mission-note", Static)
            widget.update(note)
            # a blank note must not eat a line of the list
            widget.set_class(bool(note), "has-text")

    def _run_ai_search(self) -> None:
        query = self.query_text.strip()
        if not query:
            self.ai_note = "AI search needs a query"
            self._paint_search_note()
            return
        self._ai_search(query)

    @work(exclusive=True, group="dash-ai-search")
    async def _ai_search(self, query: str) -> None:
        from rework.config import load_catalog
        from rework.kernel.llm.adapters import build_provider

        gen = self._ai_gen
        # A query must isolate, including here: with no strict match there is
        # nothing to rank, so the model is not called and the list keeps saying
        # why it is empty.
        if not search.search_missions(self._scope(), query):
            self.ai_note = f"✦ no missions match “{query}” — nothing to rank"
            self._apply_search()
            return
        self._ai_busy = True
        self.ai_results = None
        self.ai_query = query  # this search answers this query, nothing else
        self.ai_note = ""
        self._apply_search()
        # One clock over BOTH halves: the per-candidate run fetch is a query per
        # mission, so leaving it unbounded would let "✦ searching…" outlive the
        # budget the model call is already held to.
        deadline = time.monotonic() + search.TIMEOUT_S
        try:
            catalog = load_catalog()
            entry = search.pick_entry(catalog)
            if entry is None:
                self.ai_note = "AI search unavailable: no model with credentials"
            else:
                picks = search.candidates(self.snap.missions, query)
                try:
                    summaries = await asyncio.wait_for(
                        self._fetch_summaries(picks), self._budget(deadline)
                    )
                except TimeoutError:
                    # a slow/hung store must not cost the ranking: rank what fits
                    summaries = await self._fetch_summaries(picks, deadline=deadline)
                ranked = await search.run_ai_search(
                    build_provider(entry),
                    search.build_ai_request(entry.model, query, summaries),
                    summaries,
                    self._budget(deadline),
                )
                if gen != self._ai_gen:
                    return  # dismissed/expired while it ran: the note is clear
                self.ai_results = ranked
                if not ranked:
                    self.ai_note = "✦ found no mission for that query"
        except search.AISearchError as exc:
            if gen == self._ai_gen:
                self.ai_note = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a worker must never take the app down
            if gen == self._ai_gen:
                self.ai_note = f"AI search failed: {str(exc)[:160] or type(exc).__name__}"
        finally:
            if gen == self._ai_gen:
                self._ai_busy = False
                with self.painting():
                    self._paint_overview()
                    self._paint_search_note()

    @staticmethod
    def _budget(deadline: float) -> float:
        """Time left before `deadline`. Past it we still hand over a token slice,
        so the model call reports a clean timeout instead of firing for nothing."""
        return max(deadline - time.monotonic(), _MIN_BUDGET)

    async def _fetch_summaries(
        self, picks: list[Any], deadline: float | None = None
    ) -> list[dict[str, str]]:
        """One compact summary per candidate. With a `deadline`, the results are
        fetched concurrently and whatever is not back by then is summarized
        without run output: a degraded ranking beats a stuck bar."""
        data = self.hub.data
        if deadline is None:
            plain: list[dict[str, str]] = []
            for mission in picks:
                runs = await self.hub.guard(partial(data.mission_runs, mission.id), [])
                plain.append(search.summarize(mission, runs))
            return plain
        tasks = [asyncio.ensure_future(data.mission_runs(mission.id)) for mission in picks]
        done: set[Any] = set()
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=self._budget(deadline))
        for task in tasks:
            if task not in done:
                task.cancel()
        summarised: list[dict[str, str]] = []
        for mission, task in zip(picks, tasks, strict=True):
            fresh: Any = []
            if task in done and not task.cancelled() and task.exception() is None:
                fresh = task.result()
            summarised.append(search.summarize(mission, fresh))
        return summarised

    def _dismiss_ai(self) -> None:
        if self.ai_results is None and not self.ai_note and not self._ai_busy:
            return
        self._expire_ranking()
        self._apply_search()

    # -- head -------------------------------------------------------------------------

    def _paint_head(self) -> None:
        parts: list[tuple[str, str]] = [("Dashboard", "root")]
        if self.project not in (ALL,):
            name = "No project" if self.project == NONE else self.snap.project_name(self.project)
            parts.append((name, "project"))
        if self.mission_id:
            mission = self.snap.mission(self.mission_id)
            if self.project == ALL and mission is not None and mission.project_id:
                parts.append((self.snap.project_name(mission.project_id), "project-of"))
            parts.append((self.mission_id, "mission"))
            if self.run_id:
                parts.append((role_of(self.run_id), "run"))
        self.query_one("#dash-crumbs", Breadcrumb).set_parts(parts)
        controls = self.query_one("#dash-controls")
        controls.display = self.mission_id is not None
        if self.mission_id:
            mission = self.snap.mission(self.mission_id)
            st = status_value(mission.status) if mission else ""
            for btn in controls.query(Btn):
                if btn.action in ("pause", "resume", "cancel", "fetch"):
                    btn.display = actions.can(btn.action, st)
                elif btn.action == "priority":
                    level = getattr(mission, "priority", "normal") if mission else "normal"
                    btn.set_label(f"Priority: {level}")

    def on_breadcrumb_navigate(self, event: Breadcrumb.Navigate) -> None:
        event.stop()
        if event.target == "root":
            self.project = ALL
            self.close_mission()
        elif event.target == "project":
            self.close_mission()
        elif event.target == "project-of":
            mission = self.snap.mission(self.mission_id)
            self.project = mission.project_id if mission and mission.project_id else ALL
            self.close_mission()
        elif event.target == "mission":
            self.run_id = None
            self.query_one("#run-list", RowList).select(ALL)
            self._paint_head()
            self._rerender_traj()
            self._paint_detail_tabs()

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        if event.action == "search":
            event.stop()
            self.query_text = self.query_one("#mission-search", Input).value
            if self.query_text.strip() != self.ai_query:
                self._expire_ranking()
            self._apply_search()
            self.query_one("#mission-search", Input).focus()
            return
        if event.action == "ai-search":
            # the ✦ button runs on the SAME query text, into the SAME list. It
            # hands focus back to the bar, so Enter means "filter" here exactly
            # as it does after typing (it is a button, not a mode).
            event.stop()
            self.query_text = self.query_one("#mission-search", Input).value
            self._expire_ranking()
            self._run_ai_search()
            bar = self.query_one("#mission-search", Input)
            bar.focus()
            bar.cursor_position = len(bar.value)
            return
        if event.action == "add-files":
            # the selected PROJECT decides, not a mission: works with nothing open
            event.stop()
            self.add_project_files()
            return
        if not self.mission_id:
            return
        event.stop()
        if self._gate_button(event.btn):
            return
        if event.action == "msg-send":
            self._send_message()
            return
        verb = event.action
        if verb == "copy-id":
            self.rw.copy_to_clipboard(self.mission_id)
            self.toast(f"Copied {self.mission_id}")
            return
        if verb == "cancel" and not event.btn.has_class("-armed"):
            event.btn.add_class("-armed")
            event.btn.set_label("Really cancel?")
            event.btn.add_class("danger")
            self.set_timer(3, lambda b=event.btn: self._disarm(b))
            return
        if verb == "cancel":
            self._disarm(event.btn)
        if verb == "priority":
            mission = self.snap.mission(self.mission_id)
            current = getattr(mission, "priority", "normal")
            self.run_worker(actions.prioritize(self.rw, self.mission_id, current), group="control")
            return
        self.run_worker(actions.control(self.rw, self.mission_id, verb), group="control")

    def add_project_files(self) -> None:
        """Push local files into the selected project's files area (upload
        transport only). The daemon keeps them; missions seed from there."""
        from rework.missions import project_is_upload

        project = next((p for p in self.snap.projects if p.id == self.project), None)
        if project is None:
            self.toast("Select a project first", severity="warning")
            return
        if not project_is_upload(project.settings):
            self.toast("this project uses git: push a branch instead", severity="warning")
            return

        def done(values: dict[str, str] | None) -> None:
            if values is None:
                return
            paths = parse_paths(values.get("paths", ""))
            if not paths:
                self.toast("No paths given", severity="warning")
                return
            self.run_worker(self._add_project_files(project.id, paths), group="add-files")

        self.rw.push_screen(
            Form(
                f"Add files to {project.name}",
                [
                    Field(
                        "paths",
                        "Local files/folders on this machine: one path per line"
                        " (~ is expanded; commas and spaces also separate paths)",
                        kind="area",
                    )
                ],
                submit="Add",
            ),
            done,
        )

    async def _add_project_files(self, project_id: str, paths: list[str]) -> None:
        try:
            changed = await self.hub.data.add_project_files(project_id, paths)
        except Exception as exc:
            self.toast(f"Could not add files: {exc}", severity="error")
            return
        if changed:
            self.toast(f"{len(changed)} path(s) added to the project files")
        else:
            self.toast("no changes (identical)")
        await self.hub.refresh()

    def _disarm(self, btn: Btn) -> None:
        btn.remove_class("-armed")
        btn.remove_class("danger")
        btn.set_label("Cancel")

    # -- mission page ---------------------------------------------------------------------

    def open_mission(self, mission_id: str, run_id: str | None = None) -> None:
        if mission_id != self.mission_id:
            self.events = []
            self.last_seq = 0
            self.runs = []
            self.budget = None
            self.run_cost = {}
            self.mission_gates = []
            self.full_mission = None
            self.full_failed = False
            self._full_failures = 0
            self._full_next_try = 0.0
            self.report_loaded_for = None
            self.query_one("#m-traj", Trajectory).clear()
            self.query_one("#run-list", RowList).set_rows([])
        self.mission_id = mission_id
        self.run_id = run_id
        self._painted_key = None
        self.query_one("#dash-pages", ContentSwitcher).current = "dash-mission"
        self._paint_head()
        self._paint_mission_static()
        self._poll_mission()
        self.rw.refresh_hints()
        self.query_one("#run-list", RowList).focus()

    def close_mission(self) -> None:
        self.mission_id = None
        self.full_mission = None
        self.full_failed = False
        self._painted_key = None
        self.run_id = None
        self.query_one("#dash-pages", ContentSwitcher).current = "dash-overview"
        self._paint_overview()
        self._paint_head()
        self.rw.refresh_hints()
        self.query_one("#mission-list", RowList).focus()

    def back(self) -> bool:
        if self._ai_busy:
            # an in-flight ranking is dismissable state: drop it and stay put.
            # _expire_ranking also bumps _ai_gen, so a worker finishing inside
            # the cancellation window cannot land stale rows on the list.
            self._expire_ranking()
            self._apply_search()
            return True
        if self.ai_results is not None or self.ai_note:
            self._dismiss_ai()
            return True
        proj_bar = self.query_one("#proj-search", Input)
        bar = self.query_one("#mission-search", Input)
        # esc acts on the bar the user is actually typing in: checking the
        # project filter first cleared the sidebar filter out from under a query
        # typed into the mission bar
        if bar.has_focus:
            bar.value = ""
            self.query_text = ""
            self._expire_ranking()
            self._apply_search()
            if self.mission_id:
                self.query_one("#run-list", RowList).focus()
            else:
                self.query_one("#mission-list", RowList).focus()
            return True
        if proj_bar.has_focus:
            proj_bar.value = ""
            self.project_query = ""
            self._focus_projects()
            return True
        if proj_bar.value:
            # esc from the mission list also clears a project-name filter, so a
            # hidden sidebar never looks like a broken mission list
            proj_bar.value = ""
            self.project_query = ""
            self._paint_overview()
            return True
        if self.run_id:
            self.run_id = None
            self.query_one("#run-list", RowList).select(ALL)
            self._paint_head()
            self._rerender_traj()
            self._paint_detail_tabs()
            return True
        if self.mission_id:
            self.close_mission()
            return True
        if self.project != ALL:
            self.project = ALL
            self._paint_overview()
            self._paint_head()
            return True
        return False

    @work(exclusive=True, group="dash-mission")
    async def _poll_mission(self) -> None:
        mission_id = self.mission_id
        if not mission_id:
            return
        data = self.hub.data
        await self._load_full_mission(mission_id)
        if self.mission_id != mission_id:
            return
        fresh = await self.hub.guard(lambda: data.mission_events(mission_id, self.last_seq), [])
        if self.mission_id != mission_id:
            return
        if fresh:
            self.events.extend(fresh)
            self.last_seq = max(int(e.seq or 0) for e in self.events)
            self._append_traj(fresh)
        self.runs = await self.hub.guard(lambda: data.mission_runs(mission_id), self.runs)
        self.budget = await self.hub.guard(lambda: data.mission_budget(mission_id), self.budget)
        for run in self.runs:
            if run.id not in self.run_cost or status_value(run.status) == "running":
                self.run_cost[run.id] = await self.hub.guard(
                    partial(data.run_spend, run.id), self.run_cost.get(run.id, 0.0)
                )
        orch_id = self._orchestrator_id()
        if orch_id:  # the orchestrator has no Run row; its charges carry the pseudo id
            self.run_cost[orch_id] = await self.hub.guard(
                partial(data.run_spend, orch_id), self.run_cost.get(orch_id, 0.0)
            )
        self.mission_gates = await self.hub.guard(
            lambda: data.gates(mission_id), self.mission_gates
        )
        if self.mission_id != mission_id:
            return
        # the app can close while this worker awaits. Teardown removes children
        # first, so the view is still attached while '#m-header' (composed
        # statically, so only ever missing then) is gone; painting raised
        # NoMatches and failed tests under load (m7 findings #19: 2 of 8 runs)
        key = self._paint_key()
        if key == self._painted_key:
            return  # nothing this poll read has changed since the last paint
        self._painted_key = key
        try:
            self._paint_mission_static()
            self._paint_runs()
            self._paint_detail_tabs()
            # the poll supplies correctness (also while unfocused, when no tick
            # runs, and whichever detail tab is open); the tick supplies motion
            self._paint_traj_tail()
        except NoMatches:
            return

    def _paint_key(self) -> tuple[Any, ...]:
        """Everything the open mission's header, run list and detail tabs are
        painted from. Time text is not in it: the cadence clears the key."""
        mission = self._detail_mission()
        budget = self.budget
        return (
            self.run_id,
            self.mtab,
            self.full_failed,
            None
            if mission is None
            else (
                status_value(mission.status),
                mission.updated_at,
                getattr(mission, "light", False),
            ),
            tuple((r.id, status_value(r.status), r.step, r.updated_at) for r in self.runs),
            None
            if budget is None
            else (budget.spent_usd, budget.input_tokens, budget.output_tokens),
            tuple((g.id, status_value(g.status), g.decided_at) for g in self.mission_gates),
            tuple(sorted(self.run_cost.items())),
            len(self.events),
            self._report_key(),
            # the spinner reads "working" vs "idle 7m" off this stamp: a poll alone
            # (no tick while unfocused) must repaint the header when it moves
            self._stamp(self.mission_id),
        )

    async def _load_full_mission(self, mission_id: str) -> None:
        """Fetch the open mission's full row (plan, result) off the light
        snapshot row. Once per open, then only when updated_at moves (any plan or
        result write bumps it); a snapshot row that is already full is used as is."""
        row = self.snap.mission(mission_id)
        if row is not None and not getattr(row, "light", False):
            self.full_mission = row
            return
        if self.full_failed and time.monotonic() < self._full_next_try:
            return  # the last fetch failed: wait out the backoff
        have = self.full_mission
        if (
            have is not None
            and have.id == mission_id
            and (row is None or have.updated_at == row.updated_at)
        ):
            return
        data = self.hub.data
        got = await self.hub.guard(lambda: data.get_mission(mission_id), None)
        if self.mission_id != mission_id:
            return
        self.full_failed = got is None  # offline or a store error: say so, retry later
        if got is not None:
            self.full_mission = got
            self._full_failures = 0
            self._full_next_try = 0.0
        else:
            self._full_failures += 1
            self._full_next_try = time.monotonic() + min(30.0, 2.0**self._full_failures)

    def _detail_mission(self) -> Any:
        """The open mission for plan/result reads: the full row once fetched,
        else the snapshot's light row (plan and result are None on it)."""
        full = self.full_mission
        if full is not None and full.id == self.mission_id:
            return full
        return self.snap.mission(self.mission_id)

    def _paint_mission_static(self, *, frame: str | None = None, now: float | None = None) -> None:
        mission = self._detail_mission()
        header = self.query_one("#m-header", Static)
        if mission is None or (getattr(mission, "light", False) and not self.full_failed):
            header.update(f"[{FAINT}]loading {esc(self.mission_id)}…[/]")
            return
        if getattr(mission, "light", False):
            # the full row could not be fetched: show the light facts we hold
            # and say what is missing, instead of a permanent "loading"
            st = status_value(mission.status)
            end = time.time() if st in actions.ACTIVE else mission.updated_at
            header.update(
                f"[b]{esc(mission.objective)}[/b]\n{status_text(st)}  [{MUTED}]"
                + esc(
                    f"started {ago(mission.created_at)} · {duration(end - mission.created_at)}"
                    "  ·  could not load plan/result (retrying)"
                )
                + "[/]"
            )
            return
        st = status_value(mission.status)
        plan = mission.plan or {}
        bits = [plan_label(plan)]
        orch = plan.get("orchestrator") or {}
        if orch.get("model"):
            bits.append(f"orchestrator {orch['model']}")
        if mission.project_id:
            bits.append(self.snap.project_name(mission.project_id))
        if plan.get("model"):
            bits.append(f"roles {plan['model']}")
        if plan.get("effort"):
            bits.append(f"effort {plan['effort']}")
        if self.budget is not None:
            bits.append(
                f"{money(self.budget.spent_usd)} · {tokens(self.budget.input_tokens)} in / "
                f"{tokens(self.budget.output_tokens)} out"
            )
        end = time.time() if st in actions.ACTIVE else mission.updated_at
        bits.append(f"started {ago(mission.created_at)} · {duration(end - mission.created_at)}")
        if self.full_failed:
            # a refetch failed after this row was fetched: it is stale, say so
            bits.append("could not load plan/result (retrying)")
        # The header represents the MISSION, so it is judged by the mission's own
        # stamp (any event of any of its runs or its orchestrator). That stamp
        # cannot say that ONE run went quiet while its siblings work, so a
        # silent run is named next to "working" instead of being masked.
        last = self._stamp(mission.id)
        clock = time.time() if now is None else now
        stalled = st == "running" and spinner.is_stalled(clock, last)
        if st == "running":
            mark = self._mark(st, last, frame, clock)
            if stalled:
                head = f"{mark} idle {spinner.human_duration(spinner.silence(clock, last))}"
            else:
                head = f"{mark} working"
                quiet = self._quiet_runs(clock)
                if quiet:
                    head += f"  [{FAINT}]" + esc(", ".join(quiet)) + "[/]"
        else:
            head = status_text(st)
        header.update(
            f"[b]{esc(mission.objective)}[/b]\n{head}  [{MUTED}]" + esc("  ·  ".join(bits)) + "[/]"
        )
        # same run-derived round structure as the graph tab, so the two
        # cannot drift (the strip is compact, the graph is the full flow)
        phases = mission_flow_summary(mission, self.runs, self.mission_gates)
        strip = f" [{FAINT}]──[/] ".join(f"{esc(p)} {glyph(st)}" for p in phases)
        self.query_one("#m-flow", Static).update(strip)

    def _run_sub(self, run: Any) -> str:
        bits = [str((run.model_ref or {}).get("id") or "default")]
        st = status_value(run.status)
        stats = (run.artifact or {}).get("run_stats") or {}
        if stats.get("wall_s"):
            bits.append(duration(stats["wall_s"]))
        else:
            end = time.time() if st == "running" else run.updated_at
            bits.append(duration(max(0.0, end - run.created_at)))
        if self.run_cost.get(run.id):
            bits.append(money(self.run_cost[run.id]))
        return " · ".join(bits)

    def _run_rows(self, frame: str | None = None, now: float | None = None) -> list[RowData]:
        """The sessions list. Split out so the spinner tick and the poll build
        the same rows (same keys, in-place update)."""
        rows = [RowData(ALL, f"[b]All activity[/b]\n[{FAINT}]{len(self.events)} events[/]")]
        orch_id = self._orchestrator_id()
        if orch_id:
            mission = self._detail_mission()
            st = status_value(mission.status) if mission else "running"
            model = ((mission.plan or {}).get("orchestrator") or {}).get("model") if mission else ""
            cost = self.run_cost.get(orch_id, 0.0)
            sub = " · ".join(x for x in (str(model or ""), money(cost) if cost else "") if x)
            mark = self._mark(
                "running" if st in actions.ACTIVE else st,
                self._stamp(orch_id),
                frame,
                now,
            )
            rows.append(
                RowData(
                    orch_id,
                    f"{mark} [b]orchestrator[/b]\n  [{FAINT}]{esc(sub)}[/]",
                )
            )
        for run in self.runs:
            mark = self._mark(run.status, self._stamp(run.id), frame, now)
            rows.append(
                RowData(
                    run.id,
                    f"{mark} [b]{esc(role_of(run.id))}[/b]  "
                    f"[{FAINT}]{esc(run.step)}[/]\n"
                    f"  [{FAINT}]{esc(self._run_sub(run))}[/]",
                )
            )
        return rows

    def _paint_runs(self) -> None:
        lst = self.query_one("#run-list", RowList)
        lst.set_rows(self._run_rows())
        lst.select(self.run_id or ALL)

    def _quiet_runs(self, clock: float) -> list[str]:
        """ "coder idle 7m" for each RUNNING run whose own stamp is past the stall
        threshold. A run with no stamp is unknown, not quiet."""
        out = []
        for run in self.runs:
            if status_value(run.status) != "running":
                continue
            last = self._stamp(run.id)
            if spinner.is_stalled(clock, last):
                age = spinner.human_duration(spinner.silence(clock, last))
                out.append(f"{role_of(run.id)} idle {age}")
        return out

    def _paint_traj_tail(self, frame: str | None = None, now: float | None = None) -> None:
        """The ONE trailing spinner step on the trajectory (surface c).

        It is a single Step updated in place: `update()` with a new markup is a
        one-line repaint, and removing it when nothing runs keeps a finished
        mission's log clean. With a run selected it reflects THAT run (its own
        stamp); otherwise it reflects the mission (the mission stamp).

        Called by the tick (motion) and by the store poll (correctness, also
        while unfocused), so a run that finished or wedged while the terminal
        was in the background cannot keep a stale "working" claim."""
        traj = self.query_one("#m-traj", Trajectory)
        mission = self.snap.mission(self.mission_id)
        if mission is None or status_value(mission.status) != "running":
            traj.spinner(None)
            return
        running = [r for r in self.runs if status_value(r.status) == "running"]
        orch_id = self._orchestrator_id()
        if self.run_id is not None:
            if self.run_id == orch_id:
                subject, key, step = "orchestrator", orch_id, ""
            else:
                picked = [r for r in running if r.id == self.run_id]
                if not picked:
                    traj.spinner(None)
                    return
                subject, key, step = role_of(picked[0].id), picked[0].id, str(picked[0].step or "")
        else:
            if not running:
                traj.spinner(None)
                return
            # name the run that is actually talking (an unknown stamp counts as
            # fresh), not whichever happens to be last in the list
            lead = max(running, key=lambda r: self._stamp(r.id) or float("inf"))
            subject, key, step = role_of(lead.id), mission.id, str(lead.step or "")
        clock = time.time() if now is None else now
        last = self._stamp(key)
        age = spinner.human_duration(spinner.silence(clock, last))
        if spinner.is_stalled(clock, last):
            label = " ".join(x for x in (f"idle {age}", subject, step) if x)
        else:
            label = " ".join(x for x in ("working", subject, step, age) if x)
        traj.spinner(f"{self._mark('running', last, frame, clock)} [{FAINT}]{esc(label)}[/]")

    def _orchestrator_id(self) -> str | None:
        """The orchestrator's pseudo run id (its events and charges carry it)."""
        mission = self._detail_mission()
        if mission is None or not (mission.plan or {}).get("orchestrator"):
            return None
        return f"{mission.id}:orchestrate:orchestrator:1"

    # -- trajectory ---------------------------------------------------------------------------

    def _wanted(self, ev: Any) -> bool:
        return self.run_id is None or ev.run_id == self.run_id

    def _append_traj(self, evs: list[Any]) -> None:
        traj = self.query_one("#m-traj", Trajectory)
        traj.add_events([e for e in evs if self._wanted(e)], show_role=self.run_id is None)

    def _rerender_traj(self) -> None:
        traj = self.query_one("#m-traj", Trajectory)
        traj.clear()
        traj.add_events(
            [e for e in self.events if self._wanted(e)][-800:], show_role=self.run_id is None
        )
        self.call_after_refresh(traj.scroll_end, animate=False)

    # -- detail tabs ----------------------------------------------------------------------------

    def _paint_detail_tabs(self) -> None:
        mission = self._detail_mission()
        if mission is None or getattr(mission, "light", False):
            return
        tab = self.mtab
        if tab == "graph":
            meta = {r.id: f"{role_of(r.id)} · {self._run_sub(r)}" for r in self.runs}
            inner = self._inner_graphs()
            # Use layered graph for orchestrated missions
            plan = mission.plan or {}
            if plan.get("orchestrator") or {}:
                graph = self.query_one("#m-graph", GraphView)
                layers = mission_layers(
                    mission,
                    self.runs,
                    self.mission_gates,
                    meta,
                    inner,
                    self.events,
                    expanded_rounds=graph.expanded_rounds,
                )
                graph.set_layers(layers, self.run_id)
            else:
                # Fixed-plan missions use the simple node view
                self.query_one("#m-graph", GraphView).set_nodes(
                    mission_nodes(mission, self.runs, self.mission_gates, meta, inner), self.run_id
                )
        elif tab == "output":
            if self.run_id is not None and self.run_id == self._orchestrator_id():
                body = self._orchestrator_output(mission)
            else:
                runs = [r for r in self.runs if self.run_id in (None, r.id)]
                parts = []
                for run in runs:
                    parts.append(
                        f"{glyph(run.status)} [b]{esc(role_of(run.id))}[/b]  "
                        f"[{FAINT}]{esc(run.step)} · {esc(self._run_sub(run))}[/]\n"
                        + artifact_markup(run.role, run.artifact)
                    )
                body = "\n\n".join(parts) or f"[{FAINT}]no sessions yet[/]"
            self.query_one("#m-output-body", Static).update(body)
        elif tab == "diff":
            coder = [
                r
                for r in self.runs
                if (r.artifact or {}).get("diff") and self.run_id in (None, r.id)
            ]
            if coder:
                art = coder[-1].artifact or {}
                body = (
                    f"[{MUTED}]branch[/] {esc(art.get('branch', ''))}  [{MUTED}]commit[/] "
                    f"{esc(str(art.get('commit', ''))[:10])}  "
                    f"[{FAINT}]from {esc(role_of(coder[-1].id))}[/]\n\n"
                    + diff_markup(str(art.get("diff") or ""))
                )
            else:
                body = f"[{FAINT}]No diff yet. The coder hands one off when it finishes.[/]"
            self.query_one("#m-diff-body", Static).update(body)
        elif tab == "report":
            if self.report_loaded_for != self._report_key():
                self._load_report()
        elif tab == "gates":
            self._paint_gates()
        follow = self.query_one("#m-follow", Static)
        live = status_value(mission.status) in actions.ACTIVE
        follow.update(f"[{ACCENT}]● live[/]" if live else "")

    def _orchestrator_output(self, mission: Any) -> str:
        """The orchestrator's own Output: its notes to the owner (INFO events
        carrying to_owner, written by message_owner/finish), then the mission's
        final summary, oldest first. It is a synthetic run with no artifact."""
        parts: list[str] = []
        notes = [
            str((ev.payload or {}).get("note") or "")
            for ev in self.events
            if ev.run_id == self.run_id
            and str(getattr(ev.type, "value", ev.type)) == "info"
            and (ev.payload or {}).get("to_owner")
        ]
        notes = [n for n in notes if n]
        if notes:
            parts.append(
                f"[b]orchestrator[/b]  [{FAINT}]notes to you · {len(notes)}[/]\n"
                + "\n".join(f"[{MUTED}]·[/] {esc(n)}" for n in notes)
            )
        result = mission.result if isinstance(mission.result, dict) else {}
        summary = result.get("summary")
        if summary:
            parts.append(f"[b]mission summary[/b]\n{esc(str(summary))}")
        return "\n\n".join(parts) or f"[{FAINT}]nothing from the orchestrator yet[/]"

    def _inner_graphs(self) -> dict[str, tuple[list[str], str]]:
        """Role graph topology + current node, when roles publish them."""
        out: dict[str, tuple[list[str], str]] = {}
        for ev in self.events:
            if not ev.run_id:
                continue
            p = ev.payload or {}
            graph = p.get("graph")
            if isinstance(graph, dict) and graph.get("nodes"):
                nodes = [
                    str(n.get("id") if isinstance(n, dict) else n)
                    for n in graph["nodes"]
                    if str(n.get("id") if isinstance(n, dict) else n)
                    not in ("__start__", "__end__")
                ]
                out[ev.run_id] = (nodes, out.get(ev.run_id, ([], ""))[1])
            if p.get("node") and p.get("phase") == "enter":
                nodes, _ = out.get(ev.run_id, ([], ""))
                out[ev.run_id] = (nodes, str(p["node"]))
        return out

    def _report_key(self) -> str:
        """What the report body is rendered from: the mission's newest event
        seq (already fetched by the tick) plus the tick's budget totals. Keying
        on the run count alone missed a run flipping RUNNING -> COMPLETED with
        an artifact, which rewrites the body without adding a run. Charges
        publish no event, so the budget totals stand in for them until the
        next one."""
        spent = self.budget.spent_usd if self.budget is not None else 0.0
        return f"{self.mission_id}:{self.last_seq}:{spent}"

    @work(exclusive=True, group="dash-report")
    async def _load_report(self) -> None:
        mission_id = self.mission_id
        if not mission_id:
            return
        key = self._report_key()
        md = await self.hub.guard(lambda: self.hub.data.mission_report(mission_id), "")
        if self.mission_id != mission_id:
            return
        self.report_loaded_for = key
        # the app can close while this worker awaits: teardown drops the body
        # first, so the paint is guarded and the data await above is not
        with self.painting():
            await self.query_one("#m-report-body", Markdown).update(md or "_no report yet_")

    def _paint_gates(self) -> None:
        body = self.query_one("#m-gates-body", Vertical)
        body.remove_children()
        if not self.mission_gates:
            body.mount(Static(f"[{FAINT}]This mission has not asked you anything.[/]"))
            return
        widgets: list[Any] = []
        for gate in sorted(self.mission_gates, key=lambda g: -float(g.created_at or 0)):
            is_open = status_value(gate.status) == "open"
            card = Vertical(classes="card " + ("-warn" if is_open else ""))
            widgets.append(card)
        body.mount_all(widgets)
        for card, gate in zip(
            widgets,
            sorted(self.mission_gates, key=lambda g: -float(g.created_at or 0)),
            strict=True,
        ):
            is_open = status_value(gate.status) == "open"
            when = ago(gate.created_at)
            card.mount(
                Static(
                    f"{status_text(gate.status)}  [{FAINT}]{esc(gate.kind.value)} · {esc(when)} · "
                    f"{esc(gate.id)}[/]\n[b]{esc(gate.question)}[/b]"
                    + (f"\n[{MUTED}]{esc(gate.decision_note)}[/]" if gate.decision_note else ""),
                    classes="card-body",
                )
            )
            if is_open:
                row = Horizontal(classes="card-actions")
                card.mount(row)
                row.mount_all(
                    [
                        Btn("Approve", "gate-approve", variant="primary", payload=gate.id),
                        Btn("Reject", "gate-reject", variant="danger", payload=gate.id),
                        Btn("Answer in Gates…", "gate-answer", variant="ghost", payload=gate.id),
                    ]
                )

    def _send_message(self) -> None:
        box = self.query_one("#m-msg", Input)
        text = box.value.strip()
        if not text or not self.mission_id:
            return
        self.run_worker(self._deliver_message(self.mission_id, text), group="owner-message")

    async def _deliver_message(self, mission_id: str, text: str) -> None:
        """Talk to the OPEN mission, whatever chat it came from: the shared
        owner-message publisher (ADR 0007), the same one `rework message` uses."""
        from rework.missions import send_owner_message

        try:
            await send_owner_message(mission_id, text, store=await self.hub.data.store())
        except Exception as exc:
            self.toast(f"Could not reach the mission: {clip(str(exc), 200)}", severity="error")
            return
        with self.painting():
            self.query_one("#m-msg", Input).value = ""
        self.toast(f"Sent to {mission_id}'s orchestrator")

    def _gate_button(self, btn: Btn) -> bool:
        if not btn.action.startswith("gate-"):
            return False
        gate_id = str(btn.payload)
        if btn.action == "gate-answer":
            self.rw.action_tab("gates")
            self.rw.view("gates").select_gate(gate_id)
        else:
            self.run_worker(
                actions.decide(self.rw, gate_id, btn.action == "gate-approve"), group="decide"
            )
        return True

    def on_graph_view_node_clicked(self, event: GraphView.NodeClicked) -> None:
        event.stop()
        if event.node.expandable:
            # the collapsed-round summary: expand it, or fold it again
            graph = self.query_one("#m-graph", GraphView)
            graph.toggle_collapsed_rounds()
            self._paint_detail_tabs()
            return
        if event.node.run_id:
            self.run_id = event.node.run_id
            self._paint_head()
            self._paint_runs()
            self._rerender_traj()
            self._paint_detail_tabs()
