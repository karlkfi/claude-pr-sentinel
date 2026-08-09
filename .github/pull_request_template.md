<!--
Delete any section that doesn't apply. The checks below exist because each one
has been missed at least once and cost a release to catch.
-->

## What changed

<!-- One or two sentences. Link the issue it fixes. -->

## Does this add a GitHub read?

<!--
If the watcher issues a `gh` query, `gh api` call, or GraphQL read it didn't
before, PRIVACY.md's watcher enumeration must list it. That list is the
document's whole value, and it is not derivable from the diff by anyone
reviewing later.

Answer "no" and delete the rest, or say what was added and confirm PRIVACY.md
is updated.
-->

## Checks

- [ ] `make check` is green (shellcheck + `python3 -m unittest discover tests`)
- [ ] No PR/issue **comment or body** ingestion added — the injection channel this plugin excludes by design
- [ ] No auto-merge, and no path that could learn to merge
- [ ] New watcher event or env-var knob is documented in `README.md` (decision tables / Configuration)
- [ ] New GitHub read is listed in `PRIVACY.md`
