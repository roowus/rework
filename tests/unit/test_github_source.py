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
