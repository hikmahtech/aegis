"""Unit tests for the shared ``verify_hmac`` helper.

The three signed webhook routes (GitHub, Sentry, Todoist) share one
constant-time HMAC-SHA256 verifier. These tests lock the per-scheme
contract directly on the helper so a future refactor can't silently
weaken signature checking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from aegis.api.routes.webhooks import verify_hmac


def _hexdigest(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _b64digest(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_github_scheme_accepts_prefixed_signature():
    body = b'{"action": "opened"}'
    header = "sha256=" + _hexdigest("s3cr3t", body)
    assert verify_hmac("s3cr3t", body, header) is True


def test_github_scheme_rejects_unprefixed_signature():
    body = b"{}"
    # Bare hexdigest (no sha256= prefix) must fail under the default prefix.
    assert verify_hmac("s3cr3t", body, _hexdigest("s3cr3t", body)) is False


def test_bare_scheme_accepts_unprefixed_signature():
    """Sentry sends a bare HEX digest — prefix='' verifies it."""
    body = b'{"event_name": "item:added"}'
    header = _hexdigest("shh", body)
    assert verify_hmac("shh", body, header, prefix="") is True


def test_todoist_scheme_accepts_base64_signature():
    """Todoist sends a bare BASE64 digest, not hex (confirmed against
    Todoist's own webhook docs) — prefix='', encoding='base64'."""
    body = b'{"event_name": "note:added"}'
    header = _b64digest("shh", body)
    assert verify_hmac("shh", body, header, prefix="", encoding="base64") is True


def test_todoist_scheme_rejects_hex_signature():
    """Regression guard: a correctly-keyed HEX digest must NOT verify under
    encoding='base64' — locks in the fix for the hex/base64 mismatch that
    made every real Todoist webhook delivery fail signature verification."""
    body = b'{"event_name": "note:added"}'
    header = _hexdigest("shh", body)
    assert verify_hmac("shh", body, header, prefix="", encoding="base64") is False


def test_missing_header_rejected_for_every_scheme():
    body = b"{}"
    assert verify_hmac("s3cr3t", body, None) is False
    assert verify_hmac("s3cr3t", body, None, prefix="") is False
    assert verify_hmac("s3cr3t", body, "", prefix="") is False


def test_wrong_secret_rejected():
    body = b'{"x": 1}'
    header = "sha256=" + _hexdigest("right", body)
    assert verify_hmac("wrong", body, header) is False


def test_tampered_body_rejected():
    header = "sha256=" + _hexdigest("s3cr3t", b'{"x": 1}')
    assert verify_hmac("s3cr3t", b'{"x": 2}', header) is False
