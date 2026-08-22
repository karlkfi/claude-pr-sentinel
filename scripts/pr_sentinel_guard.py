#!/usr/bin/env python3
"""PreToolUse hook: DENY foreground CI-poll commands and ALLOW the plugin's own
background watcher launch, pointing the session at the watcher either way.

Fires on a `Bash` command the session is *about to run*. Four branches:

* **Duplicate deny** — if the command is this plugin's own watcher launch for a
  PR this session already has a live watcher on, it returns a `deny` naming the
  running task. One watcher per PR is enough: it re-reads the PR head on every
  poll, so it covers the latest push, and a second one wakes the session twice
  for every event. The deny names the `TaskStop` call that stops the incumbent,
  so restarting the watch stays available. Off under `PR_SENTINEL_DISABLE=1`
  and under the `PR_SENTINEL_OVERRIDE` escape hatch; an unreadable transcript
  or an unrecognised launch shape defers, never denies.

* **Auto-allow** — if the command is *unambiguously* this plugin's own watcher
  launch (`bash <own-watcher> <PR>`), it returns a PreToolUse `allow` so the
  session isn't prompted by the base Bash permission on every (re)launch. The
  match is airtight and fail-safe: a single simple command, no operators /
  redirects / substitutions / globs, `argv[1]` resolving (via realpath) to this
  plugin's own `pr-sentinel-watch.sh`, and `argv[2]` a bare positive integer or
  a `https://github.com/<owner>/<repo>/pull/<n>` URL — the two forms the watcher
  itself accepts.
  ANY doubt -> defer (emit nothing), never allow. Gated by
  `PR_SENTINEL_AUTOALLOW` (default on; `0`/`false`/empty disables) and off when
  `PR_SENTINEL_DISABLE=1`.

* **Deny** — if the command is a blocking foreground poll (`gh pr checks
  --watch`, `gh run watch`, or a `while/until … sleep …` loop that polls `gh`),
  it returns a PreToolUse `deny` whose reason points at the watcher. The deny is
  UNIFORM across permission modes: a hard `deny` (never `ask`), so a
  `bypassPermissions`/headless run self-corrects instead of stalling on an
  unanswerable prompt.

  The deny is scoped to the harm it names. A call submitted with
  `run_in_background` cannot block the session or burn idle tokens, so it is
  never denied — backgrounding IS the fix, and it's the only one available for a
  run with no PR to watch (a tag-triggered release build). A poll loop is denied
  only when it polls GitHub via `gh`; a loop around `curl` against an unrelated
  host isn't CI polling under any reading, and this hook has no business
  refusing it.

* **Overlap deny** — if the command is a `gh pr create` and this branch edits
  lines an already-open PR also changes, it returns a `deny` naming that PR and
  the shared paths. Two branches rewriting the same function is duplicated or
  mutually invalidating work, and review is an expensive place to discover it.
  Compared as LINE RANGES, not paths, so sharing a file is not a finding; see
  `pr_sentinel_overlap`. Gated by `PR_SENTINEL_OVERLAP_ENABLED` (default on;
  `0`/`false` disables) and off under `PR_SENTINEL_DISABLE=1`. Unlike the poll
  deny this one applies to a backgrounded call too — backgrounding a create
  still opens the PR, so it is not the fix here.

Escape hatch: `PR_SENTINEL_OVERRIDE=<reason>` (any non-empty value) downgrades
the deny — the hook defers, letting the command proceed under the normal
permission system — for the rare legitimate one-off. It is honoured as an
INLINE prefix on the command itself (`PR_SENTINEL_OVERRIDE=why gh run watch 5`)
as well as in the hook's own environment; the inline form is the only one a
session can reach from inside a Bash call, and it's the form the deny message
names. This mirrors prod-guard's `PROD_GUARD_OVERRIDE`.

Locality: every branch but the overlap deny is purely local — the proposed
command string plus the session transcript, for the watchers this session
launched. The overlap deny is the one exception and asks GitHub, through the
`gh` the session is already authenticated to, for the open PRs and the diffs of
the few that share a path. It reads code, never prose: no PR body, no comments,
no review text, not even the PR title. See `PRIVACY.md`.

Fail modes: defers silently (emits nothing) on ANY uncertainty — non-Bash tool,
unparseable command/input, a shape it doesn't recognise. It NEVER denies a
command it isn't sure about, and it can never break a session.
`PR_SENTINEL_DEBUG=1` re-raises for debugging.

Reached from `scripts/pr-sentinel.py`, which reads the hook JSON on stdin and
calls `run()` with it; this module emits a PreToolUse decision on stdout.
"""
import json
import os
import re
import shlex
import sys

