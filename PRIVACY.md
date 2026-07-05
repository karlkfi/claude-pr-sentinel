# Privacy Policy — pr-sentinel

_Last updated: 2026-07-05_

pr-sentinel is a Claude Code plugin that runs on your local machine. It has two
components with different data profiles, described honestly below.

## Data we collect

None. The plugin has no analytics, no telemetry, and no data collection of any
kind. It ships as one bash script and one stdlib-only Python script.

## The PostToolUse hook (`scripts/pr-sentinel-hook.py`)

- Runs **entirely locally with no network access.**
- Receives, via standard input, the Bash command Claude Code just ran and that
  command's output text, plus `CLAUDE_PLUGIN_ROOT` and the optional
  `PR_SENTINEL_*` configuration values (via environment).
- Processes these **in memory** to decide whether to emit an advisory nudge,
  then writes that nudge (or nothing) to standard output.
- Reads no files and writes nothing to disk. The only PR data it handles is a
  PR URL that the command itself already printed, which it echoes back in the
  nudge.

## The watcher (`scripts/pr-sentinel-watch.sh`)

- **Talks to GitHub** through your already-authenticated `gh` CLI — this is its
  purpose. It issues read-only queries for:
  - the PR's `state`, `mergeStateStatus`, and base branch name
    (`gh pr view --json state,mergeStateStatus,baseRefName`);
  - the PR's check results (`gh pr checks`);
  - a failing run's step log (`gh run view --log-failed`), only on a failure.
- It **never** requests or parses the PR body, PR review comments, or issue
  comments. It reads GitHub-controlled check metadata and merge state only.
- All network traffic is between your machine and GitHub, via `gh`, under your
  own credentials. The plugin adds no other endpoint, no telemetry, and no
  third party.
- It writes nothing to disk. The failing-run log excerpt is sanitized
  (ANSI-stripped, size-capped) and printed to the background task's standard
  output, which the Claude Code harness delivers to your session.

## Third parties

The plugin shares no data with any third party. The watcher's only network
peer is GitHub, reached through the `gh` CLI you have already authenticated.

## Changes to this policy

Updates will be published in this file in the project repository, with the date
above revised accordingly.

## Contact

Questions or concerns:
<https://github.com/karlkfi/claude-pr-sentinel/issues>
