"""Modular report sections: the building blocks reports are assembled from
(ADR 0003 decision 4). Each section is one component: a prompt fragment telling
the model what to produce, a JSON-schema slice it must satisfy, and a renderer.
The caller's requested_sections picks them like checkboxes; an assembly is just
a list of keys. The truss audit format is the auditor's default assembly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReportSection:
    key: str
    title: str
    prompt_fragment: str  # instructions for the model producing this section
    schema_slice: dict[str, Any]  # json-schema properties this section requires
    required_fields: tuple[str, ...] = field(default_factory=tuple)


FINDINGS = ReportSection(
    key="findings",
    title="Findings",
    prompt_fragment=(
        "List every finding with concrete evidence. Each finding needs: title,"
        " detail (what and why), evidence (file:line references you actually"
        " read, never invented), severity (critical|high|medium|low), verdict"
        " (CONFIRMED = you traced the consequence; PLAUSIBLE = suspicion),"
        " confidence 0.0-1.0. Group by severity. If nothing is wrong, say so"
        " explicitly rather than inventing findings. Do not nitpick."
    ),
    schema_slice={
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "detail", "evidence", "severity", "verdict", "confidence"],
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "severity": {"enum": ["critical", "high", "medium", "low"]},
                    "verdict": {"enum": ["CONFIRMED", "PLAUSIBLE"]},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
    required_fields=("findings",),
)

BLOCKERS = ReportSection(
    key="blockers",
    title="Blockers",
    prompt_fragment=(
        "List every question you could NOT answer from evidence, as a blocker:"
        " question (one sentence), options (2+ real options so the answer can"
        " be one word), recommendation (which option and why),"
        " why_cannot_answer (what evidence was missing). An explicit blocker"
        " is always better than a guess."
    ),
    schema_slice={
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "options", "recommendation", "why_cannot_answer"],
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                    "recommendation": {"type": "string"},
                    "why_cannot_answer": {"type": "string"},
                },
            },
        }
    },
    required_fields=("blockers",),
)

SUMMARY = ReportSection(
    key="summary",
    title="Summary",
    prompt_fragment="One paragraph: what was examined and the bottom line.",
    schema_slice={"summary": {"type": "string"}},
    required_fields=("summary",),
)

SUGGESTED_FIX = ReportSection(
    key="suggested_fix",
    title="Suggested fix",
    prompt_fragment=(
        "A concrete fix approach per actionable finding (approach, not the"
        " fix itself; name files and functions, note risks)."
    ),
    schema_slice={
        "suggested_fix": {"type": "string"},
    },
    required_fields=("suggested_fix",),
)

PROPOSED_TESTS = ReportSection(
    key="proposed_tests",
    title="Proposed tests",
    prompt_fragment=(
        "Tests that would prove the fix or answer the open questions: name,"
        " what it exercises, how (exact command or file to add)."
    ),
    schema_slice={
        "proposed_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "exercises"],
                "properties": {"name": {"type": "string"}, "exercises": {"type": "string"}},
            },
        }
    },
    required_fields=("proposed_tests",),
)

RULED_OUT = ReportSection(
    key="ruled_out",
    title="Ruled out",
    prompt_fragment=(
        "Areas examined and found clean, with the evidence that rules them"
        " out, so downstream roles do not re-investigate the same ground."
    ),
    schema_slice={
        "ruled_out": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["area", "evidence"],
                "properties": {"area": {"type": "string"}, "evidence": {"type": "string"}},
            },
        }
    },
    required_fields=("ruled_out",),
)

CLASSIFICATION = ReportSection(
    key="classification",
    title="Classification",
    prompt_fragment=(
        "Judge the work itself: complexity (trivial|low|medium|high) and"
        " priority (critical|high|medium|low) with one-line justifications"
        " anchored in what you actually saw."
    ),
    schema_slice={
        "classification": {
            "type": "object",
            "required": ["complexity", "priority"],
            "properties": {
                "complexity": {"enum": ["trivial", "low", "medium", "high"]},
                "priority": {"enum": ["critical", "high", "medium", "low"]},
                "why": {"type": "string"},
            },
        }
    },
    required_fields=("classification",),
)

STANDING_ASSESSMENTS = ReportSection(
    key="standing_assessments",
    title="Standing assessments",
    prompt_fragment=(
        "The considered-and-OK pass, present even on a clean audit. One"
        " verdict line each: proportionality (is investment proportionate to"
        " value; name the largest accessory considered or 'no accessory'),"
        " complexity (is the task's apparent size right for what shipped),"
        " priority (does the actual consequence match the ask), coverage (are"
        " changed paths tested; business-critical gaps named; regression pins"
        " for touched shared behavior), business-logic risk (could existing"
        " flows change behavior; none|low|medium|high with justification)."
        " Whenever an assessment concludes badly, also raise it as a severity-"
        "ranked finding above."
    ),
    schema_slice={
        "standing_assessments": {
            "type": "object",
            "required": [
                "proportionality",
                "complexity",
                "priority",
                "coverage",
                "business_logic_risk",
            ],
            "properties": {
                "proportionality": {"type": "string"},
                "complexity": {"type": "string"},
                "priority": {"type": "string"},
                "coverage": {"type": "string"},
                "business_logic_risk": {"type": "string"},
            },
        }
    },
    required_fields=("standing_assessments",),
)

DISPOSITIONS = ReportSection(
    key="dispositions",
    title="Dispositions",
    prompt_fragment=(
        "One disposition per finding from the review, no silence: fixed"
        " (changed the code; name the commit), refuted (read the code, the"
        " claim is wrong; give ground truth), out-of-scope (real, predates"
        " this work), accepted (agreed, choosing not to act; the tradeoff),"
        " deferred (valid but parked; name where it is tracked)."
    ),
    schema_slice={
        "dispositions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["finding_id", "action", "reason"],
                "properties": {
                    "finding_id": {"type": "string"},
                    "action": {
                        "enum": ["fixed", "refuted", "out-of-scope", "accepted", "deferred"]
                    },
                    "reason": {"type": "string"},
                    "commit": {"type": "string"},
                },
            },
        }
    },
    required_fields=("dispositions",),
)


WARNINGS = ReportSection(
    key="warnings",
    title="Warnings",
    prompt_fragment=(
        "List what this review could NOT cover, as short strings: unchecked"
        " paths, runtime that was unavailable, probes skipped. Empty list"
        " only when coverage was genuinely complete. This is the honesty"
        " valve: a reader must know what was traced versus assumed."
    ),
    schema_slice={
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    required_fields=("warnings",),
)


SECTION_REGISTRY: dict[str, ReportSection] = {
    s.key: s
    for s in (
        SUMMARY,
        FINDINGS,
        BLOCKERS,
        SUGGESTED_FIX,
        PROPOSED_TESTS,
        RULED_OUT,
        CLASSIFICATION,
        STANDING_ASSESSMENTS,
        WARNINGS,
        DISPOSITIONS,
    )
}

# named assemblies: the default checkbox sets (ADR 0003: truss format is the
# auditor's default; investigator has its own)
ASSEMBLY_INVESTIGATOR = ("summary", "findings", "blockers")
ASSEMBLY_AUDIT = ("summary", "findings", "standing_assessments", "warnings", "blockers")


def resolve_sections(requested: list[str] | None) -> list[ReportSection]:
    """Requested keys in registry order; unknown keys raise (caller bugs should
    be loud). None means the investigator default."""
    if requested is None:
        requested = list(ASSEMBLY_INVESTIGATOR)
    unknown = [k for k in requested if k not in SECTION_REGISTRY]
    if unknown:
        raise KeyError(f"unknown report sections: {unknown}")
    return [SECTION_REGISTRY[k] for k in SECTION_REGISTRY if k in set(requested)]


ORDER_KEY = "sections_order"

_ORDER_PROMPT = (
    f'You also choose the PRESENTATION ORDER: set "{ORDER_KEY}" to the keys'
    " above, ordered the way a reader should meet them. Lead with what answers"
    " 'what happened and what should I do'; put reference material (ruled-out"
    " ground, classifications, coverage warnings) last. Use exactly the keys"
    " you produced, each once."
)


def build_report_prompt(sections: list[ReportSection]) -> str:
    parts = [f"## {s.title}\n{s.prompt_fragment}" for s in sections]
    return "\n\n".join(parts) + "\n\n## " + "Presentation order\n" + _ORDER_PROMPT


def build_report_schema(sections: list[ReportSection]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for s in sections:
        properties.update(s.schema_slice)
        required.extend(s.required_fields)
    properties[ORDER_KEY] = {
        "type": "array",
        "items": {"type": "string", "enum": [s.key for s in sections]},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def resolve_order(artifact: dict[str, Any], sections: list[ReportSection]) -> list[str]:
    """The artifact's presentation order: the model's sections_order when it
    lists produced sections, else the assembly order. Mechanical sanitizer:
    unknown keys dropped, duplicates collapsed, missing-but-present sections
    appended, run_stats never ordered (it is stamped, not authored)."""
    produced = [s.key for s in sections if artifact.get(s.key) is not None]
    order = artifact.get(ORDER_KEY)
    if isinstance(order, list):
        seen: set[str] = set()
        clean: list[str] = []
        for key in order:
            if isinstance(key, str) and key in produced and key not in seen:
                clean.append(key)
                seen.add(key)
        clean += [k for k in produced if k not in seen]
        return clean
    return produced


NOT_AUDITED = "not audited"  # fail-closed: no audit artifact is NOT an approval

# severities that block "done" on their own (the orchestrator must accept them
# explicitly with a reason, or revise; see the finish tool's accepted_findings)
BLOCKING_SEVERITIES = ("critical", "high", "medium")


def open_blockers(audit_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The artifact's unanswered blockers: questions the auditor could not settle
    from evidence. A blocker is open work, so it counts toward the verdict just
    like a finding."""
    blockers = (audit_artifact or {}).get("blockers") or []
    return [b for b in blockers if isinstance(b, dict)]


def blocking_findings(audit_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Findings at medium severity or above: the ones a ship decision must
    account for (accepted with a reason, or fixed and re-audited)."""
    findings = (audit_artifact or {}).get("findings") or []
    return [
        f
        for f in findings
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
    ]


def mechanical_verdict(audit_artifact: dict[str, Any] | None) -> str:
    """The truss verdict mapping, applied mechanically and FAIL-CLOSED: no
    artifact (or an empty one) is 'not audited', never 'approve'; any critical
    finding or open blocker -> request changes; any high -> approve with fixes;
    else approve. Open blockers count because a question the auditor could not
    answer is unverified work, not a clean audit."""
    if not audit_artifact:
        return NOT_AUDITED
    findings = [f for f in audit_artifact.get("findings") or [] if isinstance(f, dict)]
    severities = {str(f.get("severity", "")).lower() for f in findings}
    if "critical" in severities or open_blockers(audit_artifact):
        return "request changes"
    if "high" in severities:
        return "approve with fixes"
    return "approve"
