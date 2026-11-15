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

### Targets (`~/.rework/targets.toml`)

Which database and daemon a command talks to is now a NAMED TARGET, so the
shell profile no longer needs to export `REWORK_DB`. The file is read-only to
rework (stdlib `tomllib`; it is never written) and holds secrets, so keep it
private: `chmod 600 ~/.rework/targets.toml`.

```toml
default = "rewvis"

[targets.rewvis]
db_env = "REWORK_REWVIS_DB"        # the env var holding the URL (secrets stay
                                   # in ~/.rework/rework.env, not in this file);
                                   # or a literal db = "postgresql://..."
daemon_url = "http://100.107.125.118:8941"
ssh = "ubuntu@100.107.125.118"     # optional, for `rework sync-config` and
                                   # `rework update --target rewvis`
library_dir = "/home/ubuntu/.rework/library"  # optional

[targets.sandbox]
db = "/Users/you/.rework/sandbox.db"       # a plain filesystem path: what
                                           # REWORK_DB actually carries for
                                           # sqlite (a `sqlite:////` URL is
                                           # accepted but normalized to this)
daemon_url = "http://127.0.0.1:8941"
```

Selection and precedence (highest wins):

1. `REWORK_DB` / `REWORK_DAEMON_URL` / `REWORK_LIBRARY_DIR` already in the
   environment always win — target application is setdefault-only, so preview
   workspaces/TUIs (`REWORK_TARGET_NAME='PREVIEW #<n>'`) and the rewvis
   systemd unit are never hijacked.
2. `rework --target NAME ...` (the whole command, and the TUI too:
   `rework --target sandbox` alone).
3. `REWORK_TARGET` in the environment.
4. the file's `default = "..."`.
5. nothing configured at all: today's behavior, unchanged.

The TUI status bar shows the resolved target's name (it reads
`REWORK_TARGET_NAME`, which target application also sets — unless the
environment already set it).

```bash
rework targets            # list targets: default marked *, per-target
                          # db reachable? daemon /healthz answers? (unreachable
                          # targets are reported, never crash the command)
rework sync-config TARGET # copy ~/.rework/models.json to TARGET's ssh host and
                          # merge ONLY the REWORK_*_KEY=... lines of local
                          # ~/.rework/rework.env into the remote
                          # ~/.rework/rework.env (mode 600 kept; REWORK_DB,
                          # REWORK_DAEMON_URL and every other remote line are
                          # left completely untouched). Prints key NAMES and
                          # value LENGTHS only, never values.
rework sync-config TARGET --dry-run   # show what would change; the remote host
                                      # is never written
