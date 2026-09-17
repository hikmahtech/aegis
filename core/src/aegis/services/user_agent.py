"""The one User-Agent AEGIS's bots send.

`AegisBot/2.0 (+<contact>)`, where the contact is the `bot_contact_url`
Integrations key, else AEGIS's public URL (`aegis_public_url`, then
`aegis_ui_url`), else nothing.
A site that wants to know who is fetching it gets a real address rather than
the placeholder `aegis.example.com` the content extractor used to send.
"""

from __future__ import annotations

from typing import Any

PRODUCT = "AegisBot/2.0"


def bot_user_agent(settings: Any = None) -> str:
    contact = ""
    for field in ("bot_contact_url", "aegis_public_url", "aegis_ui_url"):
        contact = str(getattr(settings, field, "") or "").strip() if settings is not None else ""
        if contact:
            break
    return f"{PRODUCT} (+{contact})" if contact else PRODUCT
