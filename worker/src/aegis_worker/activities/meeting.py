"""Meeting notes — fetch the note-taker's document, measure the user's part in
it, and review it for their own eyes.

Design: docs/superpowers/specs/2026-09-02-meeting-notes-design.md. The pure
helpers here are what the tests pin; `MeetingActivities` (below) is thin glue
around Gmail, Drive, the LLM and `life.observations`.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aegis.services.meeting_rules import is_self
from temporalio import activity

from aegis_worker.activities.gmail import _build_gmail_service, _extract_text_from_part

_DOC_ID_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")
# `Speaker Name: words`. The label must start with a letter so a `10:30: …`
# timestamp never reads as a speaker, and may not contain a colon.
_SPEAKER_LINE_RE = re.compile(r"^([A-Za-z][^:\n]{1,59}): \S")
# The transcript starts at the first speaker line that opens a window of 5
# non-blank lines with at least 4 speaker lines in it. The window is what stops a
# lone speaker-shaped line such as "Decision: ship it" from opening a transcript.
# A bulleted "* Tip: …" never gets that far: `_SPEAKER_LINE_RE` requires a letter
# first, so the bullet character already disqualifies it.
_TRANSCRIPT_WINDOW = 5
_TRANSCRIPT_MIN_HITS = 4
_SELF_LINES_CAP = 6_000


def extract_doc_id(texts: Iterable[str]) -> str | None:
    """First Google Docs id found in any of `texts` (plain or HTML bodies)."""
    for t in texts:
        m = _DOC_ID_RE.search(t or "")
        if m:
            return m.group(1)
    return None


def split_notes_transcript(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(notes, [(speaker, utterance), …]).

    Notes are everything before the transcript. A non-speaker, non-blank line
    inside the transcript is a wrapped continuation of the previous utterance.
    Not keyed on a "Transcript" heading: the Gemini export mentions that word
    inside the notes tab too. A leading BOM is stripped first: Drive's plain-text
    export starts with one and ``str.strip`` does not remove it, so without this
    the notes begin with an invisible U+FEFF.
    # ponytail: longest-window heuristic; add a vendor-keyed splitter if a
    # second note-taker's layout breaks it.
    """
    lines = (text or "").lstrip("\ufeff").splitlines()
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    start: int | None = None
    for k in range(len(nonblank)):
        window = nonblank[k : k + _TRANSCRIPT_WINDOW]
        if len(window) < _TRANSCRIPT_MIN_HITS:
            break
        hits = sum(1 for i in window if _SPEAKER_LINE_RE.match(lines[i]))
        if hits >= _TRANSCRIPT_MIN_HITS and _SPEAKER_LINE_RE.match(lines[window[0]]):
            start = window[0]
            break
    if start is None:
        return (text or "").strip(), []
    notes = "\n".join(lines[:start]).strip()
    utterances: list[tuple[str, str]] = []
    for ln in lines[start:]:
        if not ln.strip():
            continue
        if _SPEAKER_LINE_RE.match(ln):
            speaker, utterance = ln.split(": ", 1)
            utterances.append((speaker.strip(), utterance.strip()))
        elif utterances:
            speaker, utterance = utterances[-1]
            utterances[-1] = (speaker, f"{utterance} {ln.strip()}")
    return notes, utterances


def speaker_stats(utterances: list[tuple[str, str]], self_names: list[str]) -> dict:
    """Per-speaker turns/words plus the user's share. Deterministic, no LLM."""
    per: dict[str, dict] = {}
    for speaker, utterance in utterances:
        d = per.setdefault(speaker, {"turns": 0, "words": 0, "longest": 0})
        w = len(utterance.split())
        d["turns"] += 1
        d["words"] += w
        d["longest"] = max(d["longest"], w)
    total_words = sum(d["words"] for d in per.values())
    mine = [d for s, d in per.items() if is_self(s, self_names)]
    my_turns = sum(d["turns"] for d in mine)
    my_words = sum(d["words"] for d in mine)
    return {
        "speaker_count": len(per),
        "meeting_words_total": total_words,
        "speakers": {s: {"turns": d["turns"], "words": d["words"]} for s, d in per.items()},
        "self": {
            "matched": bool(mine),
            "turns": my_turns,
            "words": my_words,
            "talk_share_pct": round(100.0 * my_words / total_words, 1) if total_words else 0.0,
            "words_per_turn": round(my_words / my_turns, 1) if my_turns else 0.0,
            "longest_turn_words": max((d["longest"] for d in mine), default=0),
        },
    }


def self_lines(
    utterances: list[tuple[str, str]], self_names: list[str], max_chars: int = _SELF_LINES_CAP
) -> str:
    """The user's own utterances, newest kept when over budget."""
    mine = [u for s, u in utterances if is_self(s, self_names)]
    kept: list[str] = []
    total = 0
    for u in reversed(mine):
        if total + len(u) + 1 > max_chars:
            break
        kept.append(u)
        total += len(u) + 1
    return "\n".join(reversed(kept))