```

`sync-config` requires the target to have an `ssh` host (clear error if not).
`scripts/rework-local` is now just `rework --target sandbox`: a thin wrapper
around the sandbox target; the real values live in `targets.toml`.

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
`~/.rework/work/`, and — after a clean audited `done` — pushes the last coder's
branch and opens the PR with your local `gh` login. **No gate by default**: the
audit rail already guarantees the change was reviewed, and most fixes —
especially ones you asked for — ship without a gate. A gate is only for the most
crucial cases.

Asking for a gate is strict (owner decision, 2026-11-14). The orchestrator may
pass `finish(ask_before_pr=true, gate_category=..., reason=...)` ONLY when the
change falls in one of four high-stakes categories, and the `reason` should name
that category. What the engine mechanically enforces is narrower: a non-empty
`reason` and a `gate_category` from the list below. Your gate question shows
both — the category first, then the reason — so you can judge the request.
Anything else is refused with a tool error telling the orchestrator to open the
PR without a gate; a change you asked for is never gated for being large,
unusual, or touching many files, and audit findings the orchestrator accepted
belong in the finish `summary` — which becomes the PR body you read before
merging.

| `gate_category` | the change does... |
| --- | --- |
| `data_loss` | data loss or a destructive data migration |
| `security` | security, credentials, auth or permissions |
| `money` | money (billing, payments, paid resources) |
| `production_infrastructure` | irreversible or production infrastructure (deploys, DNS, cloud resources, deleting user files) |

The gate question then leads with the category and the orchestrator's reason
("Gate category: security — ..."), so you can decide from the inbox.

Only a plan that publishes (`"publish": "pr"` — the `fix-pr` playbook; older
plans carrying `"gates": ["open_pr"]` count too) pushes a branch or opens a PR
at all. `echo` and `fix` missions deliver their result and stop.

| project `settings.pr_gate` | effect |
| --- | --- |
| `auto` (default) | gate only when the orchestrator asks (or the plan lists `open_pr`) |
| `always` | always gate before opening the PR (overrides the orchestrator) |
| `never` | never gate; an `ask_before_pr` is noted and overridden |

Merging is always the owner's decision; no agent merges.

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

## Repo-less projects (documents, no code repo)

A project with no repo is an **upload** project: `rework` keeps its working
files itself, on the daemon host, so missions on it need no repo at all (ADR
0005, Amendment). Create one by leaving `--repo` off:

```bash
rework project create applications   # -> transport=upload, results=fetch
rework project ls                    # verify: transport=upload results=fetch
```

In the TUI: **File > New project...** and leave the repo field empty — the
form says rework keeps the project's files itself and results are fetched, not
pushed. (A project created *with* a repo is `git`/`push_branch`; the transport
and results are derived from the repos and cannot be set against them.)

Put files in from any machine, any number of times, before or between
missions:

```bash
rework project add-files applications ./docs ./notes.md
```

In the TUI: the dashboard project sidebar's **Add files** button, or **File >
Add project files...**, takes one local path per line (a path containing
spaces is fine; home-relative `~` is expanded). It refuses a git project: for
those, push a branch.

Run a mission on it — no `--repo` needed:

```bash
rework new --plan fix --project applications "tidy the applications folder"
```

In the TUI, a chat proposal that names an upload project launches the same
mission (a repo the model names is dropped). The mission starts from the
project's files and saves its new/changed files back when it finishes; the
mission report then ends with a line naming them, e.g. `Project files
updated: a.md (new), b.md (changed)`. Deletions are not carried back: the
save-back adds and updates only.

Files are saved back **only when the mission ends DONE**: a cancelled or
ship-rejected mission leaves the area untouched (its worktree is half-finished,
not a deliverable). If the save-back itself fails, the mission still ends DONE
but its report says `Project files NOT updated: <reason>` — never a silent
success.

Do **not** run two missions of the same project against the same files at the
same time. The area is locked per operation, so they serialise rather than
corrupt each other, but the later mission seeds from the state the earlier one
has left so far, i.e. it starts from a mix of the two.

Get results back on whichever machine you work from:

```bash
rework project pull applications ./out    # the whole project files area
rework fetch m-0d75f495121a               # one mission's own final worktree
```

The files area lives on the **daemon host** (rewvis) at
`~/.rework/projects/<project-id>/files`; `REWORK_PROJECTS_DIR` overrides the
root. It is a plain directory and a git repository (local history, no remote),
so back it up like any data directory — copy or archive it:

```bash
ssh ubuntu@100.107.125.118 'tar czf ~/backups/projects-$(date +%F).tar.gz -C ~/.rework projects'
```

Files are **never pushed anywhere**: the area has no remote and nothing ever
uploads it. Code still travels through git only — a project that carries code
gets a repo and the git/push_branch transport, and its deliverable is a pushed
branch, not this area.

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

## Cost and prompt-cache hit rates

Exact accounting is the `charges` table, one row per model call:
`mission_id`, `run_id`, `model`, `input_tokens`, `output_tokens`,
`cached_tokens`, `cost_usd`, `ts`. `input_tokens` INCLUDES the cached ones
(the adapters normalize to OpenAI semantics: Anthropic's separate cache-read
and cache-write counts are folded into it), so the prompt-cache hit rate is
`SUM(cached_tokens) / SUM(input_tokens)`. Group by model, or by role through
`runs` (`charges.run_id = runs.id`, `runs.role`), over a `ts` window:

```bash
# rewvis (Postgres). For a local sqlite DB the same SQL works with
# sqlite3 <file>, swapping the window for ts >= strftime('%s','now') - 7*86400
ssh ubuntu@100.107.125.118 "docker exec rework-dev-pg psql -U postgres -d rework_state -c \"
  SELECT c.model, SUM(c.input_tokens) AS input, SUM(c.cached_tokens) AS cached,
         round(100.0 * SUM(c.cached_tokens) / NULLIF(SUM(c.input_tokens), 0), 1) AS hit_pct
    FROM charges c WHERE c.ts >= extract(epoch from now()) - 7 * 86400
   GROUP BY 1 ORDER BY input DESC\""
