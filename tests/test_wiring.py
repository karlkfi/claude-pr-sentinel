#!/usr/bin/env python3
"""Wiring tests: the plugin manifests and hook registration are valid and point
at real files, and the versions agree.

Run with: python3 -m unittest discover tests
"""
import json
import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Claude Code reads a hook `timeout` in seconds (default 600). These hooks
# parse a transcript and at most shell out to `git rev-parse`; the suites run
# them under a 15s subprocess timeout. 60 is the ceiling a wedged one waits.
MAX_HOOK_TIMEOUT_SECONDS = 60

README = REPO / "README.md"

# The one script every hook event runs. See test_every_event_runs_the_same_script.
ENTRY_POINT = REPO / "scripts" / "pr-sentinel.py"

# How each language reads a knob: `${VAR:-default}` in the watcher,
# `os.environ` / `os.getenv` in the hooks. Narrow on purpose — a name that
# only appears in a denial message or a `--help` string is not a read, and
# shouldn't oblige a table row. The check runs one way for the same reason:
# a read this misses would otherwise report its documented row as stale.
ENV_READ = re.compile(
    r"\$\{(PR_SENTINEL_\w+)"
    r"|environ(?:\.get)?[\[(]\s*[\"'](PR_SENTINEL_\w+)[\"']"
    r"|getenv\(\s*[\"'](PR_SENTINEL_\w+)[\"']")


def load(rel):
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def env_vars_read():
    """Every `PR_SENTINEL_*` variable the shipped scripts actually read."""
    names = set()
    for path in sorted((REPO / "scripts").iterdir()):
        if path.suffix in (".sh", ".py"):
            for m in ENV_READ.finditer(path.read_text(encoding="utf-8")):
                names.add(next(g for g in m.groups() if g))
    return names


def undocumented_env_vars(readme):
    """The variables the scripts read that README's Configuration table omits.

    Scoped to that table rather than the whole README: a var explained in prose
    is still missing its default and its one-line effect, which is what someone
    reaching for a knob is looking for. Only the first column counts, so a row
    referring to a *sibling* var in its Effect cell doesn't document it.
    """
    section = readme.split("\n## Configuration\n")[1].split("\n## ")[0]
    documented = set()
    for line in section.splitlines():
        if line.startswith("|"):
            documented |= set(re.findall(r"PR_SENTINEL_\w+", line.split("|")[1]))
    return sorted(env_vars_read() - documented)


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
        self.assertIn(ENTRY_POINT.name, entries[0]["hooks"][0]["command"])

    def test_pretooluse_guard_registered(self):
        hooks = load("hooks/hooks.json")
        entries = hooks["hooks"]["PreToolUse"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["matcher"], "Bash")
        self.assertIn(ENTRY_POINT.name, entries[0]["hooks"][0]["command"])

    def test_stop_hook_registered(self):
        hooks = load("hooks/hooks.json")
        entries = hooks["hooks"]["Stop"]
        self.assertTrue(entries)
        # Stop hooks take no matcher (there is no tool to match on).
        self.assertNotIn("matcher", entries[0])
        self.assertIn(ENTRY_POINT.name, entries[0]["hooks"][0]["command"])

    def test_entry_point_exists_and_is_executable(self):
        self.assertTrue(ENTRY_POINT.is_file())
        self.assertTrue(os.access(ENTRY_POINT, os.X_OK),
                        "the hook entry point must be executable")

    def test_every_event_runs_the_same_script(self):
        """One script on every event, which is what makes the plugin reduce to
        a single label. A reader recovers the emitting plugin from the recorded
        hook command by taking its first `*.py` basename, so a script per event
        splits one plugin across three labels and no `--plugin` filter returns
        all of it. The basename is what is read, so three files in one
        directory cannot all carry the plugin's name."""
        hooks = load("hooks/hooks.json")
        commands = {hook["command"]
                    for entries in hooks["hooks"].values()
                    for entry in entries for hook in entry["hooks"]}
        self.assertEqual(len(commands), 1, "each event runs a different "
                         "script: " + ", ".join(sorted(commands)))
        basename = re.search(r"([\w.-]+\.py)", commands.pop()).group(1)
        self.assertEqual(basename, "pr-sentinel.py")

    def test_every_handler_module_exists(self):
        """The entry point dispatches to one module per event. It imports them
        lazily, so a missing one is an exception at decision time rather than
        at load — which fails open and is therefore silent."""
        for module in ("pr_sentinel_guard", "pr_sentinel_hook",
                       "pr_sentinel_stop_hook"):
            with self.subTest(module=module):
                self.assertTrue((REPO / "scripts" / (module + ".py")).is_file())

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


class Configuration(unittest.TestCase):
    def test_every_env_var_is_in_the_configuration_table(self):
        """A knob nobody can find is a knob that doesn't exist, and every var
        here arrived with its documentation self-attested. Same question as the
        PRIVACY gate asks of GitHub reads, asked by CI instead of a checkbox."""
        missing = undocumented_env_vars(README.read_text(encoding="utf-8"))
        self.assertEqual(
            missing, [],
            msg=("the scripts read these and README's Configuration table does "
                 "not list them: " + ", ".join(missing)))

    def test_the_check_can_fail(self):
        """A clean report and a broken extraction look identical, so prove the
        needle is found before trusting its absence."""
        readme = README.read_text(encoding="utf-8").replace(
            "`PR_SENTINEL_INTERVAL` | `30`", "")
        self.assertIn("PR_SENTINEL_INTERVAL", undocumented_env_vars(readme))


if __name__ == "__main__":
    unittest.main()
