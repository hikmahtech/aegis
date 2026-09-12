"""ResearchActivities — the steps of `ResearchFlow` (#509).

Each step calls `aegis.services.research`, the one implementation the chat
tools also use. No step raises for an outside failure: a search engine, an API
or a page that fails is recorded under `errors` and the run carries on with
what it has, so one flaky source never costs the whole answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aegis.services import library, notes
from aegis.services import research as rs
from temporalio import activity


@dataclass
class ResearchActivities:
    knowledge_connector: Any = None
    search_connector: Any = None
    llm_client: Any = None
    # The smart tier, resolved in `__main__` — Raphael's tier. Changing this
    # default does nothing in a real worker.
    model: str = ""
    db_pool: Any = None
    settings: Any = None
    # Stamped on every llm_calls row this lane writes.
    agent_id: str = "raphael"

    @activity.defn
    async def research_gather(self, request: dict) -> dict:
        """What the knowledge store, the web and (for an academic question) the
        paper engines have on the question, and which pages to read."""
        question = str(request.get("question") or "").strip()
        depth = request.get("depth") if request.get("depth") in rs.DEPTHS else "quick"
        domains = rs.clean_domains(request.get("domains"))
        seed = [u for u in (request.get("seed_urls") or []) if isinstance(u, str) and u]
        errors: list[str] = []

        kg: list[dict] = []
        if self.knowledge_connector is not None:
            try:
                hits = await self.knowledge_connector.search(question, limit=5)
                kg = [
                    {
                        "title": str(h.get("title") or ""),
                        "url": str(h.get("url") or ""),
                        "summary": str(h.get("summary") or h.get("content") or "")[:1500],
                    }
                    for h in hits or []
                ]
            except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
                errors.append(f"knowledge: {str(exc)[:200]}")
        kg = await self._notes_first(question, kg, errors)

        books = await self._library(question, errors)

        web: list[dict] = []
        if self.search_connector is None:
            errors.append("web: search is not configured")
        else:
            try:
                web = await rs.web_search(
                    self.search_connector, question, limit=rs.WEB_RESULTS[depth], domains=domains
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"web: {str(exc)[:200]}")

        papers: list[dict] = []
        if rs.looks_academic(question, domains):
            found = await rs.paper_search(question, limit=rs.PAPER_RESULTS[depth])
            papers = list(found.get("papers") or [])
            errors += [f"papers: {e}" for e in found.get("errors") or []]

        to_read: list[str] = []
        for url in [*seed, *(r["url"] for r in web)]:
            if url not in to_read:
                to_read.append(url)
            if len(to_read) >= rs.PAGES_TO_READ[depth]:
                break
        return {
            "kg": kg,
            "books": books,
            "web": web,
            "papers": papers,
            "to_read": to_read,
            "errors": errors,
        }

    async def _notes_first(self, question: str, kg: list[dict], errors: list[str]) -> list[dict]:
        """The user's own notes, ahead of everything else the store has (#514).

        Only with the vault configured: until then nothing is indexed as a
        note, and the search would cost an embedding to find nothing."""
        if self.knowledge_connector is None or not notes.config_from_settings(self.settings).configured:
            return kg
        try:
            hits = await self.knowledge_connector.search(question, limit=3, source_type="note")
        except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
            errors.append(f"notes: {str(exc)[:200]}")
            return kg
        mine = [
            {
                "title": str(h.get("title") or ""),
                "url": str(h.get("url") or ""),
                "summary": str(h.get("content") or h.get("summary") or "")[:1500],
            }
            for h in hits or []
        ]
        seen = {m["url"] for m in mine}
        return mine + [k for k in kg if k["url"] not in seen]

    async def _library(self, question: str, errors: list[str]) -> list[dict]:
        """Books from the Calibre library index that speak to the question, and
        for the closest one — if it is close enough — the passages that match
        (#510). A library that cannot be read is an `errors` line, never a
        failed step."""
        if self.knowledge_connector is None:
            return []
        try:
            hits = await self.knowledge_connector.search(
                question, limit=library.RESEARCH_BOOK_HITS, source_type=library.BOOK_SOURCE_TYPE
            )
        except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
            errors.append(f"library: {str(exc)[:200]}")
            return []
        books = [
            {**b, "url": library.book_url(b["id"])}
            for b in (library.book_hit(h) for h in hits or [])
            if b["id"] is not None
        ]
        if not books or books[0]["similarity"] < library.RESEARCH_PASSAGE_MIN_SIMILARITY:
            return books
        conn, _reason = library.connector_or_reason(self.settings)
        if conn is None:
            return books
        try:
            read = await asyncio.wait_for(
                library.read_book(
                    conn,
                    books[0]["id"],
                    query=question,
                    pdf_scan_pages=library.RESEARCH_PDF_SCAN_PAGES,
                ),
                timeout=library.RESEARCH_LIBRARY_READ_S,
            )
        except Exception as exc:  # noqa: BLE001 — the book's description still counts
            errors.append(f"library: {str(exc)[:200]}")
            return books
        passages = read.get("passages") or []
        if passages:
            books[0]["cite"] = passages[0]["cite"]
            books[0]["passage"] = "\n\n".join(
                f"({p['cite']}) {p['text']}" for p in passages
            )[: library.RESEARCH_PASSAGE_CHARS]
        elif read.get("error"):
            errors.append(f"library: {read['error']}")
        return books

    @activity.defn
    async def research_read(self, urls: list[str]) -> dict:
        """The chosen pages' text, read together. A page that will not read is
        an `errors` line, not a failed step."""
        results = await asyncio.gather(
            *(rs.read_url(u, max_chars=rs.PAGE_CHARS) for u in urls or [])
        )
        pages = [r for r in results if not r.get("error")]
        errors = [f"{str(r.get('url') or '')[:120]}: {r['error']}" for r in results if r.get("error")]
        return {"pages": pages, "errors": errors}

    @activity.defn
    async def research_synthesize(
        self, question: str, context: str, gathered: dict, pages: list[dict]
    ) -> dict:
        """One model call over the numbered sources; the answer cites them.

        `synthesized` is False for every answer that is not the model's own —
        nothing found, no model, a failed call — and the flow saves only a True
        one, so an apology is never stored as research (#508)."""
        sources = rs.build_sources(
            pages,
            gathered.get("papers") or [],
            gathered.get("web") or [],
            gathered.get("kg") or [],
            books=gathered.get("books") or [],
        )
        public = rs.public_sources(sources)
        if not sources:
            return {
                "answer": "I found nothing on this: no search result, paper or stored note spoke to it.",
                "synthesized": False,
                "sources": [],
            }
        if self.llm_client is None:
            return {
                "answer": f"I gathered {len(sources)} sources but no model is configured to read them.",
                "synthesized": False,
                "sources": public,
            }
        answer = ""
        try:
            # db_pool + purpose + agent_id ⇒ think() writes the llm_calls row
            # for every outcome, a failure included.
            result = await self.llm_client.think(
                prompt=rs.synthesis_prompt(question, context, sources),
                model=self.model,
                system_prompt=rs.SYNTHESIS_SYSTEM,
                max_tokens=2500,
                db_pool=self.db_pool,
                purpose="research_synthesis",
                agent_id=self.agent_id,
            )
            answer = str(result.get("response") or "").strip()
        except Exception as exc:  # noqa: BLE001 — the run still answers, saying it failed
            activity.logger.warning("research_synthesis_failed err=%s", str(exc)[:200])
        if not answer:
            return {
                "answer": f"I gathered {len(sources)} sources but the synthesis failed.",
                "synthesized": False,
                "sources": public,
            }
        return {"answer": answer, "synthesized": True, "sources": public}

    @activity.defn
    async def research_save(self, question: str, answer: str, sources: list[dict]) -> dict:
        """Keep a real answer in the knowledge store, keyed on the question so
        asking again replaces it. Raises on a failed save; the flow reports it.

        With the vault configured (#514) the answer is also appended to
        `raphael/questions/<slug>-<hash>.md` — the record, append-only; the
        outcome of that is `vault`, and a vault problem never fails the save."""
        if self.knowledge_connector is None:
            out: dict = {"saved": False, "reason": "no knowledge store"}
        else:
            await self.knowledge_connector.ingest_content(
                url=rs.research_content_url(question),
                title=f"Research: {question}"[:300],
                source_type="research",
                summary=answer[:500],
                raw_text=rs.render_report(answer, sources),
                tags=["research"],
                metadata={"sources": len(sources)},
            )
            out = {"saved": True}
        vault = await self._save_to_vault(question, answer, sources)
        if vault is not None:
            out["vault"] = vault
        return out

    async def _save_to_vault(self, question: str, answer: str, sources: list[dict]) -> dict | None:
        cfg = notes.config_from_settings(self.settings)
        if not cfg.configured:
            return None
        ap = notes.question_append(question, rs.render_report(answer, sources), datetime.now())
        try:
            res = await notes.write(cfg, [ap], "raphael: research answer")
        except notes.NotesError as exc:
            activity.logger.warning("research_vault_save_failed err=%s", str(exc)[:200])
            return {"status": "error", "error": str(exc)[:200]}
        return {"status": res["status"], "path": ap.rel}

    @activity.defn
    async def research_task_problem(self, task_id: str) -> dict:
        """The hub problem behind a `#research` task, minted when it has none —
        what gives the task a timeline and a session registry, as it does a
        `@code` task. Linking never re-tags the task."""
        if self.db_pool is None or not task_id:
            return {}
        from aegis.services.hub_project import ensure_problem_for_task

        # Source `research` makes the problem Raphael's (#513); the default,
        # `session`, is the infra agent's.
        problem = await ensure_problem_for_task(
            self.db_pool, task_id, source="research", settings=self.settings
        )
        return {"problem_id": str(problem["id"])} if problem else {}
