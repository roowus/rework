"""A mission gets a short title without any model call: `rework new` must not
pay (or wait) to name a mission, and the PR title / `rework ls` / commit
subject all come from it. Found live: PR #24 was titled with the whole first
line of the objective ("rework: Follow-up to #22 (just merged, NOT deployed to
rewvis until this lands)...")."""

from __future__ import annotations

import pytest

from rework.missions import derive_title

# the real objective of PR #24's mission
PR24 = (
    "Follow-up to #22 (just merged, NOT deployed to rewvis until this lands):"
    " role_env gives every role a scratch rework config, but the temp dirs"
    " pile up. Make gc reclaim them."
)


def test_the_pr24_objective_yields_a_short_single_line_title() -> None:
    title = derive_title(PR24)
    assert title is not None
    assert "\n" not in title
    assert "…" not in title and "..." not in title
    assert len(title.split()) <= 8
    assert len(title) <= 60
    assert not title.endswith((".", "!", "?", ",", ":"))
    assert title.startswith("Follow-up to #22")


def test_a_multiline_objective_uses_the_first_non_empty_line() -> None:
    objective = "\n\n  fix the planted bug\n\nIt returns the wrong value.\n"
    assert derive_title(objective) == "fix the planted bug"


def test_enumerator_quotes_and_markdown_are_stripped() -> None:
    # "(1) " enumerator, then the objective's own quoting/markdown, stripped
    assert derive_title('(1) "rework update" is broken.') == "rework update is broken"
    assert derive_title("(1) 'rework ls' spills rows") == "rework ls spills rows"
    gc_title = derive_title("- `rework gc` must reclaim scratch dirs")
    assert gc_title == "rework gc must reclaim scratch dirs"
    assert derive_title("2. **Add a --title option** to new") == "Add a --title option to new"
    assert derive_title("\u2022 bullet first") == "bullet first"
    # a sentence break still wins over the rest of the line
    assert derive_title("(1) rework ls. it spills rows") == "rework ls"
    # quotes INSIDE the text are part of the text, not decoration
    assert derive_title("the 'rework update' command spills rows") == (
        "the 'rework update' command spills rows"
    )


def test_only_the_first_sentence_survives() -> None:
    assert derive_title("Fix the ls column. It spills across rows.") == "Fix the ls column"
    assert derive_title("Fix the ls column! It spills.") == "Fix the ls column"


def test_the_title_is_capped_by_words_and_chars() -> None:
    title = derive_title("one two three four five six seven eight nine ten eleven twelve thirteen")
    assert title == "one two three four five six seven eight"
    long_word = derive_title("x" * 200)
    assert long_word is not None and len(long_word) <= 60


def test_an_unclosed_parenthesis_is_not_left_dangling() -> None:
    assert derive_title(PR24) == "Follow-up to #22"


@pytest.mark.parametrize("objective", ["", "   ", "\n\n\t\n", "  \n  \n"])
def test_nothing_usable_returns_none(objective: str) -> None:
    assert derive_title(objective) is None


def test_punctuation_only_returns_none() -> None:
    assert derive_title("...") is None
