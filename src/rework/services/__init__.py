"""Owner-facing operations, shared by every front end (CLI, TUI, daemon API).

Each operation lives here once, so two front ends can no longer implement the
same verb two different ways (audit 2026-11-25, finding 3.6).
"""
