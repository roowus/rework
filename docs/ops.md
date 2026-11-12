# Running rework

## One instance (since 2026-11-12)

There is ONE rework: the Postgres database on rewvis (`rework_state`) is the
source of truth for missions, runs, events, gates, chats, costs and the
Library index, and the rewvis daemon runs every mission. The Mac's TUI talks
to the same database (the owner's shell exports `REWORK_DB` and
`REWORK_DAEMON_URL` for rewvis), so both machines show the same thing.

- `scripts/rework-local` (SQLite on the Mac) is now a SANDBOX for isolated
  experiments, not a second instance. Anything worth keeping can be moved into
  rewvis with `scripts/merge_local_into.py` (dry run first; back up first:
  `docker exec rework-dev-pg pg_dump -U postgres rework_state | gzip >
  ~/backups/...`). The Mac's history was merged on 2026-11-12: 18 missions,
  26 chats, 17 Library items (backup in `~/backups/` on rewvis).
- The Library lives in the database too (#19): item bodies are stored on the
  row, and each machine's `~/.rework/library` git folder is only a fallback
  and local history. The daemon backfills empty bodies from its files on
  start (23 on 2026-11-12). Checked: all 23 items read from the Mac with an
  empty library folder.
- The Mac runs no daemon. The last local mission (m-899f95a66990) was merged
  in a second pass; the local SQLite is a sandbox from here on.

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

A sleeping Mac pauses every local mission: nothing fails, it just stops
until the lid opens (seen 2026-11-09: four stalls that looked like hangs
were clamshell sleep; check `pmset -g log | grep -E "Sleep|Wake"` before
debugging a silent mission). `scripts/rework-local serve` runs under
`caffeinate -i`, which stops idle sleep but not a lid close on battery, so
run long missions on rewvis when you step away.

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

## Get notified when rework needs you

A budget gate once sat open for 4.5 hours because nobody was looking at the
TUI. `REWORK_NOTIFY_CMD` is the fix: the daemon runs that shell command every
time a gate opens, i.e. whenever rework needs your attention or a decision.

The command gets the gate in its environment:

| variable | meaning |
| --- | --- |
| `REWORK_GATE_ID` | the gate's id (`rework gates`, `rework approve <id>`) |
| `REWORK_GATE_KIND` | the kind of gate that opened (e.g. `open_pr`, `question`) |
| `REWORK_GATE_QUESTION` | the question being asked, truncated to 500 chars |
| `REWORK_MISSION_ID` | the mission the gate belongs to |

It is fire-and-forget: the daemon waits at most 30 seconds and never blocks a
mission on it. A nonzero exit or a failure is only logged (see
[Logs](#logs)); the gate stays open either way, and so does the mission.

`scripts/rework-local serve` sets the variable for you when it is unset (and
`osascript` is available): a macOS banner with the gate question. That is the
whole default:

```bash
export REWORK_NOTIFY_CMD='osascript -e "display notification (system attribute \"REWORK_GATE_QUESTION\") with title \"rework needs you\""'
```

Away from the Mac (rewvis, phone), push to your phone with ntfy.sh instead.
The topic is the only secret, so pick a long random one:

```bash
export REWORK_NOTIFY_CMD='curl -s -d "$REWORK_GATE_QUESTION" \
  -H "Title: rework: $REWORK_GATE_KIND gate" \
  -H "Tags: robot" ntfy.sh/rewvis-rework-<long-random-topic>'
```

Set it yourself to use anything else (a webhook, a script); on rewvis put it
in `~/.rework/rework.env`.

## Models and provider keys

Keys live in `~/.rework/rework.env` on each machine (mode 600): rework reads it
at startup without overriding the environment, and systemd loads the same file
on rewvis. Names: `REWORK_FIREWORKS_KEY`, `REWORK_ANTHROPIC_KEY`,
`REWORK_DEEPSEEK_KEY`, `REWORK_DEEPINFRA_KEY`, `REWORK_ZAI_KEY`, and on the Mac
only `REWORK_ROUTER_KEY` (9router on localhost:20128).

The catalog is `~/.rework/models.json` (built 2026-11-10 from each provider's
live model list; a model was kept only if it passed a live tool-call test):

- Anthropic, direct: every model the key can see (Fable, Opus, Sonnet, Haiku).
  The endpoint is always api.anthropic.com, never `ANTHROPIC_BASE_URL` (the
  owner's shell points that at 9router for Claude Code).
- Fireworks: the serverless chat and tool models, priced from the official
  rate card (fireworks.ai/pricing). Kimi K3 (`pro`) is $3 / $15 per M tokens;
  the old catalog said $0.60 / $2.50, so costs reported before 2026-11-10 for
  `pro`-heavy missions were understated about 5x.
- 9router (Mac only): the GitHub Copilot models that answered, priced $0
  (subscription). `glm-flash` / `glm-pro` go through it too and hit the GLM
  plan's usage limit at times.
- Not in the catalog yet: Z.ai, DeepSeek and DeepInfra have working keys but
  no balance (429 / 402). After topping one up, rebuild the catalog or add
  its entries by hand.

Prices for Anthropic come from OpenRouter's list (Anthropic's list prices);
anything without a known price got a deliberately high estimate so budgets
stay conservative.

## Checks before merging (`scripts/ci-local`)

rework never uses GitHub Actions: coders and auditors run the checks
themselves. `.github/workflows/ci.yml` lists those checks and runs only when
started by hand (on every push it used the private repo's whole free
allowance, 2,000 minutes, by 2026-11-11, and GitHub refused all jobs).
Before merging a PR, run the same checks locally on it merged onto main:

```bash
scripts/ci-local 18      # PR #18 onto main, in a scratch clone
scripts/ci-local         # this checkout as it is
```

## Reclaim disk (`rework gc`)

Every role run gets its own git worktree (often with its own virtualenv), and
nothing removes them on its own: 160 of them held 8.1 GB on the Mac. `rework
gc` deletes the run worktrees of missions that are finished (done, failed or
cancelled) and idle for `--older-than-days` (default 2). It keeps each
mission's clone (`missions/<id>/repo`, which the Preview button uses), never
touches an active mission, and skips directories of missions this database
does not know. `--dry-run` lists what it would remove.

```bash
scripts/rework-local gc --dry-run      # the Mac
rework gc --older-than-days 3          # rewvis
```

## rewvis (the deployment)

**Is it up?** rewvis is a long-running box (48 days of uptime on 2026-11-12)
and is essentially always up. The Mac's own tailnet path drops for minutes at
a time, which makes rewvis LOOK down from the Mac; twice that was misread as
an outage. Check it like this, and retry for a couple of minutes before
concluding anything:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://agent.rewis/   # 200 = up (HTTPS; http:// is not the test)
ssh -o ConnectTimeout=15 ubuntu@100.107.125.118 uptime         # the box itself
```

ICMP `ping` and `tailscale ping` failing alone prove nothing. If the owner
can open https://agent.rewis/ in a browser, rewvis is up and the problem is
the path from where you are.

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
itself exits nonzero the preview is kept either way -- the daemon is stopped,
but the dir, worktree, descriptor and daemon log stay so the failure can be
read, and the command prints the log path and `rework preview --rm <n>`.
Flags:

- `--keep` — leave the preview running after the TUI exits; the command prints
  how to remove it and a ready-to-paste attach command that carries the whole
  preview env (`REWORK_DB`, `REWORK_HTTP_PORT`, `REWORK_DAEMON_URL`, the
  preview's library/log/results/reports dirs, `REWORK_TARGET_NAME='PREVIEW
  #<n>'`) and runs `uv run --project ~/.rework/previews/<n>/repo rework`.
  Set `REWORK_PREVIEW=<n>` alone and nothing would read it: only the preview
  daemon's dispatch guard consults that variable, so the TUI would open the
  real instance's DB and port.
- `--allow-missions` — let the preview dispatch role runs. By default the
  missions in the copied state DB are frozen first (any pending/running/
  awaiting_gate/on_hold row is set to `cancelled`, and the count is printed), so
  the preview daemon has nothing to recover or dispatch — the guard inside the
  previewed build cannot protect us, since an older PR does not have it. Pass
  `--allow-missions` to skip the freeze and keep the copy as-is.
- `rework preview --list` — show known previews (port, pid, liveness).
- `rework preview --rm <n>` — stop and delete a preview (worktree, branch,
  state copy, descriptor). It works even when the descriptor is missing: the
  worktree is unregistered from `--repo-dir` (or the repo you are in) and
  pruned, and without a resolvable repo it prints the manual `git worktree
  prune` / `git branch -D` commands. When git cleanup fails (a locked worktree,
  a branch still checked out) the preview dir and descriptor are still deleted
  and the command prints the failing stderr plus the exact manual recovery
  commands.

A preview whose worktree directory was deleted by hand (or by an interrupted
`--rm`) does not wedge the next `rework preview <n>`: the stale registration is
dropped before the worktree is re-added. If the daemon cannot be spawned (no
`uv` on `PATH`) or never becomes ready, the command tears the half-built
preview down and exits 2 with a one-line message.

The preview command runs the preview build's TUI as bare `rework` inside the
worktree, not `rework tui`: the previewed PR may predate the `tui` command,
while every build's CLI opens the TUI when given no arguments. `rework tui` is
still there for the instance you are in.

Previews live under `~/.rework/previews/<n>/` (override the root with
`REWORK_PREVIEW_DIR`); a descriptor `~/.rework/previews/<n>.json` records the
port, pid and paths. In the TUI, mission cards with an open PR get a
**Preview** button that runs the same command and returns to the main TUI
when done. The same command is reachable with no mission focused via **Mission
> Preview a PR...**, which asks for the PR number and a local repo path
(prefilled from the newest mission of the Work tab's project, if any) and
refuses non-numeric PRs, missing paths and non-git directories.
