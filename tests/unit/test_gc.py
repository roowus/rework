"""`rework gc` reclaims role worktrees of finished missions only (found
2026-11-10: 160 worktrees, 8.1 GB, nothing ever removed them)."""

import subprocess
from pathlib import Path

from rework.gc import collect, mission_of, remove

DAY = 86400.0
NOW = 1_000_000.0


def _run_dir(root: Path, name: str) -> Path:
    d = root / name / "worktree"
    d.mkdir(parents=True)
    (d / "big.bin").write_bytes(b"x" * 1000)
    return root / name


def test_only_old_finished_missions_are_collected(tmp_path: Path) -> None:
    for name in (
        "m-done00000001_code_coder_1",
        "m-done00000001_audit_auditor_1",
        "m-live00000002_code_coder_1",
        "m-new000000003_code_coder_1",
        "m-unknown00004_code_coder_1",
    ):
        _run_dir(tmp_path, name)
    (tmp_path / "missions" / "m-done00000001" / "repo").mkdir(parents=True)
    (tmp_path / "m-done00000001").mkdir()  # a bare workspace dir, not a run dir
    missions = {
        "m-done00000001": ("done", NOW - 5 * DAY),
        "m-live00000002": ("running", NOW - 5 * DAY),
        "m-new000000003": ("failed", NOW - 0.5 * DAY),
    }
    found = collect(tmp_path, missions, 2 * DAY, now=NOW)
    assert sorted(c.path.name for c in found) == [
        "m-done00000001_audit_auditor_1",
        "m-done00000001_code_coder_1",
    ]
    assert all(c.bytes >= 1000 for c in found)


def test_remove_keeps_the_clone_and_prunes_its_worktrees(tmp_path: Path) -> None:
    def git(*a: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    repo = tmp_path / "missions" / "m-aaaaaaaaaaaa" / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", cwd=repo)
    (repo / "a.py").write_text("x = 1\n")
    git("add", "a.py", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    wt = tmp_path / "m-aaaaaaaaaaaa_code_coder_1" / "worktree"
    git("worktree", "add", "-q", "-b", "rework/x", str(wt), cwd=repo)
    assert str(wt) in git("worktree", "list", cwd=repo)

    found = collect(tmp_path, {"m-aaaaaaaaaaaa": ("done", 0.0)}, DAY, now=NOW)
    freed = remove(found, tmp_path)
    assert freed > 0
    assert not wt.parent.exists()
    assert (repo / "a.py").exists()  # the clone stays (preview needs it)
    assert str(wt) not in git("worktree", "list", cwd=repo)


def test_mission_of() -> None:
    assert mission_of(Path("m-58d05d4101c7_code6_coder_1")) == "m-58d05d4101c7"
    assert mission_of(Path("missions")) is None
    assert mission_of(Path("m-02a838292537")) is None
