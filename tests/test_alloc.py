#!/usr/bin/env python3
"""Tests for scripts/alloc-queue-id.sh (claim-based Q-ID allocation).

Run with: python3 -m unittest discover tests

Builds a throwaway bare remote and a work repo in a temp dir, then drives the
script as a subprocess. Asserts that IDs are handed out above the floor the
store implies, that a claim lands in refs/queue-ids on the remote, that an ID
someone else already holds is skipped rather than reissued, and that each claim
is a distinct object. No real remote is touched.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "alloc-queue-id.sh"

ITEM = """---
id: Q7
rank: a0
labels:
    - docs
status: ready
size: S
---

# An item that already exists

Its body.
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
        store = self.work / "docs" / "queue"
        store.mkdir(parents=True)
        (store / "Q7.md").write_text(ITEM, encoding="utf-8")
        git("add", "-A", cwd=self.work)
        git("commit", "-m", "seed", cwd=self.work)
        git("remote", "add", "origin", str(self.remote), cwd=self.work)
        git("push", "-u", "origin", "main", cwd=self.work)
        self.addCleanup(self._tmp.cleanup)

    def alloc(self, *titles):
        proc = subprocess.run(
            ["bash", str(SCRIPT), *titles],   # --store defaults to docs/queue
            cwd=self.work, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def claims_on_remote(self):
        out = git("ls-remote", "origin", "refs/queue-ids/*", cwd=self.work).stdout
        return sorted(line.rsplit("/", 1)[-1] for line in out.splitlines() if line)

    def test_first_id_clears_the_highest_in_the_store(self):
        # The store holds Q7, so Q8 is the first free id. The floor also reads
        # git history, which the live store cannot supply on its own: a
        # delete-on-done store holds no trace of an item that has shipped.
        self.assertEqual(self.alloc("a new item"), ["Q8"])
        self.assertEqual(self.claims_on_remote(), ["Q8"])

    def test_ids_are_never_reissued(self):
        first = self.alloc("row one")
        second = self.alloc("row two")
        self.assertNotEqual(first, second)
        self.assertEqual(first + second, ["Q8", "Q9"])

    def test_one_call_per_title(self):
        self.assertEqual(self.alloc("row one", "row two", "row three"),
                         ["Q8", "Q9", "Q10"])

    def test_an_id_already_claimed_is_skipped(self):
        # Q8 is the id this store would otherwise hand out, so holding it is
        # what makes the walk observable — pre-claiming anything at or below
        # the floor would be skipped for being under the floor instead.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                              cwd=self.work, input="held\n", text=True,
                              capture_output=True, check=True).stdout.strip()
        git("push", "origin", f"{blob}:refs/queue-ids/Q8", cwd=self.work)
        self.assertEqual(self.alloc("a new item"), ["Q9"])

    def test_each_claim_is_a_distinct_object(self):
        # Every form of git push short-circuits when the ref already points at
        # the object being pushed: it sends nothing and exits 0, ahead of the
        # lease being evaluated. Measured on git 2.55.0, pushing one shared
        # blob at an existing claim exits 0, so a constant claim object would
        # report success to the loser of every race. A distinct object per
        # claim is what makes the push a real compare-and-swap.
        self.alloc("item one")
        self.alloc("item two")
        objects = [line.split()[0] for line in
                   git("ls-remote", "origin", "refs/queue-ids/*",
                       cwd=self.work).stdout.splitlines() if line]
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(set(objects)), 2,
                         "claims share one object; the loser of a race would "
                         "get exit 0 and a duplicate id")


if __name__ == "__main__":
    unittest.main()
