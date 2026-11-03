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

Milestone **M2 in progress**: all engine machinery + all four builtin roles
(echo, investigator, coder, auditor) + the `fix` plan pipeline
(investigate -> code -> audit -> ship gate) are live; the first real-model
mission is one catalog config away (see Quickstart).

## Quickstart

```bash
uv sync
uv run pytest                # scripted suite (no network, no cost)
```

Run a real mission (spends a few cents):

```bash
cp models.example.json ~/.rework/models.json    # then edit ids/keys to taste
export ZAI_API_KEY=...                          # your provider key

uv run rework serve &                                          # terminal 1: the daemon
uv run rework new --plan fix --repo examples/toyrepo \
  "make add(2, 3) == 5: calc.py's add() subtracts"             # terminal 2: the mission
uv run rework gates                                            # watch it halt at the ship gate
uv run rework approve <gate-id>                                # you are the ship decision
uv run rework attach <mission-id>                              # full timeline replay
```

The mission investigates `examples/toyrepo`, fixes the planted bug on its own
branch inside an isolated worktree, audits the diff, then stops and waits for
your approval. The agent commits sign as `rework <rework@rework.local>` unless
you set `REWORK_GIT_AUTHOR_NAME` / `REWORK_GIT_AUTHOR_EMAIL`.

## Design

- Living architecture: `docs/design.md`
- Decision records: `docs/adr/`

## License

MIT, see `LICENSE`.
