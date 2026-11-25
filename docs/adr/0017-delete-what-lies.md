# ADR 0017: Delete the declarative config nothing reads

## Status

Accepted (2026-11-24).

## Context

`RoleSpec` carried two declarative fields that no code ever read:

- `ToolPack.allowed` / `RoleSpec.tools`, described in its own docstring as "executed by the
  ToolExecutor the engine hands the graph". Nothing passed the set to `specs()`, so a tool
  named there would silently never appear. `web/tools.py` already said so in a comment.
- `EscalationPolicy.kinds` and `max_escalations`, described as a per-role escalation
  policy. Nothing read either; the engine handled escalation kinds directly.

The real mechanisms were already compositional: the executor WRAPPERS (`escalate.py`,
`readonly.py`, `library/tools.py`, `web/tools.py`) decide exposure by being wrapped around
the inner executor, and an escalation's kind travels on the run row. So the declarative
fields were a second, inert mechanism for the same job, and a new role author would
reasonably believe they worked.

Two pieces of dead code sat beside them: `git_ops.worktree_executor` (no caller, and it
hard-coded `LocalExecutor`) and `LocalExecutor.extra_specs` (no caller).

## Decision

Delete them. `ToolPack`, `EscalationPolicy`, `RoleSpec.tools`, `RoleSpec.escalation`,
`worktree_executor` and `extra_specs` are gone. `web/tools.py`'s comment now states the
positive rule instead of warning about a dead field.

## Alternatives considered

- **Implement them.** Waiving one of the wrappers and wiring the set through
  `toolloop.py`'s `executor.specs()` would make them work, but it creates two ways to
  decide a role's tools, which is exactly how the docstring drifted from the truth in the
  first place. One mechanism, the one that is already load-bearing, is better.
- **Leave them with a deprecation warning.** They have no callers to deprecate for.

## Consequences

- A role's tool surface is exactly its wrapper chain, and that chain is visible in its
  `build_graph`.
- Adding a tool is one place (a wrapper), not two.
- The escalation kind is read from the run row, which is already the source of truth.
