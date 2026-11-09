# ADR 0008: PR previews (run a pull request's build before merging it)

## Status

Accepted (2026-11-09).

## Context

The owner wants, before merging a rework pull request, to run *that PR's* rework
and try it, the way a web project gets a Tilt environment and a per-PR subdomain.
Today the loop ends at the open_pr gate: the daemon pushes the coder's branch and
`gh pr create` opens the PR (`src/rework/orchestrator/engine.py:739` `_create_pr`,
`src/rework/nodes/gh.py:15` `pr_create`); the human reviews and merges, and
the merged code is only exercised by the next real mission against the real
database. There is no way to drive a PR's TUI, daemon and migrations by hand
before they land on the owner's live state.

rework is not one process. It is:

- a Typer CLI, single file `src/rework/cli/__init__.py`, entry point
  `rework.cli:main`;
- a Textual TUI (`src/rework/tui/`) that is a pure *client*: it reads and writes
  the state DB (`REWORK_DB`) and only touches HTTP to fetch results, building
  that URL from `workspace_url_from_env()` (`src/rework/tui/data.py:250-255`,
  `src/rework/orchestrator/workspace.py:185-189`);
- an aiohttp daemon (`src/rework/orchestrator/daemon.py`) that runs missions and
  serves the workspace API, bound to `127.0.0.1:8941` by default
  (`default_bind()`, `workspace.py:192-198`).

Running a second instance is non-trivial for four concrete reasons:

1. **The port is env-only and never persisted.** `REWORK_HTTP_HOST`,
   `REWORK_HTTP_PORT`, `REWORK_DAEMON_URL` are read from the environment
   (`workspace.py:185-198`); nothing writes the chosen port to disk. There is no
   pid file and no port file anywhere in `src/` (the only on-disk concurrency
   artifact is the `daemon_lock` row). A client cannot discover a non-default
   port by itself, so a second instance needs an out-of-band channel.
2. **One daemon per database is a hard invariant.** The daemon takes a singleton
   row lock (`daemon_lock`, `CHECK (id = 1)`, `src/rework/kernel/state/sqlite.py:108-112`)
   and refuses to start when another owner holds it
   (`daemon.py:118-124`, `DaemonLocked`). "A second instance" must therefore mean
   a separate database, never a second daemon on the same DB. The same follows
   from run ids being deterministic (`<mission>:<step>:<role>:<attempt>`,
   `kernel/state/records.py:26-28`) and the workdir being derived from the DB
   directory (`cli/__init__.py` `serve`: `Path(config.db).parent / "work"`;
   `engine.py:183`), so two processes on one DB would collide on paths.
3. **Every piece of instance state defaults to a fixed global path** under
   `~/.rework`: state DB (`config.py:17` `DEFAULT_STATE_DIR`, `:37`), checkpoints
   DB (`Config.from_env`; derived from the DB dir on the sqlite branch,
   `config.py:71-73`, while on Postgres checkpoints share the main DB,
   `config.py:62-66`), library git root
   (`cli/__init__.py:1202-1208` `_library_root()`, `REWORK_LIBRARY_DIR`), log dir
   (`logs.py:54` `log_dir()`, `REWORK_LOG_DIR`), model catalog
   (`config.py:105-109` `REWORK_MODELS`, default `~/.rework/models.json`), TUI
   settings (`tui/settings.py:22`, `~/.rework/tui.json` / `REWORK_TUI_SETTINGS`),
   and the visible results/reports dirs (`config.py:21` `DEFAULT_USER_DIR =
   ~/rework`, `REWORK_RESULTS_DIR` / `REWORK_REPORTS_DIR`). There is no XDG
   usage; none of these paths is derived from an instance identity. Two instances
   that do not override them share or collide.
4. **There is no PR-number field anywhere.** Only `mission.result["pr_url"]`
   exists (`engine.py:811-813`, rendered at `tui/widgets/cards.py:186-189`). No
   repo slug, head/base, PR number or PR state is stored, and rework never lists
   or fetches PRs: it only creates them behind the open_pr gate. Per-PR keying
   must parse the number out of the URL or take it from the command line.

