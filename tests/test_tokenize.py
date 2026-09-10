"""The shared bash tokenizer: heredoc bodies, and the two callers' agreement.

`strip_heredoc_bodies` is the half with teeth. Before it, every line of a body
a command merely *writes* arrived in command position, so writing a fixture or
a PR body that quoted a command read as having run it — the guard denied a
`cat >` for the poll command inside its heredoc, and the nudge announced a push
that never happened.

The agreement corpus is the other half. This tokenizer exists as one module
because it used to exist as two that drifted; the corpus fails if they drift
again.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, 'scripts'))
import pr_sentinel_tokenize as tokenize   # noqa: E402
import pr_sentinel_guard as guard         # noqa: E402
import pr_sentinel_hook as hook           # noqa: E402


def _fixture_write(body):
    """The shape that started this: a session writing a case file whose lines
    are command strings."""
    return "cat > tests/fixtures/cases.txt <<'EOF'\n" + body + "\nEOF\ngit status"


class HeredocBodies(unittest.TestCase):
    def test_a_body_line_is_not_a_command(self):
        self.assertEqual(
            tokenize.simple_commands("cat > f <<'EOF'\ngit push\nEOF"),
            [['cat'], ['f'], ['EOF']])

    def test_the_command_after_a_heredoc_is_still_seen(self):
        """The body is dropped; the shell after it is not."""
        self.assertIn(
            ['gh', 'pr', 'create', '--body-file', 'body.md'],
            tokenize.simple_commands(
                "cat > body.md <<'EOF'\nmulti\nline body\nEOF\n"
                'gh pr create --body-file body.md'))

    def test_dash_delimiter_and_indented_terminator(self):
        self.assertNotIn(
            ['git', 'push'],
            tokenize.simple_commands('cat <<-EOF\n\tgit push\n\tEOF\ngit status'))

    def test_several_heredocs_on_one_line_consume_bodies_in_order(self):
        groups = tokenize.simple_commands(
            'cat <<A <<B\ngit push\nA\ngh pr create\nB\ngit status')
        self.assertNotIn(['git', 'push'], groups)
        self.assertNotIn(['gh', 'pr', 'create'], groups)
        self.assertIn(['git', 'status'], groups)

    def test_a_here_string_opens_no_body(self):
        """`<<<` is a here-string, a different token — the line after it is
        ordinary shell and must still classify."""
        self.assertIn(['git', 'push'],
                      tokenize.simple_commands('cat <<< word\ngit push'))

    def test_an_unterminated_delimiter_takes_the_rest(self):
        """Nothing after it can be attributed, so the caller defers."""
        self.assertNotIn(['git', 'push'],
                         tokenize.simple_commands("cat <<'EOF'\ngit push\n"))

    def test_a_terminator_needs_the_line_to_itself(self):
        self.assertNotIn(
            ['git', 'push'],
            tokenize.simple_commands("cat <<'EOF'\nEOF is not the end\ngit push\nEOF"))


class CallerBehaviour(unittest.TestCase):
    """The two classifications the body lines used to reach."""

    def test_the_guard_does_not_deny_a_fixture_of_poll_commands(self):
        self.assertIsNone(guard.classify_poll(
            _fixture_write('gh pr checks --watch\ngh run watch 123')))

    def test_the_guard_does_not_deny_a_fixture_naming_pr_create(self):
        self.assertFalse(guard.is_pr_create(_fixture_write('gh pr create --fill')))

    def test_the_hook_does_not_nudge_for_a_written_push(self):
        self.assertIsNone(hook.detect_action(_fixture_write('git push')))

    def test_a_real_command_still_classifies(self):
        """Controls: the probes above can report either way."""
        self.assertEqual(guard.classify_poll('gh pr checks --watch'),
                         'gh_pr_checks_watch')
        self.assertTrue(guard.is_pr_create('gh pr create --fill'))
        self.assertEqual(hook.detect_action('git push -u origin claude/foo'),
                         'git_push')


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
