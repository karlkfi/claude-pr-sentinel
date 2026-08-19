# Skills

Local reference for the globally-installed Claude Code skills this repo's docs
and process name. A page that needs to point at one links here with an anchor
(`skills.md#session-backlog`) rather than a source URL most readers cannot
open.

This is an index, not a copy. A skill has to stay portable across repos, and
this repo's own rules already live in the page that invokes it. Each entry
below gives the purpose, where it fires here, and which local page holds the
rules.

## Which skills can be linked

| Source | Path | Linkable from `docs/`? |
|---|---|---|
| Globally installed | `~/.claude/skills/` | No. Outside every repo, and the source is private. |
| Plugin | `~/.claude/plugins/**/skills/` | No. Same reason. |
| Repo-local | `.claude/skills/` | Yes. In-tree, so a relative link resolves. |

This repo has no `.claude/skills/`, and what it ships under `commands/` are
plugin commands rather than skills. Every skill it names therefore falls in the
first row, so link this page instead of the source.

## Skills this repo names

### session-backlog

Maintains a priority-ordered backlog under `docs/queue/` with stable Q-IDs, and
owns the process for adding items, picking the next one, completing, deferring,
and grooming.

It applies to any change under `docs/queue/`. This repo's rules for the store,
including id allocation and the pre-commit lint, are in
[maintaining-backlog.md](maintaining-backlog.md).
[`scripts/lint-backlog.sh`](../../scripts/lint-backlog.sh) is vendored from the
skill.

Named `backlog` until August 2026.

## Names drift, and nothing here goes red

Upstream can rename or retire a skill without breaking any gate in this repo.
Nothing reads the installed skill set, and `make check` runs shellcheck and the
test suite only.

The failure is silent. A missing skill raises no error when something invokes
it, so a session carries on without the rules it was told to follow, and the
first symptom is a malformed file rather than a stack trace.

`backlog` drifted to `session-backlog` this way. The tell is a name on this page
that no longer resolves under `~/.claude/skills/`. Check that before trusting an
entry here.
