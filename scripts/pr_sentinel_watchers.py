"""Shared read of a session transcript for pr-sentinel watcher launches: which
PRs this session launched a watcher for, which of those watchers are still
running, and the background task id that would stop one.

A launch is a `run_in_background` Bash call naming `pr-sentinel-watch.sh <PR>`.
The harness answers it with a background task id, and when that task exits it
records a `<task-notification>` carrying the launch's `tool_use` id. A watcher
is LIVE iff its launch got a task id and has no notification yet. Both records
are harness-generated, so untrusted CI-log text cannot forge one.

The task id is what separates a watcher that started from one that never did.
The harness writes a Bash `tool_use` entry BEFORE running the PreToolUse hook,
so a scan taken from inside that hook already sees the launch under decision —
with no task id, and, if the hook then denies it, no notification ever either.
Counting such an entry live made the guard refuse a session's first watcher as
a duplicate of itself and left the PR unwatched for the rest of the session.
Callers deciding a launch should also name it in `exclude`; the two rules are
independent, and either alone closes that case.

All three hooks read this. The Stop hook asks which PRs still need a watcher;
the PostToolUse nudge and the PreToolUse guard ask the opposite question — is
one already running — so a session stops stacking watchers on a PR it is
already watching. Keeping the rule in one module is what keeps those two
answers from drifting apart.

It also reads, off the same launches, a watch this session armed with something
other than this plugin's watcher. That is a separate answer and never the same
one: a foreign watch is not evidence the session owns the PR, and it does not
make a PR watched, because it need not report check conclusions at all. The
Stop hook uses it only to say what is already running when it asks for ours.

Purely local and read-only: it parses transcript JSON and nothing else.
"""
import json
import os
import re
import sys

# The shared bash tokenizer, so a foreign watcher launch is read off argv lists
# rather than a second hand-rolled split. The path insert makes the sibling
# import work whether this file is imported normally or loaded by path (as the
# tests load it).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_sentinel_tokenize as tokenize   # noqa: E402

# A github.com PR URL, e.g. https://github.com/owner/repo/pull/123
PR_URL_RE = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)')

# A watcher launch inside a Bash command: `... pr-sentinel-watch.sh 42`. The
# argument ends at a shell separator, not whitespace, so a launch that carries
# its status out — `… watch.sh 42; exit $?` — still names PR 42, not `42;`.
WATCH_ARG_RE = re.compile(r'pr-sentinel-watch\.sh["\']?\s+([^\s;&|<>()]+)')

# This plugin's own watcher basename, which is never foreign.
OWN_WATCHER = 'pr-sentinel-watch.sh'

# A FOREIGN PR watcher: a background watch the session armed that is not this
# plugin's, recognised by script basename — one carrying `watch`, as a `.py` or
# `.sh` file. Narrow on purpose, so a backgrounded `gh pr view 42` (no such
# file) and a `gh pr checks --watch` (a flag, not a basename) do not match.
#
# It is allowed to be a heuristic because no caller lets a foreign watcher
# satisfy a decision. A foreign watch answers the conflict half of "is this PR
# being watched" and says nothing about check conclusions, so the Stop hook
# still blocks over it (Q40) and only names it in the ask. A miss therefore
# costs a less specific sentence, and a wrong hit costs nothing else.
FOREIGN_WATCH_RE = re.compile(r'[^/\\]*watch[^/\\]*\.(?:py|sh)\Z')

# Fields pulled out of a `<task-notification>` completion record.
NOTIF_TOOL_ID_RE = re.compile(r'<tool-use-id>\s*(toolu_[A-Za-z0-9]+)')
NOTIF_OUTFILE_RE = re.compile(r'<output-file>\s*([^<\s]+)')

# The background task id, as the harness reports it back on the launch's own
# tool_result. `toolUseResult.backgroundTaskId` is the structured form; the
# sentence in the result text is the fallback for an entry that carries only
# the human-readable content.
BG_TASK_ID_RE = re.compile(r'running in background with ID:\s*(\S+?)[.\s]')