`scripts/rework-local` is the closest existing recipe and shows the shape of the
answer. It exports `REWORK_DB="$state/local.sqlite3"` and
`REWORK_CHECKPOINTS_DB="$state/local-checkpoints.sqlite3"` with
`state="${REWORK_LOCAL_STATE:-$HOME/.rework}"`, pins `REWORK_HTTP_HOST=127.0.0.1`,
`REWORK_HTTP_PORT="${REWORK_LOCAL_PORT:-8941}"` and
`REWORK_DAEMON_URL="http://127.0.0.1:${REWORK_HTTP_PORT}"`, then
`exec uv run --project "$here" rework "$@"`. It isolates the DB, the checkpoints
DB and the port, and nothing else: library, logs, models and results stay global.
It also runs the *current checkout's* code, not a PR's.

## Decision

A preview is a second, fully isolated rework instance built from a PR's head:
its own git worktree, its own venv, its own state DB (a copy), its own library
and log dirs, its own port, and a small descriptor file so a client can find it.
Nothing about the real instance is touched.

### 1. Creating a preview

New CLI: `rework preview <pr-number>` in `src/rework/cli/__init__.py`.

```
rework preview <n> [--repo-dir PATH] [--repo OWNER/NAME] [--port PORT] [--copy-db PATH]
                  [--rm <n>] [--list]
```

- `--repo-dir PATH` (default: the repo of the current working directory) is the
  base checkout the PR worktree is fetched into. `--repo OWNER/NAME` is the
  GitHub slug, used with `gh` (default: derived from `git remote get-url origin`).
- `--port` pins the daemon port; without it a free port is chosen (see §3).
- `--copy-db` overrides which database is copied (default: the instance's
  `REWORK_DB`); `--fresh-db` (new) would seed an empty DB instead.
- `--rm <n>` and `--list` are the cleanup/inspection surface (§6).

On success it prints the descriptor and the two commands to run:

```
preview #123 ready
  worktree  ~/.rework/previews/123/repo      (branch: pr-123, head <sha>)
  venv      ~/.rework/previews/123/repo/.venv
  state     ~/.rework/previews/123/state.sqlite3 (copied from ~/.rework/local.sqlite3)
  descriptor ~/.rework/previews/123.json
  port      8942

  start:  rework preview --serve 123     # daemon + workspace API on 127.0.0.1:8942
  attach: REWORK_PREVIEW=123 rework      # the TUI, pointed at the preview
```

**TUI path.** Recommended: a **Preview** action on the mission card's PR line,
not a new tab and not a `rework pr-123` shortcut. The card already carries the
`pr_url` and already has a verb dispatch: `MissionCard` (`tui/widgets/cards.py:84`)
declares `class Act(Message)` and a `Horizontal(classes="card-actions mc-actions")`
row of `Btn`s, and `WorkView.on_mission_card_act` (`tui/views/work.py:562`)
routes verbs (`open`, `approve`, `reject`, `answer`, `pause`, `resume`, `cancel`)
to `tui/actions.py`. So: new `Btn("Preview", "preview")` in the card, new
`"preview"` branch in `on_mission_card_act`, and a new `actions.preview(app,
mission_id)` that parses the PR number out of `mission.result["pr_url"]` and
shells the same `rework preview <n>` code path. The action must resolve the
repo from the *mission*, never from the process cwd that `--repo-dir`
defaults to: `OWNER/NAME` parsed out of the `pr_url` for `--repo`, and
`mission.repo_path` (`kernel/state/records.py:89`; repo identity lives on
the mission, not the card) for `--repo-dir`. If neither is resolvable the
verb must refuse with a clear message rather than fetch the wrong repo. A
`rework pr-123`-style shortcut is rejected: it invents a second, number-only
namespace next to mission ids and gives no way to say *which repo*; the card is
where the PR URL already lives.
(Whichever surface wins, both must call one implementation, per ADR 0006's
"actions are shared".)

### 2. Isolation

Preview root: `~/.rework/previews/<n>/` (new; overridable with the new
`REWORK_PREVIEW_DIR`). A preview never writes outside it except to the shared
base repo's `.git` (worktree metadata).

