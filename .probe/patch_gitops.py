from pathlib import Path

p = Path("src/rework/nodes/git_ops.py")
s = p.read_text()

old_run = '''    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        return 124, "(git timeout)"
    return proc.returncode or 0, out.decode(errors="replace").strip()
'''
new_run = '''    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        # keep whatever git had already written before it hung: replacing it
        # with a bare placeholder threw away the only evidence of the failure
        partial = b""
        with contextlib.suppress(Exception):
            partial, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        text = partial.decode(errors="replace").strip()
        note = f"(git timeout after {timeout_s:g}s)"
        return 124, f"{text}\\n{note}" if text else note
    return proc.returncode or 0, out.decode(errors="replace").strip()
'''
assert old_run in s
s = s.replace(old_run, new_run)

s = s.replace('''from __future__ import annotations

import asyncio
from pathlib import Path
''', '''from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
''', 1)

old_push = '''async def push_branch(worktree: Path, branch: str) -> None:
    """Push the run's branch to origin. For self-hosting: agents push
    branches, humans merge (CLAUDE.md dogfooding rules)."""
    code, out = await _run(worktree, "push", "-u", "origin", branch, timeout_s=120.0)
    if code != 0:
        raise RuntimeError(f"git push failed: {out}")
'''
new_push = '''# -- push delivery: transient vs. real rejection -----------------------------
#
# Found live (mission m-26591d66f460): GitHub answered one push with
# `! [remote rejected] <branch> -> <branch> (failed)` with no reason given, and
# the single unclassified attempt threw a finished mission away. A manual retry
# minutes later succeeded. So: retry what the remote can plausibly answer
# differently next time, never retry what it has decided (non-fast-forward,
# protected branch, push protection, auth).

PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)

# The remote said NO and will say no again: retrying only buries the real
# message under "3 attempts".
_PUSH_REJECTION_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "protected branch",
    "gh006",
    "push declined due to repository rule violations",
    "gh013",
    "permission denied",
    "authentication failed",
    "denied to",
)
_PUSH_REJECTION_PATTERNS = (re.compile(r"\\b403\\b"),)

# The transport (or the forge behind it) hiccuped: a retry IS the fix.
_PUSH_TRANSIENT_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "timed out",
    "timeout",
    "rpc failed",
    "unable to access",
    "could not resolve host",
    "early eof",
    "tls",
    "ssl",
    "broken pipe",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)
# `! [remote rejected] b -> b (failed)`: the remote refused the ref update and
# did NOT say why. This is the live case; treat it as transient.
_PUSH_REASONLESS_REJECTION = re.compile(r"\\[remote rejected\\].*\\(failed\\)")


class PushError(RuntimeError):
    """A push that did not happen. `retryable` says whether the remote might
    answer differently next time; `output` keeps git's FULL output, every line
    (the line that matters is the LAST one, after the forge's hints)."""

    def __init__(
        self, message: str, *, output: str = "", retryable: bool = False, attempts: int = 1
    ) -> None:
        super().__init__(message)
        self.output = output
        self.retryable = retryable
        self.attempts = attempts


def push_failure_is_transient(output: str, code: int = 1) -> bool:
    """Is this push failure worth another attempt?

    Real rejections (the remote decided) win over transient markers, because a
    protected-branch or auth failure also mentions hosts, TLS and timeouts."""
    if code == 124:  # git was killed on timeout: no answer is a transient answer
        return True
    text = (output or "").lower()
    if any(marker in text for marker in _PUSH_REJECTION_MARKERS):
        return False
    if any(pattern.search(text) for pattern in _PUSH_REJECTION_PATTERNS):
        return False
    if any(marker in text for marker in _PUSH_TRANSIENT_MARKERS):
        return True
    return bool(_PUSH_REASONLESS_REJECTION.search(text))


async def push_branch(
    worktree: Path,
    branch: str,
    *,
    attempts: int | None = None,
    backoff_s: tuple[float, ...] | None = None,
) -> None:
    """Push the run's branch to origin. For self-hosting: agents push
    branches, humans merge (CLAUDE.md dogfooding rules).

    Transient failures (a reasonless `remote rejected ... (failed)`, a dropped
    connection, an HTTP 5xx, a timeout) are retried with backoff; a real
    rejection is raised on the first answer, with the remote's own words."""
    tries = PUSH_ATTEMPTS if attempts is None else attempts
    delays = PUSH_BACKOFF_S if backoff_s is None else backoff_s
    out = ""
    code = 1
    for attempt in range(1, tries + 1):
        code, out = await _run(worktree, "push", "-u", "origin", branch, timeout_s=120.0)
        if code == 0:
            return
        if not push_failure_is_transient(out, code):
            raise PushError(f"git push rejected: {out}", output=out, attempts=attempt)
        if attempt < tries and delays:
            await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])
    raise PushError(
        f"git push failed after {tries} attempts (transient): {out}",
        output=out,
        retryable=True,
        attempts=tries,
    )
'''
assert old_push in s
s = s.replace(old_push, new_push)
p.write_text(s)
print("ok")
