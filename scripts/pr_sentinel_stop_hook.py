#!/usr/bin/env python3
"""Stop hook: the backstop that makes the advisory PostToolUse nudge reliable.

When a session ends its turn responsible for a pull request it has not concluded
(one it opened with `gh pr create`, or one it launched a watcher for),
with no live watcher tracking it, this hook BLOCKS the stop ONCE and tells the
session to launch the pr-sentinel background watcher before stopping. It respects
`stop_hook_active` so it can never loop: a stop that is itself the continuation
of a prior stop-hook block is allowed straight through.

Its inputs are all LOCAL: the session's own transcript (`transcript_path`) and
each watcher's own output file (its path learned from the harness's completion
notification for that background task). It makes NO network call, reads no
process table, writes nothing, and never touches the PR body or comment stream
(the excluded injection channel — see docs/DESIGN.md). Signals used:

  * Which PR is this session responsible for?  -> a `gh pr create` correlated
    with the PR URL that command printed, plus any PR the session launched a
    watcher for (a session that babysits a PR owns its follow-through, e.g. one
    resumed onto a branch whose PR an earlier session opened). When the create
    printed a URL the transcript never captured — output redirected to a log,
    or truncated — a second route resolves the number: the harness's own
    `pr-link` record, but ONLY one that is both emitted inside that create's own
    tool-call window and names a PR this transcript has not mentioned before.
    Both narrowings are load-bearing, because a `pr-link` standing alone is not
    an ownership signal: the harness emits one for ANY PR URL the session
    surfaces — a `gh pr view`/`gh pr comment` on someone else's PR produces the
    same record as a create — and it re-emits an already-linked PR after
    unrelated commands, so a stale one can land in a failed create's window.
    Reading "referenced" as "opened" caused false-positive blocks (PR #22); these
    two conditions keep the fallback clear of that while giving the backstop a
    resolution path the PostToolUse nudge does not share (#60). Best-effort: the
    harness emits these records for the session's own repo, so a create run
    against a different one leaves nothing to correlate. That case — and the
    larger one where no record is emitted at all — is covered by reading the
    file the create redirected its own output to: `gh pr create … > out.log`
    printed the URL, just not where the transcript could see it. The path comes
    from the create's own command string (model-authored, never tool output or
    CI-log text), is read byte-capped, and yields nothing unless it holds a
    github.com PR URL — so a command that opened no PR resolves nothing.
  * Is a watcher still running?  -> a `run_in_background` launch of
    `pr-sentinel-watch.sh <PR>` records a `tool_use` id; when that background
    task exits, the harness records a `<task-notification>` carrying the same
    `<tool-use-id>` and a `<status>`. A watcher is LIVE iff its launch id has no
    task-notification yet. This is a harness-generated record — untrusted CI-log
    text cannot forge it.
  * Was the PR handed off?  -> a `gh pr merge`/`close`, or a watcher terminal
    `ready`/`closed`/`blocked` report (NOT the non-terminal `ready_watching` and
    `blocked_watching` notices a `PR_SENTINEL_WATCH_UNTIL=closed` watcher emits
    on a still-open PR — that watcher keeps polling and may yet exit needing a
    relaunch, so treating it as a handoff would reopen the coverage gap the mode
    exists to close).
    For each completed watcher the hook reads that
    watcher's OWN output file DIRECTLY (path from the task-notification), so the
    signal does not depend on how — or whether — the session surfaced the output:
    a Bash `cat`/`tail` of it counts, not only the Read tool (issue #14). A
    concluded marker is trusted only in the report's header region, above
    the first embedded CI-log excerpt: a report embeds semi-untrusted CI logs, so
    a marker below that banner could be a forged log line. If the file is gone,
    we fall back to a transcript Read of it.

We cannot verify check status locally (that needs a network call), so "checks
still pending" is approximated as "owned, not handed off, unwatched". The block
is safe under that approximation: it fires at most once per stop-chain
(`stop_hook_active` lets the continuation through) and only asks the session to
launch the watcher, which then authoritatively determines check state (and exits
`ready` at once if the PR is already green). A watcher wake-up starts a NEW
stop-chain, so a genuinely-stuck PR could re-block on each relaunch; to avoid
that livelock we DAMPEN — once two separate watcher runs have reported the
identical terminal event (same event, same head SHA, same failed checks), the
session has pushed nothing and the stop is allowed with a non-blocking warning
instead. Every dampenable event asks the session to push, so an unmoved head SHA
is proof no push happened; for the heal events (`conflict`, `behind`) the usual
reason is that the heal is already committed locally and waiting on the
project's gate, which is exactly when a relaunch has no move available (#50).

The same dampening covers the case with no watcher report at all: a PR this hook
already blocked over once, with no watcher launched since, is warned about
rather than blocked again. The one signal it reads for that is a record only the
harness writes — its own verbatim copy of the earlier block — so nothing a tool
result carries can fake it. Repeating an ask the session did not act on cannot
help the session that had no move to make: one working under a dispatch protocol
never runs `gh pr merge` for a PR another session concludes, which leaves the
watcher's terminal report as its only route to a quiet turn end, and nothing at
all once the watcher fails to arm (#77). The first block still fires, because
launching the watcher is the right ask even there — on an already-merged PR it
answers `closed` on the first poll.

Fail-open on ANY uncertainty: unparseable input, unreadable transcript, no
opened PR, a concluded PR, or a live watcher -> emit nothing (allow the stop). It
must never break a session. PR_SENTINEL_DEBUG=1 re-raises. PR_SENTINEL_DISABLE=1
disables it (parity with the PostToolUse nudge).

Reached from `scripts/pr-sentinel.py`, which reads the Stop hook JSON on stdin
and calls `run()` with it; this module emits a block decision on stdout (or
nothing).
"""
import calendar
import json
import os
import re
import sys
import time

