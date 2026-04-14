from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from core.phone_auth import normalize_nepal_phone

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5, 20)

# GSM single-segment rough cap; keeps OTP and short transactional messages safe.
MAX_TEXT_LEN = 480


class AakashSmsError(Exception):
    """Aakash SMS API returned an error or the message was not accepted."""


def _parse_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        snippet = (resp.text or "")[:500]
        logger.error("Aakash SMS: non-JSON response HTTP %s: %s", resp.status_code, snippet)
        raise AakashSmsError("Invalid response from SMS provider.") from None


def _ascii_digits(value: str) -> str:
    return "".join(c for c in (value or "") if c in "0123456789")


def _recipient_format_retryable(message: str) -> bool:
    m = (message or "").lower()
    return "valid recipient" in m or "no valid" in m


def _post_sms(*, url: str, token: str, to_field: str, body: str) -> None:
    try:
        resp = requests.post(
            url,
            data={"auth_token": token, "to": to_field, "text": body},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.exception("Aakash SMS request failed: %s", e)
        raise AakashSmsError("SMS request failed.") from e

    payload = _parse_json(resp)

    if not resp.ok:
        msg = _message_from_payload(payload) or f"HTTP {resp.status_code}"
        logger.error("Aakash SMS HTTP error: %s", msg)
        raise AakashSmsError(msg)

    if isinstance(payload, dict) and payload.get("error") is True:
        msg = _message_from_payload(payload) or "SMS provider rejected the request."
        logger.error("Aakash SMS error: %s", msg)
        raise AakashSmsError(msg)

    if not isinstance(payload, dict) or payload.get("error") is not False:
        logger.error("Aakash SMS unexpected payload: %s", payload)
        raise AakashSmsError("Unexpected response from SMS provider.")

    data = payload.get("data")
    if isinstance(data, dict):
        invalid = data.get("invalid") or []
        valid = data.get("valid") or []
        if invalid and not valid:
            logger.error("Aakash SMS: no valid recipients; invalid=%s", invalid)
            raise AakashSmsError(
                _message_from_payload(payload) or "SMS could not be delivered to this number."
            )


def send_sms(*, to: str, text: str) -> None:
    """
    Send one SMS via Aakash SMS v3.

    `to` is normalized to Nepal's 10-digit mobile (same rules as `normalize_nepal_phone`).
    If the gateway rejects the national format with a recipient error, one retry uses
    ``977`` + national (13 digits), which some deployments accept.
    """
    token = (getattr(settings, "AAKASHSMS_AUTH_TOKEN", None) or "").strip()
    if not token:
        raise AakashSmsError("SMS is not configured (missing AAKASHSMS_AUTH_TOKEN).")

    raw = (to or "").strip()
    body = (text or "").strip()
    if not raw or not body:
        raise AakashSmsError("Missing phone or message text.")
    if len(body) > MAX_TEXT_LEN:
        body = body[:MAX_TEXT_LEN]

    to_canon = normalize_nepal_phone(raw) or normalize_nepal_phone(_ascii_digits(raw))
    if not to_canon:
        raise AakashSmsError("Invalid Nepal mobile number.")

    url = (getattr(settings, "AAKASHSMS_API_URL", None) or "").strip() or (
        "https://sms.aakashsms.com/sms/v3/send"
    )

    try:
        _post_sms(url=url, token=token, to_field=to_canon, body=body)
    except AakashSmsError as first_err:
        if not _recipient_format_retryable(str(first_err)):
            raise
        intl = f"977{to_canon}"
        logger.warning(
            "Aakash SMS national format rejected (%s); retrying with intl format ***%s",
            first_err,
            to_canon[-4:],
        )
        _post_sms(url=url, token=token, to_field=intl, body=body)


def _message_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        m = payload.get("message")
        if isinstance(m, str) and m.strip():
            return m.strip()
    return ""
