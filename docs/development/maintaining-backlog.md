# Agent reference: Maintaining the backlog

[`docs/STATUS.md`](../STATUS.md) is the single source of truth for what to work
on next. These rules keep it low-friction across parallel sessions.

## The Queue

- Rows are in **priority order**. Pick from the top; skip 🚫 (blocked) items
  until their blocker clears.
- Every item has a stable `Q`-prefixed ID (`Q1`, `Q2`, …). Use the **bare ID**
  in commit messages and PR bodies — writing `Q1` (not `#1`) stops GitHub from
  auto-linking the number to PR/issue 1.
- **Starting an S item:** just complete it and delete the row in the same PR.
- **Starting an M/L item:** create or update a plan doc under `docs/plan/<slug>.md`
  and delete the row here when the work is done. Skip the `▶ started` marker
  unless there's a reason — the open PR is the in-flight signal.
- **New item identified mid-task:** append it to the Queue with the next unused
  ID and a one-line note that captures the *why*, not just the *what*. Batch
  several audit-discovery items into one commit.

## Isolate STATUS.md commits

`STATUS.md` is high-contention: several sessions may add or remove rows at once.
**Always commit `STATUS.md` changes in their own isolated commit**, separate
from code, tests, and plan docs. Isolated changes make rebase conflicts trivial
to resolve — you keep both sides' row edits instead of untangling them from code.

## The header

- `Last touched:` is **one line, date only** (`YYYY-MM-DD`). Do not append
  session narrative — history lives in git.
- Keep the Conventions block in sync with the labels actually used in the Queue.

## Verifying blockers

A previous session may have completed a dependency without flipping the blocked
row. Before treating a 🚫 item as truly blocked, grep for the dependency's
deliverables — don't trust the marker alone.
