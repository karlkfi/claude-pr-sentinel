# Release notes

One file per tag, holding the **verbatim body** of that version's GitHub
Release. Publish from the file, never by typing into the web form:

```
gh release create v<X.Y.Z> --title "v<X.Y.Z>" --notes-file docs/releases/v<X.Y.Z>.md
gh release edit   v<X.Y.Z> --notes-file docs/releases/v<X.Y.Z>.md   # to amend
```

Editing a body on the Release page puts the text through no diff, so wrong
counts, dead links, and mismatched PR numbers ship unreviewed. In-repo makes
each fix a diff and each published body reproducible from a commit.

## Conventions

- **No title heading.** The Releases page renders the tag as the page `<h1>`, so
  a leading `# v0.8.0` duplicates it one line down.
- **No hard wrapping.** A release body is rendered with GitHub's
  *comment-flavour* GFM, where a single newline becomes `<br>`. Keep every
  paragraph, blockquote, and list continuation on one line.
- **No in-page anchors.** Headings in a release body carry no `id`, so
  `[Upgrading](#upgrading)` is dead. Refer to a section by name in bold.
- **Link the tag, not `main`.** A reader of v0.7.0's notes should land on
  v0.7.0's README: `.../blob/v0.7.0/README.md#section`.

## The invariant

**This file matches the published body** — not "this file is frozen at the
tag". A body amended after publishing (say to correct a count) is amended here
in the same change, which means the tagged copy of a notes file can lag the
published one. That is intended; don't "fix" it by reverting.

## Checking a file before publishing

Hard wrapping has to be checked against the *renderer*. `gh release view --json
body` returns raw Markdown, which never contains `<br>` however badly the file
is wrapped, so grepping that is a check that cannot fail:

```
gh api -X POST /markdown -f mode=gfm -f "text=$(cat docs/releases/v0.8.0.md)" | grep -c '<br>'
```

Zero is the passing answer. `mode=gfm` is the comment flavour; `mode=markdown`
is not, and reports 0 on a hard-wrapped file.

## Known defect in a backfilled body

`v0.3.0.md` is hard-wrapped, and its published body renders with 14 spurious
`<br>` tags. It is stored faithfully rather than repaired, so the invariant
above holds. Fixing it means republishing that release's body, which is a
deliberate edit to a shipped artifact — not something to do in passing.
