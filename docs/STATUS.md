# Project Status

Single source of truth for progress and priorities in pr-sentinel. Pick the
next task from the top of the Queue. Maintenance rules: see
[`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

**Status:** 🔲 ready · 🚫 blocked
**Size:**   S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `watcher` `hook`
**Next ID:** Q5

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q3"></a>Q3 | Friction / activity report (Roadmap R3) | `docs` `infra` | M | **Event:** real usage data accumulates in session transcripts. Then build a read-only analyzer ranking nudge-fired vs watcher-launched wakes, mirroring the guard plugins' friction-report. |
| <a id="Q4"></a>Q4 | Distinguish required vs optional checks | `watcher` | S | **Event:** failing *optional* checks spuriously wake sessions often enough to hurt. Then consult branch-protection required-check names; today erring toward waking is the safe direction. |
