# Raphael's notes: the Obsidian vault is the record, and Raphael keeps the journal

**Date:** 2026-09-12
**Status:** approved design (issue #514), built in the same PR
**Vault:** `arshadansari27/arshad-workspace` (private, personal account)

## Problem

Everything Raphael knows sits in one pile, the knowledge store: about 20,400
documents of things read, and nothing that says what was concluded.

- `research_topic` answers are stored as `aegis://research/<hash>` rows among
  10,000 PDFs.
- The daylog writes one dated entry a night into the same store, as
  `daylog` and `daylog_rollup` rows nobody can open.
- The user already keeps a vault: 1,023 markdown notes (582 journal notes from
  2014 to 2023, 315 knowledge notes, 42 literature notes, 45 reference notes),
  idle since 2023-10-25.

Maou's books solved the same problem for money: the hledger journal is the
record, Postgres is only the index, one guarded module writes, and a chat tool
validates a write while a workflow performs it. This design does the same for
Raphael, with the vault as the record.

## Decisions from the user (2026-09-12, on #514)

1. Raphael **reads the whole vault**, `journal/` and `reference/` included.
2. Raphael **takes over the journal**: the daylog's nightly, weekly and
   monthly entries become journal notes.
3. A read/write **deploy key** on the vault is approved; it is added at deploy
   time, not by this PR.

## Non-goals

- Attachments (59 PDFs, 135 images, 18 audio notes) are not indexed yet.
- No editing or deleting of anything the user wrote. Ever.
- No Templater execution. Raphael renders the core-template placeholders the
  vault's templates actually use, and drops any Templater tag it finds.
- No reorganising of old journal notes into year folders.

## 1. Where the checkout lives

`notes_path`, default `/app/config/notes`: a sibling of the books checkout
(`/app/config/books`) inside the `aegis_config` volume that core and worker
already share on the same node. **No infra change is needed.** The deploy key
is written by `notes.install_deploy_key` to `<gmail_token_dir>/notes_deploy_key`
(mode 0600, never logged), exactly like `books_deploy_key`.

The git layer is the books layer, reused rather than copied: clone inside the
flock (staged in a sibling directory), `.aegis.lock` in the checkout (added to
`.git/info/exclude` on first use, so it never shows as untracked), and the
scoped revert. Commits are authored `Raphael <raphael@aegis.local>`.

## 2. Configuration and the "not configured" state

Integrations page, group "Notes (vault)": `notes_repo_url` and
`notes_deploy_key` (secret). **Both** must be set before anything writes or
indexes; until then:

- the daylog behaves exactly as today (knowledge rows, nothing lost);
- `note_*` tools say the vault is not configured;
- `NotesSyncFlow` reports `not_configured`;
- `ResearchFlow` saves its answer to the knowledge store only.

## 3. The writer (`services/notes.py`) — append-only

One module writes the vault; nothing else does.

- **Allowed paths:** anything under `raphael/`, and the journal notes the
  daylog owns (§5). A path with `..`, an absolute path, a non-`.md` file or
  anything else is refused before git is touched.
- **Append-only:** a write creates a file, or appends a section at its end.
  It never rewrites, reorders or deletes an existing line. Each section starts
  with a heading and a hidden Obsidian comment marker, `%% aegis:<key> %%`. A
  write whose marker is already in the file is a no-op, so a re-run or retry
  never adds a section twice and never changes one.
- **Sequence**, inside the flock: clone if needed → `git pull --rebase` →
  append → commit only the paths written → push.
- **Conflicts with `obsidian-git`:** the phone and laptop auto-commit, so
  journal notes are files both sides may touch. When the push is rejected, or
  a rebase conflicts, the writer aborts the rebase, drops its own unpushed
  commit (`reset --hard @{u}` — only AEGIS writes in this checkout), pulls
  fresh and re-applies the append **once**. If that fails too, it drops the
  commit again and reports the failure. It never force-pushes. A write counts
  as done only when it is pushed, so the checkout never carries a divergent
  local commit.

## 4. Encrypted blocks

The vault uses meld-encrypt 1.6.2, which encrypts blocks inside ordinary
`.md` files between `%%🔐` and ` 🔐%%`. `notes.strip_encrypted` removes every
such span (and, for an unterminated marker, everything after it) before
**anything** is indexed, embedded, shown by `note_read`, or sent to a model in
a rollup. The file itself is never changed. Tests prove the ciphertext never
reaches the store.

## 5. The journal

The vault's periodic-notes settings decide the names:

| Entry | Path | Example |
|---|---|---|
| Nightly | `journal/DD MMM YY.md` | `journal/12 Sep 26.md` |
| Weekly rollup | `journal/[W]ww MMM YY.md` | `journal/W37 Sep 26.md` |
| Monthly rollup | `journal/<YYYY>/MM. MMM.md` | `journal/2026/09. Sep.md` |

- **Monthly naming.** The vault's monthly format `MM. MMM` has no year, so a
  note at the journal root would collide every year. The monthly note goes in
  a year folder instead, which matches how the vault already files old notes
  (`journal/<year>/<NN. Mon>/`).
- **Weekly naming.** `ww` is moment's locale week (weeks start on Sunday, week
  1 holds 1 January), formatted from the week's Sunday. The daylog's weekly
  rollup covers an ISO week (Monday to Sunday), so it goes to the vault week
  that holds the rollup's Monday — six of its seven days.
