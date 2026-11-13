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


def test_report_transcript_keeps_the_evidence() -> None:
    """The writer must see tool arguments and tool RESULTS (found live: they
    were dropped, so a report called a location it had printed 'lost')."""
    from rework.nodes.report_writer import render_transcript

    messages = [
        {"role": "user", "content": "find the menu border"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "name": "run_command",
                    "arguments": {"command": "grep -n menu app.tcss"},
                }
            ],
        },
        {"role": "tool", "name": "run_command", "content": "53:#menu {\n58:    border: none;"},
    ]
    text = render_transcript(messages)
    assert "grep -n menu app.tcss" in text
    assert "58:    border: none;" in text

    huge = [{"role": "tool", "name": "read_file", "content": "x" * 50_000}] * 10
    shrunk = render_transcript(messages + huge + messages, budget=20_000)
    assert len(shrunk) <= 20_100
    assert "find the menu border" in shrunk, "the start survives"
    assert shrunk.rstrip().endswith("border: none;"), "the end survives"


async def test_run_command_timeout_kills_the_whole_process_tree(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Found live: a timed-out `uv run pytest` left orphans holding the pipe
    and the role hung forever. A grandchild that ignores the shell's death must
    not keep the tool call from returning."""
    import time

    from rework.nodes.tools import LocalExecutor

    ex = LocalExecutor(tmp_path)
    start = time.monotonic()
    out = await ex.execute(
        "run_command",
        {
            "command": "python3 -c "
            + repr("import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"),
            "timeout_seconds": 1,
        },
    )
    assert "timeout after 1s" in out
    assert time.monotonic() - start < 10, "returned instead of waiting on orphans"


def test_a_dead_daemons_command_tree_is_reaped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Found live: a daemon restart left two `uv run pytest` trees running.
    A later daemon kills groups whose owner is gone, only if the group still
    runs that exact command, and leaves a live owner's commands alone."""
    import json
    import os
    import subprocess
    import time

    from rework.nodes.tools import _command_registry, reap_orphaned_commands

    command = "sleep 61; true"
    orphan = subprocess.Popen(["/bin/sh", "-c", command], start_new_session=True)
    dead_owner = subprocess.Popen(["true"])
    dead_owner.wait()
    reg = _command_registry()
    (reg / f"{orphan.pid}.json").write_text(
        json.dumps({"pgid": orphan.pid, "owner": dead_owner.pid, "command": command})
    )
    # a stale mark whose pgid now runs something else: never killed
    bystander = subprocess.Popen(["/bin/sh", "-c", "sleep 62; true"], start_new_session=True)
    (reg / f"{bystander.pid}.json").write_text(
        json.dumps({"pgid": bystander.pid, "owner": dead_owner.pid, "command": "pytest -x"})
    )
    # a live owner's command: left alone
    live = subprocess.Popen(["/bin/sh", "-c", "sleep 63; true"], start_new_session=True)
    (reg / f"{live.pid}.json").write_text(
        json.dumps({"pgid": live.pid, "owner": os.getppid(), "command": "sleep 63; true"})
    )
    try:
        killed = reap_orphaned_commands(force=True)
        assert killed == [orphan.pid]
        deadline = time.monotonic() + 5
        while orphan.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert orphan.poll() is not None
        assert bystander.poll() is None and live.poll() is None
        assert not (reg / f"{orphan.pid}.json").exists()
        assert (reg / f"{live.pid}.json").exists()
    finally:
        for p in (orphan, bystander, live):
            if p.poll() is None:
                p.kill()


async def test_run_command_clears_its_registry_mark(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from rework.nodes.tools import LocalExecutor, _command_registry

    out = await LocalExecutor(tmp_path).execute("run_command", {"command": "echo hi"})
    assert "hi" in out
    assert list(_command_registry().glob("*.json")) == []


async def test_role_commands_do_not_inherit_the_daemons_venv(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Found live: role commands inherited the daemon's VIRTUAL_ENV and venv
    bin on PATH, so `pytest` in a worktree imported the main checkout's code."""
    import os
    import sys

    from rework.nodes.tools import LocalExecutor

    fake = tmp_path / "daemon-venv"
    (fake / "bin").mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(fake))
    monkeypatch.setenv("PYTHONPATH", "/somewhere/src")
    monkeypatch.setenv("PATH", os.pathsep.join([str(fake / "bin"), "/usr/bin", "/bin"]))
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    out = await LocalExecutor(tmp_path).execute(
        "run_command",
        {"command": 'echo "venv=[$VIRTUAL_ENV] pp=[$PYTHONPATH]"; echo "path=$PATH"'},
    )
    assert "venv=[] pp=[]" in out
    assert str(fake) not in out
    assert "/usr/bin" in out
    if sys.prefix != sys.base_prefix:  # the test runner's own venv is stripped too
        assert os.path.join(sys.prefix, "bin") not in out.split("path=", 1)[1]


async def test_a_plain_repo_keeps_the_daemons_tools(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No pyproject: nothing of its own to import, so the daemon's python (with
    pytest) stays available, as toy repos expect."""
    from rework.nodes.tools import LocalExecutor

    monkeypatch.setenv("VIRTUAL_ENV", "/daemon/venv")
    out = await LocalExecutor(tmp_path).execute("run_command", {"command": 'echo "[$VIRTUAL_ENV]"'})
    assert "[/daemon/venv]" in out


@pytest.mark.parametrize("edited", [False, True])
async def test_a_coder_with_nothing_edited_at_halfway_is_nudged(
    tmp_path: Path, workdir: Path, edited: bool
) -> None:
    """Found live: flash coders spent whole turn budgets exploring and handed
    off nothing. At half the budget, no changes yet -> one nudge to edit."""
    store = await _store(tmp_path)
    reads = [
        ChatResult(
            tool_calls=[ToolCall(id=f"r{i}", name="read_file", arguments={"path": "src/app.py"})],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        )
        for i in range(6)
    ]
    fake = FakeProvider([*reads, ChatResult(text="done", usage=Usage(), stop_reason="end_turn")])
    checks: list[int] = []

    async def progress(state: dict[str, object]) -> bool:
        checks.append(1)
        return edited

    node = make_toolloop_node(
        fake,
        _catalog(),
        "fake",
        LocalExecutor(workdir),
        "s",
        store,
        "m3",
        "m3:code:coder:1",
        Budget(),
        max_iterations=10,
        progress_check=progress,
    )
    await node({"messages": [{"role": "user", "content": "fix it"}]})
    nudged = [
        r
        for r in fake.requests
        if any("Progress check" in str(m.get("content")) for m in r.messages)
    ]
    assert checks == [1], "checked exactly once, at the halfway turn"
    assert bool(nudged) is (not edited)
    if nudged:
        assert "5 of 10 tool rounds" in str(nudged[0].messages[-1]["content"])


async def test_has_changes_sees_edits_and_new_commits(tmp_path: Path) -> None:
    import subprocess

    from rework.nodes.git_ops import has_changes

    def git(*a: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    assert not await has_changes(tmp_path, base)
    (tmp_path / "a.py").write_text("x = 2\n")
    assert await has_changes(tmp_path, base)
    git("commit", "-qam", "edit")
    assert await has_changes(tmp_path, base)  # committed: clean tree, new HEAD


async def test_a_reply_cut_at_the_output_limit_is_continued(tmp_path: Path, workdir: Path) -> None:
    """Found live: 8192-token reports stopped at the limit and were accepted
    as final. A max_tokens stop asks for the rest; the final text is stitched."""
    store = await _store(tmp_path)
    fake = FakeProvider(
        [
            ChatResult(text="## Report\npart one, ", usage=Usage(), stop_reason="max_tokens"),
            ChatResult(text="part two.", usage=Usage(), stop_reason="end_turn"),
        ]
    )
    node = make_toolloop_node(
        fake, _catalog(), "fake", LocalExecutor(workdir), "s", store, "m4", "m4:look:x:1", Budget()
    )
    result = await node({"messages": [{"role": "user", "content": "report"}]})
    assert result["final_text"] == "## Report\npart one, part two."
    assert len(fake.requests) == 2
    assert "cut off" in str(fake.requests[1].messages[-1]["content"])


def test_openai_compat_reports_truncation_and_scales_its_timeout() -> None:
    from rework.kernel.llm.adapters.logged import hard_timeout_s
    from rework.kernel.llm.adapters.openai_compat import client_timeout_s, translate_response

    cut = translate_response(
        {"choices": [{"message": {"content": "half"}, "finish_reason": "length"}], "usage": {}}
    )
    done = translate_response(
        {"choices": [{"message": {"content": "all"}, "finish_reason": "stop"}], "usage": {}}
    )
    assert cut.stop_reason == "max_tokens" and done.stop_reason == "end_turn"
    assert client_timeout_s(None) == 180.0
    assert client_timeout_s(4096) == 180.0
    assert 290 < client_timeout_s(8192) < 300  # kimi-k3's 8192 at 35 tok/s
    assert hard_timeout_s(8192) > client_timeout_s(8192)
    assert hard_timeout_s(16384) > client_timeout_s(16384)


async def test_a_streamed_completion_is_assembled() -> None:
    """Streaming with an idle timeout replaced the flat one; the chunks must
    rebuild exactly what the non-streamed response carried."""
    from types import SimpleNamespace as NS

    from rework.kernel.llm.adapters.openai_compat import assemble_stream, translate_response

    def chunk(content=None, calls=None, finish=None, usage=None):  # type: ignore[no-untyped-def]
        choices = (
            [] if usage else [NS(delta=NS(content=content, tool_calls=calls), finish_reason=finish)]
        )
        return NS(choices=choices, usage=usage)

    def call(index, id=None, name=None, args=None):  # type: ignore[no-untyped-def]
        return NS(index=index, id=id, function=NS(name=name, arguments=args))

    class Usage:
        def model_dump(self) -> dict[str, object]:
            return {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 90},
            }

    async def stream():  # type: ignore[no-untyped-def]
        for c in [
            chunk(content="Let me "),
            chunk(content="look."),
            chunk(calls=[call(0, id="c1", name="read_file", args='{"pa')]),
            chunk(calls=[call(0, args='th": "a.py"}')]),
            chunk(calls=[call(1, id="c2", name="list_dir", args="{}")], finish="tool_calls"),
            chunk(usage=Usage()),
        ]:
            yield c

    result = translate_response(await assemble_stream(stream()))
    assert result.text == "Let me look."
    assert [(t.id, t.name, t.arguments) for t in result.tool_calls] == [
        ("c1", "read_file", {"path": "a.py"}),
        ("c2", "list_dir", {}),
    ]
    assert result.stop_reason == "tool_calls"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cached_tokens) == (
        100,
        7,
        90,
    )


# -- role_env scratch dir isolation -------------------------------------------


def test_role_env_redirects_rework_state_dirs_to_scratch(tmp_path: Path) -> None:
    """FIX 2 (PR #21): role_env must not let role commands touch the real
    ~/.rework.  REWORK_TARGETS (and other state paths) must point inside the
    per-run scratch dir under the workdir, so a rework command run by a role
    never reads/writes the dev machine's targets.toml or env file."""
    import os

    from rework.nodes.tools import role_env

    # Monkeypatch to ensure REWORK_TARGETS/etc are not already set
    env_backup = {k: os.environ.pop(k, None) for k in
                  ("REWORK_TARGETS", "REWORK_ENV_FILE", "REWORK_TUI_SETTINGS", "REWORK_LOG_DIR")}
    try:
        result = role_env(tmp_path)
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    scratch = tmp_path / ".rework-scratch"

    # REWORK_TARGETS must NOT point at the real ~/.rework
    targets = result.get("REWORK_TARGETS", "")
    home_rework = str(Path.home() / ".rework")
    assert not targets.startswith(home_rework), (
        f"REWORK_TARGETS {targets!r} points inside the real ~/.rework"
    )
    # Must point inside the per-run scratch dir under workdir
    assert targets.startswith(str(scratch)), (
        f"REWORK_TARGETS {targets!r} should be under {scratch}"
    )

    # Same for REWORK_ENV_FILE
    env_file = result.get("REWORK_ENV_FILE", "")
    assert env_file.startswith(str(scratch)), (
        f"REWORK_ENV_FILE {env_file!r} should be under {scratch}"
    )

    # Same for REWORK_TUI_SETTINGS
    tui = result.get("REWORK_TUI_SETTINGS", "")
    assert tui.startswith(str(scratch)), (
        f"REWORK_TUI_SETTINGS {tui!r} should be under {scratch}"
    )

    # Same for REWORK_LOG_DIR
    log_dir = result.get("REWORK_LOG_DIR", "")
    assert log_dir.startswith(str(scratch)), (
        f"REWORK_LOG_DIR {log_dir!r} should be under {scratch}"
    )

    # Scratch dir was created
    assert scratch.is_dir(), "scratch dir should be created by role_env"


def test_role_env_no_workdir_does_not_set_scratch_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """When workdir is None (no jail), no scratch overrides are injected."""
    from rework.nodes.tools import role_env

    monkeypatch.delenv("REWORK_TARGETS", raising=False)
    result = role_env(None)
    # Without workdir, we do not inject any scratch paths
    assert "REWORK_TARGETS" not in result
