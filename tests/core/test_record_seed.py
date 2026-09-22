"""The record's seed (vault record spec §12, §19 "The seed"). Made-up agents,
notes, payees and meetings; local bare repos; a fake model."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.llm import LLMTruncationError
from aegis.services import memory
from aegis.services import personalities as p
from aegis.services import record_seed as rs
from aegis.services import vault_layout as vl
from aegis.services.bank_parsers import has_money_shape
from aegis.services.books import BooksConfig
from aegis.services.settings_store import put_setting

from tests.notes_vault import make_vault, needs_git, remote_file, remote_head

GTD, FIN, RES = "zzseed-gtd", "zzseed-fin", "zzseed-res"
LAYOUT = vl.DEFAULT_LAYOUT


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def think(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"response": self.reply if isinstance(self.reply, str) else json.dumps(self.reply)}


def test_pure_helpers():
    assert rs.split_answer("Where do you shop?\nThe owner answered: The Sunday  market") == (
        "Where do you shop?", "The Sunday  market",
    )
    assert rs.split_answer("no marker here") == ("", "no marker here")
    assert rs.answer_line("Where?", "The Sunday\n market") == "Where? — The Sunday market"
    assert rs.doc_lines("---\na: 1\n---\n# User\n- Lives in Exampletown\n\n2. Runs twice a week\n") == [
        "Lives in Exampletown", "Runs twice a week",
    ]
    assert rs.placement({"about": [1], "work": [2], "people": [], "health": [3]}, 3) == {
        "about": [1], "work": [2], "people": [], "health": [3],
    }
    assert rs.placement({"about": [1, 1], "work": [2]}, 2) is None       # repeated, and 3 lost
    assert rs.placement({"about": [1], "work": [2], "gossip": []}, 2) is None
    assert rs.meeting_series("Bakery sync - 2026/09/15 10:00 GMT+05:30 - Notes by Gemini") == "Bakery sync"
    assert rs.draft_text("x", "X", "nothing", date(2026, 9, 22), [("Notes", [])]) == ""


def test_keep_themes_drops_a_journal_citation_and_a_missing_source():
    labels = {"n1": "knowledge/cooking/sourdough.md", "n2": "knowledge/cooking/pasta.md", "t1": 'topic "Home fermentation"'}
    parsed = {"themes": [
        {"theme": "Fermentation", "note": "Past the basics.", "sources": ["n1", "t1"]},
        {"theme": "Diary", "note": "x", "sources": ["n1", "journal/2026/09. Sep/12 Sep 26.md"]},
        {"theme": "Ghost", "note": "x", "sources": ["n1", "n99"]},
    ]}
    kept, dropped = rs.keep_themes(parsed, labels, LAYOUT)
    assert kept == ['Fermentation: Past the basics. (from knowledge/cooking/sourdough.md; topic "Home fermentation")']
    assert dropped == 2


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    for aid, tags in ((GTD, ["gtd"]), (FIN, ["finance"]), (RES, ["research"])):
        # agent_memory's foreign key has no ON DELETE CASCADE.
        await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active, capabilities) VALUES ($1, $1, 'r', '', true, $2)",
            aid, tags,
        )
    keys = ["vault_layout", "meeting_rules", "intelligence_topics"]
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE 'zzseed-%'")
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'zzseed-%'")
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE account = 'zzseed'")
    vl.invalidate_cache()
    p.invalidate()
    yield db_pool
    for aid in (GTD, FIN, RES):
        await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)
    await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE 'zzseed-%'")
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'zzseed-%'")
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE account = 'zzseed'")
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    vl.invalidate_cache()
    p.invalidate()


async def _meetings(pool):
    for i, (speakers, day) in enumerate(((["Ana Example", "Sam Doe"], "2026-08-11"), (["Ana Example"], "2026-09-15"))):
        await pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type, summary, tags, metadata) "
            "VALUES ($1, $2, $3, 'meeting', '', '{}', $4)",
            f"zzseed-m{i}", f"https://docs.example.com/{i}",
            f"Bakery sync - {day.replace('-', '/')} 10:00 GMT+05:30 - Notes by Gemini",
            {"speakers": speakers, "meeting_date": day},
        )
    await put_setting(pool, "meeting_rules", {"self_names": ["Sam Doe"]})


@needs_git
async def test_the_general_drafts_keep_every_line_word_for_word(pool, tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    await p.set_personality(pool, GTD, {"user": "# User\n- Lives in Exampletown\n- Works on a bakery app\n- Sister Ana visits on Sundays\n- Runs twice a week"})
    await memory.record_memory(pool, GTD, f"How do you start the day?\n{memory.CURIOSITY_ANSWER_PREFIX}Slow coffee, then a walk", importance=0.8, source="curiosity")
    await _meetings(pool)
    llm = FakeLLM({"about": [1, 5], "work": [2], "people": [3], "health": [4]})
    res = await rs.draft_general(pool, v["cfg"], LAYOUT, llm, "m", GTD)
    assert res["status"] == "written" and res["lines_in"] == res["lines_out"] == 5
    assert llm.calls[0]["purpose"] == rs.SORT_PURPOSE and llm.calls[0]["max_tokens"] == rs.SEED_MAX_TOKENS
    about = remote_file(v, "me/about.draft.md")
    assert about.startswith("%% Draft by AEGIS on ") and "- Lives in Exampletown" in about
    assert "- How do you start the day? — Slow coffee, then a walk" in about
    people = remote_file(v, "me/people.draft.md")
    assert "- Ana Example — 2 meetings, last 2026-09" in people and "Sam Doe" not in people
    assert "- Bakery sync — 2 meetings" in remote_file(v, "me/work.draft.md")
    head = remote_head(v)
    again = await rs.draft_general(pool, v["cfg"], LAYOUT, FakeLLM({}), "m", GTD)
    assert again["status"] == "exists" and remote_head(v) == head


@needs_git
@pytest.mark.parametrize("reply,reason", [
    ({"about": [1, 2], "work": [], "people": [], "health": []}, "lines_lost_or_repeated"),   # line 3 lost
    ({"about": [1, 1, 2, 3], "work": [], "people": [], "health": []}, "lines_lost_or_repeated"),
    (LLMTruncationError("empty"), "truncated"),
    ("not json", "unparseable"),
])
async def test_a_sort_that_loses_or_repeats_a_line_writes_nothing(pool, tmp_path, reply, reason):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    await p.set_personality(pool, GTD, {"user": "- one\n- two\n- three"})
    head = remote_head(v)
    res = await rs.draft_general(pool, v["cfg"], LAYOUT, FakeLLM(reply), "m", GTD)
    assert res["status"] == "refused" and res["reason"] == reason and remote_head(v) == head


@needs_git
async def test_the_general_drafter_stands_down_while_the_record_is_on(pool, tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    on = vl.layout_from({"record": {"enabled": True}})
    assert (await rs.draft_general(pool, v["cfg"], on, FakeLLM({}), "m", GTD))["status"] == "record_on"


@needs_git
async def test_the_money_draft_carries_no_money_shape(pool, tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    # Two bills on the 5th of the last two months, always inside the 180-day window.
    last = (date.today().replace(day=1) - timedelta(days=1)).replace(day=5)
    before = (last.replace(day=1) - timedelta(days=1)).replace(day=5)
    for mid, kind, payee, amount, channel, due in (
        ("zzseed-1", "due", "Example Power", 1234.00, None, before),
        ("zzseed-2", "due", "Example Power", 1250.00, None, last),
        ("zzseed-3", "debit", "Example Power", 1250.00, "upi", None),
    ):
        await pool.execute(
            "INSERT INTO finance.journal_index (message_id, mailbox, entity, kind, amount, currency, payee, "
            "payee_key, channel, occurred_on, due_on, parser) "
            "VALUES ($1, 'test', 'personal', $2, $3, 'INR', $4, 'example power', $5, current_date, $6, 'test')",
            mid, kind, amount, payee, channel, due,
        )
    await pool.execute(
        "INSERT INTO finance.recurring_charge (account, sender_label, vendor_name, amount_cents, currency, cadence) "
        "VALUES ('zzseed', 'x', 'Example Streaming', 49900, 'INR', 'monthly')"
    )
    await memory.record_memory(pool, FIN, f"Which card for travel?\n{memory.CURIOSITY_ANSWER_PREFIX}The blue one", importance=0.8, source="curiosity")
    await memory.record_memory(pool, FIN, f"Rent?\n{memory.CURIOSITY_ANSWER_PREFIX}Rs 25,000 a month", importance=0.8, source="curiosity")
    books_cfg = BooksConfig(path=tmp_path / "no-books")
    res = await rs.draft_money(pool, v["cfg"], books_cfg, LAYOUT, FIN)
    assert res["status"] == "written" and res["dropped"] >= 1   # other test files may leave rows
    text = remote_file(v, "me/money.draft.md")
    assert "Example Power: due around day 5, last paid by upi" in text
    assert "Example Streaming: monthly, last seen" in text
    assert "The blue one" in text and "25,000" not in text
    assert not any(has_money_shape(line) for line in text.splitlines())


@needs_git
async def test_the_interests_draft_cites_two_sources_and_never_the_journal(pool, tmp_path):
    v = make_vault(tmp_path, {
        "knowledge/cooking/sourdough.md": "---\ntags: [bread]\n---\n# Sourdough\n",
        "knowledge/cooking/pasta.md": "# Pasta\n#italian\n",
        "journal/2026/09. Sep/12 Sep 26.md": "# A private day\n",
    })
    # The registry's stored shape (research_topics.save_registry): {"topics": [...]}.
    await put_setting(pool, "intelligence_topics", {
        "topics": [{"name": "Home fermentation", "queries": ["fermentation"], "priority": "high"}],
    })
    llm = FakeLLM({"themes": [
        {"theme": "Fermentation", "note": "Past the basics.", "sources": ["n2", "t1"]},
        {"theme": "Private", "note": "x", "sources": ["n1", "journal/2026/09. Sep/12 Sep 26.md"]},
    ]})
    res = await rs.draft_interests(pool, v["cfg"], LAYOUT, llm, "m", RES)
    sent = llm.calls[0]["prompt"]
    assert "journal/" not in sent and "A private day" not in sent and "# Sourdough" not in sent
    assert res["status"] == "written" and res["dropped"] == 1
    text = remote_file(v, "me/interests.draft.md")
    assert "- Fermentation: Past the basics. (from knowledge/cooking/sourdough.md" in text
    assert "journal/" not in text


def test_the_message_lists_the_drafts_and_what_was_not_drafted():
    msg = rs.seed_message({
        "general": {"status": "written", "written": ["me/about.draft.md", "me/work.draft.md"]},
        "money": {"status": "exists", "existing": ["me/money.draft.md"]},
        "interests": {"status": "empty", "reason": "no_theme_cited_two_sources"},
    })
    assert "me/about.draft.md" in msg and "me/work.draft.md" in msg
    assert ".draft.md to <name>.md" in msg and "interests" in msg
