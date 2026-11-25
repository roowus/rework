"""Upload transport (ADR 0005), end to end: a repo-less project's files area is
the mission's workspace, and what the mission produces goes back into it.

The harness mirrors tests/integration/test_vertical_slice.py (ScriptedProvider,
a real MissionRunner over a real sqlite store, a real per-run git worktree) but
the plan is a single coder step with no human gates: nothing here is mocked —
the seeding, the worktree, the handoff commit and the save-back all run.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    AUDIT_CLEAN,
    CODER_TESTS_BLOCK,
    ScriptedProvider,
    _auditor_script,
    role_catalog,
)
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, MissionStatus, Project
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.orchestrator.workspace import ensure_files_area, git_init_commit
from rework.roles.registry import autodiscover

OBJECTIVE = "add a notes file next to the seeded one"
# no gates: the mission must reach synthesis without a human in the loop. The
# audit step is not optional any more: the finish rails (ADR 0012) refuse to
# ship a coder's work that no auditor has seen, on this path too.
PLAN_CODE_ONLY: dict[str, Any] = {
    "steps": [
        {"step": "code", "role": "coder", "model": "flash", "context": {"brief": "$OBJECTIVE"}},
        {
            "step": "audit",
            "role": "auditor",
            "model": "flash",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {
                "diff": {"step": "code", "field": "diff"},
                "worktree_path": {"step": "code", "field": "worktree"},
                "coder_tests": {"step": "code", "field": "tests"},
                "base_sha": {"step": "code", "field": "base_sha"},
                "changed_files": {"step": "code", "field": "changed_files"},
            },
        },
    ],
}


def _script_auditor(provider: ScriptedProvider) -> None:
    """The auditor's prompt starts with its GOAL line, not a step name."""
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))


