"""~/.rework/rework.env holds provider keys on each machine (rewvis's systemd
already loads it); rework reads it itself, never overriding the environment."""

import os
from pathlib import Path

import pytest

from rework.config import load_env_file


def test_the_env_file_fills_gaps_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "rework.env"
    f.write_text(
        "# provider keys\n"
        "REWORK_T_A=alpha\n"
        "export REWORK_T_B='quoted value'\n"
        'REWORK_T_C="dq"\n'
        "REWORK_T_SET=from-file\n"
        "not a line\n\n"
    )
    monkeypatch.setenv("REWORK_T_SET", "from-env")
    for k in ("REWORK_T_A", "REWORK_T_B", "REWORK_T_C"):
        monkeypatch.delenv(k, raising=False)
    added = load_env_file(f)
    try:
        assert sorted(added) == ["REWORK_T_A", "REWORK_T_B", "REWORK_T_C"]
        assert os.environ["REWORK_T_A"] == "alpha"
        assert os.environ["REWORK_T_B"] == "quoted value"
        assert os.environ["REWORK_T_C"] == "dq"
        assert os.environ["REWORK_T_SET"] == "from-env"  # the environment wins
    finally:
        for k in added:
            os.environ.pop(k, None)


def test_a_missing_file_is_fine(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == []
