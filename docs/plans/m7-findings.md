# M7 findings log

Issues found by watching real missions (monitoring, not tests). Each entry:
what happened, why, what was done. Newest first.

## 2026-11-06, post-mortem of m-0d75f495121a (owner: "why no audit loop? why $2?")

**The loop.** It did not loop because the first real audit approved (three
low-severity notes, mechanical verdict approve); the playbook revised only on
request-changes. But the orchestrator also said "budget is nearly exhausted, so
I'll stop rather than burn a revision round": budget must never decide quality.
Fixed: playbooks revise while the audit requests changes OR has medium+
findings, ask the owner after three rounds, and the prompt says money is the
owner's call (the budget gate asks).

**The cost: $3.03** (investigator $0.79, coder $0.85, failed auditor $0.51,
auditor $0.84, orchestrator $0.05). 8.9M input tokens vs 305k output: input was
~88% of the bill.

**Rerun of the same task after the fixes** (local, m-7537920472d4): $0.92 vs
$3.03, 84% of input cached, and it did MORE: investigate, code, continue on the
same branch after the turn budget, audit on the strong model, revise, audit
again, both audits approve with zero open findings, ship gate (rejected by me so
nothing was pushed). The investigator report cited exact file:line evidence.

## 2026-11-07, owner: "the TUI can't be clicked, no keyboard input"

Not the rework agent: its dogfood PR was never opened, so none of its code was
in the owner's checkout. Both causes were mine, and both were invisible to the
Pilot test suite.

| # | finding | cause | status |
|---|---|---|---|
| 25 | No test could see either bug | Pilot simulates keys by NAME and never simulated a hanging database | fixed: `tests/tui/test_real_terminal.py` runs the TUI in a real pty (pyte reads the screen) against a socket that accepts and never answers; fails on the old code with "typing reaches the prompt" |
| 24 | alt+1..5 (tabs) and alt+a/x (gates) never worked in a real macOS terminal | Option+digit arrives as ¡ ™ £ ¢ ∞ (Textual maps ESC+digit to those too) and Option+a/x type å / ≈; Textual drops printable-character bindings while a text input has focus | fixed: the App intercepts those keys in on_event before the focused input; meta-mode alt+N bindings kept |
| 23 | Whole TUI frozen (no keys, no clicks) while rewvis was unreachable | the hub refresh was awaited on the App's message queue; Postgres had no connect timeout (OS TCP timeout ~75s per try) and reads were unbounded | fixed: refresh runs in a worker; `connect_timeout=10`; reads bounded at 15s and treated as a dead connection; status shows "connecting" until the first load |

## 2026-11-06, dogfood #2 m-d454350350af (orchestrator Output tab, local, fix-pr)

| # | finding | cause | status |
|---|---|---|---|
| 22 | Four coders in a row each spent ~35 of 40 turns re-learning the same code before continuing | continuation kept the branch but not the knowledge: a budget-stopped coder handed over only "stopped: turn budget reached" | fixed: budget-stopped roles write a handoff note (tools-free final call); a continuation coder starts from it |
| 21 | Coder spent its 40 turns re-reading the same files (dashboard.py ~7 times, seed.py 4) | compaction at 60k chars / keep 6 elided reads the model still needed | tuned to 160k / keep 12 (caching makes kept context cheap) |

What worked: the orchestrator saw the coder run out of turns, continued on the
same branch with a prioritized "finish and commit" brief.

## 2026-11-06, post-mortem of m-0d75f495121a (continued)