- **Worktree.** Reuse the repo's existing convention: `git worktree add -B
  <branch> <path>` via `git_ops.ensure_worktree` / `make_worktree`
  (`src/rework/nodes/git_ops.py:68-105`), which the coder already uses with
  `worktree_path = ctx.workdir / "worktree"` and a `rework/<run-id>` branch
  (`roles/builtin/coder.py:83-84`). For a preview the worktree lives at
  `~/.rework/previews/<n>/repo`, on branch `rework/preview-pr-<n>`, created from
  the PR head fetched with `gh pr checkout <n>` or `git fetch origin
  pull/<n>/head` (new helper in `src/rework/nodes/git_ops.py`). Fetching (not
  `git clone`) is deliberate: the PR's commit is usually not on a branch the
  preview can see otherwise, and the owner's `gh` login already provides
  credentials (`gh auth setup-git`). Re-running `rework preview <n>` reuses the
  worktree and moves it to the PR's current head (see §6 on staleness).
- **Venv.** `uv sync` inside the worktree (new code in the preview command),
  which creates the worktree's own `.venv` because uv is venv-per-project and
  keys off the project root (`pyproject.toml`). `uv sync` honours the PR's own
  `uv.lock`, which is exactly the point: a PR that bumps a dependency is tested
  with the bumped dependency. This is the reason the preview runs the worktree's
  code rather than `uv run --project` against the real checkout.
- **State DB.** Copy, not share. Source: the DB the invoking CLI would use
  (`REWORK_DB`, in practice `~/.rework/local.sqlite3` from `scripts/rework-local`).
  Timing: **at creation**, with sqlite's online backup (`VACUUM INTO` or
  `sqlite3.Connection.backup`, new code) rather than `cp`, so a live WAL does not
  produce a torn copy. Migration-on-open: opening the copy runs the store's
  check-then-alter migrations (`sqlite.py:907-930`), which may *write* to it; that
  is fine and is the desired behavior (the PR's migrations are exercised), but it
  means the copy is one-way and must never be copied back over the real DB. Also
  clear `daemon_lock` in the copy (new; one `DELETE FROM daemon_lock`) so the
  preview's daemon is not refused by the real daemon's live heartbeat, and set the
  copy's `missions` rows are left as they are: the preview sees the real state as
  of creation and can read/replay it, but its writes stay in the copy.
- **Checkpoints DB.** `REWORK_CHECKPOINTS_DB=~/.rework/previews/<n>/checkpoints.sqlite3`
  (a copy of the real one, or empty if `--fresh-db`). `Config.from_env` derives
  it from the DB's directory when unset, so pointing `REWORK_DB` at the preview
  dir is nearly enough; setting it explicitly is clearer.
- **Port.** Chosen by binding `socket` to port 0 (new helper) unless `--port`
  pins one; the preview daemon then starts with
  `REWORK_HTTP_HOST=127.0.0.1`, `REWORK_HTTP_PORT=<port>`,
  `REWORK_DAEMON_URL=http://127.0.0.1:<port>`.
- **Discovery (the important part).** Because ports are env-only, the preview
  writes a descriptor at `~/.rework/previews/<n>.json` (new):

  ```json
  {"pr": 123, "repo": "roowus/rework", "head_sha": "…", "branch": "rework/preview-pr-123",
   "worktree": "~/.rework/previews/123/repo", "state_db": "…", "checkpoints_db": "…",
   "host": "127.0.0.1", "port": 8942, "pid": 51234, "daemon_url": "http://127.0.0.1:8942",
   "created_at": 1762... , "env": {"REWORK_LIBRARY_DIR": "...", "REWORK_LOG_DIR": "...",
   "REWORK_MODELS": "...", "REWORK_RESULTS_DIR": "...", "REWORK_REPORTS_DIR": "...",
   "REWORK_TUI_SETTINGS": "..."}}
  ```

  Clients read it by convention: new `REWORK_PREVIEW=<n>` makes the CLI/TUI load
  `~/.rework/previews/<n>.json` and export its `env` (and `REWORK_DAEMON_URL`)
  before doing anything else, so `REWORK_PREVIEW=123 rework` opens the preview's
  TUI and `REWORK_PREVIEW=123 rework ls` reads the preview's DB. A stale
  descriptor (pid gone, or port not answering) is reported as stale, never
  silently used.
- **Library.** `REWORK_LIBRARY_DIR=~/.rework/previews/<n>/library`, cloned from
  the real library root (`_library_root()`) at creation. A separate clone keeps a
  preview from activating or retiring real library items; the preview's Library
  tab then shows a copy, which is the honest thing for a throwaway instance.
