# CLAUDE.md: rework

rework is a portable multi-agent workflow engine: a LangGraph **orchestrator** supervises
**roles** (each a purpose-tuned LangGraph workflow defined as a `RoleSpec` subclass) to
execute missions, backed by Postgres/SQLite state, a swappable model catalog, and a
cross-harness **Library** of skills/tools/reports/memories (git files + index, exposed
via MCP). Full design: `docs/design.md`; decisions: `docs/adr/`.

## Dev commands

```bash
uv sync                      # install (dev group included); uv.lock is the exact pin set
uv run rework --help         # the CLI
uv run pytest                # unit + integration (SQLite), smoke excluded by default
uv run pytest -m smoke       # real-money tests, run manually only
uv run ruff check .          # lint
uv run ruff format .         # format (CI runs ruff format --check)
uv run mypy src              # type check (kernel/ is strict)
```

Test against Postgres: `REWORK_TEST_POSTGRES=postgresql://... uv run pytest tests/` (the
store conformance suite parametrizes over both backends; Postgres tests skip without the var).

Per-instance env (all optional): `REWORK_DB` (sqlite path or postgres URL),
`REWORK_CHECKPOINTS_DB`, `REWORK_CONCURRENCY`, `REWORK_MODELS` (catalog json path or
inline), `REWORK_GIT_AUTHOR_NAME` / `REWORK_GIT_AUTHOR_EMAIL` (agent commit identity,
defaults `rework <rework@rework.local>`; the owner's deployment uses
`rework@rework.rewis`).

## Working agreements

- **Always be committing.** Commit green work as you go, unprompted. Pushing and PRs
  still require asking the user first.
- **Docs follow code.** Every feature commit updates what it touches: README quickstart,
  `docs/design.md`, a new ADR per significant decision. Docs are never deferred cleanup.
- **Division of verification.** Claude verifies objective behavior itself: tests,
  commands, live process demos (including kill/restart), against Postgres too. The
  owner verifies subjective feel: design, workflow quality, whether a role's output
  is actually useful, using demo-able moments (example problems they can run). Build
  those moments when subjective judgment matters; never ask the owner to run tests.
- **Per-role design sessions.** Before finalizing a builtin role's `build_graph()`, run a
  dedicated design Q&A with the user; they have per-role graph preferences.

## Hard rules (architecture invariants)

1. `interrupt()` must be the FIRST side effect of any node that pauses (gates,
   escalations). Everything before it replays on resume. Only `GateManager.request` and
   the escalate node are sanctioned wrappers.
2. No blocking calls inside async nodes: async SDKs only, subprocesses via
   `asyncio.create_subprocess_exec`. (Linted via ruff `ASYNC` rules.)
3. The store is the source of truth. In-process events/queues are latency optimizations
   only; every monitor/daemon decision re-derives from store rows.
4. Run ids are deterministic: `(mission_id, step, role, attempt)`; `create_run` is
   idempotent. Recovery re-enumerates from the runs table.
5. Wait deadlines live in graph state (epoch seconds), never derived from process uptime.
6. Roles never see each other. Handoffs are typed `ArtifactRef`s, mechanically stripped
   (the auditor receives the diff and nothing else). Verification of mechanical claims is
   done by the orchestrator, not asked of the claimant.

## Dogfooding (self-hosting) rules

rework improves itself: from M3 on, rework issues get worked as rework missions. Missions
targeting this very repo ALWAYS run the SHIP gate: an agent never merges its own PR; the
human does. Agents push branches; main is PR-only (branch protection from M4).

## Repo map

- `src/rework/kernel/`: seams everything else is written against: StateStore (sqlite +
  postgres), EventBus, ModelCatalog + provider adapters, BudgetLedger, GateManager
- `src/rework/orchestrator/`: supervisor graph, engine (MissionRunner/RunSupervisor/recovery), daemon
- `src/rework/roles/`: RoleSpec base, registry, standard assemblies, builtin roles
- `src/rework/nodes/`: node library (llm, toolloop, tools, git, handoff, escalate)
- `src/rework/library/`: the Library (model, store, search, curation, memory, MCP server)
- `src/rework/evals/`: eval harness (tasks, graders, score history)
- `src/rework/cli/`: typer CLI: serve/new/ls/attach/approve/status/roles/eval/library
