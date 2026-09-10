# Plan: an `unchecked` event for a head no check reported on (issue #112)

Tracks [issue #112](https://github.com/karlkfi/claude-pr-sentinel/issues/112).
Fourth branch of the family in #29, #37 and #40, and the one where the
persistence instrument those used cannot reach.

## Goal

Stop `ready` firing on a pull request (PR) whose head has no check rows at all,
and report that state as itself instead.

## The defect

The green branch reads an empty check set as green:

```bash
if (( pending_count == 0 && fail_count == 0 )); then
    if (( pass_count > 0 )) || [[ "$MERGE" == "CLEAN" ]]; then
```

With no rows, all three counts are 0 because nothing reported, and a repo
without branch protection reports `CLEAN` throughout — so the right operand
carries the whole test, and `pass_count == 0 && pending_count == 0` is exactly
"no check has ever reported". #29's `BLOCKED` guard is gated on a merge state
this PR never has.

#37 answered the same `||` with persistence: `PR_SENTINEL_GREEN_POLLS`
consecutive green polls, on the argument that an unregistered run turns up
pending within a poll or two. That holds for the gap it was measured against
and not for this one. Four gaps on 2026-08-26 in `karlkfi/claude-spill-guard`
ran 8 to 21.5 minutes against a two-poll confirmation, and the watcher reported
`ready` throughout each. No constant survives that, because the quantity is
GitHub's queue depth rather than anything the watch can see.

## The state is already known locally

`gh pr checks` emits one row per check that exists, so `checks` is empty
exactly when the rollup is. The watcher holds that in a variable it already
reads. Neither discriminator the issue names — the rollup length, `gh pr
checks`' exit status — needs a query the script does not already make, and
neither is more than a restatement of the empty set.

What is missing is not evidence. It is that one state — nothing reported —
is being folded into another that means the opposite.

## Approach

Split it out, then report it under its own name.

1. **An empty check set is never green.** `pass_count > 0` becomes the whole
   green test; the `CLEAN` operand no longer rescues a PR with no rows. This
   alone ends the false `ready`: `GREEN_SEEN` never advances, so neither
   `ready` nor `ready_watching` can fire.
2. **`unchecked`**, a terminal event, once the empty set persists for
   `PR_SENTINEL_UNCHECKED_GRACE` (default 600s, wall clock from the first
   empty poll, reset the moment any row appears). It reports the head SHA, both
   causes, and the one command that separates them. `unchecked_watching` is its
   non-terminal counterpart under `PR_SENTINEL_WATCH_UNTIL=closed`, matching
   `blocked`/`blocked_watching`.
3. **The grace decides only when to speak, never whether the PR is green.** An
   early `unchecked` states something true — no check has reported on this head
   — where an early `ready` states something false. That is what takes the
   timing guess out of the green path rather than resizing it.

## Why 600 seconds

Push to first run, from `PushEvent` timestamps against
`actions/runs?head_sha=` (measured 2026-09-10; SHAs pushed to `main` after
already running on a PR branch excluded):

| repo | workflows per PR | median | p90 | max |
|---|---|---|---|---|
| `karlkfi/claude-pr-sentinel` | 1–2 | 2s | 5s | 6s |
| `actions-gateway/github-actions-gateway` | 14–39 | 4s | 5s | 76s |

Registration is seconds, and the number of workflows does not move it. Commit
timestamps say otherwise (median 203s, max 1759s in the second repo) because an
agent's commit sits through a local gate before the push; the `PushEvent` is
the instrument that measures the queue rather than the author.

600s is eight times the worst healthy observation and inside the incident band
the issue measured, so a run that never registers is reported in ten minutes
instead of waiting out the hour-long watch budget.

## Rejected

- **A larger `GREEN_POLLS`.** The issue's own argument: the wait is unbounded
  by anything observable, and the failure is silent in the direction that
  reports success.
- **Probing whether the repo runs CI on pull requests at all**
  (`actions/runs?event=pull_request`). Positive evidence, but it costs a
  GitHub read and a `PRIVACY.md` disclosure, and it answers the case it was
  bought for — a PR every workflow's path filter excludes — wrong: the repo
  runs PR CI, this PR just has no workflow.
- **Keeping `ready` and qualifying its body.** The smallest diff, and it leaves
  the event name a session matches on saying the thing that was false.
- **Holding until `timeout`.** `timeout` already names a withheld `ready`, so
  this needs no new event at all — at the price of an hour of silence and a
  report whose name says the budget elapsed rather than what was observed.

## Cost

A PR that genuinely has no checks — no CI, or every workflow path-filtered past
it — is no longer called `ready`. It is called `unchecked` ten minutes later,
which is what it is. `PR_SENTINEL_UNCHECKED_GRACE` tunes the wait.

## Scope

- `scripts/pr-sentinel-watch.sh` — the green split, `UNCHECKED_GRACE` and its
  clock, `emit_unchecked` / `notice_unchecked_watching`, a poll clamped to the
  remaining grace, and a `timeout` line for a withheld report.
- `scripts/pr_sentinel_stop_hook.py` — `unchecked` joins `ready`/`closed`/
  `blocked` as a conclusion, so a session handed one is not blocked from
  stopping.
- `README.md` — the watcher decision table, the Configuration row, and the
  "Green is not the same as ready" section, which currently describes the
  post-push window as the one persistence closes.
- `docs/DESIGN.md` — the empty-set case beside the `BLOCKED` reasoning.
- `tests/test_watcher.py`, `tests/test_stop_hook.py`.
