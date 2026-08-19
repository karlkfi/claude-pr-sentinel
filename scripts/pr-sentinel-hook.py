#!/usr/bin/env python3
"""PostToolUse hook: after a session opens or pushes a pull request, nudge it
to launch the pr-sentinel background watcher instead of foreground-polling CI.

Fires on a `Bash` command that ran `gh pr create` and printed a PR URL, or a
branch `git push` that did not obviously fail. Emits `additionalContext`
describing the exact background-task command to run — unless this session
already has a live watcher on that PR, in which case it says so and names the
`TaskStop` call, because a running watcher re-reads the PR head on every poll
and a second one only doubles the wake-ups. It is ADVISORY — a hook
cannot force the model to call a tool, so this asks; it does not compel. The
(roadmapped) Stop-hook backstop is what makes the launch reliable (see
docs/ROADMAP.md).

The hook is PURELY LOCAL: it inspects the just-run command string and its
output text, reads the session transcript for the watchers this session
launched, and at most asks the local repo whether a pushed ref is a tag or the
default branch. It never makes a network call. It never reads the PR body or
any comment stream — the only PR text it ever touches is a URL it echoes back.

Fail modes: defers silently (emits nothing) on any uncertainty — non-Bash
tool, unparseable command, unrecognised command, cancelled command, disabled
flag. It can never break a session.

Reads the hook JSON on stdin, emits a PostToolUse decision on stdout.
"""
import json
import os
import re
import shlex
import subprocess
import sys

# A watcher already running on this PR makes a second one pure duplication —
# both wake the session for the same event. The live-watcher read is the same
# one the Stop hook uses; the path insert makes the sibling import work whether
# this file is run as a script or loaded by path (as the tests load it).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_sentinel_watchers as watchers   # noqa: E402

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

# `gh` reports an API failure as a status line — `HTTP 503: No server is
# currently available … (https://api.github.com/graphql)` — carrying none of
# the signals above. Matched as a pattern because a literal `http` would hit an
# ordinary URL, and restricted to 4xx/5xx so a logged 200 can't silence us.
HTTP_ERROR_RE = re.compile(r'\bHTTP [45]\d\d\b')


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


