"""#674 — research areas: parsing, stories and the judge's answer (pure)."""

from __future__ import annotations

import pytest
from aegis.services import research_areas as ra

TOPICS = ["India politics", "Indian economy", "Space technology"]


def test_parse_areas_is_lenient():
    areas = ra.parse_areas(
        {
            "areas": [
                {"name": "India", "why": " I live here ", "cadence": "daily", "cap": 2,
                 "topics": ["india politics", "Nobody tracks this", "Indian economy"]},
                {"name": "india", "topics": ["Space technology"]},  # same slug: dropped
                {"name": "Curiosity", "cadence": "monthly", "cap": 99,
                 "topics": ["Indian economy", "Space technology"]},  # economy already claimed
                "junk",
                {"name": ""},
            ]
        },
        TOPICS,
    )
    assert areas == [
        ra.Area("India", "I live here", "daily", 2, ("India politics", "Indian economy")),
        ra.Area("Curiosity", "", "daily", ra.DEFAULT_CAP, ("Space technology",)),
    ]
    assert ra.parse_areas("not json", TOPICS) == []
    assert ra.parse_areas({"topics": []}, TOPICS) == []
    assert ra.area_topic_slugs(areas) == {"india-politics", "indian-economy", "space-technology"}


@pytest.mark.parametrize(
    ("areas", "message"),
    [
        ("x", "must be a list"),
        ([{"name": ""}], "needs a name"),
        ([{"name": "A"}, {"name": "a"}], "listed twice"),
        ([{"name": "A", "cadence": "hourly"}], "cadence"),
        ([{"name": "A", "cap": 0}], "cap"),
        ([{"name": "A", "cap": 2.5}], "cap"),
        ([{"name": "A", "why": 5}], "why"),
        ([{"name": "A", "topics": ["Mars"]}], "not a tracked topic"),
        ([{"name": "A", "topics": ["India politics"]}, {"name": "B", "topics": ["india politics"]}], "in both"),
    ],
)
def test_validate_areas_refuses_bad_entries(areas, message):
    with pytest.raises(ValueError, match=message):
        ra.validate_areas(areas, TOPICS)


def test_validate_areas_normalises_topic_names():
    out = ra.validate_areas([{"name": " World ", "topics": ["india politics", " "], "cap": 4}], TOPICS)
    assert out == [{"name": "World", "why": "", "cadence": "daily", "cap": 4, "topics": ["India politics"]}]


def test_cluster_folds_one_event_and_keys_it_on_the_earliest_url():
    items = [
        {"title": "RBI holds repo rate at 6% amid inflation worries", "url": "https://b/2",
         "occurred_at": "2026-09-25T08:00:00"},
        {"title": "RBI holds the repo rate at 6% amid inflation", "url": "https://a/1",
         "occurred_at": "2026-09-25T07:00:00"},
        {"title": "ISRO launches new navigation satellite", "url": "https://c/3",
         "occurred_at": "2026-09-25T09:00:00"},
    ]
    stories = ra.cluster(items)
    assert [(s["url"], s["sources"]) for s in stories] == [("https://a/1", 2), ("https://c/3", 1)]
    assert stories[0]["key"] == ra.story_key("https://a/1")


def test_parse_picks_keeps_order_range_and_cap():
    raw = 'Sure: [{"n": 3, "why": "rates"}, {"n": 3}, {"n": 9}, {"n": true}, {"n": 1, "why": "x"}, {"n": 2}]'
    assert ra.parse_picks(raw, count=3, cap=2) == [{"n": 3, "why": "rates"}, {"n": 1, "why": "x"}]
    assert ra.parse_picks("[]", 3, 2) == []
    assert ra.parse_picks("no idea", 3, 2) is None
    assert ra.parse_picks("[oops", 3, 2) is None


def test_fallback_picks_the_most_carried_stories():
    stories = [{"sources": 1}, {"sources": 4}, {"sources": 2}]
    assert ra.fallback_picks(stories, 2) == [{"n": 2, "why": ""}, {"n": 3, "why": ""}]


def test_judge_prompt_carries_the_why_the_cap_and_what_was_shown():
    area = ra.Area("India", "I invest here", cap=2)
    prompt = ra.judge_prompt(area, [{"title": "T1", "sources": 3, "summary": "s"}], ["Old story"])
    assert "I invest here" in prompt and "at most 2" in prompt
    assert "1. T1 (3 sources) — s" in prompt and "- Old story" in prompt
