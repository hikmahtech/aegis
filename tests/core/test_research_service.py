"""The research lane's shared steps (#509): identity, the public-URL guard, the
paper engines and the source list. The chat tools and the worker both call
these, so a behaviour pinned here is pinned for both."""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.services import research as rs

# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_the_workflow_id_ignores_case_spacing_and_a_question_mark():
    a = rs.research_workflow_id("What is  RAG?", "quick", [])
    assert a == rs.research_workflow_id("what is rag", "quick", None)
    assert a.startswith("research-")
    assert rs.research_workflow_id("what is rag", "thorough") != a
    assert rs.research_workflow_id("what is rag", "quick", ["arxiv.org"]) != a
    assert rs.research_workflow_id(
        "what is rag", "quick", ["ArXiv.org ", "arxiv.org"]
    ) == rs.research_workflow_id("what is rag", "quick", ["arxiv.org"])


def test_a_saved_answer_is_keyed_on_the_question_not_the_clock():
    """Asking again replaces the stored answer instead of adding a copy."""
    assert rs.research_content_url("What is RAG?") == rs.research_content_url("what is rag")
    assert rs.research_content_url("x").startswith("aegis://research/")


@pytest.mark.parametrize(
    ("question", "domains", "expected"),
    [
        ("recent papers on sparse attention", [], True),
        ("a survey of RAG evaluation", [], True),
        ("why is my cache slow after a deploy", [], False),
        ("anything at all", ["arxiv.org"], True),
    ],
)
def test_looks_academic(question, domains, expected):
    assert rs.looks_academic(question, domains) is expected


def test_urls_in_reads_a_task_description_in_order():
    text = "[Read](https://news.example/a).\n\nsee https://x.org/b, then https://news.example/a"
    assert rs.urls_in(text) == ["https://news.example/a", "https://x.org/b"]
    assert rs.urls_in("") == []


def test_site_query():
    assert rs.site_query("rag") == "rag"
    assert rs.site_query("rag", "arxiv.org") == "rag site:arxiv.org"
    assert rs.site_query("rag", domains=["a.org", "b.org"]) == "rag (site:a.org OR site:b.org)"


# --------------------------------------------------------------------------
# read_url — only the public internet
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://10.20.0.15:5432/",
        "http://[::1]/",
        "http://aegis.internal/",
    ],
)
async def test_read_url_refuses_anything_but_the_public_internet(url):
    out = await rs.read_url(url)
    assert out.get("error")
    assert "text" not in out


async def test_read_url_returns_bounded_text(monkeypatch):
    async def public(url):
        return None

    async def fetch(url, content_type=None, max_chars=0):
        return "abcdef" * 200, "Title"

    monkeypatch.setattr(rs, "public_url_problem", public)
    monkeypatch.setattr(rs, "fetch_and_extract", fetch)
    out = await rs.read_url("https://example.com/a", max_chars=600)
    assert out["title"] == "Title"
    assert len(out["text"]) == 600
    assert out["truncated"] is True


async def test_read_url_reports_a_page_with_no_text(monkeypatch):
    async def public(url):
        return None

    async def fetch(url, content_type=None, max_chars=0):
        return "", None

    monkeypatch.setattr(rs, "public_url_problem", public)
    monkeypatch.setattr(rs, "fetch_and_extract", fetch)
    out = await rs.read_url("https://example.com/spa")
    assert "no readable text" in out["error"]


# --------------------------------------------------------------------------
# paper_search — two engines, one list
# --------------------------------------------------------------------------

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <published>2024-01-03T00:00:00Z</published>
    <title>Sparse
      Attention at Scale</title>
    <summary>  We study sparse attention.  </summary>
    <author><name>A. One</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2201.00001v1</id>
    <published>2022-01-01T00:00:00Z</published>
    <title>An Old Paper</title>
    <summary>Old.</summary>
  </entry>
