# rework

A portable, deployable multi-agent workflow engine. An **orchestrator** (a hand-built
LangGraph supervisor) plans and supervises **roles**, each a purpose-tuned LangGraph
workflow (investigator, coder, auditor, later CAD/research/design). Missions run end to end:
describe an issue, get a verified fix, report, or PR.

Roles are code-first `RoleSpec` classes assembled from a shared node library: modular
like object-oriented programming, rigid where the graph says so, agentic where it doesn't.
Escalations are typed envelopes; models come from a swappable catalog (Anthropic, OpenAI,
any OpenAI-compatible endpoint); humans step in only at declared approval gates.

On top of the engine sits the **Library**: a cross-project, cross-harness collection of
skills, tools, reports and memories that agents retrieve from and contribute to, so every
mission makes the next one cheaper. It is exposed as an MCP server: Claude Code, pi, or
any MCP client can mount the same library.

## Status

Milestone **M0 (scaffold + CI)** (see `docs/design.md` for the roadmap).

## Quickstart

```bash
uv sync
uv run rework --help
uv run pytest
```

## Design

- Living architecture: `docs/design.md`
- Decision records: `docs/adr/`

## License

MIT, see `LICENSE`.
