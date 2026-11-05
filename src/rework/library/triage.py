"""Complaints triage (ADR 0004 tail, M5.5): stack similar complaints, open
ONE GitHub issue when a cluster gets loud, close the loop in the store.

Clustering is deliberately dumb (token-overlap Jaccard within a kind): the
point is a human-readable inbox drain, not ML. A cluster that crosses
`min_cluster` opens one issue titled from the loudest complaint, with every
member quoted; members flip to ISSUE_OPEN with the issue URL so they never
double-file. gh is invoked through the repo's existing nodes.gh seam
(REWORK_GH_BIN override), faked in tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from rework.kernel.state.records import Complaint, ComplaintStatus
from rework.kernel.state.store import StateStore

DEFAULT_MIN_CLUSTER = 2  # owner knob via config later (ADR 0004 thresholds)


def _tokens(text: str) -> set[str]:
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "is",
        "was",
        "it",
        "this",
        "that",
        "be",
        "as",
        "at",
        "by",
        "but",
        "not",
        "no",
        "my",
        "i",
        "when",
        "so",
        "if",
        "from",
    }
    return {t.strip(".,:;!?()[]'\"") for t in text.lower().split()} - stop


def similarity(a: Complaint, b: Complaint) -> float:
    """Jaccard over summary tokens; same kind required by the caller."""
    ta, tb = _tokens(a.summary), _tokens(b.summary)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Cluster:
    seed: Complaint
    members: list[Complaint] = field(default_factory=list)

    @property
    def all(self) -> list[Complaint]:
        return [self.seed, *self.members]


def cluster_complaints(
    complaints: list[Complaint],
    threshold: float = 0.4,
) -> list[Cluster]:
    """Greedy single-pass clustering within each kind: a complaint joins the
    first cluster whose seed it resembles; otherwise it seeds a new one."""
    clusters: list[Cluster] = []
    for c in complaints:
        home = next(
            (
                cl
                for cl in clusters
                if cl.seed.kind == c.kind and similarity(cl.seed, c) >= threshold
            ),
            None,
        )
        if home is not None:
            home.members.append(c)
        else:
            clusters.append(Cluster(seed=c))
    return clusters


async def _gh_issue_create(
    title: str, body: str, gh_bin: str, cwd: str | None = None
) -> str | None:
    """`gh issue create` via subprocess; returns the URL or None. Patched in
    tests (the fake never shells out)."""
    proc = await asyncio.create_subprocess_exec(
        gh_bin,
        "issue",
        "create",
        "--title",
        title,
        "--body",
        body,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except TimeoutError:
        proc.terminate()
        return None
    if proc.returncode != 0:
        return None
    text = out.decode(errors="replace").strip()
    url = next((tok for tok in text.split() if tok.startswith("http")), None)
    return url


def issue_title(cluster: Cluster) -> str:
    return cluster.seed.summary[:120]


def issue_body(cluster: Cluster) -> str:
    lines = [
        f"{len(cluster.all)} similar complaint(s) stacked up (ADR 0004 triage, "
        f"kind: {cluster.seed.kind.value}).",
        "",
    ]
    for c in cluster.all:
        lines.append(f"### {c.id} ({c.source_role or 'unknown role'})")
        lines.append(c.summary)
        lines.append(c.detail)
        if c.evidence:
            lines.append("")
            lines += [f"- {e}" for e in c.evidence]
        if c.suggestion:
            lines.append(f"\nSuggestion: {c.suggestion}")
        lines.append("")
    lines.append("_Filed mechanically by rework triage; agents never merge or close._")
    return "\n".join(lines)


async def triage(
    store: StateStore,
    *,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
    gh_bin: str = "gh",
    create_issue: Any = _gh_issue_create,
    repo_cwd: str | None = None,
) -> list[dict[str, Any]]:
    """The pass: cluster OPEN complaints, file one issue per loud cluster,
    flip members to issue_open with the URL. Returns a summary list."""
    open_complaints = await store.list_complaints(status=ComplaintStatus.OPEN, limit=200)
    if not open_complaints:
        return []
    results: list[dict[str, Any]] = []
    for cluster in cluster_complaints(open_complaints):
        if len(cluster.all) < min_cluster:
            continue
        title, body = issue_title(cluster), issue_body(cluster)
        url = await create_issue(title, body)
        if url is None:
            results.append(
                {
                    "cluster": [c.id for c in cluster.all],
                    "issue": None,
                    "note": "gh issue create failed; complaints stay open",
                }
            )
            continue
        for c in cluster.all:
            await store.update_complaint(c.id, status=ComplaintStatus.ISSUE_OPEN)
        results.append({"cluster": [c.id for c in cluster.all], "issue": url})
    return results
