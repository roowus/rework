# ADR 0010: Remote execution on additional hosts

## Status

Proposed.

This ADR lands before the code it describes. ADR 0000 requires an ADR to be written in the same
commit as the code that enacts it, and this document deliberately breaks that rule: the owner asked
to review the design before any code exists, because the load-bearing decisions here (the execution
seam under R1, the delegation rule under R2, the git identity under R5 and the credential boundary
under R6) are expensive to change once a worker is already deployed on a second box. Publishing the
design first, with the file-level plan attached, is the honest form of that review: nothing can be
verified yet, so what the reader gets is exact paths, exact strings and named rejected alternatives
instead of a claim that it works.

It moves to Accepted when phase 1 has landed and been verified on rewver: one role's `run_command`
executes on rewver, the output reaches the TUI as ordinary trajectory rows for that run, and the
mission record names the host. Nothing wider than that is needed to flip the status.

## Context

### What exists today

`rework` runs every role command on the daemon's own machine. The pieces that matter for remote
execution are these, and the anchors are exact because several plausible assumptions about them are
wrong:

| Layer | What exists today | Anchor |
|---|---|---|
| Execution seam | `ToolExecutor` Protocol with one implementation, `LocalExecutor`; `run_command` is the only branch that spawns a subprocess | `src/rework/nodes/tools.py:82`, `:191`, `:209`, `:233` |
| Executor binding | `LazyExecutor` forwards to `LocalExecutor` once `attach(workdir)` is called by the role's prepare node | `src/rework/nodes/tools.py:91`, `:105`; `src/rework/roles/builtin/coder.py:91`, `:128` |
| argv shaping | `command_prefix` wraps a command in a systemd scope, else `nice`/`ionice`/`ulimit`, else `/bin/sh -c`; one caller | `src/rework/nodes/hostlimits.py:272`, called from `src/rework/nodes/tools.py:227` |
| Concurrency | `RoleLimiter`, `PrioritySlots`, `MissionSlot` | `src/rework/orchestrator/limits.py:22`, `:121`, `:198` |
| Capacity control | `measure`, `plan`, `CapacitySettings`, `CapacityPolicy`, `auto_state`, `DynamicCapacity`; retunes the two caps through `set_capacity` / `set_cap` | `src/rework/orchestrator/capacity.py:165`, `:197`, `:220`, `:280`, `:444`, `:467` |
| State schema | `CREATE TABLE IF NOT EXISTS` plus an inline check-then-alter list in each store; no migrations directory, no version counter | `src/rework/kernel/state/sqlite.py:45`, `:1175`; `src/rework/kernel/state/postgres.py:46`, `:1307` |
| Transport | aiohttp app with a workspace API, mounted on the daemon's own loop | `src/rework/orchestrator/workspace.py`, `build_workspace_app` (`:529`), `start_workspace_server` |
| Auth | one shared bearer string, `REWORK_TOKEN`; absent token means open | `src/rework/orchestrator/workspace.py:519`, `:523`-`:527` |
| Bind | loopback by default, tailnet when `REWORK_HTTP_HOST` says so | `default_bind` in `src/rework/orchestrator/workspace.py` |
| Host targets | `targets.toml` carries `daemon_url` and `ssh` per named target | `src/rework/targets.py`, `Target` |
| Git identity | one deployment-wide author, `rework <rework@rework.local>` | `src/rework/config.py:157`-`:162`; `DEFAULT_GIT_AUTHOR` at `src/rework/nodes/git_ops.py:20` |
| Commit path | `commit()` passes `-c user.name` / `-c user.email` explicitly | `src/rework/nodes/git_ops.py:238` |
| TUI | polls the database; mission rows from `MissionCard`, live output from the `#chat-log` `Trajectory` | `src/rework/tui/widgets/cards.py:178`, `:50`; `src/rework/tui/views/work.py:98` |

Three corrections to the obvious reading of the code, because the plan depends on them:

1. The seam is not `command_prefix`. That function builds an argv prefix for a command that is still
   going to run locally; it has exactly one caller and no notion of where the process runs. The one
   place a role's command becomes a subprocess is `LocalExecutor.execute` (`src/rework/nodes/tools.py:191`),
   in the `if name == "run_command"` branch (`:209`): `asyncio.create_subprocess_exec(*prefix, ...)`
   at `:233`, then `_register_command` (`:241`) and `_kill_tree` (`:245`), with output trimmed to
   8000 chars. Above it sits the `ToolExecutor` Protocol whose docstring already nominates a
   `DockerExecutor` "at the sandboxing milestone (M7); roles never see the difference".
2. Concurrency primitives are in `limits.py`, not `capacity.py`. `capacity.py` measures one box and
   retunes those primitives; it holds no scheduling decisions and has no cluster capacity
   abstraction to extend.
3. There is no schema migration system. A new column is added to two inline check-then-alter lists,
   or the two stores diverge silently.

### The gap

The owner has a second machine, rewver: Ubuntu 24.04, 4 cores, 7.8 GB RAM, 645 GB free, lid-close
ignored so it survives a closed lid but not a reboot or a sleep. rewvis (the daemon box) has no
display and no browsers, so a Playwright spec cannot run there at all. Nothing in the codebase
chooses a machine, nothing measures a machine other than the daemon's own, and the only credential
the mesh has is one shared bearer string that cannot be revoked per host.

### Constraints inherited from other ADRs

- ADR 0005 (`docs/adr/0005-projects-and-workspace-transport.md`) is Accepted: the daemon already
  knows a project's `repo_origin` and clones at run start, and an aiohttp workspace API already
  exists with a 64 MiB request cap and a loopback default bind. It also records that unconditional
  `ALTER TABLE` migrations convoyed every store open behind a long-lived reader and were replaced
  with check-then-alter plus a 5 s `lock_timeout` (`src/rework/kernel/state/postgres.py:258`).
- ADR 0009 (`docs/adr/0009-credentials-and-permissions.md`) is the credential ADR. Its "Phased
  implementation plan" section names the phase that lands secret storage, the injection path and
  redaction; see R6 for how this ADR's phases relate to it.
- ADR 0007 puts an LLM orchestrator in the loop that can spawn roles. R2 has to say what that
  orchestrator is and is not allowed to decide.

## Decision

R1 to R9 below are numbered recommendations, each with its reasoning and its rejected alternatives,
so that a reader can disagree with one of them by number. R10 to R13 (phases, file plan, migration,
open questions) carry the same numbering in the later sections.

### Layers this ADR adds

| Layer | Today | After this ADR |
|---|---|---|
| Control plane | daemon owns state and scheduling | unchanged, and now the only thing that does |
| Execution | `LocalExecutor` only | `LocalExecutor` or `RemoteExecutor`, chosen before the role runs |
| Transport | aiohttp workspace API, shared bearer | same aiohttp app, per-host bearer, worker-initiated pull |
| Placement | none | `HostRegistry` plus a capability filter, in the daemon |
| Workspace | one clone per run on the daemon | one clone per job on the host, SHA plus tree proven |
| Credentials | one `REWORK_TOKEN`, one git author | per-host enrolment token, per-host push token, per-host git identity |
| State | missions, runs, events | plus `hosts` and a `host` on each run |
| TUI | reads the database | no new streaming channel: remote output lands as ordinary trajectory events; phase 1 changes nothing in the TUI |

