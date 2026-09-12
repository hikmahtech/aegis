"""The vault writes a chat tool asks for — in one place (#514).

`services/tools/notes.py` validates `note_write` / `note_link` and hands the
write here through `NotesWriteFlow`; `NotesActivities.notes_write` runs the same
call on the worker. Both go through :func:`perform_write`, so there is exactly
one implementation of each write — the seam `ledger_write.py` gives the books
(#388), for the same reason: a vault write is flock → clone-or-pull → append →
commit → push, possibly twice, and the chat loop cannot cancel the thread it
runs in.

Three rules, as for the books:

* **Normalise once, then hash.** The tool normalises the payload (path, the
  heading a dated section gets) BEFORE it names the workflow, and the writer
  builds the append from that same payload. So the workflow id, the marker in
  the note and the write itself all key on the same content, and a retried turn
  re-attaches instead of writing twice.
* **A refusal is a returned sentence, not a raise** — `perform_write` turns
  every `NotesError` into `error: …`.
* **Only under `raphael/`.** `notes.check_path` enforces it again in the writer.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from aegis.services import books, notes

# The writer's worst case: clone (first write only) + two attempts of pull,
# commit and push. The activity's budget, not a chat tool's.
NOTES_WRITE_TIMEOUT_S = books.CLONE_TIMEOUT_S + 2 * (120 + 60 + 120)

NOTE_TEXT_MAX = 20_000
HEADING_MAX = 120
LINK_TARGET_MAX = 300
OPS = ("write", "link")

_URL_RE = re.compile(r"^https?://\S+$")


def normalise_path(path: str) -> str:
    """`topics/rag` → `raphael/topics/rag.md`. The model may leave off the
    folder or the extension; nothing else is rewritten, so a path that tries to
    leave `raphael/` is still refused by `notes.check_path`."""
    rel = (path or "").strip().strip("/")
    if rel and not rel.startswith(f"{notes.RAPHAEL_DIR}/"):
        rel = f"{notes.RAPHAEL_DIR}/{rel}"
    if rel and not rel.endswith(".md"):
        rel += ".md"
    return rel


def normalise(op: str, payload: dict, now: datetime | None = None) -> tuple[dict, str | None]:
    """The payload the write will be built from, or a problem sentence."""
    if op not in OPS:
        return payload, f"unknown notes write {op!r}"
    path = normalise_path(str(payload.get("path") or ""))
    try:
        notes.check_path(path)
    except notes.NotesPathError as exc:
        return payload, str(exc)
    if op == "write":
        text = str(payload.get("text") or "").strip()
        if not text:
            return payload, "text is required"
        if len(text) > NOTE_TEXT_MAX:
            return payload, f"text is longer than {NOTE_TEXT_MAX} characters"
        heading = " ".join(str(payload.get("heading") or "").split())[:HEADING_MAX]
        if not heading:
            heading = (now or datetime.now()).strftime("%Y-%m-%d")
        title = " ".join(str(payload.get("title") or "").split())[:200]
        return {"path": path, "text": text, "heading": heading, "title": title}, None
    target = " ".join(str(payload.get("target") or "").split())
    if not target:
        return payload, "target is required"
    if len(target) > LINK_TARGET_MAX or "[[" in target or "]]" in target:
        return payload, "target must be a note name, a book, a paper or a URL, on one line"
    label = " ".join(str(payload.get("label") or "").split())[:120]
    return {"path": path, "target": target, "label": label}, None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _content_digest(op: str, payload: dict) -> str:
    if op == "write":
        return _digest(f"{payload['path']}\n{payload['heading']}\n{payload['text']}")
    return _digest(f"{payload['path']}\n{payload['target']}")


def write_workflow_id(op: str, payload: dict) -> str:
    """Derived from the normalised payload — the same content the note's
    marker keys on — never from a uuid or a clock."""
    return f"notes-write-{op}-{_content_digest(op, payload)}"


def build_append(op: str, payload: dict) -> notes.Append:
    digest = _content_digest(op, payload)
    if op == "write":
        return notes.Append(
            rel=payload["path"],
            key=f"write:{digest}",
            body=payload["text"],
            heading=payload["heading"],
            title=payload.get("title") or "",
        )
    target, label = payload["target"], payload.get("label") or ""
    if _URL_RE.match(target):
        line = f"- [{label or target}]({target})"
    else:
        line = f"- [[{target}|{label}]]" if label else f"- [[{target}]]"
    return notes.Append(rel=payload["path"], key=f"link:{digest}", body=line)


async def perform_write(op: str, payload: dict, cfg: notes.NotesConfig) -> dict:
    """Run one vault write. `{"ok": bool, "message": str}`; never raises for a
    notes-level refusal."""
    if op not in OPS:
        return {"ok": False, "message": f"error: unknown notes write {op!r}"}
    try:
        ap = build_append(op, payload)
        res = await notes.write(cfg, [ap], f"raphael: {op} {payload['path']}")
    except notes.NotesDisabled:
        return {
            "ok": False,
            "message": "error: the vault is not configured (notes_repo_url and "
            "notes_deploy_key on the Integrations page). Nothing was written.",
        }
    except (notes.NotesError, KeyError) as exc:
        return {"ok": False, "message": f"error: {exc}"}
    if res.get("status") == "exists":
        return {"ok": True, "message": f"already in {ap.rel}; nothing was written twice"}
    return {"ok": True, "message": f"wrote to {ap.rel}"}


def describe_result(result: Any) -> str:
    message = result.get("message") if isinstance(result, dict) else None
    return str(message) if message else "the vault write reported nothing"
