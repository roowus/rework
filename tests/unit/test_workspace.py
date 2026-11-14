"""Workspace transport (ADR 0005): pack/stage roundtrips + the daemon API."""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rework.config import project_files_dir
from rework.kernel.state.records import Mission, Project, Run, RunStatus, new_id, run_id
from rework.kernel.state.store import StateStore
from rework.orchestrator.workspace import (
    EXCLUDE_DIRS,
    _area_lock,
    archive_files_area,
    build_workspace_app,
    ensure_files_area,
    mission_changes,
    pack_directory,
    pack_paths,
    save_mission_files,
    seed_mission_workspace,
    stage_bundle,
    update_files_area,
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
    # the uploaded junk is gone, but the daemon git-initialized the stage:
    # the coder's worktree stage requires a repo (ADR 0005 parity with
    # _prepare_repo). Only .git survives (a fresh one), nothing from the pack
    assert not (staged / "__pycache__").exists()
    assert not (staged / "junk.pyc").exists()
    assert staged.parent.name.startswith("ws-")
    import subprocess as _sp

    log = _sp.run(["git", "log", "--oneline"], cwd=staged, capture_output=True, text=True)
    assert "init" in log.stdout


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


async def _upload_project(store: StateStore) -> Project:
    """A repo-less project: the one that travels by the upload transport."""
    return await store.create_project(Project(name="p", settings={"repos": []}))


async def test_project_files_routes_roundtrip(
    tmp_path: Path, store: StateStore, monkeypatch
) -> None:
    """POST /projects/<id>/files lands files in the area; GET .../archive gives
    them back. This is the whole `rework project add-files` / `pull` surface."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    project = await _upload_project(store)
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.md").write_text("# a\n")
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/projects/{project.id}/files", data=pack_paths([local / "a.md"]))
        assert resp.status == 200
        payload = await resp.json()
        assert payload["changed"], "the upload must report what it wrote"
        assert any("a.md" in line for line in payload["changed"])

        resp = await client.get(f"/projects/{project.id}/files/archive")
        assert resp.status == 200
        pulled = tmp_path / "pulled"
        pulled.mkdir()
        with tarfile.open(fileobj=io.BytesIO(await resp.read()), mode="r:gz") as tar:
            tar.extractall(pulled, filter="data")
        assert (pulled / "a.md").read_text() == "# a\n"


async def test_project_files_routes_check_the_token(
    tmp_path: Path, store: StateStore, monkeypatch
) -> None:
    monkeypatch.setenv("REWORK_TOKEN", "sekrit")
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    project = await _upload_project(store)
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.md").write_text("# a\n")
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        body = pack_paths([local / "a.md"])
        assert (await client.post(f"/projects/{project.id}/files", data=body)).status == 401
        assert (await client.get(f"/projects/{project.id}/files/archive")).status == 401
    monkeypatch.delenv("REWORK_TOKEN")


async def test_project_files_routes_404_for_unknown_project(
    tmp_path: Path, store: StateStore, monkeypatch
) -> None:
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.md").write_text("# a\n")
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        body = pack_paths([local / "a.md"])
        assert (await client.post("/projects/p-nope/files", data=body)).status == 404
        assert (await client.get("/projects/p-nope/files/archive")).status == 404


def _tar_with(members: list[tuple[str, bytes, str | None]]) -> bytes:
    """A tar.gz from [(name, body, linkname)]; a linkname makes a symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body, linkname in members:
            info = tarfile.TarInfo(name)
            if linkname is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                tar.addfile(info)
                continue
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_commit(tree: Path) -> None:
    _git("init", "-b", "main", cwd=tree)
    _git("add", "-A", cwd=tree)


def _commit(tree: Path, message: str = "init") -> None:
    _git("-c", "user.name=r", "-c", "user.email=r@r", "commit", "-m", message, cwd=tree)


async def test_upload_cannot_plant_a_git_hook(
    tmp_path: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POSTed tar must never write a `.git` path: the area's `git commit`
    would then EXECUTE a planted hook on the daemon (audit-proven)."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    marker = tmp_path / "hook-ran"
    hook = f"#!/bin/sh\ntouch {marker}\n".encode()
    project = await _upload_project(store)
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/projects/{project.id}/files",
            data=_tar_with(
                [
                    (".git/hooks/pre-commit", hook, None),
                    ("x.md", b"# x\n", None),
                    # the same trick in another shape: a symlink to `.git` plus
                    # a nested member, which the data filter writes THROUGH
                    ("s", b"", ".git"),
                    ("s/hooks/pre-commit", hook, None),
                ]
            ),
        )
        assert resp.status == 200
    area = project_files_dir(project.id)
    assert (area / "x.md").read_text() == "# x\n"
    assert not (area / ".git" / "hooks" / "pre-commit").exists()
    # the symlink member is dropped, so the nested member lands in an ordinary
    # directory named `s` — never redirected into the area's real `.git`
    assert not (area / "s").is_symlink(), "a symlink member must not be extracted"
    assert not (area / "s" / "hooks" / "pre-commit").is_symlink()
    assert "init" in _git("log", "--oneline", cwd=area).stdout, "area commits still happen"
    assert not marker.exists(), "the planted hook must not have run"


async def test_upload_drops_junk_members(
    tmp_path: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    project = await _upload_project(store)
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/projects/{project.id}/files",
            data=_tar_with(
                [("__pycache__/junk.pyc", b"bytecode", None), ("keep.md", b"# keep\n", None)]
            ),
        )
        assert resp.status == 200
    area = project_files_dir(project.id)
    assert (area / "keep.md").exists()
    assert not (area / "__pycache__").exists()


async def test_upload_accepts_more_than_one_mib(
    tmp_path: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDFs and docx routinely exceed aiohttp's 1 MiB default, which made
    `add-files` fail with a bare 413."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    project = await _upload_project(store)
    big = os.urandom(3 * 1024 * 1024)  # incompressible: the body stays > 1 MiB
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/projects/{project.id}/files", data=_tar_with([("big.pdf", big, None)])
        )
        assert resp.status == 200, await resp.text()
    area = project_files_dir(project.id)
    assert (area / "big.pdf").stat().st_size == len(big)


def _seeded_tree(tmp_path: Path, name: str = "tree") -> Path:
    """A worktree as `seed_mission_workspace`/`git_init_commit` leaves it: a
    repo whose root commit is the seeded snapshot."""
    tree = tmp_path / name
    tree.mkdir()
    (tree / "seed.md").write_text("# seeded\n")
    _init_commit(tree)
    _commit(tree)
    return tree


def test_mission_changes_keeps_special_filenames(tmp_path: Path) -> None:
    """git octal-quotes non-ASCII paths unless `-z` is used, and a quoted name
    never matches a real file: the file used to vanish from the area."""
    tree = _seeded_tree(tmp_path)
    (tree / "sub dir").mkdir()
    names = ["Résumé.md", "日本.md", "sub dir/é.txt"]
    for name in names:
        (tree / name).write_text(f"# {name}\n")
    assert sorted(rel for rel, _kind in mission_changes(tree)) == sorted(names)


def test_mission_changes_names_a_renamed_file(tmp_path: Path) -> None:
    tree = _seeded_tree(tmp_path)
    (tree / "a.md").write_text("# a\n")
    _git("add", "-A", cwd=tree)
    _commit(tree, "a")
    _git("mv", "a.md", "Résumé.md", cwd=tree)
    # the rename's NEW name is what the area gets, never the octal-quoted pair
    assert [rel for rel, _kind in mission_changes(tree)] == ["Résumé.md"]


def test_save_mission_files_skips_the_boilerplate_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coder writes DEFAULT_GITIGNORE into every worktree: saving it back
    would report a file a no-write mission never produced, and the area's own
    `git add -A` would then skip dist/ and build/ for good."""
    from rework.roles.builtin.coder import DEFAULT_GITIGNORE

    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    tree = _seeded_tree(tmp_path)  # the seeded snapshot is the root commit
    (tree / ".gitignore").write_text(DEFAULT_GITIGNORE)
    _git("add", "-A", cwd=tree)
    _commit(tree, "coder handoff")  # ...and the handoff commit adds the boilerplate
    assert [rel for rel, _k in mission_changes(tree)] == [".gitignore"], "it IS a change"

    assert save_mission_files("p-gitignore", [tree], "mission m: files") == []

    area = project_files_dir("p-gitignore")
    assert not (area / ".gitignore").exists()
    assert len(_git("log", "--oneline", cwd=area).stdout.splitlines()) == 1, "only init"


def test_save_mission_files_keeps_a_real_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is narrow: a gitignore a mission actually authored is saved."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    tree = _seeded_tree(tmp_path)
    (tree / ".gitignore").write_text("dist/\n")
    _git("add", "-A", cwd=tree)
    _commit(tree, "own gitignore")
    (tree / ".gitignore").write_text("dist/\nout/\n")

    assert save_mission_files("p-own", [tree], "mission m: files") == [".gitignore (new)"]
    assert (project_files_dir("p-own") / ".gitignore").read_text() == "dist/\nout/\n"


def test_area_lock_excludes_concurrent_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is the fix for `.git/index.lock` races; assert it actually
    excludes, so the concurrency test below cannot pass by luck."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    inside = 0
    peak = 0
    guard = threading.Lock()

    def _critical(_n: int) -> None:
        nonlocal inside, peak
        with _area_lock("p-lock"):
            with guard:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)  # a git commit is not instantaneous either
            with guard:
                inside -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_critical, range(8)))
    assert peak == 1, f"{peak} threads held the area lock at once"
    assert (project_files_dir("p-lock").parent / ".files.lock").exists()
    assert not (project_files_dir("p-lock") / ".files.lock").exists(), "never inside the area"


def test_concurrent_save_backs_all_land(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes (aiohttp's thread pool) and the engine mutate one area at
    once: without a cross-thread lock they race on `.git/index.lock` and files
    land last-writer-wins, uncommitted."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    trees = [_seeded_tree(tmp_path, f"tree{n}") for n in range(6)]
    for n, tree in enumerate(trees):
        (tree / f"file{n}.md").write_text(f"# {n}\n")

    def _save(item: tuple[int, Path]) -> list[str]:
        n, tree = item
        return save_mission_files("p-concurrent", [tree], f"mission m{n}: files")

    with ThreadPoolExecutor(max_workers=len(trees)) as pool:
        results = list(pool.map(_save, enumerate(trees)))

    assert all(results), "every concurrent save reports its file"
    area = project_files_dir("p-concurrent")
    for n in range(len(trees)):
        assert (area / f"file{n}.md").read_text() == f"# {n}\n"
    status = _git("status", "--porcelain", cwd=area).stdout
    assert status.strip() == "", "everything is committed, nothing left behind"


def test_pack_paths_rejects_a_missing_path(tmp_path: Path) -> None:
    """A typo'd path used to pack to an empty tarball, so the daemon answered
    200 and `rework project add-files` looked like it had added something."""
    (tmp_path / "there.md").write_text("# there\n")
    with pytest.raises(ValueError, match="no such file or folder"):
        pack_paths([tmp_path / "there.md", tmp_path / "tehre.md"])


def test_mission_changes_saves_files_the_coder_gitignore_hides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coder role writes DEFAULT_GITIGNORE (dist/, build/) into its
    worktree, so `git status` hid a real deliverable written to dist/out.txt
    and it was never saved back nor reported."""
    from rework.roles.builtin.coder import DEFAULT_GITIGNORE

    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    tree = _seeded_tree(tmp_path)
    (tree / ".gitignore").write_text(DEFAULT_GITIGNORE)
    _git("add", "-A", cwd=tree)
    _commit(tree, "coder handoff")
    (tree / "dist").mkdir()
    (tree / "dist" / "out.txt").write_text("the deliverable\n")
    (tree / "build").mkdir()
    (tree / "build" / "x.txt").write_text("also\n")
    (tree / "node_modules").mkdir()
    (tree / "node_modules" / "junk.js").write_text("junk\n")

    # it WAS a change all along — status just refused to say so
    assert {"dist/out.txt", "build/x.txt"} <= {rel for rel, _k in mission_changes(tree)}

    changed = save_mission_files("p-ignored", [tree], "mission m: files")
    assert "dist/out.txt (new)" in changed and "build/x.txt (new)" in changed
    area = project_files_dir("p-ignored")
    assert (area / "dist" / "out.txt").read_text() == "the deliverable\n"
    assert (area / "build" / "x.txt").read_text() == "also\n"
    # _excluded() still drops the junk the ignore file was hiding alongside it
    assert not (area / "node_modules").exists()
    assert not (area / ".gitignore").exists(), "the boilerplate is still skipped"


def test_archive_files_area_waits_for_a_mutation_holding_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads used to be unlocked: a pull could pack a half-written area. The
    archive now takes `_area_lock` — prove it BLOCKS while a mutation holds
    it, rather than passing by luck on an idle area."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    area = ensure_files_area("p-read-lock")
    (area / "a.md").write_text("# a\n")
    update_files_area("p-read-lock", pack_paths([area / "a.md"]), "seed")

    started = threading.Event()
    finished = threading.Event()

    def _pack() -> bytes:
        started.set()
        data = archive_files_area("p-read-lock")
        finished.set()
        return data

    with _area_lock("p-read-lock"):
        worker = threading.Thread(target=_pack)
        worker.start()
        assert started.wait(timeout=5), "the packer never started"
        time.sleep(0.3)
        assert not finished.is_set(), "the archive read ignored the area lock"
    worker.join(timeout=10)
    assert finished.is_set(), "the archive never ran once the lock was free"
    with tarfile.open(fileobj=io.BytesIO(archive_files_area("p-read-lock")), mode="r:gz") as tar:
        assert "a.md" in tar.getnames()


def test_seed_mission_workspace_waits_for_a_mutation_holding_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding out of a half-written area started a mission from a torn tree;
    the seed now locks, so it waits for the writer instead of reading through
    it (and it must not deadlock now that it takes the lock itself)."""
    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    area = ensure_files_area("p-seed-lock")
    (area / "a.md").write_text("# a\n")
    update_files_area("p-seed-lock", pack_paths([area / "a.md"]), "seed")
    dest = tmp_path / "seeded"
    started = threading.Event()
    finished = threading.Event()

    def _seed() -> Path:
        started.set()
        path = seed_mission_workspace("p-seed-lock", dest)
        finished.set()
        return path

    with _area_lock("p-seed-lock"):
        worker = threading.Thread(target=_seed)
        worker.start()
        assert started.wait(timeout=5), "the seeder never started"
        time.sleep(0.3)
        assert not finished.is_set(), "seeding ignored the area lock"
    worker.join(timeout=10)
    assert finished.is_set()
    assert (dest / "a.md").read_text() == "# a\n"
    assert (dest / ".git").is_dir(), "the seeded workspace is still a repo"


async def test_upload_route_maps_a_lock_timeout_to_503(
    tmp_path: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock TimeoutError is not a bad body: the 400 "not a readable tar.gz"
    was a lie and made the client retry a perfectly good tarball."""
    import rework.orchestrator.workspace as workspace_mod

    monkeypatch.setenv("REWORK_PROJECTS_DIR", str(tmp_path / "projects"))
    project = await _upload_project(store)
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.md").write_text("# a\n")
    body = pack_paths([local / "a.md"])

    def busy(*_args: object, **_kwargs: object) -> list[str]:
        raise TimeoutError("project p files area is busy (no lock after 120s)")

    monkeypatch.setattr(workspace_mod, "update_files_area", busy)
    app = build_workspace_app(store, tmp_path / "workroot")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/projects/{project.id}/files", data=body)
        assert resp.status == 503
        assert "busy" in (await resp.json())["error"]
