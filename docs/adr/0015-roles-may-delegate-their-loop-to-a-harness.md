# ADR 0015: A role may delegate its loop to an external coding harness

## Status

Accepted (2026-11-24).

## Context

A role is three things: a worktree and branch, a LOOP that makes the change, and a
mechanical handoff that commits and packages it. rework's own loop (`nodes/toolloop.py`)
is small and understood, and it is the part the project controls. What it is not is a
tuned coding agent: Claude Code and Codex own a checkout, know how to edit files across a
large codebase, and ship their own sandboxing.

The handoff is what the rest of the system trusts: `kind: "coder_handoff"` with branch,
commit, diff, changed files and the tests block. The auditor's red-green check, the ship
gate and the PR body read that shape and nothing else. So the worker is swappable at the
loop and nowhere else.

## Decision

**A separate role, `harness`, reusing the coder's mechanical ends.** `coding.py` holds
`prepare_node` and `handoff_node`; both `coder` and `harness` build their graph from
them, so the artifact, the worktree, the branch name and the commit are identical BY
CONSTRUCTION rather than by two copies that happen to agree.

**Only the loop is replaced.** `harness` runs `claude -p ... --output-format stream-json
--permission-mode dontAsk` or `codex exec --json` as a subprocess in the worktree, under a
scrubbed environment (`role_env`, ADR 0009), with a fresh session directory per run and a
timeout.

**`dontAsk`, not a prompt.** An unattended harness that hits a permission it cannot get
must DENY and adapt, not wait for a human who is not there. That is the same rule as ADR
0011: never proceed on silence, never hang on it either.

**The stream becomes the trajectory.** Every JSON line from the harness is published as a
`PROGRESS` event, so `check_in` and the TUI show what the harness is doing. The final
assistant message becomes the coder's `final_text`, so the same tests block is parsed and
the same rail applies.

**The harness never decides a rail.** No merge, no PR, no gate. It edits files and leaves a
commit; the host commits (ADR 0014) and the rails judge.

## Why a separate role rather than a flag on `coder`

The orchestrator picks roles by name and description, and this one has different economics:
no daemon model call for the loop, its own token budget, its own session. Keeping it
separate makes that choice visible in the plan and in `spawn_role`, rather than hidden in
the shape of a brief. A mission can mix them: harness writes, rework's auditor reviews,
which is the pairing that matters, because the auditor and the red-green check are what
make any output trustworthy and neither cares who wrote the code.

## Alternatives considered

- **Replace rework's loop entirely with a harness.** Imports that harness's retry policy,
  compaction, tool schemas and error handling, and loses red-green and the typed handoff
  unless they are rebuilt. Rejected: the loop is the part the project controls.
- **A `coder` variant selected by brief.** Invisible in the plan, and it would branch the
  graph at build time on a string. Rejected.
- **Let the harness commit and push.** Natural for a harness, but it hands a worker the
  power to rewrite history and push. The host commits (ADR 0014).

## Amendment (2026-11-26): consumers key on the artifact kind

Four consumers still looked for `role == "coder"` after this ADR landed: the auditor's
auto-wiring, `_create_pr`, `retry_pr` and the TUI's diff view. An auditor spawned after
a harness run got no diff, worktree, tests or base sha. They now find the producer with
`rails.latest_handoff` / `rails.is_code_handoff`, which match `kind: "coder_handoff"`
whatever role left it. A new code-writing role that leaves the same handoff needs no
list edited anywhere.

## Consequences

- A mission can hand the hard change to a tuned agent and keep every rail.
- The harness must be installed on whichever host runs it; a missing binary fails the run
  with a clear message rather than silently falling back.
- The harness's own sandboxing is not rework's; running it under
  `REWORK_EXECUTOR=docker` puts the daemon's own executor in the sandbox, and the harness
  binary itself must be available there. That combination is a follow-up, not a default.
- A harness session is per run and disposable, exactly like the loop's checkpoint.
