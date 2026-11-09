# Running rework

## Local (the Mac): the dogfood loop, step one

Everything runs on your machine against a local SQLite state file
(`~/.rework/local.sqlite3`). Your shell's `REWORK_DB` (which points at rewvis)
is overridden for these processes only, so nothing touches the deployment.

```bash
scripts/rework-local serve      # terminal 1: the daemon (leave it running)
scripts/rework-local            # terminal 2: the TUI
```

In the TUI, Work tab: pick project `rework`, pick the orchestrator model, and
describe the change. The orchestrator proposes; Launch starts it. The local
daemon clones `github.com/roowus/rework`, runs the roles in
`~/.rework/work/`, and stops at the final gate (open_pr for fix-pr). Approving
it pushes the branch and opens the PR with your local `gh` login.

Any CLI command works the same way: `scripts/rework-local ls`,
`scripts/rework-local gates`, `scripts/rework-local new --plan fix-pr --project
rework "..."`.

One-time setup, already done on this Mac:

```bash
scripts/rework-local project create rework --repo rework=https://github.com/roowus/rework
```

Requirements: model keys in your env (`REWORK_FIREWORKS_KEY` for the default
catalog), `gh auth status` logged in for PR missions. The orchestrator only
offers models whose key is present (`ModelEntry.has_credentials`).

Stop the daemon with ctrl+c in its terminal. Never leave a local daemon
running against the deployment DB: it holds the leader lock and idle
transactions that block migrations (this happened once, for 1d20h).

## Logs

Every process writes JSON lines to `~/.rework/logs/<component>.log`
(`REWORK_LOG_DIR` overrides; 10 MB x 5 rotation): `daemon` (runners, roles,
the orchestrator, every model call with latency and tokens, every tool call
with its duration, crashes with tracebacks), `tui` (swallowed UI errors,
offline transitions, chat model calls) and `cli`. Records inside a role or the
orchestrator are tagged with `mission`, `run` and `role` automatically.

```bash
rework logs                          # last 100 records, all components
rework logs daemon -f                # follow the engine
rework logs -m m-0d75f495121a        # one mission
rework logs -l warning               # problems only
rework logs tui -g offline           # substring filter
```

On rewvis the daemon's console lines also go to journald
(`journalctl -u rework`), and the JSON file is on the VM:
`ssh ubuntu@100.107.125.118 'cd ~/projects/rework && uv run rework logs -l warning'`.

`REWORK_LOG_LEVEL=DEBUG` adds detail. Prompts and keys are never logged.

## rewvis (the deployment)

The daemon runs as `rework.service` (systemd, User=ubuntu) on rewvis
(tailnet 100.107.125.118), state in Postgres `rework_state` on the
`rework-dev-pg` container (:5433), env in `~/.rework/rework.env`.

Deploy:

```bash
ssh ubuntu@100.107.125.118 'cd ~/projects/rework && git pull --ff-only && uv sync \
  && sudo systemctl restart rework && sleep 3 && systemctl is-active rework \
  && curl -s http://100.107.125.118:8941/healthz'
```

A `DaemonLocked` right after a restart is benign: the old heartbeat is still
fresh; systemd's restart retry wins after one stale window.

Health checks:

```bash
# idle-in-transaction connections block migrations; look before deploying
ssh ubuntu@100.107.125.118 "docker exec rework-dev-pg psql -U postgres -d rework_state \
  -c \"SELECT client_addr, state, count(*) FROM pg_stat_activity \
       WHERE datname='rework_state' GROUP BY 1,2\""
```

Tests never touch `rework_state`. Postgres tests use the dedicated
`rework_test` database:
`REWORK_TEST_POSTGRES=postgresql://postgres:rework@100.107.125.118:5433/rework_test`.

## Preview a PR

`rework preview <n>` runs pull request <n>'s build as a fully isolated second
instance, without touching the real one:

- fetches `refs/pull/<n>/head` from origin into `~/.rework/previews/<n>/repo`
  (a git worktree on branch `rework/preview-pr-<n>`) and runs `uv sync` there;
- copies the state DB with sqlite's backup API into the preview dir (if
  `REWORK_DB` points at Postgres, or at a sqlite file that does not exist, the
  preview starts on a fresh SQLite database instead and says so);
- picks a free port and starts the preview's own daemon in the background
  (logs: `~/.rework/previews/<n>/daemon.log`);
- opens the preview build's TUI in the same terminal, labelled
  `PREVIEW #<n>`.

Quitting that TUI stops the preview daemon and removes the preview. If the TUI
itself exits nonzero the command reports the exit status and the daemon log
path, and (with `--keep`) leaves the preview running instead of tearing it
down. Flags:

- `--keep` — leave the preview running after the TUI exits; the command prints
  how to re-attach and how to remove it.
- `--allow-missions` — let the preview dispatch role runs. By default a
  preview runs no missions (safe no-execute posture).
- `rework preview --list` — show known previews (port, pid, liveness).
- `rework preview --rm <n>` — stop and delete a preview (worktree, branch,
  state copy, descriptor). It works even when the descriptor is missing: the
  worktree is unregistered from `--repo-dir` (or the repo you are in) and
  pruned, and without a resolvable repo it prints the manual `git worktree
  prune` / `git branch -D` commands.

`rework tui` opens the dashboard in its own terminal (the same TUI bare
`rework` starts), which is what the preview command runs inside the worktree.

Previews live under `~/.rework/previews/<n>/` (override the root with
`REWORK_PREVIEW_DIR`); a descriptor `~/.rework/previews/<n>.json` records the
port, pid and paths. In the TUI, mission cards with an open PR get a
**Preview** button that runs the same command and returns to the main TUI
when done.
