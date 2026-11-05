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

Milestone **M3 in progress**: M2 shipped (all four builtin roles, the `fix` and
`fix-pr` pipelines, first self-hosted PR merged). M3 adds escalation policies
(needs_model auto-promotion, human gates for the rest), the burn-gate
(budget cap puts the mission ON_HOLD), an atomic leader lock, the eval harness
(`rework eval run` + score history), and the complaints inbox (ADR 0004: agents
file typed feedback on the workflow itself with the `file_complaint` tool).

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

One cross-device rule: `--repo` is resolved by the machine that runs `rework
new`, not by the daemon. If the daemon runs on another host, use one of the
transport forms (ADR 0005): a git URL (`--repo https://github.com/you/x.git`,
the daemon clones it), a project repo reference (`--project p-... --repo NAME`),
or an uploaded folder (`--repo-dir ~/folder` with `REWORK_DAEMON_URL` pointed
at the daemon; the workspace API is tailnet-only when the daemon binds its
tailnet IP). Results return as a branch (open_pr gate) or via `rework fetch
<mission-id>`.

## The TUI: just type `rework`

With no arguments, `rework` opens a full-screen dashboard (mouse + keyboard):

- **Left**: projects (select one to filter; create with `p`)
- **Right**: missions with live status (`n` opens the new-mission wizard:
  objective, repo URL / local path / folder upload, plan)
- **Bottom**: the selected mission's event timeline, streaming every 2 s;
  when a gate is waiting on you it shows an approve/reject box (`a` / `x`)
- **`g`** the gates queue across all missions · **`l`** the Library browser ·
  **`ctrl+p`** the command palette (find any action by typing) · **`q`** quit

The TUI is a client like every other command: it reads `REWORK_DB`, so from
your laptop it shows and controls the missions running on the daemon.

## Eval + complaints

```bash
uv run rework eval run                 # run every builtin eval task, graded mechanically
uv run rework eval history             # score history (the hill-climb record)
uv run rework complaints               # the inbox: agent feedback on the workflow
```

Eval tasks copy their fixture repo (never mutate it), grade rung 1 (mission
done + diff present + pytest green on the final worktree), and record one
history row per run. Complaints are fire-and-forget: a role calls
`file_complaint` mid-mission and keeps working; nothing pauses.

## Design

- Living architecture: `docs/design.md`
- Report architecture: `docs/reports.md`
- Decision records: `docs/adr/`

## License

MIT, see `LICENSE`.
