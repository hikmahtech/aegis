"""Research limits — the `research_config` settings row.

What `ResearchFlow` and the `research_topic` tool used to carry as module
constants in `services/research.py`. The defaults are those constants'
values. Edited on Admin → Research → Research limits
(`GET/PUT /api/admin/research/config`).

    {
      "wait_seconds": 45,            # how long research_topic waits before "still researching"
      "depths": {
        "quick":    {"pages": 3, "web_results": 8,  "papers": 5},
        "thorough": {"pages": 6, "web_results": 15, "papers": 10}
      },
      "page_chars": 6000,            # characters of one read page in the synthesis prompt
      "report_chars": 8000,          # a report posted as a task comment or chat reply
      "knowledge_hits": 5,           # knowledge-store documents gathered per run
      "note_hits": 3,                # vault notes gathered ahead of them (#514)
      "academic_terms": [...]        # words that make a question worth a paper search
    }

The schema caps the tools advertise (`_MAX_CHARS_CAP`, the `max_chars`
bounds) stay in code: the generated tool schema must not drift with a row.
"""

from __future__ import annotations

from typing import Any

from aegis.services.config_rows import (
    SettingsRow,
    as_int,
    require_int,
    require_str_list,
    str_list,
)

SETTINGS_KEY = "research_config"
DEPTHS = ("quick", "thorough")

DEFAULT_ACADEMIC_TERMS = [
    "papers?",
    "arxiv",
    "preprints?",
    "stud(?:y|ies)",
    "survey",
    "benchmarks?",
    "datasets?",
    "peer[- ]reviewed",
    "citations?",
    "literature",
    "state of the art",
    "sota",
]

DEFAULTS: dict[str, Any] = {
    "wait_seconds": 45,
    "depths": {
        "quick": {"pages": 3, "web_results": 8, "papers": 5},
        "thorough": {"pages": 6, "web_results": 15, "papers": 10},
    },
    "page_chars": 6000,
    "report_chars": 8000,
    "knowledge_hits": 5,
    "note_hits": 3,
    "academic_terms": list(DEFAULT_ACADEMIC_TERMS),
}
_DEPTH_KEYS = ("pages", "web_results", "papers")
_INT_KEYS = ("wait_seconds", "page_chars", "report_chars", "knowledge_hits", "note_hits")


def merge(value: Any) -> dict:
    v = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {k: as_int(v.get(k), DEFAULTS[k], minimum=1) for k in _INT_KEYS}
    raw_depths = v.get("depths") if isinstance(v.get("depths"), dict) else {}
    depths: dict[str, dict[str, int]] = {}
    for depth in DEPTHS:
        raw = raw_depths.get(depth) if isinstance(raw_depths.get(depth), dict) else {}
        depths[depth] = {
            k: as_int(raw.get(k), DEFAULTS["depths"][depth][k], minimum=0) for k in _DEPTH_KEYS
        }
    out["depths"] = depths
    terms = str_list(v.get("academic_terms")) if "academic_terms" in v else None
    out["academic_terms"] = terms if terms is not None else list(DEFAULT_ACADEMIC_TERMS)
    return out


def validate(value: Any) -> dict:
    import re

    if value is not None and not isinstance(value, dict):
        raise ValueError("research_config must be an object")
    v = {**DEFAULTS, **(value or {})}
    out: dict[str, Any] = {}
    out["wait_seconds"] = require_int(v, "wait_seconds", minimum=5, maximum=600)
    out["page_chars"] = require_int(v, "page_chars", minimum=500, maximum=60_000)
    out["report_chars"] = require_int(v, "report_chars", minimum=500, maximum=60_000)
    out["knowledge_hits"] = require_int(v, "knowledge_hits", minimum=0, maximum=50)
    out["note_hits"] = require_int(v, "note_hits", minimum=0, maximum=50)
    raw_depths = v.get("depths")
    if not isinstance(raw_depths, dict):
        raise ValueError("depths must be an object with quick and thorough")
    depths: dict[str, dict[str, int]] = {}
    for depth in DEPTHS:
        raw = {**DEFAULTS["depths"][depth], **(raw_depths.get(depth) or {})}
        if not isinstance(raw_depths.get(depth, {}), dict):
            raise ValueError(f"depths.{depth} must be an object")
        depths[depth] = {k: require_int(raw, k, minimum=0, maximum=50) for k in _DEPTH_KEYS}
    out["depths"] = depths
    terms = require_str_list(v, "academic_terms")
    for t in terms:
        try:
            re.compile(t)
        except re.error as exc:
            raise ValueError(f"academic_terms entry {t!r} is not a valid pattern: {exc}") from exc
    out["academic_terms"] = terms
    return out


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_research_config(pool: Any) -> dict:
    return await ROW.get(pool)


async def save_research_config(pool: Any, value: Any) -> dict:
    return await ROW.save(pool, value)
