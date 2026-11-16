"""Repeatable report hand-off: a finished mission's report body into the Library.

`rework report MISSION_ID --to-library` is the only caller. Copying a report by
hand means `library add`, which mints a fresh `l-<hex>` id on every run; that is
the wrong shape for a hand-off the owner repeats. This module makes the copy
idempotent and says out loud which source of the body it used.

Body resolution, highest priority first:
(a) an explicit `--from-file PATH`: the owner knows which file is the report;
(b) the mission's own docs markdown, when a role worktree or a branch of the
    mission still exists: the largest NEWLY ADDED `.md` under `docs/` (an audit
    or a design note is a document the mission wrote, not one it edited). Only
    when nothing was added does the largest merely EDITED docs markdown stand
    in, loudly: it is tagged `source:doc-modified` and a warning is printed,
    because an edited pre-existing doc is not a purpose-written report;
(c) `missions.result.summary`, LABELLED, because that field describes the work,
    it does not contain the report. A summary-only item is tagged
    `source:summary` so nobody mistakes it for the full document later.

Dedupe key: `(type=report, provenance.mission_id, hand-off marker)`.
`find_report_item` is that lookup. Role contributions get `provenance.mission_id`
stamped automatically too, so the mission id alone would match (and overwrite)
a report a role authored. What limits a re-run to items this command filed is
`provenance.role` being empty, checked before anything else: every key that
follows is role-supplied. A role chooses its own tags, so it can paste
`handoff:report` into its own item, and it can choose a `mission:<id>` tag, a
`source:<kind>` tag and the hand-off description shape, which is the backfill
fingerprint for items filed before the marker existed. What a role cannot do is
leave `provenance.role` empty: the library tool wrapper stamps the role it runs
under onto every contribution. So a role contribution is never a match,
whatever tags and description it picks, while an item this command filed (no
role recorded) matches by marker, or by fingerprint when it predates the
marker.

A re-run UPDATES the item in place (its id, body path, created_at, uses and
status survive, its version is bumped) instead of minting a duplicate, which
mirrors the re-run safety `library backfill-bodies` already promises. A mission
can therefore own exactly one handed-off report.

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
KIND_DOC_MODIFIED = "doc-modified"
KIND_SUMMARY = "summary"
_KINDS = (KIND_FILE, KIND_WORKTREE, KIND_DOC_MODIFIED, KIND_SUMMARY)

# Marks an item this command filed. A role CAN put this tag on its own item
# (tags are role-supplied), so is_handoff_item() only trusts it on an item whose
# provenance.role is empty.
HANDOFF_TAG = "handoff:report"
# Records which `source:<kind>` tag this command derived for the item, so a
# re-run replaces exactly that tag and leaves an owner `--tag source:...` (even
# one that names another source kind) alone.
_DERIVED_PREFIX = "handoff:derived:"
_DESCRIPTION_PREFIX = "Report for mission "
_SOURCE_PREFIX = "source:"

DOCS_PREFIX = "docs"
_MD_SUFFIX = ".md"


@dataclass(frozen=True)
class ReportBody:
    """One resolved report body and where it came from."""

    text: str
    kind: str  # from-file | worktree | doc-modified | summary
    detail: str  # human readable, e.g. "docs/audits/codebase-audit.md (worktree)"

    @property
    def is_full_report(self) -> bool:
        """False for the fallbacks that are not a purpose-written report."""
        return self.kind not in (KIND_SUMMARY, KIND_DOC_MODIFIED)

    @property
    def warning(self) -> str | None:
        """What the owner must know when the body is not the report itself."""
        if self.kind == KIND_SUMMARY:
            return (
                "the summary describes the report, it does not contain it;"
                " pass --from-file to hand off the document itself"
            )
        if self.kind == KIND_DOC_MODIFIED:
            return (
                "no new report document was found; the body is a docs file the mission"
                " edited, not one it wrote; pass --from-file to hand off the report itself"
            )
        return None


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


def _changed_docs(checkout: Path, ref: str) -> tuple[list[str], list[str]]:
    """(added, modified) `docs/**/*.md` of the mission. For a working-tree ref
    ("HEAD") the uncommitted and untracked files count too (untracked ones are
    added): a role does not always commit its report. With no base ref at all
    nothing can be called new, so every doc of that commit is "modified"."""
    base = _base_ref(checkout, ref)
    if base is None:
        names = _git(checkout, "ls-tree", "-r", "--name-only", ref, "--", DOCS_PREFIX)
        return [], _md(names)
    span = base if ref == "HEAD" else f"{base}..{ref}"
    added_out = _git(checkout, "diff", "--name-only", "--diff-filter=A", span, "--", DOCS_PREFIX)
    changed_out = _git(checkout, "diff", "--name-only", span, "--", DOCS_PREFIX)
    if ref == "HEAD":
        added_out += _git(checkout, "ls-files", "--others", "--exclude-standard", "--", DOCS_PREFIX)
    added = _md(added_out)
    return added, [n for n in _md(changed_out) if n not in added]


def _md(names: str) -> list[str]:
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


def _largest(checkout: Path, ref: str, names: list[str]) -> tuple[str, str] | None:
    best: tuple[int, str] | None = None
    for name in names:
        size = _doc_size(checkout, ref, name)
        if size and (best is None or size > best[0]):
            best = (size, name)
    if best is None:
        return None
    text = _doc_text(checkout, ref, best[1])
    if not text or not text.strip():
        return None
    return text, best[1]


def largest_changed_doc(checkout: Path, ref: str) -> tuple[str, str, bool] | None:
    """(body, repo-relative path, newly_added) for the biggest `.md` under docs/
    this checkout added, else the biggest it merely edited; None when it has
    no such document. A newly added doc always wins over a bigger edited one."""
    added, modified = _changed_docs(checkout, ref)
    found = _largest(checkout, ref, added)
    if found is not None:
        return found[0], found[1], True
    found = _largest(checkout, ref, modified)
    if found is not None:
        return found[0], found[1], False
    return None


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
    branch that still exists. A newly added doc from ANY surviving source beats
    an edited one; an edited doc is returned (kind `doc-modified`) only when no
    source has an added one. None means "no document found", so the caller can
    fall back to the summary."""
    runs = await store.list_runs(mission.id)
    sources = [(wt, "HEAD", "worktree") for wt in _worktrees(mission, runs)]
    sources += [(repo, branch, f"branch {branch}") for repo, branch in _branches(mission, runs)]
    fallback: ReportBody | None = None
    for checkout, ref, label in sources:
        found = largest_changed_doc(checkout, ref)
        if found is None:
            continue
        text, name, added = found
        if added:
            return ReportBody(text=text, kind=KIND_WORKTREE, detail=f"{name} ({label})")
        if fallback is None:
            fallback = ReportBody(
                text=text, kind=KIND_DOC_MODIFIED, detail=f"{name} (edited, {label})"
            )
    return fallback


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
    tags = [
        mission_tag(mission_id),
        ItemType.REPORT.value,
        HANDOFF_TAG,
        f"source:{body.kind}",
        f"{_DERIVED_PREFIX}{body.kind}",
    ]
    for tag in extra_tags or []:
        if tag and tag not in tags:
            tags.append(tag)
    return f"{header_for(mission_id, body)}\n\n{body.text}", tags


