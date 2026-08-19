# The backlog

One file per item, `QN.md`, each carrying its own priority in a `rank` key.
There is no table and no counter: an item owns a file, so two sessions working
different items never touch the same path.

**A directory listing is not the backlog.** It sorts `Q1, Q10, Q11, Q2`, which
is neither priority nor number. Render the real order:

```bash
python3 scripts/queue.py render
```

`--all` includes deferred items, and `--format table` emits Markdown. Every
pull request also prints the ordered backlog to its CI job summary, so you can
read it from the Actions tab without a checkout.

**The rendered index is never committed.** A tracked index would be the one
file every completing session has to edit, which is the contention this layout
exists to remove. `tests/test_queue_store.py` fails if one reappears.

## Filing an item

```bash
./scripts/alloc-queue-id.sh 'The item title'      # claims the id on the remote
python3 scripts/queue.py rank --head              # or --tail, --after, --before
```

Then write `docs/queue/QN.md` with the frontmatter below and run
`python3 scripts/queue.py lint`. Never hand-type a rank.

```yaml
---
id: Q42
rank: a0
labels:
    - tests
status: ready          # ready | blocked | deferred
size: S                # S = one session/PR · M = 2–3 sessions · L = needs a plan doc
---

# The title, 72 characters at most

The body, as prose. No length cap — the index summarises it and this page
carries the whole thing.
```

Labels in use: `security` `tests` `docs` `infra` `bug` `watcher` `hook`
`retro`. Add `open-question` to an item that ends in a decision rather than a
next step, and drop it in the same edit that writes the answer in.

## The rest of the process

Picking, completing, deferring and grooming are unchanged and live in
[`../development/maintaining-backlog.md`](../development/maintaining-backlog.md).
Completing an item is `git rm docs/queue/QN.md` — git history is the archive.
