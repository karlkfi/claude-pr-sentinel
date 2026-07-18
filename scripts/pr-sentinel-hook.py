#!/usr/bin/env python3
"""PostToolUse hook: after a session opens or pushes a pull request, nudge it
to launch the pr-sentinel background watcher instead of foreground-polling CI.

Fires on a `Bash` command that ran `gh pr create` or a branch `git push` and
did not obviously fail. Emits `additionalContext` describing the exact
background-task command to run. It is ADVISORY — a hook cannot force the model
to call a tool, so this asks; it does not compel. The (roadmapped) Stop-hook
backstop is what makes the launch reliable (see docs/ROADMAP.md).

The hook is PURELY LOCAL: it inspects the just-run command string and its
output text and never makes a network call. It never reads the PR body or any
comment stream — the only PR text it ever touches is a URL it echoes back.

Fail modes: defers silently (emits nothing) on any uncertainty — non-Bash
tool, unparseable command, unrecognised command, disabled flag. It can never
break a session.

Reads the hook JSON on stdin, emits a PostToolUse decision on stdout.
"""
import json
import os
import re
import shlex
import sys

# A github.com PR URL, e.g. https://github.com/owner/repo/pull/123
PR_URL_RE = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)')

# Signals in command output that the git/gh command failed. Conservative: if
# any appears we defer rather than nudge on a push that didn't land.
FAILURE_SIGNALS = (
    'fatal:',
    'error:',
    '! [rejected]',
    'failed to push',
    'everything up-to-date',   # nothing was pushed; no new PR work
    'gh: ',                    # gh error prefix
    'could not',
)


def simple_commands(command):
    """Split a bash command string into simple commands on the shell operators
    that separate them (`&&`, `||`, `|`, `;`, newlines). Best-effort: on a
    tokenizing failure we return a single-element list so the caller still gets
    a chance to match, but never crashes."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=';()<>|&\n')
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    groups, cur = [], []
    for tok in tokens:
        if tok and all(c in ';()<>|&\n' for c in tok):
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        groups.append(cur)
    return groups


def _strip_env_prefix(argv):
    """Drop leading NAME=VALUE assignments so `GH_TOKEN=x gh pr create` still
    resolves to `gh`."""
    i = 0
    while i < len(argv) and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', argv[i]):
        i += 1
    return argv[i:]


def classify_command(argv):
    """Return 'pr_create', 'git_push', or None for one simple command's argv."""
    argv = _strip_env_prefix(argv)
    if not argv:
        return None
    head = os.path.basename(argv[0])
    rest = argv[1:]
    if head == 'gh' and 'pr' in rest:
        # gh pr create ... (flags may sit between; check the two subcommands)
        non_flags = [a for a in rest if not a.startswith('-')]
        if non_flags[:2] == ['pr', 'create']:
            return 'pr_create'
        return None
    if head == 'git':
        non_flags = [a for a in rest if not a.startswith('-')]
        if non_flags[:1] == ['push']:
            # Skip tag/branch deletions — not PR-babysitting shapes.
            if '--delete' in rest or '-d' in rest or '--tags' in rest:
                return None
            return 'git_push'
    return None


def detect_action(command):
    """The most relevant action across all simple commands in the string."""
    action = None
    for argv in simple_commands(command):
        kind = classify_command(argv)
        if kind == 'pr_create':
            return 'pr_create'   # strongest signal, short-circuit
        if kind == 'git_push':
            action = 'git_push'
    return action


def output_text(response):
    """Best-effort combined stdout/stderr text from the tool response, which
    may be a dict, a string, or absent."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = []
        for key in ('stdout', 'stderr', 'output', 'content'):
            val = response.get(key)
            if isinstance(val, str):
                parts.append(val)
        return '\n'.join(parts)
    return ''


def looks_failed(text):
    low = text.lower()
    return any(sig in low for sig in FAILURE_SIGNALS)


def build_context(action, pr_num):
    """The advisory nudge injected as additionalContext. `pr_num` is the bare
    PR number (no `#`) or None."""
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', '')
    watcher = os.path.join(plugin_root, 'scripts', 'pr-sentinel-watch.sh') \
        if plugin_root else 'scripts/pr-sentinel-watch.sh'
    # The watcher accepts a bare number or a github.com PR URL, NOT `#N` — so
    # the Command line interpolates the bare number, never a `#`-prefixed ref.
    target = pr_num if pr_num else '<the PR number for this branch>'
    pr_ref = f'#{pr_num}' if pr_num else None
    if action == 'pr_create':
        lead = f'You just opened pull request {pr_ref or "(number in the output above)"}.'
    else:
        lead = 'You just pushed to a pull-request branch.'
    return (
        f'pr-sentinel: {lead} Launch the PR Sentinel watcher as a BACKGROUND '
        f'task (run_in_background) so CI failures and merge conflicts wake this '
        f'session — do NOT foreground-poll with `gh pr checks --watch`, '
        f'`gh run watch`, or a sleep loop. Command:\n'
        f'    bash "{watcher}" {target}\n'
        f'If a watcher for this PR is already running, restart it so it tracks '
        f'the latest push. When it exits and wakes you, fix the reported CI '
        f'failure or merge conflict, push, and relaunch it. Never auto-merge.'
    )


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: defer
    if data.get('tool_name') != 'Bash':
        return
    if os.environ.get('PR_SENTINEL_DISABLE') == '1':
        return
    command = (data.get('tool_input') or {}).get('command') or ''
    if not command.strip():
        return

    action = detect_action(command)
    if action is None:
        return  # not a PR-opening / branch-push command: defer

    text = output_text(data.get('tool_response'))
    if looks_failed(text):
        return  # the command appears to have failed: defer

    m = PR_URL_RE.search(text)
    pr_num = m.group(1) if m else None

    context = build_context(action, pr_num)
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': context}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PR_SENTINEL_DEBUG') == '1':
            raise
        sys.exit(0)
