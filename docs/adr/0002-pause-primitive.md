# ADR 0002: One pause primitive (gates and escalations share interrupt mechanics)

## Status

Proposed (enacted in M1)

## Context

Both human approval gates and role→orchestrator escalations pause a running graph until
an external decision arrives. LangGraph's `interrupt()` re-executes the whole node body
on resume, so side effects before the interrupt duplicate.

## Decision

Gates and escalations use the same pause mechanics: interrupt is the FIRST side effect of
the pausing node (only sanctioned wrappers: `GateManager.request`, the escalate node); the
decision is persisted in a store row that is the single source of truth; resume happens
with `Command(resume=decision)` read from that row, including decisions made while the
daemon was down (boot recovery consults open rows before the first astream call).

## Consequences

One recovery path to test; no duplicated side effects; gates resolvable from any machine
sharing the database. Cost: node-body discipline (linted/reviewed per CLAUDE.md rule 1).
