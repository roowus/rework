# ADR 0016: An issue tracker feeds missions; it does not track them

## Status

Accepted (2026-11-24). Off unless configured.

## Context

rework has no place to put work that has not become a mission. Missions carry a priority and
a project label, and each one leaves a report, but "these twenty things are candidates,
these three are next, this one is blocked on something outside the repo" has no home.
An issue tracker is the right tool for that list, and the owner runs one.

The question this ADR settles is narrower than "integrate a tracker": it is which direction
the data flows, and which system is allowed to be the source of truth for what happened.

## Decision

**Pull, never push.** The daemon polls the tracker's REST API on its own cadence. Two
facts about the deployment made this the only workable choice: the Plane API container
cannot reach the host (`EHOSTUNREACH` from `plane-app_default` to the bridge gateway), and
Plane's own SSRF validator blocks `100.64.0.0/10`, the Tailscale range, so a webhook to the
daemon's tailnet address is rejected when it is created. A poller needs neither a route nor
an allowlist entry, and it matches rework's rule that every decision re-derives from rows.

**The tracker decides WHAT; rework decides HOW.** A work item in a configured state with a
configured label becomes a mission. Both filters are optional and both are configuration,
so "what runs" stays a human decision made in the tracker's UI.

**One comment back, and nothing else.** When a mission finishes, the bridge comments on the
work item with the outcome, the summary and the PR URL. Mission status is never written to
the tracker as truth: the store is the truth, and a second system claiming to know it is
how these bridges rot.

**Idempotent by construction.** The mission id is derived from the work item id
(`m-plane-<hash>`), and an existing mission is skipped, so polling twice or restarting
mid-poll creates nothing twice. That is the same rule the run-id scheme uses.

**Failure is a log line.** A listing failure is retried on the next tick; a single
unreadable item is skipped; a write-back failure never touches a finished mission. The
poller runs as its own bounded task, never on the dispatch loop, so a hung tracker costs
one tick and no more.

## Alternatives considered

- **Webhooks.** Ruled out by the two facts above, and a webhook stream is a worse recovery
  model than polling rows.
- **A `source` column on `missions`.** Cleaner in theory, but it is a schema change to both
  stores plus the light-column list for an optional bridge. The pointer rides in
  `result["source"]`, which `_synthesize` carries like the delivery keys.
- **Linear, GitHub Issues, Jira.** The client surface is three methods (`list_ready`,
  `comment`, `close`), so another tracker is a second implementation of that interface,
  not a second design. Plane came first because the owner runs it.
- **Putting reusable agent context in the tracker.** Wrong home: the Library (MCP) already
  does that, and a brief can point at a work item for background.

## Consequences

- The tracker and rework each do one job, and neither claims the other's.
- A mission created by the bridge has no plan (the bridge cannot know which playbook fits),
  so the daemon gives it the same default a bare `rework new` gets: an orchestrated fix.
- The bridge is off unless `REWORK_PLANE_TOKEN` and friends are set, so a deployment that
  does not want it pays nothing.
- A tracker outage delays new missions; it cannot fail a finished one.

## Amendment (2026-11-26): GitHub Issues, and sources are a registry

`sources.configured()` lists every source the daemon polls; each runs on its own cadence
and is off unless its env is set. GitHub Issues joined Plane: `REWORK_GITHUB_REPO=owner/name`
turns it on, ready = open issues with `REWORK_GITHUB_LABEL` (default `rework`), the mission
targets the issue's own repo, and it talks through the `gh` CLI (no new token). The
write-back picks its client from the mission's recorded source kind. Verified live: a
labelled issue on a throwaway private repo became a mission on a real daemon, ran, and the
daemon commented the result back on the issue.

When a tracker is a TOOL instead (an agent reading or editing the backlog mid-mission) it
belongs behind the tracker's own MCP server, opt-in per mission, writes behind gates; the
source stays the default integration.