- **Logs.** `REWORK_LOG_DIR=~/.rework/previews/<n>/logs`, so preview daemon/TUI
  logs never interleave with the real ones.
- **Models.** `REWORK_MODELS` points at the preview's own copy
  (`~/.rework/previews/<n>/models.json`) if one exists, else inherits the real
  path: catalog edits in a preview are then local, but the preview can still run.
- **Results/reports.** `REWORK_RESULTS_DIR` / `REWORK_REPORTS_DIR` under
  `~/.rework/previews/<n>/results|reports`, so a preview's fetched artifacts
  never overwrite the owner's visible deliverables in `~/rework`.
- **TUI settings.** `REWORK_TUI_SETTINGS=~/.rework/previews/<n>/tui.json`, so
  trying a UI PR does not rewrite the real `~/.rework/tui.json`.

### 3. Running and using it

Two commands, both from anywhere:

```bash
rework preview 123            # once: fetch, worktree, uv sync, copy state, pick port
rework preview --serve 123    # terminal 1: the preview daemon (foreground)
REWORK_PREVIEW=123 rework     # terminal 2: the preview TUI
```

`--serve` (new) is a thin wrapper: it loads the descriptor and execs the preview
worktree's own `rework serve` (`uv run --project <worktree> rework serve`) with
the descriptor's env, so the daemon code under test is the PR's code. The TUI
similarly runs from the preview venv when launched through `rework preview
--tui 123` (new); `REWORK_PREVIEW=123 rework` is the convenience form that runs
whatever rework is on the current PATH with the preview's *env* only. That
distinction matters and must be printed by `rework preview`: a preview's TUI is
only the PR's UI if it is launched from the preview venv.

**Side by side: yes.** The real daemon and the preview daemon are different
processes on different ports with different databases and different `daemon_lock`
rows; neither the leader lock nor the workroot collides. The two TUIs are two
terminals. Disk is the only shared resource (§5).

**Real missions in a preview: default to none.** A preview runs a PR's
unreviewed code with the owner's environment, which includes the GitHub token
(`gh`'s stored credential, `REWORK_TOKEN`) and model API keys. Safe default:
`rework preview <n>` starts the preview daemon with a **no-execute** posture,
i.e. missions are created and inspected but the daemon does not dispatch role
runs until the owner explicitly enables it with `--allow-missions` (new) or by
flipping `REWORK_PREVIEW_ALLOW_MISSIONS=1` for that process. Even then the
recommendation is to point the preview at a scratch repo, not at the real one,
and to remember that the preview DB is a copy: real missions run there do not
advance the real world, but they do spend real money and can push real branches
if an open_pr gate is approved. This is the honest cost of previewing a workflow
engine, and it is why the default is off.

### 4. Returning to the normal build

Nothing in the normal instance was mutated: the preview read the real DB (via a
copy), the real worktree/repo is untouched except for the added worktree entry in
its `.git` metadata, the real library, logs, models, results, reports and
`~/.rework/tui.json` are untouched, and the real daemon keeps its own port and
its own `daemon_lock`. Getting back is:

1. In the preview TUI: `q`. In the preview daemon terminal: Ctrl-C (or
   `rework preview --stop 123`, new, which reads the descriptor's pid and sends
   `SIGTERM`).
2. Run plain `rework` (no `REWORK_PREVIEW`) — the TUI attaches to the real
   daemon/DB exactly as before. If the preview daemon is still running it does not
   interfere: it holds a lock on the copy's DB, not the real one.
3. `rework preview --rm 123` when done (§5).

Because the descriptor is only consulted when `REWORK_PREVIEW` is set, "the
normal build" needs no restore step; there is nothing to restore.

### 5. Cleanup

```
rework preview --list        # table: #, repo, head sha, port, pid alive?, disk used
rework preview --rm 123      # stop, then delete
```

`--rm <n>` deletes: the preview's worktree (`git worktree remove --force` via
`git_ops.remove_worktree`, `git_ops.py:106`, plus `git worktree prune` so the
base repo's metadata does not accumulate — the same recovery the engine already
does for dead runs), the venv, `~/.rework/previews/<n>/` (state DB copy,
checkpoints, library clone, logs, results, reports, models copy, tui.json) and
the descriptor `~/.rework/previews/<n>.json`; it stops the daemon first if the
pid is alive, which releases the port. `--rm --all` (new) sweeps every preview.

