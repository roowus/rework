"""The rigid report-writer: takes the loop's transcript plus the section
assembly, demands a JSON artifact that validates against the section schema,
retries once on failure, and refuses to guess (blockers are always available
as the honest-out). The mechanical anti-collusion guarantee lives in the
caller: what goes INTO a downstream role's context is decided by the
orchestrator from these typed artifacts, not from prose.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from rework.kernel.llm.catalog import ModelCatalog, ModelProvider
from rework.kernel.llm.types import ChatRequest
from rework.kernel.state.records import Charge
from rework.kernel.state.store import StateStore
from rework.nodes.report_sections import ReportSection, build_report_prompt, build_report_schema

log = logging.getLogger("rework.nodes")

ReportWriterNode = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def make_report_writer_node(
    provider: ModelProvider,
    catalog: ModelCatalog,
    catalog_id: str,
    store: StateStore,
    mission_id: str,
    run_id: str,
    sections: list[ReportSection],
    temperature: float | None = None,
    goal: str = "",
) -> ReportWriterNode:
    entry = catalog.resolve(catalog_id)
    schema = build_report_schema(sections)

    async def one_shot(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        result = await provider.complete(
            ChatRequest(
                model=entry.model,
                system=(
                    (f"ROLE GOAL: {goal}\n\n" if goal else "")
                    + "You are the report-writer stage of an agent pipeline. You do"
                    " not investigate: everything you need is in the transcript."
                    " Emit ONLY the JSON artifact matching the schema: no prose"
                    " outside it. Where evidence is missing, use the blockers/"
                    "open-question field for that section instead of guessing."
                ),
                messages=messages,
                temperature=temperature,
                max_tokens=entry.max_output,
            )
        )
        await store.charge(
            Charge(
                mission_id=mission_id,
                run_id=run_id,
                model=entry.model,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=entry.estimate_cost(result.usage.input_tokens, result.usage.output_tokens),
            )
        )
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        try:
            artifact = json.loads(text)
        except ValueError as exc:
            return None, f"invalid JSON: {exc}"
        if not isinstance(artifact, dict):
            return None, "artifact is not an object"
        missing = [k for k in schema["required"] if k not in artifact]
        if missing:
            return None, f"missing required sections: {missing}"
        return artifact, ""

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        transcript = state.get("messages") or []
        transcript_text = "\n".join(
            f"[{m.get('role')}]"
            + (f" {m['content']}" if m.get("content") else "")
            + (
                " tool_calls: " + ", ".join(tc["name"] for tc in m.get("tool_calls", []))
                if m.get("tool_calls")
                else ""
            )
            for m in transcript
            if m.get("role") != "tool"
        )
        prompt = build_report_prompt(sections)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Produce the report artifact.\n\nReport sections:\n{prompt}\n\n"
                    f"JSON schema your output must satisfy:\n{json.dumps(schema)}\n\n"
                    f"Transcript of the work:\n{transcript_text[:60000]}"
                ),
            }
        ]
        artifact, err = await one_shot(messages)
        if artifact is None:
            log.warning("report-writer retry after: %s", err)
            messages.append({"role": "assistant", "content": f"(rejected: {err})"})
            messages.append(
                {
                    "role": "user",
                    "content": "Fix the artifact and emit the full corrected JSON only.",
                }
            )
            artifact, err = await one_shot(messages)
        if artifact is None:
            return {
                "artifact": None,
                "artifact_error": f"report failed validation twice: {err}",
            }
        from rework.nodes.run_stats import attach_run_stats

        return {"artifact": await attach_run_stats(store, run_id, artifact)}

    return node
