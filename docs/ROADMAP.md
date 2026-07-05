# Roadmap

The MVP is the **watcher** + the **PostToolUse nudge**. The items below are
designed and scoped but deliberately **not yet implemented**, so the initial
plugin stays small and reviewable. Each is tracked as a Queue row in
[`STATUS.md`](STATUS.md); this doc holds the design intent.

## R1 — Stop-hook backstop (fast-follow)

**Problem.** The PostToolUse nudge is advisory — a hook cannot force the model
to launch the watcher. A session can end its turn with an open PR and pending
CI and never start a watcher.

**Design.** A `Stop` hook that fires when the session ends a turn. It blocks the
stop **once** — returning an instruction to launch the watcher — when *all* of:

- the session created an open PR this session (detected from prior
  `gh pr create` / `git push` activity in the transcript, or a lightweight
  marker the nudge could drop), and
- required checks are still pending, and
- no live watcher background task is running for that PR.

It must respect `stop_hook_active` to avoid an infinite stop loop: block at most
once, then let the stop proceed. This is what turns "advisory" into "reliable."

**Why not in the MVP.** It needs a way to know whether a watcher is already
running (task enumeration) and to identify the session's own open PR without a
network call or comment ingestion — a design worth landing on its own.

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
