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

* **A heredoc body is text, not shell.** Once the newline separates, every line
  of a body a command merely *writes* arrives in command position. A session
  writing a test fixture, or a PR body quoting a command in a fenced block,
  then reads as having run it: the guard denied `cat > cases.txt <<'EOF'` for
  the `gh pr checks --watch` inside it, and the nudge announced a push that
  never happened. `strip_heredoc_bodies` consumes each body to its delimiter so
  a classifier only ever sees shell the session actually runs.

Callers differ on one point, so it is a parameter rather than a fork: what to
do with a string shlex will not take. The guard returns nothing and defers
(never deny on a parse it could not make); the nudge retries a line at a time,
because an apostrophe in a heredoc PR body otherwise cost it the `gh pr create`
on the line after.
"""
import shlex

# The operator characters that separate simple commands. shlex groups a run of
# them into a single token, so `;\n` arrives whole and `<<<` never looks like
# the `<<` that opens a heredoc.
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


def _lines(tokens):
    """`tokens` as [(line_tokens, separator_token_or_None)], split on the
    operator tokens that carry a newline."""
    out, cur = [], []
    for token in tokens:
        if _is_operator(token) and '\n' in token:
            out.append((cur, token))
            cur = []
        else:
            cur.append(token)
    out.append((cur, None))
    return out


def strip_heredoc_bodies(tokens):
    """`tokens` with every heredoc body dropped, keeping the separators between
    the lines that remain.

    A `<<` token opens one and the token after it names the delimiter (`<<-EOF`
    lexes the `-` onto that name; `<<<` is a here-string and is a different
    token entirely). Several may open on one line, and bash reads their bodies
    in order. A delimiter that never reappears takes the rest of the string
    with it, which leaves the caller deferring — the safe direction for a hook
    that either denies or asserts a push happened.
    """
    out, pending, active = [], [], None
    for line, separator in _lines(tokens):
        if active is not None:
            if line == [active]:
                active = pending.pop(0) if pending else None
            continue
        for i, token in enumerate(line):
            if token == '<<' and i + 1 < len(line):
                delimiter = line[i + 1].lstrip('-')
                if delimiter:
                    pending.append(delimiter)
        out.extend(line)
        if pending:
            active = pending.pop(0)
        if separator is not None:
            out.append(separator)
    return out


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
    for token in strip_heredoc_bodies(tokens):
        if _is_operator(token):
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(token)
    if cur:
        groups.append(cur)
    return groups
