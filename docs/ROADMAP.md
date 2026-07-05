# Roadmap

The MVP is the **watcher** + the **PostToolUse nudge**. The items below are
designed and scoped but deliberately **not yet implemented**, so the initial
plugin stays small and reviewable. Each is tracked as a Queue row in
[`STATUS.md`](STATUS.md); this doc holds the design intent.

## R1 — Stop-hook backstop ✅ shipped

Implemented as `scripts/pr-sentinel-stop-hook.py` (registered under `Stop` in
`hooks/hooks.json`). A `Stop` hook that blocks the stop **once** — respecting
`stop_hook_active` so it never loops — when the session ends a turn with an open
PR it opened, no live watcher, and no local evidence the PR was handed off. This
is what turns "advisory" into "reliable."

The two open problems were solved from the session's own transcript alone: the
PR is identified from the harness's `pr-link` record (and the session's own
`gh pr create` output URL), and a watcher is treated as live only while its
`run_in_background` launch has no matching `<task-notification>` completion
record — no network call, no process table, and no PR body/comment ingestion.
Check status can't be checked without a network call, so "checks pending" is
approximated as "opened, not handed off, unwatched";
the block is safe because it fires at most once and only asks the session to
launch the watcher, which then authoritatively determines check state. See
[`DESIGN.md`](DESIGN.md#why-the-nudge-is-advisory) for the mechanism.

## R2 — PreToolUse foreground-poll deny ✅ shipped

**Problem.** Even with the watcher available, a session may still reach for a
blocking foreground poll, the exact anti-pattern this plugin replaces.

**Design.** A `PreToolUse` hook on `Bash`
([`scripts/pr-sentinel-guard.py`](../scripts/pr-sentinel-guard.py)) that
**denies** blocking-poll command shapes with a fix-it message pointing at the
watcher:

- `gh pr checks --watch` (or `-w`)
- `gh run watch`
- `until …; do sleep …; done` / `while …; do sleep …; done` polling loops

It returns a hard **deny** (not `ask`) in *every* mode — notably
`bypassPermissions`, mirroring workspace-guard — so headless runs self-correct
instead of stalling on an unanswerable prompt. `PR_SENTINEL_OVERRIDE=<reason>`
(any non-empty value) downgrades the deny: the hook defers and the command
proceeds under the normal permission system, the rare legitimate case — the
same escape-hatch pattern as prod-guard's `PROD_GUARD_OVERRIDE`.

A bare `sleep N` before a status check is deliberately **not** denied — too
fuzzy to classify without false positives, so the hook fails open on it. See
the [PreToolUse decision table](../README.md#what-it-does) for the full matrix.

## R3 — Friction / activity report (later)

A read-only analyzer over local session transcripts (the pattern from
workspace-guard / prod-guard `friction-report`) that ranks: how often the nudge
fired, how often a watcher was actually launched, and which watcher events
dominated (check_failure vs conflict vs ready). This closes the loop on whether
the advisory nudge is being followed and where CI time is spent. It adds no
telemetry — it re-reads what Claude Code already recorded.

## Non-roadmap (explicit non-goals)

These are **not** planned and would need a security rationale to reconsider:

- Ingesting PR/issue comments or the PR body (the excluded injection channel).
- Auto-merging, or any write to GitHub from the watcher.
- A cloud/cron trigger that runs in a fresh session without the working context.
