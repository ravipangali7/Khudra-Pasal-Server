"""
NCHL ConnectIPS redirect gateway (production).
Loads credentials from PaymentGatewaySettings (gateway=connectips).
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from requests.auth import HTTPBasicAuth

from core.models import PaymentGatewaySettings

CONNECTIPS_PRODUCTION_BASE = "https://login.connectips.com"


class ConnectIPSConfigError(Exception):
    pass


def connectips_settings_row() -> PaymentGatewaySettings | None:
    return PaymentGatewaySettings.objects.filter(
        gateway=PaymentGatewaySettings.Gateway.CONNECTIPS
    ).first()


def _extras(row: PaymentGatewaySettings | None) -> dict[str, Any]:
    if row and isinstance(row.gateway_extras, dict):
        return row.gateway_extras
    return {}


def get_connectips_config(row: PaymentGatewaySettings | None = None) -> dict[str, Any]:
    """Dict consumed by NCHLConnectIPS."""
    row = row or connectips_settings_row()
    extras = _extras(row)
    if not row:
        return {
            "merchant_id": "",
            "app_id": "",
            "app_name": "",
            "app_password": "",
            "pfx_path": None,
            "pfx_password": "",
            "base_url": CONNECTIPS_PRODUCTION_BASE,
            "enabled": False,
            "minimum_payment_amount": Decimal("100.00"),
        }
    pfx_path = row.certificate.path if row.certificate else None
    min_raw = extras.get("minimum_payment_amount")
    try:
        minimum = Decimal(str(min_raw)) if min_raw not in (None, "") else Decimal("100.00")
    except Exception:
        minimum = Decimal("100.00")
    base_url = (extras.get("base_url") or CONNECTIPS_PRODUCTION_BASE).strip().rstrip("/")
    return {
        "merchant_id": (row.merchant_id or "").strip(),
        "app_id": (row.api_key_live or row.api_key_test or "").strip(),
        "app_name": (row.merchant_name or "").strip(),
        "app_password": (row.secret_key_live or row.secret_key_test or "").strip(),
        "pfx_path": pfx_path,
        "pfx_password": str(extras.get("pfx_password") or ""),
        "base_url": base_url or CONNECTIPS_PRODUCTION_BASE,
        "enabled": bool(row.is_enabled),
        "minimum_payment_amount": minimum,
    }


def connectips_is_configured(row: PaymentGatewaySettings | None = None) -> bool:
    cfg = get_connectips_config(row)
    if not cfg["enabled"]:
        return False
    if not all([cfg["merchant_id"], cfg["app_id"], cfg["app_name"], cfg["app_password"]]):
        return False
    pfx = cfg.get("pfx_path")
    return bool(pfx and os.path.exists(pfx))


def connectips_status_payload(row: PaymentGatewaySettings | None = None) -> dict[str, Any]:
    row = row or connectips_settings_row()
    return {
        "gateway": PaymentGatewaySettings.Gateway.CONNECTIPS,
        "is_enabled": bool(row and row.is_enabled),
        "is_configured": connectips_is_configured(row),
        "minimum_payment_amount": float(get_connectips_config(row)["minimum_payment_amount"]),
    }


def amount_to_paisa(amount: Decimal) -> int:
    return int(amount * 100)


class NCHLConnectIPS:
    def __init__(
        self,
        merchant_id=None,
        app_id=None,
        app_name=None,
        app_password=None,
        pfx_path=None,
        pfx_password=None,
        base_url=None,
    ):
        cfg = get_connectips_config()
        self.merchant_id = merchant_id or cfg["merchant_id"]
        self.app_id = app_id or cfg["app_id"]
        self.app_name = app_name or cfg["app_name"]
        self.app_password = app_password or cfg["app_password"]
        self.pfx_path = pfx_path or cfg["pfx_path"]
        self.pfx_password = pfx_password if pfx_password is not None else cfg["pfx_password"]
        self.base_url = (base_url or cfg["base_url"] or CONNECTIPS_PRODUCTION_BASE).rstrip("/")
        self._private_key = None

        if not all([self.merchant_id, self.app_id, self.app_name, self.app_password]):
            raise ConnectIPSConfigError(
                "ConnectIPS credentials incomplete. Configure Payment Gateway → ConnectIPS in admin."
            )
        if not self.pfx_path or not os.path.exists(self.pfx_path):
            raise ConnectIPSConfigError(f"ConnectIPS PFX certificate not found: {self.pfx_path}")

    def _load_private_key(self):
        if self._private_key is None:
            with open(self.pfx_path, "rb") as f:
                pfx_data = f.read()
            private_key_obj, _, _ = pkcs12.load_key_and_certificates(
                pfx_data,
                (self.pfx_password or "").encode(),
                backend=default_backend(),
            )
            if private_key_obj is None:
                raise ConnectIPSConfigError(
                    "Failed to extract private key from PFX. Check PFX password in gateway settings."
                )
            self._private_key = private_key_obj
        return self._private_key

    def _sign_message(self, message: str) -> str:
        private_key = self._load_private_key()
        signature = private_key.sign(
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def build_payment_token_message(
        self,
        txn_id,
        txn_date,
        txn_amt_paisa,
        reference_id,
        remarks="",
        particulars="",
        txn_crncy="NPR",
    ) -> str:
        return (
            f"MERCHANTID={self.merchant_id},"
            f"APPID={self.app_id},"
            f"APPNAME={self.app_name},"
            f"TXNID={txn_id},"
            f"TXNDATE={txn_date},"
            f"TXNCRNCY={txn_crncy},"
            f"TXNAMT={str(txn_amt_paisa)},"
            f"REFERENCEID={reference_id},"
            f"REMARKS={remarks},"
            f"PARTICULARS={particulars},"
            f"TOKEN=TOKEN"
        )

    def generate_payment_token(
        self,
        txn_id,
        txn_date,
        txn_amt_paisa,
        reference_id,
        remarks="",
        particulars="",
        txn_crncy="NPR",
    ) -> str:
        message = self.build_payment_token_message(
            txn_id,
            txn_date,
            txn_amt_paisa,
            reference_id,
            remarks,
            particulars,
            txn_crncy,
        )
        return self._sign_message(message)

    def get_payment_form_data(
        self,
        txn_id,
        amount_paisa: int,
        reference_id,
        remarks="",
        particulars="",
        txn_date=None,
    ) -> dict:
        if txn_date is None:
            txn_date = datetime.now().strftime("%d-%m-%Y")
        token = self.generate_payment_token(
            txn_id,
            txn_date,
            amount_paisa,
            reference_id,
            remarks,
            particulars,
        )
        return {
            "MERCHANTID": self.merchant_id,
            "APPID": self.app_id,
            "APPNAME": self.app_name,
            "TXNID": txn_id,
            "TXNDATE": txn_date,
            "TXNCRNCY": "NPR",
            "TXNAMT": str(amount_paisa),
            "REFERENCEID": reference_id,
            "REMARKS": remarks,
            "PARTICULARS": particulars,
            "TOKEN": token,
            "gateway_url": f"{self.base_url}/connectipswebgw/loginpage",
        }

    def validate_transaction(self, reference_id: str, txn_amt_paisa: int) -> dict:
        message = (
            f"MERCHANTID={self.merchant_id},"
            f"APPID={self.app_id},"
            f"REFERENCEID={reference_id},"
            f"TXNAMT={txn_amt_paisa}"
        )
        token = self._sign_message(message)
        request_body = {
            "merchantId": int(self.merchant_id),
            "appId": self.app_id,
            "referenceId": reference_id,
            "txnAmt": int(txn_amt_paisa),
            "token": token,
        }
        url = f"{self.base_url}/connectipswebws/api/creditor/validatetxn"
        response = requests.post(
            url,
            json=request_body,
            auth=HTTPBasicAuth(self.app_id, self.app_password),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_transaction_details(self, reference_id: str, txn_amt_paisa: int) -> dict:
        message = (
            f"MERCHANTID={self.merchant_id},"
            f"APPID={self.app_id},"
            f"REFERENCEID={reference_id},"
            f"TXNAMT={txn_amt_paisa}"
        )
        token = self._sign_message(message)
        request_body = {
            "merchantId": int(self.merchant_id),
            "appId": self.app_id,
            "referenceId": reference_id,
            "txnAmt": int(txn_amt_paisa),
            "token": token,
        }
        url = f"{self.base_url}/connectipswebws/api/creditor/gettxndetail"
        response = requests.post(
            url,
            json=request_body,
            auth=HTTPBasicAuth(self.app_id, self.app_password),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


def extract_connectips_txn_id_from_query(query_string: str) -> str:
    """Parse TXNID / txn_id from query string (handles malformed ConnectIPS redirects)."""
    import re
    from urllib.parse import parse_qs, unquote

    if not query_string:
        return ""
    raw = query_string.lstrip("?")
    for part in raw.split("?"):
        if "=" in part:
            raw = part
            break
    params = parse_qs(raw, keep_blank_values=True)
    for key in ("TXNID", "txn_id", "txnId"):
        vals = params.get(key) or params.get(key.lower())
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    decoded = unquote(raw)
    m = re.search(r"(?:TXNID|txn_id)\s*=\s*([A-Z0-9-]+)", decoded, re.I)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"(TXN-[A-Z0-9]+)", decoded, re.I)
    if m2:
        return m2.group(1).upper()
    return ""
