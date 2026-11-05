"""Complaints triage (M5.5): clustering + one-issue-per-loud-cluster."""

from __future__ import annotations

from typing import Any

import pytest

from rework.kernel.state.records import Complaint, ComplaintKind, ComplaintStatus
from rework.library.model import ItemType
from rework.library.triage import cluster_complaints, issue_body, similarity, triage


def _c(summary: str, kind: ComplaintKind = ComplaintKind.TOOL_MISSING, **kw: Any) -> Complaint:
    return Complaint(
        kind=kind,
        summary=summary,
        detail=kw.get("detail", ""),
        evidence=kw.get("evidence", []),
        suggestion=kw.get("suggestion", ""),
        source_role=kw.get("role", "coder"),
        source_mission_id=kw.get("mission", "m-1"),
    )


def test_similarity_token_overlap() -> None:
    a = _c("run_command jail escapes the worktree")
    b = _c("run_command jail escapes worktree via cd")
    c = _c("pytest grader ignores exit codes")
    assert similarity(a, b) >= 0.4
    assert similarity(a, c) < 0.4


def test_cluster_groups_by_kind_and_similarity() -> None:
    a = _c("run_command jail escapes worktree")
    b = _c("run_command jail escapes worktree via cd")
    c = _c("run_command jail escapes worktree", kind=ComplaintKind.WORKFLOW_GRIPE)
    d = _c("totally different topic about docs")
    clusters = cluster_complaints([a, b, c, d])
    # a+b cluster (same kind, similar), c alone (different kind), d alone
    sizes = sorted(len(cl.all) for cl in clusters)
    assert sizes == [1, 1, 2]
    loud = next(cl for cl in clusters if len(cl.all) == 2)
    assert {x.summary for x in loud.all} == {a.summary, b.summary}


def test_issue_body_quotes_every_member() -> None:
    a = _c(
        "run_command jail escapes worktree",
        detail="the shell cds out",
        evidence=["git status showed external paths"],
        suggestion="docker now",
    )
    b = _c("run_command jail escapes worktree via cd", role="auditor")
    body = issue_body(cluster_complaints([a, b])[0])
    assert a.id in body and b.id in body
    assert "the shell cds out" in body
    assert "docker now" in body
    assert "auditor" in body
    assert "never merge or close" in body


async def test_triage_opens_issue_and_flips_status() -> None:
    import tempfile
    from pathlib import Path

    from rework.kernel.state.sqlite import open_sqlite

    db = Path(tempfile.mkdtemp()) / "state.sqlite3"
    store = await open_sqlite(str(db))
    try:
        a = await store.file_complaint(_c("run_command jail escapes worktree"))
        b = await store.file_complaint(_c("run_command jail escapes worktree via cd"))
        await store.file_complaint(_c("docs typo in readme", kind=ComplaintKind.WORKFLOW_GRIPE))

        filed: list[tuple[str, str]] = []

        async def fake_gh(title: str, body: str, gh_bin: str = "gh", **kw: Any) -> str:
            filed.append((title, body))
            return f"https://github.com/roowus/rework/issues/{len(filed)}"

        results = await triage(store, create_issue=fake_gh)

        # one loud cluster -> one issue; the quiet complaint untouched
        assert len(results) == 1 and results[0]["issue"].endswith("/1")
        assert len(filed) == 1
        assert a.id in filed[0][1] and b.id in filed[0][1]
        got_a = await store.get_complaint(a.id)
        got_b = await store.get_complaint(b.id)
        assert got_a is not None and got_a.status == ComplaintStatus.ISSUE_OPEN
        assert got_b is not None and got_b.status == ComplaintStatus.ISSUE_OPEN
        quiet = await store.list_complaints(status=ComplaintStatus.OPEN)
        assert [c.summary for c in quiet] == ["docs typo in readme"]
    finally:
        await store.aclose()


async def test_triage_survives_gh_failure() -> None:
    import tempfile
    from pathlib import Path

    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(Path(tempfile.mkdtemp()) / "state.sqlite3"))
    try:
        a = await store.file_complaint(_c("run_command jail escapes worktree"))
        b = await store.file_complaint(_c("run_command jail escapes worktree via cd"))

        async def broken_gh(title: str, body: str, **kw: Any) -> None:
            return None

        results = await triage(store, create_issue=broken_gh)
        assert results and results[0]["issue"] is None
        # complaints stay OPEN: retry after fixing gh
        got = await store.get_complaint(a.id)
        assert got is not None and got.status == ComplaintStatus.OPEN
        _ = b
    finally:
        await store.aclose()


def test_external_gate_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """External ingest (M6.2): allowlisted host + owner approval, or denied."""
    from rework.library.curation import external_gate_status
    from rework.library.model import LibraryItem, Origin, Provenance

    def ext(url: str) -> Any:
        return LibraryItem(
            type=ItemType.SKILL,
            name="web thing",
            provenance=Provenance(origin=Origin.EXTERNAL, source_url=url),
        )

    # no allowlist configured -> nothing external can pass
    monkeypatch.delenv("REWORK_LIBRARY_ALLOWLIST", raising=False)
    ok, why = external_gate_status(ext("https://docs.python.org/3/x"))
    assert not ok and "ALLOWLIST" in why

    # allowlisted host -> passes (per-item approval already applied by the cmd)
    monkeypatch.setenv("REWORK_LIBRARY_ALLOWLIST", "docs.python.org, real.world")
    ok, why = external_gate_status(ext("https://docs.python.org/3/x"))
    assert ok and "docs.python.org" in why

    # a lookalike host is NOT on the list (exact host match)
    ok, _ = external_gate_status(ext("https://docs.python.org.evil.io/3/x"))
    assert not ok

    # internal items sail through
    internal = LibraryItem(type=ItemType.SKILL, name="mine")
    ok, why = external_gate_status(internal)
    assert ok and why == "internal"
