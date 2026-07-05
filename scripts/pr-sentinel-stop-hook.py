#!/usr/bin/env python3
"""Stop hook: the backstop that makes the advisory PostToolUse nudge reliable.

When a session ends its turn having opened a pull request it has not concluded,
with no live watcher tracking it, this hook BLOCKS the stop ONCE and tells the
session to launch the pr-sentinel background watcher before stopping. It respects
`stop_hook_active` so it can never loop: a stop that is itself the continuation
of a prior stop-hook block is allowed straight through.

Everything it needs is in the ONE file the harness already hands it — the
session's own transcript (`transcript_path`). It makes NO network call, reads no
process table, writes nothing, and never touches the PR body or comment stream
(the excluded injection channel — see docs/DESIGN.md). Signals used, all from the
transcript JSONL:

  * Which PR did the session open?  -> the harness's own `pr-link` records (a
    canonical `prNumber`/`prUrl` marker), plus a `gh pr create` correlated with
    the PR URL that command printed. Both are GitHub-controlled metadata the
    session already surfaced.
  * Is a watcher still running?  -> a `run_in_background` launch of
    `pr-sentinel-watch.sh <PR>` records a `tool_use` id; when that background
    task exits, the harness records a `<task-notification>` carrying the same
    `<tool-use-id>` and a `<status>`. A watcher is LIVE iff its launch id has no
    task-notification yet. This is a harness-generated record — untrusted CI-log
    text cannot forge it.
  * Was the PR handed off?  -> a `gh pr merge`/`close`, or a watcher
    `ready`/`closed` report. The report text only reaches the transcript when the
    session READS the watcher's own output file, so we trust a `ready`/`closed`
    marker only when it appears in a read of THAT file (path learned from the
    task-notification) — a fake marker in a CI-log excerpt cannot satisfy it.

We cannot verify check status locally (that needs a network call), so "checks
still pending" is approximated as "opened, not handed off, unwatched". The block
is safe under that approximation: it fires at most once and only asks the session
to launch the watcher, which then authoritatively determines check state (and
exits `ready` at once if the PR is already green).

Fail-open on ANY uncertainty: unparseable stdin, unreadable transcript, no
opened PR, a concluded PR, or a live watcher -> emit nothing (allow the stop). It
must never break a session. PR_SENTINEL_DEBUG=1 re-raises. PR_SENTINEL_DISABLE=1
disables it (parity with the PostToolUse nudge).

Reads the Stop hook JSON on stdin, emits a block decision on stdout (or nothing).
"""
import json
import os
import re
import sys

# A github.com PR URL, e.g. https://github.com/owner/repo/pull/123
PR_URL_RE = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)')

# A watcher launch inside a Bash command: `... pr-sentinel-watch.sh 42`.
WATCH_ARG_RE = re.compile(r'pr-sentinel-watch\.sh["\']?\s+(\S+)')

# A watcher terminal report that means "nothing left to babysit" for a PR.
CONCLUDED_EVENT_RE = re.compile(r'PR-SENTINEL EVENT:\s*(?:ready|closed)\b')

# Fields pulled out of a `<task-notification>` completion record.
NOTIF_TOOL_ID_RE = re.compile(r'<tool-use-id>\s*(toolu_[A-Za-z0-9]+)')
NOTIF_OUTFILE_RE = re.compile(r'<output-file>\s*([^<\s]+)')

# Cheap line pre-filter: only JSON-parse transcript lines that can carry a
# signal we care about. Everything else (the bulk of a session) is skipped.
_NEEDLES = ('pr-link', 'pr-sentinel-watch.sh', 'PR-SENTINEL EVENT',
            'task-notification', 'pr create', 'pr merge', 'pr close', '/pull/')


def pr_number(token):
    """Normalise a PR token (a bare number or a github.com PR URL) to its
    number string, or None if it is neither."""
    token = str(token).strip().strip('"\'')
    if token.isdigit():
        return token
    m = PR_URL_RE.search(token)
    return m.group(1) if m else None


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


def _notification_text(obj):
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


def _read_file_path(obj):
    """For a Read tool_result entry, the file path it read (or None)."""
    tur = obj.get('toolUseResult')
    if isinstance(tur, dict) and isinstance(tur.get('file'), dict):
        fp = tur['file'].get('filePath')
        if isinstance(fp, str):
            return fp
    return None


