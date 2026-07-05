#!/usr/bin/env python3
"""Migration helper: disable Claude Desktop's "Auto-fix CI & address comments"
(`autoFixEnabled`) on EXISTING Claude Code sessions when switching to
pr-sentinel.

Why this exists
---------------
Desktop auto-fix wakes a credentialed local agent on the PR **review-comment
stream** — the indirect-prompt-injection channel pr-sentinel is built to avoid
(see docs/DESIGN.md). Installing pr-sentinel does NOT turn that toggle off, so a
migrating user is left with every pre-existing session still armed on the
comment channel. On a public repo, anyone who can comment on an old or merged PR
has an injection path into a local agent holding `git push` + tokens. This
helper disarms them in one pass.

What it does
------------
Per-session desktop state lives in plain JSON files:

    <sessions-root>/<install-id>/<workspace-id>/local_<sessionId>.json

with a top-level boolean `autoFixEnabled`. Flipping it to `false` disables the
comment-addressing path (the injection vector). This script scans those files
and, with `--apply`, sets the flag to `false` on the targeted set.

Safety model (this is a one-shot *editor* of undocumented app state):

- **Dry-run by default.** `--apply` is required to change anything.
- **Reversible.** `--apply` backs up every edited file first.
- **Never edits while the app runs.** The live app caches these files in memory
  and rewrites them (e.g. writing back `prState:"MERGED"` after polling GitHub),
  so a live edit can be silently clobbered. `--apply` refuses unless the app is
  quit.
- **Never touches OPEN / running sessions by default.** Default scope is
  MERGED-PR sessions (worst-case exposure, no reason to stay armed). `--all`
  widens to every enabled session (including OPEN) — an explicit opt-in.
- **Schema-verified.** Only files that parse as an object containing an
  `autoFixEnabled` boolean are ever touched. If the app's schema has changed and
  none match, the helper says so and edits nothing rather than risk corruption.

This helper is PURELY LOCAL: it reads and writes files under the desktop app's
session store and makes no network call. It does not read PR bodies or comments.

Usage:
    pr-sentinel-migrate-autofix.py            # dry run (default): report only
    pr-sentinel-migrate-autofix.py --apply    # quit the app first, then flip
    pr-sentinel-migrate-autofix.py --all      # widen scope beyond MERGED
    pr-sentinel-migrate-autofix.py --root DIR # override the session store path

Environment:
    PR_SENTINEL_SESSIONS_ROOT   override the session-store path (same as --root)
    PR_SENTINEL_ASSUME_APP_QUIT=1  assert the app is quit (skip live-app detection)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FIELD = "autoFixEnabled"
BACKUP_PREFIX = ".autofix-backup-"


# --------------------------------------------------------------------------
# Locating the session store (per-platform)
# --------------------------------------------------------------------------

def default_sessions_root():
    """Resolve the Claude Code session-store directory for this platform.

    `PR_SENTINEL_SESSIONS_ROOT` (or --root) overrides. Paths differ across the
    macOS / Linux / Windows desktop builds; macOS is the verified layout.
    """
    env = os.environ.get("PR_SENTINEL_SESSIONS_ROOT")
    if env:
        return Path(env)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / \
            "claude-code-sessions"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        base = Path(base) if base else home / "AppData" / "Roaming"
        return base / "Claude" / "claude-code-sessions"
    # Linux / other: Electron config dir.
    base = os.environ.get("XDG_CONFIG_HOME")
    base = Path(base) if base else home / ".config"
    return base / "Claude" / "claude-code-sessions"


# --------------------------------------------------------------------------
# Detecting the running app (so --apply refuses to edit live state)
# --------------------------------------------------------------------------

def app_running():
    """True/False if the Claude desktop app's running state is determinable,
    None if it can't be determined on this platform."""
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            proc = subprocess.run(
                ["pgrep", "-x", "Claude"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                return True
            if proc.returncode == 1:
                return False
            return None
        if sys.platform.startswith("win"):
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                capture_output=True, text=True)
            if proc.returncode != 0:
                return None
            return "Claude.exe" in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return None


# --------------------------------------------------------------------------
# Reading / classifying session files
# --------------------------------------------------------------------------

class Session:
    """A recognized session file: parsed data plus the fields we filter on."""

    def __init__(self, path, text, data):
        self.path = path
        self.text = text
        self.data = data
        self.enabled = data.get(FIELD) is True
        self.pr_state = (data.get("prState") or "").upper() or "UNKNOWN"
        self.repo = data.get("prRepository") or "?"
        self.number = data.get("prNumber")
        self.title = data.get("title") or ""

    def label(self):
        num = f"#{self.number}" if self.number is not None else ""
        title = f"  {self.title}" if self.title else ""
        return f"{self.repo}{num} [{self.pr_state}]{title}"


def scan(root):
    """Return (sessions, unreadable_count). `sessions` are only files that
    parse as an object carrying an `autoFixEnabled` boolean — anything else is
    left strictly untouched (unknown schema)."""
    sessions = []
    unreadable = 0
    for path in sorted(root.rglob("local_*.json")):
        # Never rescan our own backups (they carry the original true value).
        if any(part.startswith(BACKUP_PREFIX) for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if isinstance(data, dict) and isinstance(data.get(FIELD), bool):
            sessions.append(Session(path, text, data))
    return sessions, unreadable


def in_scope(session, scope):
    """Which enabled sessions the run targets. Default (`merged`) is MERGED
    only — OPEN / running sessions are never touched without `--all`."""
    if scope == "all":
        return True
    return session.pr_state == "MERGED"


# --------------------------------------------------------------------------
# Writing (backup + minimal reserialization)
# --------------------------------------------------------------------------

def dump_like(data, original_text):
    """Serialize `data` back, roughly preserving the source's style (indented
    vs compact). JSON round-trip is lossless for valid JSON; the original is
    backed up regardless."""
    if "\n" in original_text.strip():
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def disable(session, backup_root, root):
    """Back up the file, then flip `autoFixEnabled` to false atomically."""
    rel = session.path.relative_to(root)
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(session.text, encoding="utf-8")

    session.data[FIELD] = False
    out = dump_like(session.data, session.text)
    tmp = session.path.with_name(session.path.name + f".tmp.{os.getpid()}")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, session.path)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def group_counts(sessions):
    """Enabled-session counts grouped by PR state and by repository."""
    by_state, by_repo = {}, {}
    for s in sessions:
        if not s.enabled:
            continue
        by_state[s.pr_state] = by_state.get(s.pr_state, 0) + 1
        by_repo[s.repo] = by_repo.get(s.repo, 0) + 1
    return by_state, by_repo


def print_summary(enabled, by_state, by_repo):
    print(f"Sessions with auto-fix enabled: {len(enabled)}")
    if not enabled:
        return
    print("  by PR state:")
    for state in sorted(by_state):
        print(f"    {state:<8} {by_state[state]}")
    print("  by repository:")
    for repo in sorted(by_repo, key=lambda r: (-by_repo[r], r)):
        print(f"    {by_repo[repo]:>4}  {repo}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Disable Claude Desktop auto-fix (autoFixEnabled) on "
                    "existing sessions.")
    parser.add_argument("--apply", action="store_true",
                        help="actually flip the flag (default: dry run). Quit "
                             "the Claude desktop app first.")
    parser.add_argument("--all", action="store_true",
                        help="target every enabled session, including OPEN PRs "
                             "(default: MERGED only).")
    parser.add_argument("--root", default=None,
                        help="session-store path (overrides "
                             "PR_SENTINEL_SESSIONS_ROOT and the platform default).")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else default_sessions_root()
    if not root.is_dir():
        print(f"error: session store not found: {root}", file=sys.stderr)
        print("       (is Claude Code for Claude Desktop installed? override "
              "with --root)", file=sys.stderr)
        return 1

    scope = "all" if args.all else "merged"
    sessions, unreadable = scan(root)
    enabled = [s for s in sessions if s.enabled]
    by_state, by_repo = group_counts(sessions)

    print(f"Session store: {root}")
    print(f"Recognized session files: {len(sessions)}"
          + (f" ({unreadable} unreadable, skipped)" if unreadable else ""))

    if not sessions:
        print("\nNo files matching the expected schema (an object with an "
              f"`{FIELD}` boolean) were found. Nothing to do — either no "
              "sessions exist here, or the Claude Desktop session format has "
              "changed. No files were modified.")
        return 0

    print()
    print_summary(enabled, by_state, by_repo)

    targets = [s for s in enabled if in_scope(s, scope)]
    scope_desc = "all enabled sessions" if scope == "all" \
        else "MERGED-PR sessions"
    print(f"\nScope: {scope_desc} — {len(targets)} session(s) targeted.")
    if scope == "all" and any(s.pr_state == "OPEN" for s in targets):
        print("  note: --all includes OPEN-PR sessions you may still be "
              "working on.")

    for s in targets:
        print(f"  {'disable' if args.apply else 'would disable'}  {s.label()}")

    if not args.apply:
        print(f"\nDry run. Re-run with --apply (after quitting the Claude "
              f"desktop app) to disable {len(targets)} session(s).")
        return 0

    # --- apply path: refuse to edit live app state ---
    if os.environ.get("PR_SENTINEL_ASSUME_APP_QUIT") == "1":
        running = False
    else:
        running = app_running()
    if running is True:
        print("\nerror: the Claude desktop app appears to be running. Quit it "
              "(Cmd-Q) first — the running app rewrites these files and would "
              "silently clobber the edit — then re-run with --apply.",
              file=sys.stderr)
        return 2
    if running is None:
        print("\nerror: could not determine whether the Claude desktop app is "
              "running on this platform. Quit it, then re-run with "
              "PR_SENTINEL_ASSUME_APP_QUIT=1 to confirm.", file=sys.stderr)
        return 2

    if not targets:
        print("\nNothing to disable. No files were modified.")
        return 0

    backup_root = root / f"{BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}"
    for s in targets:
        disable(s, backup_root, root)

    print(f"\nDone. Disabled auto-fix on {len(targets)} session(s).")
    print(f"Backups: {backup_root}")
    print("Relaunch the app and spot-check a couple of the sessions above; the "
          '"Auto-fix CI & address comments" toggle should now be off.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
