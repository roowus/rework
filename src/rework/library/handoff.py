"""Repeatable report hand-off: a finished mission's report body into the Library.

`rework report MISSION_ID --to-library` is the only caller. Copying a report by
hand means `library add`, which mints a fresh `l-<hex>` id on every run; that is
the wrong shape for a hand-off the owner repeats. This module makes the copy
idempotent and says out loud which source of the body it used.

Body resolution, highest priority first:
(a) an explicit `--from-file PATH`: the owner knows which file is the report;
(b) the mission's own docs markdown, when a role worktree or a branch of the
    mission still exists: the largest changed `.md` under `docs/` (an audit or
    a design note is normally the biggest document a mission adds, and its size
    is what separates it from an incidental `docs/` edit);
(c) `missions.result.summary`, LABELLED, because that field describes the work,
    it does not contain the report. A summary-only item is tagged
    `source:summary` so nobody mistakes it for the full document later.

Dedupe key: `(type=report, provenance.mission_id)`. `find_report_item` is that
lookup. A re-run for the same mission UPDATES the item in place (its id, body
path, created_at, uses and status survive, its version is bumped) instead of
minting a duplicate, which mirrors the re-run safety `library backfill-bodies`
already promises. A mission can therefore own exactly one handed-off report.

Nothing here is byte-faithful on the way out by accident: the body is a one-line
header that links the mission and names the source, a blank line, and then the
resolved text unmodified. No reflow, no reformatting.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rework.library.model import ItemType, LibraryItem, Origin, Provenance

# source kinds; they ride along as `source:<kind>` tags on the item
KIND_FILE = "from-file"
KIND_WORKTREE = "worktree"
KIND_SUMMARY = "summary"

DOCS_PREFIX = "docs"
_MD_SUFFIX = ".md"


@dataclass(frozen=True)
class ReportBody:
    """One resolved report body and where it came from."""

    text: str
    kind: str  # from-file | worktree | summary
    detail: str  # human readable, e.g. "docs/audits/codebase-audit.md (worktree)"

    @property
    def is_full_report(self) -> bool:
        """False only for the summary fallback, which describes the report."""
        return self.kind != KIND_SUMMARY


def mission_tag(mission_id: str) -> str:
    """The tag that makes a mission's report findable from search."""
    return f"mission:{mission_id}"


def _git(cwd: Path, *args: str) -> str:
    """stdout of a git command, or "" when it fails. A hand-off never dies
    because a checkout is half gone: that just moves it down the priority
    list."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _base_ref(checkout: Path, ref: str) -> str | None:
    """The ref a mission's work is measured against: the remote HEAD when the
    clone has one, else a local main/master that is not the ref itself, else
    the ref's previous commit."""
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if candidate == ref:
            continue
        if _git(checkout, "rev-parse", "--verify", "--quiet", candidate):
            return candidate
    if _git(checkout, "rev-parse", "--verify", "--quiet", f"{ref}~1"):
        return f"{ref}~1"
    return None


def _changed_docs(checkout: Path, ref: str) -> list[str]:
    """`docs/**/*.md` the mission added or changed. For a working-tree ref
    ("HEAD") the uncommitted and untracked files count too: a role does not
    always commit its report. With no base ref at all (a single-commit clone)
    every doc of that commit is a candidate."""
    base = _base_ref(checkout, ref)
    if base is None:
        names = _git(checkout, "ls-tree", "-r", "--name-only", ref, "--", DOCS_PREFIX)
    elif ref == "HEAD":
        names = _git(checkout, "diff", "--name-only", base, "--", DOCS_PREFIX)
        names += _git(checkout, "ls-files", "--others", "--exclude-standard", "--", DOCS_PREFIX)
    else:
        names = _git(checkout, "diff", "--name-only", f"{base}..{ref}", "--", DOCS_PREFIX)
    return [n for n in dict.fromkeys(names.splitlines()) if n.endswith(_MD_SUFFIX)]


