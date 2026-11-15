"""The Dashboard's mission sections must agree with the Gates tab.

Found live (2026-11-14, mission m-fef8f6ff21f2): the Dashboard listed a mission
under "needs you" (its status label still said `awaiting_gate`) while the Gates
tab was empty, because its only gate had already been approved. A status label
is something a writer must remember to flip back; an OPEN gate is a fact. So
grouping, the stats strip, the per-project badges and the row's ◆ note all
derive from `Snapshot.gates_for` through `section_for`, and the four cases
below pin that rule.

The Dashboard is mounted with a bare app and a hand-built Snapshot (no store):
the rule is pure data, so the tests do not need a database to be load-bearing.
"""

from __future__ import annotations

import re
from typing import Any

from textual.app import App, ComposeResult

from rework.kernel.state.records import Gate, GateKind, GateStatus, Mission, MissionStatus
from rework.tui.hub import Snapshot
from rework.tui.spinner import FRAMES
from rework.tui.theme import ACCENT, WARN
from rework.tui.views.dashboard import DashboardView, row_glyph, section_for
from rework.tui.widgets.base import HEADER, RowList, Stat


class DashApp(App[None]):
    def __init__(self, view: DashboardView) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        yield self._view


def _mission(mid: str, status: MissionStatus, project_id: str | None = None) -> Mission:
    return Mission(id=mid, objective=f"mission {mid}", status=status, project_id=project_id)


def _gate(mid: str, gid: str, status: GateStatus = GateStatus.OPEN) -> Gate:
    return Gate(
        id=gid,
        mission_id=mid,
        kind=GateKind.SHIP,
        question=f"ship {mid}?",
        status=status,
    )


def _snap(
    missions: list[Mission], gates: list[Gate], projects: list[Any] | None = None
) -> Snapshot:
    # exactly what Hub.refresh puts here: OPEN gates only
    return Snapshot(
        missions=missions,
        gates=gates,
        projects=projects or [],
        loaded=True,
    )


# -- the rule -------------------------------------------------------------------


async def test_a_mission_with_an_open_gate_needs_you() -> None:
    """(a) awaiting_gate + an open gate -> "needs you"."""
    snap = _snap([_mission("m-a", MissionStatus.AWAITING_GATE)], [_gate("m-a", "g-a")])
    assert section_for(snap.missions[0], snap) == "needs you"


async def test_awaiting_gate_with_only_an_approved_gate_is_running() -> None:
    """(b) the live bug: the label says awaiting_gate, the decision is already
    made, so the owner owes nothing and the Dashboard must not say "needs you"."""
    # a decided gate never reaches the snapshot: Hub.refresh fills `gates` from
    # StateStore.list_open_gates, which selects `status = OPEN` only. So the
    # approved gate is not in this list and the mission is not waiting.
    snap = _snap([_mission("m-b", MissionStatus.AWAITING_GATE)], [])
    assert section_for(snap.missions[0], snap) == "running"


async def test_pending_is_queued_not_running() -> None:
    """(c) a pending mission waits for a runner slot, not for the owner, and it
    is not "running" either."""
    snap = _snap([_mission("m-c", MissionStatus.PENDING)], [])
    assert section_for(snap.missions[0], snap) == "queued"


async def test_on_hold_is_paused() -> None:
    """(d) on_hold -> "paused"."""
    snap = _snap([_mission("m-d", MissionStatus.ON_HOLD)], [])
    assert section_for(snap.missions[0], snap) == "paused"


async def test_running_done_failed_cancelled() -> None:
    snap = _snap(
        [
            _mission("m-r", MissionStatus.RUNNING),
            _mission("m-done", MissionStatus.DONE),
            _mission("m-fail", MissionStatus.FAILED),
            _mission("m-cancel", MissionStatus.CANCELLED),
        ],
        [],
    )
    assert [section_for(m, snap) for m in snap.missions] == [
        "running",
        "recent",
        "recent",
        "recent",
    ]


async def test_an_open_gate_wins_over_the_status() -> None:
    """ "needs you" is a fact about gates for ANY status, so a running mission
    with an open question really does need the owner."""
    snap = _snap([_mission("m-e", MissionStatus.RUNNING)], [_gate("m-e", "g-e")])
    assert section_for(snap.missions[0], snap) == "needs you"


