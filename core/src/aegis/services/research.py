"""The research lane's shared steps — one implementation for chat and the worker.

Raphael researches in two places: the chat tools in `services/tools/research.py`
(`web_search`, `read_url`, `paper_search`, `paper_read`, and `research_topic`,
which hands the whole job to `ResearchFlow`) and the worker's
`ResearchActivities`, which run that flow's steps. Both call the functions
here, so the two cannot drift — the seam `services/ledger_write.py` is for the
books.

Three rules hold for everything below:

* **An outside failure is an answer, never a raise.** A search engine that is
  down, an API that rate-limits, a page that will not parse: each comes back as
  an `error` the model can read. (`web_search` is the one exception, because
  its two callers report a failed search differently; each catches it.)
* **Nothing here stores anything.** Reading a page or a paper returns its text;
  keeping an answer is `ResearchFlow`'s decision, made once, on a real answer.
* **A fetch only goes to the public internet.** The URL a model asks to read
  can come from a page it just read, so `read_url` and `paper_read` refuse a
  host that resolves to a loopback, private or link-local address — the stack's
  own services included. (A public page that REDIRECTS inward is not caught:
  `fetch_and_extract` follows redirects itself. Its responses go back to the
  model, not to anyone else.)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
import structlog

from aegis.services.content_extract import fetch_and_extract

logger = structlog.get_logger()

# Core never imports worker code, so the flow is started by NAME with a plain
# dict on the queue the worker serves — the same seam as `BooksWriteFlow`.
RESEARCH_FLOW = "ResearchFlow"
TASK_QUEUE = "aegis-main"

# How long `research_topic` waits on the flow before it answers "still
# researching". A quick run is two searches, three page reads and one model
# call — about 20-30s on the measured tiers — so 45s covers it with room, and a
# longer run reports itself to the agent's channel when it finishes.
RESEARCH_WAIT_S = 45
# The chat loop's per-tool cap for `research_topic`: a floor under the wait, so
# a lowered `tool_timeout_seconds` cannot cut it short.
RESEARCH_TOOL_TIMEOUT_S = RESEARCH_WAIT_S + 15
# `read_url` / `paper_read` / `paper_search`: one or two 15-30s fetches plus
# extraction, which the 30s default cannot always fit.
FETCH_TOOL_TIMEOUT_S = 60

DEPTHS = ("quick", "thorough")
# Per run: pages read (the task's own links first), search results, papers.
PAGES_TO_READ = {"quick": 3, "thorough": 6}
WEB_RESULTS = {"quick": 8, "thorough": 15}
PAPER_RESULTS = {"quick": 5, "thorough": 10}
# Characters of one read page that go into the synthesis prompt.
PAGE_CHARS = 6000
# What the two read tools hand back by default, and the most they ever will.
READ_URL_CHARS = 20_000
PAPER_READ_CHARS = 30_000
_MAX_CHARS_CAP = 60_000
# A report posted as a task comment or a chat reply stays readable.
REPORT_CHARS = 8000

_ARXIV_API = "https://export.arxiv.org/api/query"
_S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/{}"
_S2_FIELDS = (
    "title,url,year,publicationDate,abstract,authors,citationCount,externalIds,openAccessPdf"
)
_API_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_ATOM = "{http://www.w3.org/2005/Atom}"

_ARXIV_ID_RE = re.compile(
    r"^(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$", re.I
)
_S2_ID_RE = re.compile(r"^(?:s2:)?([0-9a-f]{40})$", re.I)
_SINCE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
# Words that make a question worth a paper search. Not "research" itself: every
# `#research` task says it, and two API calls per run for nothing is noise.
_ACADEMIC_RE = re.compile(
    r"\b(papers?|arxiv|preprints?|stud(?:y|ies)|survey|benchmarks?|datasets?|"
    r"peer[- ]reviewed|citations?|literature|state of the art|sota)\b",
    re.I,
)

SYNTHESIS_SYSTEM = (
    "You are Raphael, a careful research analyst. Answer the question from the "
    "numbered sources only. Cite every claim with its source number in square "
    "brackets, like [2]. Say plainly what the sources do not settle, and do not "
    "fill the gap from memory. Short paragraphs or bullets; no preamble."
)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def normalise_question(question: str) -> str:
    """The question as it is keyed: case, spacing and a trailing `?` ignored."""
    return re.sub(r"\s+", " ", (question or "").strip().lower()).rstrip(" ?.!")


def clean_domains(domains: Any) -> list[str]:
    if not isinstance(domains, list):
        return []
    return sorted({str(d).strip().lower() for d in domains if str(d or "").strip()})


def research_workflow_id(question: str, depth: str = "quick", domains: Any = None) -> str:
    """One workflow per question, so a retried chat turn re-attaches to the run
    already in flight instead of paying for a second one."""
    key = json.dumps([normalise_question(question), depth, clean_domains(domains)])
    return "research-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def task_workflow_id(task_id: str) -> str:
    return f"research-task-{task_id}"


def research_content_url(question: str) -> str:
    """The knowledge-store key a saved answer lives under. Keyed on the question,
    so asking it again REPLACES the old answer instead of piling up a copy per
    run (the old tool keyed it on the clock)."""
    digest = hashlib.sha256(normalise_question(question).encode()).hexdigest()[:16]
    return f"aegis://research/{digest}"


def looks_academic(question: str, domains: Any = None) -> bool:
    """Is a paper search worth its two API calls for this question?"""
    if any("arxiv" in d or "scholar" in d for d in clean_domains(domains)):
        return True
    return bool(_ACADEMIC_RE.search(question or ""))


def urls_in(text: str, limit: int = 5) -> list[str]:
    """The links written in a task's description, in order, deduplicated.

    Deterministic, so `AgentTaskFlow` can call it inside the workflow."""
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?")
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------


def site_query(query: str, site: str = "", domains: Any = None) -> str:
    sites = [site.strip().lower()] if site and site.strip() else []
    sites += [d for d in clean_domains(domains) if d not in sites]
    if not sites:
        return query
    if len(sites) == 1:
        return f"{query} site:{sites[0]}"
    return f"{query} ({' OR '.join(f'site:{s}' for s in sites)})"


async def web_search(
    search_connector: Any, query: str, *, limit: int = 8, site: str = "", domains: Any = None
) -> list[dict]:
    """Raw SearxNG results as `{title, url, snippet}`. Raises on a failed
    search; the tool and the flow each report that in their own way."""
    results = await search_connector.search(site_query(query, site, domains), limit=limit)
    return [
        {
            "title": str(r.get("title") or ""),
            "url": str(r.get("url") or ""),
            "snippet": str(r.get("content") or "")[:500],
        }
        for r in results
        if r.get("url")
    ]


async def public_url_problem(url: str) -> str | None:
    """None when `url` is http(s) on the public internet, else why it is not."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return "only http and https URLs can be read"
    host = (parsed.hostname or "").lower()
    if not host:
        return "the URL has no host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return f"{host} is not a public host"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "the URL has an invalid port"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        return f"{host} does not resolve ({exc})"
    for info in infos:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return f"{host} resolves to an address that cannot be checked"
        if not addr.is_global:
            return f"{host} resolves to a non-public address"
    return None


