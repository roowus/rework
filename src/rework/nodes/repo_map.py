"""Mechanical repo map: objective context provided before any agentic loop,
so roles never spend turns building their own map (ADR 0003, owner's call).

Zero LLM: walk the workspace (bounded, junk-filtered), list files with sizes,
count languages by extension, surface likely entry points. Rendered as one
text block for the system prompt.
"""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    ".next",
    ".idea",
    ".vscode",
    "coverage",
    ".tox",
    ".eggs",
}
SKIP_FILES = {".DS_Store", "package-lock.json", "uv.lock", "poetry.lock", "yarn.lock"}

LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "c++",
    ".hpp": "c++",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
}
ENTRY_HINTS = [
    "main.py",
    "app.py",
    "cli.py",
    "manage.py",
    "index.ts",
    "index.js",
    "main.ts",
    "main.go",
    "Cargo.toml",
    "pyproject.toml",
    "package.json",
    "Makefile",
    "build.gradle",
    "pom.xml",
    "README.md",
]
MAX_FILES = 2000
MAX_RENDERED = 400


def build_repo_map(root: Path) -> str:
    root = root.resolve()
    files: list[tuple[str, int]] = []
    lang_counts: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            if filename in SKIP_FILES or filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            files.append((rel, size))
            lang = LANG_BY_EXT.get(full.suffix.lower())
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            if len(files) >= MAX_FILES:
                break
        if len(files) >= MAX_FILES:
            break

    lines: list[str] = [f"workspace: {root.name} ({len(files)} files)"]
    if lang_counts:
        langs = ", ".join(
            f"{k} ({v})" for k, v in sorted(lang_counts.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"languages: {langs}")
    entries = [name for name in ENTRY_HINTS if (root / name).exists()]
    if entries:
        lines.append(f"likely entry points: {', '.join(entries)}")

    files.sort(key=lambda f: f[0])
    shown = files[:MAX_RENDERED]
    lines.append("files:")
    lines.extend(f"  {rel} ({size}B)" for rel, size in shown)
    if len(files) > MAX_RENDERED:
        lines.append(f"  ... ({len(files) - MAX_RENDERED} more files)")
    return "\n".join(lines)
