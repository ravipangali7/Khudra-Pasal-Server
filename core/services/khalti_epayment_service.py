"""Khalti ePayment (v2) — server-to-server initiate and lookup."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class KhaltiConfigError(RuntimeError):
    pass


class KhaltiApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _khalti_from_payment_gateway_row() -> tuple[str, str]:
    """Returns (secret, api_base_url) from DB row; api_base_url may be empty to mean 'use default'."""
    from core.models import PaymentGatewaySettings

    row = PaymentGatewaySettings.objects.filter(gateway=PaymentGatewaySettings.Gateway.KHALTI).first()
    if not row:
        return "", ""
    extras = row.gateway_extras if isinstance(row.gateway_extras, dict) else {}
    api_base = (extras.get("api_base_url") or "").strip().rstrip("/")
    if row.environment == PaymentGatewaySettings.Environment.LIVE:
        secret = (row.secret_key_live or row.secret_key_test or "").strip()
    else:
        secret = (row.secret_key_test or row.secret_key_live or "").strip()
    return secret, api_base


def _secret_and_base_url() -> tuple[str, str]:
    default_base = (getattr(settings, "KHALTI_BASE_URL", "") or "https://khalti.com/api/v2").strip().rstrip("/")
    secret, api_base = _khalti_from_payment_gateway_row()
    if secret:
        base = api_base if api_base else default_base
        return secret, base
    secret = (getattr(settings, "KHALTI_SECRET_KEY", "") or "").strip()
    return secret, default_base


def require_khalti_secret() -> tuple[str, str]:
    secret, base = _secret_and_base_url()
    if not secret:
        raise KhaltiConfigError(
            "Khalti is not configured (add the secret key under Admin → Settings → Payment, "
            "or set KHALTI_SECRET_KEY in the environment)."
        )
    return secret, base


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    secret, base = require_khalti_secret()
    url = f"{base}{path}"
    if not url.startswith("http"):
        raise KhaltiConfigError("Invalid KHALTI_BASE_URL.")
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Key {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise KhaltiApiError(
            f"Khalti API error: {e.code}",
            status_code=e.code,
            body=err_body,
        ) from e
    except URLError as e:
        raise KhaltiApiError(f"Khalti request failed: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KhaltiApiError("Invalid JSON from Khalti.") from e
    if not isinstance(data, dict):
        raise KhaltiApiError("Unexpected Khalti response shape.")
    return data


def initiate_wallet_topup(
    *,
    amount_paisa: int,
    purchase_order_id: str,
    purchase_order_name: str,
    return_url: str,
    website_url: str,
    customer_info: dict[str, str],
) -> dict[str, Any]:
    payload = {
        "return_url": return_url,
        "website_url": website_url,
        "amount": amount_paisa,
        "purchase_order_id": purchase_order_id,
        "purchase_order_name": purchase_order_name,
        "customer_info": customer_info,
    }
    return _post_json("/epayment/initiate/", payload)


def lookup_payment(*, pidx: str) -> dict[str, Any]:
    return _post_json("/epayment/lookup/", {"pidx": pidx})


def rupees_to_paisa(amount: Decimal) -> int:
    return int((amount * Decimal(100)).quantize(Decimal("1")))


def map_khalti_status_to_app(status_raw: str) -> str:
    s = (status_raw or "").strip()
    if s == "Completed":
        return "SUCCESS"
    if s in ("User canceled", "Expired"):
        return "FAILED"
    if s in ("Pending", "Initiated"):
        return "PENDING"
    if s in ("Refunded", "Partially refunded") or not s:
        return "ERROR"
    return "ERROR"


def extract_lookup_status(data: dict[str, Any]) -> str:
    st = data.get("status")
    if isinstance(st, str):
        return st.strip()
    if isinstance(st, dict):
        n = st.get("name") or st.get("status")
        if isinstance(n, str):
            return n.strip()
    return ""


def extract_total_amount_paisa(data: dict[str, Any]) -> int | None:
    raw = data.get("total_amount")
    try:
        if raw is None:
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None
