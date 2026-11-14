"""`rework gc` reclaims role worktrees of finished missions only (found
2026-11-10: 160 worktrees, 8.1 GB, nothing ever removed them) and, since
2026-11-12, the role scratch dirs those runs leave in the system temp dir
(~140 unattributed `rework-scratch-<random>` dirs had piled up in /tmp)."""

import subprocess
from pathlib import Path

from rework.gc import collect, collect_scratch, mission_of, remove, remove_scratch, scratch_mission

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

    # the role's rework scratch dir lives next to the worktree, in the run dir
    scratch = wt.parent / ".rework-scratch"
    (scratch / "logs").mkdir(parents=True)
    (scratch / "logs" / "run.log").write_text("x\n")

    found = collect(tmp_path, {"m-aaaaaaaaaaaa": ("done", 0.0)}, DAY, now=NOW)
    freed = remove(found, tmp_path)
    assert freed > 0
    assert not scratch.exists()  # reclaimed with the run dir, not orphaned
    assert not wt.parent.exists()
    assert (repo / "a.py").exists()  # the clone stays (preview needs it)
    assert str(wt) not in git("worktree", "list", cwd=repo)


def test_mission_of() -> None:
    assert mission_of(Path("m-58d05d4101c7_code6_coder_1")) == "m-58d05d4101c7"
    assert mission_of(Path("missions")) is None
    assert mission_of(Path("m-02a838292537")) is None


def test_hard_linked_files_are_not_counted_as_freed(tmp_path: Path) -> None:
    import os

    cache = tmp_path / "uv-cache.bin"
    cache.write_bytes(b"y" * 5000)
    run = _run_dir(tmp_path / "work", "m-bbbbbbbbbbbb_code_coder_1")
    os.link(cache, run / "worktree" / "linked.bin")
    found = collect(tmp_path / "work", {"m-bbbbbbbbbbbb": ("done", 0.0)}, DAY, now=NOW)
    assert found[0].bytes == 1000  # big.bin only; the linked file frees nothing


def _scratch_dir(root: Path, name: str, payload: bytes = b"x" * 500) -> Path:
    d = root / name
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "run.log").write_bytes(payload)
    return d


def test_scratch_mission_reads_the_id_out_of_the_name() -> None:
    assert scratch_mission("rework-scratch-m-58d05d4101c7-ab12cd34") == "m-58d05d4101c7"
    assert scratch_mission("rework-scratch-m-58d05d4101c7") is None  # legacy, no id
    assert scratch_mission("rework-scratch-ab12cd34") is None  # legacy, no id
    assert scratch_mission("rework-scratch-") is None
    assert scratch_mission("some-other-tool-xyz") is None
    assert scratch_mission("m-58d05d4101c7_code6_coder_1") is None


def test_finished_mission_scratch_dir_is_collected_and_removed(tmp_path: Path) -> None:
    dead = _scratch_dir(tmp_path, "rework-scratch-m-58d05d4101c7-ab12cd34")

    found = collect_scratch(tmp_path, {"m-58d05d4101c7"})

    assert [c.path for c in found] == [dead]
    assert found[0].mission_id == "m-58d05d4101c7"
    assert found[0].bytes >= 500
    assert remove_scratch(found) > 0
    assert not dead.exists()


def test_a_running_mission_scratch_dir_survives(tmp_path: Path) -> None:
    live = _scratch_dir(tmp_path, "rework-scratch-m-live00000002-ff00aa11")

    found = collect_scratch(tmp_path, {"m-58d05d4101c7"})  # a different mission

    assert found == []
    assert remove_scratch(found) == 0
    assert live.exists()


def test_a_legacy_unattributed_scratch_dir_survives(tmp_path: Path) -> None:
    """No mission id in the name: nobody can say whose it is, so it is not ours
    to delete (and a prefix-only sweep would have taken it)."""
    legacy = _scratch_dir(tmp_path, "rework-scratch-ab12cd34")
    empty_id = _scratch_dir(tmp_path, "rework-scratch-")

    found = collect_scratch(tmp_path, {"m-58d05d4101c7"})

    assert found == []
    assert legacy.exists() and empty_id.exists()


def test_a_non_matching_temp_entry_survives(tmp_path: Path) -> None:
    other = _scratch_dir(tmp_path, "some-other-tool-xyz")
    close = _scratch_dir(tmp_path, "not-rework-scratch-m-58d05d4101c7-ab12")
    plain = tmp_path / "rework-scratch-m-58d05d4101c7-ab12"  # a FILE, not a dir
    plain.write_text("x")

    found = collect_scratch(tmp_path, {"m-58d05d4101c7"})

    assert found == []  # the prefix is exact, and a dir is required
    remove_scratch(found)
    assert other.exists() and close.exists() and plain.exists()


def test_a_missing_temp_dir_is_not_an_error(tmp_path: Path) -> None:
    assert collect_scratch(tmp_path / "nope", {"m-58d05d4101c7"}) == []


def test_an_unreadable_temp_dir_is_not_an_error(tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:  # root ignores the mode bits
        return
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        assert collect_scratch(locked, {"m-58d05d4101c7"}) == []
    finally:
        locked.chmod(0o755)


def test_a_dir_that_vanishes_mid_scan_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    """`itertools` sorted() runs first, so a dir removed between the listing and
    the stat (another gc, a role cleaning up) must be skipped, not raise."""
    doomed = _scratch_dir(tmp_path, "rework-scratch-m-58d05d4101c7-ab12cd34")
    kept = _scratch_dir(tmp_path, "rework-scratch-m-58d05d4101c7-cd34ab12")

    import rework.gc as gc_mod

    real_size = gc_mod._size

    def size(path: Path) -> int:
        if path == doomed:
            raise FileNotFoundError(path)
        return real_size(path)

    monkeypatch.setattr(gc_mod, "_size", size)
    found = collect_scratch(tmp_path, {"m-58d05d4101c7"})
    assert [c.path for c in found] == [kept]
