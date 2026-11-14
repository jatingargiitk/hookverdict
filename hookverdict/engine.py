"""Select the handlers that fire for an event, run them, and combine the results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config, Handler
from .matching import MATCH_FIELD, TOOL_EVENTS, if_matches, matcher_matches
from .runner import Run, run_handler
from .verdict import Outcome, Verdict, combine, interpret

EVENT_DEFAULTS = {
    "SessionStart": {"source": "startup"},
    "SessionEnd": {"reason": "other"},
    "PreCompact": {"trigger": "manual"},
    "PostCompact": {"trigger": "manual"},
    "Stop": {"stop_hook_active": False, "last_assistant_message": ""},
    "SubagentStop": {"stop_hook_active": False, "agent_type": "general-purpose", "last_assistant_message": ""},
    "UserPromptSubmit": {"prompt": ""},
    "Notification": {"message": "", "notification_type": "permission_prompt"},
}


def build_payload(event: str, project_dir: str, extra: Optional[dict] = None) -> dict:
    p = {
        "session_id": "hookverdict",
        "transcript_path": "",
        "cwd": project_dir,
        "permission_mode": "default",
        "hook_event_name": event,
    }
    if event in TOOL_EVENTS:
        p.update({"tool_name": "", "tool_input": {}, "tool_use_id": "toolu_hookverdict"})
        if event == "PostToolUse":
            p["tool_response"] = {}
    p.update(EVENT_DEFAULTS.get(event, {}))
    if extra:
        p.update(extra)
    p["hook_event_name"] = event
    return p


@dataclass
class Step:
    handler: Handler
    fires: bool
    why: str = ""                       # why it was skipped / notes from matching
    run: Optional[Run] = None
    outcome: Optional[Outcome] = None


@dataclass
class Result:
    event: str
    payload: dict
    steps: List[Step] = field(default_factory=list)
    verdict: Optional[Verdict] = None
    disabled: bool = False


def evaluate(cfg: Config, event: str, payload: dict, project_dir: str, execute: bool = True,
             max_timeout: Optional[float] = None, extra_env: Optional[Dict[str, str]] = None) -> Result:
    res = Result(event, payload)
    if cfg.disable_all:
        res.disabled = True
        res.verdict = Verdict(event, "none", "disableAllHooks is true; no hook runs")
        return res
    seen = set()
    field_name = MATCH_FIELD.get(event)
    value = payload.get(field_name) if field_name else None
    for h in cfg.handlers:
        if h.event != event:
            continue
        ok, note = matcher_matches(h.matcher, value, event)
        if not ok:
            res.steps.append(Step(h, False, note or f"matcher {h.matcher!r} does not match {value!r}"))
            continue
        notes = [note] if note else []
        if h.if_rule is not None:
            runs, inote = if_matches(h.if_rule, event, str(payload.get("tool_name", "")),
                                     payload.get("tool_input") or {}, payload.get("cwd") or project_dir, h.anchor)
            if not runs:
                res.steps.append(Step(h, False, inote or f"if {h.if_rule!r} does not match"))
                continue
            if inote:
                notes.append(inote)
        ident = h.identity()
        if ident in seen:
            res.steps.append(Step(h, False, "same handler already defined in another settings file; it runs once"))
            continue
        seen.add(ident)
        step = Step(h, True, "; ".join(notes))
        if h.type != "command":
            step.fires = False
            step.why = f"type {h.type!r} handlers are not executed by hookverdict (only 'command')"
        elif h.is_async:
            step.fires = False
            step.why = "async hook: runs in the background and cannot affect the decision"
        res.steps.append(step)
        if step.fires and execute:
            step.run = run_handler(h, payload, project_dir, max_timeout, extra_env)
            step.outcome = interpret(step.run, event, h.on_failure, h.is_async)
    ran = [(s.handler.label, s.outcome) for s in res.steps if s.outcome is not None]
    res.verdict = combine(event, ran)
    return res
