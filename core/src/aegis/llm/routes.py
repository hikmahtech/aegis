"""Purpose → category → model routing for `LLMClient.think()`.

The tier map (`tier.py`) answers "which model does this AGENT get". This
answers a different question: "which model does this JOB get". Every `think()`
call already carries a `purpose` string for the `llm_calls` ledger, so the
routing table needs no new plumbing at the call sites — a purpose names a
category, a category names a model and whether the reply should be requested
as JSON.

Two levels rather than purpose → model directly, because the useful unit of
change is the category. "Every structured extraction moves to the local model"
is one edit here; the same change spread over twenty purposes is twenty
chances to miss one.

Routing is OPTIONAL and default-empty: an unmapped purpose keeps whatever
model its caller resolved from the tier map, which is what every call did
before this existed. It applies to `think()` only — `chat()` is the
tool-calling agent loop, where the model is part of the agent's identity and a
per-purpose override would silently swap an agent's brain mid-conversation.

Installed at boot by `set_routes` from the resolved LLM backend (see
`services/llm_backend.py`), the same way `set_model_tiers` installs the tiers.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# {"categories": {name: {"model": str, "json": bool}}, "purposes": {purpose: category}}
_ROUTES: dict[str, Any] = {"categories": {}, "purposes": {}}


def set_routes(routes: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and install the in-process purpose→model routing table.

    Raises `ValueError` naming the offending key on any malformed entry, and
    leaves the currently installed table untouched when it does — validation
    builds the new table in locals and only swaps it in once every entry has
    passed. `None` or an empty mapping installs an empty table (routing off).

    A bad table must be loud here and harmless upstream: the boot sites catch
    this and carry on with routing disabled rather than refusing to start.
    """
    raw = routes or {}
    if not isinstance(raw, dict):
        raise ValueError(f"llm routes must be a mapping, got {type(raw).__name__}")

    raw_categories = raw.get("categories") or {}
    if not isinstance(raw_categories, dict):
        raise ValueError(
            f"llm routes 'categories' must be a mapping, got {type(raw_categories).__name__}"
        )
    categories: dict[str, dict[str, Any]] = {}
    for name, spec in raw_categories.items():
        if not isinstance(spec, dict):
            raise ValueError(f"category {name!r} must be a mapping with a 'model' key")
        model = spec.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"category {name!r} needs a non-empty 'model' string")
        json_mode = spec.get("json", False)
        if not isinstance(json_mode, bool):
            raise ValueError(
                f"category {name!r}: 'json' must be true or false, "
                f"got {type(json_mode).__name__}"
            )
        categories[str(name)] = {"model": model.strip(), "json": json_mode}

    raw_purposes = raw.get("purposes") or {}
    if not isinstance(raw_purposes, dict):
        raise ValueError(
            f"llm routes 'purposes' must be a mapping, got {type(raw_purposes).__name__}"
        )
    purposes: dict[str, str] = {}
    for purpose, category in raw_purposes.items():
        if not isinstance(category, str) or category not in categories:
            raise ValueError(
                f"purpose {purpose!r} maps to unknown category {category!r}; "
                f"known categories: {sorted(categories)}"
            )
        purposes[str(purpose)] = category

    _ROUTES["categories"] = categories
    _ROUTES["purposes"] = purposes
    return get_routes()


def get_routes() -> dict[str, Any]:
    """A copy of the installed table — callers must not mutate module state."""
    return {
        "categories": {k: dict(v) for k, v in _ROUTES["categories"].items()},
        "purposes": dict(_ROUTES["purposes"]),
    }


def route_for_purpose(purpose: str | None) -> tuple[str | None, bool]:
    """`(model, json_mode)` for a purpose; `(None, False)` when unmapped.

    A pure lookup that never raises: it sits in front of every `think()` call
    in AEGIS, so a half-written routing table must degrade to "no routing", not
    take generation down.
    """
    if not purpose:
        return (None, False)
    category = (_ROUTES.get("purposes") or {}).get(purpose)
    if not category:
        return (None, False)
    spec = (_ROUTES.get("categories") or {}).get(category)
    if not isinstance(spec, dict):
        return (None, False)
    model = spec.get("model")
    if not model:
        return (None, False)
    return (str(model), bool(spec.get("json", False)))


def merge_routes(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    """Layer a partial override (the DB row) over a full table (models.yaml).

    Merged per key so an override can retune one category, or one purpose,
    without restating the file: a category present in both is merged key by key
    (so `{"extract": {"model": "x"}}` repoints the model and keeps its `json`
    flag), and a purpose mapped to `null` or `""` is REMOVED rather than set —
    that is the only way to take a single purpose off routing from the DB.

    Deliberately does no validation; `set_routes` is the one gate, so a bad
    override is rejected at install time with the same message a bad file gets.
    """
    merged_categories: dict[str, Any] = {
        name: dict(spec) if isinstance(spec, dict) else spec
        for name, spec in ((base or {}).get("categories") or {}).items()
    }
    merged_purposes: dict[str, Any] = dict((base or {}).get("purposes") or {})

    over = override or {}
    for name, spec in (over.get("categories") or {}).items():
        current = merged_categories.get(name)
        if isinstance(current, dict) and isinstance(spec, dict):
            merged_categories[name] = {**current, **spec}
        else:
            merged_categories[name] = dict(spec) if isinstance(spec, dict) else spec
    for purpose, category in (over.get("purposes") or {}).items():
        if category is None or (isinstance(category, str) and not category.strip()):
            merged_purposes.pop(purpose, None)
        else:
            merged_purposes[purpose] = category

    return {"categories": merged_categories, "purposes": merged_purposes}
