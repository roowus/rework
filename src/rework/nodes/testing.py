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


def fix_files(changed: list[str]) -> list[str]:
    """The non-test files of a change: what to revert to expose a vacuous test."""
    return [f for f in changed if not _TEST_PATH.search(f)]


async def verify_red_green(
    worktree: Path,
    commands: list[str],
    revert_files: list[str],
    base: str,
    *,
    timeout_s: float = 120.0,
) -> list[dict[str, Any]]:
    """Check a claimed red-green: with the fix reverted the named tests must FAIL.

    Works in a throwaway detached worktree of the change's HEAD, so the real
    worktree is never touched (nothing to restore). In it, `revert_files` are
    checked out from `base` (files the base lacks are deleted), then each of at
    most MAX_VERIFIED_TESTS `commands` runs. A command that still exits 0 is
    `vacuous`: the test passes without the fix, so it pins nothing. Returns one
    dict per command: {command, exit_code, vacuous, output}."""
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
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(probe),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                exit_code = proc.returncode if proc.returncode is not None else -1
            except TimeoutError:
                proc.kill()
                raw, exit_code = b"timed out", -1
            results.append(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "vacuous": exit_code == 0,
                    "output": raw.decode("utf-8", "replace")[-2000:],
                }
            )
    finally:
        await _run(worktree, "worktree", "remove", "--force", str(probe))
        shutil.rmtree(tmp, ignore_errors=True)
    return results
