# ADR 0023: One pause primitive

## Status

Accepted (2026-11-26). Supersedes the interrupt() half of ADR 0002.

## Context

rework had two ways for a role to pause. Real roles asked on their run row
(`ask_orchestrator`, ADR 0011): a blocking tool call, so the agent loop's state stays in
memory and nothing replays. The other was LangGraph `interrupt()`: the run went
`HALTED_ESCALATED`, the engine materialized a gate from the interrupt payload and resumed
the graph with `Command(resume=decision)`. Only the `echo` stub used it, yet ~400 lines of
engine code, a recovery branch and several test suites existed for it, CLAUDE.md rule 1
guarded it, and it hid a real bug: `needs_model` promotion only watched halted runs, so
it never fired for real roles (fixed by ADR 0019).

## Decision

- Every pause is an ask on the run row. `ask_owner_gate(ctx, question)` is the owner-gate
  form (a run-scoped gate, found again by `for_run` after a restart; returns the typed
  answer or the approved bool).
- The DRIVER answers asks: the orchestrator on an orchestrated mission; the template
  driver applies the needs_model policy and otherwise opens an owner gate
  (`_ask_owner_for`) and writes the decision onto the row.
- Deleted: `_handle_halted`, `resume_with_decision`, `_execute_resume`,
  `_promote_and_resume`, the halted recovery branch, `_resume_value`, the gate helpers
  that served them, the orchestrator's halted handling, and the template-mission
  `_ask_owner` in the escalate node. Recovery is "restart the run from its checkpoint".
- A graph that still calls `interrupt()` fails its run with a clear error.
- `HALTED_ESCALATED` / `ROLE_HALTED` stay in the enums only so old rows still load.

## Consequences

- A rejected gate is an answer the role receives (`approved: False`), not an abort: the
  role decides what a "no" means. Tests that asserted `aborted` now assert that.
- The chaos suite runs in ~9 s instead of minutes (no halt/resume dance).
