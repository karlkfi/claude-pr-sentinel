#!/usr/bin/env python3
"""Tests for scripts/pr-sentinel-guard.py (the PreToolUse foreground-poll deny).

Run with: python3 -m unittest discover tests

Two layers:
  * Unit tests import the module and exercise the poll-shape classifier.
  * End-to-end tests invoke the script as a subprocess, feed it the hook stdin
    JSON, and assert the emitted deny decision (or silence / override).
"""
import json
import os
import subprocess
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pr-sentinel-guard.py"

_spec = util.spec_from_file_location("pr_sentinel_guard", SCRIPT)
guard = util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def run_guard(payload, env=None):
    """Invoke the guard as a subprocess; return (stdout, stderr, returncode)."""
    run_env = dict(os.environ)
    run_env.setdefault("CLAUDE_PLUGIN_ROOT", "/opt/plugins/pr-sentinel")
    # Never leak a real override from the ambient environment into a test.
    run_env.pop("PR_SENTINEL_OVERRIDE", None)
    if env:
        run_env.update(env)
    proc = subprocess.run(
        ["python3", str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=run_env, timeout=15, check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class ClassifyPollUnit(unittest.TestCase):
    def test_gh_pr_checks_watch(self):
        self.assertEqual(guard.classify_poll("gh pr checks --watch"),
                         "gh_pr_checks_watch")
        self.assertEqual(guard.classify_poll("gh pr checks 12 --watch"),
                         "gh_pr_checks_watch")
        self.assertEqual(guard.classify_poll("gh pr checks -w"),
                         "gh_pr_checks_watch")
        # env prefix and trailing chain don't hide it
        self.assertEqual(
            guard.classify_poll("GH_TOKEN=x gh pr checks --watch && echo ok"),
            "gh_pr_checks_watch")

    def test_gh_run_watch(self):
        self.assertEqual(guard.classify_poll("gh run watch"), "gh_run_watch")
        self.assertEqual(guard.classify_poll("gh run watch 999"), "gh_run_watch")
        self.assertEqual(guard.classify_poll("GH_TOKEN=x gh run watch 3"),
                         "gh_run_watch")

    def test_sleep_loops(self):
        self.assertEqual(
            guard.classify_poll("while true; do sleep 5; gh pr checks; done"),
            "sleep_loop")
        self.assertEqual(
            guard.classify_poll("until gh pr checks; do sleep 10; done"),
            "sleep_loop")
        self.assertEqual(
            guard.classify_poll(
                "until ! gh run view -q .status | grep -q completed; "
                "do sleep 15; done"),
            "sleep_loop")

    def test_non_poll_commands_pass(self):
        # gh status reads that DON'T block
        self.assertIsNone(guard.classify_poll("gh pr checks"))
        self.assertIsNone(guard.classify_poll("gh pr checks 12"))
        self.assertIsNone(guard.classify_poll("gh run list"))
        self.assertIsNone(guard.classify_poll("gh run view 12"))
        self.assertIsNone(guard.classify_poll("gh pr view 12"))
        # the watcher launch itself must never be denied
        self.assertIsNone(
            guard.classify_poll('bash scripts/pr-sentinel-watch.sh 42'))
        # a bare sleep is not a poll loop (too fuzzy to deny)
        self.assertIsNone(guard.classify_poll("sleep 5"))
        # loop keyword without sleep, or sleep-word only in a string
        self.assertIsNone(
            guard.classify_poll("while read line; do echo $line; done < file"))
        self.assertIsNone(guard.classify_poll("echo 'while you sleep'"))
        self.assertIsNone(guard.classify_poll("git status"))
        self.assertIsNone(guard.classify_poll(""))


class GuardEndToEnd(unittest.TestCase):
    def _assert_deny(self, out, shape_hint):
        obj = json.loads(out)
        hso = obj["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        reason = hso["permissionDecisionReason"]
        self.assertIn("pr-sentinel-watch.sh", reason)
        self.assertIn("background", reason.lower())
        self.assertIn("PR_SENTINEL_OVERRIDE", reason)
        self.assertIn(shape_hint, reason)
        return reason

    def test_deny_gh_pr_checks_watch(self):
        out, _, _ = run_guard(bash_payload("gh pr checks --watch"))
        reason = self._assert_deny(out, "gh pr checks --watch")
        self.assertIn("/opt/plugins/pr-sentinel", reason)  # CLAUDE_PLUGIN_ROOT

    def test_deny_gh_run_watch(self):
        out, _, _ = run_guard(bash_payload("gh run watch 5"))
        self._assert_deny(out, "gh run watch")

    def test_deny_sleep_loop(self):
        out, _, _ = run_guard(
            bash_payload("until gh pr checks; do sleep 20; done"))
        self._assert_deny(out, "poll loop")

    def test_override_downgrades_to_allow(self):
        # A non-empty override defers (emits nothing) so the command proceeds
        # under the normal permission system.
        out, _, rc = run_guard(bash_payload("gh pr checks --watch"),
                               env={"PR_SENTINEL_OVERRIDE": "flaky infra once"})
        self.assertEqual(out.strip(), "")
        self.assertEqual(rc, 0)

    def test_override_empty_still_denies(self):
        out, _, _ = run_guard(bash_payload("gh run watch"),
                              env={"PR_SENTINEL_OVERRIDE": ""})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]
                         ["permissionDecision"], "deny")

    def test_silent_on_non_poll_command(self):
        out, _, rc = run_guard(bash_payload("gh pr checks"))
        self.assertEqual(out.strip(), "")
        self.assertEqual(rc, 0)

    def test_silent_on_watcher_launch(self):
        out, _, _ = run_guard(
            bash_payload('bash "$CLAUDE_PLUGIN_ROOT/scripts/pr-sentinel-watch.sh" 42'))
        self.assertEqual(out.strip(), "")

    def test_silent_on_non_bash_tool(self):
        out, _, _ = run_guard({"tool_name": "Read",
                               "tool_input": {"file_path": "/x"}})
        self.assertEqual(out.strip(), "")

    def test_unparseable_input_defers(self):
        run_env = dict(os.environ)
        run_env.pop("PR_SENTINEL_OVERRIDE", None)
        proc = subprocess.run(
            ["python3", str(SCRIPT)],
            input="not json", capture_output=True, text=True,
            env=run_env, timeout=15, check=False,
        )
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.returncode, 0)

    def test_debug_reraises_on_bad_input_shape(self):
        # tool_input is a string, not a dict -> .get would raise; DEBUG=1
        # surfaces it instead of failing open.
        run_env = dict(os.environ)
        run_env.pop("PR_SENTINEL_OVERRIDE", None)
        run_env["PR_SENTINEL_DEBUG"] = "1"
        proc = subprocess.run(
            ["python3", str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": "oops"}),
            capture_output=True, text=True, env=run_env, timeout=15, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
