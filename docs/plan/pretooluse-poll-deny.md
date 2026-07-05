# Plan — Q2: PreToolUse foreground-poll deny

Backlog item [Q2](../STATUS.md) / Roadmap [R2](../ROADMAP.md).

## Goal (one sentence)

Add a `PreToolUse` hook on `Bash` that **denies** foreground CI-poll commands
and points the fix-it at the background watcher, with a `PR_SENTINEL_OVERRIDE`
escape hatch.

## Approach (three sentences)

A new stdlib-only script `scripts/pr-sentinel-guard.py` classifies the just-
*proposed* Bash command and, when it matches a foreground-poll shape, returns a
`PreToolUse` `permissionDecision: "deny"` whose reason points at the watcher.
The deny is uniform across permission modes — a hard **deny** (not `ask`), so a
`bypassPermissions`/headless run self-corrects instead of stalling on an
unanswerable prompt. `PR_SENTINEL_OVERRIDE=<reason>` (non-empty) downgrades the
deny so the command proceeds under the normal permission system — the same
escape-hatch pattern as prod-guard's `PROD_GUARD_OVERRIDE`.

## Denied command shapes

1. `gh pr checks … --watch` — the canonical blocking poll.
2. `gh run watch …` — blocks until a run finishes.
3. `while …; do … sleep …; done` / `until …; do … sleep …; done` — hand-rolled
   poll loops (loop keyword + a `sleep` command word both present).

Everything else defers (emits nothing). We deliberately do **not** try to catch
a bare `sleep N` before a status check — too fuzzy, too many false positives;
fail-open says skip it.

## Classifier design

- Reuse the existing `simple_commands()` splitting idea (shlex with punctuation
  chars) so `gh pr checks 12 --watch && echo done` still classifies.
- `gh pr checks` + `--watch` flag present → deny.
- `gh run watch` (first two non-flag args) → deny.
- Loop: any simple-command group whose leading word (after stripping shell
  keywords `do`/`then`/`else`/`{`) is `while`/`until`, **and** any group whose
  leading word is `sleep` → deny.
- Strip leading `NAME=VALUE` env assignments before reading the command head
  (reuse the hook's helper idea).

## Fail-open / invariants (CLAUDE.md)

- Stdlib only; purely local (no network).
- Any parsing uncertainty → **do NOT deny** (emit nothing / defer). Never break
  a session; never deny a command we're not sure about.
- `PR_SENTINEL_DEBUG=1` re-raises for debugging; otherwise `except` → `exit 0`.
- Not a `Bash` tool call → defer (belt-and-suspenders; matcher already scopes).

## Deny payload

```json
{"hookSpecificOutput": {
  "hookEventName": "PreToolUse",
  "permissionDecision": "deny",
  "permissionDecisionReason": "<fix-it pointing at the watcher + override hint>"}}
```

## Files

- `scripts/pr-sentinel-guard.py` — new hook.
- `hooks/hooks.json` — register `PreToolUse` matcher `Bash`.
- `tests/test_guard.py` — classifier (each denied form + non-poll passes),
  override downgrade, deny payload, fail-open, DEBUG.
- `tests/test_wiring.py` — assert the PreToolUse registration points at the
  real script.
- `README.md` — new PreToolUse decision table; `PR_SENTINEL_OVERRIDE` in the
  Configuration table; drop the "reserved" note.
- `docs/DESIGN.md` — note R2 now shipped where it says "scaffolded".
- `docs/ROADMAP.md` — mark R2 shipped / reconcile the override wording.
- `.claude-plugin/plugin.json` — no new keyword needed (already has `security`).
- `docs/STATUS.md` — remove the Q2 row (own isolated commit).

## Coordination with Q1

Q1 (Stop-hook backstop) adds a `"Stop"` key to `hooks/hooks.json` and edits
README/STATUS in distinct regions. Q1 lands first: before opening the PR,
`git fetch origin main` + merge; reconcile hooks.json/README/STATUS (trivial —
distinct regions).

## Test plan

`make check` (shellcheck + unittest) must pass.
