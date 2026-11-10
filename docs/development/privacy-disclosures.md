# Writing a PRIVACY.md disclosure

`PRIVACY.md` is read by someone deciding whether to let this plugin run. They
cannot check any sentence in it, so each one is a claim they take on trust.
The disclosure tests in `tests/test_watcher.py` check part of what the file
says, and this page covers what they check and the part a reviewer has to
check instead.

## What the gates check

Each gate asks one question: does a name the code uses occur in the component's
own section of `PRIVACY.md`?

| Gate | Name it looks for |
|---|---|
| `undisclosed_reads` | a GraphQL object or enum the watcher's queries traverse |
| `undisclosed_rest_reads` | a literal segment of a `gh api` REST path |
| `undisclosed_globs` | a glob pattern a component opens files by |
| `undisclosed_components` | a section for every script the plugin runs |
| `undisclosed_writes` | the watcher's `PR_SENTINEL_STATE_DIR` write knob, and no "writes nothing to disk" left beside it |

They catch the miss that keeps recurring: a read nobody mentioned at all.
None of them can tell whether the sentence around the name is true. The
sentence might describe the read wrongly, or be about something else, and the
gate passes either way, because the name occurs.

Two cases, both from the branch that added the watcher's `compare` read
(2026-09-18):

- The first draft said "No commit message, author, or file list is requested
  — only that one word." The watcher reads only `.status`. But `gh`'s `-q`
  filters on this machine after the response arrives, and the response
  carried 105,840 bytes of commit messages, author names and patches for a
  seven-commit comparison. The gate saw `compare` in the section and passed.
- Deleting the bullet that described the read also left the gate green,
  because a one-line sentence elsewhere in the section contained the word
  `compare`. One unrelated mention of a name anywhere in a section
  discharges that name for good.

No test can fix this. Comparing an English sentence against what an HTTP
response carries would need the response, so the gate would have to make the
call it is auditing.

## An absolute negative names its evidence

A sentence saying something did **not** happen is a claim about everything
the code doesn't do: "no X is requested", "reads nothing else", "writes
nothing to disk", "the only component that does". Reading the code that runs
can't confirm a claim like that. The watcher's "It writes nothing to disk"
and the migration helper's "the only component that does" were both false.
Both stayed in the tree until a change to another component made them false,
and no gate noticed either one.

So a disclosure may state an absolute negative only when it names what
established it:

- **a measurement**: what was measured, against what, and when. The
  `compare` bullet's "106 KB for a seven-commit comparison, measured
  2026-09-18" is the model;
- **or the test that enforces it.** A test can back a claim about what the
  code *asks for*. `test_never_queries_comments_or_body` backs "never
  requests the PR body". It cannot back a claim about what a response
  *carries*, which is the `compare` case above.

A reviewer can check this in one pass. Look for each negative in the diff and
for the evidence named beside it. A negative with no evidence is either
measured before merge or reworded into what the code positively does.

The rule binds disclosures written or changed from here on. The sentences
already in `PRIVACY.md` were written before it, and many state a negative
with nothing beside it. Auditing them is its own backlog item.
