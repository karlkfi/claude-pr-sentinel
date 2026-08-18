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
import tempfile
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


def bash_payload(command, background=False, transcript=None):
    tool_input = {"command": command}
    if background:
        tool_input["run_in_background"] = True
    payload = {"tool_name": "Bash", "tool_input": tool_input}
    if transcript:
        payload["transcript_path"] = transcript
    return payload


def transcript_with_live_watcher(pr, tmpdir, task_id="bk1", completed=False):
    """A synthetic transcript in which this session launched a watcher on `pr`
    as a background task — still running unless `completed`."""
    entries = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_w", "name": "Bash",
             "input": {"command": f'bash "{WATCHER}" {pr}',
                       "run_in_background": True}}]}},
        {"type": "user", "toolUseResult": {"backgroundTaskId": task_id},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_w",
              "content": f"Command running in background with ID: {task_id}."}]}},
    ]
    if completed:
        entries.append({"type": "queue-operation", "content": (
            "<task-notification>\n"
            f"<tool-use-id>toolu_w</tool-use-id>\n"
            f"<output-file>/tmp/s/tasks/{task_id}.output</output-file>\n"
            "<status>completed</status>\n</task-notification>")})
    path = os.path.join(tmpdir, "transcript.jsonl")
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


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
        # `gh` inside a command substitution — the tokenizer hands that back as
        # one opaque token, so the subject check reads the raw string (#36)
        self.assertEqual(
            guard.classify_poll(
                'until [ "$(gh run view 7 --json status --jq .status)" '
                '= "completed" ]; do sleep 30; done'),
            "sleep_loop")
        self.assertEqual(
            guard.classify_poll(
                "until /usr/local/bin/gh pr checks; do sleep 10; done"),
            "sleep_loop")

    def test_sleep_loops_on_non_gh_subjects_pass(self):
        """A poll loop this plugin has no claim on — nothing about it is CI
        (#36)."""
        for cmd in (
            "until curl -sS https://example.test/versions.json "
            "| grep -q '1.3.0'; do sleep 20; done",
            "until [ -f build/done ]; do sleep 5; done",
            "while ! nc -z localhost 8080; do sleep 1; done",
            # 'gh' as a filename fragment is not a `gh` invocation
            "until [ -f out.gh ]; do sleep 5; done",
        ):
            self.assertIsNone(guard.classify_poll(cmd), cmd)

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


class InlineOverrideUnit(unittest.TestCase):
    """The inline `PR_SENTINEL_OVERRIDE=<reason>` prefix — the only form of the
    escape hatch a session can reach from inside a Bash call."""

    def test_prefix_on_the_poll_itself(self):
        self.assertEqual(
            guard.inline_override('PR_SENTINEL_OVERRIDE=one-off gh run watch 5'),
            "one-off")
        # quoted multi-word reason
        self.assertEqual(
            guard.inline_override(
                'PR_SENTINEL_OVERRIDE="format probe on a finished run" '
                'gh run watch 5'),
            "format probe on a finished run")

    def test_prefix_survives_chains_redirects_and_keywords(self):
        for cmd in (
            'mkdir -p out && PR_SENTINEL_OVERRIDE=why gh run watch 5 > out/x',
            'PR_SENTINEL_OVERRIDE=why gh run watch 5 2>&1 | head -5',
            'GH_TOKEN=x PR_SENTINEL_OVERRIDE=why gh pr checks --watch',
            'until gh pr checks; do PR_SENTINEL_OVERRIDE=why sleep 20; done',
        ):
            self.assertEqual(guard.inline_override(cmd), "why", cmd)

    def test_non_prefix_occurrences_do_not_count(self):
        for cmd in (
            'echo PR_SENTINEL_OVERRIDE=x && gh run watch 5',
            'gh run watch 5 --repo o/PR_SENTINEL_OVERRIDE=x',
            'PR_SENTINEL_OVERRIDE= gh run watch 5',      # empty value
            'PR_SENTINEL_OVERRIDE="  " gh run watch 5',  # whitespace-only
            'gh run watch 5',
        ):
            self.assertEqual(guard.inline_override(cmd), "", cmd)


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

    def test_inline_override_downgrades_to_allow(self):
        # The form the deny message names, with nothing in the environment.
        for cmd in (
            'PR_SENTINEL_OVERRIDE="one-off format probe" gh run watch 5',
            'mkdir -p out && PR_SENTINEL_OVERRIDE=why gh run watch 5 > out/x',
        ):
            out, _, rc = run_guard(bash_payload(cmd))
            self.assertEqual(out.strip(), "", cmd)
            self.assertEqual(rc, 0)

    def test_deny_names_the_backgrounded_fallback(self):
        # The remedy for a run with no PR to watch (#36).
        out, _, _ = run_guard(bash_payload("gh run watch 5"))
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("no PR to watch", reason)
        self.assertIn("run_in_background", reason)

    def test_deny_names_the_inline_prefix_form(self):
        out, _, _ = run_guard(bash_payload("gh run watch 5"))
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("inline", reason)
        self.assertIn("PR_SENTINEL_OVERRIDE=<reason>", reason)

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