def _git_out(args, cwd):
    """Stdout of a local `git` read, stripped, or None when it could not be
    taken (git missing, not a repo, non-zero exit)."""
    try:
        run = subprocess.run(
            ['git'] + args, cwd=cwd or None,
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    return run.stdout.decode('utf-8', 'replace').strip()


def _default_branch(remote, cwd):
    """The remote's default branch name, read from the local
    `refs/remotes/<remote>/HEAD` symref — no network. None when the repo cannot
    answer (no such symref, a URL in place of a remote name, not a repo)."""
    head = _git_out(['symbolic-ref', '--short', 'refs/remotes/' + remote + '/HEAD'],
                    cwd)
    prefix = remote + '/'
    if not head or not head.startswith(prefix):
        return None
    return head[len(prefix):]


def _is_default_branch_ref(ref, default, cwd):
    """True if a push refspec lands on `default`, the remote's default branch.
    That branch never has a pull request of its own, so a push at it is a
    release cut rather than PR work. A None `default` means the repo could not
    answer: treat it as a branch and nudge, as before."""
    if not default:
        return False
    src, sep, dst = ref.lstrip('+').partition(':')
    target = dst if sep else src
    if target == 'HEAD':
        target = _git_out(['symbolic-ref', '--short', '--quiet', 'HEAD'], cwd)
    if not target:
        return False
    return target in (default, 'refs/heads/' + default)


def _is_tag_ref(ref, cwd):
    """True if a push refspec names a tag. `refs/tags/…` settles it outright; a
    bare name is resolved against the local repo."""
    src, _, dst = ref.lstrip('+').partition(':')
    probe = src or dst   # `:refs/tags/v1` deletes a tag; the name is on the right
    if not probe:
        return False
    if probe.startswith('refs/'):
        return probe.startswith('refs/tags/')
    try:
        return subprocess.run(
            ['git', 'rev-parse', '--verify', '--quiet', 'refs/tags/' + probe],
            cwd=cwd or None, capture_output=True, timeout=5, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False   # can't tell: treat it as a branch and nudge, as before


def classify_command(argv, cwd=None):
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
            # A push whose every refspec is a tag or the default branch is a
            # release cut, not PR work — neither ever has a PR of its own.
            # non_flags[1] is the remote, so refspecs start at [2].
            refspecs = non_flags[2:]
            if refspecs:
                default = _default_branch(non_flags[1], cwd)
                if all(_is_tag_ref(r, cwd) or _is_default_branch_ref(r, default, cwd)
                       for r in refspecs):
                    return None
            return 'git_push'
    return None


def detect_action(command, cwd=None):
    """The most relevant action across all simple commands in the string."""
    action = None
    for argv in simple_commands(command):
        kind = classify_command(argv, cwd)
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
    return (any(sig in low for sig in FAILURE_SIGNALS)
            or HTTP_ERROR_RE.search(text) is not None)


def build_live_context(pr_num, task_ids):
    """The nudge for a PR this session is ALREADY watching: don't stack another.

    The running watcher re-reads the PR's head SHA on every poll, so it covers
    the push that just happened — relaunching only doubles the wake-ups for
    every later event. Names the task id so stopping it is one tool call."""
    return (
        f'pr-sentinel: a watcher for #{pr_num} is ALREADY running in this '
        f'session, so this PR is covered — it re-reads the PR head on every '
        f'poll. Do NOT launch a second one: duplicate watchers wake this '
        f'session once each for the same event. '
        + watchers.stop_hint(pr_num, task_ids))


def build_context(action, pr_num, live=None):
    """The advisory nudge injected as additionalContext. `pr_num` is the bare
    PR number (no `#`), or None — only reachable on the push path, since a
    create without a number never gets this far. `live` maps PR number to the
    background task ids of watchers still running in this session."""
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', '')
    watcher = os.path.join(plugin_root, 'scripts', 'pr-sentinel-watch.sh') \
        if plugin_root else 'scripts/pr-sentinel-watch.sh'
    # The watcher accepts a bare number or a github.com PR URL, NOT `#N` — so
    # the Command line interpolates the bare number, never a `#`-prefixed ref.
    target = pr_num if pr_num else '<the PR number for this branch>'
    if action == 'pr_create':
        lead = f'You just opened pull request #{pr_num}.'
    else:
        lead = 'You just pushed to a pull-request branch.'
    if not pr_num:
        # We could not resolve a number, so the session may be on a branch with
        # no PR at all. Let it drop the nudge instead of hunting for one (#34).
        lead += (' If this branch has no open PR, ignore this — a successful '
                 '`gh pr create` nudges again with the number.')
    # With no number resolved we cannot tell whether this push was to a PR the
    # session is already watching, so name those PRs and let it decide. A
    # resolved-and-live number never reaches here — it took build_live_context.
    already = ''
    if not pr_num and live:
        named = ', '.join('#' + p for p in sorted(live, key=int))
        already = (f'This session already has a live watcher on {named} — if '
                   f'this push was to one of those, it is already covered; do '
                   f'not launch a second watcher for it. ')
    return (
        f'pr-sentinel: {lead} Launch the PR Sentinel watcher as a BACKGROUND '
        f'task (run_in_background) so CI failures and merge conflicts wake this '
        f'session — do NOT foreground-poll with `gh pr checks --watch`, '
        f'`gh run watch`, or a sleep loop. Command:\n'
        f'    bash "{watcher}" {target}\n'
        f'{already}When it exits and wakes you, fix the reported CI '
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

    action = detect_action(command, data.get('cwd'))
    if action is None:
        return  # not a PR-opening / branch-push command: defer

    response = data.get('tool_response')
    if isinstance(response, dict) and response.get('interrupted'):
        return  # the user cancelled the command mid-run: defer

    text = output_text(response)
    if looks_failed(text):
        return  # the command appears to have failed: defer

    m = PR_URL_RE.search(text)
    pr_num = m.group(1) if m else None
    if action == 'pr_create' and pr_num is None:
        # A create that opened a PR prints its URL, so require one rather than
        # nudging unless we can prove failure (#57). Covers `--help`, `--web`,
        # `--dry-run`, and any failure shape FAILURE_SIGNALS doesn't carry.
        return

    # Watchers this session started that have not exited. Fail-open: an
    # unreadable transcript yields {}, which is the pre-feature nudge.
    live = watchers.live_watchers(data.get('transcript_path'))
    if pr_num and pr_num in live:
        context = build_live_context(pr_num, live[pr_num])
    else:
        context = build_context(action, pr_num, live)
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
