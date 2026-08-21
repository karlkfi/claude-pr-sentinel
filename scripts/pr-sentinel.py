#!/usr/bin/env python3
"""The plugin's single hook entry point: read the payload once, dispatch on the
event it names, print whatever that handler decides.

One file, three events, on purpose. Claude Code records the hook `command` with
each decision, and every reader that attributes a decision to a plugin reduces
that command the same way — first `*.py` basename, minus a leading `bash-`. A
plugin wiring three differently-named scripts therefore spreads itself across
three labels and no single `--plugin` filter returns all of it. The reduction
takes a basename, so the only fix is one name on every event.

The decision logic stays in a module per event, imported lazily so a Bash call
loads one handler rather than three:

  * `PreToolUse`  -> `scripts/pr_sentinel_guard.py`     (the poll / duplicate /
    overlap denies, and the watcher-launch auto-allow)
  * `PostToolUse` -> `scripts/pr_sentinel_hook.py`      (the watcher nudge)
  * `Stop`        -> `scripts/pr_sentinel_stop_hook.py` (the unwatched-PR
    backstop)

This file owns what all three used to duplicate: the stdin parse, the fail-open
wrapper, and the `PR_SENTINEL_DEBUG=1` re-raise. Any event it does not
recognise is silence, which is the defer every handler already falls back to.
"""
import importlib
import json
import os
import sys

# The handlers are siblings of this file. Import them by path insert rather
# than as a package: the plugin ships as a directory of scripts, not an
# installed distribution.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HANDLERS = {
    'PreToolUse': 'pr_sentinel_guard',
    'PostToolUse': 'pr_sentinel_hook',
    'Stop': 'pr_sentinel_stop_hook',
}


def dispatch(data):
    """Run the handler for `data`'s event. Unknown or absent event: defer."""
    if not isinstance(data, dict):
        return
    module = HANDLERS.get(data.get('hook_event_name'))
    if module is None:
        return
    importlib.import_module(module).run(data)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: defer
    dispatch(data)


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PR_SENTINEL_DEBUG') == '1':
            raise
        sys.exit(0)
