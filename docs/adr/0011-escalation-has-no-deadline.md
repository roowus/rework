# ADR 0011: A blocked role parks until a human answers (no deadline, no slots held)

## Status

Accepted (2026-11-23).

## Context

A role that cannot proceed calls `ask_orchestrator`, which wrote the question onto
`Run.escalation` and polled for an answer on the same row. The poll had a deadline:

```python
ANSWER_TIMEOUT_S = 45 * 60
...
return "no answer within the time limit; proceed with your best judgment and say so"
```

So the safety property was really "blocked until somebody looks, or until 45 minutes pass",
and a daemon restart, a long orchestrator turn, or a quiet night all pushed toward the
second branch. A blocked role that guesses is worse than one that waits: the guess is
recorded as work, and the mission's report then claims a decision nobody made.

Two capacity leaks sat under that deadline, both invisible while it was short:

1. The role held its `RoleLimiter` slot for the entire poll. The slot is released in
   `_execute`'s `finally` (`engine.py`), which runs when the *task* ends, so a parked role
   kept consuming `REWORK_MAX_ROLES` for 45 minutes.
2. `_handle_halted`'s gate wait loop (`engine.py`) slept bare and never entered
   `self._waiting()`, so the mission kept its `REWORK_CONCURRENCY` slot while parked. Every
   other gate wait already gives it back (`_gate_decision`, `_pause_until_resumed`,
   `_burn_gate_hit`, and the orchestrator's `ask_owner`).

Removing the deadline without fixing those two would let a handful of parked missions
consume the whole daemon.

## Decision

**The wait has no deadline.** The only exits from `ask_orchestrator` are an answer landing
on the row, the run being aborted or failed, or the mission being cancelled. There is no
string that tells a role to guess, and `ANSWER_TIMEOUT_S` is deleted rather than raised.

**A parked run gives both slots back.** `RoleBuildCtx` gains a `park` callable, built by the
engine per run, which releases the role slot and then the mission slot and re-acquires them
in reverse order before the role resumes. `RoleLimiter.release` becomes idempotent per run
(tracking a held set rather than decrementing blind), so a park followed by `_execute`'s
`finally` cannot free a second run's slot. `RoleBuildCtx.parked()` is always usable: with no
limiter (tests, evals) it is a no-op context.

**The ask id is derived, not random.** `ask-<sha256(run_id, question)[:12]>` means a restart
that replays the tool call finds the question it already wrote, and an answer written while
the daemon was down, instead of writing a fresh id that orphans that answer. The ESCALATED
event is published only when the question is actually written.

**The escalation kind survives.** `blocked`, `needs_context` and `needs_tool` are questions
the owner must answer, so they now map to `GateKind.QUESTION` instead of collapsing to
`GENERIC`, and the raw kind is kept on `Gate.spec["escalation_kind"]` for the UI.

### Why not an interrupt

ADR 0002 rejected making the role loop an `interrupt()` because a role's whole agent loop is
ONE graph node: an interrupt mid-loop replays every earlier turn on resume. The blocking
tool call keeps the loop's state in memory. Releasing the slots around that call gets the
capacity benefit without the replay cost, and the store row plus the derived ask id make it
restart-safe.

## Alternatives considered

- **Raise the deadline (say 24 hours).** Still a guess, just later, and it keeps both leaks
  in a slightly smaller form. Rejected: the property wanted is "never guess".
- **Fail the run on timeout.** Surfaces the dead end, but throws away a live role loop over
  an unattended night, and a re-run costs the whole loop's spend again.
- **A real `interrupt()` plus a gate.** ADR 0002's replay cost, now with a longer park.
- **Keep the role slot and rely on the mission slot only.** The role slot is the tighter
  cap in practice (`REWORK_MAX_ROLES` is small on a 2-CPU host), so this would still strand
  the daemon.

## Consequences

- A parked mission is capacity-free: the daemon keeps picking up other work while it waits.
- The owner-facing signal is unchanged and now more accurate: the mission sits in
  `AWAITING_GATE` with an open gate, the TUI files it under "needs you", and
  `notify_owner` already fired when the gate opened.
- A truly abandoned mission waits forever instead of guessing. It is visible in the gates
  list and the dashboard, which is the intended failure mode: silence is legible.
- The resumed run does not jump the queue; it waits for a free slot like any other run. A
  park hands capacity back, it does not reserve it.
- `REWORK_NOTIFY_CMD` re-notification for gates open beyond 30 minutes is deliberately NOT
  in this ADR. It is a useful follow-up, but it is a notification policy rather than part of
  "never guess", and the gate-open notification already exists.