Deliberately kept: nothing, by default — a preview is disposable. `--keep-state`
(new) would leave the state DB copy for post-mortem inspection. The real DB, real
library and real results are never candidates for deletion by this command, and
`--rm` refuses to run if the descriptor's paths do not resolve under the preview
root.

Crash and staleness: a descriptor whose pid is dead is reported stale by
`--list` and `REWORK_PREVIEW=<n>` (with the fix: `rework preview --rm <n>` or
`rework preview <n>` to re-create). The daemon's own `daemon_lock` staleness
(`daemon.py:118-124`, `stale_after` = 10× poll interval) means a SIGKILLed
preview daemon does not block a restart. A preview worktree left behind by a
crash is reclaimed by `git_ops.prune_worktrees` (`git_ops.py:113`) the same way
dead mission worktrees are.

### 6. Risks and mitigations

| risk | mitigation |
|---|---|
| **Port discovery**: ports are env-only, so a client cannot find a non-default port (`workspace.py:185-198`) | the descriptor file `~/.rework/previews/<n>.json` is the single source of truth, plus `REWORK_PREVIEW=<n>` to load it; the preview prints the exact env on success; `--port` pins one when the owner wants a stable URL |
| **DB copy drift**: the copy is frozen at creation, so preview and real state diverge | the copy is read/write and the divergence is intended; the descriptor records `created_at` and the source DB path, `--list` shows both, and re-copying is explicit (`rework preview <n> --refresh-db`, new). The copy is never written back |
| **Leader lock**: a second daemon on the same DB is refused (`sqlite.py:108-112`, `daemon.py:118-124`) | isolation is per-database by construction; the preview deletes `daemon_lock` in its copy so the real heartbeat cannot block it |
| **Unreviewed code with real credentials**: a PR can read `gh`'s token, model keys, and push branches | default no-execute posture for missions (§3), preview `gh` usage limited to fetch/checkout by the preview command itself, recommendation to preview against a scratch repo; a preview is never started with `REWORK_TOKEN` pointing at the production API unless the owner says so |
| **Disk usage**: one venv + worktree + library clone per PR (venvs are hundreds of MB) | `--list` reports disk used per preview, `--rm` deletes everything, and the README/docs recommend one live preview at a time; a future `--rm --older-than` sweep is cheap to add |
| **Stale previews**: the PR gets new commits and the preview silently tests old code | the descriptor stores `head_sha`; `rework preview <n>` fetches and moves the worktree to the current head (re-running `uv sync` only when `uv.lock` changed), and `--list` shows whether the worktree sha matches the PR's head |
| **Which instance am I looking at**: the real and preview TUIs look identical | the preview TUI must show a visible marker: `target_label()` (`tui/app.py:496-509`) already renders the target in the menu bar and already honours `REWORK_TARGET_NAME`; the preview command sets `REWORK_TARGET_NAME="PREVIEW #123"` in the descriptor env, so the marker is on screen with no new widget code. `--serve` also prints `preview #123` on startup, and the daemon log dir is per-preview |

## Alternatives considered

- **Reuse the real DB with a `preview_id` namespace column on every table.**
  Rejected: every query in both stores (`sqlite.py`, `postgres.py`, the
  conformance suite) would need the filter, the `daemon_lock` singleton is
  explicitly `CHECK (id = 1)` so it cannot be namespaced, deterministic run ids
  and workroots would collide, and one wrong query leaks preview state into the
  real TUI. The isolation would be a convention, not an invariant.
