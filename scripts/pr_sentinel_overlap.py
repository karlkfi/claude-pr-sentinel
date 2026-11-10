#!/usr/bin/env python3
"""Whether this branch collides with an already-open pull request.

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
could not run is not. That covers a `gh pr diff` that failed or was never
spent: an entry resting on a shared path alone is listed in a deny some other
entry justifies, and never denies by itself (`deciding`).

**Line ranges, not paths.** Two branches touching one file is ordinary; two
branches touching one function is the finding. Both sides are read from the
diff's PRE-IMAGE side, so two diffs taken from a shared ancestor are numbered
in that ancestor and their ranges are comparable. Each range is widened by the
three lines of context a hunk carries, so edits within six lines meet and edits
seven apart do not. The false-positive direction carries the weight: an overlap
reported where there is none sends a session to fold a branch that was fine.

**A trial merge decides what the ranges suspect.** Six lines is a proximity
guess, and two branches adding separate blocks beside each other merge cleanly.
So a suspect whose head commit is here is merged with `git merge-tree`, and the
deny stands only on a path where that merge conflicts and the fork point's does
not, or where both sides made one identical edit. Git merges an identical edit
clean, and it is the duplicate this check exists for. Only a path the ranges
already suspected can stand, so a renumbered citation stays discounted and the
merge can only ever narrow a deny. Nothing is fetched: a head known only to
GitHub, or a git older than 2.38, leaves the range result, and the deny says so
and names the fetch that lets a re-run decide it.

**A stacked branch is numbered from its own base.** A create that declares
`--base <another open PR's head>` is numbered from that head rather than from
the default branch, so the parent's hunks are not counted as this branch's own
— and the parent is skipped outright, its lines being inherited rather than
duplicated. Once that parent is rebased and this branch is not restacked onto
it, the two sides are numbered in different pre-images and nothing comparable
is left, so the check declines rather than guess.

A create that declares no such base is still recognised as a stack when an open
PR's head commit is already in this branch: it was built on that PR, so that
PR's hunks are inherited too. `--base main` with a "merge after" note is the
ordinary way to open a stack, and refusing it spent a human override per child.
A branch that replays the parent's work as new commits carries no such head and
is still compared — that is the duplicate this check exists for.

**The branch judged is the one the create opens.** A create that names `--head`,
or follows a `git checkout`/`git switch` in the same command, is judged on that
branch rather than on whatever HEAD is when the hook runs — the hook runs before
any of the command does, so a chained checkout has not happened yet. A head that
resolves to no commit here leaves nothing to compare, and the check declines.
A create that names `--repo` is compared against that repo's open PRs.
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
GH_FILES_CAP = 100       # files `gh pr list --json files` returns per PR

FALLBACK_BASE_REF = 'origin/main'

TIMED_OUT = 'timed out'  # a `capture()` status: ran, and blew its budget

# What a hit rests on, weakest last. `build_reason` words the deny from it.
MERGED = 'merged'        # a trial merge conflicts, or both made one edit
RANGED = 'ranged'        # edits within six lines, and no trial merge ran
PATHED = 'pathed'        # a shared path alone: no diff, no trial merge

HUNK_RE = re.compile(r'^@@ -([0-9]+)(?:,([0-9]+))? \+')
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
CITATION_RE = re.compile(r'([\w./-]+\.\w+):[0-9]+(?:-[0-9]+)?')


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
    """fnmatch globs from `PR_SENTINEL_OVERLAP_IGNORE`, comma-separated.

    For the file every branch edits by construction — a changelog, a backlog
    table — where a shared range is the normal case rather than a finding.

    Comma because branch-guard's `BRANCH_GUARD_OVERLAP_IGNORE` spells the same
    list that way, and a repo that has worked one out copies it across. On a
    colon here, `a.md,b.md` pasted in would parse as one glob matching nothing,
    and nothing would report that the list had stopped discounting."""
    raw = os.environ.get('PR_SENTINEL_OVERLAP_IGNORE') or ''
    return [p for p in (part.strip() for part in raw.split(',')) if p]


def timeout_scale():
    """Multiplier on the subprocess budget, read from
    `PR_SENTINEL_PROBE_TIMEOUT_SCALE`.

    Floored at 1, so the knob can only ever buy time. A probe that answers is
    worth more than one that fails open, and a floor means no value set here
    can widen the fail-open beyond what the shipped default already allows.
    Anything but a whole number above 1 falls back to 1.
    """
    try:
        scale = int(os.environ.get('PR_SENTINEL_PROBE_TIMEOUT_SCALE', ''))
    except ValueError:
        return 1
    return scale if scale > 1 else 1


def capture(argv, cwd, timeout=None):
    """(exit status, stdout) for a subprocess.

    Two ways to get no status: `None` for a probe that never ran, `TIMED_OUT`
    for one that ran and blew `timeout`. Neither is 0, so both still fail open
    — the distinction is for a caller that wants to tell "the check stopped
    evaluating" from "the check found nothing", which are the same silence to
    every caller below and to the session reading the transcript.

    `timeout` defaults to `PROBE_TIMEOUT` scaled, resolved per call rather
    than as a parameter default: a default binds at definition, so it would
    read the environment once at import and never again. An explicit argument
    is absolute and is not scaled.

    `TimeoutExpired` is caught first because it subclasses `SubprocessError`.
    """
    if timeout is None:
        timeout = PROBE_TIMEOUT * timeout_scale()
    try:
        proc = subprocess.run(argv, cwd=cwd or None, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMED_OUT, ''
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ''
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def git(root, *args):
    """stdout of a successful `git` invocation, or None."""
    status, out = capture(('git', '-C', root) + args, root)
    return out if status == 0 else None


def resolves(root, rev):
    """Whether `rev` names a commit here."""
    out = git(root, 'rev-parse', '--verify', '--quiet', rev + '^{commit}')
    return bool(out and out.strip())


def contains(root, rev, tip='HEAD'):
    """Whether `tip` already carries `rev` — this branch is built on top of it."""
    status, _ = capture(('git', '-C', root, 'merge-base', '--is-ancestor',
                         rev, tip), root)
    return status == 0


def repo_root(cwd):
    """The working tree the session is in — from the payload's `cwd`, never
    this file's location, so a worktree session reads its own branch."""
    out = git(cwd or '.', 'rev-parse', '--show-toplevel')
    return out.strip() if out and out.strip() else None


