"""The curiosity detectors' thresholds come from the `curiosity-daily` row's
config (through `CuriosityConfig`), the "track this?" card is a two-button
choice, and the registry builders thread the new knobs."""

from __future__ import annotations

from aegis_worker.activities.curiosity import TRACK_CHOICES, CuriosityActivities
from aegis_worker.flows.curiosity import CuriosityConfig
from aegis_worker.flows.daily_briefing import DailyBriefingConfig
from aegis_worker.flows.intelligence_scan import IntelligenceScanInput
from aegis_worker.registry import activity_type_map

from tests.worker.test_curiosity_knowledge_misses import (  # noqa: F401 — fixtures and helpers
    AGENT,
    _search,
    _Store,
    world,
)


def test_the_thresholds_default_to_the_fields_and_take_usable_overrides():
    act = CuriosityActivities(db_pool=None)
    assert act._thresholds() == {
        "min_attendee_events": 3,
        "min_project_tasks": 5,
        "empty_search_days": 14,
        "min_empty_searches": 2,
        "search_miss_below": 0.60,
    }
    th = act._thresholds(
        {"min_empty_searches": 1, "search_miss_below": "0.7", "empty_search_days": "soon",
         "min_project_tasks": True, "unknown": 9}
    )
    assert (th["min_empty_searches"], th["search_miss_below"]) == (1, 0.7)
    assert (th["empty_search_days"], th["min_project_tasks"]) == (14, 5), "bad values keep the field"
    assert CuriosityConfig().thresholds() == act._thresholds()


async def test_a_lower_min_empty_searches_cards_after_one_miss(world):  # noqa: F811
    pool, token = world
    await _search(pool, f"zorb {token} engines", _Store([0.41, 0.38]))
    act = CuriosityActivities(db_pool=pool)
    default = [g for _, g in await act._detect_untracked_topic(AGENT, "") if token in g["subject"]]
    assert default == [], "one miss is under the default of two"
    lowered = [
        g
        for _, g in await act._detect_untracked_topic(AGENT, "", act._thresholds({"min_empty_searches": 1}))
        if token in g["subject"]
    ]
    assert len(lowered) == 1 and lowered[0]["gap_type"] == "untracked_topic"
    assert "(yes/no)" not in lowered[0]["question"], "the card has buttons now"
    # A stricter miss floor makes the same search a hit, so no gap.
    strict = [
        g
        for _, g in await act._detect_untracked_topic(
            AGENT, "", act._thresholds({"min_empty_searches": 1, "search_miss_below": 0.4})
        )
        if token in g["subject"]
    ]
    assert strict == []


def test_the_choice_values_are_what_the_hook_reads():
    assert set(TRACK_CHOICES) == {"yes", "no"}
    assert CuriosityActivities._is_yes("yes") is True
    assert CuriosityActivities._is_yes("no") is False
    # A free-text answer to a card raised before the buttons still reads.
    assert CuriosityActivities._is_yes("sure, go ahead") is True
    assert CuriosityActivities._is_yes("nah") is False


def _act(workflow_type: str, config: dict, agent_id: str = "sebas"):
    return activity_type_map()[workflow_type](
        {"slug": "s", "workflow_type": workflow_type, "agent_id": agent_id, "config": config,
         "_settings": {"aegis_ui_url": "", "comms_url": ""}}
    )[1]


def test_the_registry_threads_the_new_knobs_with_their_defaults():
    cfg = _act("CuriosityCardFlow", {"max_per_day": 1})
    assert cfg.thresholds() == CuriosityConfig().thresholds()
    cfg = _act("CuriosityCardFlow", {"min_empty_searches": 1, "search_miss_below": 0.5, "min_attendee_events": "x"})
    assert (cfg.min_empty_searches, cfg.search_miss_below, cfg.min_attendee_events) == (1, 0.5, 3)

    assert _act("DailyBriefingFlow", {}) == DailyBriefingConfig(agent_id="sebas", feed_review_day=1)
    assert _act("DailyBriefingFlow", {"feed_review_day": 0}).feed_review_day == 0
    assert _act("DailyBriefingFlow", {"feed_review_day": ""}).feed_review_day == 1

    scan = _act("IntelligenceScanFlow", {"source": "hn", "topics": ["ai"]})
    assert isinstance(scan, IntelligenceScanInput) and scan.query_template == ""
    assert _act("IntelligenceScanFlow", {"source": "hn", "query_template": "x {topic}"}).query_template == (
        "x {topic}"
    )
