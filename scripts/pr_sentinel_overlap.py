#!/usr/bin/env python3
"""Whether this branch edits lines an already-open pull request also changes.

Used by the PreToolUse guard to deny a `gh pr create` that would open a second
PR over the same work. Review is an expensive place to discover that two
branches rewrote the same function.

This is the ONE part of the plugin's hooks that talks to GitHub: `gh pr list`
for the open PRs and their paths, then `gh pr diff` for the few that share a
path with this branch. It reads code, never prose — no PR body, no comments, no
review text, and not even the PR title, which is human-writable and would be
echoed straight into the session's context by the deny.

Everything here shells out, so everything here FAILS SILENT: no `gh`, no
credentials, a detached HEAD, a base ref that doesn't resolve, a rate-limited
token, a slow call — every one returns "no opinion" and the create proceeds.
A missed catch is the acceptable failure; a create blocked by a probe that
could not run is not.

**Line ranges, not paths.** Two branches touching one file is ordinary; two
branches touching one function is the finding. Both sides are read from the
diff's PRE-IMAGE side, so two diffs taken from a shared ancestor are numbered
in that ancestor and their ranges are comparable. Each range is widened by the
three lines of context a hunk carries, so edits within six lines meet and edits
seven apart do not. The false-positive direction carries the weight: an overlap
reported where there is none sends a session to fold a branch that was fine.
"""
import fnmatch
import json
import os
import re
import subprocess

PROBE_TIMEOUT = 5        # seconds, per subprocess
CONTEXT_LINES = 3        # what a diff hunk carries either side
MAX_OPEN_PRS = 20        # PRs listed; a repo with more is not our problem
MAX_PR_DIFFS = 3         # diffs fetched — this runs while someone waits

FALLBACK_BASE_REF = 'origin/main'

HUNK_RE = re.compile(r'^@@ -([0-9]+)(?:,([0-9]+))? \+')
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def enabled():
    """Whether the overlap check is active. On by default; off when
    `PR_SENTINEL_OVERLAP_ENABLED` is `0`/`false`/empty, or the whole plugin is
    disabled via `PR_SENTINEL_DISABLE=1`."""
    if os.environ.get('PR_SENTINEL_DISABLE') == '1':
        return False
    val = os.environ.get('PR_SENTINEL_OVERLAP_ENABLED')
    if val is None:
        return True
    return val.strip().lower() not in ('', '0', 'false')


def ignore_patterns():
    """fnmatch globs from `PR_SENTINEL_OVERLAP_IGNORE`, colon-separated.

    For the file every branch edits by construction — a changelog, a backlog
    table — where a shared range is the normal case rather than a finding."""
    raw = os.environ.get('PR_SENTINEL_OVERLAP_IGNORE') or ''
    return [p for p in (part.strip() for part in raw.split(':')) if p]