# -- the painted overview ------------------------------------------------------

#: one mission per section, the stale-label case included
IDS = {
    "needs": "m-need000000a",  # awaiting_gate + open gate
    "stale": "m-stale00000b",  # awaiting_gate, gate already approved
    "queued": "m-queue00000c",  # pending
    "running": "m-run0000000d",  # running, no gate
    "paused": "m-hold000000e",  # on_hold
    "recent": "m-done000000f",  # done
}


def _overview() -> tuple[DashboardView, Snapshot]:
    from rework.kernel.state.records import Project

    project = Project(id="p-one", name="one")
    missions = [
        _mission(IDS["needs"], MissionStatus.AWAITING_GATE, project.id),
        _mission(IDS["stale"], MissionStatus.AWAITING_GATE, project.id),
        _mission(IDS["queued"], MissionStatus.PENDING, project.id),
        _mission(IDS["running"], MissionStatus.RUNNING, project.id),
        _mission(IDS["paused"], MissionStatus.ON_HOLD, project.id),
        _mission(IDS["recent"], MissionStatus.DONE, project.id),
    ]
    return DashboardView(), _snap(missions, [_gate(IDS["needs"], "g-need")], [project])


def _plain(text: str) -> str:
    """Rich markup stripped: the running tile and chip now lead with the spinner
    frame (`[#a394f7]⠋[/] 2`), so a test reads the text, not the markup."""
    return re.sub(r"\[[^\]]*\]", "", text)


def _running_count_shows(text: str, n: int) -> bool:
    """A frame (or the idle glyph) followed by the count: `⠋ 2`."""
    plain = _plain(text)
    return any(f"{mark} {n}" in plain for mark in (*FRAMES, "◌"))


def _headers(rlst: RowList) -> dict[str, int]:
    """section title -> row count under it, read off the painted rows."""
    out: dict[str, int] = {}
    current = ""
    for key in rlst.keys:
        if key.startswith(HEADER):
            current = key.removeprefix(HEADER)
            out[current] = 0
        elif current:
            out[current] += 1
    return out


async def test_the_painted_sections_and_the_counters_agree() -> None:
    view, snap = _overview()
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.snap = snap
        view._paint_overview()
        await pilot.pause()

        rlst = view.query_one("#mission-list", RowList)
        assert _headers(rlst) == {
            "needs you": 1,
            "queued": 1,
            "running": 2,  # running + the stale awaiting_gate label
            "paused": 1,
            "recent": 1,
        }, "one section per bucket, empty ones skipped"
        assert IDS["stale"] in rlst.selectable
        # order is the SECTIONS order, needs-you first
        assert [k for k in rlst.keys if k.startswith(HEADER)] == [
            f"{HEADER}needs you",
            f"{HEADER}queued",
            f"{HEADER}running",
            f"{HEADER}paused",
            f"{HEADER}recent",
        ]

        def stat(sid: str) -> str:
            return str(view.query_one(f"#{sid}", Stat)._value)

        assert stat("st-gates") == "1", "the needs-you count is open gates, not labels"
        assert stat("st-queued") == "1"
        assert _running_count_shows(stat("st-running"), 2), (
            "running means running-only (pending excluded): " + stat("st-running")
        )
        assert stat("st-paused") == "1"

        # the per-project chip must tell the same story as the sections
        prow = next(r for r in view.query("#proj-list Row") if r.key == "p-one")
        text = str(prow.render())
        assert "◆ 1" in text, f"one project needs you: {text!r}"
        assert _running_count_shows(text, 2), f"two of its missions are running: {text!r}"
        assert "○ 1" in text, f"one of its missions is queued: {text!r}"

        # ... and the row note: the ◆ line appears on the mission with the open
        # gate and on no other, whatever the labels say
        rows = {r.key: str(r.render()) for r in view.query("#mission-list Row")}
        assert "ship m-need000000a?" in rows[IDS["needs"]]
        assert not any("?" in rows[m] for m in (IDS["stale"], IDS["running"]))


