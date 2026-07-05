---
description: Disable Claude Desktop auto-fix (autoFixEnabled) on existing sessions when migrating to pr-sentinel
argument-hint: "[--all]"
allowed-tools: Bash(python3 *)
---

The user is migrating to pr-sentinel and wants to disable Claude Desktop's
"Auto-fix CI & address comments" (`autoFixEnabled`) on their **existing**
sessions. That toggle wakes a credentialed local agent on the PR comment stream
— the injection channel pr-sentinel exists to avoid — and installing the plugin
does **not** turn it off, so old/merged-PR sessions stay armed.

The helper is `${CLAUDE_PLUGIN_ROOT}/scripts/pr-sentinel-migrate-autofix.py`. It
is dry-run by default and only ever edits files that match the expected schema.

Do this:

1. **Run the read-only dry run** and show the user the result. This is safe
   while the app is running (it reads, never writes). Pass through `--all` only
   if the user included it in `$ARGUMENTS`:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pr-sentinel-migrate-autofix.py" $ARGUMENTS
   ```

   Summarize the grouped counts (by PR state / repo) and how many sessions would
   be disabled. Default scope is **MERGED** PRs; `--all` widens to every enabled
   session (including OPEN ones the user may still be working on).

2. **Hand off the apply step — do not run it yourself.** Applying requires the
   Claude desktop app to be **quit** (the running app rewrites these files and
   would silently clobber the edit). If this session is inside the desktop app,
   quitting the app ends this session, so the user must run the apply command
   themselves from an external terminal (e.g. Terminal.app). Tell them to:

   1. Quit the Claude desktop app (Cmd-Q).
   2. Run, in a separate terminal:

      ```
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pr-sentinel-migrate-autofix.py" --apply
      ```

      (add `--all` to include non-merged sessions). It backs up every edited
      file first and prints the backup directory.
   3. Relaunch the app and spot-check a couple of the listed sessions — the
      toggle should now be off.

Never edit these files yourself, never widen the scope beyond what the user
asked, and never touch OPEN-PR sessions unless the user explicitly passed
`--all`.
