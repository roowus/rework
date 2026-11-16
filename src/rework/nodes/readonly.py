"""Read-only tool surface for roles that must never change anything (the
surveyor). The guarantee is enforced in code, not asked for in a prompt:

- `specs()` offers only READ_ONLY_TOOL_NAMES;
- `execute()` refuses any other tool name with a short error string (the tool
  loop never checks membership itself, so a model that calls a hidden tool by
  name would otherwise reach the inner executor);
- `run_command` is validated BEFORE the inner executor sees it: every pipeline
  stage must be an allowlisted read-only program, git is limited to read-only
  subcommands, and redirection, chaining, background, substitution and
  newlines are refused.

Mount it as the OUTERMOST wrapper: inner wrappers (complaint, escalation, web,
library) re-append their own specs, an outer filter removes them again.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from rework.kernel.llm.types import ToolSpec
from rework.nodes.tools import ToolExecutor

# the single hard list of tools a read-only role may use
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "list_dir", "run_command"})

READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "git", "grep", "rg", "find", "ls", "tree", "cat", "head", "tail", "wc", "file",
        "du", "sort", "uniq", "sed", "awk", "diff", "cut", "tr", "stat", "pwd",
    }
)  # fmt: skip
READ_ONLY_GIT: frozenset[str] = frozenset(
    {
        "log", "blame", "show", "diff", "ls-files", "ls-tree", "status", "rev-parse",
        "shortlog", "describe", "grep", "cat-file", "branch",
    }
)  # fmt: skip

_BRANCH_FLAGS = frozenset(
    {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--show-current"}
)
# per program: flags that make an otherwise read-only program write or execute
_BAD_FLAGS: dict[str, tuple[str, ...]] = {
    "find": (
        "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls",
    ),
    "sort": ("-o", "--output"),
    "tree": ("-o",),
    "file": ("-C", "--compile"),
    "rg": ("--pre", "--pre-glob", "--hostname-bin"),
}  # fmt: skip
_SED_PRINT = re.compile(r"^(?:\d+|\$)(?:,(?:\d+|\$))?p$")
_AWK_BAD = ("system", "getline", ">", "|", "close(")

ALLOWED_HELP = (
    "read-only mode: only these are allowed. Tools: read_file, list_dir, run_command."
    " run_command takes ONE pipeline (stages joined by |) of: "
    + ", ".join(sorted(READ_ONLY_COMMANDS))
    + ". git is limited to: "
    + ", ".join(sorted(READ_ONLY_GIT))
    + " (branch only with --list). sed only as `sed -n 'N,Mp'`. No redirection (> >> <),"
    " no ; & && ||, no backticks or $(...), no newlines."
)

_RUN_COMMAND_DESCRIPTION = (
    "Run a READ-ONLY shell pipeline inside the workspace (grep/rg, find, ls, tree, cat,"
    " head, tail, wc, sed -n, git log/blame/show/diff/ls-files/status). Nothing that"
    " writes is accepted. Args: {command, timeout_seconds?}"
)


def _refuse(why: str) -> str:
    return f"error: refused ({why}). {ALLOWED_HELP}"


def _check_git(argv: list[str]) -> str | None:
    rest = argv[1:]
    while rest and rest[0] == "--no-pager":
        rest = rest[1:]
    if not rest:
        return "git needs a subcommand"
    sub, args = rest[0], rest[1:]
    if sub not in READ_ONLY_GIT:
        return f"git {sub} is not read-only"
    for a in args:
        if a.startswith(("--output", "--ext-diff", "--open-files-in-pager")) or a == "-O":
            return f"git {sub} {a} writes or executes"
    if sub == "branch":
        flags = [a for a in args if a.startswith("-")]
        if any(f not in _BRANCH_FLAGS for f in flags):
            return "git branch is only allowed for listing"
        if len(flags) != len(args) and not ({"--list", "-l"} & set(flags)):
            return "git branch with a name is not a listing"
    return None


def validate_command(command: str) -> str | None:
    """None when the command is a read-only pipeline, else a refusal message."""
    if "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return _refuse("newline, backtick or $(...) substitution")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        return _refuse(f"cannot parse the command: {exc}")
    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            stages.append([])
        elif tok and all(c in "();<>|&" for c in tok):
            return _refuse(f"operator {tok!r}")
        else:
            stages[-1].append(tok)
    for argv in stages:
        if not argv:
            return _refuse("empty pipeline stage")
        prog = argv[0]
        if prog not in READ_ONLY_COMMANDS:
            return _refuse(f"{prog!r} is not an allowed program")
        why = _check_stage(prog, argv)
        if why is not None:
            return _refuse(why)
    return None


def _check_stage(prog: str, argv: list[str]) -> str | None:
    if prog == "git":
        return _check_git(argv)
    args = argv[1:]
    bad = _BAD_FLAGS.get(prog, ())
    for a in args:
        if a in bad or any(a.startswith(b + "=") for b in bad if b.startswith("--")):
            return f"{prog} {a} writes or executes"
    if prog == "sed":
        script = [a for a in args if not a.startswith("-")]
        flags = [a for a in args if a.startswith("-")]
        if flags != ["-n"] or args[0] != "-n" or not script or not _SED_PRINT.match(script[0]):
            return "sed is only allowed as sed -n 'N,Mp'"
    if prog == "awk" and any(b in a for a in args for b in _AWK_BAD):
        return "awk programs may not use system, getline, close, > or |"
    if prog == "awk" and any(a in ("-i", "-f") or a.startswith("--include") for a in args):
        return "awk -i / -f is not allowed"
    if prog == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return "uniq with an output file writes"
    return None


class ReadOnlyExecutor:
    """Outermost wrapper: the whole of a read-only role's tool surface."""

    def __init__(self, inner: ToolExecutor) -> None:
        self._inner = inner

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in READ_ONLY_TOOL_NAMES:
            return (
                f"error: tool {name!r} is not available: this role is read-only."
                f" Available tools: {', '.join(sorted(READ_ONLY_TOOL_NAMES))}."
            )
        if name == "run_command":
            refusal = validate_command(str(args.get("command", "")))
            if refusal is not None:
                return refusal
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        keep = READ_ONLY_TOOL_NAMES if allowed is None else READ_ONLY_TOOL_NAMES & allowed
        out: list[ToolSpec] = []
        for spec in self._inner.specs(allowed):
            if spec.name not in keep:
                continue
            if spec.name == "run_command":
                spec = spec.model_copy(update={"description": _RUN_COMMAND_DESCRIPTION})
            out.append(spec)
        return out