def is_handoff_item(item: LibraryItem, mission_id: str) -> bool:
    """True only for a report item this command filed for `mission_id`.

    provenance.mission_id alone is not enough: role contributions are stamped
    with it too. The role gate is checked FIRST, before the marker and before
    the fingerprint, because both of those keys are role-supplied: a role picks
    its own tags through library_contribute, so it can mint an item carrying
    `handoff:report` and be mistaken for this command's work. What a role
    cannot do is leave `provenance.role` empty, because
    LibraryAwareExecutor.execute stamps the role it runs under onto every
    contribution. The test is "falsy", so both None and "" count as no role
    recorded (an item the owner adds by hand, or one filed by `library add`
    without --role); any non-empty role means "not mine", whatever tags and
    description it picked. With that gate passed, the marker decides, and an
    item filed before the marker existed is still recognised by the
    fingerprint: mission tag + source tag + the hand-off description."""
    if item.type is not ItemType.REPORT or item.provenance.mission_id != mission_id:
        return False
    if item.provenance.role:
        return False
    if HANDOFF_TAG in item.tags:
        return True
    return (
        mission_tag(mission_id) in item.tags
        and any(t in {f"{_SOURCE_PREFIX}{k}" for k in _KINDS} for t in item.tags)
        and item.description.startswith(f"{_DESCRIPTION_PREFIX}{mission_id};")
    )


async def find_report_item(index: Any, mission_id: str) -> LibraryItem | None:
    """The dedupe lookup: a report item THIS command filed for the mission (type
    report, provenance mission id, hand-off marker). Oldest wins, so a mission
    that somehow owns two converges on the first one instead of flipping."""
    items = await index.list_items(type_=ItemType.REPORT, limit=100_000)
    matches = [i for i in items if is_handoff_item(i, mission_id)]
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
    name: str | None = None,
    extra_tags: list[str] | None = None,
    dry_run: bool = False,
) -> tuple[LibraryItem, bool, str]:
    """Write (or plan) the item. Returns (item, created, note).

    Creation goes through `curation.contribute`, so a report gets the same git
    body file, status policy and note as any other item. An existing item is
    updated in place through the same git writer plus the same index upsert,
    which is what makes a second run a safe re-run rather than a duplicate. On
    `dry_run` neither path is taken: no body file, no index row.

    `name` is the owner's --title: an update changes the item name only when it
    is given, and tags are merged (union), so a bare re-run never reverts what
    an earlier run or the owner set. Only the `source:<kind>` tag this command
    derived last time is dropped, and the `handoff:derived:` tag records which
    one that was; an owner-supplied `--tag source:...` is left alone even when
    it names another source kind.
    """
    from rework.library.curation import accept_status, contribute

    full_body, tags = compose(mission.id, body, extra_tags)
    description = f"{_DESCRIPTION_PREFIX}{mission.id}; body from {body.detail}"
    existing = await find_report_item(index, mission.id)
    if existing is not None:
        item = existing.model_copy(deep=True)
        if name:
            item.name = name
        item.description = description
        # only the source tag this command derived LAST TIME is replaced (the
        # derived marker tag says which one); an owner-supplied
        # `--tag source:...` shares the namespace and is kept, so a bare
        # re-run never eats it
        previous = {t[len(_DERIVED_PREFIX) :] for t in item.tags if t.startswith(_DERIVED_PREFIX)}
        replaced = {f"{_SOURCE_PREFIX}{kind}" for kind in previous}
        item.tags = [
            t for t in item.tags if t not in replaced and not t.startswith(_DERIVED_PREFIX)
        ]
        item.tags += [t for t in tags if t not in item.tags]
        item.body = full_body
        item.touch()
        if not dry_run:
            item.body_path = await asyncio.to_thread(
                git.write_body, item, full_body, f"library: update report {item.name!r}"
            )
            item.body = full_body
            await index.upsert_item(item)
        return item, False, "update"
    name = name or f"Report: {mission.id}"
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
    if body.warning:
        lines.append(f"  WARNING: {body.warning}")
    return lines
