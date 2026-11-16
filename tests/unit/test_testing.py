"""Tests are a deliverable: the deterministic pieces (no LLM, no network).

The acceptance test for the auditor's red-green check is here: a fix whose
test still passes with the fix reverted is reported as vacuous."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rework.nodes.git_ops import _run, diff_base, head_sha
from rework.nodes.testing import (
    classify_red,
    fix_files,
    handoff_tests_gap,
    parse_tests_block,
    verify_red_green,
)
from rework.roles.builtin.auditor import changed_files, handoff_files, red_green_notes

GOOD = {
    "added": ["test_calc.py"],
    "changed": [],
    "red_green_verified": True,
    "commands": ["pytest -q"],
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


def _rev(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


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
CMD = f"{sys.executable} -m pytest -q test_calc.py"


def assert_real_failure(output: str) -> None:
    """A real red: the assertion failed. A wrong red (no pytest, import or
    collection error, nothing collected) must not satisfy this."""
    assert "assert add(2, 3) == 5" in output and "1 failed" in output, output
    assert classify_red(1, output) == ("fails_for_right_reason", None), output


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
    assert result["classification"] == "fails_for_right_reason" and result["marker"] is None
    assert_real_failure(result["output"])
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
    assert result["classification"] == "vacuously_passes"
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


async def test_an_uncheckable_claim_says_it_was_not_verified() -> None:
    """Silence would read as success: the auditor is told the check did not run."""
    claim = {"coder_tests": GOOD, "worktree_path": "/x", "diff": "diff --git a/c.py b/c.py\n"}
    [no_base] = await red_green_notes(claim)
    assert "NOT performed" in no_base and "no base_sha" in no_base
    test_only = {**claim, "base_sha": "abc", "diff": "diff --git a/test_c.py b/test_c.py\n"}
    [note] = await red_green_notes(test_only)
    assert "NOT performed" in note and "test-only" in note


NEEDS_MUL = "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"


def _two_commit_repo(tmp_path: Path, base_calc: str, fix_calc: str, test_body: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(base_calc)
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "calc.py")
    _git(repo, "commit", "-m", "base")
    base = _rev(repo)
    (repo / "calc.py").write_text(fix_calc)
    (repo / "test_calc.py").write_text(test_body)
    _git(repo, "add", "calc.py", "test_calc.py")
    _git(repo, "commit", "-m", "fix")
    return repo, base


ADD_ONLY = "def add(a, b):\n    return a + b\n"
ADD_AND_MUL = ADD_ONLY + "\ndef mul(a, b):\n    return a * b\n"
DESCRIBE_BUG = 'def describe(t):\n    return "TokenError: " + t\n'
DESCRIBE_FIX = 'def describe(t):\n    return "SyntaxError: " + t\n'
STRICT = (
    "import argparse\n\n"
    "def parse(argv):\n"
    "    p = argparse.ArgumentParser(prog='calc')\n"
    "    return p.parse_args(argv)\n"
)
STRICT_FIX = STRICT.replace(
    "    return p", "    p.add_argument('--strict', action='store_true')\n    return p"
)

# the six audited cases: (id, base calc.py, fixed calc.py, test body, expected)
RED_CASES = [
    (
        "module-level import",
        ADD_ONLY,
        ADD_AND_MUL,
        "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        "wrong_red",
    ),
    (
        "local import in the test body",
        ADD_ONLY,
        ADD_AND_MUL,
        "def test_mul():\n    from calc import mul\n    assert mul(2, 3) == 6\n",
        "wrong_red",
    ),
    (
        "attribute access calc.mul",
        ADD_ONLY,
        ADD_AND_MUL,
        "import calc\n\ndef test_mul():\n    assert calc.mul(2, 3) == 6\n",
        "wrong_red",
    ),
    (
        "honest assertion, expected string holds SyntaxError",
        DESCRIBE_BUG,
        DESCRIBE_FIX,
        "from calc import describe\n\n"
        "def test_describe():\n"
        '    assert describe("bad token") == "SyntaxError: bad token"\n',
        "fails_for_right_reason",
    ),
    (
        "honest argparse red, unrecognized arguments",
        STRICT,
        STRICT_FIX,
        "import pytest\nfrom calc import parse\n\n"
        "def test_strict():\n"
        "    with pytest.raises(SystemExit) as e:\n"
        '        parse(["--strict"])\n'
        "    assert e.value.code == 0\n",
        "fails_for_right_reason",
    ),
    (
        "genuine assertion failure",
        "def add(a, b):\n    return a - b\n",
        ADD_ONLY,
        REAL_TEST,
        "fails_for_right_reason",
    ),
]


@pytest.mark.parametrize(
    ("base_calc", "fix_calc", "body", "expected"),
    [c[1:] for c in RED_CASES],
    ids=[c[0] for c in RED_CASES],
)
async def test_the_red_is_classified_by_pytests_own_vocabulary(
    tmp_path: Path, base_calc: str, fix_calc: str, body: str, expected: str
) -> None:
    repo, base = _two_commit_repo(tmp_path, base_calc, fix_calc, body)
    [result] = await verify_red_green(repo, [CMD], ["calc.py"], base)
    assert result["exit_code"] != 0 and result["vacuous"] is False, result["output"]
    assert result["classification"] == expected, result["output"]
    assert (result["marker"] is None) == (expected == "fails_for_right_reason")


MASKING_SHAPES = [
    ("pipe to tail", f"{CMD} | tail -3", "fails_for_right_reason"),
    ("tee", f"{CMD} 2>&1 | tee x.log", "fails_for_right_reason"),
    # `; true` discards pytest's code and pipefail cannot restore it: the output still
    # shows the failure, so the run is uninterpretable (never vacuous)
    ("semicolon true", f"{CMD} ; true", "unverifiable"),
    ("cd and", "cd {repo} && " + CMD, "fails_for_right_reason"),
]


@pytest.mark.parametrize(
    ("template", "expected"),
    [c[1:] for c in MASKING_SHAPES],
    ids=[c[0] for c in MASKING_SHAPES],
)
async def test_a_shell_shape_cannot_turn_an_honest_red_into_a_vacuous_one(
    tmp_path: Path, template: str, expected: str
) -> None:
    """The shell's exit code is not pytest's: a pipe, `; true` or `cd ... &&` around a
    genuinely failing test must still be read as the right-reason red it is."""
    repo, base = _repo(tmp_path, REAL_TEST)
    command = template.format(repo=repo)
    [result] = await verify_red_green(repo, [command], ["calc.py"], base)
    assert result["classification"] != "vacuously_passes", result["output"]
    assert result["classification"] == expected, result
    assert result["vacuous"] is False and "1 failed" in result["output"]
    notes = await red_green_notes(
        {
            "coder_tests": {**GOOD, "commands": [command]},
            "worktree_path": str(repo),
            "base_sha": base,
            "diff": await diff_base(repo, base=base),
        }
    )
    assert "vacuous_test" not in notes[0] and "STILL PASSES" not in notes[0]
    if expected == "unverifiable":
        assert "verify by hand" in notes[0]
    else:
        assert "fails as it should" in notes[0]


async def test_an_honest_failing_test_without_a_pipe_is_still_a_right_reason_red(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path, REAL_TEST)
    [result] = await verify_red_green(repo, [CMD], ["calc.py"], base)
    assert result["exit_code"] == 1
    assert (result["classification"], result["marker"]) == ("fails_for_right_reason", None)


async def test_exit_zero_without_pytest_evidence_is_unverifiable_not_vacuous(
    tmp_path: Path,
) -> None:
    """Exit 0 alone never proves the test cannot fail: with no pytest summary or
    outcome line the run could not be interpreted, and the coder is not accused."""
    repo, base = _repo(tmp_path, REAL_TEST)
    command = "echo all good"
    [result] = await verify_red_green(repo, [command], ["calc.py"], base)
    assert result["exit_code"] == 0 and result["vacuous"] is False
    assert result["classification"] == "unverifiable"
    assert "pytest" in str(result["marker"])
    assert classify_red(0, "")[0] == "unverifiable"
    assert classify_red(0, "..\n2 passed in 0.01s")[0] == "vacuously_passes"
    assert classify_red(0, "test_a.py::test_x PASSED")[0] == "vacuously_passes"
    notes = await red_green_notes(
        {
            "coder_tests": {**GOOD, "commands": [command]},
            "worktree_path": str(repo),
            "base_sha": base,
            "diff": await diff_base(repo, base=base),
        }
    )
    assert "vacuous_test" not in notes[0] and "STILL PASSES" not in notes[0]
    assert "could not interpret this run, verify by hand" in notes[0]


@pytest.mark.parametrize(
    "template",
    [
        "cd {repo}/missing-dir && " + CMD,
        "test -d venv && true",
        "source .venv/bin/activate && " + CMD,
    ],
    ids=["cd missing dir", "test -d", "missing venv"],
)
async def test_exit_one_before_pytest_starts_is_unverifiable_not_proof(
    tmp_path: Path, template: str
) -> None:
    """A command that dies before pytest runs exits 1 too. With the test genuinely
    vacuous, that must not read as "fails as it should"."""
    repo, base = _repo(tmp_path, VACUOUS_TEST)
    command = template.format(repo=repo)
    [result] = await verify_red_green(repo, [command], ["calc.py"], base)
    assert result["exit_code"] != 0 and result["vacuous"] is False, result
    assert result["classification"] == "unverifiable", result
    assert result["marker"] == "pytest produced no test output"
    [note] = await red_green_notes(
        {
            "coder_tests": {**GOOD, "commands": [command]},
            "worktree_path": str(repo),
            "base_sha": base,
            "diff": await diff_base(repo, base=base),
        }
    )
    assert "verify by hand" in note and "pytest produced no test output" in note
    assert "fails as it should" not in note and "vacuous_test" not in note


async def test_a_fix_listed_only_past_the_100kb_cut_is_still_reverted(tmp_path: Path) -> None:
    """The stored diff is cut at 100 KB, so a big change loses its tail: the fix file
    must come from the coder's own full-diff `changed_files`, or it stays applied and
    an honest test is accused of passing without the fix."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "calc.py")
    _git(repo, "commit", "-m", "base")
    base = _rev(repo)
    (repo / "a_big.txt").write_text("".join(f"line {i} of the big file\n" for i in range(6000)))
    (repo / "calc.py").write_text(ADD_ONLY)
    (repo / "test_calc.py").write_text(REAL_TEST)
    _git(repo, "add", "a_big.txt", "calc.py", "test_calc.py")
    _git(repo, "commit", "-m", "fix")
    full = await diff_base(repo, base=base)
    stored = full[:100_000]  # what the coder's handoff keeps
    assert len(full) > 100_000 and "calc.py" in changed_files(full)
    assert "calc.py" not in changed_files(stored)  # the tail is gone from the stored diff

    run_ctx = {
        "coder_tests": {**GOOD, "commands": [CMD]},
        "worktree_path": str(repo),
        "base_sha": base,
        "diff": stored,
    }
    # old handoff without the field: the fallback parses the stored diff (the old blind spot)
    [old] = await red_green_notes(run_ctx)
    assert "STILL PASSES: vacuous_test" in old  # the false accusation this field prevents
    # with the field the fix is reverted, the test fails honestly, nobody is accused
    [note] = await red_green_notes({**run_ctx, "changed_files": changed_files(full)})
    assert "NOT performed" not in note and "fails as it should" in note
    assert "vacuous_test" not in note and "STILL PASSES" not in note
    assert handoff_files({**run_ctx, "changed_files": changed_files(full)}) == [
        "a_big.txt",
        "calc.py",
        "test_calc.py",
    ]


