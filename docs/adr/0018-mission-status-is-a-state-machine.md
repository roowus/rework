# ADR 0018: Mission status is a state machine; every change is compare-and-set

## Status

Accepted (2026-11-26).

## Context

`StateStore.update_mission(status=...)` wrote whatever it was given. Every front end and
the engine used it directly:

- `rework resume` on a DONE mission set it to `running`. Reproduced on a temp database: the
  daemon picked it up, re-ran its final gates and emitted a second `mission_done`. On a
  `fix-pr` mission that is a second pass through delivery.
- The TUI's Cancel acts on a snapshot up to 10 s old, so it could flip a mission that had
  just finished DONE (PR open) to CANCELLED.
- The engine wrote RUNNING after every gate wait (`_gate_decision`, `_handle_halted`, the
  burn gate, the orchestrator's ask/budget/turn-cap gates). An owner pause or cancel that
  landed during the wait was overwritten.
- `resume` always wrote `running`, so a mission paused while still `pending` claimed to run
  with no runner and no slot behind it.
- The CLI and the TUI each implemented pause/resume/cancel separately.

## Decision

1. `kernel/state/transitions.py` holds the one table of legal moves. DONE, FAILED and
   CANCELLED are final.
2. `StateStore.transition_mission(id, to, from_=..., result=...)` is a single
   `UPDATE ... WHERE id = ? AND status IN (...) RETURNING *`. It returns None when the row
   is in another status; `from_` defaults to every status the table allows to reach `to`.
   Both backends implement it and the conformance suite pins it.
3. Every status write in the engine, the orchestrator and the daemon goes through it with
   the narrowest `from_` that describes what the writer believes (e.g. the post-gate return
   is `AWAITING_GATE -> RUNNING`, so a concurrent cancel wins).
4. `_synthesize` writes the terminal status by compare-and-set from any unfinished status.
   A mission already DONE is left untouched (no second `mission_done`); a mission the
   owner cancelled first stays cancelled and still receives the artifacts.
5. `rework.services.missions` implements pause/resume/cancel once; the CLI and the TUI
   call it. Pause records `held_from` in the result and resume restores it. Verbs on a
   finished mission raise `IllegalTransition` ("cannot resume mission m-...: it is done").

`update_mission(status=...)` still exists for plan/result/repo writes and for tests that
seed rows; new status writes in product code use `transition_mission`.

## Consequences

- A status refusal is now visible to the owner instead of silently rewriting history.
- The gate-row/status pair is still two statements (the gate row is written first). Making
  that pair one transaction is the next step (atomic compound writes).
- Resume no longer "un-cancels" a mission. Re-running finished work means a new mission.
