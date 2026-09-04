"""Telegram Mini App authentication and short-lived authorization tickets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class MiniAppAuthError(ValueError):
    """Raised when Telegram Web App data or an authorization ticket is invalid."""


@dataclass(frozen=True)
class TelegramWebUser:
    user_id: int
    username: str = ""
    first_name: str = ""


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 3600,
    now: int | None = None,
) -> TelegramWebUser:
    """Validate Telegram's official Web App ``initData`` HMAC signature."""
    if not init_data or not bot_token:
        raise MiniAppAuthError("Telegram authentication data is missing.")

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash or len(received_hash) != 64:
        raise MiniAppAuthError("Telegram authentication signature is missing.")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise MiniAppAuthError("Telegram authentication signature is invalid.")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MiniAppAuthError("Telegram authentication data is malformed.") from exc

    current_time = int(time.time() if now is None else now)
    if auth_date > current_time + 60 or current_time - auth_date > max_age_seconds:
        raise MiniAppAuthError("Telegram authentication data has expired.")
    if user_id <= 0:
        raise MiniAppAuthError("Telegram user ID is invalid.")

    return TelegramWebUser(
        user_id=user_id,
        username=str(user.get("username") or ""),
        first_name=str(user.get("first_name") or ""),
    )


def issue_ticket(
    user: TelegramWebUser,
    session_secret: str,
    *,
    ttl_seconds: int = 900,
    now: int | None = None,
) -> str:
    """Create a short-lived, signed backend ticket without storing session data."""
    if not session_secret:
        raise MiniAppAuthError("Mini App session signing is not configured.")
    issued_at = int(time.time() if now is None else now)
    payload = {
        "user_id": user.user_id,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    encoded_payload = _urlsafe_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        session_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded_payload}.{signature}"


def verify_ticket(
    ticket: str,
    session_secret: str,
    *,
    now: int | None = None,
) -> int:
    """Verify a ticket and return only its Telegram user ID."""
    if not ticket or not session_secret:
        raise MiniAppAuthError("Authorization ticket is missing.")
    try:
        encoded_payload, received_signature = ticket.split(".", 1)
        payload = json.loads(_urlsafe_decode(encoded_payload))
        user_id = int(payload["user_id"])
        expires_at = int(payload["exp"])
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise MiniAppAuthError("Authorization ticket is malformed.") from exc

    expected_signature = hmac.new(
        session_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received_signature, expected_signature):
        raise MiniAppAuthError("Authorization ticket is invalid.")
    current_time = int(time.time() if now is None else now)
    if user_id <= 0 or expires_at <= current_time:
        raise MiniAppAuthError("Authorization ticket has expired.")
    return user_id