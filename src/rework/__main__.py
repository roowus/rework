"""`python -m rework` — the same entry point as the `rework` console script.

It exists for the self-reload: after the TUI exits to load new code, `run_tui`
re-execs the process and needs a form that does not depend on how this one was
launched (`uv run rework` execs a console script). Without it the fallback
`[sys.executable, "-m", "rework", ...]` would fail with "No module named
rework.__main__"."""

from rework.cli import main

if __name__ == "__main__":
    main()
