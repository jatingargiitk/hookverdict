# hookverdict

Run your Claude Code hooks against a fake event and see the verdict Claude Code would reach: **deny, ask, allow, defer, block, or nothing at all**.

Hooks fail quietly. A policy hook that exits `1` instead of `2` doesn't block anything. A script that prints a log line before its JSON gets its decision ignored. A matcher like `mcp__github` matches no tool, and a `matcher` on `UserPromptSubmit` is silently dropped. Claude Code never errors on any of these; your guard just doesn't fire, and you find out when the agent runs the command.

`hookverdict` reads the same settings files Claude Code reads, picks the handlers that would fire (group `matcher`, handler `if` rule), **really runs** your command hooks with the JSON payload on stdin, and applies Claude Code's documented rules for exit codes, stdout parsing, `onFailure`, timeouts and decision precedence. You get a per-handler trace and one verdict.

- `hookverdict run`: fire one event and print the trace and verdict
- `hookverdict test`: a JSON file of cases with expected verdicts, for CI
- `hookverdict lint`: static checks for hooks that silently never run
- `hookverdict list`: what's configured, by event

Python 3.9+ and the standard library only. No Claude Code install or API key needed.

## Install

```bash
pipx install git+https://github.com/jatingargiitk/hookverdict
# or: pip install git+https://github.com/jatingargiitk/hookverdict
```

## Example

[`examples/broken`](examples/broken) is a project with two hooks that look right:

- `no-rm.sh` prints `Blocked: no rm -rf in this repo` to stderr and exits `1`
- `guard-push.py` prints a debug line, then a `permissionDecision: "ask"` JSON for `git push`

Neither one works:

```text
$ hookverdict run PreToolUse --command "rm -rf build"
PreToolUse  tool=Bash  input={"command": "rm -rf build"}
  [project] .claude/settings.json  matcher Bash  if Bash(rm *)
    command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh
    exit 1 (22 ms)
    stderr: Blocked: no rm -rf in this repo
    => error
       - exit 1: Blocked: no rm -rf in this repo; non-blocking error, the action goes ahead
       - exit 1 does NOT block; a policy hook must exit 2 (or print a JSON decision and exit 0)
  [project] .claude/settings.json  matcher Bash
    command: python3 ${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-push.py
    exit 0 (26 ms)
    stdout: guard-push: checking 'rm -rf build'
    => ok
       - plain-text stdout only goes to the debug log
  [project] .claude/settings.json  matcher mcp__github
    command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh
    skipped: matcher 'mcp__github' does not match 'Bash'

VERDICT: NONE — no hook decided; the normal permission flow applies
```

```text
$ hookverdict run PreToolUse --command "npm test && git push origin main"
PreToolUse  tool=Bash  input={"command": "npm test && git push origin main"}
  [project] .claude/settings.json  matcher Bash  if Bash(rm *)
    command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh
    skipped: if 'Bash(rm *)' does not match
  [project] .claude/settings.json  matcher Bash
    command: python3 ${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-push.py
    exit 0 (22 ms)
    stdout: guard-push: checking 'npm test && git push origin main'
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "git push needs a human"}}
    => ok
       - stdout has a JSON object mixed with other text, so Claude Code reads it all as plain text and ignores the JSON; print only the JSON object (send logs to stderr)
       - plain-text stdout only goes to the debug log
  [project] .claude/settings.json  matcher mcp__github
    command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh
    skipped: matcher 'mcp__github' does not match 'Bash'

VERDICT: NONE — no hook decided; the normal permission flow applies
```

Write down what you expect once, and run it in CI:

```json
{
  "cases": [
    { "name": "rm -rf is blocked", "event": "PreToolUse", "tool": "Bash",
      "input": { "command": "rm -rf build" }, "expect": { "verdict": "deny", "reason_contains": "no rm -rf" } },
    { "name": "rm -rf inside $() is blocked", "event": "PreToolUse", "tool": "Bash",
      "input": { "command": "echo $(rm -rf /tmp/x)" }, "expect": "deny" },
    { "name": "git push asks first", "event": "PreToolUse", "tool": "Bash",
      "input": { "command": "npm test && git push origin main" }, "expect": "ask" },
    { "name": "ls is left alone", "event": "PreToolUse", "tool": "Bash",
      "input": { "command": "ls -la" }, "expect": { "verdict": "none", "no_errors": true } }
  ]
}
```

