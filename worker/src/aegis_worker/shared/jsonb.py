"""One decoder for a jsonb column, whichever shape asyncpg hands back.

With the JSONB codec registered (`aegis.db.create_pool` sets it via
`set_type_codec`) asyncpg returns a Python object. Without it — and for a
legacy row that was inserted pre-serialised and double-encoded — the value is
a raw string. Five activity modules each grew their own dual-path decoder for
that; this is the one they share.

What it does NOT decide is what a caller does with a string that will not
parse. That is a per-caller policy and the callers differ on purpose: a
channel's `config` must raise, because the updater writes the decoded dict
back and an empty one would wipe the row's other keys, while a digest's
counts would rather degrade to `{}` than fail a whole weekly review. So this
raises, and a caller that wants to degrade catches.
"""

from __future__ import annotations

import json
from typing import Any


def decode_jsonb(raw: Any, empty: Any) -> Any:
    """A jsonb column's value as a Python object.

    `None` and `""` decode to `empty`; an object passes straight through; a
    string is parsed, and `json.JSONDecodeError` (a `ValueError`) or
    `TypeError` is left to the caller.
    """
    if not raw:
        return empty
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(raw)
