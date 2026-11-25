"""The `role-env` CLI commands: the allowlist an owner can read and extend."""

from __future__ import annotations

import tomllib

import pytest
from typer.testing import CliRunner

from rework.cli import app


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = tmp_path / "role_env.toml"
    monkeypatch.setenv("REWORK_ROLE_ENV_FILE", str(path))
    import rework.nodes.tools as tools

    tools._allow_cache.clear()
    return path


def test_show_lists_the_baseline_and_the_owner_additions(isolated, monkeypatch):
    isolated.write_text('allow = ["SSH_AUTH_SOCK"]\n')
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "secret")

    out = CliRunner().invoke(app, ["role-env", "show"])
    text = out.stdout

    assert out.exit_code == 0
    assert "PATH" in text, "the baseline must be listed"
    assert "SSH_AUTH_SOCK" in text, "an owner grant must be listed"
    assert "REWORK_FIREWORKS_KEY" in text, "a dropped name is named, not silently omitted"


def test_show_never_prints_a_value(isolated, monkeypatch):
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "fw-SECRET-VALUE")

    out = CliRunner().invoke(app, ["role-env", "show"])

    assert "fw-SECRET-VALUE" not in out.stdout


def test_allow_appends_and_keeps_existing_entries(isolated):
    isolated.write_text('allow = ["SSH_AUTH_SOCK"]\n')
    runner = CliRunner()

    assert runner.invoke(app, ["role-env", "allow", "HTTPS_PROXY"]).exit_code == 0

    data = tomllib.loads(isolated.read_text())
    assert data["allow"] == ["SSH_AUTH_SOCK", "HTTPS_PROXY"]


def test_allow_is_idempotent(isolated):
    runner = CliRunner()
    runner.invoke(app, ["role-env", "allow", "HTTPS_PROXY"])
    out = runner.invoke(app, ["role-env", "allow", "HTTPS_PROXY"])

    assert "already allowed" in out.stdout
    assert tomllib.loads(isolated.read_text())["allow"] == ["HTTPS_PROXY"]


def test_allow_refuses_to_append_to_an_unreadable_file(isolated):
    """ "Append blindly to a file I cannot parse" would silently drop the owner's
    other grants, so the command refuses and says which file and why."""
    isolated.write_text("this is not toml\n")

    out = CliRunner().invoke(app, ["role-env", "allow", "HTTPS_PROXY"])

    assert out.exit_code != 0
    assert "fix it by hand" in out.stderr, f"reason must reach the owner, got {out.stderr!r}"
    assert isolated.read_text() == "this is not toml\n", "the bad file is left alone"


def test_a_new_grant_takes_effect_without_a_restart(isolated, monkeypatch):
    """The daemon re-reads on mtime, but the CLI's own cache must also drop the
    stale entry, or `role-env allow` followed by `role-env show` lies."""
    from rework.nodes.tools import role_env

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    assert "HTTPS_PROXY" not in role_env()

    CliRunner().invoke(app, ["role-env", "allow", "HTTPS_PROXY"])

    assert role_env()["HTTPS_PROXY"] == "http://proxy:3128"
