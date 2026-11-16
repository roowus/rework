# rework TUI handoff: facts you need before you build it

You are building (or rebuilding) the terminal UI for **rework**. This document is
the factual context: what rework is, what state exists, what APIs you read and
write, and the traps that cost the previous implementation real debugging time.

The current implementation is `src/rework/tui/app.py` + `src/rework/tui/data.py`
and its tests are `tests/tui/test_tui.py`. You may keep, rewrite or replace all of
it. The data layer (`data.py`) is largely independent of presentation and is worth
reading before you discard anything.

---

## 1. What rework is

A portable multi-agent workflow engine. An **orchestrator** supervises **roles**
(`investigator`, `coder`, `auditor`, plus test-only `echo`) to complete a
**mission**. State lives in Postgres (deployment) or SQLite (local), so a mission
runs on a daemon and can be watched/controlled from any machine that can reach
that database.

Vocabulary that appears everywhere:

| term | meaning |
|---|---|
| **mission** | one unit of work: an objective + a plan + a status |
| **run** | one role's execution inside a mission, e.g. `m-abc:code:coder:2` |
| **gate** | a human decision that BLOCKS the mission until answered (ship / open_pr / spawn / generic) |
| **escalation** | a role asking the orchestrator for a model, tool or context; unresolved ones become gates |
| **event** | an append-only record on the mission (spawn, progress, gate opened, done, …) |
| **trajectory** | a PROGRESS event carrying one agent-loop step (turn / tool_call / tool_result) |
| **library** | cross-project store of skills, tools, reports, memories |
| **charge** | one model call's tokens + cost — the exact accounting record |

Design docs if you need depth: `docs/design.md`, `docs/adr/0001`–`0005`,
`CLAUDE.md`.

---

## 2. How the TUI is launched

`src/rework/cli/__init__.py::main` — when `sys.argv` has **no arguments at all**:

```python
if len(sys.argv) == 1:
    from rework.tui import run_tui

    run_tui()
```

`rework.tui.run_tui()` constructs the app and runs it. Everything else
(`rework --help`, `rework new …`) uses the Typer CLI and must keep working.

The TUI is a **client**, not the daemon. It opens its own store connection from
`REWORK_DB` and reads/writes rows. Missions keep running whether or not the TUI is
open. Do not move engine logic into the TUI.

**Requirement from the owner:** bare `rework` must open to an empty/blank state —
do not auto-open, auto-focus or auto-resume any mission.

---

## 3. The backend you talk to

### 3.1 `TuiData` (`src/rework/tui/data.py`)

A single store connection opened lazily from `Config.from_env()`. All methods are
async. Reusable as-is:

```python
data.projects()                      -> list[Project]
data.missions(limit=60)              -> list[Mission]
data.get_mission(mission_id)         -> Mission | None
data.mission_runs(mission_id)        -> list[Run]        # every attempt, oldest first
data.open_gates(mission_id=None)     -> list[Gate]       # None = across all missions
data.events_tail(mission_id, since_seq, limit=500) -> list[EventRecord]
data.mission_spend(mission_id)       -> float (USD)
data.spend_by_model(mission_id=None) -> list[dict]  # model, calls, input_tokens,
                                                    # output_tokens, cost_usd
data.decide_gate(gate_id, approved)  -> bool
data.create_mission(**kwargs)        -> Mission     # objective, repo, repo_dir,
                                                    # project_id, plan, model, effort
data.create_project(name, transport) -> Project
data.fetch_result(mission_id)        -> (dest_path, file_count)   # network, may be slow
data.mission_result_path(mission_id) -> str | None
data.library_search(query)           -> list[LibraryItem]
data.library_hybrid_search(query)    -> list[LibraryItem]   # falls back to keyword
data.library_body(item_id)           -> (item, body_text | None)
data.library_set_status(item_id, retire) -> bool
data.store()                         -> StateStore   # raw access when you need it
```

`TuiData` wraps reads in `_resilient()`: on a connection-class error it drops the
connection, reopens and retries once, then falls back to the last good result
while setting `data._reconnecting_hint`. **Keep this behaviour or re-implement
it.** That contract is not optional — see §7 trap #1.

