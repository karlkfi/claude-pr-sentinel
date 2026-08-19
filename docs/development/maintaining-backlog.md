# Maintaining the backlog

The backlog is a per-item store under [`docs/queue/`](../queue/README.md),
following the [**`session-backlog` skill's**](skills.md#session-backlog) format
and process: one file per item, stable Q-IDs claimed on the remote, priority in
each item's `rank` key, delete-on-done (git history is the archive). See that
skill for adding, picking, completing, deferring, and grooming.

The essentials, for sessions without the skill loaded:

- Read the backlog with `python3 scripts/queue.py render`, never by listing the
  directory — a listing sorts `Q1, Q10, Q11, Q2`, which is neither priority nor
  number.
- Pick with `python3 scripts/queue.py next`; run `gh pr list` first, because an
  open PR is the in-flight signal. Three statuses: `ready`, `blocked`,
  `deferred`.
- New item: claim an ID (below), compute a rank with `queue.py rank`, then
  write `docs/queue/QN.md`. Never hand-type a rank.
- Done item: `git rm docs/queue/QN.md`. Reference items by bare Q-ID (`Q4`,
  never `#4`) in commits and PR bodies.

Backlog edits need no isolated commit. That rule existed so a rebase conflict
in one shared table could be resolved with `git checkout --ours`; an item owns
a file, so there is no shared file left to contend on. It retired with the
table.

## Allocating an ID

```bash
./scripts/alloc-queue-id.sh 'The item title'
```

One ID per title, printed to stdout. There is nothing to release: a session
that claims an ID and dies strands it, which is intended, because reclaiming
would mean deciding a claim is stale and the session holding it is the one you
cannot ask.

**Never read a number and add one.** A counter is not an allocator: two
sessions read the same value, both take it, and both branches lint clean
because each is internally consistent — the duplicate exists only in the merged
set. Under the old table that produced three items numbered Q13, and then three
more numbered Q16 within the hour.

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

## Checks

`python3 scripts/queue.py lint` checks the store and is enforced as a
pre-commit gate via `.githooks/pre-commit`. One-time setup per clone:

```bash
git config core.hooksPath .githooks
```

Read the item count it prints, not just its exit code: a directory holding no
items reports `0 item(s) OK`, so the exit status alone cannot tell a clean
store from one the linter never read.

`core.hooksPath` is relative, and git resolves it against the **primary**
checkout rather than the worktree you are in — so every worktree runs the
hook file as it exists on the primary checkout's branch, not its own. A change
to `.githooks/pre-commit` therefore takes effect only once that checkout has
it, and until then a worktree runs the old hook. Run `make check` when the
hook itself is what changed.

CI runs the same lint on the pull request's **merge result** —
`actions/checkout` resolves `refs/pull/N/merge` — which is the only place a
defect that exists solely in the merged set is visible. It also runs
`queue.py claims`, so an ID a branch adds without claiming it is caught, and
prints the ordered backlog to the job summary. The pre-commit hook cannot stand
in for either: it sees one branch's files, and `git rebase --continue` skips it
entirely.

## Reading the backlog

```bash
python3 scripts/queue.py render            # ordered, ready items
python3 scripts/queue.py render --all      # including deferred
python3 scripts/queue.py render --format table --all
```

The rendered index is built, never committed — a tracked index would be the one
file every completing session has to edit, which is exactly the contention the
store removes. `tests/test_queue_store.py` fails if one reappears. Every pull
request publishes the ordered table to its CI job summary, so it is readable
from the Actions tab without a checkout.

Companion scripts: `scripts/queue.py` reads, checks, orders and migrates the
store; `scripts/alloc-queue-id.sh` claims an ID. Both are vendored from the
skill. `tests/test_alloc.py` covers the allocator against a throwaway remote,
and `tests/test_queue_store.py` covers the store's invariants.
