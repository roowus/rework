# Report architecture (what reports are made of)

Every report artifact is an assembly of **sections** (`nodes/report_sections.py`).
A section is one checkbox: a prompt fragment telling the model what to produce, a
JSON-schema slice it must satisfy, and required fields. The caller's
`requested_sections` picks keys; unknown keys fail loudly; `None` means the
investigator default. Two things are NOT sections: they are mechanical, always on,
and never LLM-written.

## Always-on mechanical sections (not configurable)

| part | what it is | where from |
|---|---|---|
| `run_stats` | model calls, tokens in/out, cached tokens, cost estimate, elapsed seconds, models used | the ledger + run row; stamped by `nodes/run_stats.py` after every artifact (and live-computed by `rework report --md` for old artifacts) |
| coder handoff extras | branch, commit sha, base sha, mechanical diff, worktree path | `nodes/git_ops.py` at handoff time |

These exist because objective claims must come from measurement. Cost and time are
the owner's examples of permanently forced content.

## Section registry (the configurable checklist)

| key | meaning | required fields | typical role |
|---|---|---|---|
| `summary` | one paragraph: what was examined, bottom line | summary | all roles |
| `findings` | numbered findings: title, detail, evidence (file:line actually read, never invented), severity, verdict CONFIRMED/PLAUSIBLE, confidence | findings[] | investigator, auditor |
| `blockers` | structured open questions: question, 2+ options, recommendation, why it cannot self-answer. The honesty valve: an explicit blocker beats a guess | blockers[] | all roles |
| `suggested_fix` | concrete fix approach per actionable finding (approach, not the fix) | suggested_fix | investigator |
| `tests` | the SMALLEST test that would catch the bug: name, what_it_pins, where, should_fail_because, command; an empty list needs `no_tests_reason` | tests[], no_tests_reason | investigator |
| `ruled_out` | areas examined and clean, with the evidence that rules them out | ruled_out[] | investigator |
| `classification` | complexity + priority judgment with justification | classification | investigator, auditor |
| `standing_assessments` | the five truss verdicts, present even on a clean audit: proportionality, complexity, priority, coverage, business-logic risk. A bad assessment must also raise a finding | standing_assessments | auditor |
| `coverage` | can each test actually fail? findings of kind `untested_behavior` or `vacuous_test` (a test that passes with the fix reverted), with command output as evidence; they count toward the verdict like findings | coverage[] | auditor |
| `warnings` | what this review could NOT cover: unchecked paths, unavailable runtime. The honesty valve for coverage | warnings[] | auditor |
| `dispositions` | one response per prior finding, no silence: fixed (commit) / refuted (ground truth) / out-of-scope / accepted (tradeoff) / deferred (tracked) | dispositions[] | coder (after an audit) |

## Named assemblies (default checkbox sets)

- **investigator default**: summary, findings, blockers
- **audit default** (the truss format): summary, findings, standing_assessments,
  warnings, blockers
- custom: pass any key list in the plan's `requested_sections`

## Presentation order (model-chosen, mechanically sanitized)

The report writer also asks the producing model for `sections_order`: the keys it
produced, in the order a reader should meet them (lead with what happened and what
to do; reference material last). The renderer honors it after mechanical
sanitation (`resolve_order`): unknown keys dropped, duplicates collapsed,
produced-but-unlisted sections appended in assembly order, and run_stats never
ordered (it is stamped at the end, not authored). Artifacts without the field
render in assembly order. Findings always render once, as severity-grouped
narrative blocks (truss shape): no parallel table repeating the same titles.

## Named formats vs sections

The truss audit report is the audit default assembly expressed in a specific order
(context paragraph, findings grouped by severity, standing assessments, warnings,
machine block). The section list is the architecture; the truss layout is one
rendering of it. Rendering lives in `rework report` / `rework report --md` and the
PR body builder (`orchestrator/engine.py::_pr_body`), all mechanical consumers of
the same artifact.

## Lineage and rules

- Findings/severity/CONFIRMED-PLAUSIBLE, dispositions, and warnings come from the
  owner's `roowus/truss` pr-audit format (ADR 0003).
- Blockers and honest-state language come from `roowus/workflow` schema contracts.
- Rules that are enforced, not hoped for: the auditor never sees the coder's
  self-assessment on its first pass; dispositioned findings are not re-raised
  without new evidence; verdicts map mechanically (critical: request changes,
  high: approve with fixes, else approve); run_stats is ledger-truth.

## Known gap (pending mechanical verification)

The truss header line "build green on this head" requires the ORCHESTRATOR to run
the suite itself. Currently the coder self-checks and the auditor may run tests,
but there is no orchestrator-run verification pass. Until it lands (M3), auditors
state verification coverage in `warnings` instead of asserting build status.
