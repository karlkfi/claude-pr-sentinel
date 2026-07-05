# Project Status

Single source of truth for progress and priorities in pr-sentinel. Pick the
next task from the top of the Queue.

## Conventions

**Status:** ✅ done · ▶ started · 🔲 ready · 🚫 blocked · 💤 deferred
**Size:** S = one session · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `watcher` `hook`

**Maintaining this file:** see
[`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).
Short version:
- **Starting an S item:** complete it, delete the row.
- **Starting an M/L item:** create/update a plan doc under `docs/plan/`; delete
  the row here when done.
- **New item identified:** append it with the next unused ID.
- **`Last touched:` is one line, date only.**

Last touched: 2026-07-05

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items
until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q1"></a>Q1 | Stop-hook backstop | `hook` `security` | 🔲 | M | Roadmap R1 in [`ROADMAP.md`](ROADMAP.md). Block the stop once (respect `stop_hook_active`) when the session has an open PR it created, required checks pending, and no live watcher task. Needs a way to detect a running watcher task and identify the session's own PR without a network call or comment ingestion. |
| <a id="Q3"></a>Q3 | Friction / activity report | `docs` `infra` | 💤 | M | Roadmap R3. Read-only transcript analyzer ranking nudge-fired vs watcher-launched vs event mix. Mirror the workspace-guard / prod-guard `friction-report` pattern. Deferred until there's usage data to analyze. |
| <a id="Q4"></a>Q4 | Distinguish required vs optional checks | `watcher` | 💤 | S | Today any failing/cancelled check triggers `check_failure`. Optionally consult branch-protection required-check names so a failing *optional* check doesn't wake the session. Deferred: errs toward waking, which is the safe direction. |