# The launch/completion records are read through the shared scan all three
# hooks use, so "is a watcher live for this PR" has exactly one definition.
# The path insert makes the sibling import work whether this file is run as a
# script or loaded by path (as the tests load it).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_sentinel_watchers as watchers   # noqa: E402

PR_URL_RE = watchers.PR_URL_RE
WATCH_ARG_RE = watchers.WATCH_ARG_RE
NOTIF_TOOL_ID_RE = watchers.NOTIF_TOOL_ID_RE
NOTIF_OUTFILE_RE = watchers.NOTIF_OUTFILE_RE
pr_number = watchers.pr_number

# A watcher TERMINAL report that means "nothing left to babysit" for a PR.
# `blocked` counts: its two causes — an outstanding approval, or a required
# check that never registered — both need a human, and neither can be waited
# out, so re-blocking the stop would only have the session relaunch a watcher
# that re-reports it (the livelock the check_failure dampening exists to avoid).
# The trailing guard is load-bearing: under `PR_SENTINEL_WATCH_UNTIL=closed` the
# watcher emits non-terminal `ready_watching`/`blocked_watching` NOTICES and
# keeps polling, and those must NOT read as a handoff — the PR is still open,
# still owned, and the watcher may yet exit needing a relaunch. Rejecting any
# word-or-dash continuation keeps a future `ready_*`/`closed_*` event from
# silently inheriting "concluded" too.
CONCLUDED_EVENT_RE = re.compile(
    r'PR-SENTINEL EVENT:\s*(?:ready|closed|blocked)(?![\w-])')

# The banner the watcher prints before every embedded CI-log excerpt. Everything
# from the FIRST such banner onward is semi-untrusted log text (a compromised
# dependency's test output can reach it), so a trusted `PR-SENTINEL EVENT:`
# marker is only honoured in the report header region ABOVE it. The watcher
# always writes its own header first, so the real marker always precedes this.
LOG_EXCERPT_BANNER = '----- BEGIN CI LOG EXCERPT'

