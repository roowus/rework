# ADR 0021: The daemon is the control plane; front ends send commands

## Status

Accepted (2026-11-26). Phase 1 (command API), phase 2a (TUI and CLI owner actions) and
phase 2b (chat turns) landed; phase 3 adds the read API and event stream.

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

