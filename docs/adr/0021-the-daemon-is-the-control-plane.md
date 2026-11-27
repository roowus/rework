# ADR 0021: The daemon is the control plane; front ends send commands

## Status

Accepted (2026-11-26). Phases 1 (command API), 2a (owner actions), 2b (chat turns) and 3a
(live event stream), 3b (the heartbeat as one `/v1/snapshot` read) and 3c (detail reads over
`/v1/read`) and 3d (every owner write as a command) landed: with a daemon configured a TUI
opens neither the database nor the Library index. The one local exception is `repo_dir`
(uploading a folder from the client's disk), which starts on the client by nature.

## Context

The owner wants several TUIs, on several machines, connected to one rework host. Today
every front end writes the database itself and the TUI also runs engine work in-process
(chat orchestrator turns, `retry-pr` pushes, AI search). Two front ends acting at once
raced on the same rows, and work ran on whichever machine the TUI happened to be on
(`retry_pr` against a worktree path that only exists on the daemon host).

Remote workers (ADR 0010) stay as executors under the single host: one control plane,
many workers, many viewers.

## Decision

- The daemon's existing aiohttp server (the workspace API, bearer `REWORK_TOKEN`) gains
  versioned command routes under `/v1`: mission pause/resume/cancel/prioritize/message/
  retry-pr and gate decide. Each route is a thin shell over code that already owns the
  rule (`services.missions`, `GateManager`, `delivery.retry_pr`), so nothing is
  re-implemented.
- Refusals are typed JSON (`409 illegal`, `404 not_found`, `400 bad_request`,
  `401 unauthorized`) carrying the daemon's own reason.
- A gate decision reports `decided: true` only to the call whose write landed (the
  store writes GATE_DECIDED only for that call), so two racing clients learn who won.
- `rework.api.client.DaemonClient` is the one client.

Phase 2: the TUI's `TuiData` and the CLI's owner verbs call the client when a daemon URL
is configured; in-process engine work moves into the daemon behind new routes (chat
turns with a per-chat lease, AI search). Phase 3: `GET /v1/snapshot` and an SSE event
stream replace direct DB polling.

## Consequences

- Every owner action has one implementation and one executor.
- Clients need network reach to the daemon (they already did for uploads and fetch).

## Phase 2a (2026-11-26)

With `REWORK_DAEMON_URL` set (a `--target`, or the env file), the TUI's and the CLI's
owner actions (pause, resume, cancel, prioritize, gate decisions, retry-pr) are sent to
the daemon. `api.client.call` falls back to in-process when no URL is set, when the
daemon predates `/v1` (aiohttp's plain 404, mixed versions during a rollout) or when it
is unreachable; a refusal from the API itself is final and shown as is. Verified with
two TUI data layers in separate processes, each on an empty local database, racing one
gate on a real daemon: one decision, one `gate_decided`, each told whether it won.

## Phase 2b (2026-11-26): chat turns run on the daemon

`POST /v1/chats/{id}/messages` runs one owner turn (`services.chat.send`): one engine per
chat in the daemon, one turn at a time per chat (an asyncio lock: one daemon process serves
turns), the Library handed in. With a daemon configured, the TUI's engine gets no provider
of its own and only mirrors the pending proposals for Launch/Scrap; the daemon writes the
transcript and every TUI's poller paints it. No daemon, or one without the route, runs the
turn in the TUI as before. Verified live: two TUIs on ONE chat, the TUIs' model factory
booby-trapped, three alternating turns; each shows once in both and every model turn sees
the whole conversation.

## Phase 3a (2026-11-26): a live event stream

`GET /v1/events` is a server-sent-events stream of every event the store records, in seq
order (`?since=` resumes after a reconnect). It tails the events TABLE, so it carries writes
from every process, and the in-process bus only wakes it early. Every mission status move
now writes `STATUS_CHANGED` (from/to) in the same transaction, so a pause or cancel from
anywhere reaches the stream. The TUI follows the stream when a daemon is configured and
refreshes on an event (at most every 0.5 s), keeping its timer as the fallback. Verified
live: a TUI with its poll set to 30 s saw another TUI's pause in 0.35 s.

The TUI still reads its data from the store after a wake; moving those reads behind one
`/v1/snapshot` call is the step that lets a TUI run with no database access at all.


## Phase 3b (2026-11-26): the heartbeat is one read

`GET /v1/snapshot` returns everything the TUI's heartbeat shows: light missions, open
gates, projects, the listed chats, spend since the client's `?spend_since=` (its local
midnight: the daemon does not know the owner's timezone), budget raises after
`?events_after=` (the reply's `event_seq` is the next cursor; no cursor = start from now),
and the spinner's event stamps. It replaces 8 to 30 store round trips per poll, made from
the TUI over the tailnet, with one HTTP call answered next to the database.

The rules (which chats are listed, how stamps are read, where the cursor starts) live in
`services/snapshot.py`, used by both the daemon and the TUI's no-daemon path, and a test
asserts both paths produce the same snapshot. With a daemon configured, an unreachable
daemon shows OFFLINE with the last good data; the TUI never goes around it to the
database. Only a daemon that predates the route (plain 404) sends the hub back to the
store, once, remembered.

Verified live (`scripts/e2e`, `heartbeat_from_the_daemon`): TUI B's store-backed heartbeat
is booby-trapped; it still saw A's mission appear, park on its gate and the gate clear.
The run also counts B's remaining direct store reads: detail views, the next step.

## Phase 3c (2026-11-26): detail reads over the API

`POST /v1/read {"method", "args"}` runs one allowlisted `StateStore` READ on the daemon
(`api/remote_store.READS`: get/list/events/spend/chat-transcript methods, nothing that
writes). Arguments and the result are encoded by the method's own type hints, so the TUI's
`RemoteReadStore` returns the same typed records a local store does; a test calls every
allowlisted read both ways and asserts equality. A write method, an unknown method or a
bad argument is 400. `TuiData.reader()` is the daemon when one is configured, the store
otherwise; a daemon without the route (plain 404) is remembered and the store is used.

Trade-off, accepted on purpose: this is a generic read RPC, so the API surface is the
`StateStore` read signatures. That kept 20 reads from becoming 20 bespoke routes and makes
drift impossible (one definition), at the cost that renaming a read method is an API
change. The allowlist is the compatibility list: change it with a version bump.

Verified live: `heartbeat_from_the_daemon` now fails if TUI B opens the database at all
while it watches A's mission and visits Dashboard, Gates and Cost: 0 opens. Negative
control: with reads forced to the store the same run reports 8 opens.

## Phase 3d (2026-11-26): every owner write is a command

New routes: `POST /v1/chats`, `PATCH /v1/chats/{id}` (title, project, model, effort,
first mission; nothing else), `DELETE /v1/chats/{id}`, `POST /v1/chats/{id}/notes`,
`POST /v1/missions` (the same `missions.create_mission`, budget/plan/repo rules
included), `POST /v1/projects` (`services/projects.py`, shared with the no-daemon path),
and the Library: `GET/POST /v1/library/items`, `GET /v1/library/items/{id}`,
`POST /v1/library/items/{id}/status`. The dashboard's owner message now goes through
`/v1/missions/{id}/message` like the Work view's. A daemon with no Library answers 409
`unavailable`.

Decisions:
- A launched mission's local `repo` path resolves on the DAEMON's machine, where the
  mission runs. That is a behaviour change for a remote TUI (its local path meant nothing
  to the daemon anyway); a folder on the client travels by `repo_dir` upload, which stays
  a client-side call.
- The chat engine a daemon-mode TUI keeps (it mirrors pending proposals for Launch and
  Scrap) reads its history over `/v1/read` and gets no Library handle; if an older daemon
  forces a local turn, it is re-pointed at the real store first, because a local turn writes.
- The Library tab's "hybrid" label now comes from where the search ran (the daemon's
  embedder), not from the TUI machine's environment.
- A note written over the API returns its row id, and the writing TUI marks it as its own,
  as the tracked local store did, so the transcript poller does not paint it twice.

Verified: a unit-level test runs every TUI write with the database and the Library index
booby-trapped and checks each one on the daemon's store (negative control: removing one
route fails it). Every live e2e scenario now traps both TUIs' database and Library opens:
8 scenarios, 0 findings; with `new_chat` sabotaged the live run names it.