def base_ref(root, declared=''):
    """The ref this branch would be merged into: the `--base` the create itself
    declares, else `PR_SENTINEL_BASE_REF`, else the remote's own default branch,
    else `origin/main`.

    The declared base comes first because it is the only one of the four that
    can know about a stack — `origin/HEAD` names the default branch by
    definition, and `PR_SENTINEL_BASE_REF` is one value for every branch in the
    repo.

    Resolved against the remote first: `--base` names a branch in the GitHub
    repo, and a local branch of that name is routinely stale.
    """
    declared = (declared or '').strip()
    if declared:
        remote = 'origin/' + declared
        return remote if resolves(root, remote) else declared
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


def is_repoint(removed, added):
    """Whether a hunk only renumbers `file.ext:NNN` citations.

    A code change that shifts lines sends every branch to refresh each doc that
    cites the moved lines, so two unrelated PRs rewrite the same citation to
    different numbers. That is a git conflict, but a mechanical one — whoever
    lands second regenerates — and not the duplicated work this check is for.
    The path and the quoted text must survive unchanged; only the number moves.
    """
    def norm(lines):
        return [CITATION_RE.sub(r'\1:N', line) for line in lines]
    return bool(removed) and removed != added and norm(removed) == norm(added)


def parse_edits(diff, widen=True):
    """[(path, (start, end), removed, added)] from a unified diff, in pre-image
    line numbers. A hunk that only renumbers citations is left out — see
    `is_repoint`.

    A `--- ` line only names a file inside a header run, because a removed line
    carries a `-` of its own: an SQL comment `-- DROP` comes out of the diff as
    `--- DROP` and is content, not a header. `@@` needs no such guard — a
    removed hunk header is prefixed too, and a context line starts with a space.

    Colour is stripped first. `gh` decides that by whether stdout is a terminal
    and a config file can override it either way; an escape byte ahead of `@@`
    would take every hunk out of the count and read as a PR that changes
    nothing.
    """
    edits, path, in_header, hunk = [], '', False, None

    def flush():
        if hunk and not is_repoint(hunk[2], hunk[3]):
            edits.append(hunk)

    for line in ANSI_RE.sub('', diff).splitlines():
        if line.startswith('diff --git '):
            flush()
            path, in_header, hunk = '', True, None
            continue
        if in_header and line.startswith('--- '):
            path = '' if line == '--- /dev/null' else line[6:]
            continue
        if not path:
            continue
        m = HUNK_RE.match(line)
        if m:
            flush()
            in_header = False
            hunk = (path, hunk_range(int(m.group(1)),
                                     1 if m.group(2) is None
                                     else int(m.group(2)),
                                     widen), [], [])
        elif hunk and not in_header and line[:1] in '-+':
            hunk[2 if line[0] == '-' else 3].append(line[1:])
    flush()
    return edits