# ... and per role: SELECT r.role, SUM(c.input_tokens), SUM(c.cached_tokens)
#     FROM charges c JOIN runs r ON r.id = c.run_id WHERE c.ts >= ... GROUP BY 1
```

A long role loop sits at 85-95%; the orchestrator sat at 37.8% while its
per-turn "CURRENT STATE" tail carried the only message breakpoint. `rework
report <mission>` prints `cached` per run, and the TUI's Cost view groups the
same ledger by model, project, role and mission.

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
does not know. It also reclaims the per-run scratch dirs `role_env` gives every
non-checkout workdir in the system temp dir (`rework-scratch-<mission>-<random>`):
the ones naming a finished mission are removed (they are dead the moment the
mission ends), the unattributed legacy `rework-scratch-<random>` ones are left
alone. `--dry-run` lists what it would remove and prints both counts.

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

Deploy: `rework update --target rewvis` does the whole thing from the Mac —
fetch, fast-forward, `uv sync`, `sudo systemctl restart rework`, then report
`systemctl is-active`. It refuses (exit 1, nothing moved) rather than guessing
when the remote tree is dirty, off `main` or diverged, and `--check` only
reports whether the remote checkout is behind.

```bash
rework update --target rewvis          # deploy, then the health check below
rework update --target rewvis --check  # report only; nothing on rewvis changes
```

This needs the target's `targets.toml` entry to have an `ssh` host (the
`rewvis` entry in [Targets](#targets-reworktargetstoml) above has one; without
it the command says so and exits 1).

A `DaemonLocked` right after a restart is benign: the old heartbeat is still
fresh; systemd's restart retry wins after one stale window.

### rework cannot overload its host

rewvis is 2 CPUs, 12 GB of RAM, 4 GB of swap, and it is shared: sshd,
tailscaled, Postgres (`rework-dev-pg`), journald and the daemon all live on it.
rework therefore bounds itself. Three knobs, all optional, all in
`~/.rework/rework.env`:

| variable | default | what it caps |
| --- | --- | --- |
| `REWORK_MAX_ROLES` | one per CPU (`os.cpu_count()`, min 1) — 2 on rewvis | roles RUNNING at the same time, across all missions |
| `REWORK_ROLE_MEMORY_MAX` | 60% of RAM | the memory one role's command tree may use before the kernel kills it |
| `REWORK_CONCURRENCY` | `min(cpu_count, 4)` — 2 on rewvis | working MISSIONS at the same time |

`REWORK_MAX_ROLES` is daemon-wide: every `spawn_role` counts, whichever mission
it belongs to. Runs beyond the cap are not refused, they queue — the run row
stays `intent` and the orchestrator sees `queued (waiting for a free role
slot, cap N)` in `wait` / `check_in`, so a queue never looks like a hung role.

`REWORK_ROLE_MEMORY_MAX` accepts `4G`, `512M`, `60%` or raw bytes (a percentage
is that share of total RAM, resolved once at startup). rework applies it through
a chain of mechanisms and uses the FIRST one that works on the box, so a single
runaway `pytest`/`mypy` dies on its own instead of taking the box (or the
daemon) down:

1. a systemd transient USER scope with `MemoryMax=` (`systemd-run --user
   --scope`) — interactive, user-managed installs;
2. a systemd transient SYSTEM scope with `MemoryMax=` (`systemd-run --system
   --scope --uid=<uid>`, plus `MemoryAccounting=yes` and `--collect`) — this is
   the one in force on rewvis: a transient user scope needs a user bus, and
   `rework.service` has none (`systemd-run --user` there fails with "Failed to
   connect to bus: No medium found" even though lingering is enabled for
   ubuntu), whereas the system bus is there and `--uid` runs the command as the
   owner, not root. It is only attempted when `/run/systemd/private` exists and
   it fails soft (3 s probe, cached once per process);
3. a hard `ulimit -v` in the wrapper shell (`/bin/sh -c 'ulimit -v <kB>
   2>/dev/null; exec <command>'`) — no systemd, no cgroup, no privileges
   needed, and it still turns swap-thrashing into an allocation failure. It
   only affects the wrapper's children, never the daemon;
4. no memory limit at all, if `REWORK_ROLE_MEMORY_MAX` is unset or unparseable
   and no RAM could be read.

Role commands are deprioritized on every path so the work rework does stays
behind sshd, tailscaled and Postgres: `nice -n 10`, and the IO IDLE class —
`ionice -c3` in the fallback and `IOSchedulingClass=idle` in a scope. rework
says which mechanism is actually in force in ONE daemon log record at the first
command (`role commands run in a systemd system scope with MemoryMax=5G: …`, or
`… with MemoryMax=5G (no systemd scope available: applying ulimit -v instead;
…)`, or `… with NO memory limit`). Check it with `rework logs daemon -g 'role
commands run'`.