def _doc_text(checkout: Path, ref: str, name: str) -> str | None:
    """The body of one doc at `ref`; the working tree wins for HEAD."""
    path = checkout / name
    if ref == "HEAD" and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return _git(checkout, "show", f"{ref}:{name}") or None


def _doc_size(checkout: Path, ref: str, name: str) -> int:
    path = checkout / name
    if ref == "HEAD" and path.is_file():
        return path.stat().st_size
    out = _git(checkout, "cat-file", "-s", f"{ref}:{name}").strip()
    try:
        return int(out)
    except ValueError:
        return 0


def largest_changed_doc(checkout: Path, ref: str) -> tuple[str, str] | None:
    """(body, repo-relative path) for the biggest changed `.md` under docs/, or
    None when this checkout has no such document."""
    best: tuple[int, str] | None = None
    for name in _changed_docs(checkout, ref):
        size = _doc_size(checkout, ref, name)
        if size and (best is None or size > best[0]):
            best = (size, name)
    if best is None:
        return None
    text = _doc_text(checkout, ref, best[1])
    if not text or not text.strip():
        return None
    return text, best[1]


def _worktrees(mission: Any, runs: list[Any]) -> list[Path]:
    """Role worktrees of this mission that still exist, newest run first."""
    found: list[Path] = []
    for run in reversed(runs):
        path = str((run.artifact or {}).get("worktree") or "")
        if path and Path(path).is_dir():
            found.append(Path(path))
    return found


def _branches(mission: Any, runs: list[Any]) -> list[tuple[Path, str]]:
    """(repo, branch) pairs to try when no worktree survives. The branch is a
    ref of the mission's own clone, which `rework gc` keeps on purpose."""
    repo = Path(str(mission.repo_path or ""))
    if not (repo.is_dir() and (repo / ".git").exists()):
        return []
    pairs: list[tuple[Path, str]] = []
    for run in reversed(runs):
        branch = str((run.artifact or {}).get("branch") or "")
        if branch and branch not in {b for _, b in pairs}:
            pairs.append((repo, branch))
    return pairs


async def resolve_from_mission(store: Any, mission: Any) -> ReportBody | None:
    """(b) then nothing: the mission's own docs markdown from a worktree or a
    branch that still exists. None means "no document found", so the caller can
    fall back to the summary."""
    runs = await store.list_runs(mission.id)
    for worktree in _worktrees(mission, runs):
        found = largest_changed_doc(worktree, "HEAD")
        if found is not None:
            text, name = found
            return ReportBody(text=text, kind=KIND_WORKTREE, detail=f"{name} (worktree)")
    for repo, branch in _branches(mission, runs):
        found = largest_changed_doc(repo, branch)
        if found is not None:
            text, name = found
            return ReportBody(text=text, kind=KIND_WORKTREE, detail=f"{name} (branch {branch})")
    return None