### 3.2 Raw `StateStore` (`src/rework/kernel/state/store.py`)

Same seam for both backends. Useful beyond the helpers above:

```python
create_mission / get_mission / update_mission / list_missions
create_project / get_project / update_project / list_projects
create_run / get_run / update_run / list_runs / list_runs_by_status
create_gate / get_gate / decide_gate / list_open_gates / list_gates
append_event / events_since
charge / mission_spend / spend_by_model / run_charges
list_complaints / update_complaint
```

### 3.3 Record shapes (`src/rework/kernel/state/records.py`)

```python
Mission: id, objective, repo_path, repo_origin, project_id, status, plan, result,
         created_at, updated_at
MissionStatus: pending | running | awaiting_gate | on_hold | done | failed | cancelled

Run: id ("<mission>:<step>:<role>:<attempt>"), mission_id, role, step, attempt,
     status, model_ref {id, effort}, context, artifact, escalation, redirect
RunStatus: intent | running | halted_escalated | completed | failed | aborted

Gate: id, mission_id, kind, question, status, decision_note, for_run, created_at,
      decided_at
GateKind: ship | open_pr | spawn | generic
GateStatus: open | approved | rejected

EventRecord: mission_id, run_id, type, payload (dict), ts, seq
EventType: mission_created | plan_created | role_spawned | progress | escalated |
           role_halted | role_completed | model_promoted | gate_opened |
           gate_decided | mission_done | mission_failed | cancelled | info

Project: id, name, settings {repos: [{name, kind, url}], transport, results}
Charge: mission_id, run_id, model, input_tokens, output_tokens, cached_tokens,
        cost_usd, ts
```

### 3.4 Trajectory events — the agent-loop view

The tool-loop nodes publish `EventType.PROGRESS` events whose payload has a
`trajectory` key. Three kinds:

```python
{
    "trajectory": "turn",
    "turn": 3,
    "text": "…≤400 chars…",
    "tools": ["read_file", "run_command"],
    "tokens": 3800,
}

{"trajectory": "tool_call", "name": "read_file", "arguments": "path=src/app.py"}

{
    "trajectory": "tool_result",
    "name": "read_file",
    "result": "…≤400 chars…",
    "error": "…≤200 chars, empty when not an error…",
}
```

These are already truncated server-side; do not expect full payloads. They are
what makes a running mission legible (turn N, which tools, what came back)
instead of a bare "running". `events_tail(mission_id, since_seq)` is the read
path — pass the highest `seq` you have seen to get only new rows.

### 3.5 Artifacts you can display

A completed `Run.artifact` is a plain dict whose keys depend on the role:

- **investigator**: `summary`, `findings` [ {title, detail, evidence[], severity,
  verdict, confidence} ], `blockers`, `suggested_fix`, `tests`
- **coder**: `kind="coder_handoff"`, `branch`, `commit`, `base_sha`, `summary`,
  `worktree`, `diff` (string, up to 100 KB)
- **auditor**: `findings`, `verdict`, plus a run-stats block
- most roles get `run_stats` attached (`rework.nodes.run_stats.attach_run_stats`)

`rework.cli._render_md(runs, spend, mission)` renders a full markdown report from
`[(run, artifact), …]` — reusable if you want the report view for free.

### 3.5b Chats are persisted (you can list and reopen them)

`chats` + `chat_messages` tables exist in both backends. A conversation survives
closing the app, and the Work surface can offer a list of them.

```python
data.chats(limit=50, include_missions=True)  -> list[Chat]
data.new_chat(title=..., project_id=..., model_id=..., effort=...) -> Chat
data.chat(chat_id)                            -> Chat | None
data.rename_chat(chat_id, title)              -> Chat
data.attach_chat_to_mission(chat_id, mission_id)
data.delete_chat(chat_id)                     # cascades to messages
```

`Chat` = id, title, project_id, model_id, effort, mission_id, timestamps.
`mission_id` is set once a proposal from that chat launched, so
`include_missions=False` hides finished conversations.

To restore a conversation, construct `OrchestratorChat(..., store=store,
chat_id=<id>)` and `await chat.load()` — that replays the stored history into
the model AND returns `[(who, text)]` for rendering, where `who` is `"you"` or
`"orchestrator"`.

