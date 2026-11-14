"""Static checks on hook configuration: mistakes that make a hook silently not run or not block."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import List

from .config import Config, Handler
from .matching import (KNOWN_EVENTS, NO_MATCHER_EVENTS, TOOL_EVENTS, matcher_is_all, matcher_kind)
from .verdict import ON_FAILURE_IGNORED

KNOWN_TOOLS = ["Agent", "AskUserQuestion", "Bash", "Edit", "ExitPlanMode", "Glob", "Grep", "Monitor",
               "MultiEdit", "NotebookEdit", "PowerShell", "Read", "Skill", "Task", "TodoWrite",
               "WebFetch", "WebSearch", "Write"]
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "deno", "bun", "uv", "uvx", "npx"}


@dataclass
class Finding:
    level: str      # error | warn
    where: str
    message: str


def _expand(s: str, project_dir: str) -> str:
    s = s.replace("${CLAUDE_PROJECT_DIR}", project_dir).replace("$CLAUDE_PROJECT_DIR", project_dir)
    return os.path.expanduser(s)


def _script_path(h: Handler, project_dir: str):
    """Best guess at the script file a command hook runs, or None."""
    if h.args is not None:
        parts = [h.command] + h.args
    else:
        try:
            parts = shlex.split(h.command)
        except ValueError:
            return None
    parts = [p for p in parts if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p)]
    if not parts:
        return None
    first = parts[0]
    if os.path.basename(first) in INTERPRETERS and len(parts) > 1:
        cand = next((p for p in parts[1:] if not p.startswith("-")), None)
        if cand and ("/" in cand or cand.endswith((".py", ".js", ".sh", ".ts", ".rb"))):
            return _expand(cand, project_dir), False
        return None
    if "/" in first and "$" not in _expand(first, project_dir):
        return _expand(first, project_dir), True
    return None


def lint(cfg: Config, project_dir: str) -> List[Finding]:
    out = [Finding("error", "settings", p) for p in cfg.problems]
    if cfg.disable_all:
        out.append(Finding("warn", "settings", "disableAllHooks is true: no hook runs"))
    for h in cfg.handlers:
        w = f"{os.path.relpath(h.path, project_dir) if h.path.startswith(project_dir) else h.path} {h.event}[{h.group}].hooks[{h.index}]"
        if h.event not in KNOWN_EVENTS:
            close = [e for e in KNOWN_EVENTS if e.lower() == h.event.lower()]
            hint = f" (did you mean {close[0]}?)" if close else ""
            out.append(Finding("error", w, f"unknown hook event {h.event!r}{hint}; this hook never runs"))
        m = h.matcher
        if h.event in NO_MATCHER_EVENTS and not matcher_is_all(m):
            out.append(Finding("warn", w, f"{h.event} has no matcher support; matcher {m!r} is silently ignored and the hook fires every time"))
        elif not matcher_is_all(m):
            kind = matcher_kind(m, h.event)
            if kind == "regex":
                try:
                    re.compile(m)
                except re.error as exc:
                    out.append(Finding("error", w, f"matcher {m!r} is not a valid regex: {exc}"))
            elif h.event in TOOL_EVENTS:
                for name in [n.strip() for n in re.split(r"[|,]", m) if n.strip()]:
                    if name.startswith("mcp__") and name.count("__") < 2:
                        out.append(Finding("warn", w, f"matcher {name!r} is compared as an exact tool name and matches no MCP tool; use {name}__.*"))
                    else:
                        case = [t for t in KNOWN_TOOLS if t.lower() == name.lower() and t != name]
                        if case:
                            out.append(Finding("warn", w, f"matcher {name!r} is case-sensitive; the tool is {case[0]!r}"))
        typ = h.raw.get("type")
        if typ not in ("command", "http", "mcp_tool", "prompt", "agent"):
            out.append(Finding("error", w, f"handler type {typ!r} is not one of command/http/mcp_tool/prompt/agent"))
            continue
        if h.if_rule is not None:
            if h.event not in TOOL_EVENTS:
                out.append(Finding("warn", w, f"'if' is only evaluated on tool events; on {h.event} this handler never runs"))
            if re.search(r"&&|\|\||\)\s*,\s*\w+\(", h.if_rule):
                out.append(Finding("error", w, f"'if' holds exactly one permission rule; {h.if_rule!r} combines several"))
        t = h.raw.get("timeout")
        if t is not None and not (isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0):
            out.append(Finding("error", w, f"timeout must be a positive number of seconds (got {t!r})"))
        of = h.raw.get("onFailure")
        if of is not None and of not in ("continue", "block"):
            out.append(Finding("error", w, f"onFailure must be 'continue' or 'block' (got {of!r})"))
        elif of == "block" and (h.event in ON_FAILURE_IGNORED or h.is_async):
            out.append(Finding("warn", w, f"onFailure 'block' has no effect on {'async hooks' if h.is_async else h.event}"))
        if "once" in h.raw:
            out.append(Finding("warn", w, "'once' is only honored in skill frontmatter; ignored in settings files"))
        if typ != "command":
            continue
        if not h.command:
            out.append(Finding("error", w, "command hook has no 'command'"))
            continue
        if h.args is not None and " " in h.command and "/" not in h.command:
            out.append(Finding("warn", w, f"exec form: {h.command!r} is resolved as one executable name; move extra words into 'args'"))
        sp = _script_path(h, project_dir)
        if sp:
            path, needs_exec = sp
            if not os.path.exists(path):
                out.append(Finding("error", w, f"script not found: {path} (the hook fails to start, which does not block)"))
            elif needs_exec and not os.access(path, os.X_OK):
                out.append(Finding("error", w, f"script is not executable: {path} (chmod +x)"))
    return out
