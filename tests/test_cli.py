"""CLI tests: the M0 smoke (entrypoint imports, --help, version) plus the
mission commands (`new`, `ls`, `gates`, `report`) driven through typer's
CliRunner against a throwaway sqlite DB.

Every CLI command body calls `asyncio.run(...)`, so these tests are plain sync
functions: invoking CliRunner from inside an `async def` test dies with
"asyncio.run() cannot be called from a running event loop" (pytest-asyncio is
in auto mode). The `rework_db` fixture below is autouse precisely because a
command that runs without REWORK_DB set would read and write the developer's
real ~/.rework/state.sqlite3.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rework import __version__
from rework.cli import _models_doc_entries, app
from rework.kernel.state.records import Gate, GateKind, GateStatus, Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite

runner = CliRunner()


@pytest.fixture(autouse=True)
def plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer/rich colour error panels when FORCE_COLOR is set (GitHub Actions
    sets it), which splits messages with ANSI codes: asserting on
    `result.output` then fails in CI only. Tests read plain text."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture(autouse=True)
def rework_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point REWORK_DB at a tmp sqlite file for every test in this module.

    Nothing else sets it (tests/conftest.py has no autouse REWORK_DB fixture),
    so without this the commands would hit the real ~/.rework/state.sqlite3.
    """
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    return db


def _missions(db: Path) -> list[Mission]:
    async def go() -> list[Mission]:
        store = await open_sqlite(str(db))
        try:
            return await store.list_missions()
        finally:
            await store.aclose()

    return asyncio.run(go())


def _seed_mission(db: Path, *, mission_id: str, objective: str, title: str | None = None) -> None:
    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(
                    id=mission_id,
                    objective=objective,
                    title=title,
                    status=MissionStatus.PENDING,
                )
            )
        finally:
            await store.aclose()

    asyncio.run(go())


def _seed_gate(db: Path, gate: Gate) -> None:
    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_gate(gate)
        finally:
            await store.aclose()

    asyncio.run(go())


def _get_mission(db: Path, mission_id: str) -> Mission | None:
    async def go() -> Mission | None:
        store = await open_sqlite(str(db))
        try:
            return await store.get_mission(mission_id)
        finally:
            await store.aclose()

    return asyncio.run(go())


def _get_project(db: Path, project_id: str):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        store = await open_sqlite(str(db))
        try:
            return await store.get_project(project_id)
        finally:
            await store.aclose()

    return asyncio.run(go())


def _get_chat(db: Path, chat_id: str):
    async def go():
        store = await open_sqlite(str(db))
        try:
            return await store.get_chat(chat_id)
        finally:
            await store.aclose()

    return asyncio.run(go())


def test_project_create_defaults_to_the_upload_transport(rework_db: Path) -> None:
    """A project with no --repo has no origin to push to (ADR 0005): creation
    must store transport=upload / results=fetch, never git/push_branch."""
    result = runner.invoke(app, ["project", "create", "NAME"])

    assert result.exit_code == 0, result.output
    project = _get_project(rework_db, result.output.strip())
    assert project is not None
    assert project.settings["transport"] == "upload"
    assert project.settings["results"] == "fetch"
    assert project.settings["repos"] == []


def test_project_create_with_a_repo_defaults_to_git(rework_db: Path) -> None:
    result = runner.invoke(app, ["project", "create", "N2", "--repo", "r=https://github.com/a/b"])

    assert result.exit_code == 0, result.output
    project = _get_project(rework_db, result.output.strip())
    assert project is not None
    assert project.settings["transport"] == "git"
    assert project.settings["results"] == "push_branch"
    assert project.settings["repos"] == [
        {"name": "r", "kind": "github", "url": "https://github.com/a/b"}
    ]


def test_project_create_rejects_git_transport_without_a_repo(rework_db: Path) -> None:
    """--transport git with nothing to clone would create a project every fix
    mission then fails on; refuse it at creation."""
    result = runner.invoke(app, ["project", "create", "N3", "--transport", "git"])

    assert result.exit_code != 0, result.output
    assert "needs at least one --repo" in result.output


def test_project_create_rejects_a_transport_that_contradicts_the_repo(
    rework_db: Path,
) -> None:
    """`--transport upload --repo r=URL` stored a transport the project does not
    have (the repos derive git), and `project show` printed that stale value.
    Storing the derived pair is the rule; a contradiction is refused outright."""
    result = runner.invoke(
        app, ["project", "create", "N4", "--transport", "upload", "--repo", "r=github.com/a/b"]
    )

    assert result.exit_code != 0, result.output
    assert "--transport upload needs no --repo; drop one" in result.output


def test_project_create_stores_the_transport_the_repos_derive(rework_db: Path) -> None:
    """Whatever the flags say, the stored settings agree with the repo list, so
    `project show` can never print a transport the project does not have."""
    with_repo = runner.invoke(app, ["project", "create", "N5", "--repo", "r=github.com/a/b"])
    assert with_repo.exit_code == 0, with_repo.output
    assert _get_project(rework_db, with_repo.output.strip()).settings["transport"] == "git"
    assert _get_project(rework_db, with_repo.output.strip()).settings["results"] == "push_branch"

    bare = runner.invoke(app, ["project", "create", "N6"])
    assert bare.exit_code == 0, bare.output
    assert _get_project(rework_db, bare.output.strip()).settings["transport"] == "upload"
    assert _get_project(rework_db, bare.output.strip()).settings["results"] == "fetch"


def test_project_add_files_rejects_a_missing_path(
    rework_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd path used to pack to an EMPTY tarball and come back 200, so a
    wrong path looked like success. It now fails before any network call."""
    created = runner.invoke(app, ["project", "create", "ADDF"])
    assert created.exit_code == 0, created.output
    project_id = created.output.strip()

    def no_network(*_a: object, **_k: object) -> None:  # pragma: no cover
        raise AssertionError("a missing path must fail before any HTTP call")

    import rework.project_files as project_files

    monkeypatch.setattr(project_files, "_check", no_network)
    present = tmp_path / "there.md"
    present.write_text("# there\n")

    result = runner.invoke(
        app, ["project", "add-files", project_id, str(present), str(tmp_path / "tehre.md")]
    )

    assert result.exit_code == 1, result.output
    assert "no such file or folder" in result.output


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "rework" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_new_accepts_git_url_without_local_path(rework_db: Path) -> None:
    """A --repo that is a git URL is an origin for the daemon to clone: `new`
    must never stat it as a local path (the URL points at no local directory)."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "fix the planted bug", "--repo", url, "--plan", "fix"])

    assert result.exit_code == 0, result.output
    assert "repo not found" not in result.output
    mission_id = result.stdout.strip()
    assert mission_id.startswith("m-")

    mission = _get_mission(rework_db, mission_id)
    assert mission is not None
    assert mission.repo_origin == url  # the daemon clones this itself
    assert mission.repo_path is None  # nothing was resolved on the local filesystem


def test_new_accepts_git_url_without_plan(rework_db: Path) -> None:
    url = "git@github.com:example/repo.git"
    result = runner.invoke(app, ["new", "plain mission", "--repo", url])

    assert result.exit_code == 0, result.output
    assert "repo not found" not in result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert mission.repo_origin == url
    assert mission.repo_path is None


def test_ls_output_format(rework_db: Path) -> None:
    """`ls` prints `{id}  {status:<14}  {name}` (two spaces, status
    left-justified to width 14), where `name` is the title when the mission has
    one and the flattened objective otherwise."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0, result.output
    assert result.output == "m-abc123  pending         fix the planted bug\n"


def test_ls_prints_one_row_per_mission_with_a_multiline_objective(rework_db: Path) -> None:
    """Objectives are free text and are often numbered briefs; `ls` must render
    one line per mission, never spill the objective across terminal rows."""
    objective = (
        "(1) 'rework update' must not print the whole objective\n"
        "(2) derive a short title when none is given\n"
        "   (6) gc must reclaim role scratch dirs\n"
    )
    _seed_mission(rework_db, mission_id="m-multi", objective=objective)

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 1, result.output
    assert "\n" not in result.output.rstrip("\n")
    assert lines[0].startswith("m-multi  pending         (1) 'rework update'")
    assert lines[0].endswith("\u2026")  # flattened and clipped, not cut mid-nowhere


def test_ls_prefers_the_title_and_clips_a_long_one(rework_db: Path) -> None:
    """A title (item 2) is what `ls` shows; it is clipped too, so a stray long
    title cannot break the one-row-per-mission rule."""
    _seed_mission(rework_db, mission_id="m-titled", objective="raw\nobjective", title="short name")
    _seed_mission(rework_db, mission_id="m-long", objective="x", title="word " * 40)

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 2, result.output
    assert "short name" in lines[1]
    assert "raw" not in result.output
    assert all(len(line) <= 6 + 2 + 14 + 2 + 72 for line in lines)
    assert lines[0].endswith("\u2026")


def test_new_without_a_title_stores_a_derived_short_title(rework_db: Path) -> None:
    """CLI-created missions used to store title=None, so the PR title fell back
    to the objective's first line (found live: PR #24)."""
    url = "https://github.com/example/repo.git"
    objective = (
        "Follow-up to #22 (just merged, NOT deployed to rewvis until this lands):"
        " role_env scratch dirs pile up. Make gc reclaim them."
    )
    result = runner.invoke(app, ["new", objective, "--repo", url])

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert mission.title
    assert "\n" not in mission.title
    assert len(mission.title.split()) <= 8


def test_new_title_option_wins(rework_db: Path) -> None:
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "a long objective. more words", "--repo", url, "--title", "My Title"]
    )

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert mission.title == "My Title"


def test_ls_on_empty_db_prints_nothing(rework_db: Path) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_gates_with_no_open_gates(rework_db: Path) -> None:
    """A decided gate is not open, so `gates` reports none."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")
    _seed_gate(
        rework_db,
        Gate(
            id="gate-decided",
            mission_id="m-abc123",
            kind=GateKind.SHIP,
            question="Ship the mission result?",
            status=GateStatus.APPROVED,
        ),
    )

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert result.output == "no open gates\n"


def test_gates_lists_open_gate_for_mission(rework_db: Path) -> None:
    """`--mission-id` is an option (not a positional argument)."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")
    _seed_gate(
        rework_db,
        Gate(
            id="gate-xyz",
            mission_id="m-abc123",
            kind=GateKind.SHIP,
            question="Ship the mission result?",
        ),
    )

    result = runner.invoke(app, ["gates", "--mission-id", "m-abc123"])

    assert result.exit_code == 0, result.output
    assert result.output == "gate-xyz  [ship]  Ship the mission result?\n"


def test_report_missing_mission_exits_cleanly(rework_db: Path) -> None:
    result = runner.invoke(app, ["report", "m-nope"])

    assert result.exit_code == 1
    assert "no such mission: m-nope" in result.output
    assert "Traceback" not in result.output


def test_report_missing_mission_md_flag(rework_db: Path) -> None:
    result = runner.invoke(app, ["report", "m-nope", "--md"])

    assert result.exit_code == 1
    assert "no such mission: m-nope" in result.output
    assert "Traceback" not in result.output