# Terminal events a repeat of which means "nothing moved": every one of them
# asks the session to change the PR and push, so a second report at the SAME head
# commit proves no push happened between them. `check_failure` is the original
# case (#9); the heal events are the ones where a repeat is most reliably NOT
# actionable — the session has usually already healed the branch on disk and is
# waiting on its own gate before pushing, so the remote head cannot have moved
# yet (#50). The non-terminal notices (`base_failure`, `ready_watching`,
# `blocked_watching`) are excluded: the watcher keeps polling past them, so they
# are not the report the session is being blocked over. So are the concluded
# events, which already suppress the block outright.
DAMPENABLE_EVENT_RE = re.compile(
    r'PR-SENTINEL EVENT:\s*(check_failure|conflict|behind|dequeued)(?![\w-])')

# The opening words of the block message, which double as how a block this hook
# ALREADY made is found on a later turn: the harness records the reason verbatim
# in records only it writes (see `_prior_block_prs`).
BLOCK_MARKER = 'pr-sentinel: you are ending your turn'

# The PRs a recorded block named, from that message's own phrasing
# ("pull request #7" / "pull requests #7, #9").
_BLOCKED_PRS_RE = re.compile(r'pull requests?\s+((?:#\d+(?:,\s*)?)+)')
_HASH_NUM_RE = re.compile(r'#(\d+)')

# The dampening reason for a PR this hook already blocked over once with no
# watcher launched since. Not an event name, so a future watcher event cannot
# collide with it.
REPEAT_ASK = 'repeat_ask'

# The report-header fields that identify WHICH occurrence of an event it is: the
# head commit SHA (every dampenable event carries one) and, for `check_failure`,
# the set of failed checks. Both are matched only in the header region (above the
# excerpt banner), so a forged line in a CI log cannot drive the dampening.
# Deliberately NOT line-anchored: a Read result reaches the transcript in
# `cat -n` form (a line-number + tab prefix), and the header region is entirely
# watcher-authored, so a leading, unanchored search is both safe and
# prefix-robust.
FAILED_CHECKS_RE = re.compile(r'Failed checks:[ \t]*([^\n]*)')
HEAD_SHA_RE = re.compile(r'Head SHA:[ \t]*(\S+)')

# The hook reads only the HEADER of a watcher output file (the marker and the
# check_failure signature both sit above the first CI-log excerpt), so a byte cap
# bounds the read: it comfortably spans the fixed-template header plus the first
# excerpt banner, and a truncated read only ever yields watcher-authored header
# text, which is safe.
_OUTFILE_READ_CAP = 65536

# A tool call, matched on the raw line. Used ONLY to close a `gh pr create`'s
# correlation window, and only while one is open, so it costs nothing on the
# rest of the transcript. It has to work on the raw line because most tool calls
# carry none of the needles below and are never parsed. Matching the `type`
# field keeps it off a `tool_use_id` on a result entry.
TOOL_USE_LINE_RE = re.compile(r'"type":\s*"tool_use"')

# Cheap line pre-filter: only JSON-parse transcript lines that can carry a
# signal we care about. Everything else (the bulk of a session) is skipped.
# The shared scan's needles are unioned in so a single pass feeds it too.
_NEEDLES = tuple(set(
    ('PR-SENTINEL EVENT', 'pr create', 'pr merge', 'pr close', '/pull/',
     BLOCK_MARKER)
) | set(watchers.SCAN_NEEDLES))


# The redirect on the same simple command as a `gh pr create`: `> out.log`,
# `>> out.log`, `&> out.log`. A target carrying `$` or a backtick is left alone —
# the hook cannot expand it, and guessing would read the wrong file.
_REDIRECT_RE = re.compile(r"""(?:&|\d)?>>?\s*("[^"$`]+"|'[^'$`]+'|[^\s;&|<>$`]+)""")

# A `cd` to a literal path earlier in the same command, which is what a relative
# redirect target resolves against (`cd /path/to/repo && gh pr create … > o.log`).
_CD_RE = re.compile(r"""\bcd\s+("[^"$`]+"|'[^'$`]+'|[^\s;&|<>$`]+)""")

