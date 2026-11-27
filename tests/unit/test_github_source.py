"""GitHub Issues as a mission source, with a fake `gh` runner (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.sources import github, poller


class FakeGh:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str, timeout_s: float = 60.0) -> str:
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(self.issues)
        return ""


async def test_labelled_issues_become_missions_once(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        fake = FakeGh(
            [
                {"number": 7, "title": "add() subtracts", "body": "see calc.py", "url": "u7"},
                {"number": 3, "title": "older one", "body": "", "url": "u3"},
            ]
        )
        client = github.GitHubIssues("me/toy", "rework", runner=fake)
        first = await github.poll_once(store, client)
        again = await github.poll_once(store, client)
        assert len(first) == 2 and again == [], "a re-poll creates nothing twice"
        assert first[0] == github.mission_id_for("me/toy", 3), "oldest issue first"
        m = await store.get_mission(github.mission_id_for("me/toy", 7))
        assert m is not None
        assert m.repo_origin == "https://github.com/me/toy.git"
        assert m.objective.startswith("[me/toy#7] add() subtracts") and "see calc.py" in m.objective
        assert m.result["source"]["kind"] == "github" and m.result["source"]["item_id"] == "7"
        list_call = fake.calls[0]
        assert "--label" in list_call and "rework" in list_call and "open" in list_call
    finally:
        await store.aclose()


async def test_a_finished_mission_comments_on_its_issue(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(
            Mission(
                id="m-gh-x",
                objective="x",
                status=MissionStatus.DONE,
                result={
                    "source": {"kind": "github", "item_id": "7", "repo": "me/toy"},
                    "outcome": "done",
                    "summary": "fixed add()",
                    "pr_url": "https://github.com/me/toy/pull/9",
                },
            )
        )
        fake = FakeGh([])
        assert await poller.write_back(
            github.GitHubIssues("me/toy", "rework", runner=fake), store, "m-gh-x"
        )
        comment = fake.calls[-1]
        assert comment[:3] == ("issue", "comment", "7") and "me/toy" in comment
        assert "pull/9" in comment[-1]
    finally:
        await store.aclose()


def test_sources_are_off_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.sources import configured

    for name in ("REWORK_GITHUB_REPO", "REWORK_PLANE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert configured() == []
    monkeypatch.setenv("REWORK_GITHUB_REPO", "me/toy")
    kinds = [s["kind"] for s in configured()]
    assert kinds == ["github"]


async def test_status_sync_keeps_one_phase_label_and_closes_on_done() -> None:
    fake = FakeGh([])
    client = github.GitHubIssues("me/toy", "rework", runner=fake)
    await client.set_status("7", "working")
    await client.set_status("7", "done")
    creates = [c for c in fake.calls if c[:2] == ("label", "create")]
    assert len(creates) == 4, "the four phase labels are created once"
    edits = [c for c in fake.calls if c[:2] == ("issue", "edit")]
    assert "rework:working" in edits[0] and "rework:done" in edits[1]
    removed = edits[1][edits[1].index("--remove-label") + 1]
    assert "rework:working" in removed and "rework:done" not in removed
    assert fake.calls[-1][:3] == ("issue", "close", "7")


async def test_the_daemon_syncs_each_phase_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's sync loop mirrors a phase change once and records it, so the
    next tick (or a restart) does not repeat it."""
    import asyncio

    import rework.sources as sources
    from rework.orchestrator import daemon as daemon_mod

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    synced: list[tuple[str, str]] = []

    async def fake_sync(source: dict, status: str) -> None:
        synced.append((source["item_id"], status))

    monkeypatch.setattr(sources, "sync_status", fake_sync)
    monkeypatch.setattr(daemon_mod, "SYNC_EVERY_S", 0.05)
    try:
        await store.create_mission(
            Mission(
                id="m-gh-1",
                objective="x",
                status=MissionStatus.RUNNING,
                result={"source": {"kind": "github", "item_id": "7"}},
            )
        )
        d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
        d._store = store
        task = asyncio.create_task(d._sync_tracker_status())
        await asyncio.sleep(0.3)
        await store.transition_mission("m-gh-1", MissionStatus.AWAITING_GATE)
        await asyncio.sleep(0.3)
        task.cancel()
        assert synced == [("7", "running"), ("7", "awaiting_gate")], synced
        row = await store.get_mission("m-gh-1")
        assert row is not None and row.result["source_phase"] == "needs_you"
    finally:
        await store.aclose()
