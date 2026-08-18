# Plan — Q10: poll at the base interval while checks are pending

Backlog item [Q10](../STATUS.md).

## Goal (one sentence)

Stop a check failure waiting up to five minutes to wake the session, without
turning a forty-minute build into hundreds of API calls.

## The defect

The backoff is unconditional. `sleep_for` starts at `PR_SENTINEL_INTERVAL`
(30s) and is multiplied by `BACKOFF_NUM/BACKOFF_DEN` (3/2) after **every** idle
poll, capped at `PR_SENTINEL_MAX_INTERVAL` (300s):

```bash
sleep "$sleep_for"
# Exponential-ish backoff toward MAX_INTERVAL.
sleep_for=$(( sleep_for * BACKOFF_NUM / BACKOFF_DEN ))
(( sleep_for > MAX_INTERVAL )) && sleep_for="$MAX_INTERVAL"
```

The ramp is 30 → 45 → 67 → 101 → 151 → 227 → 300, so roughly ten minutes into a
watch every poll is five minutes apart. A check that fails just after a poll
waits that long to wake the session. For scale: `gh pr checks --watch` refreshes
every 10 seconds by default, so the current ceiling is thirty times more
conservative than the command this plugin exists to replace.

Three cases already reset to the base interval — a green poll awaiting
confirmation, a confirmed-green poll held by an `UNKNOWN` merge state, and a
vanished queue entry awaiting confirmation. A plain "CI still running" poll is
not one of them, which is the case that matters most.

## Approach

**Make the sleep proportional to how long the checks have been running**, rather
than to how many times we have already asked:

```
sleep = clamp(elapsed_since_checks_started / K, FLOOR, MAX_INTERVAL)
```

Checks that started ten seconds ago get a ten-second poll; checks that have been
running forty minutes get a four-minute one. This self-tunes to a 30-second
suite and to an hour-long one with no history, no stored model, and no extra API
call — and it is correct on the first run of a brand-new workflow, which a
duration estimate is not.

**Reset to `FLOOR` whenever checks return to pending.** A push restarts the
clock, which is exactly the moment the session wants a tight loop again.

**Keep the full backoff once checks settle.** Under
`PR_SENTINEL_WATCH_UNTIL=closed` the watch continues past green, but what it is
waiting for then is a conflict landed by someone else's merge, or a close —
events driven by other people, on nobody's schedule. Backing off to
`MAX_INTERVAL` there is right.

## Optional refinement — never overshoot an expected finish

Age-proportional widening keeps widening, so it is at its loosest right when a
long build is about to finish. With an expected duration `D`:

```
sleep = min(age / K, D - age + slack, MAX_INTERVAL)
```

That gives wide polls in the middle of a build and a tight one at the expected
finish. `D` is cheap to obtain: the watcher already fetches the base branch's
latest completed run of the same workflow for `PR_SENTINEL_BASE_CHECK`, and that
run carries its own start and end timestamps.

Treat this as a second step. The clamp is only as good as `D`, and a workflow
whose duration just changed will mis-predict; the age-proportional rule beneath
it degrades gracefully and should land first.

## Non-goals

- **Running `gh pr checks --watch` in the background.** It watches checks only,
  so it cannot report `conflict`, `behind`, `dequeued`, or `closed` — a conflict
  from a sibling merge produces no check transition at all, and `--watch` would
  block while the PR is already unmergeable. The poll loop would still be needed
  beside it.
- **Storing timing history between watches.** The elapsed-time signal is already
  in the current watch; persisting anything is new state to keep correct.

## Verification

`tests/test_watcher.py` drives the watcher against a stub `gh` with per-call
fixtures, so a pending → pending → fail sequence can assert the interval never
widened while pending, and a settled sequence can assert it did. Pin the
direction that would otherwise regress silently: a mutation removing the
pending reset must go red.