</feed>"""

_S2 = {
    "data": [
        {
            "paperId": "a" * 40,
            "title": "Sparse Attention at Scale",
            "externalIds": {"ArXiv": "2401.01234"},
            "citationCount": 42,
            "year": 2024,
            "publicationDate": "2024-01-03",
            "abstract": "We study sparse attention.",
            "authors": [{"name": "A. One"}],
            "url": "https://www.semanticscholar.org/paper/aaa",
            "openAccessPdf": None,
        },
        {
            "paperId": "b" * 40,
            "title": "A Journal Paper",
            "externalIds": {},
            "citationCount": 7,
            "year": 2023,
            "publicationDate": "2023-05-01",
            "abstract": "Journal only.",
            "authors": [],
            "url": "https://www.semanticscholar.org/paper/bbb",
            "openAccessPdf": {"url": "https://journal.example/b.pdf"},
        },
    ]
}


@respx.mock
async def test_paper_search_merges_the_engines_and_honours_since():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=_ATOM)
    )
    s2 = respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=_S2)
    )
    out = await rs.paper_search("sparse attention", since="2023")
    ids = [p["id"] for p in out["papers"]]
    # One entry for the paper both engines know; the 2022 paper is filtered.
    assert ids == ["arxiv:2401.01234", "s2:" + "b" * 40]
    assert out["papers"][0]["citations"] == 42
    assert "errors" not in out
    assert s2.calls.last.request.url.params["year"] == "2023-"


@respx.mock
async def test_one_engine_failing_still_returns_the_other():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=_ATOM)
    )
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(429)
    )
    out = await rs.paper_search("sparse attention")
    assert [p["id"] for p in out["papers"]] == ["arxiv:2401.01234", "arxiv:2201.00001"]
    assert out["errors"] == ["semantic_scholar: rate-limited; try again in a minute"]
    assert "error" not in out


@respx.mock
async def test_both_engines_failing_is_an_error():
    respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(503))
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(500)
    )
    out = await rs.paper_search("sparse attention")
    assert out["papers"] == []
    assert "arxiv: HTTP 503" in out["error"]


@pytest.mark.parametrize(("query", "since"), [("", ""), ("rag", "last year"), ("rag", "2024-1")])
async def test_paper_search_refuses_bad_input(query, since):
    assert (await rs.paper_search(query, since=since)).get("error")


# --------------------------------------------------------------------------
# paper_read — an id to a PDF's text
# --------------------------------------------------------------------------


@pytest.fixture
def fetched(monkeypatch):
    seen: list = []

    async def public(url):
        return None

    async def fetch(url, content_type=None, max_chars=0):
        seen.append((url, content_type))
        return "paper text " * 50, None

    monkeypatch.setattr(rs, "public_url_problem", public)
    monkeypatch.setattr(rs, "fetch_and_extract", fetch)
    return seen


@pytest.mark.parametrize("paper_id", ["2401.01234", "arxiv:2401.01234v3"])
async def test_paper_read_takes_an_arxiv_id(fetched, paper_id):
    out = await rs.paper_read(paper_id)
    assert out["url"] == "https://arxiv.org/pdf/2401.01234"
    assert fetched == [("https://arxiv.org/pdf/2401.01234", "pdf")]
    assert out["text"].startswith("paper text")


@respx.mock
async def test_paper_read_resolves_a_semantic_scholar_id(fetched):
    respx.get(f"https://api.semanticscholar.org/graph/v1/paper/{'b' * 40}").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "A Journal Paper",
                "externalIds": {},
                "openAccessPdf": {"url": "https://journal.example/b.pdf"},
            },
        )
    )
    out = await rs.paper_read("s2:" + "b" * 40)
    assert out["title"] == "A Journal Paper"
    assert fetched == [("https://journal.example/b.pdf", "pdf")]


@respx.mock
async def test_paper_read_says_when_there_is_no_open_pdf(fetched):
    respx.get(f"https://api.semanticscholar.org/graph/v1/paper/{'c' * 40}").mock(
        return_value=httpx.Response(200, json={"title": "Closed", "externalIds": {}})
    )
    out = await rs.paper_read("s2:" + "c" * 40)
    assert "no open-access PDF" in out["error"]
    assert fetched == []


async def test_paper_read_refuses_what_it_cannot_resolve(fetched):
    assert (await rs.paper_read("not a paper")).get("error")
    assert (await rs.paper_read("")).get("error")
    assert fetched == []


# --------------------------------------------------------------------------
# sources and the report
# --------------------------------------------------------------------------


def test_sources_put_read_pages_first_and_number_each_url_once():
    sources = rs.build_sources(
        pages=[{"title": "Page", "url": "https://a/1", "text": "body"}],
        papers=[{"title": "Paper", "url": "https://arxiv.org/abs/1", "abstract": "abs"}],
        web=[
            {"title": "Page again", "url": "https://a/1", "snippet": "dup"},
            {"title": "Other", "url": "https://b/2", "snippet": "s"},
        ],
        kg=[{"title": "Stored", "url": "aegis://research/x", "summary": "known"}],
    )
    assert [(s["n"], s["kind"], s["url"]) for s in sources] == [
        (1, "page", "https://a/1"),
        (2, "paper", "https://arxiv.org/abs/1"),
        (3, "web", "https://b/2"),
        (4, "knowledge", "aegis://research/x"),
    ]
    prompt = rs.synthesis_prompt("Q?", "extra context", sources)
    assert "[1] Page <https://a/1> (page)\nbody" in prompt
    assert "aegis://" not in prompt
    assert "extra context" in prompt


def test_the_report_lists_sources_and_hides_internal_urls():
    report = rs.render_report(
        "It works [1].",
        [
            {"n": 1, "kind": "page", "title": "Page", "url": "https://a/1"},
            {"n": 2, "kind": "knowledge", "title": "Stored", "url": "aegis://research/x"},
        ],
    )
    assert report == "It works [1].\n\nSources:\n[1] Page — https://a/1\n[2] Stored"
