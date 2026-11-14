"""Which hook groups and handlers fire for an event.

Two filters decide this in Claude Code:

* the group ``matcher`` (exact names, ``|``/``,`` lists, or an unanchored regex), and
* the handler ``if`` field (one permission rule such as ``Bash(git *)`` or ``Edit(*.ts)``).

Rules follow https://code.claude.com/docs/en/hooks (Matcher patterns, Common fields,
"How if patterns match Bash commands") and https://code.claude.com/docs/en/permissions
(Wildcard patterns, Compound commands, Wrappers, How path patterns match).
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"}

# Field of the stdin payload that the matcher is tested against.
MATCH_FIELD = {
    **{e: "tool_name" for e in TOOL_EVENTS},
    "SessionStart": "source",
    "SessionEnd": "reason",
    "Notification": "notification_type",
    "SubagentStart": "agent_type",
    "SubagentStop": "agent_type",
    "PreCompact": "trigger",
    "PostCompact": "trigger",
    "ConfigChange": "source",
}

# Events where a matcher is silently ignored (the group always fires).
NO_MATCHER_EVENTS = {
    "UserPromptSubmit", "PostToolBatch", "Stop", "TeammateIdle", "TaskCreated",
    "TaskCompleted", "WorktreeCreate", "WorktreeRemove", "MessageDisplay", "CwdChanged",
}

KNOWN_EVENTS = set(MATCH_FIELD) | NO_MATCHER_EVENTS | {
    "Setup", "InstructionsLoaded", "UserPromptExpansion", "StopFailure", "DirectoryAdded",
    "FileChanged", "PreModelSwitch", "PostModelSwitch", "Elicitation", "ElicitationResult",
}

_EXACT = re.compile(r"^[A-Za-z0-9_\- ,|]*$")
_EXACT_NARROW = re.compile(r"^[A-Za-z0-9_|]*$")  # FileChanged, StopFailure


def matcher_is_all(matcher: Optional[str]) -> bool:
    return matcher is None or matcher in ("", "*")


def matcher_kind(matcher: Optional[str], event: str = "") -> str:
    """'all', 'exact' or 'regex'."""
    if matcher_is_all(matcher):
        return "all"
    pat = _EXACT_NARROW if event in ("FileChanged", "StopFailure") else _EXACT
    return "exact" if pat.match(matcher) else "regex"


def matcher_matches(matcher: Optional[str], value: Optional[str], event: str = "") -> Tuple[bool, str]:
    """Return (matches, note). ``note`` explains anything surprising."""
    if event in NO_MATCHER_EVENTS:
        if not matcher_is_all(matcher):
            return True, f"{event} has no matcher support; matcher {matcher!r} is ignored"
        return True, ""
    kind = matcher_kind(matcher, event)
    if kind == "all":
        return True, ""
    if value is None:
        value = ""
    if kind == "exact":
        seps = r"\|" if event in ("FileChanged", "StopFailure") else r"[|,]"
        names = [n.strip() for n in re.split(seps, matcher) if n.strip()]
        return value in names, ""
    try:
        return re.search(matcher, value) is not None, ""
    except re.error as exc:
        return False, f"matcher {matcher!r} is not a valid regex ({exc})"


# ---------------------------------------------------------------- if rules

def parse_rule(rule: str) -> Tuple[str, Optional[str]]:
    """'Bash(git *)' -> ('Bash', 'git *'); 'Read' -> ('Read', None)."""
    rule = rule.strip()
    i = rule.find("(")
    if i == -1 or not rule.endswith(")"):
        return rule, None
    return rule[:i].strip(), rule[i + 1:-1]


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PRIMARY_FIELDS = {"command", "file_path", "path", "notebook_path", "url"}


def rule_tool_matches(rule_tool: str, tool_name: str) -> bool:
    if rule_tool == tool_name:
        return True
    if rule_tool == "Edit" and tool_name in EDIT_TOOLS:
        return True
    if "*" in rule_tool:
        return fnmatch.fnmatchcase(tool_name, rule_tool)
    return False


def if_matches(rule: str, event: str, tool_name: str, tool_input: dict,
               cwd: str, anchor: str) -> Tuple[bool, str]:
    """Would a handler with this ``if`` rule run? Returns (runs, note)."""
    if event not in TOOL_EVENTS:
        return False, f"'if' is only evaluated on tool events; on {event} this handler never runs"
    rule_tool, spec = parse_rule(rule)
    if not rule_tool_matches(rule_tool, tool_name):
        return False, ""
    if spec is None or spec.strip() == "*":
        return True, ""
    tool_input = tool_input or {}

    param = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", spec)
    if param and param.group(1) in tool_input and param.group(1) not in PRIMARY_FIELDS:
        name, want = param.group(1), param.group(2).strip()
        got = tool_input[name]
        got = str(got).lower() if isinstance(got, bool) else str(got)
        return fnmatch.fnmatchcase(got, want), ""

    if tool_name in ("Bash", "Monitor"):
        return bash_rule_matches(spec, str(tool_input.get("command", "")))
    if tool_name in EDIT_TOOLS or tool_name == "Read":
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not path:
            return True, "no file_path in tool_input; hook runs regardless of the pattern"
        return path_rule_matches(spec, str(path), cwd, anchor), ""
    if tool_name == "WebFetch" and spec.startswith("domain:"):
        host = (urlparse(str(tool_input.get("url", ""))).hostname or "").lower()
        want = spec[len("domain:"):].strip().lower()
        return fnmatch.fnmatchcase(host, want), ""
    return True, f"hookverdict does not evaluate {rule!r}; assuming the hook runs"


# ---------------------------------------------------------------- Bash

_ASSIGN = re.compile(r"""^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"(?:[^"\\]|\\.)*"|[^\s'"]*)+\s+""")
_KEYWORDS = ("then", "do", "else", "elif", "if", "while", "until", "!", "{", "(")
_SIMPLE_WRAPPERS = {"nohup", "builtin", "noglob"}


class Unparseable(Exception):
    pass


def split_bash(command: str) -> List[str]:
    """Split a shell command into subcommands, including ones inside $() and backticks.

    Separators: && || ; | |& & and newlines. Raises Unparseable on unbalanced quotes,
    unbalanced $( ), or an operator with nothing after it.
    """
    out: List[str] = []
    _split_into(command, out)
    return [s for s in out if s]


def _split_into(cmd: str, out: List[str]) -> None:
    cur: List[str] = []
    i, n = 0, len(cmd)
    quote: Optional[str] = None
    last_op = False

    def flush():
        s = "".join(cur).strip()
        cur.clear()
        if s:
            out.append(s)
        return bool(s)

    while i < n:
        c = cmd[i]
        if quote == "'":
            cur.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if quote == '"' and c == '"':
            quote = None
            cur.append(c)
            i += 1
            continue
        if c == "$" and cmd.startswith("$(", i) and not cmd.startswith("$((", i):
            end = _find_close(cmd, i + 2)
            inner = cmd[i + 2:end]
            _split_into(inner, out)
            cur.append(cmd[i:end + 1])
            i = end + 1
            last_op = False
            continue
        if c == "`":
            end = cmd.find("`", i + 1)
            if end == -1:
                raise Unparseable("unbalanced backtick")
            _split_into(cmd[i + 1:end], out)
            cur.append(cmd[i:end + 1])
            i = end + 1
            last_op = False
            continue
        if quote == '"':
            cur.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            i += 1
            continue
        op = None
        for cand in ("&&", "||", "|&", ";;", "|", ";", "&", "\n"):
            if cmd.startswith(cand, i):
                op = cand
                break
        if op == "&" and (cmd.startswith("&>", i) or (i > 0 and cmd[i - 1] in "<>")):
            op = None  # redirection such as &> or 2>&1
        if op:
            had = flush()
            if op in ("&&", "||", "|", "|&") and not had and not out:
                raise Unparseable(f"{op!r} with nothing before it")
            last_op = op in ("&&", "||", "|", "|&")
            i += len(op)
            continue
        if not c.isspace():
            last_op = False
        cur.append(c)
        i += 1
    if quote:
        raise Unparseable("unbalanced quote")
    flush()
    if last_op:
        raise Unparseable("operator with nothing after it")


def _find_close(cmd: str, start: int) -> int:
    depth, i, quote = 1, start, None
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Unparseable("unbalanced $(")


def normalize_subcommand(sub: str) -> str:
    """Strip leading assignments, control-flow keywords, subshell parens and wrappers."""
    s = sub.strip()
    for _ in range(20):
        before = s
        s = s.lstrip("({").strip()
        s = re.sub(r"[)}]\s*$", "", s).strip() if s.count(")") > s.count("(") or s.endswith("}") else s
        m = _ASSIGN.match(s)
        if m:
            s = s[m.end():]
        words = s.split(None, 1)
        if not words:
            return ""
        head, rest = words[0], (words[1] if len(words) > 1 else "")
        if head in _KEYWORDS or head in _SIMPLE_WRAPPERS:
            s = rest
        elif head == "command" and not rest.startswith(("-v", "-V")):
            s = rest
        elif head == "time":
            s = re.sub(r"^-p\s+", "", rest)
        elif head == "nice":
            s = re.sub(r"^(?:-n\s*-?\d+|-\d+|--adjustment=-?\d+)\s+", "", rest)
        elif head == "timeout":
            s = re.sub(r"^(?:(?:-[a-zA-Z]\s+\S+|--\S+|-[a-zA-Z]+)\s+)*\S+\s+", "", rest)
        elif head == "stdbuf":
            s = re.sub(r"^(?:(?:-[ioe]\s*\S+|--\S+)\s+)+", "", rest)
        elif head == "xargs" and rest and not rest.startswith("-"):
            s = rest
        s = s.strip()
        if s == before:
            break
    return s


def bash_pattern_matches(pattern: str, sub: str) -> bool:
    p = pattern.strip()
    if p.endswith(":*"):
        p = p[:-2] + " *"
    if "*" not in p:
        return " ".join(sub.split()) == " ".join(p.split())
    rx = "^" + ".*".join(re.escape(part) for part in p.split("*")) + "$"
    if re.match(rx, sub, re.S):
        return True
    return p.endswith(" *") and p.count("*") == 1 and sub == p[:-2]


def _names_more_than_program(pattern: str) -> bool:
    p = pattern.strip()
    if p.endswith(":*"):
        p = p[:-2] + " *"
    literal = p.split("*", 1)[0].strip()
    return " " in literal


def bash_rule_matches(pattern: str, command: str) -> Tuple[bool, str]:
    try:
        subs = split_bash(command)
    except Unparseable as exc:
        return True, f"command is unparseable ({exc}); Claude Code runs the hook regardless"
    for sub in subs:
        if bash_pattern_matches(pattern, normalize_subcommand(sub)):
            return True, ""
    dynamic = "$(" in command or "`" in command or re.search(r"\$[A-Za-z_{]", command)
    if dynamic and _names_more_than_program(pattern):
        return True, "pattern names more than the program and the command uses $()/backticks/$VAR; the hook runs anyway"
    return False, ""


# ---------------------------------------------------------------- paths

def _glob_to_regex(glob: str) -> str:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def path_rule_matches(spec: str, path: str, cwd: str, anchor: str) -> bool:
    """gitignore-style match of a Read/Edit specifier against an absolute or cwd-relative path.

    ``//x`` absolute, ``~/x`` home, ``/x`` relative to ``anchor`` (the settings source),
    anything else relative to ``cwd``. A pattern without a slash matches at any depth.
    """
    spec = spec.strip()
    if spec.startswith("//"):
        base, pat = "/", spec[2:]
    elif spec.startswith("~/"):
        base, pat = os.path.expanduser("~"), spec[2:]
    elif spec.startswith("/"):
        base, pat = anchor, spec[1:]
    else:
        base, pat = cwd, spec[2:] if spec.startswith("./") else spec
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    full = os.path.normpath(full)
    base = os.path.normpath(base)
    if base == "/":
        rel = full.lstrip("/")
    else:
        if not (full == base or full.startswith(base + os.sep)):
            return False
        rel = os.path.relpath(full, base)
    if pat.endswith("/"):
        pat += "**"
    if "/" not in pat.rstrip("/"):
        pat = "**/" + pat
    return re.fullmatch(_glob_to_regex(pat), rel, re.S) is not None
