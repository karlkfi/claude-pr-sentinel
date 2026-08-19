#!/usr/bin/env python3
"""Report pr-sentinel's activity and friction, from local session transcripts.

Read-only analyzer. The plugin writes no telemetry (see PRIVACY.md): it emits
nudges on stdout and watcher reports into background-task output, and Claude
Code already records both — plus the triggering command, cwd, and timestamp —
in ``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and
answers the three questions the roadmap's R3 asks:

  1. how often the advisory nudge fired;
  2. how often a watcher was actually launched in response (follow-through);
  3. which watcher events dominated, and so where CI time is spent.

Nothing here changes the plugin or adds collection: it parses data Claude Code
already persisted locally.

Usage:
    python3 scripts/friction-report.py                  # last 7 days
    python3 scripts/friction-report.py --since all
    python3 scripts/friction-report.py --since 2026-08-01 --repo gateway
    python3 scripts/friction-report.py --json           # machine-readable

Three record shapes carry the signal, and each is anchored so the plugin's own
source, tests, and fixtures are not counted as usage — this repository's
transcripts are full of all three, and a substring match reports development
noise as activity:

  nudge    an ``attachment`` of type ``hook_success`` whose ``hookName`` is
           ``PostToolUse:Bash`` and whose ``stdout`` parses to an
           ``additionalContext`` beginning with ``pr-sentinel:``.
  launch   an ``assistant`` Bash ``tool_use`` whose command *invokes* the
           watcher (``bash "<path>/pr-sentinel-watch.sh" <target>``). Matching
           the bare filename instead counts every ``grep``, ``cat`` and
           ``git diff`` that merely names the script: measured over the local
           corpus, that reads 2558 launches where 2372 happened, and inflates
           the foreground-launch count from 22 to 207.
  event    a watcher report inside a ``user`` ``tool_result`` — a line reading
           ``PR-SENTINEL EVENT: <name>`` followed by its ``PR: <number>`` line.

A backgrounded watcher's report does **not** come back through the launch's own
tool_result, so there is no ``tool_use_id`` join to be had: the harness hands it
over as a task-output file the session then reads (``Read``, ``cat``, or
``TaskOutput``). Measured over the local corpus, 7 of 2054 reports join to their
launch and 2047 do not. Reports are therefore matched wherever they surface and
deduplicated per session on (event, PR, report body), which collapses the same
file being read twice — 352 of those 2054, 17% — without merging two genuinely
distinct runs.
"""
import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys

# The watcher emits exactly one header line per event, `report_header <name>`.
# Grouping them by what the event asks of the session is what turns a raw tally
# into "where does the time go": `work` events are the ones the plugin exists to
# catch, and a run dominated by `degraded` means the watch is losing to its own
# budget rather than reporting on CI.
#
# This mapping is the report's contract with the watcher, and
# test_event_kinds_cover_every_watcher_event fails the build when the watcher
# gains an event this file does not classify.
EVENT_KIND = {
    'check_failure': 'work',      # CI failed — the session has a fix to push
    'conflict':      'work',      # needs a base merge
    'behind':        'work',      # same fix, before it becomes a conflict
    'dequeued':      'work',      # left the merge queue, re-enqueue owed
    'blocked':       'work',      # green but a merge requirement is unmet
    'ready':         'done',      # green and mergeable — hand back
    'closed':        'done',      # merged or closed
    'timeout':       'degraded',  # watch budget elapsed with no verdict
    'error':         'degraded',  # gh unreachable after retries
    'base_failure':  'notice',    # inherited from the base; watch continues
    'ready_watching': 'notice',   # green, still watching for a sibling merge
    'blocked_watching': 'notice',
}

KIND_HINT = {
    'work':     'the watcher caught something — the plugin earning its keep',
    'done':     'terminal: the PR needed no further babysitting',
    'degraded': 'the watch ended with no verdict — consider a longer budget',
    'notice':   'reported without exiting; the watch continued',
}

# The nudge's two openings, from build_context() in pr-sentinel-hook.py.
NUDGE_PREFIX = 'pr-sentinel:'
NUDGE_CREATE = re.compile(r'^pr-sentinel: You just opened pull request #(\d+)')
# Every nudge spells the launch out as `bash "<watcher>" <target>`; the target is
# a bare PR number, or a placeholder when the push path could not resolve one.
NUDGE_TARGET = re.compile(r'bash "[^"]*pr-sentinel-watch\.sh" (\S+)')