| # | finding | cause | status |
|---|---|---|---|
| 20 | The coder wrote 20+ throwaway probe scripts measuring pixel alignment for a one-property CSS change | the brief forwarded speculative side concerns; the coder prompt had no proportionality rule | fixed: coder "smallest change, existing tests, no probes"; orchestrator "scope briefs to the objective" |
| 19 | Shipping delivered nothing: the commit stayed on rewvis | the project's `results: push_branch` setting (ADR 0005) was never implemented | fixed: ship pushes the branch for git-origin missions; chat prefers fix-pr for git projects |
| 18 | 6k of 8.9M input tokens cached | no session key, so Fireworks routed each turn to a random replica (its cache is per replica); the orchestrator's system prompt also changed every turn | fixed: `ChatRequest.session_id` -> `user`; static orchestrator system prompt; Anthropic cache breakpoints. Rerun: 76% cached |
| 17 | 58-80 calls per role, 35-45k tokens resent per call | no turn budget; whole transcript resent forever | fixed: turn budgets per spawn (+ wrap-up warning), compaction of old tool output |
| 16 | Auditor died on a Fireworks 500 ($0.51) | the retry wrapper did not treat 500 as transient | fixed |
| 15 | Auditor (after the budget gate) and coder (after the crash) restarted their loops at turn 1 | the whole loop is one checkpointed node | fixed: per-turn transcript saved to the run's workdir, resumed on re-entry |
| 14 | A revision coder would start from base, redoing everything | each coder run got a fresh worktree | fixed: revisions continue the previous coder's branch (diff base unchanged) |
| 13 | Cost reports ignored cached-token discounts | estimate_cost had no cached term | fixed: `cost_cached_per_mtok` (default half input) |

## 2026-11-06, mission m-0d75f495121a (owner's first dogfood task)

Task: "TUI drop-down menus get an outline border; stop at the ship gate."
Launched from the TUI against the rewvis deployment.

| # | finding | cause | status |
|---|---|---|---|
| 12 | An old TUI process (pre-fix code) still holds two connections idle-in-transaction for 2h+ | the library-store fix only helps processes running new code | owner: restart old TUIs |
| 11 | Cost grows with transcript length: coder at 43k tokens/turn, `cached_tokens` always 0 | every turn resends the full history; no prompt caching on this provider | open: history compaction for long role loops |
| 10 | Roles cannot ask anything: only the echo stub calls interrupt(); the escalate node in the design never existed | never built; interrupt mid-loop would replay the whole loop | fixed: `ask_orchestrator` blocking tool (M7 P2) |
| 9 | Runner crashed mid-coder: `cached plan must not change result type` | a client (my monitoring scripts, newer code) applied the gates.spec migration; the daemon's auto-prepared `SELECT *` broke | fixed: `prepare_threshold=None`; mission restored and resumed from its saved conversation |
| 8 | Investigator report called the exact location "lost" and filed it as a blocker, though the run had printed it | report writer dropped every tool result and reduced calls to names | fixed `report-writer sees the evidence` |
| 7 | Investigator spent 65 calls / $0.79 prototyping the fix in /tmp and running tests | prompt never said investigating excludes implementing; orchestrator could not set depth | fixed: prompt rule + `spawn_role(depth=)` |
| 6 | `cd $D` slips past the cd jail (only literal absolute `cd` is refused) | the jail is advisory string matching | known; real fix is container sandboxing (M8) |
| 5 | At the budget cap, roles quit with half-done work instead of pausing for the burn gate; roles kept spending while the gate waited; a raised cap was invisible to roles built earlier | role guard used the cap as a hard stop; burn gate did not stop tasks; cap read once at build | fixed `at the cap, roles pause` |
| 4 | TUI library connections sat idle-in-transaction on prod for 1h38m | library store reads never committed | fixed `Postgres reads end their transaction` |
| 3 | Mission stuck on "running" after its runner crashed | daemon logged MISSION_FAILED but never updated the row | fixed `a crashed runner marks its mission failed` |
| 2 | Mission failed with "empty plan" | the TUI (new code) created an orchestrated plan; rewvis ran pre-orchestrator code | deployed; the mission resumed on the new daemon |
| 1 | Owner's TUI pointed at prod, not local | shell exports REWORK_DB for rewvis; plain `rework` uses it | documented `scripts/rework-local`; consider a visible "connected to" label in the TUI |

What worked: the orchestrator read the thin investigation, judged it unreliable
and wrote the coder a brief treating it as leads rather than facts. Its
self-chosen 12-minute check-in fired, and after the crash and restore it
reloaded its conversation, called `check_in` on the coder and carried on.

## 2026-11-06, mission m-808acff14c50 (local toyrepo, first orchestrated run)

| # | finding | cause | status |
|---|---|---|---|
| 1 | Orchestrator picked `glm-flash`; the coder died before its first call | catalog listed models whose key is not on this machine | fixed: `ModelEntry.has_credentials`, catalog filtered |

What worked: skipped the investigator as too small, recovered from the dead
coder by respawning on `flash`, audited, finished with an accurate summary.
