"""Turn handler runs into the outcome Claude Code would reach.

Encodes the "Exit code output" table, "Exit code 2 behavior per event", "JSON output",
"Decision control" and "Block the action when a hook fails" sections of
https://code.claude.com/docs/en/hooks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .runner import Run

# Events where exit 2 blocks / stops the action.
CAN_BLOCK = {
    "PreToolUse", "UserPromptSubmit", "UserPromptExpansion", "Stop", "SubagentStop",
    "TeammateIdle", "TaskCreated", "TaskCompleted", "ConfigChange", "PostToolBatch",
    "PreCompact", "PreModelSwitch", "Elicitation", "ElicitationResult",
    "WorktreeCreate", "WorktreeRemove",
}
# Exit 2 shows stderr to Claude but cannot undo the action.
EXIT2_FEEDBACK = {"PostToolUse", "PostToolUseFailure"}

# Events that read a top-level {"decision": "block", "reason": ...}.
TOP_LEVEL_DECISION = {
    "UserPromptSubmit", "UserPromptExpansion", "PostToolUse", "PostToolUseFailure",
    "PostToolBatch", "Stop", "SubagentStop", "ConfigChange", "PreCompact", "TaskCreated",
}

# onFailure has no effect here.
ON_FAILURE_IGNORED = {"Stop", "SubagentStop", "TaskCompleted", "TeammateIdle"}

# Plain-text stdout on exit 0 is added to Claude's context.
STDOUT_IS_CONTEXT = {"UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PostModelSwitch"}

PRE_TOOL_ORDER = ["deny", "defer", "ask", "allow"]

BLOCK_MEANING = {
    "PreToolUse": "the tool call is blocked",
    "UserPromptSubmit": "the prompt is rejected and never reaches Claude",
    "Stop": "Claude is prevented from stopping and keeps working",
    "SubagentStop": "the subagent is prevented from stopping",
    "PostToolUse": "the reason is added next to the tool result (the tool already ran)",
    "PostToolUseFailure": "the reason is added next to the failure (the tool already failed)",
    "PreCompact": "compaction is blocked",
    "ConfigChange": "the configuration change does not take effect",
    "TaskCompleted": "the task is not marked completed",
    "TaskCreated": "the task creation is rolled back",
    "TeammateIdle": "the teammate keeps working",
    "PostToolBatch": "the agentic loop stops before the next model call",
}


@dataclass
class Outcome:
    """What one handler contributed."""
    status: str                     # ok | block | error | skipped | ignored
    stdout_kind: str = "empty"      # empty | text | json | invalid
    fields: Optional[Dict[str, Any]] = None
    reason: str = ""
    context: str = ""
    notes: List[str] = field(default_factory=list)


def classify_stdout(stdout: str) -> Tuple[str, Optional[dict], str]:
    s = stdout.strip()
    if not s:
        return "empty", None, ""
    if not (s.startswith("{") and s.endswith("}")):
        return "text", None, ""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return "json", obj, ""
        return "text", None, ""
    except ValueError as exc:
        lines = [ln for ln in s.splitlines() if ln.strip()]
        if len(lines) >= 2:
            parsed = []
            for ln in lines:
                try:
                    parsed.append(json.loads(ln))
                except ValueError:
                    parsed = None
                    break
            if parsed is not None:
                if any(isinstance(p, dict) and p for p in parsed):
                    return "invalid", None, "several JSON values on stdout; print exactly one JSON object"
                return "text", None, ""
        return "invalid", None, f"stdout looks like JSON but does not parse: {exc}"


def _buried_json(stdout: str) -> bool:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                if isinstance(json.loads(line), dict):
                    return True
            except ValueError:
                pass
    return False


_TYPES = {"continue": bool, "stopReason": str, "suppressOutput": bool, "systemMessage": str,
          "decision": str, "reason": str, "hookSpecificOutput": dict, "terminalSequence": str}


def validate(obj: dict, event: str) -> Tuple[List[str], List[str]]:
    """Return (schema_errors, warnings)."""
    errors, warnings = [], []
    for key, typ in _TYPES.items():
        if key in obj and not isinstance(obj[key], typ):
            errors.append(f"'{key}' must be a {typ.__name__}")
    hso = obj.get("hookSpecificOutput")
    if isinstance(hso, dict):
        name = hso.get("hookEventName")
        if name != event:
            errors.append(f"hookSpecificOutput.hookEventName must be {event!r} (got {name!r})")
        if event == "PreToolUse":
            pd = hso.get("permissionDecision")
            if pd is not None and pd not in PRE_TOOL_ORDER:
                errors.append(f"permissionDecision must be one of allow/deny/ask/defer (got {pd!r})")
            if "updatedInput" in hso and not isinstance(hso["updatedInput"], dict):
                errors.append("updatedInput must be an object")
    for key in ("permissionDecision", "permissionDecisionReason", "additionalContext", "updatedInput"):
        if key in obj:
            warnings.append(f"top-level '{key}' is ignored; it belongs inside hookSpecificOutput")
    dec = obj.get("decision")
    if event == "PreToolUse" and dec is not None:
        if dec in ("approve", "block"):
            warnings.append(f"top-level decision {dec!r} is deprecated for PreToolUse; use hookSpecificOutput.permissionDecision")
        else:
            errors.append(f"decision {dec!r} is not valid for PreToolUse")
    elif dec is not None and event in TOP_LEVEL_DECISION and dec != "block":
        warnings.append(f"decision {dec!r} has no effect; the only value is 'block' (omit it to allow)")
    elif dec is not None and event not in TOP_LEVEL_DECISION and event != "PreToolUse":
        warnings.append(f"{event} does not read a top-level 'decision'")
    if event in ("Stop", "SubagentStop") and dec == "block" and not obj.get("reason"):
        warnings.append("'reason' is required when a Stop hook returns decision 'block'")
    return errors, warnings


def _json_blocks(obj: dict, event: str) -> Optional[str]:
    """Reason string if the JSON itself makes a blocking decision, else None."""
    if event == "PreToolUse":
        hso = obj.get("hookSpecificOutput") or {}
        if hso.get("permissionDecision") == "deny":
            return str(hso.get("permissionDecisionReason", ""))
        if obj.get("decision") == "block":
            return str(obj.get("reason", ""))
        return None
    if event in TOP_LEVEL_DECISION and obj.get("decision") == "block":
        return str(obj.get("reason", ""))
    return None


def interpret(run: Run, event: str, on_failure: Optional[str], is_async: bool = False) -> Outcome:
    """Apply the exit-code x stdout table to one handler run."""
    block_on_failure = on_failure == "block" and event not in ON_FAILURE_IGNORED and not is_async
    can_block = event in CAN_BLOCK

    def failure(why: str) -> Outcome:
        if block_on_failure:
            o = Outcome("block", reason=why)
            o.notes.append(f'{why}; blocking because onFailure is "block"')
            return o
        o = Outcome("error", reason=why)
        o.notes.append(f"{why}; non-blocking error, the action goes ahead")
        return o

    if run.start_error:
        return failure(f"hook could not start ({run.start_error})")
    if run.timed_out:
        o = failure(f"timed out after {run.timeout:g}s; output discarded")
        return o

    kind, obj, perr = classify_stdout(run.stdout)
    errors, warnings = validate(obj, event) if obj is not None else ([], [])
    if errors:
        kind, perr = "invalid", "; ".join(errors)
    stderr_first = run.stderr.strip().splitlines()[0] if run.stderr.strip() else ""
    code = run.exit_code

    if code == 2:
        if event in ("Stop", "SubagentStop", "TaskCompleted") and kind == "empty" and "No such file or directory" in run.stderr:
            o = Outcome("error", kind, reason=stderr_first)
            o.notes.append("exit 2 with a missing-file error on stderr counts as a non-blocking error on this event")
            return o
        fields = obj if kind == "json" else None
        reason = (_json_blocks(obj, event) if fields else None) or run.stderr.strip()
        if can_block:
            o = Outcome("block", kind, fields, reason)
            o.notes.append("exit 2: blocking error" + ("; JSON fields are read but cannot override the block" if fields else ""))
        elif event in EXIT2_FEEDBACK:
            o = Outcome("ok", kind, fields, reason)
            o.notes.append("exit 2 on this event shows stderr to Claude; it cannot undo the action")
            o.status = "feedback"
        else:
            o = Outcome("ignored", kind, fields, reason)
            o.notes.append(f"exit 2 does not block {event}")
        o.notes.extend(warnings)
        if kind == "invalid":
            o.notes.append(f"invalid JSON ignored: {perr}")
        return o

    if code == 0:
        if kind == "invalid":
            o = failure(f"invalid hook output ({perr})")
            o.stdout_kind = "invalid"
            return o
        o = Outcome("ok", kind, obj if kind == "json" else None)
        o.notes.extend(warnings)
        if kind == "text" and _buried_json(run.stdout):
            o.notes.append("stdout has a JSON object mixed with other text, so Claude Code reads it all as "
                           "plain text and ignores the JSON; print only the JSON object (send logs to stderr)")
        if kind == "text" and event in STDOUT_IS_CONTEXT:
            o.context = run.stdout.strip()
            o.notes.append("plain-text stdout is added to Claude's context")
        elif kind == "text":
            o.notes.append("plain-text stdout only goes to the debug log")
        return o

    # any other exit code
    if kind == "json":
        if block_on_failure:
            o = failure(f"exit {code}")
            o.stdout_kind, o.fields = "json", None
            return o
        o = Outcome("ok", kind, obj)
        o.notes.append(f"exit {code} is ignored because stdout is a valid JSON object; its fields decide")
        o.notes.extend(warnings)
        return o
    detail = perr if kind == "invalid" else stderr_first
    o = failure(f"exit {code}" + (f": {detail}" if detail else ""))
    o.stdout_kind = kind
    if code == 1 and not block_on_failure:
        o.notes.append("exit 1 does NOT block; a policy hook must exit 2 (or print a JSON decision and exit 0)")
    return o


@dataclass
class Verdict:
    event: str
    decision: str                  # deny|defer|ask|allow|block|feedback|stop|none
    meaning: str
    reason: str = ""
    stop_reason: str = ""
    additional_context: List[str] = field(default_factory=list)
    system_messages: List[str] = field(default_factory=list)
    updated_input: Optional[dict] = None
    decided_by: List[str] = field(default_factory=list)


def combine(event: str, outcomes: List[Tuple[str, Outcome]]) -> Verdict:
    """outcomes: list of (handler label, Outcome) for handlers that ran."""
    v = Verdict(event, "none", "")
    stop = None
    pre: Dict[str, List[Tuple[str, str]]] = {k: [] for k in PRE_TOOL_ORDER}
    blocks: List[Tuple[str, str]] = []
    feedback: List[Tuple[str, str]] = []

    for label, o in outcomes:
        f = o.fields or {}
        if f.get("continue") is False and stop is None:
            stop = (label, str(f.get("stopReason", "")))
        if isinstance(f.get("systemMessage"), str):
            v.system_messages.append(f["systemMessage"])
        hso = f.get("hookSpecificOutput") if isinstance(f.get("hookSpecificOutput"), dict) else {}
        if isinstance(hso.get("additionalContext"), str):
            v.additional_context.append(hso["additionalContext"])
        if o.context:
            v.additional_context.append(o.context)
        if o.status == "block":
            if event == "PreToolUse":
                pre["deny"].append((label, o.reason))
            else:
                blocks.append((label, o.reason))
            continue
        if o.status == "feedback":
            feedback.append((label, o.reason))
        if o.status not in ("ok", "feedback") or not f:
            continue
        if event == "PreToolUse":
            pd = hso.get("permissionDecision")
            reason = str(hso.get("permissionDecisionReason", ""))
            if pd is None and f.get("decision") in ("approve", "block"):
                pd = "allow" if f["decision"] == "approve" else "deny"
                reason = str(f.get("reason", ""))
            if pd in pre:
                pre[pd].append((label, reason))
                if isinstance(hso.get("updatedInput"), dict) and pd in ("allow", "ask"):
                    v.updated_input = hso["updatedInput"]
        elif event in TOP_LEVEL_DECISION and f.get("decision") == "block":
            blocks.append((label, str(f.get("reason", ""))))

    if event == "PreToolUse":
        for d in PRE_TOOL_ORDER:
            if pre[d]:
                v.decision = d
                v.decided_by = [lbl for lbl, _ in pre[d]]
                v.reason = "; ".join(r for _, r in pre[d] if r)
                break
        v.meaning = {
            "deny": "the tool call is blocked; Claude sees the reason",
            "defer": "the turn ends so the call can be resumed later (needs -p mode)",
            "ask": "the user is asked to confirm",
            "allow": "the permission prompt is skipped (deny/ask permission rules still apply)",
            "none": "no hook decided; the normal permission flow applies",
        }[v.decision]
    elif blocks:
        v.decision = "block"
        v.decided_by = [lbl for lbl, _ in blocks]
        v.reason = "; ".join(r for _, r in blocks if r)
        v.meaning = BLOCK_MEANING.get(event, f"{event} is blocked")
    elif feedback:
        v.decision = "feedback"
        v.decided_by = [lbl for lbl, _ in feedback]
        v.reason = "; ".join(r for _, r in feedback if r)
        v.meaning = "stderr is shown to Claude; the tool already ran"
    else:
        v.meaning = "no hook blocked; the action goes ahead"

    if stop is not None:
        v.decided_by = [stop[0]]
        v.stop_reason = stop[1]
        v.decision = "stop"
        v.meaning = "continue:false — Claude stops processing entirely (overrides event decisions)"
    return v
