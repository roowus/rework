"""The role environment is an allowlist (ADR 0009), not an inheritance.

These tests are the channel's regression lock. The one that matters most is
`test_role_command_cannot_see_provider_keys_or_db`: a role command that can read
`REWORK_*_KEY` or `REWORK_DB` from `env` can exfiltrate every credential the
daemon holds, which is exactly what the old `dict(os.environ)` copy allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.nodes.tools import (
    BASELINE_EXACT,
    allowed_names,
    load_allowlist,
    role_env,
    role_env_file,
)

pytestmark = pytest.mark.usefixtures("_isolated_role_env")


@pytest.fixture
def _isolated_role_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the owner's file at a temp path so a real ~/.rework/role_env.toml on
    a dev machine cannot change the result, and clear the mtime cache."""
    path = tmp_path / "role_env.toml"
    monkeypatch.setenv("REWORK_ROLE_ENV_FILE", str(path))
    import rework.nodes.tools as tools

    tools._allow_cache.clear()
    yield path
    tools._allow_cache.clear()


def test_role_env_has_baseline_names_and_drops_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/rewis")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "rework")
    monkeypatch.setenv("SOME_STRAY_VAR", "leak me not")

    env = role_env()

    for name in ("PATH", "HOME", "LANG", "LC_ALL", "GIT_AUTHOR_NAME"):
        assert name in env, f"{name} is on the baseline and must survive"
    assert "SOME_STRAY_VAR" not in env
    assert set(env) <= BASELINE_EXACT | {
        n for n in env if n.startswith(("LC_", "GIT_AUTHOR_", "GIT_COMMITTER_"))
    }


def test_role_command_cannot_see_provider_keys_or_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0009's named test. A role shell that can read these can exfiltrate
    every credential the daemon holds."""
    secrets = {
        "REWORK_FIREWORKS_KEY": "fw-secret",
        "REWORK_DEEPSEEK_KEY": "ds-secret",
        "REWORK_DEEPINFRA_KEY": "di-secret",
        "REWORK_ZAI_KEY": "zai-secret",
        "REWORK_DB": "postgresql://user:pw@host/db",
        "REWORK_TOKEN": "bearer-secret",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-openai-secret",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    env = role_env(Path("/tmp"))

    leaked = sorted(set(secrets) & set(env))
    assert leaked == [], f"these secrets reached the role environment: {leaked}"
    # and nothing that merely looks like a secret arrives by another name
    assert not [n for n in env if n.endswith(("_KEY", "_TOKEN", "_SECRET"))]


def test_role_env_baseline_names_pass_everything_else_dropped() -> None:
    """A direct table check, so adding a variable to the baseline is a deliberate
    edit to this list rather than an accident."""
    assert {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ"} <= BASELINE_EXACT
    assert {"LANG", "LANGUAGE", "TERM"} <= BASELINE_EXACT
    assert {"UV_CACHE_DIR", "PYTHONIOENCODING"} <= BASELINE_EXACT
    forbidden = {
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
        "REWORK_DB",
        "REWORK_TOKEN",
        "LD_PRELOAD",
    }
    assert not (forbidden & BASELINE_EXACT), "these must never be on the baseline"


def test_role_env_owner_list_exact_and_prefix_entries(
    _isolated_role_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_role_env.write_text(
        'allow = ["SSH_AUTH_SOCK", "UV_INDEX_*", "REWORK_TEST_POSTGRES"]\n'
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("UV_INDEX_URL", "https://pypi.example/simple")
    monkeypatch.setenv("REWORK_TEST_POSTGRES", "postgresql://test")
    monkeypatch.setenv("UV_OTHER", "must not pass")

    env = role_env()

    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock", "an explicit owner grant is honoured"
    assert env["UV_INDEX_URL"] == "https://pypi.example/simple", "a prefix grant is honoured"
    assert env["REWORK_TEST_POSTGRES"] == "postgresql://test"
    assert "UV_OTHER" not in env


@pytest.mark.parametrize(
    "content",
    [
        "allow = 'not a list'\n",
        'allow = ["*"]\n',
        'allow = ["AB*"]\n',
        'allow = [""]\n',
        "allow = [1, 2]\n",
        "this is not toml\n",
        "allow = [\n",
    ],
)
def test_role_env_has_no_fallthrough_when_the_owner_file_is_invalid(
    _isolated_role_env: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Every failure mode moves toward FEWER variables. A file that cannot be
    trusted is ignored as a whole, and the baseline still applies."""
    _isolated_role_env.write_text(content)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = role_env()

    assert "SSH_AUTH_SOCK" not in env, "a bad file must not widen the allowlist"
    assert env["PATH"] == "/usr/bin", "the baseline still applies"