def _coder_writes_and_edits() -> list[ChatResult]:
    write_b = "cat > b.md << 'REWORK_EOF'\n# b\n\nwritten by the coder\nREWORK_EOF"
    edit_a = "cat > a.md << 'REWORK_EOF'\n# a\n\nseeded, then edited\nREWORK_EOF"
    return [
        ChatResult(
            text="reading the seeded file first",
            tool_calls=[ToolCall(id="c0", name="read_file", arguments={"path": "a.md"})],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text="writing",
            tool_calls=[
                ToolCall(id="c1", name="run_command", arguments={"command": write_b}),
                ToolCall(id="c2", name="run_command", arguments={"command": edit_a}),
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text="all done: wrote b.md and edited a.md in the workspace." + CODER_TESTS_BLOCK,
            usage=Usage(input_tokens=10, output_tokens=8),
        ),
    ]


@pytest.fixture
async def upload_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    store: SqliteStore = await open_sqlite(str(tmp_path / "upload.sqlite3"))
    try:
        project = await store.create_project(Project(name="p", settings={"repos": []}))
        area = ensure_files_area(project.id)
        (area / "a.md").write_text("# a\n\nseeded\n")
        git_init_commit(area, "seed")  # the run must start from this
        mission = await store.create_mission(
            Mission(
                id="m-upload",
                objective=OBJECTIVE,
                project_id=project.id,
                plan=PLAN_CODE_ONLY,
            )
        )
        provider = ScriptedProvider()
        yield {
            "store": store,
            "project": project,
            "area": area,
            "mission": mission,
            "provider": provider,
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
            "tmp": tmp_path,
        }
    finally:
        await store.aclose()


def _is_under(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents


async def test_upload_project_seeds_the_mission_and_saves_back(
    upload_world: dict[str, Any],
) -> None:
    store: SqliteStore = upload_world["store"]
    provider: ScriptedProvider = upload_world["provider"]
    workroot: Path = upload_world["workroot"]
    area: Path = upload_world["area"]
    provider.route("Fulfill the task", _coder_writes_and_edits())
    _script_auditor(provider)

    runner = MissionRunner(
        upload_world["mission"].id,
        store,
        InProcessEventBus(store),
        autodiscover(),
        role_catalog(),
        sqlite_checkpointer_factory(upload_world["ck"]),
        workroot,
        poll_interval_s=0.05,
        provider=provider,
    )
    await asyncio.wait_for(runner.run(), timeout=60)

    mission = await store.get_mission(upload_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE, (
        mission.result if mission else None
    )

    # the seeded files area became the mission's workspace, inside the workroot
    assert mission.repo_path, "upload transport must adopt a seeded repo_path"
    seeded = Path(mission.repo_path)
    assert _is_under(seeded, workroot)
    # the run started from the seeded a.md: the workspace carries it untouched
    # and the coder saw it as a MODIFICATION of an existing file, not a new one
    assert (seeded / "a.md").read_text() == "# a\n\nseeded\n"

    # ... and the files area came back with what the mission produced
    assert (area / "b.md").read_text() == "# b\n\nwritten by the coder\n"
    assert (area / "a.md").read_text() == "# a\n\nseeded, then edited\n"

    # the report names them
    note = mission.result["project_files"]
    assert "b.md (new)" in note
    assert "a.md (changed)" in note
    assert "Project files updated:" in mission.result["summary"]
    assert note in mission.result["summary"]


def _runner_for(world: dict[str, Any]) -> MissionRunner:
    return MissionRunner(
        world["mission"].id,
        world["store"],
        InProcessEventBus(world["store"]),
        autodiscover(),
        role_catalog(),
        sqlite_checkpointer_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )


async def test_save_back_keeps_non_ascii_filenames(upload_world: dict[str, Any]) -> None:
    """git octal-quotes non-ASCII paths unless `-z` is used, so `Résumé.md`
    failed the is_file() check and was silently dropped from the area AND the
    report."""
    store: SqliteStore = upload_world["store"]
    provider: ScriptedProvider = upload_world["provider"]
    area: Path = upload_world["area"]
    names = ["Résumé.md", "日本.md", "sub dir/é.txt"]
    write = "\n".join(
        f'mkdir -p "$(dirname {name!r})"; printf "# {name}\\n" > {name!r}' for name in names
    )
    provider.route(
        "Fulfill the task",
        [
            ChatResult(
                text="writing the documents",
                tool_calls=[ToolCall(id="c1", name="run_command", arguments={"command": write})],
                usage=Usage(input_tokens=10, output_tokens=5),
                stop_reason="tool_calls",
            ),
            ChatResult(
                text="done: wrote the files above." + CODER_TESTS_BLOCK,
                usage=Usage(input_tokens=10, output_tokens=3),
            ),
        ],
    )
    _script_auditor(provider)

    await asyncio.wait_for(_runner_for(upload_world).run(), timeout=60)

    mission = await store.get_mission(upload_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    for name in names:
        assert (area / name).is_file(), f"{name} must land in the files area"
        assert name in mission.result["project_files"], f"{name} must be in the report"


# a ship gate the owner declines: the mission is a CANCELLED rejection
PLAN_SHIP_GATE: dict[str, Any] = {
    "steps": list(PLAN_CODE_ONLY["steps"]),  # coder, then auditor (ADR 0012)
    "gates": ["ship"],
}


async def test_rejected_ship_gate_does_not_save_files(
    upload_world: dict[str, Any],
) -> None:
    """Save-back is "iff the mission ends DONE": a mission declined at the ship
    gate used to write its half-finished files into the area anyway."""
    store: SqliteStore = upload_world["store"]
    provider: ScriptedProvider = upload_world["provider"]
    area: Path = upload_world["area"]
    mission = upload_world["mission"].model_copy(
        update={"plan": {**PLAN_SHIP_GATE, "steps": PLAN_SHIP_GATE["steps"]}}
    )
    await store.update_mission(mission.id, plan=mission.plan)
    upload_world["mission"] = mission
    provider.route("Fulfill the task", _coder_writes_and_edits())
    _script_auditor(provider)
    before = (area / "a.md").read_text()

    task = asyncio.create_task(_runner_for(upload_world).run())
    for _ in range(400):
        gates = await store.list_open_gates(mission.id)
        if gates:
            break
        await asyncio.sleep(0.05)
    gates = await store.list_open_gates(mission.id)
    assert gates, "the ship gate never opened"
    await GateManager(store, InProcessEventBus(store)).decide(gates[0].id, approved=False)
    await asyncio.wait_for(task, timeout=60)

    final = await store.get_mission(mission.id)
    assert final is not None and final.status == MissionStatus.CANCELLED
    assert not (area / "b.md").exists(), "a rejected mission must not save back"
    assert (area / "a.md").read_text() == before
    assert "project_files" not in (final.result or {})


async def test_mission_that_writes_nothing_saves_nothing(upload_world: dict[str, Any]) -> None:
    """The coder writes DEFAULT_GITIGNORE into every worktree: it must not be
    reported as the mission's output, nor committed into the area."""
    store: SqliteStore = upload_world["store"]
    provider: ScriptedProvider = upload_world["provider"]
    area: Path = upload_world["area"]
    provider.route(
        "Fulfill the task",
        [
            ChatResult(
                # a coder that changes nothing still owes the rail its tests
                # block (or a reason): the finish rails are the same on every
                # driver now (ADR 0012)
                text="nothing to do here" + '\n\n```json\n{"tests": {"added": [], "changed": [], '
                '"red_green_verified": false, "commands": [], "result": "", '
                '"not_testable_reason": "no change was needed"}}\n```',
                usage=Usage(input_tokens=10, output_tokens=3),
            )
        ],
    )
    _script_auditor(provider)

    def commits() -> list[str]:
        return subprocess.run(
            ["git", "log", "--oneline"], cwd=area, capture_output=True, text=True, check=True
        ).stdout.splitlines()

    before = commits()
    await asyncio.wait_for(_runner_for(upload_world).run(), timeout=60)

    mission = await store.get_mission(upload_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert "project_files" not in (mission.result or {})
    # the no-op guard is at the _synthesize level too: no save was even
    # attempted, so a failed one cannot hide behind an empty change list
    assert "project_files_error" not in (mission.result or {})
    assert "Project files updated:" not in str((mission.result or {}).get("summary") or "")
    assert not (area / ".gitignore").exists()
    assert commits() == before, "a no-write mission must not commit into the area"
    assert not (area / "dist").exists()


async def test_audit_revision_saves_the_final_coder_state(
    upload_world: dict[str, Any],
) -> None:
    """A revision must END with the revised content: `_save_project_files`
    built its tree list newest-first, and `shutil.copy2` makes the LAST copy of
    a path win, so the EARLIEST coder's stale draft overwrote the audit-revised
    FINAL one while the mission still reported DONE."""
    from rework.kernel.state.records import Run, RunStatus, run_id

    store: SqliteStore = upload_world["store"]
    area: Path = upload_world["area"]
    tmp: Path = upload_world["tmp"]
    mission = upload_world["mission"]

    # the seeded workspace the mission started from (the mission's repo_path)
    repo = tmp / "seeded"
    shutil.copytree(area, repo)
    assert (repo / "a.md").is_file()
    await store.update_mission(mission.id, repo_path=str(repo))

    def coder_tree(name: str, body: str) -> Path:
        tree = tmp / name
        tree.mkdir()
        (tree / "a.md").write_text("# a\n\nseeded\n")  # the seeded snapshot
        git_init_commit(tree, "init")
        (tree / "report.md").write_text(body)
        subprocess.run(["git", "add", "-A"], cwd=tree, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=r", "-c", "user.email=r@r", "commit", "-m", name],
            cwd=tree,
            check=True,
            capture_output=True,
        )
        return tree

    stale = coder_tree("coder1", "v1 stale\n")  # written, then an audit revision
    final = coder_tree("coder2", "v2 FINAL\n")
    # created_at-ordered, exactly as `list_runs` returns them
    runs = [
        await store.create_run(
            Run(
                id=run_id(mission.id, "code", "coder", attempt),
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=attempt,
                status=RunStatus.COMPLETED,
                artifact={"worktree": str(tree)},
            )
        )
        for attempt, tree in enumerate((stale, final), start=1)
    ]

    note, error = await _runner_for(upload_world)._save_project_files(mission.id, runs)

    assert error == ""
    assert "report.md (new)" in note
    assert (area / "report.md").read_text() == "v2 FINAL\n", "the FINAL revision must win"


async def test_failed_save_back_is_reported_not_swallowed(
    upload_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save-back that raises must not read as success: the mission stays DONE
    (bookkeeping may not fail finished work) but the summary carries the
    NOT-updated line and never the success line."""
    import rework.orchestrator.workspace as workspace_mod

    store: SqliteStore = upload_world["store"]
    provider: ScriptedProvider = upload_world["provider"]
    area: Path = upload_world["area"]
    provider.route("Fulfill the task", _coder_writes_and_edits())
    _script_auditor(provider)

    def boom(*_args: Any, **_kwargs: Any) -> list[str]:
        raise OSError("disk on fire")

    monkeypatch.setattr(workspace_mod, "save_mission_files", boom)

    await asyncio.wait_for(_runner_for(upload_world).run(), timeout=60)

    mission = await store.get_mission(upload_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert not (area / "b.md").exists(), "nothing was written"
    assert mission.result["project_files_error"] == "disk on fire"
    assert "project_files" not in mission.result
    summary = mission.result["summary"]
    assert "Project files NOT updated: disk on fire" in summary
    assert "Project files updated:" not in summary
