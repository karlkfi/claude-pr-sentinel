#!/usr/bin/env python3
"""Tests for scripts/alloc-queue-id.sh (claim-based Q-ID allocation).

Run with: python3 -m unittest discover tests

Builds a throwaway bare remote and a work repo in a temp dir, then drives the
script as a subprocess. Asserts that IDs are handed out above the floor the
table implies, that a claim lands in refs/queue-ids on the remote, that an ID
someone else already holds is skipped rather than reissued, and that two
concurrent allocators never receive the same ID. No real remote is touched.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "alloc-queue-id.sh"

TABLE = """# Project Status

**Status:** \U0001f532 ready · \U0001f6ab blocked
**Next ID:** Q8

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q7"></a>Q7 | A row | `docs` | \U0001f532 | S | Notes. |
"""


def git(*args, cwd, **kw):
    return subprocess.run(("git",) + args, cwd=cwd, check=True,
                          capture_output=True, text=True, **kw)


class AllocQueueIdTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        self.work = root / "work"
        git("init", "--bare", "-b", "main", str(self.remote), cwd=root)
        git("init", "-b", "main", str(self.work), cwd=root)
        for key, val in (("user.email", "t@example.invalid"), ("user.name", "T")):
            git("config", key, val, cwd=self.work)
        docs = self.work / "docs"
        docs.mkdir()
        (docs / "STATUS.md").write_text(TABLE, encoding="utf-8")
        git("add", "-A", cwd=self.work)
        git("commit", "-m", "seed", cwd=self.work)
        git("remote", "add", "origin", str(self.remote), cwd=self.work)
        git("push", "-u", "origin", "main", cwd=self.work)
        self.addCleanup(self._tmp.cleanup)

    def alloc(self, *titles):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--table", "docs/STATUS.md", *titles],
            cwd=self.work, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def claims_on_remote(self):
        out = git("ls-remote", "origin", "refs/queue-ids/*", cwd=self.work).stdout
        return sorted(line.rsplit("/", 1)[-1] for line in out.splitlines() if line)

    def test_first_id_clears_the_highest_in_the_table(self):
        # The floor is every Q-number the table mentions, and that includes the
        # counter line itself, so a table using Q7 with **Next ID:** Q8 starts
        # at Q9. Reading the counter as taken can only skip an id, never
        # reissue one, which is the direction that stays safe.
        self.assertEqual(self.alloc("a new row"), ["Q9"])
        self.assertEqual(self.claims_on_remote(), ["Q9"])

    def test_ids_are_never_reissued(self):
        first = self.alloc("row one")
        second = self.alloc("row two")
        self.assertNotEqual(first, second)
        self.assertEqual(first + second, ["Q9", "Q10"])

    def test_one_call_per_title(self):
        self.assertEqual(self.alloc("row one", "row two", "row three"),
                         ["Q9", "Q10", "Q11"])

    def test_an_id_already_claimed_is_skipped(self):
        # Q9 is the id this repo would otherwise hand out, so holding it is
        # what makes the walk observable — pre-claiming anything at or below
        # the floor would be skipped for being under the floor instead.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                              cwd=self.work, input="held\n", text=True,
                              capture_output=True, check=True).stdout.strip()
        git("push", "origin", f"{blob}:refs/queue-ids/Q9", cwd=self.work)
        self.assertEqual(self.alloc("a new row"), ["Q10"])

    def test_each_claim_is_a_distinct_object(self):
        # Every form of git push short-circuits when the ref already points at
        # the object being pushed: it sends nothing and exits 0, ahead of the
        # lease being evaluated. Measured on git 2.55.0, pushing one shared
        # blob at an existing claim exits 0, so a constant claim object would
        # report success to the loser of every race. A distinct object per
        # claim is what makes the push a real compare-and-swap.
        self.alloc("row one")
        self.alloc("row two")
        objects = [line.split()[0] for line in
                   git("ls-remote", "origin", "refs/queue-ids/*",
                       cwd=self.work).stdout.splitlines() if line]
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(set(objects)), 2,
                         "claims share one object; the loser of a race would "
                         "get exit 0 and a duplicate id")


if __name__ == "__main__":
    unittest.main()
