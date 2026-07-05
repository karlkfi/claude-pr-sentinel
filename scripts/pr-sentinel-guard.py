#!/usr/bin/env python3
"""PreToolUse hook: DENY foreground CI-poll commands and ALLOW the plugin's own
background watcher launch, pointing the session at the watcher either way.

Fires on a `Bash` command the session is *about to run*. Two branches:

* **Auto-allow** — if the command is *unambiguously* this plugin's own watcher
  launch (`bash <own-watcher> <PR>`), it returns a PreToolUse `allow` so the
  session isn't prompted by the base Bash permission on every (re)launch. The
  match is airtight and fail-safe: a single simple command, no operators /
  redirects / substitutions / globs, `argv[1]` resolving (via realpath) to this
  plugin's own `pr-sentinel-watch.sh`, and `argv[2]` a bare positive integer.
  ANY doubt -> defer (emit nothing), never allow. Gated by
  `PR_SENTINEL_AUTOALLOW` (default on; `0`/`false`/empty disables) and off when
  `PR_SENTINEL_DISABLE=1`.

* **Deny** — if the command is a blocking foreground poll (`gh pr checks
  --watch`, `gh run watch`, or a `while/until … sleep …` poll loop), it returns
  a PreToolUse `deny` whose reason points at the watcher. The deny is UNIFORM
  across permission modes: a hard `deny` (never `ask`), so a
  `bypassPermissions`/headless run self-corrects instead of stalling on an
  unanswerable prompt.

Escape hatch: `PR_SENTINEL_OVERRIDE=<reason>` (any non-empty value) downgrades
the deny — the hook defers, letting the command proceed under the normal
permission system — for the rare legitimate one-off. This mirrors prod-guard's
`PROD_GUARD_OVERRIDE`.

The hook is PURELY LOCAL: it inspects only the proposed command string and
never makes a network call or reads any PR text.

Fail modes: defers silently (emits nothing) on ANY uncertainty — non-Bash tool,
unparseable command/input, a shape it doesn't recognise. It NEVER denies a
command it isn't sure about, and it can never break a session.
`PR_SENTINEL_DEBUG=1` re-raises for debugging.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import json
import os
import re
import shlex
import sys

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
    own location (`<root>/scripts/pr-sentinel-guard.py`) or `CLAUDE_PLUGIN_ROOT`.
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


def is_watcher_launch(command):
    """True ONLY if `command` is unambiguously `bash <own-watcher> <PR>`:

      * no shell operator / redirect / substitution / glob (`_AUTOALLOW_FORBIDDEN`)
      * exactly three tokens, `argv[0]` basename `bash`
      * `argv[1]` realpath-equals this plugin's own watcher script
      * `argv[2]` a bare positive integer (the PR number)

    Any doubt returns False so the caller defers rather than allows."""
    if any(c in _AUTOALLOW_FORBIDDEN for c in command):
        return False
    try:
        argv = shlex.split(command)  # posix; respects quotes
    except ValueError:
        return False
    if len(argv) != 3:
        return False
    if os.path.basename(argv[0]) != 'bash':
        return False
    if not re.match(r'\A[1-9][0-9]*\Z', argv[2]):
        return False
    expected = _expected_watcher_path()
    if expected is None:
        return False
    try:
        return os.path.realpath(argv[1]) == expected
    except OSError:
        return False


def simple_commands(command):
    """Split a bash command string into simple commands on the shell operators
    that separate them (`&&`, `||`, `|`, `;`, `(`, `)`, newlines). Best-effort:
    on a tokenizing failure return [] so the caller defers rather than crashes.
    """
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
    """Drop leading NAME=VALUE assignments so `GH_TOKEN=x gh run watch` still
    resolves to `gh`."""
    i = 0
    while i < len(argv) and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', argv[i]):
        i += 1
    return argv[i:]


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
    if has_loop_kw and has_sleep:
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
        f'If you genuinely need this one command, set '
        f'PR_SENTINEL_OVERRIDE=<reason> to allow it.'
    )


def build_allow_reason():
    """The reason attached to the watcher-launch auto-allow."""
    return (
        'pr-sentinel: auto-approving the first-party PR Sentinel watcher launch '
        '— a read-only background task that polls GitHub-controlled check state '
        'and wakes this session on a failure, conflict, green, or close. Gated '
        'by PR_SENTINEL_AUTOALLOW (set it to 0 to keep the base Bash prompt).'
    )


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: defer
    if data.get('tool_name') != 'Bash':
        return
    command = (data.get('tool_input') or {}).get('command') or ''
    if not command.strip():
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

    override = os.environ.get('PR_SENTINEL_OVERRIDE', '')
    if override.strip():
        return  # escape hatch: defer to the normal permission system

    shape = classify_poll(command)
    if shape is None:
        return  # not a recognised foreground poll: defer (never deny unsure)

    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': build_reason(shape)}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PR_SENTINEL_DEBUG') == '1':
            raise
        sys.exit(0)
