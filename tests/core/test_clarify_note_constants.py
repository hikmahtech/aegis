"""Constant + SQL-LIKE pin tests for clarify-note prefixes."""

from aegis.clarify_note import (
    AGENT_REPLY_PREFIX,
    AGENT_REPLY_SQL_LIKE,
    CLARIFY_NOTE_PREFIX,
)


def test_agent_reply_prefix_exact_value():
    """Pin the exact string. Renames will break this test, which is the
    intent — webhook + SQL filters elsewhere hard-code this same prefix.
    """
    assert AGENT_REPLY_PREFIX == "[Agent reply @ "


def test_agent_reply_sql_like_exact_value():
    """Pin the exact SQL LIKE pattern Postgres receives. Derived from
    PREFIX but pinned independently so a coordinated rename can't sneak by.
    """
    assert AGENT_REPLY_SQL_LIKE == "[Agent reply @ %"


def test_agent_reply_prefix_distinct_from_clarify_prefix():
    """Distinct families so observability + filtering stay separable."""
    assert AGENT_REPLY_PREFIX != CLARIFY_NOTE_PREFIX
