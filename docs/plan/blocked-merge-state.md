# Plan: don't call a BLOCKED merge state green (issue #29)

Tracks [issue #29](https://github.com/karlkfi/claude-pr-sentinel/issues/29).

## Goal

Stop the watcher firing `ready` on a pull request (PR) whose required checks
have never registered, without inventing a false negative on the far more
common PR that is merely waiting for a required review.

## The defect

`ready` is decided from `gh pr checks` buckets alone:

```bash
if (( pending_count == 0 && fail_count == 0 )); then
    if (( pass_count > 0 )) || [[ "$MERGE" == "CLEAN" ]]; then
```

`gh pr checks` emits one row per check that **exists**. A required check whose
workflow never registered — the classic case being a path-filtered heavy gate
on a PR that started docs-only — produces no row, so it lands in no bucket:
`pending_count` is 0 because the check is absent, not because it reported. Any
one unrelated registered check then satisfies `pass_count > 0`.

`mergeStateStatus` is the only field fetched that can observe the absent check.
It was assigned, printed in every report header, and compared against `DIRTY`
and `BEHIND` only. `BLOCKED` was never read.

## Why the one-line fix is not enough

`BLOCKED` is GitHub's answer for *any* unsatisfied merge requirement, not just
unreported checks. The most common one by far is an outstanding required review.
Making `MERGE != BLOCKED` a precondition of `ready` therefore means a fully
green PR in a review-gated repo never fires `ready` at all: the watcher burns
the whole budget and wakes the session with `timeout`. That regression lands on
exactly the repos that also have path-gated required checks.

The two causes are not distinguishable from the field, but they are
distinguishable by **persistence**. A check that is merely slow to register
appears as `pending` within a poll or two; a review gate persists.

## Approach

1. `BLOCKED` no longer satisfies the green branch, so `ready` cannot fire on it.
2. Green-but-`BLOCKED` is counted across **consecutive** polls
   (`PR_SENTINEL_BLOCKED_POLLS`, default 3). Any poll that is not green-and-
   blocked resets the streak.
3. Once the streak is reached, emit a new terminal **`blocked`** event whose
   report names both candidate causes and refuses to call the PR green. Under
   `PR_SENTINEL_WATCH_UNTIL=closed` it is the non-terminal
   **`blocked_watching`** notice instead, mirroring `ready_watching`.

Deliberately **not** doing the issue's fix direction 2 (read the branch's
required-check list and count a required-but-absent check as pending). It needs
an extra API call and a token scope that can read branch protection. That is
already tracked as Q4 in `docs/STATUS.md`.

## Why `blocked` is a handoff

The Stop hook's `CONCLUDED_EVENT_RE` learns `blocked`. Both causes need a human:
a review gate is the human's turn by definition, and a required check that never
registered cannot be waited out — the branch protection or the trigger paths
have to change. Leaving `blocked` out of the concluded set would have the hook
re-block each stop, the session relaunch the watcher, and the watcher re-emit
`blocked` — the livelock shape `dampen-repeat-check-failure.md` exists to avoid.

The `(?![\w-])` guard on that regex keeps `blocked_watching` out of the
concluded set, exactly as it does for `ready_watching`.

## Scope

- `scripts/pr-sentinel-watch.sh` — config var, streak counter, two emitters,
  restructured green branch, `timeout` report mentions a reported block.
- `scripts/pr-sentinel-stop-hook.py` — `CONCLUDED_EVENT_RE`.
- `tests/test_watcher.py` — blocked fires, grace period holds, streak resets,
  non-`BLOCKED` states still reach `ready`, `closed`-mode notice.
- `tests/test_stop_hook.py` — `blocked` concludes, `blocked_watching` does not.
- `README.md` (watcher event table, Stop hook table, Configuration table),
  `docs/DESIGN.md` (exit conditions, a section on the ambiguity),
  `.claude-plugin/plugin.json` keywords.
