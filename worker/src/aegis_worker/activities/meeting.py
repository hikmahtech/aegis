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
from itertools import islice
from pathlib import Path
from typing import Any

from aegis.llm import LLMTruncationError, parse_llm_json
from aegis.services.email_rules import get_email_rules
from aegis.services.meeting_rules import get_meeting_rules, is_self
from aegis.services.meeting_rules import merge as merge_meeting_rules
from aegis.services.observations import record_external_observation
from temporalio import activity

from aegis_worker.activities.gmail import _build_gmail_service, _extract_text_from_part

_DOC_ID_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")
# `Speaker Name: words`. The label must start with a letter so a `10:30: …`
# timestamp never reads as a speaker, and may not contain a colon.
_SPEAKER_LINE_RE = re.compile(r"^([A-Za-z][^:\n]{1,59}): \S")
# The transcript starts at the first speaker line whose LABEL is a candidate AND
# where speaker lines go on to dominate. A candidate label either recurs on two or
# more speaker lines anywhere in the document, or looks like a person's name. Every
# real transcript has one or the other, so "found no transcript" is safe to read as
# "there is none" — which is what lets the notes be the whole document in that case.
# A lone "Decision: ship it" in the notes is neither recurring nor name-like and so
# stays in the notes; a bulleted "* Tip: …" never gets that far, because
# `_SPEAKER_LINE_RE` requires a leading letter.
_NAME_WORD_RE = re.compile(r"^[A-Z][A-Za-z'\-.]*$")
# Density: a candidate label alone was not enough. A Gemini notes tab opens with
# the doc's own title ("Data Foundations: Session 4 — …"), which is two capitalised
# words and so name-like, and the whole notes body was swallowed into one
# pseudo-utterance. So a candidate only opens the transcript when at least half of
# the next `_DENSITY_WINDOW` non-blank lines (itself included, rounded up, never
# fewer than 2) are candidate speaker lines. A heading over bullets scores 1 in 6;
# a real transcript scores at worst 3 in 6 — every utterance wrapped over two
# lines, or every pair separated by a timestamp line.
_DENSITY_WINDOW = 6
_TIMESTAMP_LINE_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_SELF_LINES_CAP = 6_000
_PROMPT_NOTES_CAP = 8_000
_MIN_NOTES_FOR_REVIEW = 400
_REVIEW_MAX_TOKENS = 3000  # _reasoning_floor lifts this to 4096 on kimi/qwen
_OBS_METRICS = ("talk_share_pct", "words_per_turn", "turns")
_REVIEW_SYSTEM = """\
You review ONE meeting on behalf of the person named below, for their own eyes only.
Return JSON only, no prose around it:
{"contributions": [...], "problems_raised": [...], "commitments": [...], "verbosity_note": "..."}

- contributions: what THEY added — proposals, decisions they drove, facts they supplied. Max 5, one line each.
- problems_raised: problems, risks or blockers THEY raised. Max 5.
- commitments: things THEY agreed to do, with any dates mentioned. Max 5.
- verbosity_note: one or two concrete sentences on how they could have said the same in fewer words, citing their own lines. Empty string when you were given no transcript lines.
Use only the material provided. Never invent. Empty list when nothing applies."""
# Appended to the prompt on the one retry below. It names the mistake and asks
# for nothing new, so the second attempt is the same question — see the comment
# in `analyse_meeting` for why re-asking is the whole cure.
_REVIEW_RETRY_LINE = (
    "Your previous reply was not valid JSON. Return only the JSON object "
    "described above, with no prose before or after it."
)


def extract_doc_id(texts: Iterable[str]) -> str | None:
    """First Google Docs id found in any of `texts` (plain or HTML bodies)."""
    for t in texts:
        m = _DOC_ID_RE.search(t or "")
        if m:
            return m.group(1)
    return None


def _is_name_like(label: str) -> bool:
    """Two to four capitalised words of letters, apostrophes, hyphens and dots.

    "Ada Lovelace", "A Person" and "Mary-Jane O'Neil" are names; "Decision",
    "Tip" and "Owner" are not, so a one-off notes heading keeps its place in the
    notes."""
    words = label.split()
    return 1 < len(words) < 5 and all(_NAME_WORD_RE.match(w) for w in words)