### R1: one control plane in the daemon, and the seam is `ToolExecutor`

**Recommendation.** The daemon stays the only writer of the state database and the only scheduler.
Remote execution is delegated to a small worker process that runs on the host and does four things:
enrols, claims a job, executes it inside a workspace it created itself, and reports. `rework` is
never installed on the host. The code seam is a new `RemoteExecutor` implementing the existing
`ToolExecutor` Protocol (`src/rework/nodes/tools.py:82`), selected at the point where the engine
builds or attaches the executor: `LazyExecutor.attach` (`src/rework/nodes/tools.py:105`) for the
coder, the direct `LocalExecutor(...)` construction in `src/rework/roles/builtin/auditor.py:120` and
`src/rework/roles/builtin/investigator.py:116`. The fourth construction site, `worktree_executor`
(`src/rework/nodes/git_ops.py:626`), is defined but currently uncalled: a repo-wide grep finds only
its definition, so this ADR leaves it alone and does not present it as a construction site.
`RemoteExecutor` lives in `src/rework/nodes/remote.py`, and `src/rework/nodes/tools.py` is the
binding point, not a second home: `LazyExecutor.attach` (`:105`-`:106`) hard-codes
`self._inner = LocalExecutor(...)` today and has to take an executor factory (or a resolved
executor) instead (R11). Roles call `executor.execute(name, args)` and never learn where the
process ran.

**What the seam covers, and what it does not.** `execute("run_command", ...)` becomes remote. Nothing
else does. Every other tool in a role's toolset is still pure Python running on the daemon against
`self._workdir`: `read_file`, `list_dir` and `file_complaint` are branches and wrappers of the same
`LocalExecutor` (`execute` at `src/rework/nodes/tools.py:191`, `BUILTIN_TOOLS` at `:22`), and
`ask_orchestrator`, the web tools and the library tools are wrapper layers built on top of it. The
coder's handoff is daemon-side too: `stage_all` (`src/rework/nodes/git_ops.py:182`),
`unstage_nested_repos` (`:202`), `commit` (`:237`) and `diff_base` (`:262`) all take a local path,
and the coder stores `"worktree": str(wt)` (`src/rework/roles/builtin/coder.py:213`) in its
artifact. The auditor's and the investigator's read root is `ctx.read_root`, a daemon path
(`src/rework/roles/builtin/investigator.py:116`). Enclosing only the subprocess spawn is therefore
**not** sufficient for a remote coder run, and R2.1 turns that into a hard placement gate instead
of a footnote. What the seam does buy is that the decision is one object, so the diff stays
reviewable (a new module plus a factory) and every existing role test stays valid. Putting the decision above
the executor level, for example in the plan or in `command_prefix`, would either duplicate the tool
loop on the host or require a second copy of `rework`'s jail semantics in a shell string.

**Consequences to state plainly.** A remote host runs its own `command_prefix`, never the daemon's:
the memory cap it applies is the host's own `MemoryMax`/`ulimit` policy, and `LocalExecutor`'s own
docstring already concedes that the `run_command` memory cap is advisory. A remote host inherits
that weakness, and this ADR does not fix it. One further consequence, stated once here and relied on
by R2.1 and R4: an artifact carrying a daemon-local `"worktree"` path must not be produced by a
remote run in phase 1, and RESULT must never carry a path the daemon will later use as if it were
local.

