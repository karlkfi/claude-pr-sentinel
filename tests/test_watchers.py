#!/usr/bin/env python3
"""Tests for scripts/pr_sentinel_watchers.py (the shared live-watcher read).

Run with: python3 -m unittest discover tests

All three hooks decide "is a watcher already running for this PR" through this
module, so the tests pin the rule itself: a launch with no completion
notification is live, a completed one is not, and the background task id — the
thing that makes stopping the incumbent one tool call — is recovered from both
shapes the harness records it in.

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
    def live(self, entries):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
            path = fh.name
        return watchers.live_watchers(path)

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

    def test_launch_with_no_recorded_task_id_is_still_live(self):
        # The id is what makes stopping cheap, not what makes a watcher live.
        self.assertEqual(self.live([launch("42")]), {"42": [""]})

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
            path = fh.name
        self.assertEqual(watchers.live_watchers(path), {"42": [""]})


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
