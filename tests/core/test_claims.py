from aegis.services.claims import calendar_event_to_content


class TestCalendarEventToContent:
    """ponytail: graph layer removed. Tests content conversion instead of claims."""

    def test_event_with_summary_and_description(self):
        event = {
            "id": "cal-123",
            "summary": "Team standup",
            "description": "Discuss sprint progress",
            "start": "2026-03-29T10:00:00+05:30",
            "end": "2026-03-29T10:30:00+05:30",
        }
        content = calendar_event_to_content(event)
        assert content["url"] == "calendar://cal-123"
        assert content["title"] == "Team standup"
        assert content["source_type"] == "calendar"
        assert "Team standup" in content["raw_text"]
        assert "2026-03-29T10:00:00+05:30" in content["raw_text"]
        assert "Discuss sprint progress" in content["raw_text"]
        assert content["summary"] == "Discuss sprint progress"
        assert "calendar" in content["tags"]
        assert "event" in content["tags"]

    def test_event_without_summary(self):
        event = {"id": "cal-456", "start": "2026-03-29T14:00:00Z"}
        content = calendar_event_to_content(event)
        assert content["url"] == "calendar://cal-456"
        assert content["title"] == "Untitled"
        assert content["source_type"] == "calendar"
        assert "2026-03-29T14:00:00Z" in content["raw_text"]

    def test_event_with_attendees(self):
        event = {
            "id": "cal-789",
            "summary": "Board meeting",
            "start": "2026-04-01T09:00:00Z",
            "attendees": [
                {"displayName": "Alice", "email": "alice@example.com"},
                {"email": "bob@example.com"},
            ],
        }
        content = calendar_event_to_content(event)
        assert content["title"] == "Board meeting"
        assert "Alice" in content["raw_text"]
        assert "bob@example.com" in content["raw_text"]

    def test_event_with_string_attendees(self):
        """fetch_events emits attendees as plain email STRINGS — must not crash
        (regression: 'str' object has no attribute 'get')."""
        event = {
            "id": "cal-str",
            "summary": "Sync",
            "start": "2026-04-01T09:00:00Z",
            "attendees": ["alice@example.com", "bob@example.com"],
        }
        content = calendar_event_to_content(event)
        assert "alice@example.com" in content["raw_text"]
        assert "bob@example.com" in content["raw_text"]
