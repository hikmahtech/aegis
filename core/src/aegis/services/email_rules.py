"""Email triage rules — your personal sender verdicts and notification markers.

Everything here is DB-owned so a fork ships nobody's mailbox. Stored in the
``settings`` table under ``email_triage_rules`` and merged over the (empty)
defaults below; edited on the admin **Email triage** page via
``GET/PUT /api/admin/email/triage-rules`` (``routes/email_admin.py``), mirroring
the Todoist gtd-rules/content-routes pair.

The generic ``PUT /api/settings/{key}`` editor can also reach this row, but only
once the row exists — it has no create control — and it validates nothing, so a
mistyped category vanishes silently. Prefer the dedicated page.

    {
      "sender_overrides": {
        "@substack.com": "informational",
        "no-reply@accounts.google.com": "important_read",
        "billing@bank.example": {"category": "important_read", "tags": ["financial"]}
      },
      "extra_notification_markers": ["incorrect login attempt", "account unlocked"]
    }

``sender_overrides`` is checked FIRST in ``classify_email`` — ahead of the
per-sender cache and the LLM — and deliberately does not write ``triage_state``:
an override must stop applying the moment you delete it, not live on as learned
sender state. A rule value is either a bare category string or an object also
carrying content ``tags``: an override skips the LLM, so it produces no tags of
its own, and the ``MoneyProcessFlow`` fan-out keys on ``financial``/``payments``
— silencing a biller used to silently stop its receipt extraction (#263).
``extra_notification_markers`` extend the shared ``_NOTIFICATION_MARKERS`` list
that caps courtesy notifications out of ``important_action``.

Lives in core (a worker dependency) so the worker classifier and the admin API
share one definition, matching ``services/gtd_rules.py``.
"""

from __future__ import annotations

from typing import Any

SETTINGS_KEY = "email_triage_rules"

CATEGORIES = ("important_action", "important_read", "informational", "useless")

# Both deliberately empty: the open-source default must carry no personal
# senders and no mailbox-specific phrasing. Yours live in the DB row.
DEFAULT_SENDER_OVERRIDES: dict[str, dict] = {}
DEFAULT_EXTRA_NOTIFICATION_MARKERS: list[str] = []


def _normalise_override(value: Any) -> dict | None:
    """One rule value — either shape — as ``{"category", "tags"}``, or None if
    the category is unusable. Lenient by design; ``validate`` is the loud half.
    """
    if isinstance(value, dict):
        category = str(value.get("category") or "")
        raw_tags = value.get("tags")
        tags = (
            [str(t).strip().lower() for t in raw_tags if isinstance(t, str) and t.strip()]
            if isinstance(raw_tags, list)
            else []
        )
    else:
        category, tags = str(value), []
    return {"category": category, "tags": tags} if category in CATEGORIES else None


def merge(value: dict | None) -> dict:
    """A stored (possibly partial) override merged over the defaults.

    Unknown categories are dropped rather than raising — a typo in one sender
    entry must not take the whole ruleset (and with it every classification)
    down. Keys, tags and markers are normalised to lowercase because all three
    are matched against lowercased input.
    """
    v = value or {}
    overrides = {}
    for addr, cat in (v.get("sender_overrides") or {}).items():
        key = str(addr).strip().lower()
        rule = _normalise_override(cat)
        if key and rule:
            overrides[key] = rule
    markers = [
        str(m).strip().lower() for m in (v.get("extra_notification_markers") or []) if str(m).strip()
    ]
    return {
        "sender_overrides": {**DEFAULT_SENDER_OVERRIDES, **overrides},
        "extra_notification_markers": [*DEFAULT_EXTRA_NOTIFICATION_MARKERS, *markers],
    }


def validate(value: dict | None) -> dict:
    """Strict counterpart to ``merge``, for the WRITE path only.

    ``merge`` is deliberately lenient — a malformed row must never be what stops
    email being classified. That leniency is wrong at the trust boundary: it
    means a mistyped category is silently dropped and the author is told
    nothing, which is the exact silent-degradation failure this whole subsystem
    was fixed for. So the admin PUT validates loudly and the classifier reads
    forgivingly.

    Raises ValueError with a human-readable message; the route turns it into a 400.
    """
    v = value or {}
    overrides = v.get("sender_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("sender_overrides must be an object of {sender: category}")
    for addr, cat in overrides.items():
        if not str(addr).strip():
            raise ValueError("sender_overrides has an entry with an empty sender")
        category = cat
        if isinstance(cat, dict):
            category = cat.get("category")
            tags = cat.get("tags") or []
            if not isinstance(tags, list) or not all(
                isinstance(t, str) and t.strip() for t in tags
            ):
                raise ValueError(
                    f"tags for '{addr}' must be a list of non-empty strings"
                )
        if str(category) not in CATEGORIES:
            raise ValueError(
                f"'{category}' is not a valid category for '{addr}' "
                f"— use one of: {', '.join(CATEGORIES)}"
            )
    markers = v.get("extra_notification_markers") or []
    if not isinstance(markers, list):
        raise ValueError("extra_notification_markers must be a list of strings")
    for m in markers:
        # A blank entry is an empty row left behind in the editor, not a
        # mistake that changes meaning — `merge` drops it and we say nothing.
        # A non-string IS malformed: it would be dropped too, but silently,
        # and the author would never learn their marker does nothing.
        if not isinstance(m, str):
            raise ValueError(
                f"extra_notification_markers entries must be strings, got {type(m).__name__}"
            )
    return merge(v)


async def get_email_rules(pool: Any) -> dict:
    """The effective rules: DB override (settings.email_triage_rules) over defaults."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return merge(row["value"] if row and row["value"] else {})


async def save_email_rules(pool: Any, rules: dict) -> dict:
    """Validate then persist. Returns the merged result. Raises ValueError on bad input."""
    normalised = validate(rules)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        normalised,
    )
    return await get_email_rules(pool)


async def known_senders(pool: Any, limit: int = 200) -> list[dict]:
    """Senders AEGIS has actually seen, newest-reputation first.

    The page offers these as pick-from options so a rule is written against a
    real address rather than a typed-from-memory one, and shows `skips_llm` —
    a sender at n>=3 and confidence>=0.75 short-circuits the classifier
    entirely, so a wrong verdict there cannot self-correct and is precisely
    what an override is for.
    """
    rows = await pool.fetch(
        """
        SELECT email_addr,
               state,
               COALESCE((metadata->>'n')::int, 0)              AS n,
               COALESCE((metadata->>'confidence')::float, 0.0) AS confidence
        FROM triage_state
        ORDER BY (state = 'important_action') DESC, n DESC, email_addr
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "email_addr": r["email_addr"],
            "state": r["state"],
            "n": r["n"],
            "confidence": round(float(r["confidence"]), 2),
            "skips_llm": r["n"] >= 3 and float(r["confidence"]) >= 0.75,
        }
        for r in rows
    ]


def match_sender_override(overrides: dict[str, dict], sender: str) -> dict | None:
    """The matching rule (``{"category", "tags"}``) or None.

    Exact address wins over the domain, so one sender can be pulled out of a
    blanket domain rule. `sender` must already be a bare lowercased address
    (``_normalize_sender``). Values are the normalised shape ``merge`` produces.
    """
    addr = (sender or "").strip().lower()
    if not addr:
        return None
    if addr in overrides:
        return overrides[addr]
    domain = addr.rpartition("@")[2]
    if not domain:
        return None
    # Accept both "@example.com" and "example.com" as domain keys — the leading
    # @ reads better in the settings JSON and is the shape people type.
    return overrides.get(f"@{domain}") or overrides.get(domain)