# Cheap line pre-filter: the transcript lines that can carry a signal this scan
# reads. A caller doing its own pass should union this with its own needles —
# the launch and its task id sit on different entries, and only the launch line
# names the watcher.
#
# `watch` rather than `pr-sentinel-watch.sh`, because a foreign watcher's launch
# line carries neither that basename nor any other needle here, and there is no
# narrower literal common to every `*watch*.py`/`*watch*.sh`. It parses more
# lines than the old needle did; the filter is an optimisation, and a launch it
# skips is one the scan cannot see at all.
SCAN_NEEDLES = ('watch', 'task-notification',
                'backgroundTaskId', 'running in background')


def pr_number(token):
    """Normalise a PR token (a bare number or a github.com PR URL) to its
    number string, or None if it is neither."""
    token = str(token).strip().strip('"\'')
    if token.isdigit():
        return token
    m = PR_URL_RE.search(token)
    return m.group(1) if m else None


def foreign_watch_pr(command):
    """The PR number a command watches with a watcher that is NOT this
    plugin's, or None. Recognises the watcher by script basename
    (`FOREIGN_WATCH_RE`) and takes the first PR operand after it.

    Fail-open like everything else here: a command the tokenizer cannot parse
    yields None, which reads as no foreign watcher."""
    for argv in tokenize.simple_commands(command):
        for i, token in enumerate(argv):
            base = token.replace('\\', '/').rsplit('/', 1)[-1]
            if base == OWN_WATCHER or not FOREIGN_WATCH_RE.match(base):
                continue
            num = _pr_operand(argv[i + 1:])
            if num:
                return num, base
    return None


def _pr_operand(tokens):
    """The first PR token (bare number or github.com PR URL) in an argument
    list, or None. Skips flags, and the value of a separated option — the
    `600` in `--timeout 600 42` is a number and is not the PR."""
    prev = ''
    for token in tokens:
        flagged = token.startswith('-') or \
            (prev.startswith('-') and '=' not in prev)
        if not flagged:
            num = pr_number(token)
            if num:
                return num
        prev = token
    return None


def notification_text(obj):
    """The `<task-notification>` payload of an entry, from either a
    `queue-operation` (.content) or an `attachment` (.attachment.prompt)."""
    if obj.get('type') == 'queue-operation':
        c = obj.get('content')
        return c if isinstance(c, str) and '<task-notification>' in c else ''
    att = obj.get('attachment')
    if isinstance(att, dict):
        p = att.get('prompt')
        if isinstance(p, str) and '<task-notification>' in p:
            return p
    return ''