# The create's redirected output is read only for its URL, so a small cap is
# plenty: `gh` prints the URL on the first line.
_REDIRECT_READ_CAP = 8192

# How far a redirect file's mtime may predate its create's own entry timestamp
# before the file reads as a leftover from an earlier run. Only clock jitter
# needs absorbing — a genuinely stale log is minutes or hours old, not seconds.
_MTIME_SLACK = 60


def _unquote(token):
    return token.strip().strip('"\'')


def _create_redirect_path(command, cwd):
    """The absolute path a `gh pr create` in this command sent its output to, or
    None. Only the create's own simple command is considered, so an unrelated
    redirect elsewhere in a chain is not mistaken for it. A relative target
    resolves against a literal `cd` earlier in the command, else the entry's
    `cwd`; `/dev/…` and anything the hook cannot expand yield None."""
    m = re.search(r'\bgh\b(?:\s+\S+)*?\s+pr\s+create\b', command)
    if not m:
        return None
    segment = re.split(r'[;\n]|&&|\|\||(?<![0-9&])\|', command[m.end():])[0]
    rm = _REDIRECT_RE.search(segment)
    if not rm:
        return None
    target = _unquote(rm.group(1))
    if not target or target.startswith('/dev/'):
        return None
    if target.startswith('~'):
        target = os.path.expanduser(target)
    if os.path.isabs(target):
        return target
    cm = _CD_RE.search(command[:m.start()])
    base = os.path.expanduser(_unquote(cm.group(1))) if cm else (cwd or '')
    if not os.path.isabs(base):
        return None
    return os.path.join(base, target)


def _entry_epoch(timestamp):
    """An entry's ISO-8601 UTC `timestamp` as epoch seconds, or None if absent
    or in a shape this cannot read."""
    if not isinstance(timestamp, str):
        return None
    try:
        return calendar.timegm(time.strptime(timestamp[:19], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return None


def _redirected_pr_urls(path, created_after=None):
    """The github.com PR URLs in a create's redirected output file. Empty on any
    I/O trouble or when the file names no PR — a create that opened nothing
    (`--help`, a failure, `--dry-run`) leaves no URL to find.

    `created_after` is the create's own timestamp, and a file older than that is
    ignored: log paths get reused (`tmp/prcreate.log` in the same worktree run
    after run), so a create that failed would otherwise read the PREVIOUS run's
    URL and claim a PR this session never opened. An unreadable timestamp skips
    the check rather than suppressing the route."""
    try:
        if created_after is not None:
            if os.path.getmtime(path) < created_after - _MTIME_SLACK:
                return []
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read(_REDIRECT_READ_CAP)
    except OSError:
        return []
    return [m.group(0) for m in PR_URL_RE.finditer(text)]


def _is_pr_create(command):
    """True if a command string runs `gh pr create` (env prefixes / flags may
    sit anywhere before the subcommands)."""
    return bool(re.search(r'\bgh\b(?:\s+\S+)*?\s+pr\s+create\b', command))


def _pr_close_targets(command):
    """PR numbers a `gh pr merge`/`gh pr close` in this command concludes."""
    targets = set()
    for m in re.finditer(r'\bgh\s+pr\s+(?:merge|close)\s+(\S+)', command):
        num = pr_number(m.group(1))
        if num:
            targets.add(num)
    return targets


def _block_texts(content):
    """Yield text from a message `content`, which may be a string, or a list of
    blocks (tool_result / text) whose payloads are strings or nested blocks."""
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get('text'), str):
            yield block['text']
        inner = block.get('content')
        if isinstance(inner, str):
            yield inner
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict) and isinstance(sub.get('text'), str):
                    yield sub['text']


def _entry_text(obj, content):
    """All human/tool text on one transcript entry: message content plus a
    structured Bash `toolUseResult` stdout/stderr, if present."""
    parts = list(_block_texts(content))
    tur = obj.get('toolUseResult')
    if isinstance(tur, dict):
        parts += [tur[k] for k in ('stdout', 'stderr') if isinstance(tur.get(k), str)]
    return '\n'.join(parts)


