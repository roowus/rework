# ADR 0003: Role contracts, goal-powered specs, orchestrator-assembled context

## Status

Accepted (decided in the M2 per-role design sessions, 2026-11-03)

## Context

M2 adds real roles (investigator, coder, auditor). Design Q&A with the owner
settled their graphs, artifacts, and loop protocol. Two earlier systems of the
owner's inform the contracts: `roowus/workflow` (JSON-schema contracts between
pipeline stages) and the `pr-audit` workflow in `roowus/truss` (a production
LLM PR auditor with a proven report format).

## Decision

1. **Goal-powered roles.** Every RoleSpec carries a `goal` line (investigator:
   find as much relevant information as possible, report honestly; coder: make
   it work reliably; auditor: find issues before they reach production). The
   goal anchors every prompt for that role.
2. **Graph shapes.** Investigator: mechanical repo-map node, free agentic tool
   loop (a depth *hint* tunes thoroughness guidance; turn counts are not
   hardcoded, the budget ledger is the enforcement), rigid report-writer.
   Coder: free loop in a per-run git worktree + branch, rigid handoff node,
   self-checked tests. Auditor: one general evaluator (domain variants only if
   performance shows the need), tool-equipped, two-phase interaction.
3. **Contracts from the workflow lineage.** Honest states (blocked is a
   correct outcome), the red-green test record, the disposition protocol
   (fixed/refuted/out-of-scope/accepted/deferred; silence is not an option),
   structured blockers (question + options + recommendation +
   why_i_cannot_answer), CONFIRMED vs PLAUSIBLE verdicts with file:line
   evidence.
4. **Modular report sections.** A report is an assembly of section components
   (prompt fragment + validator + renderer). The caller's `requested_sections`
   picks them like checkboxes. The auditor's default assembly is the truss
   format: severity-grouped findings, the five standing assessments
   (proportionality, complexity, priority, coverage, business-logic risk)
   present even on clean audits, machine-readable JSON that must agree with
   the prose, mechanical verdict mapping (critical: request changes; high:
   approve with fixes; else approve).
5. **Context is orchestrator policy, not tiers.** Roles declare a context
   manifest (what they can consume: diff, test_results, brief, findings,
   author_narrative, notes, ...). The orchestrator chooses per mission what to
   include. The default auditor policy: neutral context + author narrative
   (marked as claims to weigh, per truss), withholding the coder's
   self-assessment until after the auditor's own first pass. This default is
   policy, not structure; a guidebook skill (general "less but enough"
   guidance) comes later via the Library.
6. **Loop protocol.** Orchestrator-routed (coder to auditor to investigator
   back to coder, whatever the plan needs) until decided-done, a structured
   blocker needs a human, or the mission is held. ON_HOLD is a general
   mission state reachable from any role's escalation or the user at any
   time; budget/time burn-gates that auto-hold are one more trigger,
   landing with M3. Findings already dispositioned stay closed unless new
   evidence (the no-re-raise rule).

## Consequences

- RoleSpec gains `goal` and `context_manifest`; TaskContext gains
  `requested_sections` and orchestrator-chosen context keys.
- MissionStatus gains ON_HOLD (user pause; burn-gate reuse in M3).
- The truss no-re-raise and verdict-mapping rules become mechanical checks in
  the auditor's report-writer node, not prompt hopes.
- Domain-specific auditors stay cheap to add later: same RoleSpec, different
  requested_sections default and checklist.
