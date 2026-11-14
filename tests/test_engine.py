import json
import os
import shutil
import sys

from conftest import cmd, pre_bash

from hookverdict.cli import main
from hookverdict.config import load
from hookverdict.engine import build_payload, evaluate
from hookverdict.lint import lint

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")

DENY_PY = """#!/usr/bin/env python3
import json, sys
e = json.load(sys.stdin)
if "prod" in e["tool_input"].get("command", ""):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": "no prod from agents"}}))
"""


def run(project_dir, event="PreToolUse", tool="Bash", tool_input=None, **kw):
    cfg = load(project_dir, include_user=False)
    payload = build_payload(event, project_dir, {"tool_name": tool, "tool_input": tool_input or {}, **kw})
    return evaluate(cfg, event, payload, project_dir, max_timeout=10)


def test_json_deny_end_to_end(project):
    d = project(pre_bash(cmd("deny.py")), {"deny.py": DENY_PY})
    r = run(d, tool_input={"command": "kubectl --context prod delete ns x"})
    assert r.verdict.decision == "deny" and r.verdict.reason == "no prod from agents"
    assert run(d, tool_input={"command": "kubectl get pods"}).verdict.decision == "none"


def test_payload_reaches_hook_on_stdin(project, tmp_path):
    out = tmp_path / "seen.json"
    d = project(pre_bash({"type": "command", "command": f"cat > {out}"}))
    run(d, tool_input={"command": "ls"})
    seen = json.loads(out.read_text())
    assert seen["hook_event_name"] == "PreToolUse" and seen["tool_input"] == {"command": "ls"}
    assert seen["cwd"] == d


def test_matcher_and_if_skip(project):
    d = project({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "exit 2"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "if": "Bash(git push *)", "command": "exit 2"}]},
    ]}})
    r = run(d, tool_input={"command": "git status"})
    assert r.verdict.decision == "none"
    assert [s.run is None for s in r.steps] == [True, True]
    assert run(d, tool_input={"command": "git push origin main"}).verdict.decision == "deny"


def test_exec_form_with_args(project):
    d = project(pre_bash({"type": "command", "command": sys.executable,
                          "args": ["-c", "import sys; sys.stderr.write('exec form'); sys.exit(2)"]}))
    r = run(d, tool_input={"command": "ls"})
    assert r.verdict.decision == "deny" and r.verdict.reason == "exec form"


def test_missing_script_and_onfailure(project):
    d = project(pre_bash(cmd("nope.sh")))
    r = run(d, tool_input={"command": "ls"})
    assert r.verdict.decision == "none" and r.steps[0].outcome.status == "error"
    d = project(pre_bash(cmd("nope.sh", onFailure="block")))
    assert run(d, tool_input={"command": "ls"}).verdict.decision == "deny"


def test_timeout(project):
    d = project(pre_bash({"type": "command", "command": "sleep 5", "timeout": 1}))
    r = run(d, tool_input={"command": "ls"})
    assert r.steps[0].run.timed_out and r.verdict.decision == "none"


def test_same_handler_in_two_files_runs_once(project, tmp_path):
    h = {"type": "command", "command": "echo hi >> " + str(tmp_path / "count")}
    d = project(pre_bash(h))
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps(pre_bash(h)))
    r = run(d, tool_input={"command": "ls"})
    assert (tmp_path / "count").read_text() == "hi\n"
    assert "runs once" in r.steps[1].why


def test_disable_all_hooks(project, tmp_path):
    d = project(pre_bash({"type": "command", "command": "exit 2"}))
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({"disableAllHooks": True}))
    r = run(d, tool_input={"command": "ls"})
    assert r.disabled and r.verdict.decision == "none"


def test_non_command_handlers_are_listed_not_run(project):
    d = project(pre_bash({"type": "prompt", "prompt": "is this safe?"}, {"type": "command", "command": "true", "async": True}))
    r = run(d, tool_input={"command": "ls"})
    assert all(s.run is None for s in r.steps)
    assert "not executed" in r.steps[0].why and "async" in r.steps[1].why


def test_stop_hook_block(project):
    d = project({"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "echo '{\"decision\": \"block\", \"reason\": \"tests not run\"}'"}]}]}})
    cfg = load(d, include_user=False)
    r = evaluate(cfg, "Stop", build_payload("Stop", d), d)
    assert r.verdict.decision == "block" and r.verdict.reason == "tests not run"


def test_lint_findings(project):
    d = project({"hooks": {
        "Pretooluse": [{"hooks": [{"type": "command", "command": "true"}]}],
        "PreToolUse": [
            {"matcher": "mcp__github", "hooks": [cmd("missing.sh")]},
            {"matcher": "bash", "hooks": [{"type": "command", "command": "true", "if": "Bash(a *) && Bash(b *)"}]},
        ],
        "Stop": [{"matcher": "x", "hooks": [{"type": "command", "command": "true", "if": "Bash(*)", "onFailure": "block"}]}],
    }})
    msgs = [f.message for f in lint(load(d, include_user=False), d)]
    text = "\n".join(msgs)
    assert "unknown hook event 'Pretooluse' (did you mean PreToolUse?)" in text
    assert "matches no MCP tool; use mcp__github__.*" in text
    assert "script not found" in text
    assert "case-sensitive; the tool is 'Bash'" in text
    assert "combines several" in text
    assert "silently ignored" in text
    assert "never runs" in text
    assert "no effect on Stop" in text


def _copy_example(name, tmp_path):
    dst = tmp_path / name
    shutil.copytree(os.path.join(EXAMPLES, name), dst)
    return str(dst)


def test_cli_examples(tmp_path, capsys):
    broken = _copy_example("broken", tmp_path)
    fixed = _copy_example("fixed", tmp_path)
    cases = "hookverdict-cases.json"
    assert main(["test", os.path.join(broken, cases), "-C", broken, "--no-user"]) == 1
    out = capsys.readouterr().out
    assert "1 passed, 3 failed" in out
    assert main(["test", os.path.join(fixed, cases), "-C", fixed, "--no-user"]) == 0
    assert "4 passed, 0 failed" in capsys.readouterr().out
    assert main(["run", "PreToolUse", "--command", "rm -rf build", "-C", broken, "--no-user", "--expect", "deny"]) == 1
    assert "exit 1 does NOT block" in capsys.readouterr().out
    assert main(["run", "PreToolUse", "--command", "rm -rf build", "-C", fixed, "--no-user", "--expect", "deny", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "deny"
    assert main(["lint", "-C", fixed, "--no-user", "--strict"]) == 0
    assert main(["lint", "-C", broken, "--no-user", "--strict"]) == 1