def prs_needing_watcher(path):
    """The set of PR numbers the session opened that are unconcluded AND have no
    live watcher — i.e. the PRs a stop should be blocked over. Fail-open:
    returns an empty set on any I/O trouble (allow the stop)."""
    created = set()
    concluded = set()
    launch_pr_by_toolid = {}   # watcher launch tool_use_id -> PR number
    completed_toolids = set()  # tool_use_ids with a task-notification (exited)
    outfile_by_toolid = {}     # watcher launch tool_use_id -> its output file
    reads = []                 # (file_path, text) for Read results
    create_ids = []            # tool_use_ids that ran `gh pr create`
    result_text = {}           # tool_use_id -> concatenated result text

    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                if not any(n in raw for n in _NEEDLES):
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue

                if obj.get('type') == 'pr-link':
                    num = pr_number(obj.get('prNumber', ''))
                    if num:
                        created.add(num)
                    continue

                notif = _notification_text(obj)
                if notif and '<status>' in notif:
                    tm = NOTIF_TOOL_ID_RE.search(notif)
                    if tm:
                        completed_toolids.add(tm.group(1))
                        om = NOTIF_OUTFILE_RE.search(notif)
                        if om:
                            outfile_by_toolid[tm.group(1)] = om.group(1).strip()
                    continue

                msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
                content = msg.get('content') if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        btype = b.get('type')
                        if btype == 'tool_use' and b.get('name') == 'Bash':
                            cmd = (b.get('input') or {}).get('command') or ''
                            if (b.get('input') or {}).get('run_in_background'):
                                for wm in WATCH_ARG_RE.finditer(cmd):
                                    num = pr_number(wm.group(1))
                                    if num:
                                        launch_pr_by_toolid[b.get('id')] = num
                            if _is_pr_create(cmd):
                                create_ids.append(b.get('id'))
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
        return set()

    # Opened PRs: the number gh printed in the create command's own output.
    for tid in create_ids:
        for m in PR_URL_RE.finditer(result_text.get(tid, '')):
            created.add(m.group(1))

    # Handed off: a watcher `ready`/`closed` report, trusted ONLY when read from
    # that watcher's own output file (path from the completion notification).
    outfile_pr = {outfile_by_toolid[t]: launch_pr_by_toolid[t]
                  for t in outfile_by_toolid if t in launch_pr_by_toolid}
    for fp, text in reads:
        pr = outfile_pr.get(fp)
        if pr and CONCLUDED_EVENT_RE.search(text):
            concluded.add(pr)

    # Live: a watcher launch whose task has not reported completion.
    live = {pr for tid, pr in launch_pr_by_toolid.items()
            if tid not in completed_toolids}

    return created - concluded - live


def watcher_command(pr):
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', '')
    watcher = os.path.join(plugin_root, 'scripts', 'pr-sentinel-watch.sh') \
        if plugin_root else 'scripts/pr-sentinel-watch.sh'
    return f'    bash "{watcher}" {pr}'


def build_reason(prs):
    """The block message fed back to the model."""
    prs = sorted(prs, key=int)
    label = 'pull request #' + prs[0] if len(prs) == 1 \
        else 'pull requests ' + ', '.join('#' + p for p in prs)
    commands = '\n'.join(watcher_command(p) for p in prs)
    return (
        f'pr-sentinel: you are ending your turn with an open {label} you opened '
        f'this session, but no watcher is tracking it and CI may still be '
        f'running. Launch the PR Sentinel watcher as a BACKGROUND task '
        f'(run_in_background) before you stop, so a CI failure or merge conflict '
        f'wakes this session — do NOT foreground-poll with `gh pr checks '
        f'--watch`, `gh run watch`, or a sleep loop. Command'
        f'{"s" if len(prs) > 1 else ""}:\n{commands}\n'
        f'When the watcher wakes you, act on the single reported event, push, '
        f'and relaunch it. If you have already handed this PR to a human for '
        f'merge review, you may stop. Never auto-merge.'
    )


def main():
    if os.environ.get('PR_SENTINEL_DISABLE') == '1':
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: allow the stop
    if not isinstance(data, dict):
        return
    # Never block a stop that is itself a continuation of a prior stop-hook
    # block — this is the no-loop guarantee.
    if data.get('stop_hook_active'):
        return

    transcript = data.get('transcript_path')
    if not transcript:
        return
    unwatched = prs_needing_watcher(transcript)
    if not unwatched:
        return  # no opened-and-unwatched PR: allow

    print(json.dumps({'decision': 'block', 'reason': build_reason(unwatched)}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PR_SENTINEL_DEBUG') == '1':
            raise
        sys.exit(0)
