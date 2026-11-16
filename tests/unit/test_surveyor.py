"""Surveyor role: genuinely read-only (enforced by the executor wrapper), a
typed cited artifact, a plan that can never publish, and a report that renders
the whole map."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from rework.cli import _render_md
from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus, run_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import (
    PLAN_AUDIT,
    PLANS,
    PLAYBOOK_GATES,
    PLAYBOOK_PUBLISH,
    orchestrated_plan,
)
from rework.nodes.readonly import (
    READ_ONLY_TOOL_NAMES,
    ReadOnlyExecutor,
    ToolRefused,
    build_git_argv,
    build_search_argv,
    build_tree_argv,
)
from rework.nodes.report_sections import ASSEMBLY_SURVEYOR, build_report_schema, resolve_sections
from rework.nodes.tools import ComplaintAwareExecutor, LocalExecutor
from rework.orchestrator.agent import DEFAULT_TURNS, PLAYBOOKS
from rework.roles.base import RoleBuildCtx
from rework.roles.builtin.surveyor import MAX_RISKS_AND_GAPS, finalize_survey
from rework.roles.registry import autodiscover

WRITE_TOOLS = {"write_file", "edit_file", "apply_patch", "git_commit", "open_pr", "git_push"}

ARTIFACT: dict[str, Any] = {
    "title": "tiny app",
    "summary": "A one-file greeter.",
    "layout": [{"path": "app.py", "purpose": "the greeter"}],
    "entry_points": [{"name": "greet", "where": "app.py:1-2", "purpose": "say hello"}],
    "dependencies": [],
    "config": [],
    "tests": {"framework": "none", "command": "", "where": "", "notes": "no tests"},
    "ci": {"system": "none", "where": "", "checks": []},
    "risks": [
        {
            "severity": "high",
            "area": "input handling",
            "detail": "name.upper() assumes a str",
            "where": "app.py:2",
        }
    ],
    "gaps": ["no tests were found"],
    "next_steps": ["add a test for greet"],
    "report": "# tiny app\n\nOne-file greeter; risk at app.py:2.",
}


@pytest.fixture
async def sv_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    from conftest import make_git_repo, role_catalog

    repo = make_git_repo(tmp_path)
    (repo / "app.py").write_text("def greet(name):\n    return f'hello {name.upper()}'\n")
    store: SqliteStore = await open_sqlite(str(tmp_path / "sv.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(
            Mission(id="mi", objective="map this repo", repo_path=str(repo))
        )
        run = await store.create_run(
            Run(
                id=run_id("mi", "survey", "surveyor", 1),
                mission_id="mi",
                role="surveyor",
                step="survey",
                context={"brief": "map this repo"},
            )
        )
        yield RoleBuildCtx(
            run=run,
            mission=mission,
            catalog=role_catalog(),
            provider=FakeProvider(),
            store=store,
            bus=bus,
            gates=GateManager(store, bus),
            budget=Budget(max_usd=5.0),
            workdir=tmp_path,
        )
    finally:
        await store.aclose()


def _push(fake: FakeProvider, result: ChatResult) -> None:
    fake.push(result)


async def test_surveyor_scripted_run_yields_cited_artifact(sv_ctx: RoleBuildCtx) -> None:
    fake = sv_ctx.provider
    assert isinstance(fake, FakeProvider)
    _push(
        fake,
        ChatResult(
            text="reading",
            tool_calls=[
                ToolCall(id="t1", name="read_file", arguments={"path": "app.py"}),
                ToolCall(id="t2", name="write_file", arguments={"path": "x", "content": "y"}),
                ToolCall(id="t3", name="run_command", arguments={"command": "cat /etc/hostname"}),
                ToolCall(id="t4", name="search_code", arguments={"pattern": "upper"}),
                ToolCall(id="t5", name="read_file", arguments={"path": "/etc/passwd"}),
            ],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        ),
    )
    _push(fake, ChatResult(text="mapped", usage=Usage(input_tokens=5, output_tokens=5)))
    _push(fake, ChatResult(text=json.dumps(ARTIFACT), usage=Usage(input_tokens=9, output_tokens=9)))

    graph = autodiscover().get("surveyor").build_graph(sv_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-sv"}}
    final = await graph.ainvoke({"objective": "map this repo"}, config=config)

    artifact = final["artifact"]
    assert artifact is not None, final.get("artifact_error")
    assert {
        "title", "summary", "layout", "entry_points", "dependencies", "config",
        "tests", "ci", "risks", "gaps", "next_steps", "report",
    } <= set(artifact)  # fmt: skip
    assert artifact["risks"][0]["where"] == "app.py:2"
    assert artifact["report"].strip()
    tool_text = " ".join(m["content"] for m in final["messages"] if m.get("role") == "tool")
    assert "name.upper()" in tool_text  # the read happened
    assert "app.py:2:" in tool_text  # search_code ran through the real stack
    assert "read-only" in tool_text  # the write_file call was refused, not run
    assert "tool 'run_command' is not available" in tool_text
    assert "root:" not in tool_text  # /etc/passwd never came back
    assert not (Path(sv_ctx.mission.repo_path or "") / "x").exists()


def test_surveyor_tool_menu_is_read_only(sv_ctx: RoleBuildCtx) -> None:
    spec = autodiscover().get("surveyor")
    assert spec.name == "surveyor"
    # the executor the role mounts: outermost wrapper over the complaint wrapper
    # that would otherwise re-add file_complaint
    inner = ComplaintAwareExecutor(LocalExecutor(Path(".")), sv_ctx.store, "mi", "r", "surveyor")
    assert "file_complaint" in {s.name for s in inner.specs()}
    assert "run_command" in {s.name for s in inner.specs()}
    ro = ReadOnlyExecutor(inner, Path("."))
    names = {s.name for s in ro.specs()}
    assert names <= READ_ONLY_TOOL_NAMES
    assert names == {"read_file", "list_dir", "search_code", "repo_tree", "git_read"}
    assert "run_command" not in READ_ONLY_TOOL_NAMES
    assert not names & WRITE_TOOLS
    # asking for run_command explicitly does not bring it back
    assert "run_command" not in {s.name for s in ro.specs({"run_command", "read_file"})}


async def test_surveyor_system_prompt_refuses_change(sv_ctx: RoleBuildCtx) -> None:
    fake = sv_ctx.provider
    assert isinstance(fake, FakeProvider)
    _push(fake, ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1)))
    _push(fake, ChatResult(text=json.dumps(ARTIFACT), usage=Usage(input_tokens=1, output_tokens=1)))
    graph = autodiscover().get("surveyor").build_graph(sv_ctx)
    graph.checkpointer = InMemorySaver()
    await graph.ainvoke({"objective": "map"}, config={"configurable": {"thread_id": "t-p"}})
    system = fake.requests[0].system
    assert "READ-ONLY and must never change anything" in system
    assert "do not commit, push or open a pull request" in system
    assert "NO shell" in system and "run_command" not in system
    assert "—" not in system  # no em dashes
    assert {t.name for t in fake.requests[0].tools} == READ_ONLY_TOOL_NAMES


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return "ok"

    def specs(self, allowed: set[str] | None = None) -> list[Any]:
        return []


def _names(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


def _ro(tmp_path: Path) -> tuple[ReadOnlyExecutor, _Recorder]:
    inner = _Recorder()
    return ReadOnlyExecutor(inner, tmp_path), inner


@pytest.mark.parametrize(
    "name,args",
    [
        ("run_command", {"command": "ls"}),
        ("run_command", {"command": "cat /etc/passwd"}),
        ("write_file", {"path": "a", "content": "b"}),
        ("edit_file", {"path": "a"}),
        ("open_pr", {}),
        ("file_complaint", {"summary": "x"}),
        ("ask_orchestrator", {"question": "x"}),
    ],
)
async def test_wrapper_refuses_every_tool_outside_the_menu(
    tmp_path: Path, name: str, args: dict[str, Any]
) -> None:
    ex, inner = _ro(tmp_path)
    out = await ex.execute(name, args)
    assert out.startswith("error:") and "read-only" in out and "no shell" in out
    assert "search_code" in out and "git_read" in out, "the refusal names the tools that exist"
    assert inner.calls == []


BAD_PATHS = [
    "/etc/passwd",
    "/root",
    "../../etc/hostname",
    "..",
    "a/../../b",
    "a/../b",
    "~",
    "~/.bashrc",
    "~root/x",
    "\\windows",
    "C:\\x",
    "a\nb",
    "a\x00b",
]


@pytest.mark.parametrize("bad", BAD_PATHS)
@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {}),
        ("list_dir", {}),
        ("search_code", {"pattern": "x"}),
        ("repo_tree", {}),
        ("git_read", {"subcommand": "log"}),
        ("git_read", {"subcommand": "blame"}),
        ("git_read", {"subcommand": "ls-files"}),
    ],
)
async def test_every_path_argument_goes_through_the_jail(
    tmp_path: Path, tool: str, args: dict[str, Any], bad: str
) -> None:
    ex, inner = _ro(tmp_path)
    out = await ex.execute(tool, {**args, "path": bad})
    assert out.startswith("error: refused"), out
    assert inner.calls == [], "nothing reached the inner executor"


async def test_a_symlink_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link").symlink_to(outside)
    ex, inner = _ro(ws)
    for tool, args in (
        ("read_file", {"path": "link/secret.txt"}),
        ("search_code", {"pattern": "secret", "path": "link"}),
        ("repo_tree", {"path": "link"}),
    ):
        assert (await ex.execute(tool, args)).startswith("error: refused")
    assert inner.calls == []


@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_code", {"pattern": "a\nb"}),
        ("search_code", {"pattern": "a\x00b"}),
        ("search_code", {"pattern": ""}),
        ("search_code", {"pattern": "x", "glob": "a\nb"}),
        ("search_code", {"pattern": "x", "max_results": "many"}),
        ("repo_tree", {"depth": "deep"}),
        ("git_read", {"subcommand": "commit"}),
        ("git_read", {"subcommand": "push"}),
        ("git_read", {"subcommand": "config"}),
        ("git_read", {"subcommand": "log --output=x"}),
        ("git_read", {"subcommand": "log", "rev": "--output=out.txt"}),
        ("git_read", {"subcommand": "log", "rev": "-p"}),
        ("git_read", {"subcommand": "diff", "rev": "--no-index"}),
        ("git_read", {"subcommand": "log", "rev": "HEAD --output=x"}),
        ("git_read", {"subcommand": "log", "rev": "a;b"}),
        ("git_read", {"subcommand": "log", "rev": "$(id)"}),
        ("git_read", {"subcommand": "blame"}),  # needs a path
        ("git_read", {}),
    ],
)
async def test_bad_arguments_are_refused_before_any_argv(
    tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    ex, inner = _ro(tmp_path)
    assert (await ex.execute(tool, args)).startswith("error: refused")
    assert inner.calls == []


def test_user_values_only_land_in_positions_the_tool_chose(tmp_path: Path) -> None:
    """The argv is built from a fixed template: a pattern that looks like a flag
    sits after -e, a path that looks like a flag sits after --, and the
    result is handed over shlex-quoted so no shell syntax can survive."""
    (tmp_path / "-rf").write_text("x\n")
    # a pattern that is an option and a shell fragment stays one argv element
    hostile = "--pre=sh; touch pwned"
    rg = build_search_argv(tmp_path, {"pattern": hostile, "path": "-rf"}, have_rg=True)
    assert rg[rg.index("-e") + 1] == hostile
    assert rg[-2:] == ["--", "./-rf"]
    assert rg[0] == "rg" and "--no-config" in rg
    grep = build_search_argv(
        tmp_path, {"pattern": hostile, "glob": "*.py", "fixed": True}, have_rg=False
    )
    assert grep[:2] == ["grep", "-rnI"] and "-F" in grep
    assert grep[grep.index("-e") + 1] == hostile and grep[-2:] == ["--", "."]
    assert "--include=*.py" in grep
    assert build_tree_argv(tmp_path, {"path": "-rf", "depth": 99})[:4] == [
        "find", "./-rf", "-maxdepth", "8",
    ]  # fmt: skip
    git = build_git_argv(tmp_path, {"subcommand": "log", "path": "-rf", "rev": "main"})
    assert git[-3:] == ["main", "--", "-rf"] and git[0] == "git"
    assert "--literal-pathspecs" in git and "--no-pager" in git
    show = build_git_argv(tmp_path, {"subcommand": "show", "rev": "v1.0"})
    assert "--no-ext-diff" in show and "--no-textconv" in show and "v1.0" in show
    assert shlex.split(shlex.join(rg)) == rg


@pytest.mark.parametrize(
    "command",
    [
        "sort -o/tmp/svprobe/out data.txt",
        "cat data.txt | uniq - uniq_out.txt",
        "cat /etc/passwd",
        "cat ../../etc/hostname",
        "head -3 ~/.bashrc",
        "grep -rn AWS_SECRET /home/ubuntu/.aws",
        "ls /root",
        "echo x > f",
    ],
)
async def test_every_audited_shell_bypass_is_gone(tmp_path: Path, command: str) -> None:
    """The auditor's reproduced bypasses, run against the real executor stack:
    the shell is not on the menu at all, so none of them can run or write."""
    (tmp_path / "data.txt").write_text("b\na\n")
    ex = ReadOnlyExecutor(LocalExecutor(tmp_path), tmp_path)
    out = await ex.execute("run_command", {"command": command})
    assert out.startswith("error: tool 'run_command' is not available")
    assert _names(tmp_path) == ["data.txt"], "nothing was written"
    assert "root:" not in out


async def test_the_audited_write_flag_shapes_are_inert_as_search_values(tmp_path: Path) -> None:
    """`-o<file>` and `- out.txt` are only ever TEXT here: as a pattern or a glob
    they cannot become a flag or an output file."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("b\na\n")
    target = tmp_path / "sort_out"
    ex = ReadOnlyExecutor(LocalExecutor(ws), ws)
    for args in (
        {"pattern": f"-o{target}", "fixed": True},
        {"pattern": "- uniq_out.txt", "fixed": True},
    ):
        out = await ex.execute("search_code", args)
        assert not out.startswith("error: refused"), out
    out = await ex.execute("search_code", {"pattern": "a", "glob": f"-o{target}"})
    assert out.startswith("error: refused"), out  # a leading '-' glob is refused
    for rev in (f"-o{target}", "- out.txt"):
        out = await ex.execute("git_read", {"subcommand": "log", "rev": rev})
        assert out.startswith("error: refused")
    assert _names(ws) == ["data.txt"]
    assert not target.exists()


