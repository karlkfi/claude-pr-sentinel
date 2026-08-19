#!/usr/bin/env python3
"""Wiring tests: the plugin manifests and hook registration are valid and point
at real files, and the versions agree.

Run with: python3 -m unittest discover tests
"""
import json
import os
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Claude Code reads a hook `timeout` in seconds (default 600). These hooks
# parse a transcript and at most shell out to `git rev-parse`; the suites run
# them under a 15s subprocess timeout. 60 is the ceiling a wedged one waits.
MAX_HOOK_TIMEOUT_SECONDS = 60


def load(rel):
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


class Wiring(unittest.TestCase):
    def test_plugin_json_valid(self):
        pj = load(".claude-plugin/plugin.json")
        self.assertEqual(pj["name"], "pr-sentinel")
        self.assertIn("version", pj)
        self.assertEqual(pj["license"], "MIT")

    def test_marketplace_matches_plugin(self):
        pj = load(".claude-plugin/plugin.json")
        mp = load(".claude-plugin/marketplace.json")
        plugin = mp["plugins"][0]
        self.assertEqual(plugin["name"], "pr-sentinel")
        self.assertEqual(plugin["version"], pj["version"],
                         "marketplace and plugin versions must agree")
        self.assertEqual(plugin["source"]["repo"], "karlkfi/claude-pr-sentinel")

    def test_hooks_json_points_at_real_script(self):
        hooks = load("hooks/hooks.json")
        entries = hooks["hooks"]["PostToolUse"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["matcher"], "Bash")
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn("pr-sentinel-hook.py", cmd)
        self.assertTrue((REPO / "scripts" / "pr-sentinel-hook.py").is_file())

    def test_pretooluse_guard_registered(self):
        hooks = load("hooks/hooks.json")
        entries = hooks["hooks"]["PreToolUse"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["matcher"], "Bash")
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn("pr-sentinel-guard.py", cmd)
        guard = REPO / "scripts" / "pr-sentinel-guard.py"
        self.assertTrue(guard.is_file())
        self.assertTrue(os.access(guard, os.X_OK),
                        "guard script must be executable")

    def test_stop_hook_registered_and_points_at_real_script(self):
        hooks = load("hooks/hooks.json")
        entries = hooks["hooks"]["Stop"]
        self.assertTrue(entries)
        # Stop hooks take no matcher (there is no tool to match on).
        self.assertNotIn("matcher", entries[0])
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn("pr-sentinel-stop-hook.py", cmd)
        script = REPO / "scripts" / "pr-sentinel-stop-hook.py"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK),
                        "stop hook script must be executable")

    def test_hook_timeouts_are_bounded(self):
        """Every registered hook declares a timeout, in seconds. A value meant as
        milliseconds reads as hours and leaves a hung hook wedged (#72)."""
        hooks = load("hooks/hooks.json")
        for event, entries in hooks["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("timeout", hook,
                                  f"{event} hook must declare a timeout")
                    self.assertLessEqual(
                        hook["timeout"], MAX_HOOK_TIMEOUT_SECONDS,
                        f"{event} hook timeout is in seconds, not milliseconds")

    def test_watcher_present_and_executable(self):
        watcher = REPO / "scripts" / "pr-sentinel-watch.sh"
        self.assertTrue(watcher.is_file())
        self.assertTrue(os.access(watcher, os.X_OK),
                        "watcher script must be executable")
        self.assertTrue(watcher.read_text(encoding="utf-8")
                        .startswith("#!/usr/bin/env bash"))

    def test_migrate_helper_present_and_executable(self):
        script = REPO / "scripts" / "pr-sentinel-migrate-autofix.py"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK),
                        "migration helper must be executable")

    def test_migrate_command_present(self):
        cmd = REPO / "commands" / "pr-sentinel-migrate-autofix.md"
        self.assertTrue(cmd.is_file(), "migration slash command must exist")
        text = cmd.read_text(encoding="utf-8")
        self.assertIn("pr-sentinel-migrate-autofix.py", text,
                      "command must point at the helper script")

    def test_friction_report_present_and_executable(self):
        script = REPO / "scripts" / "friction-report.py"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK),
                        "activity report must be executable")

    def test_friction_report_command_present(self):
        cmd = REPO / "commands" / "pr-sentinel-friction-report.md"
        self.assertTrue(cmd.is_file(), "activity report slash command must exist")
        text = cmd.read_text(encoding="utf-8")
        self.assertIn("friction-report.py", text,
                      "command must point at the report script")

    def test_agents_symlink(self):
        agents = REPO / "AGENTS.md"
        self.assertTrue(agents.is_symlink(), "AGENTS.md should symlink to CLAUDE.md")
        self.assertEqual(os.readlink(agents), "CLAUDE.md")


if __name__ == "__main__":
    unittest.main()