# A second watcher on a PR one is already watching wakes the session twice for
# every event and polls GitHub twice as often. The live-watcher read is the
# same one the Stop hook uses; the path insert makes the sibling import work
# whether this file is run as a script or loaded by path (as the tests load it).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_sentinel_watchers as watchers   # noqa: E402
import pr_sentinel_overlap as overlap     # noqa: E402

# Shell keywords that can lead a simple-command group but aren't the command
# word itself (e.g. `do sleep 5`). Stripped before reading the leading word.
_LEADING_KEYWORDS = ('do', 'then', 'else', '{', '(', '!')

# Any of these in the raw command string means it is NOT a single simple
# command we can safely auto-allow: command separators / operators (`;` `|`
# `&`), redirects (`<` `>`), command/parameter substitution (`$` backtick),
# subshell / process substitution (`(` `)`), brace expansion (`{` `}`), a
# backslash escape, or globs (`*` `?` `[`). Presence of any -> defer, never
# allow. (Newlines are covered by the separators too.) Quotes are allowed so
# the nudge's `bash "<path>" N` form matches.
_AUTOALLOW_FORBIDDEN = set(';|&<>$`()*?[]{}\\\n\r')

# The other identifier the watcher accepts: a github.com PR URL. Anchored, so
# it validates a whole argument rather than finding one inside a longer string
# — this is an allow decision, so the match has to be exact.
_WATCHER_PR_URL_RE = re.compile(
    r'\Ahttps://github\.com/[^/\s]+/[^/\s]+/pull/([1-9][0-9]*)/?\Z')


def _autoallow_enabled():
    """Whether the watcher-launch auto-allow is active. On by default; off when
    `PR_SENTINEL_AUTOALLOW` is `0`/`false`/empty, or the plugin is disabled via
    `PR_SENTINEL_DISABLE=1` (disabled plugin -> no auto-allow)."""
    if os.environ.get('PR_SENTINEL_DISABLE') == '1':
        return False
    val = os.environ.get('PR_SENTINEL_AUTOALLOW')
    if val is None:
        return True  # default on
    return val.strip().lower() not in ('', '0', 'false')


def _expected_watcher_path():
    """The realpath of THIS plugin's own watcher script, derived from the hook's
    own location (`<root>/scripts/pr_sentinel_guard.py`) or `CLAUDE_PLUGIN_ROOT`.
    No version to hardcode -> upgrade-proof. None on any resolution failure."""
    root = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if root and root.strip():
        scripts_dir = os.path.join(root, 'scripts')
    else:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        return os.path.realpath(
            os.path.join(scripts_dir, 'pr-sentinel-watch.sh'))
    except OSError:
        return None


def watcher_launch_pr(command):
    """The PR number if `command` is unambiguously `bash <own-watcher> <PR>`:

      * no shell operator / redirect / substitution / glob (`_AUTOALLOW_FORBIDDEN`)
      * exactly three tokens, `argv[0]` basename `bash`
      * `argv[1]` realpath-equals this plugin's own watcher script
      * `argv[2]` a bare positive integer, or a github.com PR URL

    Both identifier forms normalise to the number, so a URL launch and a bare
    number for the same PR are one PR to the duplicate check.

    Any doubt returns None so the caller defers rather than allowing — or, for
    the duplicate check, rather than denying."""
    if any(c in _AUTOALLOW_FORBIDDEN for c in command):
        return None
    try:
        argv = shlex.split(command)  # posix; respects quotes
    except ValueError:
        return None
    if len(argv) != 3:
        return None
    if os.path.basename(argv[0]) != 'bash':
        return None
    if re.match(r'\A[1-9][0-9]*\Z', argv[2]):
        pr = argv[2]
    else:
        m = _WATCHER_PR_URL_RE.match(argv[2])
        if m is None:
            return None
        pr = m.group(1)
    expected = _expected_watcher_path()
    if expected is None:
        return None
    try:
        if os.path.realpath(argv[1]) != expected:
            return None
    except OSError:
        return None
    return pr


