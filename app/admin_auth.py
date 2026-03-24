from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds_raw, salt_hex, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except Exception:  # noqa: BLE001
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def make_session_token(user_id: int, email: str, secret: str, hours: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=max(hours, 1))
    payload = {
        "uid": int(user_id),
        "email": email,
        "exp": int(exp.timestamp()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def parse_session_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    exp = int(payload.get("exp") or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if exp <= now_ts:
        return None

    return payload