class BackgroundedCallsEndToEnd(unittest.TestCase):
    """`run_in_background` is the harness's own signal that the call can't
    block the session — the harm the deny names. So it defers (#36)."""

    def test_backgrounded_polls_are_not_denied(self):
        for cmd in (
            "gh run watch 5 --exit-status",
            "gh pr checks --watch",
            'until [ "$(gh run view 7 --json status --jq .status)" '
            '= "completed" ]; do sleep 30; done',
        ):
            out, _, rc = run_guard(bash_payload(cmd, background=True))
            self.assertEqual(out.strip(), "", cmd)
            self.assertEqual(rc, 0)

    def test_same_command_in_the_foreground_still_denies(self):
        out, _, _ = run_guard(bash_payload("gh run watch 5 --exit-status"))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]
                         ["permissionDecision"], "deny")

    def test_falsey_background_flag_still_denies(self):
        payload = {"tool_name": "Bash",
                   "tool_input": {"command": "gh run watch 5",
                                  "run_in_background": False}}
        out, _, _ = run_guard(payload)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]
                         ["permissionDecision"], "deny")


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

    def test_backgrounded_launch_is_still_allowed(self):
        # The launch the nudge names IS backgrounded — the auto-allow runs
        # before the backgrounded-call defer, so the prompt is still skipped.
        out, _, _ = run_guard(bash_payload(f'bash "{WATCHER}" 42', background=True),
                              env={"CLAUDE_PLUGIN_ROOT": str(REPO)})
        self._assert_allow(out)

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


class DuplicateWatcherDeny(unittest.TestCase):
    """A launch for a PR this session is already watching is denied, because a
    second watcher wakes the session twice for every event. The deny names the
    background task id so stopping the incumbent is one tool call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _launch(self, pr=7):
        return f'bash "{WATCHER}" {pr}'

    def _run(self, command, transcript=None, env=None):
        # CLAUDE_PLUGIN_ROOT must name THIS checkout, or the launch does not
        # resolve to the plugin's own watcher and nothing recognises it.
        run_env = {"CLAUDE_PLUGIN_ROOT": str(REPO)}
        run_env.update(env or {})
        return run_guard(bash_payload(command, transcript=transcript),
                         env=run_env)

    def _decision(self, out):
        self.assertTrue(out.strip(), "expected a decision, got silence")
        return json.loads(out)["hookSpecificOutput"]

    def test_second_launch_is_denied(self):
        t = transcript_with_live_watcher("7", self.tmp.name)
        out, _, rc = self._run(self._launch(), transcript=t)
        self.assertEqual(rc, 0)
        d = self._decision(out)
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIn("#7", d["permissionDecisionReason"])
        self.assertIn('TaskStop(task_id="bk1")', d["permissionDecisionReason"])

    def test_first_launch_is_still_auto_allowed(self):
        t = transcript_with_live_watcher("7", self.tmp.name, completed=True)
        out, _, _ = self._run(self._launch(), transcript=t)
        self.assertEqual(self._decision(out)["permissionDecision"], "allow")

    def test_a_different_pr_is_not_a_duplicate(self):
        t = transcript_with_live_watcher("7", self.tmp.name)
        out, _, _ = self._run(self._launch(8), transcript=t)
        self.assertEqual(self._decision(out)["permissionDecision"], "allow")

    def test_unreadable_transcript_never_denies(self):
        out, _, _ = self._run(
            self._launch(), transcript="/nonexistent/transcript.jsonl")
        self.assertEqual(self._decision(out)["permissionDecision"], "allow")

    def test_missing_transcript_path_never_denies(self):
        out, _, _ = self._run(self._launch())
        self.assertEqual(self._decision(out)["permissionDecision"], "allow")

    def test_override_downgrades_the_duplicate_deny(self):
        t = transcript_with_live_watcher("7", self.tmp.name)
        out, _, _ = self._run(
            f'PR_SENTINEL_OVERRIDE=why bash "{WATCHER}" 7', transcript=t)
        self.assertEqual(out.strip(), "")   # not a recognised launch shape
        out, _, _ = self._run(self._launch(), transcript=t,
                              env={"PR_SENTINEL_OVERRIDE": "why"})
        self.assertEqual(self._decision(out)["permissionDecision"], "allow")

    def test_disabled_plugin_never_denies(self):
        t = transcript_with_live_watcher("7", self.tmp.name)
        out, _, _ = self._run(self._launch(), transcript=t,
                              env={"PR_SENTINEL_DISABLE": "1"})
        self.assertEqual(out.strip(), "")

    def test_duplicate_denied_even_with_autoallow_off(self):
        # The deny is about duplication, not about the permission prompt.
        t = transcript_with_live_watcher("7", self.tmp.name)
        out, _, _ = self._run(self._launch(), transcript=t,
                              env={"PR_SENTINEL_AUTOALLOW": "0"})
        self.assertEqual(self._decision(out)["permissionDecision"], "deny")
