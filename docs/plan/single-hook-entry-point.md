# One hook entry point, dispatching on `hook_event_name`

## Goal

Make every friction report and transcript reader reduce all three of this
plugin's hooks to the label `pr-sentinel`, so a single `--plugin pr-sentinel`
returns the whole plugin.

## Why the current shape fails

A reader recovers the emitting plugin from two inputs: the reason string's
opener, and the hook `command` recorded with each decision. Every friction
report reduces that command the same way — take the first `*.py` basename, drop
a leading `bash-`, use the rest. pr-sentinel's openers already conform
(`pr-sentinel: `, position 0, colon-space). Its filenames do not:

| Hook | Script | Reduces to |
|---|---|---|
| PreToolUse | `scripts/pr-sentinel-guard.py` | `pr-sentinel-guard` |
| PostToolUse | `scripts/pr-sentinel-hook.py` | `pr-sentinel-hook` |
| Stop | `scripts/pr-sentinel-stop-hook.py` | `pr-sentinel-stop-hook` |

Four labels for one plugin, counting the one the reasons announce. No single
`--plugin` value returns all of it, and which part is missing depends on which
reader you asked.

This is not three renames. The reduction takes a *basename*, so three files in
one directory cannot all reduce to `pr-sentinel`.

## Approach

One executable entry point wired to all three events, dispatching on the
payload's `hook_event_name`. The three implementations stay in their own files,
demoted to importable modules named the way this repo already names modules
(`pr_sentinel_overlap.py`, `pr_sentinel_watchers.py`).

1. `git mv` each hook script to its underscore module name; replace its
   `main()` (which parsed stdin) with `run(data)` (which takes the parsed
   payload) and drop its `if __name__` block. Modules lose the executable bit.
2. Add `scripts/pr-sentinel.py`: parse stdin once, dispatch on
   `hook_event_name`, own the fail-open wrapper all three shared.
3. Point all three `hooks/hooks.json` commands at it.
4. `scripts/friction-report.py` is a *consumer* of the hook command shape:
   `GUARD_SCRIPT` anchors the guard's decisions on `pr-sentinel-guard.py`.
   Widen it to match the new name and the old, so the report keeps reading
   transcripts recorded before this change.

## Blast radius

- `tests/test_wiring.py` — asserts the three old paths in `hooks.json`.
- `tests/test_guard.py`, `tests/test_hook.py`, `tests/test_stop_hook.py`,
  `tests/test_overlap.py`, `tests/test_friction_report.py` — all invoke a hook
  script by path as a subprocess. They now drive the dispatcher, which means
  their payloads need the `hook_event_name` a real payload always carries.
- `tests/test_watcher.py`'s PRIVACY component closure seeds from `hooks.json`
  and follows `scripts/…` references. The dispatcher names its three modules,
  so the closure reaches them and each keeps its own disclosure section.
- `PRIVACY.md`, `README.md`, `CLAUDE.md`, `docs/DESIGN.md`, `docs/ROADMAP.md`.
- `docs/queue/Q30.md` and `docs/queue/Q31.md` cite the renamed files, Q31 by
  line number. The rename is what falsifies those citations, so they are
  repointed in this change rather than filed as drift.

## Out of scope

The verdict half. pr-sentinel already denies rather than asks on every branch,
and its denies carry a rewrite and name `PR_SENTINEL_OVERRIDE` last.
