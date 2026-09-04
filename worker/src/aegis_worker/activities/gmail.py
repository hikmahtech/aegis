"""Gmail fetch + OAuth helpers. Shared by GmailIngestFlow and ReceiptIngestFlow."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from aegis.llm import parse_llm_json
from aegis.services.email_rules import get_email_rules, match_sender_override
from aegis.services.email_rules import merge as merge_email_rules
from aegis.services.memory import record_gmail_triage_correction
from temporalio import activity
from temporalio.exceptions import ApplicationError

# `_NOTIFICATION_MARKERS` is the one shared definition of "this is a courtesy
# notification", used by clarify and by the classifier's important_action cap so
# the two can't drift into disagreeing about what junk is. clarify does not
# import this module, so the dependency stays one-way.
from aegis_worker.activities.clarify import _NOTIFICATION_MARKERS

logger = structlog.get_logger()


class GmailAuthExpiredError(ApplicationError):
    """Raised when Gmail refresh token is revoked/expired. Non-retryable."""

    def __init__(self, account_label: str, reauth_url: str):
        super().__init__(
            f"gmail_auth_expired:{account_label}",
            account_label,
            reauth_url,
            non_retryable=True,
        )
        self.account_label = account_label
        self.reauth_url = reauth_url


@dataclass
class FetchEmailsInput:
    account_label: str
    query: str
    since_cursor_ts: str | None
    max_results: int = 0  # 0 = no limit; paginate all results


@dataclass
class FetchEmailsResult:
    messages: list[dict] = field(default_factory=list)
    latest_internal_date_ms: int = 0


def _build_gmail_service(creds_file: str, token_path: Path):
    """Build a googleapiclient Gmail service. Separated so tests can monkeypatch."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not token_path.exists():
        raise RefreshError(f"token_missing:{token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as GoogleRequest

        creds.refresh(GoogleRequest())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _parse_headers(payload: dict) -> dict:
    out = {"From": "", "Subject": "", "To": "", "Date": ""}
    for h in payload.get("headers") or []:
        name = h.get("name", "")
        if name in out:
            out[name] = h.get("value", "")
    return out


def _triage_meta(row: Any) -> dict:
    """Decode a `triage_state` row's `metadata` column to a dict (handles the
    legacy double-encoded JSON-string case the jsonb codec can surface)."""
    meta = row["metadata"] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta


# Each receiving Gmail account has user-maintained filters that tag forwarded
# mail with a label like `forwarded/acme`. The suffix after that prefix
# is the lane key; when no such label is present the mail arrived at the
# primary address directly and the lane is "own".
_FORWARDED_LABEL_PREFIX = "forwarded/"
_OWN_LANE = "own"


def _derive_lane(label_names: list[str]) -> str:
    for name in label_names:
        if name.startswith(_FORWARDED_LABEL_PREFIX):
            suffix = name[len(_FORWARDED_LABEL_PREFIX) :].strip()
            if suffix:
                return suffix
    return _OWN_LANE


def _fetch_label_map(svc) -> dict[str, str]:
    """Return {label_id: label_name} for the account. Called once per fetch loop."""
    try:
        resp = svc.users().labels().list(userId="me").execute()
    except Exception:
        return {}
    return {lbl["id"]: lbl.get("name", "") for lbl in resp.get("labels") or []}


def _extract_text_from_part(part: dict) -> str:
    """Recursively extract plain-text content from a Gmail message part."""
    import base64

    mime = part.get("mimeType", "")
    body_data = (part.get("body") or {}).get("data", "")

    if mime == "text/plain" and body_data:
        try:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""
    for subpart in part.get("parts") or []:
        text = _extract_text_from_part(subpart)
        if text:
            return text
    return ""


def _extract_html_from_part(part: dict) -> str:
    """Recursively extract the first text/html part, decoded."""
    import base64

    mime = part.get("mimeType", "")
    body_data = (part.get("body") or {}).get("data", "")
    if mime == "text/html" and body_data:
        try:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""
    for subpart in part.get("parts") or []:
        text = _extract_html_from_part(subpart)
        if text:
            return text
    return ""


_URL_RE = re.compile(r"https?://\S+")
_TAG_BLOCK_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
# Tags that end a line. Everything else is INLINE and is deleted outright
# rather than replaced by a space: HTML mailers wrap parts of a number in
# <b>/<span>, so "Amount:<b>1,00,308</b>.53" must not become
# "Amount: 1,00,308 .53" — the deterministic bank parsers match exact
# phrases like "Rs.<amt> is debited" against this output.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:br|p|div|tr|td|th|table|li|ul|ol|h[1-6]|blockquote"
    r"|section|article|header|footer|hr)\b[^>]*>",
    re.I,
)
# Space, tab, NBSP (U+00A0), then three characters that LOOK LIKE NOTHING in
# this source line and are meant to: zero-width space (U+200B), zero-width
# non-joiner (U+200C) and combining grapheme joiner (U+034F). HTML mailers
# pad layout with them, so they are exactly what arrives in a bank receipt.
# Do not "clean up" the apparently-empty run inside the class — it is the point.
_SPACE_RE = re.compile(r"[ \t\xa0​‌͏]+")
_BLANK_RE = re.compile(r"\n\s*\n+")


def html_to_text(html_src: str) -> str:
    """Reduce an HTML email body to readable text for the money extractor."""
    import html as _html

    text = _TAG_BLOCK_RE.sub(" ", html_src)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    return _clean_text(text)


def _clean_text(text: str) -> str:
    text = _URL_RE.sub("<url>", text)
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n", text)
    return text.strip()


_CLASSIFY_SYSTEM = """\
You are an email triage assistant. Classify the email into exactly one category:

- important_action  — a human is waiting on your reply, or money or a deadline is genuinely at stake and only you can act (an unpaid bill, a failed payment, a contract or filing due, a real person writing to you, a job offer)
- important_read    — worth reading but asks nothing of you (receipts, paid invoices, shipping updates, GitHub notifications, newsletters you actually read)
- informational     — low-value but harmless (automated reports, digests, minor notifications)
- useless           — pure noise with no value (marketing, promotions, spam, unsubscribe bait)

important_action is the ONLY category that interrupts the user, so it is
expensive. Be strict: if nothing would go wrong by reading it tomorrow, it
is not important_action.

NEVER important_action — a courtesy notice about something the user almost
certainly did themselves or cannot act on: sign-in / new-device / new-location
alerts, one-time passcodes and login or verification codes, "incorrect login
attempt", account locked / unlocked / recovered, password-changed
confirmations, connection and friend requests, social relay pings, calendar
reminders for events already accepted. These are important_read at most.

A no-reply sender can still be important_action, but only when it is asking
for money or naming a real deadline.

Additionally, assign zero or more tags from this exact set (lowercase):
  financial, payments, receipt, subscription, security,
  calendar_invite, shipping, travel, health, work, personal,
  newsletter, technology, support

Tags are orthogonal to category — a payment failure is
important_action AND ["financial", "payments"]. A Stripe receipt is
important_read AND ["financial", "payments", "receipt"]. Use [] when
none apply.

Also produce two free-text fields:
- ``reason``: one sentence explaining why you chose this category.
- ``summary``: 2-3 sentences summarising what the email actually says.
  Write it for a reader who hasn't seen the email — capture sender, the
  ask, dates/amounts/links if present, and what action (if any) is
  needed. When the body excerpt is empty, infer from sender + subject.

If the email header includes ``Forwarded from: <lane>`` (e.g. work,
freelance, personal), it arrived in the primary inbox via a forwarding
rule from another mailbox identity. Treat the lane as additional context
(work-vs-personal, which org sent it) when judging importance.

Respond with JSON only: {"category": "<one of the four>", "confidence": <0.0-1.0>, "reason": "<one sentence>", "summary": "<2-3 sentences>", "tags": ["..."]}
"""