def _repo(tmp_path: Path) -> Path:
    from conftest import make_git_repo

    repo = make_git_repo(tmp_path)
    (repo / "app.py").write_text("def greet(name):\n    return f'hello {name.upper()}'\n")
    sub = {"cwd": repo, "check": True, "capture_output": True}
    subprocess.run(["git", "add", "app.py"], **sub)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add greeter"],
        **sub,
    )
    return repo


async def test_real_tools_run_and_return_cited_lines(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)
    found = await ex.execute("search_code", {"pattern": "upper", "glob": "*.py"})
    assert "app.py:2:" in found and "name.upper()" in found
    assert "app.py:2:" in await ex.execute("search_code", {"pattern": "name.upper", "fixed": True})
    tree = await ex.execute("repo_tree", {})
    assert "app.py" in tree and ".git" not in tree
    log = await ex.execute("git_read", {"subcommand": "log", "max_lines": 5})
    assert "add greeter" in log
    assert "app.py" in await ex.execute("git_read", {"subcommand": "ls-files"})
    assert "upper" in await ex.execute("git_read", {"subcommand": "show", "path": "app.py"})
    blame = await ex.execute("git_read", {"subcommand": "blame", "path": "app.py"})
    assert "hello" in blame and "(exit 0)" in blame
    rev = await ex.execute("git_read", {"subcommand": "rev-parse"})
    assert re.search(r"\b[0-9a-f]{40}\b", rev)
    assert "(exit 0)" in await ex.execute("git_read", {"subcommand": "status"})
    assert "(exit 0)" in await ex.execute("git_read", {"subcommand": "describe"})
    assert "(exit 0)" in await ex.execute("git_read", {"subcommand": "shortlog"})
    assert "app.py" in await ex.execute("git_read", {"subcommand": "ls-tree"})
    assert "name.upper" in await ex.execute("read_file", {"path": "app.py"})
    assert "app.py" in await ex.execute("list_dir", {})


