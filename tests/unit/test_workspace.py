"""Workspace transport (ADR 0005): pack/stage roundtrips + the daemon API."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from rework.kernel.state.records import Mission, Run, RunStatus, new_id, run_id
from rework.kernel.state.store import StateStore
from rework.orchestrator.workspace import (
    EXCLUDE_DIRS,
    build_workspace_app,
    pack_directory,
    stage_bundle,
)


def _make_dir(tmp_path: Path) -> Path:
    src = tmp_path / "project"
    (src / "docs").mkdir(parents=True)
    (src / "notes.md").write_text("# notes\n")
    (src / "docs" / "spec.txt").write_text("spec body\n")
    (src / "__pycache__").mkdir()
    (src / "junk.pyc").write_text("bytecode")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main")
    return src


def test_pack_stage_roundtrip(tmp_path: Path) -> None:
    src = _make_dir(tmp_path)
    data = pack_directory(src)
    staged = stage_bundle(tmp_path / "workroot", data, "capstone-files")

    assert (staged / "notes.md").read_text() == "# notes\n"
    assert (staged / "docs" / "spec.txt").exists()
    # exclusions survive the round trip: no .git, no bytecode junk
    assert not (staged / ".git").exists()
    assert not (staged / "__pycache__").exists()
    assert not (staged / "junk.pyc").exists()
    assert staged.parent.name.startswith("ws-")


def test_pack_skips_excluded_dirs(tmp_path: Path) -> None:
    src = _make_dir(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(pack_directory(src)), mode="r:gz") as tar:
        names = tar.getnames()
    assert all(not any(part in EXCLUDE_DIRS for part in Path(n).parts) for n in names)
    assert all(not n.endswith(".pyc") for n in names)


async def test_workspace_api_upload_and_result(tmp_path: Path, store: StateStore) -> None:
    """POST /workspaces stages under the workroot; GET .../result serves the
    mission's latest worktree back as a tarball."""
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        src = _make_dir(tmp_path)
        data = pack_directory(src)
        resp = await client.post("/workspaces/capstone-files", data=data)
        assert resp.status == 200
        payload = await resp.json()
        staged = Path(payload["path"])
        ok = await asyncio.to_thread(lambda: staged.exists() and (staged / "notes.md").exists())
        assert ok
        assert payload["workspace_id"].startswith("ws-")

        # bad name rejected (aiohttp normalizes literal .., so this arrives
        # percent-encoded: the handler-side validator is what catches it)
        assert (await client.post("/workspaces/%2E%2E%2Fevil", data=data)).status == 400
        # empty body rejected
        assert (await client.post("/workspaces/x", data=b"")).status == 400

        # result: unknown mission -> 404
        assert (await client.get("/workspaces/results/m-nope")).status == 404

        # result: a mission whose latest code run carries a worktree
        mission = await store.create_mission(Mission(id=new_id("m"), objective="o"))
        wt = tmp_path / "wt"
        (wt / "out").mkdir(parents=True)
        (wt / "out" / "fixed.txt").write_text("done\n")
        (wt / "out" / ".pytest_cache").mkdir()
        await store.create_run(
            Run(
                id=run_id(mission.id, "code", "coder", 1),
                mission_id=mission.id,
                role="coder",
                step="code",
                status=RunStatus.COMPLETED,
                artifact={"worktree": str(wt), "branch": "rework/x"},
            )
        )
        resp = await client.get(f"/workspaces/results/{mission.id}")
        assert resp.status == 200
        out = tmp_path / "fetched"
        out.mkdir()
        with tarfile.open(fileobj=io.BytesIO(await resp.read()), mode="r:gz") as tar:
            tar.extractall(out, filter="data")
        assert (out / "out" / "fixed.txt").read_text() == "done\n"
        assert not (out / "out" / ".pytest_cache").exists()


async def test_token_auth(tmp_path: Path, store: StateStore, monkeypatch) -> None:
    """REWORK_TOKEN turns on bearer auth; tailnet-only deployments leave it off."""
    monkeypatch.setenv("REWORK_TOKEN", "sekrit")
    app = build_workspace_app(store, tmp_path / "workroot")
    bundle = pack_directory(_make_dir(tmp_path))
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/healthz")).status == 200  # healthz stays open
        assert (await client.post("/workspaces/x", data=bundle)).status == 401
        ok = await client.post(
            "/workspaces/x", data=bundle, headers={"Authorization": "Bearer sekrit"}
        )
        assert ok.status == 200  # past auth with the right token
        monkeypatch.delenv("REWORK_TOKEN")
