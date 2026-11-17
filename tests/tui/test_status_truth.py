"""Only a mission that is actually working may look like it: spinner, live
tag, role chip, and a clock that moves.

`actions.ACTIVE` answers one question ("is this mission finished?"), which is
why a cancel/resume/pause verb is legal for those statuses and why the poll
keeps enriching them. Every "it is working RIGHT NOW" surface (role chip, live
tag, spinner, elapsed clock, the graph's orchestrator node) was asking that
same set, so a `pending` mission claimed a role and a clock and an `on_hold`
mission span a live spinner and counted its own still time as work.
`actions.WORKING` is the second question, answered once.

The tests are split the same way the bug was: the constant, the pure projectors
(each surface reads one), and the three painted surfaces (row, header, card)."""

from __future__ import annotations

import re
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from rework.kernel.state.records import Mission, MissionStatus
from rework.tui import actions, theme
from rework.tui.hub import Snapshot
from rework.tui.spinner import FRAMES
from rework.tui.theme import FAINT, glyph, status_text
from rework.tui.views.dashboard import DashboardView
from rework.tui.widgets.cards import MissionCard
from rework.tui.widgets.graph import GLayer, mission_layers, mission_nodes

#: a whole day past `created_at`: if a paused mission's clock ran, the header
#: would say "1d 0h" instead of its own (frozen) duration
LATER = 24 * 3600.0


def _plain(text: str) -> str:
    """Rich markup stripped: these surfaces carry colour, the tests read text."""
    return re.sub(r"\[[^\]]*\]", "", text)


def _mission(mid: str, status: MissionStatus, created: float = 1_000_000.0) -> Mission:
    return Mission(
        id=mid,
        objective=f"mission {mid}",
        title=f"Mission {mid}",
        status=status,
        created_at=created,
        updated_at=created + 60.0,
    )


def _snap(missions: list[Mission]) -> Snapshot:
    return Snapshot(missions=missions, gates=[], projects=[], loaded=True)


def _node_statuses(nodes: list[Any]) -> set[str]:
    """Every node status across nodes or layers (the two graph builders)."""
    out: set[str] = set()
    for item in nodes:
        if isinstance(item, GLayer):
            out.update(n.status for n in item.nodes)
        else:
            out.add(item.status)
    return out


class DashApp(App[None]):
    def __init__(self, view: DashboardView) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        yield self._view


class CardApp(App[None]):
    def __init__(self, card: MissionCard) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        yield self._card


# -- (a) the two questions, answered once -------------------------------------


def test_working_is_active_minus_pending_and_on_hold() -> None:
    """The ONLY statuses that differ are the two that are not working: `pending`
    has not started yet, and `on_hold` has been stopped on purpose, so a clock,
    a spinner or a role chip under either of them is a lie. `awaiting_gate` is
    WORKING (the mission is on the hook waiting for a decision) and `running`
    obviously is."""
    assert actions.WORKING <= actions.ACTIVE
    assert actions.WORKING == {"running", "awaiting_gate"}
    assert actions.WORKING ^ actions.ACTIVE == {"pending", "on_hold"}


def test_a_paused_or_cancelled_mission_offers_its_verbs_unchanged() -> None:
    """(e) WORKING is a display set: a paused mission is still resumable AND
    cancellable, and a cancelled one offers neither resume nor pause."""
    assert actions.can("resume", "on_hold")
    assert actions.can("cancel", "on_hold")
    assert not actions.can("pause", "on_hold")
    for verb in ("resume", "pause"):
        assert not actions.can(verb, "cancelled"), verb
    assert not actions.can("cancel", "cancelled")


