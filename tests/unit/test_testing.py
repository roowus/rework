"""Tests are a deliverable: the deterministic pieces (no LLM, no network).

The acceptance test for the auditor's red-green check is here: a fix whose
test still passes with the fix reverted is reported as vacuous."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rework.nodes.git_ops import _run, diff_base, head_sha
from rework.nodes.testing import (
    fix_files,
    handoff_tests_gap,
    parse_tests_block,
    verify_red_green,
)
from rework.roles.builtin.auditor import changed_files, red_green_notes

GOOD = {
    "added": ["test_calc.py"],
    "changed": [],
    "red_green_verified": True,
    "commands": ["python3 -m pytest -q"],
    "result": "red then green",
    "not_testable_reason": "",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path, test_body: str) -> tuple[Path, str]:
    """calc.py with the add() bug at the base; the 'fix' commit adds + the test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "calc.py")
    _git(repo, "commit", "-m", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(test_body)
    _git(repo, "add", "calc.py", "test_calc.py")
    _git(repo, "commit", "-m", "fix")
    return repo, base


REAL_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
VACUOUS_TEST = "from calc import add\n\ndef test_add():\n    assert add(0, 0) == 0\n"
CMD = "python3 -m pytest -q test_calc.py"


def test_parse_tests_block_reads_the_last_fenced_json() -> None:
    text = 'done.\n```json\n{"tests": {"red_green_verified": true, "commands": ["x"]}}\n```'
    assert parse_tests_block(text) == {"red_green_verified": True, "commands": ["x"]}
    assert parse_tests_block("no block here") is None
    assert parse_tests_block('```json\n{"other": 1}\n```') is None


def test_tests_problem_accepts_complete_and_reasoned_blocks_only() -> None:
    assert handoff_tests_gap({"tests": GOOD}) is None
    reasoned = {"red_green_verified": False, "not_testable_reason": "docs-only change"}
    assert handoff_tests_gap({"tests": reasoned}) is None
    assert "no `tests` field" in str(handoff_tests_gap({"kind": "coder_handoff"}))
    unverified = {"tests": {**GOOD, "red_green_verified": False}}
    assert "not_testable_reason" in str(handoff_tests_gap(unverified))
    assert "commands" in str(handoff_tests_gap({"tests": {**GOOD, "commands": []}}))


def test_fix_files_drops_test_paths() -> None:
    assert fix_files(["calc.py", "test_calc.py", "tests/unit/x.py", "src/a_test.go"]) == ["calc.py"]


async def test_a_test_that_fails_without_the_fix_is_not_vacuous(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, REAL_TEST)
    [result] = await verify_red_green(repo, [CMD], ["calc.py"], base)
    assert result["vacuous"] is False
    assert result["exit_code"] != 0
    # the real worktree was never touched: the fix is still in place
    assert "a + b" in (repo / "calc.py").read_text()
    _code, status = await _run(repo, "status", "--porcelain")
    assert status == ""


async def test_a_test_that_still_passes_with_the_fix_reverted_is_vacuous(tmp_path: Path) -> None:
    """Acceptance (b): the coder claims red_green_verified, the revert check shows the
    test still passes, so the auditor is handed a vacuous_test verdict."""
    repo, base = _repo(tmp_path, VACUOUS_TEST)
    [result] = await verify_red_green(repo, [CMD], ["calc.py"], base)
    assert result["vacuous"] is True and result["exit_code"] == 0
    assert "1 passed" in result["output"]

    diff = await diff_base(repo, base=base)
    assert await head_sha(repo)
    notes = await red_green_notes(
        {
            "coder_tests": {**GOOD, "commands": [CMD]},
            "worktree_path": str(repo),
            "base_sha": base,
            "diff": diff,
        }
    )
    assert changed_files(diff) == ["calc.py", "test_calc.py"]
    assert len(notes) == 1
    assert "vacuous_test" in notes[0] and "STILL PASSES" in notes[0]


async def test_red_green_check_is_bounded_to_three_commands(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, REAL_TEST)
    results = await verify_red_green(repo, [CMD] * 5, ["calc.py"], base)
    assert len(results) == 3


async def test_no_red_green_claim_means_no_mechanical_check() -> None:
    assert await red_green_notes({}) == []
    unverified = {"coder_tests": {**GOOD, "red_green_verified": False}, "worktree_path": "/x"}
    assert await red_green_notes(unverified) == []
