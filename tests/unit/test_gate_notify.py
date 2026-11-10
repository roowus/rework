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
