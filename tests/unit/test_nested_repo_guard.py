"""The handoff never commits nested git repos (found live: a coder's bare
`origin.git/` and a clone `work/` landed in the PR-preview branch), nor the
role's own rework scratch dir (follow-up to PR #22)."""

import subprocess
from pathlib import Path

from rework.nodes.git_ops import stage_all, unstage_nested_repos


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


async def test_bare_repos_and_clones_stay_out_of_the_commit(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    git(wt, "init", "-q")
    (wt / "a.py").write_text("x = 1\n")
    git(wt, "add", "a.py")
    git(wt, "commit", "-qm", "init")
    # the real change, plus the junk of a manual experiment
    (wt / "a.py").write_text("x = 2\n")
    (wt / "src").mkdir()
    (wt / "src" / "new.py").write_text("y = 1\n")
    git(wt, "init", "-q", "--bare", "origin.git")
    git(tmp_path, "clone", "-q", str(wt / "origin.git"), str(wt / "work"))
    (wt / "work" / "f.txt").write_text("hi\n")
    git(wt / "work", "add", "f.txt")
    git(wt / "work", "commit", "-qm", "c")

    await stage_all(wt)
    assert await unstage_nested_repos(wt) == ["origin.git", "work"]
    staged = git(wt, "diff", "--cached", "--name-only").split()
    assert staged == ["a.py", "src/new.py"]
    assert (wt / "origin.git" / "HEAD").exists()  # left on disk, out of the commit


async def test_a_normal_change_is_untouched(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "refs").mkdir()  # a dir merely NAMED like git internals
    (tmp_path / "refs" / "HEAD").write_text("not git\n")
    await stage_all(tmp_path)
    assert await unstage_nested_repos(tmp_path) == []
    assert git(tmp_path, "diff", "--cached", "--name-only").split() == ["refs/HEAD"]


def test_the_auditor_gets_the_complete_file_list() -> None:
    from rework.roles.builtin.auditor import changed_files

    diff = (
        "diff --git a/src/x.py b/src/x.py\n+1\n"
        + "+pad\n" * 20000
        + "diff --git a/origin.git/HEAD b/origin.git/HEAD\n+ref\n"
    )
    assert changed_files(diff) == ["src/x.py", "origin.git/HEAD"]


async def test_the_roles_rework_scratch_dir_never_reaches_the_commit(tmp_path: Path) -> None:
    """Follow-up to PR #22: role_env used to put rework's per-run state in
    `<workdir>/.rework-scratch`, and a coder's workdir IS its worktree, so the
    handoff's `git add -A` committed logs/targets/run registry into the PR.
    Real worktree + real role_env + a real jailed command writing into it."""
    from rework.nodes.tools import LocalExecutor, role_env

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "base")

    run_dir = tmp_path / "m-abc123_code_coder_1"  # the run dir; loop.json lives here
    run_dir.mkdir()
    wt = run_dir / "worktree"  # coder.py's <run_dir>/worktree
    git(repo, "worktree", "add", "-q", "-b", "rework/x", str(wt))

    # REWORK_RUN_DIR is pinned by the _isolated_command_registry fixture, so
    # locate the scratch dir through REWORK_LOG_DIR (same dir).
    scratch = Path(role_env(wt)["REWORK_LOG_DIR"]).parent
    assert not scratch.is_relative_to(wt), "scratch dir must be outside the worktree"

    # the role's own rework invocation, run through the real jail
    out = await LocalExecutor(wt).execute(
        "run_command",
        {
            "command": 'mkdir -p "$REWORK_LOG_DIR" && echo log > "$REWORK_LOG_DIR/run.log"'
            ' && echo t > "$REWORK_TARGETS"'
        },
    )
    assert out.startswith("(exit 0)"), out
    assert (scratch / "logs" / "run.log").exists()
    assert (scratch / "targets.toml").exists()

    # the genuine change the coder made
    (wt / "a.py").write_text("x = 2\n")

    await stage_all(wt)
    assert await unstage_nested_repos(wt) == []
    staged = git(wt, "diff", "--cached", "--name-only").split()
    assert staged == ["a.py"], f"only the real change is staged, got {staged}"
    assert not any(".rework-scratch" in p for p in staged)