def capture(argv, cwd, timeout=PROBE_TIMEOUT):
    """(exit status, stdout) for a subprocess, or (None, '') if it never ran."""
    try:
        proc = subprocess.run(argv, cwd=cwd or None, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ''
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def git(root, *args):
    """stdout of a successful `git` invocation, or None."""
    status, out = capture(('git', '-C', root) + args, root)
    return out if status == 0 else None


def repo_root(cwd):
    """The working tree the session is in — from the payload's `cwd`, never
    this file's location, so a worktree session reads its own branch."""
    out = git(cwd or '.', 'rev-parse', '--show-toplevel')
    return out.strip() if out and out.strip() else None


def base_ref(root):
    """The ref this branch would be merged into: `PR_SENTINEL_BASE_REF`, else
    the remote's own default branch, else `origin/main`."""
    configured = (os.environ.get('PR_SENTINEL_BASE_REF') or '').strip()
    if configured:
        return configured
    out = git(root, 'symbolic-ref', '--short', '--quiet',
              'refs/remotes/origin/HEAD')
    return out.strip() if out and out.strip() else FALLBACK_BASE_REF


def hunk_range(start, count, widen):
    """A hunk's pre-image span, reaching as far as an edit there could collide.

    `widen` adds the context a hunk would carry, and is for a `-U0` diff only.
    A diff taken at the default context already counts those lines inside
    `count`, so widening it again would double the reach — an overlap claimed
    between edits nine lines apart, in a check whose whole point is that seven
    apart is not one.

    `-a,0` is an insertion after line `a` and covers no pre-image line of its
    own; it still collides with an edit beside it, so it spans that one.
    """
    last = start + count - 1 if count else start
    pad = CONTEXT_LINES if widen else 0
    return max(1, start - pad), last + pad


def parse_hunks(diff, widen=True):
    """{path: [(start, end)]} from a unified diff, in pre-image line numbers.

    A `--- ` line only names a file inside a header run, because a removed line
    carries a `-` of its own: an SQL comment `-- DROP` comes out of the diff as
    `--- DROP` and is content, not a header. `@@` needs no such guard — a
    removed hunk header is prefixed too, and a context line starts with a space.

    Colour is stripped first. `gh` decides that by whether stdout is a terminal
    and a config file can override it either way; an escape byte ahead of `@@`
    would take every hunk out of the count and read as a PR that changes
    nothing.
    """
    ranges, path, in_header = {}, '', False
    for line in ANSI_RE.sub('', diff).splitlines():
        if line.startswith('diff --git '):
            path, in_header = '', True
            continue
        if in_header and line.startswith('--- '):
            path = '' if line == '--- /dev/null' else line[6:]
            continue
        if not path:
            continue
        m = HUNK_RE.match(line)
        if m:
            in_header = False
            ranges.setdefault(path, []).append(
                hunk_range(int(m.group(1)),
                           1 if m.group(2) is None else int(m.group(2)),
                           widen))
    return ranges


def changed_ranges(root, old, new):
    """What changed between two revisions, or None.

    `-U0` so a hunk covers only the lines that moved; the context comes back in
    `hunk_range`, which is where its width is stated once. The prefixes are
    pinned rather than inherited, because `diff.mnemonicPrefix` renames them and
    the path would then be read out of the wrong column.
    """
    out = git(root, 'diff', '-U0', '--no-color', '--no-ext-diff',
              '--src-prefix=a/', '--dst-prefix=b/', old, new)
    return None if out is None else parse_hunks(out)


def ranges_meet(mine, theirs):
    return any(a[0] <= b[1] and b[0] <= a[1] for a in mine for b in theirs)


def is_ignored(path, patterns):
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def current_branch(root):
    out = git(root, 'symbolic-ref', '--short', '--quiet', 'HEAD')
    return out.strip() if out else ''


def open_prs(root):
    """Open PRs with the paths each one touches, or None.

    The field list is deliberately narrow and must stay that way: `number` and
    `headRefName` identify the PR, `files` is what the comparison needs. A
    human-writable field here (`title`, `body`) would be echoed into the
    session by the deny — the channel this plugin exists to keep shut.
    """
    status, out = capture(
        ('gh', 'pr', 'list', '--state', 'open', '--limit', str(MAX_OPEN_PRS),
         '--json', 'number,headRefName,files'), root)
    if status != 0:
        return None
    try:
        prs = json.loads(out)
    except ValueError:
        return None
    return prs if isinstance(prs, list) else None


def pr_ranges(root, number):
    """What an open PR changes, or None.

    Numbered from that PR's own merge base rather than this branch's, so a
    long-lived PR's ranges drift. Close enough to tell an edit in the same
    function from one at the other end of the file, which is the question.

    `gh pr diff` has no `-U`, so this arrives at the default context and is not
    widened again — see `hunk_range`.
    """
    status, out = capture(('gh', 'pr', 'diff', str(number)), root)
    return None if status != 0 else parse_hunks(out, widen=False)


def overlapping_prs(cwd):
    """[(number, paths, precise)] for open PRs on this branch's own lines, or [].

    `precise` is False when the PR's diff was not fetched — the cap was reached,
    or the call failed — and the entry rests on a shared path alone. The deny
    says so rather than passing it off as a range match.
    """
    root = repo_root(cwd)
    if root is None:
        return []
    branch = current_branch(root)
    if not branch:
        return []                        # detached HEAD: nothing to compare
    fork = git(root, 'merge-base', 'HEAD', base_ref(root))
    if fork is None or not fork.strip():
        return []
    mine = changed_ranges(root, fork.strip(), 'HEAD')
    if not mine:
        return []                        # nothing committed yet, or git said no
    prs = open_prs(root)
    if prs is None:
        return []
    patterns = ignore_patterns()
    hits, fetched = [], 0
    for pr in prs:
        if pr.get('headRefName') == branch:
            continue                     # this branch's own PR, already open
        shared = sorted(p for p in (f.get('path')
                                    for f in pr.get('files') or [])
                        if p in mine and not is_ignored(p, patterns))
        if not shared:
            continue
        theirs = None
        if fetched < MAX_PR_DIFFS:
            fetched += 1                 # a failed fetch spends it too
            theirs = pr_ranges(root, pr.get('number'))
        if theirs is not None:
            shared = [p for p in shared
                      if p in theirs and ranges_meet(mine[p], theirs[p])]
            if not shared:
                continue
        hits.append((pr.get('number'), shared, theirs is not None))
    return hits


def build_reason(prs):
    """The deny reason. Names each PR by number and the paths it shares — never
    its title, which its author writes."""
    parts = ['#%s (%s%s)' % (number, ', '.join(paths),
                             '' if precise else
                             '; shared path only, diff not fetched')
             for number, paths, precise in prs]
    first = prs[0][0]
    return (
        f'pr-sentinel: refusing `gh pr create` — this branch edits lines an '
        f'open PR already changes: {"; ".join(parts)}. That is duplicated or '
        f'mutually invalidating work, and review is an expensive place to find '
        f'it. Read that diff first (`gh pr diff {first}`), then either fold '
        f'this branch into that one or narrow it to what does not overlap.\n'
        f'If the overlap is known and deliberate, re-run with an inline '
        f'PR_SENTINEL_OVERRIDE=<reason> prefix on the command itself '
        f'(PR_SENTINEL_OVERRIDE="why this once" gh pr create …).\n'
        f'Set PR_SENTINEL_OVERLAP_ENABLED=false to turn this check off, or '
        f'name the paths every branch edits in PR_SENTINEL_OVERLAP_IGNORE.'
    )
