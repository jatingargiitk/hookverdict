"""Load hook handlers from Claude Code settings files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Handler:
    event: str
    source: str          # user | project | local | file
    path: str            # settings file it came from
    anchor: str          # where '/x' paths in 'if' rules anchor
    group: int
    index: int
    matcher: Optional[str]
    raw: Dict[str, Any]

    @property
    def type(self) -> str:
        return str(self.raw.get("type", ""))

    @property
    def command(self) -> str:
        return str(self.raw.get("command", ""))

    @property
    def args(self) -> Optional[List[str]]:
        a = self.raw.get("args")
        return [str(x) for x in a] if isinstance(a, list) else None

    @property
    def if_rule(self) -> Optional[str]:
        v = self.raw.get("if")
        return str(v) if v is not None else None

    @property
    def on_failure(self) -> Optional[str]:
        return self.raw.get("onFailure")

    @property
    def is_async(self) -> bool:
        return bool(self.raw.get("async") or self.raw.get("asyncRewake"))

    @property
    def label(self) -> str:
        if self.type == "command":
            text = self.command
            if self.args is not None:
                text = " ".join([self.command] + self.args)
        else:
            text = self.raw.get("url") or self.raw.get("prompt") or self.raw.get("tool") or ""
        text = str(text).replace("\n", " ")
        return text if len(text) <= 70 else text[:67] + "..."

    @property
    def where(self) -> str:
        return f"{self.source}:{self.event}[{self.group}].hooks[{self.index}]"

    def identity(self) -> str:
        return json.dumps(self.raw, sort_keys=True)


@dataclass
class Config:
    handlers: List[Handler] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    disable_all: bool = False


def default_sources(project_dir: str, include_user: bool = True):
    home = os.path.expanduser("~")
    out = []
    if include_user:
        out.append(("user", os.path.join(home, ".claude", "settings.json"), os.path.join(home, ".claude")))
    out.append(("project", os.path.join(project_dir, ".claude", "settings.json"), project_dir))
    out.append(("local", os.path.join(project_dir, ".claude", "settings.local.json"), project_dir))
    return out


def load(project_dir: str, settings_files: Optional[List[str]] = None, include_user: bool = True) -> Config:
    """Settings files are read in precedence order low -> high (user, project, local).

    ``settings_files`` replaces the default lookup with explicit files (source 'file').
    """
    cfg = Config()
    if settings_files:
        sources = [("file", os.path.abspath(p), os.path.dirname(os.path.abspath(p))) for p in settings_files]
    else:
        sources = default_sources(project_dir, include_user)
    for source, path, anchor in sources:
        if not os.path.isfile(path):
            if source == "file":
                cfg.problems.append(f"{path}: file not found")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            cfg.problems.append(f"{path}: cannot parse JSON ({exc})")
            continue
        cfg.files.append(path)
        if not isinstance(data, dict):
            cfg.problems.append(f"{path}: top level is not an object")
            continue
        if "disableAllHooks" in data:
            cfg.disable_all = bool(data["disableAllHooks"])
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            cfg.problems.append(f"{path}: 'hooks' must be an object keyed by event name")
            continue
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                cfg.problems.append(f"{path}: hooks.{event} must be a list of matcher groups")
                continue
            for gi, group in enumerate(groups):
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    cfg.problems.append(f"{path}: hooks.{event}[{gi}] needs a 'hooks' list")
                    continue
                matcher = group.get("matcher")
                if matcher is not None and not isinstance(matcher, str):
                    cfg.problems.append(f"{path}: hooks.{event}[{gi}].matcher must be a string")
                    matcher = str(matcher)
                for hi, raw in enumerate(group["hooks"]):
                    if not isinstance(raw, dict):
                        cfg.problems.append(f"{path}: hooks.{event}[{gi}].hooks[{hi}] is not an object")
                        continue
                    cfg.handlers.append(Handler(event, source, path, anchor, gi, hi, matcher, raw))
    return cfg
