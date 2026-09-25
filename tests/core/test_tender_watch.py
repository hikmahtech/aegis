"""#673 — the GeM tender watch: bids as area items, filed once, never too late."""

from __future__ import annotations

import json
import uuid
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
import respx
from aegis.connectors.gem import BASE, GemClient, GemError
from aegis.services import research_topics, tender_watch
from aegis.services.tender_watch import TenderConfig, tender_item

TODAY = date(2026, 9, 25)


def _doc(b_id="9001", closes="2026-10-06T17:00:00Z", **kw):
    # GeM's index returns most fields as one-element lists.
    return {
        "b_id": [b_id], "b_bid_number": [f"GEM/2026/B/{b_id}"], "b_category_name": ["Custom Bid for MIS Software"],
        "ba_official_details_minName": ["Ministry of Railways"], "final_end_date_sort": [closes],
        "b_total_quantity": [1], **kw,
    }


def test_a_bid_becomes_an_item_keyed_on_its_document():
    item = tender_item(_doc(), "MIS", TODAY, 3)
    assert item == {
        "title": "GeM bid: Custom Bid for MIS Software — Ministry of Railways (closes 06 Oct)",
        "url": "https://bidplus.gem.gov.in/showbidDocument/9001",
        "summary": "GEM/2026/B/9001 · qty 1 · matched 'MIS'",
    }


def test_bids_closing_too_soon_or_without_an_id_are_dropped():
    assert tender_item(_doc(closes="2026-09-27T17:00:00Z"), "k", TODAY, 3) is None
    assert tender_item(_doc(closes="2026-09-28T17:00:00Z"), "k", TODAY, 3) is not None
    assert tender_item(_doc(b_id=""), "k", TODAY, 3) is None
    assert tender_item(_doc(final_end_date_sort=["?"]), "k", TODAY, 3) is None


TERMS = ("software", "digiti", "data", "GIS", "MIS", "IT")


@pytest.mark.parametrize(
    ("title", "kept"),
    [
        # The noise GeM's loose search returned live on 2026-09-25.
        ("EMISSIVITY COATING OF RHF-ROOF-WALLS", False),
        ("Uni-Ball Pen Micro,Pilot Pen V5,Pencil,Plastic Scale", False),
        ("Tentage Service - Floorings, Lighting, Items", False),
        ("Repair and Overhauling Service - cars; REPAIR OF MAHINDRA SCORPIO", False),
        # What it should keep.
        ("Custom Bid for Services - 5 Nos of Stereo Digitization Software", True),
        ("Hiring of GIS Implementation Agency for Survey", True),
        ("Data Analytics Service (Version 2)", True),
        ("Supply of IT equipment", True),
    ],
)
def test_a_bid_title_must_name_it_work(title, kept):
    assert tender_watch.names_a_term(title, TERMS) is kept
    assert tender_watch.names_a_term(title, ()) is True


def test_a_bid_whose_title_names_no_term_is_dropped():
    pens = _doc(b_category_name=["Uni-Ball Pen Micro,Pencil"])
    assert tender_item(pens, "GIS", TODAY, 3, TERMS) is None
    assert tender_item(_doc(), "MIS", TODAY, 3, TERMS) is not None


def test_config_is_lenient_and_caps_a_page():
    cfg = TenderConfig.from_config({"keywords": ["MIS", " "], "per_keyword": 50, "min_days_left": "x"})
    assert (cfg.keywords, cfg.per_keyword, cfg.min_days_left) == (("MIS",), 10, 3)
    assert TenderConfig.from_config(None) == TenderConfig()


PAGE = """<script>data: {'payload': JSON.stringify(postdata), 'csrf_bd_gem_nk': '3563db8b'},</script>"""


@pytest.mark.asyncio
async def test_the_client_reads_the_page_token_once_and_posts_the_search():
    with respx.mock(base_url=BASE) as mock:
        page = mock.get("/all-bids").respond(200, text=PAGE)
        data = mock.post("/all-bids-data").respond(200, json={"code": 200, "response": {"response": {"docs": [_doc()]}}})
        client = GemClient()
        assert (await client.search("MIS"))[0]["b_id"] == ["9001"]
        await client.search("GIS")
        assert page.call_count == 1 and data.call_count == 2
        form = parse_qs(data.calls[0].request.content.decode())
        assert form["csrf_bd_gem_nk"] == ["3563db8b"]
        payload = json.loads(form["payload"][0])
        assert payload["param"]["searchBid"] == "MIS" and payload["filter"]["bidStatusType"] == "ongoing_bids"
        await client.close()

    with respx.mock(base_url=BASE) as mock:
        mock.get("/all-bids").respond(200, text="no token here")
        with pytest.raises(GemError, match="no search token"):
            await GemClient().search("MIS")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/all-bids").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(GemError, match="ConnectError"):
            await GemClient().search("MIS")


class _Client:
    def __init__(self, by_kw):
        self.by_kw = by_kw

    async def search(self, kw):
        if kw == "broken":
            raise GemError("/all-bids-data: HTTP 500")
        return self.by_kw.get(kw, [])


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'connector_health:%'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'connector_health:%'")


@pytest.mark.asyncio
async def test_a_run_files_each_bid_once_and_survives_a_failing_keyword(pool):
    topic = f"Tenders {uuid.uuid4().hex[:6]}"
    await research_topics.track(pool, topic, [topic])
    both = _doc("1")
    client = _Client({"MIS": [both, _doc("2", closes="2026-09-26T00:00:00Z")], "GIS": [both, _doc("3")]})
    cfg = TenderConfig(keywords=("MIS", "broken", "GIS"), topic=topic)

    first = await tender_watch.run(pool, client, cfg, today=TODAY)
    assert (first["items"], first["attached"], first["failed_topics"]) == (2, 2, ["broken"])
    assert (await tender_watch.run(pool, client, cfg, today=TODAY))["attached"] == 0
    health = await pool.fetchval("SELECT value FROM settings WHERE key = 'connector_health:gem'")
    assert health is None  # a partial failure is still a working connector

    dead = await tender_watch.run(pool, client, TenderConfig(keywords=("broken",), topic=topic), today=TODAY)
    assert dead["items"] == 0
    health = await pool.fetchval("SELECT value FROM settings WHERE key = 'connector_health:gem'")
    assert health["consecutive_failures"] == 1
    assert await tender_watch.run(pool, client, TenderConfig(), today=TODAY) == {"skipped": "no_keywords"}
