"""Symmetric encryption for secrets stored in the DB (Phase A — BYO keys).

Uses Fernet with a key derived from ``AEGIS_SECRET_KEY``. When no key is set,
values are stored plaintext (the single-user self-hosted default) — each stored
secret carries an ``encrypted`` flag so decryption does the right thing either
way, and turning encryption on later only affects newly-saved secrets.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet(secret_key: str) -> Fernet | None:
    if not secret_key:
        return None
    digest = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str, secret_key: str) -> dict:
    """Return a stored-secret dict ``{value, encrypted}``. Encrypts when a key is set."""
    if not plaintext:
        return {"value": "", "encrypted": False}
    f = _fernet(secret_key)
    if f is None:
        return {"value": plaintext, "encrypted": False}
    return {"value": f.encrypt(plaintext.encode()).decode(), "encrypted": True}


def decrypt_secret(stored: dict | None, secret_key: str) -> str:
    """Inverse of :func:`encrypt_secret`. Returns "" when absent or undecryptable."""
    if not stored or not stored.get("value"):
        return ""
    if not stored.get("encrypted"):
        return str(stored["value"])
    f = _fernet(secret_key)
    if f is None:
        return ""
    try:
        return f.decrypt(str(stored["value"]).encode()).decode()
    except Exception:
        return ""
