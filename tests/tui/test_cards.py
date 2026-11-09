"""MissionCard unit tests: mount the widget in a bare app, no store needed.

The card is handed the full Mission model by the Work view, so the tests
build Missions directly and drive update_from()/paint() the same way
_refresh_cards() does."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from rework.kernel.state.records import Mission, MissionStatus
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