- **Shape.** A new note is rendered from the vault's own template
  (`_templates/{{tp_title_today}}.md`, `weekly-…`, `monthly.md`), read from the
  checkout at write time, so the user's sections are there to fill in.
  `{{date:FMT}}`, `{{date}}`, `{{time}}` and `{{title}}` are rendered; a
  Templater tag (`<% … %>`) is dropped. Raphael's entry follows as
  `## Raphael`. A note that already exists — the user wrote that day — gets the
  section appended at the end and nothing else.
- **The index follows the record.** With the vault configured, the daylog stops
  writing its own `daylog` / `daylog_rollup` rows; `NotesSyncFlow` indexes the
  journal note like any other note. If the vault write fails, the daylog files
  the knowledge row as before and reports `vault_error`, so no day is lost.
- **Rollups read the journal.** `gather_daylogs` reads each day's journal note
  (encrypted blocks stripped) and falls back to the old knowledge row for a day
  that has no note — so a week spanning the switch-over still rolls up whole.

## 6. The index (`NotesSyncFlow`)

Hourly at minute 19. Pull, then index every `.md` note as `source_type='note'`,
url `vault://<path>`, skipping `.obsidian/`, `_templates/`, `backups/`,
`_attachments/` and `.trash/`. Incremental by commit: the last indexed commit
is kept in `settings.notes_index_state`, changed files are re-indexed, deleted
files are removed from the index, and a rename is both. At most 300 files per
run; the rest wait for the next run, so the first full pass of 1,023 notes
takes about four runs.

## 7. Notes rank above raw documents

`SourceTypeInfo` gains `rank_boost` (default 1.0, so every existing type ranks
exactly as before). `note` gets `rank_boost=1.25` and a 10-year decay window,
and chat's `_apply_knowledge_decay` multiplies the boost in. `ResearchFlow`'s
gather step also searches notes on their own and puts them first.

## 8. Tools and writes

- `note_search` (read-only): the index, notes only.
- `note_read` (read-only): one note from the checkout, encrypted blocks
  stripped, bounded.
- `note_write`: create or append under `raphael/` only.
- `note_link`: append a `[[wikilink]]` or URL line to a note under `raphael/`.

The two writers validate and then hand the write to `NotesWriteFlow` under
`notes-write-<op>-<sha256 of the content>`, wait `NOTES_WRITE_WAIT_S` and relay
the answer — the `BooksWriteFlow` seam — so a retried turn re-attaches and a
slow write reports itself to the agent's channel. `services/notes_write.py` is
the one implementation both sides call.

`ResearchFlow`'s answer is also saved as `raphael/questions/<slug>-<hash>.md`
through the same writer (marker keyed on the question and the answer, so the
same answer is never appended twice and a new answer is a new dated section).

## 9. Backfill

`NotesBackfillFlow` (started by hand, never scheduled) writes the existing
`daylog` and `daylog_rollup` rows into the matching journal notes once,
through the same writer and markers, so it is safe to run twice.

## 10. Testing

Local bare git repos, as the books tests use: append-only and markers, path
refusal, the conflict retry (a second clone pushing to the same file between
pull and push), the double failure, encrypted blocks never reaching the store,
journal naming across year boundaries, template rendering, the daylog's
configured and unconfigured paths, and the indexer's add/change/delete/rename.
