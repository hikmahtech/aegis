"""Temporal retry policies for AEGIS v2 workflows."""

from datetime import timedelta

from temporalio.common import RetryPolicy

FAST = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))
STANDARD = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=5))
NO_RETRY = RetryPolicy(maximum_attempts=1)
RETRY_ONCE = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))
# 3 attempts with Temporal's DEFAULT initial_interval (1s grows via the default
# 2.0 backoff). Deliberately NOT `FAST` (FAST pins initial_interval=1s but is a
# distinct policy); this is the shared replacement for the per-flow
# `_ACT_RETRY = RetryPolicy(maximum_attempts=3)` declarations.
ACT_RETRY = RetryPolicy(maximum_attempts=3)

TIMEOUT_FAST = timedelta(seconds=15)
TIMEOUT_STANDARD = timedelta(seconds=60)
TIMEOUT_LLM = timedelta(seconds=180)  # 3 min — local qwen3:14b can be slow under load
TIMEOUT_LONG = timedelta(seconds=300)
TIMEOUT_CHAT_REPLY = timedelta(
    seconds=600
)  # comment-channel synthesize_reply: smart-tier LLM + heavy tools (kimi SSH, deep search)
TIMEOUT_CLAUDE = timedelta(minutes=35)