### 3.6 Mission creation is a CONVERSATION (**architectural requirement**)

Owner's principle, verbatim: *"rework works by u talking to an orchestrator and
the orchestrator decides"*. There must be **no mission form** with plan/repo/
project dropdowns. The conversation layer is `src/rework/orchestrator/chat.py`:

```python
from rework.orchestrator.chat import OrchestratorChat, Proposal
from rework.config import load_catalog
from rework.kernel.llm.adapters import build_provider

catalog = load_catalog()
entry = catalog.resolve(model_id)
chat = OrchestratorChat(
    build_provider(entry), catalog, model_id, effort=None, context=my_context_note
)
result = await chat.send("fix the flaky test in toyrepo", images=["/path/shot.png"])
# ChatResult: reply, proposal, asked, turn_tokens, turn_cost_usd, transcript[Turn]
```

Key semantics you must preserve:

- The model speaks through **tools** (`propose_mission`, `ask`), so proposals are
  structured, never parsed from prose.
- `result.proposal` is a **Proposal**, NOT a mission. Nothing is created until the
  owner confirms. `chat.launched` flips only on a yes-word (`y`, `yes`, `go`,
  `launch`, `do it`, …); no-words scrap it.
- A `Proposal` carries `objective, plan, project, repo, model, effort`.
- `chat.supports_images` is False for catalog entries without `"vision"` in
  `capabilities` — refuse attachments rather than silently dropping them.
- The orchestrator has **read-only tools** when a store is supplied:
  `list_projects`, `list_missions`, `get_mission`, `library_search`. It runs them
  in a bounded loop before proposing (max 4 rounds). `on_turn(kind, payload)`
  streams `turn` / `tool_call` / `tool_result` live — render that to give the
  Work surface a real agent trajectory (same vocabulary as §3.4). It cannot
  change anything from the conversation; only a confirmed proposal creates rows.
- `context` is a string of facts you feed in (project names, their repo refs, the
  plan menu) so the orchestrator proposes instead of interrogating.

Creating the mission afterwards:

```python
await data.create_mission(
    objective=proposal.objective,
    plan=proposal.plan or "fix",  # echo | fix | fix-pr
    project_id=resolved_project_id,  # resolve the NAME the chat used
    repo=proposal.repo or None,
    model=...,
    effort=...,
)
```

`plan="fix"` / `"fix-pr"` raise `ValueError` when there is no repo — that is the
one case worth asking the user a follow-up instead of showing an error.
`plan="echo"` takes no repo.

### 3.6b Gate answers can carry structured feedback

A gate decision is not only yes/no. `Gate.decision` is an arbitrary dict handed
to the paused role as its `interrupt()` result:

```python
data.decide_gate(gate_id, approved=False, note="needs work",
                 decision={"feedback": "don't touch the parser"})   -> bool
```

The engine resumes the role with `{**decision, "approved": bool}` when a typed
answer exists, else the plain bool (roles written before this keep working). A
REJECTION with feedback also resumes the role rather than aborting the run, so
the agent can act on what you said. CLI: `rework approve <gate> --answer "text"`
or `--answer-json '{...}'`.

### 3.6c Cancel and pause

```python
data.cancel_mission(mission_id)  # stops it; stays cancelled
data.pause_mission(mission_id)  # stops spending, keeps everything resumable
data.resume_mission(mission_id)  # continues from the checkpoints
data.set_mission_status(mission_id, "on_hold")  # generic
```

These write the mission row; the daemon reads it, so they work from any machine
and survive restarts. CLI: `rework cancel|pause|resume <mission>`.

### 3.7 Fetching a finished mission

`data.fetch_result(mission_id)` does an HTTP GET against the daemon's workspace
API (`REWORK_DAEMON_URL`, default `http://127.0.0.1:8941`), extracts the tarball
into `REWORK_RESULTS_DIR` (default `~/rework/results/<mission-id>/`) and returns
`(path, file_count)`. It raises `FileNotFoundError` when there is nothing to
fetch. It is network + disk bound: run it off the UI loop.

