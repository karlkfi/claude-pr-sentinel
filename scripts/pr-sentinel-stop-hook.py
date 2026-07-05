#!/usr/bin/env python3
"""Stop hook: the backstop that makes the advisory PostToolUse nudge reliable.

When a session ends its turn having opened a pull request it has not concluded,
with no live watcher tracking it, this hook BLOCKS the stop ONCE and tells the
session to launch the pr-sentinel background watcher before stopping. It respects
`stop_hook_active` so it can never loop: a stop that is itself the continuation
of a prior stop-hook block is allowed straight through.

Two open problems, solved WITHOUT a network call and WITHOUT reading the PR body
or any comment stream (the excluded injection channel — see docs/DESIGN.md):

  * Is a watcher running?  -> enumerate local processes (`ps`) for a running
    `pr-sentinel-watch.sh <PR>`. A watcher that exited (delivered its event)
    correctly reads as NOT live. `ps` failing -> we can't confirm -> fail-open.
  * Which PR did the session open?  -> parse the local transcript JSONL: the
    harness's own `pr-link` records (a canonical `prNumber`/`prUrl` marker) and,
    as a fallback, the session's own `gh pr create` correlated with the PR URL
    `gh` printed. Both are GitHub-controlled metadata the session already
    surfaced; we never touch the PR body or comments.

We cannot verify check status locally (that needs a network call), so "checks
still pending" is approximated as "created, not known-concluded, unwatched". The
block is safe under that approximation: it fires at most once and only asks the
session to launch the watcher, which then authoritatively determines check state
(and exits `ready` at once if the PR is already green).

Fail-open on ANY uncertainty: unparseable stdin, unreadable transcript, no
created PR, a concluded PR, a live watcher, or `ps` unavailable -> emit nothing
(allow the stop). It must never break a session. PR_SENTINEL_DEBUG=1 re-raises.
PR_SENTINEL_DISABLE=1 disables it (parity with the PostToolUse nudge).

Reads the Stop hook JSON on stdin, emits a block decision on stdout (or nothing).
"""
import json
import os
import re
import subprocess
import sys

# A github.com PR URL, e.g. https://github.com/owner/repo/pull/123
PR_URL_RE = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)')

# A watcher launch on a process command line: `... pr-sentinel-watch.sh 42`.
WATCH_ARG_RE = re.compile(r'pr-sentinel-watch\.sh["\']?\s+(\S+)')

# A watcher terminal report that means "nothing left to babysit" for a PR.
CONCLUDED_EVENT_RE = re.compile(r'PR-SENTINEL EVENT:\s*(ready|closed)\b')
# The `PR: <ref>` line the watcher prints right under the event marker.
PR_LINE_RE = re.compile(r'^PR:\s*(\S+)', re.MULTILINE)


def pr_number(token):
    """Normalise a PR token (a bare number or a github.com PR URL) to its
    number string, or None if it is neither."""
    token = token.strip().strip('"\'')
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
        if 'text' in block and isinstance(block['text'], str):
            yield block['text']
        inner = block.get('content')
        if isinstance(inner, str):
            yield inner
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict) and isinstance(sub.get('text'), str):
                    yield sub['text']


def _tool_use_result_text(obj):
    """Text from an entry's structured `toolUseResult` (Bash stdout/stderr),
    where background-task output and command output are also surfaced."""
    tur = obj.get('toolUseResult')
    if not isinstance(tur, dict):
        return ''
    parts = [tur[k] for k in ('stdout', 'stderr') if isinstance(tur.get(k), str)]
    return '\n'.join(parts)


