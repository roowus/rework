"""The animated "working" marker: a shared clock, not a stored counter.

Every surface that animates rebuilds its markup from `frame_for(time)`, so a
store poll that replaces all row markup cannot reset or clobber the frame.
Pure functions only: no Textual imports, unit-testable in isolation.

Zombie rule: a run whose status still says running but which has published no
event for `STALL_SECONDS` is NOT animated. It shows a static "idle 7m" instead,
because a frozen spinner is exactly what the owner must be able to tell apart
from a working mission.
"""

from __future__ import annotations

# Braille spinner, the classic 10 frames. EVERY frame is one cell wide (a single
# codepoint with no wide/zero-width property), so the glyph swap never reflows
# the row it sits in.
FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# One frame per tick at the app's 10Hz interval, so a full cycle takes 1s.
SECONDS_PER_FRAME = 0.1

# A running thing silent for this long is treated as stalled (probably dead).
STALL_SECONDS = 300.0

# Plain-text marker for a stalled run. One cell wide, like a frame.
IDLE_GLYPH = "◌"


def frame_for(t: float) -> str:
    """The frame shown at monotonic time `t`. Deterministic: same t, same frame.

    Negative times are fine (they just index backwards modulo the set)."""
    return FRAMES[int(t / SECONDS_PER_FRAME) % len(FRAMES)]


def is_stalled(now: float, last_event: float | None) -> bool:
    """True when a running thing has been silent for at least STALL_SECONDS.

    `last_event` is a wall-clock epoch (the `ts` of the newest event, or a run's
    `updated_at`); `now` is `time.time()`. A missing timestamp is NOT stalled:
    nothing has been observed yet, so there is nothing to declare dead."""
    if last_event is None:
        return False
    return (now - float(last_event)) >= STALL_SECONDS


def silence(now: float, last_event: float | None) -> float:
    """Seconds since the last event, never negative. 0.0 when unknown."""
    if last_event is None:
        return 0.0
    return max(0.0, now - float(last_event))


def human_duration(seconds: float) -> str:
    """Compact age: "12s", "7m", "2h 05m", "3d 4h".

    Deliberately shorter than theme.ago (no " ago" suffix) because it is
    appended to a "working ·" or "idle" label; theme.ago is for standalone
    timestamps."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"
