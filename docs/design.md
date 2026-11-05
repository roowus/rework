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

Supervisor graph nodes: `intake` → `plan` (LLM, validated) → `dispatch` (SpawnIntents →
Run rows; never spawns tasks itself) → `monitor` (bounded wait, self-loop, store-reconciled)
→ conditional edges to `checkin` (cheap LLM: continue/redirect/spawn/abort) · `escalate`
(mechanical policies first, LLM-authored answers second) · `promote` (tier bump) · `gate`
(interrupt) · `synthesis` (LLM → MissionResult) → `done`.

The engine (`orchestrator/engine.py`) is the heart: MissionRunner streams the graph,
materializes interrupts as gate rows, waits on store-derived wake conditions, resumes with
`Command(resume=...)`. RunSupervisor reconciles INTENT rows into live role asyncio tasks.
The daemon runs one runner per mission under a semaphore, holds a leader lock, and runs a
boot recovery pass (reopen missions, re-enumerate runs, pick up gates decided while down,
before the first astream call).

Roles are NOT subgraphs of the orchestrator checkpoint; each Run is an independent
asyncio task with its own checkpointer thread. Parallelism = N run rows for one step.

### Roles

Code-first `RoleSpec` subclasses; a registry auto-discovers role modules; standard
assemblies (`roles/assembly.py`) compose the shared node library (tool-loop agent,
deep-agents node, gate node, report-writer, git/shell tools, escalate node). Escalation to
the orchestrator is a typed envelope `{kind: blocked|needs_model|needs_tool|
needs_context|question, detail}`: runtime policies act on the kind mechanically; the
orchestrator LLM reads the detail.

Escalation policies (engine, M3): `needs_model` promotes within the provider (catalog
tier order) and resumes the role with no human; when the catalog is exhausted the run
falls through to a gate tracked by id (decisions are read from gate ROWS, and a resume
already in flight is never re-gated). Every other kind opens a gate for the human.
The burn-gate is plan-level: `plan.budget.max_usd` exceeded puts the mission ON_HOLD
with a continue/abort gate; approving doubles the cap and resumes, rejecting cancels.
Steps that FAIL get exactly one in-mission retry (the 429-storm lesson) before the
plan moves on; synthesis counts only the latest attempt per step.

### The TUI (owner ask, post-M5)

Bare `rework` opens a Textual dashboard: projects sidebar, missions table,
live detail pane (2s event tail + inline gate approve/reject), gates queue,
library browser, new-mission wizard + create-project modal, ctrl+p command
palette. It is a CLIENT (own store connection from REWORK_DB), the
scriptable command surface is unchanged underneath, and the quality bar is
Claude-Code-grade: no blocking IO on the UI loop, one in-flight refresh
guard, notify-not-crash on store hiccups, layout survives resizes
(verified 60x20 to 140x44). An in-app assistant model (back burner) would
live here.

### The Library

Items (skills/tools/reports/memories) live as files in a dedicated git repo (canonical
on the VPS, mirrored to GitHub), indexed in Postgres with rich metadata and provenance.
Two-tier memory: project-local namespaces + a global catalog with origin info. Retrieval:
metadata search now, pgvector semantic (hybrid ranking) at scale, one `search()` interface.
Curation is tiered: skills/memories/reports auto-accept; tools always human-gated;
external items only from user-permissioned sites with explicit approval + verification.
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
| M5.5 | Complaints clustering → GitHub issues (table + tool landed early in M3, ADR 0004) | planned |
| M6 | Semantic search + external ingest pipeline + eval-in-CI | planned |
| M7+ | Container sandboxing, dashboard, real event transport, more roles | stubbed |

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
