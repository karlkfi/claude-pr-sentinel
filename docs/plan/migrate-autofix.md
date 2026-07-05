# Plan: migration helper — disable Desktop auto-fix on existing sessions

Tracks [issue #3](https://github.com/karlkfi/claude-pr-sentinel/issues/3).

## Goal

When a user switches to pr-sentinel, disable Claude Desktop's `autoFixEnabled`
(the "Auto-fix CI & address comments" toggle — the PR-comment-channel injection
vector) across their **existing** Claude Code sessions in one safe pass, so
migrating doesn't leave 70+ live, credentialed agents armed on the comment
stream. Merged-PR sessions are the worst case (no legitimate reason to keep
auto-fix armed, maximal exposure) and are the default target.

## Approach

A stdlib-only Python helper plus a guiding slash command:

- `scripts/pr-sentinel-migrate-autofix.py` — resolves the per-platform session
  store, scans `local_*.json`, and (dry-run by default) reports how many
  sessions have `autoFixEnabled:true`, grouped by PR state and repository. With
  `--apply` it refuses to run while the desktop app is up, backs up each target
  file, then flips the flag to `false`. Default scope is **MERGED** PRs; `--all`
  widens to every enabled session (including OPEN — opt-in).
- `commands/pr-sentinel-migrate-autofix.md` — a slash command that runs the
  read-only dry-run for the user, then hands off the exact `--apply` command to
  run from an external terminal **after quitting the app** (a command inside the
  app can neither quit it nor safely edit live state).

### Why Python, not the issue's bash reference

The helper must (a) resolve macOS/Linux/Windows session-store paths and (b)
round-trip undocumented app JSON without corrupting fields it doesn't
understand. Python stdlib (`json`, `pathlib`, `sys.platform`) does both safely
with no `jq` dependency, and drops into the existing `unittest` harness. The
watcher is bash only because it must be a cheap sleeping background process;
this is a one-shot local file editor.

## Hard requirements (from the issue) and how each is met

- **Dry-run first, reversible.** Dry-run is the default; `--apply` backs up every
  edited file under `<root>/.autofix-backup-<ts>/` (relative path preserved)
  before writing.
- **Never edit while the app runs.** `--apply` detects the running app
  (`pgrep -x Claude`; `tasklist` on Windows) and refuses. If detection can't run
  (unknown platform / tool missing), it refuses unless the user asserts
  `PR_SENTINEL_ASSUME_APP_QUIT=1`.
- **Never touch OPEN / running sessions by default.** Default scope is MERGED
  only. OPEN sessions are edited only under the explicit `--all` opt-in.
- **Schema-verify, no-op on the unknown.** Only files that parse as an object
  containing an `autoFixEnabled` boolean are ever touched. If none are found the
  helper says so plainly (schema may have changed) and edits nothing.
- **Report grouped by PR state / repo**, count updated, backup path echoed.

Out of scope (noted in the issue as optional): the GitHub-side conversation
lock, and interactive per-session select. Left as possible future opt-ins;
excluded here for smallest-change + secure-by-default.

## Tests

`tests/test_migrate.py` — runs the script as a subprocess against a fixture
session store (via `PR_SENTINEL_SESSIONS_ROOT`):

- dry-run reports the right grouped counts and mutates nothing;
- `--apply` (with `PR_SENTINEL_ASSUME_APP_QUIT=1`) flips only MERGED enabled
  sessions, backs up originals, and leaves OPEN / already-disabled / unknown-
  schema files untouched;
- `--all` additionally disarms OPEN sessions;
- a store with no recognizable schema is a clean no-op.

`tests/test_wiring.py` — script is present + executable; the command file exists.

## Status

Shipped in this PR.
