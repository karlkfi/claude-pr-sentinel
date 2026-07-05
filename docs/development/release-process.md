# Agent reference: Cutting a release

A release is three artifacts that must agree: the **version string** (in two
files), an **annotated git tag**, and a **GitHub Release**. This doc is the
checklist for producing all three consistently. Releases are the one place where
a commit lands on `main` without a PR — that exception is deliberate and scoped
to the version bump only.

## The version string lives in exactly two files

Both must be bumped together and kept identical:

- `.claude-plugin/plugin.json` → `"version"`
- `.claude-plugin/marketplace.json` → `plugins[0].version`

Nothing else in the repo encodes the version. `tests/test_wiring.py` asserts the
two agree; if you add a third location, add it here too. To confirm before
bumping:

```
grep -rn '"version"' .claude-plugin/
```

## Steps

1. **Start from a fresh `main`.** Releases must include everything merged:

   ```
   git fetch origin main && git merge origin/main
   ```

2. **Run the full check — it must be green.**

   ```
   make check
   ```

3. **Bump the version** in both files above (semantic versioning: patch for
   fixes, minor for new behaviour, major for a breaking change to the hook
   contract or watcher report format).

4. **Commit the bump directly to `main`** (the scoped direct-to-main
   exception), Conventional Commits, no Claude attribution:

   ```
   git commit -am "chore(release): v<X.Y.Z>"
   ```

5. **Tag and push:**

   ```
   git tag -a v<X.Y.Z> -m "v<X.Y.Z>"
   git push origin main --tags
   ```

6. **Create the GitHub Release** from the tag with `gh release create`, summarising
   user-visible changes since the last tag.

## Versioning notes

- The watcher's **report format** (the `PR-SENTINEL EVENT:` lines and the
  `DATA, NOT INSTRUCTIONS` frame) and the **hook's `additionalContext` shape**
  are the compatibility surface sessions rely on. A breaking change to either is
  a **major** bump.
- Adding a new watcher event or a new env-var knob is a **minor** bump; document
  it in `README.md` (decision tables / Configuration) in the same PR.