_FALLBACK_CATEGORY = "informational"

_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "financial",
        "payments",
        "receipt",
        "subscription",
        "security",
        "calendar_invite",
        "shipping",
        "travel",
        "health",
        "work",
        "personal",
        "newsletter",
        "technology",
        "support",
    }
)


def _parse_tags(raw: Any) -> list[str]:
    """Coerce an LLM-returned tags field into a deduped, order-preserving list of allowed tags."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip().lower()
        if tag in _ALLOWED_TAGS and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


# Data-driven triage cascade (2026-05-30). A sender must be observed at least
# _CACHE_MIN_N times and agree at >= _CACHE_MIN_CONF before the per-sender
# cache is allowed to short-circuit the LLM.
_CACHE_MIN_N = 3
_CACHE_MIN_CONF = 0.75
_GMAIL_PROMO_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}
_TRIAGE_CATEGORIES = {"important_action", "important_read", "informational", "useless"}


def _normalize_sender(raw: str) -> str:
    """Extract a lowercased bare email address from a From header
    ("Name <a@b.com>" -> "a@b.com"); falls back to the raw string."""
    import re

    m = re.search(r"<([^>]+)>", raw or "")
    return (m.group(1) if m else (raw or "")).strip().lower()


def _sender_from_description(description: str | None) -> str:
    """Recover the sender from a captured `#email` task's description.

    `flows/gmail_ingest.py::_route` writes `From: <sender>` as the FIRST line of
    every email capture, which is the only place a `triage_accuracy` row's
    sender survives without a Gmail round-trip (the table stores none). Used by
    the Todoist-disposition feedback below so a "you were wrong" verdict can
    reach `triage_state`.

    ponytail: parses the flow's own output format rather than adding a sender
    column + migration. `test_sender_from_description_matches_capture_format`
    pins the shape; if it ever drifts, the correction is still recorded and only
    the sender relearn is skipped.
    """
    first = (description or "").split("\n", 1)[0].strip()
    if not first.lower().startswith("from:"):
        return ""
    return _normalize_sender(first[len("from:") :].strip())


def cap_notification_category(category: str, subject: str, extra_markers: list[str]) -> str:
    """A courtesy notification can never be `important_action`.

    The classifier's own judgement was not enough: prod cached
    no-reply@accounts.google.com, security-noreply@linkedin.com and seven more
    pure-notification senders as `important_action`, and the sender cache then
    short-circuited the LLM entirely — so a prompt fix alone would not have
    unstuck them, and "Security Alert: Your one-time sign in code is 429718"
    kept becoming a Todoist task. This runs AFTER both the cache and the LLM so
    it catches every path, and the capped verdict is what teaches
    `triage_state`, letting a poisoned sender decay instead of self-reinforcing.

    Deliberately caps at `important_read`, not `useless` — a false positive here
    should cost visibility, never the mail itself.
    """
    if category != "important_action":
        return category
    low = (subject or "").lower()
    if any(marker in low for marker in (*_NOTIFICATION_MARKERS, *extra_markers)):
        return "important_read"
    return category


# Tiers AEGIS treats as "not important" (marks READ, no IMPORTANT label).
_TRIAGE_UNIMPORTANT = {"useless", "informational"}
# Tiers AEGIS treats as "important" (IMPORTANT label + kept unread).
_TRIAGE_IMPORTANT = {"important_action", "important_read"}

# What the "READ" verdict (useless/informational) removes in its single
# Gmail modify call.
#
# (#102) IMPORTANT is in here for measurement integrity, not tidiness. Gmail
# auto-applies IMPORTANT at *delivery* — the same marker this module already
# discounts as a classifier input ("liberal, inflates fake important", see
# classify_email). Until 2026-08 the unimportant verdict removed only UNREAD,
# so Gmail's own delivery-time IMPORTANT survived untouched and
# assess_triage_correction — which reads IMPORTANT ∨ STARRED as "the user
# elevated this" — logged a "user correction" for every auto-IMPORTANT
# marketing mail we correctly called useless, with no human involved. Prod
# bore this out exactly: 75/75 corrections ran unimportant→important and
# ZERO ran the other way, the signature of a one-directional artefact rather
# than a model error, and the subject lines were campaign spam (two of them
# recorded twice, once per duplicate send).
#
# Stripping IMPORTANT here means that at recheck time the label can only be
# present because a human put it back, which is precisely the signal we want.
# Gmail's importance classifier scores a message once, on delivery, and does
# not re-mark an already-delivered message; the ingest fetch only ever sees
# delivered mail (`is:unread` + a forward-moving `after:` cursor), so nothing
# re-adds it behind us. IMPORTANT is user-mutable over the API — the flow
# already writes it in the add direction (`addLabelIds:["IMPORTANT"]` for the
# important_* tiers), and removal is the symmetric operation on the same
# `gmail.modify` scope.
_READ_VERDICT_REMOVES = ("UNREAD", "IMPORTANT")

# (#102) assess_triage_correction speaks in coarse directions; triage_state
# caches one of the four fine-grained _TRIAGE_CATEGORIES. Map a correction
# onto the *conservative* member of its direction: "important" relearns as
# important_read (label + keep unread) rather than important_action, which
# would start manufacturing Todoist tasks off one label; "unimportant"
# relearns as informational rather than useless.
_CORRECTION_TO_CATEGORY = {"important": "important_read", "unimportant": "informational"}

# Closing an AEGIS-created `#email` task with either of these is the user saying
# "this needed nothing from me" — see `_mine_todoist_dispositions`.
_DISPOSITION_NOISE_LABELS = ("#trash", "@reference")

# ...but ONLY when a human put the label there. ClarifyFlow classifies AEGIS's
# own `#email` captures on a 15-min tick and applies exactly these two
# dispositions itself, so a task it clarified carries AEGIS's opinion, not the
# user's. Reading those back scored 39 self-authored verdicts as user
# corrections in prod — 39 of 39 — each one demoting a sender and writing a
# "User corrected email triage" memory no human ever expressed.
_CLARIFY_SELF_DISPOSITIONS = ("trash", "reference")

# How much one disagreement costs a cached sender's confidence. A human verdict
# uses the full step, which flips a 0.6-confidence sender on the first hit. A
# clarify disagreement is a SECOND MACHINE OPINION, not evidence about what the
# user wanted, so it costs half and needs corroboration before it can flip
# anything. Same arithmetic, different weight — see `_triage_upsert`.
_DISAGREEMENT_STEP = 0.3
_MACHINE_DISAGREEMENT_STEP = 0.15


def assess_triage_correction(predicted: str, labels: list[str]) -> str | None:
    """Compare AEGIS's prediction to the email's CURRENT Gmail labels,
    returning the user's correction signal or None.

    The ingest fetch (`is:unread` + forward cursor) never re-observes an
    actioned email, so recheck_triage_outcomes re-reads labels explicitly
    (#74) and sees whatever the user has since done to the email:
      - predicted unimportant (AEGIS marked READ, no IMPORTANT) but the user
        added IMPORTANT or STARRED → mis-triaged → "important".
      - predicted important (AEGIS added IMPORTANT, kept unread) but the user
        removed IMPORTANT/STARRED and read it → mis-triaged → "unimportant".
    Returns None when the current state is consistent with the prediction.
    """
    lset = {str(x).upper() for x in (labels or [])}
    elevated = bool(lset & {"IMPORTANT", "STARRED"})
    if predicted in _TRIAGE_UNIMPORTANT and elevated:
        return "important"
    if predicted in _TRIAGE_IMPORTANT and not elevated and "UNREAD" not in lset:
        return "unimportant"
    return None


@dataclass
class GmailActivities:
    gmail_credentials_file: str
    gmail_token_dir: str
    aegis_ui_url: str = ""
    llm_client: Any = None
    model_balanced: str = "qwen3:14b"
    db_pool: Any = None
    # Owning agent for triage — matches GmailIngestFlow's config default.
    # Threaded into llm_calls rows so gmail_classification stops recording
    # NULL agent_id (see MoneyActivities.agent_id for the same pattern).
    agent_id: str = "sebas"
    # Wired post-construction in worker/__main__ so important emails
    # land in the knowledge graph and become searchable later via
    # Raphael's `search_knowledge` / `ask_knowledge` tools.
    knowledge_connector: Any = None

    @activity.defn
    async def fetch_emails(self, input: FetchEmailsInput) -> FetchEmailsResult:
        """Fetch matching messages. Raises GmailAuthExpiredError on refresh failure."""
        token_path = Path(self.gmail_token_dir) / f"{input.account_label}.json"

        def _sync_fetch() -> FetchEmailsResult:
            from google.auth.exceptions import RefreshError

            try:
                svc = _build_gmail_service(self.gmail_credentials_file, token_path)
                query_parts = [input.query] if input.query else []
                if input.since_cursor_ts:
                    import datetime as _dt

                    ts = _dt.datetime.fromisoformat(input.since_cursor_ts)
                    query_parts.append(f"after:{int(ts.timestamp())}")
                q = " ".join(query_parts)

                # Paginate until exhausted. Gmail API max per page is 500.
                page_size = 500 if input.max_results == 0 else min(input.max_results, 500)
                msg_ids: list[str] = []
                page_token: str | None = None
                while True:
                    kwargs: dict = {"userId": "me", "q": q, "maxResults": page_size}
                    if page_token:
                        kwargs["pageToken"] = page_token
                    page = svc.users().messages().list(**kwargs).execute()
                    msg_ids.extend(m["id"] for m in page.get("messages") or [])
                    page_token = page.get("nextPageToken")
                    if not page_token:
                        break
                    if input.max_results > 0 and len(msg_ids) >= input.max_results:
                        msg_ids = msg_ids[: input.max_results]
                        break

                # Fetch the label map once so each message can resolve its
                # labelIds → human-readable names for lane derivation.
                label_map = _fetch_label_map(svc) if msg_ids else {}

                out: list[dict] = []
                latest_ms = 0
                for mid in msg_ids:
                    full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
                    headers = _parse_headers(full.get("payload") or {})
                    idate = int(full.get("internalDate") or 0)
                    latest_ms = max(latest_ms, idate)
                    label_names = [
                        name for lid in full.get("labelIds") or [] if (name := label_map.get(lid))
                    ]
                    out.append(
                        {
                            "id": mid,
                            "thread_id": full.get("threadId", ""),
                            "sender": headers["From"],
                            "subject": headers["Subject"],
                            "to": headers["To"],
                            "date": headers["Date"],
                            "snippet": (full.get("snippet") or "")[:500],
                            "internal_date_ms": idate,
                            "labels": label_names,
                            "lane": _derive_lane(label_names),
                        }
                    )
                return FetchEmailsResult(messages=out, latest_internal_date_ms=latest_ms)
            except RefreshError as exc:
                reauth_url = (
                    f"{self.aegis_ui_url.rstrip('/')}"
                    f"/api/admin/gmail/reauth/{input.account_label}/initiate"
                )
                raise GmailAuthExpiredError(input.account_label, reauth_url) from exc

        return await asyncio.to_thread(_sync_fetch)

    @activity.defn
    async def fetch_thread(
        self, account_label: str, thread_id: str, max_chars: int = 2000
    ) -> str:
        """Return the plain-text body of the most recent messages in a thread.

        `max_chars` defaults to the classifier's prompt budget. Raise it only for
        a reader that needs the whole message: machine-generated mail front-loads
        boilerplate, so the part that carries the meaning can sit far past 2000
        chars. Jira is the worked example — its notification opens with two ~200
        char tracking URLs, and the field table that says `Resolution : Done`
        lands around char 2400 of a body that reaches 15k. At the old fixed
        600-per-message cap, an `email_task_links` rule matching on that table
        saw nothing but the URLs and could never fire.
        """
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"
        per_message = max(600, max_chars // 3)

        def _sync() -> str:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
            parts: list[str] = []
            for msg in (thread.get("messages") or [])[-5:]:  # last 5 messages max
                text = _extract_text_from_part(msg.get("payload") or {})
                if text.strip():
                    parts.append(text[:per_message])
            return "\n---\n".join(parts)[:max_chars]

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:
            activity.logger.warning("fetch_thread_failed thread=%s: %s", thread_id, str(exc)[:200])
            return ""

    @activity.defn
    async def fetch_message_body(
        self, account_label: str, message_id: str, max_chars: int = 6000
    ) -> str:
        """Full text of one message for the money extractor (spec §2 step 2).

        text/plain part first, else text/html reduced to text. Best-effort:
        any failure returns "" so the caller falls back to the snippet.
        """
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> str:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            full = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            payload = full.get("payload") or {}
            text = _clean_text(_extract_text_from_part(payload))
            if not text:
                text = html_to_text(_extract_html_from_part(payload))
            return text[:max_chars]

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:  # noqa: BLE001 — body is an enhancement; never fail the flow
            activity.logger.warning(
                "fetch_message_body_failed account=%s msg=%s err=%s",
                account_label,
                message_id,
                str(exc)[:200],
            )
            return ""

    @activity.defn
    async def is_message_unread(self, account_label: str, message_id: str) -> bool:
        """Re-read the message's CURRENT unread state, right before we interrupt.

        The fetch query is `is:unread`, but classification happens minutes later
        and the user reads mail on their phone in between. Interrupting about
        something they have already seen is the loudest way to be useless, so
        the important_action path re-checks here before creating a task or
        pinging chat.

        Fails OPEN (returns True) on any error: a missed capture loses a real
        action item, while a redundant one costs a single archive.
        """
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> bool:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            m = (
                svc.users()
                .messages()
                .get(userId="me", id=message_id, format="minimal")
                .execute()
            )
            return "UNREAD" in (m.get("labelIds") or [])

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:
            activity.logger.warning(
                "is_message_unread_failed msg_id=%s err=%s — assuming unread",
                message_id,
                str(exc)[:200],
            )
            return True

    @activity.defn
    async def classify_email(self, msg: dict, thread_content: str = "") -> dict:
        """Classify an email via a data-driven cascade — cheapest signal first,
        LLM last. Returns {category, confidence, tags, reason, summary, lane, source}.

        Cascade (2026-05-30):
          0. (O) user `sender_overrides` rule    -> use it, NO LLM (source=override)
          1. (A) confident per-sender cache hit  -> use it, NO LLM (source=cache)
          2. (B) unknown sender + Gmail promo    -> useless, NO LLM (source=gmail_promo)
          3. LLM tie-breaker (fed Gmail's IMPORTANT prior); result teaches the
             sender cache for next time (source=llm)

        Every learned verdict then passes through `cap_notification_category`,
        which is the one thing an override skips: an explicit rule is the user's
        own judgement and outranks the heuristic.

        Falls back to 'informational' if the LLM is unavailable or returns bad JSON.
        thread_content: full thread text from fetch_thread (preferred over snippet).
        """
        lane = msg.get("lane") or _OWN_LANE
        sender = _normalize_sender(msg.get("sender") or "")
        subject = msg.get("subject") or ""
        labels = msg.get("labels") or []
        gmail_promo = any(c in labels for c in _GMAIL_PROMO_LABELS)

        rules = await self._load_email_rules()
        extra_markers = rules["extra_notification_markers"]

        # (O) An explicit user rule wins outright. It deliberately does NOT
        # write triage_state: deleting the rule must stop it applying, not
        # leave its verdict behind as learned sender reputation. Its `tags` are
        # the rule author's, because skipping the LLM means nothing else can
        # produce them — and the money fan-out keys on financial/payments, so
        # without this an override on a biller silently killed its receipt
        # extraction (#263).
        override = match_sender_override(rules["sender_overrides"], sender)
        if override:
            return {
                "category": override["category"],
                "confidence": 1.0,
                "tags": list(override["tags"]),
                "reason": "sender_overrides rule",
                "summary": "",
                "lane": lane,
                "source": "override",
            }

        # (A) Confident sender-reputation cache -> trust it, skip the LLM.
        cached = await self._triage_lookup(sender) if (sender and self.db_pool) else None
        if (
            cached
            and cached["n"] >= _CACHE_MIN_N
            and cached["confidence"] >= _CACHE_MIN_CONF
            # A row that has never recorded tags cannot answer the fan-out
            # question, and a sender above the threshold never reaches the LLM
            # again — so short-circuiting here would strand it tagless forever.
            # Fall through to the LLM ONCE; the upsert below records the tags
            # and every later message for this sender takes the cache path.
            and cached.get("tags") is not None
        ):
            category = cap_notification_category(cached["category"], subject, extra_markers)
            # (#262) A sender above the threshold never reaches the LLM, and
            # only the LLM path used to re-teach the cache — so a wrong verdict
            # here was permanent. Feeding the CAPPED verdict back lets a
            # repeatedly-capped sender decay out of important_action on its own.
            # Only on disagreement: reinforcing every cache hit would ratchet
            # every sender's n and confidence up merely for sending mail.
            if category != cached["category"]:
                await self._triage_upsert(sender, category)
            return {
                "category": category,
                "confidence": cached["confidence"],
                # Replay the LLM's tags. Returning [] here is what disabled the
                # MoneyProcessFlow fan-out for every cached financial sender.
                "tags": list(cached.get("tags") or []),
                "reason": "",
                "summary": "",
                "lane": lane,
                "source": "cache",
            }

        # (B) Strong Gmail promo signal for a sender we've never seen -> not
        # important, no LLM needed.
        if gmail_promo and cached is None:
            await self._triage_upsert(sender, "useless")
            return {
                "category": "useless",
                "confidence": 0.7,
                "tags": [],
                "reason": "Gmail promotions/social category",
                "summary": "",
                "lane": lane,
                "source": "gmail_promo",
            }

        if not self.llm_client:
            return {
                "category": _FALLBACK_CATEGORY,
                "confidence": 0.5,
                "tags": [],
                "reason": "",
                "summary": "",
                "lane": lane,
                "source": "fallback",
            }

        body = thread_content.strip() if thread_content else (msg.get("snippet") or "")

        # Surface forwarding provenance to the classifier so it can weigh
        # work-vs-personal context (e.g. a Acme security alert
        # forwarded into the work inbox is meaningfully different from
        # the same alert in the personal lane).
        # Note: we deliberately do NOT pass Gmail's auto-IMPORTANT marker as a
        # prior — it's liberal and inflates "fake important". The LLM decides
        # importance from content + sender + lane only.
        prompt_parts = [f"From: {sender}", f"Subject: {subject}"]
        if lane != _OWN_LANE:
            prompt_parts.append(f"Forwarded from: {lane}")
        prompt_parts.append(f"Body:\n{body[:800]}")
        prompt = "\n".join(prompt_parts)
        try:
            # db_pool + purpose ⇒ think() writes the llm_calls row itself, for
            # success and failure alike (LLMClient._record_call).
            raw = await self.llm_client.think(
                prompt=prompt,
                model=self.model_balanced,
                system_prompt=_CLASSIFY_SYSTEM,
                # gpt-oss:20b (a reasoning model) bills hidden reasoning_content
                # against max_tokens. 256 truncated always; 768 still truncated
                # intermittently in prod (llm_truncated + classify_email_llm_failed
                # on long-reasoning emails). 2048 leaves ample reasoning headroom
                # with ~256 for the JSON payload.
                max_tokens=2048,
                db_pool=self.db_pool,
                purpose="gmail_classification",
                agent_id=self.agent_id,
            )
            # think() returns {"response": str, "model": str, ...}
            text = (raw.get("response") or "").strip()
            # Guard: if the model returned empty content (truncation or other),
            # fall through to the fallback path rather than crashing on json.loads.
            if not text:
                raise ValueError("empty LLM response for email classification")
            parsed = parse_llm_json(text)
            if not isinstance(parsed, dict):
                raise ValueError("unparseable LLM response for email classification")
            category = parsed.get("category", _FALLBACK_CATEGORY)
            if category not in _TRIAGE_CATEGORIES:
                category = _FALLBACK_CATEGORY
            # Cap BEFORE teaching, so a notification sender can never accumulate
            # important_action reputation and start short-circuiting the LLM
            # straight into a Todoist task.
            category = cap_notification_category(category, subject, extra_markers)
            # Teach the per-sender cache so repeat senders skip the LLM next
            # time — including the tags, which the cache hit replays into the
            # fan-out decision.
            tags = _parse_tags(parsed.get("tags"))
            if sender and self.db_pool:
                await self._triage_upsert(sender, category, tags)
            return {
                "category": category,
                "confidence": float(parsed.get("confidence", 0.7)),
                "tags": tags,
                "reason": str(parsed.get("reason") or "").strip(),
                "summary": str(parsed.get("summary") or "").strip(),
                "lane": lane,
                "source": "llm",
            }
        except Exception as exc:
            activity.logger.warning("classify_email_llm_failed: %s", str(exc)[:200])
            return {
                "category": _FALLBACK_CATEGORY,
                "confidence": 0.5,
                "tags": [],
                "reason": "",
                "summary": "",
                "lane": lane,
                "source": "fallback",
            }

    @activity.defn
    async def record_triage_outcome(
        self,
        email_id: str,
        predicted: str,
        labels: list[str],
        account_label: str = "",
    ) -> dict:
        """Feedback loop: log the per-email prediction and capture user
        corrections into `triage_accuracy` (the only objective mis-triage signal
        — the table was previously unused). First sight inserts the prediction
        with actual=NULL; a later re-observation whose Gmail labels contradict
        the ORIGINAL prediction sets actual + corrected_by='user_gmail'. Records
        confirmations are not stored (only corrections), keeping the table a
        clean list of where AEGIS got it wrong. Fire-and-forget: never raises.

        `account_label` records WHICH mailbox the message lives in (#260) — this
        is the only INSERT into the table, so it is the only place that knowledge
        exists to be captured. Without it `recheck_triage_outcomes` cannot tell
        "this message is gone" from "this message belongs to someone else's
        token".
        """
        if not email_id or not self.db_pool:
            return {"recorded": False}
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT predicted, actual FROM triage_accuracy WHERE email_id=$1",
                    email_id,
                )
                if row is None:
                    await conn.execute(
                        "INSERT INTO triage_accuracy (email_id, predicted, account_label) "
                        "VALUES ($1,$2,$3)",
                        email_id,
                        predicted,
                        account_label or None,
                    )
                    return {"recorded": True, "outcome": "predicted"}
                if row["actual"] is not None:
                    return {"recorded": False, "outcome": "already_scored"}
                correction = assess_triage_correction(row["predicted"], labels)
                if correction is None:
                    return {"recorded": False, "outcome": "consistent"}
                await conn.execute(
                    "UPDATE triage_accuracy SET actual=$2, corrected_by='user_gmail' "
                    "WHERE email_id=$1 AND actual IS NULL",
                    email_id,
                    correction,
                )
                return {"recorded": True, "outcome": "corrected", "actual": correction}
        except Exception as exc:
            activity.logger.warning(
                "record_triage_outcome_failed email_id=%s err=%s", email_id, str(exc)[:200]
            )
            return {"recorded": False, "outcome": "error"}

    @activity.defn
    async def recheck_triage_outcomes(self, account_label: str, limit: int = 50) -> dict:
        """Close the triage feedback loop (#74): score unscored predictions
        against the email's CURRENT Gmail labels.

        record_triage_outcome's correction branch assumed actioned emails get
        re-observed by the ingest fetch — false in practice (`is:unread` + a
        forward-moving `after:` cursor), so predictions never got an `actual`.
        This actively re-reads labels for unscored predictions 1h–7d old,
        prioritizing the oldest never-checked rows first (`ORDER BY
        last_checked_at ASC NULLS FIRST, created_at ASC`):
          - labels contradict the prediction → actual + corrected_by='user_gmail'.
            This is a REAL, zero-effort human correction signal (#116) — also
            write an agent_memory row so the mis-triage is remembered, and
            (#102) feed the sender back into `triage_state` so the correction
            actually changes a future verdict.
          - consistent → stamp last_checked_at and keep cycling.
          - unobservable (deleted mail) → stamp last_checked_at too (#115):
            leaving it NULL forever let an unresolvable row camp at the front
            of the NULLS-FIRST queue on every future call, starving
            genuinely-resolvable rows behind it once unresolvable rows ever
            outnumbered `limit`.
          - unscored rows past the 7d window, whether or not they were ever
            actively checked (#115 — silence is agreement applies equally to
            a row this loop never got to) → actual = predicted,
            corrected_by='implicit'.

        Rows are scoped to `account_label` (#260). Selecting account-agnostically
        while resolving with ONE account's token made every foreign row a 404 →
        "unobservable" → stamped, and since the flow runs the accounts in
        sequence, account #1 stamped the others' rows behind the NULLS-FIRST
        queue before they ran. A real human IMPORTANT label on any non-first
        account could therefore never be seen, and aged into
        corrected_by='implicit' — inverting the user's verdict. Pre-#260 rows
        have account_label NULL and stay resolvable by any account (unchanged
        best-effort; they drain within the 7d window).

        Fire-and-forget: never raises.
        """
        empty = {
            "checked": 0,
            "corrected": 0,
            "confirmed": 0,
            "memories_written": 0,
            "senders_relearned": 0,
            "disposition_corrected": 0,
            "machine_corrected": 0,
        }
        if not self.db_pool:
            return empty
        try:
            # Mine the Todoist verdicts FIRST, so a prediction the user has
            # already ruled on is off the table before the Gmail-label pass
            # selects rows — each prediction records exactly one correction and
            # teaches `triage_state` exactly once.
            empty["disposition_corrected"] = 0
            mined = await self._mine_todoist_dispositions(limit)

            rows = await self.db_pool.fetch(
                "SELECT id, email_id, predicted FROM triage_accuracy "
                "WHERE actual IS NULL "
                "  AND (account_label = $2 OR account_label IS NULL) "
                "  AND created_at > now() - interval '7 days' "
                "  AND created_at < now() - interval '1 hour' "
                "ORDER BY last_checked_at ASC NULLS FIRST, created_at ASC LIMIT $1",
                limit,
                account_label,
            )
            checked = corrected = memories_written = senders_relearned = 0
            if rows:
                token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

                def _sync_labels() -> dict[str, tuple[list[str], str, str] | None]:
                    svc = _build_gmail_service(self.gmail_credentials_file, token_path)
                    out: dict[str, tuple[list[str], str, str] | None] = {}
                    for r in rows:
                        try:
                            m = (
                                svc.users()
                                .messages()
                                .get(
                                    userId="me",
                                    id=r["email_id"],
                                    format="metadata",
                                    # (#102) From comes back too: triage_accuracy
                                    # stores no sender, and the relearn step below
                                    # needs one to key triage_state.
                                    metadataHeaders=["Subject", "From"],
                                )
                                .execute()
                            )
                            # System labels (IMPORTANT/STARRED/UNREAD) have
                            # id == name, so labelIds feed assess directly.
                            hdrs = _parse_headers(m.get("payload") or {})
                            out[r["email_id"]] = (
                                m.get("labelIds") or [],
                                hdrs["Subject"],
                                hdrs["From"],
                            )
                        except Exception:  # noqa: BLE001 — message gone
                            out[r["email_id"]] = None
                    return out

                labels_by_id = await asyncio.to_thread(_sync_labels)
                for r in rows:
                    observed = labels_by_id.get(r["email_id"])
                    if observed is None:
                        # (#115) Stamp last_checked_at even on failure so an
                        # unobservable row doesn't win queue-front priority
                        # (NULLS FIRST) on every subsequent call, crowding out
                        # rows that ARE resolvable. It still ages into the
                        # honest implicit-confirm path at the 7d mark. Safe
                        # now that the SELECT is account-scoped (#260): a miss
                        # here means OUR message is gone, not that we asked the
                        # wrong mailbox.
                        await self.db_pool.execute(
                            "UPDATE triage_accuracy SET last_checked_at=now() WHERE id=$1",
                            r["id"],
                        )
                        continue
                    labels, subject, from_header = observed
                    checked += 1
                    correction = assess_triage_correction(r["predicted"], labels)
                    if correction:
                        corrected += 1
                        await self.db_pool.execute(
                            "UPDATE triage_accuracy SET actual=$2, "
                            "corrected_by='user_gmail', last_checked_at=now() WHERE id=$1",
                            r["id"],
                            correction,
                        )
                        # (#102) Teach the sender cache. Corrections used to be
                        # write-only — recorded in triage_accuracy and
                        # agent_memory, but never fed back into triage_state,
                        # so they could not change a single future verdict. The
                        # classify cascade short-circuits the LLM entirely once
                        # a sender reaches n>=3 at conf>=0.75 (and gmail_promo
                        # seeds unseen promo senders straight to 'useless'), so
                        # a mis-cached sender stayed mis-cached forever. Route
                        # through _triage_upsert rather than a parallel rule so
                        # a human correction lands with exactly the same
                        # disagreement arithmetic as an LLM disagreement:
                        # conf -= 0.3, flip category once conf <= 0.3.
                        relearn_as = _CORRECTION_TO_CATEGORY.get(correction)
                        sender = _normalize_sender(from_header)
                        if sender and relearn_as:
                            await self._triage_upsert(sender, relearn_as)
                            senders_relearned += 1
                        if await record_gmail_triage_correction(
                            self.db_pool,
                            self.agent_id,
                            r["email_id"],
                            subject,
                            r["predicted"],
                            correction,
                        ):
                            memories_written += 1
                    else:
                        await self.db_pool.execute(
                            "UPDATE triage_accuracy SET last_checked_at=now() WHERE id=$1",
                            r["id"],
                        )
            # (#115) "Silence is agreement" applies whether or not a row was
            # ever actively checked — last_checked_at IS NULL no longer
            # exempts a row from implicit-confirm once it's past the 7d
            # window, so a backlogged/unresolvable row can't get stuck with
            # actual NULL forever.
            result = await self.db_pool.execute(
                "UPDATE triage_accuracy SET actual=predicted, corrected_by='implicit' "
                "WHERE actual IS NULL "
                "  AND created_at <= now() - interval '7 days'"
            )
            confirmed = int(result.split()[-1])
            if checked or confirmed or mined["corrected"]:
                activity.logger.info(
                    "recheck_triage_outcomes account=%s checked=%d corrected=%d confirmed=%d "
                    "memories_written=%d senders_relearned=%d disposition_corrected=%d",
                    account_label,
                    checked,
                    corrected,
                    confirmed,
                    memories_written + mined["memories_written"],
                    senders_relearned + mined["senders_relearned"],
                    mined["corrected"],
                )
            return {
                "checked": checked,
                "corrected": corrected,
                "confirmed": confirmed,
                "machine_corrected": mined["machine_corrected"],
                "memories_written": memories_written + mined["memories_written"],
                "senders_relearned": senders_relearned + mined["senders_relearned"],
                "disposition_corrected": mined["corrected"],
            }
        except Exception as exc:  # noqa: BLE001 — feedback must never block ingest
            activity.logger.warning(
                "recheck_triage_outcomes_failed account=%s err=%s", account_label, str(exc)[:200]
            )
            return empty

    async def _mine_todoist_dispositions(self, limit: int = 50) -> dict:
        """Read the user's own verdict on captured emails out of Todoist.

        The Gmail-label detector can only ever learn in one direction. Its
        "unimportant" branch requires IMPORTANT and STARRED to both be ABSENT —
        but AEGIS itself stamps IMPORTANT on every `important_*` verdict, so
        that branch was unreachable in practice: 76 corrections in prod, 100% of
        them unimportant→important, zero the other way, ever. Every correction
        then relearned the sender as `important_read`. The loop could only make
        triage noisier.

        This is the missing negative signal, and it was already sitting in the
        DB: when the user closes an AEGIS-created `#email` task carrying
        `#trash` or `@reference`, they have said "this needed nothing from me"
        with no extra effort.

        The signal is only worth anything if the AUTHOR of the disposition is
        known, and the first cut of this did not check. ClarifyFlow applies
        `@reference` and `#trash` to AEGIS's own `#email` captures every 15
        minutes, so the loop was reading its own classification back as the
        user's verdict: all 39 `user_todoist` corrections in prod had an applied
        clarify decision behind them, 39 of 39, each one demoting a sender and
        writing a "User corrected email triage" memory nobody expressed (#353).

        Both authors are now mined, under different provenance, because a
        measurement of prod showed the human signal is not merely rare but
        ABSENT: of 212 completed `important_action` captures, every one of the
        76 carrying a noise label had been disposed by clarify, and a 300-message
        sample of flagged mail found 0 trashed, 0 unstarred and 0 with IMPORTANT
        removed. Excluding clarify and stopping there leaves the loop with no
        negative direction at all.

        So a clarify disposition is kept as what it honestly is — a second
        machine opinion that disagrees with triage. Triage's LLM said this
        warranted an interrupt; clarify's own logic (deterministic notification
        markers, then an LLM with a different prompt) said it was reference or
        trash. That disagreement is real evidence, and it is abundant. It is
        recorded as `corrected_by='clarify_disagreement'`, costs half a step of
        sender confidence so it cannot flip a sender alone, and NEVER writes an
        `agent_memory` row. A human disposition still gets full weight, the
        `user_todoist` provenance and a memory.

        Routes through `_triage_upsert` so a Todoist verdict lands with exactly
        the same disagreement arithmetic as any other (conf -= 0.3, flip at
        <= 0.3) rather than as a privileged override.
        """
        out = {
            "corrected": 0,
            "senders_relearned": 0,
            "memories_written": 0,
            "machine_corrected": 0,
        }
        rows = await self.db_pool.fetch(
            """
            SELECT ta.id, ta.email_id, ta.predicted, t.content, t.description,
                   EXISTS (
                       SELECT 1 FROM gtd_clarify_log g
                       WHERE g.todoist_task_id = tci.todoist_task_ref
                         AND g.applied
                         AND g.classification = ANY($2::text[])
                   ) AS clarify_disposed
            FROM triage_accuracy ta
            JOIN todoist_capture_idempotency tci
              ON tci.source_tag = '#email'
             AND tci.external_id = 'gmail-' || ta.email_id
            JOIN todoist_tasks t ON t.id = tci.todoist_task_ref
            WHERE ta.actual IS NULL
              AND t.is_completed
              AND t.labels && $1::text[]
            LIMIT $3
            """,
            list(_DISPOSITION_NOISE_LABELS),
            list(_CLARIFY_SELF_DISPOSITIONS),
            limit,
        )
        for r in rows:
            # WHO disposed of the task decides everything downstream: the
            # provenance string, how hard it hits the sender cache, and whether
            # a memory is written at all. Keyed on `applied` — a decision
            # clarify made but did not apply left the label to a human.
            machine = bool(r["clarify_disposed"])
            provenance = "clarify_disagreement" if machine else "user_todoist"
            updated = await self.db_pool.execute(
                "UPDATE triage_accuracy SET actual='unimportant', "
                "corrected_by=$2, last_checked_at=now() "
                "WHERE id=$1 AND actual IS NULL",
                r["id"],
                provenance,
            )
            if updated.split()[-1] == "0":  # raced by the label pass
                continue
            out["machine_corrected" if machine else "corrected"] += 1
            sender = _sender_from_description(r["description"])
            if sender:
                await self._triage_upsert(
                    sender,
                    _CORRECTION_TO_CATEGORY["unimportant"],
                    weight=_MACHINE_DISAGREEMENT_STEP if machine else _DISAGREEMENT_STEP,
                )
                out["senders_relearned"] += 1
            else:
                activity.logger.warning(
                    "disposition_sender_unparsed email_id=%s — correction recorded, "
                    "sender not relearned",
                    r["email_id"],
                )
            if machine:
                # No agent_memory row, ever. A memory reads as a fact about the
                # USER ("User corrected email triage: …") and this is AEGIS
                # disagreeing with itself. Writing one is exactly the defect
                # #353 removed; the signal lives in triage_state and
                # triage_accuracy, where its provenance travels with it.
                continue
            if await record_gmail_triage_correction(
                self.db_pool,
                self.agent_id,
                r["email_id"],
                r["content"] or "",
                r["predicted"],
                "unimportant",
            ):
                out["memories_written"] += 1
        return out

    @activity.defn
    async def ingest_email_to_kg(
        self, msg: dict, thread_content: str, classification: dict
    ) -> dict:
        """Persist an important email into the knowledge graph so it can
        be recalled later via Raphael's `search_knowledge` / `ask_knowledge`.

        Pre-2026-05-21 the gmail flow classified + routed + archived
        but the body never reached the KG — important emails were
        effectively forgotten outside Gmail itself. This closes that
        loop for `important_action` and `important_read` categories.

        Best-effort: failures are logged and swallowed so the email
        triage continues.
        """
        if not self.knowledge_connector:
            return {"ingested": False, "reason": "no_connector"}
        body = (thread_content or msg.get("snippet") or "").strip()
        if not body:
            return {"ingested": False, "reason": "empty_body"}
        msg_id = msg.get("id") or ""
        subject = (msg.get("subject") or "(no subject)").strip()
        sender = (msg.get("sender") or "").strip()
        permalink = msg.get("permalink") or f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
        category = classification.get("category", "informational")
        lane = classification.get("lane") or msg.get("lane") or _OWN_LANE
        tags = ["email", category, f"lane:{lane}", *(classification.get("tags") or [])]
        try:
            await self.knowledge_connector.ingest_content(
                url=permalink,
                title=subject[:200],
                source_type="email",
                raw_text=body[:8000],
                tags=tags,
                metadata={
                    "message_id": msg_id,
                    "sender": sender[:200],
                    "category": category,
                    "confidence": float(classification.get("confidence") or 0.0),
                    "lane": lane,
                },
            )
            return {"ingested": True}
        except Exception as exc:
            activity.logger.warning(
                "ingest_email_to_kg_failed msg_id=%s err=%s",
                msg_id,
                str(exc)[:200],
            )
            return {"ingested": False, "reason": str(exc)[:200]}

    @activity.defn
    async def gather_email_context(
        self, subject: str, sender: str, exclude_url: str = ""
    ) -> str:
        """Search KS for prior emails/notes related to this one, so the task
        created from an important email carries context (the related thread,
        a prior commitment) and is more pointed than the email alone.

        Best-effort: returns "" on no connector / no relevant hits / any error.
        Excludes the just-ingested email itself (`exclude_url`) and applies a
        light relevance floor so unrelated chunks don't get stapled on.
        """
        if not self.knowledge_connector:
            return ""
        query = f"{subject} {sender}".strip()
        if not query:
            return ""
        try:
            hits = await self.knowledge_connector.search(query, limit=6)
        except Exception as exc:
            activity.logger.warning("gather_email_context_failed err=%s", str(exc)[:200])
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for h in hits or []:
            score = h.get("score")
            if score is not None and float(score) < 0.25:
                continue
            url = str(h.get("url") or h.get("source_url") or "")
            if exclude_url and url == exclude_url:
                continue
            title = str(h.get("title") or h.get("content") or "").strip()[:120]
            key = url or title
            if not title or key in seen:
                continue
            seen.add(key)
            lines.append(f"• {title}")
            if len(lines) >= 3:
                break
        return "\n".join(lines)

    async def _load_email_rules(self) -> dict:
        """User rules from `settings.email_triage_rules`, or the empty defaults.

        Best-effort like `_triage_lookup`: a config read must never be what stops
        mail from being classified.
        """
        if not self.db_pool:
            return merge_email_rules(None)
        try:
            return await get_email_rules(self.db_pool)
        except Exception as exc:
            activity.logger.warning("email_rules_load_failed err=%s", str(exc)[:120])
            return merge_email_rules(None)

    async def _triage_lookup(self, sender: str) -> dict | None:
        """Return {category, n, confidence, tags} for a sender from
        triage_state, or None. Best-effort — never raises into the classifier.

        `tags` is None when the row has never recorded any (every row written
        before the tags column existed), which is NOT the same as `[]` — see
        the cache-hit branch in `classify_email`, which refuses to
        short-circuit on a tagless row precisely so it can learn them.
        """
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state, metadata FROM triage_state WHERE email_addr = $1",
                    sender,
                )
            if not row:
                return None
            meta = _triage_meta(row)
            raw_tags = meta.get("tags")
            return {
                "category": row["state"],
                "n": int(meta.get("n", 0)),
                "confidence": float(meta.get("confidence", 0.0)),
                "tags": _parse_tags(raw_tags) if isinstance(raw_tags, list) else None,
            }
        except Exception as exc:
            activity.logger.warning("triage_lookup_failed sender=%s err=%s", sender, str(exc)[:120])
            return None

    async def _triage_upsert(
        self,
        sender: str,
        category: str,
        tags: list[str] | None = None,
        weight: float = _DISAGREEMENT_STEP,
    ) -> None:
        """Reinforce a sender's cached category. Agreement raises confidence;
        disagreement lowers it and flips the category once it bottoms out.
        Best-effort — never raises into the classifier.

        `weight` is how much a disagreement costs. Defaults to the full step;
        a machine-authored signal passes `_MACHINE_DISAGREEMENT_STEP` so it
        nudges rather than flips on a single observation.

        `tags` is the LLM's content tags for this message. They are stored so a
        later cache hit can replay them: the fan-out in `GmailIngestFlow` keys
        on `financial`/`payments`, so a cache hit that returned `[]` silently
        disabled receipt extraction for every sender that ever got cached
        (same defect class as #263, which fixed it for `sender_overrides` only).
        Pass None from the non-LLM callers (the cap-decay path) to PRESERVE the
        tags already on the row — a decay must not wipe what the LLM taught.
        Tags describe what the mail IS, not how much attention it deserves, so
        they deliberately survive a category flip.
        """
        if not sender or not self.db_pool:
            return
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state, metadata FROM triage_state WHERE email_addr = $1 FOR UPDATE",
                    sender,
                )
                if row is None:
                    meta = {"n": 1, "confidence": 0.6, "category": category}
                    if tags is not None:
                        meta["tags"] = list(tags)
                    # Pass the dict directly — the pool's asyncpg jsonb codec
                    # (db/pool.py::_init_connection) encodes it. json.dumps here
                    # would double-encode it into a JSON string scalar.
                    await conn.execute(
                        "INSERT INTO triage_state (email_addr, state, metadata, updated_at) "
                        "VALUES ($1, $2, $3, now())",
                        sender,
                        category,
                        meta,
                    )
                    return
                meta = _triage_meta(row)
                n = int(meta.get("n", 0)) + 1
                conf = float(meta.get("confidence", 0.6))
                cur = row["state"]
                if category == cur:
                    new_cat = cur
                    conf = min(1.0, conf + 0.15)
                else:
                    conf -= weight
                    if conf <= 0.3:
                        new_cat, conf = category, 0.6  # flip to the new majority
                    else:
                        new_cat = cur
                meta = {"n": n, "confidence": round(conf, 3), "category": new_cat}
                # None => caller has nothing to teach; keep what we already know.
                prior_tags = _triage_meta(row).get("tags")
                if tags is not None:
                    meta["tags"] = list(tags)
                elif isinstance(prior_tags, list):
                    meta["tags"] = list(prior_tags)
                await conn.execute(
                    "UPDATE triage_state SET state = $2, metadata = $3, updated_at = now() "
                    "WHERE email_addr = $1",
                    sender,
                    new_cat,
                    meta,
                )
        except Exception as exc:
            activity.logger.warning("triage_upsert_failed sender=%s err=%s", sender, str(exc)[:120])

    @activity.defn
    async def apply_label(self, account_label: str, message_id: str, label: str) -> dict:
        """Apply a Gmail label to a message. Best-effort — returns {ok: bool}.

        "READ" is not a generic mark-as-read: it is the *verdict* AEGIS applies
        to `useless`/`informational` (the only two categories the flow routes
        here — `flows/gmail_ingest.py`). See `_READ_VERDICT_REMOVES` for why it
        also strips IMPORTANT.

        "IMPORTANT_READ" is the `important_read` verdict: surface it with Gmail's
        IMPORTANT marker but do NOT hold it unread. Until 2026-08 that tier both
        labelled AND kept the mail unread, and nothing ever cleared it — with
        important_read running at 68% of all triaged mail (2,155 of 3,186
        predictions) the unread count could only grow, forever, which is exactly
        what it did. The label still makes the mail findable; the unread badge
        goes back to meaning "you have not seen this".
        """
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> dict:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            body: dict = {"addLabelIds": [label]}
            if label == "READ":
                body = {"removeLabelIds": list(_READ_VERDICT_REMOVES)}
            elif label == "IMPORTANT_READ":
                body = {"addLabelIds": ["IMPORTANT"], "removeLabelIds": ["UNREAD"]}
            elif label == "ARCHIVE":
                body = {"removeLabelIds": ["INBOX"]}
            return svc.users().messages().modify(userId="me", id=message_id, body=body).execute()

        try:
            result = await asyncio.to_thread(_sync)
            return {"ok": True, "id": result.get("id")}
        except Exception as exc:
            activity.logger.warning("gmail_label_failed: %s", str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}
