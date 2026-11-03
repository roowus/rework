"""Tests for the toy calculator: mul passes, add exposes the planted bug."""

from calc import add, mul


def test_add():
    assert add(2, 3) == 5


def test_mul():
    assert mul(3, 4) == 12
    assert mul(-2, 5) == -10
