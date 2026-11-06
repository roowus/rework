# M7: the dogfood loop

Goal: rework builds rework. The owner describes a change in the TUI, an
orchestrator runs the mission on the rewvis daemon, and the result arrives as a
pull request on `roowus/rework` that the owner reviews and merges.

This replaces `docs/tui/BACKEND-NEEDS.md` as the working plan. There is now one
agent on the project, so the TUI/backend split is gone.

## Acceptance: the loop works when

1. In the TUI Work tab, with project `rework` selected, the owner types a real
   change request. The orchestrator looks things up (tool calls visible),
   proposes a mission, and the owner presses Launch.
2. The daemon on rewvis picks the mission up. Its orchestrator (an LLM, ADR
   0007) spawns roles, reads their handoffs and decides what comes next. The
   chat's mission card and the Dashboard show its decisions and every role's
   trajectory live.
3. Mid-mission the owner can send the mission a message ("also add a test"),
   answer any gate it opens, and pause or cancel it.
4. The mission ends at the `open_pr` gate. Approving it pushes the branch and
   opens a PR whose body is assembled mechanically (summary, audit verdict,
   findings, run stats).
5. The owner merges; CI is green on main; redeploying the daemon is one
   documented command.
6. Steps 1 to 5 succeed for three real rework changes in a row, and their cost
   is visible in the Cost tab.

## Phases

Each phase ends green (pytest, ruff, format, mypy), committed, pushed, CI
checked with `gh run list`, and deployed to rewvis when the daemon changed.

### P0: unblock (done 2026-11-06)

- [x] Fix chat bug B1: tool calls are stored in OpenAI wire format, so any chat
      that uses a lookup crashes on the next model call (`KeyError: 'name'`).
      Add a test that runs a real chat history through BOTH adapters'
      `translate_request` (the FakeProvider never touched an adapter, which is
      why tests missed it).
- [x] Fix chat bug B2: the assistant message that issues look-tool calls is
      never persisted, so a reopened chat replays orphan tool results.
- [x] `Mission.chat_id` (both backends + conformance) so one chat can own many
      missions; the TUI passes it on launch.
- [x] The production project `rework` has `repos: []`. Give it its repo ref
      (`https://github.com/roowus/rework`) so the chat can propose against it.
- [x] Docs truth pass: `design.md` stops describing an orchestrator that does
      not exist; house style (no em dashes) across the new TUI docs.
- [x] A project-only mission inherits the project's single repo, and a URL
      ref becomes an origin (found while wiring the rework project).

### P1: the orchestrator core (ADR 0007 sections 1 to 5)

- [x] Plan kind `auto`: `create_mission` accepts the orchestrator model/effort
      and writes `plan["orchestrator"]`, plus the playbook name, final gates and
      budget (`max_usd` $2, `ask_every_usd` $2, both configurable).
- [x] `Orchestrator` loop in `MissionRunner`: tools `spawn_role`, `wait`,
      `read_artifact`, `finish`; playbooks for fix / fix-pr; rails (budget, run
      caps, concurrency cap, fixed final gates).
- [x] Persistence and recovery: conversation persisted per turn, state digest
      rebuilt from store rows every turn, idempotent spawns; a chaos test that
      kills the daemon mid-mission and resumes.
- [x] Orchestrator trajectory events (`actor="orchestrator"`), rendered in the
      Dashboard and the chat's mission card.
- [x] The chat proposes `auto` missions and hands its own model/effort to the
      mission orchestrator.
- [x] Scripted tests through the real engine (sequential, parallel, kill and
      resume, owner cancel), then a live toyrepo run on a real model: mission
      m-808acff14c50 (local, $0.04, ~3 min) skipped the investigator as too
      small, recovered from a coder whose model had no key, audited, finished,
      ship gate approved, DONE.

### P2: steering and conversation (done 2026-11-06)

- [x] `check_in`, `redirect` (role tool loops read `Run.redirect` between
      turns), `cancel_run`.
- [x] `answer_escalation` (the orchestrator handles question / needs_context
      itself) and `ask_owner` with gate kinds question / choice / permission /
      task, carrying a `spec` the TUI renders (options, placeholder).
- [x] Owner messages: a message in the launching chat becomes an
      `owner_message` event that wakes the orchestrator; its `message_owner`
      notes appear in the chat.

### P3: trust and visibility

- [x] Verification stays with roles (owner: the orchestrator never does work):
      the auditor re-runs the tests; a revision coder continues on the previous
      coder's branch instead of starting from base.
- [ ] Role graph topology on `ROLE_SPAWNED` and node enter events, so the
      Graph tab shows where each role is inside its own LangGraph.
- [ ] Run timing (`started_at` / `ended_at`) and compute time in the Cost tab.
- [ ] `Mission.title` written by the orchestrator for lists.

### P4: dogfood

- [ ] Local first (owner): daemon + TUI on the Mac against a local SQLite DB,
      missions clone rework from GitHub and open real PRs. Runbook in
      `docs/ops.md` and `scripts/rework-local` (done); first real PR pending.
- [ ] Then deploy to rewvis and run the same loop there.
- [ ] Run three real rework changes end to end from the TUI: bugs found along
      the way, TUI work, workflow features. (1/3: the dropdown border,
      m-0d75f495121a, shipped; delivery fixed afterwards, see findings #19.)
- [x] Cost pass after the first dogfood mission: caching, turn budgets,
      compaction, loop resume, branch continuation ($3.03 -> $0.92 for the same
      task with more work done).
- [x] Default plan is `auto` (orchestrated) since P1; findings are recorded in
      `docs/plans/m7-findings.md`.

## Not in M7

Container sandboxing, catalog-in-the-Library, library repo mirror sync, funnel
access, GitHub issue intake. They stay in the design.md backlog.
