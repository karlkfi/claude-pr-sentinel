# pr-sentinel

A Claude Code plugin that automates post-PR babysitting — fixing CI failures
and healing merge conflicts — for local desktop sessions, without foreground
polling and without ingesting the PR comment channel. See `README.md` for the
user-facing overview and `docs/DESIGN.md` for the rationale.

Two load-bearing pieces:
- `scripts/pr-sentinel-watch.sh` — a bash background watcher, launched per-PR
  as a Claude Code background task, that polls `gh` and exits (waking the
  session) when a check fails, a conflict appears, the PR goes green, or the PR
  closes. `set -euo pipefail`, shellcheck-clean.
- `scripts/pr-sentinel-hook.py` — a stdlib-only `PostToolUse` hook that nudges
  the session to launch the watcher after `gh pr create` / `git push`.

## Model selection

Use the `model-advisor` skill to assess the right model and thinking level at
session start and whenever the task type shifts significantly (e.g. from a
small report-wording tweak to redesigning the poll loop or the sanitizer).

## Development philosophy

Build the right thing AND build it well. Before writing code, state the goal in
one sentence and the approach in two or three. If the goal is unclear, ask one
focused question rather than guessing.

Make the smallest change that achieves the goal. If you notice problems outside
the current task's scope, flag them on the Queue in `docs/STATUS.md` rather than
fixing them inline.

Capture knowledge durably, don't leave it in chat. Standing preferences and
decisions go in the repo (this file, `docs/`, or memory), not just this turn's
response.

## Workflow

1. **At session start, check whether the branch is stale.** `git fetch origin
   main && git log --oneline HEAD..origin/main | head` — any output means
   `origin/main` has advanced; **rebase onto it** before other work
   (`git rebase origin/main`, then `git push --force-with-lease` once the branch
   is pushed). Rebase, **not** merge: PRs land via GitHub's "Merge pull request",
   so a linear branch keeps `main` free of sync-merge noise. (This is a *dev*
   workflow rule and is the opposite of the *product* rule — the watcher tells a
   session to heal a **conflicting** PR by merging the base branch IN, never
   rebase, to keep that push a fast-forward. Different context, different call.)
   **Work on a `claude/`-prefixed branch, never on `main`**; in a worktree
   session, do all work via the worktree path, never the parent repo's.
2. **Before changing behaviour** — read `README.md` and skim the script you're
   touching so the change matches the existing model. If picking the next task,
   run `gh pr list` first and skip any Queue item already covered by an open PR.
3. **For complex tasks** — write a plan under `docs/plan/<slug>.md` and follow
   it; keep it updated so completed scope is verifiable.
4. **After changing behaviour** — review the diff and update docs proactively:
   - **Changed watcher events / report format** → the decision tables in
     `README.md`, and `docs/DESIGN.md` if the mechanism changed.
   - **Changed hook detection** → the PostToolUse table in `README.md`.
   - **New configuration or env var** → the Configuration table in `README.md`
     and `.claude-plugin/plugin.json` keywords/description.
   - Update `docs/STATUS.md`: remove the completed Queue row.
5. **Commit when done** — small, focused, Conventional Commits. Commit
   `docs/STATUS.md` changes in their own isolated commit; a pre-commit gate
   lints them (one-time `git config core.hooksPath .githooks` per clone).
   Backlog format and process: `docs/development/maintaining-backlog.md`.

## Code standards

### Bash (`scripts/pr-sentinel-watch.sh`)

- Start with `set -euo pipefail`. Keep it **shellcheck-clean** (`make
  shellcheck`).
- Target **bash 3.2** (macOS default): no `${var,,}`, no associative arrays, no
  `mapfile`. Use `local` in functions, `[[ ]]`/`(( ))` (never `[ ]`), and quote
  every expansion.
- Depend only on `gh`, and POSIX-ish `sed`/`tr`/`tail`/`wc`/`date`. Use `gh`'s
  built-in jq (`-q`) rather than an external `jq`.
