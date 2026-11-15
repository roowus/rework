"""Mission search: the dashboard's text filter and the ✦ AI ranking.

Pure functions, no Textual and no store: the dashboard passes in Mission rows
(anything with id/objective/title/project_id/status) and gets rows back, so the
matching rules are unit-testable without mounting an app.

Two stages:
  * `search_missions` — the live filter. Case-insensitive and STRICT: every
    whitespace separated token must be a plain substring somewhere in the
    mission's id/title/objective/project/status. There is no fuzzy fallback:
    a subsequence match over a long objective made two-letter queries match
    every mission (m7 finding #44), so a query only ever shows real hits.
  * the AI ranking — `build_ai_prompt` + `parse_ai_ranking` (pure) around
    `ai_search`, which asks a cheap model to order the most plausible missions
    and explain each in one line.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

#: how many missions ever go into an AI prompt (each carries one run line)
MAX_CANDIDATES = 40
#: how many ranked missions the model may return
MAX_RESULTS = 5
#: clip for the objective / run-output lines in a prompt (chat.py precedent)
SUMMARY_CLIP = 100
#: clip for a returned reason, so one chatty line cannot blow up the row list
REASON_CLIP = 120
#: the whole AI search (summary fetch + model call) gives up after this
TIMEOUT_S = 20.0
#: JSON for five ids is short; a small cap keeps a chatty model cheap and fast
MAX_TOKENS = 400

SYSTEM_PROMPT = (
    "You rank rework missions against a search query. Answer with a JSON array only: "
    '[{"id": "<mission id>", "reason": "<one short line>"}], best match first, at most '
    f"{MAX_RESULTS} entries. Use only ids from the list you are given; never invent one."
)


class AISearchError(RuntimeError):
    """A failed AI search whose message is safe to show in the UI as-is."""


# -- text search ---------------------------------------------------------------


def _text(value: object) -> str:
    """Field as text: enums (MissionStatus) render as their value, None as ""."""
    return str(getattr(value, "value", value) or "")


def _fields(mission: Any) -> tuple[str, ...]:
    return (
        _text(getattr(mission, "id", "")),
        _text(getattr(mission, "title", "")),
        _text(getattr(mission, "objective", "")),
        _text(getattr(mission, "project_id", "")),
        _text(getattr(mission, "status", "")),
    )


def token_matches(token: str, fields: Sequence[str]) -> bool:
    """A token hits when it is a plain substring of any of the mission's fields.
    Nothing more forgiving than that: no subsequence, no scoring."""
    return any(token in field for field in fields)


def search_missions(missions: Iterable[Any], query: str) -> list[Any]:
    """The missions genuinely matching `query` — every whitespace token must be
    a substring of some field. Empty query = everything, in the order given;
    hits keep the caller's order (no ranking, no near-misses)."""
    tokens = [t for t in str(query or "").lower().split() if t]
    if not tokens:
        return list(missions)
    hits = []
    for mission in missions:
        fields = [f.lower() for f in _fields(mission)]
        if all(token_matches(token, fields) for token in tokens):
            hits.append(mission)
    return hits


# -- AI ranking: prompt, summaries, parsing ------------------------------------


def summarize(mission: Any, runs: Sequence[Any] = ()) -> dict[str, str]:
    """One compact line-set per mission for the prompt: id, project, status,
    objective and the newest run output that has one (chat.py:266-281 does the
    same extraction from Run.artifact)."""
    output = ""
    for run in reversed(list(runs)):
        artifact = getattr(run, "artifact", None) or {}
        if not isinstance(artifact, dict):
            continue
        line = str(artifact.get("summary") or artifact.get("kind") or "").strip()
        if line:
            output = _clip(line, SUMMARY_CLIP)
            break
    return {
        "id": _text(getattr(mission, "id", "")),
        "project": _text(getattr(mission, "project_id", "")),
        "status": _text(getattr(mission, "status", "")),
        "objective": _clip(
            _text(getattr(mission, "objective", "")) or _text(getattr(mission, "title", "")),
            SUMMARY_CLIP,
        ),
        "output": output,
    }


def candidates(missions: Sequence[Any], query: str, limit: int = MAX_CANDIDATES) -> list[Any]:
    """Which missions the AI ranks. Under the cap: all of them. Over it: the
    text search's hits when it has any (so a phrased query still reaches the
    right mission), else the most recent `limit`."""
    missions = list(missions)
    if len(missions) <= limit:
        return missions
    hits = search_missions(missions, query)
    if hits:
        return hits[:limit]
    return sorted(missions, key=lambda m: float(getattr(m, "created_at", 0) or 0), reverse=True)[
        :limit
    ]


