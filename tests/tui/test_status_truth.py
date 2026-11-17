"""Status truth: an un-finished mission is not necessarily a working one.

`actions.ACTIVE` answers "is a cancel/resume verb meaningful, should polling and
enrichment continue". It used to answer "is work happening right now" too, so a
`pending` mission (nothing started) and an `on_hold` mission (stopped on
purpose) painted themselves as working: a role chip, an advancing elapsed clock,
an animation frame and a "live" tag. Every surface now derives both the set and
the marker from theme.glyph/status_text, so a surface cannot invent its own
character either.
"""

from __future__ import annotations

import re
import time
from typing import Any

from textual.app import App, ComposeResult

from rework.kernel.state.records import Mission, MissionStatus
from rework.tui import actions, dashboard_shim  # noqa: F401  (placeholder, removed below)