```text
$ hookverdict test hookverdict-cases.json
FAIL  rm -rf is blocked
      verdict: expected deny, got none
      reason: expected to contain 'no rm -rf', got ''
FAIL  rm -rf inside $() is blocked
      verdict: expected deny, got none
FAIL  git push asks first
      verdict: expected ask, got none
ok    ls is left alone  (none)

1 passed, 3 failed
```

`lint` catches the config mistakes without running anything:

```text
$ hookverdict lint
warn   .claude/settings.json PreToolUse[1].hooks[0]: matcher 'mcp__github' is compared as an exact tool name and matches no MCP tool; use mcp__github__.*
warn   .claude/settings.json UserPromptSubmit[0].hooks[0]: UserPromptSubmit has no matcher support; matcher 'deploy' is silently ignored and the hook fires every time
4 handlers in 1 files: 0 errors, 2 warnings
```

[`examples/fixed`](examples/fixed) changes `exit 1` to `exit 2` (and adds `"onFailure": "block"`), sends the debug line to stderr, uses `mcp__github__.*`, and drops the ignored matcher:

```text
$ hookverdict test hookverdict-cases.json
ok    rm -rf is blocked  (deny)
ok    rm -rf inside $() is blocked  (deny)
ok    git push asks first  (ask)
ok    ls is left alone  (none)

4 passed, 0 failed
```

Machine-readable output:

```text
$ hookverdict run PreToolUse --command "rm -rf build" --json | jq "{verdict, reason, decided_by}"
{
  "verdict": "deny",
  "reason": "Blocked: no rm -rf in this repo",
  "decided_by": [
    "${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh"
  ]
}
```

Plain stdout from a `UserPromptSubmit` hook becomes context for Claude:

```text
$ hookverdict run UserPromptSubmit --prompt "deploy to prod"
UserPromptSubmit  prompt="deploy to prod"
  [project] .claude/settings.json  matcher *
    command: echo 'Deploys go through the release checklist in RELEASING.md.'
    exit 0 (1 ms)
    stdout: Deploys go through the release checklist in RELEASING.md.
    => ok
       - plain-text stdout is added to Claude's context

VERDICT: NONE — no hook blocked; the action goes ahead
  context for Claude: Deploys go through the release checklist in RELEASING.md.
```

```text
$ hookverdict list
# .claude/settings.json

PreToolUse
  [project] matcher Bash  if Bash(rm *)  command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh  [onFailure=block]
  [project] matcher Bash  command: python3 ${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-push.py
  [project] matcher mcp__github__.*  command: ${CLAUDE_PROJECT_DIR}/.claude/hooks/no-rm.sh

UserPromptSubmit
  [project] matcher *  command: echo 'Deploys go through the release checklist in RELEASING.md.'
```

## What it models

