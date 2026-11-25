"""The harness role: an external coding agent's loop, rework's contract (ADR 0015).

A fake harness binary on PATH does two things a real one would: it writes a file
into the worktree, and it prints a final message carrying the same tests block
the coder must report. What is under test is that everything else is unchanged:
the artifact is a `coder_handoff`, the host commits it, red-green still verifies
it, and two runs never share a session.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import CODER_TESTS_BLOCK
from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.state.records import Mission, Run, RunStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.roles.base import RoleBuildCtx
from rework.roles.builtin.harness import _final_text_of, _progress_note, harness_argv
from rework.roles.registry import autodiscover


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=r,
        check=True,
        capture_output=True,
    )
    return r


@pytest.fixture
def fake_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A harness that edits the file and emits a stream-json result.

    The tests block is written to its own file and read back, so the JSON the
    fake prints carries the block verbatim: embedding the block's repr would
    escape every newline twice and the fenced JSON would never parse, which is a
    bug in the FIXTURE, not in what is under test."""
    block_file = tmp_path / "block.txt"
    block_file.write_text(CODER_TESTS_BLOCK)
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        'echo \'{"type":"system","subtype":"init"}\'\n'
        "cat > calc.py <<'EOF'\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "EOF\n"
        "python3 - <<'PY'\n"
        "import json\n"
        f"block = open({str(block_file)!r}).read()\n"
        'print(json.dumps({"type": "result", "result": "fixed add() so it adds." + block}))\n'
        "PY\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("REWORK_HARNESS_BIN_CLAUDE", str(script))
    monkeypatch.setenv("REWORK_HARNESS", "claude")
    return script


def _ctx(repo: Path, store: Any, workdir: Path, context: dict[str, Any] | None = None):
    mission = Mission(id="m-h", objective="make add(2,3)==5", repo_path=str(repo))
    run = Run(
        id="m-h:code:harness:1",
        mission_id=mission.id,
        role="harness",
        step="code",
        context=context or {"brief": "make add(2,3)==5"},
    )
    catalog = ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1", tier=0)])
    return RoleBuildCtx(
        run=run,
        mission=mission,
        catalog=catalog,
        provider=FakeProvider([]),
        store=store,
        bus=InProcessEventBus(store),
        gates=GateManager(store, InProcessEventBus(store)),
        budget=Budget(),
        workdir=workdir,
        git_author=("r", "r@r"),
    )


async def test_the_harness_role_is_registered() -> None:
    assert "harness" in autodiscover().names()


async def test_harness_argv_uses_accept_edits_plus_bash_and_streaming(
    monkeypatch, tmp_path
) -> None:
    """`dontAsk` denies Edit/Write/Bash (the worker cannot change anything);
    `acceptEdits` alone leaves Bash denied (it cannot run the test). The role
    needs both, granted narrowly via --allowedTools (found live)."""
    monkeypatch.setenv("REWORK_HARNESS_BIN_CLAUDE", "/usr/bin/claude")
    argv = harness_argv("claude", "do the thing", tmp_path)
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in argv
    for tool in ("Edit", "Write", "Bash"):
        assert tool in argv, f"{tool} must be granted or the worker cannot work"
    assert "--output-format" in argv and "stream-json" in argv


