# ADR 0005: Projects and the workspace transport

## Status

Status: Accepted — amended 2026-11 (persistent project files for repo-less
projects; see Amendment).

Accepted and landed (2026-11-04, M4.5, commits `b310c9d`..`db171ac`): the
projects table (both stores, conformance-tested), the daemon workspace API
(POST /workspaces, GET /workspaces/results/<mission>, healthz; localhost by
default, tailnet-only via REWORK_HTTP_HOST), the CLI surface (rework project
create/ls/show, --repo-dir upload, --repo resolving URLs / local paths /
project refs, rework fetch), and the git transport (the daemon clones
repo_origin itself at run start, restart-safe). Verified live on the rewvis
deployment: a mission created from the Mac with a plain non-git folder ran
investigate -> code -> audit -> ship gate -> DONE via the upload transport
($0.06, 7m), and a URL-only mission saw the daemon clone the private origin.
Two live-found fixes: unconditional ALTER TABLE migrations convoyed every
store open behind one long-lived reader (now check-then-alter with a 5s
lock_timeout), and the echo stub's absent default model failed real
deployments (now a visible cheapest-tier fallback).

## Context

A mission today is welded to a single `--repo` path, and `rework new` resolves
that path **on the creating machine** (`_prepare_repo`, `cli/__init__.py`). The
daemon then reads the stored path on *its* filesystem. Across devices this
breaks: six real missions failed with `coder failed 0 calls` because a
Mac-created mission stored a Mac-only path the rewvis daemon could not see.
The current workaround (create the mission on the daemon's machine) defeats the
cross-device design.

The owner also thinks in **projects**, not repos: a capstone project owns an
OnShape doc, a Drive folder, school files, and sometimes a code repo, sometimes
none. Forcing every mission through one `--repo` argument fits the tool, not
the human.

One scoping principle, owner-stated and firm: **rework never becomes a code
transport.** Code always travels through existing VCS (GitHub, and later
whatever the owner's SCION/GitAI research lands on). The transport mechanism
this ADR defines exists for *workflow-level project files only* (documents,
folders, datasets: things that are not repos).

## Decision

1. **Project is a first-class entity.** `rework project create/ls/show`; a
   mission optionally belongs to one (`missions.project_id`, nullable, so
   one-off label-less missions still work). A project is a label plus settings
   plus zero-or-more repo references:

   ```
   Project {
     id: p-…, name, created_at
     settings: {                       # a dict, not columns: new knobs
       transport: "git" (default)      #   never need schema migrations
         | "upload"
         | <future adapters: "scion", "gitai", …>
       results: "push_branch"          # default when a repo has an origin
         | "fetch" | "both"
       access: "tailnet" (default)     # "funnel" is a future adapter
       repos: [ {name, kind: github | path, url, default_branch} ]  # 0..n
     }
   }
   ```

2. **The resolution inversion.** Missions store *references* (project id, repo
   name, workspace id), never machine-local paths. The **daemon resolves
   references against its own disk at dispatch time**. The creating machine
   never needs to know where the daemon keeps anything.

3. **Workspace transport, per-project configurable, one-shot per mission.**
   - `git` (default): the daemon clones the repo from its origin itself. Code
     never uploads through rework; git is already the transport, and the
     result returns the same way (push branch / PR, behind the open_pr gate).
   - `upload`: for non-git working files, `rework new` packages the folder
     (gitignore-aware tarball) and POSTs it to the daemon, which stages it
     under its workroot and records its own path on the mission. No continuous
     sync, no bidirectional state, no conflict resolution to build.
   - Future transports (SCION, GitAI, …) are new adapters behind the same
     `transport` setting; nothing else moves.

4. **The daemon's HTTP surface.** `rework serve` gains a small API in the same
   process, reachable over the tailnet it already listens on:

   - `POST /workspaces` (auth token) → stage a bundle, return a workspace id
   - `GET /workspaces/{id}/result` → download the final worktree (the
     `fetch` results path)

   Token auth from `~/.rework` (same pattern as the model key). Tailnet-only
   at v1; a device that cannot join the tailnet later reaches the identical
   API through Tailscale Funnel with the same token: no new code paths.

5. **Results return is per-project configurable.**
   - `push_branch` (default for git repos): the fixed branch lands on the
     repo's origin behind the open_pr gate; the owner pulls locally.
   - `fetch`: the CLI downloads the result tarball, for plain-folder projects.

6. **What stays untouched.** Roles, engine, gates, artifacts, and reports only
   ever see "a workdir that exists on this machine"; that promise now holds on
   whichever machine the daemon runs. The Library stays separate
   infrastructure: projects carry *working files*, the Library carries
   *knowledge* (skills/reports/memories, git + PG index, per ADR 0004's plan).

## Consequences

- Store: `projects` table in both impls, `missions.project_id`, workspace
  staging rows. The parametrized conformance suite covers both backends.
- CLI: `rework project create/ls/show`; `rework new --project p-x [--repo
  name]`; inline `--repo` stays for quick one-shot missions.
- Daemon: a workspace API and a staging area under the workroot.
- The `_prepare_repo` create-time staging goes away for daemon-dispatched
  missions; it survives only as the fallback when the daemon runs locally.
- ADR 0001's "missions take `--repo`" line is superseded: missions take a
  *project*, projects take repos.

## Alternatives considered

- **Persistent synced workspace** (a project folder kept identical across
  devices, two-way, Syncthing-class): rejected for v1. Conflict resolution,
  watching, and failure modes are a product of their own; per-mission one-shot
  bundles cover the actual cross-device pain.
- **Sync everything through rework** (code included): rejected per the owner's
  principle. Code belongs to its VCS; duplicating that machinery invites
  divergence and drift.
- **Git for everything** (turn every project folder into a repo): rejected.
  Documents and datasets do not want commits, and the owner explicitly does
  not want workflow files forced through git.

## Amendment: persistent project files (2026-11)

Repo-less projects now work as this ADR intended. The gap was that the upload
transport was one-shot *and stateless*: `--repo-dir` packed a folder once, so
a second mission on the same project started from nothing, and a mission's
output could only be downloaded per mission (`rework fetch`). A project with
no repos had nowhere to keep its working files between missions.

Each repo-less project now owns a **files area** on the daemon host at
`~/.rework/projects/<project-id>/files` (`REWORK_PROJECTS_DIR` overrides the
root). It is git-initialised on first use — a local version store, so every
change is an auditable commit — and it has **no remote and is never pushed
anywhere**. It is not a repository anyone clones; it is the project's own
working set, on the machine that runs the missions.

Sync stays one-shot and explicit, per decision 3, and is explicitly **not**
the continuous bidirectional sync this ADR rejected under Alternatives:

- **seed at mission start**: the files area is copied into the mission's
  workspace before the first role runs (no repo required, `repo_path` becomes
  the seeded workspace);
- **save at mission end**: the mission's new and changed files are copied back
  into the files area, committed there, and named in the mission report
  (`Project files updated: a.md (new), b.md (changed)`); deletions are not
  carried back — the save-back is additive by design;
- **save-back is conditioned on DONE, and its failure is visible**: a
  cancelled or ship-rejected mission leaves the area untouched, and a failed
  save-back still ends the mission DONE but reports `Project files NOT
  updated: <reason>`; two missions of one project must not run concurrently
  against the same area — the per-operation lock serialises them, so the later
  mission simply seeds from whatever state the earlier one has reached;
- **`rework project add-files NAME PATHS...`** puts files from any machine
  into the area (TUI: the sidebar's "Add files" button or File → "Add project
  files…");
- **`rework project pull NAME DEST`** takes the area back to any machine.
  A mission's own result still comes back with `rework fetch`.

Transport and results are **derived from `repos`**, not taken on trust: a
project with at least one repo reference is `git`/`push_branch`, and a project
with none is `upload`/`fetch` with no way to be configured otherwise
(`derive_transport`/`project_is_upload`, `rework project create` rejects
`--transport git` without a repo). A repo-less project therefore cannot be
left needing an origin it does not have.

Decision 3's `upload` transport is unchanged and still available per mission
(`rework new --repo-dir`). The original scoping rule still holds, and this
amendment does not move it: **code travels through git only.** The files area
is for documents and working files; a project that carries code has repos and
travels by `git`/`push_branch`.
