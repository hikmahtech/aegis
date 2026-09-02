# Meeting notes: store, analyse, digest weekly

**Date:** 2026-09-02
**Status:** implemented 2026-09-02 (PR pending)

## Problem

Meeting note-takers (Gemini in Google Meet, Read.ai, Otter, Fireflies, …) email a
summary after every call. AEGIS already sees those emails: `GmailIngestFlow`
classifies them `important_read` and `ingest_email_to_kg` files them as
`source_type='email'`. That path has three gaps:

- The email is cut at 2000 characters, so "Suggested next steps" and everything
  after is lost. Prod has 35 such rows since July; every one is truncated.
- The email links to a document that holds far more: the full notes and, for
  Gemini, a speaker-labelled transcript. Nothing follows the link.
- Nothing reads the notes back for the person. The user wants to see what they
  contributed, what problems they raised, and how to talk less.

Verified on prod on 2026-09-02, read-only:

- Gemini notes arrive from `gemini-notes@google.com` in the stockopedia mailbox.
  The email body carries one `docs.google.com/document/d/<id>` link and no
  attachments.
- Exporting that doc as `text/plain` with the account's existing token returned
  46k characters: the notes, then a transcript in `Speaker Name: text` lines.
  The doc is owned by a colleague. Export still worked because Gemini shares the
  doc with every invited guest in the organisation.
- All four live account tokens (`<label>.json`) already carry
  `drive.readonly`; `gmail_reauth._SCOPES` requests it, so any re-authorised
  account has it. The `*_gmail_token.json` files without it are legacy and not
  read by any flow.

## Decisions taken during brainstorming

| Decision | Choice | Consequence |
|---|---|---|
| Trigger | Sender-override tag `meeting`, set on the existing Email triage page | Vendor-agnostic; no new config surface; the fan-out mirrors `financial` → `MoneyProcessFlow` |
| Document fetch | Follow Google Docs links with the account's Drive token; fall back to the email body | Works for Gemini today; Read.ai (login-walled link) degrades to body-only |
| What goes in the vector store | Notes only, as `meeting`; the analysis as `meeting_review`; never the raw transcript | Transcripts would dominate search the way arXiv did in the knowledge-search starvation incident, and they are other people's speech |
| Numbers vs prose | Code computes talk share, turns, words per turn from the transcript; the LLM sees only the user's own lines plus the notes | Deterministic metrics, small prompt, trend-able through `life.observations` |
| Who is "me" | `settings.meeting_rules.self_names`; empty means store notes, skip analysis, say so in the digest | Generic for a fork; no name hardcoded |
| Weekly digest | A block appended to the existing Sunday `WeeklyReviewFlow` narrative, next to the key-dates block | No new flow, schedule, or delivery path; deterministic formatting, no extra LLM call |
| Owning agent | The child inherits `GmailIngestFlow`'s `agent_id` | No tag resolution needed; attribution stays with the mailbox owner's agent |

## Non-goals

- A `life.meetings` table, an admin page, or per-colleague statistics.
- Parsing transcript attachments (`.vtt`, `.txt`, `.pdf`). Upgrade path noted in
  code; Gemini does not attach anything.
- Calendar join (attendees, duration). The doc's speaker set is the attendee
  proxy for now.
- A conciseness "score". The metrics are the objective signal; the LLM writes one
  concrete note, not a grade.
- Backfilling the 35 historical emails in code. That is an ops step (below).

## Architecture

```
GmailIngestFlow (hourly, per mailbox)
  └─ classify_email → tags contain "meeting"
       ├─ skip ingest_email_to_kg          (one-line guard: no truncated duplicate)
       └─ start_child_workflow MeetingNotesFlow  id=meeting-notes-<msg_id>, ABANDON
              ├─ fetch_meeting_document(account, message_id)
              │     full body → docs.google.com ids → Drive export → split notes/transcript
              │     doc_status: ok | no_link | inaccessible | no_drive_scope | fetch_failed
              ├─ ingest_content  source_type=meeting  (ContentActivities, existing)
              ├─ analyse_meeting(notes, transcript, self_names)
              │     code: per-speaker stats; LLM: contributions/problems/commitments/verbosity_note
              │     writes life.observations via record_external_observation
              └─ ingest_content  source_type=meeting_review

WeeklyReviewFlow (Sunday, existing)
  └─ after check_upcoming_key_dates:
       gather_meeting_week() → format_meeting_week() → appended to the narrative
```