def test_graph_orchestrator_node_does_not_run_while_paused() -> None:
    """(b, second surface) the graph's orchestrator node reads the same set: a
    paused or queued mission's node must not be painted as running."""
    for status in (MissionStatus.ON_HOLD, MissionStatus.PENDING):
        mission = _mission(f"m-{status.value}", status).model_copy(
            update={"plan": {"orchestrator": {"model": "m"}}}
        )
        for view_of in (
            lambda m: mission_nodes(m, [], [], {}, {}),
            lambda m: mission_layers(m, [], [], {}, {}, None),
        ):
            assert "running" not in _node_statuses(view_of(mission)), status
    # ... while a mission genuinely working still animates
    live = _mission("m-run0000000b", MissionStatus.RUNNING).model_copy(
        update={"plan": {"orchestrator": {"model": "m"}}}
    )
    assert "running" in _node_statuses(mission_nodes(live, [], [], {}, {}))


async def test_a_paused_mission_paints_no_role_chip_and_no_animation() -> None:
    view = DashboardView()
    # the two statuses only "un-finished", never working: neither may claim a
    # role or a spinner (pending has not started, on_hold has been stopped)
    for mission, section in (
        (_mission("m-hold000000c", MissionStatus.ON_HOLD), "paused"),
        (_mission("m-pend000000c", MissionStatus.PENDING), "queued"),
    ):
        view.snap = _snap([mission])
        view.mission_role = {mission.id: "coder"}
        row = view._mission_row(mission, section, frame=FRAMES[2])
        assert "coder" not in row, row
        assert not any(f in row for f in FRAMES), row
    assert glyph("on_hold") in view._mission_row(
        _mission("m-hold000000c", MissionStatus.ON_HOLD), "paused", frame=FRAMES[2]
    )  # the ‖ pause marker, not an animation

    # the same row for a mission that IS working keeps both
    live = _mission("m-run0000000d", MissionStatus.RUNNING)
    view.snap = _snap([live])
    view.mission_role = {live.id: "coder"}
    row = view._mission_row(live, "running", frame=FRAMES[2])
    assert "coder" in row, row
    assert FRAMES[2] in row, row


# -- (c) cancelled is ⊘ and the word, on every surface ------------------------


async def test_a_cancelled_mission_paints_the_cross_and_the_word_everywhere() -> None:
    mission = _mission("m-cancel0000e", MissionStatus.CANCELLED)
    view = DashboardView()
    view.snap = _snap([mission])
    assert glyph("cancelled") == f"[{FAINT}]⊘[/]", "theme owns the glyph"
    assert status_text("cancelled") == f"[{FAINT}]⊘ cancelled[/]", "and the word"
    # the label is spelled out in STATUS_LABEL instead of falling back to the
    # value with underscores replaced: same text either way, so this pins the
    # explicit entry (a future status value cannot inherit the wrong word)
    assert theme.STATUS_LABEL["cancelled"] == "cancelled"
    assert theme.STATUS_LABEL["aborted"] == "aborted"

    # the row: its leading MARKER (a row sits under a section header, so it
    # carries the glyph, not the word; the word is on the header and the card)
    row = view._mission_row(mission, "recent")
    assert row.startswith(glyph("cancelled")), row
    assert glyph("cancelled") in row and glyph("on_hold") not in row, row

    # the card
    card = MissionCard(mission.id)
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(mission, [])
        await pilot.pause()
        head = _plain(str(card.query(".mc-head").first().render()))
        assert "⊘ cancelled" in head, head

    # the drill-down header
    view = DashboardView()
    view.snap = _snap([mission])
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.mission_id = mission.id
        await pilot.pause()
        view._paint_mission_static()
        await pilot.pause()
        header = _plain(str(view.query_one("#m-header", Static).render()))
        assert "⊘ cancelled" in header, header


# -- (d) a mission at a gate is still working --------------------------------


