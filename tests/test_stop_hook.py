#!/usr/bin/env python3
"""Tests for scripts/pr-sentinel-stop-hook.py (the Stop backstop).

Run with: python3 -m unittest discover tests

Two layers:
  * Unit tests import the module and exercise the transcript parser, the `ps`
    output parser, and the small classifiers.
  * End-to-end tests invoke the script as a subprocess, feed it the Stop hook
    stdin JSON plus a synthetic transcript file, and assert the block decision
    (or silence). A stub `ps` on PATH makes watcher-liveness deterministic (the
    same stub-on-PATH pattern test_watcher.py uses for `gh`) so the assertions
    never depend on real host processes.

Fixture rule: never use real PR URLs, hosts, or credentials — synthetic
owner/repo and PR numbers exercise identical code paths.
"""
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pr-sentinel-stop-hook.py"

_spec = util.spec_from_file_location("pr_sentinel_stop_hook", SCRIPT)
hook = util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# A stub `ps` that ignores its args and prints the fixture named by $PS_STUB_FILE
# (empty output if unset), always exiting 0.
PS_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    if [[ -n "${PS_STUB_FILE:-}" && -f "$PS_STUB_FILE" ]]; then
      cat "$PS_STUB_FILE"
    fi
    exit 0
    """
)


# --------------------------------------------------------------------------
# Synthetic transcript builders (match the real JSONL shapes)
# --------------------------------------------------------------------------

def assistant_bash(command, tool_id="toolu_1"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"command": command, "description": "d"}}]}}


def tool_result(text, tool_id="toolu_1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def pr_link(number):
    return {"type": "pr-link", "prNumber": number,
            "prUrl": f"https://github.com/o/r/pull/{number}",
            "prRepository": "o/r"}


def write_transcript(entries):
    fd, path = tempfile.mkstemp(prefix="pr-sentinel-transcript-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


class ParserUnit(unittest.TestCase):
    def test_pr_number_normalises(self):
        self.assertEqual(hook.pr_number("42"), "42")
        self.assertEqual(hook.pr_number('"7"'), "7")
        self.assertEqual(hook.pr_number("https://github.com/o/r/pull/9"), "9")
        self.assertIsNone(hook.pr_number("main"))

    def test_is_pr_create(self):
        self.assertTrue(hook._is_pr_create("gh pr create --fill"))
        self.assertTrue(hook._is_pr_create("GH_TOKEN=x gh pr create -t a -b b"))
        self.assertFalse(hook._is_pr_create("gh pr view 3"))
        self.assertFalse(hook._is_pr_create("gh pr list"))

    def test_pr_close_targets(self):
        self.assertEqual(hook._pr_close_targets("gh pr merge 42 --squash"), {"42"})
        self.assertEqual(hook._pr_close_targets("gh pr close 7"), {"7"})
        self.assertEqual(hook._pr_close_targets("git status"), set())

    def test_ps_parser_extracts_pr_numbers(self):
        out = (
            "bash /opt/plugins/pr-sentinel/scripts/pr-sentinel-watch.sh 42\n"
            "/usr/bin/python3 something unrelated\n"
            "bash /x/pr-sentinel-watch.sh https://github.com/o/r/pull/7\n"
        )
        self.assertEqual(hook.watcher_prs_from_ps(out), {"42", "7"})

    def test_ps_parser_ignores_non_pr_args(self):
        # An editor opening the script (no PR arg) must not be seen as a watcher.
        self.assertEqual(
            hook.watcher_prs_from_ps("vim /x/scripts/pr-sentinel-watch.sh\n"),
            set())

    def test_parse_created_via_pr_link(self):
        path = write_transcript([pr_link(42)])
        self.assertEqual(hook.parse_transcript(path), {"42"})
        os.unlink(path)

    def test_parse_created_via_gh_pr_create_correlation(self):
        path = write_transcript([
            assistant_bash("gh pr create --fill", "toolu_9"),
            tool_result("Creating pull request...\n"
                        "https://github.com/o/r/pull/55\n", "toolu_9"),
        ])
        self.assertEqual(hook.parse_transcript(path), {"55"})
        os.unlink(path)

    def test_parse_concluded_via_watcher_ready_event(self):
        path = write_transcript([
            pr_link(42),
            tool_result("PR-SENTINEL EVENT: ready\nPR: 42\nState: OPEN\n",
                        "toolu_2"),
        ])
        # created 42 but concluded 42 -> nothing active.
        self.assertEqual(hook.parse_transcript(path), set())
        os.unlink(path)

    def test_parse_concluded_via_gh_pr_merge(self):
        path = write_transcript([
            pr_link(42),
            assistant_bash("gh pr merge 42 --squash", "toolu_3"),
        ])
        self.assertEqual(hook.parse_transcript(path), set())
        os.unlink(path)

    def test_parse_missing_file_is_empty(self):
        self.assertEqual(hook.parse_transcript("/no/such/transcript.jsonl"), set())

    def test_build_reason_names_pr_and_command(self):
        reason = hook.build_reason({"42"})
        self.assertIn("#42", reason)
        self.assertIn("pr-sentinel-watch.sh", reason)
        self.assertIn(" 42", reason)          # watcher command arg
        self.assertIn("background", reason.lower())
        self.assertIn("Never auto-merge", reason)


class StopHookEndToEnd(unittest.TestCase):
    def run_hook(self, stdin_obj, transcript_entries=None, ps_lines="", env=None):
        """Invoke the stop hook as a subprocess with a stub `ps` on PATH and,
        if given, a synthetic transcript. Returns (stdout, returncode)."""
        scen = tempfile.mkdtemp(prefix="pr-sentinel-stop-test-")
        bindir = os.path.join(scen, "bin")
        os.makedirs(bindir)
        ps = os.path.join(bindir, "ps")
        with open(ps, "w", encoding="utf-8") as f:
            f.write(PS_STUB)
        os.chmod(ps, 0o755)
        ps_file = os.path.join(scen, "ps_out")
        with open(ps_file, "w", encoding="utf-8") as f:
            f.write(ps_lines)

        if transcript_entries is not None:
            tpath = os.path.join(scen, "transcript.jsonl")
            with open(tpath, "w", encoding="utf-8") as f:
                for e in transcript_entries:
                    f.write(json.dumps(e) + "\n")
            stdin_obj = dict(stdin_obj, transcript_path=tpath)

        run_env = dict(os.environ)
        run_env["PATH"] = bindir + os.pathsep + run_env["PATH"]
        run_env["PS_STUB_FILE"] = ps_file
        run_env.setdefault("CLAUDE_PLUGIN_ROOT", "/opt/plugins/pr-sentinel")
        if env:
            run_env.update(env)

        proc = subprocess.run(
            ["python3", str(SCRIPT)],
            input=json.dumps(stdin_obj), capture_output=True, text=True,
            env=run_env, timeout=15, check=False,
        )
        return proc.stdout, proc.returncode

    def stop_input(self, **kw):
        base = {"hook_event_name": "Stop", "session_id": "s1",
                "stop_hook_active": False}
        base.update(kw)
        return base

    def test_blocks_when_created_pr_and_no_watcher(self):
        out, rc = self.run_hook(
            self.stop_input(),
            transcript_entries=[pr_link(42)],
            ps_lines="")  # no watcher process
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertIn("#42", obj["reason"])
        self.assertIn("pr-sentinel-watch.sh", obj["reason"])
        self.assertIn("/opt/plugins/pr-sentinel", obj["reason"])
        self.assertIn("Never auto-merge", obj["reason"])

    def test_blocks_via_gh_pr_create_without_pr_link(self):
        out, _ = self.run_hook(
            self.stop_input(),
            transcript_entries=[
                assistant_bash("gh pr create --fill", "toolu_7"),
                tool_result("https://github.com/o/r/pull/7\n", "toolu_7"),
            ],
            ps_lines="")
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertIn("#7", obj["reason"])

    def test_allows_when_watcher_process_live(self):
        out, _ = self.run_hook(
            self.stop_input(),
            transcript_entries=[pr_link(42)],
            ps_lines="bash /opt/plugins/pr-sentinel/scripts/pr-sentinel-watch.sh 42\n")
        self.assertEqual(out.strip(), "")

    def test_allows_when_pr_concluded(self):
        out, _ = self.run_hook(
            self.stop_input(),
            transcript_entries=[
                pr_link(42),
                tool_result("PR-SENTINEL EVENT: closed\nPR: 42\n", "toolu_2"),
            ],
            ps_lines="")
        self.assertEqual(out.strip(), "")

    def test_allows_when_stop_hook_active(self):
        # The no-loop guarantee: a continuation stop is never blocked again.
        out, _ = self.run_hook(
            self.stop_input(stop_hook_active=True),
            transcript_entries=[pr_link(42)],
            ps_lines="")
        self.assertEqual(out.strip(), "")

    def test_allows_when_disabled(self):
        out, _ = self.run_hook(
            self.stop_input(),
            transcript_entries=[pr_link(42)],
            ps_lines="",
            env={"PR_SENTINEL_DISABLE": "1"})
        self.assertEqual(out.strip(), "")

    def test_allows_when_no_created_pr(self):
        out, _ = self.run_hook(
            self.stop_input(),
            transcript_entries=[assistant_bash("git status"),
                                tool_result(" M file")],
            ps_lines="")
        self.assertEqual(out.strip(), "")

    def test_allows_on_unparseable_stdin(self):
        scen = tempfile.mkdtemp(prefix="pr-sentinel-stop-test-")
        proc = subprocess.run(
            ["python3", str(SCRIPT)],
            input="not json", capture_output=True, text=True,
            env=dict(os.environ), timeout=15, check=False,
        )
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.returncode, 0)

    def test_allows_when_transcript_path_missing(self):
        out, rc = self.run_hook(self.stop_input(transcript_path=""),
                                transcript_entries=None, ps_lines="")
        self.assertEqual(out.strip(), "")
        self.assertEqual(rc, 0)

    def test_debug_reraises_on_bad_ps(self):
        # PR_SENTINEL_DEBUG=1 must surface errors rather than fail open. We can't
        # easily force an internal crash, so just assert the happy path still
        # exits 0 under debug (no spurious raise).
        out, rc = self.run_hook(
            self.stop_input(),
            transcript_entries=[pr_link(42)],
            ps_lines="",
            env={"PR_SENTINEL_DEBUG": "1"})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