def by_path(edits):
    """{path: [(start, end)]} from `parse_edits`."""
    ranges = {}
    for path, span, _removed, _added in edits:
        ranges.setdefault(path, []).append(span)
    return ranges


def parse_hunks(diff, widen=True):
    """{path: [(start, end)]} from a unified diff — see `parse_edits`."""
    return by_path(parse_edits(diff, widen))


def changed_edits(root, old, new):
    """The edits between two revisions, or None.

    `-U0` so a hunk covers only the lines that moved; the context comes back in
    `hunk_range`, which is where its width is stated once. The prefixes are
    pinned rather than inherited, because `diff.mnemonicPrefix` renames them and
    the path would then be read out of the wrong column.
    """
    out = git(root, 'diff', '-U0', '--no-color', '--no-ext-diff',
              '--src-prefix=a/', '--dst-prefix=b/', old, new)
    return None if out is None else parse_edits(out)


def changed_ranges(root, old, new):
    """What changed between two revisions, or None."""
    edits = changed_edits(root, old, new)
    return None if edits is None else by_path(edits)


def ranges_meet(mine, theirs):
    return any(a[0] <= b[1] and b[0] <= a[1] for a in mine for b in theirs)


def merge_conflicts(root, ours, theirs):
    """The paths a trial merge conflicts on, or None if it could not run.

    Exit 1 means conflicts, and also a revision that does not resolve, so only
    the tree id leading the output tells them apart. The merge writes the
    resulting objects into the repository's own store, unreferenced until `git
    gc` takes them; it moves no ref and touches no working tree.
    """
    status, out = capture(('git', '-C', root, 'merge-tree', '--write-tree',
                           '--name-only', '--no-messages', '-z', ours, theirs),
                          root)
    fields = out.split('\0')
    if status not in (0, 1) or not fields[0]:
        return None
    return {f for f in fields[1:] if f}


def same_edits(root, fork, tip, head):
    """The paths where this branch and `head` made one identical edit."""
    mine = changed_edits(root, fork, tip)
    theirs = changed_edits(root, fork, head)
    if not mine or not theirs:
        return set()

    def keys(edits):
        return {(p, span, tuple(rm), tuple(add)) for p, span, rm, add in edits}

    return {key[0] for key in keys(mine) & keys(theirs)}


def collisions(root, fork, tip, head):
    """The paths where landing this branch beside `head` collides, or None when
    no trial merge can say — the head is not here, or git would not try.

    A conflict the fork point has as well is not this branch's: that PR is stale
    against the base, and would conflict on it with or without this branch.
    """
    if not head or not resolves(root, head):
        return None
    conflicts = merge_conflicts(root, tip, head)
    if conflicts is None:
        return None
    if conflicts:
        conflicts -= merge_conflicts(root, fork, head) or set()
    return conflicts | same_edits(root, fork, tip, head)


def is_ignored(path, patterns):
    """`fnmatchcase`, not `fnmatch`: the same glob must select the same paths
    on every platform, and branch-guard's `overlap_ignored` matches that way."""
    return any(fnmatch.fnmatchcase(path, pat) for pat in patterns)


def current_branch(root):
    out = git(root, 'symbolic-ref', '--short', '--quiet', 'HEAD')
    return out.strip() if out else ''


def head_ref(root, name):
    """The ref a named head branch resolves to here, or None. The local branch
    first — it is what a chained checkout lands on, and what a session pushes
    from — then the remote's, for a branch only ever fetched."""
    for ref in ('refs/heads/' + name, 'refs/remotes/origin/' + name):
        if resolves(root, ref):
            return ref
    return None


