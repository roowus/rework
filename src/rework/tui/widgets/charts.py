"""Terminal charts: calendar heatmap, column chart, share bars."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from textual.widgets import Static

from rework.tui.theme import ACCENT, FAINT, MUTED, RAISED, esc


def _blend(a: str, b: str, t: float) -> str:
    a_rgb = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    b_rgb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    mix = [round(x + (y - x) * t) for x, y in zip(a_rgb, b_rgb, strict=True)]
    return "#" + "".join(f"{c:02x}" for c in mix)


SHADES = [_blend(RAISED, ACCENT, t) for t in (0.0, 0.25, 0.5, 0.75, 1.0)]


def shade(value: float, peak: float) -> str:
    if value <= 0 or peak <= 0:
        return SHADES[0]
    ratio = value / peak
    return SHADES[min(4, 1 + int(ratio * 3.999))]


class Heatmap(Static):
    """GitHub-style calendar: columns are weeks, rows are weekdays. Fits as
    many weeks as the widget is wide (max one year)."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id, classes="heatmap")
        self.values: dict[dt.date, float] = {}
        self.fmt: Callable[[float], str] = lambda v: f"{v:.2f}"
        self.label = ""

    def set_data(
        self, values: dict[dt.date, float], fmt: Callable[[float], str], label: str
    ) -> None:
        self.values, self.fmt, self.label = values, fmt, label
        self.paint()

    def on_resize(self) -> None:
        self.paint()

    def paint(self) -> None:
        width = max(20, (self.size.width or 120) - 6)
        weeks = max(4, min(53, width // 2))
        today = dt.date.today()
        start = today - dt.timedelta(days=today.weekday() + 7 * (weeks - 1))
        peak = max(self.values.values(), default=0.0)
        rows: list[str] = []
        # month labels
        months = [" "] * (weeks * 2)
        last_month = None
        for w in range(weeks):
            day = start + dt.timedelta(days=7 * w)
            if day.month != last_month:
                name = day.strftime("%b")
                for i, ch in enumerate(name):
                    if w * 2 + i < len(months):
                        months[w * 2 + i] = ch
                last_month = day.month
        rows.append(f"[{FAINT}]    {''.join(months)}[/]")
        for wd, name in enumerate(["Mon", "", "Wed", "", "Fri", "", "Sun"]):
            cells = []
            for w in range(weeks):
                day = start + dt.timedelta(days=7 * w + wd)
                if day > today:
                    cells.append("  ")
                    continue
                cells.append(f"[{shade(self.values.get(day, 0.0), peak)}]■[/] ")
            rows.append(f"[{FAINT}]{name:<4}[/]" + "".join(cells))
        total = sum(v for d, v in self.values.items() if d >= start)
        active = sum(1 for d, v in self.values.items() if d >= start and v > 0)
        legend = " ".join(f"[{c}]■[/]" for c in SHADES)
        rows.append(
            f"[{FAINT}]    less {legend} more   ·   {esc(self.fmt(total))} {esc(self.label)} over "
            f"{weeks} weeks · {active} active days · peak {esc(self.fmt(peak))}/day[/]"
        )
        self.update("\n".join(rows))


BLOCKS = " ▁▂▃▄▅▆▇█"


class Columns(Static):
    """A column chart (one column per bucket), `height` rows tall."""

    def __init__(self, *, id: str | None = None, height: int = 8) -> None:
        super().__init__("", id=id, classes="columns")
        self.series: list[tuple[str, float]] = []
        self.fmt: Callable[[float], str] = lambda v: f"{v:.2f}"
        self.rows = height

    def set_data(self, series: list[tuple[str, float]], fmt: Callable[[float], str]) -> None:
        self.series, self.fmt = series, fmt
        self.paint()

    def on_resize(self) -> None:
        self.paint()

    def paint(self) -> None:
        if not self.series:
            self.update(f"[{FAINT}]no spend in this range[/]")
            return
        width = max(10, (self.size.width or 100) - 10)
        series = self.series
        # merge buckets so they fit (2 cells per column when room allows)
        per = max(1, -(-len(series) // max(1, width // 2)))
        merged: list[tuple[str, float]] = []
        for i in range(0, len(series), per):
            chunk = series[i : i + per]
            merged.append((chunk[0][0], sum(v for _, v in chunk)))
        col_w = 2 if len(merged) * 2 <= width else 1
        peak = max((v for _, v in merged), default=0.0) or 1.0
        lines = []
        for row in range(self.rows, 0, -1):
            cells = []
            for _, v in merged:
                level = v / peak * self.rows
                if level >= row:
                    ch = "█"
                elif level > row - 1:
                    ch = BLOCKS[max(1, int((level - (row - 1)) * 8))]
                else:
                    ch = " "
                cells.append(
                    f"[{ACCENT}]{ch * (col_w - 1 if col_w > 1 else 1)}[/]"
                    + (" " if col_w > 1 else "")
                )
            axis = f"{self.fmt(peak):>8} " if row == self.rows else " " * 9
            lines.append(f"[{FAINT}]{axis}[/]{''.join(cells)}")
        first, last = merged[0][0], merged[-1][0]
        span = len(merged) * col_w
        pad = max(1, span - len(first) - len(last))
        lines.append(f"[{FAINT}]{' ' * 9}{esc(first)}{' ' * pad}{esc(last)}[/]")
        self.update("\n".join(lines))


def share_bar(value: float, peak: float, width: int = 18) -> str:
    if peak <= 0:
        return f"[{FAINT}]{'·' * width}[/]"
    filled = value / peak * width
    full = int(filled)
    part = BLOCKS[int((filled - full) * 8)] if full < width else ""
    bar = "█" * full + (part if part.strip() else "")
    return f"[{ACCENT}]{bar}[/][{RAISED}]{'█' * max(0, width - len(bar))}[/]"


def table(rows: list[tuple[str, float, str]], fmt: Callable[[float], str], width: int = 14) -> str:
    """[(label, value, extra)] -> aligned lines with share bars."""
    if not rows:
        return f"[{FAINT}]nothing yet[/]"
    peak = max(v for _, v, _ in rows) or 1.0
    total = sum(v for _, v, _ in rows) or 1.0
    label_w = min(26, max(len(r[0]) for r in rows))
    out = []
    for label, value, extra in rows:
        name = label if len(label) <= label_w else label[: label_w - 1] + "…"
        out.append(
            f"{esc(name):<{label_w}}  {share_bar(value, peak, width)}  "
            f"[b]{esc(fmt(value)):>9}[/b]  [{FAINT}]{value / total:>4.0%}[/]  "
            f"[{MUTED}]{esc(extra)}[/]"
        )
    return "\n".join(out)
