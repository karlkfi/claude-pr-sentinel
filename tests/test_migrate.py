#!/usr/bin/env python3
"""Tests for scripts/pr-sentinel-migrate-autofix.py (the migration helper).

Run with: python3 -m unittest discover tests

Builds a synthetic session store in a temp dir (via PR_SENTINEL_SESSIONS_ROOT)
and drives the script as a subprocess, asserting the dry-run report, the
--apply mutation + backup, the scope rules (MERGED-only default, --all widens),
and the schema no-op. No real session paths, repos, or PR numbers are used.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pr-sentinel-migrate-autofix.py"

_spec = util.spec_from_file_location("pr_sentinel_migrate", SCRIPT)
migrate = util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def session(**fields):
    """A minimal session-file dict with sensible defaults."""
    data = {"autoFixEnabled": True, "prState": "MERGED",
            "prRepository": "acme/widgets", "prNumber": 1, "title": "a fix"}
    data.update(fields)
    return data


def build_store(root, sessions):
    """Write {relpath: dict} under root as local_*.json files."""
    for rel, data in sessions.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")  # raw (e.g. malformed)
        else:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run(root, *args, assume_quit=True):
    env = dict(os.environ)
    env["PR_SENTINEL_SESSIONS_ROOT"] = str(root)
    if assume_quit:
        env["PR_SENTINEL_ASSUME_APP_QUIT"] = "1"
    else:
        env.pop("PR_SENTINEL_ASSUME_APP_QUIT", None)
    proc = subprocess.run(
        ["python3", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30, check=False)
    return proc


def read_flag(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))["autoFixEnabled"]


class Unit(unittest.TestCase):
    def test_in_scope_default_is_merged_only(self):
        merged = migrate.Session(Path("x"), "{}", session(prState="MERGED"))
        openpr = migrate.Session(Path("x"), "{}", session(prState="OPEN"))
        self.assertTrue(migrate.in_scope(merged, "merged"))
        self.assertFalse(migrate.in_scope(openpr, "merged"))
        self.assertTrue(migrate.in_scope(openpr, "all"))

    def test_dump_like_preserves_style(self):
        indented = migrate.dump_like({"a": 1}, "{\n  \"a\": 1\n}")
        self.assertIn("\n", indented)
        compact = migrate.dump_like({"a": 1}, '{"a":1}')
        self.assertNotIn("\n", compact)


class DryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_reports_counts_and_mutates_nothing(self):
        build_store(self.root, {
            "inst/ws/local_a.json": session(prState="MERGED"),
            "inst/ws/local_b.json": session(prState="OPEN", prNumber=2),
            "inst/ws/local_c.json": session(autoFixEnabled=False, prNumber=3),
        })
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Sessions with auto-fix enabled: 2", proc.stdout)
        self.assertIn("MERGED-PR sessions — 1", proc.stdout)
        self.assertIn("would disable", proc.stdout)
        # dry run never writes
        self.assertTrue(read_flag(self.root / "inst/ws/local_a.json"))
        self.assertTrue(read_flag(self.root / "inst/ws/local_b.json"))

    def test_unknown_schema_is_clean_noop(self):
        build_store(self.root, {
            "inst/ws/local_x.json": {"somethingElse": True},
            "inst/ws/local_y.json": "{not json",
        })
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("No files matching the expected schema", proc.stdout)

    def test_missing_store_errors(self):
        proc = run(self.root / "does-not-exist")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("session store not found", proc.stderr)


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        build_store(self.root, {
            "inst/ws/local_merged.json": session(prState="MERGED"),
            "inst/ws/local_open.json": session(prState="OPEN", prNumber=2),
            "inst/ws/local_off.json": session(autoFixEnabled=False, prNumber=3),
            "inst/ws/local_other.json": {"noFlagHere": 1},
        })

    def test_default_disables_only_merged(self):
        proc = run(self.root, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Disabled auto-fix on 1 session", proc.stdout)
        self.assertFalse(read_flag(self.root / "inst/ws/local_merged.json"))
        # OPEN left armed; already-off untouched; unknown schema untouched
        self.assertTrue(read_flag(self.root / "inst/ws/local_open.json"))
        self.assertFalse(read_flag(self.root / "inst/ws/local_off.json"))
        self.assertEqual(
            json.loads((self.root / "inst/ws/local_other.json")
                       .read_text()), {"noFlagHere": 1})

    def test_backup_created_and_restores_original(self):
        run(self.root, "--apply")
        backups = list(self.root.glob(".autofix-backup-*/inst/ws/local_merged.json"))
        self.assertEqual(len(backups), 1, "backup of the edited file must exist")
        self.assertTrue(read_flag(backups[0]),
                        "backup must preserve the original true value")

    def test_rerun_ignores_own_backups(self):
        # A second apply must not descend into the backup dir the first created
        # (those copies carry the original true value).
        run(self.root, "--apply")
        proc = run(self.root, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Sessions with auto-fix enabled: 1", proc.stdout)  # OPEN only
        self.assertIn("MERGED-PR sessions — 0", proc.stdout)

    def test_all_scope_disables_open_too(self):
        proc = run(self.root, "--apply", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(read_flag(self.root / "inst/ws/local_merged.json"))
        self.assertFalse(read_flag(self.root / "inst/ws/local_open.json"))

    def test_apply_refuses_while_app_running(self):
        # Detection is non-deterministic under subprocess (depends on whether
        # the real app is up), so drive main() in-process with app_running
        # patched, and without the assume-quit env.
        os.environ.pop("PR_SENTINEL_ASSUME_APP_QUIT", None)
        orig = migrate.app_running
        try:
            migrate.app_running = lambda: True
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = migrate.main(["--apply", "--root", str(self.root)])
        finally:
            migrate.app_running = orig
        self.assertEqual(rc, 2)
        # nothing edited
        self.assertTrue(read_flag(self.root / "inst/ws/local_merged.json"))

    def test_apply_refuses_when_detection_unavailable(self):
        os.environ.pop("PR_SENTINEL_ASSUME_APP_QUIT", None)
        orig = migrate.app_running
        try:
            migrate.app_running = lambda: None
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = migrate.main(["--apply", "--root", str(self.root)])
        finally:
            migrate.app_running = orig
        self.assertEqual(rc, 2)
        self.assertTrue(read_flag(self.root / "inst/ws/local_merged.json"))


if __name__ == "__main__":
    unittest.main()
