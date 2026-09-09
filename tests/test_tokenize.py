"""The shared bash tokenizer.

This tokenizer exists as one module because it used to exist as two that
drifted: the newline fix reached the hook's copy three PRs before the guard's,
and nothing detected the gap because each suite exercised its own module. The
agreement corpus is that detector.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, 'scripts'))
import pr_sentinel_tokenize as tokenize   # noqa: E402
import pr_sentinel_guard as guard         # noqa: E402
import pr_sentinel_hook as hook           # noqa: E402


class UnbalancedQuotes(unittest.TestCase):
    """The one place the callers legitimately differ: what a quote shlex cannot
    balance costs. The guard denies, so it defers on the whole string; the hook
    only nudges, so it retries a line at a time."""

    BAD = "gh pr create --title \"it's"

    def test_strict_returns_nothing(self):
        self.assertEqual(tokenize.simple_commands(self.BAD), [])

    def test_lenient_recovers_the_line(self):
        self.assertIn(['gh', 'pr', 'create', '--title', '"it\'s'],
                      tokenize.simple_commands(self.BAD, lenient=True))


class CallersAgree(unittest.TestCase):
    """Q31: `simple_commands` lived in both modules and the newline fix reached
    one three PRs before the other, so the guard's denies and the nudge
    tokenized the same command differently for eighteen hours. Nothing detected
    it — each suite exercised its own module. This is that detector."""

    CORPUS = (
        'git push -u origin claude/foo',
        'gh pr create --fill',
        'git push origin HEAD && gh pr create --fill',
        'echo hi\ngh pr create --title t',
        'git status; git push',
        "cat > body.md <<'EOF'\ngit push\nEOF\ngh pr create --body-file body.md",
        'cat <<-EOF\n\tgh pr checks --watch\n\tEOF',
        'cat <<< word\ngit push',
        'GH_TOKEN=x gh pr create --fill',
        'while true; do gh pr checks; sleep 5; done',
        'gh pr create -t "a\nb"',
        'echo "git push origin main" > note.txt',
    )

    def test_the_two_entry_points_tokenize_alike(self):
        for command in self.CORPUS:
            self.assertEqual(guard.simple_commands(command),
                             hook.simple_commands(command), repr(command))


if __name__ == '__main__':
    unittest.main()
