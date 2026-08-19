# Privacy Policy — pr-sentinel

_Last updated: 2026-08-18_

pr-sentinel is a Claude Code plugin that runs on your local machine. Its
components have different data profiles, described honestly below: two hooks
and a migration helper that never leave your machine, one watcher that talks to
GitHub, and one hook that asks GitHub a single narrow question before you open
a pull request.

## Data we collect

None. The plugin has no analytics, no telemetry, and no data collection of any
kind. It ships as one bash watcher and a few stdlib-only Python scripts (the
three hooks and the migration helper).

## The PostToolUse hook (`scripts/pr-sentinel-hook.py`)

- Runs **entirely locally with no network access.**
- Receives, via standard input, the Bash command Claude Code just ran and that
  command's output text, plus `CLAUDE_PLUGIN_ROOT` and the optional
  `PR_SENTINEL_*` configuration values (via environment).
- Processes these **in memory** to decide whether to emit an advisory nudge,
  then writes that nudge (or nothing) to standard output.
- Writes nothing to disk. On a `git push` it may run `git rev-parse` and
  `git symbolic-ref` in your working directory, to tell a tag from a branch and
  to learn which branch the remote calls its default — reads of your local
  repo's refs, nothing else. The only PR data it handles is a PR URL that the
  command itself already printed, which it echoes back in the nudge.
- Reads your Claude Code **session transcript** (the harness supplies the path)
  for one thing only: the watchers this session launched and which of them have
  reported completion, so it does not ask for a second watcher on a pull
  request one is already watching. It extracts the PR number, the background
  task id, and nothing else.

## The PreToolUse hook (`scripts/pr-sentinel-guard.py`)

- Denies a Bash command that would foreground-poll continuous integration (CI),
  auto-approves the plugin's own watcher launch, and denies a `gh pr create`
  that would open a second pull request over lines an open one already changes.
- Receives, via standard input, the Bash command Claude Code is about to run
  and your working directory, plus `CLAUDE_PLUGIN_ROOT` and the optional
  `PR_SENTINEL_*` configuration values (via environment). It inspects the
  command string only — never the command's output.
- Reads your **session transcript** for the same narrow fact the PostToolUse
  hook reads it for — which watchers are still running — so it can refuse a
  duplicate watcher on a PR already covered.
- **Runs locally for every decision except the overlap check.** The poll deny,
  the auto-allow and the duplicate deny make no network call at all.
- **The overlap check talks to GitHub**, through your already-authenticated
  `gh` CLI, and only when you run a `gh pr create`. It reads your local repo
  with `git diff` / `git merge-base` to learn which lines this branch changes,
  then issues two read-only queries:
  - `gh pr list --json number,headRefName,files` — the open PRs, their head
    branch names, and the paths each one touches;
  - `gh pr diff <number>` — the diff of at most **three** PRs, and only ones
    that already share a path with your branch, to compare line ranges rather
    than filenames.
- That field list is the whole of it. The check **never** requests the PR body,
  review comments, issue comments, or even the PR **title** — a title is
  written by its author, and the denial message goes straight into your
  session's context. A denial names the PR **number** and the shared **paths**
  from your own diff, nothing else.
- Every probe **fails silent**: no `gh`, no credentials, an unresolvable base
  ref, or a slow call costs a missed catch, never a blocked command. Set
  `PR_SENTINEL_OVERLAP_ENABLED=false` to switch the check — and its two
  queries — off entirely.
- Writes nothing to disk, and emits only its allow/deny decision to standard
  output.

## The watcher (`scripts/pr-sentinel-watch.sh`)

- **Talks to GitHub** through your already-authenticated `gh` CLI — this is its
  purpose. It issues read-only queries for:
  - the PR's `state`, `mergeStateStatus`, base branch name, head commit, and
    canonical URL (`gh pr view --json`);
  - whether the PR currently holds a merge-queue entry (a GraphQL
    `mergeQueueEntry` read);
  - who removed the PR from the merge queue — the `__typename` and `login` of
    the `actor` on the most recent `REMOVED_FROM_MERGE_QUEUE_EVENT` (a GraphQL
    `timelineItems` read), so the report can tell a queue eviction from a person
    removing it deliberately. Read once, when that event is reported, never
    per poll. The event's own free-form text is not requested;
  - the PR's check results (`gh pr checks`);
  - a failing check's workflow run — its conclusion, and its workflow id and
    that workflow's latest completed run on the **base branch**, to tell a
    failure the PR caused from one it inherited — only on a failure;
  - a failing run's step log (`gh run view --log-failed`), only on a failure.
- It **never** requests or parses the PR body, PR review comments, or issue
  comments. It reads GitHub-controlled check metadata and merge state only.
- All network traffic is between your machine and GitHub, via `gh`, under your
  own credentials. The plugin adds no other endpoint, no telemetry, and no
  third party.
- It writes nothing to disk. The failing-run log excerpt is sanitized
  (ANSI-stripped, size-capped) and printed to the background task's standard
  output, which the Claude Code harness delivers to your session.

## The Stop hook (`scripts/pr-sentinel-stop-hook.py`)

- Runs **entirely locally with no network access.** It blocks the end of a turn
  once when the session has an open PR that nothing is watching.
- Reads three local files, all of them paths the session itself produced: your
  Claude Code **session transcript** (the harness supplies the path), each
  **watcher's own output file** (the path is in the background task's completion
  notification), and, when a `gh pr create` sent its output to a log rather than
  to the transcript, **that log** — the path is parsed out of the create's own
  command string, the read is capped at 8 KiB, and a file older than the create
  is ignored so a reused log path cannot donate a stale PR URL.
- Extracts nothing from the redirected log but a `github.com` PR URL. It does
  **not** read PR bodies or comments, and it inspects no process table.
- Writes nothing to disk, and emits only its block decision and an optional
  non-blocking notice to standard output.

## The migration helper (`scripts/pr-sentinel-migrate-autofix.py`)

- Runs **entirely locally with no network access.** You invoke it manually (or
  via the `/pr-sentinel-migrate-autofix` command) — it is not a hook and does
  not run on its own.
- Reads the Claude **desktop app's** own session files under its
  `claude-code-sessions` store to find the `autoFixEnabled` toggle, along with
  the sibling `prState` / `prRepository` / `prNumber` / `title` fields it uses
  to filter and to print a report. It does **not** read PR bodies or comments.
- With `--apply` it **writes to disk** — the only component that does: it backs
  up each targeted file under `.autofix-backup-<timestamp>/` before setting
  `autoFixEnabled` to `false`. This is local file editing under your own
  account; nothing leaves the machine.

## Third parties

The plugin shares no data with any third party. Its only network peer is
GitHub, reached through the `gh` CLI you have already authenticated — the
watcher on every poll, and the PreToolUse hook's overlap check when you open a
pull request.

## Changes to this policy

Updates will be published in this file in the project repository, with the date
above revised accordingly.

## Contact

Questions or concerns:
<https://github.com/karlkfi/claude-pr-sentinel/issues>