def repo_args(repo):
    """`--repo` for a `gh` read, when the create names the repo it opens in."""
    return ('--repo', repo) if repo else ()


def open_prs(root, repo=''):
    """Open PRs with the paths each one touches, or None.

    The field list is deliberately narrow and must stay that way: `number` and
    `headRefName` identify the PR, `headRefOid` says whether this branch is
    built on it, `files` is what the comparison needs. A
    human-writable field here (`title`, `body`) would be echoed into the
    session by the deny — the channel this plugin exists to keep shut.
    """
    status, out = capture(
        ('gh', 'pr', 'list', '--state', 'open', '--limit', str(MAX_OPEN_PRS),
         '--json', 'number,headRefName,headRefOid,files') + repo_args(repo),
        root)
    if status != 0:
        return None
    try:
        prs = json.loads(out)
    except ValueError:
        return None
    return prs if isinstance(prs, list) else None


def listed_paths(pr):
    """(paths `gh` listed for this PR, whether that list may be cut short).

    `gh` lists at most `GH_FILES_CAP` files, alphabetically, so a full list is
    a prefix: a path missing from it proves nothing, and only the diff can say
    whether the PR touches it."""
    paths = [f.get('path') for f in pr.get('files') or []]
    return set(paths), len(paths) >= GH_FILES_CAP


def may_share(pr, mine):
    """The paths of `mine` this PR may also change, before its diff is read."""
    listed, truncated = listed_paths(pr)
    return sorted(mine) if truncated else sorted(p for p in listed if p in mine)


def pr_ranges(root, number, repo=''):
    """What an open PR changes, or None.

    Numbered from that PR's own merge base rather than this branch's, so a
    long-lived PR's ranges drift. Close enough to tell an edit in the same
    function from one at the other end of the file, which is the question.

    `gh pr diff` has no `-U`, so this arrives at the default context and is not
    widened again — see `hunk_range`.
    """
    status, out = capture(('gh', 'pr', 'diff', str(number)) + repo_args(repo),
                          root)
    return None if status != 0 else parse_hunks(out, widen=False)


def built_on(root, prs, branch, tip, mine):
    """(nearest head commit, {head branch names}) for the open PRs this branch is
    built on, or None.

    A PR qualifies when its head commit is an ancestor of `tip` and not `tip`
    itself — a second branch name on the very same commit is a duplicate, not a
    stack. Its head has to be here already: nothing is fetched, so a parent
    known only to GitHub is not recognised and the create is judged as before.
    A chain of stacked PRs all qualify, and the branch is numbered from the
    nearest; two parents neither of which contains the other leave no single
    base to number from, so that is judged as before too.
    """
    tip_oid = (git(root, 'rev-parse', '--verify', '--quiet', tip) or '').strip()
    under = [pr for pr in prs
             if pr.get('headRefName') != branch and pr.get('headRefOid')
             and pr['headRefOid'] != tip_oid
             and may_share(pr, mine)
             and contains(root, pr['headRefOid'], tip)]
    for near in under:
        if all(contains(root, pr['headRefOid'], near['headRefOid'])
               for pr in under):
            return near['headRefOid'], {pr.get('headRefName') for pr in under}
    return None


