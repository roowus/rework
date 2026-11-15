"""The card's "Retry PR" button: shown for a finished mission whose PR delivery
is pending, and driven by ONE writer (`actions.can` via the card's sync loop) so
nothing can overwrite it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from textual.app import App, ComposeResult

from rework.kernel.state.records import Mission, MissionStatus
from rework.tui import actions
from rework.tui.widgets.base import Btn
from rework.tui.widgets.cards import MissionCard

PR_URL = "https://github.com/roowus/rework/pull/9"
PENDING = {"pr_pending": True, "pr_status": "failed", "pr_error": "PushError: nope"}


class CardApp(App[None]):
    def __init__(self, card: MissionCard) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        yield self._card


def _mission(result: dict[str, Any] | None, status: MissionStatus = MissionStatus.DONE) -> Mission:
    return Mission(
        id="m-test0000000a",
        objective="add dark mode",
        status=status,
        plan={"kind": "fix-pr"},
        result=result,
    )


def test_can_retry_pr_only_for_a_done_mission_with_a_pending_delivery() -> None:
    assert actions.can("retry-pr", "done", PENDING) is True
    assert actions.can("retry-pr", "done", {**PENDING, "pr_url": PR_URL}) is False
    assert actions.can("retry-pr", "done", {"summary": "x"}) is False
    assert actions.can("retry-pr", "done", None) is False
    assert actions.can("retry-pr", "running", PENDING) is False, "a stale marker on a live run"
    assert "retry-pr" in actions.VERBS


def test_existing_verbs_keep_their_status_only_semantics() -> None:
    assert actions.can("pause", "running") and not actions.can("pause", "done")
    assert actions.can("resume", "on_hold") and not actions.can("resume", "running")
    assert actions.can("cancel", "awaiting_gate") and not actions.can("cancel", "done")
    assert actions.can("fetch", "done") and not actions.can("fetch", "running")


async def test_retry_pr_button_visibility_follows_the_recorded_delivery() -> None:
    card = MissionCard("m-test0000000a")
    async with CardApp(card).run_test() as pilot:
        retry = card.query_one(".mc-retrypr", Btn)
        assert retry.action == "retry-pr"

        card.update_from(_mission(PENDING), [])
        await pilot.pause()
        assert retry.display, "DONE + pr_pending + no pr_url: the button must show"

        card.update_from(_mission({**PENDING, "pr_url": PR_URL}), [])
        await pilot.pause()
        assert not retry.display, "a PR exists: nothing to retry"

        card.update_from(_mission({"summary": "plain done"}), [])
        await pilot.pause()
        assert not retry.display, "a plain DONE mission never shows it"

        card.update_from(_mission(PENDING), [])
        await pilot.pause()
        assert retry.display
        card.update_from(
            _mission({"pr_url": PR_URL, "pr_pending": False, "pr_status": "opened"}), []
        )
        await pilot.pause()
        assert not retry.display, "after a successful retry the button goes away"

        card.update_from(_mission(PENDING, MissionStatus.RUNNING), [])
        await pilot.pause()
        assert not retry.display, "only a finished mission can be retried"


async def test_pressing_retry_pr_reaches_control_and_the_refreshed_card_hides_the_button() -> None:
    """Button -> MissionCard.Act('retry-pr') -> actions.control -> retry_pr ->
    hub.refresh() repaints the card from the stored mission."""
    card = MissionCard("m-test0000000a")
    seen: list[str] = []
    notes: list[str] = []
    store = {"mission": _mission(PENDING)}

    async def retry(mission_id: str) -> tuple[bool, str]:
        seen.append(mission_id)
        store["mission"] = _mission({"pr_url": PR_URL, "pr_pending": False})
        return True, PR_URL

    async def refresh() -> None:
        card.update_from(store["mission"], [])

    fake = SimpleNamespace(
        hub=SimpleNamespace(data=SimpleNamespace(retry_pr=retry), refresh=refresh),
        notify=lambda msg, **_k: notes.append(str(msg)),
    )

    class RoutingApp(CardApp):
        def on_mission_card_act(self, event: MissionCard.Act) -> None:
            # what views/work.py does for these verbs
            if event.verb in ("pause", "resume", "cancel", "retry-pr"):
                self.run_worker(actions.control(fake, event.card.mission_id, event.verb))  # type: ignore[arg-type]

    async with RoutingApp(card).run_test() as pilot:
        card.update_from(store["mission"], [])
        await pilot.pause()
        retry_btn = card.query_one(".mc-retrypr", Btn)
        assert retry_btn.display
        retry_btn.action_press()
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert seen == ["m-test0000000a"]
        assert any(PR_URL in n for n in notes)
        assert not retry_btn.display