`REWORK_CONCURRENCY` caps WORKING missions only. A mission that is waiting — at
a gate, for the owner, or on hold — gives its slot back and takes a new one
before it works again, AHEAD of missions that never started
(`PrioritySlots`; order is FIFO inside each class). Before that, with
`REWORK_CONCURRENCY=1`, an approved `open_pr` gate could sit for hours behind a
mission that was merely parked at its own gate, and on 2026-11-14 behind every
never-started mission even at 5 slots (the mission also kept saying
`awaiting_gate` while the Gates tab was empty). The engine now sets the mission
back to `running` the moment its last gate is decided, and the daemon corrects
any `awaiting_gate` row with no open gate on every poll (a log line and an INFO
event say so).

A sqlite deployment keeps its historical concurrency of 1 unless the env says
otherwise; Postgres deployments get the host-aware default.

#### The 2026-11-13 incident

The reason all three exist. The kernel OOM killer fired three times — Nov 12
19:23, Nov 13 06:23 and 06:58 UTC — the last killing a role's `mypy` at 790 MB
*inside* `rework.service`, which took the service itself down. From about
16:20 UTC the box was swap-thrashing instead: journald wrote its lines out of
order, no OOM kill came, and nothing answered until a hard reboot at 17:02.

The load that did it: between 15:57 and 16:24 UTC four missions ran at once,
one of them a batch mission running four investigators in parallel — 6-7 roles
at a time, each running `pytest`, `mypy` and `uv sync` on 2 CPUs and 12 GB.
`REWORK_CONCURRENCY=1` was the stopgap until the caps above existed.

Look for OOM kills before blaming the network, a bad deploy or a hung daemon;
they are logged by the kernel, not by rework:

```bash
ssh ubuntu@100.107.125.118 "journalctl -k | grep -i oom"          # every OOM kill
ssh ubuntu@100.107.125.118 "journalctl -u rework --since '2026-11-13 06:00'"  # died too?
ssh ubuntu@100.107.125.118 "free -h; uptime; systemctl is-active rework"
```

A `rework.service` that died without a Python traceback is an OOM kill, not a
bug: look for `Killed process ... (python3)` in the kernel log and lower
`REWORK_MAX_ROLES` (1 is the floor and it is a legitimate setting).

#### Boot recovery: `rework-dev-pg` comes up with no network

`rework-dev-pg` publishes its port on the Tailscale address
`100.107.125.118:5433`, not on `0.0.0.0`. After a reboot Docker started before
tailscaled had assigned that address, the port bind failed with `cannot assign
requested address`, and the container came up with no network at all — which
looks exactly like a dead database from the Mac:

```bash
ssh ubuntu@100.107.125.118 "docker inspect -f '{{json .NetworkSettings.Ports}}' rework-dev-pg"
ssh ubuntu@100.107.125.118 "docker logs --tail 30 rework-dev-pg | grep -i 'cannot assign'"
ssh ubuntu@100.107.125.118 "ip -brief addr show tailscale0"   # is the address there?
```

Only recreate the container if the data volume is safe. Back the database up
first, and keep the broken container under a new name instead of deleting it:

```bash
ssh ubuntu@100.107.125.118 "mkdir -p ~/backups && docker exec rework-dev-pg \
  pg_dump -U postgres rework_state | gzip > ~/backups/rework_state-$(date +%Y%m%d).sql.gz"
ssh ubuntu@100.107.125.118 "docker rename rework-dev-pg rework-dev-pg-broken-20261113"
# then `docker run` the new container on the SAME data volume
```

The real fix is ordering, in
`/etc/systemd/system/docker.service.d/wait-for-tailscale.conf`:

```ini
[Unit]
After=tailscaled.service
Wants=tailscaled.service

[Service]
# a Docker port published on the tailnet address needs tailscaled to have
# assigned it first, or the bind fails and the container comes up offline
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do \
  ip -brief addr show tailscale0 | grep -q 100.107.125.118 && exit 0; sleep 1; done; \
  echo "tailscale address 100.107.125.118 not up after 60s" >&2; exit 1'
```

Then `sudo systemctl daemon-reload` and `sudo systemctl restart docker`. Verify
`docker ps` shows the port mapped and the daemon's Postgres connection is
fresh: `systemctl is-active rework` plus a mission that is not stuck.

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

## Mission priority (`normal` | `high`)

Every mission carries a pickup priority, `normal` by default. It only affects
the order the daemon starts pending missions in: high priority first, oldest
first within a level (`StateStore.next_pending_missions`). The TUI's mission
list keeps its newest-first browse order.

- `rework new "…" --priority high` creates a high-priority mission.
- `rework prioritize <mission-id>` sets it to `high` (the default) and
  `rework prioritize <mission-id> --priority normal` puts it back.
- The Dashboard's mission controls have a **Priority** button that flips it in
  one keypress and confirms in the status line.

The default is `normal` for every project — there is no automatic high for
non-rework projects; the explicit flag and the Dashboard button cover it.
