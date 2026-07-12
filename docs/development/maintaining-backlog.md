# Maintaining the backlog

[`docs/STATUS.md`](../STATUS.md) follows the **`backlog` skill's** format and
process (installed globally at `~/.claude/skills/backlog`): a priority-ordered
Queue with stable Q-IDs allocated from the `**Next ID:**` counter, a Deferred
table with concrete revive triggers, delete-on-done (git history is the
archive). See that skill for adding, picking, completing, deferring, and
grooming.

The essentials, for sessions without the skill loaded:

- Pick from the top of the Queue; run `gh pr list` first — an open PR is the
  in-flight signal. Only two Queue states: 🔲 ready · 🚫 blocked.
- New item: take the ID from `**Next ID:**`, bump the counter in the same
  edit, and insert the row at the position its priority deserves.
- Done item: delete the row. Reference rows by bare Q-ID (`Q4`, never `#4`)
  in commits and PR bodies.
- **Commit `STATUS.md` changes in their own isolated commit**
  (`docs(status): …`), never mixed with code.

Every edit is linted by [`scripts/lint-backlog.sh`](../../scripts/lint-backlog.sh)
(vendored from the skill), enforced as a pre-commit gate via
`.githooks/pre-commit`. One-time setup per clone:

```bash
git config core.hooksPath .githooks
```

Companion scripts: `scripts/next-task.sh` prints the top ready item (prompt
and session title), `scripts/backlog-metrics.sh` reports throughput, cycle
time, and aging from git history.