## Components

### 1. Fan-out in `GmailIngestFlow` (`worker/src/aegis_worker/flows/gmail_ingest.py`)

Inside the existing tag-based fan-out block:

- If `"meeting" in tag_set`: start `MeetingNotesFlow` as a child with
  `id=f"meeting-notes-{msg['id']}"`, `ParentClosePolicy.ABANDON`, input
  `MeetingNotesInput(agent_id=input.agent_id, msg=msg, account_label=label)`.
  Log `meeting_fanout_start_failed` on exception, same as the money fan-out.
- In the `important_action`/`important_read` knowledge branch: skip
  `ingest_email_to_kg` when `"meeting" in tag_set`. The child stores the full
  notes under its own url, so the 2000-character email copy is only noise.

The `meeting` tag reaches `classification["tags"]` through the sender-override
path (`match_sender_override` returns `{"category", "tags"}`), and through the
LLM/cache path if the classifier ever emits it. Nothing in the flow special-cases
the vendor.

### 2. `MeetingNotesFlow` (`worker/src/aegis_worker/flows/meeting_notes.py`)

```python
@dataclass
class MeetingNotesInput:
    agent_id: str          # first field, WorkflowRunRecorderInterceptor contract
    msg: dict              # the fetch_emails message dict
    account_label: str
```

Steps, each `execute_activity` with `RETRY_ONCE` for the fetch and `NO_RETRY`
for the LLM step, wrapped so a failure at step X raises
`ApplicationError("meeting_notes_failed at step=X: …", non_retryable=True)`:

1. `fetch_meeting_document` → `{title, meeting_date, doc_id, doc_url, notes,
   transcript, doc_status, speakers}`.
2. `ingest_content` with:
   - `url`: `gdoc://<doc_id>` when a doc was fetched, else the Gmail permalink.
     Re-running on the same email overwrites in place (content id derives from
     url), so the flow is idempotent.
   - `title`: doc name when fetched, else the email subject.
   - `source_type="meeting"`, `tags=["meeting"]`.
   - `raw_text`: the notes section, capped at 16 000 characters. Never the
     transcript.
   - `metadata`: `doc_id, doc_status, message_id, account, meeting_date,
     speakers, stats` (stats present only when a transcript parsed).
3. `analyse_meeting` (skipped when `doc_status != "ok"` and the body is under
   400 characters, or when `self_names` is empty; either way the return says
   why).