**Rejected.** Replicating `rework` or the state database to hosts (two writers, split brain, and the
daemon's schema discipline would have to ship as a second deployable). A second full daemon per host
(two schedulers, no way to say which one owns a mission). Making `command_prefix`
(`src/rework/nodes/hostlimits.py:272`) or the shell string the seam (it is an argv shaper with one
caller and no transport; naming it as the seam would produce an unimplementable plan).

### R2: delegation is explicit ask plus policy, with policy as the sole arbiter

This is the owner's explicit open question, so it is answered directly. Three shapes were compared:

| Shape | Who decides placement | Strengths | Where it breaks |
|---|---|---|---|
| (i) explicit orchestrator tool `spawn_on_host` | the LLM orchestrator, per step | maximally expressive, one obvious knob | puts routing in a non-deterministic loop that costs tokens per decision, is unauditable after the fact, and cannot express "this suite always goes to the 4 core box" without the model remembering every time |
| (ii) engine policy from capability tags | the daemon, deterministically | reproducible, testable, auditable, cheap | cannot express a genuine per-step need the taxonomy did not anticipate, and gives the human no pin when they know better |
| (iii) both, policy arbitrates | daemon, informed by tags and an optional pin | deterministic default, expressiveness where needed, human override | needs one precedence rule, stated below, or the two inputs fight |
| (iv) global worker queue, no daemon-side decision | the workers, by first claim | simplest daemon | the daemon can no longer say why a step ran where it did, cannot honour a capability requirement, and has no place to put the no-host policy |

**Recommendation: (iii)**, with this exact precedence.

1. A step or role may carry `requires` (a set of capability entries from R7) and optionally
   `prefer_host` (a host name).
2. Both are inputs to the placement policy. The policy is the sole arbiter: it may veto or refuse an
   explicit request, never the reverse.
3. An explicit request the policy cannot satisfy is queued, or fails with a named reason. It is never
   silently ignored, and it never bypasses a capability filter.

**Reasoning.** Explicit-only makes placement an LLM judgement call: unauditable, token-costed, and
incapable of a stable "this suite always lands here" rule. Policy-only cannot express the Playwright
case where one specific step genuinely needs a browser, and it leaves the human with no way to pin a
mission to the big box. The precedence rule is the part that keeps (iii) from becoming a coin flip:
`prefer_host` narrows the candidate set, it does not command it.

**Rejected.** (i) alone, (ii) alone, and (iv) the worker pulling from a shared global queue with no
daemon-side decision at all.

#### R2.1: the toolset gate

**Recommendation.** A host may be chosen for a run only if every tool the role can call is in the
remote-capable set, and in phase 1 that set is exactly `{run_command}`. A role whose toolset includes
anything else is not placeable on a host: the policy refuses, with a reason string naming the first
non-remote-capable tool, for example
`role coder is not placeable on a host: ask_orchestrator is not remote-capable`.

**What the non-remote set is, read from the code.** Every tool that exists today is answered either
by a branch of `LocalExecutor.execute` (`src/rework/nodes/tools.py:191`) or by one of the wrappers
that compose around it: `read_file`, `list_dir`, `run_command` in `BUILTIN_TOOLS`
(`src/rework/nodes/tools.py:22`), `file_complaint` (`COMPLAINT_SPEC`, `:57`), `ask_orchestrator`
(`src/rework/nodes/escalate.py:27`), `web_search` and `fetch_url` (`src/rework/web/tools.py:19`), and
`library_search`, `library_read`, `library_contribute` (`src/rework/library/tools.py:123`). There is
no file-writing or patch tool at all: the coder edits files through `run_command` (heredocs,
`git apply`), not through a write tool.

**Where the toolset is actually declared.** Not in `RoleSpec.tools`: the declarative `ToolPack.allowed`
field (`src/rework/roles/base.py:54`, used at `:108`) is dead code that nothing reads, exactly as the
`src/rework/web/tools.py:5` comment says, so a tool registered there would never appear. A role's real
toolset is the wrapper chain it builds at its own `make_toolloop_node` call
(`src/rework/roles/builtin/coder.py:236`-`:244`, `auditor.py:192`-`:199`,
`investigator.py:107`-`:115`). All three builtin roles wrap `ComplaintAwareExecutor`,
`EscalationAwareExecutor`, `library_executor` and (for two of them) `web_executor`, so all three carry
non-remote tools today and none of them is placeable on a host in phase 1. The gate therefore reads
the composed executor's `specs()`, not a declarative field.

**`prefer_host` cannot override the gate.** R2 already makes the policy the sole arbiter of an
explicit request, and this is that rule applied to one of its inputs: a pin narrows the candidate set,
and an empty candidate set stays empty. An explicit request that fails the gate is refused with the
tool name, never silently downgraded to a remote run that cannot finish.

**What phase 1 still delivers.** Test suites, lint matrices, soak and fuzz runs, doc builds, and any
role that shells out without editing files: in one line, a role whose whole toolset is
`run_command`.

**What it does not deliver yet.** A coder role that edits files, an auditor or investigator that reads
them, and any run whose result depends on a commit or a diff. Those need R10 phase 2.

**Rejected.** A warning instead of a refusal (produces a remote coder run with no commit and no diff,
which is the defect this gate exists to prevent). Proxying `read_file` back to the daemon per call
while leaving the handoff local (the half-remote state R1 describes, and it would make a remote run
depend on a daemon path that does not exist on the host). Inferring capability from what the model
says it needs instead of from the toolset (the toolset is a fact about the code; the intent is not).

### R3: one fresh clone per job, on the host, proven by SHA and tree

**Recommendation.** Each job gets one fresh workspace on the host: a clone of the mission's exact
commit in a per-job directory, never a shared mount, never shared between two jobs.

Mechanism:

- The daemon already knows `repo_origin` (`Mission.repo_origin`, `src/rework/kernel/state/records.py:108`)
  and the commit the run is based on. The ASSIGN message (R4) carries `repo_origin`, `commit_sha`,
  `branch_for_push` and a per-job `workspace_id`.
- The host keeps a bare mirror per repo (`git clone --mirror` once, `git fetch` per job), then
  `git worktree add --detach` from that mirror (or a plain `git clone` from the local mirror) and
  `git checkout <commit_sha>`.
- The RESULT message proves the tree: it echoes `git rev-parse HEAD` and `git rev-parse HEAD^{tree}`.
  The daemon compares both against what it expected and rejects a mismatch as a failure, not as a
  success with a warning.
- Lifecycle: the workspace is removed as soon as the result is durably recorded. It is kept only when
  the job failed, for post-mortem, and a per-host GC removes workspaces older than a configured age
  or beyond a disk floor.

**Failure modes each choice removes.**

| Choice | Failure it removes | Failure it would otherwise add |
|---|---|---|
| fresh per-job clone | stale tree, silent wrong commit, cross-job interference | none beyond clone time, which the mirror pays for |
| mirror plus per-job worktree | clone latency on a 645 GB box with a cold cache | mirror corruption, which is repairable by refetching |
| SHA plus tree proof in RESULT | a job that ran somewhere else and reported green | none; it is two extra fields |
| shared mount | nothing | a killed job corrupts the other job's build cache, and the daemon's own files appear on a host |
| reuse with fetch by default | clone time | stale tree and silent wrong commit, precisely the failure the SHA-plus-tree proof exists to kill |
| keep every workspace forever | post-mortem convenience | disk exhaustion on a 645 GB consumer box |

**What a job may not assume.** No daemon filesystem (nothing outside its own workspace and its own
env file). No state database, at all. No other job's workspace. No display unless the host advertises
`has_display` (R7). No network beyond what its host advertises as `network_egress`.

**Rejected.** A network-mounted workspace shared by daemon and host (see the table). Reuse of a warm
checkout by default. Keeping workspaces indefinitely.

### R4: the protocol

**Shape.** Worker-initiated pull, over the tailnet, aiohttp, extending the ADR 0005 style rather than
inventing a second transport. The worker opens outbound HTTPS to the daemon's tailnet address and
long-polls for work; nothing listens on the host, so there is no inbound port and no firewall change
on a machine that comes and goes.

| Property | Choice | Why |
|---|---|---|
| Direction | worker to daemon only | no inbound reachability, survives sleep, address change and NAT |
| Framework | aiohttp, same app object as `workspace.py` | already a dependency, already mounted on the daemon loop |
| Claim style | long-poll with a server-side hold of about 25 s | one connection per claim, no websocket state to leak, shorter than any sane proxy timeout |
| TLS | tailscale provides transport encryption | no certificate management of our own |
| Bind | daemon binds the tailnet address (loopback stays available for local tools) | see R13 question 1; `default_bind` already reads `REWORK_HTTP_HOST` |
| Identity | one persistent worker identity per host, connection per claim | revocation is a row delete (R6) |

**Endpoints.** Eight, of which one is the existing health route:

| Method and path | Purpose |
|---|---|
| `POST /remote/hello` | HELLO, returns HELLO_ACK with expected versions |
| `POST /remote/claim` | CLAIM, long-polls, returns ASSIGN or 204 |
| `POST /remote/heartbeat` | HEARTBEAT, returns HEARTBEAT_ACK carrying cancels and verdicts |
| `POST /remote/log` | LOG chunk for the run the worker holds a lease on |
| `POST /remote/result` | RESULT, terminal success candidate |
| `POST /remote/fail` | FAIL, terminal failure with a class |
| `GET /remote/hosts` | enrolled host list and last heartbeat, for the CLI and TUI |
| `GET /healthz` | existing route, reused by the worker for reachability checks |

**Message kinds.** Every message, in both directions, carries `protocol_version`. Ten kinds:

| Kind | Direction | Payload (abridged) |
|---|---|---|
| HELLO | worker, daemon | `worker_version`, `protocol_version`, `host_name`, `capabilities`, `limits` |
| HELLO_ACK | daemon, worker | `protocol_version`, `accepted` (bool), `reason`, `claim_hold_s` |
| CLAIM | worker, daemon | `in_flight` (job ids), `metrics` |
| ASSIGN | daemon, worker | `job_id`, `run_id`, `attempt`, `repo_origin`, `commit_sha`, `command`, `env` allowlist, `timeouts`, `memory_max`, `workspace_id` |
| HEARTBEAT | worker, daemon | `job_id`, `lease_token`, `metrics` |
| HEARTBEAT_ACK | daemon, worker | `lease_valid`, `extend_s`, `cancel` (bool), `verdict` |
| LOG | worker, daemon | `job_id`, `lease_token`, `seq`, `stream`, `data`, `final` |
| RESULT | worker, daemon | `job_id`, `lease_token`, `exit_code`, `head_sha`, `tree_sha`, `pushed_refs`, `output_tail`, `artifacts` |
| FAIL | worker, daemon | `job_id`, `lease_token`, `failure_class` from `host_error`, `command_failed`, `workspace_error`, `timeout`, `oom`, `cancelled` |
| CANCEL | daemon, worker | delivered inside HEARTBEAT_ACK, never a separate inbound request |

Example ASSIGN:

```json
{
  "kind": "ASSIGN",
  "protocol_version": 1,
  "job_id": "j-8f31",
  "run_id": "r-2a91",
  "attempt": 1,
  "repo_origin": "git@github.com:owner/rework.git",
  "commit_sha": "4f2c1ab9e0d7c3a5b6f81d24e9c0aa71b3d5e6f2",
  "branch_for_push": "rework/r-2a91",
  "command": "uv run pytest tests/",
  "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "CI": "1"},
  "timeouts": {"idle_s": 600, "wall_s": 3600},
  "memory_max": "4G",
  "workspace_id": "ws-2a91-1"
}
```

Example LOG:

```json
{
  "kind": "LOG",
  "protocol_version": 1,
  "job_id": "j-8f31",
  "lease_token": "lt-9c0d4e",
  "seq": 41,
  "stream": "stdout",
  "data": "tests/test_add.py ..    [100%]\n",
  "final": false
}
```

Example RESULT:

```json
{
  "kind": "RESULT",
  "protocol_version": 1,
  "job_id": "j-8f31",
  "lease_token": "lt-9c0d4e",
  "exit_code": 0,
  "head_sha": "b71e0c95a2d44f18c3e6a7b90d12f45aa8c3e071",
  "tree_sha": "3d9a51c07b2e84f6a1c05d3b9e7f2048a6b1c2d3",
  "pushed_refs": ["refs/heads/rework/r-2a91"],
  "output_tail": "tests/test_add.py .. [100%]\n2 passed in 0.41s\n",
  "artifacts": ["shot-1.png", "trace.zip"]
}
```

**Auth.** One bearer token per host, issued at enrolment, sent as `Authorization: Bearer <token>`. The
daemon stores only the SHA-256 hash of the token plus the host name and creation time, so revocation
is a row delete. The plaintext exists once, in `/etc/rework/worker.env` on the host, mode 0600, owned
by the worker's service user. The current single shared `REWORK_TOKEN`
(`src/rework/orchestrator/workspace.py:519`, and "no token means open" at `:523`-`:527`) is **not
acceptable for hosts and must not be reused**: it is one secret for the whole mesh, it cannot be
revoked per host, and a compromised host would inherit everything the daemon exposes.

**Streaming to the TUI.** Each LOG chunk is appended by the daemon as an ordinary trajectory/chat
event for that run, using the existing event path (`EventRecord` with the run id, the `events` table
at `src/rework/kernel/state/sqlite.py:97`). The worker's `seq` is used only for ordering and gap
detection inside the daemon; the events table is the single source of truth. Phase 1 needs no TUI
change: remote output shows exactly like local output, because the TUI already reads only the database
(`MissionCard` in `src/rework/tui/widgets/cards.py:178`, `#chat-log` in
`src/rework/tui/views/work.py`). A gap is filled with an explicit
`[N bytes of output lost, host stream gap]` event, never silently.

**Size budget.** The target one person can implement in a week and audit in an afternoon: about 8
endpoints, 10 message kinds, one dependency that is already present (`aiohttp`), no new
database, no new state store, and a worker that is a single stdlib-only Python file (R11) so nothing
is installed on the host.

**Rejected.** Daemon-initiated SSH push (needs inbound reachability, breaks when the host sleeps or
its address moves, makes the daemon hold long-lived per-host connections, and puts a privileged
channel on the control plane). SSH-only with a queue directory (no live output, awkward per-job auth,
and file-based state the daemon must garbage collect blind). A message broker (a dependency and an
operational surface for a two-host tailnet).

### R5: git identity names the host

**Recommendation.** A commit made by a job on host `H` in project `P` carries author name
`rework+H` and author email `P-sanitised@rework+H.rewis` when the job is project-scoped, and
`rework+H@rewis` when it is not. Both parts are derived, never hand-edited into the repo.

Sanitisation rule for an email local part (applies to the project name and to the host name):

1. lowercase;
2. replace every character outside `[a-z0-9]` with `-`;
3. collapse runs of `-` into one;
4. trim leading and trailing `-`;
5. cap at 32 characters;
6. fall back to `project` if the result is empty.

ASCII transliteration is explicitly not attempted: a name that sanitises to something unhelpful gets
an explicit override instead (below). Tailnet host names are already DNS-safe, and they go through
the same rule so that one function covers both.

**Where it is configured.** Derived by default from the host record's `name` and the project's
`sanitised name`, with an optional explicit override per host (`git_name`, `git_email_domain`). Never
per job, never hand-edited into the repo, never typed by the model. The derivation lives in
`src/rework/remote/identity.py` (R11) and is wired next to the existing `git_author` property
(`src/rework/config.py:157`-`:162`), which keeps its current default for local work.

**Worked examples, exact strings.**

| Case | Author line |
|---|---|
| project `rework`, host `rewver`, project-scoped | `rework+rewver <rework@rework+rewver.rewis>` |
| project `rework`, host `rewver`, not project-scoped | `rework+rewver <rework+rewver@rewis>` |
| project `My_Project 2` (sanitises to `my-project-2`), host `rewver` | `rework+rewver <my-project-2@rework+rewver.rewis>` |

The first row's email local part is `rework`, the domain is `rework+rewver.rewis`; the second row's
local part is `rework+rewver` and the domain is `rewis`. That asymmetry is deliberate: in the
project-scoped form the project owns the local part and the host owns the domain, so sorting by
`%ae` groups by project first and host second.

**How the owner sees it at a glance.** Three channels, all three named:

1. The author identity itself: `git log --format='%an %ae'` and GitHub's commit list show the host
   without any extra tooling.
2. The mission record: a `host` on the run row, and a mission-level result line naming each host
   that ran steps and how many.
3. The PR body: one line per host, for example
   `Remote: rewver (100.101.74.108) ran 2 of 5 steps; commits authored as rework+rewver <rework@rework+rewver.rewis>`.

`100.101.74.108` is rewver's tailnet address, supplied by the owner; no host address is hardcoded in
the repository, it lives in the host record written at enrolment (R7). `src/rework/targets.py:12`
already carries the same kind of tailnet address for rewvis
(`daemon_url = "http://100.107.125.118:8941"`), which is the style precedent for this line, not a
substitute for the rewver record.

**Why not a trailer as the primary signal.** A `Co-authored-by:` line or a bespoke
`Rework-Host:` trailer is invisible in `git log --format=%ae`, has to be remembered by whoever writes
the commit (a role, an LLM, or a human), and cannot be enforced after the fact. It is allowed as an
optional extra on top of R5, never instead of it: the author identity is the only channel that
cannot be forgotten, because it is applied by the code that creates the commit
(`src/rework/nodes/git_ops.py:237`, which already passes `-c user.name` / `-c user.email`).

**Local path unchanged.** On rewvis the identity stays `rework <rework@rework.local>`, byte for byte,
as today (`DEFAULT_GIT_AUTHOR`, `src/rework/nodes/git_ops.py:20`, and `Config.git_author`,
`src/rework/config.py:157`-`:162`).

**Rejected.** Making the identity a per-job field set by the planner (unauditable and easy to lose on
retry). Encoding the host only in the commit message (invisible in `%ae`, skippable). Making the
host part of the local-part instead of the domain (breaks the project-first grouping above and puts
the variable part where sorting is least useful).

### R6: three credential classes

**(a) Enrolment token, one per host.** Minted by a daemon CLI command, shown once at issue time,
stored as a SHA-256 hash in the state DB, revoked by deleting the row. A revoked host's next CLAIM or
HEARTBEAT gets a 401, and its in-flight jobs expire per R8. This is the credential the worker uses as
its bearer for every R4 call.

**(b) Git push credential, one per host.** So that revoking one host does not touch the others: a
GitHub fine-grained personal access token scoped to the specific repositories the host may push to,
permission `Contents: read and write` only. It lives in `/etc/rework/worker.env` on the host, mode
0600, and the worker injects it into the job process environment only when the assignment says the
job must push. It is never written into the workspace, never echoed in logs (redaction is by exact
value), and never sent to the daemon.

Delivering it per job from the daemon is rejected for phase 1: that would make the daemon the holder
of every host's push credential and would require the secret-delivery machinery from ADR 0009.

Residual risk, stated plainly: a host holding a push token can push to whatever repositories that
token names. Mitigations are scope (the token names the project's repo only), a dedicated branch
namespace per run (`rework/<run id>`, as `src/rework/roles/builtin/coder.py:89` already names), and a
daemon check of the reported pushed SHA against the branch tip before the push is treated as part of
the mission.

**(c) What must never reach a worker.** The state database credentials. The shared `REWORK_TOKEN`.
Any other host's enrolment token or push token. The owner's chat or bot tokens. Provider API keys the
worker does not need. Anything in ADR 0009's Library beyond an explicitly scoped grant.

**What a job's environment looks like.** An allowlist, built by the worker from its own env file plus
the assignment's explicit `env` map, nothing inherited from the worker's own environment. Inheritance
is the failure mode this rule exists to remove: on a host, `systemctl show-environment` or a stray
export would otherwise leak into every job.

**Relationship to ADR 0009.** ADR 0009's "Phased implementation plan" section contains the phase that
lands secret storage, the injection path and redaction (its phase 1: the role-environment allowlist,
`credential_keys`/`credentials` tables, `CredentialVault`, `Redactor` and the `run_command`
credentials hook). Phase 1 of this ADR does **not** depend on it: host-local env files only, no
daemon-to-host secret delivery, no vault write from the remote path. Any later phase that wants
daemon-delivered per-job secrets does depend on it, and is therefore sequenced after that ADR 0009
phase lands.

**Rejected.** Reusing `REWORK_TOKEN` as the host credential (one secret for the mesh, no per-host
revocation). A push token per job (no reason, more moving parts, and the daemon would have to hold
them). Sending the push token in ASSIGN (makes every LOG and every support bundle a leak risk).

### R7: capabilities, requirements and placement

**Capability vocabulary.** Advertised by the host in HELLO and amended only by re-enrolment. Live
metrics ride on every CLAIM and HEARTBEAT.

| Capability | Type | How it is measured |
|---|---|---|
| `cpus` | int | `os.cpu_count()` on the host |
| `ram_mb` | int | `/proc/meminfo` `MemTotal` |
| `os` | `linux` \| `darwin` | `sys.platform` |
| `arch` | string | `platform.machine()` |
| `has_display` | bool | `DISPLAY` or `WAYLAND_DISPLAY` present at worker start |
| `has_browsers` | bool plus build list | presence of the pinned browser image or a named browser binary, recorded as a version string |
| `has_docker` | bool | `docker info` succeeds without sudo |
| `has_node` | bool plus version | `node --version` |
| `has_uv` | bool plus version | `uv --version` |
| `network_egress` | `none` \| `tailnet` \| `internet` | probe at worker start and on re-enrolment |
| `worker_version` | string | the worker's own version |
| `protocol_version` | int | the protocol it speaks |

Live metrics, on every heartbeat: `load1`, `mem_available_mb`, `disk_free_mb`, `inflight`.

**Job requirements.** `requires` on a step or role (vocabulary as above), plus `memory_max` and
`timeout_s` as today.

**Selection, in order.**

1. Hard filter: every `requires` entry satisfied; worker and protocol version compatible; host not
   draining.
2. Guardrail: refuse a host whose `load1` exceeds a configurable fraction of its `cpus`, whose
   `mem_available_mb` is below the job's `memory_max` plus a reserve, or whose `disk_free_mb` is below
   a configured floor.
3. Choice: least in-flight work normalised by `cpus`, deterministic tie-break by host name.

**Integration with the existing code.** A new `HostRegistry` module holds enrolled hosts and their
last heartbeat. The per-host concurrency budget extends `PrioritySlots`
(`src/rework/orchestrator/limits.py:121`) with one slot pool per host, sized from that host's `cpus`,
and that pool is retuned by the existing capacity loop (`DynamicCapacity`,
`src/rework/orchestrator/capacity.py:467`, calling `set_capacity`). `RoleLimiter`
(`src/rework/orchestrator/limits.py:22`) stays global per role. `capacity.py` today measures only the
local box (`measure`, `:165`), so a per-host measurement feed is the new part; the file plan does not
aim new scheduling logic at `capacity.py`, only a feed into it.

**When no host qualifies.** Queue with a bounded wait, then fall back to local execution if and only
if the step's `requires` are satisfiable locally; otherwise fail the step with a single explicit
reason string naming the missing capability and the wait duration. Nothing is silently downgraded and
nothing waits forever. The TUI shows the waiting state with the reason, for example
`waiting for a host with has_browsers (waited 3m12s, deadline 10m)`.

**Rejected.** Immediate hard failure (throws away a box that was merely busy for 40 seconds).
Indefinite wait (a silently stuck mission). Silent local fallback (produces a wrong-but-green result
on a box with no browser, which is the failure the owner already called out).

### R8: failure modes

Lease model behind the table: the worker heartbeats every 30 s; the daemon expires a lease after
about 90 s of missed heartbeats (configurable). Every timestamp that matters is stamped by the daemon
and lease expiry runs off the daemon's monotonic clock; the worker's clock is diagnostic only.

| Failure | Detection | Daemon action | TUI | Mission row |
|---|---|---|---|---|
| Host sleeps or network drops mid-job | heartbeat missed; lease expires at about 90 s | kill the job, re-queue the run at its last durable checkpoint, increment attempt | run shows `requeued (lease expired on rewver)` as an INFO event | attempt count moves; status returns to running |
| Host goes away entirely | all its leases expire; no CLAIM from it | queued jobs return to the queue; host marked stale; mission not failed | host listed as stale in the hosts view | unchanged except step attempts |
| Duplicate or late result from an expired claim | result arrives with a lease that is no longer live | first durable result wins; later ones rejected with a logged reason, never overwriting a terminal state | one INFO event naming the rejection | unchanged |
| Clock skew on the host | daemon compares its own timestamps only | skew cannot extend a lease or reorder events; worker timestamps recorded as diagnostics | nothing | nothing |
| Stale worker or protocol version | reported in HELLO and every CLAIM | refuse with 409 naming the expected version; mark the host incompatible | host flagged incompatible with the expected version | steps route elsewhere or wait |
| Hostile or compromised worker | every message validated as untrusted input | may append only to the run it holds a live lease on; cannot claim a specific run; cannot exceed granted work; message kinds are an allowlist; payload size and chunk rate capped; exit code advisory until the R3 tree and ref proof checks out | an INFO event on any rejected message | blast radius is that job's output only, never other runs' events or mission state |

**Retry budget.** Three attempts per step on remote failures (phase 2 owns the number; it lives in
config, not in the worker). When the budget is exhausted the step fails with a reason naming the host
and the last failure class, for example
`step test failed on rewver after 3 attempts (last failure: oom)`. Mission-level behaviour is then
the existing policy, unchanged: the engine's in-mission retry gives a failed step one more chance
(`src/rework/orchestrator/engine.py:720`-`:735`), and `_synthesize` decides the outcome from the
latest attempt per step (`src/rework/orchestrator/engine.py:1186`-`:1194`), so a step that stays
failed makes the mission `FAILED` while a later green attempt can still save it. This ADR adds no new
mission policy.

### R9: scope boundary

Not in scope, deliberately and completely:

- No `rework` install on the host.
- No state database on the host, not even a cache of one.
- No remote TUI, and no TUI push channel (the TUI stays a database reader).
- No cross-host mission migration: a mission's roles stay on one host per run in phase 1, and the
  mission stays owned by the daemon.
- No automatic host discovery: enrolment is explicit and out of band.
- No Windows.
- No shared or network-mounted workspace.
- No remote file editing, no remote commit and no remote diff in phase 1 (R2.1): the file tools,
  `stage_all`, `commit` and `diff_base` all stay daemon-side until R10 phase 2.
- No orchestrator or scheduling logic inside the worker: it claims, executes, reports.
- No protocol feature whose absence a single box cannot reveal (multi-daemon consensus, cross-host
  ordering, distributed locks).

## Consequences

### What this costs

| Cost | Size, plainly |
|---|---|
| One more deployable | a single-file worker per host, plus its systemd unit and env file |
| One more protocol | about 8 endpoints and 10 message kinds to keep compatible (R4) |
| Two schema changes in both stores | a `hosts` table plus a `host` column on runs and an attempt column, added to both inline check-then-alter lists |
| A test surface nothing on rewvis can exercise alone | capability filtering, lease expiry and re-queue need a second machine or a fake host in the test suite |
| Git identity derivation | a new module plus its tests, and a per-host override path to keep |
| The memory cap on remote jobs is advisory | inherited from `LocalExecutor`'s own docstring, and this ADR does not fix it |

### What it buys

- Playwright and browser work becomes possible at all: rewvis has no display and no browsers.
- A mission's cheap work stays local while its heavy or browser-bound work uses a host with 4 cores
  and 645 GB of disk.
- Remote output reaches the owner with no new streaming channel: it arrives as ordinary trajectory
  events for the run, and phase 1 requires zero TUI changes for it.
- Every commit says which machine produced it, in the one field that cannot be forgotten.
- Per-host revocation becomes a row delete instead of a mesh-wide secret rotation.

### Rejected alternatives

Collected here as well as per decision, because most readers will want the list in one place.

| Rejected | Recommendation it argues against | Why |
|---|---|---|
| Replicate `rework` or the database to hosts | R1 | two writers, split brain, and a second deployable to keep in step |
| A second daemon per host | R1 | two schedulers, no single owner of a mission |
| `command_prefix` as the seam | R1 | it is an argv shaper with one caller and no transport |
| Explicit `spawn_on_host` only | R2 | routing decided by an LLM loop: unauditable, token-costed, no stable rule |
| Capability policy only | R2 | cannot express a genuine per-step need, gives the human no pin |
| Worker pulls from a global queue with no daemon decision | R2 | the daemon cannot say why a step ran where, or honour a requirement |
| Shared or network-mounted workspace | R3 | removes none of the failure modes and adds cross-job corruption |
| Warm checkout reused by default | R3 | reinstates stale tree and silent wrong commit |
| Workspaces kept forever | R3 | disk exhaustion on a 645 GB box |
| Daemon-initiated SSH push | R4 | needs inbound reachability, dies when the host sleeps or moves, long-lived privileged channel |
| SSH plus a queue directory | R4 | no live output, awkward per-job auth, blind garbage collection |
| A message broker | R4 | a dependency and an operational surface for two hosts |
| Reusing the shared `REWORK_TOKEN` for hosts | R4, R6 | one secret for the mesh, no per-host revocation |
| Trailer or `Co-authored-by` as the primary host signal | R5 | invisible in `%ae`, unenforceable, must be remembered |
| Daemon-delivered per-job push credentials in phase 1 | R6 | makes the daemon hold every host's secret and needs ADR 0009's delivery machinery |
| Immediate hard failure when no host qualifies | R7 | discards a box that was busy for 40 seconds |
| Indefinite wait for a host | R7 | a silently stuck mission |
| Silent local fallback | R7 | a green browser test that never opened a browser |
| Any protocol feature a single box cannot reveal | R9 | unverifiable design is unverified design |

### Risks

1. **The lease is the only thing between a lost host and a half-written run.** Mitigation: re-queue
   from the last durable checkpoint, and never mark a run terminal on worker testimony alone.
2. **The worker is code running on a machine the daemon does not control.** Mitigation: the R6(c)
   never-list, the R8 untrusted-input rules, and per-host tokens so a compromise is revocable.
3. **Two stores can diverge on the new tables.** Mitigation: both inline lists plus the conformance
   suite, in the same commit.
4. **A push token on a host is a real GitHub credential.** Mitigation: fine-grained, repo-scoped,
   branch namespace per run, daemon-side ref verification.
5. **Scope creep toward a cluster scheduler.** Mitigation: R9, and R13 question 7 which keeps local
   execution off the protocol in phase 1.

### On automation of this document

Nothing gates this file automatically: CI runs ruff, ruff-format, mypy and pytest over Python only,
`docs/adr/` has no index or README, and there is no markdown lint, link checker, ADR linter or
`make docs` target, so adding a line to an index is not part of this change.

## Phased implementation plan

**R10.** Four phases, each landable and verifiable alone. Each names what lands, what proves it, and what it
does not do yet.

### Phase 1: the protocol, the worker, the `RemoteExecutor`, one host by name

**Lands.** R4's protocol and endpoints, the stdlib-only worker, `RemoteExecutor` implementing
`ToolExecutor` (R1), per-host enrolment token issuance and verification (R6a), and one host
configured by explicit name in config (no registry, no capability filtering yet). The R2.1 toolset
gate lands here as well, because it is the only placement rule phase 1 has: the placeable set is a
role whose whole toolset is `run_command`. R5's identity
derivation lands here too, because the commits a remote job makes need it from day one.

**Proves it works.** One role's `run_command` executes on rewver, and the trajectory rows appear in
the TUI exactly as they would locally, with the host named in the mission record. Concretely: a
mission whose single step is a `run_command` that prints a marker; the marker appears in the run's
`#chat-log`; `git log --format='%an %ae'` on the resulting commit shows
`rework+rewver <rework@rework+rewver.rewis>`; the run row carries `host = "rewver"`.

**Does not do yet.** No capability filtering, no `requires`, no per-host slot pools, no lease expiry
and re-queue, no artifacts, no revocation tooling. And, by R2.1: no remote file editing, no remote
commit, no remote diff.

### Phase 2: the registry, capabilities, requirements, leases

**Lands.** `HostRegistry` and the capability vocabulary (R7), `requires` on steps and roles, the
placement policy with the R2 precedence rule, per-host slot pools in `limits.py`, lease expiry and
re-queue (R8's first three rows), and the retry budget. Two more things land in phase 2 and are the
reason it is bigger than phase 1: the **remote file API** (the file-touching tools become messages
to the worker, so `read_file`, `list_dir` and any later write or patch tool answer from the host's
own workspace) and the **on-host git path** (stage, commit and diff are produced on the host and
shipped back in RESULT, so no daemon-local path is ever handed to a remote run).

**Recommendation on the file tools.** Reimplement them as protocol messages handled by the worker,
for the same reason `run_command` is a message: one jail implementation, one place a path is
resolved, one place to bound size. The obvious shortcut, rejected: `rsync` or a shared mount of the
worktree back to the daemon, which breaks R3's one-fresh-clone-per-job rule and reinstates every
failure the R3 table removes.

**Proves it works.** A suite tagged `cpus>=4` lands on rewver while an untagged role stays local; a
`kill -9` on the worker mid-job re-queues the run and it completes on the next attempt.

**Does not do yet.** No browsers, no artifacts, no guardrails beyond load and memory, no revocation
CLI, no host health in the TUI.

### Phase 3: browser and display jobs

**Lands.** `has_browsers` and `has_display` in practice: Playwright against a web project inside the
browser container (R12), artifact upload (screenshots, traces) reusing an ADR 0005-style files route,
and the PR-body host line from R5.

**Proves it works.** A Playwright spec that cannot run on rewvis passes on rewver, the screenshot
artifact is retrievable through the daemon, and the resulting commits carry the R5 identity.

**Does not do yet.** No disk floors or per-host GC, no revocation tooling, no host metrics in the
mission record.

### Phase 4: guardrails, metrics and hardening

**Lands.** Load and memory thresholds, per-host workspace GC, revocation tooling (mint, list, revoke),
host health in the TUI, metrics in the mission record, and the audit hardening of the R8 paths
(duplicate results, stale versions, hostile-worker input).

**Proves it works.** A revoked host's next claim gets 401 and its in-flight job expires; a workspace
older than the configured age is removed; a deliberately malformed message from a fake worker is
rejected and logged without touching mission state.

**Does not do yet.** Cross-host mission migration, multi-daemon, or Windows: R9.

## File-level plan

**R11.** Every path below is a real anchor in this repo, and the phase headings below are the
R10 phases. "Additive" means new code or new fields; "risky"
means it touches a file whose existing behaviour other features depend on.

### Phase 1

| Path | New or changed | Content and why this file | Risk |
|---|---|---|---|
| `src/rework/nodes/remote.py` | new | `RemoteExecutor` implementing the `ToolExecutor` Protocol next to `LocalExecutor`, so the seam is in the module that already defines the interface (`src/rework/nodes/tools.py:82`) | additive |
| `src/rework/remote/__init__.py` | new | package marker and the one public factory the engine imports | additive |
| `src/rework/remote/protocol.py` | new | the message dataclasses and `protocol_version`, one place for the R4 shapes so client and server cannot drift | additive |
| `src/rework/remote/server.py` | new | the aiohttp routes from R4, mounted by the same `build_workspace_app` call site (`src/rework/orchestrator/workspace.py:530`) so there is one app, not two | additive |
| `src/rework/remote/leases.py` | new | claim, renew, expire, idempotency keyed on run id plus job id plus attempt | additive |
| `src/rework/remote/identity.py` | new | the R5 derivation (`sanitise`, `author_for(host, project)`) and nothing else | additive |
| `src/rework/remote/worker/worker.py` | new | the single stdlib-only file that is copied to the host by scp; it imports nothing from `rework` | additive |
| `src/rework/nodes/tools.py` | changed | `LazyExecutor.attach` takes an executor factory (or a resolved executor) instead of hard-coding `LocalExecutor` (`:105`-`:106`); the `ToolExecutor` Protocol (`:82`) is unchanged. This file is the binding point, `nodes/remote.py` is `RemoteExecutor`'s only home | risky |
| `src/rework/remote/registry.py` | new | the R2.1 toolset gate: the remote-capable set and the refusal reason, keyed on the composed executor's `specs()`; phase 2 grows the same file into `HostRegistry` | additive |
| `src/rework/config.py` | changed | host settings and the identity hook next to the existing `git_author` property (`:157`-`:162`); `git_author` itself is untouched | additive |
| `src/rework/roles/builtin/coder.py` | changed | choose `RemoteExecutor` instead of `LazyExecutor`/`LocalExecutor` at the `LazyExecutor(...)` construction (`:91`) and its `attach` call (`:128`), per the placement decision | risky |
| `src/rework/roles/builtin/auditor.py` | changed | same choice at the direct `LocalExecutor(...)` (`:120`) | risky |
| `src/rework/roles/builtin/investigator.py` | changed | same choice at `LocalExecutor(ctx.read_root, ...)` (`:116`) | risky |
| `src/rework/nodes/git_ops.py` | changed | `commit` (`:237`) takes the R5 author; `worktree_executor` (`:626`) is dead code and this ADR neither wires nor edits it | risky |
| `src/rework/kernel/state/sqlite.py` | changed | `CREATE TABLE IF NOT EXISTS hosts` plus `host` and attempt columns, added to the inline check-then-alter list (`:1175`-`:1188`) | risky |
| `src/rework/kernel/state/postgres.py` | changed | the same three additions in its own list (`:1307`-`:1334`), with the 5 s `lock_timeout` lesson from `:258` respected | risky |
| `src/rework/kernel/state/records.py` | changed | `host` on `Run` and on `Mission.result`; a new `Host` model | additive |
| `tests/conformance/test_store.py` | changed | round-trip `hosts` and `Run.host` on both stores, or they silently diverge | additive |
| `tests/test_remote_identity.py` | new | the R5 worked examples as literal string assertions | additive |
| `tests/test_remote_protocol.py` | new | message round-trip, version refusal with 409, auth 401 | additive |
| `tests/test_remote_executor.py` | new | `RemoteExecutor` against a fake worker: output becomes events, tree mismatch becomes a failure | additive |

### Phase 2

| Path | New or changed | Content and why this file | Risk |
|---|---|---|---|
| `src/rework/remote/registry.py` | changed | `HostRegistry`, the R7 vocabulary, eligibility, guardrails, the deterministic choice, beside the R2.1 gate the file already holds | additive |
| `src/rework/remote/placement.py` | new | the R2 precedence rule as one pure function, so it is testable without a host | additive |
| `src/rework/orchestrator/limits.py` | changed | per-host `PrioritySlots` pools beside `MissionSlot` (`:198`); `RoleLimiter` (`:22`) stays global | risky |
| `src/rework/orchestrator/capacity.py` | changed | feed per-host measurements into the existing plan/retune path (`measure` `:165`, `DynamicCapacity` `:467`); no new scheduling logic here | risky |
| `src/rework/orchestrator/engine.py` | changed | apply placement at step dispatch (`_run_steps_once`, `:686`) and record the chosen host on the run | risky |
| `src/rework/remote/leases.py` | changed | expiry, re-queue and the retry budget from R8 | additive |
| `src/rework/tui/views/work.py` | changed | render the R7 waiting reason and the R8 requeue note from existing events; both are recommendations this ADR makes, so showing them is part of the design, not cosmetics | additive |
| `src/rework/nodes/remote.py` | changed | the file-touching tool names become worker messages (R10 phase 2); `run_command` is untouched | risky |
| `src/rework/remote/worker/worker.py` | changed | serve the file tool messages and run the git steps on the host | additive |
| `src/rework/nodes/git_ops.py` | changed | `stage_all` (`:182`), `commit` (`:237`) and `diff_base` (`:262`) produce their result on the host and ship it back in RESULT | risky |
| `tests/test_remote_placement.py` | new | the 2x3 cross product of `requires`/`prefer_host` against eligible, busy and incompatible hosts | additive |
| `tests/test_remote_leases.py` | new | missed heartbeat, duplicate result, clock skew, exhausted budget | additive |

### Phase 3

| Path | New or changed | Content and why this file | Risk |
|---|---|---|---|
| `src/rework/remote/artifacts.py` | new | artifact listing and transfer, reusing the ADR 0005 files route shape rather than inventing one; the browser image itself is set up per R12 | additive |
| `src/rework/remote/server.py` | changed | the artifact routes on the same app | additive |
| `src/rework/orchestrator/delivery.py` | changed | the PR-body host line from R5, next to the body it already renders (`:382`) | risky |
| `src/rework/tui/widgets/cards.py` | changed | the host chip on `MissionCard` (`:178`) and `ProposalCard` (`:50`), the at-a-glance channel R5 names | additive |
| `tests/test_remote_artifacts.py` | new | retrieve a screenshot produced by a remote job | additive |

### Phase 4

| Path | New or changed | Content and why this file | Risk |
|---|---|---|---|
| `src/rework/cli/__init__.py` | changed | `rework host enroll`, `list`, `revoke` (R6a), a sibling of the existing daemon commands | additive |
| `src/rework/remote/registry.py` | changed | GC of stale hosts and their workspaces | additive |
| `src/rework/tui/views/dashboard.py` | changed | host health from the database, no new channel | additive |
| `tests/test_remote_revocation.py` | new | 401 after revoke, in-flight job expires | additive |

**Not in this plan, on purpose.** `src/rework/nodes/hostlimits.py` is not edited: `command_prefix`
(`:272`) stays exactly as it is, and a remote host uses its own.

## Migration and setup on rewver

**R12.** The host: Ubuntu 24.04, 4 cores, 7.8 GB RAM, 645 GB free, lid-close ignored. A reboot or a sleep can
happen, so the worker must come back by itself. Rewver's tailnet address is `100.101.74.108`, supplied
by the owner and recorded in the host row at enrolment; `src/rework/targets.py:12` carries rewvis's
own `daemon_url`, which is the style precedent for this and not this literal.

Steps, in order, each with the command that verifies it:

1. **Service user.** `sudo useradd --system --create-home --shell /usr/sbin/nologin rework`. Verify:
   `getent passwd rework`. No shell login is needed beyond enrolment.
2. **Worker file.** Copy the single stdlib-only file:
   `scp src/rework/remote/worker/worker.py rewver:/usr/local/bin/rework-worker`. Verify:
   `ssh rewver 'python3 /usr/local/bin/rework-worker --version'`. No pip, no uv, no rework checkout on
   the host.
3. **Env file.** `sudo install -o rework -g rework -m 0600 /dev/null /etc/rework/worker.env`, then
   fill `REWORK_HOST_NAME=rewver`,
   `REWORK_DAEMON_URL=http://100.107.125.118:8941` (rewvis, the daemon's tailnet address, as
   `src/rework/targets.py:12` records it; rewver's own address, `100.101.74.108`, belongs in the host
   row at enrolment, not in this file),
   `REWORK_HOST_TOKEN=<enrolment token>`, `REWORK_GIT_PUSH_TOKEN=<fine-grained PAT>`. Verify:
   `sudo stat -c '%U %a' /etc/rework/worker.env` prints `rework 600`.
4. **Workspace root.** `sudo install -d -o rework -g rework -m 0700 /var/lib/rework/workspaces`
   with a disk floor in config. Verify: `df -h /var/lib/rework`.
5. **systemd unit.** `Restart=always` with `RestartSec=5s` and a start limit, `KillMode=control-group`
   so a killed job cannot leave orphans, `User=rework`, `EnvironmentFile=/etc/rework/worker.env`, and
   a journal that holds no secrets (the worker never logs the values from that file). Verify:
   `systemctl status rework-worker` then `sudo systemctl kill -9 rework-worker` and confirm it comes
   back within 10 s.
6. **Git on the host.** `git --version`; the mirror cache under `/var/lib/rework/mirrors`. Verify:
   `sudo -u rework git clone --mirror <repo_origin> /var/lib/rework/mirrors/probe.git`.
7. **Per-project toolchains, installed by the job, not baked in.** A Python project brings `uv` (or
   uses the host's Python), a Node project runs `npm ci` itself. Verify by running the project's own
   suite locally on the host once, by hand.

**The browser question.** Docker with a pinned Playwright image
(`mcr.microsoft.com/playwright:<pinned>`, or an equivalent pinned browser image) is recommended over
installing Playwright browsers on the host, because:

- the box has no desktop, so a container carrying the browser plus its system dependencies is
  reproducible and pinned, while a host install drifts with whatever apt has that week;
- it does not drag a dozen apt packages and a user-level browser cache onto a host that is otherwise
  clean;
- it isolates the browser from the host filesystem;
- it can be swapped or upgraded without touching the host.

Costs, named: the job must run with `has_docker` (R7), and access to the Docker socket is a privilege
question worth stating out loud (socket access is effectively root on the host; a userns-mapped setup
or a rootless daemon is the safer variant and should be the default if it works on 24.04). The
container must be given the workspace as a bind mount, read-write, with a path inside that job's own
workspace only and no other host path mounted. Verify: `docker run --rm <image> npx playwright
--version` as the `rework` user.

**Why these steps and not a setup script.** Each is a single command with a single verifiable result,
because a host that is set up wrongly fails later, at the least convenient moment, and on a machine
that comes and goes the failure looks like a network problem.

## Questions for the owner

**R13.** Seven decisions that the brief left open. Each has a recommendation in one sentence.

1. **Bind address for the daemon's aiohttp app: tailnet interface directly, or loopback plus one SSH
   tunnel per host?** Recommend binding the tailnet address, leaving loopback available for local
   tools, because a tunnel per host is a thing to keep alive on a machine that sleeps.
2. **Push path: does a host push to the project's GitHub origin with its own token, or push to the
   daemon as a relay remote?** Recommend direct push plus the daemon's ref verification (R6b),
   because the relay makes the daemon the holder of every host's credential.
3. **Is an explicit `spawn_on_host` ask exposed to the LLM in phase 1, or does phase 1 carry only
   config and tag level policy?** Recommend tags and config only in phase 1, because the policy is
   the arbiter anyway (R2) and the model does not need a second way to say the same thing yet.
4. **When the local box merely has no browser, does the no-host fallback run locally or fail with the
   capability reason?** Recommend fail, because a green browser test that never opened a browser is
   worse than a red one.
5. **Are enrolment and revocation daemon CLI commands, or a config file edit plus restart?**
   Recommend CLI (`rework host enroll|list|revoke`), because revocation must not need a restart.
6. **Is the identity domain `.rewis` fixed, or configurable per host?** Recommend configurable with
   `.rewis` as the default (R5), because a second domain costs one config field.
7. **Should the worker also be able to run jobs on rewvis itself, making local execution just another
   host?** Recommend not in phase 1, to keep one code path at a time; revisit after phase 2 proves
   the protocol.
