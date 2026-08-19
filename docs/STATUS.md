# Project Status

Single source of truth for progress and priorities in pr-sentinel. Pick the
next task from the top of the Queue. Maintenance rules: see
[`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

**Status:** 🔲 ready · 🚫 blocked
**Size:**   S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `watcher` `hook` `retro`
**Next ID:** Q14

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q11"></a>Q11 | Cap the PostToolUse and Stop hook timeouts | `bug` `infra` | 🔲 | S | Claude Code reads `hooks/hooks.json`'s `timeout` in **seconds**, so the remaining `10000` entries are 2.8 hours. The PreToolUse entry already dropped to 60 when its overlap check started shelling out. |
| <a id="Q5"></a>Q5 | Don't nudge on a push whose target is the default branch | `bug` `hook` | 🔲 | S | A release push (`git push origin HEAD:main v0.9.0`) reads as a branch push, so the hook asks for a watcher on a branch that will never have a PR. Extend the classifier as #34 did for tag refspecs. |
| <a id="Q8"></a>Q8 | Require every `PR_SENTINEL_*` env var to appear in the README Configuration table | `tests` `retro` | 🔲 | S | Same shape as the PRIVACY gate: extract `PR_SENTINEL_*` from the scripts and fail a var the README does not document. Passes today, so land a falsifiability test with it. |
| <a id="Q9"></a>Q9 | Audit the release window's PRs against the PR template at pre-flight | `docs` `retro` | 🔲 | S | v0.9.0 shipped a GitHub read `PRIVACY.md` never named. Add the pre-flight step, and record that notes archaeology doubles as fresh-eyes review. |
| <a id="Q12"></a>Q12 | Clamp the pending poll so it never overshoots the expected finish | `watcher` | 🔲 | S | Age-proportional pacing is loosest right when a long build is about to finish: `sleep = min(age/K, D - age + slack, MAX_INTERVAL)`. Second step in [`plan/adaptive-poll-interval.md`](plan/adaptive-poll-interval.md), which names where `D` comes from. |
| <a id="Q7"></a>Q7 | Fix the wrong issue citation in the Stop hook docstring | `docs` | 🔲 | S | `scripts/pr-sentinel-stop-hook.py:30` cites #34 for the `pr-link` false-positive blocks, but #34 is the tag-push nudge bug and never mentions `pr-link`. Find the right issue or drop the number. |
| <a id="Q13"></a>Q13 | Report the PreToolUse guard's own decisions in the activity report | `docs` `infra` | 🔲 | S | The guard's denies and auto-allows sit in the same transcripts as `PreToolUse:Bash` attachments but go uncounted, so an over-fired `PR_SENTINEL_OVERRIDE` is invisible. Copy foreground-guard's shape. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q4"></a>Q4 | Distinguish required vs optional checks | `watcher` | S | **Event:** optional checks that genuinely fail their run wake sessions often (`continue-on-error` is already absorbed, #32). Then consult branch-protection required-check names. |
