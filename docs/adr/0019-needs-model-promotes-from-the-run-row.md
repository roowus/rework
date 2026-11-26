# ADR 0019: needs_model promotes from the run row

## Status

Accepted (2026-11-26).

## Context

Real roles pause with `ask_orchestrator`, a blocking tool that writes the question onto
`Run.escalation` and polls for an answer (ADR 0011; `nodes/escalate.py`). Only the `echo`
stub uses LangGraph `interrupt()`. The engine's needs_model policy
(`_promote_and_resume`) lived on the interrupt path: it looked at `HALTED_ESCALATED`
runs only. So `ask_orchestrator(kind="needs_model")` from a coder, investigator or
auditor reached the orchestrator as a plain question, or, with nobody answering, the role
sat `running` forever. Verified on main with a scripted mission.

The tool loop also resolved its model once, at graph build, so even a promotion written
onto the row would not have reached the running loop.

## Decision

- The orchestrator's `wait` hands every open `needs_model` ask to
  `MissionRunner.promote_asking_run`: it resolves the escalation model
  (`escalation_entry`: REWORK_ESCALATION_MODEL, else the strongest usable entry, else the
  next tier), writes it onto `run.model_ref`, answers the ask and publishes
  `MODEL_PROMOTED`. With nothing to promote to, it marks the ask `promotion_tried` and the
  question goes to the orchestrator as before.
- The tool loop reads `model_ref.id` from the row before each turn (next to the redirect
  read) and switches its catalog entry and provider when it changed. `RoleBuildCtx`
  carries `provider_for(entry)` so the new provider is built the same way as the first.
- Charges are attributed to the model that actually answered each turn.

## Consequences

- needs_model works for every real role, mid-loop, without restarting the run.
- The interrupt-based pause is now used only by `echo`; removing it (and routing echo's
  gate through the same row) is a follow-up.