Artifacts directory policy (owner decision): **deliverables are visible** —
`~/rework/results/`, `~/rework/reports/`. `~/.rework/` stays hidden and holds only
machinery. Do not put user-facing output in a dot-directory.

---

## 4. Models, effort and the catalog

`rework.config.load_catalog()` reads `REWORK_MODELS` (a JSON path or inline JSON)
falling back to `~/.rework/models.json`, then to a built-in fake demo catalog.
Each entry (`ModelEntry`) has `id` (catalog key), `provider`, `model` (wire id),
`tier`, `cost_in_per_mtok`, `cost_out_per_mtok`, `capabilities` (a set; `"vision"`
and `"thinking"` are used), `context_window`, `max_output`, `base_url`,
`api_key_env`.

Effort is `None | "low" | "medium" | "high"` and is passed through to
`ChatRequest.effort` / the mission plan (`plan["effort"]`). Adapters translate it
(Anthropic thinking budget, OpenAI `reasoning_effort`) or ignore it.

To send images: put **file paths** on the user message dict
(`{"role": "user", "content": "…", "images": ["/abs/path.png"]}`). Adapters read
and encode them at request time. Never inline base64 into message dicts — they are
checkpointed and replayed.

---

## 5. Environment variables

| var | effect |
|---|---|
| `REWORK_DB` | sqlite path **or** `postgresql://…`. Everything follows from it. |
| `REWORK_CHECKPOINTS_DB` | sqlite checkpoints path (Postgres shares the main DB) |
| `REWORK_MODELS` | catalog JSON path or inline JSON |
| `REWORK_LIBRARY_DIR` | library git root (default `~/.rework/library`) |
| `REWORK_RESULTS_DIR` / `REWORK_REPORTS_DIR` | visible artifact dirs (default `~/rework/…`) |
| `REWORK_DAEMON_URL` / `REWORK_HTTP_PORT` | workspace API for fetching results |
| `REWORK_TOKEN` | bearer token for that API when the deployment sets one |
| `REWORK_CONCURRENCY` | daemon-side; the TUI does not manage it |
| `REWORK_GIT_AUTHOR_NAME` / `_EMAIL` | agent commit identity |
| `REWORK_NOTIFY` | the previous TUI's notify mode; free to keep or drop |

---

## 6. Testing your build

`tests/tui/test_tui.py` shows the pattern: a tmp SQLite file set in `REWORK_DB`
**before** the app is constructed, seeded through a second connection, then the
real app driven with Textual's `Pilot`:

```python
async with app.run_test(size=(120, 32)) as pilot:
    await pilot.pause()
    await pilot.click("#some-button")
    await pilot.press("enter")
```

Rules that keep these tests honest:

- Assert against the **store** (reopen the sqlite file) for anything that writes
  (deciding a gate, creating a mission), not against widget state.
- Never point a test at the deployment database. Use a tmp file, or
  `REWORK_TEST_POSTGRES` pointing at a scratch Postgres.
- If you stub out collaboration, check the stub is honest: a fake that shares the
  production assumption proves nothing about either.

Commands:

