# M7 findings log

Issues found by watching real missions (monitoring, not tests). Each entry:
what happened, why, what was done. Newest first.

## 2026-11-06, mission m-0d75f495121a (owner's first dogfood task)

Task: "TUI drop-down menus get an outline border; stop at the ship gate."
Launched from the TUI against the rewvis deployment.

| # | finding | cause | status |
|---|---|---|---|
| 8 | Investigator report called the exact location "lost" and filed it as a blocker, though the run had printed it | report writer dropped every tool result and reduced calls to names | fixed `report-writer sees the evidence` |
| 7 | Investigator spent 65 calls / $0.79 prototyping the fix in /tmp and running tests | prompt never said investigating excludes implementing; orchestrator could not set depth | fixed: prompt rule + `spawn_role(depth=)` |
| 6 | `cd $D` slips past the cd jail (only literal absolute `cd` is refused) | the jail is advisory string matching | known; real fix is container sandboxing (M8) |
| 5 | At the budget cap, roles quit with half-done work instead of pausing for the burn gate; roles kept spending while the gate waited; a raised cap was invisible to roles built earlier | role guard used the cap as a hard stop; burn gate did not stop tasks; cap read once at build | fixed `at the cap, roles pause` |
| 4 | TUI library connections sat idle-in-transaction on prod for 1h38m | library store reads never committed | fixed `Postgres reads end their transaction` |
| 3 | Mission stuck on "running" after its runner crashed | daemon logged MISSION_FAILED but never updated the row | fixed `a crashed runner marks its mission failed` |
| 2 | Mission failed with "empty plan" | the TUI (new code) created an orchestrated plan; rewvis ran pre-orchestrator code | deployed; the mission resumed on the new daemon |
| 1 | Owner's TUI pointed at prod, not local | shell exports REWORK_DB for rewvis; plain `rework` uses it | documented `scripts/rework-local`; consider a visible "connected to" label in the TUI |

What worked: the orchestrator read the thin investigation, judged it unreliable
and wrote the coder a brief treating it as leads rather than facts.

## 2026-11-06, mission m-808acff14c50 (local toyrepo, first orchestrated run)

| # | finding | cause | status |
|---|---|---|---|
| 1 | Orchestrator picked `glm-flash`; the coder died before its first call | catalog listed models whose key is not on this machine | fixed: `ModelEntry.has_credentials`, catalog filtered |

What worked: skipped the investigator as too small, recovered from the dead
coder by respawning on `flash`, audited, finished with an accurate summary.