4. `ingest_content` with `source_type="meeting_review"`,
   `url=f"aegis://meeting-review/{doc_id or message_id}"`, `raw_text` = the
   rendered review (markdown, so `search_knowledge` and `ask` can answer "what
   did I commit to this week"), `metadata` = the structured JSON plus the stats
   and the parent meeting url.

Result summary: `{status, doc_status, analysis, content_id, review_content_id}`.
`status` is `stored` (notes filed), `stored_no_analysis` (notes filed, analysis
skipped with `analysis.reason`), or `skipped` (nothing usable, e.g. body under
200 characters and no doc). The Flows page shows these, so a mailbox whose token
lost the Drive scope reads `doc_status=no_drive_scope` row after row instead of
silently degrading.

Registration: one `FlowSpec(MeetingNotesFlow)` in `registry.FLOWS` with no
schedule builder, exactly like `MoneyProcessFlow`. No feature flag: the flow is
inert until a sender rule carries the tag.

### 3. `MeetingActivities` (`worker/src/aegis_worker/activities/meeting.py`)

Dataclass with `gmail_token_dir, gmail_credentials_file, db_pool, llm_client,
model_balanced, agent_id`. Constructed in `worker/__main__.py` next to
`DriveActivities` and added to the `collect_activities(...)` list. Takes the
tier-resolved `model_balanced` local, never `settings.model_*`.

**`fetch_meeting_document(account_label, message_id) -> dict`**

- Reads the message with `format="full"` and walks every MIME part (reuse
  `_extract_text_from_part` for text; scan the HTML part too, because Gemini
  puts the link only in the HTML body).
- Link extraction: `docs.google.com/document/d/([A-Za-z0-9_-]+)`; first match
  wins. No match → `doc_status="no_link"`, `notes=body`.
- Export via `aegis.services.drive._build_drive_service(token_path)` and
  `files().export(mimeType="text/plain")`. Errors map to:
  - `HttpError` 403 whose reason mentions scope, or a token whose `scopes`
    lacks `drive.readonly` → `no_drive_scope`.
  - `HttpError` 403/404 otherwise → `inaccessible` (not shared with this
    account: a forwarded email, an external organiser).
  - anything else → `fetch_failed`.
  Every non-ok status still returns `notes=body` so the flow can file
  something. Logged once per message at WARNING with the status.
- Split: `split_notes_transcript(text) -> (notes, transcript)`. Pure function. A
  speaker-shaped line is `^([A-Za-z][^:\n]{1,59}): \S`; its label is a
  *candidate* when it appears on ≥ 2 such lines anywhere in the document, or
  when it looks like a person's name (2–4 capitalised words of letters, `'`,
  `-`, `.`). The transcript opens at the first speaker-shaped line with a
  candidate label and the notes are everything before it. Inside it, a bare
  timestamp line is dropped and any other non-blank line folds into the previous
  utterance. Every real transcript has a recurring or name-like label, so "no
  candidate" is safe to read as "no transcript" and the whole text is notes.
  Not keyed on a "Transcript" heading, because the Gemini export mentions that
  word inside the notes tab too. `# ponytail: label heuristic; add a
  vendor-keyed splitter if a second note-taker's layout breaks it.`
- `meeting_date`: the email's `internal_date_ms`, ISO. Good enough for trends.
- Runs in `asyncio.to_thread` like every other Google call in the worker.

**`analyse_meeting(doc: dict, agent_id: str) -> dict`**

- Loads `settings.meeting_rules` (`services/meeting_rules.py` in core, same
  shape as `email_rules.py`: `merge` lenient, `validate` strict, defaults
  empty, 30 s cache is unnecessary at this call rate). Empty `self_names` →
  `{"skipped": "no_self_names"}`.
- Stats, code only, from the transcript: for every speaker `turns, words`; for
  self (case-insensitive match of any `self_names` entry against the speaker
  label, substring allowed) `talk_share_pct, words_per_turn,
  longest_turn_words`; plus `meeting_words_total, speaker_count`. No transcript
  → stats empty, analysis runs on notes alone and the verbosity note is
  omitted. A transcript that matches none of `self_names` is the opposite case
  and stops there with `{"skipped": "self_not_matched"}`: reviewing it would
  file a confident account of somebody else's meeting under the user's name.
- Observations: one `record_external_observation` per metric with
  `source="meeting"`, `metric in {talk_share_pct, words_per_turn, turns}`,
  `external_id=doc_id or message_id`, `observed_at=meeting_date`,
  `metadata={"title", "speaker_count"}`. `None` return means already ingested,
  not failure. That makes the existing `query_observations` chat tool answer
  "how is my talk share trending".
- LLM call (`think`, `purpose="meeting_review"`, `max_tokens=3000`, which
  `_reasoning_floor` lifts to 4096 on kimi). Prompt = notes (≤ 8 000 chars) +
  the user's own transcript lines (≤ 6 000 chars, oldest dropped first) + the
  stats. Output JSON:

  ```json
  {"contributions": ["…"], "problems_raised": ["…"], "commitments": ["…"],
   "verbosity_note": "one or two concrete sentences, or empty"}
  ```

  Lists capped at 5. `LLMTruncationError` or bad JSON → `{"skipped":
  "llm_failed"}`; the notes are already filed, so this is a degraded run, not a
  failed one, and the flow result says so.
- Returns `{"stats", "review", "rendered"}` where `rendered` is the markdown
  the flow ingests.

### 4. Config: `settings.meeting_rules`

```json
{"self_names": ["Arshad Ansari", "Arshad"]}
```

Read DB-first with empty defaults, so a fork ships nobody's name. Edited through
`PUT /api/admin/email/meeting-rules` with body `{"self_names": [...]}` (no
`value` wrapper). `validate` rejects anything but a list of non-empty strings, so
a typo'd row 400s instead of silently disabling every analysis. The generic
`PUT /api/settings/meeting_rules` can reach the same row and validates nothing —
`merge` therefore reads a non-object row as empty rather than raising, but do not
send operators there.

### 5. Weekly block in `WeeklyReviewFlow`

`ReviewActivities.gather_meeting_week() -> dict`, SQL only:

- `knowledge_content` rows with `source_type='meeting_review'` and
  `ingested_at` in the last 7 days: title, `metadata` (review + stats + doc
  status).
- `life.observations` for `source='meeting'`, `metric='talk_share_pct'` and
  `words_per_turn`, split into this week and the previous 7 days, averaged.
- Count of `meeting` rows this week whose `metadata->>'doc_status'` is not
  `ok`, grouped by `metadata->>'account'`.

`format_meeting_week(data) -> str`, pure, next to `format_key_dates`, returns
`""` when there were no meetings:

```
🎙 <b>Meetings this week</b> (4)
  • New Pipeline Standup — you spoke 6% · proposed the pull-based batch pattern
  • Data Foundations: Session 4 — 18% · raised the security-master parity gap
  Commitments: move reference collections to Postgres · review Statements export
  Talk share 11% (last week 14%) · 38 words per turn (last week 51)
  On brevity: your longest turn ran 240 words; the decision landed in the first 40.
  ⚠ 2 meetings stored without their doc — re-authorise Drive for arshad-stpd
```

The verbosity line quotes the most recent non-empty `verbosity_note`. The flow
appends the block after the key-dates block, inside the same best-effort
try/except, so a broken meeting query never costs the user their weekly review.

### 6. Registry and vocabulary

- `services/source_types.py`: add `meeting` ("Meeting notes fetched from a
  note-taker's linked document or email body; MeetingNotesFlow") and
  `meeting_review` ("Per-meeting self-review: contributions, problems,
  commitments, verbosity; MeetingNotesFlow"). Decay: default 90 days for
  `meeting_review`; `meeting` at 365, since the notes are the record.
- `tests/core/services/test_source_types.py::VERIFIED_LITERALS`: both added.
- `config/seed/activities.yaml`: nothing. The child is not scheduled.
- Migrations: none. `settings`, `knowledge_content`, `life.observations` exist.

## Data flow, end to end

1. 09:06 Gemini emails the notes; 10:00 `GmailIngestFlow` fetches it.
2. The sender rule returns `important_read` + `["meeting"]`; the email is
   labelled read and, because of the tag, *not* copied into the store.
3. `MeetingNotesFlow` exports the doc, files 6k characters of notes as
   `meeting`, computes that the user spoke 15 of 450 turns, writes three
   observations, asks the LLM for the review, files it as `meeting_review`.
4. Sunday 03:30 UTC the weekly review carries the block above.
5. Any time: "what did I commit to in standup this week" hits `meeting_review`
   through `search_knowledge`; "how is my talk share trending" hits
   `query_observations`.

## Access: what has to be true, and what happens when it is not

| Condition | How it is met | If it is not |
|---|---|---|
| Account token has `drive.readonly` | `gmail_reauth._SCOPES` includes it; all four prod tokens have it | `doc_status=no_drive_scope`; notes stored from the body; the weekly block says which account to re-authorise (Admin → Gmail re-auth) |
| The doc is shared with the account | Gemini shares with every invited guest in the organisation | `doc_status=inaccessible`; body-only, same visibility |
| The email links a Google Doc | Gemini, Otter and Fireflies do; Read.ai links its own site | `doc_status=no_link`; body-only |

No new OAuth scope, no service account, no domain-wide delegation. The one
thing a fork's operator must do is re-authorise each mailbox once after
upgrading, which the existing Drive-sync note already asks for.

## Error handling

- Fetch errors never fail the flow; they set `doc_status` and fall back to the
  body.
- LLM errors never fail the flow; the notes are filed first, the review is
  best-effort.
- Observation writes are wrapped per metric; a failure is logged and counted,
  the review still files.
- The parent (`GmailIngestFlow`) never waits on or fails because of the child.
- `gather_meeting_week` failing leaves the weekly review intact (same guard as
  key dates).

## Testing

Falsifiable tests only. Every fake mirrors the real interface it stands in for,
and each test was broken once on purpose during development to prove it fails.

- `tests/worker/activities/test_meeting_notes.py`
  - `split_notes_transcript` on a synthetic Gemini-shaped export (fake names):
    notes end before the first speaker run; speakers with one line are not
    speakers; a transcript-less doc returns the whole text as notes.
  - Stats: talk share, words per turn, longest turn from a five-line fixture,
    checked to the number.
  - Self matching: `self_names=["Sam"]` matches `Sam Doe`, case-insensitive.
  - `analyse_meeting` with empty `self_names` returns `skipped=no_self_names`
    and calls neither the LLM nor the DB.
  - Observation dedupe: running `analyse_meeting` twice against the real test
    DB leaves exactly one row per metric.
  - Link extraction: HTML-only link found; no link → `no_link`; a 403 from a
    stubbed Drive service → `inaccessible`; a token without the scope →
    `no_drive_scope`.
- `tests/worker/flows/test_meeting_notes_flow.py`, `WorkflowEnvironment` +
  stub activities as in `test_money_process.py`: `doc_status=ok` files two
  items with the right `source_type`s and urls; `no_link` files `meeting` from
  the body; a skipped analysis files no `meeting_review` and reports
  `stored_no_analysis`.
- `tests/worker/test_review_flows.py`: `format_meeting_week` renders the block
  from a fixture and returns `""` for no meetings; the missing-doc warning
  appears only when the count is non-zero.
- `GmailIngestFlow` has no test coverage today. Add one narrow flow test that a
  `meeting`-tagged classification starts the child and skips
  `ingest_email_to_kg` if the activity stubbing stays under roughly 80 lines;
  otherwise rely on the child tests plus the prod validation below and say so in
  the PR.
- `test_source_types.py` gains both literals.

CI paths: all files live under `worker/**`, `core/**`, `tests/**`, already in
the path filters.

## Rollout on this deployment

1. Email triage page: add sender override `gemini-notes@google.com` →
   `important_read`, tags `["meeting"]`. The override outranks the cached
   `triage_state` row.
2. `PUT /api/admin/email/meeting-rules` with body `{"self_names": [...]}` — the
   validating route, which 400s on a malformed list.
3. `make aegis-release` from a pulled checkout; confirm the worker boots with
   one more flow and one more activities class.
4. Wait for the next hourly run after a meeting, or trigger `gmail-ingest-hourly`
   through `temporal schedule trigger`. Check `workflow_runs` for
   `MeetingNotesFlow` with `status=stored`, `doc_status=ok`, and the two
   `knowledge_content` rows.
5. Backfill the 35 historical notes: from the core container, list
   `knowledge_content` rows with `source_type='email'` and `metadata->>'sender'
   like 'Gemini%'`, and `temporal workflow start` a `MeetingNotesFlow` per
   `message_id` with the stored `msg` shape. Ops step, no code.
6. First Sunday: read the block. If the verbosity note is generic, tighten the
   prompt; that is the only tunable expected to need a second pass.

## Open questions

None blocking. Two things to watch after the first week: whether `self_names`
substring matching ever collides with a colleague's name (switch to full-name
match if it does), and whether the 8 000-character notes cap clips a long
workshop's "next steps" section (raise it; the transcript, not the notes, is what
makes the doc large).
