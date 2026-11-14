"""Cost: where the money (and tokens) go. Exact accounting from the charges
ledger (handoff §3.5 / spend_history).

Range + metric chips on top, headline numbers, a one-year calendar heatmap,
a column chart over the range, then breakdowns by model, project, role and
mission."""

from __future__ import annotations

import datetime as dt
import time
from functools import partial
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from rework.tui.hub import Snapshot, midnight
from rework.tui.theme import FAINT, MUTED, clip, duration, money, tokens
from rework.tui.views.base import View
from rework.tui.widgets.base import ChipGroup, Label, Stat
from rework.tui.widgets.charts import Columns, Heatmap, table

RANGES = [("7d", "7"), ("30d", "30"), ("90d", "90"), ("1y", "365"), ("All", "0")]
METRICS = [("Cost", "cost"), ("Tokens", "tokens"), ("Calls", "calls")]


def metric_of(row: dict[str, Any], metric: str) -> float:
    if metric == "tokens":
        return float(row.get("input_tokens") or 0) + float(row.get("output_tokens") or 0)
    if metric == "calls":
        return float(row.get("calls") or 0)
    return float(row.get("cost_usd") or 0)


def fmt_for(metric: str) -> Any:
    if metric == "tokens":
        return tokens
    if metric == "calls":
        return lambda v: f"{v:,.0f}"
    return money


def short_model(name: str) -> str:
    return name.rsplit("/", 1)[-1] if name else "unattributed"


def active_seconds_of(runs: list[Any]) -> float:
    """Summed active time from the `run_stats` blob on each run's artifact.

    `active_seconds` (first to last model call) wins whenever the key is
    present: 0.0 is a real value for an ordinary single-call run, so a falsy
    fallback would swap in wall time and inflate the headline. `elapsed_seconds`
    only stands in when the key is missing, and a run without usable stats
    contributes nothing."""
    total = 0.0
    for run in runs:
        artifact = getattr(run, "artifact", None)
        stats = artifact.get("run_stats") if isinstance(artifact, dict) else None
        if not isinstance(stats, dict):
            continue
        value = stats.get("active_seconds")
        if value is None:
            value = stats.get("elapsed_seconds")
        if value is None:
            continue
        try:
            total += max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return total


