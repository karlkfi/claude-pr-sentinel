# Roadmap

The MVP is the **watcher** + the **PostToolUse nudge**. The items below were
designed and scoped separately from it, so the initial plugin stayed small and
reviewable; all four have since shipped. This doc holds the design intent —
open work is tracked as an item in [`queue/`](queue/README.md).

## R1 — Stop-hook backstop ✅ shipped

Implemented as `scripts/pr-sentinel-stop-hook.py` (registered under `Stop` in
`hooks/hooks.json`). A `Stop` hook that blocks the stop **once** — respecting
`stop_hook_active` so it never loops — when the session ends a turn with an open
PR it opened, no live watcher, and no local evidence the PR was handed off. This
is what turns "advisory" into "reliable."

The two open problems were solved from the session's own transcript alone: the
PR is identified from the session's own `gh pr create` output URL and its
watcher launches, with two fallbacks for a create whose URL never reached the
transcript — the harness's `pr-link` record (a bare record marks any PR the
session *references*, not ones it opened, so it counts only inside that create's
own tool call and only for a PR not mentioned before it), and the file the create
redirected its output to, which also recovers a PR in another repository — and a
watcher is
treated as live only while its
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

## R4 — Desktop auto-fix migration helper ✅ shipped

**Problem.** pr-sentinel positions itself as the replacement for Claude
Desktop's "Auto-fix CI & address comments," but installing it doesn't turn that
toggle off. A migrating user is left with every pre-existing session still
armed on the PR comment stream — each a credentialed local agent an attacker
can reach by commenting on an old or merged PR. The toggle is per-session
desktop state with no bulk or UI control, and archiving (the only bulk stop)
destroys the session list some users compute metrics from.

**Design.** A stdlib-only Python helper
([`scripts/pr-sentinel-migrate-autofix.py`](../scripts/pr-sentinel-migrate-autofix.py))
plus a guiding slash command
([`commands/pr-sentinel-migrate-autofix.md`](../commands/pr-sentinel-migrate-autofix.md)).
The per-session state is a plain JSON file under the desktop app's
`claude-code-sessions` store with a top-level `autoFixEnabled` boolean; the
helper scans those files and flips the flag to `false` on the targeted set.
Safety is the whole point: dry-run by default, backs up before editing, refuses
to run while the app is up (the live app rewrites these files and would clobber
a live edit), targets **MERGED** PRs by default (never OPEN without `--all`),
and only touches files matching the expected schema — no-op with a clear
message otherwise. It is purely local: no network call, no PR text. The design
plan is [`docs/plan/migrate-autofix.md`](plan/migrate-autofix.md).

**Why Python, not the watcher's bash.** It must resolve per-platform session
paths and round-trip undocumented app JSON safely; Python stdlib does both with
no `jq` dependency and fits the existing test harness. The optional GitHub-side
conversation lock (issue #3) was deliberately left out — a separate concern
with a public-repo community cost.

## R3 — Friction / activity report ✅ shipped

Implemented as [`scripts/friction-report.py`](../scripts/friction-report.py)
plus the [`/pr-sentinel-friction-report`](../commands/pr-sentinel-friction-report.md)
slash command, following the workspace-guard / prod-guard `friction-report`
pattern. It ranks how often the nudge fired, how often a watcher was actually
launched, and which watcher events dominated. It adds no telemetry — it re-reads
what Claude Code already recorded, and makes no network call.

**What the transcripts actually hold**, measured over the local corpus while
building it — each of these changed the implementation:

- **A backgrounded watcher's report does not come back through its launch's own
  `tool_result`.** The harness hands it over as a task-output file the session
  then reads (`Read`, `cat`, `TaskOutput`), so the obvious `tool_use_id` join
  from launch to report finds almost nothing: 7 of 2054 reports joined, 2047 did
  not. Reports are matched wherever they surface instead.
- **A `Read` of that file arrives line-numbered.** Anchoring the report header at
  the very start of the text drops most real events; the prefix has to be
  stripped first.
- **Matching the watcher's filename counts mentions as launches.** Every
  `grep`, `cat`, `find`, `shellcheck` and heredoc naming the script is counted:
  2558 launches where 2372 happened, and 207 foreground launches where 22
  happened. Detection is anchored on the `bash …` invocation.
- **The same output file gets read more than once**, so reports are deduplicated
  per session on (event, PR, report body) — 352 of 2054 raw matches, 17%.

All four were measured over the same 875 local transcripts. The plugin's own
repository is the largest single source of text mentioning these markers, all
of it source, tests and prose rather than usage.
Every detector is anchored so that development noise is not reported as
activity, and `tests/test_friction_report.py` fixes each anchor with a fixture
built from the real signature.

## Non-roadmap (explicit non-goals)

These are **not** planned and would need a security rationale to reconsider:

- Ingesting PR/issue comments or the PR body (the excluded injection channel).
- Auto-merging, or any write to GitHub from the watcher.
- A cloud/cron trigger that runs in a fresh session without the working context.
