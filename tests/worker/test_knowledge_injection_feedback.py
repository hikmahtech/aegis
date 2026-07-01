"""Tests for knowledge injection feedback helpers."""

from aegis.services.chat import _check_knowledge_references, _content_hash, _extract_keywords


class TestContentHash:
    def test_deterministic(self):
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_different_inputs(self):
        assert _content_hash("hello") != _content_hash("world")

    def test_returns_12_chars(self):
        assert len(_content_hash("anything")) == 12


class TestExtractKeywords:
    def test_filters_stop_words(self):
        keywords = _extract_keywords("the quick brown fox is running")
        assert "the" not in keywords
        assert "is" not in keywords
        assert "quick" in keywords
        assert "brown" in keywords

    def test_filters_short_words(self):
        keywords = _extract_keywords("I am at it by go")
        # all <= 2 chars or stop words
        assert keywords == []

    def test_max_words(self):
        keywords = _extract_keywords("one two three four five six seven", max_words=3)
        assert len(keywords) == 3

    def test_strips_punctuation(self):
        keywords = _extract_keywords("hello, world! testing.")
        assert "hello" in keywords
        assert "world" in keywords
        assert "testing" in keywords


class TestCheckKnowledgeReferences:
    def test_detects_keyword_overlap(self):
        injected = [
            {"content_hash": "abc", "keywords": ["temporal", "workflow", "dispatch"]},
        ]
        response = "The temporal workflow handles this case properly."
        results = _check_knowledge_references(injected, response)
        assert len(results) == 1
        assert results[0]["referenced"] is True

    def test_no_reference_when_keywords_absent(self):
        injected = [
            {"content_hash": "abc", "keywords": ["temporal", "workflow", "dispatch"]},
        ]
        response = "The database migration was successful."
        results = _check_knowledge_references(injected, response)
        assert len(results) == 1
        assert results[0]["referenced"] is False

    def test_case_insensitive(self):
        injected = [
            {"content_hash": "abc", "keywords": ["Temporal", "Workflow"]},
        ]
        response = "temporal workflow is great"
        results = _check_knowledge_references(injected, response)
        assert results[0]["referenced"] is True

    def test_single_keyword_item(self):
        injected = [
            {"content_hash": "abc", "keywords": ["sentry"]},
        ]
        response = "The sentry alert fired."
        results = _check_knowledge_references(injected, response)
        assert results[0]["referenced"] is True

    def test_single_keyword_no_match(self):
        injected = [
            {"content_hash": "abc", "keywords": ["sentry"]},
        ]
        response = "The database is fine."
        results = _check_knowledge_references(injected, response)
        assert results[0]["referenced"] is False

    def test_multiple_items(self):
        injected = [
            {"content_hash": "aaa", "keywords": ["temporal", "workflow"]},
            {"content_hash": "bbb", "keywords": ["sentry", "alert"]},
        ]
        response = "The temporal workflow completed, but no alerts."
        results = _check_knowledge_references(injected, response)
        assert results[0]["content_hash"] == "aaa"
        assert results[0]["referenced"] is True
        assert results[1]["content_hash"] == "bbb"
        # only "alert" matches (substring of "alerts"), "sentry" is absent → 1/2 < threshold
        assert results[1]["referenced"] is False

    def test_empty_keywords(self):
        injected = [{"content_hash": "abc", "keywords": []}]
        response = "anything"
        results = _check_knowledge_references(injected, response)
        assert results[0]["referenced"] is False