class CostView(View):
    TAB = "cost"
    LABEL = "Cost"

    def __init__(self) -> None:
        super().__init__()
        self.days = 30
        self.metric = "cost"
        self.snap = Snapshot()
        self._loaded_at = 0.0
        self.year: list[dict[str, Any]] = []
        self.by: dict[str, list[dict[str, Any]]] = {}
        self.missions: list[tuple[str, float, str]] = []
        self.active_seconds = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-head cost-head"):
            yield Static("[b]Cost[/b]", classes="pane-head-title")
            yield Static("", classes="spacer")
            yield ChipGroup(METRICS, "cost", id="cost-metric")
            yield Static("  ", classes="chips-label")
            yield ChipGroup(RANGES, "30", id="cost-range")
        with VerticalScroll(id="cost-body"):
            with Horizontal(id="cost-stats"):
                yield Stat("spent", id="c-total")
                yield Stat("tokens in", id="c-in")
                yield Stat("tokens out", id="c-out")
                yield Stat("model calls", id="c-calls")
                yield Stat("per active day", id="c-avg")
                yield Stat("today", id="c-today")
                yield Stat("active time", id="c-active")
            yield Label("Activity")
            yield Heatmap(id="c-heat")
            yield Label("Over the range", id="c-range-label")
            yield Columns(id="c-cols", height=7)
            with Horizontal(classes="cost-grid"):
                with Vertical(classes="cost-cell"):
                    yield Label("By model")
                    yield Static("", id="c-model")
                with Vertical(classes="cost-cell"):
                    yield Label("By project")
                    yield Static("", id="c-project")
            with Horizontal(classes="cost-grid"):
                with Vertical(classes="cost-cell"):
                    yield Label("By role")
                    yield Static("", id="c-role")
                with Vertical(classes="cost-cell"):
                    yield Label("Top missions")
                    yield Static("", id="c-mission")
            yield Static(
                f"[{FAINT}]Exact accounting from the charges ledger. Days are UTC. "
                "Chat turns before a mission exists count as unattributed.[/]",
                id="c-foot",
            )

    def hints(self) -> list[tuple[str, str]]:
        return [("click", "range / metric"), ("ctrl+r", "refresh")]

    def shown(self) -> None:
        if time.time() - self._loaded_at > 20:
            self._load()

    def on_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        if self.rw.active_tab == "cost" and time.time() - self._loaded_at > 60:
            self._load()

    def on_chip_group_changed(self, event: ChipGroup.Changed) -> None:
        event.stop()
        if event.group.id == "cost-range":
            self.days = int(event.value)
            self._load()
        else:
            self.metric = event.value
            self._paint()

    # -- data ---------------------------------------------------------------

    def _since(self) -> float | None:
        return None if self.days == 0 else midnight() - (self.days - 1) * 86400

    @work(exclusive=True, group="cost-load")
    async def _load(self) -> None:
        self._loaded_at = time.time()
        data = self.hub.data
        since = self._since()
        year_since = midnight() - 371 * 86400
        self.year = await self.hub.guard(
            lambda: data.spend_history(bucket="day", since=year_since), []
        )
        range_since = since if since is not None else None
        self.by = {}
        for group in ("model", "project", "role"):
            self.by[group] = await self.hub.guard(
                partial(data.spend_history, bucket="total", since=range_since, group_by=group),
                [],
            )
        self.by["day"] = await self.hub.guard(
            lambda: data.spend_history(bucket="day", since=range_since), []
        )
        # top missions: per-mission spend for missions active in the range
        cutoff = range_since or 0.0
        candidates = [m for m in self.snap.missions if m.updated_at >= cutoff][:30]
        rows = []
        active = 0.0
        for mission in candidates:
            budget = await self.hub.guard(partial(data.mission_budget, mission.id), None)
            runs = await self.hub.guard(partial(data.mission_runs, mission.id), [])
            active += active_seconds_of(runs)
            if budget is None:
                continue
            rows.append(
                (
                    mission,
                    {
                        "cost_usd": budget.spent_usd,
                        "input_tokens": budget.input_tokens,
                        "output_tokens": budget.output_tokens,
                        "calls": 0,
                    },
                )
            )
        self._mission_rows = rows
        self.active_seconds = active
        with self.painting():  # the app can close during the awaits above
            self._paint()

    # -- paint ----------------------------------------------------------------

    def _paint(self) -> None:
        metric = self.metric
        fmt = fmt_for(metric)
        days = self.by.get("day", [])
        total_cost = sum(float(r.get("cost_usd") or 0) for r in days)
        tin = sum(int(r.get("input_tokens") or 0) for r in days)
        tout = sum(int(r.get("output_tokens") or 0) for r in days)
        calls = sum(int(r.get("calls") or 0) for r in days)
        active_days = sum(1 for r in days if metric_of(r, metric) > 0) or 1
        span = "all time" if self.days == 0 else f"last {self.days} days"
        self.query_one("#c-total", Stat).set(money(total_cost), span)
        self.query_one("#c-in", Stat).set(tokens(tin))
        self.query_one("#c-out", Stat).set(tokens(tout))
        self.query_one("#c-calls", Stat).set(f"{calls:,}")
        avg = sum(metric_of(r, metric) for r in days) / active_days
        self.query_one("#c-avg", Stat).set(fmt(avg), metric)
        self.query_one("#c-today", Stat).set(money(self.snap.spend_today))
        self.query_one("#c-active", Stat).set(duration(self.active_seconds), span)

        values: dict[dt.date, float] = {}
        for r in self.year:
            try:
                day = dt.date.fromisoformat(str(r.get("bucket_label"))[:10])
            except ValueError:
                day = dt.date.fromtimestamp(float(r.get("bucket_start") or 0))
            values[day] = values.get(day, 0.0) + metric_of(r, metric)
        label = {"cost": "spent", "tokens": "tokens", "calls": "calls"}[metric]
        self.query_one("#c-heat", Heatmap).set_data(values, fmt, label)

        # columns over the range, every day present (zeros included)
        by_label = {str(r.get("bucket_label"))[:10]: metric_of(r, metric) for r in days}
        if days:
            first = (
                dt.date.fromisoformat(min(by_label))
                if self.days == 0
                else (dt.date.today() - dt.timedelta(days=self.days - 1))
            )
            series = []
            d = first
            while d <= dt.date.today():
                series.append((d.strftime("%b %d"), by_label.get(d.isoformat(), 0.0)))
                d += dt.timedelta(days=1)
        else:
            series = []
        self.query_one("#c-cols", Columns).set_data(series, fmt)
        self.query_one("#c-range-label", Label).update(f"{metric.upper()} PER DAY · {span.upper()}")

        def rows_for(group: str, name: Any) -> list[tuple[str, float, str]]:
            out = []
            for r in self.by.get(group, []):
                extra = f"{int(r.get('calls') or 0)} calls"
                if metric != "cost":
                    extra = money(float(r.get("cost_usd") or 0))
                out.append((name(str(r.get("group_key") or "")), metric_of(r, metric), extra))
            return sorted(out, key=lambda x: -x[1])[:10]

        self.query_one("#c-model", Static).update(table(rows_for("model", short_model), fmt))
        self.query_one("#c-project", Static).update(
            table(
                rows_for(
                    "project",
                    lambda k: self.snap.project_name(k) if k else "no project",
                ),
                fmt,
            )
        )
        self.query_one("#c-role", Static).update(
            table(rows_for("role", lambda k: k or "unattributed"), fmt)
        )
        mrows = []
        for mission, r in getattr(self, "_mission_rows", []):
            if metric == "calls":
                continue
            value = metric_of(r, metric)
            if value <= 0:
                continue
            mrows.append((clip(mission.objective, 30), value, ""))
        mrows.sort(key=lambda x: -x[1])
        self.query_one("#c-mission", Static).update(
            table(mrows[:8], fmt)
            if mrows or metric != "calls"
            else f"[{MUTED}]calls per mission are not tracked yet[/]"
        )
