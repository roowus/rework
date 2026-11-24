"""Where missions come from, other than a human typing one.

A source turns work items in another system into missions. It is deliberately
one-way and small: the tracker decides WHAT to work on (a list someone can read),
rework decides how, and one comment goes back when the mission finishes. The
tracker is never the source of truth for mission state; the store is (ADR 0016).
"""
