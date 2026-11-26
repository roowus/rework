"""The TUI's offline fallback cache is bounded (audit 2026-11-25, 4.3).

Several query keys carry a moving cursor (`events:{mission}:{since}`,
`events_after:{seq}`), so the unbounded dict kept every event batch a long-
running TUI ever read. It is an LRU of LAST_GOOD_MAX entries now, and the
offline fallback still serves the most recent results.
"""

from __future__ import annotations

from rework.tui.data import LAST_GOOD_MAX, TuiData


async def test_cursor_keys_do_not_grow_the_cache_without_bound() -> None:
    data = TuiData(store_factory=lambda: None)
    for seq in range(LAST_GOOD_MAX * 4):

        async def call(seq: int = seq) -> list[int]:
            return [seq]

        await data._resilient(f"events_after:{seq}", call)
    assert len(data._last_good) == LAST_GOOD_MAX
    # the newest results are the ones kept (what the offline view falls back to)
    newest = f"events_after:{LAST_GOOD_MAX * 4 - 1}"
    assert data._last_good[newest] == [LAST_GOOD_MAX * 4 - 1]
    assert "events_after:0" not in data._last_good


async def test_a_reused_key_stays_fresh() -> None:
    data = TuiData(store_factory=lambda: None)

    async def missions() -> list[str]:
        return ["m-1"]

    await data._resilient("missions", missions)
    for seq in range(LAST_GOOD_MAX + 10):

        async def call(seq: int = seq) -> int:
            return seq

        await data._resilient(f"tail:{seq}", call)
        if seq % 50 == 0:
            await data._resilient("missions", missions)  # polled every tick
    assert "missions" in data._last_good, "a key in steady use must not be evicted"
