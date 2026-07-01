"""Channel adapters for the AEGIS comms seam.

Slack is the only channel — the dormant Telegram branch was removed (Telegram is
blocked in India / retired). `comms.__main__` constructs `SlackAdapter` directly;
the channel-neutral types (`DeliveryRef`, `SendResult`, `CardSpec`) in `base`
remain the shared surface a future channel would re-use.
"""

from __future__ import annotations
