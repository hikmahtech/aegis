"""The owner's record: the `me/` notes, compiled into each agent's `user` document.

Vault record spec §5 (`docs/superpowers/specs/2026-09-22-vault-record-design.md`).
The vault is the record and `agent_personalities(kind='user')` is its cache.
Prompt assembly does not change: chat, the money extraction persona, curiosity's
`_known_text` and the reflection flow keep reading the row. The copy runs one
way. With `record.enabled` off nothing here does anything.

Which notes an agent reads (`notes_for`): the shared ones, then the ones the
`by_tag` map gives its capability tags, then — for the `gtd` holder, the
generalist of #556 — every note no tag claims. A draft is never read. What a
note gives (`render`): its body under a `From <dir>/<name>.md:` line, without
frontmatter, encrypted blocks or `%% … %%` comments, cut at `max_chars` with a
sentence saying so. The row is written through `apply_profile_patch`, so
`agent_profile_revisions` stays the log of what reached a prompt; a shrink is
accepted, because the owner's deletion is the record.

A row is written only when its text differs from the compiled document, and
`apply_profile_patch` stores the text exactly as given, so an unchanged folder
writes nothing: no row, no revision, no overwrite warning.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import structlog

from aegis.agent_tags import GENERALIST_TAG
from aegis.errors import error_text
from aegis.services import notes
from aegis.services import vault_layout as vl
from aegis.services.agents import resolve_tag
from aegis.services.personalities import apply_profile_patch, doc_fingerprint, get_personality
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

SOURCE = "vault_record"
STATE_KEY = "notes_record_state"
_COMMENT_RE = re.compile(r"%%.*?%%", re.S)
_BLANKS_RE = re.compile(r"\n{3,}")


def notes_for(tags: Iterable[str], layout: vl.Layout, present: Iterable[str]) -> list[str]:
    """The note names an agent with `tags` reads, in order, from those `present`."""
    rec = layout.record
    have = set(present)
    tags = list(tags)
    wanted = list(rec.shared)
    for tag in tags:
        wanted += rec.names_for(tag)
    if GENERALIST_TAG in tags:
        wanted += sorted(have - rec.claimed())
    return [n for n in dict.fromkeys(wanted) if n in have]


def clean(text: str) -> str:
    """A note's text as a prompt may see it: no frontmatter, no encrypted
    block, no `%% … %%` comment, no run of blank lines."""
    text = notes.FRONTMATTER_RE.sub("", text or "", count=1)
    text = _COMMENT_RE.sub("", notes.strip_encrypted(text))
    return _BLANKS_RE.sub("\n\n", text).strip()


def render(texts: list[tuple[str, str]], layout: vl.Layout) -> tuple[str, bool]:
    """`(document, whether it was cut)` from `(name, text)` pairs. A note with
    nothing left after `clean` is left out, heading and all."""
    rec = layout.record
    parts = [f"From {rec.note_path(name)}:\n{body}" for name, text in texts if (body := clean(text))]
    doc = "\n\n".join(parts)
    if len(doc) <= rec.max_chars:
        return doc, False
    notice = (
        f"\n\n[Cut at {rec.max_chars} characters. The rest is in {rec.dir}/, "
        "and note_read can open it.]"
    )
    return doc[: max(0, rec.max_chars - len(notice))].rstrip() + notice, True


def _tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [t for t in (raw or []) if isinstance(t, str)]


def document_for(
    tags: Iterable[str], layout: vl.Layout, files: notes.RecordFiles
) -> tuple[str, list[str], bool]:
    """`(document, the notes it came from, whether it was cut)`."""
    names = notes_for(tags, layout, files.notes)
    doc, cut = render([(n, files.notes[n]) for n in names], layout)
    return doc, [layout.record.note_path(n) for n in names], cut


async def is_on(pool: Any) -> bool:
    """Whether the record owns the `user` documents now. A fresh read of the
    row, not the 30s cache: the guards that call it refuse a write."""
    return bool((await vl.get_layout_value(pool))["record"]["enabled"])


async def get_state(pool: Any) -> dict:
    value = await get_setting(pool, STATE_KEY)
    return value if isinstance(value, dict) else {}


async def compile_all(pool: Any, cfg: notes.NotesConfig, layout: vl.Layout) -> dict:
    """Compile every active agent's `user` document from the record folder and
    write the ones whose text changed. Keeps `settings.notes_record_state`:
    the commit, the waiting drafts and, per agent, the fingerprint, size,
    notes, whether it was cut, and the commit and revision of its last write.
    A row whose fingerprint differs from the state was edited by hand: it is
    overwritten and `record_cache_overwritten` is logged."""
    if not layout.record.enabled:
        return {"status": "off"}
    if not cfg.configured:
        return {"status": "not_configured"}
    try:
        files = await asyncio.to_thread(notes.read_record_sync, cfg, layout)
    except notes.NotesError as exc:
        logger.warning("record_unreadable", error=error_text(exc, 200))
        return {"status": "unreadable", "error": error_text(exc, 200)}
    if files.missing:
        logger.warning("record_folder_missing", folder=layout.record.dir)
        return {"status": "no_folder", "folder": layout.record.dir}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    before = (await get_state(pool)).get("agents")
    before = before if isinstance(before, dict) else {}
    agents: dict[str, dict] = {}
    written: list[str] = []
    overwritten: list[str] = []
    for row in await pool.fetch("SELECT id, capabilities FROM agents WHERE active = TRUE ORDER BY id"):
        aid = row["id"]
        doc, rels, cut = document_for(_tags(row["capabilities"]), layout, files)
        fp = doc_fingerprint(doc)
        current = (await get_personality(pool, aid, use_cache=False)).get("user", "") or ""
        entry = dict(before.get(aid) or {})
        if doc_fingerprint(current) != fp:
            if entry.get("fingerprint") and entry["fingerprint"] != doc_fingerprint(current):
                logger.warning("record_cache_overwritten", agent_id=aid, row_chars=len(current))
                overwritten.append(aid)
            res = await apply_profile_patch(pool, aid, "user", doc, source=SOURCE, allow_shrink=True)
            entry.update(commit=files.head, revision_id=res["revision_id"], written_at=now)
            written.append(aid)
        entry.update(fingerprint=fp, chars=len(doc), notes=rels, cut=cut)
        agents[aid] = entry
    await put_setting(
        pool,
        STATE_KEY,
        {
            "commit": files.head,
            "compiled_at": now,
            "folder": layout.record.dir,
            "drafts": [layout.record.draft_path(n) for n in files.drafts],
            "agents": agents,
        },
    )
    return {
        "status": "ok",
        "commit": files.head[:12],
        "written": written,
        "overwritten": overwritten,
        "cut": sorted(a for a, e in agents.items() if e["cut"]),
    }


async def check_switch(pool: Any, cfg: notes.NotesConfig, layout: vl.Layout) -> None:
    """Refuse turning the record on while the `gtd` holder's compiled document
    would be empty and its current row is not (spec §5): the switch must not
    wipe the one real document before its draft is accepted. ValueError with
    the sentence the admin page shows."""
    holder = await resolve_tag(pool, GENERALIST_TAG)
    if not holder:
        return
    current = (await get_personality(pool, holder, use_cache=False)).get("user", "") or ""
    if not current.strip():
        return
    if not cfg.configured:
        raise ValueError(
            f"record.enabled: {holder}'s user document ({len(current)} characters) would be "
            "replaced by nothing, because the vault is not configured"
        )
    try:
        files = await asyncio.to_thread(notes.read_record_sync, cfg, layout)
    except notes.NotesError as exc:
        raise ValueError(f"record.enabled: the vault could not be read ({error_text(exc, 200)})") from exc
    tags = _tags(await pool.fetchval("SELECT capabilities FROM agents WHERE id = $1", holder))
    if not document_for(tags, layout, files)[0]:
        raise ValueError(
            f"record.enabled: {holder}'s user document ({len(current)} characters) would be "
            f"replaced by an empty one: {layout.record.dir}/ holds no accepted note for it yet. "
            f"Accept a draft first: rename {layout.record.dir}/<name>.draft.md to <name>.md."
        )