async def test_a_mission_at_a_gate_still_animates_and_keeps_its_role() -> None:
    """(d) `awaiting_gate` is on the hook: it is working, so it keeps its role
    chip and its live spinner (the amber ◆ marker is the gate note, not the
    row's leading icon)."""
    mission = _mission("m-gate000000f", MissionStatus.AWAITING_GATE)
    view = DashboardView()
    view.snap = _snap([mission])
    view.mission_role = {mission.id: "coder"}
    row = view._mission_row(mission, "running", frame=FRAMES[2])
    assert "coder" in row, row
    # the row's leading marker still comes from theme for a gated mission
    assert view._row_mark(mission, "running", FRAMES[2]) == glyph("running")

    # the live tag follows WORKING too: a gate is live, a pause is not
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.mission_id = mission.id
        await pilot.pause()
        view.mtab = "gates"
        view._paint_detail_tabs()
        await pilot.pause()
        assert "live" in str(view.query_one("#m-follow", Static).render())

        paused = _mission("m-hold0000010", MissionStatus.ON_HOLD)
        view.snap = _snap([paused])
        view.mission_id = paused.id
        view._paint_detail_tabs()
        await pilot.pause()
        follow = str(view.query_one("#m-follow", Static).render())
        assert "live" not in follow, "a pause is not live"


# -- (f) the paused clock does not move -------------------------------------


async def test_the_paused_clock_does_not_advance_across_a_tick() -> None:
    """(f) the header's elapsed text was computed from `time.time()` for every
    ACTIVE status, so a paused mission's clock kept running and counted the
    pause itself as work. It now stops at the mission's own last update, so two
    ticks far apart paint the same text, while a WORKING mission's clock really
    does advance with the wall clock."""

    def elapsed_starts(view: DashboardView, now: float) -> str:
        view._run_rows(frame=FRAMES[2], now=now)  # the tick that repaints too
        view._paint_mission_static(frame=FRAMES[2], now=now)
        header = _plain(str(view.query_one("#m-header", Static).render()))
        found = re.search(r"started [^·]+ · ([0-9]+[a-z] [0-9]+[a-z])", header)
        assert found, header
        return found.group(1)

    paused = _mission("m-hold0000011", MissionStatus.ON_HOLD)
    view = DashboardView()
    view.snap = _snap([paused])
    app = DashApp(view)
    async with app.run_test() as pilot:
        view.mission_id = paused.id
        await pilot.pause()
        # created 1m before its last update: a pause freezes at "1m 00s", where
        # the wall clock (created_at is a year ago when the suite runs) would
        # have printed weeks
        first = elapsed_starts(view, paused.updated_at + 60.0)
        later = elapsed_starts(view, paused.updated_at + LATER)
        assert first == "1m 00s", first
        assert first == later, f"a paused mission's clock advanced: {first} -> {later}"

        # the control: the same mission, still working, measures the wall clock
        working = paused.model_copy(
            update={"status": MissionStatus.RUNNING, "created_at": time.time() - 7200.0}
        )
        view.snap = _snap([working])
        assert elapsed_starts(view, working.updated_at + LATER).startswith("2h 00m")


def test_the_orchestrator_row_does_not_animate_while_paused() -> None:
    """The orchestrator pseudo-row forced the status to `running` for every
    ACTIVE status, so a paused mission kept a spinner in its sessions list."""
    paused = _mission("m-hold0000012", MissionStatus.ON_HOLD).model_copy(
        update={"plan": {"orchestrator": {"model": "m"}}}
    )
    view = DashboardView()
    view.snap = _snap([paused])
    view.mission_id = paused.id
    rows = {r.key: r for r in view._run_rows(frame=FRAMES[2], now=paused.created_at + 120.0)}
    orch = f"{paused.id}:orchestrate:orchestrator:1"
    assert orch in rows, list(rows)
    assert not any(f in rows[orch].markup for f in FRAMES), rows[orch].markup

    live = paused.model_copy(update={"status": MissionStatus.RUNNING})
    view.snap = _snap([live])
    view.mission_id = live.id
    rows = {r.key: r for r in view._run_rows(frame=FRAMES[2], now=live.created_at + 120.0)}
    assert FRAMES[2] in rows[orch].markup, rows[orch].markup
