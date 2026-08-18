"""Shared read of a session transcript for pr-sentinel watcher launches: which
PRs this session launched a watcher for, which of those watchers are still
running, and the background task id that would stop one.

A launch is a `run_in_background` Bash call naming `pr-sentinel-watch.sh <PR>`.
The harness answers it with a background task id, and when that task exits it
records a `<task-notification>` carrying the launch's `tool_use` id. A watcher
is LIVE iff its launch id has no notification yet. Both records are
harness-generated, so untrusted CI-log text cannot forge one.

All three hooks read this. The Stop hook asks which PRs still need a watcher;
the PostToolUse nudge and the PreToolUse guard ask the opposite question — is
one already running — so a session stops stacking watchers on a PR it is
already watching. Keeping the rule in one module is what keeps those two
answers from drifting apart.

Purely local and read-only: it parses transcript JSON and nothing else.
"""
import json
import os
import re

# A github.com PR URL, e.g. https://github.com/owner/repo/pull/123
PR_URL_RE = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)')

# A watcher launch inside a Bash command: `... pr-sentinel-watch.sh 42`.
WATCH_ARG_RE = re.compile(r'pr-sentinel-watch\.sh["\']?\s+(\S+)')

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
SCAN_NEEDLES = ('pr-sentinel-watch.sh', 'task-notification',
                'backgroundTaskId', 'running in background')


def pr_number(token):
    """Normalise a PR token (a bare number or a github.com PR URL) to its
    number string, or None if it is neither."""
    token = str(token).strip().strip('"\'')
    if token.isdigit():
        return token
    m = PR_URL_RE.search(token)
    return m.group(1) if m else None


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
                for m in WATCH_ARG_RE.finditer(inp.get('command') or ''):
                    num = pr_number(m.group(1))
                    if num:
                        self.pr_by_toolid[b.get('id')] = num
            elif b.get('type') == 'tool_result':
                tid = b.get('tool_use_id')
                if tid is not None:
                    task = _background_task_id(obj, b)
                    if task:
                        self.task_by_toolid[tid] = task
        return False

    def live(self):
        """Live watchers as {PR number: [background task id, ...]}, launch order
        preserved. A task id is '' when the transcript never recorded one, so a
        caller must always be able to say something useful without it."""
        out = {}
        for tid, pr in self.pr_by_toolid.items():
            if tid in self.completed:
                continue
            out.setdefault(pr, []).append(self.task_by_toolid.get(tid, ''))
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


def live_watchers(path):
    """{PR number: [background task id, ...]} for every watcher this transcript
    launched that has not reported completion. Fail-open: {} on any I/O or
    parsing trouble, so a caller never blocks a launch it cannot reason about."""
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
    return scan.live()


def stop_hint(pr, task_ids):
    """One sentence naming how to stop the live watcher(s) on `pr`, for a hook
    telling a session not to launch another."""
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