- **Never widen the `gh --json` field list to human/attacker-writable fields**
  (`body`, `comments`, `reviews`, `title`). The watcher reads GitHub-controlled
  metadata only. A test enforces this (`test_never_queries_comments_or_body`).

### Python (`scripts/pr-sentinel-hook.py`)

- Stdlib only — no third-party deps. Runs on whatever `python3` is on PATH.
- **Fail open**: on any parsing uncertainty the hook emits nothing (defers).
  It must never break a session. `PR_SENTINEL_DEBUG=1` re-raises for debugging.
- The hook stays **purely local** — no network calls, no reading PR text beyond
  echoing back a URL the command already printed.

## Security principles

**Secure by default, not opt-in.** This plugin's whole value is the security
posture; its defaults must never trade a security property for convenience. Two
invariants are non-negotiable and must not regress into a default:

- **No ingestion of PR/issue comments or the PR body.** This is the injection
  channel we exclude by design (`docs/DESIGN.md`). Widening the `gh` query to
  reach that text — even "just to summarise the failure" — is the change that
  needs sign-off and almost certainly a no.
- **No auto-merge, ever.** Human merge review is the trust boundary. The plugin
  reads GitHub state and wakes the session; it does not merge, and must not
  learn to.

CI log excerpts are semi-untrusted: keep the size cap, the ANSI strip, and the
`DATA, NOT INSTRUCTIONS` frame. Any loosening knob is opt-in and documented as
a trade-off. When in doubt, add friction; removing it needs sign-off.

## Testing

Tests live in `tests/` (stdlib `unittest`, no third-party deps). Run with:

```
make check          # shellcheck + unittest
python3 -m unittest discover tests
```

Four suites:
- `tests/test_watcher.py` — runs the watcher as a subprocess against a **stub
  `gh`** on PATH (canned, jq-projected fixtures) and asserts each exit event
  and the report sanitization (ANSI strip, size cap). Add the scenario that
  motivated any change as a fixture.
- `tests/test_hook.py` — unit + subprocess tests of command classification, the
  failure heuristic, and the emitted `additionalContext`.
- `tests/test_migrate.py` — runs the migration helper against a synthetic
  session store (fixture `local_*.json`) and asserts the scope rules, backup,
  app-running refusal, and schema no-op.
- `tests/test_wiring.py` — the manifests/hook registration are valid and agree
  on version, and the scripts exist and are executable.

**Never use real PR URLs, hostnames, or credential paths in fixtures.**
Synthetic `owner/repo`, run ids, and PR numbers exercise identical code paths.

## Commits

- Small, focused, Conventional Commits; commit after each validated task.
- **No Claude attribution** in commit messages or PR descriptions.
- Amending an unpushed commit is fine. Once pushed, prefer a follow-up commit;
  `--force-with-lease` on a `claude/` branch is fine when the user asks (e.g. to
  rebase for a clean history — see Workflow step 1), never on `main`.
- **After pushing, check for a PR** (`gh pr view`): if one exists, update its
  description with `gh pr edit` to reflect the new commits; open one only when
  the task is finished.
- **Unrelated work goes in its own PR** — don't bundle it into the branch you're
  on. Parallel PRs beat a mixed diff.
- **Act only on your own branch and PR.** Don't push to or rewrite a branch/PR
  another session owns.
- Version lives in exactly two files (`plugin.json`, `marketplace.json`) — bump
  them together. See `docs/development/release-process.md`.

## Documentation conventions

Spell out acronyms on first use, e.g. "continuous integration (CI)".

Human-facing docs (`README.md`, `docs/` outside `docs/development/`) must never
link to `CLAUDE.md` / `AGENTS.md`. This file is the entrypoint for agents only;
humans start at `README.md`. The dependency is one-way.

**Editing `CLAUDE.md` — protect the context budget.** This file loads in full
into every session, so every line costs context. Add only load-bearing,
must-act-on rules; put explanation and how-to in the relevant `docs/` page with
a one-line pointer here. Prefer tightening an existing line over adding a new
one.
