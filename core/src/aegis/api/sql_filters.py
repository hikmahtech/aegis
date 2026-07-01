"""Shared SQL WHERE-clause builder for list/stat endpoints.

Several read-only routes hand-rolled the same ``conditions/params/idx`` loop
to turn a dict of optional equality filters into a parameterised WHERE
fragment. ``build_where`` centralises that, including the placeholder
numbering offset so callers can append further params (LIMIT/OFFSET) after
the filter params.
"""

from __future__ import annotations

from typing import Any


def build_where(filters: dict[str, Any], *, start_index: int = 1) -> tuple[str, list[Any]]:
    """Build a parameterised ``WHERE col = $n AND ...`` fragment.

    Only filters whose value is truthy contribute a condition (matching the
    prior ``if value:`` guards). Placeholder numbering starts at
    ``start_index`` so callers that prepend fixed conditions can offset it.

    Returns ``(fragment, params)`` where ``fragment`` is either ``""`` (no
    active filters) or ``" WHERE col = $1 AND ..."`` (leading space + WHERE
    keyword, matching the original inline builders). ``params`` are the
    values in the same order as their placeholders; the next free placeholder
    index is ``start_index + len(params)``.
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = start_index
    for column, value in filters.items():
        if value:
            conditions.append(f"{column} = ${idx}")
            params.append(value)
            idx += 1
    fragment = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return fragment, params
