"""Node library tests: path-jailing, executor tools, repo map, report sections,
and the full toolloop -> report-writer chain with the FakeProvider."""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.nodes.repo_map import build_repo_map
from rework.nodes.report_sections import (
    ASSEMBLY_AUDIT,
    build_report_prompt,
    build_report_schema,
    resolve_order,
    resolve_sections,
)
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import LocalExecutor


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# demo\n")
    return tmp_path


# -- path jail -------------------------------------------------------------------


async def test_read_file_inside_jail(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    out = await ex.execute("read_file", {"path": "src/app.py"})
    assert "print('hi')" in out


async def test_read_file_escape_rejected(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    out = await ex.execute("read_file", {"path": "../outside.txt"})
    assert out.startswith("error:")


async def test_read_file_absolute_escape_rejected(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    out = await ex.execute("read_file", {"path": "/etc/hostname"})
    assert out.startswith("error:")


async def test_run_command_jailed_cwd(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    expected = str(workdir.resolve())  # noqa: ASYNC240  (test: sync resolve of a fixture path)
    out = await ex.execute("run_command", {"command": "pwd"})
    assert expected in out


async def test_absolute_cd_outside_worktree_is_refused(workdir: Path, tmp_path: Path) -> None:
    """Live bug (m-c8b72e7734b7): the model invented an absolute worktree path
    that did not exist; /bin/sh exited 2 and every chained command silently
    skipped. Refuse with the real location instead of an opaque failure."""
    ex = LocalExecutor(workdir)
    outside = tmp_path / "code_coder_2" / "worktree"
    out = await ex.execute("run_command", {"command": f"cd {outside} && git status"})
    assert out.startswith("error: refusing `cd")
    expected = str(workdir.resolve())  # noqa: ASYNC240  (test: sync resolve of a fixture path)
    assert expected in out


async def test_absolute_cd_to_the_worktree_is_allowed(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    target = str(workdir.resolve())  # noqa: ASYNC240  (test: sync resolve of a fixture path)
    out = await ex.execute("run_command", {"command": f"cd {target} && pwd"})
    assert out.startswith("(exit 0)")


async def test_relative_cd_is_left_alone(workdir: Path) -> None:
    (workdir / "sub").mkdir()
    ex = LocalExecutor(workdir)
    out = await ex.execute("run_command", {"command": "cd sub && pwd"})
    assert out.startswith("(exit 0)")
    assert "/sub" in out


async def test_specs_filtered_by_allowed(workdir: Path) -> None:
    ex = LocalExecutor(workdir)
    assert {s.name for s in ex.specs()} == {"read_file", "list_dir", "run_command"}
    assert [s.name for s in ex.specs({"read_file"})] == ["read_file"]


# -- repo map ----------------------------------------------------------------------


def test_repo_map_lists_files_and_languages(workdir: Path) -> None:
    (workdir / ".git").mkdir()
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "junk.js").write_text("junk")
    text = build_repo_map(workdir)
    assert "src/app.py" in text
    assert "README.md" in text
    assert "python" in text
    assert "node_modules" not in text  # junk-filtered
    assert ".git" not in text


# -- report sections ------------------------------------------------------------------


def test_resolve_sections_unknown_key_loud() -> None:
    with pytest.raises(KeyError, match="unknown report sections"):
        resolve_sections(["findings", "nope"])


def test_resolve_sections_none_is_investigator_default() -> None:
    keys = [s.key for s in resolve_sections(None)]
    assert keys == ["summary", "findings", "blockers"]


def test_audit_assembly_has_standing_assessments() -> None:
    keys = [s.key for s in resolve_sections(list(ASSEMBLY_AUDIT))]
    assert "standing_assessments" in keys


def test_schema_and_prompt_build() -> None:
    sections = resolve_sections(["summary", "findings"])
    schema = build_report_schema(sections)
    assert schema["required"] == ["summary", "findings"]
    assert "findings" in schema["properties"]
    assert "sections_order" in schema["properties"]  # optional, model-chosen
    prompt = build_report_prompt(sections)
    assert "Findings" in prompt and "Summary" in prompt
    assert "sections_order" in prompt  # the ordering instruction is in the ask


def test_resolve_order_mechanical_sanitation() -> None:
    """Model-chosen order is honored only for produced sections; junk, dupes,
    and unproduced keys are dropped, missing-but-produced appended in assembly
    order (found as the clutter complaint: the renderer needs a fixed rule)."""
    sections = resolve_sections(["summary", "findings", "blockers"])
    artifact = {"summary": "s", "findings": [], "blockers": [{"question": "q"}]}
    assert resolve_order(
        {**artifact, "sections_order": ["findings", "summary", "blockers"]}, sections
    ) == ["findings", "summary", "blockers"]
    # unknown + duplicate keys dropped, missing-but-produced appended
    assert resolve_order(
        {**artifact, "sections_order": ["nope", "findings", "findings"]}, sections
    ) == ["findings", "summary", "blockers"]
    # absent order: assembly order over produced sections
    assert resolve_order(artifact, sections) == ["summary", "findings", "blockers"]
    # an empty findings list still counts as produced (explicitly clean)
    assert resolve_order({**artifact, "sections_order": ["summary"]}, sections) == [
        "summary",
        "findings",
        "blockers",
    ]


# -- toolloop + report-writer chain --------------------------------------------------


def _catalog() -> ModelCatalog:
    return ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1", tier=0)])


async def test_toolloop_runs_tools_until_done(tmp_path: Path, workdir: Path) -> None:
    store = await _store(tmp_path)
    fake = FakeProvider(
        [
            ChatResult(
                text="looking",
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "src/app.py"})],
                usage=Usage(input_tokens=10, output_tokens=5),
                stop_reason="tool_calls",
            ),
            ChatResult(
                text="the file prints hi",
                usage=Usage(input_tokens=8, output_tokens=4),
                stop_reason="end_turn",
            ),
        ]
    )
    ex = LocalExecutor(workdir)
    node = make_toolloop_node(
        fake, _catalog(), "fake", ex, "be brief", store, "m1", "m1:step:echo:1", Budget()
    )
    result = await node({"messages": [{"role": "user", "content": "what does app.py do"}]})
    assert result["final_text"] == "the file prints hi"
    assert result["loop_turns"] == 2
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    spend = await store.mission_spend("m1")
    assert spend.output_tokens > 0  # the ledger was charged


async def test_toolloop_budget_guard_stops(tmp_path: Path, workdir: Path) -> None:
    store = await _store(tmp_path)
    # pre-charge the mission past its cap: the first guard check must stop the loop
    from rework.kernel.state.records import Charge

    await store.charge(Charge(mission_id="m2", model="x", cost_usd=10.0))
    fake = FakeProvider([ChatResult(text="never reached", stop_reason="end_turn")])
    node = make_toolloop_node(
        fake,
        _catalog(),
        "fake",
        LocalExecutor(workdir),
        "s",
        store,
        "m2",
        "m2:step:echo:1",
        Budget(max_usd=1.0),
    )
    result = await node({"messages": []})
    assert result["final_text"].startswith("stopped: budget exceeded")
    assert fake.requests == []  # the guarded call never reached the provider


async def _store(tmp_path: Path):
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(tmp_path / "nodes.sqlite3"))
