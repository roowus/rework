# Your first fix mission

A six-command walkthrough of the `fix` pipeline: an investigator finds the bug, a
coder fixes it on its own branch, an auditor reviews the diff, and the mission halts
at a **ship gate** that only you can pass. It runs against `examples/toyrepo`, a tiny
calculator where `add()` subtracts: both of its tests fail — `test_add` directly, and
`test_mul` because `mul` is built on the buggy `add`.

## 0. Prerequisites

```bash
uv sync                                        # install rework
cp models.example.json ~/.rework/models.json   # then edit model ids/keys to taste
export ZAI_API_KEY=...                         # your provider key (per models.json)
uv run pytest                                  # sanity: the scripted suite, no network
```

All state lives under `~/.rework/` (`state.sqlite3`, `reports/`); override the
database with `REWORK_DB`. Skip the `cp` above and the catalog falls back to a fake
zero-cost provider — fine for watching the plumbing, useless for a real fix.

## 1. Start the daemon (terminal 1)

```bash
uv run rework serve
```

`serve` is the headless daemon: it polls the database for pending missions and drives
the orchestrator. It is leader-locked — one daemon per database (a second one exits
with an error). Leave it running; every later command is a client of the same store.

## 2. Create the mission (terminal 2)

```bash
uv run rework new --plan fix --repo examples/toyrepo \
  "make add(2, 3) == 5: calc.py's add() subtracts"
```

It prints the mission id (e.g. `m-8b201ce75338`) — export it to save typing:

```bash
export MID=m-8b201ce75338
```

`--plan fix` is the investigate -> code -> audit pipeline with a ship gate; it
requires `--repo`. A plain directory is copied into the state workspace and
git-initialized there, so `examples/toyrepo` is never mutated; the fix lands on a
branch in an isolated worktree. (`--plan fix-pr` swaps the ship gate for an open-PR
gate; the default plan is `echo`.) Check it registered with `rework ls`.

Cross-device note: `--repo` is resolved by the machine that runs `rework new`,
not by the daemon. If the daemon runs elsewhere (a VPS, another laptop), you
have three first-class options (ADR 0005):

- `--repo https://github.com/you/repo.git`: the daemon clones the origin
  itself; the creating machine needs no copy of the repo at all.
- `--repo NAME --project p-...`: resolve a repo reference defined on a
  project (`rework project create`).
- `--repo-dir ~/folder`: non-git working files are packed and uploaded to the
  daemon's workspace API (set `REWORK_DAEMON_URL` to the daemon's address;
  the API is tailnet-only when the daemon binds its tailnet IP).

A bare local path (the original form) is staged where `rework new` runs, so
it only works when the daemon shares that filesystem. The results come back
as a branch behind the open_pr gate (git repos) or via `rework fetch <id>`
(any project).

## 3. Watch it work

```bash
uv run rework gates            # open approval gates, across all missions
uv run rework gates $MID       # same, for one mission
uv run rework attach $MID      # stream the event timeline
```

`attach` replays every event from the beginning, then follows live (timestamps,
event type, step, note) until the mission reaches `done`, `failed` or `cancelled`.
Press Ctrl-C to detach — the mission keeps running; the daemon owns the work.
Expect: `investigate` (investigator), `code` (coder, commits with sign
`rework <rework@rework.local>`), `audit` (auditor; if it requests changes, the coder
revises, up to 2 rounds), then a halt.

## 4. Approve the ship gate

When the audit is clean the mission halts at the gate — it will not move until you
decide:

```bash
uv run rework gates $MID
# g-a1b2c3d4e5f6  [ship]  Ship the mission result? audit verdict: clean
uv run rework approve g-a1b2c3d4e5f6
```

`gates` is how you find each gate's id and question. `approve` takes an optional
`--note "why"` that lands in the event log. `--reject` is final: the run aborts and
the mission ends `cancelled` with result `rejected at ship gate` — there is no
automatic loop back to the coder; cut a new mission instead. Because a gate decision
is just a row in the store, you can run `approve` from any machine sharing the
database (your laptop tonight, the deploy box tomorrow). Approval lets the daemon
finish synthesis and mark the mission `done`.

## 5. Judge the result

```bash
uv run rework report $MID          # human-readable: every artifact + run stats
uv run rework report $MID --md     # markdown file (default ~/.rework/reports/<id>.md)
uv run rework report $MID --to-library   # file the report into the Library
```

Plain `report` shows each step's artifact — the investigator's findings and suggested
fix, the coder's branch/commit/diff, the auditor's verdicts — plus mechanical run
stats (model calls, tokens, cache, cost estimate, wall clock). `--md` writes the same
as markdown (default `~/.rework/reports/<id>.md`) and prints a per-step cost summary;
`--out path.md` writes elsewhere. `--to-library` keeps the document instead of
printing it: the body goes into the Library as one `report` item per mission, so a
mission whose report is stranded in a worktree `rework gc` is about to delete stays
readable. The body comes from `--from-file PATH` when you name one, else the
mission's own largest newly added `docs/*.md` in a worktree or branch that still
exists. If the mission only edited existing docs, the largest edited one is used with a
warning and the tag `source:doc-modified`; failing that, `missions.result.summary`,
tagged `source:summary` because that field describes the report rather than containing
it. Re-running updates the same item (no duplicate ids): only items this command filed
(tagged `handoff:report`) are ever updated, never a report a role wrote. A re-run keeps
the item's name unless you pass `--title`, and merges `--tag` into the existing tags.
`--dry-run` prints the item and writes nothing. `--from-file`, `--title`, `--tag` and
`--dry-run` are errors without `--to-library`, and `--to-library` cannot be combined
with `--md` or `--out`.

Judge like an owner: does the diff fix `add`? do
both tests in `test_calc.py` pass now? does the investigation cite file:line evidence
you can check? One caveat to read for: if the investigator couldn't answer something,
it says so plainly in the report's `BLOCKERS` section (an open question, not a pause)
— treat a mission with unanswered blockers as unproven, whatever the gate says.

## Troubleshooting

- `--plan fix requires --repo` — `fix`/`fix-pr` must point at a repo; the echo plan is
  the one that runs without one.
- Mission stuck at `pending` — the daemon isn't running (`serve`, terminal 1), or a
  second daemon took the lock on the same database.
- `no such mission` / `gate not found` — check ids with `rework ls` and
  `rework gates`; mission ids are `m-` + 12 hex chars, gate ids `g-` + 12 hex chars.
- Model auth errors at the first LLM call — `~/.rework/models.json` names its key in
  `api_key_env`; export that variable (e.g. `ZAI_API_KEY`).