def parse_transcript(path):
    """Read the Stop-hook transcript JSONL and return the set of PR numbers the
    session opened that are NOT yet concluded (no watcher ready/closed report,
    no `gh pr merge`/`close`).

    Two 'opened' signals, unioned for robustness: (1) the harness's own
    `pr-link` entries (a canonical record carrying `prNumber`/`prUrl`), and
    (2) a `gh pr create` correlated with the PR URL that command printed. Both
    are GitHub-controlled metadata the session already surfaced — never the PR
    body or comments. Fail-open: returns an empty set on any I/O trouble."""
    create_ids = []          # tool_use ids that ran `gh pr create`
    result_text = {}         # tool_use_id -> concatenated result text
    created = set()
    concluded = set()
    all_text = []            # loose scan corpus for event markers

    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                # The harness's canonical per-session PR record.
                if obj.get('type') == 'pr-link':
                    num = pr_number(str(obj.get('prNumber', '')))
                    if num:
                        created.add(num)
                msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
                content = msg.get('content') if isinstance(msg, dict) else None
                entry_tids = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get('type')
                        if btype == 'tool_use' and block.get('name') == 'Bash':
                            cmd = (block.get('input') or {}).get('command') or ''
                            if _is_pr_create(cmd):
                                create_ids.append(block.get('id'))
                            concluded |= _pr_close_targets(cmd)
                        elif btype == 'tool_result':
                            tid = block.get('tool_use_id')
                            text = '\n'.join(_block_texts(block.get('content')))
                            if tid is not None:
                                entry_tids.append(tid)
                                result_text[tid] = result_text.get(tid, '') + '\n' + text
                # Structured Bash stdout/stderr is attached to this entry's
                # tool_result id(s), and also feeds the event-marker scan.
                tur_text = _tool_use_result_text(obj)
                if tur_text:
                    for tid in entry_tids:
                        result_text[tid] = result_text.get(tid, '') + '\n' + tur_text
                    all_text.append(tur_text)
                # Collect all text (result blocks, background-task output, etc.)
                # for the loose concluded-event scan.
                for text in _block_texts(content):
                    all_text.append(text)
    except OSError:
        return set()

    # Created PRs: the number gh printed in the create command's own output.
    for tid in create_ids:
        for m in PR_URL_RE.finditer(result_text.get(tid, '')):
            created.add(m.group(1))

    # Concluded PRs: watcher ready/closed reports anywhere in the transcript.
    corpus = '\n'.join(all_text)
    for m in CONCLUDED_EVENT_RE.finditer(corpus):
        # The `PR: <ref>` line sits a couple of lines below the event marker.
        window = corpus[m.end():m.end() + 200]
        pm = PR_LINE_RE.search(window)
        if pm:
            num = pr_number(pm.group(1))
            if num:
                concluded.add(num)

    return created - concluded


def watcher_prs_from_ps(ps_output):
    """Parse `ps` output into the set of PR numbers that have a live
    `pr-sentinel-watch.sh <PR>` process."""
    prs = set()
    for line in ps_output.splitlines():
        for m in WATCH_ARG_RE.finditer(line):
            num = pr_number(m.group(1))
            if num:
                prs.add(num)
    return prs


# `-ww` disables command-line truncation (BSD/macOS + GNU/Linux); `ax` is the
# portable "all processes, full args" fallback if `-o` is unsupported.
_PS_INVOCATIONS = (['ps', '-A', '-ww', '-o', 'args='], ['ps', 'ax'])


def running_watcher_prs():
    """PR numbers with a live watcher process, or None if we cannot tell (no
    `ps`, or it errored) — the caller treats None as 'unknown -> allow'."""
    for argv in _PS_INVOCATIONS:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return watcher_prs_from_ps(proc.stdout)
    return None


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
    active = parse_transcript(transcript)
    if not active:
        return  # no created-and-unconcluded PR: allow

    running = running_watcher_prs()
    if running is None:
        return  # can't confirm watcher liveness: fail-open, allow
    unwatched = active - running
    if not unwatched:
        return  # every active PR already has a live watcher: allow

    reason = build_reason(unwatched)
    print(json.dumps({'decision': 'block', 'reason': reason}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PR_SENTINEL_DEBUG') == '1':
            raise
        sys.exit(0)
