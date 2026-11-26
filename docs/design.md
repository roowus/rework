# rework design (living document)

This is the living architecture doc. Update it in the same commit as the code it
describes. Decision history lives in `docs/adr/`.

## What rework is

A portable, deployable multi-agent workflow engine. An **orchestrator** (hand-built
LangGraph supervisor) plans and supervises **roles**, each a purpose-tuned LangGraph
workflow defined as a code-first `RoleSpec`. Roles execute **missions**. A compounding
**Library** of skills/tools/reports/memories crosses projects and harnesses. Runs as one
headless process on a laptop or VPS; missions persist and are attachable from any machine
sharing the state database.

```
             ┌──────────────────────────────────────────────────────────┐
 CLI ───────►│  rework core (headless daemon, one process)               │
 rework new  │                                                           │
 ls/attach/  │  ┌────────────┐  spawn/monitor/escalate   ┌────────────┐  │    ┌──────────────┐
 approve     │  │ORCHESTRATOR│◄─────────────────────────►│ROLE        │  │    │  THE LIBRARY │
             │  │(LangGraph  │  typed escalation          │INSTANCES   │◄─┼───►│ git files +  │
             │  │ supervisor)│                            │(LangGraph  │  │MCP │ PG index    │
             │  └────────────┘                            │ threads)   │  │    └──────────────┘
             │  │  kernel: StateStore ─► Postgres / SQLite │            │  │
             │  │  EventBus (asyncio) │ ModelCatalog      └────────────┘  │
             │  │  GateManager │ BudgetLedger │ evals harness             │
             └──────────────────────────────────────────────────────────┘
                  │                     │                      │
             rewvis systemd        GitHub mirror         Claude Code / pi
             (Postgres + lib git)  (backup)              (mount library MCP)
```

## Subsystems

### Kernel (seams)

Everything else is written against these protocols:

- **StateStore** (`kernel/state/`): missions, runs, gates, events, ledger, telemetry.
  Two implementations (Postgres default, SQLite fallback/demo/tests), one conformance
  suite parametrized over both. LangGraph checkpointers hang off the same database.
- **EventBus** (`kernel/events/`): typed events (ROLE_SPAWNED, PROGRESS, ESCALATED,
  GATE_OPENED, DONE, …), in-process asyncio fan-out, write-through to the store.
- **ModelCatalog** (`kernel/llm/`): provider/model entries with tier, cost, caps; three
  adapters: Anthropic, OpenAI, generic OpenAI-compatible (covers DeepInfra/z.ai/DeepSeek
  via config). Promotion moves a run up a tier within the same provider by default.
- **BudgetLedger** (`kernel/budget/`): single persisted writer; enforcement approximate
  (overage tolerated, enforced at next guard), accounting exact.
- **GateManager** (`kernel/gates.py`): human approval gates: request half (interrupt +
  AWAITING_GATE row), resolve half (`rework approve <gate-id>` from any machine sharing
  the DB).

### Orchestrator

**`MissionRunner._run_template`** is a FROZEN mechanical runner over a canned plan
(`missions.PLAN_FIX`: investigate, code, audit, revise on request-changes at most
twice, one retry per failed step, then the ship or open_pr gate). Steps run one
at a time. It exists for the eval harness, which needs the same plan every run,
and for `rework new --mechanical`; it is not the product driver (ADR 0012), and
it gains bug fixes only, never new behaviour. It applies the same finish rails
as the orchestrator (`orchestrator/rails.py`), so a template mission cannot ship
unaudited. The only LLM that talks to the owner on this path is the pre-mission
chat (`orchestrator/chat.py`), which proposes missions.

