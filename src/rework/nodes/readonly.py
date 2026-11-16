"""Read-only tool surface for roles that must never change anything (the
surveyor).

The guarantee is structural, not a validator over a shell string: the model
cannot express a shell command at all. Its whole surface is READ_ONLY_TOOL_NAMES:

- `read_file` and `list_dir`, delegated to the inner executor (path-jailed);
- `search_code`, `repo_tree` and `git_read`: purpose-built tools whose handlers
  CONSTRUCT the argv themselves from a fixed template. Model-supplied values only
  ever land in positions chosen here (after `-e` for a pattern, after `--` for
  paths and revisions, as a bounded integer for sizes), so a flag cannot be
  smuggled in. Every path goes through the same jail as `read_file` (relative,
  no `~`, no `..` component, resolved inside the workdir, symlinks included)
  BEFORE it reaches an argv. The argv is then handed to the inner executor as
  its own `run_command` (shlex-quoted), which keeps cwd, env, timeout, process
  group and truncation behaviour: no process spawning is reimplemented here.

`run_command` is NOT offered by `specs()` and `execute()` refuses it by name, as
it does every other tool name (the tool loop never checks membership itself, so
a model that calls a hidden tool by name would otherwise reach the inner
executor).

Mount it as the OUTERMOST wrapper: inner wrappers (complaint, escalation, web,
library) re-append their own specs, an outer filter removes them again.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from rework.kernel.llm.types import ToolSpec
from rework.nodes.tools import ToolExecutor

# the single hard list of tools a read-only role may use
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "list_dir", "search_code", "repo_tree", "git_read"}
)

GIT_SUBCOMMANDS: tuple[str, ...] = (
    "log", "blame", "show", "diff", "ls-files", "ls-tree", "status", "rev-parse",
    "shortlog", "describe",
)  # fmt: skip

DEFAULT_MAX_LINES = 200
MAX_LINES_CAP = 1000
MAX_DEPTH = 8
DEFAULT_DEPTH = 3
_COMMAND_TIMEOUT = 60

_REV_OK = re.compile(r"^[A-Za-z0-9_./@^~:{}+,=-]+$")
_PATHLIKE_BAD = re.compile(r"[\x00\n\r]")

# git is pinned so a hostile repo config/attributes cannot run anything:
# no pager, no fsmonitor hook, no external diff or textconv, literal pathspecs.
_GIT_BASE = (
    "git", "--no-optional-locks", "--no-pager", "--literal-pathspecs",
    "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "color.ui=never",
)  # fmt: skip
_DIFF_SAFE = ("--no-ext-diff", "--no-textconv", "--no-color")

HELP = (
    "read-only mode: the shell is not available. Tools: "
    + ", ".join(sorted(READ_ONLY_TOOL_NAMES))
    + ". Use search_code for text search, repo_tree for a file tree, git_read for"
    " git log/blame/show/diff/ls-files/ls-tree/status/rev-parse/shortlog/describe,"
    " read_file and list_dir for contents. Paths are relative to the workspace."
)

_OWN_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_code",
        description=(
            "Search file contents in the workspace (ripgrep, or grep when rg is not"
            " installed). Returns path:line:text. Read-only. Args: {pattern, path?,"
            " glob?, fixed?, max_results?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "text or regex to find"},
                "path": {"type": "string", "description": "relative file or directory"},
                "glob": {"type": "string", "description": "only files matching, e.g. *.py"},
                "fixed": {"type": "boolean", "description": "literal text, not a regex"},
                "max_results": {"type": "integer", "description": "max lines returned"},
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="repo_tree",
        description=(
            "List files and directories under a path, to a bounded depth, without"
            " .git. Read-only. Args: {path?, depth?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "relative directory, default ."},
                "depth": {"type": "integer", "description": f"1..{MAX_DEPTH}, default 3"},
            },
        },
    ),
    ToolSpec(
        name="git_read",
        description=(
            "Read git history and state. subcommand is one of: "
            + ", ".join(GIT_SUBCOMMANDS)
            + ". blame needs a path. Read-only. Args: {subcommand, path?, rev?,"
            " max_lines?, start_line?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subcommand": {"type": "string", "enum": list(GIT_SUBCOMMANDS)},
                "path": {"type": "string", "description": "relative file or directory"},
                "rev": {"type": "string", "description": "commit, tag, branch or a..b"},
                "max_lines": {"type": "integer", "description": "max lines returned"},
                "start_line": {"type": "integer", "description": "blame: first line"},
            },
            "required": ["subcommand"],
        },
    ),
)


class ToolRefused(Exception):
    """A model-supplied argument was refused; the message goes back to the model."""


def jail_relative(workdir: Path, raw: Any, *, default: str | None = None) -> str:
    """The read_file jail, applied BEFORE a path reaches an argv. Returns the path
    relative to the workdir ('.' for the workdir itself). Refuses non-strings,
    NUL/newline, absolute paths, `~`, any `..` component and anything that
    resolves (symlinks included) outside the workdir."""
    if raw is None or raw == "":
        if default is None:
            raise ToolRefused("a path is required")
        raw = default
    if not isinstance(raw, str):
        raise ToolRefused("path must be a string")
    if _PATHLIKE_BAD.search(raw):
        raise ToolRefused("path contains a NUL or newline")
    if raw.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", raw):
        raise ToolRefused(f"path must be relative to the workspace, got {raw!r}")
    if ".." in re.split(r"[\\/]+", raw):
        raise ToolRefused(f"'..' is not allowed in a path: {raw!r}")
    root = workdir.resolve()
    resolved = (root / raw).resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolRefused(f"path escapes the workspace: {raw!r}")
    return os.path.relpath(resolved, root).replace(os.sep, "/")


def _dash_safe(rel: str) -> str:
    """A path in a position where a leading `-` would be read as a flag."""
    return rel if rel in (".", "") or rel.startswith("/") else f"./{rel}"


def _int(raw: Any, name: str, default: int, low: int, high: int) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ToolRefused(f"{name} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolRefused(f"{name} must be an integer") from None
    return max(low, min(high, value))


def _positive(raw: Any, name: str, default: int, high: int) -> int:
    """A count that must be at least 1: a non-positive value is refused, not
    clamped into an empty range that would read as a result. Only an
    over-large value is clamped (to `high`)."""
    value = _int(raw, name, default, -(10**9), high)
    if value < 1:
        raise ToolRefused(f"{name} must be at least 1, got {value}")
    return value


def _text(raw: Any, name: str, *, limit: int = 500) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolRefused(f"{name} must be a non-empty string")
    if _PATHLIKE_BAD.search(raw):
        raise ToolRefused(f"{name} contains a NUL or newline")
    if len(raw) > limit:
        raise ToolRefused(f"{name} is longer than {limit} characters")
    return raw


def _rev(raw: Any, default: str | None = None) -> str | None:
    if raw is None or raw == "":
        return default
    rev = _text(raw, "rev", limit=200)
    if rev.startswith("-"):
        raise ToolRefused("rev must not start with '-'")
    if not _REV_OK.match(rev):
        raise ToolRefused(f"rev has characters a revision never needs: {rev!r}")
    return rev


def build_search_argv(workdir: Path, args: dict[str, Any], *, have_rg: bool) -> list[str]:
    pattern = _text(args.get("pattern"), "pattern")
    rel = _dash_safe(jail_relative(workdir, args.get("path"), default="."))
    glob = args.get("glob")
    if glob not in (None, ""):
        glob = _text(glob, "glob", limit=200)
    else:
        glob = None
    fixed = args.get("fixed") is True
    if have_rg:
        # --hidden --no-ignore: the same file set as the grep fallback (dotfiles
        # and gitignored paths included); .git is skipped in both branches
        argv = ["rg", "--no-config", "--color=never", "-n", "--no-heading", "--max-columns=300"]
        argv += ["--max-columns-preview", "--hidden", "--no-ignore"]
        if fixed:
            argv.append("-F")
        if glob:
            argv.append(f"--glob={glob}")
        argv.append("--glob=!.git")  # last, so no user glob can re-include .git
        return [*argv, "-e", pattern, "--", rel]
    argv = ["grep", "-rnI", "--exclude-dir=.git", "-E" if not fixed else "-F"]
    if glob:
        argv.append(f"--include={glob}")
    return [*argv, "-e", pattern, "--", rel]


def build_tree_argv(workdir: Path, args: dict[str, Any]) -> list[str]:
    rel = _dash_safe(jail_relative(workdir, args.get("path"), default="."))
    depth = _int(args.get("depth"), "depth", DEFAULT_DEPTH, 1, MAX_DEPTH)
    return [
        "find", rel, "-maxdepth", str(depth),
        "-not", "-path", "*/.git/*", "-not", "-path", "*/.git",
    ]  # fmt: skip


def build_git_argv(workdir: Path, args: dict[str, Any]) -> list[str]:
    sub = args.get("subcommand")
    if sub not in GIT_SUBCOMMANDS:
        raise ToolRefused(f"subcommand must be one of: {', '.join(GIT_SUBCOMMANDS)}")
    raw_path = args.get("path")
    rel = jail_relative(workdir, raw_path) if raw_path not in (None, "") else None
    paths = ["--", rel] if rel is not None else []
    rev = _rev(args.get("rev"))
    max_lines = _positive(args.get("max_lines"), "max_lines", DEFAULT_MAX_LINES, MAX_LINES_CAP)
    base = list(_GIT_BASE)
    if sub == "log":
        fmt = ["-n", str(max_lines), "--date=short", "--format=%h %ad %an %s", *_DIFF_SAFE]
        return [*base, "log", *fmt, rev or "HEAD", "--", *paths[1:]]
    if sub == "blame":
        if rel is None:
            raise ToolRefused("blame needs a path")
        window: list[str] = []
        if args.get("start_line") not in (None, ""):
            start = _positive(args.get("start_line"), "start_line", 1, 10**7)
            window = ["-L", f"{start},{start + max_lines - 1}"]
        return [*base, "blame", "--no-textconv", "--date=short", *window, rev or "HEAD", "--", rel]
    if sub in ("show", "diff"):
        head = [*base, sub, *_DIFF_SAFE]
        if sub == "show":
            return [*head, "--stat", "--patch", rev or "HEAD", *paths]
        return [*head, *([rev] if rev else []), *paths]
    if sub == "ls-files":
        return [*base, "ls-files", *paths]
    if sub == "ls-tree":
        return [*base, "ls-tree", "-r", "--name-only", rev or "HEAD", *paths]
    if sub == "status":
        return [*base, "status", "--short", "--branch", *paths]
    if sub == "rev-parse":
        return [*base, "rev-parse", "--verify", rev or "HEAD"]
    if sub == "shortlog":
        return [*base, "shortlog", "-s", "-n", rev or "HEAD", *paths]
    return [*base, "describe", "--tags", "--always", *([rev] if rev else [])]  # describe


def cap_lines(text: str, max_lines: int) -> str:
    """Keep the first max_lines lines of the inner executor's output (its leading
    `(exit N)` line is kept free) and say so when lines were dropped."""
    lines = text.split("\n")
    head = lines[0] if lines and lines[0].startswith("(exit ") else None
    body = lines[1:] if head is not None else lines
    if len(body) <= max_lines:
        return text
    kept = body[:max_lines]
    note = f"... ({len(body) - max_lines} more lines cut at max {max_lines})"
    return "\n".join(([head] if head is not None else []) + kept + [note])


class ReadOnlyExecutor:
    """Outermost wrapper: the whole of a read-only role's tool surface."""

    def __init__(self, inner: ToolExecutor, workdir: Path) -> None:
        self._inner = inner
        self._workdir = Path(workdir).resolve()

    def _refuse_tool(self, name: str) -> str:
        return (
            f"error: tool {name!r} is not available: this role is read-only and has no"
            f" shell. Available tools: {', '.join(sorted(READ_ONLY_TOOL_NAMES))}."
            f" {HELP}"
        )

    async def _run(self, argv: list[str], max_lines: int = MAX_LINES_CAP) -> str:
        out = await self._inner.execute(
            "run_command",
            {"command": shlex.join(argv), "timeout_seconds": _COMMAND_TIMEOUT},
        )
        return cap_lines(out, max_lines)

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in READ_ONLY_TOOL_NAMES:
            return self._refuse_tool(name)
        try:
            if name in ("read_file", "list_dir"):
                default = "." if name == "list_dir" else None
                rel = jail_relative(self._workdir, args.get("path"), default=default)
                return await self._inner.execute(name, {"path": rel})
            if name == "search_code":
                argv = build_search_argv(
                    self._workdir, args, have_rg=shutil.which("rg") is not None
                )
                limit = _int(args.get("max_results"), "max_results", DEFAULT_MAX_LINES, 1, 1000)
                return await self._run(argv, limit)
            if name == "repo_tree":
                return await self._run(build_tree_argv(self._workdir, args), 500)
            argv = build_git_argv(self._workdir, args)
            limit = _positive(args.get("max_lines"), "max_lines", DEFAULT_MAX_LINES, MAX_LINES_CAP)
            return await self._run(argv, limit)
        except ToolRefused as exc:
            return f"error: refused ({exc}). {HELP}"

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        keep = READ_ONLY_TOOL_NAMES if allowed is None else READ_ONLY_TOOL_NAMES & allowed
        out = [s for s in self._inner.specs(allowed) if s.name in keep and s.name in _INNER_TOOLS]
        out += [s for s in _OWN_SPECS if s.name in keep]
        return out


_INNER_TOOLS = frozenset({"read_file", "list_dir"})