async def test_project_badge_ignores_the_stale_awaiting_gate_label() -> None:
    """The badge was the second place that trusted the label: with only a stale
    awaiting_gate mission, a project must NOT show the amber ◆ chip."""
    from rework.kernel.state.records import Project

    project = Project(id="p-two", name="two")
    stale = _mission("m-stale00001a", MissionStatus.AWAITING_GATE, project.id)
    view = DashboardView()
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.snap = _snap([stale], [], [project])
        view._paint_overview()
        await pilot.pause()
        prow = next(r for r in view.query("#proj-list Row") if r.key == "p-two")
        text = str(prow.render())
        assert "◆" not in text, f"no open gate, so no needs-you chip: {text!r}"
        assert _running_count_shows(text, 1), f"it is running, not waiting for the owner: {text!r}"
        assert _headers(view.query_one("#mission-list", RowList)) == {"running": 1}


async def test_a_stale_awaiting_gate_row_under_running_paints_no_amber_diamond() -> None:
    """Audit finding: section_for files a stale `awaiting_gate` (its gate is
    already decided) under "running", but the row glyph came from the raw stored
    status, so the row sat under a faint "running" header painting the amber
    ◆. `$warn` means a human is needed and nothing else is amber (DESIGN §1),
    so the leading icon now comes from the section. The raw status stays visible
    in the drill-down header."""
    view, snap = _overview()
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.snap = snap
        view._paint_overview()
        await pilot.pause()

        # the ROW's markup carries the colour (`render()` strips it), so read
        # the markup the Row was built from, one entry per mission row
        rows = {r.key: r for r in view.query("#mission-list Row")}
        first_line = rows[IDS["stale"]]._markup.split("\n")[0]
        assert f"[{ACCENT}]●[/]" in first_line, f"it looks running: {first_line!r}"
        assert f"[{WARN}]◆[/]" not in first_line, (
            f"a mission filed under running must not paint the needs-you diamond: {first_line!r}"
        )
        # ... while a mission that really needs you still does
        assert f"[{WARN}]◆[/]" in rows[IDS["needs"]]._markup.split("\n")[0]
        # and the rule itself, section by section
        stale = snap.mission(IDS["stale"])
        assert row_glyph(stale, "running") == f"[{ACCENT}]●[/]"
        assert row_glyph(stale, "needs you") == f"[{WARN}]◆[/]"


async def test_the_ai_ranked_list_paints_the_section_glyph_too() -> None:
    """Same bug, one ✦ search away: `_ai_rows` built its rows from the raw stored
    status while `_paint_overview` swaps the whole list in, so a stale
    `awaiting_gate` ranked by ✦ painted the amber ◆ again (with no section
    header at all to contradict it). The ranked rows go through row_glyph now."""
    view, snap = _overview()
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.snap = snap
        view.ai_results = [
            {"id": IDS["stale"], "reason": "closest to your query"},
            {"id": IDS["needs"], "reason": "also close"},
        ]
        view._paint_overview()
        await pilot.pause()

        rows = {r.key: r for r in view.query("#mission-list Row")}
        stale = rows[IDS["stale"]]._markup.split("\n")[0]
        assert stale.startswith(f"[{ACCENT}]●[/]"), f"a ranked stale row looks running: {stale!r}"
        assert f"[{WARN}]◆[/]" not in stale, f"no amber diamond in the ✦ list: {stale!r}"
        needs = rows[IDS["needs"]]._markup.split("\n")[0]
        assert needs.startswith(f"[{WARN}]◆[/]"), f"a ranked needs-you row keeps the ◆: {needs!r}"


async def test_the_active_tab_keeps_every_live_section_and_search_still_filters() -> None:
    view, snap = _overview()
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.snap = snap
        view.filter = "active"
        view._paint_overview()
        await pilot.pause()
        rlst = view.query_one("#mission-list", RowList)
        assert _headers(rlst) == {"needs you": 1, "queued": 1, "running": 2, "paused": 1}
        assert IDS["recent"] not in rlst.selectable, "done is not active"

        view.filter = "all"
        view.query_text = "stale"
        view._paint_overview()
        await pilot.pause()
        assert _headers(rlst) == {"running": 1}, "search narrows across sections"
        assert rlst.selectable == [IDS["stale"]]
