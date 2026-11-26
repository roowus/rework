"""The daemon's command API (ADR 0021): every owner action as one HTTP call.

Front ends (the TUI, the CLI, a second TUI on another machine) send commands
here instead of writing the database themselves, so there is ONE control plane:
the daemon validates and executes, and a client renders what it gets back.
"""