**Orchestrated missions (ADR 0007, M7; core landed):** `plan.kind == "auto"`
missions are run by `orchestrator/agent.py::MissionOrchestrator`, a
tool-calling agent on the model chosen in the launching chat (or `rework new
--orchestrator`). It only orchestrates: `spawn_role` (roles in parallel when
useful, model and effort chosen per role, inputs wired automatically or
explicitly), `wait` (engine-side, store-derived: a role finished, failed or
escalated, or the orchestrator's own timeout for a check-in), `read_artifact`,
`check_in`, `cancel_run`, `message_owner`, `finish`. `fix` / `fix-pr` / `echo` / `draft`
are playbooks in its prompt, not pipelines. A `draft` mission (an audit or any
other read-and-report task) delivers its report in the mission's Report tab,
never a repository change and never a pull request. Rails stay mechanical: the burn gate
(`plan.budget.max_usd`, then ask again every `ask_every_usd`), a runaway turn
cap, owner pause/cancel, escalation policies, and the plan's fixed final gates
(`_final_gates`, shared with the mechanical runner). Its conversation persists
as chat `orch-<mission>`, each turn's system prompt carries a digest rebuilt
from store rows, spawns are idempotent by tool-call id, and a restart re-runs
an interrupted turn's unanswered calls. Its events and charges carry the pseudo
run id `<mission>:orchestrate:orchestrator:1`. `rework new` creates orchestrated
missions by default; `--mechanical` keeps the canned runner. The orchestrator
can also `redirect` a running role, answer escalations itself
(`answer_escalation`), ask the owner (`ask_owner`, `message_owner`), and it
receives the owner's messages to a running mission.

The engine (`orchestrator/engine.py`) is the heart: MissionRunner streams the graph,
materializes interrupts as gate rows, waits on store-derived wake conditions, resumes with
`Command(resume=...)`.
A gate row is committed before the mission status and the pair is never atomic, so both
an open gate beside a `running` mission and an `awaiting_gate` mission whose gate row is
not committed yet are legal transients: readers converge and wait for both facts
(`conftest.wait_for_gate_park`), never for the gate row alone. The write order is
deliberately not flipped, because status-first would move the transient to
`awaiting_gate` with no gate row, which the daemon's repair pass and the
`_handle_halted` wait loop (it waits for no open gate before writing `running`) both
assume away.
RunSupervisor reconciles INTENT rows into live role asyncio tasks.
The daemon runs one runner per mission under a semaphore, holds a leader lock, and runs a
boot recovery pass (reopen missions, re-enumerate runs, pick up gates decided while down,
before the first astream call).

The leader lock is fenced. Its heartbeat runs in its own task every sixth of the stale
window (`REWORK_LOCK_STALE_S`, default 30 s), not from the dispatch loop, so a slow
iteration cannot let a second daemon in. A heartbeat that returns False (another daemon
took the row) or keeps failing for the whole window ends the daemon: it cancels its
runners and exits with `LeaderLost`, and the service manager restarts it as an ordinary
second daemon. On Postgres both locks are stamped and compared on the database clock, so
client clock skew cannot move the takeover point. Every `serve` exit path closes the
store and the library index (a refused start used to print its error and hang on
aiosqlite's worker thread), and a busy workspace port is a one-line refusal.

Roles are NOT subgraphs of the orchestrator checkpoint; each Run is an independent
asyncio task with its own checkpointer thread. Parallelism = N run rows for one step.

### Reliability

A mission must survive a database that aborts a transaction on purpose.
`kernel/state/retry.py` is the one place that recognises those events and
retries them: SQLSTATE `40P01` (deadlock_detected), `40001`
(serialization_failure) and sqlite's `database is locked`. A store operation
gets 3 total attempts (`MAX_ATTEMPTS`) with a short jittered sleep between them
(`RETRY_SLEEP_RANGE_S`, 10 to 100 ms), exactly one WARNING per retry naming the
attempt number and the SQLSTATE, and a clean connection per attempt (the aborted
transaction is rolled back first). Logical errors (unique violation `23505`,
syntax `42601`, any other non-transient SQLSTATE) are never retried: they
propagate on the first attempt, and exhausted attempts surface as
`TransientDatabaseError` with the original as its cause. Both stores share it:
`_serialized` wraps every public `PostgresStore` method (the op lock is taken
per attempt, inside the retry, so the backoff sleep holds no lock), and
`@retry_transient_methods` wraps `SqliteStore` as a class.

The retry is the safety net, not the fix. Every statement that touches both
`chats` and `chat_messages` takes the `chats` row first (`SELECT 1 FROM chats
WHERE id = $1 FOR UPDATE`), then `chat_messages`: found live on 2026-11-14, a
deadlock on `UPDATE chats SET updated_at = $1 WHERE id = $2` when one
transaction took the two relations the other way round. sqlite does the parity
equivalent (a no-op `UPDATE chats SET id = id`), and the Postgres checkpointer's
`setup()` (DDL on the same database) runs at most once per process and database
URL instead of per run connection, so it cannot race store writes.

`MAX_TRANSIENT_CRASHES` = 3: a runner that dies on a transient database error
does not fail its mission while `transient_crash_count` (a missions column, in
both stores' DDL and idempotent migrations) is at most 3. The mission goes back
to PENDING with `transient_db_error` and a `crash` note in its result
("transient database error, crash 1 of 3, resuming from its checkpoint"), a
WARNING is logged, an INFO event says it will resume from its checkpoint, and
the pickup machinery restarts it from there. Past 3 crashes it fails with
"transient database error, crash N after 3 requeues, giving up: <error>" as the result
error, and a non-transient error fails at once. The counter resets to 0 at a
terminal status, so a later run gets the full budget.

### Logs

`rework/logs.py`: JSON lines per component in `~/.rework/logs/` with
rotation. A contextvar (`logs.bind`) tags every record inside a role run or
the orchestrator with mission / run / role; `build_provider` wraps real
providers in `LoggedProvider` (model, latency, tokens, failures); the tool
loop logs each tool call with its duration; role and mission lifecycles,
runner crashes and uncaught exceptions (sys + asyncio hooks) are logged with
tracebacks. Mission events in the store remain the product record the UI
shows; logs are the operational record. `rework logs` reads and follows them.

### Roles

Code-first `RoleSpec` subclasses; a registry auto-discovers role modules; standard
assemblies (`roles/assembly.py`) compose the shared node library (tool-loop agent,
deep-agents node, gate node, report-writer, git/shell tools, escalate node). Escalation to
the orchestrator is a typed envelope `{kind: blocked|needs_model|needs_tool|
needs_context|question, detail}`: runtime policies act on the kind mechanically; the
orchestrator LLM reads the detail.

| Role | Does | Changes code? |
|---|---|---|
| investigator | Investigates a brief and reports structured findings with evidence | no |
| surveyor | Reads a codebase cold and reports a cited map: layout, entry points, tests, CI, risks, gaps | no: read-only, cannot publish |
| coder | Implements the change in its own worktree and hands off a branch | yes |
| auditor | Verifies another role's diff and informs the ship decision | no |

Surveyor maps, auditor verifies, coder changes. The surveyor's read-only guarantee is
structural and enforced in code (`nodes/readonly.py`): it has no shell at all. Its
executor wrapper is outermost and offers only `read_file`, `list_dir`, `search_code`,
`repo_tree` and `git_read`; every other tool name, `run_command` included, is refused.
The three search tools build their own argv from a fixed template (user values only
land after `-e` or `--`, git is pinned with no pager, fsmonitor, textconv or external
diff), every path passes the same jail as `read_file` (relative, no `~`, no `..`,
resolved inside the workspace), and the argv is handed to the inner executor as its
`run_command`, so cwd, env, timeout and truncation are unchanged. The `audit` plan
(investigator, then surveyor) has no coder step, no gate and no `publish`, so it can
never open a PR. `finish(done)` on such a mission needs no auditor only when NO coder
run exists in it; a coder run of any status still requires a fresh completed audit.

Escalation policies (engine, M3): `needs_model` promotes within the provider (catalog
tier order) and resumes the role with no human; when the catalog is exhausted the run
falls through to a gate tracked by id (decisions are read from gate ROWS, and a resume
already in flight is never re-gated). Every other kind opens a gate for the human.
The burn-gate is plan-level: `plan.budget.max_usd` exceeded puts the mission ON_HOLD
with a continue/abort gate; approving doubles the cap and resumes, rejecting cancels.
Steps that FAIL get exactly one in-mission retry (the 429-storm lesson) before the
plan moves on; synthesis counts only the latest attempt per step.

Delivery is one serialized step (ADR 0013): merge, push and PR create happen under a
`delivery_lock` row in the store, so two missions finishing together cannot both push
against the main each one saw, and the ship path merges main like the PR path does.

An escalation has NO deadline (ADR 0011). A role that asks waits until an answer lands on
its row, its run is aborted, or the mission is cancelled; there is no timeout that tells it
to proceed on its own judgment. While it waits it gives both its role-concurrency slot and
the daemon's mission slot back, so a parked mission costs no capacity, and the ask id is
derived from `(run, question)` so a restart consumes an answer instead of re-asking.

### Tests are a deliverable

A test that cannot fail is worse than no test, because it looks like safety. The
defense, from the owner's workflow discipline (github.com/roowus/workflow, "Why the
test thing is clever"), is red-green: write the test first, run it, watch it fail for
the right reason (the behaviour is missing, not a typo, an import error or a collection
error), then fix and watch it pass; the system demands it and records whether it
happened. What each role owes:

| role | owes on tests |
|---|---|
| investigator | `tests`: the smallest test that would catch the bug (name, what it pins, where, why it fails on current code, command), or a `no_tests_reason` |
| coder | handoff `tests` {added, changed, red_green_verified, commands, result, not_testable_reason}, taken from a fenced json block in its final message; never edits an assertion to get green |
| auditor | a `coverage` section (`untested_behavior`, `vacuous_test`); a claimed red-green is re-checked mechanically (`nodes/testing.py: verify_red_green` reverts the fix files in a throwaway worktree and re-runs at most 3 named commands; one that still passes is vacuous) |
| orchestrator | `finish(done)` is refused when the latest coder handoff has no `tests` field, or says `red_green_verified: false` with no `not_testable_reason` (`_enforce_audit_fresh`, coder runs only) |

The mechanical check runs the coder's named commands through `bash -o pipefail` in a disposable worktree, bounded to three commands, and classifies each run by pytest's own vocabulary: exit 0 with pytest's passing summary in the output is `vacuously_passes`, while exit 0 with no pytest evidence (or with a failure summary the shell's exit code hid, as in `pytest ; true`), and exit 1 with no pytest output at all (the command died before pytest started, e.g. a missing directory or `.venv`), are `unverifiable`, which asks a human to verify and accuses nobody; exit 2 to 5 (interrupted, internal error, usage error, nothing collected) is `wrong_red`; at exit 1, an `E ` line naming `ImportError`, `ModuleNotFoundError`, `NameError` or a module-attribute `AttributeError` is treated as a wrong red, and any other exit 1 that shows pytest ran is `fails_for_right_reason`. The wrong-red rule cannot tell a broken import from an exception raised by the code under test, so it errs toward asking a human to verify rather than toward claiming proof. Known limitation: with `--tb=no` or `--tb=line` pytest emits no `E ` lines, so an import failure is then indistinguishable from a real red.

The mission acceptance re-run of pytest lives in the eval grader, not the engine; the
engine has no verification step and none was added.

### The TUI (rebuilt 2026-11-06, ADR 0006)

Bare `rework` opens a Textual app: a menu bar (wordmark, File / Edit / View /
Mission / Help dropdowns, and a live status cluster: connection, open gates,
spend today), a tab bar, one full-width view per tab, and a hint row. The
visual language (tokens, status glyphs, components, interaction rules) is
specified in `docs/tui/DESIGN.md`; every view is built from that kit.

- **Work**: Claude-Code-style conversation with the orchestrator. Saved chats
  on the left (persisted in the store, synced across devices); the transcript
  shows your messages, the orchestrator's tool calls and results live, one
  proposal card per proposed mission (each with its own Launch and Scrap, so a
  request that spans separate pieces of work never has to squeeze all of it
  through one card), a "Launch all" action on the group, and live mission cards
  (status, last steps, inline gate answers, pause/cancel). Nothing runs until
  you confirm: typing "yes" launches every pending proposal and "no" scraps
  every one of them. Project / model / effort knobs under the prompt. Opens
  with no chat selected. What the list holds is the rule below.
- **Dashboard**: overview (stats strip, projects, missions grouped needs-you →
  queued → running → paused → recent) and a mission drill-down: header + flow strip,
  sessions (one per run) on the left, and Trajectory / Graph (LangGraph-style
  node view, click a node to focus its run) / Output / Diff / Report / Gates /
  Chat (the mission's orchestrator transcript, read-only and scrollable).
  A section is derived from the store, never from a remembered label: **needs
  you** means the mission has an OPEN gate (any status), **running** means
  status `running` or `awaiting_gate` with no open gate left (its gate was
  already decided, so nobody owes an answer), **queued** means status `pending`
  (waiting for a runner slot, not for you), **paused** means `on_hold`, and
  **recent** collects `done`, `failed` and `cancelled`. The stats strip and the
  per-project badges count those same sections through one helper, so the
  Dashboard cannot list a mission under "needs you" while the Gates tab is
  empty.
- **What the Work tab lists.** Chats are conversations; missions are missions.
  A chat is a row iff it is not an orchestrator transcript (`orch-<mission id>`)
  AND it holds at least one `user` or `assistant` message. So an empty chat, a
  chat holding only `note` rows, and a mission's own transcript are never rows.
  Rows are ordered and aged by their newest MESSAGE (`Chat.last_message_at`,
  derived by `list_chats`, never stored), with `updated_at` only as a tiebreak,
  so a rename does not float a chat to the top. Agents never create chats:
  creating a mission mints none (its `chat_id` points at `orch-<id>`, a row
  that appears when the orchestrator first runs). A real conversation that
  launched a mission stays listed and keeps a Transcript button that opens that
  mission's transcript in Work. A mission's transcript otherwise lives on the
  Dashboard (its Chat tab). If the store cannot say which chats hold messages,
  the list is empty for that refresh rather than showing everything.
- **Gates**: inbox oldest-first with full decision context (mission, the run it
  unblocks, the diff + audit verdict for ship gates, recent steps) and an answer
  box; the typed answer reaches the paused role as `Gate.decision`.
- **Library**: search-first (hybrid when embeddings are on), type/status
  filters, reader with activate / retire, and New item.
- **Cost**: range and metric chips, headline numbers, a one-year calendar
  heatmap, a per-day column chart, and breakdowns by model / project / role /
  mission (from `spend_history`).

Rules: mouse first with keys as accelerators (`alt+1..5`, `ctrl+n`, `alt+a/x`);
one click acts; esc goes back, esc twice quits; ctrl+c copies (OSC 52 +
pbcopy), never quits. One `Hub` polls the store and broadcasts snapshots;
every poll is guarded, a dead connection shows `○ offline` with stale data,
and `ReworkApp._handle_exception` turns surprises into toasts. Gate
notifications are configurable (`REWORK_NOTIFY=toast,bell,os|none`, or File ›
Settings). What the TUI still needs from the engine is tracked in
`docs/plans/m7-dogfood-loop.md`.

**What a poll carries.** The poll is small on purpose (one open TUI over a
tailnet once pulled 3 MB/s, 17.5 GB in 1h40m, from
`SELECT * FROM missions ... LIMIT 200` every 2 s):

- *Slim mission list.* The snapshot's missions come from
  `StateStore.list_missions_light` (sqlite and postgres): id, title, objective
  clipped to 200 chars, status, project_id, chat_id, priority, created_at,
  updated_at, repo_path. `plan` and `result` are not selected; on those rows
  `Mission.light` is True and both are `None` (never a silent `{}`). `repo_path`
  stays because the PR preview default reads it off the snapshot. Same
  filters, order and limit as `list_missions`, which still returns full rows.
  `Mission.light` is excluded from serialization, so it never reaches an API
  payload or persisted JSON. Because the objective is clipped, dashboard search
  matches only the first 200 characters of the objective in the list: a token
  that appears only after character 200 is not found (the full row, opened,
  still has all of it).
- *Full row on demand.* A view that needs `plan` or `result` calls
  `TuiData.get_mission`: the dashboard when a mission is opened (kept on
  `full_mission`, refetched only when the row's `updated_at` moves, which any
  plan or result write bumps), the Work view's mission cards, and the Gates
  view when a gate is selected. Never per keypress or per tick.
- *List row.* The dashboard's mission row no longer shows the plan label
  (`fix`, `auto orchestrated`): that was the one list-level read of `plan`.
  The label is still in the mission header and on Work cards.
- *Gates are left alone.* A gate record carries only bounded fields (question,
  decision, note, spec); the diff and audit verdict are fetched per selection.
- *Repaint skip.* `Hub` compares missions, gates, projects, chats, spend and
  the online/error state with the previous broadcast and skips the broadcast
  when nothing changed (the first refresh always broadcasts). Views get
  `on_tick` instead, so live detail polls (events, steps, shared chats,
  history, cost, the library index) keep running without repainting the lists.
  The live paints respect the skip too: an open dashboard mission repaints its
  header, run list and tabs only when something they are drawn from changed
  (mission status or `updated_at`, runs, budget, gates, per-run cost, event
  count, selected run or tab), and a live Work card repaints only when its
  spend moved (new events paint themselves). The Cost view reloads at most
  every 60 s regardless of ticks; the Library recheck is one index read per
  poll that repaints only when its fingerprint moves.
- *Time text.* `ago()`/`duration()` text would freeze on an unchanged
  snapshot, so `on_tick` repaints the text that carries time every
  `TIME_REPAINT_SECONDS` (20 s, a monotonic stamp in `View.time_repaint_due`,
  reset by every `on_snapshot`): the dashboard overview rows, the open mission
  (header, runs, tabs), the gates list and the selected gate, the Work mission
  cards and the Work chat list, and the Library list and reader line. Nothing
  else repaints on a tick. Absolute values (Cost, and the artifact line, which shows a fixed
  run duration `wall_s` and no relative time) do not move and are not
  repainted.
- *Idle backoff.* The app timer ticks every `poll_seconds` (2 s). A tick polls
  when `now - last_poll >= interval`, less one tenth of `poll_seconds` for
  timer jitter, where `interval` is `poll_interval()`: `poll_seconds` while the
  owner is active, `max(poll_seconds, IDLE_POLL_SECONDS)` once they have been
  away (`reload.IDLE_SECONDS` = 30 s with no key, no modal, no chat turn).
  `last_poll` moves only in the branch that polls, so a skipped tick cannot
  starve the next poll. In numbers: active, every tick polls (every 2 s); after
  30 s idle the app still polls, every 10 s (every 5th tick, 6 a minute, an
  overnight TUI keeps discovering missions, gates and offline/online changes);
  a key after idle polls at once (`_kick(force=True)`) and restores 2 s, as does
  ctrl+r. The offline backoff in `Hub.refresh` composes with it (it only ever
  delays a poll further). A tick that lands during teardown (`App.is_running`
  false, or `_closing` set first thing in `on_unmount`) polls nothing.
- *Failed full-row fetch.* If `get_mission` fails for an open mission or a
  Work card, the header shows the light facts and "could not load plan/result
  (retrying)". The same notice is appended to a stale full row when a refetch
  fails after a successful open (the old plan stays visible, marked stale). The retry backs off (2 s, 4 s, ... capped at 30 s, the
  `Hub._next_try` idiom) so an unreadable row is not re-SELECTed on every
  tick, and recovers on its own when the fetch succeeds.

`scripts/measure_poll_bytes.py` prints bytes per poll before and after.

### Several missions from one conversation

One owner request can span separate pieces of work (a bug in one repo, a doc in
another), so the orchestrator may call `propose_mission` more than once in one
turn. The Work tab then shows one proposal card per call, each with its own
Launch and Scrap, plus a "Launch all" action on the group.

- `OrchestratorChat.proposals` is the pending list, in call order. A later turn
  that proposes REPLACES the pending list (`self.proposal` remains a
  compatibility view of the first entry).
- A typed "yes" launches every pending proposal and a typed "no" scraps every
  pending one; `ChatResult.launched_all` / `ChatResult.scrapped_all` carry that
  signal so the UI never has to re-derive which proposal was meant.
- Launching or scrapping ONE card settles only that card; its siblings stay
  pending. Each launch still goes through `Conversation.launch` ->
  `missions.create_mission`, so the per-mission kwargs are unchanged.
- `Chat.mission_id` stays a scalar and keeps the FIRST mission the chat
  launched (a non-NULL value is never overwritten). It is only the denormalised
  pointer the chat list uses for a status glyph; `Mission.chat_id` is the real
  one-chat-to-many-missions link that `WorkView.active_missions()` queries.
- Proposals have no table of their own: they ride in `chat_messages.body` as the
  assistant message's `tool_calls`, so several proposals replay from one stored
  message with no migration. A launch note reads
  `launched <mission id> · <plan> · <objective>`; on replay a proposal is settled
  when such a note names its plan AND its objective (an objective alone cannot
  tell two proposals apart, and a chat may hold two with the same objective and
  a different plan because the model re-proposes when the owner changes their
  mind). A note with only two fields is a legacy `launched <id> · <objective>`
  note: its second field is the objective, so it settles on the objective alone.
  A legacy bare `launched <id>` note settles the OLDEST still-pending proposal.
  In every case a note that matches nothing settles the oldest pending proposal,
  and two proposals that are identical settle the oldest of them (they are
  interchangeable). Everything left pending is restored into the engine by
  `Conversation.ensure_engine`, so a reloaded chat shows and can launch all of
  them. Replay is idempotent: the Work view keeps a high-water mark of the
  `chat_messages` ids it has already rendered, per store connection, so
  reopening a saved chat or reconnecting the store never paints a pending
  proposal card twice.

### The Library

Items (skills/tools/reports/memories) live as files in a dedicated git repo (canonical
on the VPS, mirrored to GitHub), indexed in Postgres with rich metadata and provenance.
Two-tier memory: project-local namespaces + a global catalog with origin info. Retrieval:
metadata search now, pgvector semantic (hybrid ranking) at scale, one `search()` interface.
Curation is tiered: skills/memories/reports auto-accept; tools always human-gated;
external items only from user-permissioned sites with explicit approval + verification.
A skill also needs evidence of generality: a skill is a general, reusable technique an
agent would apply again on a DIFFERENT task (e.g. "how to make a TUI look good"), not a
recap of the one task it just finished (e.g. "toyrepo add() planted-bug: one-line fix
flips both tests green") -- those are reports/memories. An agent contribution of type
skill needs a `reusable_because` justification and must not read like a one-off recap
(deterministic, conservative checks: see `skill_generality_ok()` in
`rework.library.curation`); if it fails, it is auto-accepted as a REPORT instead, never
dropped, with a note telling the caller why and how to resubmit. A human at the CLI
(`--force`) or the TUI new-item form is never blocked by this gate.
An MCP server exposes search/get/contribute so any harness mounts the same library.

### Evals

Telemetry (tokens/cost/duration/outcome per role per phase) is recorded from day one.
The evals package adds task fixtures + a grader ladder (tests → LLM-judge → human
spot-check) + `rework eval run` with stored score history. Optimization loop: failure
taxonomy from traces → hypothesis → one-variable change → eval → keep/revert.

## Isolation model

v1 is logical: roles never see each other (all comms orchestrator-mediated); handoffs are
typed artifacts mechanically stripped (auditor gets the diff only); the orchestrator
mechanically verifies claims (runs the tests itself). v2 (Docker milestone) sandboxes tool
execution per role behind the `ToolExecutor` protocol.

The environment a role command runs with is an allowlist, not an inheritance (ADR 0009):
`role_env` builds from `{}` plus a fixed baseline, so no `REWORK_*_KEY`, `REWORK_DB`,
`SSH_AUTH_SOCK` or proxy credential reaches a role shell. `~/.rework/role_env.toml` grants
additions; `rework role-env show` prints the effective list and `rework role-env allow NAME`
appends an entry. The same allowlist covers the second role-shaped spawn, `verify_red_green`
in `nodes/testing.py`. Until the container lands, `run_command` is still a real shell on the
host: the allowlist narrows what it can read, not what it can reach.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Scaffold + CI (`uv sync && uv run pytest && uv run rework --help` green) | ✅ done |
| M1 | Kernel seams + engine + Echo stub role + CLI + crash-recovery demo | ✅ done (LLM plan/check-in nodes land in M2's supervisor graph) |
| M2 | Vertical slice: investigator→coder→auditor on toyrepo; first self-hosted docs-PR | ✅ done (PR #1 self-hosted, merged) |
| M2.5 | Real-repo effectiveness run on a live project (polystats), owner-driven | waiting until the workflow is competent |
| M3 | Robustness (escalation policies, burn-gate, lock hardening, concurrency, chaos suite) + eval harness v1 + complaints table (ADR 0004 early) | ✅ done (chaos suite closed 2 recovery gaps: offline gate decisions applied on restart, synthesis no longer resurrects burn-rejected missions) |
| M4 | rewvis deploy (systemd, Postgres, deploy key, branch protection) + cross-device | ✅ done (systemd daemon, Postgres state + checkpoints on rewvis, gh auth verified against the private repo; first cross-device mission done: created on the VM, ship gate approved from the Mac, $0.05 total. Open items: branch protection deferred to an owner decision; `rework new --repo` resolves the repo on the creating machine, so cross-device creation needs a daemon-visible path) |
| M4.5 | Projects + workspace transport (ADR 0005): project entity, daemon-side repo resolution, one-shot upload for non-git working files, daemon workspace API (tailnet-only v1) | ✅ done (`b310c9d`..`db171ac`: projects table + conformance, workspace API, CLI project/new/fetch, git transport clone; live: mission created from the Mac with a non-git folder ran to DONE on rewvis via upload, and a URL-only mission cloned the private origin on the daemon; found + fixed en route: unconditional ALTER migrations convoyed store opens behind a live reader, and the echo stub's absent default model failed real deployments) |
| M5 | Library v1 (git store, curation, MCP server, role integration) | ✅ done MVP (`251c1fc`..`cbe9fd3`: item model + git store + index on both backends, CLI add/ls/show/search/activate/retire, curation tiers (tools human-gated), roles carry search/read/contribute + spawn menu, MCP server mounted by Claude Code. Flywheel live-verified: mission m-4b9de000d56c contributed two skills (git-provenance stamped mission+role), read the seeded one, audit approved the contribution as a deliverable. Open for later: pgvector hybrid ranking (M6), library repo mirror sync across devices (back burner), per-project memory namespaces |
| M5.5 | Complaints clustering → GitHub issues (table + tool landed early in M3, ADR 0004) | ✅ done (`fc5adbc`: Jaccard clustering within kind, one gh issue per loud cluster, members → issue_open with URL, gh failure non-destructive; `rework complaints-triage`) |
| M6 | Semantic search + external ingest pipeline + eval-in-CI | ✅ done MVP (`70a07d3`+`7e942e0`: embeddings via REWORK_EMBEDDINGS_URL/KEY/MODEL, vectors as JSON on the item row (native pgvector deferred until scale; dev container lacks the extension), HybridSearch 60/40 keyword+cosine behind the same search() wired into roles/MCP/serve, ensure_indexed backfill; eval-in-CI = toyrepo task through the real engine on a scripted provider in the normal pytest pass (zero cost) with the broken-coder regression pinned; external ingest MVP = allowlist hosts + per-item owner approval, activate refuses un-gated external items) |
| M7 | Dogfood loop: LLM mission orchestrator (ADR 0007), steering from the chat, verification, three real rework changes shipped as PRs from the TUI | in progress (plan: `docs/plans/m7-dogfood-loop.md`) |
| M8+ | Container sandboxing, real event transport, more roles, catalog in the Library | stubbed |

## Backlog (back burner: ideas parked for the owner to flesh out later)

The owner steers: new ideas raised mid-build land here MVP-first; deeper design
happens when the owner picks them back up.

- **Persistent synced workspace**: a project folder kept identical across
  devices (two-way, continuous, conflict resolution). Rejected for M4.5's
  one-shot bundles; revisit as its own design effort.
- **Non-tailnet routes**: Tailscale Funnel or port-forwarded access for
  devices that cannot join the tailnet. The workspace API + token already
  work behind it; adapter only.
- **AI-centered VCS transports**: the owner is researching SCION and GitAI as
  better version-control substrates. `transport` is an adapter seam (ADR 0005);
  these would land as new adapters.
- **Configurable agent-merge**: today humans merge, full stop (no merge code
  exists). If ever wanted, it must be designed as a gated capability; not
  built until asked.
- **Branch protection on main**: blocked by GitHub Pro paywall on private
  repos. Revisit if the repo goes public or the plan upgrades.
- **Investigator workspace population**: investigators file complaints that
  their workdir is empty by design (they get a repo map, not the repo).
  Candidate workflow improvement (mount the repo read-only or index richer
  context); evaluate now that the Library landed.
- **In-app rework assistant**: an AI helper inside the TUI that manages
  rework for you (create a project, draft a mission, organize/curate the
  Library on request). Owner raised 2026-11-05; needs its own design pass.
- **Library repo mirror sync**: the library git repo is daemon-canonical
  (rewvis); other devices share the INDEX via the state DB, but body fetches
  need the daemon until the repo is mirrored (GitHub per the owner plan) and
  cloned elsewhere. Mac mount currently mirrors one seeded file by hand.