_notification_text = watchers.notification_text


def _report_header_region(text):
    """The part of a watcher report ABOVE its first CI-log excerpt — the region
    the watcher itself writes, before any semi-untrusted log text. Splitting at
    the FIRST banner is what makes it forgery-proof: the real header always
    precedes all excerpts, so a marker planted inside a log cannot climb above
    one. If no banner is present the whole text is header."""
    return text.split(LOG_EXCERPT_BANNER, 1)[0]


def _report_signature(text):
    """For a read of a watcher output file, the identity of the dampenable
    terminal event it reports as `(event, failed_checks, head_sha)`, or None if
    it reports none (or predates the head-SHA field on that event). Read only
    from the header region so a forged copy inside a CI-log excerpt cannot be
    mistaken for it, and only from the marker FORWARD: the same file can carry an
    earlier `base_failure` notice, which has its own `Failed checks:` and
    `Head SHA:` lines, and the signature has to describe the terminal event. The
    LAST marker is the terminal one — every notice that can precede it keeps the
    watcher polling, and each emitter writes its whole header before any
    excerpt, so all markers sit in the header region in emission order."""
    header = _report_header_region(text)
    marks = list(DAMPENABLE_EVENT_RE.finditer(header))
    if not marks:
        return None
    event = marks[-1].group(1)
    header = header[marks[-1].end():]
    sm = HEAD_SHA_RE.search(header)
    if not sm:
        return None
    fm = FAILED_CHECKS_RE.search(header)
    return (event, fm.group(1).strip() if fm else '', sm.group(1))


def _prior_block_prs(obj):
    """PR numbers a PREVIOUS run of this hook already blocked this session over,
    from the harness's own record of that block. Three shapes carry it — a
    `hook_blocking_error` attachment, the `stop_hook_summary` system entry, and
    the `Stop hook feedback:` message the block is fed back on — and all three
    are written by the harness, never by the model or a tool result, so a
    CI-log excerpt quoting the text cannot manufacture one. Empty set for any
    other entry."""
    texts = []
    att = obj.get('attachment')
    if isinstance(att, dict) and att.get('type') == 'hook_blocking_error' \
            and att.get('hookName') == 'Stop':
        err = att.get('blockingError')
        if isinstance(err, dict):
            err = err.get('blockingError')
        if isinstance(err, str):
            texts.append(err)
    elif obj.get('type') == 'system' and obj.get('subtype') == 'stop_hook_summary':
        texts += [e for e in (obj.get('hookErrors') or []) if isinstance(e, str)]
    elif obj.get('type') == 'user':
        msg = obj.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        # A string content is a harness-injected message; a tool result is a
        # list of blocks, so this can never read one.
        if isinstance(content, str) and content.startswith('Stop hook feedback:'):
            texts.append(content)
    prs = set()
    for text in texts:
        if BLOCK_MARKER not in text:
            continue
        m = _BLOCKED_PRS_RE.search(text)
        if m:
            prs |= set(_HASH_NUM_RE.findall(m.group(1)))
    return prs


def _read_file_path(obj):
    """For a Read tool_result entry, the file path it read (or None)."""
    tur = obj.get('toolUseResult')
    if isinstance(tur, dict) and isinstance(tur.get('file'), dict):
        fp = tur['file'].get('filePath')
        if isinstance(fp, str):
            return fp
    return None


