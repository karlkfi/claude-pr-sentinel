#!/usr/bin/env python3
"""Tests for scripts/pr_sentinel_hook.py (the PostToolUse nudge).

Run with: python3 -m unittest discover tests

Two layers:
  * Unit tests import the module and exercise command classification and the
    failure heuristic.
  * End-to-end tests invoke the plugin's entry point as a subprocess, feed it
    the hook stdin JSON, and assert the emitted additionalContext (or silence).
    The entry point dispatches on `hook_event_name`, so every payload here
    carries the one a real PostToolUse call carries.
"""
import json
import os
import subprocess
import tempfile
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pr-sentinel.py"
MODULE = REPO / "scripts" / "pr_sentinel_hook.py"

_spec = util.spec_from_file_location("pr_sentinel_hook", MODULE)
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


def bash_payload(command, response="", cwd=None, transcript=None):
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": response,
    }
    if cwd:
        payload["cwd"] = cwd
    if transcript:
        payload["transcript_path"] = transcript
    return payload


def transcript_with_live_watcher(prs, tmpdir, completed=()):
    """A synthetic transcript in which this session launched a background
    watcher on each PR in `prs`; those also in `completed` have exited."""
    entries = []
    for pr in prs:
        tool_id = f"toolu_w{pr}"
        task_id = f"bk{pr}"
        entries.append({"type": "assistant", "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {
                    "command": f'bash "/opt/w/pr-sentinel-watch.sh" {pr}',
                    "run_in_background": True}}]}})
        entries.append({"type": "user",
                        "toolUseResult": {"backgroundTaskId": task_id},
                        "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": tool_id,
                             "content": "Command running in background with "
                                        f"ID: {task_id}."}]}})
        if pr in completed:
            entries.append({"type": "queue-operation", "content": (
                "<task-notification>\n"
                f"<tool-use-id>{tool_id}</tool-use-id>\n"
                f"<output-file>/tmp/s/tasks/{task_id}.output</output-file>\n"
                "<status>completed</status>\n</task-notification>")})
    path = os.path.join(tmpdir, "transcript.jsonl")
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def make_repo(path):
    """A repo holding one commit and the tag `v9.9.9`, for the bare-ref probe."""
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=path, check=True)
    subprocess.run(git + ["tag", "v9.9.9"], cwd=path, check=True)


