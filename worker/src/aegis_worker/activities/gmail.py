"""Gmail fetch + OAuth helpers. Shared by GmailIngestFlow and ReceiptIngestFlow."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from aegis.llm import parse_llm_json
from aegis.observability import record_llm_call
from temporalio import activity
from temporalio.exceptions import ApplicationError

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


_CLASSIFY_SYSTEM = """\
You are an email triage assistant. Classify the email into exactly one category:

- important_action  — requires a decision or response (payment due, account issue, security alert, job offer, personal message from a real person)
- important_read    — informational but worth reading (receipts, invoices, shipping updates, newsletters with real value, GitHub notifications)
- informational     — low-value but harmless (automated reports, digests, minor notifications)
- useless           — pure noise with no value (marketing, promotions, spam, unsubscribe bait)

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


# Tiers AEGIS treats as "not important" (marks READ, no IMPORTANT label).
_TRIAGE_UNIMPORTANT = {"useless", "informational"}
# Tiers AEGIS treats as "important" (IMPORTANT label + kept unread).
_TRIAGE_IMPORTANT = {"important_action", "important_read"}


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
    async def fetch_thread(self, account_label: str, thread_id: str) -> str:
        """Return plain-text body of the most recent messages in a thread (up to 2000 chars)."""
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> str:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
            parts: list[str] = []
            for msg in (thread.get("messages") or [])[-5:]:  # last 5 messages max
                text = _extract_text_from_part(msg.get("payload") or {})
                if text.strip():
                    parts.append(text[:600])
            return "\n---\n".join(parts)[:2000]

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:
            activity.logger.warning("fetch_thread_failed thread=%s: %s", thread_id, str(exc)[:200])
            return ""

    @activity.defn
    async def classify_email(self, msg: dict, thread_content: str = "") -> dict:
        """Classify an email via a data-driven cascade — cheapest signal first,
        LLM last. Returns {category, confidence, tags, reason, summary, lane, source}.

        Cascade (2026-05-30):
          1. (A) confident per-sender cache hit  -> use it, NO LLM (source=cache)
          2. (B) unknown sender + Gmail promo    -> useless, NO LLM (source=gmail_promo)
          3. LLM tie-breaker (fed Gmail's IMPORTANT prior); result teaches the
             sender cache for next time (source=llm)

        Falls back to 'informational' if the LLM is unavailable or returns bad JSON.
        thread_content: full thread text from fetch_thread (preferred over snippet).
        """
        lane = msg.get("lane") or _OWN_LANE
        sender = _normalize_sender(msg.get("sender") or "")
        labels = msg.get("labels") or []
        gmail_promo = any(c in labels for c in _GMAIL_PROMO_LABELS)

        # (A) Confident sender-reputation cache -> trust it, skip the LLM.
        cached = await self._triage_lookup(sender) if (sender and self.db_pool) else None
        if cached and cached["n"] >= _CACHE_MIN_N and cached["confidence"] >= _CACHE_MIN_CONF:
            return {
                "category": cached["category"],
                "confidence": cached["confidence"],
                "tags": [],
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

        subject = msg.get("subject") or ""
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
            _t0 = time.monotonic()
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
            )
            await record_llm_call(
                self.db_pool,
                model=raw.get("model", self.model_balanced),
                prompt_tokens=raw.get("prompt_tokens", 0),
                completion_tokens=raw.get("completion_tokens", 0),
                latency_ms=int((time.monotonic() - _t0) * 1000),
                purpose="gmail_classification",
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
            # Teach the per-sender cache so repeat senders skip the LLM next time.
            if sender and self.db_pool:
                await self._triage_upsert(sender, category)
            return {
                "category": category,
                "confidence": float(parsed.get("confidence", 0.7)),
                "tags": _parse_tags(parsed.get("tags")),
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
    async def record_triage_outcome(self, email_id: str, predicted: str, labels: list[str]) -> dict:
        """Feedback loop: log the per-email prediction and capture user
        corrections into `triage_accuracy` (the only objective mis-triage signal
        — the table was previously unused). First sight inserts the prediction
        with actual=NULL; a later re-observation whose Gmail labels contradict
        the ORIGINAL prediction sets actual + corrected_by='user_gmail'. Records
        confirmations are not stored (only corrections), keeping the table a
        clean list of where AEGIS got it wrong. Fire-and-forget: never raises.
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
                        "INSERT INTO triage_accuracy (email_id, predicted) VALUES ($1,$2)",
                        email_id,
                        predicted,
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
        This actively re-reads labels for unscored predictions 1h–7d old
        (round-robin via last_checked_at so every row cycles within the window):
          - labels contradict the prediction → actual + corrected_by='user_gmail'
          - consistent → stamp last_checked_at and keep cycling
          - unscored rows past the 7d window that were checked at least once →
            silence is agreement: actual = predicted, corrected_by='implicit'.
        Rows never successfully observed (deleted mail, or another account's —
        predictions don't record their account) stay NULL rather than lie.
        Fire-and-forget: never raises.
        """
        empty = {"checked": 0, "corrected": 0, "confirmed": 0}
        if not self.db_pool:
            return empty
        try:
            rows = await self.db_pool.fetch(
                "SELECT id, email_id, predicted FROM triage_accuracy "
                "WHERE actual IS NULL "
                "  AND created_at > now() - interval '7 days' "
                "  AND created_at < now() - interval '1 hour' "
                "ORDER BY last_checked_at ASC NULLS FIRST, created_at ASC LIMIT $1",
                limit,
            )
            checked = corrected = 0
            if rows:
                token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

                def _sync_labels() -> dict[str, list[str] | None]:
                    svc = _build_gmail_service(self.gmail_credentials_file, token_path)
                    out: dict[str, list[str] | None] = {}
                    for r in rows:
                        try:
                            m = (
                                svc.users()
                                .messages()
                                .get(userId="me", id=r["email_id"], format="minimal")
                                .execute()
                            )
                            # System labels (IMPORTANT/STARRED/UNREAD) have
                            # id == name, so labelIds feed assess directly.
                            out[r["email_id"]] = m.get("labelIds") or []
                        except Exception:  # noqa: BLE001 — gone/foreign message
                            out[r["email_id"]] = None
                    return out

                labels_by_id = await asyncio.to_thread(_sync_labels)
                for r in rows:
                    labels = labels_by_id.get(r["email_id"])
                    if labels is None:
                        # ponytail: unobservable rows (deleted mail / another
                        # account's) keep queue-front priority until they age
                        # out of the 7d window; >limit of them per account
                        # would starve a run. Track-per-account is the upgrade
                        # path if that ever happens.
                        continue
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
                    else:
                        await self.db_pool.execute(
                            "UPDATE triage_accuracy SET last_checked_at=now() WHERE id=$1",
                            r["id"],
                        )
            result = await self.db_pool.execute(
                "UPDATE triage_accuracy SET actual=predicted, corrected_by='implicit' "
                "WHERE actual IS NULL "
                "  AND created_at <= now() - interval '7 days' "
                "  AND last_checked_at IS NOT NULL"
            )
            confirmed = int(result.split()[-1])
            if checked or confirmed:
                activity.logger.info(
                    "recheck_triage_outcomes account=%s checked=%d corrected=%d confirmed=%d",
                    account_label,
                    checked,
                    corrected,
                    confirmed,
                )
            return {"checked": checked, "corrected": corrected, "confirmed": confirmed}
        except Exception as exc:  # noqa: BLE001 — feedback must never block ingest
            activity.logger.warning(
                "recheck_triage_outcomes_failed account=%s err=%s", account_label, str(exc)[:200]
            )
            return empty

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

    async def _triage_lookup(self, sender: str) -> dict | None:
        """Return {category, n, confidence} for a sender from triage_state, or
        None. Best-effort — never raises into the classifier."""
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state, metadata FROM triage_state WHERE email_addr = $1",
                    sender,
                )
            if not row:
                return None
            meta = _triage_meta(row)
            return {
                "category": row["state"],
                "n": int(meta.get("n", 0)),
                "confidence": float(meta.get("confidence", 0.0)),
            }
        except Exception as exc:
            activity.logger.warning("triage_lookup_failed sender=%s err=%s", sender, str(exc)[:120])
            return None

    async def _triage_upsert(self, sender: str, category: str) -> None:
        """Reinforce a sender's cached category. Agreement raises confidence;
        disagreement lowers it and flips the category once it bottoms out.
        Best-effort — never raises into the classifier."""
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
                    conf -= 0.3
                    if conf <= 0.3:
                        new_cat, conf = category, 0.6  # flip to the new majority
                    else:
                        new_cat = cur
                meta = {"n": n, "confidence": round(conf, 3), "category": new_cat}
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
        """Apply a Gmail label to a message. Best-effort — returns {ok: bool}."""
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> dict:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            body: dict = {"addLabelIds": [label]}
            if label == "READ":
                body = {"removeLabelIds": ["UNREAD"]}
            elif label == "ARCHIVE":
                body = {"removeLabelIds": ["INBOX"]}
            return svc.users().messages().modify(userId="me", id=message_id, body=body).execute()

        try:
            result = await asyncio.to_thread(_sync)
            return {"ok": True, "id": result.get("id")}
        except Exception as exc:
            activity.logger.warning("gmail_label_failed: %s", str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}
