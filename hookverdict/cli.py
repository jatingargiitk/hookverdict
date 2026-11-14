"""hookverdict command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .config import load
from .engine import Result, build_payload, evaluate
from .lint import lint
from .matching import TOOL_EVENTS


def _json_arg(value: str):
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-C", "--project", default=".", help="project directory (default: .)")
    p.add_argument("--settings", action="append", metavar="FILE",
                   help="read hooks only from this settings file (repeatable); default: user, project and local settings")
    p.add_argument("--no-user", action="store_true", help="skip ~/.claude/settings.json")


def _load(args):
    project = os.path.abspath(args.project)
    return project, load(project, args.settings, include_user=not args.no_user)


def _rel(path: str, project: str) -> str:
    return os.path.relpath(path, project) if path.startswith(project + os.sep) else path


# ------------------------------------------------------------------ list

def cmd_list(args) -> int:
    project, cfg = _load(args)
    if args.json:
        print(json.dumps([{"event": h.event, "source": h.source, "file": h.path, "matcher": h.matcher,
                           "handler": h.raw} for h in cfg.handlers], indent=2))
        return 0
    if not cfg.files:
        print("no settings files found")
        return 0
    for f in cfg.files:
        print(f"# {_rel(f, project)}")
    last = None
    for h in cfg.handlers:
        if h.event != last:
            print(f"\n{h.event}")
            last = h.event
        m = "*" if h.matcher in (None, "", "*") else h.matcher
        extra = f"  if {h.if_rule}" if h.if_rule else ""
        flags = [k for k in ("async", "asyncRewake") if h.raw.get(k)]
        if h.on_failure:
            flags.append(f"onFailure={h.on_failure}")
        fl = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  [{h.source}] matcher {m}{extra}  {h.type}: {h.label}{fl}")
    for p in cfg.problems:
        print(f"problem: {p}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ run

def _payload_from_args(args, project: str) -> dict:
    extra = {}
    tool_input = _json_arg(args.input) if args.input else {}
    if args.command is not None:
        tool_input["command"] = args.command
    if args.file is not None:
        tool_input["file_path"] = args.file
    tool = args.tool
    if tool is None and args.event in TOOL_EVENTS:
        tool = "Bash" if "command" in tool_input else ("Edit" if "file_path" in tool_input else None)
    if args.event in TOOL_EVENTS:
        if not tool:
            raise SystemExit("hookverdict: tool events need --tool (or --command / --file)")
        extra["tool_name"] = tool
        extra["tool_input"] = tool_input
    if args.prompt is not None:
        extra["prompt"] = args.prompt
    if args.payload:
        extra.update(_json_arg(args.payload))
    return build_payload(args.event, project, extra)


def _describe(res: Result, project: str, out) -> None:
    p = res.payload
    head = res.event
    if "tool_name" in p:
        head += f"  tool={p['tool_name']}  input={json.dumps(p.get('tool_input', {}))}"
    elif res.event == "UserPromptSubmit":
        head += f"  prompt={json.dumps(p.get('prompt', ''))}"
    print(head, file=out)
    if not res.steps:
        print("  (no hooks configured for this event)", file=out)
    for s in res.steps:
        h = s.handler
        m = "*" if h.matcher in (None, "", "*") else h.matcher
        cond = f"  if {h.if_rule}" if h.if_rule else ""
        print(f"  [{h.source}] {_rel(h.path, project)}  matcher {m}{cond}", file=out)
        print(f"    {h.type}: {h.label}", file=out)
        if s.run is None:
            print(f"    skipped: {s.why}", file=out)
            continue
        r = s.run
        if r.start_error:
            print(f"    could not start: {r.start_error}", file=out)
        elif r.timed_out:
            print(f"    timed out after {r.timeout:g}s", file=out)
        else:
            print(f"    exit {r.exit_code} ({r.seconds * 1000:.0f} ms)", file=out)
            if r.stdout.strip():
                print("    stdout: " + r.stdout.strip().replace("\n", "\n            "), file=out)
            if r.stderr.strip():
                print("    stderr: " + r.stderr.strip().replace("\n", "\n            "), file=out)
        if s.why:
            print(f"    note: {s.why}", file=out)
        o = s.outcome
        print(f"    => {o.status}", file=out)
        for n in o.notes:
            print(f"       - {n}", file=out)
    v = res.verdict
    print(f"\nVERDICT: {v.decision.upper()} — {v.meaning}", file=out)
    if v.reason:
        print(f"  reason: {v.reason}", file=out)
    if v.stop_reason:
        print(f"  stopReason: {v.stop_reason}", file=out)
    if v.updated_input is not None:
        print(f"  updatedInput: {json.dumps(v.updated_input)}", file=out)
    for c in v.additional_context:
        print(f"  context for Claude: {c}", file=out)
    for m in v.system_messages:
        print(f"  systemMessage: {m}", file=out)


def _result_json(res: Result) -> dict:
    v = res.verdict
    return {
        "event": res.event,
        "verdict": v.decision,
        "meaning": v.meaning,
        "reason": v.reason,
        "stop_reason": v.stop_reason,
        "updated_input": v.updated_input,
        "additional_context": v.additional_context,
        "system_messages": v.system_messages,
        "decided_by": v.decided_by,
        "handlers": [
            {
                "source": s.handler.source, "file": s.handler.path, "matcher": s.handler.matcher,
                "if": s.handler.if_rule, "handler": s.handler.label, "ran": s.run is not None,
                "skipped": s.why if s.run is None else "",
                "exit_code": s.run.exit_code if s.run else None,
                "timed_out": s.run.timed_out if s.run else False,
                "start_error": s.run.start_error if s.run else None,
                "stdout": s.run.stdout if s.run else "", "stderr": s.run.stderr if s.run else "",
                "status": s.outcome.status if s.outcome else None,
                "notes": s.outcome.notes if s.outcome else [],
            } for s in res.steps
        ],
    }


def cmd_run(args) -> int:
    project, cfg = _load(args)
    for p in cfg.problems:
        print(f"problem: {p}", file=sys.stderr)
    payload = _payload_from_args(args, project)
    res = evaluate(cfg, args.event, payload, project, max_timeout=args.timeout)
    if args.json:
        print(json.dumps(_result_json(res), indent=2))
    else:
        _describe(res, project, sys.stdout)
    if args.expect and res.verdict.decision != args.expect:
        print(f"\nexpected {args.expect}, got {res.verdict.decision}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------ test

def _check_case(case: dict, res: Result) -> List[str]:
    v = res.verdict
    exp = case.get("expect")
    if isinstance(exp, str):
        exp = {"verdict": exp}
    exp = exp or {}
    fails = []
    if "verdict" in exp and v.decision != exp["verdict"]:
        fails.append(f"verdict: expected {exp['verdict']}, got {v.decision}")
    if "reason_contains" in exp and exp["reason_contains"] not in (v.reason or v.stop_reason):
        fails.append(f"reason: expected to contain {exp['reason_contains']!r}, got {v.reason!r}")
    if "context_contains" in exp and not any(exp["context_contains"] in c for c in v.additional_context):
        fails.append(f"additionalContext: expected to contain {exp['context_contains']!r}")
    if "updated_input" in exp and v.updated_input != exp["updated_input"]:
        fails.append(f"updatedInput: expected {exp['updated_input']}, got {v.updated_input}")
    if "ran" in exp:
        ran = sum(1 for s in res.steps if s.run is not None)
        if ran != exp["ran"]:
            fails.append(f"handlers run: expected {exp['ran']}, got {ran}")
    if exp.get("no_errors"):
        errs = [s.handler.label for s in res.steps if s.outcome and s.outcome.status == "error"]
        if errs:
            fails.append(f"hook errors: {', '.join(errs)}")
    return fails


def cmd_test(args) -> int:
    project, cfg = _load(args)
    for p in cfg.problems:
        print(f"problem: {p}", file=sys.stderr)
    with open(args.cases, encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data["cases"] if isinstance(data, dict) else data
    failed = 0
    for i, case in enumerate(cases):
        name = case.get("name", f"case {i + 1}")
        event = case["event"]
        extra = dict(case.get("payload", {}))
        if event in TOOL_EVENTS:
            extra.setdefault("tool_name", case.get("tool", "Bash"))
            extra.setdefault("tool_input", case.get("input", {}))
        if "prompt" in case:
            extra["prompt"] = case["prompt"]
        payload = build_payload(event, project, extra)
        res = evaluate(cfg, event, payload, project, max_timeout=args.timeout)
        fails = _check_case(case, res)
        if fails:
            failed += 1
            print(f"FAIL  {name}")
            for f in fails:
                print(f"      {f}")
            if args.verbose:
                _describe(res, project, sys.stdout)
        else:
            print(f"ok    {name}  ({res.verdict.decision})")
    print(f"\n{len(cases) - failed} passed, {failed} failed")
    return 1 if failed else 0


# ------------------------------------------------------------------ lint

def cmd_lint(args) -> int:
    project, cfg = _load(args)
    findings = lint(cfg, project)
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.level:5}  {f.where}: {f.message}")
        n_err = sum(f.level == "error" for f in findings)
        print(f"{len(cfg.handlers)} handlers in {len(cfg.files)} files: "
              f"{n_err} errors, {len(findings) - n_err} warnings")
    errors = any(f.level == "error" for f in findings)
    warns = any(f.level == "warn" for f in findings)
    return 1 if errors or (args.strict and warns) else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hookverdict",
                                 description="Run Claude Code hooks against a fake event and see the verdict Claude Code would reach.")
    ap.add_argument("--version", action="version", version=f"hookverdict {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="fire one event at your hooks and print the verdict")
    p.add_argument("event", help="hook event, e.g. PreToolUse, PostToolUse, UserPromptSubmit, Stop")
    p.add_argument("--tool", help="tool_name for tool events (default Bash with --command, Edit with --file)")
    p.add_argument("--command", help="shorthand for tool_input.command")
    p.add_argument("--file", help="shorthand for tool_input.file_path")
    p.add_argument("--input", help="tool_input as JSON (or @file.json)")
    p.add_argument("--prompt", help="prompt text for UserPromptSubmit")
    p.add_argument("--payload", help="extra top-level payload fields as JSON (or @file.json)")
    p.add_argument("--expect", help="exit 1 unless the verdict is this (deny/ask/allow/defer/block/feedback/stop/none)")
    p.add_argument("--timeout", type=float, help="cap every hook's timeout at this many seconds")
    p.add_argument("--json", action="store_true")
    _common(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("test", help="run a JSON file of cases and check each verdict")
    p.add_argument("cases", help="cases file")
    p.add_argument("--timeout", type=float, help="cap every hook's timeout at this many seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="show the full trace for failing cases")
    _common(p)
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("lint", help="static checks for hooks that silently never run or never block")
    p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    p.add_argument("--json", action="store_true")
    _common(p)
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("list", help="list configured hooks by event")
    p.add_argument("--json", action="store_true")
    _common(p)
    p.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
