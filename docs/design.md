# rework — design (living document)

This is the living architecture doc. Update it in the same commit as the code it
describes. Decision history lives in `docs/adr/`.

## What rework is

A portable, deployable multi-agent workflow engine. An **orchestrator** (hand-built
LangGraph supervisor) plans and supervises **roles** — each a purpose-tuned LangGraph
workflow defined as a code-first `RoleSpec` — to execute **missions**. A compounding
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

- **StateStore** (`kernel/state/`) — missions, runs, gates, events, ledger, telemetry.
  Two implementations (Postgres default, SQLite fallback/demo/tests), one conformance
  suite parametrized over both. LangGraph checkpointers hang off the same database.
- **EventBus** (`kernel/events/`) — typed events (ROLE_SPAWNED, PROGRESS, ESCALATED,
  GATE_OPENED, DONE, …), in-process asyncio fan-out, write-through to the store.
- **ModelCatalog** (`kernel/llm/`) — provider/model entries with tier, cost, caps; three
  adapters: Anthropic, OpenAI, generic OpenAI-compatible (covers DeepInfra/z.ai/DeepSeek
  via config). Promotion moves a run up a tier within the same provider by default.
- **BudgetLedger** (`kernel/budget/`) — single persisted writer; enforcement approximate
  (overage tolerated, enforced at next guard), accounting exact.
- **GateManager** (`kernel/gates.py`) — human approval gates: request half (interrupt +
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
boot recovery pass (reopen missions, re-enumerate runs, pick up gates decided while down —
before the first astream call).

Roles are NOT subgraphs of the orchestrator checkpoint — each Run is an independent
asyncio task with its own checkpointer thread. Parallelism = N run rows for one step.

### Roles

Code-first `RoleSpec` subclasses; a registry auto-discovers role modules; standard
assemblies (`roles/assembly.py`) compose the shared node library (tool-loop agent,
deep-agents node, gate node, report-writer, git/shell tools, escalate node). Escalation to
the orchestrator is a typed envelope — `{kind: blocked|needs_model|needs_tool|
needs_context|question, detail}` — runtime policies act on the kind mechanically; the
orchestrator LLM reads the detail.

### The Library

Items (skills/tools/reports/memories) live as files in a dedicated git repo — canonical
on the VPS, mirrored to GitHub — indexed in Postgres with rich metadata and provenance.
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
| M1 | Kernel seams + engine + Echo stub role + CLI + crash-recovery demo | planned |
| M2 | Vertical slice: investigator→coder→auditor on toyrepo; first self-hosted docs-PR | planned |
| M3 | Robustness (recovery paths, promotions, budget E2E, concurrency) + eval harness v1 | planned |
| M4 | rewvis deploy (systemd, Postgres, deploy key, branch protection) + cross-device | planned |
| M5 | Library v1 (git store, curation, MCP server, role integration) | planned |
| M6 | Semantic search + external ingest pipeline + eval-in-CI | planned |
| M7+ | Container sandboxing, dashboard, real event transport, more roles | stubbed |
