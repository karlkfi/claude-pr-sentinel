#!/usr/bin/env python3
"""Tests for scripts/pr-sentinel-hook.py (the PostToolUse nudge).

Run with: python3 -m unittest discover tests

Two layers:
  * Unit tests import the module and exercise command classification and the
    failure heuristic.
  * End-to-end tests invoke the script as a subprocess, feed it the hook stdin
    JSON, and assert the emitted additionalContext (or silence).
"""
import json
import os
import subprocess
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pr-sentinel-hook.py"

_spec = util.spec_from_file_location("pr_sentinel_hook", SCRIPT)
hook = util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def run_hook(payload, env=None):
    """Invoke the hook as a subprocess; return (stdout, stderr)."""
    run_env = dict(os.environ)
    run_env.setdefault("CLAUDE_PLUGIN_ROOT", "/opt/plugins/pr-sentinel")
    if env:
        run_env.update(env)
    proc = subprocess.run(
        ["python3", str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=run_env, timeout=15, check=False,
    )
    return proc.stdout, proc.stderr


def bash_payload(command, response=""):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": response,
    }


class ClassificationUnit(unittest.TestCase):
    def test_detect_pr_create(self):
        self.assertEqual(hook.detect_action("gh pr create --fill"), "pr_create")

    def test_detect_pr_create_with_env_prefix(self):
        self.assertEqual(
            hook.detect_action("GH_TOKEN=x gh pr create -t hi -b there"),
            "pr_create")

    def test_detect_git_push(self):
        self.assertEqual(hook.detect_action("git push -u origin claude/foo"),
                         "git_push")

    def test_pr_create_wins_over_push(self):
        self.assertEqual(
            hook.detect_action("git push origin HEAD && gh pr create --fill"),
            "pr_create")

    def test_ignore_git_push_delete(self):
        self.assertIsNone(hook.detect_action("git push origin --delete claude/foo"))
        self.assertIsNone(hook.detect_action("git push --tags"))

    def test_ignore_unrelated(self):
        self.assertIsNone(hook.detect_action("gh pr view 12"))
        self.assertIsNone(hook.detect_action("gh pr list"))
        self.assertIsNone(hook.detect_action("git status"))
        self.assertIsNone(hook.detect_action("echo push"))

    def test_failure_heuristic(self):
        self.assertTrue(hook.looks_failed("fatal: not a git repository"))
        self.assertTrue(hook.looks_failed("! [rejected]  main -> main"))
        self.assertTrue(hook.looks_failed("Everything up-to-date"))
        self.assertFalse(hook.looks_failed(
            "https://github.com/o/r/pull/42\nbranch pushed"))


class HookEndToEnd(unittest.TestCase):
    def test_nudge_on_pr_create_with_url(self):
        out, _ = run_hook(bash_payload(
            "gh pr create --fill",
            "https://github.com/o/r/pull/42\n"))
        obj = json.loads(out)
        ctx = obj["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(obj["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("#42", ctx)  # prose may reference the PR as #42
        self.assertIn("pr-sentinel-watch.sh", ctx)
        # The Command line must interpolate the BARE number — the watcher
        # rejects `#N`, so a `#`-prefixed arg would make a verbatim copy fail.
        self.assertIn('pr-sentinel-watch.sh" 42', ctx)
        self.assertNotIn('pr-sentinel-watch.sh" #42', ctx)
        self.assertIn("/opt/plugins/pr-sentinel", ctx)  # CLAUDE_PLUGIN_ROOT
        self.assertIn("background", ctx.lower())
        self.assertIn("Never auto-merge", ctx)

    def test_nudge_on_git_push_without_url(self):
        out, _ = run_hook(bash_payload(
            "git push -u origin claude/foo",
            "Branch 'claude/foo' set up to track 'origin/claude/foo'.\n"))
        obj = json.loads(out)
        ctx = obj["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pr-sentinel-watch.sh", ctx)
        # No PR number known -> a placeholder pointing the session to resolve it.
        self.assertIn("PR number", ctx)

    def test_silent_on_failed_push(self):
        out, _ = run_hook(bash_payload(
            "git push origin claude/foo",
            "! [rejected] claude/foo -> claude/foo (fetch first)\nerror: failed to push"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_unrelated_command(self):
        out, _ = run_hook(bash_payload("git status", " M file"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_non_bash_tool(self):
        out, _ = run_hook({"tool_name": "Read",
                           "tool_input": {"file_path": "/x"},
                           "tool_response": "gh pr create"})
        self.assertEqual(out.strip(), "")

    def test_disabled_flag(self):
        out, _ = run_hook(bash_payload("gh pr create --fill",
                                       "https://github.com/o/r/pull/42"),
                          env={"PR_SENTINEL_DISABLE": "1"})
        self.assertEqual(out.strip(), "")

    def test_unparseable_input_defers(self):
        run_env = dict(os.environ)
        proc = subprocess.run(
            ["python3", str(SCRIPT)],
            input="not json", capture_output=True, text=True,
            env=run_env, timeout=15, check=False,
        )
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.returncode, 0)

    def test_response_as_dict(self):
        out, _ = run_hook({
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --fill"},
            "tool_response": {"stdout": "https://github.com/o/r/pull/7\n",
                              "stderr": ""},
        })
        obj = json.loads(out)
        self.assertIn("#7", obj["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
