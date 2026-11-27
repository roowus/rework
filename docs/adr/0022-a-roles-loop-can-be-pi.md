# ADR 0022: A role's agent loop can be pi

## Status

Accepted as an opt-in (2026-11-26). Default stays native until the eval harness says
otherwise.

## Context

A role is rigid nodes around ONE agent loop (prepare, loop, report/handoff). rework's value
is around the loop: the orchestrator, gates, rails, delivery, recovery, budgets, typed
artifacts. The loop itself is commodity, and tuned agents (pi, Claude Code) are better at it
than a hand-rolled loop will be. The owner asked whether to borrow one. ADR 0015 already
lets the `harness` role run Claude Code as a coder.

## Decision

- `nodes/pi_loop.py`: a loop node that runs `pi -p --mode json` in the run's workspace and
  returns the same `{messages, loop_turns, final_text}` as the native loop, so every node
  and rail downstream is unchanged.
- rework keeps the ledger (each assistant message charged as it streams, priced by
  REWORK's catalog, because pi's price list returns $0 for models it does not know), the
  budget stop (process killed past the cap), the trajectory (turns and tool calls as
  PROGRESS events) and containment (role env allowlist, an isolated pi agent dir with the
  owner's models/auth but no extensions, read-only roles get `--tools read` only).
- Selection: the run context's `loop` (`native` | `pi`), else `REWORK_ROLE_LOOP`, else
  native. The investigator is wired first (read-only, lowest risk).
- The model mapping is explicit: `REWORK_PI_PROVIDER` (default `9router`) plus the catalog
  entry's provider-side model id.

## Measured (2026-11-26, toyrepo, glm-5.3-flash via 9router, same brief, 12-turn cap)

| loop | result | wall | spend | tokens in/out |
|---|---|---|---|---|
| native | correct root cause, file:line evidence, ran the code | 73 s | $0.0123 | 18.3k / 7.9k |
| pi | correct root cause, file:line evidence, read-only | 80 s | $0.0112 | 16.0k / 7.3k |

Equivalent on this task. One task is not an eval; the next step is the eval harness on
real repos before any default changes.

## Amendment (2026-11-26): ask_orchestrator and the Library inside pi

pi has no MCP flag, but a LOCAL extension in an isolated agent dir works under `-p` (the
hangs came from the owner's npm extensions). rework bundles one, `nodes/pi_extension`, as
the only extension a role's pi loads. It registers `ask_orchestrator` and `library_search`,
which call the daemon's `POST /v1/runs/{run}/ask` (the same run-row ask the native loop
uses, long-polled until answered) and `GET /v1/library/search`. Verified live: a pi-run
investigator on a real daemon asked "Which file should I start with?", the orchestrator
answered with `answer_escalation`, pi received "Start with calc.py." and continued.

## Not yet

- Mid-run redirect and `needs_model` promotion apply to the native loop only.
- Writing roles (coder) on pi, and Claude Code as the loop for non-coding roles.
