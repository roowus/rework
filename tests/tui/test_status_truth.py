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

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
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
    # the label is now stated in STATUS_LABEL rather than derived by
    # `status_text`'s fallback (`value.replace("_", " ")`). Both entries re-state
    # a word the fallback already produced, so the rendered bytes are unchanged:
    # what these two lines pin is that the word is stated, not inherited
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


# -- (g) a stopped mission's RUN rows are stopped too -------------------------

RUN_CREATED = 1_000_000.0


def _run(mid: str, status: RunStatus = RunStatus.RUNNING, role: str = "coder") -> Run:
    return Run(
        id=f"{mid}:code:{role}:1",
        mission_id=mid,
        role=role,
        step="code",
        status=status,
        created_at=RUN_CREATED,
        updated_at=RUN_CREATED,
    )


def _detail(status: MissionStatus, run_status: RunStatus = RunStatus.RUNNING) -> Any:
    """The drill-down of a mission whose RUN ROW is stored RUNNING, which is
    exactly what the engine leaves behind on an owner pause or cancel: it
    cancels the live role tasks and never rewrites the rows (engine.py:1495)."""
    mission = _mission(f"m-{status.value}", status)
    run = _run(mission.id, run_status)
    view = DashboardView()
    view.snap = _snap([mission])
    # both the mission and the run have a brand new event: nothing here is
    # stalled, so any marker that is not a frame is a deliberate choice
    view.snap.last_event = {mission.id: mission.updated_at, run.id: run.updated_at}
    view.mission_id = mission.id
    view.runs = [run]
    return view, mission, run


@pytest.mark.parametrize("status", [MissionStatus.ON_HOLD, MissionStatus.CANCELLED])
def test_a_stopped_mission_does_not_animate_its_run_rows(status: MissionStatus) -> None:
    """(g) A pause or cancel stops the ROLE TASKS but leaves their rows stored
    RUNNING, so a row built from that run's own status kept spinning and its
    clock kept counting the still time as work. Under a mission that is not
    WORKING the row takes the MISSION's marker (‖ paused, ⊘ cancelled) and its
    elapsed text is frozen at the mission's last update."""
    view, mission, run = _detail(status)

    def rows(now: float) -> dict[str, str]:
        return {r.key: r.markup for r in view._run_rows(frame=FRAMES[2], now=now)}

    first = rows(mission.updated_at + 60.0)
    assert run.id in first, list(first)
    assert not any(f in first[run.id] for f in FRAMES), (
        f"a {status.value} mission animated a run row: {first[run.id]}"
    )
    assert glyph(status.value) in first[run.id], first[run.id]
    # the clock is frozen at the mission's own stamp, not the wall clock: the
    # run was created one minute before it, where the wall clock would say days
    assert "1m 00s" in first[run.id], first[run.id]
    later = rows(mission.updated_at + LATER)[run.id]
    assert first[run.id] == later, f"the run clock advanced: {first[run.id]} -> {later}"


@pytest.mark.parametrize("status", [MissionStatus.ON_HOLD, MissionStatus.CANCELLED])
def test_a_finished_run_under_a_stopped_mission_keeps_its_own_marker(
    status: MissionStatus,
) -> None:
    """The other half of (g): a run that genuinely finished is not a lie, so it
    keeps its own terminal marker (✓ done) instead of the mission's ‖ or ⊘."""
    view, _mission_row, run = _detail(status, RunStatus.COMPLETED)
    markup = {r.key: r.markup for r in view._run_rows(frame=FRAMES[2], now=RUN_CREATED + 60.0)}
    assert glyph("completed") in markup[run.id], markup[run.id]


def test_a_mission_at_a_gate_still_animates_its_run_rows() -> None:
    """... and the fix must not over-reach: `awaiting_gate` is WORKING, so a run
    that is stored RUNNING under it still animates."""
    view, _mission_row, run = _detail(MissionStatus.AWAITING_GATE)
    markup = {r.key: r.markup for r in view._run_rows(frame=FRAMES[2], now=RUN_CREATED + 60.0)}
    assert FRAMES[2] in markup[run.id], markup[run.id]


# -- (h) the label fallback is still the fallback -----------------------------


def test_a_status_with_no_label_falls_back_to_its_value() -> None:
    """The two STATUS_LABEL entries "cancelled" and "aborted" spell out what
    `status_text` would have derived anyway, so the rendered bytes are the same;
    this pins that derivation for a status that has no entry at all (a test that
    only checked cancelled/aborted could not tell the table from the fallback)."""
    assert status_text("needs_review") == f"[{FAINT}]· needs review[/]"
    assert status_text("under-review") == f"[{FAINT}]· under-review[/]", "no underscores to swap"
    # ... and the two explicit entries render exactly those bytes: proof that
    # stating them changed the table and not the screen
    assert status_text("cancelled") == f"[{FAINT}]⊘ cancelled[/]"
    assert status_text("aborted") == f"[{FAINT}]⊘ aborted[/]"
