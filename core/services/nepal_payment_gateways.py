"""eSewa (ePay v2) and Khalti ePayment helpers — sandbox/test endpoints only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
import requests
from django.conf import settings


def esewa_signature(secret_key: str, *, total_amount: str, transaction_uuid: str, product_code: str) -> str:
    """HMAC-SHA256 base64 per eSewa Epay v2 (signed_field_names order)."""
    message = f"total_amount={total_amount},transaction_uuid={transaction_uuid},product_code={product_code}"
    digest = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def esewa_build_form_fields(
    *,
    grand_total: Decimal,
    transaction_uuid: str,
    product_code: str,
    secret_key: str,
    success_url: str,
    failure_url: str,
    form_action: str,
) -> tuple[str, dict[str, str]]:
    """Returns (form_action_url, flat form fields for POST)."""
    total_s = f"{grand_total.quantize(Decimal('0.01')):.2f}"
    signature = esewa_signature(
        secret_key,
        total_amount=total_s,
        transaction_uuid=transaction_uuid,
        product_code=product_code,
    )
    fields = {
        "amount": total_s,
        "tax_amount": "0",
        "total_amount": total_s,
        "transaction_uuid": transaction_uuid,
        "product_code": product_code,
        "product_service_charge": "0",
        "product_delivery_charge": "0",
        "success_url": success_url,
        "failure_url": failure_url,
        "signed_field_names": "total_amount,transaction_uuid,product_code",
        "signature": signature,
    }
    return form_action, fields


def esewa_fetch_status(
    *,
    product_code: str,
    total_amount: Decimal,
    transaction_uuid: str,
    status_url: str | None = None,
    timeout: int = 25,
) -> dict[str, Any]:
    """GET transaction status from eSewa (test: rc.esewa.com.np)."""
    base = (status_url or getattr(settings, "ESEWA_STATUS_URL", "")).strip()
    if not base:
        raise ValueError("ESEWA_STATUS_URL is not configured.")
    total_s = f"{total_amount.quantize(Decimal('0.01')):.2f}"
    url = base
    if not url.endswith("/"):
        url = url + "/"
    # API accepts query params
    params = {
        "product_code": product_code,
        "total_amount": total_s,
        "transaction_uuid": transaction_uuid,
    }
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"raw": r.text}


def esewa_status_is_complete(payload: dict[str, Any]) -> bool:
    st = payload.get("status")
    if st is None and "data" in payload:
        inner = payload.get("data")
        if isinstance(inner, dict):
            st = inner.get("status")
    if isinstance(st, str):
        return st.upper() in ("COMPLETE", "COMPLETED")
    return False


def khalti_initiate_payment(
    *,
    return_url: str,
    website_url: str,
    amount_paisa: int,
    purchase_order_id: str,
    purchase_order_name: str,
    secret_key: str,
    initiate_url: str | None = None,
    customer_info: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /epayment/initiate/ — returns JSON including pidx and payment_url."""
    url = (initiate_url or getattr(settings, "KHALTI_INITIATE_URL", "")).strip()
    if not url:
        raise ValueError("KHALTI_INITIATE_URL is not configured.")
    headers = {
        "Authorization": f"Key {secret_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "return_url": return_url,
        "website_url": website_url,
        "amount": amount_paisa,
        "purchase_order_id": purchase_order_id[:48],
        "purchase_order_name": purchase_order_name[:255],
    }
    if customer_info:
        body["customer_info"] = customer_info
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except json.JSONDecodeError:
            detail = r.text
        raise ValueError(f"Khalti initiate failed: {detail}")
    return r.json()


def khalti_lookup_payment(*, pidx: str, secret_key: str, lookup_url: str | None = None, timeout: int = 30) -> dict[str, Any]:
    url = (lookup_url or getattr(settings, "KHALTI_LOOKUP_URL", "")).strip()
    if not url:
        raise ValueError("KHALTI_LOOKUP_URL is not configured.")
    headers = {
        "Authorization": f"Key {secret_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json={"pidx": pidx}, timeout=timeout)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except json.JSONDecodeError:
            detail = r.text
        raise ValueError(f"Khalti lookup failed: {detail}")
    return r.json()


def rupees_to_khalti_paisa(amount: Decimal) -> int:
    """Khalti amounts are integer paisa (Rs 1 = 100)."""
    p = (amount.quantize(Decimal("0.01")) * Decimal(100)).quantize(Decimal("1"))
    return int(p)


def decode_esewa_redirect_data(data_b64: str) -> dict[str, Any]:
    raw = base64.b64decode(data_b64).decode("utf-8")
    return json.loads(raw)