def is_watcher_launch(command):
    """True if `command` is this plugin's own watcher launch."""
    return watcher_launch_pr(command) is not None


def simple_commands(command):
    """Split a bash command string into simple commands on the shell operators
    that separate them (`&&`, `||`, `|`, `;`, `(`, `)`, newlines). Best-effort:
    on a tokenizing failure return [] so the caller defers rather than crashes.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=';()<>|&\n')
        # `\n` in punctuation_chars is not enough on its own: shlex's default
        # `whitespace` also holds `\n` and consumes it before the punctuation
        # rule is consulted, so the separator vanishes and two simple commands
        # are handed back glued into one. Dropping it from `whitespace` is what
        # makes the newline in punctuation_chars actually separate.
        lex.whitespace = lex.whitespace.replace('\n', '')
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


_ASSIGNMENT_RE = re.compile(r'\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z', re.S)


def _strip_env_prefix(argv):
    """Drop leading NAME=VALUE assignments so `GH_TOKEN=x gh run watch` still
    resolves to `gh`."""
    i = 0
    while i < len(argv) and _ASSIGNMENT_RE.match(argv[i]):
        i += 1
    return argv[i:]


def inline_override(command):
    """The reason from an inline `PR_SENTINEL_OVERRIDE=<reason>` prefix on any
    simple command in `command`, or ''. Covers the prefix on the poll itself
    (`PR_SENTINEL_OVERRIDE=why gh run watch 5`) and on a later link of a chain
    (`mkdir -p out && PR_SENTINEL_OVERRIDE=why gh run watch 5 > out/x`).

    A session cannot set a variable in THIS hook's environment from inside a
    Bash call, so the inline prefix is the only reachable form of the escape
    hatch — and the one `build_reason` names. Only a real leading assignment
    counts: the name as an argument (`echo PR_SENTINEL_OVERRIDE=x && gh run
    watch`) does not, and neither does an empty value."""
    for group in simple_commands(command):
        argv = list(group)
        while argv:
            m = _ASSIGNMENT_RE.match(argv[0])
            if m:
                if m.group(1) == 'PR_SENTINEL_OVERRIDE' and m.group(2).strip():
                    return m.group(2)
                argv = argv[1:]
            elif argv[0] in _LEADING_KEYWORDS:
                argv = argv[1:]
            else:
                break
    return ''


def _leading_word(group):
    """The command word of a simple-command group, after stripping leading env
    assignments and leading shell keywords like `do`/`then`. '' if none."""
    argv = _strip_env_prefix(list(group))
    while argv and argv[0] in _LEADING_KEYWORDS:
        argv = argv[1:]
    argv = _strip_env_prefix(argv)
    return os.path.basename(argv[0]) if argv else ''


def _is_gh_pr_checks_watch(group):
    argv = _strip_env_prefix(list(group))
    if not argv or os.path.basename(argv[0]) != 'gh':
        return False
    rest = argv[1:]
    non_flags = [a for a in rest if not a.startswith('-')]
    if non_flags[:2] != ['pr', 'checks']:
        return False
    return '--watch' in rest or '-w' in rest


def _is_gh_run_watch(group):
    argv = _strip_env_prefix(list(group))
    if not argv or os.path.basename(argv[0]) != 'gh':
        return False
    rest = argv[1:]
    non_flags = [a for a in rest if not a.startswith('-')]
    return non_flags[:2] == ['run', 'watch']


# `gh pr create --help` asks what the command does; it opens nothing, so there
# is no PR to overlap with.
_PR_CREATE_PROBE_FLAGS = frozenset({'--help', '-h'})


def _is_gh_pr_create(group):
    argv = _strip_env_prefix(list(group))
    if not argv or os.path.basename(argv[0]) != 'gh':
        return False
    rest = argv[1:]
    non_flags = [a for a in rest if not a.startswith('-')]
    if non_flags[:2] != ['pr', 'create']:
        return False
    return _PR_CREATE_PROBE_FLAGS.isdisjoint(rest)


def is_pr_create(command):
    """Whether `command` opens a pull request. Matched on a simple command's
    own leading word, so a `gh pr create` inside a quoted commit message is not
    one. A heredoc body is not tracked, so a line inside one that leads with
    `gh pr create` does read as a simple command — that errs toward denying."""
    return any(_is_gh_pr_create(group) for group in simple_commands(command))


# `gh` in command position: at the start, after whitespace or a shell operator,
# or opening a substitution (`$(gh …)`, `` `gh …` ``) — which the tokenizer
# hands back as one opaque token, so this is matched on the raw string. An
# optional path prefix covers `/usr/local/bin/gh`.
_GH_COMMAND_RE = re.compile(r'(?:\A|[\s;|&(`!])(?:[^\s;|&(`]*/)?gh\s')


def polls_gh(command):
    """Whether `command` invokes `gh` anywhere — the only CI a poll loop can be
    watching, since this plugin reads GitHub through `gh` and nothing else. A
    loop around some other subject is not this hook's business."""
    return bool(_GH_COMMAND_RE.search(command))