def _outfile_text(path, fallback_by_path):
    """The terminal report text of a watcher output file. Read DIRECTLY from the
    file — the hook always learns the path from the completion notification, so
    this does not depend on how (or whether) the session surfaced the output (a
    Bash `cat`/`tail` counts, not only the Read tool; issue #14). Only the header
    prefix is needed, so the read is byte-capped. If the file is gone, fall back
    to a transcript Read of that path. Empty string if neither is available;
    fail-open on any I/O error (treated as 'no terminal report')."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read(_OUTFILE_READ_CAP)
    except OSError:
        return fallback_by_path.get(path, '')


def _analyze(path):
    """Core transcript analysis, returning `(block, dampened)`:

      * block    — PR numbers the session is responsible for (opened via
                   `gh pr create`, or babysat via a watcher launch) that are
                   unconcluded AND have no live watcher AND are not dampened:
                   the stop is blocked over these.
      * dampened — `{PR: event}` for PRs that WOULD block, but whose watcher has
                   now reported the identical terminal event (same event +
                   failed-set + head SHA) on two separate reads. The session
                   pushed nothing between them, so the report is one it cannot
                   clear in-session (or has already cleared locally and cannot
                   push yet); we stop blocking and let `main` warn instead of
                   nagging forever.

    Fail-open: returns `(set(), {})` on any I/O trouble (allow the stop)."""
    created = set()
    concluded = set()
    scan = watchers.WatcherScan()   # watcher launches and their completions
    reads = []                 # (file_path, text) for Read results
    create_ids = []            # tool_use_ids that ran `gh pr create`
    result_text = {}           # tool_use_id -> concatenated result text
    seen_prs = set()           # PR numbers this transcript has mentioned so far
    in_create = False          # the most recent tool_use ran `gh pr create`
    redirects = []             # files a `gh pr create` sent its output to
    url_by_pr = {}             # PR number -> the full URL, when one was resolved
    asked = set()              # PRs a previous block by this hook already named
    launched_since_ask = set()  # ... of those, ones watched since that block

    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                if in_create and TOOL_USE_LINE_RE.search(raw):
                    in_create = False   # a later tool call closes the window
                if not any(n in raw for n in _NEEDLES):
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue

                # A harness `pr-link` record resolves a create whose own output
                # never reached the transcript — but only inside that create's
                # window, and only for a PR number not seen before it (a stale
                # re-emission names one this transcript already mentioned).
                line_prs = {m.group(1) for m in PR_URL_RE.finditer(raw)}
                if obj.get('type') == 'pr-link':
                    num = str(obj.get('prNumber') or '').strip()
                    if in_create and num.isdigit() and num not in seen_prs:
                        created.add(num)
                    seen_prs |= line_prs | {num}
                    continue
                seen_prs |= line_prs

                # A block this hook already made, and which PRs it named. A
                # later launch clears the PR again, so what survives is "asked
                # for a watcher, and none launched since".
                prior = _prior_block_prs(obj)
                if prior:
                    asked |= prior
                    launched_since_ask -= prior
                    continue

                # The shared scan takes the watcher launches, their background
                # task ids, and the completion notifications; True means the
                # entry was a notification and carries nothing else we read.
                if scan.feed(obj):
                    continue

                msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
                content = msg.get('content') if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        btype = b.get('type')
                        if btype == 'tool_use':
                            in_create = False
                            if b.get('name') != 'Bash':
                                continue
                            inp = b.get('input') or {}
                            cmd = inp.get('command') or ''
                            if inp.get('run_in_background'):
                                for wm in WATCH_ARG_RE.finditer(cmd):
                                    num = pr_number(wm.group(1))
                                    if num:
                                        launched_since_ask.add(num)
                            if _is_pr_create(cmd):
                                create_ids.append(b.get('id'))
                                in_create = True
                                rp = _create_redirect_path(cmd, obj.get('cwd'))
                                if rp:
                                    redirects.append(
                                        (rp, _entry_epoch(obj.get('timestamp'))))
                            concluded |= _pr_close_targets(cmd)
                        elif btype == 'tool_result':
                            tid = b.get('tool_use_id')
                            if tid is not None:
                                result_text[tid] = result_text.get(tid, '') \
                                    + '\n' + '\n'.join(_block_texts(b.get('content')))

                fp = _read_file_path(obj)
                if fp:
                    reads.append((fp, _entry_text(obj, content)))
    except OSError:
        return set(), {}, {}

    # Opened PRs: the number gh printed in the create command's own output
    # (plus any resolved from a correlated `pr-link` above).
    for tid in create_ids:
        for m in PR_URL_RE.finditer(result_text.get(tid, '')):
            created.add(m.group(1))

    # The same output, when the create redirected it to a file: the URL is on
    # disk rather than in the transcript. This resolves the cross-repo create
    # too, and it resolves the full URL, which is what lets the block name a PR
    # in a repository other than the one the session is sitting in.
    for rp, created_at in redirects:
        for url in _redirected_pr_urls(rp, created_at):
            num = pr_number(url)
            if num:
                created.add(num)
                url_by_pr.setdefault(num, url)

    # Map each watcher's output file to the PR it watches (path from the
    # completion notification, PR from the launch's `pr-sentinel-watch.sh` arg).
    outfile_pr = {scan.outfile_by_toolid[t]: scan.pr_by_toolid[t]
                  for t in scan.outfile_by_toolid if t in scan.pr_by_toolid}

    # Fallback text for each watcher output file: any Read-tool read of it. Used
    # only if the file itself is gone; the direct read below is authoritative.
    read_text_by_outfile = {}
    for fp, text in reads:
        if fp in outfile_pr:
            read_text_by_outfile[fp] = \
                read_text_by_outfile.get(fp, '') + '\n' + text

    # Handed off / dampening: read each completed watcher's OWN output file
    # DIRECTLY (issue #14 — no longer hostage to the session's read method), and
    # judge only its header region so an embedded CI-log excerpt cannot forge the
    # marker or the signature.
    sig_outfiles = {}   # PR -> {report signature -> set of output files}
    for outfile, pr in outfile_pr.items():
        text = _outfile_text(outfile, read_text_by_outfile)
        if not text:
            continue
        if CONCLUDED_EVENT_RE.search(_report_header_region(text)):
            concluded.add(pr)
        sig = _report_signature(text)
        if sig is not None:
            sig_outfiles.setdefault(pr, {}).setdefault(sig, set()).add(outfile)

    # Live: a watcher launch whose task has not reported completion.
    live = set(scan.live())

    # The session's own PRs: ones it created, plus ones it launched a watcher
    # for (babysitting a PR is taking responsibility for it — this covers a
    # session resumed onto a branch whose PR an earlier session opened). A PR
    # merely referenced — `gh pr view`/`gh pr comment` on someone else's PR —
    # is in neither set and never blocks.
    owned = created | set(scan.pr_by_toolid.values())

    block = owned - concluded - live
    # Dampen: an unresolved-and-unwatched PR whose identical terminal event was
    # reported by two separate watcher runs (two distinct output files, same
    # event + failed-set + SHA -> nothing pushed between them).
    # A PR this hook already asked about once, with nothing launched since, is
    # dampened too: the ask cannot be satisfied by repeating it (#77).
    dampened = {}
    for pr in block:
        repeated = next((sig[0] for sig, files in sig_outfiles.get(pr, {}).items()
                         if len(files) >= 2), None)
        if repeated:
            dampened[pr] = repeated
        elif pr in asked and pr not in launched_since_ask:
            dampened[pr] = REPEAT_ASK
    return block - set(dampened), dampened, url_by_pr


def prs_needing_watcher(path):
    """The set of PR numbers a stop should be blocked over (owned, unconcluded,
    unwatched, not dampened). Fail-open: empty set on any I/O trouble."""
    return _analyze(path)[0]


def watcher_command(pr, url=None):
    """The launch line for one PR. `url` is passed when the hook resolved the
    full URL — the watcher accepts either, and the URL is what makes the launch
    land on the right repository when the PR is not in the one the session is
    sitting in."""
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', '')
    watcher = os.path.join(plugin_root, 'scripts', 'pr-sentinel-watch.sh') \
        if plugin_root else 'scripts/pr-sentinel-watch.sh'
    return f'    bash "{watcher}" {url or pr}'


def build_reason(prs, urls=None):
    """The block message fed back to the model."""
    urls = urls or {}
    prs = sorted(prs, key=int)
    label = 'pull request #' + prs[0] if len(prs) == 1 \
        else 'pull requests ' + ', '.join('#' + p for p in prs)
    commands = '\n'.join(watcher_command(p, urls.get(p)) for p in prs)
    return (
        f'{BLOCK_MARKER} with an open {label} this '
        f'session opened or was watching, but no watcher is tracking it and CI may still be '
        f'running. Launch the PR Sentinel watcher as a BACKGROUND task '
        f'(run_in_background) before you stop, so a CI failure or merge conflict '
        f'wakes this session — do NOT foreground-poll with `gh pr checks '
        f'--watch`, `gh run watch`, or a sleep loop. Command'
        f'{"s" if len(prs) > 1 else ""}:\n{commands}\n'
        f'When the watcher wakes you, act on the single reported event, push, '
        f'and relaunch it. If you have already handed this PR to a human for '
        f'merge review, you may stop. Never auto-merge.'
    )


# What a repeat of each dampenable event most likely means, so the notice tells
# the session something true about the state it is walking away from rather than
# describing every case as a stuck check.
_DAMPEN_DETAIL = {
    'check_failure':
        'a failing check that has not changed across repeated watcher reports '
        '(same failed checks, same commit) — it looks like one this session '
        'cannot fix (e.g. inherited from the base branch, out-of-scope, or '
        'external)',
    'conflict':
        'a merge conflict still reported at the same commit across repeated '
        'watcher reports — nothing has been pushed to clear it, so either the '
        'heal is done locally and still waiting on your gate, or it needs a '
        'human',
    'behind':
        'a branch still behind its base at the same commit across repeated '
        'watcher reports — nothing has been pushed to bring it up to date, so '
        'either the update is done locally and still waiting on your gate, or '
        'it needs a human',
    'dequeued':
        'a merge-queue removal still reported at the same commit across '
        'repeated watcher reports — re-enqueueing is a human\'s call, so there '
        'is nothing further to do here',
    REPEAT_ASK:
        'no watcher launched since this hook asked for one — it asks once per '
        'pull request, so this is a notice rather than a second block; launch '
        'the watcher if the PR is still open and yours, and nothing at all if '
        'another session has already concluded it',
}
_DAMPEN_GENERIC = ('an unchanged watcher report at the same commit across '
                   'repeated runs — nothing has been pushed to move it')


def build_warning(dampened):
    """A non-blocking notice for PRs whose watcher keeps reporting the same
    unmoved state. The block already fired once with full detail; this keeps the
    PR visible without nagging the session into a relaunch loop. `dampened` maps
    each PR to the event its watcher repeated."""
    parts = '; '.join(
        f'pull request #{pr} with '
        f'{_DAMPEN_DETAIL.get(dampened[pr], _DAMPEN_GENERIC)}'
        for pr in sorted(dampened, key=int))
    return (
        f'pr-sentinel: leaving {parts}. NOT blocking your stop. If it is in '
        f'fact actionable here, act and push; otherwise hand it to a human. '
        f'Never auto-merge.'
    )


def run(data):
    """Decide whether to block this stop, and print the decision if so.

    Called by the `pr-sentinel.py` entry point, which owns the stdin parse and
    the fail-open wrapper. Emitting nothing allows the stop."""
    if os.environ.get('PR_SENTINEL_DISABLE') == '1':
        return
    # Never block a stop that is itself a continuation of a prior stop-hook
    # block — this is the no-loop guarantee.
    if data.get('stop_hook_active'):
        return

    transcript = data.get('transcript_path')
    if not transcript:
        return
    unwatched, dampened, urls = _analyze(transcript)
    if not unwatched and not dampened:
        return  # nothing opened-and-unwatched, nothing to warn about: allow

    out = {}
    if unwatched:
        out['decision'] = 'block'
        out['reason'] = build_reason(unwatched, urls)
    if dampened:
        # Non-blocking notice; survives even when the stop is allowed.
        out['systemMessage'] = build_warning(dampened)
    print(json.dumps(out))

