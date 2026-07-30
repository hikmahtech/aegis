"""Article platforms need more than `__type` or Postiz rejects the post.

Proven live on 2026-07-31 against the deployed Postiz:

    POST /api/public/v1/posts  settings={"__type":"medium","title":...}
    -> 400 ["posts.0.settings.subtitle should not be null or undefined",
            "posts.0.settings.subtitle must be longer than or equal to 2 characters"]

`_POSTIZ_REQUIRED_SETTINGS` covered only `x` and `youtube`, so aegis could not post
to Medium or dev.to at all — the two channels were synced, mapped, and would have
failed at the last step. Field names were confirmed against the deployed DTOs
(`medium.settings.dto.js`: canonical/label/publication/subtitle/tags/title/value;
`dev.to.settings.dto.js`: canonical/main_image/organization/tags/title).
"""
import pytest
from aegis.connectors.social import _build_postiz_settings


def test_medium_gets_title_and_subtitle():
    s = _build_postiz_settings("medium", "Headline goes here\nA second line that reads as a subtitle", {})
    assert s["__type"] == "medium"
    assert s["title"] == "Headline goes here"
    assert s["subtitle"] == "A second line that reads as a subtitle"


def test_medium_subtitle_falls_back_to_title_on_a_one_line_post():
    """A short-form post has no second line, and an empty subtitle 400s."""
    s = _build_postiz_settings("medium", "Only one line here", {})
    assert s["subtitle"] == s["title"] == "Only one line here"
    assert len(s["subtitle"]) >= 2


def test_medium_subtitle_is_never_shorter_than_the_minimum():
    s = _build_postiz_settings("medium", "A", {})
    assert len(s["subtitle"]) >= 2, "MinLength(2) would reject this"


def test_devto_gets_a_title():
    s = _build_postiz_settings("devto", "Why HTTP 200 is the dangerous one\nbody text", {})
    assert s["__type"] == "devto"
    assert s["title"] == "Why HTTP 200 is the dangerous one"


def test_devto_does_not_get_a_subtitle():
    """DevToSettingsDto has no subtitle field; sending one is not required."""
    assert "subtitle" not in _build_postiz_settings("devto", "Title\nbody", {})


def test_title_is_the_first_line_not_the_whole_post():
    """A 90-char slice of a multi-line post would drag body text into the title."""
    s = _build_postiz_settings("medium", "Short title\nthen a much longer body paragraph that should not appear", {})
    assert s["title"] == "Short title"


def test_title_is_capped_at_ninety_chars():
    s = _build_postiz_settings("devto", "x" * 200, {})
    assert len(s["title"]) == 90


def test_empty_text_still_yields_a_valid_title():
    s = _build_postiz_settings("medium", "", {})
    assert s["title"] == "Untitled"
    assert len(s["subtitle"]) >= 2


@pytest.mark.parametrize("platform", ["bluesky", "linkedin", "facebook", "x"])
def test_short_form_platforms_are_untouched(platform):
    """Only article/video platforms take a title — don't change what already works."""
    s = _build_postiz_settings(platform, "some post text\nsecond line", {})
    assert "title" not in s
    assert "subtitle" not in s


def test_x_keeps_its_required_reply_setting():
    assert _build_postiz_settings("x", "hi", {})["who_can_reply_post"] == "everyone"


def test_youtube_still_gets_its_title():
    assert _build_postiz_settings("youtube", "My video\nbody", {})["title"] == "My video"


def test_account_meta_overrides_win():
    """meta.postiz_settings is the operator escape hatch and must stay highest priority."""
    s = _build_postiz_settings("medium", "Derived\nDerived sub",
                               {"postiz_settings": {"subtitle": "Operator wins", "publication": "pub123"}})
    assert s["subtitle"] == "Operator wins"
    assert s["publication"] == "pub123"
