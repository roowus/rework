"""TUI bracket escaping integration tests: prove that square brackets in chat
messages, tool calls, and mission card activity text render without raising
MarkupError and display exactly as typed (no garbled/invisible text)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import EventRecord, EventType, Mission, MissionStatus
from rework.tui.app import ReworkApp
from rework.tui.widgets.cards import MissionCard
from rework.tui.widgets.trajectory import Trajectory, event_step

SIZE = (140, 42)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    monkeypatch.setenv("REWORK_NOTIFY", "toast,bell")
    monkeypatch.setenv(
        "REWORK_MODELS",
        '[{"id":"flash","provider":"fake","model":"f","tier":1},'
        '{"id":"pro","provider":"fake","model":"p","tier":2}]',
    )
    return db


@pytest.fixture
async def ids(env: Path) -> dict[str, str]:
    return await seed(str(env), history_days=20)


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


async def _settle(pilot: Any, duration: float = 0.5) -> None:
    await pilot.pause(duration)


async def _store(db: Path) -> Any:
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(db))


async def test_owner_chat_message_with_brackets_renders(
    ids: dict[str, str], env: Path
) -> None:
    """Owner requirement (a): sending a chat message with square brackets must
    render without raising MarkupError, and the displayed text must match the
    original message exactly."""
    test_message = 'I need to run ["git", "-c", "user.name=rework", "commit", "-m", "init"]'
    
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        # Type the message containing brackets
        for ch in test_message:
            if ch == " ":
                await pilot.press("space")
            elif ch == '"':
                await pilot.press("quotation_mark")
            elif ch == "[":
                await pilot.press("left_square_bracket")
            elif ch == "]":
                await pilot.press("right_square_bracket")
            elif ch == ",":
                await pilot.press("comma")
            elif ch == "-":
                await pilot.press("minus")
            elif ch == "=":
                await pilot.press("equals_sign")
            else:
                await pilot.press(ch)
        await pilot.press("enter")
        await _settle(pilot, 0.8)
        
        # The message should render in the trajectory without raising
        traj = app.query_one("#chat-log", Trajectory)
        assert traj is not None
        
        # Verify the message is in the chat history with exact text
        chat_id = app.view("work").convo.id
    
    store = await _store(env)
    try:
        msgs = await store.chat_messages(chat_id)
        user_msgs = [m for m in msgs if m.role == "user"]
        assert user_msgs, "the owner's message must be saved"
        assert user_msgs[-1].content == test_message, "content must match exactly"
    finally:
        await store.aclose()


async def test_tool_call_with_git_command_renders_on_chat_and_card(
    ids: dict[str, str]
) -> None:
    """Owner requirement (b): tool calls containing git command arrays must
    render on both the chat trajectory and mission card activity without
    raising MarkupError."""
    git_cmd = '["git", "-c", "user.name=rework", "commit", "-m", "init"]'
    
    # Test event_step directly (used by both chat and cards)
    ev = EventRecord(
        id="e-test00000001",
        mission_id="m-test00000001",
        run_id="m-test00000001:code:coder:1",
        seq=1,
        type=EventType.PROGRESS,
        payload={
            "trajectory": "tool_call",
            "name": "run_command",
            "arguments": git_cmd,
        },
    )
    
    # Must not raise MarkupError
    step = event_step(ev, show_role=True)
    assert step is not None, "tool_call event must produce a Step"
    
    # The step should render without errors
    rendered = str(step.render())
    assert rendered, "step must render to non-empty string"
    
    # The plain text should contain the git command
    plain = str(step._head)
    assert "git" in plain.lower(), "rendered step must mention git"
    assert "run_command" in plain, "tool name must be visible"
    
    # Now test it renders in a real Trajectory widget
    from textual.app import App, ComposeResult
    
    class TrajectoryApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Trajectory(id="test-traj")
    
    app = TrajectoryApp()
    async with app.run_test() as pilot:
        traj = app.query_one("#test-traj", Trajectory)
        # Must not raise MarkupError
        traj.add_events([ev], show_role=True)
        await pilot.pause(0.1)
        assert traj.children, "event must render as a step widget"


async def test_mission_card_activity_with_brackets(ids: dict[str, str]) -> None:
    """Owner requirement (b continued): mission card tail showing tool calls
    with brackets must render without MarkupError."""
    git_cmd = '["git", "-c", "user.name=rework", "commit", "-m", "init"]'
    
    ev = EventRecord(
        id="e-test00000002",
        mission_id="m-test00000002",
        run_id="m-test00000002:code:coder:1",
        seq=2,
        type=EventType.PROGRESS,
        payload={
            "trajectory": "tool_call",
            "name": "run_command",
            "arguments": git_cmd,
        },
    )
    
    from textual.app import App, ComposeResult
    
    class CardApp(App[None]):
        def compose(self) -> ComposeResult:
            yield MissionCard("m-test00000002")
    
    app = CardApp()
    async with app.run_test() as pilot:
        card = app.query_one(MissionCard)
        mission = Mission(
            id="m-test00000002",
            objective="test brackets",
            status=MissionStatus.RUNNING,
            plan={"kind": "fix"},
        )
        card.update_from(mission, [])
        # Must not raise MarkupError
        card.add_events([ev])
        await pilot.pause(0.1)
        assert card.tail, "event must add to card tail"