@pytest.mark.parametrize(
    "args",
    [
        {"start_line": -5, "max_lines": 0},
        {"start_line": 1, "max_lines": 0},
        {"start_line": 1, "max_lines": -3},
        {"start_line": 0},
        {"start_line": -5},
    ],
)
async def test_blame_refuses_a_non_positive_range_instead_of_clamping_it(
    tmp_path: Path, args: dict[str, int]
) -> None:
    """`start_line=-5, max_lines=0` used to clamp to `-L 1,0`, git printed
    nothing and the tool reported a misleading cut note. Now it is refused."""
    repo = _repo(tmp_path)
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)
    out = await ex.execute("git_read", {"subcommand": "blame", "path": "app.py", **args})
    assert out.startswith("error: refused") and "at least 1" in out
    assert "cut" not in out
    with pytest.raises(ToolRefused):
        build_git_argv(repo, {"subcommand": "blame", "path": "app.py", **args})


def test_blame_still_clamps_an_over_large_max_lines(tmp_path: Path) -> None:
    from rework.nodes.readonly import MAX_LINES_CAP

    argv = build_git_argv(
        tmp_path,
        {"subcommand": "blame", "path": "a.py", "start_line": 3, "max_lines": 10**9},
    )
    assert argv[argv.index("-L") + 1] == f"3,{3 + MAX_LINES_CAP - 1}"


