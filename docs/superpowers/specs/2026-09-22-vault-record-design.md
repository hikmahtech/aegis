# The vault is the record of the owner: Sebas keeps the journal, every agent keeps its part of `me/`

**Date:** 2026-09-22 (revised twice the same day: meetings, finance facts, research interests and seeding; then the standing finance note)
**Status:** design for review. No code is written.
**Builds on:** `docs/superpowers/specs/2026-09-12-raphael-notes-design.md` (#514)
**Devices:** the owner's devices sync with each other through Obsidian Sync, and the Mac also runs obsidian-git, which makes it the bridge to the git repository AEGIS uses. The Mac has pushed since 2026-09-21. A phone edit reaches AEGIS when the Mac is next open. Where this spec says "until the Mac syncs", that day has come.

## Problem

What AEGIS knows about its owner sits in places he cannot see or edit.

- One `user` persona document per agent (`agent_personalities`). Only one has real content: the `gtd` holder's, 2,042 characters, grown by five approved weekly revisions. The other three are untouched starter text (276 to 286 characters).
- 28 live `agent_memory` rows. 17 are the owner's own typed answers to curiosity questions. 8 are Gmail triage corrections from a loop known to be polluted. 3 are reasons typed on cards.
- 74 meeting notes and 55 meeting self-reviews sit in the knowledge store, where nobody opens them.
- The finance facts he wants at hand exist only as ledger queries, or in three tables nothing has written since 2026-09-05.
- `life.people` has 0 rows and `life.assets` 0. The admin forms are not used.

The vault is where the owner already keeps his life. AEGIS writes a nightly block into it, but the journal belongs to the research agent, only that agent holds the note tools, and `chat_tool_calls` has no `note_*` row. The journal has a hole on every day the owner wrote nothing.

## Decisions from the owner (not reopened)

1. For what agents learn about the owner, the vault is the record. The database is its index or cache. The notes live in a `me/` folder, one note per domain. The owner and the agents both edit them.
2. Agents add freely and ask to change. A new bullet is written at once. Changing or removing a bullet needs a Slack approval first.
3. The `gtd` holder (Sebas) keeps the journal, and he is the one who asks the owner.
4. No journal gaps. When the owner wrote nothing for a day, he is asked on Slack, and his answer is filed into that day's note.
5. All four agents use the vault.
6. The vault covers the important things from meetings (decisions, commitments he made or was given, context about the people), the finance facts he wants at hand, and his research interests. Amounts of record stay in the hledger journal.
7. The record is seeded from what already exists, and he reviews each first draft before it counts.

## What the code and production say today

Measured 2026-09-21 and 2026-09-22. Each fact names its file.

**The journal and the cards**

- **Readers find a journal block by its marker, never by its tag.** `DayLogActivities._journal_entry` (`worker/src/aegis_worker/activities/daylog.py:507-527`) calls `notes.split_section` with `notes.journal_key`, which is `daylog:<label>` and holds no agent id. So the existing `#raphael` blocks need nothing when the owner changes.
- **Production has no `vault_layout` row.** The code defaults are live: `agent_dir = "raphael"`, `entry.tag = "#raphael"` (`core/src/aegis/services/vault_layout.py:99-106, 340-347`). A changed default changes production.
- **The journal's owner is set in two places.** The five `activities` rows name `raphael` (`config/seed/activities.yaml`). `seed.py:200-222` rewrites `activities.agent_id` from the yaml on every boot, and `ActivityPatch` (`core/src/aegis/api/routes/activities.py:37-40`) has no `agent_id` field. The second place is a fallback for a run started with no agent: `_OWNER_TAG = "research"` in `flows/daylog.py:65`, `flows/notes_backfill.py:38`, `activities/notes.py:52` and `services/tools/notes.py:49`.
- **The rollups already send the owner's own words to a model.** `_journal_entry` puts the agent's block first and "whatever else the note holds" after it. Encrypted blocks are stripped by the read.
- **`remember_this` does not write `agent_memory`.** It files a `source_type='chat'` knowledge row with a 30-day decay (`core/src/aegis/services/tools/knowledge.py:69-97`). The writers of `agent_memory` are `apply_curiosity_answer` (`worker/src/aegis_worker/activities/curiosity.py:887-935`), `record_correction_from_interaction` and `record_gmail_triage_correction` (`core/src/aegis/services/memory.py`).
- **The curiosity card is weak.** It uses the `input` kind (`flows/curiosity.py:201`), which in Slack is only a link to the admin UI (`comms/src/aegis_comms/cards.py:87-92`) behind Cloudflare Access. Of 28 cards, 17 were answered and 11 expired. Answers average 49 characters.
- **`ProfileReflectionFlow` is the working promoter of durable facts**, not memory consolidation. It runs weekly for the `gtd` holder and proposes a whole new `user` document on a `draft_review` card (`worker/src/aegis_worker/flows/profile_reflection.py:210-258`). Five of six proposals were approved. `MemoryReflectionFlow` only merges memory rows, and it is still a dry run.
- **The `ack` card's text box is the wrong seam for an answer.** It is one line, labelled "Why?" (`cards.py:106-131`), cut to 500 characters (`comms/src/aegis_comms/slack_inbound.py:483`), and it arrives under `note`, a key the learning loop reads (`memory.py:20`).

**Meetings**

- `MeetingNotesFlow` stores the notes, never the transcript (`worker/src/aegis_worker/flows/meeting_notes.py:97-125`). A `meeting` row averages 11,600 characters.
- The self-review is already structured. `analyse_meeting` returns JSON with `contributions`, `problems_raised`, `commitments` and `verbosity_note` (`worker/src/aegis_worker/activities/meeting.py:59-68, 543-555`), kept in `metadata.review`. Reviews average 1.3 commitments.
- **Nothing extracts decisions, or what others asked of him.** The Gemini notes' own "next steps" section is not parsed.
- Attendees are the speaker labels of the transcript: names, no emails (`meeting.py:145-223`). The labels are free text, and a few of them make up most mentions.
- The rate is 2 to 5 meetings a week. Most of the stored rows came from one backfill week.

**Finance**

- **`finance.recurring_charge`, `renewal_alert` and `subscription_digest` are frozen.** Migration 027 removed the two schedules that wrote them. Their newest rows are dated 2026-09-04, 09-05 and 09-01. `renewal_alert` and `subscription_digest` have no reader. They can seed a draft once. They cannot feed a schedule.
- The live index is `finance.journal_index`. It holds dues by payee, failed rows, transactions, account and instrument labels and the entity.
- **Autopay is not stored.** `is_autopay` (`core/src/aegis/services/bank_parsers.py:77-80`) runs once per mail, sets `MoneyEvent.autopay` in memory (`worker/src/aegis_worker/activities/money.py:523`), and `capture_due` uses it once. `journal_index` has no such column. So "how each biller is paid" is a fact no database holds.
- `books.run_hledger` allows `accounts` (`core/src/aegis/services/books.py:1466-1470`), so account names can be listed with no balance.
- The `books_chart` row has 2 entities. Nothing anywhere holds a filing rhythm. `life.expiring_items` holds 5 rows, all domains.

**Research interests**

- `intelligence_topics` holds 20 topics, each with a name, queries and a priority (`core/src/aegis/services/research_topics.py:77-118`). There is no `research_topics_config` row, so its defaults apply.
- There are 36 `rss` channels, 35 active (`core/src/aegis/services/feeds.py:176-206` measures their use), one Raindrop channel, and 235 book rows, 233 with Calibre tags (`core/src/aegis/services/library.py:173-206`).
- Outside `journal/` the vault has 303 notes in `knowledge/` under 11 second-level names, 42 in `literature/`, 43 in `reference/` and 3 root notes. The index keeps only `commit`, `folder` and `path` for a note. It does not keep Obsidian tags, so a drafter reads those from the checkout.
- The research holder's `user` document is starter text, and it has no memory rows.

## Changes to the first sketch

| Sketch | This design | Why |
|---|---|---|
| Reuse the `ack` card's note box for the diary answer | Make the `input` kind Slack-native: an Answer button opens a modal | The answer arrives as `value`, which the learning loop does not read. The admin textarea stays as the fallback. Curiosity cards improve for free. |
| Change and remove through the `apply_profile_patch` path | An `approval` card with a post-resolve activity, and an exact one-line match when applied | `apply_profile_patch` replaces a whole document. The record's change is one line in git. |
| Consolidation promotes durable facts into `me/` | `ProfileReflectionFlow` does, re-targeted to emit line operations | It already does this job and is already approved weekly. |
| `agent_dir` becomes a per-agent map | `agent_dir` accepts `{agent}` | One field. `raphael/` stays where it is. |
| The seed creates the record notes directly | The seed creates drafts. The owner accepts a draft by renaming it | A whole first draft about him is bigger than one bullet (§12). |
| (not in the sketch) | Compiled record notes leave the note index | The same text would reach a prompt twice. `questions_dir` is the precedent. |

`apply_profile_patch` keeps one job: it writes the cache row, so `agent_profile_revisions` stays the log of what reached a prompt.

## Non-goals

- No editing of anything the owner wrote outside `me/`. The insert-only rule of the 09-12 spec holds everywhere else. The one note AEGIS rewrites is a note it alone owns and nobody has edited (§8).
- No merging by AEGIS. Every merge still happens on the owner's device.
- No second ledger. No amount goes into `me/money.md` (§10).
- No transcripts and no full meeting notes in the vault (§9).
- A commitment does not become a Todoist task here. That is a separate decision.
- No mirror of the topic registry or the feed list in a note (§11).
- No people pages created by AEGIS, no sync into `life.people`, no `#ask` from a note, no incident write-ups, no morning briefing in the daily note (§15).
- No nagging. One diary card a day at most, and it expires.

## 1. The journal moves to the `gtd` holder

**Code.**

- One constant, `JOURNAL_OWNER_TAG = "gtd"`, in `core/src/aegis/services/notes.py`. It replaces the literal in `flows/daylog.py:65`, `flows/notes_backfill.py:38` and `activities/notes.py:52`. `services/tools/notes.py:49` keeps `research`: a chat write with no calling agent is a knowledge note.
- `config/seed/activities.yaml`: `daylog-nightly`, `daylog-weekly`, `daylog-monthly` and `notes-backfill-weekly` change to the seed's `gtd` holder. `notes-sync-hourly` stays: the index has no author.
- `entry.tag` accepts `{agent}`. `notes.journal_append` takes the writing agent's id and resolves the tag once. A literal tag behaves as today.
- The code default for `entry.tag` becomes `#{agent}`, because a default must name nobody. A migration writes a `vault_layout` row holding the old literals only where a `notes_index_state` row exists, which means only on a deployment that has already synced a vault. It follows migrations 048 and 049.

**Config.** The owner sets `entry.tag` to `#{agent}` on the admin Vault page.

**The old entries** keep `#raphael`. Nothing rewrites them and nothing needs to: every reader uses the marker. A rollup that spans the change reads all seven days.

**What follows the row without code:** the commit author, `workflow_runs.agent_id` and `llm_calls.agent_id`.

## 2. Free text on a Slack card

The `input` kind becomes Slack-native. No new kind, no migration: `interactions.kind` has no CHECK constraint.

- `comms/src/aegis_comms/cards.py`, `input` branch: an **Answer** button (`action_id = "text_open"`) beside the admin link.
- `comms/src/aegis_comms/slack_modal.py`: `build_text_modal(interaction_id, prompt, label, placeholder)`, with a multiline `plain_text_input`, `max_length` 3000 (Slack's limit) and `callback_id = "text_submit"`. The label comes from `options`, which already reaches the renderer.
- `comms/src/aegis_comms/adapters/slack.py`: `handle_text_open` and `handle_text_submit`, registered as the hint handlers are. Submit calls `core.resolve_interaction(interaction_id, value=<text>)`.
- The response is `{"value": "<text>"}`, the shape the admin textarea sends (`admin-panel/frontend/src/pages/InteractionDetail.tsx:248-264`). `apply_curiosity_answer` works unchanged, and `record_correction_from_interaction` writes nothing.
- After submit the card is edited to "Answered". It never quotes the text.

## 3. The journal gap prompt (`JournalPromptFlow`)

One `FlowSpec` in `worker/src/aegis_worker/registry.py`, one seed row `journal-prompt-daily` owned by the `gtd` holder, `active: false`, example cron 03:00 UTC.

1. **Which day.** The flow converts its clock with `DayLogActivities.daylog_local_day` and asks about `logged_day(local_today)` (`flows/daylog.py:104`), the daylog's own functions.
2. **Is there a gap.** A new activity `journal_gap_check(day)` reads the day's notes with `notes.read_journal_days_sync` (filed and live, encrypted blocks stripped). It removes every agent block: for each `%% aegis:<key> %%` it takes the second value of `split_section`. It drops the rendered template's lines, headings, empty bullets, checkbox lines and frontmatter, and counts the words left. Fewer than `min_words` (default 5) is a gap. A note holding `aegis:selfreport:<day>` is never a gap.
3. **Ask once.** A gap starts an `InteractionFlow` child: kind `input`, origin `journal_prompt`, `timeout_seconds` 20 hours, policy `archive`, no escalation, `post_resolve_activity = "file_journal_answer"`, parent close policy ABANDON. The workflow id is `journal-prompt-<day>`.
4. **File the answer.** `file_journal_answer` makes no model call. It passes the text to `notes.journal_append` with a new `slot` argument, so the key is `selfreport:<day>`. The author is the `gtd` holder. `clean_body` removes control characters and defuses a forged marker. Nothing else changes the text.
5. **The way back.** `InteractionFlow` swallows a failed post-resolve hook. So `NotesBackfillFlow` also sweeps `journal_prompt` interactions resolved in the last `since_days` whose note lacks the marker, and files them.

## 4. The `me/` record: layout and gate

A `record` block in the `vault_layout` row (`vault_layout.py`: `DEFAULTS`, `merge`, `validate`, `Layout`; admin `Vault.tsx` gets a "The record" panel):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Off means nothing compiles the record, and the `user` rows behave as today |
| `dir` | `me` | The folder. One level, no subfolders |
| `shared` | `["about"]` | Notes every agent reads |
| `by_tag` | `{}` | Capability tag to note names. The owner's row maps `finance`, `infra`, `research` and `gtd` to his domain notes |
| `no_amounts` | `[]` | Notes where a line with a money shape is refused. The owner's row lists `money` |
| `max_chars` | 6000 | The cap on one agent's compiled document |
| `line_max` | 500 | The longest bullet an agent may write |
| `default_section` | `Notes` | Where an add goes when the tool names no section |
| `people_dir` | `people` | People pages. AEGIS appends to one that exists and never creates one (§9) |

The `gtd` holder also reads every record note no tag claims. That is the generalist rule of #556, and it is code. A file named `<name>.draft.md` is a draft: never compiled, never indexed (§12).

**The gate.** `notes.check_path` gains `record=True`, which allows `<dir>/<name>.md` and nothing deeper. A new write shape sits beside `Append`:

```
RecordEdit(rel, op, line, new_line="", section="", subject="", approval="", layout)
```

- `add`: a no-op when the normalised line is already in the note, or, when `subject` is set, when any bullet already starts with that subject. Otherwise `insert_block` puts `- <line>` at the end of the named section, or under a new `## <section>`. The guard is `is_one_insertion`. A missing note that is in the map is created as `# <Title>`.
- `change`: the bullet text must match exactly one line. It replaces that line's text and keeps its indent. A new guard, `is_one_line_change`, requires the same line count with exactly one line different.
- `remove`: the same match, and it deletes that one line. It refuses a bullet with indented children. The guard is `is_one_line_removal`.
- `change` and `remove` raise without a non-empty `approval`. The activity verifies it against the database (§6).
- A line bound for a `no_amounts` note that passes `bank_parsers.has_money_shape` is refused with a sentence: the figure lives in the books.
- No match is `gone`. More than one is `ambiguous`. Both write nothing and are reported.

`write_sync` keeps its loop. No hidden marker goes on a record bullet: the owner edits these notes by hand, and git blame says which agent added a line.

This amends the 09-12 spec's "no editing or deleting, ever": outside `me/`, still never. Inside `me/`, one line at a time, and only the exact line the owner approved.

## 5. Reading the record: compile into the `user` row

Prompt assembly does not change. `_build_agent_system_prompt` (`core/src/aegis/services/chat.py:965-997`), the money extraction persona (`activities/money.py:34-47`), curiosity's `_known_text` and the reflection flow all keep reading `agent_personalities(kind='user')`. That row becomes a cache.

A new module, `core/src/aegis/services/record.py`:

- `notes_for(tags, layout, present)`: shared notes, then the notes mapped to the agent's tags, then, for the `gtd` holder, every note no tag claims. Drafts are never present.
- `render(texts, layout)`: per note, a `From <dir>/<name>.md:` line and the body, with frontmatter removed, `strip_encrypted` applied and `%% … %%` comments removed. It stops at `max_chars` and says so in the text.
- `compile_all(pool, cfg, layout)`: for each active agent, when the fingerprint differs, `apply_profile_patch(pool, agent_id, "user", doc, source="vault_record", allow_shrink=True)`. A shrink is accepted: the owner's deletion is the record. A missing record folder skips the run and warns.
- State goes to `settings.notes_record_state`: the commit, and per agent the fingerprint, size, note list and whether it was cut. The Vault page shows it, with the drafts that are waiting.

**When.** A new activity, `notes_compile_record`, runs at the end of `NotesSyncFlow` (hourly) and after every record write by AEGIS.

**The switch.** Turning `record.enabled` on is refused while the `gtd` holder's compiled document would be empty and its current row is not. That stops the switch from wiping the one real document before its draft is accepted.

**Drift.** The copy runs one way. `put_agent_personality` (`core/src/aegis/api/routes/agents.py:44`) and `revert_profile_revision` answer 409 for the `user` kind while the record is on, and `AgentDetail.tsx` shows that document read-only with its source notes. A row whose fingerprint differs from the state was written by hand. The next compile overwrites it and logs `record_cache_overwritten`.

**The index.** `Layout.is_indexable` leaves out compiled record notes and drafts. Other agents reach a record note with `note_read`. Each prompt lists the record notes by name.

**Until §7 ships**, `propose_profile_patch` returns no proposal with `reason = "record_in_vault"` while the record is on.

## 6. Writing the record: add freely, ask to change

Two chat tools in `core/src/aegis/services/tools/notes.py`, built with `@aegis_tool`. The golden file `tests/core/fixtures/chat_tools_golden.json` is regenerated.

- `owner_record_add(note, line, section="")`. The note must be in the map or already exist. It goes through `notes_write.normalise`, then `NotesWriteFlow` under `notes-write-record_add-<digest>`. `notes_write.OPS` gains `record_add`.
- `owner_record_change(note, line, new_line="", reason)`. An empty `new_line` means remove. The tool pulls, checks that the line matches exactly once, and starts `RecordChangeFlow` under `record-change-<digest>`, which spawns an `approval` card (origin `record_change`) showing the note, the old line, the new line and the reason. At most three cards may be pending per agent.
- On approve, the post-resolve activity `NotesActivities.record_apply(interaction_id)` loads the interaction, checks status `resolved`, value `approve` and a record origin, builds the `RecordEdit` with `approval=<interaction id>` and writes it. On `gone` or `ambiguous` it tells the agent's channel that nothing was written.

The `remember_this` docstring gains one sentence: a lasting fact about the owner goes to `owner_record_add`.

## 7. What feeds the record after the seed

1. **The owner says it in chat.** `owner_record_add`, written at once.
2. **Curiosity answers.** `apply_curiosity_answer` writes `<question> — <answer>` on one line to the asking agent's first mapped note. When the vault is off or the write fails it calls `record_memory` as today, so no answer is lost.
3. **An observed autopay.** When a due's mail says the money moves by itself, the money activity adds `<Payee>: paid automatically (seen <month>)` to the finance note with `subject = <payee>`. The `subject` rule means a line the owner has since rewritten is never added again. It is a regex reading of his own mail, not a model's inference, so it is added at once. It carries no amount.
4. **The weekly reflection.** `propose_profile_patch` (`worker/src/aegis_worker/activities/profile.py:620-697`) is given the agent's record notes and returns up to eight line operations. They go on one `approval` card, origin `record_reflection`. Approve applies them in one commit. It replaces today's weekly `draft_review` card. Its evidence (`gather_profile_evidence`, `profile.py:303-347`) gains the week's `meeting_review` rows, which is how a pattern from meetings reaches `me/work.md`. This is open question 2.
5. **Card corrections and triage corrections** stay in `agent_memory`. They are lessons about how to act, and the triage ones are known to be polluted.

## 8. Per-agent folders and reports

- `agent_dir` accepts `{agent}`. `notes_write.normalise_path` and `notes.check_path` take the writing agent, from a new `Append.agent` field. The research holder's folder is still `raphael/`. `questions_dir` is untouched.
- **The weekly review** needs no folder. `WeeklyReviewFlow` (`worker/src/aegis_worker/flows/review.py`, after `send_message`) calls `notes_journal_write` with `slot: "review"`. The block lands in the weekly note under `aegis:review:<week>`. It is best-effort.
- **The month close** (`worker/src/aegis_worker/activities/money.py:1287-1301`) already commits `reports/monthly/<month>.md` to the books repository, and that stays the record. It gains a copy at `<finance folder>/reports/<month>.md` and a headline block with a wikilink in the month note (`slot: "close"`). The copy's first line says where the record is. It is written once and never updated.

**A note AEGIS alone owns (a generated note).** A standing projection has to change, and insert-only cannot change anything. So there is a third write shape, `Generated(rel, key, body)`, with a narrow rule: AEGIS replaces only text it wrote and nobody has edited.

- The path must be under the writing agent's own folder. Never `me/`, never a journal note, never a people page.
- The first line is `%% aegis:generated:<key> sha:<hash of the body> %%`. The second line says who generated it, from what, and that the next run replaces it.
- A replace is allowed when the note does not exist, or when its first line carries the same key and the hash still matches the body under it. A match means nobody edited it.
- When the hash does not match, the owner has edited the note. AEGIS writes nothing and tells the agent's channel once: move your lines to `me/`, then delete the note and it will be written again.
- A body equal to the old one is a no-op, so an unchanged week makes no commit. The body therefore holds no run date, only a "last changed" date.

`_apply` dispatches on the shape, and `write_sync` keeps its loop. §10 is the one user.

## 9. Meetings: what reaches the vault

The `gtd` holder owns this. The `meeting` and `meeting_review` knowledge rows stay the record of the notes and the review. The vault gets the important things only.

**One model call, three more lists.** `_REVIEW_SYSTEM` (`activities/meeting.py:59-68`) gains `decisions` (what the meeting decided, at most 5), `asked_of_me` (what others asked him to do, with who and any date, at most 5) and `people` (at most 5 `{name, line}` items, names limited to the speaker labels: what that person owns, asked for or cares about, as said in this meeting). `_str_list` parses them as it parses `commitments`. It is the same call.

**On each new meeting**, after `analyse_meeting` returns (`flows/meeting_notes.py:145-157`), a new activity `meeting_vault_note` writes:

1. **One short meeting note**, `<gtd folder>/meetings/<YYYY-MM-DD> <title>.md`, created once under the key `meeting:<doc id>`. It holds the date, the attendees as `[[Name]]` links, a link to the Google Doc, and the sections Decisions, I committed to, Asked of me, I raised and People. It is about 1,500 characters. It holds no notes text and no transcript. The folder is not indexed, because the `meeting_review` row already carries the same text. It needs §8's folders.
2. **One line on a people page that already exists.** For each `people` item, when `<people_dir>/<Name>.md` is in the checkout, it appends `- <date> [[<meeting note>]]: <line>` under the key `meeting:<doc id>:<name>`. AEGIS never creates a people page. The `[[Name]]` links in the meeting note are unresolved until the owner clicks one, and that click is his opt-in for that person. Speaker labels are free text, so a label that matches no page writes nothing. People pages are the owner's notes and are indexed like any other.
3. **The day note, through the nightly day log.** The day log's meetings source (`_source_meetings`, `activities/daylog.py`) adds, for each of that day's meetings, the meeting note's link, his commitments and what was asked of him. It lands at 00:30 with the rest of the day log. Nothing writes to today's note, so a day never gets two notes.

**Nothing from a meeting goes straight into `me/`.** There are two reasons. At 2 to 5 meetings a week, with up to 20 lines each, the 6,000-character cap would be gone in weeks, and most lines stop mattering within a month. And every one of these lines is a model's reading of a meeting, which is an inferred fact. Open question 2 puts inferred facts on the weekly card, and meetings follow the same rule: the weekly reflection reads the week's `meeting_review` rows as evidence (§7) and proposes at most eight lines, for `me/work.md` or `me/people.md`, that he approves together.

The meeting note, the people-page line and the day-log lines are not the record. They are written once, they are never compiled into a prompt, and the knowledge row stays the record. That is why they may be written at once.

**Skipped** when the review was skipped (`analysis = no_self_names`).

## 10. Finance: the facts at hand

Two homes, split by who owns the fact.

**A. `me/money.md`: facts the vault owns. No amounts.**

| Section | What it holds | Where a line comes from after the seed |
|---|---|---|
| Accounts and cards | Which exist and what each is for | The owner. `at-hand.md` lists every account name in the books, so a new one shows there first |
| Billers and how each is paid | Autopay or by hand, from which account, the usual due day | The owner, and the autopay observation (§7) |
| Entities and filing | The sets of books, and how often each files what | The owner. No database holds this |
| Subscriptions | What recurs and how often | The owner |
| What you told me | His curiosity answers, in his words | Curiosity answers (§7) |

The `no_amounts` guard refuses a line with a money shape. The tool's description says the figure lives in the books and points to `ledger_query`. This is what keeps the note from becoming a second ledger.

**B. `<finance folder>/at-hand.md`: a standing projection. The database and the journal stay the record.** It is a generated note (§8): the finance agent alone owns it and rewrites it whole. Due dates move every week, so a monthly snapshot would be stale by mid-month, and an insert-only note could never drop a bill that was paid.

- Open dues from `journal_index.OPEN_DUE_SQL` (`core/src/aegis/services/journal_index.py:258-262`): payee and due date.
- Billers seen in the last 180 days: payee, usual due day, last channel and instrument label.
- `life.expiring_items` that expire in the next 90 days.
- The account names from `hledger accounts`, and the entity labels from `books_chart`.

It carries names and dates. It carries no amount and no balance, and the generator checks every line with `has_money_shape` before it writes. It is regenerated as the last step of `MoneyBriefFlow` (`money-brief-weekly`) and of `MonthCloseFlow`, best-effort. `me/money.md` links to it, so the owned facts and the current dates are one click apart. It is indexed like any note, so any agent can answer "when is X due" from it.

If he edits it, the hash no longer matches and AEGIS leaves it alone and says so (§8). His own facts belong in `me/money.md`.

**What each source becomes.**

| Source | Verdict |
|---|---|
| The hledger journal | The record. Never moves |
| `finance.journal_index` | The index. Projected weekly (B). Read once for the seed |
| `finance.recurring_charge` (frozen 2026-09-04) | Read once for the seed's Subscriptions section, marked "last seen 2026-09". Never a schedule |
| `finance.renewal_alert`, `finance.subscription_digest` (frozen) | Not used. Dead tables |
| The `books_chart` row | The database is the record, because posting reads it. Its entity labels are read once for the seed |
| `life.expiring_items` | The database is the record. Projected weekly (B) |
| The finance agent's `user` document | Starter text. Skipped |
| The finance agent's 12 curiosity rows | Become vault-owned lines through the seed (§12) |

## 11. Research interests

`me/interests.md` is vault-owned and read by the research holder on every turn. It holds themes: what he cares about, how deep he already is, what he is tired of. It does not list the tracked topics or the feeds.

**The registries stay in the database.** Scans, the RSS gate and the problem hub read `intelligence_topics`. `RssIngestFlow` reads `channels`. A copy of either in a note would be stale within a week. Nothing flows from the note to the registries by itself: the owner tracks a topic with `track_topic` or on Admin → Research, as today.

**Sources for the first draft** (§12): the 20 topic names and priorities, the 36 feed labels, tag counts over the 235 Calibre books, Raindrop tags, and the vault's own structure: folder names, titles and Obsidian tags of `knowledge/` (303), `literature/` (42), `reference/` (43) and the 3 root notes, read from the checkout AEGIS already has. **`journal/` is excluded by path in code.** Note bodies are not read, only titles and tags.

**After the seed** the note grows through `owner_record_add` and the weekly reflection card. The topic round digests of §15 go to `raphael/topics/`, not to `me/`.

## 12. Seeding

"Add freely" covers one bullet. A first draft of a whole note about him is bigger, so it is a draft until he accepts it.

**The mechanism.**

- A hand-started `RecordSeedFlow`, launched from a button on the Vault page, writes each draft as `<dir>/<name>.draft.md`. A draft is created once and never updated. It is never compiled and never indexed.
- **Accept** is a rename to `<name>.md`, in Obsidian or on github.com. He may edit first. **Reject** is a delete. When `<name>.md` already exists, he copies the lines he wants and deletes the draft.
- **Nothing detects "accepted".** The compile reads `<name>.md` and nothing else, so a draft counts from the hour its name loses `.draft`. AEGIS never edits, moves or promotes a draft, and keeps no state about it.
- A suffix is used, not a `_drafts/` folder, because the record gate allows one level under `dir` (§4). Both work the same way on github.com: edit the file's name.
- He gets one Slack message listing the drafts. There are no cards.
- Every draft opens with a line saying what it was built from and on which date.
- The flow refuses a note that already has a draft. Deleting the draft allows a fresh one.
- He then turns `record.enabled` on. The switch guard of §5 holds until the general notes are accepted.

**Draft by draft.**

| Draft | Built from | Drafted by | Model call | Check before it is written |
|---|---|---|---|---|
| `about`, `work`, `people`, `health` | The `gtd` holder's `user` document (2,042 characters) and its 5 curiosity answers, sorted into the four notes | `gtd` holder | One call: "sort these lines, do not reword, drop nothing" | Every input line appears word for word in exactly one draft. Otherwise nothing is written |
| `people`, in addition | The speaker labels, with a meeting count and the last month met, as a list to annotate | `gtd` holder | None | Names and counts only |
| `work`, in addition | Recurring meeting titles with counts | `gtd` holder | None | None |
| `money` | `hledger accounts` (names, no balances), the `books_chart` entity labels, the billers from `journal_index` (per payee: usual due day, last channel), the frozen `recurring_charge` (per vendor: cadence, "last seen 2026-09"), the names in `life.expiring_items`, the finance agent's curiosity answers word for word, and empty "how is it paid" and "how often does it file" lines for him to fill | `finance` holder | None. It is assembled from columns | No line passes `has_money_shape` |
| `interests` | §11's sources | `research` holder | One call over titles and tags, about 20,000 characters, purpose `record_seed` | Each theme cites at least two sources that exist. No cited path is under `journal/`. A theme that fails is dropped |
| `infra` | Nothing. The `user` document is starter text and the one memory row is a lesson about acting | None | None | Not drafted. The first add creates the note |

An empty draft is not written. `health` has no source today, so it appears only if the sort call finds health lines. The `money` draft links to `at-hand.md` (§10) once that note exists.

**What the two model calls cost, and what they send.** The sort sends the 2,042-character document and 5 short answers, text the same model already reads on every turn of that agent. The interests call sends about 390 note titles with their folders and tags, 20 topic names, 36 feed labels and the book and bookmark tag counts: about 20,000 characters, or roughly 6,000 tokens in and 1,500 out, once. No note body is sent, and no `journal/` path or title is. Those titles already sit in the note index and already reach prompts as search results. The whole seed is two calls, run once.

**The memory rows.** The 17 curiosity rows stay live until the record is on. A hand-run step, `retire_seeded_memory`, then retires each row whose answer text is found in a record note. It does so through a DELETE plan passed to `apply_consolidation`, the only sanctioned writer, so the ops log records it. A row whose answer he cut from the draft stays live.

**Past meetings.** The meeting step takes `backfill_days`, default 0. With a window it writes meeting notes for the reviewed meetings in it from the stored `metadata.review`, with no model call. They have no decisions, asked-of-me or people sections, because those fields did not exist then. This is open question 6.

**What needs the Mac.** No draft needs it to be written: every source is in the database or in the checkout AEGIS already has. Reviewing five drafts is comfortable only on a synced device. On github.com it works, note by note. So ship the seed when it is ready, and run it on the day the Mac syncs. The `money` draft is the one worth running sooner on github.com, because he fills in its blanks from memory.

## 13. Tool grants, routing and personas

These are database writes on the admin Behavior tab, plus the seed for a fresh install.

- `note_search`, `note_read`, `note_write`, `note_link`, `owner_record_add` and `owner_record_change` go to all four agents through `agents.metadata.tool_set`. `AGENT_TOOL_SETS` in `chat.py` is updated only for the boot check and the tests.
- `intent_keywords`: the `gtd` holder gains `journal`, `diary`, `day log` and `meeting`. The research holder gains `note`, `vault` and `obsidian`.
- The `soul` documents are human-only. The owner adds a short paragraph to each that names the note tools and the agent's record notes. A tool that no persona mentions goes unused: `chat_tool_calls` has no `note_*` row today. The PR that ships a tool gives the paragraph's wording in its description.
- On this deployment `note_search` and `note_read` were granted to the other three agents on 2026-09-22. The write tools wait for their phases.

## 14. Store by store

| Store | Holds | Verdict | Move and drift control |
|---|---|---|---|
| `agent_personalities`, kind `user` (4 rows, 1 real) | Prose about the owner | **Vault is the record, the row is a cache** | Seeded as drafts (§12). One-way compile, PUT refused, fingerprint state |
| `agent_personalities`, kinds `soul`, `agents`, `memory` | Who the agent is | Database only | Edited by a person in admin |
| `agent_profile_revisions` (5) | Before and after of persona edits | Database only | Logs what reached a prompt. Git is the history of the record |
| `agent_memory`, source `curiosity` (17 live) | The owner's typed answers | **Vault is the record** | Into the drafts, then retired once found in a record note (§12) |
| `agent_memory`, other sources (11 live, 39 retired) | How-to-act lessons | Database only. **Must not move**: polluted and operational | None |
| `agent_memory_ops_log` (82) | Consolidation ledger | Database only | None |
| `life.people` (0), `life.assets` (0) | Registries nobody fills | Leave alone | `me/people.md` and opt-in people pages (§9) |
| `life.expiring_items` (5) | Dates the radar reads | Database is the record | Projected weekly into `at-hand.md` (§10 B) |
| `life.observations` (162) | Numeric series | Database only | None |
| `knowledge_content`: meeting, meeting_review (74, 55) | Notes and self-reviews | Database is the record | A short note per meeting, written once (§9) |
| `knowledge_content`: outside material, briefings | An index | Database only | None |
| `knowledge_content`: note, daylog, daylog_rollup, research | Vault-first already | Done in #514 | None |
| `intelligence_topics`, `research_topics_config`, `channels` (rss) | What the scans and feeds read | Database is the record. **Must not be mirrored** | Read once for the `interests` draft (§11) |
| `books_chart` | The chart of accounts | Database is the record | Entity labels read once for the `money` draft |
| `finance.journal_index` and the hledger journal | Money | The books repository is the record. **Must not move into the vault** | The vault has no `hledger check --strict` and no shared flock. Names and dates are projected weekly into a generated note (§10 B). The `no_amounts` guard protects `me/money.md` |
| `finance.recurring_charge`, `renewal_alert`, `subscription_digest` | v1 subscription tables, frozen since 2026-09-05 | Dead | `recurring_charge` is read once for the seed. The other two are not used |
| `review_digest_log` (96) and the Slack review | The weekly review | Database is the record, projected | §8. Written once under a marker |
| `chat_history`, `interactions`, `problems`, `todoist_tasks`, `settings`, `activities`, `agents` | Operational state and configuration | Database only. **Must not move** | Workflows signal `interactions`. `problems` owns alert identity. Todoist is the record for tasks |

**What flows from the vault to the database**, beyond the note index:

1. The `me/` notes become the `user` rows (§5).
2. The owner's journal words are read by the gap check. They are read only and never copied.
3. The owner's hand edits in `me/` stop repeat curiosity questions, because `_known_text` reads the persona rows.
4. A people page that exists switches on the per-meeting line for that person (§9).
5. A diary answer passes from Slack through `interactions.response` into the vault. The database copy is transient.

## 15. Best other uses

Meetings moved into the design (§9). Kept, ranked by value against cost:

1. **The weekly review in the weekly note** (`gtd`). One activity call and the `slot` argument.
2. **The month close and the desk score in the month note** (`finance`). It needs §8's folders.
3. **Tracked-topic round digests** (`research`). When a round closes, a dated section with its articles goes to `raphael/topics/<topic>.md` through the existing `write` operation. Today a closed round's articles are left only in `problem_events`.
4. **Reading highlights.** The owner installs the Kindle plugin, and `NotesSyncFlow` indexes what it writes. There is no AEGIS code. It needs a synced device.

Rejected, one line each:

- **People pages created by AEGIS, or synced to `life.people`:** speaker labels are free text and would make duplicate pages. Opt-in lines on pages he creates cost nothing.
- **A decisions log:** it is just another `me/` note.
- **The morning briefing in the daily note:** it is about today, and AEGIS writes only days that have passed.
- **Expiring items from a note:** the admin form holds 5 rows. Revisit if `me/` shows he edits notes more than forms.
- **Incident write-ups:** the hub timeline, the Todoist task and the knowledge row already exist.
- **`#ask` from inside a note:** Slack answers in seconds. A note round trip takes an hour or more.
- **Dataview frontmatter:** only on notes AEGIS creates, and only once he says he uses Dataview.

## 16. Phasing

Each phase is one PR, smallest useful first. No phase needs the Mac to ship. The last column says what the Mac changes.

| # | PR | Touches | Config or code | Proof on production | Mac |
|---|---|---|---|---|---|
| 1 | The journal moves to the `gtd` holder | `notes.py`, the three `_OWNER_TAG` sites, the seed yaml, the tag format, one migration, the agents seed | Code and seed. The owner sets `entry.tag`, the grants, the keywords and the persona text | The next nightly block opens `- #sebas`. `git log -1 --format=%an` on the vault is the `gtd` holder. Sunday's rollup logs `daylog_rollup_gathered n=7` across the change | No |
| 2 | The weekly review in the weekly note | `journal_append` `slot`, `notes_journal_write`, `flows/review.py` | Code | The weekly note holds `aegis:review:<week>`. A second run adds nothing | No |
| 3 | Text answers in Slack | comms only | Code | A curiosity card answered from the phone leaves `response->>'value'` set and a `curiosity` memory row | No |
| 4 | The journal gap prompt | A new flow and two activities, a seed row (inactive), the backfill sweep | Code. The cron, the prompt text and `min_words` are config | A day with no words gives one card, and the answer gives a commit with `aegis:selfreport:<day>`. A rerun gives neither. A day with words logs `gap=false` | Until it syncs, every day is a gap |
| 5 | The record, read side | The `record` block, `record.py`, the gate, `notes_compile_record`, the PUT 409, the Vault page, the index skip, the switch guard | Code. The map and the switch are config | With only drafts present the rows do not change. A line edited on GitHub reaches the row within the hour with a `vault_record` revision. A hand-edited row is overwritten and logged | Edits are on github.com until it syncs |
| 6 | **The seed** | `RecordSeedFlow`, the three drafters, `retire_seeded_memory` | Code. Started by hand | The drafts exist. `money.draft` has no line with a money shape. The sort reports lines in equal to lines out. No `journal/` path is in the interests draft. Rows are retired only where the text is in a record note | Review is comfortable only after it syncs |
| 7 | Add | `RecordEdit` add, the `record_add` operation, `owner_record_add`, the `no_amounts` guard | Code | A chat add makes a one-line commit. The same add again says it is already there. An amount bound for `money` is refused in words | No |
| 8 | Ask to change | `RecordEdit` change and remove, the two guards, `RecordChangeFlow`, `record_apply`, `owner_record_change` | Code | Approve gives a commit of one insertion and one deletion. Editing the line between the ask and the approval gives "nothing written" | No |
| 9 | The feeders | `apply_curiosity_answer`, the autopay observation, the `propose_profile_patch` contract with meeting evidence, the reflection card | Code | A curiosity answer becomes a line in `me/` and no new memory row. The weekly card lists operations. An autopay mail adds one line, and the next mail from that biller adds none | No |
| 10 | Per-agent folders and the month-close copy | `agent_dir` `{agent}`, `Append.agent`, `money.py` | Code. The owner sets `agent_dir` | `<finance folder>/reports/<month>.md` exists and the month note links it. A write into another agent's folder is refused | No |
| 11 | The finance note at hand | The `Generated` write shape and its guard, the generator, one step in `MoneyBriefFlow` and `MonthCloseFlow` | Code | `at-hand.md` exists with no money shape in it. A week with no change makes no commit. A bill ticked off in Todoist is gone from it the next week. A hand edit stops the rewrite and sends one message | No |
| 12 | Meetings | The three prompt lists, `meeting_vault_note`, the opt-in people lines, the day-log lines, `backfill_days` | Code | The next meeting makes a short note with linked attendees and no notes text. A people page that exists gains one line and no page is created. The folder has no index rows | People pages need a device to create them |
| 13 | Topic round digests | The research round close | Code | A closed round adds a dated section once | No |

## 17. Risks

- **Privacy, `me/`:** whatever is in a mapped note goes to the hosted model on every turn of that agent, and on every money extraction call (`_format_agent_persona`). His controls are the map, and meld-encrypt, which never leaves the file.
- **Privacy, the seed:** the interests drafter sends about 390 note titles and their tags to the hosted model. Those notes are already indexed and already reach prompts as snippets. `journal/` never goes.
- **Privacy, third parties:** the `people` list is a model's reading of what colleagues said. It lands only in his private vault and only on pages he created.
- **Privacy, the journal:** all four agents can call `note_read` on any note. They share one provider, so what is new is the volume.
- **Privacy, Slack:** a diary answer passes through Slack. The admin textarea is the other way in.
- **A second ledger by stealth:** the `no_amounts` guard is a pattern match, so an amount written in words gets past it. The tool's description and the finance persona say the same thing.
- **Prompt size:** the cap is 6,000 characters per agent. A long `me/money.md` raises the cost of every receipt extraction.
- **Stale drafts:** a draft that waits a month describes last month's billers. It says its date, and deleting it allows a fresh one.
- **Merge conflicts on `me/`:** AEGIS adds at the end of a section. A conflict needs him editing that section's last lines, unsynced, in the same hour, and it would happen on his device.
- **Two copies drifting:** the cache is one-way and self-healing. Every other projection is either written once or, for `at-hand.md`, rewritten whole from the record each week. Each says where the record is.
- **A generated note he edits:** the hash check leaves his edit alone and the note stops updating until he deletes it. He is told once. The risk is a stale note, never a lost line.
- **Card fatigue:** at most one diary card a day. The reflection card replaces an existing weekly card. Change cards are capped at three pending per agent. The seed sends one message and no cards.
- **Slack limits:** 3,000 characters in a modal input and in a card's prompt section. A reflection card is capped at eight operations.
- **Two notes for one day:** AEGIS files a day note at 00:30 when the owner's root note for that day has not been pushed yet. Git does not conflict, but the day has two notes and he merges them by hand. It happens when the Mac was closed that evening. This exists today and the rollups read both.
- **The phone:** a phone edit travels by Obsidian Sync to the Mac and only then by git to AEGIS. With the Mac closed, the gap check can ask about a day he already wrote up on the phone.
- **Open source defaults:** the seed yaml must name an agent id for the journal rows. A fork that renames its agents edits the yaml, as it does today.
- **Slack formatting in a note:** the weekly review is Slack mrkdwn, and `*bold*` shows as italics in Obsidian. Accepted.

## 18. Questions that are the owner's

1. **Turn the gap prompt on before the Mac syncs?** Until then every day is a gap, so it is a daily diary prompt. *Recommended: yes.*
2. **Facts a model inferred from a week of evidence: add at once, or one weekly approval card?** *Recommended: the card.* "Add freely" fits what you told an agent or what your own mail says. 39 of 47 triage memory rows had to be retired.
3. **The tag on new journal blocks: `#sebas`, keep `#raphael`, or a neutral `#aegis`?** *Recommended: `#{agent}`.* Old entries stay `#raphael` either way.
4. **Which `me/` notes go to which agent, and should any be kept from a hosted model?** *Recommended:* money to finance, infra to infra, interests to research, people, health and work to Sebas, and a short `about` for everyone. Encrypt anything that must never leave.
5. **Is diary text passing through Slack acceptable?** *Recommended: yes*, with the admin page as the other way in.
6. **Meeting notes for past meetings?** *Recommended: the last 90 days*, from the stored reviews, with no model call. Older ones stay in the knowledge store.
7. **A generated `at-hand.md` that AEGIS rewrites whole each week (§8, §10 B)?** It is the one place AEGIS replaces text, and only text it wrote and nobody edited. *Recommended: yes.* It is phase 11, so dropping it changes nothing before it.

**The owner's answers, 2026-09-22.** He was shown all seven with the recommendations and said to continue, so each stands as recommended until he changes it in review of this spec. On question 1, the Mac now syncs, so the prompt asks only about days he really left empty, apart from the phone case in §17.

## 19. Testing

Local bare git repositories, as `tests/core/test_notes.py` uses. For each guard, break it and see the test fail before trusting it.

- **The owner move:** a rollup over a week whose first days carry `#raphael` and last days `#sebas` reads seven entries. `{agent}` resolves. The migration writes nothing on a fresh database.
- **The gap check:** a template-only note, a note with only the agent block, a note with only checkboxes, a live root note with words, and a note that already has a self-report.
- **The prompt:** the same day twice gives one card. An empty answer files nothing. A failed write is filed later by the backfill sweep. An answer containing `%% aegis:` cannot forge a marker.
- **`RecordEdit`:** add is idempotent on a normalised line and on a subject. A change with two matches writes nothing. A remove of a bullet with children is refused. A change without an approval raises. A path outside `me/` is refused. A money shape bound for a `no_amounts` note is refused. Each of the three guards is fed a two-line diff and must refuse it.
- **Conflict:** a second clone edits the target line between the pull and the push. The retry finds the line gone and reports, with no commit left behind.
- **Compile:** encrypted blocks, comments and drafts never reach the row. The cap cuts and says so. A shrink is accepted. A missing folder skips. A hand-edited row is overwritten. The PUT answers 409 only while the record is on. The switch is refused while the general document would be emptied.
- **The seed:** the sort refuses to write when a line is lost or reworded. A fixture `journal_index` with amounts yields a `money` draft with none. An interests theme citing a `journal/` path or a missing note is dropped. A second run writes no second draft. `retire_seeded_memory` leaves a row whose text is not in a record note.
- **The generated note:** a first write, an unchanged body (no commit), a changed body (replaced), a hand-edited note (refused and reported), a note without the marker at that path (refused), a path outside the agent's folder (refused), and a body with a money shape (refused).
- **Meetings:** the note has no notes text. A people line is written only where the page exists, and no page is ever created. The same meeting twice writes nothing new. A skipped review writes no note.
- **Comms:** the modal builds with the card's own label. Submit resolves with `value`. `record_correction_from_interaction` writes nothing for it. The edited card does not contain the text.