def from_file(path: str) -> ReportBody:
    """(a) an explicit path the owner named. Must exist and carry text: an
    empty file is a mistake, not a report."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise ValueError(f"--from-file not found: {src}")
    text = src.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"--from-file is empty: {src}")
    return ReportBody(text=text, kind=KIND_FILE, detail=str(src))


def summary_body(mission: Any) -> ReportBody | None:
    """(c) the labelled fallback: missions.result.summary describes the report,
    it does not contain it. Only used when nothing better exists."""
    summary = str(((mission.result or {}) or {}).get("summary") or "")
    if not summary.strip():
        return None
    return ReportBody(
        text=summary,
        kind=KIND_SUMMARY,
        detail="missions.result.summary (a description of the report, not the report)",
    )


def header_for(mission_id: str, body: ReportBody) -> str:
    """One header line linking the mission and naming the source, then a blank
    line, then the body exactly as it was resolved."""
    return f"Handed off from rework mission {mission_id}. Source: {body.detail}."


def compose(
    mission_id: str, body: ReportBody, extra_tags: list[str] | None = None
) -> tuple[str, list[str]]:
    """(full body, tags) for this hand-off. The tags carry the mission id and
    the source kind, so a summary-only item is never mistaken for a report."""
    tags = [mission_tag(mission_id), ItemType.REPORT.value, f"source:{body.kind}"]
    for tag in extra_tags or []:
        if tag and tag not in tags:
            tags.append(tag)
    return f"{header_for(mission_id, body)}\n\n{body.text}", tags


async def find_report_item(index: Any, mission_id: str) -> LibraryItem | None:
    """The dedupe lookup: a report item whose provenance carries this mission
    id. Oldest wins, so a mission that somehow owns two converges on the first
    one instead of flipping between them."""
    items = await index.list_items(type_=ItemType.REPORT, limit=100_000)
    matches = [i for i in items if i.provenance.mission_id == mission_id]
    if not matches:
        return None
    matches.sort(key=lambda i: i.created_at)
    return matches[0]


async def hand_off(
    index: Any,
    git: Any,
    mission: Any,
    *,
    body: ReportBody,
    name: str,
    extra_tags: list[str] | None = None,
    dry_run: bool = False,
) -> tuple[LibraryItem, bool, str]:
    """Write (or plan) the item. Returns (item, created, note).

    Creation goes through `curation.contribute`, so a report gets the same git
    body file, status policy and note as any other item. An existing item is
    updated in place through the same git writer plus the same index upsert,
    which is what makes a second run a safe re-run rather than a duplicate. On
    `dry_run` neither path is taken: no body file, no index row.
    """
    from rework.library.curation import accept_status, contribute

    full_body, tags = compose(mission.id, body, extra_tags)
    description = f"Report for mission {mission.id}; body from {body.detail}"
    existing = await find_report_item(index, mission.id)
    if existing is not None:
        item = existing.model_copy(deep=True)
        item.name = name
        item.description = description
        item.tags = tags
        item.body = full_body
        item.touch()
        if not dry_run:
            item.body_path = await asyncio.to_thread(
                git.write_body, item, full_body, f"library: update report {name!r}"
            )
            item.body = full_body
            await index.upsert_item(item)
        return item, False, "update"
    planned = LibraryItem(
        type=ItemType.REPORT,
        name=name,
        description=description,
        tags=tags,
        provenance=Provenance(origin=Origin.INTERNAL, mission_id=mission.id),
        body=full_body,
    )
    planned.status = accept_status(planned.type, planned.provenance.origin)
    if dry_run:
        return planned, True, "create"
    item, _note = await contribute(
        index,
        git,
        type_=ItemType.REPORT,
        name=name,
        description=description,
        body=full_body,
        tags=tags,
        mission_id=mission.id,
    )
    return item, True, "create"


# verb -> (what happened, what a dry run would do). `rework gc` prints "would
# remove" for a dry run and "remove" otherwise; a hand-off says which of the
# two writes it planned, so the owner can tell a first filing from a re-run.
_ACTIONS = {
    "create": ("created", "would create"),
    "update": ("updated", "would update"),
}


def describe(item: LibraryItem, body: ReportBody, dry_run: bool, verb: str) -> list[str]:
    """The owner-facing lines for a hand-off: the id, name, type, status, tags,
    body length and the source, prefixed with what happens to that item."""
    done, planned = _ACTIONS[verb]
    action = planned if dry_run else done
    lines = [
        f"{action} report {item.id}  [{item.type.value}/{item.status.value}]",
        f"  name: {item.name}",
        f"  tags: {', '.join(item.tags)}",
        f"  source: {body.kind} ({body.detail})",
        f"  body: {len(item.body)} chars",
    ]
    if not body.is_full_report:
        lines.append(
            "  note: the summary describes the report, it does not contain it;"
            " pass --from-file to hand off the document itself"
        )
    return lines
