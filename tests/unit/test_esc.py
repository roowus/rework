"""esc() must make ANY text safe for Textual markup and round-trip exactly
(found live: textual.markup.escape let '["git", "-c", ...]' through, and the
TUI broke on mission cards, in the chat and in the Gates view)."""

import pytest
from textual.content import Content

from rework.tui.theme import esc

CASES = [
    '["git", "-c", "user.name=rework", "commit", "-m", "init"]',
    "[n], edge_to_next=GEdge()) for n in nodes]",
    "path\\[0] and \\",
    "a\\",
    "[b]bold?[/b]",
    "[@click=x]y[/]",
    "\\[already]",
    "x[/]y",
    "[$var]",
    "plain text",
    "",
]


@pytest.mark.parametrize("text", CASES)
def test_esc_round_trips_through_textual_markup(text: str) -> None:
    assert Content.from_markup(esc(text)).plain == text


def test_esc_inside_markup_keeps_the_markup() -> None:
    content = Content.from_markup(f"[b]{esc('["git", "-c"]')}[/b]")
    assert content.plain == '["git", "-c"]'
