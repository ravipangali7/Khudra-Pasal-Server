"""NCHL NepalPAY dynamic QR helpers (NPI / ConnectIPS)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from core.models import PaymentGatewaySettings


def nchl_qr_settings_row() -> PaymentGatewaySettings | None:
    return PaymentGatewaySettings.objects.filter(
        gateway=PaymentGatewaySettings.Gateway.NCHL_QR
    ).first()


def _extras(row: PaymentGatewaySettings | None) -> dict[str, Any]:
    if row and isinstance(row.gateway_extras, dict):
        return row.gateway_extras
    return {}


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or getattr(settings, key, "") or default).strip()


def _active_api_key(row: PaymentGatewaySettings) -> str:
    if row.environment == PaymentGatewaySettings.Environment.LIVE:
        return (row.api_key_live or row.api_key_test or _env("NCHL_QR_API_KEY_LIVE")).strip()
    return (row.api_key_test or row.api_key_live or _env("NCHL_QR_API_KEY_TEST")).strip()


def _active_secret(row: PaymentGatewaySettings) -> str:
    if row.environment == PaymentGatewaySettings.Environment.LIVE:
        return (row.secret_key_live or row.secret_key_test or _env("NCHL_QR_SECRET_LIVE")).strip()
    return (row.secret_key_test or row.secret_key_live or _env("NCHL_QR_SECRET_TEST")).strip()


def _active_merchant_id(row: PaymentGatewaySettings) -> str:
    return (row.merchant_id or _env("NCHL_QR_MERCHANT_ID")).strip()


def _active_api_base(row: PaymentGatewaySettings) -> str:
    extras = _extras(row)
    if row.environment == PaymentGatewaySettings.Environment.LIVE:
        base = (extras.get("api_base_url_live") or _env("NCHL_QR_API_BASE_URL_LIVE")).strip()
    else:
        base = (extras.get("api_base_url_test") or _env("NCHL_QR_API_BASE_URL_TEST") or _env("NCHL_QR_API_BASE_URL")).strip()
    return base.rstrip("/")


def nchl_qr_is_configured(row: PaymentGatewaySettings | None = None) -> bool:
    row = row or nchl_qr_settings_row()
    if not row or not row.is_enabled:
        return False
    extras = _extras(row)
    if extras.get("demo_mode") in (True, "true", "1", 1):
        return True
    merchant_id = _active_merchant_id(row)
    api_key = _active_api_key(row)
    secret = _active_secret(row)
    base = _active_api_base(row)
    path = str(extras.get("dynamic_qr_path") or "").strip()
    return bool(merchant_id and api_key and secret and base and path)


def nchl_qr_status_payload(row: PaymentGatewaySettings | None = None) -> dict[str, Any]:
    row = row or nchl_qr_settings_row()
    configured = nchl_qr_is_configured(row)
    return {
        "gateway": PaymentGatewaySettings.Gateway.NCHL_QR,
        "is_enabled": bool(row and row.is_enabled),
        "is_configured": configured,
        "environment": row.environment if row else PaymentGatewaySettings.Environment.TEST,
        "demo_mode": bool(_extras(row).get("demo_mode")) if row else False,
    }


def build_signature(fields: dict[str, Any], signed_field_names: list[str], secret: str) -> str:
    message = ",".join(f"{k}={fields[k]}" for k in signed_field_names)
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _demo_qr_payload(*, amount: str, reference: str, merchant_name: str) -> tuple[str, str]:
    qr_string = f"NEPALPAY|{reference}|{amount}|NPR|{merchant_name or 'KhudraPasal'}"
    try:
        import qrcode

        img = qrcode.make(qr_string)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}", qr_string
    except ImportError:
        return "", qr_string


def _parse_qr_from_response(data: dict[str, Any]) -> tuple[str, str]:
    qr_payload = ""
    for key in ("qr_payload", "qrPayload", "qr_image", "qrImage", "qr_code", "qrCode"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            qr_payload = val.strip()
            if not qr_payload.startswith("data:") and len(qr_payload) > 200:
                qr_payload = f"data:image/png;base64,{qr_payload}"
            break
    qr_string = ""
    for key in ("qr_string", "qrString", "emvco_qr", "emvcoQr", "payload"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            qr_string = val.strip()
            break
    return qr_payload, qr_string


def generate_dynamic_qr(
    *,
    amount: Decimal,
    reference: str,
    purpose: str = "POS sale",
) -> dict[str, Any]:
    row = nchl_qr_settings_row()
    if not row or not row.is_enabled:
        raise ValueError("NCHL QR is not enabled.")
    if not nchl_qr_is_configured(row):
        raise ValueError("NCHL QR is not configured. Add credentials in Admin → Settings → Payment.")

    amount_str = f"{amount.quantize(Decimal('0.01')):.2f}"
    extras = _extras(row)
    if extras.get("demo_mode") in (True, "true", "1", 1):
        qr_payload, qr_string = _demo_qr_payload(
            amount=amount_str,
            reference=reference,
            merchant_name=row.merchant_name,
        )
        return {
            "qr_payload": qr_payload,
            "qr_string": qr_string,
            "gateway_response": {"demo_mode": True, "reference": reference},
            "expires_at": timezone.now() + timezone.timedelta(seconds=row.qr_expiry_seconds or 300),
        }

    merchant_id = _active_merchant_id(row)
    api_key = _active_api_key(row)
    secret = _active_secret(row)
    base = _active_api_base(row)
    path = str(extras.get("dynamic_qr_path") or "").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base}{path}"

    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    signed_names_raw = str(extras.get("signed_field_names") or "amount,reference,merchant_id,timestamp")
    signed_field_names = [x.strip() for x in signed_names_raw.split(",") if x.strip()]

    body: dict[str, Any] = {
        "merchant_id": merchant_id,
        "merchant_name": row.merchant_name or "",
        "amount": amount_str,
        "currency": str(extras.get("currency") or "NPR"),
        "reference": reference,
        "purpose": purpose[:200],
        "timestamp": timestamp,
        "terminal_id": str(extras.get("terminal_id") or "POS-001"),
        "merchant_vpa": str(extras.get("merchant_vpa") or ""),
        "acquirer_id": str(extras.get("acquirer_id") or ""),
        "institution_code": str(extras.get("institution_code") or ""),
        "source_id": str(extras.get("source_id") or ""),
    }
    sign_fields = {k: body[k] for k in signed_field_names if k in body}
    body["signed_field_names"] = ",".join(signed_field_names)
    body["signature"] = build_signature(sign_fields, signed_field_names, secret)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_header = str(extras.get("api_key_header") or "X-API-KEY")
    headers[api_header] = api_key

    resp = requests.post(url, json=body, headers=headers, timeout=20)
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid NCHL QR response (HTTP {resp.status_code}).") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid NCHL QR response payload.")
    if resp.status_code >= 400:
        msg = str(data.get("message") or data.get("detail") or data.get("responseMessage") or resp.text)[:300]
        raise ValueError(f"NCHL QR error: {msg}")

    qr_payload, qr_string = _parse_qr_from_response(data)
    if not qr_payload and not qr_string:
        qr_payload, qr_string = _demo_qr_payload(
            amount=amount_str,
            reference=reference,
            merchant_name=row.merchant_name,
        )
        data = {**data, "fallback_local_qr": True}

    ttl = int(data.get("expires_in") or row.qr_expiry_seconds or 300)
    expires_at = timezone.now() + timezone.timedelta(seconds=max(30, ttl))

    return {
        "qr_payload": qr_payload,
        "qr_string": qr_string,
        "gateway_response": data,
        "expires_at": expires_at,
    }


def inquire_transaction_status(*, reference: str, amount: Decimal) -> str:
    """Return normalized status: pending | success | failed | expired."""
    row = nchl_qr_settings_row()
    if not row:
        return "failed"
    extras = _extras(row)
    if extras.get("demo_mode") in (True, "true", "1", 1):
        return "pending"

    base = _active_api_base(row)
    path = str(extras.get("status_inquiry_path") or "").strip()
    if not base or not path:
        return "pending"
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base}{path}"

    amount_str = f"{amount.quantize(Decimal('0.01')):.2f}"
    merchant_id = _active_merchant_id(row)
    api_key = _active_api_key(row)
    secret = _active_secret(row)
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    signed_names_raw = str(
        extras.get("status_signed_field_names") or extras.get("signed_field_names") or "amount,reference,merchant_id,timestamp"
    )
    signed_field_names = [x.strip() for x in signed_names_raw.split(",") if x.strip()]
    body: dict[str, Any] = {
        "merchant_id": merchant_id,
        "reference": reference,
        "amount": amount_str,
        "timestamp": timestamp,
    }
    sign_fields = {k: body[k] for k in signed_field_names if k in body}
    body["signed_field_names"] = ",".join(signed_field_names)
    body["signature"] = build_signature(sign_fields, signed_field_names, secret)

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_header = str(extras.get("api_key_header") or "X-API-KEY")
    headers[api_header] = api_key

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return "pending"
    if not isinstance(data, dict):
        return "pending"

    raw = str(
        data.get("status")
        or data.get("payment_status")
        or data.get("transactionStatus")
        or data.get("responseCode")
        or ""
    ).lower()
    if raw in {"success", "successful", "complete", "completed", "paid", "00", "000"}:
        return "success"
    if raw in {"failed", "failure", "declined", "cancelled", "canceled", "expired"}:
        return "failed" if raw != "expired" else "expired"
    return "pending"
