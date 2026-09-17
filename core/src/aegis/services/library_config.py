"""Library limits — the `library_config` settings row.

What `services/library.py` used to carry as module constants: how much of a
book one read returns, how passage search is sized, how research uses the
library. Edited on Admin → Research → Library limits
(`GET/PUT /api/admin/research/library-config`). The defaults are the old
constants' values.

    {
      "read_chars": 12000,                   # what library_read returns by default
      "passages": 4,                         # best windows a query returns
      "passage_chars": 1200,                 # about this many characters each
      "pdf_default_pages": 5,                # a PDF read with no pages/section/query
      "research_book_hits": 3,               # books ResearchFlow considers
      "research_passage_min_similarity": 0.5,# how close the best must be to be read
      "research_passage_chars": 3000,        # characters of passage handed to synthesis
      "research_pdf_scan_pages": 60,         # PDF pages research scans for a passage
      "stopwords": [...]                     # words passage search ignores
    }

Kept in code: the tool schema caps (`MAX_READ_CHARS` 40,000, `PDF_MAX_SPAN`
30 pages) so the generated schema cannot drift, and the size and page caps
of the Calibre connector, which are Integrations keys.
"""

from __future__ import annotations

from typing import Any

from aegis.services.config_rows import (
    SettingsRow,
    as_float,
    as_int,
    require_float,
    require_int,
    require_str_list,
    str_list,
)

SETTINGS_KEY = "library_config"

DEFAULT_STOPWORDS = [
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "what",
    "which", "when", "how", "why", "who", "whom", "into", "about", "have", "has", "had",
    "not", "but", "you", "your", "our", "their", "its", "can", "will", "would", "could",
    "should", "than", "then", "them", "they", "there", "these", "those", "also", "more",
    "most", "such", "does", "did", "doing", "been", "being", "over", "under", "between",
    "book", "books", "chapter",
]  # fmt: skip

DEFAULTS: dict[str, Any] = {
    "read_chars": 12_000,
    "passages": 4,
    "passage_chars": 1_200,
    "pdf_default_pages": 5,
    "research_book_hits": 3,
    "research_passage_min_similarity": 0.5,
    "research_passage_chars": 3_000,
    "research_pdf_scan_pages": 60,
    "stopwords": list(DEFAULT_STOPWORDS),
}
_INT_KEYS = (
    "read_chars",
    "passages",
    "passage_chars",
    "pdf_default_pages",
    "research_book_hits",
    "research_passage_chars",
    "research_pdf_scan_pages",
)


def merge(value: Any) -> dict:
    v = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {k: as_int(v.get(k), DEFAULTS[k], minimum=1) for k in _INT_KEYS}
    out["research_passage_min_similarity"] = as_float(
        v.get("research_passage_min_similarity"),
        DEFAULTS["research_passage_min_similarity"],
        minimum=0.0,
    )
    words = str_list(v.get("stopwords")) if "stopwords" in v else None
    out["stopwords"] = [w.lower() for w in words] if words is not None else list(DEFAULT_STOPWORDS)
    return out


def validate(value: Any) -> dict:
    if value is not None and not isinstance(value, dict):
        raise ValueError("library_config must be an object")
    v = {**DEFAULTS, **(value or {})}
    out: dict[str, Any] = {
        "read_chars": require_int(v, "read_chars", minimum=500, maximum=40_000),
        "passages": require_int(v, "passages", minimum=1, maximum=20),
        "passage_chars": require_int(v, "passage_chars", minimum=200, maximum=10_000),
        "pdf_default_pages": require_int(v, "pdf_default_pages", minimum=1, maximum=30),
        "research_book_hits": require_int(v, "research_book_hits", minimum=0, maximum=20),
        "research_passage_min_similarity": require_float(
            v, "research_passage_min_similarity", minimum=0.0, maximum=1.0
        ),
        "research_passage_chars": require_int(v, "research_passage_chars", minimum=200, maximum=20_000),
        "research_pdf_scan_pages": require_int(v, "research_pdf_scan_pages", minimum=1, maximum=300),
    }
    out["stopwords"] = [w.lower() for w in require_str_list(v, "stopwords")]
    return out


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_library_config(pool: Any) -> dict:
    return await ROW.get(pool)


async def save_library_config(pool: Any, value: Any) -> dict:
    return await ROW.save(pool, value)