async def test_harness_argv_refuses_an_unknown_harness(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REWORK_HARNESS_BIN_NOPE", "/bin/true")
    with pytest.raises(ValueError):
        harness_argv("nope", "x", tmp_path)


async def test_harness_argv_reports_a_missing_binary(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REWORK_HARNESS_BIN_CLAUDE", raising=False)
    monkeypatch.setattr("rework.roles.builtin.harness._binary", lambda name: None)
    with pytest.raises(FileNotFoundError):
        harness_argv("claude", "x", tmp_path)


async def test_the_harness_artifact_matches_the_coder_contract(
    repo: Path, fake_harness: Path, tmp_path: Path
) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        ctx = _ctx(repo, store, tmp_path / "work")
        await store.create_mission(ctx.mission)
        await store.create_run(ctx.run)
        await store.update_run(ctx.run.id, status=RunStatus.RUNNING)
        graph = autodiscover().get("harness").build_graph(ctx)
        final = await graph.ainvoke(
            {"objective": ctx.mission.objective}, config={"configurable": {"thread_id": "t"}}
        )
        artifact = final["artifact"]

        assert artifact["kind"] == "coder_handoff"
        assert artifact["branch"] == "rework/m-h-code-harness-1"
        assert artifact["commit"], "the HOST must have committed the harness's edit"
        assert "+    return a + b" in artifact["diff"]
        assert artifact["tests"]["red_green_verified"] is True
        assert "harness" in final
        # the fix landed on the branch, not on main
        assert "a + b" in (Path(artifact["worktree"]) / "calc.py").read_text()
        assert "a - b" in (repo / "calc.py").read_text()
    finally:
        await store.aclose()


async def test_two_runs_never_share_a_session(
    repo: Path, fake_harness: Path, tmp_path: Path
) -> None:
    """Each run gets its own session dir under its own workdir."""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(Mission(id="m-h", objective="x", repo_path=str(repo)))
        sessions = []
        for n in (1, 2):
            ctx = _ctx(repo, store, tmp_path / f"work{n}")
            run = ctx.run.model_copy(update={"id": f"m-h:code:harness:{n}"})
            ctx = ctx.model_copy(update={"run": run})
            await store.create_run(run)
            await store.update_run(run.id, status=RunStatus.RUNNING)
            graph = autodiscover().get("harness").build_graph(ctx)
            final = await graph.ainvoke(
                {"objective": "x"}, config={"configurable": {"thread_id": f"t{n}"}}
            )
            sessions.append(final["harness"]["session"])
        assert sessions[0] != sessions[1]
        missing = [s for s in sessions if not await asyncio.to_thread(Path(s).is_dir)]
        assert not missing, missing
    finally:
        await store.aclose()


async def test_a_timeout_fails_the_run_with_a_reason(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = tmp_path / "slow-harness"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setenv("REWORK_HARNESS_BIN_CLAUDE", str(slow))
    monkeypatch.setenv("REWORK_HARNESS", "claude")

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        ctx = _ctx(repo, store, tmp_path / "work", {"brief": "x", "max_seconds": 1})
        await store.create_mission(ctx.mission)
        await store.create_run(ctx.run)
        await store.update_run(ctx.run.id, status=RunStatus.RUNNING)
        graph = autodiscover().get("harness").build_graph(ctx)
        with pytest.raises(RuntimeError, match="ran past"):
            await graph.ainvoke({"objective": "x"}, config={"configurable": {"thread_id": "t"}})
    finally:
        await store.aclose()


async def test_streamed_events_become_progress_rows(
    repo: Path, fake_harness: Path, tmp_path: Path
) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        ctx = _ctx(repo, store, tmp_path / "work")
        await store.create_mission(ctx.mission)
        await store.create_run(ctx.run)
        await store.update_run(ctx.run.id, status=RunStatus.RUNNING)
        graph = autodiscover().get("harness").build_graph(ctx)
        await graph.ainvoke({"objective": "x"}, config={"configurable": {"thread_id": "t"}})
        events = await store.events_since(0, mission_id="m-h")
        notes = [e.payload.get("note", "") for e in events]
        assert any("harness:" in n for n in notes), "the stream must reach the trajectory"
    finally:
        await store.aclose()


async def test_red_green_still_verifies_a_harness_handoff(
    repo: Path, fake_harness: Path, tmp_path: Path
) -> None:
    """The rail that protects the coder protects a harness the same way: it
    works on a diff and a test command, not on who wrote the diff."""
    from rework.nodes.testing import handoff_tests_gap

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        ctx = _ctx(repo, store, tmp_path / "work")
        await store.create_mission(ctx.mission)
        await store.create_run(ctx.run)
        await store.update_run(ctx.run.id, status=RunStatus.RUNNING)
        graph = autodiscover().get("harness").build_graph(ctx)
        final = await graph.ainvoke({"objective": "x"}, config={"configurable": {"thread_id": "t"}})
        assert handoff_tests_gap(final["artifact"]) is None, "a complete tests block passes"
    finally:
        await store.aclose()


def test_final_text_is_extracted_from_the_stream_shape() -> None:
    assert _final_text_of({"type": "result", "result": "done"}) == "done"
    assert (
        _final_text_of(
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
        )
        == "hi"
    )
    assert _final_text_of({"type": "system", "subtype": "init"}) == ""
    assert "system" in _progress_note({"type": "system", "subtype": "init"})
