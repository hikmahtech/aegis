# Research hub: a tracked topic is one hub problem that Raphael owns

**Date:** 2026-09-12
**Status:** approved by the programme brief (#513, #515); built in the same PR.
**Builds on:** the problem hub (`2026-09-07-problem-hub-design.md`), the research
lane (#509), the feeds lane (#511/#512).

## Problem

Measured on prod, 30 days to 2026-09-11:

- The intel scans created **277 `#research` Inbox tasks**. Clarify closed every
  one as `@reference` on arrival, so the user saw them only as a digest line.
  Todoist was being used as a log.
- 371 intelligence items are stored; **1** was ever used in a prompt.
- Topics lived in three places that did not agree: the scan rows'
  `activities.config.topics`, the `intelligence_topics` settings row that
  `track_topic` writes (#508 made the scans read it), and chat.
- A `#research` task's problem was minted with source `session`, so the hub
  said Pandora owned it (#522 follow-up).
- A failing feed (#511) raised an `#alert @pandora` task, and the agent sweep
  would have run the infra verb on it.

## Decisions

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Does a research problem use the full state machine? | **No.** It uses identity (`correlation_key`), occurrences, the projector and completion read-back. Never investigation, never `service_state`, never groups. | A topic is not an outage: nothing investigates it, no deploy window suppresses it, and two topics are never "the same failure". Reusing the whole machine would have meant opting out of most of it. |
| 2 | Who owns what | Source `research` → `#research @raphael @next`, Inbox. Source `feeds` → `#feeds @raphael @next`, Inbox. | Ownership is by the first occurrence's source, as for money. Both carry `@next` because clarify must not classify them (#139 needs a state label on every task). |
| 3 | Which of those may the agent sweep work? | A `#research` task: yes (the `research` verb). A `#feeds` task: **never** — `EXCLUDED_LABELS` and an explicit `None` verb. | A topic that crossed its threshold is something Raphael can research. A failing feed is the user's to fix or drop; no verb could act on it without guessing. |
| 4 | Where do a topic's search terms live? | **The `intelligence_topics` settings row** stays the registry. The problem is the activity record. | Terms must outlive a problem's rounds (see 6). Both existing readers (`load_tracked_topics` #508, `load_gate_terms` #512) keep reading the same row, so nothing moves. Prod has no row today, so there is nothing to migrate; a tracked topic with no live problem gets one on the next `track_topic` or on its first matching item. |
| 5 | What attaches to a topic | Every intel-scan worthy item and every stored RSS entry whose title or summary names one of the topic's terms (whole word, any case, no LLM) is an occurrence, external id `item:<sha1(url)>:<topic>` so the same article from two paths attaches once. The per-item Inbox capture from the intel scans stops. Raindrop keeps its capture (the user bookmarked it). | The Inbox was a log. The knowledge store still gets every item and the briefing still shows them. |
| 6 | When does a topic earn a task (attention threshold) | When the current round holds enough items: 2 for `high` priority, 3 for `medium`, 5 for `low`. Below that the problem stays in the hub and the briefing. | A topic with one new article is news, not a chore. |
| 7 | What does completing a topic task mean | "Seen." The problem resolves **and closes at once**, so the next matching item starts a fresh round (a new problem, and a new task only when that round crosses the threshold again). | The hub's normal reopen window would reopen the task the next day on one article — exactly the interruption the user just dismissed. |
| 8 | A `#research` task's own problem | Class `question`, subject the task, kind `task`, source `research`. | Research-owned, and `task` is a kind the hub already refuses to group. The `@code` path keeps class `manual` and source `session`. |
| 9 | Groups | Feed findings use the existing group machinery unchanged (three feeds failing at once is a candidate for the judge). `research` is a non-groupable source. | Topics are distinct by construction; folding two would merge unrelated news. |
| 10 | Question problems (`question:{hash}`) for chat research | **Not built.** | #509 already gives a chat question an identity (`research-<sha256>` workflow id, answer keyed on the question). A problem would only add a row nobody acts on; ResearchFlow never needs the user's input. A `#research` task is the question problem (8). |
| 11 | Curiosity loop | A new detector, `untracked_topic`: the same subject searched in the knowledge store (`search_knowledge`, `ask_knowledge`, `find_reference`, chat surface only) twice or more in 14 days with nothing found. The card asks "Track this?"; a yes calls the same `track` as the chat tool. | Maou's unknown-payee pattern: a gap the data shows, asked once, answered into config. Operator (MCP) searches are excluded — they are debugging, not interests. |
| 12 | The hub's daily Problem digest | Leaves topic problems out. | It is the infra digest; news on a topic is not a problem. The briefing gets its own topics line. |

## Shape

- `core/src/aegis/services/research_topics.py` — the one implementation:
  `load_topics`, `track`, `untrack`, `match_topics`, `attach_items`,
  `acknowledge` (close a round). The chat tools, the curiosity hook and the
  worker activity all call it.
- `hub.py`: `research` joins `SOURCES`; `digest` skips class `topic`.
- `hub_project.py`: two owners; a topic without attention is never projected
  (and `project_pending` does not select it); a topic task's description lists
  the round's items; occurrence comments read "📰 N new items"; completing a
  topic task closes its round.
- `hub_group.py`: `research` in `NON_GROUPABLE_SOURCES`.
- Worker: `IntelligenceActivities.attach_topic_items`; IntelligenceScanFlow and
  RssIngestFlow call it once per run (as built, without a `workflow.patched`
  marker); the briefing gathers `topics`; clarify treats `#feeds` and hub-projected
  `#research` tasks as hub-owned; the verb table gains `#feeds: None`.
- Tools: `track_topic` delegates to the service; `untrack_topic` is new.

## Non-goals

- Story clustering across sources beyond URL identity.
- A per-topic admin page (the Problems page shows them).
- Moving topic terms into a table.