```bash
uv run pytest tests/tui -q          # your tests
uv run pytest -q                     # whole suite
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

CI runs the suite on 3.12 and 3.13 with a Postgres service; a red CI blocks the
project, so keep the suite green.

---

## 7. Traps that already bit the previous implementation

These are bugs that shipped and were found in production. They are listed because
they are easy to reintroduce in a rewrite.

1. **A dropped connection must never exit the app.** The UI reads Postgres over a
   tailnet from a laptop. `consuming input failed: Operation timed out` /
   `server closed the connection` are routine. Textual's default for an unhandled
   exception is to paint a full-screen traceback and quit — that happened to the
   owner. Handle it at three levels: retry/fallback in the data layer, try/except
   around every poll, and an app-level handler that turns surprises into a toast.
2. **A poll that raises kills the session.** Timer callbacks propagate. Wrap them.
3. **Bindings declared on the App resolve against the App.** A key bound at app
   level calls a method on the App even when a pane defines the same name; the action
   silently does nothing if only the pane has it. Verify a key by pressing it in a
   test.
4. **A focused text widget swallows most printable keys**, so single-letter
   shortcuts never fire when a text box has focus. Prefer modifier keys
   (`alt+…`, `ctrl+…`) or explicit buttons for anything important.
5. **In Textual's `DataTable`, one click only moves the cursor.** `RowSelected`
   fires when you click the row that is already selected, so "click to open"
   needs a second click on the same row. Do not assume one click selects and acts.
6. **`TextArea` handles Enter itself** (inserts a newline and stops the event), so
   `Binding("enter", …)` never runs on it; override the key handler instead.
   Messages posted from inside key dispatch can be dropped — defer with
   `call_later`.
7. **`@on(Input.Submitted)` silently filters by type**; a custom message class
   from a subclass never matches. Filter by the actual message type or drop the
   decorator.
8. **Long pastes.** A single-line `Input` silently discards pastes past its size
   limit — it reads as the app losing work. Use a widget that accepts them.
9. **`interrupt()` replays its node on resume** (architecture invariant, ADR 0002).
   If you add anything that pauses a role, interrupt must be the first side effect.
10. **Do not let the UI decide things the daemon owns.** Gate decisions, mission
    creation and fetching all go through the store/API. The UI is a view plus a
    thin command surface.

---

## 8. What the UI must let a person do

Functional capabilities that exist in the backend today. How you present them is
yours to decide; this list is the scope.

**Missions**
- list missions (running and awaiting-gate ones matter most), filter by project
- cancel / pause / resume a mission (§3.6c)
- see per-mission: objective, status, plan kind, age, current cost, which role is
  active, and the live trajectory (turns / tool calls / results)
- open a mission to inspect: findings, the coder's diff, the rendered report,
  per-role run stats and timings
- create a mission by talking to the orchestrator (see §3.6)
- fetch a finished mission's worktree to `~/rework/results/<id>/`

**Gates** (blocking; the most time-sensitive surface)
- see every open gate across missions, and per mission
- read the question, the kind, which run it unblocks
- approve / reject; the mission resumes from the store row, from any machine
- optionally send a typed answer (free text feedback or structured data) that
  the paused role receives — see §3.6b
- know when one appears without polling with your eyes; how you notify is your
  call, but the owner asked for it to be **configurable** (the previous build used
  `REWORK_NOTIFY=toast|bell|both|none`)
- a gate that cannot be answered is the single worst failure mode this UI has had

**Cost**
- spend across all missions and per model; per-mission spend
- token counts (input / output); call counts
- **history over time**: `data.spend_history(bucket="day"|"hour"|"week"|"total",
  since=, until=, mission_id=, group_by="none"|"project"|"role"|"model")`
  returns rows of `bucket_start, bucket_label, group_key, calls, input_tokens,
  output_tokens, cost_usd`. Labels are UTC and identical on both backends.
  A charge with no run groups under `""` ("unattributed") — show it rather than
  dropping it, or your totals will not reconcile.
- the source is the `charges` table — exact accounting

**Library**
- search (keyword or hybrid), read an item's body, activate/retire an item
- items have type (skill/tool/report/memory), status (proposed/active/retired) and
  provenance (mission, role, project, source URL)

**Projects**
- list, create, see which repo refs a project defines
- missions can be project-less

**General**
- mouse and keyboard both; the owner explicitly wants clicks to work, not only keys
- copy text out of the UI
- survive terminal resizes and a dead database connection
- quit cleanly (note: `ctrl+q` is swallowed by macOS terminal flow control and
  `ctrl+c` is conventionally copy — pick exits that actually work)

---

## 9. Working agreements in this repo

From `CLAUDE.md`:

- **Always be committing.** Commit green work as you go. Pushing/PRs need asking.
- **Docs follow code.** Update `docs/design.md` / README / an ADR in the same
  commit as the change.
- **Division of verification.** You verify objective behaviour yourself — tests,
  commands, live runs, against Postgres too. The owner judges subjective feel.
  Never ask the owner to run the test suite.
- Tests must never touch the deployment database `rework_state`; use a tmp SQLite
  file or a dedicated `rework_test` Postgres.
- Do not commit unless the gate passes: `uv run pytest`, `ruff check`,
  `ruff format --check`, `uv run mypy src`.
