"""Role artifacts as markup: findings, diffs, summaries, run stats (handoff §3.5)."""

from __future__ import annotations

from typing import Any

from rework.tui.theme import (
    ACCENT,
    DANGER,
    FAINT,
    FG,
    MUTED,
    OK,
    WARN,
    duration,
    esc,
)

SEVERITY = {"critical": DANGER, "high": DANGER, "medium": WARN, "low": MUTED, "info": FAINT}


def findings_markup(findings: list[Any]) -> str:
    out: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            out.append(f"• {esc(f)}")
            continue
        sev = str(f.get("severity") or "info").lower()
        colour = SEVERITY.get(sev, MUTED)
        title = f.get("title") or f.get("summary") or "finding"
        conf = f.get("confidence")
        meta = [sev]
        if f.get("verdict"):
            meta.append(str(f["verdict"]))
        if isinstance(conf, int | float):
            meta.append(f"{conf:.0%} sure")
        out.append(f"[{colour}]●[/] [b]{esc(title)}[/b]  [{FAINT}]{esc(' · '.join(meta))}[/]")
        if f.get("detail"):
            out.append(f"  [{MUTED}]{esc(f['detail'])}[/]")
        for ev in (f.get("evidence") or [])[:4]:
            out.append(f"  [{FAINT}]↳ {esc(ev)}[/]")
        out.append("")
    return "\n".join(out).rstrip() or f"[{FAINT}]no findings[/]"


def diff_markup(diff: str, max_lines: int = 2000) -> str:
    if not diff:
        return f"[{FAINT}]no diff[/]"
    out: list[str] = []
    lines = diff.splitlines()
    for line in lines[:max_lines]:
        if line.startswith(("diff --git", "index ")):
            out.append(f"[{FAINT}]{esc(line)}[/]")
        elif line.startswith(("+++", "---")):
            out.append(f"[b]{esc(line)}[/b]")
        elif line.startswith("@@"):
            out.append(f"[{ACCENT}]{esc(line)}[/]")
        elif line.startswith("+"):
            out.append(f"[{OK}]{esc(line)}[/]")
        elif line.startswith("-"):
            out.append(f"[{DANGER}]{esc(line)}[/]")
        else:
            out.append(f"[{MUTED}]{esc(line)}[/]")
    if len(lines) > max_lines:
        out.append(f"[{FAINT}]… {len(lines) - max_lines} more lines[/]")
    return "\n".join(out)


def diff_stats(diff: str) -> str:
    if not diff:
        return ""
    files = sum(1 for line in diff.splitlines() if line.startswith("diff --git"))
    add = sum(
        1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    rem = sum(
        1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    return f"{max(files, 1)} file(s)  [{OK}]+{add}[/] [{DANGER}]-{rem}[/]"


def stats_markup(stats: dict[str, Any] | None) -> str:
    if not stats:
        return ""
    bits = []
    if "model_calls" in stats:
        bits.append(f"{stats['model_calls']} calls")
    if "tool_calls" in stats:
        bits.append(f"{stats['tool_calls']} tools")
    if "wall_s" in stats:
        bits.append(duration(stats["wall_s"]))
    if "cost_usd" in stats:
        bits.append(f"${float(stats['cost_usd']):.3f}")
    return f"[{FAINT}]{esc(' · '.join(bits))}[/]" if bits else ""


def artifact_markup(role: str, artifact: dict[str, Any] | None) -> str:
    """A role's handoff, readable."""
    if not artifact:
        return f"[{FAINT}]no artifact yet[/]"
    parts: list[str] = []
    if artifact.get("summary"):
        parts.append(f"[{FG}]{esc(artifact['summary'])}[/]")
    if artifact.get("verdict"):
        verdict = str(artifact["verdict"])
        colour = OK if "approve" in verdict.lower() else WARN
        parts.append(f"verdict [{colour}][b]{esc(verdict)}[/b][/]")
    if artifact.get("branch"):
        commit = str(artifact.get("commit") or "")[:10]
        parts.append(
            f"[{MUTED}]branch[/] {esc(artifact['branch'])}  [{MUTED}]commit[/] {esc(commit)}"
        )
    if artifact.get("diff"):
        parts.append(f"[{MUTED}]diff[/] {diff_stats(str(artifact['diff']))}")
    if artifact.get("findings"):
        parts.append("")
        parts.append(findings_markup(artifact["findings"]))
    if artifact.get("suggested_fix"):
        parts.append(f"\n[{MUTED}]suggested fix[/]\n{esc(artifact['suggested_fix'])}")
    blockers = artifact.get("blockers")
    if blockers:
        parts.append(f"\n[{WARN}]blockers[/]")
        for b in blockers if isinstance(blockers, list) else [blockers]:
            text = b.get("detail") or b.get("title") if isinstance(b, dict) else b
            parts.append(f"  • {esc(text)}")
    if stats := stats_markup(artifact.get("run_stats")):
        parts.append(f"\n{stats}")
    return "\n".join(parts) or f"[{FAINT}](empty artifact)[/]"


def escalation_markup(escalation: dict[str, Any] | None) -> str:
    if not escalation:
        return ""
    kind = escalation.get("kind") or "escalation"
    detail = escalation.get("detail") or escalation.get("question") or ""
    return f"[{WARN}]{esc(kind)}[/]  {esc(detail)}"