def _clamp(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(500, min(n, _MAX_CHARS_CAP))


async def read_url(url: str, *, max_chars: int = READ_URL_CHARS) -> dict:
    """A page's readable text, bounded, never stored."""
    url = (url or "").strip()
    problem = await public_url_problem(url)
    if problem:
        return {"url": url, "error": problem}
    max_chars = _clamp(max_chars, READ_URL_CHARS)
    try:
        text, title = await fetch_and_extract(url, None, max_chars=max_chars + 1)
    except Exception as exc:  # noqa: BLE001 — an unreadable page is an answer
        return {"url": url, "error": f"could not read the page: {str(exc)[:200]}"}
    if not text:
        return {
            "url": url,
            "error": "the page gave no readable text (blocked, empty, an image, or script-only)",
        }
    return {
        "url": url,
        "title": title or "",
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


# --------------------------------------------------------------------------
# Papers
# --------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "rate-limited; try again in a minute"
        return f"HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return str(exc)[:200] or type(exc).__name__


def _title_key(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def parse_arxiv(xml_text: str) -> list[dict]:
    """Papers out of an arXiv API Atom feed."""
    root = ElementTree.fromstring(xml_text)
    papers: list[dict] = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        aid = re.sub(r"v\d+$", "", raw_id.rsplit("/abs/", 1)[-1]) if "/abs/" in raw_id else ""
        if not aid:
            continue  # arXiv reports a malformed query as an entry with no /abs/ id
        published = (entry.findtext(f"{_ATOM}published") or "").strip()[:10]
        papers.append(
            {
                "id": f"arxiv:{aid}",
                "source": "arxiv",
                "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
                "authors": [
                    (a.findtext(f"{_ATOM}name") or "").strip()
                    for a in entry.findall(f"{_ATOM}author")
                ][:6],
                "published": published,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "abstract": " ".join((entry.findtext(f"{_ATOM}summary") or "").split())[:1200],
                "url": f"https://arxiv.org/abs/{aid}",
                "pdf_url": f"https://arxiv.org/pdf/{aid}",
                "citations": None,
            }
        )
    return papers


def parse_semantic_scholar(data: Any) -> list[dict]:
    """Papers out of a Semantic Scholar `/paper/search` response."""
    papers: list[dict] = []
    for p in (data or {}).get("data") or []:
        ext = p.get("externalIds") or {}
        aid = str(ext.get("ArXiv") or "").strip()
        s2id = str(p.get("paperId") or "").strip()
        if not (aid or s2id):
            continue
        pdf = (p.get("openAccessPdf") or {}).get("url") or (
            f"https://arxiv.org/pdf/{aid}" if aid else ""
        )
        papers.append(
            {
                "id": f"arxiv:{aid}" if aid else f"s2:{s2id}",
                "source": "semantic_scholar",
                "title": str(p.get("title") or ""),
                "authors": [str(a.get("name") or "") for a in (p.get("authors") or [])][:6],
                "published": str(p.get("publicationDate") or ""),
                "year": p.get("year"),
                "abstract": str(p.get("abstract") or "")[:1200],
                "url": str(p.get("url") or (f"https://arxiv.org/abs/{aid}" if aid else "")),
                "pdf_url": pdf,
                "citations": p.get("citationCount"),
            }
        )
    return papers


def _published_on_or_after(paper: dict, since: str) -> bool:
    if not since:
        return True
    published = str(paper.get("published") or "")
    if not published and paper.get("year"):
        published = str(paper["year"])
    if not published:
        return True  # an undated paper is kept, not guessed away
    return published[: len(since)] >= since[: len(published)]


def merge_papers(groups: list[list[dict]], limit: int) -> list[dict]:
    """One list, one entry per paper. An arXiv id joins the two engines; a title
    joins a Semantic Scholar record that has no arXiv id. A merged entry keeps
    the first engine's fields and takes the citation count wherever it exists."""
    merged: list[dict] = []
    by_id: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for group in groups:
        for paper in group:
            key = paper["id"]
            tkey = _title_key(paper["title"])
            existing = by_id.get(key) or (by_title.get(tkey) if tkey else None)
            if existing is not None:
                if existing.get("citations") is None and paper.get("citations") is not None:
                    existing["citations"] = paper["citations"]
                for field in ("abstract", "pdf_url", "published"):
                    if not existing.get(field) and paper.get(field):
                        existing[field] = paper[field]
                continue
            entry = dict(paper)
            merged.append(entry)
            by_id[key] = entry
            if tkey:
                by_title[tkey] = entry
    return merged[:limit]


async def _search_arxiv(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    terms = re.findall(r"\w[\w-]*", query)[:8]
    resp = await client.get(
        _ARXIV_API,
        params={
            "search_query": " AND ".join(f"all:{t}" for t in terms) or f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": "relevance",
        },
    )
    resp.raise_for_status()
    return parse_arxiv(resp.text)


async def _search_s2(client: httpx.AsyncClient, query: str, since: str, limit: int) -> list[dict]:
    params: dict[str, Any] = {"query": query, "limit": limit, "fields": _S2_FIELDS}
    if since:
        params["year"] = f"{since[:4]}-"
    resp = await client.get(_S2_SEARCH, params=params)
    resp.raise_for_status()
    return parse_semantic_scholar(resp.json())


async def paper_search(
    query: str, *, since: str = "", limit: int = 8, client: httpx.AsyncClient | None = None
) -> dict:
    """Papers from arXiv and Semantic Scholar together. One engine failing is
    reported under `errors` and the other's papers still come back; only when
    both fail is there a top-level `error`."""
    query = (query or "").strip()
    since = (since or "").strip()
    if not query:
        return {"error": "query is required"}
    if since and not _SINCE_RE.match(since):
        return {"error": "since must be YYYY, YYYY-MM or YYYY-MM-DD"}
    try:
        limit = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit = 8
    own = client is None
    client = client or httpx.AsyncClient(timeout=_API_TIMEOUT, follow_redirects=True)
    try:
        s2, arxiv = await asyncio.gather(
            _search_s2(client, query, since, limit),
            _search_arxiv(client, query, limit),
            return_exceptions=True,
        )
    finally:
        if own:
            await client.aclose()
    errors: list[str] = []
    groups: list[list[dict]] = []
    for name, res in (("semantic_scholar", s2), ("arxiv", arxiv)):
        if isinstance(res, BaseException):
            logger.warning("paper_search_engine_failed", engine=name, error=_describe(res))
            errors.append(f"{name}: {_describe(res)}")
        else:
            groups.append([p for p in res if _published_on_or_after(p, since)])
    out: dict[str, Any] = {"query": query, "papers": merge_papers(groups, limit)}
    if errors:
        out["errors"] = errors
        if not groups:
            out["error"] = "; ".join(errors)
    return out


async def paper_read(
    paper_id: str,
    *,
    max_chars: int = PAPER_READ_CHARS,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """A paper's text from its PDF, bounded, never stored.

    Takes an arXiv id (`2401.01234`, `arxiv:2401.01234`), a Semantic Scholar id
    from `paper_search` (`s2:<40 hex>`), or a PDF URL."""
    pid = (paper_id or "").strip()
    if not pid:
        return {"error": "paper_id is required"}
    title = ""
    arxiv = _ARXIV_ID_RE.match(pid)
    s2 = _S2_ID_RE.match(pid)
    if pid.lower().startswith(("http://", "https://")):
        url = pid
    elif arxiv:
        aid = re.sub(r"v\d+$", "", arxiv.group(1))
        url = f"https://arxiv.org/pdf/{aid}"
    elif s2:
        own = client is None
        client = client or httpx.AsyncClient(timeout=_API_TIMEOUT, follow_redirects=True)
        try:
            resp = await client.get(
                _S2_PAPER.format(s2.group(1)),
                params={"fields": "title,externalIds,openAccessPdf"},
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:  # noqa: BLE001 — a lookup failure is an answer
            return {"id": pid, "error": f"Semantic Scholar lookup failed: {_describe(exc)}"}
        finally:
            if own:
                await client.aclose()
        title = str(data.get("title") or "")
        aid = str((data.get("externalIds") or {}).get("ArXiv") or "")
        url = (data.get("openAccessPdf") or {}).get("url") or (
            f"https://arxiv.org/pdf/{aid}" if aid else ""
        )
        if not url:
            return {"id": pid, "title": title, "error": "no open-access PDF is known for this paper"}
    else:
        return {
            "id": pid,
            "error": "give an arXiv id (2401.01234), an s2:<id> from paper_search, or a PDF URL",
        }
    problem = await public_url_problem(url)
    if problem:
        return {"id": pid, "url": url, "error": problem}
    max_chars = _clamp(max_chars, PAPER_READ_CHARS)
    try:
        text, _title = await fetch_and_extract(url, "pdf", max_chars=max_chars + 1)
    except Exception as exc:  # noqa: BLE001 — a PDF that will not parse is an answer
        return {"id": pid, "url": url, "error": f"could not read the PDF: {str(exc)[:200]}"}
    if not text:
        return {"id": pid, "url": url, "error": "the PDF gave no text (scanned, or blocked)"}
    return {
        "id": pid,
        "title": title,
        "url": url,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------


def build_sources(
    pages: list[dict], papers: list[dict], web: list[dict], kg: list[dict], cap: int = 30
) -> list[dict]:
    """The numbered source list the answer cites. Pages actually read come
    first (the strongest evidence), then papers, search snippets, and what the
    knowledge store already held. One number per URL."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, title: Any, url: Any, text: Any) -> None:
        url = str(url or "")
        title = str(title or "")
        key = url or f"{kind}:{title}"
        if key in seen or len(out) >= cap:
            return
        seen.add(key)
        out.append(
            {
                "n": len(out) + 1,
                "kind": kind,
                "title": title or url or "(untitled)",
                "url": url,
                "text": str(text or ""),
            }
        )

    for p in pages:
        add("page", p.get("title"), p.get("url"), str(p.get("text") or "")[:PAGE_CHARS])
    for p in papers:
        add("paper", p.get("title"), p.get("url"), p.get("abstract"))
    for r in web:
        add("web", r.get("title"), r.get("url"), r.get("snippet"))
    for k in kg:
        add("knowledge", k.get("title"), k.get("url"), str(k.get("summary") or k.get("content") or "")[:1500])
    return out


def synthesis_prompt(question: str, context: str, sources: list[dict]) -> str:
    blocks = "\n\n".join(
        f"[{s['n']}] {s['title']}"
        + (f" <{s['url']}>" if s["url"] and not s["url"].startswith("aegis://") else "")
        + f" ({s['kind']})\n{s['text']}"
        for s in sources
    )
    extra = (
        f"\n\nWHAT THE ASKER ADDED:\n{context.strip()[:4000]}"
        if context and context.strip()
        else ""
    )
    return (
        f"QUESTION: {question}{extra}\n\nSOURCES:\n{blocks}\n\n"
        "Answer the question, citing the sources as [n]."
    )


def public_sources(sources: list[dict]) -> list[dict]:
    """The source list without the text fed to the model."""
    return [{k: s[k] for k in ("n", "kind", "title", "url")} for s in sources]


def render_report(answer: str, sources: list[dict], *, limit_chars: int = REPORT_CHARS) -> str:
    """The answer and its numbered sources, as posted on a task or in chat."""
    lines = [answer.strip()]
    listed = [s for s in sources if s.get("url") or s.get("title")]
    if listed:
        lines += ["", "Sources:"]
        for s in listed[:20]:
            url = s.get("url") or ""
            shown = "" if url.startswith("aegis://") else url
            lines.append(f"[{s['n']}] {s['title']}" + (f" — {shown}" if shown else ""))
    text = "\n".join(lines)
    return text if len(text) <= limit_chars else text[: limit_chars - 2] + " …"
