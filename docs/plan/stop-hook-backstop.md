# Plan — Q1 / R1: Stop-hook backstop

**Goal.** Add a `Stop` hook that blocks the stop **once** — nudging the session
to launch the watcher — when the session created an open PR, that PR isn't yet
concluded, and no live watcher is running for it. Turns the advisory PostToolUse
nudge into a reliable backstop.

**Non-goal.** Querying GitHub for check status (network call) or reading the PR
body/comments (the excluded injection channel). Both are hard invariants.

## Design decisions (the open problems from R1/STATUS)

### 1. Detect a running watcher without a network call — process enumeration

A live watcher is a running `pr-sentinel-watch.sh <PR>` OS process (a sleeping
background bash task launched by the session). We enumerate local processes with
`ps` and look for a command line containing `pr-sentinel-watch.sh` and the PR
identifier. This is:

- **Decisive** — it answers "is a watcher running *right now*", not "was one
  ever launched", so a watcher that exited (delivered its event) and wasn't
  relaunched correctly reads as *not live*.
- **Local** — `ps` reads the local process table; no network, no PR text.
- **Schema-independent** — no dependence on the transcript's background-task
  completion representation.

`ps` is factored behind a seam: a pure `watcher_prs_from_ps(ps_output)` parser
plus a thin `running_watcher_prs()` that shells out to `ps ax`. Tests drive the
parser directly and drive end-to-end runs with a **stub `ps` on PATH** (mirrors
`test_watcher.py`'s stub-`gh`). If `ps` is missing or errors, we cannot confirm
liveness → **fail-open: allow the stop** (never falsely block a watched session).

### 2. Identify the session's own PR without a network call or comments

Parse the `Stop` hook's `transcript_path` (a local JSONL file). Correlate each
`Bash` `tool_use` with its `tool_result` by `tool_use_id`, then:

- **created PRs** — a `gh pr create` command whose result text contains a
  `github.com/<o>/<r>/pull/<N>` URL. The number comes from the URL `gh` itself
  printed — the same GitHub-controlled signal the PostToolUse hook already uses.
  We never read the PR body or comments; only the session's own command text and
  the URL in its own stdout.
- **concluded PRs** — a PR is treated as concluded (don't block) if the
  transcript contains a watcher `PR-SENTINEL EVENT: ready` / `closed` report for
  it, or a `gh pr merge` / `gh pr close` command targeting it.

A PR is **active** (may warrant a block) if it was created this session and is
not concluded. Only `gh pr create` (which yields a number locally) is used; a
bare `git push` gives no number without a network call, so it can't anchor a
block — a documented fail-open gap.

### 3. "Required checks still pending" — an honest local approximation

We cannot verify check status without a network call, which is excluded. So we
approximate "checks pending" as "the PR is created, not known-concluded locally,
and unwatched." The block is safe under this approximation because it fires **at
most once** and only asks the session to launch the watcher — which then
authoritatively determines check state and will itself exit `ready` if the PR is
already green. Worst case: one extra, non-looping nudge.

### 4. Fail-open and the no-loop guarantee

- `stop_hook_active == true` → **allow** immediately. This is the loop breaker:
  a stop that is itself a continuation of a prior stop-hook block never blocks
  again.
- Any uncertainty — unparseable stdin, unreadable transcript, no created PR, a
  concluded PR, a live watcher, or `ps` unavailable — → **emit nothing / allow**.
- `PR_SENTINEL_DEBUG=1` re-raises; otherwise the hook exits 0 on any exception.
- `PR_SENTINEL_DISABLE=1` disables the hook (parity with the PostToolUse nudge).

## Block condition (all must hold)

1. `stop_hook_active` is not true, and `PR_SENTINEL_DISABLE` is unset.
2. The transcript shows ≥1 **active** created PR (created, not concluded).
3. **None** of those active PRs has a live watcher process.
4. → emit `{"decision": "block", "reason": <nudge naming the watcher command>}`.

Otherwise emit nothing (allow the stop).

## Files

- **`scripts/pr-sentinel-stop-hook.py`** — new, stdlib-only, fail-open. Single
  responsibility: the Stop backstop. Mirrors the existing hook's structure.
- **`hooks/hooks.json`** — add a `"Stop"` key (Stop hooks take no matcher).
- **`tests/test_stop_hook.py`** — new suite: unit tests for the transcript
  parser and the `ps` parser; subprocess end-to-end tests with a stub `ps` and a
  synthetic transcript covering block / allow paths.
- **`tests/test_wiring.py`** — assert the `Stop` registration points at the real
  script.
- **Docs** — `README.md` (Stop-hook row in the decision tables, move it out of
  Limitations/Roadmap "not yet built"), `docs/DESIGN.md` (mechanism note if
  changed), `docs/STATUS.md` (remove Q1 — isolated commit).

## Test scenarios

- created PR + no watcher process + not concluded → **block** (reason names the
  watcher and the PR).
- created PR + live watcher process (stub `ps` shows it) → **allow**.
- created PR + watcher reported `ready`/`closed` in transcript → **allow**.
- `stop_hook_active: true` → **allow** (no-loop).
- `PR_SENTINEL_DISABLE=1` → **allow**.
- no `gh pr create` in transcript → **allow**.
- unparseable stdin / missing transcript → **allow**, exit 0.
- parser units: correlate create→URL; ps line → PR set; concluded via event and
  via `gh pr merge`.

## Coordination with Q2

Q2 adds a `"PreToolUse"` key to `hooks/hooks.json` and its own README decision
row + STATUS edit. Keep this change's `hooks.json` and README edits minimal and
localized (append a sibling top-level key; add one table row) so Q2 merges
cleanly.
