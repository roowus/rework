from pathlib import Path

p = Path("src/rework/nodes/gh.py")
s = p.read_text()

old = '''    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {text}")
    for token in text.split():
        if token.startswith("http"):
            return token
    raise RuntimeError(f"gh pr create produced no URL: {text}")
'''
new = '''    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise GHError(f"gh pr create failed: {text}", output=text)
    for token in text.split():
        if token.startswith("http"):
            return token
    raise GHError(f"gh pr create produced no URL: {text}", output=text)
'''
assert old in s
s = s.replace(old, new)

old_head = '''from __future__ import annotations

import asyncio
import os
from pathlib import Path


async def pr_create(
    repo_path: Path,
    head: str,
    title: str,
    body: str,
    base: str = "main",
    timeout_s: float = 60.0,
) -> str:
    """Create a PR from `head` into `base`; returns the PR URL."""
    gh_bin = os.environ.get("REWORK_GH_BIN", "gh")
'''
new_head = '''from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

PR_ATTEMPTS = 3
PR_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)

# `gh` (or GitHub behind it) hiccuped: the same call can succeed next time.
_PR_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "early eof",
    "tls",
    "broken pipe",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "server error",
    "bad gateway",
)
# The forge already answered (a PR exists, the token is not allowed): repeating
# the call changes nothing and can only confuse the owner.
_PR_DEFINITIVE_MARKERS = (
    "already exists",
    "already been taken",
    "authentication",
    "not logged in",
    "permission",
    "forbidden",
    "unauthorized",
    "gh_token",
    "no commits between",
    "invalid",
)
_PR_5XX = re.compile(r"\\b5\\d\\d\\b")


class GHError(RuntimeError):
    """A `gh` call that failed. `output` is gh's full output; `retryable` says
    whether calling again could plausibly work."""

    def __init__(self, message: str, *, output: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.output = output
        self.retryable = retryable


def pr_failure_is_transient(output: str) -> bool:
    """Is this `gh pr create` failure worth another attempt? Conservative on
    purpose: only a timeout or a 5xx, never "already exists" or an auth error."""
    text = (output or "").lower()
    if any(marker in text for marker in _PR_DEFINITIVE_MARKERS):
        return False
    if _PR_5XX.search(text):
        return True
    return any(marker in text for marker in _PR_TRANSIENT_MARKERS)


async def pr_create(
    repo_path: Path,
    head: str,
    title: str,
    body: str,
    base: str = "main",
    timeout_s: float = 60.0,
    *,
    attempts: int | None = None,
    backoff_s: tuple[float, ...] | None = None,
) -> str:
    """Create a PR from `head` into `base`; returns the PR URL.

    Clearly transient failures (a timeout, a 5xx) get a few attempts with
    backoff; anything the forge actually decided is raised on the first
    answer (rerunning would only add noise to the owner's report)."""
    tries = PR_ATTEMPTS if attempts is None else attempts
    delays = PR_BACKOFF_S if backoff_s is None else backoff_s
    failure = ""
    for attempt in range(1, tries + 1):
        try:
            return await _pr_create_once(repo_path, head, title, body, base, timeout_s)
        except GHError as exc:
            if not exc.retryable:
                raise
            failure = str(exc)
            if attempt < tries and delays:
                await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])
    raise GHError(f"{failure}\\n(gave up after {tries} attempts)", output=failure, retryable=True)


async def _pr_create_once(
    repo_path: Path,
    head: str,
    title: str,
    body: str,
    base: str,
    timeout_s: float,
) -> str:
    gh_bin = os.environ.get("REWORK_GH_BIN", "gh")
'''
assert old_head in s
s = s.replace(old_head, new_head)

old_timeout = '''    except TimeoutError:
        proc.terminate()
        raise RuntimeError("gh pr create timed out") from None
'''
new_timeout = '''    except TimeoutError:
        proc.terminate()
        raise GHError("gh pr create timed out", output="timed out", retryable=True) from None
'''
assert old_timeout in s
s = s.replace(old_timeout, new_timeout)

old_raise = '''    if proc.returncode != 0:
        raise GHError(f"gh pr create failed: {text}", output=text)
'''
new_raise = '''    if proc.returncode != 0:
        raise GHError(
            f"gh pr create failed: {text}", output=text, retryable=pr_failure_is_transient(text)
        )
'''
s = s.replace(old_raise, new_raise)
p.write_text(s)
print("ok")