def is_backgrounded(tool_input):
    """Whether the Bash call was submitted with `run_in_background` — the
    harness's own signal that it will not block the session. Any truthy value
    counts; an unrecognised shape reads as backgrounded, which errs toward not
    denying."""
    return bool(tool_input.get('run_in_background'))


def classify_poll(command):
    """Return a short poll-shape label for a foreground-poll command, or None.

    Labels: 'gh_pr_checks_watch', 'gh_run_watch', 'sleep_loop'. None means the
    command is not a recognised foreground poll (defer — do NOT deny)."""
    groups = simple_commands(command)
    if not groups:
        return None
    has_loop_kw = False
    has_sleep = False
    for group in groups:
        if _is_gh_pr_checks_watch(group):
            return 'gh_pr_checks_watch'
        if _is_gh_run_watch(group):
            return 'gh_run_watch'
        lead = _leading_word(group)
        if lead in ('while', 'until'):
            has_loop_kw = True
        elif lead == 'sleep':
            has_sleep = True
    if has_loop_kw and has_sleep and polls_gh(command):
        return 'sleep_loop'
    return None


_SHAPE_DESC = {
    'gh_pr_checks_watch': '`gh pr checks --watch` blocks the session until CI '
                          'finishes',
    'gh_run_watch': '`gh run watch` blocks the session until the run finishes',
    'sleep_loop': 'a `while/until … sleep …` poll loop blocks the session and '
                  'burns tokens',
}


def build_reason(shape):
    """The deny (fix-it) message pointing the session at the watcher."""
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', '')
    watcher = os.path.join(plugin_root, 'scripts', 'pr-sentinel-watch.sh') \
        if plugin_root else 'scripts/pr-sentinel-watch.sh'
    desc = _SHAPE_DESC.get(shape, 'this command foreground-polls CI')
    return (
        f'pr-sentinel: refusing to foreground-poll CI — {desc}. Launch the '
        f'PR Sentinel watcher as a BACKGROUND task (run_in_background) instead:\n'
        f'    bash "{watcher}" <PR>\n'
        f'It sleeps (zero idle tokens) and wakes this session when a check '
        f'fails, a conflict appears, the PR goes green, or the PR closes. '
        f'When it wakes you, act on the reported event, push, and relaunch it. '
        f'Never auto-merge.\n'
        f'If there is no PR to watch — a tag-triggered release build, say — '
        f're-run this same command as a background task '
        f'(run_in_background: true); only the foreground form is denied.\n'
        f'If you genuinely need this one command, re-run it with an inline '
        f'PR_SENTINEL_OVERRIDE=<reason> prefix on the command itself '
        f'(PR_SENTINEL_OVERRIDE="why this once" <command>).'
    )


