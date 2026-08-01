"""Passive people enrichment (C2) — keep `life.people` current from the mail
and meetings that already flow through AEGIS, with no manual entry.

Two deliberately ASYMMETRIC lanes, because the two sources carry very different
risk:

  email    — ENRICHES ONLY, never creates. An inbox is unbounded and dominated
             by transactional senders; auto-creating from it would bury the
             registry's real humans under hundreds of vendors. What it does do
             is valuable and cheap to undo: when a sender's address (or their
             display name, matched exactly against a person the user entered by
             hand) resolves to an existing row, the address is learned as an
             alias and `last_contact` moves forward. That is what makes "when
             did I last talk to X?" answerable.

  calendar — MAY CREATE. An event you and a handful of people agreed to attend
             is a strong, low-volume human signal, and the set is bounded by
             your actual meeting schedule. Guarded three ways: the owner's own
             addresses are excluded, mass invites (> `max_event_attendees`) are
             skipped wholesale, and the lane REFUSES to run at all while
             `owner_emails` is unset — Google lists the calendar owner among
             every event's attendees, so without it the very first run would
             mint a person record for the user themselves.

Calendar never touches `last_contact`: `CalendarActivities.fetch_events` reads
`timeMin=now` forward, so every event it returns is in the FUTURE. A meeting you
have not had yet is not contact.

Both activities are best-effort — enrichment failing must never fail an email
triage or a calendar ingest run. No LLM is involved: matching is exact.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass
class PeopleActivities:
    db_pool: Any = None
    # Ships dark. DB-backed integration config (`people_enrichment_enabled` on
    # the admin Integrations page), so enabling it is a worker restart, not a
    # redeploy.
    enabled: bool = False
    # Settings.owner_emails — the addresses that are the user. Empty is the
    # live production state, and the calendar lane treats it as "refuse".
    owner_emails: frozenset[str] = frozenset()
    # An all-hands with 40 invitees is not a personal relationship.
    max_event_attendees: int = 8

    @activity.defn
    async def enrich_people_from_email(self, msg: dict) -> dict:
        """Fold one fetched email's sender into `life.people`. Never creates."""
        if not self.enabled or self.db_pool is None:
            return {"outcome": "disabled"}
        try:
            from aegis.services.people import parse_contact, record_contact

            email, display_name = parse_contact(msg.get("sender") or "")
            if not email:
                return {"outcome": "skipped_invalid"}
            ms = msg.get("internal_date_ms") or 0
            contact_at = (
                dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.UTC) if int(ms) > 0 else None
            )
            outcome = await record_contact(
                self.db_pool,
                email,
                display_name,
                contact_at,
                allow_create=False,
                owner_emails=self.owner_emails,
            )
            return {"outcome": outcome}
        except Exception as exc:  # noqa: BLE001 — enrichment must never fail triage
            activity.logger.warning(
                "enrich_people_from_email_failed msg_id=%s err=%s",
                msg.get("id", ""),
                str(exc)[:200],
            )
            return {"outcome": "error"}

    @activity.defn
    async def enrich_people_from_events(self, events: list[dict]) -> dict:
        """Fold small upcoming meetings' attendees into `life.people`.

        Returns a per-outcome tally so a run that created nothing says WHY.
        """
        if not self.enabled or self.db_pool is None:
            return {"outcome": "disabled"}
        if not self.owner_emails:
            # Refuse rather than risk auto-creating a person record for the
            # user (same guard as the curiosity calendar-attendee lane).
            activity.logger.warning(
                "enrich_people_from_events_refused reason=owner_emails_unset events=%d",
                len(events or []),
            )
            return {"outcome": "owner_emails_unset"}

        from aegis.services.people import record_contact

        tally: dict[str, int] = {}
        for event in events or []:
            attendees = event.get("attendees") or []
            if len(attendees) > self.max_event_attendees:
                tally["skipped_mass_invite"] = tally.get("skipped_mass_invite", 0) + 1
                continue
            for attendee in attendees:
                # fetch_events emits bare email strings; Google's own dict shape
                # ({displayName, email}) is accepted too, same as
                # `calendar_event_to_content`.
                if isinstance(attendee, dict):
                    email = attendee.get("email") or ""
                    display_name = attendee.get("displayName") or ""
                else:
                    email, display_name = str(attendee), ""
                if not email:
                    continue
                try:
                    outcome = await record_contact(
                        self.db_pool,
                        email,
                        display_name,
                        None,  # upcoming event — not contact that has happened
                        allow_create=True,
                        owner_emails=self.owner_emails,
                        source="calendar_enrichment",
                    )
                except Exception as exc:  # noqa: BLE001 — one bad attendee, not the run
                    activity.logger.warning(
                        "enrich_people_from_events_attendee_failed err=%s", str(exc)[:200]
                    )
                    outcome = "error"
                tally[outcome] = tally.get(outcome, 0) + 1
        return tally or {"outcome": "no_attendees"}
