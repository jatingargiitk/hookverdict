import json
import os
import stat

import pytest


@pytest.fixture
def project(tmp_path):
    """Make a project dir; write(settings, {name: script}) creates .claude/settings.json and hook scripts."""
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)

    def write(settings, scripts=None):
        for name, body in (scripts or {}).items():
            p = hooks / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings))
        return str(tmp_path)

    return write


def pre_bash(*handlers, matcher="Bash"):
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": list(handlers)}]}}


def cmd(script, **kw):
    return {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + script, **kw}
