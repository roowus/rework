"""REWORK_NOTIFY_CMD runs when a gate opens (found live: a budget gate sat
4.5 hours unseen). Fire-and-forget: a broken notifier never hurts a mission."""

from pathlib import Path

import pytest

from rework.kernel.gates import notify_owner
from rework.kernel.state.records import Gate, GateKind


def _gate() -> Gate:
    return Gate(id="gate-1", mission_id="m-1", kind=GateKind.PERMISSION, question="Raise to $7?")


async def test_the_notify_command_gets_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "got"
    monkeypatch.setenv(
        "REWORK_NOTIFY_CMD",
        'echo "$REWORK_GATE_ID $REWORK_GATE_KIND $REWORK_MISSION_ID"'
        f' "$REWORK_GATE_QUESTION" > {out}',
    )
    task = notify_owner(_gate())
    assert task is not None
    await task
    assert out.read_text().strip() == "gate-1 permission m-1 Raise to $7?"


async def test_no_command_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REWORK_NOTIFY_CMD", raising=False)
    assert notify_owner(_gate()) is None


async def test_a_failing_notifier_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_NOTIFY_CMD", "exit 3")
    task = notify_owner(_gate())
    assert task is not None
    await task  # logged, not raised


# -- notify_text: the same contract for events that are NOT a gate -------------


async def test_notify_text_carries_its_own_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An automatic budget raise happens with nobody looking, so it must be
    pushable exactly like a gate."""
    from rework.kernel.gates import notify_text

    out = tmp_path / "text"
    monkeypatch.setenv(
        "REWORK_NOTIFY_CMD",
        'echo "$REWORK_NOTIFY_KIND $REWORK_MISSION_ID $REWORK_BUDGET_OLD'
        f' $REWORK_BUDGET_NEW $REWORK_NOTIFY_MESSAGE" > {out}',
    )
    task = notify_text(
        "$2.00 -> $3.00 (auto): one more coder",
        "m-9",
        extra_env={"REWORK_BUDGET_OLD": "2.00", "REWORK_BUDGET_NEW": "3.00"},
    )
    assert task is not None
    await task
    assert out.read_text().strip() == (
        "budget_raise m-9 2.00 3.00 $2.00 -> $3.00 (auto): one more coder"
    )


async def test_notify_text_kind_is_a_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework.kernel.gates import notify_text

    out = tmp_path / "kind"
    monkeypatch.setenv("REWORK_NOTIFY_CMD", f'echo "$REWORK_NOTIFY_KIND" > {out}')
    task = notify_text("hi", "m-1", kind="something_else")
    assert task is not None
    await task
    assert out.read_text().strip() == "something_else"


async def test_notify_text_without_a_command_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.kernel.gates import notify_text

    monkeypatch.delenv("REWORK_NOTIFY_CMD", raising=False)
    assert notify_text("nobody home", "m-1") is None


async def test_a_failing_notify_text_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.kernel.gates import notify_text

    monkeypatch.setenv("REWORK_NOTIFY_CMD", "exit 4")
    task = notify_text("bad notifier", "m-1")
    assert task is not None
    await task  # logged, never raised
