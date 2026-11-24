# ADR 0012: One implementation of each rail, and the template runner is a fixture

## Status

Accepted (2026-11-23).

## Context

rework has two drivers:

- the **LLM orchestrator** (ADR 0007), the product path: an agent chooses roles,
  reads typed handoffs and calls `finish`;
- the **template runner**, a mechanical loop over a canned step list, used by the eval
  harness (which needs the same plan every run) and by `rework new --mechanical`.

Each driver owned some of the finish rails and not others:

| rail | template runner | orchestrator |
|---|---|---|
| one in-mission retry after a transient failure | yes | no |
| audit "request changes" sends the coder back, `max_rounds` | yes | no |
| a fresh audit after the last coder write | **no** | yes |
| coder handoff must carry its tests block | **no** | yes |
| open medium+ findings must be accepted with a reason | **no** | yes |
| a finish summary must say what changed | **no** | yes |
| refuse a model that keeps failing on provider errors | **no** | yes |

The asymmetry was invisible because the two paths look equivalent and are used for
different things. The consequence is concrete: a `--mechanical` fix-pr mission could open
a PR without ever passing "was this audited after the last change", which an orchestrator
mission cannot skip.

## Decision

**The finish rails are one module.** `orchestrator/rails.py` holds `audit_problem`,
`findings_problem`, `summary_problem`, `covers` and `accepted_findings` as pure functions
over store rows. The orchestrator calls them from `finish`; the template runner calls
`audit_problem` immediately before `_final_gates`, and fails the mission (rather than
refusing a tool call, because there is no model to explain it to) when it fires.

**The template runner is frozen, not maintained.** `_run_steps`/`_run_steps_once` are
renamed `_run_template`/`_run_template_once`. A mission with no `plan["orchestrator"]` and
no plan steps at all is a caller bug and now raises instead of silently falling through to
the echo default. `create_mission` stamps `kind` on every template plan (including `echo`),
and `TEMPLATE_KINDS` names them.

**No new rails on the template path.** Bug fixes only. New behaviour goes in the
orchestrator or in a shared rail, never in a second implementation of one.

## Why not delete the template runner

`evals/harness.py` builds missions with a fixed plan and no orchestrator, and a grader
needs the plan not to vary run to run. Keeping a deterministic path for tests and for the
`--mechanical` escape hatch is legitimate. Keeping two live product paths is not, so the
template runner stops being one.

## Consequences

- A `--mechanical` mission can no longer ship unaudited; it fails before its ship gate.
- The rails table in `tests/unit/test_rail_conformance.py` runs both drivers over the same
  scenarios, and `test_the_template_runner_calls_the_shared_audit_rail` pins the call site,
  because a behavioural test cannot see a driver that simply forgot to call the rail.
- The mechanical-only rails (the transient retry, the audit-revise loop) stay in the
  template runner. They describe how a canned plan proceeds, not what may ship, so they
  are not finish rails and the orchestrator needs no equivalent (it decides).
- Adding a fifth rail means adding one function and one row to the table.
