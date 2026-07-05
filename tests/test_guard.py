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
WATCHER = str((REPO / "scripts" / "pr-sentinel-watch.sh").resolve())

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


class WatcherLaunchUnit(unittest.TestCase):
    """The airtight, fail-safe watcher-launch matcher behind the auto-allow."""

    def setUp(self):
        self._saved = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = self._saved

    def test_exact_launch_matches(self):
        self.assertTrue(guard.is_watcher_launch(f"bash {WATCHER} 42"))
        # quoted path (the form the PostToolUse nudge emits) still matches
        self.assertTrue(guard.is_watcher_launch(f'bash "{WATCHER}" 6'))
        # a relative path that resolves to the same file matches (realpath, not
        # string, comparison)
        self.assertTrue(guard.is_watcher_launch(
            "bash scripts/../scripts/pr-sentinel-watch.sh 6"))

    def test_near_misses_never_match(self):
        cases = [
            f"bash {WATCHER} 6 --force",          # extra trailing arg
            f"bash {WATCHER}",                     # missing PR number
            f"bash {WATCHER} abc",                 # non-digit PR
            f"bash {WATCHER} 0",                   # zero is not a valid PR
            f"bash {WATCHER} -6",                  # negative / flag-shaped
            f"bash {WATCHER} 6; rm -rf /",         # chained command
            f"bash {WATCHER} 6 && echo hi",        # chained command
            f"bash {WATCHER} 6 | tee log",         # pipe
            f"bash {WATCHER} 6 > /tmp/x",          # redirect
            f"bash {WATCHER} $(echo 6)",           # command substitution
            f"bash {WATCHER} `echo 6`",            # backtick substitution
            f"bash {WATCHER} 6 &",                 # background operator
            f"sh {WATCHER} 6",                     # not bash
            f"bash {REPO}/scripts/pr-sentinel-hook.py 6",   # different script
            f"bash {WATCHER}-evil 6",              # look-alike path
            "bash /opt/other/pr-sentinel-watch.sh 6",       # unrelated path
            f"bash {WATCHER}* 6",                  # glob
        ]
        for cmd in cases:
            self.assertFalse(guard.is_watcher_launch(cmd), cmd)


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


class AutoAllowEndToEnd(unittest.TestCase):
    """The PreToolUse auto-allow for the plugin's own watcher launch."""

    def _run(self, command, env=None):
        run_env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
        if env:
            run_env.update(env)
        return run_guard(bash_payload(command), env=run_env)

    def _assert_allow(self, out):
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertIn("PR_SENTINEL_AUTOALLOW", hso["permissionDecisionReason"])

    def _assert_not_allow(self, out):
        """Either silence (defer) or a non-allow decision — never allow."""
        if out.strip():
            self.assertNotEqual(
                json.loads(out)["hookSpecificOutput"]["permissionDecision"],
                "allow")

    def test_exact_launch_is_allowed(self):
        out, _, rc = self._run(f'bash "{WATCHER}" 42')
        self._assert_allow(out)
        self.assertEqual(rc, 0)

    def test_autoallow_off_defers(self):
        for val in ("0", "false", "FALSE", ""):
            out, _, _ = self._run(f"bash {WATCHER} 42",
                                  env={"PR_SENTINEL_AUTOALLOW": val})
            self.assertEqual(out.strip(), "", val)

    def test_disable_suppresses_autoallow(self):
        out, _, _ = self._run(f"bash {WATCHER} 42",
                              env={"PR_SENTINEL_DISABLE": "1"})
        self.assertEqual(out.strip(), "")

    def test_near_misses_are_not_allowed(self):
        # extra arg, non-digit PR, chained rm, redirect, look-alike script,
        # sh not bash, command substitution — none may auto-allow.
        for cmd in (
            f"bash {WATCHER} 6 --force",
            f"bash {WATCHER} notanumber",
            f"bash {WATCHER} 6; rm -rf /",
            f"bash {WATCHER} 6 > /tmp/x",
            f"bash {REPO}/scripts/pr-sentinel-hook.py 6",
            f"sh {WATCHER} 6",
            f"bash {WATCHER} $(echo 6)",
        ):
            out, _, _ = self._run(cmd)
            self._assert_not_allow(out)

    def test_override_does_not_block_autoallow(self):
        # The override escape hatch targets the deny; the watcher launch is
        # still auto-allowed (checked before override defers).
        out, _, _ = self._run(f"bash {WATCHER} 42",
                              env={"PR_SENTINEL_OVERRIDE": "x"})
        self._assert_allow(out)


if __name__ == "__main__":
    unittest.main()
