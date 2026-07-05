# Plan — Q1 / R1: Stop-hook backstop

**Goal.** Add a `Stop` hook that blocks the stop **once** — nudging the session
to launch the watcher — when the session created an open PR, that PR isn't yet
concluded, and no live watcher is running for it. Turns the advisory PostToolUse
nudge into a reliable backstop.

**Non-goal.** Querying GitHub for check status (network call) or reading the PR
body/comments (the excluded injection channel). Both are hard invariants.

## Design decisions (the open problems from R1/STATUS)

### 1. Detect a running watcher without a network call — from the transcript

> **Revised after review.** The first cut enumerated local processes with `ps`.
> That was dropped: for a security-posture plugin, shelling out to read the whole
> process table (every app's / user's command line, some carrying secrets in
> argv) is off-brand even when we only grep for our own script name, and it adds
> a subprocess plus flag-portability and false-match warts. The signal is
> available in the transcript we already parse, so we use that instead.

A `run_in_background` launch of `pr-sentinel-watch.sh <PR>` records a `tool_use`
id; when that background task exits, the harness records a `<task-notification>`
(a `queue-operation`/`attachment`) carrying the same `<tool-use-id>`, an
`<output-file>`, and a `<status>`. So a watcher is **live** iff its launch id has
no task-notification yet. This is:

- **Decisive** — it answers "did this watcher exit", not "was one ever launched",
  so a watcher that already exited (and wasn't relaunched) reads as *not live*
  and a session that stopped mid-fix is nudged.
- **Confined** — reads only the transcript the hook is already handed; no process
  table, no subprocess, no new capability beyond PR identification.
- **Spoof-resistant** — the task-notification is harness-generated, so untrusted
  CI-log text can't forge a "completion". The `ready`/`closed` handed-off signal
  is trusted only when read back from that watcher's *own* output file (path from
  the notification), not from a free-text corpus scan.

`prs_needing_watcher(transcript)` returns the block set directly (opened −
concluded − live). A line pre-filter (only JSON-parse lines carrying a needle:
`pr-link`, `pr-sentinel-watch.sh`, `PR-SENTINEL EVENT`, `task-notification`, a
`gh pr` verb, `/pull/`) keeps it fast on large transcripts. Any I/O trouble →
**fail-open: allow the stop**.

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
  concluded PR, or a still-live watcher — → **emit nothing / allow**.
- `PR_SENTINEL_DEBUG=1` re-raises; otherwise the hook exits 0 on any exception.
- `PR_SENTINEL_DISABLE=1` disables the hook (parity with the PostToolUse nudge).

## Block condition (all must hold)

1. `stop_hook_active` is not true, and `PR_SENTINEL_DISABLE` is unset.
2. The transcript shows ≥1 **active** created PR (created, not concluded).
3. **None** of those active PRs has a live watcher (launch with no completion).
4. → emit `{"decision": "block", "reason": <nudge naming the watcher command>}`.

Otherwise emit nothing (allow the stop).

## Files

- **`scripts/pr-sentinel-stop-hook.py`** — new, stdlib-only, fail-open. Single
  responsibility: the Stop backstop. Mirrors the existing hook's structure.
- **`hooks/hooks.json`** — add a `"Stop"` key (Stop hooks take no matcher).
- **`tests/test_stop_hook.py`** — new suite: unit tests for the classifiers and
  `prs_needing_watcher` over synthetic transcripts (launch / completion /
  scoped-read / spoof cases); subprocess end-to-end tests for the stdin, block
  decision, `stop_hook_active`, disable, and exit-code behavior.
- **`tests/test_wiring.py`** — assert the `Stop` registration points at the real
  script.
- **Docs** — `README.md` (Stop-hook row in the decision tables, move it out of
  Limitations/Roadmap "not yet built"), `docs/DESIGN.md` (mechanism note if
  changed), `docs/STATUS.md` (remove Q1 — isolated commit).

## Test scenarios

- opened PR + no watcher launched + not concluded → **block** (reason names the
  watcher and the PR).
- opened PR + launched watcher, no completion notification → **allow** (live).
- opened PR + watcher exited (notification) but not relaunched → **block**
  (Scenario C: stopped mid-fix).
- opened PR + watcher exited, relaunched → **allow** (relaunch is live).
- opened PR + `ready`/`closed` read from the watcher's own output file → **allow**.
- opened PR + spoofed `ready` in a *different* file (CI log) → **block** (scoped
  concluded ignores it).
- concluded via `gh pr merge`/`close` → **allow**.
- `stop_hook_active: true` → **allow** (no-loop).
- `PR_SENTINEL_DISABLE=1` → **allow**.
- no opened PR in transcript → **allow**.
- unparseable stdin / missing transcript → **allow**, exit 0.

## Coordination with Q2

Q2 adds a `"PreToolUse"` key to `hooks/hooks.json` and its own README decision
row + STATUS edit. Keep this change's `hooks.json` and README edits minimal and
localized (append a sibling top-level key; add one table row) so Q2 merges
cleanly.
