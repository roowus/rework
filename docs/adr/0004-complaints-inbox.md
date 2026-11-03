# ADR 0004: The complaints inbox (agent feedback on the workflow itself)

## Status

Proposed (design from the owner, 2026-11-03; slots after M3, building on the Library)

## Context

Evals measure role quality; hill-climbing improves it. What the system lacks is a
qualitative channel: the agents doing the work see the workflow's rough edges first
(an audit missing information it needed, a tool that kept failing, a prompt that
forced rework) but have nowhere to file that observation. The owner wants agents to
be able to complain about the workflow itself, centrally, so that stacked-up similar
complaints drive changes the way public opinion drives policy.

## Decision

1. **Complaints are typed, non-blocking artifacts.** Unlike escalations (which pause
   a mission until answered), a complaint is fire-and-forget feedback:

   ```
   Complaint {
     kind: workflow_gripe | needed_info | tool_missing | prompt_issue |
           bug_verified | process_issue
     source: {mission_id, run_id, role, step}
     summary, detail, evidence[], suggestion
     status: open | triaged | issue_open | resolved | wontfix
   }
   ```

2. **Filing paths.** A `file_complaint` tool every role carries (one call, no
   interruption), plus mechanical extraction from artifacts: an audit that raises
   `needed_info` findings, a coder whose handoff notes say "the audit lacked X", an
   escalation with a recurring kind.

3. **Central inbox, local first.** Complaints land in the store (a complaints table,
   queryable like everything else) and later surface in the Library's inbox, next to
   the todo side. The store is already the cross-machine substrate.

4. **Aggregation policy.** Similar complaints cluster (keyword/tag match first,
   embeddings when the Library gains them). Policy: N similar open complaints (or 1
   `bug_verified` with reproducible evidence) auto-opens a GitHub issue in
   roowus/rework with the cluster attached; the human works issues the usual way.
   Thresholds live in config, tuneable.

5. **The loop closes.** A complaint that becomes a code change gets its status set
   to resolved with the fixing commit; the eval suite gains the scenario that caused
   it where possible. Complaints are one of the Library's memory item sources: the
   lesson "auditors need the test output to review coverage" becomes a skill other
   missions retrieve.

## Consequences

- Roles gain one more tool and one more artifact field; the engine gains one table
  and one CLI surface (`rework complaints`), nothing structural.
- Noise is the risk: complaint quality needs curation like the Library's (tiered:
  auto-accept skill/process complaints, gate bug_verified until evidence checks
  pass). The disposition protocol already gives agents the habit of structured,
  evidenced claims.
- Where it slots: design lands with the Library (M5), because clustering and the
  inbox want the Library's search and item model; the complaints table and
  file_complaint tool can land earlier (M3.5) if live missions start producing
  complaints worth keeping.
