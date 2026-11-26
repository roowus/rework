# ADR 0021: The daemon is the control plane; front ends send commands

## Status

Accepted (2026-11-26). Phase 1 (command API) landed; phase 2 moves the TUI and CLI onto
it; phase 3 adds the read API and event stream.

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
