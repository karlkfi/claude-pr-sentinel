# Plan: distinguish a check already failing on the base branch (issue #44)

Tracks [issue #44](https://github.com/karlkfi/claude-pr-sentinel/issues/44).

## Goal

Stop N concurrent sessions each diagnosing, fixing, and colliding over the same
inherited failure. When every failing check on a pull request (PR) is already
red on the base branch, say so and keep watching instead of waking the session
with "diagnose and fix the failing check(s) below in this local session".

## The defect

Nothing in a `check_failure` report says whether the PR caused the failure or
inherited it. The next-action text assumes it caused it, so every session
touching a file the gate covers writes its own fix; the second one to land pays
for a rebase conflict on top.

`build_warning` in the Stop hook is the shipped escape hatch, but it is
*inferred from repetition* — the same failed set at the same head SHA across two
watcher runs. The earliest it can fire is the second report, and the duplicate
work starts on the first.

## Approach

On a poll that found failing checks (after `continue-on-error` absorption), ask
GitHub whether the same workflows are already failing on the base branch. If
**every** surviving failure is, emit a non-terminal **`base_failure`** notice and
keep polling; otherwise fall through to today's `check_failure`.

1. Resolve each distinct failing check's run to its `workflow_id`
   (`gh api repos/<o>/<r>/actions/runs/<id>`, the path the check link already
   carries — the same call shape `failures_absorbed` uses).
2. Ask for that workflow's latest **completed** run on the base branch
   (`gh api repos/<o>/<r>/actions/workflows/<id>/runs?branch=<base>&status=completed&per_page=1`).
   Query by **workflow**, never by "the base branch's newest run": a path-gated
   workflow only runs when its paths change, so the base tip and the workflow's
   last run can be many commits apart, and reading the former gives a stale green
   from before the breakage or a stale red long after the fix. A
   workflow-scoped query self-corrects, because a run exists only where the paths
   matched.
3. All-or-nothing, like absorption. One failure the base does not share is this
   PR's own, and the mixed case must still wake the session.
4. `base_failure` does **not** exit. The unblock signal is "green on the base
   again", not "somebody closed a tracking issue", and it holds whether the fix
   arrives by a PR or a revert. When the base clears while the check is still red
   here, that failure *is* this PR's and the next poll wakes the session with
   `check_failure`.
5. The notice re-fires when the failing set changes, not on every poll — same
   once-per-state shape as `ready_watching`.

Fail safe to "not inherited" everywhere: an unresolvable run, an unreadable
`workflow_id`, a base with **no** run of that workflow at all (a new workflow, or
one whose paths the base has never touched — the case the issue flagged as
speculation), and a `cancelled` base run all fall through to `check_failure`.

The report reads `.path` (the workflow file) and never `.name` /
`.display_title`, which a `run-name:` expression can interpolate a commit message
or PR title into — human-writable text this plugin does not ingest.

## Why a knob

`PR_SENTINEL_BASE_CHECK=0` restores today's behaviour. Absorption (#32) has no
knob because it reads GitHub's own verdict *on the exact run in question*; this
is an inference *across* runs, and its false negative is real: a PR that
independently breaks the same workflow is masked until the base goes green. The
masking is a delay, not a loss — once the base clears, the still-red check wakes
the session — but the weaker evidence earns an off switch.

## Cost

Two extra `gh api` calls per distinct failing run, only on polls that already
found a failure. While a `base_failure` holds, that repeats each poll at the
backed-off interval (~12 calls/hour per run at the 300s ceiling), which is what
detecting "the base went green" costs.

## Scope

- `scripts/pr-sentinel-watch.sh` — config var, `base_run_failure` and
  `base_failures_only`, the `notice_base_failure` emitter, the gate in front of
  `emit_check_failure`, and a `timeout` line naming a withheld failure.
- `tests/test_watcher.py` — stub `gh` grows the two API shapes; notice fires,
  fires once, holds the watch, and falls through on green base / mixed failures /
  absent base run / disabled knob.
- `README.md` (watcher event table, Configuration table, a *Failures inherited
  from the base branch* section, Limitations), `docs/DESIGN.md` (the mechanism
  and the security note on `.path`).