def _speaker_lines_dominate(
    lines: list[str], labels: list[str | None], start: int, candidates: set[str | None]
) -> bool:
    """Do candidate speaker lines carry at least half the window at `start`?

    The window is the next `_DENSITY_WINDOW` non-blank lines, `start` included,
    and is shorter at the end of the text. Blank lines are skipped rather than
    counted so a double-spaced transcript scores the same as a single-spaced one.
    """
    window = list(islice((i for i in range(start, len(lines)) if lines[i].strip()), _DENSITY_WINDOW))
    needed = max(2, -(-len(window) // 2))
    return sum(1 for i in window if labels[i] in candidates) >= needed


def _transcript_start(
    lines: list[str],
    labels: list[str | None],
    candidates: set[str | None],
    counts: dict[str, int],
) -> int | None:
    """Where the transcript opens, or None when the document has no candidate.

    Density alone was not safe to gate on. A real transcript can fail it — four
    wrapped continuation lines per utterance puts it at 2 of 6, and a speaker at
    the end of the document has a window too short to pass — and rejecting every
    candidate would file the whole transcript as `notes`. That is the one
    direction this lane forbids: `analyse_meeting` would then read a
    transcript-less doc and send other people's words to the LLM as the user's
    own notes. So the choice degrades instead of failing:

    1. the first candidate where speaker lines dominate (the density rule);
    2. else the first candidate whose label RECURS — real speakers come back, a
       notes heading usually appears once;
    3. else the first candidate at all.

    Only a document with no candidate line anywhere stays wholly notes, which is
    still safe: there was no transcript to lose. Steps 2 and 3 can cost us the
    notes above a speaker-shaped heading; that is the deliberate trade.
    """
    hits = [i for i, lab in enumerate(labels) if lab in candidates]
    if not hits:
        return None
    dense = next((i for i in hits if _speaker_lines_dominate(lines, labels, i, candidates)), None)
    if dense is not None:
        return dense
    return next((i for i in hits if counts.get(str(labels[i]), 0) >= 2), hits[0])


def split_notes_transcript(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(notes, [(speaker, utterance), …]).

    Notes are everything before the transcript. A candidate line — speaker-shaped,
    with a label that recurs or looks like a name — opens it, preferring one where
    such lines dominate what follows; `_transcript_start` holds the full rule and
    the reason it degrades rather than rejecting every candidate. Inside the
    transcript a candidate line opens an utterance when its label is a
    transcript SPEAKER: it labels two or more lines below `start`, or it labels
    no line in the notes above. The line at `start` is tested like every other
    one — a note-taker that opens its transcript tab by reprinting the doc title
    puts a heading exactly there, and density selects it because the real
    speakers follow. A candidate that fails the test is a notes heading
    reprinted inside the transcript, and it is dropped along with any wrapped
    lines that follow it, because its words are not speech and folding them
    would put a heading in the previous speaker's mouth. A bare timestamp line
    is dropped and any other non-speaker, non-blank line is a wrapped
    continuation of the previous utterance. Not keyed on a "Transcript"
    heading: the Gemini export mentions that word inside the notes tab too. A
    leading BOM is stripped first: Drive's plain-text export starts with one and
    ``str.strip`` does not remove it, so without this the notes begin with an
    invisible U+FEFF.
    # ponytail: label heuristic plus a density count, no vendor knowledge. Its
    # ceiling is a notes heading that reads as a speaker and either sits within
    # two non-speaker lines of the transcript (dense enough to win outright) or is
    # the only candidate left once density has failed everywhere. Either way it
    # costs notes, never the transcript. The speaker rule has a ceiling of its
    # own, and this one costs speech: a participant who speaks exactly once and
    # whose label also heads a line up in the notes loses that turn's text
    # entirely — it is read as a reprinted heading and dropped, with its wrapped
    # lines. That ceiling is uniform, the first line of the transcript included.
    # Exempting that line instead was tried and cost more than it saved: a
    # note-taker reprints the doc title there, so every export of that shape
    # gained a speaker who was never in the room. Position is not evidence;
    # absence from the notes is what protects a real opening turn, and a second
    # turn anywhere below `start` makes the label a speaker outright. The
    # upgrade path is unchanged: a vendor-keyed splitter chosen from the sending
    # address.
    """
    lines = (text or "").lstrip("\ufeff").splitlines()
    labels: list[str | None] = []
    counts: dict[str, int] = {}
    for ln in lines:
        m = _SPEAKER_LINE_RE.match(ln)
        label = m.group(1).strip() if m else None
        labels.append(label)
        if label:
            counts[label] = counts.get(label, 0) + 1
    candidates: set[str | None] = {
        lab for lab, n in counts.items() if n >= 2 or _is_name_like(lab)
    }
    start = _transcript_start(lines, labels, candidates, counts)
    if start is None:
        return (text or "").strip(), []
    notes = "\n".join(lines[:start]).strip()
    region_counts: dict[str, int] = {}
    for lab in labels[start:]:
        if lab:
            region_counts[lab] = region_counts.get(lab, 0) + 1
    notes_labels = {labels[i] for i in range(start) if labels[i]}
    speakers = {lab for lab, n in region_counts.items() if n >= 2 or lab not in notes_labels}
    utterances: list[tuple[str, str]] = []
    dropping = False  # inside a dropped heading, so its wrapped lines go too
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        label = labels[i]
        if not stripped:
            continue
        if label in candidates and label in speakers:
            utterances.append((str(label), lines[i].split(": ", 1)[1].strip()))
            dropping = False
        elif label in candidates:
            dropping = True  # a notes heading reprinted inside the transcript
        elif _TIMESTAMP_LINE_RE.match(stripped):
            continue
        elif utterances and not dropping:
            speaker, utterance = utterances[-1]
            utterances[-1] = (speaker, f"{utterance} {stripped}")
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

    async def _load_rules(self) -> dict:
        if not self.db_pool:
            return merge_meeting_rules(None)
        try:
            return await get_meeting_rules(self.db_pool)
        except Exception as exc:  # noqa: BLE001 — a config read must not stop the flow
            activity.logger.warning("meeting_rules_read_failed err=%s", str(exc)[:200])
            return merge_meeting_rules(None)

    async def _review_once(self, prompt: str, agent_id: str) -> Any:
        """One review completion, parsed. Anything but a dict is unusable."""
        raw = await self.llm_client.think(
            prompt=prompt,
            model=self.model_balanced,
            system_prompt=_REVIEW_SYSTEM,
            max_tokens=_REVIEW_MAX_TOKENS,
            db_pool=self.db_pool,
            purpose="meeting_review",
            agent_id=agent_id or self.agent_id,
        )
        return parse_llm_json((raw.get("response") or "").strip())

    @activity.defn
    async def analyse_meeting(self, doc: dict, agent_id: str = "") -> dict:
        """Stats in code, numbers to life.observations, one LLM review from the
        user's own lines — retried ONCE when the reply does not parse (#363),
        and never otherwise. A skipped analysis is a normal outcome — the notes
        are already filed by the time this runs."""
        rules = await self._load_rules()
        self_names = rules["self_names"]
        if not self_names:
            return {"skipped": "no_self_names", "stats": {}, "observations": 0}

        utterances = [(str(u[0]), str(u[1])) for u in (doc.get("transcript") or []) if len(u) == 2]
        stats = speaker_stats(utterances, self_names) if utterances else {}
        matched = bool(stats and stats["self"]["matched"])
        # A transcript that names nobody we recognise is a configuration error
        # ("Arshad A." vs "Arshad Ansari"), and reviewing it anyway files a
        # confident account of somebody else's meeting under the user's name.
        # The transcript-less case below is different: nothing to misattribute.
        if utterances and not matched:
            return {"skipped": "self_not_matched", "stats": stats, "observations": 0}
        observations = await self._record_observations(doc, stats) if matched else 0

        notes = (doc.get("notes") or "")[:_PROMPT_NOTES_CAP]
        mine = self_lines(utterances, self_names) if matched else ""
        if len(notes) < _MIN_NOTES_FOR_REVIEW and not mine:
            return {"skipped": "too_thin", "stats": stats, "observations": observations}
        if not self.llm_client:
            return {"skipped": "no_llm", "stats": stats, "observations": observations}

        me = stats.get("self") or {}
        prompt_parts = [
            f"Person: {', '.join(self_names)}",
            f"Meeting: {doc.get('title') or ''} ({(doc.get('meeting_date') or '')[:10]})",
        ]
        if matched:
            prompt_parts.append(
                f"Their speaking stats: {me['turns']} turns, {me['words']} words "
                f"({me['talk_share_pct']}% of all words), {me['words_per_turn']} words per turn, "
                f"longest turn {me['longest_turn_words']} words."
            )
            prompt_parts.append(f"Their own lines, in order:\n{mine}")
        prompt_parts.append(f"Meeting notes:\n{notes}")
        prompt = "\n\n".join(prompt_parts)
        try:
            parsed = await self._review_once(prompt, agent_id)
            # A reply that does not parse is a stochastic tail, not a broken
            # prompt or a tight budget: prod saw this call SUCCEED on the Sep 3
            # standup — 683 output tokens, `llm_calls.status='success'`, no
            # error — while the body failed to parse, and an unchanged re-run
            # come back clean at 334 tokens. Roughly one call in fifty. So the
            # cure is another roll of the dice with the mistake named, and
            # nothing about the question changes.
            #
            # Capped at ONE retry — two upstream calls, the retry never retries
            # — for the same reason as the truncation re-roll in
            # `LLMClient.think` (#321): a tail this thin is cleared by one
            # re-roll, and a second failure is evidence about this transcript,
            # not more dice worth buying. Both attempts bill and both are
            # recorded, because `think()` writes an `llm_calls` row per call.
            # `LLMTruncationError` deliberately never reaches here: by the time
            # `think()` raises it, it has already spent its own internal
            # re-roll, so a retry on top would be a third upstream call.
            if not isinstance(parsed, dict):
                activity.logger.warning(
                    "meeting_review_unparseable_retrying doc_id=%s", doc.get("doc_id")
                )
                parsed = await self._review_once(f"{prompt}\n\n{_REVIEW_RETRY_LINE}", agent_id)
            if not isinstance(parsed, dict):
                raise ValueError("unparseable meeting review")
        except LLMTruncationError as exc:
            activity.logger.warning("meeting_review_truncated: %s", str(exc)[:200])
            return {"skipped": "llm_failed", "stats": stats, "observations": observations}
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("meeting_review_llm_failed: %s", str(exc)[:200])
            return {"skipped": "llm_failed", "stats": stats, "observations": observations}

        review = {
            "contributions": _str_list(parsed.get("contributions")),
            "problems_raised": _str_list(parsed.get("problems_raised")),
            "commitments": _str_list(parsed.get("commitments")),
            "verbosity_note": str(parsed.get("verbosity_note") or "").strip()[:600],
        }
        return {
            "stats": stats,
            "observations": observations,
            "self_matched": matched,
            "review": review,
            "rendered": render_review(doc, review, stats),
        }

    @activity.defn
    async def record_analysis_outcome(self, content_id: str, outcome: str) -> dict:
        """Stamp the analysis verdict onto the already-stored `meeting` row.

        The row is filed BEFORE the analysis runs — deliberately, so the notes
        survive a review that never happens — which is why the outcome is
        written back with a targeted metadata update instead of a re-ingest: a
        re-ingest would re-embed the whole document to store one string. The
        weekly review reads it (`gather_meeting_week`), keyed on the meeting
        date, so a backfill cannot make an old skip "this week".

        Best-effort in every direction. MeetingNotesFlow fires this and moves
        on, so no pool, a blank id, an unknown id or a dead connection is a
        warning and a False — never an exception, and never the flow's result.
        """
        if not self.db_pool or not content_id:
            activity.logger.warning(
                "meeting_outcome_skipped content_id=%r outcome=%s", content_id, outcome
            )
            return {"recorded": False}
        try:
            async with self.db_pool.acquire() as conn:
                updated = await conn.fetchval(
                    "UPDATE knowledge_content "
                    "SET metadata = metadata || jsonb_build_object('analysis', $2::text) "
                    "WHERE content_id = $1 RETURNING content_id",
                    content_id,
                    outcome,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, see the docstring
            activity.logger.warning(
                "meeting_outcome_failed content_id=%s err=%s", content_id, str(exc)[:200]
            )
            return {"recorded": False}
        if updated is None:
            activity.logger.warning("meeting_outcome_no_row content_id=%s", content_id)
        return {"recorded": updated is not None}

    @activity.defn
    async def meeting_sender_addresses(self) -> list[str]:
        """Whose mail MeetingSweepFlow sweeps — derived, never configured twice.

        The `meeting` tag on a `sender_overrides` rule is already what makes the
        hourly path fan out to MeetingNotesFlow. Reading the same rules here
        keeps one switch for both paths: adding a note-taker stays a single rule
        on the Email triage page, no vendor name reaches this code, and an
        install where nothing carries the tag sweeps nobody.

        A domain key loses its leading `@` — Gmail's `from:` term wants
        `example.com`, not `@example.com`. Never raises: a config read must not
        be what stops the sweep, and an empty list is a clean no-op.
        """
        if not self.db_pool:
            return []
        try:
            rules = await get_email_rules(self.db_pool)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            activity.logger.warning("meeting_senders_load_failed err=%s", str(exc)[:200])
            return []
        addrs = {
            key.lstrip("@")
            for key, rule in (rules.get("sender_overrides") or {}).items()
            if "meeting" in ((rule or {}).get("tags") or []) and key.lstrip("@")
        }
        return sorted(addrs)

    @activity.defn
    async def unstored_meeting_messages(self, message_ids: list[str]) -> list[str]:
        """Of `message_ids`, the ones with no `meeting` row — in input order.

        One `= ANY($1)` over the ids, never a query per message. Only
        `source_type='meeting'` counts: the `meeting_review` row filed under the
        same message is the review, not the notes, and the hourly path's `email`
        copy is not the notes either.

        Fails CLOSED — no pool or a dead one returns `[]`, not the whole input.
        "I cannot tell what is already filed" must never become "file all of
        it": that would re-ingest and re-review every meeting in the window on
        every run. Losing one cycle is the cheaper failure, and the next run
        recovers it.
        """
        ids: list[str] = []
        for raw in message_ids or []:
            mid = str(raw).strip()
            if mid and mid not in ids:
                ids.append(mid)
        if not ids:
            return []
        if not self.db_pool:
            activity.logger.warning("meeting_unstored_no_pool count=%d", len(ids))
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT metadata->>'message_id' AS message_id FROM knowledge_content "
                    "WHERE source_type = 'meeting' AND metadata->>'message_id' = ANY($1::text[])",
                    ids,
                )
        except Exception as exc:  # noqa: BLE001 — see the docstring
            activity.logger.warning("meeting_unstored_failed err=%s", str(exc)[:200])
            return []
        stored = {r["message_id"] for r in rows}
        return [mid for mid in ids if mid not in stored]

    async def _record_observations(self, doc: dict, stats: dict) -> int:
        """One row per metric, deduped on (source, metric, external_id).
        Returns how many rows were NEW; None from the writer means seen before."""
        ext_id = str(doc.get("doc_id") or doc.get("message_id") or "")
        if not self.db_pool or not ext_id:
            return 0
        observed_at = _parse_iso(doc.get("meeting_date"))
        written = 0
        for metric in _OBS_METRICS:
            try:
                row = await record_external_observation(
                    self.db_pool,
                    source="meeting",
                    metric=metric,
                    external_id=ext_id,
                    value=stats["self"][metric],
                    observed_at=observed_at,
                    metadata={
                        "title": doc.get("title") or "",
                        "speaker_count": stats["speaker_count"],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning(
                    "meeting_observation_failed metric=%s err=%s", metric, str(exc)[:200]
                )
                continue
            if row is not None:
                written += 1
        return written


def _str_list(value: Any, cap: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:cap]


def _parse_iso(value: Any) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