async def test_a_wrong_red_note_says_it_is_not_proof(tmp_path: Path) -> None:
    repo, base = _two_commit_repo(tmp_path, ADD_ONLY, ADD_AND_MUL, RED_CASES[0][3])
    [note] = await red_green_notes(
        {
            "coder_tests": {**GOOD, "commands": [CMD]},
            "worktree_path": str(repo),
            "base_sha": base,
            "diff": await diff_base(repo, base=base),
        }
    )
    assert "WRONG REASON" in note and "NOT proof" in note
    assert "fails as it should" not in note and "vacuous_test" not in note


def test_classify_red_covers_every_outcome() -> None:
    assert classify_red(0, "1 passed") == ("vacuously_passes", None)
    assert classify_red(1, "E   AssertionError: assert 1 == 2\n1 failed") == (
        "fails_for_right_reason",
        None,
    )
    for code in (2, 3, 4, 5):
        assert classify_red(code, "")[0] == "wrong_red"
    assert classify_red(1, "E   ImportError: cannot import name 'mul'") == (
        "wrong_red",
        "ImportError",
    )
    assert classify_red(1, "E       NameError: name 'x' is not defined")[0] == "wrong_red"
    attr = "E   AttributeError: module 'calc' has no attribute 'mul'"
    assert classify_red(1, attr) == ("wrong_red", "AttributeError")
    # an AttributeError raised by the code under test is a real red
    nonetype = "E   AttributeError: 'NoneType' object has no attribute 'x'\n1 failed in 0.1s"
    assert classify_red(1, nonetype)[0] == "fails_for_right_reason"
    # exception names quoted inside an assertion are content, not the exception type
    quoted = (
        "E   AssertionError: assert 'ImportError' == 'x'\nE     - ImportError\nE     + x\n1 failed"
    )
    assert classify_red(1, quoted)[0] == "fails_for_right_reason"
    assert classify_red(1, "/usr/bin/python3: No module named pytest")[0] == "wrong_red"
    # exit 1 with no pytest output at all proves nothing: the command never reached pytest
    assert classify_red(1, "") == ("unverifiable", "pytest produced no test output")
    assert classify_red(1, "bash: cd: x: No such file or directory")[0] == "unverifiable"


def test_the_auditor_cli_view_shows_test_coverage_even_without_evidence() -> None:
    from rework.cli import _render_artifact

    entry = {"severity": "high", "kind": "vacuous_test", "title": "cannot fail", "detail": "d"}
    text = "\n".join(_render_artifact("auditor", {"summary": "s", "coverage": [entry]}))
    assert "TEST COVERAGE" in text
    assert "[high/vacuous_test] cannot fail" in text
    assert "evidence: None" in text  # pinned: a missing evidence key neither crashes nor hides
    with_evidence = {**entry, "evidence": "pytest: 1 passed"}
    shown = "\n".join(_render_artifact("auditor", {"summary": "s", "coverage": [with_evidence]}))
    assert "evidence: pytest: 1 passed" in shown
