# ADR 0005: Projects and the workspace transport

## Status

Proposed (2026-11-04). Landed work planned as M4.5, before M5 Library v1.

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