@pytest.fixture()
def no_capacity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient capacity env must not decide what these tests prove."""
    for name in (
        "REWORK_CAPACITY",
        "REWORK_CAPACITY_INTERVAL_S",
        "REWORK_CAPACITY_PINS",
        "REWORK_CONCURRENCY",
        "REWORK_MAX_ROLES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_capacity_prints_a_recomputed_plan_when_auto_is_active(no_capacity_env: None) -> None:
    """`rework capacity` recomputes (measure + planner) and says so: it must not
    pretend to read the daemon's live caps, because there is no such route."""
    from rework.orchestrator.capacity import measure, plan

    result = runner.invoke(app, ["capacity"])
    assert result.exit_code == 0, result.output
    cpus, _load, _avail = measure()
    planned = plan(*measure())
    assert "measured on this host now:" in result.output
    assert f"{cpus} CPUs" in result.output
    assert f"plan: {planned.concurrency} missions / {planned.max_roles} roles" in result.output, (
        result.output
    )
    assert "NOT read from the daemon" in result.output
    assert "missions MANAGED" in result.output and "roles MANAGED" in result.output
    assert "AUTO IS ACTIVE" in result.output
    assert "rework logs daemon -g 'capacity'" in result.output


def test_capacity_prints_the_truth_when_auto_is_off(
    no_capacity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REWORK_CAPACITY=fixed: AUTO IS OFF, and the pair printed is the one resolved
    at startup, not a plan that never runs."""
    from rework.config import Config
    from rework.orchestrator.capacity import measure

    monkeypatch.setenv("REWORK_CAPACITY", "fixed")
    config = Config.from_env()
    result = runner.invoke(app, ["capacity"])
    assert result.exit_code == 0, result.output
    cpus = measure()[0]
    roles = config.max_roles or cpus
    assert "AUTO IS OFF" in result.output
    assert "REWORK_CAPACITY=fixed" in result.output
    assert f"plan: {config.concurrency} missions / {roles} roles" in result.output, result.output
    assert "as resolved at startup" in result.output
    assert "no planner, no watchdog" in result.output
    # nothing measured, so no live measurement line above "no measurement"
    assert "measured on this host now" not in result.output, result.output
    assert "measured nothing" in result.output, result.output


def test_capacity_reports_both_pinned_caps_as_pinned_fixed(
    no_capacity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_CAPACITY_PINS", "fixed")
    monkeypatch.setenv("REWORK_CONCURRENCY", "1")
    monkeypatch.setenv("REWORK_MAX_ROLES", "2")
    result = runner.invoke(app, ["capacity"])
    assert result.exit_code == 0, result.output
    assert "missions PINNED at 1 by REWORK_CONCURRENCY=1 (pins=fixed)" in result.output
    assert "roles PINNED at 2 by REWORK_MAX_ROLES=2 (pins=fixed)" in result.output
    assert "AUTO IS OFF" in result.output
    assert "both caps PINNED under pins=fixed" in result.output
    # a frozen pair must not be introduced by a live measurement line, nor carry
    # the recompute wording: nothing was measured and nothing was recomputed
    assert "measured on this host now" not in result.output, result.output
    assert "measured nothing" in result.output, result.output
    assert "recomputed here from the env" not in result.output, result.output


def test_capacity_json_does_not_claim_a_recompute_when_auto_is_off(
    no_capacity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully pinned config: `recomputed`/`basis` must point at the startup pair,
    never at a measurement, or a script reading them concludes 8 missions came
    from the box. Same facts in the text branch and the JSON one."""
    monkeypatch.setenv("REWORK_CAPACITY_PINS", "fixed")
    monkeypatch.setenv("REWORK_CONCURRENCY", "8")
    monkeypatch.setenv("REWORK_MAX_ROLES", "2")
    result = runner.invoke(app, ["capacity", "--json"])
    assert result.exit_code == 0, result.output
    facts = json.loads(result.output)
    assert facts["auto_active"] is False
    assert facts["recomputed"] is False, "nothing was recomputed"
    assert "startup" in facts["planned"]["basis"], facts["planned"]["basis"]
    assert "measurement" not in facts["planned"]["basis"], facts["planned"]["basis"]
    assert facts["measurement"] is None
    assert facts["planned"]["measured_missions"] is None
    assert facts["planned"]["measured_roles"] is None
    assert (facts["planned"]["missions"], facts["planned"]["roles"]) == (8, 2)
    text = runner.invoke(app, ["capacity"]).output
    assert f"plan: 8 missions / 2 roles  ({facts['planned']['basis']}" in text, text


def test_capacity_reads_max_roles_as_a_floor_by_default(
    no_capacity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_MAX_ROLES", "2")
    result = runner.invoke(app, ["capacity"])
    assert result.exit_code == 0, result.output
    assert "roles MANAGED, floor 2" in result.output
    assert "AUTO IS ACTIVE" in result.output, "a floor never freezes a cap"


def test_capacity_rejects_an_invalid_pins_value(
    no_capacity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_CAPACITY_PINS", "floo")
    result = runner.invoke(app, ["capacity"])
    assert result.exit_code == 2, result.output
    assert "REWORK_CAPACITY_PINS" in result.output


def test_capacity_json_carries_the_same_facts(no_capacity_env: None) -> None:
    result = runner.invoke(app, ["capacity", "--json"])
    assert result.exit_code == 0, result.output
    facts = json.loads(result.output)
    assert facts["recomputed"] is True
    assert facts["auto_active"] is True
    assert facts["pins"] == "floor"
    assert facts["measurement"]["cpus"] >= 1
    assert facts["planned"]["missions"] >= 1
    assert "MANAGED" in facts["why"]["missions"]


def test_cancel_pause_resume_commands(rework_db: Path) -> None:
    """`rework cancel|pause|resume <id>` write the mission status; that row is
    what the daemon reads, so the commands work from any machine."""

    async def seed() -> str:
        store = await open_sqlite(str(rework_db))
        try:
            m = await store.create_mission(
                Mission(id="m-ctl", objective="control me", status=MissionStatus.RUNNING)
            )
            return m.id
        finally:
            await store.aclose()

    mid = asyncio.run(seed())
    runner = CliRunner()

    def status() -> str:
        async def read() -> str:
            store = await open_sqlite(str(rework_db))
            try:
                m = await store.get_mission(mid)
                assert m is not None
                return m.status.value
            finally:
                await store.aclose()

        return asyncio.run(read())

    assert runner.invoke(app, ["cancel", mid]).exit_code == 0
    assert status() == "cancelled"

    assert runner.invoke(app, ["pause", mid]).exit_code == 0
    assert status() == "on_hold"

    assert runner.invoke(app, ["resume", mid]).exit_code == 0
    assert status() == "running"

    # unknown ids fail loudly rather than silently succeeding
    assert runner.invoke(app, ["cancel", "m-nope"]).exit_code == 1


def test_approve_accepts_a_typed_answer(rework_db: Path) -> None:
    """--answer / --answer-json put a typed decision on the gate row, which is
    what the paused role receives."""

    counter = {"n": 0}

    async def seed() -> str:
        counter["n"] += 1
        n = counter["n"]
        store = await open_sqlite(str(rework_db))
        try:
            mid = f"m-g{n}"
            await store.create_mission(Mission(id=mid, objective="gate me"))
            gate = await store.create_gate(
                Gate(id=f"gate-typed-{n}", mission_id=mid, kind=GateKind.GENERIC, question="fix?")
            )
            return gate.id
        finally:
            await store.aclose()

    gid = asyncio.run(seed())
    runner = CliRunner()
    result = runner.invoke(app, ["approve", gid, "--reject", "--answer", "don't touch the parser"])
    assert result.exit_code == 0, result.output

    async def read(gate_id: str):
        store = await open_sqlite(str(rework_db))
        try:
            return await store.get_gate(gate_id)
        finally:
            await store.aclose()

    gate = asyncio.run(read(gid))
    assert gate is not None
    assert gate.status == GateStatus.REJECTED
    assert gate.decision == {"feedback": "don't touch the parser"}

    # structured form
    gid2 = asyncio.run(seed())
    out = runner.invoke(app, ["approve", gid2, "--answer-json", '{"changes": ["a"], "n": 2}'])
    assert out.exit_code == 0, out.output
    gate2 = asyncio.run(read(gid2))
    assert gate2 is not None and gate2.decision == {"changes": ["a"], "n": 2}

    # bad JSON is a usage error, not a crash
    bad = runner.invoke(app, ["approve", gid2, "--answer-json", "{nope"])
    assert bad.exit_code != 0


def test_new_creates_an_orchestrated_mission_by_default(rework_db: Path) -> None:
    """ADR 0007: missions are run by an LLM orchestrator; the plan is its
    playbook, the final gate and budget are rails."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "fix it", "--repo", url, "--plan", "fix-pr", "--budget", "3"]
    )
    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    plan = mission.plan or {}
    assert plan["kind"] == "auto" and plan["playbook"] == "fix-pr"
    assert plan["gates"] == []
    assert plan["orchestrator"]["model"]
    assert plan["budget"]["max_usd"] == 3.0
    # a CLI-created mission gets a chat titled from the mission title, so it is
    # reachable from the Work tab / Dashboard like any chat-backed mission
    assert mission.chat_id is not None
    chat = _get_chat(rework_db, mission.chat_id)
    assert chat is not None
    assert chat.title == mission.title
    assert chat.mission_id == mission.id


def test_new_mechanical_keeps_the_legacy_pipeline(rework_db: Path) -> None:
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "fix it", "--repo", url, "--plan", "fix", "--mechanical"])
    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert "orchestrator" not in (mission.plan or {})
    assert [s["step"] for s in (mission.plan or {})["steps"]] == ["investigate", "code", "audit"]


def test_new_mechanical_never_reroutes_an_audit_to_draft(rework_db: Path) -> None:
    """`--mechanical` runs the fixed template steps (investigate, code, audit),
    so the draft default must not apply: the coder would still write into the
    target repo, while a draft plan has no gate and no publish, i.e. the work
    would be unreachable. The caller's plan stays exactly as it is on base."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "audit the release process", "--repo", url, "--mechanical"])

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    # a template mission is built from no playbook spec at all (the engine's
    # DEFAULT_PLAN template routes it), which is exactly what base did here: a
    # draft plan would instead carry PLAN_FIX's steps under kind "draft"
    assert mission.plan is None, "a template mission keeps the caller's plan"
    assert "read-and-report objective" not in result.stderr


def test_report_md_shows_accepted_findings(rework_db: Path) -> None:
    """What the mission shipped with knowingly open belongs in the human report,
    with the same wording the PR body uses (one shared renderer)."""

    async def seed() -> None:
        store = await open_sqlite(str(rework_db))
        try:
            await store.create_mission(
                Mission(id="m-acc", objective="ship knowingly", status=MissionStatus.DONE)
            )
            await store.update_mission(
                "m-acc",
                result={
                    "summary": "shipped with one finding accepted",
                    "accepted_findings": [
                        {"finding": "No test for the zero-results path", "reason": "issue filed"}
                    ],
                },
            )
        finally:
            await store.aclose()

    asyncio.run(seed())
    out = rework_db.parent / "report.md"
    result = runner.invoke(app, ["report", "m-acc", "--md", "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "### Accepted findings (shipped knowingly)" in text
    assert "**No test for the zero-results path** — issue filed" in text


def test_library_backfill_bodies_command(
    rework_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rework library backfill-bodies` fills rows that predate the body
    column from this machine's library git folder, and is safe to re-run."""
    from rework.library.model import ItemStatus, ItemType, LibraryItem
    from rework.library.store import GitLibrary, open_library_store

    lib_root = tmp_path / "library"
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(lib_root))

    async def seed() -> str:
        index = await open_library_store(str(rework_db))
        git = GitLibrary(lib_root)
        try:
            legacy = LibraryItem(type=ItemType.SKILL, name="legacy skill", status=ItemStatus.ACTIVE)
            legacy.body_path = await asyncio.to_thread(
                git.write_body, legacy, "# legacy body\n", "library: add skill"
            )
            await index.upsert_item(legacy)  # row with an empty body: pre-migration
            return legacy.id
        finally:
            await index.aclose()

    item_id = asyncio.run(seed())

    out = runner.invoke(app, ["library", "backfill-bodies"])
    assert out.exit_code == 0, out.output
    assert "backfilled 1 item body/bodies" in out.output
    assert "skipped" not in out.output

    async def body_of() -> str:
        index = await open_library_store(str(rework_db))
        try:
            got = await index.get_item(item_id)
            return got.body if got else "<missing>"
        finally:
            await index.aclose()

    assert asyncio.run(body_of()) == "# legacy body\n"

    # idempotent: nothing left to fill
    again = runner.invoke(app, ["library", "backfill-bodies"])
    assert again.exit_code == 0, again.output
    assert "backfilled 0 item body/bodies" in again.output


# --- report --to-library (repeatable report hand-off) -----------------------


@pytest.fixture()
def library_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway library git folder: without this the command would commit a
    body file into the developer's real ~/.rework/library."""
    root = tmp_path / "library"
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(root))
    return root


def _library_items(db: Path) -> list[Any]:
    from rework.library.store import open_library_store

    async def go() -> list[Any]:
        index = await open_library_store(str(db))
        try:
            return await index.list_items(limit=1000)
        finally:
            await index.aclose()

    return asyncio.run(go())


def _seed_summary_mission(db: Path, mission_id: str, summary: str) -> None:
    """A finished mission whose ONLY report trace is result.summary, which is
    exactly the state the hand-off has to survive."""

    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(
                    id=mission_id,
                    objective="audit the code base",
                    status=MissionStatus.DONE,
                )
            )
            await store.update_mission(mission_id, result={"summary": summary})
        finally:
            await store.aclose()

    asyncio.run(go())


def test_report_to_library_files_the_summary_fallback(rework_db: Path, library_dir: Path) -> None:
    """`rework report M --to-library` turns a finished mission into one report
    item: type report, name from the mission, the seeded text in the body, and
    the mission id in the tags. The summary is a labelled fallback, so the tag
    says so."""
    summary = "## What changed\n\nOne new file, nothing else: docs/audits/x.md"
    _seed_summary_mission(rework_db, "m-hand", summary)

    result = runner.invoke(app, ["report", "m-hand", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "source: summary" in result.output

    items = _library_items(rework_db)
    assert len(items) == 1
    item = items[0]
    from rework.library.model import ItemStatus, ItemType

    assert item.type is ItemType.REPORT
    assert item.status is ItemStatus.ACTIVE
    assert item.name == "Report: m-hand"
    assert item.provenance.mission_id == "m-hand"
    assert "mission:m-hand" in item.tags
    assert "source:summary" in item.tags
    # the resolved text rides along unmodified, behind a one-line header
    assert summary in item.body
    assert item.body.startswith("Handed off from rework mission m-hand.")
    # the body also landed in the library git folder, not only the index row
    assert item.body_path and (library_dir / item.body_path).is_file()


def test_report_to_library_unknown_mission_writes_nothing(
    rework_db: Path, library_dir: Path
) -> None:
    """An unknown mission id is a clean non-zero exit with nothing written."""
    result = runner.invoke(app, ["report", "m-nope", "--to-library"])
    assert result.exit_code != 0
    assert "no such mission: m-nope" in result.output
    assert _library_items(rework_db) == []
    assert not library_dir.exists() or not any(library_dir.rglob("*.md"))


def test_report_to_library_dry_run_writes_nothing_but_prints_the_plan(
    rework_db: Path, library_dir: Path
) -> None:
    """--dry-run prints exactly what would be created (id, name, type, status,
    tags, body length, source) and writes NOTHING: no index row, no body file."""
    _seed_summary_mission(rework_db, "m-dry", "the audit body")

    result = runner.invoke(app, ["report", "m-dry", "--to-library", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would create report l-" in result.output
    assert "[report/active]" in result.output
    assert "name: Report: m-dry" in result.output
    assert "mission:m-dry" in result.output
    assert "source: summary" in result.output
    assert "body:" in result.output
    assert "dry run; nothing written" in result.output

    assert _library_items(rework_db) == []
    assert not library_dir.exists() or not any(library_dir.rglob("*.md"))


def test_report_to_library_is_idempotent_on_mission_id(rework_db: Path, library_dir: Path) -> None:
    """The dedupe key is (type=report, provenance.mission_id): a second run
    updates the same item (version bumped, id kept) instead of minting the
    duplicate `library add` would."""
    _seed_summary_mission(rework_db, "m-again", "first body")
    first = runner.invoke(app, ["report", "m-again", "--to-library"])
    assert first.exit_code == 0, first.output
    created = _library_items(rework_db)
    assert len(created) == 1
    item_id, version = created[0].id, created[0].version

    async def reseed(summary: str) -> None:
        store = await open_sqlite(str(rework_db))
        try:
            await store.update_mission("m-again", result={"summary": summary})
        finally:
            await store.aclose()

    asyncio.run(reseed("second body"))

    again = runner.invoke(app, ["report", "m-again", "--to-library"])
    assert again.exit_code == 0, again.output
    assert f"updated report {item_id}" in again.output
    items = _library_items(rework_db)
    assert len(items) == 1, "a re-run must not mint a second item"
    assert items[0].id == item_id
    assert items[0].version > version
    assert "second body" in items[0].body
    assert "first body" not in items[0].body


def test_report_to_library_prefers_from_file_and_never_reflows(
    rework_db: Path, library_dir: Path, tmp_path: Path
) -> None:
    """--from-file outranks the mission's own markdown and the summary, and the
    document is stored byte for byte behind the header line."""
    _seed_summary_mission(rework_db, "m-file", "the summary, not the document")
    doc = tmp_path / "audit.md"
    original = "# Audit\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n    indented code\n"
    doc.write_text(original, encoding="utf-8")

    result = runner.invoke(
        app,
        ["report", "m-file", "--to-library", "--from-file", str(doc), "--title", "Audit: x"],
    )
    assert result.exit_code == 0, result.output
    assert "source: from-file" in result.output
    item = _library_items(rework_db)[0]
    assert item.name == "Audit: x"
    assert "source:from-file" in item.tags
    assert item.body == f"Handed off from rework mission m-file. Source: {doc}.\n\n{original}"

    # an empty or missing file is a usage error, and writes nothing new
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    for bad in (empty, tmp_path / "gone.md"):
        fail = runner.invoke(app, ["report", "m-file", "--to-library", "--from-file", str(bad)])
        assert fail.exit_code != 0
    assert len(_library_items(rework_db)) == 1


def test_report_to_library_finds_the_mission_doc_in_a_surviving_worktree(
    rework_db: Path, library_dir: Path, tmp_path: Path
) -> None:
    """Source (b): with no --from-file, the mission's own largest changed
    docs/*.md is used. That is the case the live rescue relies on, so it must
    not fall through to the summary while the document is still on disk."""
    import subprocess

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "seed.md").write_text("seed\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], cwd=worktree, check=True, capture_output=True)

    # the shape of a real mission checkout: main carries the base repo, the
    # role's branch carries the deliverables on top of it
    run("init", "-b", "main")
    run("add", "-A")
    run("commit", "-m", "base")
    run("checkout", "-b", "rework/m-doc-code-coder-1")
    (worktree / "docs" / "audits").mkdir(parents=True)
    (worktree / "docs" / "audits" / "small.md").write_text("# small\n", encoding="utf-8")
    big = "# Audit\n\n" + "finding line\n" * 40
    (worktree / "docs" / "audits" / "codebase-audit.md").write_text(big, encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "the audit")

    async def seed() -> None:
        from rework.kernel.state.records import Run, RunStatus

        store = await open_sqlite(str(rework_db))
        try:
            await store.create_mission(
                Mission(id="m-doc", objective="audit", status=MissionStatus.DONE)
            )
            await store.update_mission("m-doc", result={"summary": "one new file"})
            await store.create_run(
                Run(
                    id="r-doc",
                    mission_id="m-doc",
                    role="coder",
                    step="code",
                    status=RunStatus.COMPLETED,
                    artifact={"worktree": str(worktree)},
                )
            )
        finally:
            await store.aclose()

    asyncio.run(seed())
    result = runner.invoke(app, ["report", "m-doc", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "source: worktree" in result.output
    assert "docs/audits/codebase-audit.md" in result.output
    assert "source:summary" not in result.output
    item = _library_items(rework_db)[0]
    assert big in item.body
    assert "source:worktree" in item.tags


def _git_repo(root: Path, files: dict[str, str]) -> Any:
    """A throwaway git repo on `main` holding `files`; returns a `run(*args)`."""
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    run("init", "-b", "main")
    run("add", "-A")
    run("commit", "-m", "base")
    return run


def _seed_worktree_mission(db: Path, mission_id: str, worktree: Path) -> None:
    async def go() -> None:
        from rework.kernel.state.records import Run, RunStatus

        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(id=mission_id, objective="audit", status=MissionStatus.DONE)
            )
            await store.update_mission(mission_id, result={"summary": "a description"})
            await store.create_run(
                Run(
                    id=f"r-{mission_id}",
                    mission_id=mission_id,
                    role="coder",
                    step="code",
                    status=RunStatus.COMPLETED,
                    artifact={"worktree": str(worktree)},
                )
            )
        finally:
            await store.aclose()

    asyncio.run(go())


def test_report_to_library_prefers_a_new_doc_over_a_bigger_edited_one(
    rework_db: Path, library_dir: Path, tmp_path: Path
) -> None:
    """The mission EDITED a big pre-existing doc and ADDED a small report: the
    added one is the report. A bigger edited doc must not win on size."""
    wt = tmp_path / "wt"
    run = _git_repo(wt, {"docs/guide.md": "# Guide\n" + "old line\n" * 5})
    run("checkout", "-b", "rework/m-ae-code-coder-1")
    (wt / "docs" / "guide.md").write_text("# Guide\n" + "edited line\n" * 200, encoding="utf-8")
    (wt / "docs" / "audits").mkdir()
    (wt / "docs" / "audits" / "report.md").write_text("# The report\nshort\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "work")
    _seed_worktree_mission(rework_db, "m-ae", wt)

    result = runner.invoke(app, ["report", "m-ae", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "docs/audits/report.md" in result.output
    assert "WARNING" not in result.output
    item = _library_items(rework_db)[0]
    assert "# The report" in item.body and "edited line" not in item.body
    assert "source:worktree" in item.tags


def test_report_to_library_labels_an_edited_doc_loudly(
    rework_db: Path, library_dir: Path, tmp_path: Path
) -> None:
    """Nothing was added, only an existing doc was edited: fall back to it, but
    warn and tag it source:doc-modified so it is never taken for a report."""
    wt = tmp_path / "wt"
    run = _git_repo(wt, {"docs/guide.md": "# Guide\nold\n"})
    run("checkout", "-b", "rework/m-ed-code-coder-1")
    (wt / "docs" / "guide.md").write_text("# Guide\nnew text\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "work")
    _seed_worktree_mission(rework_db, "m-ed", wt)

    result = runner.invoke(app, ["report", "m-ed", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "WARNING: no new report document was found" in result.output
    item = _library_items(rework_db)[0]
    assert "source:doc-modified" in item.tags
    assert "source:worktree" not in item.tags


def _contribute_role_report(db: Path, library_dir: Path, mission_id: str) -> str:
    """A report a ROLE authored: contribute() stamps provenance.mission_id on
    it, but it carries none of the hand-off's markers."""
    from rework.library.curation import contribute
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store

    async def go() -> str:
        index = await open_library_store(str(db))
        try:
            item, _ = await contribute(
                index,
                GitLibrary(library_dir),
                type_=ItemType.REPORT,
                name="Role authored report",
                description="written by a role",
                body="# role body\n",
                tags=["audit"],
                mission_id=mission_id,
            )
            return item.id
        finally:
            await index.aclose()

    return asyncio.run(go())


def test_report_to_library_never_overwrites_a_role_authored_report(
    rework_db: Path, library_dir: Path
) -> None:
    """A role's own report carries provenance.mission_id too. The hand-off must
    neither select nor rewrite it: it files its own item beside it, and only a
    re-run updates that one."""
    _seed_summary_mission(rework_db, "m-role", "the summary")
    role_id = _contribute_role_report(rework_db, library_dir, "m-role")

    from rework.library.handoff import find_report_item
    from rework.library.store import open_library_store

    async def lookup() -> Any:
        index = await open_library_store(str(rework_db))
        try:
            return await find_report_item(index, "m-role")
        finally:
            await index.aclose()

    assert asyncio.run(lookup()) is None
    dry = runner.invoke(app, ["report", "m-role", "--to-library", "--dry-run"])
    assert "would create report" in dry.output and role_id not in dry.output

    for expected_updates in (False, True):
        result = runner.invoke(app, ["report", "m-role", "--to-library"])
        assert result.exit_code == 0, result.output
        assert ("updated report" in result.output) is expected_updates
        items = {i.id: i for i in _library_items(rework_db)}
        assert len(items) == 2
        role = items[role_id]
        assert (role.name, role.tags, role.body, role.version) == (
            "Role authored report",
            ["audit"],
            "# role body\n",
            1,
        )
        (own,) = [i for i in items.values() if i.id != role_id]
        assert "handoff:report" in own.tags


def test_report_to_library_recognises_an_item_filed_before_the_marker(
    rework_db: Path, library_dir: Path
) -> None:
    """The item filed by the first version of this command has no handoff tag,
    only mission/source tags and the hand-off description. A re-run must still
    find and update it instead of minting a duplicate."""
    _seed_summary_mission(rework_db, "m-old", "first")
    assert runner.invoke(app, ["report", "m-old", "--to-library"]).exit_code == 0

    from rework.library.store import open_library_store

    async def strip_marker() -> None:
        index = await open_library_store(str(rework_db))
        try:
            (item,) = await index.list_items(limit=10)
            item.tags = [t for t in item.tags if t != "handoff:report"]
            await index.upsert_item(item)
        finally:
            await index.aclose()

    asyncio.run(strip_marker())
    result = runner.invoke(app, ["report", "m-old", "--to-library", "--dry-run"])
    assert "would update report" in result.output
    assert runner.invoke(app, ["report", "m-old", "--to-library"]).exit_code == 0
    assert len(_library_items(rework_db)) == 1


def test_report_to_library_refuses_a_role_fingerprint_spoof(
    rework_db: Path, library_dir: Path
) -> None:
    """The pre-marker fingerprint (mission tag + source tag + hand-off
    description) is role-supplied input: a role picks its own tags and
    description through library_contribute. A role item shaped exactly like the
    backfill fingerprint must therefore NOT be selected; the hand-off files its
    own item beside it and the role item keeps its body and version 1."""
    _seed_summary_mission(rework_db, "m-spoof", "the summary")
    from rework.library.curation import contribute
    from rework.library.handoff import find_report_item
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store

    async def file_spoof() -> str:
        index = await open_library_store(str(rework_db))
        try:
            item, _note = await contribute(
                index,
                GitLibrary(library_dir),
                type_=ItemType.REPORT,
                name="Spoof",
                description="Report for mission m-spoof; see threads",
                body="# the role's own body\n",
                tags=["mission:m-spoof", "source:from-file"],
                mission_id="m-spoof",
                role="coder",
            )
            assert item.provenance.role == "coder"
            return item.id
        finally:
            await index.aclose()

    spoof_id = asyncio.run(file_spoof())

    async def lookup() -> Any:
        index = await open_library_store(str(rework_db))
        try:
            return await find_report_item(index, "m-spoof")
        finally:
            await index.aclose()

    assert asyncio.run(lookup()) is None
    dry = runner.invoke(app, ["report", "m-spoof", "--to-library", "--dry-run"])
    assert "would create report" in dry.output, dry.output
    assert spoof_id not in dry.output

    result = runner.invoke(app, ["report", "m-spoof", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "created report" in result.output
    items = {i.id: i for i in _library_items(rework_db)}
    spoof = items[spoof_id]
    assert spoof.version == 1
    assert spoof.body == "# the role's own body\n"
    assert spoof.description == "Report for mission m-spoof; see threads"
    (own,) = [i for i in items.values() if i.id != spoof_id]
    assert "handoff:report" in own.tags


def test_report_to_library_ignores_a_role_marker_spoof(
    rework_db: Path,
    library_dir: Path,
) -> None:
    """The marker branch must not run before the role gate. Tags are entirely
    role-supplied: `library_contribute` passes the caller's tags through
    verbatim, so a role can mint a report carrying `handoff:report`. Shaped
    exactly like this command's own item, it must still be refused: the dry run
    says "would create", and after a real run the role item keeps its body and
    version 1 while the command files its own item beside it."""
    _seed_summary_mission(rework_db, "m-tool", "the summary")
    from rework.library.handoff import find_report_item
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store
    from rework.library.tools import LibraryAwareExecutor, LibraryHandle

    class _StubInner:
        async def execute(self, name: str, args: dict[str, Any]) -> str:  # pragma: no cover
            return ""

    body = "# the role's own body\n"
    role_tags = ["handoff:report", "mission:m-tool"]

    async def file_spoof() -> str:
        """Through the real contribution path: the role executor, whose role is
        set by the wrapper and whose tags are the role's own choice."""
        index = await open_library_store(str(rework_db))
        try:
            handle = LibraryHandle(index=index, git=GitLibrary(library_dir))
            ex = LibraryAwareExecutor(
                _StubInner(), handle, "m-tool", "m-tool:code:coder:1", "coder"
            )
            note = await ex.execute(
                "library_contribute",
                {
                    "type": "report",
                    "name": "Role report",
                    "description": "a report a role wrote",
                    "body": body,
                    "tags": role_tags,
                },
            )
            assert "contributed: l-" in note, note
            spoof_id = note.split()[1]
            (item,) = await index.list_items(type_=ItemType.REPORT, limit=10)
            assert item.id == spoof_id
            assert item.provenance.role == "coder"
            assert "handoff:report" in item.tags
            return spoof_id
        finally:
            await index.aclose()

    spoof_id = asyncio.run(file_spoof())

    async def lookup() -> Any:
        index = await open_library_store(str(rework_db))
        try:
            return await find_report_item(index, "m-tool")
        finally:
            await index.aclose()

    assert asyncio.run(lookup()) is None
    dry = runner.invoke(app, ["report", "m-tool", "--to-library", "--dry-run"])
    assert "would create report" in dry.output, dry.output
    assert "would update report" not in dry.output, dry.output
    assert spoof_id not in dry.output

    result = runner.invoke(app, ["report", "m-tool", "--to-library"])
    assert result.exit_code == 0, result.output
    assert "created report" in result.output, result.output
    assert "updated report" not in result.output, result.output
    items = {i.id: i for i in _library_items(rework_db)}
    assert len(items) == 2
    spoof = items[spoof_id]
    assert (spoof.version, spoof.body, spoof.tags) == (1, body, role_tags)
    (own,) = [i for i in items.values() if i.id != spoof_id]
    assert "handoff:report" in own.tags and own.provenance.role is None


def test_report_to_library_dedupe_relies_on_the_marker(rework_db: Path, library_dir: Path) -> None:
    """The marker, not the fingerprint, is the dedupe key for an item this
    command filed. If the owner hand-edits such an item (tags and description
    changed, marker kept) a re-run must still find it: the fingerprint is only
    a backfill for items filed before the marker existed."""
    _seed_summary_mission(rework_db, "m-mark", "body")

    from rework.library.store import open_library_store

    assert runner.invoke(app, ["report", "m-mark", "--to-library"]).exit_code == 0

    async def hand_edit() -> str:
        index = await open_library_store(str(rework_db))
        try:
            (item,) = await index.list_items(limit=10)
            item.tags = ["handoff:report", "edited-by-owner"]
            item.description = "edited by the owner"
            await index.upsert_item(item)
            return item.id
        finally:
            await index.aclose()

    item_id = asyncio.run(hand_edit())
    dry = runner.invoke(app, ["report", "m-mark", "--to-library", "--dry-run"])
    assert "would update report" in dry.output, dry.output
    assert item_id in dry.output
    result = runner.invoke(app, ["report", "m-mark", "--to-library"])
    assert result.exit_code == 0, result.output
    (only,) = _library_items(rework_db)
    assert only.id == item_id and "mission:m-mark" in only.tags


def test_report_to_library_rerun_keeps_title_tags_and_body_file(
    rework_db: Path, library_dir: Path
) -> None:
    """A bare re-run must not revert --title or drop --tag; a re-run with a new
    --title renames the item and overwrites the SAME body file (no orphan)."""
    _seed_summary_mission(rework_db, "m-keep", "body one")
    first = runner.invoke(
        app, ["report", "m-keep", "--to-library", "--title", "Mine", "--tag", "owner-tag"]
    )
    assert first.exit_code == 0, first.output
    (item,) = _library_items(rework_db)
    body_path = item.body_path
    assert item.name == "Mine" and "owner-tag" in item.tags

    bare = runner.invoke(app, ["report", "m-keep", "--to-library"])
    assert bare.exit_code == 0, bare.output
    (again,) = _library_items(rework_db)
    assert again.name == "Mine"
    assert "owner-tag" in again.tags and "mission:m-keep" in again.tags
    assert again.body_path == body_path

    renamed = runner.invoke(app, ["report", "m-keep", "--to-library", "--title", "Renamed"])
    assert renamed.exit_code == 0, renamed.output
    (final,) = _library_items(rework_db)
    assert final.name == "Renamed" and "owner-tag" in final.tags
    assert final.body_path == body_path
    assert [p.name for p in (library_dir / "reports").glob("*.md")] == [Path(body_path).name]


def test_report_to_library_keeps_an_owner_source_tag_on_a_bare_rerun(
    rework_db: Path, library_dir: Path
) -> None:
    """The command derives one `source:<kind>` tag and replaces that tag on a
    re-run. An owner `--tag source:worktree` sits in the same namespace, names
    another source kind, and must survive a bare re-run that passes no tags."""
    _seed_summary_mission(rework_db, "m-src", "body")
    first = runner.invoke(app, ["report", "m-src", "--to-library", "--tag", "source:worktree"])
    assert first.exit_code == 0, first.output
    (item,) = _library_items(rework_db)
    assert "source:worktree" in item.tags and "source:summary" in item.tags

    bare = runner.invoke(app, ["report", "m-src", "--to-library"])
    assert bare.exit_code == 0, bare.output
    (again,) = _library_items(rework_db)
    assert "source:worktree" in again.tags, again.tags
    assert "source:summary" in again.tags, again.tags
    assert [t for t in again.tags if t.startswith("handoff:derived:")] == [
        "handoff:derived:summary"
    ]


def test_report_flags_without_to_library_are_usage_errors(
    rework_db: Path, library_dir: Path, tmp_path: Path
) -> None:
    """--from-file/--title/--tag/--dry-run do nothing without --to-library, so
    they are rejected instead of silently falling through to the plain report;
    --to-library with --md/--out is a conflict. Nothing is written either way."""
    _seed_summary_mission(rework_db, "m-flag", "body")
    doc = tmp_path / "d.md"
    doc.write_text("# d\n", encoding="utf-8")
    cases = [
        (["--from-file", str(doc)], "--from-file"),
        (["--title", "T"], "--title"),
        (["--tag", "x"], "--tag"),
        (["--dry-run"], "--dry-run"),
        (["--to-library", "--md"], "cannot be combined"),
        (["--to-library", "--out", str(tmp_path / "o.md")], "cannot be combined"),
    ]
    for args, needle in cases:
        result = runner.invoke(app, ["report", "m-flag", *args])
        assert result.exit_code != 0, args
        assert needle in result.output, (args, result.output)
    assert _library_items(rework_db) == []
    assert not (tmp_path / "o.md").exists()


# --- named connection targets (rework targets / sync-config) -----------------


TARGETS_TOML = """\
default = "rewvis"

[targets.rewvis]
db_env = "REWORK_TEST_REWVIS_DB"
daemon_url = "http://100.107.125.118:8941"
ssh = "ubuntu@100.107.125.118"

[targets.sandbox]
db = "sqlite:////tmp/rework-sandbox.db"
daemon_url = "http://127.0.0.1:8941"
ssh = "ubuntu@100.107.125.118"
"""


@pytest.fixture()
def targets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """targets.toml with a real sandbox sqlite db; monkeypatched into place."""
    from rework import targets as targets_mod

    db = tmp_path / "sandbox.db"
    db.write_bytes(b"")  # an empty file is a valid sqlite database
    path = tmp_path / "targets.toml"
    path.write_text(TARGETS_TOML.replace("sqlite:////tmp/rework-sandbox.db", f"sqlite:///{db}"))
    monkeypatch.setattr(targets_mod, "TARGETS_PATH", path)
    monkeypatch.setenv("REWORK_TEST_REWVIS_DB", "postgresql://nobody:nobody@127.0.0.1:1/nope")
    for name in ("REWORK_TARGET", "REWORK_TARGET_NAME", "REWORK_DAEMON_URL"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_targets_lists_each_target_and_survives_unreachable(
    targets_file: Path, rework_db: Path
) -> None:
    result = runner.invoke(app, ["targets"])
    if result.exit_code != 0:
        print(result.output)
    # unreachable hosts must be REPORTED, not raised: both rows printed
    assert "rewvis" in result.output and "sandbox" in result.output
    assert "UNREACHABLE" in result.output
    assert result.exit_code == 0


def test_targets_without_any_targets_file_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework import targets as targets_mod

    monkeypatch.setattr(targets_mod, "TARGETS_PATH", tmp_path / "nope.toml")
    result = runner.invoke(app, ["targets"])
    assert result.exit_code == 1
    assert "no targets configured" in result.output


def test_sync_config_merges_only_key_lines_and_never_prints_values(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    envdir = tmp_path / "state"
    envdir.mkdir(exist_ok=True)  # a test may fake two remote shapes in a row
    (envdir / "rework.env").write_text(
        "# local keys\nREWORK_FIREWORKS_KEY=supersecretvalue123\nOTHER=stay\n"
    )
    (envdir / "models.json").write_text(
        json.dumps([{"id": "flash", "provider": "fake", "model": "f", "tier": 1}])
    )
    import rework.config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_STATE_DIR", envdir)

    # (argv, stdin): the merged env travels over the ssh process's stdin, not
    # the command line (a command line is visible in `ps` on the remote host)
    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((list(cmd), kwargs.get("input")))
        if cmd[:1] == ["ssh"] and len(cmd) > 2 and cmd[2].startswith("cat"):
            stdout = "REWORK_FIREWORKS_KEY=old\nREWORK_DB=keepme\n"
            if "models.json" in cmd[2]:
                # the read completed: the end marker the command checks for
                stdout = "__REWORK_MODELS_EOF__\n"
            return type("P", (), {"stdout": stdout, "returncode": 0})()
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(app, ["sync-config", "sandbox"])
    if result.exit_code != 0:
        print(result.output)
    assert result.exit_code == 0
    out = result.output
    # key NAMES and lengths only, never values
    assert "REWORK_FIREWORKS_KEY: 19 chars" in out
    assert "supersecretvalue123" not in out
    # what was sent over ssh: keys replaced, REWORK_DB untouched, mode 600 kept
    ssh_writes = [
        (argv, stdin) for argv, stdin in calls if argv[:1] == ["ssh"] and "cat >" in argv[2]
    ]
    assert ssh_writes, "the merged rework.env was never written"
    env_writes = [(argv, stdin) for argv, stdin in ssh_writes if "rework.env" in argv[2]]
    assert env_writes, "the merged rework.env was never written"
    write_cmd, stdin = env_writes[-1]
    assert stdin is not None and "REWORK_FIREWORKS_KEY=supersecretvalue123" in str(stdin)
    assert "REWORK_DB=keepme" in str(stdin)  # remote non-KEY line preserved verbatim
    assert any("chmod 600" in part for part in write_cmd)
    # models.json is MERGED, not scp'd over: the merged JSON travels to
    # ~/.rework/models.json over ssh, and no scp of a local file happens at all
    assert not any(argv[0] == "scp" for argv, _ in calls), "models.json was still scp'd"
    models_writes = [(argv, stdin) for argv, stdin in ssh_writes if "models.json" in argv[2]]
    assert models_writes, "the merged models.json was never written"
    _, models_stdin = models_writes[-1]
    assert json.loads(str(models_stdin))[0]["id"] == "flash"


# A local catalog with one callable-from-anywhere entry, one loopback-only
# entry (the Mac-only 9router case) and one that SHARES the loopback entry's
# key. The remote catalog has one entry only the target knows.
LOCAL_MODELS = [
    {
        "id": "flash",
        "provider": "openai-compatible",
        "model": "glm-4.5-flash",
        "tier": 1,
        "base_url": "https://api.z.ai/api/coding/paas/v4",
        "api_key_env": "REWORK_FIREWORKS_KEY",
    },
    {
        "id": "router",
        "provider": "openai-compatible",
        "model": "router-model",
        "tier": 1,
        "base_url": "http://localhost:20128/v1",
        "api_key_env": "REWORK_ROUTER_KEY",
    },
    {
        "id": "router-sibling",
        "provider": "openai-compatible",
        "model": "router-sibling",
        "tier": 1,
        "base_url": "https://api.example.com/v1",
        "api_key_env": "REWORK_SIBLING_KEY",  # its own key: the router's is droppable
    },
]

REMOTE_MODELS = [
    {"id": "flash", "provider": "fake", "model": "old-flash", "tier": 0},
    {"id": "rewvis-only", "provider": "fake", "model": "rewvis-1", "tier": 2},
]


def _sync_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, remote_models: object = None
) -> tuple[list[tuple[list[str], object]], Path]:
    """Fake the whole remote side: local state dir + a fake `subprocess.run`
    that answers the read-only `cat` calls with canned remote files and records
    every (argv, stdin) pair. Hermetic: no ssh, no network, no real HOME."""
    envdir = tmp_path / "state"
    envdir.mkdir(exist_ok=True)  # a test may fake two remote shapes in a row
    (envdir / "rework.env").write_text(
        "REWORK_FIREWORKS_KEY=supersecretvalue123\n"
        "REWORK_ROUTER_KEY=routersecret\n"
        "REWORK_SIBLING_KEY=siblingsecret\n"
        "REWORK_STALE_KEY=stale\n"
    )
    (envdir / "models.json").write_text(json.dumps(LOCAL_MODELS))
    import rework.config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_STATE_DIR", envdir)

    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((list(cmd), kwargs.get("input")))
        stdout = ""
        if cmd[:1] == ["ssh"] and len(cmd) > 2:
            if "models.json" in cmd[2]:
                stdout = "" if remote_models is None else json.dumps(remote_models)
                # the read is answered like a real shell: the file (or nothing)
                # then the end-of-read marker the command checks for
                stdout += "__REWORK_MODELS_EOF__\n"
            elif "rework.env" in cmd[2]:
                stdout = "REWORK_FIREWORKS_KEY=old\nREWORK_DB=keepme\n"
        return type("P", (), {"stdout": stdout, "returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls, envdir


def _ssh_write(calls: list[tuple[list[str], object]], needle: str) -> str:
    writes = [
        stdin
        for argv, stdin in calls
        if argv[:1] == ["ssh"] and "cat >" in argv[2] and needle in argv[2]
    ]
    assert writes, f"nothing was written to {needle} over ssh"
    return str(writes[-1])


def test_sync_config_skips_loopback_entries_and_their_keys(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = _sync_env(tmp_path, monkeypatch, remote_models=REMOTE_MODELS)

    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code == 0, result.output
    out = result.output
    # the loopback entry is skipped, with its reason
    assert "skip router: loopback base_url http://localhost:20128/v1" in out
    # ...and so is the key ONLY that skipped entry uses, with its reason
    assert "skip REWORK_ROUTER_KEY: used only by skipped entries" in out
    env_written = _ssh_write(calls, "rework.env")
    assert "REWORK_ROUTER_KEY" not in env_written, "a key only skipped entries use was sent"
    assert "REWORK_FIREWORKS_KEY=supersecretvalue123" in env_written
    assert "REWORK_DB=keepme" in env_written  # remote line survives
    # no secret value is ever printed
    assert "supersecretvalue123" not in out and "routersecret" not in out
    # the merged catalog: loopback entry gone, target-only entry kept, and the
    # local entry replaced the target's same-id one
    merged = json.loads(_ssh_write(calls, "models.json"))
    ids = [e["id"] for e in merged]
    assert "router" not in ids
    assert ids == ["flash", "router-sibling", "rewvis-only"]
    assert merged[0]["model"] == "glm-4.5-flash"  # local wins on id
    assert "  keep    rewvis-only: only on the target" in out
    assert "  replace flash:" in out and "  add     router-sibling:" in out
    assert not any(argv[0] == "scp" for argv, _ in calls)


def test_sync_config_keeps_a_target_entry_containing_the_marker_token(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is appended LAST by the remote shell, so it must be stripped
    from the TAIL. A target entry whose text happens to contain the token
    (here a "note") is legitimate: a head-anchored split truncates the document
    there, the remainder fails json.loads, the merge then treats the target as
    empty and `cat >` drops every target-only entry."""
    remote = [
        *REMOTE_MODELS,
        {
            "id": "tokens-holder",
            "provider": "fake",
            "model": "t",
            "tier": 1,
            "note": "echo __REWORK_MODELS_EOF__ appears in a string",
        },
    ]
    calls, _ = _sync_env(tmp_path, monkeypatch, remote_models=remote)

    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code == 0, result.output
    # the target's catalog parsed, so no "treating it as empty" warning
    assert "not valid JSON" not in result.output
    assert "keep    tokens-holder: only on the target" in result.output

    payload = _ssh_write(calls, "models.json")
    merged = json.loads(payload)
    ids = [entry["id"] for entry in merged]
    assert ids == ["flash", "router-sibling", "rewvis-only", "tokens-holder"]
    holder = next(entry for entry in merged if entry["id"] == "tokens-holder")
    assert holder["note"] == "echo __REWORK_MODELS_EOF__ appears in a string"
    # the same payload round-trips through the loader's entry parser
    assert [entry["id"] for entry in _models_doc_entries(merged)] == ids
    written = tmp_path / "token-models.json"
    written.write_text(payload)
    monkeypatch.setenv("REWORK_MODELS", str(written))
    from rework.config import load_catalog

    assert [e.id for e in load_catalog().all()] == ids


def test_sync_config_writes_nothing_when_the_remote_read_fails(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ssh read that never completed must not be mistaken for an empty
    target catalog: the merge would then drop the target's own entries and
    `cat >` would REPLACE the remote models.json with local-only ones. The
    read carries an end marker only a finished shell run can produce, so a
    missing marker aborts the whole write phase."""
    calls, _ = _sync_env(tmp_path, monkeypatch, remote_models=REMOTE_MODELS)

    def dead_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((list(cmd), kwargs.get("input")))
        # unreachable host: ssh exits non-zero and prints a diagnostic, and the
        # shell never ran, so the marker is absent (this is what ssh_run returns)
        stdout = "ssh: connect to host 100.107.125.118 port 22: No route to host\n"
        return type("P", (), {"stdout": stdout, "returncode": 255})()

    monkeypatch.setattr("subprocess.run", dead_run)
    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code != 0, "a failed remote read must exit non-zero"
    assert "__REWORK_MODELS_EOF__" in result.output or "did not complete" in result.output
    assert "100.107.125.118" in result.output  # names the target
    assert "nothing was written" in result.output
    writes = [
        argv
        for argv, _ in calls
        if argv[0] == "scp" or (argv[0] == "ssh" and ("cat >" in argv[2] or "chmod" in argv[2]))
    ]
    assert not writes, f"a failed remote read still wrote to the target: {writes}"

    # --dry-run must report the same problem, not crash, and still write nothing
    calls2, _ = _sync_env(tmp_path, monkeypatch, remote_models=REMOTE_MODELS)
    monkeypatch.setattr("subprocess.run", dead_run)
    result = runner.invoke(app, ["sync-config", "sandbox", "--dry-run"])
    assert result.exit_code != 0
    assert "nothing was written" in result.output
    assert not [a for a, _ in calls2 if a[0] == "scp" or (a[0] == "ssh" and "cat >" in a[2])]

    # ...while a SUCCESSFUL read of an absent file still writes the local
    # catalog (empty target: no file, only the marker)
    calls3, _ = _sync_env(tmp_path, monkeypatch, remote_models=None)
    ok = runner.invoke(app, ["sync-config", "sandbox"])
    assert ok.exit_code == 0, ok.output
    merged = json.loads(_ssh_write(calls3, "models.json"))
    assert [e["id"] for e in merged] == ["flash", "router-sibling"]


def test_sync_config_keeps_a_key_a_kept_entry_also_uses(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the key rule: a key the skipped loopback entry uses is
    still needed on the target when a KEPT entry needs it too, so it must be
    sent and must NOT be reported as skipped."""
    calls, envdir = _sync_env(tmp_path, monkeypatch, remote_models=None)
    shared = [
        {**e, "api_key_env": "REWORK_ROUTER_KEY"} if e["id"] == "router-sibling" else e
        for e in LOCAL_MODELS
    ]
    (envdir / "models.json").write_text(json.dumps(shared))

    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code == 0, result.output
    assert "skip router: loopback base_url http://localhost:20128/v1" in result.output
    assert "REWORK_ROUTER_KEY: used only by skipped entries" not in result.output
    env_written = _ssh_write(calls, "rework.env")
    assert "REWORK_ROUTER_KEY=routersecret" in env_written


def test_sync_config_never_writes_a_models_json_the_loader_cannot_read(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload actually written must PARSE through the loader.

    An object-shaped remote document ({"version": 2, "entries": [...]}) is not
    readable by rework at all: ModelCatalog.from_config runs ModelEntry(**item)
    over the raw document, so preserving that shape would hand the target a
    catalog that raises TypeError. So the sync writes the flat list, says why,
    and the target keeps its own entries in the merge. Asserting the payload
    parses (not merely that it is JSON) is the point: json.loads alone passed
    while the file was unloadable."""
    from rework.config import load_catalog

    calls, _ = _sync_env(
        tmp_path, monkeypatch, remote_models={"version": 2, "entries": REMOTE_MODELS}
    )
    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code == 0, result.output
    assert "not an entry list the loader can read" in result.output

    payload = _ssh_write(calls, "models.json")
    assert isinstance(json.loads(payload), list), "the object shape must not survive"

    # the REAL proof: feed the exact payload to the loader the daemon uses
    written = tmp_path / "written-models.json"
    written.write_text(payload)
    monkeypatch.setenv("REWORK_MODELS", str(written))
    catalog = load_catalog()
    assert [e.id for e in catalog.all()] == ["flash", "router-sibling", "rewvis-only"]
    assert catalog.resolve("flash").model == "glm-4.5-flash"  # local wins on id

    # a malformed remote document: reported, and still written as a loadable list
    calls2, _ = _sync_env(tmp_path, monkeypatch, remote_models=None)

    def bad_run(cmd: list[str], **kwargs: object) -> object:
        calls2.append((list(cmd), kwargs.get("input")))
        stdout = (
            "{not json__REWORK_MODELS_EOF__\n"
            if cmd[:1] == ["ssh"] and "models.json" in cmd[2]
            else ""
        )
        return type("P", (), {"stdout": stdout, "returncode": 0})()

    monkeypatch.setattr("subprocess.run", bad_run)
    result = runner.invoke(app, ["sync-config", "sandbox"])
    assert result.exit_code == 0, result.output  # a warning, never a crash
    assert "not valid JSON" in result.output
    payload2 = _ssh_write(calls2, "models.json")
    assert [e["id"] for e in json.loads(payload2)] == ["flash", "router-sibling"]
    written.write_text(payload2)
    monkeypatch.setenv("REWORK_MODELS", str(written))
    assert [e.id for e in load_catalog().all()] == ["flash", "router-sibling"]


def test_sync_config_dry_run_prints_the_same_plan_and_writes_nothing(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = _sync_env(tmp_path, monkeypatch, remote_models=REMOTE_MODELS)

    result = runner.invoke(app, ["sync-config", "sandbox", "--dry-run"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "merge models.json" in out
    assert "skip router: loopback base_url http://localhost:20128/v1" in out
    assert "skip REWORK_ROUTER_KEY: used only by skipped entries" in out
    assert "dry run: nothing was sent" in out
    assert "supersecretvalue123" not in out
    # dry-run may READ the remote files (the read-only cat, to know add vs
    # replace) but must never WRITE anything: no heredoc/cat>, no chmod, no scp
    writes = [
        argv
        for argv in [a for a, _ in calls]
        if argv[0] == "scp" or (argv[0] == "ssh" and ("cat >" in argv[2] or "chmod" in argv[2]))
    ]
    assert not writes, f"dry run wrote to the remote host: {writes}"

    # the plan is byte-identical to the real run's plan lines
    real, _ = _sync_env(tmp_path, monkeypatch, remote_models=REMOTE_MODELS)
    real_out = runner.invoke(app, ["sync-config", "sandbox"]).output
    plan = lambda text: [  # noqa: E731
        line
        for line in text.splitlines()
        if line.startswith(("merge ", "  add", "  replace", "  keep", "  skip"))
    ]
    assert plan(out) == plan(real_out), "dry run and real run printed different plans"
    assert real, "the real run must write"


def test_sync_config_requires_an_ssh_host(targets_file: Path) -> None:
    path = targets_file
    path.write_text('[targets.localonly]\ndb = "sqlite:///x.db"\ndaemon_url = "http://h:1"\n')
    result = runner.invoke(app, ["sync-config", "localonly"])
    assert result.exit_code == 1
    assert "no `ssh` host" in result.output


def test_sync_config_unknown_target_errors(targets_file: Path) -> None:
    result = runner.invoke(app, ["sync-config", "ghost"])
    assert result.exit_code == 1
    assert "no target named" in result.output


# --- main()'s own dispatch, END TO END --------------------------------------
# The audit's root cause: CliRunner tests bypass main() entirely, so
# REWORK_TARGET / default selection and the --target argv handling there were
# never exercised. These run the real console entrypoint (rework.cli:main) in
# a subprocess with a temp HOME + REWORK_TARGETS, and cover exactly the cases
# the audit probed live.

MAIN_TARGETS_TOML = """\
default = "sandbox"

[targets.sandbox]
db = "sqlite:////tmp/probe/sandbox.db"
daemon_url = "http://127.0.0.1:8941"

[targets.rewvis]
db = "postgresql://nobody:nobody@127.0.0.1:1/nope"
daemon_url = "http://100.107.125.118:8941"
"""


def _run_main(
    argv: list[str], home: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # `python -m rework ...`: the same main() the console entrypoint runs
    env = {
        **os.environ,
        "HOME": str(home),
        "REWORK_TARGETS": str(home / "targets.toml"),
        "PYTHONPATH": "src",
        "REWORK_LOG_DIR": str(home / "logs"),
        "REWORK_DB": "",  # never inherited: selection must come from the target
    }
    env = {k: v for k, v in env.items() if v != "" or k == "REWORK_DB"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(Path(sys.executable).parent / "rework"), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


@pytest.fixture()
def main_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".rework").mkdir(parents=True)
    (home / "targets.toml").write_text(
        MAIN_TARGETS_TOML.replace("/tmp/probe/sandbox.db", str(tmp_path / "sandbox.db"))
    )
    (tmp_path / "sandbox.db").write_bytes(b"")  # a valid empty sqlite database
    return home


def _probe_env_in_child(home: Path, env_extra: dict[str, str]) -> str:
    """Print the child process's resolved REWORK_* env (one per line) after a
    real main() dispatch. Resolution lives in main() itself, so the probe has
    to RUN main() (sys.argv = ['rework', 'version']) -- a bare `python -c`
    would print the raw inherited env and prove nothing. `version`'s own
    output is swallowed (redirect_stdout) so only the two probe lines reach
    stdout. `version` never READS REWORK_DB, so exit codes alone cannot catch
    a regression where resolution stops applying env -- this probe can."""
    script = (
        "import contextlib, io, os, sys\n"
        "from rework.cli import main\n"
        "sys.argv = ['rework', 'version']\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    try:\n"
        "        main()\n"
        "    except SystemExit as exc:\n"
        "        if exc.code not in (0, None):\n"
        "            raise\n"
        "print(os.environ.get('REWORK_DB'))\n"
        "print(os.environ.get('REWORK_TARGET_NAME'))\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "REWORK_TARGETS": str(home / "targets.toml"),
        "PYTHONPATH": "src",
        "REWORK_LOG_DIR": str(home / "logs"),
    }
    # never inherited: selection must come from the target (env_extra).
    # REWORK_TARGETS (the file PATH) is kept -- only the resolved values are
    # stripped, and only by exact name (a startswith would also drop
    # REWORK_TARGETS and the probe would silently read the wrong file).
    for name in ("REWORK_DB", "REWORK_TARGET", "REWORK_TARGET_NAME"):
        env.pop(name, None)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_main_rework_target_env_selects_end_to_end(main_home: Path) -> None:
    # BLOCKER 1: bare `rework` with only REWORK_TARGET set must still resolve
    # (the pre-fix bug: main() resolved targets only when "--target" was in argv)
    result = _run_main(["version"], main_home, {"REWORK_TARGET": "rewvis"})
    # rewvis IS defined (postgres, unreachable — but selection is not reachability):
    # resolution must succeed and the command must run
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rework ")
    # not just exit 0: the resolution must actually APPLY env (a command like
    # `version` never reads REWORK_DB, so only a probe proves this end to end)
    db, name = _probe_env_in_child(main_home, {"REWORK_TARGET": "rewvis"}).splitlines()
    assert db == "postgresql://nobody:nobody@127.0.0.1:1/nope"  # rewvis's db_env value
    assert name == "rewvis"
    ok = _run_main(["targets"], main_home, {"REWORK_TARGET": "sandbox"})
    assert ok.returncode == 0
    assert "sandbox" in ok.stdout
    # the sandbox target's sqlite:// URL form is normalized to the plain path
    db, name = _probe_env_in_child(main_home, {"REWORK_TARGET": "sandbox"}).splitlines()
    assert db == str(main_home.parent / "sandbox.db"), db
    assert name == "sandbox"
    # an unknown REWORK_TARGET is the error case: exit 2, on stderr
    bad = _run_main(["version"], main_home, {"REWORK_TARGET": "ghost"})
    assert bad.returncode == 2
    assert "no target named 'ghost'" in bad.stderr


def test_main_default_only_selection_reaches_subcommands(main_home: Path) -> None:
    # BLOCKER 1: no flag, no REWORK_TARGET: the file's `default = "sandbox"`
    # applies to any subcommand, and an unknown DEFAULT never kills the CLI
    result = _run_main(["version"], main_home, {})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rework ")
    # the default target must actually APPLY env, not just exit 0 (`version`
    # never reads REWORK_DB; the probe proves resolution happened)
    db, name = _probe_env_in_child(main_home, {}).splitlines()
    assert db == str(main_home.parent / "sandbox.db")  # plain path, URL normalized
    assert name == "sandbox"
    # and `rework --target ghost` IS an error (explicit names are checked)
    bad = _run_main(["--target", "ghost", "version"], main_home, {})
    assert bad.returncode == 2
    assert "no target named 'ghost'" in bad.stderr


def test_main_target_equals_form_and_missing_name(main_home: Path) -> None:
    # BLOCKER 2a: `--target=NAME` works end to end; 2c: a NAME that looks like
    # a flag or is a subcommand is a usage error, not a target name
    ok = _run_main(["--target=sandbox", "version"], main_home, {})
    assert ok.returncode == 0, ok.stderr
    for argv in (["--target", "--help"], ["--target=", "version"], ["--target"]):
        bad = _run_main(argv, main_home, {})
        assert bad.returncode == 2, (argv, bad.stderr)
        assert "needs a name" in bad.stderr


def test_main_target_after_subcommand_is_rejected_before_applying(
    main_home: Path,
) -> None:
    # BLOCKER 2b: after the subcommand --target is not a global option: a
    # usage error, exit 2, and (checked in test_main_flag_never_mutates_env)
    # no env mutation before the rejection
    for argv in (["ls", "--target", "sandbox"], ["ls", "--target=sandbox"]):
        bad = _run_main(argv, main_home, {"REWORK_TEST_MARK": "1"})
        assert bad.returncode == 2, (argv, bad.stdout, bad.stderr)
        assert "must come before the subcommand" in bad.stderr


def test_main_flag_never_mutates_env_on_error(main_home: Path) -> None:
    # the wrong-target guard: a rejected invocation writes NOTHING to the env.
    # Prove it by re-running `version` as a child with a snapshot comparison:
    # `python -m rework --version` prints the version WITHOUT resolution side
    # effects, so instead compare against a direct python -c run of main().
    probe = (
        "import os, sys; sys.argv=['rework','ls','--target','sandbox'];"
        "from rework.cli import main;\n"
        "try:\n    main()\nexcept SystemExit:\n    pass\n"
        "print('REWORK_DB' in os.environ, 'REWORK_TARGET_NAME' in os.environ)"
    )
    env = {
        **os.environ,
        "HOME": str(main_home),
        "REWORK_TARGETS": str(main_home / "targets.toml"),
        "PYTHONPATH": "src",
        "REWORK_LOG_DIR": str(main_home / "logs"),
    }
    env = {k: v for k, v in env.items() if not k.startswith("REWORK_DB")}
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.stdout.strip() == "False False", result.stdout + result.stderr


def test_main_stderr_and_exit_code_are_consistent(main_home: Path) -> None:
    # BLOCKER 2d: one error shape — all --target errors on stderr, exit 2
    for argv, needle in (
        (["--target", "ghost", "version"], "no target named 'ghost'"),
        (["--target", "rewvis", "version"], None),  # postgres target is fine to SELECT
    ):
        result = _run_main(argv, main_home, {})
        if needle is None:
            assert result.returncode == 0, result.stderr
            assert result.stdout.startswith("rework ")
            continue
        assert result.returncode == 2
        assert needle in result.stderr
        assert needle not in result.stdout


def test_new_with_priority_high_and_prioritize_command(rework_db: Path) -> None:
    """(d) `new --priority high` and `prioritize` both flip the stored level."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "urgent fix", "--repo", url, "--priority", "high"])
    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None and mission.priority == "high"

    plain = runner.invoke(app, ["new", "a later fix", "--repo", url])
    assert plain.exit_code == 0, plain.output
    other = _get_mission(rework_db, plain.stdout.strip())
    assert other is not None and other.priority == "normal"

    # focusing a mission is a flag: default high, --priority normal goes back
    focused = runner.invoke(app, ["prioritize", other.id])
    assert focused.exit_code == 0, focused.output
    assert focused.output.strip() == f"{other.id}: priority high"
    assert (_get_mission(rework_db, other.id) or other).priority == "high"

    back = runner.invoke(app, ["prioritize", other.id, "--priority", "normal"])
    assert back.exit_code == 0, back.output
    assert (_get_mission(rework_db, other.id) or other).priority == "normal"

    bad = runner.invoke(app, ["prioritize", other.id, "--priority", "urgent"])
    assert bad.exit_code != 0


def test_new_with_unknown_priority_is_a_clean_cli_error(rework_db: Path) -> None:
    """BLOCKER: `new --priority urgent` exits non-zero with a clear message, not
    an unhandled ValueError traceback out of typer."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "urgent fix", "--repo", url, "--priority", "urgent"])
    assert result.exit_code != 0
    output = result.output + (result.stderr if result.stderr else "")
    assert "unknown priority 'urgent'; priorities: normal, high" in output
    assert "Traceback" not in output
    assert "ValueError" not in output

    # and nothing was stored
    async def all_missions() -> list[Mission]:
        store = await open_sqlite(str(rework_db))
        try:
            return await store.list_missions()
        finally:
            await store.aclose()

    assert asyncio.run(all_missions()) == []


# --- main()'s dispatch of the help flags ------------------------------------
# `rework --help` / `rework -h` used to open the TUI: parse_target_flag dropped
# the leading flag, so main() saw an empty rest and took its "bare rework"
# branch before typer ever ran. CliRunner invokes app() directly and cannot see
# that dispatch, so these call the real main() with run_tui monkeypatched to
# fail loudly instead of opening a dashboard.


@pytest.fixture()
def dispatched_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME/state dir for a real main() run, with no targets.toml.

    The REWORK_* vars resolve_target would setdefault are pinned to empty
    strings first: on an empty value setdefault leaves them alone, and
    monkeypatch restores whatever the session had before.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REWORK_TARGETS", str(home / "targets.toml"))
    # TARGETS_PATH is read from the env at import time, so the env var alone
    # does not redirect an already-imported rework.targets: patch the constant.
    monkeypatch.setattr("rework.targets.TARGETS_PATH", home / "targets.toml")
    monkeypatch.setenv("REWORK_ENV_FILE", str(home / "rework.env"))
    monkeypatch.setenv("REWORK_LOG_DIR", str(home / "logs"))
    monkeypatch.delenv("REWORK_TARGET", raising=False)
    for name in (
        "REWORK_DB",
        "REWORK_DAEMON_URL",
        "REWORK_LIBRARY_DIR",
        "REWORK_TARGET_NAME",
        "REWORK_CHECKPOINTS_DB",
    ):
        monkeypatch.setenv(name, "")
    return home


def _tui_must_not_start(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("run_tui() was called: this argv belongs to the CLI, not the TUI")


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_main_help_flag_prints_help_and_does_not_start_the_tui(
    flag: str,
    dispatched_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BLOCKER: `rework --help` and `rework -h` print typer help, exit 0, no TUI."""
    import rework.tui as tui
    from rework.cli import main

    monkeypatch.setattr(tui, "run_tui", _tui_must_not_start)
    monkeypatch.setattr(sys, "argv", ["rework", flag])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    # launcher-agnostic: click's "Usage: <prog>" prefix is derived from
    # sys.modules["__main__"].__package__, so under `python -m pytest` it reads
    # "Usage: python -m pytest.rework". Assert the usage header plus a real help
    # body line, so an error panel cannot satisfy it either.
    assert "Usage:" in out
    assert "COMMAND" in out
    assert "--target" in out  # the real help body, not an error panel
    assert "No such option" not in out


def test_main_bare_and_target_only_argv_still_open_the_tui(
    dispatched_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the TUI path keeps working for a bare `rework` and `--target NAME`."""
    import rework.tui as tui
    from rework.cli import main

    (dispatched_home / "targets.toml").write_text(
        "[targets.sandbox]\n"
        f'db = "sqlite:///{dispatched_home / "sandbox.db"}"\n'
        'daemon_url = "http://127.0.0.1:8941"\n'
    )

    calls: list[str] = []

    def fake_run_tui() -> None:
        calls.append("started")

    monkeypatch.setattr(tui, "run_tui", fake_run_tui)

    for argv in (["rework"], ["rework", "--target", "sandbox"]):
        calls.clear()
        monkeypatch.setattr(sys, "argv", argv)
        main()
        assert calls == ["started"], argv


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_main_help_flag_before_a_subcommand_still_prints_help(
    flag: str,
    dispatched_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REGRESSION: `rework --help update --target rewvis` is still a help request.

    Leading flags are now KEPT in rest, so `_rest[0]` can be a flag: keying the
    `update` exemption off it read the subcommand as "--help", fired the --target
    usage-error guard, and turned a help request that printed help at base
    eb7556c into a red usage error. The command name must come from the first
    non-flag token.
    """
    import rework.tui as tui
    from rework.cli import main

    monkeypatch.setattr(tui, "run_tui", _tui_must_not_start)
    monkeypatch.setattr(sys, "argv", ["rework", flag, "update", "--target", "rewvis"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out  # launcher-agnostic: see the test above
    assert "COMMAND" in captured.out
    assert "needs a name" not in captured.out
    assert "needs a name" not in captured.err


def test_main_update_target_stays_exempt_from_the_guard(
    dispatched_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Positive control: `rework update --target NAME` is still exempt.

    The update command owns its own --target, so the guard must not fire: the
    command itself runs and reports the unknown target (exit 1), never the
    "needs a name" usage error (exit 2).
    """
    import rework.tui as tui
    from rework.cli import main

    monkeypatch.setattr(tui, "run_tui", _tui_must_not_start)
    monkeypatch.setattr(sys, "argv", ["rework", "update", "--target", "rewvis"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "no target named 'rewvis'" in captured.err
    assert "needs a name" not in captured.err


class _StopLogging(Exception):
    """Sentinel: the logging branch ran, so stop main() before the typer app."""


def _logging_spy(calls: list[str]):
    """A setup_logging stand-in that records its component and halts main().

    main() imports setup_logging inside the function, so patching the module
    attribute is what it resolves; raising the sentinel keeps the test away
    from app() (and therefore from any daemon or TUI start).
    """

    def spy(component: str, **_kwargs: object) -> None:
        calls.append(component)
        raise _StopLogging

    return spy


@pytest.mark.parametrize("argv", [["rework", "--", "serve"], ["rework", "--", "logs"]])
def test_main_double_dash_shapes_take_the_logging_path(
    argv: list[str],
    dispatched_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: `rework -- serve` / `rework -- logs` still log like base.

    `--` is the end-of-options marker, so `serve` is a POSITIONAL and not the
    subcommand: the "serve/logs/library-mcp already log themselves" exemption
    must not fire and the CLI logging path must run. Reading rest[0] as the
    subcommand skipped setup_logging("cli") for exactly these shapes.
    """
    import rework.logs
    import rework.tui as tui
    from rework.cli import main

    calls: list[str] = []
    monkeypatch.setattr(rework.logs, "setup_logging", _logging_spy(calls))
    monkeypatch.setattr(tui, "run_tui", _tui_must_not_start)
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(_StopLogging):
        main()

    assert calls == ["cli"], argv  # exactly once, and for the CLI component


def test_main_exempt_subcommand_skips_the_cli_logging(
    dispatched_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complement: `rework serve` does NOT take the CLI logging branch.

    `serve` IS the subcommand here, and it configures its own ("daemon")
    logging, so main() must leave setup_logging alone and go straight to the
    app. The spy raises on its first call, which stops main() inside the serve
    command body, before the daemon can boot: the recorded call list therefore
    shows whose logging ran (serve's own, never "cli").
    """
    import rework.logs
    import rework.tui as tui
    from rework.cli import main

    calls: list[str] = []
    monkeypatch.setattr(rework.logs, "setup_logging", _logging_spy(calls))
    monkeypatch.setattr(tui, "run_tui", _tui_must_not_start)
    monkeypatch.setattr(sys, "argv", ["rework", "serve"])

    with pytest.raises(_StopLogging):
        main()

    # exactly serve's own call: main() did not log for "cli" first
    assert calls == ["daemon"]


def test_main_target_after_a_subcommand_still_errors(
    dispatched_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Positive control: the guard itself is unchanged for `rework ls --target sandbox`."""
    from rework.cli import main

    monkeypatch.setattr(sys, "argv", ["rework", "ls", "--target", "sandbox"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "must come before the subcommand" in capsys.readouterr().err


def test_message_publishes_an_owner_message(rework_db: Path) -> None:
    """`rework message ID TEXT` appends an OWNER_MESSAGE for that mission;
    an unknown id fails loudly and publishes nothing."""
    from rework.kernel.state.records import EventType

    async def seed() -> None:
        store = await open_sqlite(str(rework_db))
        try:
            await store.create_mission(Mission(id="m-msg", objective="talk to me"))
        finally:
            await store.aclose()

    async def owner_texts(mission_id: str) -> list[str]:
        store = await open_sqlite(str(rework_db))
        try:
            events = await store.events_since(0, mission_id=mission_id)
            return [e.payload["text"] for e in events if e.type == EventType.OWNER_MESSAGE]
        finally:
            await store.aclose()

    asyncio.run(seed())
    result = runner.invoke(app, ["message", "m-msg", "please also add tests"])
    assert result.exit_code == 0, result.output
    assert asyncio.run(owner_texts("m-msg")) == ["please also add tests"]

    missing = runner.invoke(app, ["message", "m-nope", "hello"])
    assert missing.exit_code != 0
    assert asyncio.run(owner_texts("m-nope")) == []


def test_new_estimates_the_budget_and_keeps_stdout_the_bare_mission_id(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --budget: the cap follows the task's size, the reason goes to stderr
    so stdout stays one machine-readable line (the mission id)."""
    monkeypatch.delenv("REWORK_BUDGET_USD", raising=False)
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "fix the typo in the readme", "--repo", url, "--plan", "fix"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("m-")  # stdout is the bare id
    assert "budget: $1.00 (docs/typo-sized change" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    budget = (mission.plan or {})["budget"]
    assert budget["max_usd"] == 1.0
    assert budget["original_max_usd"] == 1.0


def test_new_explicit_budget_wins_over_the_estimate(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REWORK_BUDGET_USD", raising=False)
    url = "https://github.com/example/repo.git"
    # a migration would be estimated at >= $8; --budget 7 must win
    result = runner.invoke(app, ["new", "migrate the schema", "--repo", url, "--budget", "7"])

    assert result.exit_code == 0, result.output
    assert "budget: $7.00 (from --budget)" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["budget"]["max_usd"] == 7.0
    assert (mission.plan or {})["budget"]["original_max_usd"] == 7.0


def test_new_estimates_over_the_stale_env_default(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that still carries the OLD blanket default ($2) must not
    flatten every `rework new` back to $2: the estimate wins, the env var is
    only the fallback for a mission with no task text to size."""
    monkeypatch.setenv("REWORK_BUDGET_USD", "2")
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "migrate the schema", "--repo", url, "--plan", "fix"])

    from rework.budget_policy import estimate_budget

    estimated, why = estimate_budget("migrate the schema")
    assert estimated > 2.0, "the estimate must differ from the stale default"
    assert result.exit_code == 0, result.output
    assert f"budget: ${estimated:.2f} ({why})" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["budget"]["max_usd"] == estimated


def test_new_without_task_text_falls_back_to_the_env_default(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to size: the deployment default is still the last word."""
    monkeypatch.setenv("REWORK_BUDGET_USD", "4")
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "  ", "--repo", url, "--plan", "fix"])

    assert result.exit_code == 0, result.output
    assert "budget: $4.00 (REWORK_BUDGET_USD" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["budget"]["max_usd"] == 4.0


def test_new_is_sized_from_what_finished_missions_really_cost(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calibration half of the estimate: two finished missions that really
    cost ~$3 each pull a long feature brief from the band's $5 down to $4.50 on
    the CLI path, which is what makes "sized from the charges table" true."""
    from rework.kernel.state.records import Charge

    monkeypatch.delenv("REWORK_BUDGET_USD", raising=False)

    async def seed() -> None:
        store = await open_sqlite(str(rework_db))
        try:
            for mid, cost in (("m-done1", 2.0), ("m-done2", 4.0)):
                await store.create_mission(
                    Mission(id=mid, objective="earlier mission", status=MissionStatus.DONE)
                )
                await store.charge(
                    Charge(
                        mission_id=mid,
                        model="m",
                        input_tokens=10,
                        output_tokens=10,
                        cost_usd=cost,
                    )
                )
        finally:
            await store.aclose()

    asyncio.run(seed())
    url = "https://github.com/example/repo.git"
    brief = "add a feature to the parser, " * 20  # > 300 chars: the top of the band
    result = runner.invoke(app, ["new", brief, "--repo", url, "--plan", "fix"])

    assert result.exit_code == 0, result.output
    assert "budget: $4.50 (normal feature-sized change" in result.stderr
    assert "typical finished-mission cost $3.00" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["budget"]["max_usd"] == 4.5


@pytest.mark.parametrize("value", ["nan", "inf", "-3", "0"])
def test_new_refuses_a_cap_that_could_never_bind(
    value: str, rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--budget nan` used to print `budget: $nan (from --budget)`, create the
    mission and persist max_usd = nan — and `spent_usd > nan` is False for
    ever, so the mission spent with NO cap at all. `-3` was already spent
    through. Now the CLI refuses before anything is written."""
    monkeypatch.delenv("REWORK_BUDGET_USD", raising=False)
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "fix the typo", "--repo", url, "--budget", value])

    assert result.exit_code != 0, result.output
    assert result.exit_code == 2, "a typer.BadParameter is a usage error"
    # THE ERROR ITSELF must name the flag. Asserting on the whole output was
    # vacuous: stderr also carries `budget: $nan (from --budget)`, and the
    # backend sink (`budget_rail`/`create_mission`) refuses the same value with
    # the same BUDGET_ERROR and exit 2 — so the test stayed green with this
    # guard deleted. `_opening_budget`'s message is the one that says which
    # option to fix, and only the CLI's own check produces it.
    error = next((line for line in result.output.splitlines() if "Invalid value" in line), "")
    assert f"--budget {value}:" in error, error
    assert "finite number greater than 0" in error
    assert "Traceback" not in result.output
    assert result.stdout.strip().startswith("m-") is False, "no mission id may be printed"
    assert _missions(rework_db) == [], "no mission may be created"


@pytest.mark.parametrize("value", ["inf", "nan", "-3", "0"])
def test_new_ignores_a_poisoned_env_default_for_an_empty_objective(
    value: str, rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No task text: REWORK_BUDGET_USD is the fallback, and `inf` there used to
    be persisted as the cap (and as original_max_usd). An unusable value falls
    back to the normal default instead."""
    monkeypatch.setenv("REWORK_BUDGET_USD", value)
    monkeypatch.delenv("REWORK_BUDGET_STEP_USD", raising=False)
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "  ", "--repo", url, "--plan", "fix"])

    assert result.exit_code == 0, result.output
    assert "budget: $2.00 (" in result.stderr
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    budget = (mission.plan or {})["budget"]
    assert budget["max_usd"] == 2.0
    assert budget["original_max_usd"] == 2.0


def test_template_plan_carries_an_explicit_budget(rework_db: Path) -> None:
    """`--plan fix --mechanical` used to drop --budget on the floor: the cap
    only reached plan['budget'] through orchestrated_plan."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "fix it", "--repo", url, "--plan", "fix", "--mechanical", "--budget", "4"]
    )
    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    budget = (mission.plan or {})["budget"]
    assert budget["max_usd"] == 4.0
    assert budget["original_max_usd"] == 4.0
    assert budget["auto_raises"] == 0
    assert budget["raises"] == []


def test_template_plan_without_a_budget_gets_no_budget_through_the_cli(
    rework_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No regression: a template mission created WITHOUT a cap keeps no
    plan['budget'], because engine._build_graph would then enforce one and the
    mission would pause at a burn gate it never had.

    Through the CLI on purpose: the estimate is for the orchestrated default
    path only, so `--plan fix --mechanical` without --budget must stay
    uncapped — and must not CLAIM a budget on stderr either."""
    monkeypatch.delenv("REWORK_BUDGET_USD", raising=False)
    url = "https://github.com/example/repo.git"
    for extra in (["--plan", "fix"], ["--plan", "fix-pr"], ["--plan", "echo"]):
        result = runner.invoke(app, ["new", "fix it", "--repo", url, "--mechanical", *extra])
        assert result.exit_code == 0, result.output
        mission = _get_mission(rework_db, result.stdout.strip())
        assert mission is not None
        assert "budget" not in (mission.plan or {}), extra
        assert "no cap" in result.stderr, extra
        assert "$" not in result.stderr, extra


def test_new_reads_a_read_and_report_objective_as_draft(rework_db: Path) -> None:
    """An audit's deliverable is the report, not a branch and a PR, so with no
    --plan the CLI defaults the mission to the `draft` playbook and says so on
    stderr (stdout stays the bare mission id)."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "audit the release process", "--repo", url])

    assert result.exit_code == 0, result.output
    mission_id = result.stdout.strip()
    assert mission_id.startswith("m-"), "stdout is still the bare mission id"
    mission = _get_mission(rework_db, mission_id)
    assert mission is not None
    plan = mission.plan or {}
    assert plan["playbook"] == "draft"
    assert not plan.get("publish"), "a draft plan never pushes a branch"
    assert "open_pr" not in (plan.get("gates") or []), "a draft plan never opens a PR gate"
    assert "read-and-report objective" in result.stderr
    assert "draft" in result.stderr


def test_an_explicit_plan_beats_the_read_and_report_default(rework_db: Path) -> None:
    """The classifier only picks a DEFAULT: `--plan fix` on the same objective
    has to stay fix, and the note must not claim a plan the owner did not get."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "audit the release process", "--repo", url, "--plan", "fix"]
    )

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["playbook"] == "fix"
    assert "read-and-report objective" not in result.stderr


def test_an_explicit_echo_plan_is_never_overridden_by_the_classifier(rework_db: Path) -> None:
    """`--plan echo` is the plumbing check: an audit-shaped objective must not
    turn it into a draft mission behind the owner's back."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(
        app, ["new", "audit the release process", "--repo", url, "--plan", "echo"]
    )

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["playbook"] == "echo"
    assert "read-and-report objective" not in result.stderr


def test_a_repo_less_audit_keeps_the_callers_plan_and_still_succeeds(rework_db: Path) -> None:
    """`draft` cannot be built without a repository, so the classifier must not
    suggest it when none is being resolved: `rework new "audit the release
    process"` with no --repo succeeded on base (an echo mission) and has to keep
    doing so, instead of dying with a usage error about --plan draft."""
    result = runner.invoke(app, ["new", "audit the release process"])

    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.stdout.strip())
    assert mission is not None
    assert (mission.plan or {})["playbook"] == "echo"
    assert "read-and-report objective" not in result.stderr