def test_role_env_allowlist_applies_to_a_repo_without_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0009 point (c1): the old early return handed a plain repo the whole
    daemon environment. The allowlist now applies to every repo shape."""
    (tmp_path / "README.md").write_text("toy repo\n")
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "fw-secret")

    env = role_env(tmp_path)

    assert "REWORK_FIREWORKS_KEY" not in env


def test_role_env_scratch_redirects_are_set_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five scratch paths are assigned, so a stray REWORK_LOG_DIR in the
    daemon's environment cannot redirect a run's state elsewhere."""
    monkeypatch.setenv("REWORK_LOG_DIR", "/somewhere/else")
    monkeypatch.setenv("REWORK_TARGETS", "/somewhere/else/targets.toml")

    env = role_env(tmp_path)

    assert env["REWORK_LOG_DIR"] != "/somewhere/else"
    assert env["REWORK_TARGETS"] != "/somewhere/else/targets.toml"
    assert str(tmp_path.resolve()) not in env["REWORK_LOG_DIR"]


def test_role_env_reloads_when_the_owner_file_changes(
    _isolated_role_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit applies to the next command with no daemon restart."""
    monkeypatch.setenv("MY_EXTRA", "1")
    assert "MY_EXTRA" not in role_env()

    _isolated_role_env.write_text('allow = ["MY_EXTRA"]\n')
    import os

    stamp = _isolated_role_env.stat().st_mtime + 1
    os.utime(_isolated_role_env, (stamp, stamp))
    import rework.nodes.tools as tools

    tools._allow_cache.clear()

    assert role_env()["MY_EXTRA"] == "1"


def test_allowed_names_reports_what_the_allowlist_keeps() -> None:
    sample = {"PATH": "/bin", "REWORK_DB": "x", "LC_TIME": "C"}
    assert allowed_names(sample) == {"PATH", "LC_TIME"}


def test_role_env_file_is_overridable_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_ROLE_ENV_FILE", "/tmp/some-role-env.toml")
    assert role_env_file() == Path("/tmp/some-role-env.toml")


def test_load_allowlist_missing_file_is_baseline_only(tmp_path: Path) -> None:
    exact, prefixes = load_allowlist(tmp_path / "nope.toml")
    assert (exact, prefixes) == (frozenset(), ())


def _git_repo(tmp_path: Path) -> Path:
    """A tiny repo with one commit. Synchronous on purpose: every caller here
    runs it before its first await (ruff ASYNC221)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


async def test_verify_red_green_spawn_uses_the_role_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second role-shaped spawn (`verify_red_green`'s shell) must not inherit
    the daemon's keys either.

    Built synchronously through `make_git_repo`, before the coroutine makes any
    await: a blocking subprocess.run after an await is what ruff's ASYNC221
    forbids, and the rule is right about the daemon even when a test gets away
    with it."""
    from rework.nodes.testing import verify_red_green

    repo = _git_repo(tmp_path)
    monkeypatch.setenv("REWORK_FIREWORKS_KEY", "fw-secret")

    results = await verify_red_green(
        repo,
        ['echo "key=[$REWORK_FIREWORKS_KEY] db=[$REWORK_DB]"'],
        [],
        "HEAD",
        env=role_env(repo, "m-test"),
    )

    assert results, "the command must have run"
    assert "key=[]" in results[0]["output"]
    assert "db=[]" in results[0]["output"]
