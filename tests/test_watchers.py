#!/usr/bin/env python3
"""Tests for scripts/pr_sentinel_watchers.py (the shared live-watcher read).

Run with: python3 -m unittest discover tests

All three hooks decide "is a watcher already running for this PR" through this
module, so the tests pin the rule itself: a launch that the harness gave a
background task id and that has not reported completion is live, a completed
one is not, one with no task id never started, and the id — the thing that
makes stopping the incumbent one tool call — is recovered from both shapes the
harness records it in.

Fixture rule: never use real PR URLs, hosts, or credentials — synthetic
owner/repo and PR numbers exercise identical code paths.
"""
import json
import tempfile
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "scripts" / "pr_sentinel_watchers.py"

_spec = util.spec_from_file_location("pr_sentinel_watchers", MODULE)
watchers = util.module_from_spec(_spec)
_spec.loader.exec_module(watchers)

WATCHER = "/opt/plugins/pr-sentinel/scripts/pr-sentinel-watch.sh"


def launch(pr, tool_id="toolu_w", background=True):
    inp = {"command": f'bash "{WATCHER}" {pr}', "description": "watch"}
    if background:
        inp["run_in_background"] = True
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": inp}]}}


def launch_result(tool_id="toolu_w", task_id="bk1", structured=True):
    """The harness's answer to a backgrounded launch. It reports the task id
    twice — a `toolUseResult.backgroundTaskId` field and a sentence in the
    result text — and the module reads either."""
    entry = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id,
         "content": (f"Command running in background with ID: {task_id}. "
                     f"Output is being written to: /tmp/s/tasks/{task_id}.output.")}]}}
    if structured:
        entry["toolUseResult"] = {"stdout": "", "stderr": "",
                                  "backgroundTaskId": task_id}
    return entry


