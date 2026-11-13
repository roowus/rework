"""Unit tests for theme.esc() bracket escaping."""

from textual.content import Content

from rework.tui.theme import esc


def test_esc_roundtrip():
    """esc() must escape ALL brackets so Content.from_markup doesn't raise."""
    test_cases = [
        '["git", "-c", "user.name=rework", "commit", "-m", "init"]',
        "[n], edge_to_next=GEdge()) for n in nodes]",
        "path\\[0] and \\",
        "a\\",
        "[b]bold?[/b]",
        "[@click=x]y[/]",
        "\\[already]",
        "x[/]y",
        "[$var]",
    ]

    for original in test_cases:
        escaped = esc(original)
        # Must not raise MarkupError
        content = Content.from_markup(escaped)
        # Must round-trip exactly
        assert content.plain == original, f"Failed for {original!r}: got {content.plain!r}"
