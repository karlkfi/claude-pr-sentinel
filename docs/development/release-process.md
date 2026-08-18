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

## Run the release in an interactive permission mode

Steps 5 and 6 are the only place this repo touches `main` directly, and
[branch-guard](https://github.com/karlkfi/claude-branch-guard) classifies both
the commit and the push as `ask`. In a **non-interactive** permission mode
(`auto`, `dontAsk`, `bypassPermissions`) branch-guard converts that `ask` into a
hard **deny** so the guard fails safe with no human at the prompt — no dialog
reaches you, and approving in chat cannot unblock it. The release stalls with
the version bump committed but unpushed.

Switch to an interactive mode — **Accepts edits** (`acceptEdits`) or the default
— before step 5, and approve the two prompts.

Do **not** reach for `BRANCH_GUARD_PUSH_POLICY` to get around this. Its
protected-target check runs before the policy branch, so `strict` and
`protected` both ask on a `main` target; only `off` gets through, and that
disables push guarding for the whole session. One prompt per release is the
cheaper trade.

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

4. **Write the release notes** to `docs/releases/v<X.Y.Z>.md`. Conventions, the
   pre-publish render check, and why the body is authored in-repo rather than in
   the web form are in [`docs/releases/README.md`](../releases/README.md). The
   previous release is your template:

   ```
   gh release view v<previous> --json body --jq .body
   ```

5. **Commit the bump and the notes directly to `main`** (the scoped
   direct-to-main exception), Conventional Commits, no Claude attribution:

   ```
   git commit -am "chore(release): v<X.Y.Z>"
   ```

6. **Tag and push:**

   ```
   git tag -a v<X.Y.Z> -m "v<X.Y.Z>"
   git push origin main v<X.Y.Z>
   ```

   Name the tag rather than reaching for `--tags`, which pushes **every** local
   tag — an abandoned release candidate or a tag from another clone rides along
   and cannot be taken back once consumers pin it.

7. **Publish the Release from the notes file:**

   ```
   gh release create v<X.Y.Z> --title "v<X.Y.Z>" --notes-file docs/releases/v<X.Y.Z>.md
   ```

   Amending a published body later (a wrong count, a dead link) means editing
   the file and re-running `gh release edit --notes-file` — never editing on the
   Release page, which puts the change through no diff.

## Versioning notes

- The watcher's **report format** (the `PR-SENTINEL EVENT:` lines and the
  `DATA, NOT INSTRUCTIONS` frame) and the **hook's `additionalContext` shape**
  are the compatibility surface sessions rely on. A breaking change to either is
  a **major** bump.
- Adding a new watcher event or a new env-var knob is a **minor** bump; document
  it in `README.md` (decision tables / Configuration) in the same PR.