def build_ai_prompt(
    query: str, summaries: Sequence[dict[str, str]], limit: int = MAX_RESULTS
) -> str:
    """The user turn for the ranking model: the query plus one compact line per
    mission. Capped at MAX_CANDIDATES missions so the prompt stays small."""
    lines = [f"Query: {_clip(query, 200)}", "", "Missions:"]
    for summary in list(summaries)[:MAX_CANDIDATES]:
        project = summary.get("project") or "(none)"
        status = summary.get("status") or "?"
        objective = _clip(summary.get("objective", ""), SUMMARY_CLIP)
        line = f"- id={summary.get('id', '')} project={project} status={status} {objective}"
        output = _clip(summary.get("output", ""), SUMMARY_CLIP)
        if output:
            line += f" | last run: {output}"
        lines.append(line)
    lines += ["", f"Return at most {limit} missions as JSON only, best first."]
    return "\n".join(lines)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _json_candidates(text: str) -> Iterable[str]:
    """The whole reply first, then the outermost JSON array/object inside it."""
    yield text.strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            yield text[start : end + 1]


def _load(text: str) -> Any:
    for chunk in _FENCE.findall(text) or [text]:
        for candidate in _json_candidates(chunk):
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    return None


def parse_ai_ranking(text: str, known_ids: Iterable[str]) -> list[dict[str, str]]:
    """The ranked missions in `text`, tolerating code fences and prose around
    the JSON. Unknown ids and duplicates are dropped, reasons clipped, and a
    reply that is not usable JSON at all yields []."""
    known = {str(i) for i in known_ids}
    payload = _load(str(text or ""))
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("missions")
    if not isinstance(payload, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        mission_id = str(item.get("id") or "").strip()
        if mission_id not in known or mission_id in seen:
            continue
        seen.add(mission_id)
        out.append({"id": mission_id, "reason": _clip(str(item.get("reason") or ""), REASON_CLIP)})
        if len(out) >= MAX_RESULTS:
            break
    return out


def build_ai_request(model: str, query: str, missions_summaries: Sequence[dict[str, str]]) -> Any:
    """The ranking request on its own, so a caller that has already spent part of
    the budget can hand the remaining time to `run_ai_search`."""
    from rework.kernel.llm.types import ChatRequest

    return ChatRequest(
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_ai_prompt(query, missions_summaries)}],
        max_tokens=MAX_TOKENS,
        temperature=0,
    )


async def run_ai_search(
    provider: Any,
    request: Any,
    missions_summaries: Sequence[dict[str, str]],
    timeout: float = TIMEOUT_S,  # noqa: ASYNC109 - an explicit budget, not asyncio's
) -> list[dict[str, str]]:
    """Run a prebuilt request under `timeout` (the caller's remaining budget)."""
    try:
        result = await asyncio.wait_for(provider.complete(request), timeout)
    except TimeoutError:
        budget = f" after {timeout:.0f}s" if timeout >= 1 else ""
        raise AISearchError(f"AI search timed out{budget}") from None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # every provider failure is displayable, never fatal
        raise AISearchError(f"AI search failed: {str(exc)[:160] or type(exc).__name__}") from exc
    known = [summary["id"] for summary in missions_summaries]
    return parse_ai_ranking(getattr(result, "text", "") or "", known)


async def ai_search(
    provider: Any,
    model: str,
    query: str,
    missions_summaries: Sequence[dict[str, str]],
    timeout: float = TIMEOUT_S,  # noqa: ASYNC109 - an explicit budget, not asyncio's
) -> list[dict[str, str]]:
    """Ask `provider` to rank the summarized missions; returns [{"id","reason"}].

    Raises `AISearchError` with a displayable message on timeout or any
    provider failure, so a flaky endpoint can never take the dashboard down."""
    request = build_ai_request(model, query, missions_summaries)
    return await run_ai_search(provider, request, missions_summaries, timeout)


def pick_entry(catalog: Any) -> Any:
    """The cheapest catalog entry this process can actually call (cheapest()
    restricted to has_credentials()). None = AI search is unavailable."""
    usable = [entry for entry in catalog.all() if entry.has_credentials()]
    if not usable:
        return None
    return min(usable, key=lambda e: (e.tier, e.cost_in_per_mtok + e.cost_out_per_mtok))


def _clip(text: str, width: int) -> str:
    """One line, at most `width` chars, with an ellipsis (no rich markup)."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"
