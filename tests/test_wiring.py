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

    def test_watcher_present_and_executable(self):
        watcher = REPO / "scripts" / "pr-sentinel-watch.sh"
        self.assertTrue(watcher.is_file())
        self.assertTrue(os.access(watcher, os.X_OK),
                        "watcher script must be executable")
        self.assertTrue(watcher.read_text(encoding="utf-8")
                        .startswith("#!/usr/bin/env bash"))

    def test_agents_symlink(self):
        agents = REPO / "AGENTS.md"
        self.assertTrue(agents.is_symlink(), "AGENTS.md should symlink to CLAUDE.md")
        self.assertEqual(os.readlink(agents), "CLAUDE.md")


if __name__ == "__main__":
    unittest.main()
