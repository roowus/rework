"""Helpers for classifying a tool's failure text (git push, `gh pr create`).

The classifiers match marker phrases in git/gh output. Done naively (plain
substring tests on the whole text) they fire on UNRELATED text: a branch called
`rework/fix-permission-gate`, a repo path containing `ssl`, a progress counter
like `(512/512)`. So the text is scrubbed of ref names, URLs, paths and quoted
values first, and markers only match as whole tokens.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_URL = re.compile(r"\S+://\S+")
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
# `] <ref> -> <ref>` on a push status line
_REF_PAIR = re.compile(r"(\])\s+\S+\s+->\s+\S+")
# path-like tokens (branches, repo paths, `(512/512)`), but keep `HTTP/1.1`
_PATHLIKE = re.compile(r"(?<!\S)(?!http/\d)\S*/\S*", re.IGNORECASE)


def scrub(text: str) -> str:
    """Lower-cased `text` without the parts that carry names, not meaning."""
    out = _URL.sub(" ", text or "")
    out = _QUOTED.sub(" ", out)
    out = _REF_PAIR.sub(r"\1 -> ", out)
    out = _PATHLIKE.sub(" ", out)
    return out.lower()


def marker_regex(markers: Iterable[str]) -> re.Pattern[str]:
    """One regex matching any marker as a whole token (letters/digits on either
    side disqualify: `ssl` must not match inside `classless`)."""
    alternation = "|".join(re.escape(m) for m in markers)
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


# A 5xx status in git/gh output: `HTTP 503`, `status: 502`, and the bare curl
# form `The requested URL returned error: 503`. Shared by both classifiers.
HTTP_5XX = re.compile(r"\b(?:http|status|error|returned error)[/:\s.\d]*\b5\d\d\b")

# GitHub's primary/secondary rate limit and abuse detection: often HTTP 403 (the
# permission status) but a "wait and retry", never a rejection to fix.
RATE_LIMIT = marker_regex(
    ("secondary rate limit", "rate limit", "rate limited", "abuse detection", "too many requests")
)
