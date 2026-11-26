# ADR 0020: The agent loop's transcript lives in the store

## Status

Accepted (2026-11-26).

## Context

A role's agent loop is ONE LangGraph node on purpose: the model, not the graph, decides
which tools to call and when to stop (the owner's design: the agent is the core of a
role; the rigid nodes around it fit it into the framework). LangGraph checkpoints node
boundaries, so mid-loop progress was kept in `<workdir>/loop.json` on the daemon's disk.

That file is outside the store. A run could resume only on the machine and workdir that
started it (a blocker for ADR 0010's remote execution and for moving a daemon), a lost
workdir silently cost every earlier turn, and it broke CLAUDE.md rule 3 (the store is the
source of truth).

## Decision

- New `loop_state` table (run_id, turns, messages JSON, updated_at) in both backends,
  behind `StateStore.save_loop_state / load_loop_state / clear_loop_state`.
- The tool loop saves after each turn, resumes from the store, and clears it when the
  loop ends. A save or load failure is logged and costs replay, never the run.
- A legacy `loop.json` is still read once when the store has nothing, so a run
  interrupted across the upgrade resumes; it is deleted with the state.
- The loop stays one free agent node. Per-turn graph nodes were considered and rejected:
  they would make the agent's moves a graph shape, which is the opposite of the intent.

## Consequences

- Any host with the database can resume a run's loop.
- One upsert per turn; transcripts are bounded by the loop's own compaction.
