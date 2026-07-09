# Plan: dampen repeat `check_failure` blocks (issue #9, fix B)

## Goal

Stop the Stop-hook livelock when a PR has a failing check the session cannot
fix (base-inherited, out-of-scope, external, or misconfigured — issue #9
classes 1–4). Break the loop without letting a session *silently* abandon a red
PR, and without base-branch inspection (which cannot detect classes 2–4).

## Approach

Give the session exactly **one** block to attempt a fix, then stop re-blocking
if nothing changed. A `check_failure` is "unchanged" iff a later watcher report
for the same PR carries the **same failed-check set** and the **same head SHA**.
Two identical reports ⇒ the session pushed no fix (a fix moves the SHA) ⇒
dampen: allow the stop, but surface a non-blocking `systemMessage` so the red PR
is never abandoned silently.

This is deliberately state-derived from the transcript — no new marker files, no
counters to persist. The signature (failed-set + SHA) already lives in the
reports the session read.

## Why this shape

- **One block, not zero:** the first `check_failure` still blocks with full
  detail (launch/act instruction). Only the *repeat* is dampened. A session
  that can fix the check pushes, moving the SHA, so the next report has a new
  signature and blocking resumes on the genuinely-new state.
- **SHA is the "did the session act?" signal.** Same SHA across two reports is
  proof no fix landed. It also covers classes 2–4 that no base inspection can
  see — this is why B is the general fix and A only narrows the common case.

## Changes

### watcher — `scripts/pr-sentinel-watch.sh`
- Add `headRefOid` to `gh_pr_state`'s `--json` list (GitHub-controlled
  metadata; safe under the never-widen-to-writable-fields rule) and to the
  `IFS` read.
- Print `Head SHA: <oid>` in `emit_check_failure`'s **header** (above the CI-log
  excerpt banner, so it is trusted under the #10 anchoring rule).

### hook — `scripts/pr-sentinel-stop-hook.py`
- Parse, per PR, the `check_failure` signature `(failed-set, head-sha)` from the
  **header region** of each read of that PR's own watcher outfile
  (`_report_header_region`, from the #10 fix — this branch is stacked on it).
- A PR is **dampened** iff ≥2 reads share an identical non-empty signature.
- `prs_needing_watcher` returns the block set as before, now minus dampened PRs.
  `main()` emits a `systemMessage` naming any dampened red PR so the warning
  survives the un-block.

## Security invariants preserved
- No foreground polling; no auto-merge; no PR body/comment ingestion.
- Signature is parsed only from the header region — a forged `Failed checks:` or
  `Head SHA:` line in a CI-log excerpt cannot manipulate dampening (#10).
- `systemMessage` is informational and fails safe: if the harness drops it, the
  loop is still broken and the red PR was already surfaced by the first block.

## Tests
- watcher: `emit_check_failure` fixture asserts the `Head SHA:` line.
- hook:
  - two identical `check_failure` reads ⇒ dampened (allow) + warning present.
  - two reads with a **moved SHA** ⇒ still blocks (fix attempt in flight).
  - two reads with a **different failed-set** ⇒ still blocks.
  - a forged `Failed checks:`/`Head SHA:` in an excerpt ⇒ ignored (anchoring).
  - single `check_failure` ⇒ still blocks (one-block-to-try semantics).

## Out of scope (tracked elsewhere)
- Fix A (base-inherited detection) — issue #9, follow-up.
- Fix C (`acknowledged` terminal state) — issue #9, only if A+B insufficient;
  must inherit the #10 anchoring rule.
