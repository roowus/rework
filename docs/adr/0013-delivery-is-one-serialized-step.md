# ADR 0013: Delivery is one serialized step, and both push paths merge main

## Status

Accepted (2026-11-24).

## Context

Two things push. `delivery.deliver_pr` merges `origin/main` into the branch, then pushes
and opens the PR. `MissionRunner._deliver_branch` (the ship-gate path, ADR 0005) pushed
the branch and never merged at all. Neither takes a lock.

So two missions finishing together both merged against the main each one saw, both pushed
and both opened a PR. Each PR was "clean" against its own base and the owner rebased one by
hand. On the ship path there was no merge at all, so a ship-approved branch could arrive
already conflicting.

A process-local `asyncio.Lock` cannot fix this. `rework mission retry-pr` runs from a
laptop against the same database, and the TUI's Retry PR does the same, so the lock has to
live where both processes can see it.

## Decision

**One lock, in the store.** A `delivery_lock` table (one row, `owner`, `heartbeat_at`)
next to the existing leader lock, with the same atomic-upsert acquire. `serialized_delivery`
holds it across the WHOLE sequence: merge, push, PR create. Not one lock per step: the gap
between the merge and the push is exactly the window that produced the race.

**A dead holder is taken over in a few heartbeats, not a full TTL.** The holder heartbeats
every 20 seconds; a row untouched for three heartbeats is stale and can be taken at once.
The alternative (wait for a 120 second TTL) means a crashed delivery blocks the next one
for two minutes, which is a mission doing nothing.

**Waiting is bounded and never fatal.** If the lock cannot be taken in about four minutes,
delivery proceeds unserialized with a warning. Ordered-but-occasionally-unserialized beats
a mission hanging on a lock row.

**The ship path merges.** `_deliver_branch` calls `merge_main_before_push` under the same
lock, so the two delivery paths cannot drift apart again.

## Alternatives considered

- **A file lock.** Does not work across machines, which is the case that matters (the
  laptop retry).
- **Serialize per mission only.** The race is BETWEEN missions, so this does not help.
- **A merge queue (GitHub's).** The right answer when more than one host ships; recorded in
  ADR 0010 for the multi-host case. A single daemon needs one lock, not a queue.
- **Retry the push on a non-fast-forward.** Kept as the second line of defence, alongside
  the lock, through the existing `pr_error` / `pr_attempts` state.

## Consequences

- Two missions shipping together land in some order, both mergeable, with no manual rebase.
- A ship-approved branch arrives mergeable, like a PR-opened one.
- A crashed or killed daemon does not block delivery for longer than three heartbeats.
- The lock is best-effort: under sustained contention (four minutes) delivery proceeds
  regardless, so the guarantee is "ordered in practice" not "ordered always". That is a
  deliberate trade against hanging a finished mission.
