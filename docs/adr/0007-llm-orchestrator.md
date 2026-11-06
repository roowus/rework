# ADR 0007: The mission orchestrator becomes an LLM agent (playbooks, not pipelines)

## Status

Accepted (2026-11-06; owner answers folded in, see the end).

## Context

ADR 0001 decided on a hand-built supervisor in which an orchestrator plans,
spawns roles, checks in with them, handles their escalations and decides what
happens next. The owner's description of the product is the same: "I tell the
orchestrator about an issue, it starts an investigator, then a coder, then an
auditor, maybe with periodic check-ins."

That orchestrator was never built. The M2 vertical slice shipped `PLAN_FIX`, a
canned step list, as a stopgap, and `MissionRunner._run_steps` became a
mechanical loop over it:

- steps run strictly in order, one run per step, no parallelism
- routing is two hard rules (one retry on failure; audit "request changes"
  sends the coder back, at most twice)
- escalations are handled by mechanical policy (needs_model promotes) or become
  human gates; nothing reads the prose
- synthesis is mechanical
- the only LLM that talks to the owner is the pre-mission chat, which proposes
  and then forgets the mission exists

`docs/design.md` described the intended supervisor graph as if it existed.
This ADR replaces that description with a buildable design.

## Decision

### 1. One orchestrator agent per mission

Each mission gets an orchestrator: a tool-calling agent loop that runs inside
the daemon's `MissionRunner`, on the model and effort chosen in the chat that
launched it (`plan["orchestrator"] = {model, effort}`). It replaces
`_run_steps` for missions whose plan kind is `auto`; the canned `fix` /
`fix-pr` runners stay as a fallback.

The orchestrator's tools:

| tool | effect |
|---|---|
| `spawn_role(role, step, brief, context_from, model?, effort?, sections?)` | creates a Run row (deterministic id, idempotent) and starts it; several calls in one turn run in parallel |
| `wait(minutes?)` | NOT a model call: the engine blocks until a run finishes, escalates or halts, a gate is decided, the owner sends a message, or the orchestrator's own timeout passes, then returns what happened |
| `read_artifact(run_id)` | the role's typed handoff (findings, diff summary, verdict) |
| `check_in(run_id)` | the role's recent trajectory (turns, tool calls, results) |
| `redirect(run_id, instruction)` | writes `Run.redirect`; the role's tool loop injects it at its next turn |
| `cancel_run(run_id)` | stops a role that is going nowhere |
| `answer_escalation(run_id, answer)` | resolves a role's `question` / `needs_context` escalation without bothering the owner |
| `ask_owner(question, kind, options?)` | opens a gate (question / choice / permission / task) and waits for it |
| `message_owner(text)` | a progress note shown in the chat that launched the mission |
| `finish(outcome, summary)` | ends the loop; the plan's final gates (ship / open_pr) then run mechanically, as today |

The orchestrator only orchestrates. It never does mission work itself (no
editing, no running tests, no investigating): every piece of work is a role.
Verification of a role's claims is also delegated (the auditor re-runs the
tests; a dedicated verifier role can come later), never performed by the
orchestrator.

Roles run asynchronously and report back when they finish. Check-ins are the
orchestrator's choice, not a cadence: it decides per mission whether and when
to look in on a running role (`wait(minutes)` then `check_in`), more often for
hard or failure-prone work, not at all for easy work.

It also picks each role's model and effort from the catalog (costs and tiers
are in its prompt), so a cheap model can investigate while a strong one audits.

### 2. Plans become playbooks

`fix` and `fix-pr` stop being executable pipelines. They become playbooks: a
paragraph in the orchestrator's prompt describing the usual shape
(investigate, then code, then audit, revise on request-changes, then the
gate). The orchestrator follows the playbook by default and may deviate when
the mission calls for it: skip investigation for a one-line fix, run two
investigators in parallel, send the auditor's findings back to an
investigator. Playbooks can later live in the Library as skills.

### 3. Rails stay mechanical

The model decides; the engine enforces. Limits the orchestrator cannot talk
its way past:

- budget: `plan.budget.max_usd` (default $2) and the burn gate: past it the
  mission goes ON_HOLD and asks the owner; approving extends the cap by
  `plan.budget.ask_every_usd` (default $2), so a long mission asks again every
  step. Both are configurable per mission and by env.
- no hard cap on parallel roles; the prompt tells the orchestrator to be
  conservative and the budget rail bounds the damage
- a high cap on orchestrator turns (runaway protection only)
- final gates are fixed by the plan, not by the model: a mission targeting the
  rework repo always ends at `open_pr` (agents push branches, humans merge)
- `needs_model` escalations keep their mechanical promotion policy
- owner pause / cancel (mission status) preempt everything

### 4. The store stays the source of truth (crash recovery)

The orchestrator's conversation is persisted turn by turn (the same pattern as
chats: provider-neutral message bodies), and every turn's prompt opens with a
digest rebuilt from store rows (runs, statuses, artifact summaries, open
gates, spend). After a daemon restart the runner reloads the conversation,
rebuilds the digest and continues. Spawns are idempotent because run ids are
deterministic `(mission, step, role, attempt)`; a re-issued `spawn_role` for a
step whose latest attempt is live or done returns that run instead of starting
another.

### 5. Visible like the roles

Orchestrator turns and tool calls are published as trajectory events
(`PROGRESS`, `run_id=None`, `actor="orchestrator"`), so the Dashboard and the
chat's mission card render them exactly like a role's loop. `message_owner`
notes and owner messages appear in the launching chat.

### 6. Talking to a running mission

An owner message in the chat that launched a mission is stored as an
`owner_message` event. It wakes the orchestrator's `wait()`, so "skip the
audit" or "focus on the parser" reaches the agent that is running the mission.

## Consequences

- `MissionRunner` grows an orchestrator loop; the mechanical runner remains
  for `fix` / `fix-pr` and for scripted tests.
- Roles need one change: the tool loop reads `Run.redirect` between turns.
- Every mission costs some orchestrator tokens. Turns happen only on events
  (spawn, finish, escalation, owner message, check-in), not on a timer
  spinning a model.
- Tests: scripted-provider tests drive the orchestrator through the real
  engine (spawn, parallel wait, escalation answer, redirect, crash and resume
  mid-mission); a live toyrepo eval on a real model gates the switch of the
  default plan from `fix-pr` to `auto`.

## Owner answers (2026-11-06)

1. The orchestrator always orchestrates; it never performs tasks. The canned
   runner survives only as a scripted-test fixture and a legacy CLI path.
2. No check-in cadence: roles report back when done; the orchestrator chooses
   whether and when to check in, by difficulty and failure risk.
3. Parallel roles: yes, no hard limit, prompt says be conservative.
4. The orchestrator runs on the model picked in the chat and picks the roles'
   models itself.
5. Budget: $2 per mission, then ask; ask again every configurable step.
6. Dogfood targets: bugs found along the way, TUI work, workflow features.
7. Make it work locally first (Mac daemon + local DB), then rewvis.
