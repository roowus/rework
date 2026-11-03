# ADR 0000: ADR process

## Status

Accepted

## Context

rework accrues architectural decisions that must outlive any single conversation. Without
a record, the reasoning behind seams and invariants evaporates and gets re-litigated.

## Decision

Significant decisions get one markdown file here, numbered sequentially:

- **Status**: Proposed | Accepted | Superseded (by ADR-NNNN)
- **Context**: the forces at play
- **Decision**: what we chose
- **Consequences**: what this costs and buys, including rejected alternatives

An ADR is written in the same commit as the code that enacts it (see CLAUDE.md,
docs follow code). Superseding replaces, never edits: the old ADR keeps its status
updated with a pointer.
