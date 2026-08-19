#!/usr/bin/env python3
"""Invariants of the per-item backlog store under docs/queue/.

Run with: python3 -m unittest discover tests

The store exists so that concurrent sessions never edit the same file. Two
things would quietly undo that — a committed rendered index, which every
completing session then has to edit, and a drift back to a single table — so
both are asserted here rather than left to review.
"""
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STORE = REPO / "docs" / "queue"
QUEUE_PY = REPO / "scripts" / "queue.py"

# A rendered index row names an item id in a Markdown table cell.
INDEX_ROW = re.compile(r"^\|.*\bQ\d+\b", re.M)


class QueueStoreTest(unittest.TestCase):
    def test_the_store_holds_only_items_and_its_readme(self):
        unexpected = sorted(p.name for p in STORE.iterdir()
                            if p.name != "README.md"
                            and not re.fullmatch(r"Q\d+\.md", p.name))
        self.assertEqual(unexpected, [],
                         "only Q<n>.md items and README.md belong in the store")

    def test_no_rendered_index_is_committed(self):
        # The index is built and served, never tracked: a committed one is the
        # single file every completing session would have to edit, which is the
        # contention this layout removes.
        offenders = [p.name for p in STORE.glob("*.md")
                     if not re.fullmatch(r"Q\d+\.md", p.name)
                     and INDEX_ROW.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [],
                         "a rendered index has been committed; render it with "
                         "`queue.py render` instead of tracking it")

    def test_the_legacy_table_and_its_tooling_are_gone(self):
        for gone in ("docs/STATUS.md", "scripts/lint-backlog.sh",
                     "scripts/next-task.sh", "scripts/backlog-metrics.sh"):
            self.assertFalse((REPO / gone).exists(),
                             f"{gone} serves the retired single-table layout")

    def test_the_store_lints_clean_and_is_not_empty(self):
        proc = subprocess.run(["python3", str(QUEUE_PY), "lint"],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # `lint` reports "0 item(s) OK" for a directory holding no items, so the
        # exit code alone cannot tell a clean store from one it never read.
        count = re.search(r"(\d+) item\(s\) OK", proc.stdout + proc.stderr)
        self.assertIsNotNone(count, "lint did not report an item count")
        self.assertGreater(int(count.group(1)), 0, "lint checked no items")


if __name__ == "__main__":
    unittest.main()