def build_duplicate_reason(pr, task_ids):
    """The reason attached to a duplicate-launch deny. Names the running task so
    the session can stop it in one call rather than being left with no move."""
    return (
        f'pr-sentinel: refusing a SECOND watcher on #{pr} — this session '
        f'already has one running, and it re-reads the PR head on every poll, '
        f'so it covers the latest push. Two watchers wake this session twice '
        f'for every event and poll GitHub twice as often. Do nothing: the '
        f'running watcher will wake you. '
        + watchers.stop_hint(pr, task_ids)
        + ' If you genuinely need a second one, re-run with an inline '
          'PR_SENTINEL_OVERRIDE=<reason> prefix.')


def build_allow_reason():
    """The reason attached to the watcher-launch auto-allow."""
    return (
        'pr-sentinel: auto-approving the first-party PR Sentinel watcher launch '
        '— a read-only background task that polls GitHub-controlled check state '
        'and wakes this session on a failure, conflict, green, or close. Gated '
        'by PR_SENTINEL_AUTOALLOW (set it to 0 to keep the base Bash prompt).'
    )


def run(data):
    """Decide the PreToolUse verdict for `data`, and print it if there is one.

    Called by the `pr-sentinel.py` entry point, which owns the stdin parse and
    the fail-open wrapper. Emitting nothing is the defer."""
    if data.get('tool_name') != 'Bash':
        return
    tool_input = data.get('tool_input') or {}
    command = tool_input.get('command') or ''
    if not command.strip():
        return

    # Read once: all three denies honour the same escape hatch.
    overridden = bool(os.environ.get('PR_SENTINEL_OVERRIDE', '').strip()
                      or inline_override(command))

    # A launch for a PR this session is already watching is pure duplication;
    # deny it and name the task that would stop the incumbent. Checked before
    # the auto-allow, which would otherwise wave the duplicate straight through.
    # Fail-safe both ways: an unrecognised launch shape yields no PR number, and
    # an unreadable transcript yields no live watchers, so either defers.
    launch_pr = watcher_launch_pr(command)
    if (launch_pr and os.environ.get('PR_SENTINEL_DISABLE') != '1'
            and not overridden):
        live = watchers.live_watchers(data.get('transcript_path'))
        if launch_pr in live:
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': build_duplicate_reason(
                    launch_pr, live[launch_pr])}}))
            return

    # Auto-allow the plugin's OWN watcher launch (default on) so the session
    # isn't prompted by the base Bash permission on every (re)launch. The match
    # is airtight and fail-safe (see is_watcher_launch): any doubt falls through
    # to the normal permission system rather than allowing.
    if _autoallow_enabled() and is_watcher_launch(command):
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'permissionDecisionReason': build_allow_reason()}}))
        return

    # A `gh pr create` over another open PR's lines. Ahead of the backgrounded
    # early return on purpose: backgrounding a create still opens the PR, so
    # unlike a foreground poll it is not the fix. The probes fail silent, so a
    # machine with no `gh`, no credentials, or a base ref that won't resolve
    # yields no hits and the create proceeds.
    if overlap.enabled() and not overridden and is_pr_create(command):
        hits = overlap.overlapping_prs(data.get('cwd'))
        if hits:
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': overlap.build_reason(hits)}}))
            return

    # A backgrounded call can't block the session or burn idle tokens, so the
    # harm the deny names doesn't apply. Checked after the auto-allow, which the
    # watcher's own (backgrounded) launch still needs.
    if is_backgrounded(tool_input):
        return

    if overridden:
        return  # escape hatch: defer to the normal permission system

    shape = classify_poll(command)
    if shape is None:
        return  # not a recognised foreground poll: defer (never deny unsure)

    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': build_reason(shape)}}))