- **One daemon multiplexing multiple databases.** Rejected: `Daemon` is built
  around one store, one bus, one workroot and one `daemon_lock` owner
  (`daemon.py:96-116`); multiplexing would mean re-plumbing the engine for a
  feature whose whole point is to run *different code* (the PR's) — the same
  process cannot run two versions of rework.
- **Container or VM per preview.** Rejected for v1: strongest isolation, but
  the owner's credentials, git config, model keys and the Mac's `gh` login would
  all have to be plumbed in, TUI interaction over a container is awkward, and it
  is heavy machinery for a check the owner runs by hand. Worth revisiting if
  previews ever run untrusted PRs unattended.
- **`uv run --project <pr-checkout>` without its own venv.** Rejected: it is the
  cheapest thing that "works", but `uv run --project` resolves and writes the
  *project's* environment, so pointing it at a PR checkout still needs a venv in
  that checkout to honour the PR's `uv.lock`; skipping it silently tests the PR's
  Python against the real instance's dependency set — precisely the class of
  change (lockfile bumps) a preview exists to catch.
- **A full copy of `~/.rework` per preview.** Rejected: copies state *and* the
  library clone *and* logs *and* the checkpoints DB wholesale, which is slow,
  duplicates the library git repo per PR, and still needs port selection and
  descriptor discovery — the hard parts. The chosen design copies only the state
  and checkpoints DBs and clones the library, and reuses the existing worktree
  and port conventions.
- **A per-PR subdomain / tailnet URL** (the web-project analogy taken literally).
  Rejected for v1: the workspace API is localhost by default and tailnet-only
  otherwise (`default_bind`, `workspace.py:192-198`), and exposing a preview
  beyond the machine is a deliberate act, not a default. The descriptor covers
  local discovery; a tailnet host per preview is a later adapter.

## Consequences

- **Easier:** a PR can be driven by hand — its CLI, its TUI, its migrations, its
  dependency set — before it lands; UI and migration PRs stop being reviewed on
  faith; the local dogfood loop (`scripts/rework-local`) gains a per-PR variant
  with the same mental model.
- **Harder / new surface:** one new CLI command with create/serve/list/rm verbs
  and a descriptor format that becomes a small compatibility contract; new env
  var `REWORK_PREVIEW` (plus `REWORK_PREVIEW_DIR`,
  `REWORK_PREVIEW_ALLOW_MISSIONS`, `REWORK_TARGET_NAME` set by the command); a
  new worktree-fetch helper and a sqlite backup helper in the nodes/kernel
  layers; a `Preview` button and verb in `tui/widgets/cards.py` +
  `tui/views/work.py` + `tui/actions.py`. There is nothing yet that makes the
  owner clean up, so preview directories will accumulate until `--rm` is
  habitual.
- **Explicitly not implemented by this ADR.** Nothing above exists today: there
  is no `preview` subcommand, no `~/.rework/previews/`, no descriptor, no
  `REWORK_PREVIEW*` variable, and no Preview button. This document is the design
  to build against; a follow-up mission implements it, and this ADR's status
  moves to Accepted (with the landed commit range, in the style of ADR 0005)
  when it does.
- **Follow-up work:** decide the create-time default for the library clone
  (clone vs. read-only mount of the real one); decide whether `--refresh-db`
  should be automatic when the PR touches migrations; add a
  `docs/ops.md` "preview a PR" section next to the local dogfood loop; and
  consider a `pr_url` → `pr_number` field on the mission result so the TUI's
  Preview button does not parse URLs.

## Amendments (accepted with implementation)

1. **One command does it all.** `rework preview <n>` fetches the PR head into
   `~/.rework/previews/<n>/repo` as a git worktree, runs `uv sync`, copies the
   state DB with sqlite's backup API (a Postgres `REWORK_DB` falls back to a
   fresh SQLite DB with a printed notice), picks a free port, starts the
   preview's own daemon in the background (logs in the preview dir), then runs
   the preview build's TUI in the same terminal with the preview env and
   `REWORK_TARGET_NAME='PREVIEW #<n>'`. When that TUI exits, the preview daemon
   is stopped and the preview removed unless `--keep` was passed. This
   supersedes the two-terminal `--serve` / `REWORK_PREVIEW=<n> rework` flow in
   §3–§4.
2. **TUI Preview button.** Mission cards whose mission has a `pr_url` get a
   Preview button that suspends the main TUI (Textual `App.suspend`), runs
   `rework preview <n>`, and returns to the main TUI afterwards.
3. **`rework preview --list` / `rework preview --rm <n>`** list and remove
   previews (descriptor files `~/.rework/previews/<n>.json`).
4. **No-execute default.** A preview daemon does not dispatch role runs unless
   `--allow-missions` was passed (which sets
   `REWORK_PREVIEW_ALLOW_MISSIONS=1` in the preview daemon env). The guard
   lives in the daemon's dispatch path.
