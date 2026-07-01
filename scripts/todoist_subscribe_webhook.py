"""Print instructions for setting up the Todoist webhook.

Todoist deprecated programmatic webhook subscription in 2025 — the old
POST /sync/v9/webhooks endpoint returns 410 and there is no v1 equivalent.
Webhooks now must be configured via the Todoist Developer Console:

  https://developer.todoist.com/appconsole.html

This script prints the URL + secret + event list a user needs to paste
into that UI. It does not make any HTTP calls.

Usage:
    python scripts/todoist_subscribe_webhook.py \\
        --url https://aegis-api.example.com/api/webhooks/todoist \\
        --secret <YOUR_HMAC_SECRET>

The secret must match AEGIS_TODOIST_WEBHOOK_SECRET in the deployed Core env.

Without webhooks, AEGIS still works — TodoistSyncFlow polls every 5 minutes
via /api/v1/sync, so phone-side changes propagate within that window.
Configuring webhooks via the developer console upgrades that to sub-second
latency.

Two gotchas confirmed by live testing (2026-06-30), both silent until fixed:
- Saving a webhook URL in the console is NOT enough — Todoist only delivers
  to an app that's connected to your account (normally via OAuth). For a
  single-user setup, generate a Test Token for the app in the console (it
  bypasses the OAuth redirect) — confirmed live: zero delivery attempts
  reached AEGIS, not even a failed one, until a Test Token existed.
- Of the three values the console shows per app (Client ID, Client Secret,
  Verification Token), the one that must equal AEGIS_TODOIST_WEBHOOK_SECRET
  is the Client Secret — NOT the Verification Token, despite that field's
  name. Confirmed by testing both: Verification Token produced real
  deliveries that failed signature verification; Client Secret verified.
"""

from __future__ import annotations

import argparse
import sys

_EVENTS = [
    "item:added",
    "item:updated",
    "item:completed",
    "item:uncompleted",
    "item:deleted",
    "project:added",
    "project:updated",
    "project:deleted",
    "note:added",
    "note:updated",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Print Todoist webhook setup instructions")
    parser.add_argument("--url", required=True, help="Public webhook URL AEGIS exposes")
    parser.add_argument(
        "--secret",
        required=True,
        help="HMAC secret (must match AEGIS_TODOIST_WEBHOOK_SECRET in Core env)",
    )
    args = parser.parse_args()

    print(
        "Todoist Webhook Setup\n"
        "=====================\n"
        "\n"
        "Open the Todoist Developer Console:\n"
        "  https://developer.todoist.com/appconsole.html\n"
        "\n"
        "Create (or pick an existing) app, then under Webhook settings:\n"
        f"  Webhook URL: {args.url}\n"
        f"  Client Secret (use THIS for AEGIS_TODOIST_WEBHOOK_SECRET, not\n"
        f"  the Verification Token field): {args.secret}\n"
        "\n"
        "Then generate a Test Token for the app (separate button/section in\n"
        "the console) — without it Todoist will accept the webhook config but\n"
        "never actually deliver anything, with no error shown anywhere.\n"
        "\n"
        "Subscribe to these events (Todoist may name them differently in the UI):\n"
        + "\n".join(f"  - {e}" for e in _EVENTS)
        + "\n\n"
        "AEGIS verifies the X-Todoist-Hmac-SHA256 header on every delivery,\n"
        "so the secret you paste above MUST match AEGIS_TODOIST_WEBHOOK_SECRET\n"
        "in the deployed Core service env (managed via the infra-gitops\n"
        "ansible/group_vars/all.yml vault under aegis_todoist_webhook_secret).\n"
        "\n"
        "Without webhooks: AEGIS polls /api/v1/sync every 5 minutes, so this\n"
        "step is optional. Webhooks just upgrade propagation to sub-second.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
