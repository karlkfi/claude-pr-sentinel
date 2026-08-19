# Move the backlog to a per-item store

## Intent

Replace the single Queue table in `docs/STATUS.md` with one file per item under
`docs/queue/`, so concurrent sessions stop contending on one file.

## Why now

Claiming IDs on the remote (`scripts/alloc-queue-id.sh`) stopped two sessions
taking the same number, and the CI backlog lint catches a defect that only
exists in the merged set. Neither touches the reason those defects were
frequent: in a single table, priority *is* line position, and the process aims
every edit at the same end of the file. The hot region and the contended region
are the same region.

Three failure modes survive the collision fix, and all three are structural:

- Two sessions deleting adjacent rows conflict. Git needs one unchanged line
  between two changes, and neighbouring rows leave none.
- Every session that files a row edits the `**Next ID:**` line.
- Reordering rewrites many lines at once, so it is confined to a deliberate
  groom rather than done when it is needed.

Under a per-item store an item owns a file, priority lives in a `rank` key
inside it, and none of the three can happen.

## Scope

In:

- `queue.py migrate docs/STATUS.md` → `docs/queue/Q*.md`; delete the table.
- Vendor `scripts/queue.py`; retire `lint-backlog.sh`, `next-task.sh` and
  `backlog-metrics.sh`, which serve the table only.
- Repoint the pre-commit hook and the CI backlog job at `queue.py lint`.
- Publish the ordered index rather than committing it, plus a guard that it
  has not reappeared.
- Update every doc that names `docs/STATUS.md`.

Out:

- GitHub Pages. The repo is public so Pages is available, but it publishes
  nothing today and a deploy workflow is a bigger surface than this change
  needs. The CI job summary and a local render cover reading the backlog;
  Pages can follow if anyone wants a URL.
- Re-ranking or re-prioritising items. The migration preserves the table's
  order exactly; changing priorities in the same commit would hide whether the
  conversion was faithful.

## Acceptance criteria

- `queue.py render --all` lists the same IDs, in the same order, as the table
  did — verified against `origin/main`'s copy, not from memory.
- `queue.py lint` exits 0 and reports the item count, so an empty store cannot
  pass as a clean one.
- `make check` is green.
- No `docs/STATUS.md`, and no committed rendered index.
- Nothing in the tree still tells a reader to edit a Queue table.

## Verification

The round-trip is the whole proof, so it runs against the live table before the
table is deleted: same ID set, same Queue order, deferred item still deferred.
A latent defect the table never enforced is expected to surface — the title cap
is not a rule `lint-backlog.sh` had — and is fixed as its own change rather
than folded into the conversion silently.