Rules come from the Claude Code [hooks reference](https://code.claude.com/docs/en/hooks) and [permissions](https://code.claude.com/docs/en/permissions) docs (read 2026-11-14).

| Area | Behaviour |
| :- | :- |
| Settings | `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json` (or `--settings FILE`). Hooks merge across files; an identical handler runs once; `disableAllHooks` honoured. |
| `matcher` | `*`/empty/omitted match all; letters, digits, `_ - , \|` and spaces are exact names (`Edit\|Write`, `Edit, Write`); anything else is an unanchored regex. Ignored on events without matcher support (`UserPromptSubmit`, `Stop`, ...). Tested against `tool_name`, `source`, `reason`, `notification_type`, `agent_type` or `trigger` depending on the event. |
| `if` | Tool events only (a handler with `if` never runs on other events). `Bash(...)` rules are checked against every subcommand (`&&`, `\|\|`, `;`, `\|`, `&`, newlines, `$()` and backticks), with leading `VAR=` assignments and the `timeout`/`time`/`nice`/`nohup`/`stdbuf`/`command`/`builtin`/`noglob`/bare `xargs` wrappers stripped. Trailing ` *` and `:*` also match the bare command. Unparseable commands run the hook, as Claude Code does. `Read(...)`/`Edit(...)` use gitignore-style paths (`//abs`, `~/home`, `/settings-relative`, `cwd-relative`); `Edit` covers Write, MultiEdit and NotebookEdit. `Tool(param:value)` and `WebFetch(domain:...)` are supported. |
| Running | Shell form via `bash -c` (or `sh`), exec form when `args` is set, `${CLAUDE_PROJECT_DIR}` exported and substituted, per-handler `timeout` (defaults 600 s, 30 s on `UserPromptSubmit`), cap with `--timeout`. |
| Exit codes | `0` applies JSON; `2` blocks on events that can block (stderr, or the JSON reason, becomes the reason; JSON can't override it); any other code is a non-blocking error unless stdout is a valid JSON object, whose fields then decide. Exit 2 on `PostToolUse` is feedback only. |
| stdout | Counts as JSON only if it's one object and nothing else. Text before it, or two objects, means the JSON is ignored or rejected. Basic schema checks (`hookEventName` must match the event, `permissionDecision` values, field types). |
| Failures | Can't start, timeout, bad JSON, or exit codes other than 0/2 are non-blocking unless `"onFailure": "block"` (which does nothing on `Stop`/`SubagentStop`/`TaskCompleted`/`TeammateIdle` or async hooks). |
| Verdict | `PreToolUse`: `deny` > `defer` > `ask` > `allow`, deprecated `approve`/`block` mapped. Top-level `decision: "block"` for `UserPromptSubmit`, `PostToolUse`, `Stop`, `SubagentStop`, `PreCompact`, `ConfigChange` and others. `continue: false` overrides everything (`stop`). `additionalContext`, `systemMessage` and `updatedInput` are reported. |

Verdicts: `deny`, `defer`, `ask`, `allow` (PreToolUse), `block`, `feedback` (exit 2 after the tool ran), `stop` (`continue: false`), `none`.

## Case file

A JSON object with a `cases` list (or just the list). Each case:

| Field | Meaning |
| :- | :- |
| `name` | label in the output |
| `event` | hook event, e.g. `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop` |
| `tool`, `input` | `tool_name` and `tool_input` for tool events (`tool` defaults to `Bash`) |
| `prompt` | prompt text for `UserPromptSubmit` |
| `payload` | extra top-level payload fields, e.g. `{"permission_mode": "plan"}` or `{"stop_hook_active": true}` |
| `expect` | a verdict string, or an object with any of `verdict`, `reason_contains`, `context_contains`, `updated_input`, `ran` (number of handlers executed), `no_errors` |

Payloads get `session_id`, `cwd` (the project), `permission_mode: "default"`, `hook_event_name` and per-event defaults (`source: "startup"` for `SessionStart`, `stop_hook_active: false` for `Stop`, ...); `payload` overrides them.

## In CI

```yaml
- run: pipx install git+https://github.com/jatingargiitk/hookverdict
- run: hookverdict lint --no-user
- run: hookverdict test hookverdict-cases.json --no-user --timeout 30
```

`--no-user` skips `~/.claude/settings.json` so the run only depends on the repo.

## Limits

- Only `command` handlers are executed. `http`, `mcp_tool`, `prompt` and `agent` handlers are listed as skipped, and `async` hooks are skipped because they can't affect the decision.
- Hooks really run, with your environment. Don't point it at hooks that deploy or delete things unless that's what you want to test.
- Plugin, skill, subagent-frontmatter and managed-policy hooks aren't loaded. Neither are permission rules, so `allow` means "the hook approved it", not "the call will run".
- The `if` matcher follows the documented rules, not Claude Code's own parser; exotic shell (heredocs, `case` bodies, nested quoting tricks) can differ. Matcher regexes run on Python's `re`, which agrees with JavaScript for the usual patterns.
- Monitor-tool, Windows/PowerShell-without-Git-Bash and Code Mode behaviour isn't modelled.

## License

MIT
