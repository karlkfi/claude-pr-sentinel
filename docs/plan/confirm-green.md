# Plan: confirm green across two polls before `ready` (issue #37)

Tracks [issue #37](https://github.com/karlkfi/claude-pr-sentinel/issues/37).
Follow-up to [`blocked-merge-state.md`](blocked-merge-state.md) (#29), on the
other branch of the same `||`.

## Goal

Stop the watcher firing `ready` in the seconds after a push, before the new
run's checks have registered — on repos where the `BLOCKED` guard from #29
cannot engage because there is no branch protection to block on.

## The defect

The green branch requires evidence that checks actually ran:

```bash
if (( pending_count == 0 && fail_count == 0 )); then
    if (( pass_count > 0 )) || [[ "$MERGE" == "CLEAN" ]]; then
```

Neither operand is that evidence in the window right after a push:

- The new head has **no check rows at all** until the run registers, so
  `pending_count` is 0 because the run is absent, not because it reported.
- `mergeStateStatus` is `CLEAN` for any pull request (PR) with no unsatisfied
  merge requirement, and a repo without branch protection never has one — so
  the right operand is true on every poll, including that one.

The test then degrades to `pending_count == 0 && fail_count == 0`, which is
exactly the no-rows-yet state it was written to exclude. #29's `BLOCKED` guard
sits below and is gated on `MERGE == BLOCKED`, so it never runs here.

Reproduced on a private repo with no branch protection: the watcher exited
`ready` while two jobs of the run sat pending at 0s. That repo *cannot* be
configured out of it — `gh api …/branches/main/protection` returns 403
`Upgrade to GitHub Pro`.

## Approach

Separate the two by **persistence**, the same instrument #29 used one branch
over. A run that has not registered is a state that resolves on its own within
seconds; a genuinely green PR stays green.

1. Count consecutive green polls (`PR_SENTINEL_GREEN_POLLS`, default 2). Any
   poll that is not green resets the streak.
2. `ready` — and its `ready_watching` notice, which claims the same green —
   fires only once the streak is met.
3. The confirmation poll does not inherit the idle backoff. It asks about the
   last few seconds, so waiting `MAX_INTERVAL` (300s, reached on any long CI
   run) to ask would be absurd; it is scheduled at `PR_SENTINEL_INTERVAL`,
   which bounds the cost to one base interval per genuine `ready`.

The `BLOCKED` path keeps its own `BLOCKED_POLLS` streak, which already
subsumes this one.

Rejected, from the issue's own list:

- **Require a check row observed at the current `HEAD_SHA`.** Encodes the
  intent directly, but costs an API call on exactly the polls that currently
  look green, and check rows carry no SHA — it needs a second query shape the
  watcher does not have.
- **Drop the `MERGE == CLEAN` operand.** Stale rows from the previous run
  satisfy `pass_count > 0` in the same window, so it closes the hole only for a
  brand-new PR, not for a re-push.

## Cost

One extra poll interval on every genuine `ready` — 30s at the default, bounded
by step 3 above. That is the price of the event meaning what a session trusts
it to mean.

## Scope

- `scripts/pr-sentinel-watch.sh` — config var, streak counter, `ready` and
  `ready_watching` gated on the confirmed streak, confirmation poll scheduled
  at the base interval.
- `tests/test_watcher.py` — the post-push window times out instead of firing
  `ready`, the streak resets on a pending poll, `PR_SENTINEL_GREEN_POLLS=1`
  restores single-poll behaviour, and the notice holds to the same bar.
- `README.md` (watcher event table, Configuration table, *Green is not the same
  as ready*), `docs/DESIGN.md` (the same section).