def overlapping_prs(cwd, declared_base='', head='', repo=''):
    """[(number, paths, basis)] for open PRs this branch collides with, or [].

    `head` is the branch the create opens, when the command names one; empty
    means the checked-out branch. `repo` is the `--repo` it opens in; empty
    means the one `gh` resolves here.

    `basis` says what the entry rests on — `MERGED`, `RANGED` or `PATHED` — and
    the deny words it accordingly rather than passing a guess off as a finding.
    """
    root = repo_root(cwd)
    if root is None:
        return []
    branch = (head or '').strip()
    if branch:
        tip = head_ref(root, branch)
        if tip is None:
            return []                    # not a branch here: nothing to compare
    else:
        branch, tip = current_branch(root), 'HEAD'
        if not branch:
            return []                    # detached HEAD: nothing to compare
    base = base_ref(root, declared_base)
    fork = (git(root, 'merge-base', tip, base) or '').strip()
    if not fork:
        return []
    mine = changed_ranges(root, fork, tip)
    if not mine:
        return []                        # nothing committed yet, or git said no
    prs = open_prs(root, repo)
    if prs is None:
        return []
    parent = (declared_base or '').strip()
    stacked = bool(parent) and any(pr.get('headRefName') == parent
                                   for pr in prs)
    if stacked and not contains(root, base, tip):
        return []                        # stale stack: nothing comparable left
    inherited = {parent} if stacked else set()
    if not stacked:
        found = built_on(root, prs, branch, tip, mine)
        if found:
            fork = found[0]
            mine = changed_ranges(root, fork, tip)
            if not mine:
                return []
            inherited = found[1]
    patterns = ignore_patterns()
    hits, fetched = [], 0
    for pr in prs:
        if pr.get('headRefName') == branch:
            continue                     # this branch's own PR, already open
        if pr.get('headRefName') in inherited:
            continue                     # the stack's parent: inherited work
        shared = [p for p in may_share(pr, mine)
                  if not is_ignored(p, patterns)]
        if not shared:
            continue
        theirs = None
        if fetched < MAX_PR_DIFFS:
            fetched += 1                 # a failed fetch spends it too
            theirs = pr_ranges(root, pr.get('number'), repo)
        if theirs is not None:
            shared = [p for p in shared
                      if p in theirs and ranges_meet(mine[p], theirs[p])]
        else:                            # path-only: claim what gh listed
            shared = [p for p in shared if p in listed_paths(pr)[0]]
        if not shared:
            continue
        basis = RANGED if theirs is not None else PATHED
        collided = collisions(root, fork, tip, pr.get('headRefOid'))
        if collided is not None:
            shared, basis = [p for p in shared if p in collided], MERGED
        if shared:
            hits.append((pr.get('number'), shared, basis))
    return hits


def deciding(hits):
    """`hits` if any of them justifies a deny, else [].

    A `PATHED` entry is what a diff fetch that failed, or was never spent,
    leaves behind — the probe could not run. So it rides along in a deny a
    `RANGED` or `MERGED` entry already justifies, and decides nothing alone.
    """
    return hits if any(basis != PATHED for _n, _p, basis in hits) else []


BASIS_NOTE = {
    MERGED: 'a trial merge conflicts, or both make the same edit',
    RANGED: 'edits within six lines, not trial-merged',
    PATHED: 'shared path only, diff not fetched, not trial-merged; listed, '
            'not counted',
}


def build_reason(prs):
    """The deny reason. Names each PR by number and the paths it shares — never
    its title, which its author writes.

    An entry no trial merge decided gets the fetch that lets a re-run decide it.
    That is the model's to run, and spends no override on what may merge clean.
    """
    parts = ['#%s (%s; %s)' % (number, ', '.join(paths), BASIS_NOTE[basis])
             for number, paths, basis in prs]
    first = prs[0][0]
    untried = [number for number, _paths, basis in prs if basis != MERGED]
    fetch = (
        f'What was not trial-merged may still merge clean: run '
        f'`git fetch origin pull/{untried[0]}/head` (for each such PR) and '
        f're-run the create, which trial-merges a head it has and allows a '
        f'clean merge.\n'
    ) if untried else ''
    return (
        f'pr-sentinel: refusing `gh pr create` — this branch collides with an '
        f'open PR: {"; ".join(parts)}.\n'
        f'{fetch}'
        f'A real collision is duplicated or mutually invalidating work, and '
        f'review is an expensive place to find it. Read that diff first '
        f'(`gh pr diff {first}`), then either fold this branch into that one '
        f'or narrow it to what does not overlap.\n'
        f'If this PR replaces that one, say so in the create\'s own --title or '
        f'--body ("Supersedes #{first}") and re-run; a PR named that way is '
        f'not counted. A --body-file is not read. The reviewer will close #{first} '
        f'on that word, so name only a PR this one really replaces.\n'
        f'If the overlap is otherwise known and deliberate, re-run with an inline '
        f'PR_SENTINEL_OVERRIDE=<reason> prefix on the command itself '
        f'(PR_SENTINEL_OVERRIDE="why this once" gh pr create …).\n'
        f'Set PR_SENTINEL_OVERLAP_ENABLED=false to turn this check off, or '
        f'name the paths every branch edits in PR_SENTINEL_OVERLAP_IGNORE.'
    )
