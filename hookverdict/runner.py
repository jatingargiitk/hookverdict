"""Run one command hook handler with a JSON payload on stdin."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .config import Handler

DEFAULT_TIMEOUT = 600
SHORT_TIMEOUT = {"UserPromptSubmit": 30, "PreModelSwitch": 30, "PostModelSwitch": 30, "MessageDisplay": 10}


@dataclass
class Run:
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    start_error: Optional[str] = None
    seconds: float = 0.0
    timeout: float = 0.0


def configured_timeout(h: Handler) -> float:
    t = h.raw.get("timeout")
    if isinstance(t, (int, float)) and t > 0:
        return float(t)
    return float(SHORT_TIMEOUT.get(h.event, DEFAULT_TIMEOUT))


def _substitute(s: str, env: Dict[str, str]) -> str:
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"):
        if key in env:
            s = s.replace("${" + key + "}", env[key])
    return s


def run_handler(h: Handler, payload: dict, project_dir: str, max_timeout: Optional[float] = None,
                extra_env: Optional[Dict[str, str]] = None) -> Run:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    if extra_env:
        env.update(extra_env)
    timeout = configured_timeout(h)
    if max_timeout is not None:
        timeout = min(timeout, max_timeout)
    cwd = payload.get("cwd") or project_dir
    if not os.path.isdir(cwd):
        cwd = project_dir

    if h.args is not None:
        exe = _substitute(h.command, env)
        resolved = exe if os.sep in exe else shutil.which(exe, path=env.get("PATH"))
        if not resolved or not os.path.exists(resolved):
            return Run(start_error=f"executable not found: {exe}", timeout=timeout)
        argv = [resolved] + [_substitute(a, env) for a in h.args]
    else:
        shell = h.raw.get("shell")
        if shell == "powershell":
            exe = shutil.which("pwsh") or shutil.which("powershell")
            if not exe:
                return Run(start_error="shell 'powershell' requested but not installed", timeout=timeout)
            argv = [exe, "-NoProfile", "-Command", h.command]
        else:
            argv = [shutil.which("bash") or "/bin/sh", "-c", h.command]

    data = json.dumps(payload).encode()
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd, env=env, start_new_session=True)
    except OSError as exc:
        return Run(start_error=str(exc), timeout=timeout)
    try:
        out, err = proc.communicate(data, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        timed_out = True
    return Run(
        exit_code=None if timed_out else proc.returncode,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        timed_out=timed_out,
        seconds=time.monotonic() - start,
        timeout=timeout,
    )
