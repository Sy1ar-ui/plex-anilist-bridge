"""Admin password and session cookies, stdlib only.

The UI holds tokens to the user's AniList and MAL accounts, so it is not left
open even on a LAN. Until a password is set (first run) the UI is reachable so
that setup can happen at all; the dashboard says so in as many words.

scrypt comes from hashlib and the cookie is a plain HMAC, so this adds no
dependency and nothing here invents its own crypto.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from app import settings_store

COOKIE_NAME = "pab_session"
SESSION_MAX_AGE = 14 * 24 * 3600

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, key_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(actual, expected)


def password_is_set() -> bool:
    return bool(settings_store.get("admin_password_hash"))


def set_password(password: str) -> None:
    settings_store.set("admin_password_hash", hash_password(password))


def _secret() -> bytes:
    value = settings_store.get("session_secret", "")
    if not value:
        value = secrets.token_hex(32)
        settings_store.set("session_secret", value)
    return value.encode("utf-8")


def issue_session() -> str:
    issued = str(int(time.time()))
    signature = hmac.new(_secret(), issued.encode("utf-8"), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{issued}.{token}"


def session_is_valid(cookie: str | None) -> bool:
    if not cookie:
        return False
    try:
        issued, token = cookie.split(".", 1)
    except ValueError:
        return False
    if not issued.isdigit():
        return False
    if time.time() - int(issued) > SESSION_MAX_AGE:
        return False
    expected = hmac.new(_secret(), issued.encode("utf-8"), hashlib.sha256).digest()
    expected_token = base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
    return hmac.compare_digest(token, expected_token)


def webhook_path_token() -> str:
    """The random path segment on the webhook URL.

    Plex cannot send an auth header with a webhook, so the secret has to live in
    the path. It is generated once, on first run.
    """
    value = settings_store.get("webhook_path_token", "")
    if not value:
        value = secrets.token_hex(16)
        settings_store.set("webhook_path_token", value)
    return value


def webhook_token_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate, webhook_path_token())
