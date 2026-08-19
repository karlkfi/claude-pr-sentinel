# Maintaining the backlog

[`docs/STATUS.md`](../STATUS.md) follows the
[**`session-backlog` skill's**](skills.md#session-backlog) format and process:
a priority-ordered Queue with stable Q-IDs, a Deferred table with concrete
revive triggers, delete-on-done (git history is the archive). See that skill
for adding, picking, completing, deferring, and grooming. IDs are claimed on
the remote rather than read from a counter — see Allocating an ID below.

The essentials, for sessions without the skill loaded:

- Pick from the top of the Queue; run `gh pr list` first — an open PR is the
  in-flight signal. Only two Queue states: 🔲 ready · 🚫 blocked.
- New item: claim an ID (below), then insert the row at the position its
  priority deserves and raise `**Next ID:**` above it.
- Done item: delete the row. Reference rows by bare Q-ID (`Q4`, never `#4`)
  in commits and PR bodies.
- **Commit `STATUS.md` changes in their own isolated commit**
  (`docs(status): …`), never mixed with code.

## Allocating an ID

```bash
./scripts/alloc-queue-id.sh --table docs/STATUS.md 'The row title'
```

One ID per title, printed to stdout. There is nothing to release: a session
that claims an ID and dies strands it, which is intended, because reclaiming
would mean deciding a claim is stale and the session holding it is the one you
cannot ask.

**The counter is not an allocator.** Two sessions filing rows read the same
`**Next ID:**`, both take it, and both branches lint clean because each is
internally consistent — the duplicate exists only in the merged set, where the
rows sit at different table positions and git has nothing to conflict on. That
is how three rows numbered Q13 reached `main`, and then three more numbered
Q16 within the hour.

Claiming pushes a blob to `refs/queue-ids/QN` carrying
`--force-with-lease=<ref>:`, an empty expectation meaning *this ref must not
exist*, which the receiving end enforces. Two sessions asking at the same
instant are therefore handed different IDs with no lock and no coordinator.
The blob is unique per attempt on purpose: every form of `git push`
short-circuits when the ref already points at the object being pushed, sending
nothing and exiting 0 ahead of the lease check, so a shared claim object would
report success to the loser of every race.

Clones never fetch this namespace, so a local `git for-each-ref` reads empty
whatever has been claimed. Ask the remote:

```bash
git ls-remote origin 'refs/queue-ids/*'
```

`**Next ID:**` stays in the file because the linter requires it, and it now
records the floor rather than handing out the next value. Raise it above the
ID you claimed in the same edit that files the row.

Every edit is linted by [`scripts/lint-backlog.sh`](../../scripts/lint-backlog.sh)
(vendored from the skill), enforced as a pre-commit gate via
`.githooks/pre-commit`. One-time setup per clone:

```bash
git config core.hooksPath .githooks
```

Companion scripts: `scripts/next-task.sh` prints the top ready item (prompt
and session title), `scripts/backlog-metrics.sh` reports throughput, cycle
time, and aging from git history, `scripts/alloc-queue-id.sh` claims an ID.
All four are vendored from the skill; `tests/test_alloc.py` covers the
allocator against a throwaway remote.
