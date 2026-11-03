"""M0 smoke: the CLI entrypoint imports, answers --help, and reports its version."""

from typer.testing import CliRunner

from rework import __version__
from rework.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "rework" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
