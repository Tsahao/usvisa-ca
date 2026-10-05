"""Workflow-independent Telegram delivery. No configuration or retry side effects."""

import math
import re
from dataclasses import dataclass
from enum import Enum

import requests


class DeliveryStatus(str, Enum):
    SENT = "sent"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class TelegramDeliveryResult:
    status: DeliveryStatus
    reason: str
    retry_after: int | None = None

    @property
    def delivered(self):
        return self.status == DeliveryStatus.SENT


def send_telegram_message(
    text, *, bot_token, chat_id, timeout=(10, 20), session=None
):
    """Make one verified HTTPS request; return only sanitized delivery information.

    Callers own retries, dry-run policy, formatting, logging, and alert state.
    Read timeouts and broken connections can mean the message was delivered.
    """
    if not isinstance(bot_token, str) or not re.fullmatch(
        r"[0-9]+:[A-Za-z0-9_-]+", bot_token
    ):
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Missing or invalid Telegram bot token"
        )
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool) or not str(chat_id).strip():
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Missing Telegram chat ID"
        )
    try:
        text_length = len(text.encode("utf-16-le")) // 2
    except (AttributeError, UnicodeError):
        text_length = 0
    if not 1 <= text_length <= 4096:
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Message must contain 1-4096 characters"
        )
    timeouts = timeout if isinstance(timeout, tuple) else (timeout,)
    if (isinstance(timeout, tuple) and len(timeout) != 2) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in timeouts
    ):
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Timeout must be finite and positive"
        )

    client = session if session is not None else requests
    try:
        response = client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": str(chat_id).strip(), "text": text},
            timeout=timeout,
            allow_redirects=False,
            verify=True,
        )
    except requests.exceptions.SSLError:
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "HTTPS certificate verification failed"
        )
    except requests.exceptions.ConnectTimeout:
        return TelegramDeliveryResult(
            DeliveryStatus.RETRYABLE_FAILURE, "Connection timed out before delivery"
        )
    except requests.exceptions.RequestException:
        # Never return str(exc): Requests exceptions can contain the token URL.
        return TelegramDeliveryResult(
            DeliveryStatus.UNCERTAIN, "Network failure; delivery could not be confirmed"
        )

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    parameters = payload.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 1:
        retry_after = None
    error_code = payload.get("error_code")
    if response.status_code == 429 or error_code == 429:
        return TelegramDeliveryResult(
            DeliveryStatus.RETRYABLE_FAILURE, "Telegram rate limit", retry_after
        )
    if response.status_code >= 500 or (
        isinstance(error_code, int) and error_code >= 500
    ):
        return TelegramDeliveryResult(
            DeliveryStatus.RETRYABLE_FAILURE, "Telegram server unavailable"
        )
    if 300 <= response.status_code < 500:
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Telegram rejected the destination or request"
        )
    result = payload.get("result")
    if (
        response.status_code == 200
        and payload.get("ok") is True
        and isinstance(result, dict)
        and isinstance(result.get("message_id"), int)
        and not isinstance(result["message_id"], bool)
    ):
        return TelegramDeliveryResult(DeliveryStatus.SENT, "Message delivered")
    if payload.get("ok") is False:
        return TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Telegram rejected the destination or request"
        )
    return TelegramDeliveryResult(
        DeliveryStatus.UNCERTAIN, "Unexpected response; delivery could not be confirmed"
    )
