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
    prompt = build_report_prompt(sections)
    assert "Findings" in prompt and "Summary" in prompt


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
