"""Design tokens: the ONLY place colours, glyphs and number formats are defined.

See docs/tui/DESIGN.md. Widgets reference tokens as TCSS variables ($fg,
$muted, $accent, …) or through the helpers here for Rich markup; nothing else
hard-codes a hex value."""

from __future__ import annotations

import time

from textual.theme import Theme

# -- palette -----------------------------------------------------------------

BG = "#0c0c10"
SURFACE = "#121218"
RAISED = "#18181f"
OVERLAY = "#1e1e27"
LINE = "#24242d"
LINE_STRONG = "#34343f"
FG = "#e6e6eb"
MUTED = "#8c8c99"
FAINT = "#5a5a66"
ACCENT = "#a394f7"
ACCENT_DIM = "#2e2950"
OK = "#76c893"
WARN = "#e3b66b"
DANGER = "#ec6a6a"

THEME = Theme(
    name="rework",
    primary=ACCENT,
    secondary=ACCENT,
    accent=ACCENT,
    warning=WARN,
    error=DANGER,
    success=OK,
    foreground=FG,
    background=BG,
    surface=SURFACE,
    panel=RAISED,
    boost=OVERLAY,
    dark=True,
    variables={
        "bg": BG,
        "raised": RAISED,
        "overlay": OVERLAY,
        "line": LINE,
        "line-strong": LINE_STRONG,
        "fg": FG,
        "muted": MUTED,
        "faint": FAINT,
        "accent-dim": ACCENT_DIM,
        "ok": OK,
        "warn": WARN,
        "danger": DANGER,
        # built-in widget knobs, so stock widgets match the palette
        "block-cursor-background": OVERLAY,
        "block-cursor-foreground": FG,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": RAISED,
        "block-hover-background": RAISED,
        "input-cursor-background": ACCENT,
        "input-selection-background": f"{ACCENT} 30%",
        "scrollbar": LINE,
        "scrollbar-hover": LINE_STRONG,
        "scrollbar-active": ACCENT,
        "scrollbar-background": BG,
        "scrollbar-background-hover": BG,
        "scrollbar-background-active": BG,
        "scrollbar-corner-color": BG,
        "footer-background": BG,
        "border": LINE_STRONG,
        "border-blurred": LINE,
        "markdown-h1-color": FG,
        "markdown-h2-color": FG,
        "markdown-h3-color": FG,
    },
)

# -- status vocabulary ---------------------------------------------------------

# state -> (glyph, colour). Covers mission, run and gate statuses.
STATUS: dict[str, tuple[str, str]] = {
    "pending": ("○", FAINT),
    "intent": ("○", FAINT),
    "running": ("●", ACCENT),
    "awaiting_gate": ("◆", WARN),
    "halted_escalated": ("◆", WARN),
    "open": ("◆", WARN),
    "on_hold": ("‖", WARN),
    "done": ("✓", OK),
    "completed": ("✓", OK),
    "approved": ("✓", OK),
    "failed": ("✗", DANGER),
    "rejected": ("✗", DANGER),
    "cancelled": ("⊘", FAINT),
    "aborted": ("⊘", FAINT),
}

STATUS_LABEL: dict[str, str] = {
    "awaiting_gate": "needs you",
    "halted_escalated": "needs you",
    "on_hold": "paused",
    "intent": "queued",
}


def status_value(status: object) -> str:
    return str(getattr(status, "value", status) or "")


def glyph(status: object) -> str:
    """Rich markup: the coloured glyph for a status."""
    g, colour = STATUS.get(status_value(status), ("·", FAINT))
    return f"[{colour}]{g}[/]"


def status_text(status: object) -> str:
    """Rich markup: glyph + human label, coloured."""
    value = status_value(status)
    g, colour = STATUS.get(value, ("·", FAINT))
    label = STATUS_LABEL.get(value, value.replace("_", " "))
    return f"[{colour}]{g} {label}[/]"


def status_colour(status: object) -> str:
    return STATUS.get(status_value(status), ("·", FAINT))[1]


# -- number formats ------------------------------------------------------------


def money(usd: float | None) -> str:
    usd = float(usd or 0.0)
    if usd == 0:
        return "$0"
    if abs(usd) < 0.001:
        return "<$0.001"
    if abs(usd) < 1:
        return f"${usd:.3f}"
    if abs(usd) < 1000:
        return f"${usd:.2f}"
    return f"${usd:,.0f}"


def tokens(n: float | None) -> str:
    n = float(n or 0)
    if n < 1000:
        return f"{n:.0f}"
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def duration(seconds: float | None) -> str:
    s = max(0.0, float(seconds or 0))
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    if s < 86400:
        return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"
    return f"{int(s // 86400)}d {int(s % 86400 // 3600)}h"


def ago(epoch: float | None) -> str:
    if not epoch:
        return ""
    delta = max(0.0, time.time() - float(epoch))
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{max(1, round(delta / 60))}m ago"
    if delta < 86400:
        return f"{round(delta / 3600)}h ago"
    if delta < 86400 * 30:
        return f"{round(delta / 86400)}d ago"
    return time.strftime("%b %d", time.localtime(epoch))


def clip(text: str, width: int) -> str:
    """One line, at most `width` cells, with an ellipsis."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"

def esc(text: object) -> str:
    """Escape markup in user/agent text (Textual content markup).
    
    Escapes EVERY '[' as '\\[' to prevent MarkupError crashes.
    """
    s = str(text if text is not None else "")
    return s.replace("[", "\\[")

def m(colour: str, text: object) -> str:
    """Markup helper: escaped text in a palette colour."""
    return f"[{colour}]{esc(text)}[/]"


SEP = f" [{FAINT}]·[/] "


def plan_label(plan: dict | None) -> str:
    """'fix-pr' for a canned plan; 'fix-pr · orchestrated' for ADR 0007 plans."""
    plan = plan or {}
    if plan.get("orchestrator"):
        return f"{plan.get('playbook') or 'auto'} · orchestrated"
    return str(plan.get("kind") or "echo")


def mission_name(mission: object, width: int = 60) -> str:
    """The mission's title when the orchestrator gave it one, else its
    objective, clipped (plain text; escape before rendering)."""
    title = getattr(mission, "title", None)
    return clip(str(title or getattr(mission, "objective", "") or ""), width)