def make_repo_with_default(path, default):
    """A repo checked out on `work`, holding the tag `v0.9.0` and a remote HEAD
    naming `default` — the shape the default-branch probe reads. The checked-out
    branch is deliberately neither, so `HEAD` and the default branch can't be
    confused for each other."""
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "work", path], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=path, check=True)
    subprocess.run(git + ["tag", "v0.9.0"], cwd=path, check=True)
    subprocess.run(git + ["symbolic-ref", "refs/remotes/origin/HEAD",
                          "refs/remotes/origin/" + default], cwd=path, check=True)


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

    def test_ignore_tag_refspec_push(self):
        # A release cut is not PR work — issue #34.
        self.assertIsNone(
            hook.detect_action("git push origin refs/tags/v1.3.0-rc.5"))
        self.assertIsNone(hook.detect_action("git push origin +refs/tags/v1.0"))
        self.assertIsNone(hook.detect_action("git push origin :refs/tags/v1.0"))

    def test_push_with_a_branch_among_the_refspecs_still_nudges(self):
        self.assertEqual(
            hook.detect_action("git push origin claude/foo refs/tags/v1.0"),
            "git_push")
        self.assertEqual(hook.detect_action("git push origin refs/heads/claude/foo"),
                         "git_push")

    def test_bare_tag_name_resolved_against_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp)
            self.assertIsNone(hook.detect_action("git push origin v9.9.9", tmp))
            self.assertEqual(hook.detect_action("git push origin claude/foo", tmp),
                             "git_push")
            self.assertEqual(hook.detect_action("git push origin HEAD", tmp),
                             "git_push")

    def test_ignore_default_branch_push(self):
        # A release cut pushes the default branch and a tag together — neither
        # ever has a PR of its own (Q5).
        with tempfile.TemporaryDirectory() as tmp:
            make_repo_with_default(tmp, "main")
            for cmd in ("git push origin HEAD:main v0.9.0",
                        "git push origin main",
                        "git push origin refs/heads/main",
                        "git push origin +main:main"):
                self.assertIsNone(hook.detect_action(cmd, tmp), cmd)
            # A branch that isn't the default still nudges, including the
            # checked-out one reached as `HEAD`.
            for cmd in ("git push origin claude/foo",
                        "git push origin HEAD",
                        "git push origin HEAD:main claude/foo"):
                self.assertEqual(hook.detect_action(cmd, tmp), "git_push", cmd)

    def test_default_branch_is_read_not_assumed(self):
        # `main` is not privileged: the probe reads the remote's own HEAD.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo_with_default(tmp, "trunk")
            self.assertIsNone(hook.detect_action("git push origin trunk", tmp))
            self.assertEqual(hook.detect_action("git push origin main", tmp),
                             "git_push")

    def test_default_branch_push_without_a_remote_head_still_nudges(self):
        # No symref to read: fail toward the old behaviour.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp)
            self.assertEqual(hook.detect_action("git push origin main", tmp),
                             "git_push")

    def test_bare_ref_without_a_repo_still_nudges(self):
        # The probe can't answer outside a repo; fail toward the old behaviour.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(hook.detect_action("git push origin v9.9.9", tmp),
                             "git_push")

    def test_unbalanced_quote_retries_line_by_line(self):
        # A contraction in a heredoc PR body made shlex reject the whole
        # string, so nothing classified and nothing nudged (#76).
        self.assertEqual(hook.detect_action(
            "cat > body.md <<'EOF'\nit's fine\nEOF\n"
            "gh pr create --body-file body.md"), "pr_create")
        self.assertEqual(hook.detect_action(
            "cat > body.md <<'EOF'\ndoesn't work\nEOF\n"
            "git push -u origin claude/foo"), "git_push")

    def test_unbalanced_quote_on_the_gh_line_itself(self):
        self.assertEqual(hook.detect_action('gh pr create --title "it\'s'),
                         "pr_create")

    def test_newline_separates_simple_commands(self):
        # shlex counted the newline as whitespace and ate it before the
        # punctuation rule could split on it, folding the create into the
        # preceding argv, so nothing nudged — issue #76.
        self.assertEqual(
            hook.simple_commands("echo hi\ngh pr create --title t"),
            [["echo", "hi"], ["gh", "pr", "create", "--title", "t"]])
        self.assertEqual(
            hook.detect_action("echo hi\ngh pr create --title t"), "pr_create")
        self.assertEqual(
            hook.detect_action("git status\ngit push -u origin claude/foo"),
            "git_push")

    def test_pr_create_after_a_heredoc(self):
        # The ordinary shape for a body longer than a line, and the one a
        # session reverts to even after applying the workaround (#76).
        self.assertEqual(hook.detect_action(
            "cat > body.md <<'EOF'\nmulti\nline body\nEOF\n"
            "gh pr create --body-file body.md"), "pr_create")

    def test_newline_inside_quotes_is_not_a_separator(self):
        self.assertEqual(
            hook.simple_commands('gh pr create -t "a\nb"'),
            [["gh", "pr", "create", "-t", "a\nb"]])

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

    def test_failure_heuristic_http_status(self):
        # `gh` API errors carry none of the literal signals (#54).
        self.assertTrue(hook.looks_failed(
            "HTTP 503: No server is currently available to service your "
            "request. (https://api.github.com/graphql)"))
        self.assertTrue(hook.looks_failed(
            "failed to get runs: HTTP 404: Not Found "
            "(https://api.github.com/repos/o/r/actions/runs)"))
        self.assertTrue(hook.looks_failed("HTTP 422 (https://api.github.com/x)"))
        # 2xx isn't a failure, and a bare `http` must not match a URL.
        self.assertFalse(hook.looks_failed("HTTP 200 OK"))
        self.assertFalse(hook.looks_failed("http://example.invalid/pull/1"))


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
        # No PR number known -> a placeholder pointing the session to resolve it,
        # and leave to drop the nudge if the branch has no PR at all (#34).
        self.assertIn("PR number", ctx)
        self.assertIn("no open PR, ignore this", ctx)

    def test_silent_on_tag_push(self):
        out, _ = run_hook(bash_payload(
            "git push origin refs/tags/v1.3.0-rc.5",
            "To github.com:o/r.git\n * [new tag]  v1.3.0-rc.5 -> v1.3.0-rc.5\n"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_bare_tag_push_with_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp)
            out, _ = run_hook(bash_payload(
                "git push origin v9.9.9",
                "To github.com:o/r.git\n * [new tag]  v9.9.9 -> v9.9.9\n",
                cwd=tmp))
        self.assertEqual(out.strip(), "")

    def test_silent_on_release_push_with_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo_with_default(tmp, "main")
            out, _ = run_hook(bash_payload(
                "git push origin main v0.9.0",
                "To github.com:o/r.git\n   abc1234..def5678  main -> main\n"
                " * [new tag]  v0.9.0 -> v0.9.0\n",
                cwd=tmp))
        self.assertEqual(out.strip(), "")

    def test_silent_on_failed_push(self):
        out, _ = run_hook(bash_payload(
            "git push origin claude/foo",
            "! [rejected] claude/foo -> claude/foo (fetch first)\nerror: failed to push"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_pr_create_that_hit_an_http_error(self):
        out, _ = run_hook(bash_payload(
            "gh pr create --fill",
            "HTTP 503: No server is currently available to service your "
            "request. (https://api.github.com/graphql)\n"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_pr_create_without_a_url(self):
        # A create that opened a PR prints its URL; without one there is no PR
        # to watch, whatever the reason (#57).
        out, _ = run_hook(bash_payload(
            "gh pr create --fill", "Warning: 1 uncommitted change\n"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_pr_create_that_opened_no_pr(self):
        # Flags that print instead of creating. `classify_command()` strips
        # flags, so all four arrive as `pr create` (#57).
        for command, response in (
            ("gh pr create --help",
             "Create a pull request on GitHub.\n\nUSAGE\n  gh pr create [flags]\n"),
            ("gh pr create -h",
             "Create a pull request on GitHub.\n\nUSAGE\n  gh pr create [flags]\n"),
            ("gh pr create --web",
             "Opening github.com/o/r/compare/main...claude/foo in your browser.\n"),
            ("gh pr create --fill --dry-run",
             "Would have created a Pull Request with:\ntitle:\tfix: a thing\n"),
        ):
            with self.subTest(command=command):
                out, _ = run_hook(bash_payload(command, response))
                self.assertEqual(out.strip(), "")

    def test_silent_on_interrupted_command(self):
        # A cancelled command's partial output carries no failure signal (#56).
        for command, stdout in (
            ("gh pr create --fill",
             "Creating pull request for claude/x into main\n"),
            ("git push -u origin claude/x", ""),
        ):
            out, _ = run_hook({
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_response": {"stdout": stdout, "stderr": "",
                                  "interrupted": True, "isImage": False},
            })
            self.assertEqual(out.strip(), "", command)

    def test_nudge_when_interrupted_is_false(self):
        out, _ = run_hook({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --fill"},
            "tool_response": {"stdout": "https://github.com/o/r/pull/7\n",
                              "stderr": "", "interrupted": False},
        })
        self.assertIn("#7", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_silent_on_unrelated_command(self):
        out, _ = run_hook(bash_payload("git status", " M file"))
        self.assertEqual(out.strip(), "")

    def test_silent_on_non_bash_tool(self):
        out, _ = run_hook({"hook_event_name": "PostToolUse",
                           "tool_name": "Read",
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
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --fill"},
            "tool_response": {"stdout": "https://github.com/o/r/pull/7\n",
                              "stderr": ""},
        })
        obj = json.loads(out)
        self.assertIn("#7", obj["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()


class LiveWatcherSuppression(unittest.TestCase):
    """A push to a PR this session is already watching must not produce a
    launch nudge: the running watcher re-reads the PR head every poll, so a
    second one only doubles the wake-ups."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _context(self, out):
        self.assertTrue(out.strip(), "expected additionalContext, got silence")
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_push_naming_a_watched_pr_is_told_not_to_relaunch(self):
        t = transcript_with_live_watcher(["42"], self.tmp.name)
        out, _ = run_hook(bash_payload(
            "git push", "https://github.com/o/r/pull/42\n", transcript=t))
        ctx = self._context(out)
        self.assertIn("ALREADY running", ctx)
        self.assertIn('TaskStop(task_id="bk42")', ctx)
        self.assertNotIn("Launch the PR Sentinel watcher", ctx)

    def test_push_naming_an_unwatched_pr_still_gets_the_launch_nudge(self):
        t = transcript_with_live_watcher(["42"], self.tmp.name,
                                         completed=["42"])
        out, _ = run_hook(bash_payload(
            "git push", "https://github.com/o/r/pull/42\n", transcript=t))
        self.assertIn("Launch the PR Sentinel watcher", self._context(out))

    def test_push_with_no_resolvable_number_names_the_live_prs(self):
        # The common shape: `git push` prints no PR URL, so the hook cannot tell
        # which PR this was. It names what is already watched and lets the
        # session decide, rather than asking for a launch unconditionally.
        t = transcript_with_live_watcher(["42", "43"], self.tmp.name)
        out, _ = run_hook(bash_payload("git push", "", transcript=t))
        ctx = self._context(out)
        self.assertIn("#42, #43", ctx)
        self.assertIn("do not launch a second watcher", ctx)

    def test_push_with_no_live_watcher_says_nothing_about_one(self):
        t = transcript_with_live_watcher([], self.tmp.name)
        out, _ = run_hook(bash_payload("git push", "", transcript=t))
        ctx = self._context(out)
        self.assertNotIn("already has a live watcher", ctx)
        self.assertIn("Launch the PR Sentinel watcher", ctx)

    def test_never_advises_restarting_a_running_watcher(self):
        # A live watcher re-reads the head SHA on every poll, so "restart it so
        # it tracks the latest push" was both wrong and the main source of
        # stacked watchers.
        for payload in (bash_payload("git push", ""),
                        bash_payload("gh pr create --fill",
                                     "https://github.com/o/r/pull/9\n")):
            out, _ = run_hook(payload)
            self.assertNotIn("restart it", self._context(out))

    def test_unreadable_transcript_falls_back_to_the_plain_nudge(self):
        out, _ = run_hook(bash_payload(
            "git push", "https://github.com/o/r/pull/42\n",
            transcript="/nonexistent/t.jsonl"))
        self.assertIn("Launch the PR Sentinel watcher", self._context(out))
