"""MissionCard unit tests: mount the widget in a bare app, no store needed.

The card is handed the full Mission model by the Work view, so the tests
build Missions directly and drive update_from()/paint() the same way
_refresh_cards() does."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from rework.kernel.state.records import Mission, MissionStatus
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
