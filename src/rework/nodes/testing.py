"""Tests are a deliverable: the contract and the deterministic checks.

The discipline is the owner's workflow's red-green rule (github.com/roowus/
workflow, "Why the test thing is clever"): write the test, run it, watch it
FAIL for the right reason, then fix and watch it pass. A test that cannot fail
is worse than no test, so the system demands the claim AND checks it.

Everything here is mechanical (no LLM): the coder's `tests` block parser, the
finish gate's completeness check, and the auditor's revert-and-rerun check.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from rework.nodes.git_ops import _run

__all__ = [
    "MAX_VERIFIED_TESTS",
    "changed_files",
    "fix_files",
    "handoff_tests_gap",
    "parse_tests_block",
    "verify_red_green",
]

# the auditor re-runs at most this many named test commands (bounded cost)
MAX_VERIFIED_TESTS = 3

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.test\.[a-z]+$")


def parse_tests_block(text: str) -> dict[str, Any] | None:
    """The coder's `tests` block from the last fenced JSON object in its final
    message that carries a `tests` object. None when absent (an incomplete
    handoff, which the finish gate then refuses)."""
    for raw in reversed(_FENCE.findall(text or "")):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        block = data.get("tests") if isinstance(data, dict) else None
        if isinstance(block, dict):
            return block
    return None


def handoff_tests_gap(artifact: dict[str, Any] | None) -> str | None:
    """Why a coder handoff is incomplete on tests, or None when it is complete.
    Complete = a `tests` block that either claims red_green_verified true with
    the commands it ran, or states a not_testable_reason."""
    block = (artifact or {}).get("tests")
    if not isinstance(block, dict):
        return (
            "the handoff has no `tests` field: report tests {added, changed,"
            " red_green_verified, commands, result} or a not_testable_reason"
        )
    reason = str(block.get("not_testable_reason") or "").strip()
    if reason:
        return None
    if block.get("red_green_verified") is not True:
        return (
            "tests.red_green_verified is not true and tests.not_testable_reason"
            " is empty: watch the test fail then pass, or say why it cannot be tested"
        )
    commands = block.get("commands")
    if not isinstance(commands, list) or not [c for c in commands if str(c).strip()]:
        return (
            "tests.red_green_verified is true but tests.commands is empty:"
            " record the commands you ran"
        )
    return None


def changed_files(diff: str) -> list[str]:
    """Paths from `diff --git a/X b/X` headers, in order, deduplicated."""
    seen: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
            if path not in seen:
                seen.append(path)
    return seen


def fix_files(changed: list[str]) -> list[str]:
    """The non-test files of a change: what to revert to expose a vacuous test."""
    return [f for f in changed if not _TEST_PATH.search(f)]


# pytest's own exit codes: none of these is a verdict on the behaviour.
_PYTEST_EXIT_MEANING = {
    2: "pytest exit 2: interrupted (collection error or interrupt)",
    3: "pytest exit 3: internal error",
    4: "pytest exit 4: usage error",
    5: "pytest exit 5: no tests collected",
}

# Exit 1 means tests ran and something failed. The exception TYPE that opens a
# traceback detail line (`E   ImportError: ...`) says whether the test died on a
# missing name or on the behaviour. Anchored to the line start so an exception
# name quoted inside an assertion (`E   AssertionError: assert 'SyntaxError'...`)
# cannot match. AttributeError only counts in the missing-module-attribute shape.
_WRONG_RED_EXCEPTION = re.compile(
    r"^E\s+(?P<exc>ImportError|ModuleNotFoundError|NameError"
    r"|AttributeError(?=: module '[^']*' has no attribute))\b",
    re.MULTILINE,
)
# pytest's own outcome vocabulary: the summary line (`1 failed`, `2 passed in 0.1s`,
# `no tests ran`) or a verbose node line (`test_a.py::test_x PASSED`). Exit 0 with none
# of these means pytest never visibly ran (a masked or unrelated command).
_PYTEST_FAILURE = re.compile(r"\b\d+ (?:failed|errors?)\b|::\S+ (?:FAILED|ERROR)\b")
_PYTEST_EVIDENCE = re.compile(
    r"\b\d+ (?:passed|failed|skipped|deselected|xfailed|xpassed|errors?)\b"
    r"|\bno tests ran\b"
    r"|::\S+ (?:PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b"
)
# the interpreter itself cannot start the runner: `/usr/bin/python: No module named pytest`
_RUNNER_MISSING = re.compile(r"^\S*python\S*: No module named\b", re.MULTILINE)


def classify_red(exit_code: int, output: str) -> tuple[str, str | None]:
    """(classification, marker) of a run with the fix reverted, from pytest's own
    vocabulary (exit code, then the exception type opening a traceback line), never
    a free-text scan: `vacuously_passes` (exit 0 with pytest's passing summary in the
    output, so the test cannot fail), `unverifiable` (exit 0 or 1 with no pytest
    evidence, or exit 0 with pytest's own failure in the output: the run cannot be
    interpreted, which is no verdict on the test), `wrong_red`
    (the run gave no verdict on the behaviour, or the test died on a missing
    import/name/module attribute) or `fails_for_right_reason`."""
    if exit_code == 0:
        # an exit code alone never proves the test cannot fail: pytest must have run
        if not _PYTEST_EVIDENCE.search(output):
            return "unverifiable", "no pytest summary or test outcome line in the output"
        if _PYTEST_FAILURE.search(output):  # `pytest ; true`: the shell hid pytest's code
            return "unverifiable", "exit 0 contradicts the failure pytest printed"
        return "vacuously_passes", None
    if exit_code != 1:
        meaning = _PYTEST_EXIT_MEANING.get(exit_code, f"exit {exit_code}: the tests did not run")
        return "wrong_red", meaning
    found = _WRONG_RED_EXCEPTION.search(output)
    if found:
        return "wrong_red", found.group("exc")
    if _RUNNER_MISSING.search(output):
        return "wrong_red", "runner not installed"
    # exit 1 is only proof when pytest visibly ran: a command that died before
    # pytest started (`cd missing && pytest`, a missing .venv) also exits 1
    if not _PYTEST_EVIDENCE.search(output):
        return "unverifiable", "pytest produced no test output"
    return "fails_for_right_reason", None


async def _spawn(
    command: str, cwd: Path, env: dict[str, str] | None = None
) -> asyncio.subprocess.Process:
    """Start the coder's command in `cwd`. Through `bash -o pipefail` so a pipeline
    (`| tail`, `2>&1 | tee`) cannot mask a failing pytest behind the last stage's exit
    code; plain sh when bash is absent. `env` is the role allowlist when the caller
    has one: this spawn is role-shaped (it runs the coder's own commands), so it must
    not inherit the daemon's provider keys any more than LocalExecutor does."""
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if env is not None:
        kwargs["env"] = env
    bash = shutil.which("bash")
    if bash:
        return await asyncio.create_subprocess_exec(bash, "-o", "pipefail", "-c", command, **kwargs)
    return await asyncio.create_subprocess_shell(command, **kwargs)


async def verify_red_green(
    worktree: Path,
    commands: list[str],
    revert_files: list[str],
    base: str,
    *,
    timeout_s: float = 120.0,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Check a claimed red-green: with the fix reverted the named tests must FAIL.

    Works in a throwaway detached worktree of the change's HEAD, so the real
    worktree is never touched (nothing to restore). In it, `revert_files` are
    checked out from `base` (files the base lacks are deleted), then each of at
    most MAX_VERIFIED_TESTS `commands` runs (through `bash -o pipefail`: they are the
    coder's own strings, and the worktree is disposable; an absolute path of the real
    worktree in a command, as in `cd <worktree> && pytest`, is pointed at the
    throwaway one so the run cannot escape the revert). Each is classified by
    `classify_red`; `vacuous` is kept as a boolean (`vacuously_passes` only). Returns one dict per
    command: {command, exit_code, vacuous, classification, marker, output}."""
    tmp = Path(tempfile.mkdtemp(prefix="rework-redgreen-"))
    probe = tmp / "wt"
    results: list[dict[str, Any]] = []
    try:
        code, out = await _run(worktree, "worktree", "add", "--detach", str(probe), "HEAD")
        if code != 0:
            raise RuntimeError(f"could not create revert worktree: {out.strip()[:300]}")
        for path in revert_files:
            code, _ = await _run(probe, "checkout", base, "--", path)
            if code != 0:  # absent at base: the fix added it, so reverting removes it
                (probe / path).unlink(missing_ok=True)
        for command in commands[:MAX_VERIFIED_TESTS]:
            proc = await _spawn(command.replace(str(worktree), str(probe)), probe, env)
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                exit_code = proc.returncode if proc.returncode is not None else -1
            except TimeoutError:
                proc.kill()
                raw, exit_code = b"timed out", -1
            text = raw.decode("utf-8", "replace")
            classification, marker = classify_red(exit_code, text)
            if exit_code == -1 and text == "timed out":
                classification, marker = "wrong_red", "timed out"
            results.append(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "vacuous": classification == "vacuously_passes",
                    "classification": classification,
                    "marker": marker,
                    "output": text[-2000:],
                }
            )
    finally:
        await _run(worktree, "worktree", "remove", "--force", str(probe))
        shutil.rmtree(tmp, ignore_errors=True)
    return results
