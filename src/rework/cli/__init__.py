"""rework CLI.

M0 ships `rework --help` and `rework version`. Commands land per milestone:
serve/new/ls/attach/approve (M1), status/roles (M4), eval (M3), library (M5).
"""

import typer

from rework import __version__

app = typer.Typer(
    name="rework",
    help=(
        "Portable multi-agent workflow engine: an orchestrator supervising role "
        "workflows, with a compounding cross-harness library."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def root() -> None:
    """rework: see `rework --help` for commands."""


@app.command()
def version() -> None:
    """Print the rework version."""
    typer.echo(f"rework {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