def render_review(doc: dict, review: dict, stats: dict) -> str:
    """Markdown filed as `source_type=meeting_review` — searchable by chat."""
    title = doc.get("title") or "Meeting"
    date = (doc.get("meeting_date") or "")[:10]
    lines = [f"# Meeting review: {title} ({date})" if date else f"# Meeting review: {title}"]
    me = (stats or {}).get("self") or {}
    if me.get("matched"):
        lines.append(
            f"Speakers: {stats.get('speaker_count', '?')} · you: {me['talk_share_pct']}% of words, "
            f"{me['turns']} turns, {me['words_per_turn']} words/turn, "
            f"longest {me['longest_turn_words']} words"
        )
    for heading, key in (
        ("Contributions", "contributions"),
        ("Problems raised", "problems_raised"),
        ("Commitments", "commitments"),
    ):
        lines.append(f"\n## {heading}")
        items = review.get(key) or []
        if items:
            lines.extend(f"- {it}" for it in items)
        else:
            lines.append("- (none)")
    if review.get("verbosity_note"):
        lines.append("\n## On brevity")
        lines.append(str(review["verbosity_note"]))
    return "\n".join(lines).strip()


_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _token_has_drive_scope(token_path: Path) -> bool:
    """Cheap pre-check on the stored token so a missing scope is named, not
    discovered as an opaque 403 a call later."""
    try:
        scopes = json.loads(token_path.read_text()).get("scopes") or []
    except Exception:  # noqa: BLE001 — unreadable token reads as "no scope"
        return False
    return _DRIVE_SCOPE in scopes


def _export_doc(token_path: Path, doc_id: str) -> tuple[str, str, str]:
    """(name, modifiedTime, text/plain export). Blocking; run in a thread.
    Separated so tests can monkeypatch it."""
    from aegis.services.drive import _build_drive_service

    svc = _build_drive_service(token_path)
    meta = svc.files().get(fileId=doc_id, fields="name,modifiedTime").execute()
    data = svc.files().export(fileId=doc_id, mimeType="text/plain").execute()
    text = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data)
    return meta.get("name") or "", meta.get("modifiedTime") or "", text.lstrip("\ufeff")


def _all_text_parts(payload: dict) -> list[str]:
    """Every decoded text/* body in the MIME tree — plain AND html, because
    Gemini puts the doc link only in the HTML part."""
    out: list[str] = []

    def walk(p: dict) -> None:
        data = (p.get("body") or {}).get("data")
        if data and str(p.get("mimeType", "")).startswith("text/"):
            try:
                out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload or {})
    return out


def _iso_from_ms(ms: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_dt.UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return _dt.datetime.now(tz=_dt.UTC).isoformat()


def _classify_export_error(exc: BaseException) -> str:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode()
    if status == 403 and b"insufficient" in content.lower():
        return "no_drive_scope"
    if status in (403, 404):
        return "inaccessible"
    return "fetch_failed"


@dataclass
class MeetingActivities:
    gmail_credentials_file: str
    gmail_token_dir: str
    db_pool: Any = None
    llm_client: Any = None
    model_balanced: str = "gemma4:e2b"
    agent_id: str = "sebas"

    @activity.defn
    async def fetch_meeting_document(self, account_label: str, msg: dict) -> dict:
        """Read the email, follow its Google Docs link with the same account's
        Drive token, split notes from transcript. Never raises: every failure
        becomes a `doc_status` and the body (or snippet) still comes back."""
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"
        message_id = msg.get("id") or ""
        base = {
            "title": (msg.get("subject") or "").strip()[:200],
            "meeting_date": _iso_from_ms(msg.get("internal_date_ms")),
            "doc_id": "",
            "doc_url": "",
            "doc_modified_time": "",
            "notes": "",
            "transcript": [],
            "speakers": [],
            "doc_status": "no_link",
        }

        def _sync() -> dict:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            full = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            payload = full.get("payload") or {}
            parts = _all_text_parts(payload)
            body = (_extract_text_from_part(payload) or (parts[0] if parts else "")).strip()
            out = {**base, "notes": body or (msg.get("snippet") or "").strip()}
            doc_id = extract_doc_id(parts)
            if not doc_id:
                return out
            out["doc_id"] = doc_id
            out["doc_url"] = f"https://docs.google.com/document/d/{doc_id}"
            if not _token_has_drive_scope(token_path):
                out["doc_status"] = "no_drive_scope"
                return out
            try:
                name, mtime, text = _export_doc(token_path, doc_id)
            except Exception as exc:  # noqa: BLE001 — mapped, never raised
                out["doc_status"] = _classify_export_error(exc)
                return out
            notes, utterances = split_notes_transcript(text)
            speakers = sorted({s for s, _ in utterances})
            # `notes` is what gets filed as knowledge_content, and the transcript
            # must never reach it. A doc that opens on a speaker line has empty
            # split notes, so it gets a one-line header — enough to file the row
            # and let analysis proceed. Never fall back to the raw export: that
            # is the whole transcript.
            out.update(
                {
                    "doc_status": "ok",
                    "title": (name or out["title"])[:200],
                    "doc_modified_time": mtime,
                    "notes": notes or (f"Speakers: {', '.join(speakers)}" if utterances else ""),
                    "transcript": utterances,
                    "speakers": speakers,
                }
            )
            return out

        try:
            result = await asyncio.to_thread(_sync)
        except Exception as exc:  # noqa: BLE001 — the Gmail read itself failed
            activity.logger.warning(
                "meeting_fetch_failed msg_id=%s err=%s", message_id, str(exc)[:200]
            )
            return {
                **base,
                "notes": (msg.get("snippet") or "").strip(),
                "doc_status": "fetch_failed",
            }
        if result["doc_status"] != "ok":
            activity.logger.warning(
                "meeting_doc_%s msg_id=%s account=%s",
                result["doc_status"],
                message_id,
                account_label,
            )
        return result
