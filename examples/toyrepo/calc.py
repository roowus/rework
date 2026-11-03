"""Tiny calculator: the toy repo for rework's vertical-slice missions."""


def add(a, b):
    return a - b  # the planted bug: rework missions must find and fix this


def mul(a, b):
    result = 0
    for _ in range(abs(int(b))):
        result = add(result, a)
    return result if b >= 0 else -result
