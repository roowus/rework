"""Dashboard: every project and mission at a glance, then drill down.

Overview: stats strip · projects (left) · missions grouped by what matters
(needs you → running → paused → recent). Click a mission to open it.

Mission: header + flow strip · runs (left: one row per role session) · tabs
for the live Trajectory, the LangGraph-style Graph, each role's Output, the
Diff, the full Report and the mission's Gates. Click a run to focus everything
on that session; esc steps back out."""

from __future__ import annotations

import time
from functools import partial
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, Markdown, Static

from rework.tui import actions
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
from rework.tui.widgets.graph import GraphView, mission_nodes
from rework.tui.widgets.trajectory import Trajectory, role_of

ALL = "*"
NONE = "-"

SECTIONS = [
    ("needs you", {"awaiting_gate"}),
    ("running", {"running", "pending"}),
    ("paused", {"on_hold"}),
    ("recent", {"done", "failed", "cancelled"}),
]

MTABS = [
    ("Trajectory", "traj"),
    ("Graph", "graph"),
    ("Output", "output"),
    ("Diff", "diff"),
    ("Report", "report"),
    ("Gates", "gates"),
]


class DashboardView(View):
    TAB = "dashboard"
    LABEL = "Dashboard"

    def __init__(self) -> None:
        super().__init__()
        self.snap = Snapshot()
        self.project: str = ALL
        self.filter = "all"
        self.mission_id: str | None = None
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
                yield Btn("Fetch result", "fetch", variant="ghost")
                yield Btn("Copy id", "copy-id", variant="ghost")
        with ContentSwitcher(initial="dash-overview", id="dash-pages"):
            with Vertical(id="dash-overview"):
                with Horizontal(id="dash-stats"):
                    yield Stat("running", id="st-running")
                    yield Stat("need you", id="st-gates")
                    yield Stat("paused", id="st-paused")
                    yield Stat("done · 7d", id="st-done")
                    yield Stat("spend today", id="st-today")
                    yield Stat("spend · 7d", id="st-week")
                with Horizontal():
                    with Vertical(classes="sidebar", id="dash-side"):
                        yield Label("Projects")
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

    def on_mount(self) -> None:
        self._paint_head()

    def hints(self) -> list[tuple[str, str]]:
        if self.mission_id:
            return [("click", "focus a session"), ("esc", "back"), ("ctrl+c", "copy")]
        return [("click", "open mission"), ("↑↓", "move"), ("enter", "open")]

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
        self._paint_overview()
        self._paint_head()
        if time.time() - self._enriched_at > 15:
            self._enriched_at = time.time()
            self._enrich()
        if self.mission_id:
            self._paint_mission_static()
            self._poll_mission()

    @work(exclusive=True, group="dash-enrich")
    async def _enrich(self) -> None:
        """Cost + active role for the missions people look at (throttled)."""
        data = self.hub.data
        week = await self.hub.guard(
            lambda: data.spend_history(bucket="total", since=time.time() - 7 * 86400), []
        )
        self.spend_week = sum(float(r.get("cost_usd") or 0) for r in week)
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
        self._paint_overview()

    # -- overview -------------------------------------------------------------------

    def _filtered(self) -> list[Any]:
        out = []
        for m in self.snap.missions:
            if self.project == NONE and m.project_id:
                continue
            if self.project not in (ALL, NONE) and m.project_id != self.project:
                continue
            st = status_value(m.status)
            if self.filter == "active" and st not in actions.ACTIVE:
                continue
            if self.filter == "done" and st != "done":
                continue
            if self.filter == "failed" and st not in ("failed", "cancelled"):
                continue
            out.append(m)
        return out

    def _paint_overview(self) -> None:
        snap = self.snap
        counts: dict[str, int] = {}
        for m in snap.missions:
            counts[status_value(m.status)] = counts.get(status_value(m.status), 0) + 1
        week_ago = time.time() - 7 * 86400
        done_week = sum(
            1 for m in snap.missions if status_value(m.status) == "done" and m.updated_at > week_ago
        )
        if not snap.loaded:
            # owner screenshot: zeros everywhere looked like real data while the
            # first refresh was still pending (or the database was unreachable)
            note = "offline" if not snap.online else "connecting…"
            for sid in ("st-running", "st-gates", "st-paused", "st-done", "st-today", "st-week"):
                self.query_one(f"#{sid}", Stat).set("—", note)
            return
        self.query_one("#st-running", Stat).set(str(counts.get("running", 0)))
        self.query_one("#st-gates", Stat).set(str(len(snap.gates)))
        self.query_one("#st-paused", Stat).set(str(counts.get("on_hold", 0)))
        self.query_one("#st-done", Stat).set(str(done_week))
        self.query_one("#st-today", Stat).set(money(snap.spend_today))
        self.query_one("#st-week", Stat).set(
            money(self.spend_week) if self._enriched_at else "—", "" if self._enriched_at else "…"
        )
        self.query_one("#st-gates").set_class(bool(snap.gates), "-warn")

        # projects
        rows = [RowData(ALL, self._proj_row("All projects", snap.missions))]
        for p in snap.projects:
            ms = [m for m in snap.missions if m.project_id == p.id]
            rows.append(RowData(p.id, self._proj_row(p.name, ms)))
        loose = [m for m in snap.missions if not m.project_id]
        if loose:
            rows.append(RowData(NONE, self._proj_row("No project", loose, dim=True)))
        plist = self.query_one("#proj-list", RowList)
        plist.set_rows(rows)
        plist.select(self.project)

        # missions, grouped
        missions = self._filtered()
        mrows: list[RowData] = []
        for title, statuses in SECTIONS:
            group = [m for m in missions if status_value(m.status) in statuses]
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
                mrows.append(RowData(m.id, self._mission_row(m)))
        mlist = self.query_one("#mission-list", RowList)
        mlist.set_rows(mrows)

    def _proj_row(self, name: str, missions: list[Any], dim: bool = False) -> str:
        running = sum(1 for m in missions if status_value(m.status) == "running")
        waiting = sum(1 for m in missions if status_value(m.status) == "awaiting_gate")
        badges = []
        if running:
            badges.append(f"[{ACCENT}]● {running}[/]")
        if waiting:
            badges.append(f"[{WARN}]◆ {waiting}[/]")
        label = f"[{MUTED}]{esc(name)}[/]" if dim else esc(name)
        return f"{label}  [{FAINT}]{len(missions)}[/]  " + " ".join(badges)

    def _mission_row(self, m: Any) -> str:
        st = status_value(m.status)
        bits = []
        if self.project == ALL and m.project_id:
            bits.append(self.snap.project_name(m.project_id))
        bits.append(plan_label(m.plan))
        role = self.mission_role.get(m.id)
        if role and st in actions.ACTIVE:
            bits.append(role)
        if m.id in self.mission_cost:
            bits.append(money(self.mission_cost[m.id]))
        bits.append(ago(m.created_at))
        bits.append(m.id)
        gate_note = ""
        if st == "awaiting_gate":
            gates = self.snap.gates_for(m.id)
            if gates:
                gate_note = f"\n  [{WARN}]◆ {esc(clip(gates[0].question, 90))}[/]"
        return (
            f"{glyph(st)} [b]{esc(clip(m.objective, 100))}[/b]\n"
            f"  [{FAINT}]{esc('  ·  '.join(bits))}[/]{gate_note}"
        )

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
        if not self.mission_id:
            return
        event.stop()
        if self._gate_button(event.btn):
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
        self.run_worker(actions.control(self.rw, self.mission_id, verb), group="control")

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
            self.report_loaded_for = None
            self.query_one("#m-traj", Trajectory).clear()
            self.query_one("#run-list", RowList).set_rows([])
        self.mission_id = mission_id
        self.run_id = run_id
        self.query_one("#dash-pages", ContentSwitcher).current = "dash-mission"
        self._paint_head()
        self._paint_mission_static()
        self._poll_mission()
        self.rw.refresh_hints()
        self.query_one("#run-list", RowList).focus()

    def close_mission(self) -> None:
        self.mission_id = None
        self.run_id = None
        self.query_one("#dash-pages", ContentSwitcher).current = "dash-overview"
        self._paint_overview()
        self._paint_head()
        self.rw.refresh_hints()
        self.query_one("#mission-list", RowList).focus()

    def back(self) -> bool:
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
        self._paint_mission_static()
        self._paint_runs()
        self._paint_detail_tabs()

    def _paint_mission_static(self) -> None:
        mission = self.snap.mission(self.mission_id)
        header = self.query_one("#m-header", Static)
        if mission is None:
            header.update(f"[{FAINT}]loading {esc(self.mission_id)}…[/]")
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
        header.update(
            f"[b]{esc(mission.objective)}[/b]\n{status_text(st)}  [{MUTED}]"
            + esc("  ·  ".join(bits))
            + "[/]"
        )
        nodes = mission_nodes(mission, self.runs, self.mission_gates, {}, {})
        strip = f" [{FAINT}]──[/] ".join(f"{esc(n.title)} {glyph(n.status)}" for n in nodes)
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

    def _paint_runs(self) -> None:
        rows = [RowData(ALL, f"[b]All activity[/b]\n[{FAINT}]{len(self.events)} events[/]")]
        orch_id = self._orchestrator_id()
        if orch_id:
            mission = self.snap.mission(self.mission_id)
            st = status_value(mission.status) if mission else "running"
            model = ((mission.plan or {}).get("orchestrator") or {}).get("model") if mission else ""
            cost = self.run_cost.get(orch_id, 0.0)
            sub = " · ".join(x for x in (str(model or ""), money(cost) if cost else "") if x)
            rows.append(
                RowData(
                    orch_id,
                    f"{glyph('running' if st in actions.ACTIVE else st)} [b]orchestrator[/b]\n"
                    f"  [{FAINT}]{esc(sub)}[/]",
                )
            )
        for run in self.runs:
            rows.append(
                RowData(
                    run.id,
                    f"{glyph(run.status)} [b]{esc(role_of(run.id))}[/b]  "
                    f"[{FAINT}]{esc(run.step)}[/]\n"
                    f"  [{FAINT}]{esc(self._run_sub(run))}[/]",
                )
            )
        lst = self.query_one("#run-list", RowList)
        lst.set_rows(rows)
        lst.select(self.run_id or ALL)

    def _orchestrator_id(self) -> str | None:
        """The orchestrator's pseudo run id (its events and charges carry it)."""
        mission = self.snap.mission(self.mission_id)
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
        mission = self.snap.mission(self.mission_id)
        if mission is None:
            return
        tab = self.mtab
        if tab == "graph":
            meta = {r.id: f"{role_of(r.id)} · {self._run_sub(r)}" for r in self.runs}
            inner = self._inner_graphs()
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
            if self.report_loaded_for != f"{self.mission_id}:{len(self.runs)}":
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

    @work(exclusive=True, group="dash-report")
    async def _load_report(self) -> None:
        mission_id = self.mission_id
        if not mission_id:
            return
        key = f"{mission_id}:{len(self.runs)}"
        md = await self.hub.guard(lambda: self.hub.data.mission_report(mission_id), "")
        if self.mission_id != mission_id:
            return
        self.report_loaded_for = key
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
        if event.node.run_id:
            self.run_id = event.node.run_id
            self._paint_head()
            self._paint_runs()
            self._rerender_traj()
            self._paint_detail_tabs()