class WatcherScan(object):
    """Accumulates watcher launches and completions from transcript entries.

    Fed one parsed entry at a time so a caller already walking the transcript
    for its own signals pays for a single pass (`feed` returns True on an entry
    it fully consumed, which is the caller's cue to skip its own handling)."""

    def __init__(self):
        self.pr_by_toolid = {}       # launch tool_use_id -> PR number
        self.task_by_toolid = {}     # launch tool_use_id -> background task id
        self.outfile_by_toolid = {}  # completed launch id -> its output file
        self.completed = set()       # launch ids whose task reported completion
        # A foreign watcher's launch, kept apart from `pr_by_toolid` because it
        # is NOT an ownership signal: a session watching someone else's PR with
        # another tool must not start being blocked over it.
        self.foreign_pr_by_toolid = {}      # launch tool_use_id -> PR number
        self.foreign_script_by_toolid = {}  # launch tool_use_id -> basename

    def feed(self, obj):
        """Read one transcript entry. True if it was a completion notification
        (nothing else on such an entry concerns any caller)."""
        notif = notification_text(obj)
        if notif and '<status>' in notif:
            m = NOTIF_TOOL_ID_RE.search(notif)
            if m:
                self.completed.add(m.group(1))
                om = NOTIF_OUTFILE_RE.search(notif)
                if om:
                    self.outfile_by_toolid[m.group(1)] = om.group(1).strip()
            return True

        msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
        content = msg.get('content') if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return False
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'tool_use':
                if b.get('name') != 'Bash':
                    continue
                inp = b.get('input') or {}
                if not inp.get('run_in_background'):
                    continue   # a foreground run is not a watcher this can stop
                cmd = inp.get('command') or ''
                for m in WATCH_ARG_RE.finditer(cmd):
                    num = pr_number(m.group(1))
                    if num:
                        self.pr_by_toolid[b.get('id')] = num
                if b.get('id') not in self.pr_by_toolid:
                    hit = foreign_watch_pr(cmd)
                    if hit:
                        self.foreign_pr_by_toolid[b.get('id')] = hit[0]
                        self.foreign_script_by_toolid[b.get('id')] = hit[1]
            elif b.get('type') == 'tool_result':
                tid = b.get('tool_use_id')
                if tid is not None:
                    task = _background_task_id(obj, b)
                    if task:
                        self.task_by_toolid[tid] = task
        return False

    def live(self, exclude=()):
        """Live watchers as {PR number: [background task id, ...]}, launch order
        preserved. `exclude` names launch tool_use ids to skip — a hook deciding
        a launch passes its own, so it never reads that launch as an incumbent.

        A launch with no recorded task id never started, so it is not evidence
        of a running watcher and is skipped. Erring this way stacks a redundant
        watcher at worst; erring the other way leaves the PR unwatched with a
        deny telling the session not to retry."""
        return {pr: [self.task_by_toolid[t] for t in tids] for pr, tids
                in self._live_launches(self.pr_by_toolid, exclude).items()}

    def foreign(self, exclude=()):
        """Live FOREIGN watchers as {PR number: [script basename, ...]} — the
        same liveness rule as `live`, over watches this session armed with
        something other than this plugin's watcher.

        Names the script rather than the task id because no caller offers to
        stop one: a foreign watch never satisfies a decision here, so the only
        thing a caller does with this is say what is already running."""
        return {pr: [self.foreign_script_by_toolid[t] for t in tids] for pr, tids
                in self._live_launches(self.foreign_pr_by_toolid, exclude).items()}

    def _live_launches(self, pr_by_toolid, exclude):
        """{PR number: [launch tool_use id, ...]} for the launches in
        `pr_by_toolid` that started and have not reported completion, launch
        order preserved."""
        skip = {t for t in exclude if t}
        out = {}
        for tid, pr in pr_by_toolid.items():
            if tid in self.completed or tid in skip:
                continue
            if not self.task_by_toolid.get(tid, ''):
                continue
            out.setdefault(pr, []).append(tid)
        return out


def _background_task_id(obj, block):
    """The harness's background task id for a launch's tool_result entry, from
    the structured field or the sentence in the result text. '' if absent —
    which is what a foreground command's result looks like."""
    tur = obj.get('toolUseResult')
    if isinstance(tur, dict):
        task = tur.get('backgroundTaskId')
        if isinstance(task, str) and task.strip():
            return task.strip()
    content = block.get('content')
    if not isinstance(content, str):
        parts = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get('text'), str):
                    parts.append(c['text'])
        content = '\n'.join(parts)
    m = BG_TASK_ID_RE.search(content)
    return m.group(1) if m else ''


def live_watchers(path, exclude=()):
    """{PR number: [background task id, ...]} for every watcher this transcript
    launched that has started and not reported completion. `exclude` names
    launch tool_use ids to skip — see `WatcherScan.live`. Fail-open: {} on any
    I/O or parsing trouble, so a caller never blocks a launch it cannot reason
    about."""
    if not path or not os.path.isfile(path):
        return {}
    scan = WatcherScan()
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                if not any(n in raw for n in SCAN_NEEDLES):
                    continue
                try:
                    scan.feed(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        return {}
    return scan.live(exclude)


def stop_hint(pr, task_ids):
    """One sentence naming how to stop the live watcher(s) on `pr`, for a hook
    telling a session not to launch another. `live()` only reports watchers it
    has an id for, so the no-id sentence is the floor for a hand-built call
    rather than something a hook path reaches."""
    known = [t for t in task_ids if t]
    if not known:
        return (f'To restart the watch on #{pr} instead, stop the running '
                f'watcher task first (TaskStop) and then relaunch.')
    if len(known) == 1:
        return (f'To restart the watch on #{pr} instead, stop it first: '
                f'TaskStop(task_id="{known[0]}"), then relaunch.')
    listed = ', '.join(f'"{t}"' for t in known)
    return (f'{len(known)} watchers are already running on #{pr} — stop the '
            f'extras with TaskStop (task ids: {listed}); keep at most one.')
