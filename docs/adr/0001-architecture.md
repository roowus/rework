# ADR 0001: Orchestrator–workers engine on LangGraph, code-first roles

## Status

Accepted

## Context

rework replaces hard-coded fixer→auditor scripts with a portable engine that must handle
"the vast majority of tasks, not just coding." Requirements gathered over a five-round
design Q&A with the owner (2026-11-02): modularity (roles come and go like OOP classes),
rigid graph structure where determinism matters with agentic freedom inside nodes, cheap-by-default
models with promotion, human gates, cross-device missions, and a compounding library.

## Decision

1. **Orchestrator–workers on LangGraph.** A hand-built supervisor graph (not the
   langgraph-supervisor library, not the Claude Agent SDK) because check-ins, typed
   escalations, model promotion and gates are first-class nodes with custom semantics.
2. **Roles are code-first `RoleSpec` subclasses**, each building a LangGraph graph from a
   shared node library — rejecting declarative YAML (GitHub-Actions rigidity: new behavior
   would require engine surgery) and a hybrid knobs layer (the knobs already live as typed
   RoleSpec fields).
3. **Roles are not orchestrator subgraphs.** Each run is an independent asyncio task with
   its own checkpointer thread; the orchestrator dispatches SpawnIntents that the engine
   reconciles into tasks. This keeps join semantics simple and crash recovery enumerable
   from run rows.
4. **Escalation is a typed envelope** `{kind, detail}` — machine-actable kinds
   (blocked/needs_model/needs_tool/needs_context/question) with prose detail, after
   Anthropic's structured-returns philosophy.
5. **Own model catalog + three thin adapters** (Anthropic, OpenAI, generic
   OpenAI-compatible) instead of litellm: ~hundreds of owned lines, no heavyweight
   dependency churn in the core, matches the harness-style catalog the owner already runs.
6. **State behind a StateStore seam**: Postgres default, SQLite fallback, per-instance
   config; LangGraph checkpointers on the same database.
7. **Python 3.12 + uv**: the LangGraph/deepagents/MCP-adapter ecosystem is Python-first.

## Consequences

- More core code than library-based supervision — repaid by exact semantics (promotion
  mid-run, rate-limited escalation policies, resumable gates from any machine).
- SQLite mode is demo/test-grade (concurrency 1, WAL); Postgres is the real deployment.
- Promotion defaults to same-provider (cross-provider breaks on Anthropic content-block
  shapes; behind a config flag pending a message-normalization pass).
- Rejected alternatives: langgraph-supervisor (shallow semantics), Claude Agent SDK brain
  (opaque, provider-locked), YAML roles (rigid), litellm (dependency churn), SQLite-only
  (fights the cross-device vision).