_RG_EXTRA_DIRS = ("/home/ubuntu/.pi/agent/bin",)


def _rg_path() -> str | None:
    """A PATH that finds ripgrep: the ambient one, else a known extra dir."""
    ambient = os.environ.get("PATH", "")
    for candidate in (ambient, os.pathsep.join([ambient, *_RG_EXTRA_DIRS])):
        if shutil.which("rg", path=candidate):
            return candidate
    return None


def test_search_argv_covers_dotfiles_and_ignored_paths_in_both_branches(tmp_path: Path) -> None:
    rg = build_search_argv(tmp_path, {"pattern": "x"}, have_rg=True)
    assert "--hidden" in rg and "--no-ignore" in rg and rg[-1] == "."
    assert rg[rg.index("--glob=!.git")] == "--glob=!.git"
    grep = build_search_argv(tmp_path, {"pattern": "x"}, have_rg=False)
    assert "--exclude-dir=.git" in grep
    # a user glob cannot re-include .git: ours comes last
    both = build_search_argv(tmp_path, {"pattern": "x", "glob": "*"}, have_rg=True)
    assert both.index("--glob=*") < both.index("--glob=!.git")


@pytest.mark.skipif(_rg_path() is None, reason="ripgrep is not installed")
async def test_rg_and_grep_branches_return_the_same_file_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same call, same map, whichever searcher the host has: a dotfile and a
    gitignored dir are found by both, and .git is skipped by both."""
    repo = _repo(tmp_path)
    (repo / ".env").write_text("needle in a dotfile\n")
    (repo / ".hidden").mkdir()
    (repo / ".hidden" / "x.txt").write_text("needle hidden dir\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.txt").write_text("needle ignored dir\n")
    (repo / ".gitignore").write_text("build/\n.env\n")
    (repo / "plain.txt").write_text("needle plain\n")
    (repo / "run.log").write_text("needle in a log\n")
    (repo / ".git" / "NEEDLE_IN_GIT").write_text("needle inside .git\n")
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)

    def files(out: str) -> set[str]:
        return {
            line.split(":", 1)[0].removeprefix("./")
            for line in out.splitlines()
            if re.match(r"\.?/?[\w.-]+(/[\w.-]+)*:\d+:", line)
        }

    async def both(args: dict[str, Any]) -> tuple[str, str]:
        monkeypatch.undo()
        monkeypatch.setenv("PATH", _rg_path() or "")
        with_rg = await ex.execute("search_code", args)
        monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)  # no rg: grep branch
        return with_rg, await ex.execute("search_code", args)

    everything = {".env", ".hidden/x.txt", "build/out.txt", "plain.txt", "run.log"}
    accepted = {
        None: everything,
        "*": everything,
        "*.txt": {".hidden/x.txt", "build/out.txt", "plain.txt"},
        "*.log": {"run.log"},
        ".env": {".env"},
        "?lain.txt": {"plain.txt"},
        ".git": set(),  # a glob cannot reach .git either
    }
    for glob, expected in accepted.items():
        args: dict[str, Any] = {"pattern": "needle"}
        if glob is not None:
            args["glob"] = glob
        with_rg, with_grep = await both(args)
        assert files(with_rg) == files(with_grep) == expected, (glob, with_rg, with_grep)
    # a glob grep cannot express is refused in BOTH branches, never half-honoured
    for glob in ("**/*", "!.git", "*.{py,log}", "[ab]*", "sub/*.txt", "-x", "**"):
        with_rg, with_grep = await both({"pattern": "needle", "glob": glob})
        assert with_rg.startswith("error: refused") and "glob" in with_rg, (glob, with_rg)
        assert with_grep == with_rg, glob
    # a path inside .git is refused in both branches (rg would otherwise descend)
    for path in (".git", ".git/", "./.git", ".git/objects"):
        with_rg, with_grep = await both({"pattern": "needle", "path": path})
        assert with_rg.startswith("error: refused") and ".git internals" in with_rg, path
        assert with_grep == with_rg and "NEEDLE_IN_GIT" not in with_rg


async def test_git_internals_are_refused_by_every_path_tool(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / "NEEDLE_IN_GIT").write_text("needle SECRETBODY\n")
    (repo / "viaLink").symlink_to(repo / ".git")
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)
    for tool, args in (
        ("read_file", {"path": ".git/config"}),
        ("read_file", {"path": ".git/NEEDLE_IN_GIT"}),
        ("list_dir", {"path": ".git"}),
        ("list_dir", {"path": "./.git/"}),
        ("repo_tree", {"path": ".git"}),
        ("search_code", {"pattern": "needle", "path": ".git"}),
        ("search_code", {"pattern": "needle", "path": "viaLink"}),
        ("git_read", {"subcommand": "ls-files", "path": ".git"}),
        ("git_read", {"subcommand": "log", "path": ".git/config"}),
    ):
        out = await ex.execute(tool, args)
        assert out.startswith("error: refused") and ".git internals are not surveyed" in out, (
            tool,
            args,
            out,
        )
        assert "SECRETBODY" not in out and "[core]" not in out


async def test_legitimate_survey_calls_still_work_after_the_git_and_glob_guards(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "notes.txt").write_text("--dash-leading flag text\n")
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)
    assert "app.py:2:" in await ex.execute("search_code", {"pattern": "upper"})
    assert "app.py:2:" in await ex.execute("search_code", {"pattern": "upper", "glob": "*.py"})
    dash = await ex.execute("search_code", {"pattern": "--dash-leading", "fixed": True})
    assert "notes.txt:1:" in dash
    blame = await ex.execute("git_read", {"subcommand": "blame", "path": "app.py"})
    assert "hello" in blame and "(exit 0)" in blame
    # a revision with a path part is a rev, not a path argument: not blocked
    shown = await ex.execute("git_read", {"subcommand": "show", "rev": "HEAD:app.py"})
    assert "upper" in shown and not shown.startswith("error: refused")
    assert "def greet" in await ex.execute("read_file", {"path": "app.py"})
    assert "app.py" in await ex.execute("list_dir", {})


def _poison_repo(repo: Path, hook: str) -> None:
    for key in ("core.fsmonitor", "diff.evil.textconv", "diff.evil.command", "core.pager"):
        subprocess.run(["git", "config", key, hook], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "app.py").write_text("def greet(name):\n    return 1\n")


async def test_a_hostile_repo_config_cannot_run_anything_through_git_read(tmp_path: Path) -> None:
    """The repo being surveyed is untrusted: fsmonitor, textconv and an external
    diff driver configured in it must not execute (they could write)."""
    repo = _repo(tmp_path)
    marker = tmp_path / "pwned"
    _poison_repo(repo, f"sh -c 'touch {marker}'")
    ex = ReadOnlyExecutor(LocalExecutor(repo), repo)
    for sub in ("status", "diff", "show", "log", "blame", "ls-files"):
        args = {"subcommand": sub, **({"path": "app.py"} if sub == "blame" else {})}
        await ex.execute("git_read", args)
    assert not marker.exists(), "a hostile repo config ran a command"


async def test_output_is_capped_in_lines_with_a_note(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"f{i:02d}.txt").write_text("needle\n")
    ex = ReadOnlyExecutor(LocalExecutor(tmp_path), tmp_path)
    out = await ex.execute("search_code", {"pattern": "needle", "max_results": 5})
    # (exit N) header + max_results body lines + the note
    assert len(out.splitlines()) == 1 + 5 + 1
    assert "more lines cut at max 5" in out
    assert out.startswith("(exit ")


def test_audit_plan_cannot_publish() -> None:
    assert "audit" in PLANS
    assert PLAN_AUDIT["gates"] == []
    assert not PLAN_AUDIT.get("publish")
    assert [s["role"] for s in PLAN_AUDIT["steps"]] == ["investigator", "surveyor"]
    assert PLAYBOOK_GATES["audit"] == []
    assert PLAYBOOK_PUBLISH["audit"] == ""
    assert "audit" in PLAYBOOKS and set(PLAYBOOK_GATES) == set(PLAYBOOKS)
    plan = orchestrated_plan("audit", "pro")
    assert plan["gates"] == [] and plan["publish"] == ""
    assert "coder" not in PLAYBOOKS["audit"].replace("Do NOT spawn a coder", "")


async def test_create_mission_audit_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import make_git_repo
    from rework.missions import create_mission

    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    repo = make_git_repo(tmp_path / "r")
    mission = await create_mission("map the repo", plan="audit", repo=str(repo))
    assert mission.plan is not None
    assert mission.plan["kind"] == "audit"
    assert mission.plan["gates"] == []
    assert "publish" not in mission.plan
    assert all(s["role"] != "coder" for s in mission.plan["steps"])


def test_survey_sections_and_turns() -> None:
    keys = [s.key for s in resolve_sections(list(ASSEMBLY_SURVEYOR))]
    assert set(keys) == set(ASSEMBLY_SURVEYOR)
    schema = build_report_schema(resolve_sections(list(ASSEMBLY_SURVEYOR)))
    assert set(schema["required"]) == set(ASSEMBLY_SURVEYOR)
    assert DEFAULT_TURNS["surveyor"] == 25


def test_cap_cuts_with_explicit_note() -> None:
    risks = [
        {"severity": "low" if i % 2 else "high", "area": "a", "detail": str(i), "where": "f:1"}
        for i in range(30)
    ]
    out = finalize_survey({**ARTIFACT, "risks": risks, "gaps": [f"g{i}" for i in range(25)]})
    assert len(out["risks"]) + len(out["gaps"]) == MAX_RISKS_AND_GAPS
    assert out["risks"][0]["severity"] == "high"  # worst first
    assert out["cut_note"].startswith("15 of 55 risks and gaps were cut")
    assert "were cut" in out["report"]
    small = finalize_survey({**ARTIFACT, "report": ""})
    assert "cut_note" not in small
    assert "app.py:2" in small["report"]  # empty report rebuilt from the fields


def test_render_md_surveyor_only_mission_shows_the_map() -> None:
    run = SimpleNamespace(step="survey", role="surveyor", attempt=1, status=RunStatus.COMPLETED)
    mission = SimpleNamespace(
        id="m-1",
        status=MissionStatus.DONE,
        objective="map it",
        updated_at=2.0,
        created_at=1.0,
        result={},
    )
    spend = SimpleNamespace(spent_usd=0.0, input_tokens=0, output_tokens=0, cached_tokens=0)
    artifact = {**ARTIFACT, "cut_note": "3 of 43 risks and gaps were cut at the cap of 40."}
    md = _render_md([(run, artifact)], spend, mission)
    assert "A one-file greeter." in md
    assert "### Layout" in md and "app.py" in md
    assert "### Risks" in md and "`app.py:2`" in md
    assert "app.py:1-2" in md  # entry point where
    assert "were cut" in md
    assert "no tests were found" in md
    assert "coder" not in md.replace("(surveyor)", "")


async def test_surveyor_only_mission_end_to_end_renders_the_map_via_report_md(
    sv_ctx: RoleBuildCtx, tmp_path: Path
) -> None:
    """A scripted survey through the real role graph, stored, then rendered by
    the real `rework report --md` path (a subprocess on the same sqlite DB):
    the Report output is the actual map, not just run stats, and an over-cap
    survey says how many findings were dropped."""
    fake = sv_ctx.provider
    assert isinstance(fake, FakeProvider)
    risks = [
        {"severity": "low", "area": f"area {i}", "detail": f"risk {i}", "where": f"app.py:{i + 1}"}
        for i in range(30)
    ]
    big = {**ARTIFACT, "risks": risks, "gaps": [f"gap {i}" for i in range(20)]}  # 50 > 40
    _push(fake, ChatResult(text="mapped", usage=Usage(input_tokens=5, output_tokens=5)))
    _push(fake, ChatResult(text=json.dumps(big), usage=Usage(input_tokens=9, output_tokens=9)))
    graph = autodiscover().get("surveyor").build_graph(sv_ctx)
    graph.checkpointer = InMemorySaver()
    final = await graph.ainvoke(
        {"objective": "map this repo"}, config={"configurable": {"thread_id": "t-e2e"}}
    )
    artifact = final["artifact"]
    assert artifact is not None, final.get("artifact_error")
    required = {
        "title", "summary", "layout", "entry_points", "dependencies", "config",
        "tests", "ci", "risks", "gaps", "next_steps",
    }  # fmt: skip
    assert required <= set(artifact), "all eleven required keys"
    assert len(artifact["risks"]) + len(artifact["gaps"]) == MAX_RISKS_AND_GAPS
    assert "10 of 50" in artifact["cut_note"]
    assert all(re.search(r"\S+:\d+", r["where"]) for r in artifact["risks"]), "file:line"

    store = sv_ctx.store
    await store.update_run(sv_ctx.run.id, status=RunStatus.COMPLETED, artifact=artifact)
    await store.update_mission("mi", status=MissionStatus.DONE)
    dbfile = tmp_path / "sv.sqlite3"
    md_path = tmp_path / "report.md"
    env = {**os.environ, "REWORK_DB": str(dbfile), "NO_COLOR": "1"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rework", "report", "mi", "--md", "--out", str(md_path),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, stderr.decode()
    md = md_path.read_text()
    assert "tiny app" in md and "A one-file greeter." in md
    assert "### Layout" in md and "app.py" in md and "the greeter" in md
    assert "greet" in md and "app.py:1-2" in md  # entry point
    assert "### Risks" in md and "`app.py:2`" in md  # file:line visible in where
    assert "### Gaps" in md and "- gap 0" in md  # gaps (the cut keeps the first ones)
    assert "add a test for greet" in md  # next steps
    assert "10 of 50" in md and "were cut" in md  # the cut note names how many
