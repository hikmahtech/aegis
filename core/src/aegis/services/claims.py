"""Convert structured data from external sources to content format for knowledge service."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def calendar_event_to_content(event: dict) -> dict:
    """Convert a calendar event to content format for ingestion.

    ponytail: graph layer removed. Returns content dict for chunk+embed ingest.
    """
    event_id = event.get('id', 'unknown')
    summary = event.get("summary", "Untitled")
    start = event.get("start", "")
    end = event.get("end", "")
    description = event.get("description", "")
    attendees = event.get("attendees", [])

    # Synthesize event to raw text
    raw_text_parts = [summary]
    if start:
        raw_text_parts.append(f"Start: {start}")
    if end:
        raw_text_parts.append(f"End: {end}")
    if description:
        raw_text_parts.append(f"Description: {description}")
    if attendees:
        # fetch_events emits attendees as a list of email STRINGS, but other
        # callers may pass Google's dict shape ({displayName, email}). Handle both.
        attendee_names = []
        for a in attendees:
            name = (a.get("displayName") or a.get("email")) if isinstance(a, dict) else str(a)
            if name:
                attendee_names.append(name)
        if attendee_names:
            raw_text_parts.append(f"Attendees: {', '.join(attendee_names)}")

    raw_text = "\n".join(raw_text_parts)

    return {
        "url": f"calendar://{event_id}",
        "title": summary,
        "source_type": "calendar",
        "raw_text": raw_text,
        "summary": description if description else summary,
        "tags": ["calendar", "event"],
    }
