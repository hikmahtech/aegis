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

## 3. The writer (`services/notes.py`) — insert-only

One module writes the vault; nothing else does.

- **Allowed paths:** anything under `raphael/`, and the journal notes the
  daylog owns (§5). A path with `..`, an absolute path, a non-`.md` file or
  anything else is refused before git is touched.
- **Insert-only** (amended 2026-09-12, see §5): a write creates a file, or
  inserts one contiguous block into it — a journal entry at the end of the
  note's own section, anything else at the end of the note. It never rewrites,
  reorders or deletes an existing line; `_apply` checks that the new text is
  the old text with exactly one block inserted. Each block carries a hidden
  Obsidian comment marker, `%% aegis:<key> %%`. A write whose marker is already
  in the file is a no-op, so a re-run or retry never adds a block twice and
  never changes one.
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

**Amended 2026-09-12**, after the user compared the backfilled notes with
their own. The first version put every note at the `journal/` root, named
weeks from their Sunday, put the month note loose in the year folder, and
appended a `## Raphael` section. The vault's real conventions, read from its
journal:

| Entry | Path | Example |
|---|---|---|
| Nightly | `journal/<YYYY>/<NN. Mon>/DD MMM YY.md` | `journal/2026/09. Sep/11 Sep 26.md` |
| Weekly rollup | `journal/<YYYY>/<NN. Mon>/W<ww> MMM YY.md` | `journal/2026/09. Sep/W37 Sep 26.md` |
| Monthly rollup | `journal/<YYYY>/<NN. Mon>/<NN. Mon>.md` | `journal/2026/08. Aug/08. Aug.md` |

- **Filed, not at the root.** periodic-notes creates today's note at the
  `journal/` root and the user files notes into `journal/<YYYY>/<NN. Mon>/`
  later — all but two of their own journal notes are filed. Raphael writes a
  day that has passed, so it files straight away; when the user already has
  that day's (or week's) note open at the root, it writes into that one
  instead, so a day never gets two notes. The root note is never created.
- **The month is the folder note** (`2023/08. Aug/08. Aug.md`,
  `2022/03. Mar/03. Mar.md`), which `folder-note-plugin` shows as the folder.
  `MM. MMM` has no year, so it only ever lives inside its year.
- **Weeks start on Monday.** The calendar plugin's `weekStart` is `locale`.
  The 2022 weekly notes were Sunday-dated; every one since 2023 is
  Monday-dated with ISO week numbers (`W40 Oct 23` opens `# Oct 02, 2023`, a
  Monday; `W05 Jan 23` is Mon 30 Jan). A week is named from its Monday and
  filed in that Monday's month; the daylog's ISO week (Monday to Sunday) is
  the same week. (The user filed a few weeks that straddle two months by hand
  in the later month; the Monday's month is the predictable rule.)
- **Shape.** A new note is rendered from the vault's own template
  (`_templates/{{tp_title_today}}.md`, `weekly-…`, `monthly.md`), read from the
  checkout at write time. `{{date:FMT}}`, `{{date}}`, `{{time}}` and
  `{{title}}` are rendered; a Templater tag (`<% … %>`) is dropped; open
  checkboxes and the target section's empty `- ` placeholder are dropped.
  Raphael's entry goes INTO the note's own section, after whatever is there —
  daily `Journal`, weekly `Review`, monthly `Review` (else an older note's
  `Month Review`) — as the user's own bullets are written:

      - #raphael day log %% aegis:daylog:2026-09-11 %%
      	- first paragraph
      	- second paragraph

  One converter, `notes.body_outline`, lays the text out. A prose paragraph
  is one bullet, its wrapped lines joined. The daylog's fallback format keeps
  its outline: a `Label:` line with its indented items nested under it, a tab
  or two spaces a level, at most four deep. `split_section` reads the block
  back as the same text.
  The section is found by its heading text at any level (the redesigned
  templates of 2026-09-12 use `##`; older notes `###`) and ends at the next
  heading, a `---` line or a code fence, so the month note's `ccard` folder
  card stays last. A note without that section gets `## Journal` /
  `## Review` and the block at its end.
  Readers (`split_section`, the rollups, the backfill) find the block by its
  marker and look at both the filed note and the root one. The first
  `## Raphael` shape is no longer read: the layout repair rewrote every note
  that carried it.
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

**Amended 2026-09-13** (audit): the index also skips `raphael/questions/`.
`ResearchFlow` keeps each answer in the knowledge store too
(`aegis://research/<hash>`), so indexing the note put every answer in
retrieval twice. A row an earlier run made for one is removed on the next
run.

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

Insert-only has a consequence worth stating: a write can never fill in a
placeholder that is already in a note (an empty `- ` bullet, a template's
blank field). It inserts one new block; the placeholder stays where the user
left it. Only a note Raphael creates from a template drops its empty
placeholders, before anyone has seen it (§5).

Not built: a `raphael/topics/` note per tracked topic and a `raphael/books/`
note per book. Tracked topics live on the problem hub (#513) and books in
Calibre (#510). `raphael/topics/` in the `note_write` example is only a folder
Raphael may choose to write in.

The two writers validate and then hand the write to `NotesWriteFlow` under
`notes-write-<op>-<sha256 of the content>`, wait `NOTES_WRITE_WAIT_S` and relay
the answer — the `BooksWriteFlow` seam — so a retried turn re-attaches and a
slow write reports itself to the agent's channel. `services/notes_write.py` is
the one implementation both sides call.

`ResearchFlow`'s answer is also saved as `raphael/questions/<slug>-<hash>.md`
through the same writer (marker keyed on the question and the answer, so the
same answer is never appended twice and a new answer is a new dated section).

## 9. Backfill

`NotesBackfillFlow` writes the `daylog` and `daylog_rollup` rows into the
matching journal notes through the same writer and markers, newest first, so
running it again writes nothing new.

**Amended 2026-09-13** (audit): it runs weekly (`notes-backfill-weekly`,
Sunday 04:47 UTC), not only by hand. The first run moved the old rows. With
the vault configured the daylog files a row only when its vault write fails,
so a later run is what puts such a day in the journal. The scheduled run
looks only at rows filed in the last `since_days` (14, two weekly chances):
the pre-vault rows are still in the store, and rereading them every week
would put back a block the user deleted from an old journal note. A run
started by hand defaults to `since_days` 0 and takes every row.

## 10. Testing

Local bare git repos, as the books tests use: append-only and markers, path
refusal, the conflict retry (a second clone pushing to the same file between
pull and push), the double failure, encrypted blocks never reaching the store,
journal naming across year boundaries, template rendering, the daylog's
configured and unconfigured paths, and the indexer's add/change/delete/rename.
