"""GitHub operations via the gh CLI (thin, mechanical, no gate by default).

After a clean audited `done` the engine pushes the branch and opens the PR with
no gate; the orchestrator may request one (`ask_before_pr` + reason) and a
project `settings.pr_gate` of `always`/`never` overrides. Merging is always the
owner's. REWORK_GH_BIN overrides the binary (tests use a fake that records the
call and prints a URL).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rework.nodes.failure_text import HTTP_5XX, marker_regex, scrub

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
# the call changes nothing and can only confuse the owner. Only phrases the
# forge itself DECIDED belong here. Broad words (`invalid`, a bare
# `authentication`) fire on proxy/init noise such as `invalid response`, so
# they are deliberately absent.
# Principle: a wasted retry costs seconds; a false "real rejection" costs the
# mission's PR (the owner is told to fix something they cannot). When in
# doubt, retry.
_PR_DEFINITIVE_MARKERS = (
    "already exists",
    "already been taken",
    "authentication failed",
    "not logged in",
    "permission",
    "forbidden",
    "unauthorized",
    "gh_token",
    "no commits between",
    "protected branch",
    "gh006",
    "gh013",
)
_PR_TRANSIENT_RE = marker_regex(_PR_TRANSIENT_MARKERS)
_PR_DEFINITIVE_RE = marker_regex(_PR_DEFINITIVE_MARKERS)


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
    text = scrub(output)
    if _PR_DEFINITIVE_RE.search(text):
        return False
    if HTTP_5XX.search(text):
        return True
    return bool(_PR_TRANSIENT_RE.search(text))


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
    raise GHError(f"{failure}\n(gave up after {tries} attempts)", output=failure, retryable=True)


async def _pr_create_once(
    repo_path: Path,
    head: str,
    title: str,
    body: str,
    base: str,
    timeout_s: float,
) -> str:
    gh_bin = os.environ.get("REWORK_GH_BIN", "gh")
    proc = await asyncio.create_subprocess_exec(
        gh_bin,
        "pr",
        "create",
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        raise GHError("gh pr create timed out", output="timed out", retryable=True) from None
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise GHError(
            f"gh pr create failed: {text}", output=text, retryable=pr_failure_is_transient(text)
        )
    for token in text.split():
        if token.startswith("http"):
            return token
    raise GHError(f"gh pr create produced no URL: {text}", output=text)
