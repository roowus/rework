"""MissionCard unit tests: mount the widget in a bare app, no store needed.

The card is handed the full Mission model by the Work view, so the tests
build Missions directly and drive update_from()/paint() the same way
_refresh_cards() does."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from rework.kernel.state.records import EventRecord, EventType, Mission, MissionStatus
from rework.tui.widgets.base import Btn
from rework.tui.widgets.cards import MissionCard

PR_URL = "https://github.com/roowus/rework/pull/123"


class CardApp(App[None]):
    """A bare app that mounts a single mission card."""

    def __init__(self, card: MissionCard) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        yield self._card


def _mission(result: dict | None) -> Mission:
    return Mission(
        id="m-test0000000a",
        objective="add dark mode to the settings page",
        title="Dark mode",
        status=MissionStatus.DONE,
        plan={"kind": "fix"},
        result=result,
    )


async def test_mission_card_shows_pr_line_when_result_has_pr_url() -> None:
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_mission({"pr_url": PR_URL}), [])
        await pilot.pause()
        pr = card.query_one(".mc-pr", Static)
        assert pr.display, "the PR line must be visible when result has a pr_url"
        assert f"PR: {PR_URL}" in str(pr.render())


async def test_mission_card_hides_pr_line_when_result_is_none() -> None:
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_mission(None), [])
        await pilot.pause()
        pr = card.query_one(".mc-pr", Static)
        assert not pr.display, "missions without a result must not show a PR line"
        assert not str(pr.render())


async def test_mission_card_hides_pr_line_when_result_has_no_pr_url() -> None:
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_mission({"summary": "done, no PR requested"}), [])
        await pilot.pause()
        pr = card.query_one(".mc-pr", Static)
        assert not pr.display, "a result without pr_url must not show a PR line"
        assert not str(pr.render())


async def test_mission_card_hides_pr_line_when_pr_url_is_blank() -> None:
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_mission({"pr_url": ""}), [])
        await pilot.pause()
        pr = card.query_one(".mc-pr", Static)
        assert not pr.display, "a blank pr_url is no PR"
        assert not str(pr.render())


async def test_mission_card_preview_button_only_with_a_pr_url() -> None:
    """The Preview button is the card's only entry point to `rework preview`
    (ADR 0008), so it must appear exactly when there is a PR to preview."""
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        preview = card.query_one(".mc-previewbtn", Btn)

        card.update_from(_mission(None), [])
        await pilot.pause()
        assert not preview.display

        card.update_from(_mission({"pr_url": PR_URL}), [])
        await pilot.pause()
        assert preview.display
        assert preview.action == "preview"


async def test_mission_card_preview_click_posts_the_preview_verb() -> None:
    """A click must post the verb the Work view routes (on_mission_card_act)."""
    seen: list[tuple[str, str]] = []

    class ClickApp(CardApp):
        def on_mission_card_act(self, event: MissionCard.Act) -> None:
            seen.append((event.verb, event.gate_id))

    card = MissionCard("m-test0000000a")
    app = ClickApp(card)
    async with app.run_test() as pilot:
        card.update_from(_mission({"pr_url": PR_URL}), [])
        await pilot.pause()
        card.query_one(".mc-previewbtn", Btn).action_press()
        await pilot.pause()
    assert seen == [("preview", "")]


def _ev(seq: int, **payload: Any) -> EventRecord:
    """A PROGRESS event for the card's mission, with an explicit seq."""
    return EventRecord(
        mission_id="m-test0000000a",
        run_id="m-test0000000a:code:coder:1",
        type=EventType.PROGRESS,
        payload=payload,
        seq=seq,
    )


def _call(seq: int, name: str) -> EventRecord:
    return _ev(seq, trajectory="tool_call", name=name, arguments="path=x")


def _status(status: MissionStatus) -> Mission:
    return Mission(
        id="m-test0000000a",
        objective="add dark mode to the settings page",
        status=status,
        plan={"kind": "fix"},
    )


def _tail_text(card: MissionCard) -> str:
    return str(card.query_one(".mc-tail", Static).render())


async def test_mission_card_tail_skips_node_enter_events() -> None:
    """Finding #37: `{"node": ..., "phase": "enter"}` is trajectory plumbing —
    the Dashboard feed shows it, the card's tail must not."""
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_status(MissionStatus.RUNNING), [])
        card.add_events(
            [
                _ev(1, node="prepare", phase="enter"),
                _call(2, "read_file"),
                _ev(3, node="loop", phase="enter"),
            ]
        )
        await pilot.pause()
        tail = _tail_text(card)
        assert "prepare" not in tail and "loop" not in tail
        assert "read_file" in tail
        assert len(card.tail) == 1

        # a card that only ever saw node-enter events shows no tail at all
        card.tail = []
        card.add_events([_ev(4, node="handoff", phase="enter")])
        await pilot.pause()
        assert card.tail == []
        assert not card.query_one(".mc-tail", Static).display


async def test_finished_mission_tail_rebuilds_from_the_newest_events() -> None:
    """A finished card freezes whatever it had; rebuild_tail must replace those
    early lines with the mission's newest renderable events."""
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_status(MissionStatus.DONE), [])
        early = [_call(i, f"early_tool_{i}") for i in range(1, 5)]
        card.add_events(early)
        await pilot.pause()
        assert "early_tool_1" in _tail_text(card)

        late = [_call(i, f"late_tool_{i}") for i in range(5, 13)]
        card.rebuild_tail(early + late)
        await pilot.pause()
        tail = _tail_text(card)
        assert "late_tool_12" in tail and "late_tool_8" in tail
        assert "early_tool_1" not in tail, "the stale early lines must be gone"
        assert len(card.tail) == MissionCard.TAIL
        assert card.last_seq == 12

        # nothing renderable at all -> no tail, not a stale one
        card.rebuild_tail([_ev(13, node="report", phase="enter")])
        await pilot.pause()
        assert card.tail == []
        assert not card.query_one(".mc-tail", Static).display


async def test_active_mission_card_keeps_appending_incrementally() -> None:
    """The live path is unchanged: each poll appends and keeps the last TAIL
    lines, tracking the newest seq."""
    card = MissionCard("m-test0000000a")
    app = CardApp(card)
    async with app.run_test() as pilot:
        card.update_from(_status(MissionStatus.RUNNING), [])
        card.add_events([_call(1, "grep")])
        await pilot.pause()
        card.add_events([_call(2, "read_file")])
        await pilot.pause()
        tail = _tail_text(card)
        assert "grep" in tail and "read_file" in tail
        assert card.last_seq == 2
        assert not card.tail_filled

        card.add_events([_call(i, f"tool_{i}") for i in range(3, 12)])
        await pilot.pause()
        tail = _tail_text(card)
        assert len(card.tail) == MissionCard.TAIL
        assert "tool_11" in tail and "grep" not in tail
        assert card.last_seq == 11
