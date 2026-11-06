"""Mechanical run-cost stats stamped onto report artifacts.

Objective data (tokens, cache hits, model calls, cost, elapsed time) comes from
the ledger and the run row, never from the model: the report-writer emits its
sections, then this helper injects `run_stats` as the bottom section.
"""

from __future__ import annotations

import time
from typing import Any

from rework.kernel.state.store import StateStore


async def attach_run_stats(
    store: StateStore, run_id: str, artifact: dict[str, Any]
) -> dict[str, Any]:
    charges = await store.run_charges(run_id)
    run = await store.get_run(run_id)
    # stamped at completion, so wall time is now - created (found live:
    # updated_at-based elapsed reported 0.1s for a 65-call run); the active
    # span (first to last model call) excludes pauses and restarts' downtime
    elapsed = max(0.0, time.time() - run.created_at) if run is not None else 0.0
    stamps = sorted(c.ts for c in charges)
    active = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
    artifact["run_stats"] = {
        "model_calls": len(charges),
        "input_tokens": sum(c.input_tokens for c in charges),
        "output_tokens": sum(c.output_tokens for c in charges),
        "cached_tokens": sum(c.cached_tokens for c in charges),
        "cost_usd": round(sum(c.cost_usd for c in charges), 6),
        "elapsed_seconds": round(elapsed, 1),
        "active_seconds": round(active, 1),
        "models": sorted({c.model for c in charges}),
    }
    return artifact
