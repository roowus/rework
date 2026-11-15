"""The `draft` playbook: document work with no PR gate.

Two things must hold for it to be usable at all: the orchestrator is actually
told about it (the system prompt AND the tool enum the model must satisfy), and
it produces NO open_pr gate -- a writing mission's deliverable is the project's
files, so pushing a branch and opening a PR would be wrong behaviour, not just
unnecessary.
"""

from __future__ import annotations

import pytest

from rework.missions import PLANS, PLAYBOOK_GATES, orchestrated_plan
from rework.orchestrator.agent import PLAYBOOKS
from rework.orchestrator.chat import PROPOSE_TOOL, SYSTEM_PROMPT


def test_draft_is_a_known_playbook() -> None:
    assert "draft" in PLAYBOOKS
    assert "draft" in PLANS
    assert "draft" in PLAYBOOK_GATES


def test_draft_text_instructs_research_draft_review_revise() -> None:
    text = PLAYBOOKS["draft"].lower()
    assert "web_search" in text and "fetch_url" in text, "roles must research with the web tools"
    assert "source" in text, "every claim must be recorded against a citation"
    assert "auditor" in text, "the draft is reviewed before it counts"
    assert "constraint" in text or "length" in text, "stated constraints are checked"
    assert "invent" in text or "assum" in text, "details about the owner must not be guessed"
    assert "revise" in text, "audit findings loop back into a revision"
    assert "ask_owner" in text, "facts only the owner knows are asked, not guessed"


def test_draft_text_says_no_push_no_pr() -> None:
    text = PLAYBOOKS["draft"]
    assert "NO BRANCH PUSH AND NO PR" in text
    assert "files" in text


def test_draft_yields_no_open_pr_gate() -> None:
    assert PLAYBOOK_GATES["draft"] == []
    plan = orchestrated_plan("draft", "pro")
    assert plan["playbook"] == "draft"
    assert plan["gates"] == [], "a writing mission must not offer to open a pull request"
    assert "open_pr" not in plan["gates"]
    assert "ship" not in plan["gates"], "there is no diff to ship"


def test_fix_pr_still_gates_on_open_pr() -> None:
    """Negative control for the assertion above: the PR gate still exists."""
    assert orchestrated_plan("fix-pr", "pro")["gates"] == ["open_pr"]
    assert orchestrated_plan("fix", "pro")["gates"] == ["ship"]


def _plan_listing() -> list[str]:
    """The 'Plans you can choose' entries, continuation lines folded in."""
    lines = SYSTEM_PROMPT.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Plans you can choose"))
    entries: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        if line.startswith("- "):
            entries.append(line[2:])
        elif entries:
            entries[-1] += " " + line.strip()
    return entries


def test_chat_prompt_lists_draft_with_a_hint() -> None:
    entries = _plan_listing()
    draft = next((e for e in entries if e.startswith("draft")), "")
    assert draft, f"the chat must be told draft exists: {entries}"
    assert "writing" in draft, f"the hint must say what draft is for: {draft!r}"
    assert "not code" in draft
    # and the negative control: the other plans are still listed
    names = {e.split()[0] for e in entries}
    assert names == set(PLANS), f"plan listing drifted from PLANS: {names}"


def test_chat_proposal_tool_accepts_draft() -> None:
    enum = PROPOSE_TOOL.input_schema["properties"]["plan"]["enum"]
    assert "draft" in enum
    assert set(enum) == set(PLANS), "the tool enum and the accepted plans must not drift"


@pytest.mark.parametrize("playbook", sorted(PLAYBOOKS))
def test_every_playbook_has_a_gate_entry(playbook: str) -> None:
    """A missing entry silently falls back to 'no gates' -- the bug that would
    make `draft` produce PR behaviour if the tables ever diverge."""
    assert playbook in PLAYBOOK_GATES
    assert set(PLAYBOOK_GATES) == set(PLAYBOOKS)
