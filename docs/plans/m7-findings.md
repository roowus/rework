# M7 findings log

Issues found by watching real missions (monitoring, not tests). Each entry:
what happened, why, what was done. Newest first.

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
