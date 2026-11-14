import pytest

from hookverdict.matching import (Unparseable, bash_rule_matches, if_matches, matcher_matches,
                                  normalize_subcommand, path_rule_matches, split_bash)


@pytest.mark.parametrize("matcher,value,want", [
    (None, "Bash", True), ("", "Bash", True), ("*", "Write", True),
    ("Bash", "Bash", True), ("Bash", "BashOutput", False),
    ("Edit|Write", "Write", True), ("Edit, Write", "Edit", True), ("Edit|Write", "NotebookEdit", False),
    ("Edit.*", "NotebookEdit", True), ("^Edit$", "NotebookEdit", False),
    ("mcp__memory__.*", "mcp__memory__create_entities", True),
    ("mcp__memory", "mcp__memory__create_entities", False),
    ("mcp__brave-search__.*", "mcp__brave-search__web", True),
    ("bash", "Bash", False),
])
def test_matcher(matcher, value, want):
    assert matcher_matches(matcher, value, "PreToolUse")[0] is want


def test_matcher_ignored_on_events_without_support():
    ok, note = matcher_matches("deploy", None, "UserPromptSubmit")
    assert ok and "ignored" in note


def test_invalid_regex_matches_nothing():
    ok, note = matcher_matches("Bash(", "Bash", "PreToolUse")
    assert not ok and "not a valid regex" in note


def test_split_bash():
    assert split_bash("npm test && git push; ls | wc -l") == ["npm test", "git push", "ls", "wc -l"]
    assert split_bash("echo 'a && b'") == ["echo 'a && b'"]
    assert split_bash("echo $(rm -rf /)") == ["rm -rf /", "echo $(rm -rf /)"]
    assert split_bash("ls 2>&1 | grep x") == ["ls 2>&1", "grep x"]
    with pytest.raises(Unparseable):
        split_bash("npm test &&")
    with pytest.raises(Unparseable):
        split_bash("echo 'oops")


def test_normalize_strips_assignments_and_wrappers():
    assert normalize_subcommand("FOO=bar git push") == "git push"
    assert normalize_subcommand("timeout 30 npm test") == "npm test"
    assert normalize_subcommand("nice -n 5 nohup make") == "make"
    assert normalize_subcommand("xargs grep x") == "grep x"
    assert normalize_subcommand("xargs -n1 grep x") == "xargs -n1 grep x"


# Rows from the hooks reference table "How if patterns match Bash commands".
@pytest.mark.parametrize("pattern,command,runs", [
    ("git *", "FOO=bar git push", True),
    ("git *", "npm test && git push", True),
    ("rm *", "echo $(rm -rf /)", True),
    ("rm *", "echo $(date)", False),
    ("git push *", "echo $(date)", True),
    # From the permissions wildcard table.
    ("ls *", "ls", True), ("ls *", "lsof", False), ("ls*", "lsof", True),
    ("npm run build", "npm run build --watch", False),
    ("git log * main", "git log main", False),
    ("* --version", "node --version", True),
    ("ls:*", "ls -la", True),
])
def test_bash_if(pattern, command, runs):
    assert bash_rule_matches(pattern, command)[0] is runs


def test_unparseable_command_runs_hook():
    runs, note = bash_rule_matches("rm *", "npm test &&")
    assert runs and "unparseable" in note


@pytest.mark.parametrize("spec,path,want", [
    ("*.ts", "/p/src/app.ts", True),
    ("*.ts", "/p/src/app.js", False),
    ("src/**", "/p/src/app.ts", True),
    ("src/**", "/p/vendor/pkg/src/lib.js", False),
    ("**/src/**", "/p/vendor/pkg/src/lib.js", True),
    ("/src/**", "/p/src/a/b.ts", True),
    (".env", "/p/config/.env", True),
    ("//tmp/scratch.txt", "/tmp/scratch.txt", True),
    ("*.ts", "/elsewhere/a.ts", False),
])
def test_path_if(spec, path, want):
    assert path_rule_matches(spec, path, cwd="/p", anchor="/p") is want


def test_if_tool_family_and_events():
    assert if_matches("Edit(*.ts)", "PreToolUse", "Write", {"file_path": "/p/a.ts"}, "/p", "/p")[0]
    assert not if_matches("Bash(git *)", "PreToolUse", "Write", {"file_path": "/p/a.ts"}, "/p", "/p")[0]
    runs, note = if_matches("Bash(git *)", "Stop", "", {}, "/p", "/p")
    assert not runs and "never runs" in note
    assert if_matches("Bash(run_in_background:true)", "PreToolUse", "Bash",
                      {"command": "sleep 9", "run_in_background": True}, "/p", "/p")[0]
    assert if_matches("WebFetch(domain:*.example.com)", "PreToolUse", "WebFetch",
                      {"url": "https://docs.example.com/x"}, "/p", "/p")[0]
