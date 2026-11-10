"""Commit subjects stay short (found live: the whole brief became the
squash-merge title of PR #5 on main)."""

from rework.roles.builtin.coder import SUBJECT_MAX, commit_message

BRIEF = (
    "In the TUI Cost tab (src/rework/tui/views/cost.py), add an 'active time' "
    "headline stat: the sum of run_stats.active_seconds over the runs. Add a test."
)


def test_the_mission_title_is_the_subject_and_the_brief_the_body() -> None:
    msg = commit_message("Cost tab active time stat", BRIEF, "code", "m:code:coder:1")
    subject, _, body = msg.partition("\n\n")
    assert subject == "Cost tab active time stat"
    assert BRIEF in body and "rework run: m:code:coder:1" in body


def test_without_a_title_the_first_sentence_is_capped() -> None:
    subject = commit_message(None, BRIEF, "code", "r").split("\n", 1)[0]
    assert len(subject) <= SUBJECT_MAX and subject.endswith("…")
    assert subject.startswith("In the TUI Cost tab")


def test_revisions_say_so() -> None:
    subject = commit_message("Fix calc add", "Fix the audit findings.", "revise", "r").split("\n")[
        0
    ]
    assert subject == "Fix calc add (revision)"
