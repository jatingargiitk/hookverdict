#!/usr/bin/env python3
"""Ask before any git push."""
import json
import sys

event = json.load(sys.stdin)
command = event["tool_input"].get("command", "")
print(f"guard-push: checking {command!r}")
if "git push" in command:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "git push needs a human",
        }
    }))
