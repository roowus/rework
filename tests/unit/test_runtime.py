"""The code-on-disk fingerprint: a merge into the checkout a long-running
process was started from must be noticed (docs/plans/m7-findings.md, finding
28), and noticing it must be cheap and must never raise."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rework import runtime
from rework.runtime import CodeChangeWatcher


def _package(tmp_path: Path, name: str = "pkg") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "mod.py").write_text("x = 1\n")
    return root


def _scan(root: Path) -> str:
    count, newest = runtime._scan(root)
    return f"{count}:{newest}"


def test_touching_a_module_changes_the_fingerprint(tmp_path: Path) -> None:
    root = _package(tmp_path)
    before = _scan(root)
    stat = (root / "mod.py").stat()
    os.utime(root / "mod.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert _scan(root) != before, "a touched module must move the fingerprint"


def test_adding_and_removing_a_module_changes_the_fingerprint(tmp_path: Path) -> None:
    root = _package(tmp_path)
    before = _scan(root)
    (root / "extra.py").write_text("y = 2\n")
    added = _scan(root)
    assert added != before, "an added module must move the fingerprint"
    (root / "extra.py").unlink()
    assert _scan(root) != added, "a removed module must move the fingerprint"


def test_non_python_files_and_caches_are_ignored(tmp_path: Path) -> None:
    root = _package(tmp_path)
    before = _scan(root)
    (root / "notes.md").write_text("not code\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00")
    assert _scan(root) == before, "only *.py source counts"


def test_the_installed_package_fingerprints_and_carries_the_git_head() -> None:
    fp = runtime.source_fingerprint()
    assert fp is not None, "the installed package must be readable"
    count, _, head = fp.split(":", 2)
    assert int(count) > 10, fp
    assert head == "-" or len(head) >= 7, fp


def test_the_public_fingerprint_follows_a_temp_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source_fingerprint() derives its root from runtime.py's own location:
    point that at a temp package and the real function must react to a touch."""
    root = _package(tmp_path)
    monkeypatch.setattr(runtime, "__file__", str(root / "runtime.py"))
    before = runtime.source_fingerprint()
    assert before is not None and before.startswith("1:"), before
    stat = (root / "mod.py").stat()
    os.utime(root / "mod.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert runtime.source_fingerprint() != before


def test_an_unreadable_tree_degrades_to_none(tmp_path: Path, monkeypatch) -> None:
    """A stat() failure must not raise and must not look like a change: an
    unreadable tree is "unknown" (None), which the watcher ignores."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert runtime._scan(empty) == (0, 0), "no modules, no timestamp"

    def boom(_root: Path) -> tuple[int, int]:
        raise OSError("vanished")

    monkeypatch.setattr(runtime, "_scan", boom)
    assert runtime.source_fingerprint() is None


def test_git_head_is_none_outside_a_repo(tmp_path: Path) -> None:
    assert runtime._git_head(tmp_path) is None
    assert runtime._read_head(tmp_path / "nope") is None


def test_watcher_fires_once_then_stays_quiet(monkeypatch) -> None:
    values = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    watcher = CodeChangeWatcher()
    assert watcher.value == "a"
    assert watcher.poll() is False, "no change yet"
    values[0] = "b"
    assert watcher.poll() is True, "the change is reported"
    assert watcher.poll() is False, "and reported exactly once"
    assert watcher.value == "b"


def test_watcher_never_fires_on_an_unreadable_tree(monkeypatch) -> None:
    values: list[str | None] = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    watcher = CodeChangeWatcher()
    values[0] = None
    assert watcher.poll() is False, "unknown is not a change"
    values[0] = "a"
    assert watcher.poll() is False, "and the known value is remembered"
    values[0] = "b"
    assert watcher.poll() is True


def test_watcher_swallows_a_raising_source(monkeypatch) -> None:
    def boom() -> str | None:
        raise OSError("gone")

    watcher = CodeChangeWatcher(boom)
    assert watcher.value is None
    assert watcher.poll() is False
