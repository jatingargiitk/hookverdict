import json

from hookverdict.runner import Run
from hookverdict.verdict import classify_stdout, combine, interpret


def j(obj):
    return json.dumps(obj)


def deny(reason="no"):
    return j({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": reason}})


def test_classify_stdout():
    assert classify_stdout("")[0] == "empty"
    assert classify_stdout("hello")[0] == "text"
    assert classify_stdout('log line\n{"a": 1}')[0] == "text"
    assert classify_stdout("[1, 2]")[0] == "text"
    assert classify_stdout('{"a": 1}')[0] == "json"
    assert classify_stdout('{"a": }')[0] == "invalid"
    assert classify_stdout('{"continue": true}\n{"decision": "block"}')[0] == "invalid"
    assert classify_stdout('{}\n{}')[0] == "text"


def test_exit_table_pretooluse():
    ev = "PreToolUse"
    assert interpret(Run(exit_code=0, stdout=deny()), ev, None).status == "ok"
    assert interpret(Run(exit_code=2, stderr="nope"), ev, None).status == "block"
    one = interpret(Run(exit_code=1, stderr="nope"), ev, None)
    assert one.status == "error" and any("exit 1 does NOT block" in n for n in one.notes)
    # Exit 1 with valid JSON: the JSON decides.
    o = interpret(Run(exit_code=1, stdout=deny()), ev, None)
    assert o.status == "ok" and o.fields
    # ...unless onFailure is block, then it is a failure that blocks.
    assert interpret(Run(exit_code=1, stdout=deny()), ev, "block").status == "block"
    # Exit 0 with broken JSON is a non-blocking error.
    assert interpret(Run(exit_code=0, stdout='{"x": }'), ev, None).status == "error"
    assert interpret(Run(exit_code=0, stdout='{"x": }'), ev, "block").status == "block"


def test_failures():
    assert interpret(Run(timed_out=True, timeout=5), "PreToolUse", None).status == "error"
    assert interpret(Run(timed_out=True, timeout=5), "PreToolUse", "block").status == "block"
    assert interpret(Run(start_error="executable not found: x"), "PreToolUse", "block").status == "block"
    # onFailure has no effect on Stop.
    assert interpret(Run(timed_out=True, timeout=5), "Stop", "block").status == "error"


def test_exit2_by_event():
    assert interpret(Run(exit_code=2, stderr="x"), "PostToolUse", None).status == "feedback"
    assert interpret(Run(exit_code=2, stderr="x"), "Notification", None).status == "ignored"
    assert interpret(Run(exit_code=2, stderr="x"), "UserPromptSubmit", None).status == "block"
    missing = interpret(Run(exit_code=2, stderr="/bin/sh: hook.sh: No such file or directory"), "Stop", None)
    assert missing.status == "error"


def test_exit2_uses_json_reason():
    o = interpret(Run(exit_code=2, stdout=deny("json reason"), stderr="stderr reason"), "PreToolUse", None)
    assert o.reason == "json reason"


def test_schema_errors():
    bad = j({"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "deny"}})
    assert interpret(Run(exit_code=0, stdout=bad), "PreToolUse", None).status == "error"
    bad = j({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "nope"}})
    assert interpret(Run(exit_code=0, stdout=bad), "PreToolUse", None).status == "error"
    o = interpret(Run(exit_code=0, stdout=j({"permissionDecision": "deny"})), "PreToolUse", None)
    assert any("belongs inside hookSpecificOutput" in n for n in o.notes)


def _ok(stdout, ev="PreToolUse"):
    return interpret(Run(exit_code=0, stdout=stdout), ev, None)


def test_precedence_deny_defer_ask_allow():
    def pd(d):
        return _ok(j({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": d}}))
    assert combine("PreToolUse", [("a", pd("allow")), ("b", pd("ask"))]).decision == "ask"
    assert combine("PreToolUse", [("a", pd("ask")), ("b", pd("defer"))]).decision == "defer"
    assert combine("PreToolUse", [("a", pd("allow")), ("b", pd("deny"))]).decision == "deny"
    assert combine("PreToolUse", []).decision == "none"


def test_deprecated_pretooluse_decision():
    v = combine("PreToolUse", [("a", _ok(j({"decision": "approve", "reason": "ok"})))])
    assert v.decision == "allow"


def test_continue_false_overrides():
    v = combine("PreToolUse", [("a", _ok(j({"continue": False, "stopReason": "halt"}))),
                               ("b", _ok(deny()))])
    assert v.decision == "stop" and v.stop_reason == "halt"


def test_top_level_block_and_context():
    v = combine("Stop", [("a", _ok(j({"decision": "block", "reason": "run tests"}), "Stop"))])
    assert v.decision == "block" and v.reason == "run tests"
    v = combine("UserPromptSubmit", [("a", _ok("Use pnpm.", "UserPromptSubmit"))])
    assert v.decision == "none" and v.additional_context == ["Use pnpm."]
