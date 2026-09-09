#!/usr/bin/env python3
"""The shared bash tokenizer behind every command classification this plugin
makes — the PostToolUse nudge's `classify_command` and the PreToolUse guard's
`classify_poll` / `is_pr_create`.

It exists as one module because it used to exist as two. The same function
lived in `pr_sentinel_hook.py` and `pr_sentinel_guard.py` with the same job,
and the newline defect was fixed in one copy three PRs and eighteen hours
before the other, so for that window the guard's denies tokenized commands
differently from the nudge that pointed at them. Nothing detected the split:
each suite exercised its own module.

Two shell facts do the work here:

* **A newline separates simple commands.** Listing `\\n` in `punctuation_chars`
  is not enough on its own — shlex's default `whitespace` holds it too and eats
  it before the punctuation rule is consulted, gluing two commands into one so
  the second one's leading word never reaches a classifier.

Callers differ on one point, so it is a parameter rather than a fork: what to
do with a string shlex will not take. The guard returns nothing and defers
(never deny on a parse it could not make); the nudge retries a line at a time,
because an apostrophe in a heredoc PR body otherwise cost it the `gh pr create`
on the line after.
"""
import shlex

# The operator characters that separate simple commands. shlex groups a run of
# them into a single token, so `;\n` arrives whole.
OPERATOR_CHARS = ';()<>|&\n'


def lex(text):
    """shlex tokens for a command string, with newlines separating rather than
    vanishing. Raises ValueError on an unbalanced quote."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=OPERATOR_CHARS)
    lexer.whitespace_split = True
    lexer.whitespace = lexer.whitespace.replace('\n', '')
    return list(lexer)


def lex_by_line(command):
    """Tokens for a command shlex will not take whole. Retry a line at a time
    and fall back to a plain split for the one line carrying the bad quote —
    rarely the line running `gh`."""
    tokens = []
    for line in command.splitlines():
        try:
            tokens.extend(lex(line))
        except ValueError:
            tokens.extend(line.split())
        tokens.append('\n')
    return tokens


def _is_operator(token):
    return bool(token) and all(c in OPERATOR_CHARS for c in token)


def simple_commands(command, lenient=False):
    """Split a bash command string into simple commands — a list of argv lists
    — on the operators that separate them (`&&`, `||`, `|`, `;`, `(`, `)`,
    redirects, newlines), with heredoc bodies removed.

    `lenient` picks what an unbalanced quote costs: False returns `[]` so the
    caller defers on the whole string, True retries line by line.
    """
    try:
        tokens = lex(command)
    except ValueError:
        if not lenient:
            return []
        tokens = lex_by_line(command)
    groups, cur = [], []
    for token in tokens:
        if _is_operator(token):
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(token)
    if cur:
        groups.append(cur)
    return groups