# The Stop hook's backstop block — the authoritative record of a nudge that was
# NOT followed, since it fires only on an open PR with no live watcher. R3
# predates the Stop hook (R1); without this line the follow-through rate has
# no independent check.
STOP_BLOCK = re.compile(r'^pr-sentinel: you are ending your turn')

# An actual invocation, not a mention. Anchored on `bash` so a grep, cat, find
# or heredoc naming the script is not counted as a launch.
LAUNCH = re.compile(
    r'(?:^|[\s;&|(])bash\s+"?([^"\s]*pr-sentinel-watch\.sh)"?\s+(\S+)')

# A `Read` renders the task-output file with `<lineno>\t` prefixes; a `cat`
# does not. Strip either shape before matching a report line.
LINE_NO = re.compile(r'^\s*\d+\t')
EVENT_HEADER = re.compile(r'^PR-SENTINEL EVENT:\s*([a-z_]+)\s*$')
EVENT_PR = re.compile(r'^PR:\s*(\d+)\s*$')
# How far past a header the body hash reaches. Reports are far shorter; this
# only bounds the work done on a pathological line.
REPORT_LINES = 40


def parse_since(spec):
    """Return a tz-aware UTC cutoff datetime, or None. Accepts Nd/Nh/Nm or a
    YYYY-MM-DD date."""
    if not spec:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    m = re.fullmatch(r'(\d+)([dhm])', spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {'d': dt.timedelta(days=n),
                 'h': dt.timedelta(hours=n),
                 'm': dt.timedelta(minutes=n)}[unit]
        return now - delta
    try:
        d = dt.datetime.strptime(spec, '%Y-%m-%d')
        return d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        sys.exit("--since: expected Nd/Nh/Nm or YYYY-MM-DD, got %r" % spec)


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def blocks(rec):
    """The content blocks of a record's message, or an empty list."""
    msg = rec.get('message')
    if not isinstance(msg, dict):
        return []
    content = msg.get('content')
    return content if isinstance(content, list) else []


def result_text(block):
    """The text of a tool_result block. Content is a bare string or a list of
    typed blocks depending on how the harness recorded it; both reach here."""
    content = block.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(c.get('text', '') for c in content
                       if isinstance(c, dict) and c.get('type') == 'text')
    return ''


def find_reports(text):
    """Yield (event, pr, body_hash) for each watcher report in `text`.

    A report is a `PR-SENTINEL EVENT: <name>` line immediately followed by its
    `PR: <number>` line. Requiring the pair is what separates a real report from
    the watcher's own source, where the header is `report_header`'s `$1`, and
    from a test asserting on the string, where the marker sits mid-line inside
    Python rather than at the start of one.
    """
    lines = [LINE_NO.sub('', l) for l in text.split('\n')]
    for i, line in enumerate(lines):
        m = EVENT_HEADER.match(line)
        if not m or i + 1 >= len(lines):
            continue
        pr = EVENT_PR.match(lines[i + 1])
        if not pr:
            continue
        body = []
        for j in range(i, min(len(lines), i + REPORT_LINES)):
            if j > i and EVENT_HEADER.match(lines[j]):
                break
            body.append(lines[j].rstrip())
        digest = hashlib.sha1('\n'.join(body).encode('utf-8')).hexdigest()
        yield m.group(1), pr.group(1), digest


def nudge_context(rec):
    """The pr-sentinel nudge carried by a PostToolUse attachment, or None.

    Anchored on the hook's own prefix so another plugin's PostToolUse output,
    recorded in exactly this shape, is not counted as ours.
    """
    att = rec.get('attachment')
    if not isinstance(att, dict) or att.get('hookName') != 'PostToolUse:Bash':
        return None
    stdout = att.get('stdout') or ''
    if NUDGE_PREFIX not in stdout:
        return None
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    ctx = ((out.get('hookSpecificOutput') or {}).get('additionalContext') or '')
    return ctx if ctx.startswith(NUDGE_PREFIX) else None


def stop_block_count(rec):
    """How many pr-sentinel backstop blocks this stop_hook_summary carries."""
    if rec.get('subtype') != 'stop_hook_summary':
        return 0
    errors = rec.get('hookErrors')
    if not isinstance(errors, list):
        return 0
    return sum(1 for e in errors if isinstance(e, str) and STOP_BLOCK.match(e))


def scan(path):
    """Read one transcript and return its nudges, launches, events and blocks.

    Reports are deduplicated within the file: a session that reads a watcher's
    output more than once records the same bytes each time.
    """
    nudges, launches, events, stop_blocks = [], [], [], []
    seen = set()
    try:
        fh = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return nudges, launches, events, stop_blocks
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rtype = rec.get('type')
            ts, cwd = parse_ts(rec), rec.get('cwd') or ''

            if rtype == 'attachment':
                ctx = nudge_context(rec)
                if ctx is None:
                    continue
                m = NUDGE_TARGET.search(ctx)
                target = m.group(1) if m else ''
                nudges.append({
                    'kind': 'create' if NUDGE_CREATE.match(ctx) else 'push',
                    'pr': target if target.isdigit() else None,
                    'ts': ts, 'cwd': cwd,
                })
            elif rtype == 'assistant':
                for b in blocks(rec):
                    if not (isinstance(b, dict) and b.get('type') == 'tool_use'
                            and b.get('name') == 'Bash'):
                        continue
                    inp = b.get('input') or {}
                    m = LAUNCH.search(inp.get('command') or '')
                    if not m:
                        continue
                    target = m.group(2).strip('"')
                    launches.append({
                        'pr': target if target.isdigit() else None,
                        'background': bool(inp.get('run_in_background')),
                        'ts': ts, 'cwd': cwd,
                    })
            elif rtype == 'user':
                for b in blocks(rec):
                    if not (isinstance(b, dict)
                            and b.get('type') == 'tool_result'):
                        continue
                    for event, pr, digest in find_reports(result_text(b)):
                        key = (event, pr, digest)
                        if key in seen:
                            continue
                        seen.add(key)
                        events.append({'event': event, 'pr': pr,
                                       'ts': ts, 'cwd': cwd})
            elif rtype == 'system':
                n = stop_block_count(rec)
                if n:
                    stop_blocks.append({'n': n, 'ts': ts, 'cwd': cwd})
    return nudges, launches, events, stop_blocks


def keep(item, cutoff, repo):
    if repo and repo not in (item.get('cwd') or ''):
        return False
    ts = item.get('ts')
    if cutoff and ts and ts < cutoff:
        return False
    return True


def collect(paths, cutoff, repo):
    """Per-file scan, filtered. Follow-through is computed per file, because a
    nudge is answered by a launch in the session that received it."""
    out = {'nudges': [], 'launches': [], 'events': [], 'stop_blocks': 0,
           'sessions': 0, 'answered': 0, 'unanswered': 0, 'unresolved': 0,
           'repos': collections.Counter()}
    for path in paths:
        nudges, launches, events, stop_blocks = scan(path)
        nudges = [n for n in nudges if keep(n, cutoff, repo)]
        launches = [l for l in launches if keep(l, cutoff, repo)]
        events = [e for e in events if keep(e, cutoff, repo)]
        here = [s for s in stop_blocks if keep(s, cutoff, repo)]
        if not (nudges or launches or events or here):
            continue
        out['sessions'] += 1
        out['nudges'] += nudges
        out['launches'] += launches
        out['events'] += events
        out['stop_blocks'] += sum(s['n'] for s in here)

        # Follow-through, per (session, PR): a nudge naming a PR is answered if
        # this session ever launched a watcher on that PR. A nudge whose PR the
        # hook could not resolve is counted apart rather than scored, since
        # nothing ties it to a particular launch.
        launched = set(l['pr'] for l in launches if l['pr'])
        nudged = set(n['pr'] for n in nudges if n['pr'])
        out['unresolved'] += sum(1 for n in nudges if not n['pr'])
        out['answered'] += len(nudged & launched)
        out['unanswered'] += len(nudged - launched)
        for item in events:
            repo_name = os.path.basename(item.get('cwd') or '') or '(unknown)'
            out['repos'][repo_name] += 1
    return out


def build_report(c):
    nudges = collections.Counter(n['kind'] for n in c['nudges'])
    events = collections.Counter(e['event'] for e in c['events'])
    kinds = collections.Counter()
    for name, n in events.items():
        kinds[EVENT_KIND.get(name, 'unknown')] += n
    foreground = sum(1 for l in c['launches'] if not l['background'])
    paired = c['answered'] + c['unanswered']
    return {
        'sessions': c['sessions'],
        'nudges': nudges,
        'nudges_total': len(c['nudges']),
        'nudges_unresolved': c['unresolved'],
        'launches': len(c['launches']),
        'launches_foreground': foreground,
        'answered': c['answered'],
        'unanswered': c['unanswered'],
        'paired': paired,
        'follow_through': (100.0 * c['answered'] / paired) if paired else None,
        'stop_blocks': c['stop_blocks'],
        'events': events,
        'events_total': sum(events.values()),
        'kinds': kinds,
        'repos': c['repos'],
    }


def print_text(r, top):
    if not (r['nudges_total'] or r['launches'] or r['events_total']):
        print("No pr-sentinel activity found for the given filters.")
        return
    print("pr-sentinel activity across %d session(s)" % r['sessions'])
    print()

    print("Nudges fired: %d" % r['nudges_total'])
    for kind, n in r['nudges'].most_common():
        where = 'on `gh pr create`' if kind == 'create' \
            else 'on a branch `git push`'
        print("  %5d  %s" % (n, where))
    if r['nudges_unresolved']:
        print("  %5d  named no PR number (push with no resolvable PR)"
              % r['nudges_unresolved'])
    print()

    print("Watchers launched: %d" % r['launches'])
    if r['launches_foreground']:
        print("  %5d  NOT backgrounded — these pinned the main thread"
              % r['launches_foreground'])
    print()

    if r['follow_through'] is None:
        print("Follow-through: no nudge named a PR, so none can be scored.")
    else:
        print("Follow-through: %.0f%% (%d of %d nudged PRs got a watcher)"
              % (r['follow_through'], r['answered'], r['paired']))
        if r['unanswered']:
            print("  %5d  nudged PRs never got one" % r['unanswered'])
    if r['stop_blocks']:
        print("  %5d  Stop-hook backstop blocks — a turn ended with an open,"
              % r['stop_blocks'])
        print("         unwatched PR and the hook had to intervene")
    print()

    if r['events']:
        print("Watcher events — what actually woke sessions (%d):"
              % r['events_total'])
        for name, n in r['events'].most_common():
            print("  %5d  %-18s %s" % (n, name,
                                       EVENT_KIND.get(name, 'unknown')))
        print()
        print("By kind:")
        for kind, n in r['kinds'].most_common():
            print("  %5d  %-9s %s" % (n, kind, KIND_HINT.get(kind, '')))
        print()

    if r['repos']:
        print("Events by repository (top %d):" % top)
        for name, n in r['repos'].most_common(top):
            print("  %5d  %s" % (n, name))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--transcripts',
                    default=os.path.expanduser('~/.claude/projects'),
                    help='transcript root (default: ~/.claude/projects)')
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "use 'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only activity whose cwd contains this substring')
    ap.add_argument('--top', type=int, default=15, help='rows per ranking')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    cutoff = None if args.since == 'all' else parse_since(args.since)
    paths = glob.glob(os.path.join(args.transcripts, '**', '*.jsonl'),
                      recursive=True)
    if not paths:
        sys.exit("No transcripts under %s" % args.transcripts)

    report = build_report(collect(paths, cutoff, args.repo))

    if args.json:
        print(json.dumps({
            'sessions': report['sessions'],
            'nudges': dict(report['nudges']),
            'nudges_total': report['nudges_total'],
            'nudges_unresolved': report['nudges_unresolved'],
            'launches': report['launches'],
            'launches_foreground': report['launches_foreground'],
            'answered': report['answered'],
            'unanswered': report['unanswered'],
            'follow_through': report['follow_through'],
            'stop_blocks': report['stop_blocks'],
            'events': dict(report['events']),
            'events_total': report['events_total'],
            'kinds': dict(report['kinds']),
            'top_repos': report['repos'].most_common(args.top),
        }, indent=2))
    else:
        print_text(report, args.top)


if __name__ == '__main__':
    main()
