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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rework import __version__
from rework.cli import app
from rework.kernel.state.records import Gate, GateKind, GateStatus, Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite

runner = CliRunner()


@pytest.fixture(autouse=True)
def rework_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point REWORK_DB at a tmp sqlite file for every test in this module.

    Nothing else sets it (tests/conftest.py has no autouse REWORK_DB fixture),
    so without this the commands would hit the real ~/.rework/state.sqlite3.
    """
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    return db


def _seed_mission(db: Path, *, mission_id: str, objective: str) -> None:
    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(id=mission_id, objective=objective, status=MissionStatus.PENDING)
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
    mission_id = result.output.strip()
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
    mission = _get_mission(rework_db, result.output.strip())
    assert mission is not None
    assert mission.repo_origin == url
    assert mission.repo_path is None


def test_ls_output_format(rework_db: Path) -> None:
    """`ls` prints `{id}  {status:<14}  {objective}` (two spaces, status
    left-justified to width 14)."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0, result.output
    assert result.output == "m-abc123  pending         fix the planted bug\n"


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
    mission = _get_mission(rework_db, result.output.strip())
    assert mission is not None
    plan = mission.plan or {}
    assert plan["kind"] == "auto" and plan["playbook"] == "fix-pr"
    assert plan["gates"] == ["open_pr"]
    assert plan["orchestrator"]["model"]
    assert plan["budget"]["max_usd"] == 3.0


def test_new_mechanical_keeps_the_legacy_pipeline(rework_db: Path) -> None:
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "fix it", "--repo", url, "--plan", "fix", "--mechanical"])
    assert result.exit_code == 0, result.output
    mission = _get_mission(rework_db, result.output.strip())
    assert mission is not None
    assert "orchestrator" not in (mission.plan or {})
    assert [s["step"] for s in (mission.plan or {})["steps"]] == ["investigate", "code", "audit"]


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
    envdir.mkdir()
    (envdir / "rework.env").write_text(
        "# local keys\nREWORK_FIREWORKS_KEY=supersecretvalue123\nOTHER=stay\n"
    )
    (envdir / "models.json").write_text("{}")  # a local catalog exists to copy
    import rework.config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_STATE_DIR", envdir)

    # (argv, stdin): the merged env travels over the ssh process's stdin, not
    # the command line (a command line is visible in `ps` on the remote host)
    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((list(cmd), kwargs.get("input")))
        if cmd[:1] == ["ssh"] and len(cmd) > 2 and cmd[2].startswith("cat"):
            stdout = "REWORK_FIREWORKS_KEY=old\nREWORK_DB=keepme\n"
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
    write_cmd, stdin = ssh_writes[-1]
    assert stdin is not None and "REWORK_FIREWORKS_KEY=supersecretvalue123" in str(stdin)
    assert "REWORK_DB=keepme" in str(stdin)  # remote non-KEY line preserved verbatim
    assert any("chmod 600" in part for part in write_cmd)
    assert any(argv[0] == "scp" for argv, _ in calls), "models.json was not copied"


def test_sync_config_dry_run_touches_nothing(
    targets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    envdir = tmp_path / "state"
    envdir.mkdir()
    (envdir / "rework.env").write_text("REWORK_FIREWORKS_KEY=supersecretvalue123\n")
    import rework.config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_STATE_DIR", envdir)

    called: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        called.append(list(cmd))
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(app, ["sync-config", "rewvis", "--dry-run"])
    if result.exit_code != 0:
        print(result.output)
    assert result.exit_code == 0
    assert "would copy" in result.output
    assert "dry run: nothing was sent" in result.output
    assert "REWORK_FIREWORKS_KEY: 19 chars" in result.output
    assert "supersecretvalue123" not in result.output
    # dry-run may READ the remote env (the read-only cat, to know add vs
    # replace) but must never WRITE anything: no heredoc/cat>, no chmod, no scp
    writes = [
        argv
        for argv in called
        if argv[0] == "scp" or (argv[0] == "ssh" and ("cat >" in argv[2] or "chmod" in argv[2]))
    ]
    assert not writes, f"dry run wrote to the remote host: {writes}"


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
        [sys.executable, "-m", "rework", *argv],
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
    (home / ".rework" / "targets.toml").write_text(
        MAIN_TARGETS_TOML.replace("/tmp/probe/sandbox.db", str(tmp_path / "sandbox.db"))
    )
    (tmp_path / "sandbox.db").write_bytes(b"")  # a valid empty sqlite database
    return home


def test_main_rework_target_env_selects_end_to_end(main_home: Path) -> None:
    # BLOCKER 1: bare `rework` with only REWORK_TARGET set must still resolve
    # (the pre-fix bug: main() resolved targets only when "--target" was in argv)
    result = _run_main(["version"], main_home, {"REWORK_TARGET": "rewvis"})
    assert result.returncode == 2, result.stderr  # unknown-target, exit 2, stderr
    assert "REWORK_TARGET: no target named" not in result.stdout
    ok = _run_main(["targets"], main_home, {"REWORK_TARGET": "sandbox"})
    assert ok.returncode == 0
    assert "sandbox" in ok.stdout


def test_main_default_only_selection_reaches_subcommands(main_home: Path) -> None:
    # BLOCKER 1: no flag, no REWORK_TARGET: the file's `default = "sandbox"`
    # applies to any subcommand, and an unknown DEFAULT never kills the CLI
    result = _run_main(["version"], main_home, {})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rework ")
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
