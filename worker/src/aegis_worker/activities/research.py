"""ResearchActivities — the steps of `ResearchFlow` (#509).

Each step calls `aegis.services.research`, the one implementation the chat
tools also use. No step raises for an outside failure: a search engine, an API
or a page that fails is recorded under `errors` and the run carries on with
what it has, so one flaky source never costs the whole answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aegis.services import library, library_config, notes, research_config, topics_config
from aegis.services import research as rs
from aegis.services.user_time import user_now
from aegis.services.vault_layout import get_layout
from temporalio import activity


@dataclass
class ResearchActivities:
    knowledge_connector: Any = None
    search_connector: Any = None
    llm_client: Any = None
    # The smart tier, resolved in `__main__` — the research agent's tier.
    # Changing this default does nothing in a real worker.
    model: str = ""
    db_pool: Any = None
    settings: Any = None
    # Stamped on every llm_calls row this lane writes: the agent holding the
    # `research` tag, resolved in `__main__` at boot. "" = no such agent; the
    # rows are then written with no agent (NULL), never a made-up id.
    agent_id: str = ""

    async def _config(self) -> dict:
        return await research_config.get_research_config(self.db_pool)

    async def _agent_name(self) -> str:
        """The owning agent's `agents.name`, for the synthesis prompt; "" when
        there is no agent or the lookup fails."""
        if not self.agent_id or self.db_pool is None:
            return ""
        try:
            return str(
                await self.db_pool.fetchval("SELECT name FROM agents WHERE id = $1", self.agent_id)
                or ""
            )
        except Exception as exc:  # noqa: BLE001 — a name is a nicety
            activity.logger.warning("research_agent_name_failed err=%s", str(exc)[:200])
            return ""

    @activity.defn
    async def research_gather(self, request: dict) -> dict:
        """What the knowledge store, the web and (for an academic question) the
        paper engines have on the question, and which pages to read."""
        question = str(request.get("question") or "").strip()
        depth = request.get("depth") if request.get("depth") in rs.DEPTHS else "quick"
        domains = rs.clean_domains(request.get("domains"))
        seed = [u for u in (request.get("seed_urls") or []) if isinstance(u, str) and u]
        errors: list[str] = []
        # Every stored document this run hands the model, for the retrieval log.
        used: list[str] = []
        cfg = await self._config()
        limits = rs.depth_limits(cfg, depth)

        kg: list[dict] = []
        if self.knowledge_connector is not None and int(cfg["knowledge_hits"]) > 0:
            try:
                hits = await self.knowledge_connector.search(
                    question, limit=int(cfg["knowledge_hits"])
                )
                kg = [
                    {
                        "title": str(h.get("title") or ""),
                        "url": str(h.get("url") or ""),
                        "summary": str(h.get("summary") or h.get("content") or "")[:1500],
                    }
                    for h in hits or []
                ]
                used += [str(h.get("content_id") or "") for h in hits or []]
            except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
                errors.append(f"knowledge: {str(exc)[:200]}")
        kg = await self._notes_first(question, kg, errors, used, int(cfg["note_hits"]))

        books = await self._library(question, errors, used)

        web: list[dict] = []
        if self.search_connector is None:
            errors.append("web: search is not configured")
        elif int(limits["web_results"]) > 0:
            try:
                web = await rs.web_search(
                    self.search_connector,
                    question,
                    limit=int(limits["web_results"]),
                    domains=domains,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"web: {str(exc)[:200]}")

        papers: list[dict] = []
        if int(limits["papers"]) > 0 and rs.looks_academic(
            question, domains, cfg.get("academic_terms")
        ):
            found = await rs.paper_search(
                question,
                limit=int(limits["papers"]),
                api_key=str(getattr(self.settings, "semantic_scholar_api_key", "") or ""),
            )
            papers = list(found.get("papers") or [])
            errors += [f"papers: {e}" for e in found.get("errors") or []]

        to_read: list[str] = []
        for url in [*seed, *(r["url"] for r in web)]:
            if len(to_read) >= int(limits["pages"]):
                break
            if url not in to_read:
                to_read.append(url)
        await self._log_retrieval(question, used)
        return {
            "kg": kg,
            "books": books,
            "web": web,
            "papers": papers,
            "to_read": to_read,
            "errors": errors,
        }

    async def _log_retrieval(self, question: str, content_ids: list[str]) -> None:
        """Record what this run pulled from the knowledge store, as chat does,
        under `source='research'`.

        `knowledge_injection_log` is what "used in a prompt" is measured from
        (the feed stats, the monthly "drop it?" line, the retention preview).
        Only chat used to write it, so a feed ResearchFlow read every day still
        counted as unused. `workflow_run_id` stays NULL: it references
        `workflow_runs`, whose row may not exist yet; the workflow id rides in
        the payload. Best-effort: a failed log never fails the step."""
        ids = list(dict.fromkeys(c for c in content_ids if c))
        if not ids or self.db_pool is None:
            return
        if not self.agent_id:
            # The log's `agent_id` is NOT NULL: with no research agent there
            # is nobody to file the read under, so the feed stats miss this
            # run's use. Said once per run rather than failing the step.
            activity.logger.warning("research_retrieval_log_skipped reason=no_research_agent")
            return
        try:
            workflow_id = activity.info().workflow_id
        except RuntimeError:
            workflow_id = ""
        try:
            await self.db_pool.execute(
                "INSERT INTO knowledge_injection_log "
                "(agent_id, thread_id, workflow_run_id, source, content_ids, triples_used) "
                "VALUES ($1, NULL, NULL, 'research', $2, $3)",
                self.agent_id,
                ids,
                {"workflow_id": workflow_id, "question": question[:300]},
            )
        except Exception as exc:  # noqa: BLE001 — the log is bookkeeping, not the answer
            activity.logger.warning("research_retrieval_log_failed err=%s", str(exc)[:200])

    async def _notes_first(
        self, question: str, kg: list[dict], errors: list[str], used: list[str], limit: int = 3
    ) -> list[dict]:
        """The user's own notes, ahead of everything else the store has (#514).

        Only with the vault configured: until then nothing is indexed as a
        note, and the search would cost an embedding to find nothing."""
        if (
            self.knowledge_connector is None
            or limit <= 0
            or not notes.config_from_settings(self.settings).configured
        ):
            return kg
        try:
            hits = await self.knowledge_connector.search(question, limit=limit, source_type="note")
        except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
            errors.append(f"notes: {str(exc)[:200]}")
            return kg
        used += [str(h.get("content_id") or "") for h in hits or []]
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

    async def _library(self, question: str, errors: list[str], used: list[str]) -> list[dict]:
        """Books from the Calibre library index that speak to the question, and
        for the closest one — if it is close enough — the passages that match
        (#510). A library that cannot be read is an `errors` line, never a
        failed step."""
        if self.knowledge_connector is None:
            return []
        lim = await library_config.get_library_config(self.db_pool)
        if int(lim["research_book_hits"]) <= 0:
            return []
        try:
            hits = await self.knowledge_connector.search(
                question,
                limit=int(lim["research_book_hits"]),
                source_type=library.BOOK_SOURCE_TYPE,
            )
        except Exception as exc:  # noqa: BLE001 — a slow store costs its part, not the run
            errors.append(f"library: {str(exc)[:200]}")
            return []
        used += [str(h.get("content_id") or "") for h in hits or []]
        books = [
            {**b, "url": library.book_url(b["id"])}
            for b in (library.book_hit(h) for h in hits or [])
            if b["id"] is not None
        ]
        if not books or books[0]["similarity"] < float(lim["research_passage_min_similarity"]):
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
                    pdf_scan_pages=int(lim["research_pdf_scan_pages"]),
                    limits=lim,
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
            )[: int(lim["research_passage_chars"])]
        elif read.get("error"):
            errors.append(f"library: {read['error']}")
        return books

    @activity.defn
    async def research_read(self, urls: list[str]) -> dict:
        """The chosen pages' text, read together. A page that will not read is
        an `errors` line, not a failed step."""
        page_chars = int((await self._config())["page_chars"])
        results = await asyncio.gather(
            *(rs.read_url(u, max_chars=page_chars) for u in urls or [])
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
        cfg = await self._config()
        sources = rs.build_sources(
            pages,
            gathered.get("papers") or [],
            gathered.get("web") or [],
            gathered.get("kg") or [],
            books=gathered.get("books") or [],
            page_chars=int(cfg["page_chars"]),
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
                # The owning agent's name, read at synthesis time, so a
                # renamed agent is what the model is told it is.
                system_prompt=rs.synthesis_system(await self._agent_name()),
                max_tokens=2500,
                db_pool=self.db_pool,
                purpose="research_synthesis",
                agent_id=self.agent_id or None,
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
        return {
            "answer": answer,
            "synthesized": True,
            "sources": public,
            # Rendered here, where the `report_chars` limit can be read; the
            # flow posts this and falls back to its own rendering without it.
            "report": rs.render_report(answer, public, limit_chars=int(cfg["report_chars"])),
        }

    @activity.defn
    async def research_save(self, question: str, answer: str, sources: list[dict]) -> dict:
        """Keep a real answer in the knowledge store, keyed on the question so
        asking again replaces it. Raises on a failed save; the flow reports it.

        With the vault configured (#514) the answer is also appended to
        `raphael/questions/<slug>-<hash>.md` — the record, append-only; the
        outcome of that is `vault`, and a vault problem never fails the save."""
        report_chars = int((await self._config())["report_chars"])
        report = rs.render_report(answer, sources, limit_chars=report_chars)
        if self.knowledge_connector is None:
            out: dict = {"saved": False, "reason": "no knowledge store"}
        else:
            await self.knowledge_connector.ingest_content(
                url=rs.research_content_url(question),
                title=f"Research: {question}"[:300],
                source_type="research",
                summary=answer[:500],
                raw_text=report,
                tags=["research"],
                metadata={"sources": len(sources)},
            )
            out = {"saved": True}
        vault = await self._save_to_vault(question, answer, sources, report=report)
        if vault is not None:
            out["vault"] = vault
        return out

    async def _save_to_vault(
        self, question: str, answer: str, sources: list[dict], *, report: str | None = None
    ) -> dict | None:
        cfg = notes.config_from_settings(self.settings)
        if not cfg.configured:
            return None
        # Dated on the user's calendar, not the container's UTC one; filed
        # where the vault layout puts questions (#567), signed by the owning
        # agent under its name.
        asked = await user_now(self.db_pool)
        layout = await get_layout(self.db_pool)
        author = notes.author_for(self.agent_id, await self._agent_name())
        ap = notes.question_append(
            question, report or rs.render_report(answer, sources), asked, layout
        )
        try:
            res = await notes.write(cfg, [ap], f"{author.prefix}: research answer", author=author)
        except Exception as exc:  # noqa: BLE001 — the store save stands; the vault is the extra
            # Any failure, not only a NotesError: an unexpected one used to
            # escape, fail the activity and report `saved: False` for an answer
            # the knowledge store had already kept.
            activity.logger.warning("research_vault_save_failed err=%s", str(exc)[:200])
            return {"status": "error", "error": str(exc)[:200]}
        return {"status": res["status"], "path": ap.rel}

    @activity.defn
    async def research_task_problem(self, task_id: str) -> dict:
        """The hub problem behind a `#research` task, minted when it has none —
        what gives the task a timeline and a session registry, as it does a
        `@code` task. Linking never re-tags the task.

        For a topic's task (#513) it also says so — `class: topic`, the topic
        and the round's items — because that task's title is "<topic>: new
        items worth a look", which is not a question to research."""
        if self.db_pool is None or not task_id:
            return {}
        from aegis.services import research_topics
        from aegis.services.hub import TOPIC_CLASS
        from aegis.services.hub_project import ensure_problem_for_task

        # Source `research` makes the problem the research agent's (#513); the
        # default, `session`, is the infra agent's.
        problem = await ensure_problem_for_task(
            self.db_pool, task_id, source="research", settings=self.settings
        )
        if not problem:
            return {}
        out: dict = {"problem_id": str(problem["id"]), "class": str(problem.get("class") or "")}
        meta = problem.get("metadata") if isinstance(problem.get("metadata"), dict) else {}
        if out["class"] == TOPIC_CLASS and meta.get("topic"):
            # As many of the round's items as its digest lists
            # (`research_topics_config.digest_items`).
            digest = int((await topics_config.get_topics_config(self.db_pool))["digest_items"])
            items = await research_topics.round_items(
                self.db_pool, out["problem_id"], limit=digest
            )
            out["topic"] = str(meta["topic"])
            out["items"] = [
                {"title": str(i.get("title") or ""), "url": str(i.get("url") or "")} for i in items
            ]
        return out
