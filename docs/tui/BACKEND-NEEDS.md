# What the TUI rebuild needs from the backend

Audience: the agent working on engine / store / orchestrator. The TUI agent owns
`src/rework/tui/**`, `tests/tui/**` and `docs/tui/**`; everything below lives
outside those paths. Each item says **what** the UI needs and **why**, with a
suggested shape. Shapes are suggestions; the contract the UI codes against is
whatever lands, as long as it is documented here or in `docs/TUI-HANDOFF.md`.

Ordered by how much of the owner's vision depends on it.

Status legend: ✅ landed · 🟡 partial · ⬜ not started (as of writing)

---

## 0. ⬜ The mission orchestrator is not an LLM today (biggest gap)

The owner's mental model (verbatim intent): *the orchestrator manages the whole
flow — it spawns roles, checks in with them, roles report back to it when they
finish; each mission has its own orchestrator running on the model/effort chosen
in the chat that launched it.*

What the code does today (`orchestrator/engine.py::MissionRunner._run_steps`):
a **mechanical loop over a canned plan** (`missions.PLAN_FIX`: investigate → code
→ audit, revise ≤2 rounds, one retry per failed step, escalation policies
applied mechanically). No model is consulted for planning, check-ins, or
synthesis decisions; `docs/design.md` describes an LLM supervisor graph
(intake → plan → dispatch → monitor → checkin → …) that does not exist in code.
Steps also run strictly one at a time (one Run per step), so "many roles in
parallel" is not possible yet.

(The mission-level model the chat sets does reach the roles today:
`missions.create_mission` pops the per-step pins when a model is given. What
is missing is a model *for the orchestrator itself*.)

What the UI needs once this is built:
- `Mission` carries the orchestrator's model + effort (e.g. `plan["orchestrator"]
  = {"model": "...", "effort": "..."}` or columns).
- The orchestrator's own decisions are visible as events, in the same shape as
  role trajectory, so the UI renders them identically. Suggested:
  `PROGRESS` with `run_id=None` and payload
  `{"trajectory": "turn"|"tool_call"|"tool_result", "actor": "orchestrator", ...}`
  where tool calls are its actions (`spawn_role`, `check_in`, `redirect`,
  `open_gate`, `finish`).
- Parallel runs within one step (N Run rows) — the dashboard is designed for it.

## 1. 🟡 Chats ↔ missions are one-to-many

Landed: `chats` + `chat_messages`, persistence in `OrchestratorChat`. ✅

Gap: `Chat.mission_id` is a single value, but one chat launches several
missions. Suggested: `Mission.chat_id` column + `list_missions(chat_id=...)`
(keep `Chat.mission_id` as "most recent" if useful).

## 2. ⬜ The chat orchestrator needs real tools, streamed

The Work tab is "Claude Code for rework": you talk to the orchestrator, and it
acts, with its tool calls visible inline. Today it has only `propose_mission`
and `ask`.

Tools wanted (all go through the store / the same functions the CLI uses):
- read: `list_projects`, `list_missions(project?, status?)`, `mission_status(id)`
  (runs, current role, cost, open gates), `mission_trajectory(id, run?, last_n)`,
  `read_artifact(run_id)`, `search_library(query)`, `list_gates`
- act (owner-confirmed where it spends money or is destructive):
  `propose_mission` (exists), `pause_mission`, `resume_mission`,
  `cancel_mission`, `retry_step`, `create_project`, `add_repo_to_project`,
  `message_mission` (see §3)
- a bounded tool loop (several tool rounds per owner message, cap ~8)

Streaming contract the UI needs — either is fine:
- an async callback `on_step(step)` passed to `send()`, or
- `send()` as an async iterator yielding steps,

where a step is `{"kind": "text"|"tool_call"|"tool_result"|"proposal"|"ask",
...}`. Every step must ALSO be persisted as a `ChatMessage` (with a `display`
string and enough in `body` to re-render: tool name, args summary, result
summary, error) so a reopened chat shows the same trajectory.

## 3. ⬜ Talking to a RUNNING mission

Owner: "you can also tell the orchestrator to do it for you". Once a mission
exists, the owner's messages in that chat should be able to reach that
mission's orchestrator (steer: "skip the audit", "focus on the parser").
Suggested: an `owner_message` event (or table) the mission orchestrator reads at
its next check-in; it answers via an event the UI shows in the chat.

## 4. 🟡 Mission and run controls

In progress (uncommitted at time of writing): pause / resume / cancel via mission
status. Needed in addition:
- one shared module (e.g. `rework.missions.control`) with
  `pause_mission`, `resume_mission`, `cancel_mission`, `retry_step(mission_id,
  step)`, `cancel_run(run_id)`, each appending an event describing who asked —
  the CLI and TUI both call these, never raw `update_mission`.
- retry semantics for a failed/cancelled mission (re-open and re-run from the
  failed step).

## 5. 🟡 Gate variety

Landed: typed `decision` dict delivered to the role. ✅

Needed so the UI renders the right input for each gate without guessing:
- gate kinds beyond approve/reject: `question` (free-text answer), `choice`
  (pick one of `options`), `task` (owner does something by hand, then
  Done / Can't), `permission` (allow / deny, optionally "always for this
  mission").
- a `spec` dict on the gate row, e.g. `{"options": [...], "detail": "...",
  "context_run": "...", "placeholder": "..."}`.
- a documented decision shape per kind, e.g. `question → {"answer": str}`,
  `choice → {"choice": str}`, `task → {"done": bool, "note": str}`.

## 6. ⬜ LangGraph view: topology + "where is it now"

The dashboard draws each run's LangGraph (and the orchestrator's, once it is a
graph) with the current node highlighted, like LangGraph Studio.

- **Topology**: at spawn, put the compiled graph's shape on the event
  (`ROLE_SPAWNED.payload["graph"] = {"nodes": [...], "edges": [[src, dst,
  conditional?], ...]}` from `compiled.get_graph()`). Storing it on the event
  keeps the UI store-only and cross-device; the UI must not build role graphs
  (that needs providers/tools).
- **Position**: a `PROGRESS` event on node enter/exit:
  `{"node": "loop", "phase": "enter"|"exit"}` (LangGraph `stream_mode="updates"`
  or a wrapper gives this cheaply).

## 7. ⬜ Compute time

The cost tab shows time spent alongside money/tokens (including a calendar
heatmap). Needed: `Run.started_at` / `Run.ended_at` (or `run_stats.wall_s`
reliably on every terminal run), and ideally `compute_seconds` in
`spend_history` rows.

## 8. ⬜ Library add from a client

The Library tab can add items. Needed: one function (shared with
`rework library add`) that takes type, title, body, tags, provenance and does
the git write + index upsert, e.g. `rework.library.curation.add_item(...)`.

## 9. Nice to have

- `Mission.title`: a short name for lists (the orchestrator can write it when
  proposing). The UI truncates the objective until then.
- `list_missions(project_id=..., status=...)` server-side filters.
- `events_since(..., types=[...])` filter, so the gates badge poll stays cheap.

---

## Coordination

- The TUI agent never edits engine/store/orchestrator code; the backend agent
  never edits `src/rework/tui/**`. `tui/data.py` is TUI-owned: add store
  methods, the TUI wraps them.
- Commit only your own paths (`git add <paths>`, never `git add -A`) while
  both agents share a working tree.
- When something lands, flip its status here and add the exact signature.
