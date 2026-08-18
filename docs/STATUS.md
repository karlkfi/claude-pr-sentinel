# Project Status

Single source of truth for progress and priorities in pr-sentinel. Pick the
next task from the top of the Queue. Maintenance rules: see
[`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

**Status:** 🔲 ready · 🚫 blocked
**Size:**   S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `watcher` `hook`
**Next ID:** Q8

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q5"></a>Q5 | Don't nudge on a push whose target is the default branch | `bug` `hook` | 🔲 | S | A release push (`git push origin HEAD:main v0.9.0`) reads as a branch push, so the hook asks for a watcher on a branch that will never have a PR. Extend the classifier as #34 did for tag refspecs. |
| <a id="Q7"></a>Q7 | Fix the wrong issue citation in the Stop hook docstring | `docs` | 🔲 | S | `scripts/pr-sentinel-stop-hook.py:30` cites #34 for the `pr-link` false-positive blocks, but #34 is the tag-push nudge bug and never mentions `pr-link`. Find the right issue or drop the number. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q3"></a>Q3 | Friction / activity report (Roadmap R3) | `docs` `infra` | M | **Event:** real usage data accumulates in session transcripts. Then build a read-only analyzer ranking nudge-fired vs watcher-launched wakes, mirroring the guard plugins' friction-report. |
| <a id="Q4"></a>Q4 | Distinguish required vs optional checks | `watcher` | S | **Event:** optional checks that genuinely fail their run wake sessions often (`continue-on-error` is already absorbed, #32). Then consult branch-protection required-check names. |