def completion(tool_id="toolu_w", task_id="bk1", status="completed"):
    content = ("<task-notification>\n"
               f"<task-id>{task_id}</task-id>\n"
               f"<tool-use-id>{tool_id}</tool-use-id>\n"
               f"<output-file>/tmp/s/tasks/{task_id}.output</output-file>\n"
               f"<status>{status}</status>\n"
               "</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "content": content}


class LiveWatchers(unittest.TestCase):
    def live(self, entries, exclude=()):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
            path = fh.name
        return watchers.live_watchers(path, exclude=exclude)

    def test_launch_without_completion_is_live(self):
        self.assertEqual(
            self.live([launch("42"), launch_result()]), {"42": ["bk1"]})

    def test_completed_launch_is_not_live(self):
        self.assertEqual(
            self.live([launch("42"), launch_result(), completion()]), {})

    def test_task_id_recovered_from_the_result_text_alone(self):
        self.assertEqual(
            self.live([launch("42"), launch_result(structured=False)]),
            {"42": ["bk1"]})

    def test_launch_with_no_recorded_task_id_is_not_a_live_watcher(self):
        # The harness answers every backgrounded launch with a task id before
        # the next turn, so a launch that has none never started — most often
        # the in-flight call the PreToolUse hook is deciding right now, whose
        # tool_use entry the harness has already written. Counting it live let
        # the guard refuse a session's FIRST watcher as a duplicate of itself.
        self.assertEqual(self.live([launch("42")]), {})

    def test_the_in_flight_launch_is_excluded_by_its_tool_use_id(self):
        # Belt and braces: even where the transcript somehow carries a task id
        # for the call under decision, naming it excludes it.
        self.assertEqual(
            self.live([launch("42"), launch_result()],
                      exclude=("toolu_w",)), {})

    def test_excluding_one_launch_leaves_another_live(self):
        self.assertEqual(
            self.live([launch("42", "toolu_a"),
                       launch_result("toolu_a", "bk1"),
                       launch("42", "toolu_b"),
                       launch_result("toolu_b", "bk2")],
                      exclude=("toolu_b",)),
            {"42": ["bk1"]})

    def test_relaunch_after_completion_reports_only_the_live_one(self):
        self.assertEqual(
            self.live([launch("42", "toolu_a"),
                       launch_result("toolu_a", "bk1"),
                       completion("toolu_a", "bk1"),
                       launch("42", "toolu_b"),
                       launch_result("toolu_b", "bk2")]),
            {"42": ["bk2"]})

    def test_stacked_launches_report_every_live_task(self):
        self.assertEqual(
            self.live([launch("42", "toolu_a"), launch_result("toolu_a", "bk1"),
                       launch("42", "toolu_b"), launch_result("toolu_b", "bk2")]),
            {"42": ["bk1", "bk2"]})

    def test_separate_prs_do_not_mask_each_other(self):
        self.assertEqual(
            self.live([launch("42", "toolu_a"), launch_result("toolu_a", "bk1"),
                       launch("43", "toolu_b"), launch_result("toolu_b", "bk2"),
                       completion("toolu_a", "bk1")]),
            {"43": ["bk2"]})

    def test_foreground_run_is_not_a_watcher_this_can_stop(self):
        self.assertEqual(self.live([launch("42", background=False)]), {})

    def test_url_argument_resolves_to_the_number(self):
        entry = launch("42")
        entry["message"]["content"][0]["input"]["command"] = (
            f'bash "{WATCHER}" https://github.com/owner/repo/pull/42')
        self.assertEqual(self.live([entry, launch_result()]), {"42": ["bk1"]})

    def test_launch_carrying_its_status_out_is_live(self):
        # `… watch.sh 42; exit $?` is the shape exit-status-guard asks for on a
        # backgrounded call, and the `;` is glued to the PR argument.
        entry = launch("42")
        entry["message"]["content"][0]["input"]["command"] = (
            f'bash "{WATCHER}" 42; exit $?')
        self.assertEqual(self.live([entry, launch_result()]), {"42": ["bk1"]})

    def test_a_killed_task_is_not_live(self):
        # Any status closes the launch: the process is gone either way.
        self.assertEqual(
            self.live([launch("42"), launch_result(),
                       completion(status="killed")]), {})


class FailOpen(unittest.TestCase):
    def test_missing_transcript_yields_nothing(self):
        self.assertEqual(watchers.live_watchers("/nonexistent/x.jsonl"), {})

    def test_no_path_yields_nothing(self):
        self.assertEqual(watchers.live_watchers(None), {})

    def test_unparseable_lines_are_skipped_not_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write("{not json at all\n")
            fh.write(json.dumps(launch("42")) + "\n")
            fh.write(json.dumps(launch_result()) + "\n")
            path = fh.name
        self.assertEqual(watchers.live_watchers(path), {"42": ["bk1"]})


class ForeignWatchers(unittest.TestCase):
    """A background watch this session armed that is not this plugin's. It is
    never an ownership signal and never makes a PR watched — the Stop hook only
    names it — so these pin what the matcher does and does not claim."""

    def pr(self, command):
        return watchers.foreign_watch_pr(command)

    def scan(self, entries):
        s = watchers.WatcherScan()
        for e in entries:
            s.feed(e)
        return s

    def foreign_launch(self, tool_id="toolu_f", background=True,
                       command="python3 /s/pr-mergeability-watch.py 42"):
        entry = launch("42", tool_id=tool_id, background=background)
        entry["message"]["content"][0]["input"]["command"] = command
        return entry

    def test_recognises_another_tool_s_pr_watch(self):
        self.assertEqual(
            self.pr("python3 /skills/scripts/pr-mergeability-watch.py 42"),
            ("42", "pr-mergeability-watch.py"))

    def test_a_separated_option_value_is_not_the_pr(self):
        # `--timeout 600` is a number and is not a PR number.
        self.assertEqual(
            self.pr("python3 /s/pr-mergeability-watch.py --timeout 600 42"),
            ("42", "pr-mergeability-watch.py"))

    def test_a_pr_url_resolves_to_the_number(self):
        self.assertEqual(
            self.pr("python3 /s/watch.py https://github.com/o/r/pull/9"),
            ("9", "watch.py"))

    def test_gh_pr_view_is_not_a_watcher(self):
        # The negative case that keeps the matcher from firing on any
        # backgrounded `gh` call that happens to name a PR.
        self.assertIsNone(self.pr("gh pr view 42"))

    def test_a_watch_flag_is_not_a_watcher_basename(self):
        self.assertIsNone(self.pr("gh pr checks --watch 42"))

    def test_this_plugin_s_own_watcher_is_never_foreign(self):
        self.assertIsNone(self.pr(f'bash "{WATCHER}" 42'))

    def test_a_watcher_with_no_pr_operand_resolves_nothing(self):
        self.assertIsNone(self.pr("python3 /s/pr-mergeability-watch.py --help"))

    def test_an_unparseable_command_yields_nothing(self):
        self.assertIsNone(self.pr("python3 /s/watch.py 'unbalanced 42"))

    def test_a_live_foreign_launch_is_reported_with_its_script(self):
        scan = self.scan([self.foreign_launch(),
                          launch_result(tool_id="toolu_f")])
        self.assertEqual(scan.foreign(), {"42": ["pr-mergeability-watch.py"]})

    def test_a_foreign_launch_is_not_an_ownership_signal(self):
        # `pr_by_toolid` feeds the Stop hook's `owned` set, so a foreign watch
        # on someone else's PR must stay out of it.
        scan = self.scan([self.foreign_launch(),
                          launch_result(tool_id="toolu_f")])
        self.assertEqual(scan.pr_by_toolid, {})
        self.assertEqual(scan.live(), {})

    def test_a_completed_foreign_launch_is_not_live(self):
        scan = self.scan([self.foreign_launch(),
                          launch_result(tool_id="toolu_f"),
                          completion(tool_id="toolu_f")])
        self.assertEqual(scan.foreign(), {})

    def test_a_foreground_foreign_run_is_not_a_watch(self):
        scan = self.scan([self.foreign_launch(background=False),
                          launch_result(tool_id="toolu_f")])
        self.assertEqual(scan.foreign(), {})


class StopHint(unittest.TestCase):
    def test_names_the_single_task_id(self):
        hint = watchers.stop_hint("42", ["bk1"])
        self.assertIn('TaskStop(task_id="bk1")', hint)

    def test_lists_every_task_when_several_are_stacked(self):
        hint = watchers.stop_hint("42", ["bk1", "bk2"])
        self.assertIn('"bk1"', hint)
        self.assertIn('"bk2"', hint)

    def test_still_actionable_with_no_task_id(self):
        hint = watchers.stop_hint("42", [""])
        self.assertIn("TaskStop", hint)


if __name__ == "__main__":
    unittest.main()
