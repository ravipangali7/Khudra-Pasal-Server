"""Wallet top-up via eSewa / Khalti: pending PaymentTransaction, callback credit by target wallet."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from core.models import (
    FamilyGroup,
    FamilyMember,
    PaymentGatewaySettings,
    PaymentTransaction,
    User,
    Vendor,
    Wallet,
    WalletTransaction,
)
from core.services import family_portal_wallet_service, family_service, wallet_service
from core.services.base import get_or_create_personal_wallet
from core.services.khalti_epayment_service import (
    KhaltiApiError,
    KhaltiConfigError,
    extract_lookup_status,
    extract_total_amount_paisa,
    initiate_wallet_topup,
    lookup_payment,
    map_khalti_status_to_app,
    rupees_to_paisa,
)

ESEWA_SIGNED_FIELD_NAMES = "total_amount,transaction_uuid,product_code"

TOPUP_TARGET_CUSTOMER = "customer_personal"
TOPUP_TARGET_FAMILY_MASTER = "family_master"
TOPUP_TARGET_CHILD = "child"
TOPUP_TARGET_VENDOR = "vendor"
TOPUP_TARGET_ADMIN_PERSONAL = "admin_personal"


def _family_groups_for_parent_user(user: User) -> list[FamilyGroup]:
    led = list(FamilyGroup.objects.filter(leader=user, status=FamilyGroup.Status.ACTIVE))
    member_groups = list(
        FamilyMember.objects.filter(user=user, status=FamilyMember.Status.ACTIVE).select_related("group")
    )
    groups = {g.id: g for g in led}
    for fm in member_groups:
        groups[fm.group_id] = fm.group
    out = list(groups.values())
    out.sort(key=lambda g: (1 if g.is_platform_hub else 0, g.id))
    return out


def primary_family_group_for_parent(user: User) -> FamilyGroup | None:
    gl = _family_groups_for_parent_user(user)
    if not gl:
        return None
    for g in gl:
        if family_service.user_can_manage_family_invites(user, g):
            return g
    return gl[0]


def esewa_payment_settings() -> tuple[str, str, str, str]:
    row = PaymentGatewaySettings.objects.filter(gateway=PaymentGatewaySettings.Gateway.ESEWA).first()
    extras: dict = row.gateway_extras if row and isinstance(row.gateway_extras, dict) else {}

    def pick_secret() -> str:
        if row:
            if row.environment == PaymentGatewaySettings.Environment.LIVE:
                s = (row.secret_key_live or row.secret_key_test or "").strip()
            else:
                s = (row.secret_key_test or row.secret_key_live or "").strip()
            if s:
                return s
        sk = (getattr(settings, "ESEWA_EPAY_SECRET_KEY", "") or "").strip()
        return sk or "8gBm/:&EnhH.1/q"

    product_code = (row.merchant_id.strip() if row and (row.merchant_id or "").strip() else "") or (
        getattr(settings, "ESEWA_EPAY_PRODUCT_CODE", "") or "EPAYTEST"
    ).strip()
    secret_key = pick_secret()
    form_url = (extras.get("form_url") or "").strip() or (
        getattr(settings, "ESEWA_EPAY_FORM_URL", "") or "https://rc-epay.esewa.com.np/api/epay/main/v2/form"
    ).strip()
    status_url_base = (extras.get("status_url_base") or "").strip() or (
        getattr(settings, "ESEWA_EPAY_STATUS_URL_BASE", "")
        or "https://rc.esewa.com.np/api/epay/transaction/status/"
    ).strip()
    return product_code, secret_key, form_url, status_url_base


def esewa_amount_str(amount: Decimal) -> str:
    return f"{amount:.2f}"


def esewa_signature(message: str, secret_key: str) -> str:
    return base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")


def esewa_parse_callback_payload(request: HttpRequest) -> dict:
    raw = (
        request.data.get("data")
        or request.query_params.get("data")
        or request.data.get("payload")
        or request.query_params.get("payload")
        or ""
    )
    if not raw:
        raise ValueError("Missing eSewa callback payload.")
    try:
        decoded = base64.b64decode(str(raw)).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:
        raise ValueError("Invalid eSewa callback payload.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid eSewa callback payload.")
    return payload


def esewa_verify_callback_signature(payload: Mapping, secret_key: str) -> bool:
    signed_field_names = str(payload.get("signed_field_names") or "").strip()
    if not signed_field_names:
        return False
    parts = [x.strip() for x in signed_field_names.split(",") if x.strip()]
    if not parts:
        return False
    msg = ",".join(f"{k}={payload.get(k, '')}" for k in parts)
    expected = esewa_signature(msg, secret_key)
    received = str(payload.get("signature") or "")
    return hmac.compare_digest(expected, received)


def esewa_status_verify(
    *, status_url_base: str, product_code: str, total_amount: str, transaction_uuid: str
) -> dict:
    qs = urlencode(
        {
            "product_code": product_code,
            "total_amount": total_amount,
            "transaction_uuid": transaction_uuid,
        }
    )
    url = f"{status_url_base}?{qs}"
    with urlopen(url, timeout=8) as res:
        raw = res.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid status response")
    return data


def khalti_customer_info(user: User) -> dict[str, str]:
    phone = (user.phone or "").strip() or "9800000000"
    email = (user.email or "").strip()
    if not email:
        email = f"u{user.pk}@wallet.khalti.local"
    name = (user.name or "").strip() or "Customer"
    return {"name": name[:120], "email": email[:120], "phone": phone[:15]}


def khalti_website_url() -> str:
    return (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/") or "http://localhost:8080"


def khalti_return_url_for_path(return_path: str, *, extra_query: str | None = None) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/") or "http://localhost:8080"
    path = return_path.strip() if return_path.strip().startswith("/") else f"/{return_path.strip()}"
    q = "khalti_wallet=1"
    if extra_query:
        q = f"{q}&{extra_query}" if not extra_query.startswith("&") else f"khalti_wallet=1{extra_query}"
    return f"{base}{path}?{q}"


def esewa_frontend_redirect_url_for_row(
    request: HttpRequest,
    row: PaymentTransaction,
    *,
    status: str,
    txn_ref: str | None = None,
) -> str:
    gw = row.gateway_response if isinstance(row.gateway_response, dict) else {}
    path = (gw.get("return_path") or "/portal/wallet").strip()
    if not path.startswith("/"):
        path = "/" + path
    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    q: dict[str, str] = {"esewa": status}
    if txn_ref:
        q["txn_ref"] = txn_ref
    extra = gw.get("return_query_esewa")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None and str(v) != "":
                q[str(k)] = str(v)
    full_path = f"{path}?{urlencode(q)}"
    if base:
        return f"{base}{full_path}"
    return request.build_absolute_uri(full_path)


def assert_can_topup_wallet(*, payer: User, wallet: Wallet, target: str) -> None:
    if target == TOPUP_TARGET_CUSTOMER:
        pw = get_or_create_personal_wallet(payer)
        if wallet.pk != pw.pk or wallet.owner_id != payer.pk:
            raise ValueError("Invalid customer wallet for top-up.")
        if wallet.type != Wallet.Type.PERSONAL:
            raise ValueError("Customer top-up must target a personal wallet.")
    elif target == TOPUP_TARGET_FAMILY_MASTER:
        primary = primary_family_group_for_parent(payer)
        if not primary:
            raise ValueError("No family group for parent.")
        master = family_portal_wallet_service.get_default_shared_wallet(primary)
        if not master or master.pk != wallet.pk:
            raise ValueError("Invalid family master wallet for top-up.")
    elif target == TOPUP_TARGET_CHILD:
        fm = (
            FamilyMember.objects.filter(
                user=payer,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            )
            .select_related("group")
            .first()
        )
        if not fm:
            raise ValueError("Child membership not found.")
        cw = family_portal_wallet_service.get_member_family_wallet(fm.group, payer)
        if not cw or cw.pk != wallet.pk:
            raise ValueError("Invalid child wallet for top-up.")
    elif target == TOPUP_TARGET_VENDOR:
        v = Vendor.objects.filter(user=payer).select_related("wallet").first()
        if not v or not v.wallet_id or v.wallet_id != wallet.pk:
            raise ValueError("Invalid vendor wallet for top-up.")
    elif target == TOPUP_TARGET_ADMIN_PERSONAL:
        pw = get_or_create_personal_wallet(payer)
        if wallet.pk != pw.pk or wallet.owner_id != payer.pk:
            raise ValueError("Invalid admin personal wallet.")
        if wallet.type != Wallet.Type.PERSONAL:
            raise ValueError("Admin top-up must target a personal wallet.")
    else:
        raise ValueError("Unknown top-up target.")


def resolve_credit_wallet_for_topup(row: PaymentTransaction) -> Wallet:
    gw = row.gateway_response if isinstance(row.gateway_response, dict) else {}
    wid = gw.get("wallet_id")
    target = str(gw.get("topup_target") or TOPUP_TARGET_CUSTOMER)
    if wid:
        w = Wallet.objects.filter(pk=str(wid)).select_related("owner", "vendor", "family_group").first()
        if not w:
            raise ValueError("Wallet not found for payment.")
        assert_can_topup_wallet(payer=row.customer, wallet=w, target=target)
        if w.status != Wallet.Status.ACTIVE:
            raise ValueError("Wallet is not active.")
        return w
    w = get_or_create_personal_wallet(row.customer)
    if w.status != Wallet.Status.ACTIVE:
        raise ValueError("Wallet is not active.")
    return w


def topup_description_for_method(method: str) -> str:
    m = (method or "").lower()
    if m == "esewa":
        return "Wallet top-up (eSewa)"
    if m == "khalti":
        return "Wallet top-up (Khalti)"
    return f"Wallet top-up ({method or 'gateway'})"


@transaction.atomic
def credit_wallet_for_completed_topup(
    row: PaymentTransaction,
    wallet: Wallet,
    *,
    gateway_response_patch: dict[str, Any],
) -> WalletTransaction:
    row_locked = PaymentTransaction.objects.select_for_update().get(pk=row.pk)
    if row_locked.status == PaymentTransaction.Status.SUCCESS and row_locked.wallet_transaction_id:
        return row_locked.wallet_transaction  # type: ignore[return-value]
    w = Wallet.objects.select_for_update().get(pk=wallet.pk)
    if w.status != Wallet.Status.ACTIVE:
        raise ValueError("Wallet is not active")
    wt = wallet_service.credit_wallet(
        w,
        row_locked.amount,
        wtype=WalletTransaction.Type.TOPUP,
        description=topup_description_for_method(row_locked.method),
        performed_by=row_locked.customer,
    )
    wallet_service.apply_topup_bonus_after_credit(
        w,
        row_locked.amount,
        bonus_reference_id=wt.txn_id,
        performed_by=row_locked.customer,
    )
    row_locked.wallet_transaction = wt
    row_locked.status = PaymentTransaction.Status.SUCCESS
    row_locked.gateway_response = {**(row_locked.gateway_response or {}), **gateway_response_patch}
    row_locked.verified_at = timezone.now()
    row_locked.save(
        update_fields=["wallet_transaction", "status", "gateway_response", "verified_at"]
    )
    return wt


def gateway_response_base(
    *,
    wallet: Wallet,
    method: str,
    topup_target: str,
    return_path: str,
    return_query_esewa: dict[str, str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "wallet_topup",
        "wallet_id": str(wallet.pk),
        "topup_target": topup_target,
        "method": method,
        "return_path": (return_path or "/portal/wallet").strip()
        if (return_path or "/portal/wallet").strip().startswith("/")
        else f"/{(return_path or '/portal/wallet').strip().lstrip('/')}",
    }
    if return_query_esewa:
        out["return_query_esewa"] = return_query_esewa
    return out


def build_esewa_initiate_response(
    *,
    request: HttpRequest,
    payer: User,
    wallet: Wallet,
    amount: Decimal,
    method: str,
    topup_target: str,
    return_path: str,
    return_query_esewa: dict[str, str] | None,
    success_reverse_name: str,
    failure_reverse_name: str,
) -> dict[str, Any]:
    from django.urls import reverse

    product_code, secret_key, form_url, _status_url_base = esewa_payment_settings()
    txn_uuid = str(uuid4())
    total_amount = esewa_amount_str(amount)
    message = f"total_amount={total_amount},transaction_uuid={txn_uuid},product_code={product_code}"
    signature = esewa_signature(message, secret_key)
    success_url = request.build_absolute_uri(reverse(success_reverse_name))
    failure_url = request.build_absolute_uri(reverse(failure_reverse_name))
    gw = gateway_response_base(
        wallet=wallet,
        method=method,
        topup_target=topup_target,
        return_path=return_path,
        return_query_esewa=return_query_esewa,
    )
    gw["esewa_init"] = {
        "total_amount": total_amount,
        "transaction_uuid": txn_uuid,
        "product_code": product_code,
    }
    PaymentTransaction.objects.create(
        txn_ref=txn_uuid,
        customer=payer,
        amount=amount,
        method=PaymentTransaction.Method.ESEWA,
        status=PaymentTransaction.Status.PENDING,
        gateway_response=gw,
    )
    return {
        "ok": True,
        "flow": "esewa_redirect",
        "action_url": form_url,
        "fields": {
            "amount": total_amount,
            "tax_amount": "0",
            "product_service_charge": "0",
            "product_delivery_charge": "0",
            "total_amount": total_amount,
            "transaction_uuid": txn_uuid,
            "product_code": product_code,
            "success_url": success_url,
            "failure_url": failure_url,
            "signed_field_names": ESEWA_SIGNED_FIELD_NAMES,
            "signature": signature,
        },
    }


def build_khalti_initiate_response(
    *,
    payer: User,
    wallet: Wallet,
    amount: Decimal,
    method: str,
    topup_target: str,
    return_path: str,
    return_query_esewa: dict[str, str] | None,
    purchase_order_id: str,
    purchase_order_name: str,
) -> dict[str, Any]:
    amount_paisa = rupees_to_paisa(amount)
    if amount_paisa < 100:
        raise ValueError("amount too small for Khalti (minimum Rs. 1)")
    init_data = initiate_wallet_topup(
        amount_paisa=amount_paisa,
        purchase_order_id=purchase_order_id,
        purchase_order_name=purchase_order_name,
        return_url=khalti_return_url_for_path(return_path),
        website_url=khalti_website_url(),
        customer_info=khalti_customer_info(payer),
    )
    pidx = str(init_data.get("pidx") or "").strip()
    payment_url = str(init_data.get("payment_url") or "").strip()
    if not pidx or not payment_url:
        raise ValueError("Khalti initiate response missing pidx or payment_url.")
    gw = gateway_response_base(
        wallet=wallet,
        method=method,
        topup_target=topup_target,
        return_path=return_path,
        return_query_esewa=return_query_esewa,
    )
    gw["khalti_pidx"] = pidx
    gw["khalti_purchase_order_id"] = purchase_order_id
    gw["khalti_initiate"] = init_data
    PaymentTransaction.objects.create(
        txn_ref=purchase_order_id,
        customer=payer,
        amount=amount,
        method=PaymentTransaction.Method.KHALTI,
        status=PaymentTransaction.Status.PENDING,
        gateway_response=gw,
    )
    return {
        "ok": True,
        "flow": "khalti_redirect",
        "payment_url": payment_url,
        "pidx": pidx,
        "purchase_order_id": purchase_order_id,
        "expires_at": init_data.get("expires_at"),
        "expires_in": init_data.get("expires_in"),
    }


def _legacy_portal_wallet_esewa_redirect(
    request: HttpRequest, status: str, txn_ref: str | None = None
) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    path = f"/portal/wallet?{urlencode({'esewa': status, **({'txn_ref': txn_ref} if txn_ref else {})})}"
    if base:
        return f"{base}{path}"
    return request.build_absolute_uri(path)


def _parse_decimal(v, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def handle_esewa_wallet_topup_success(request: HttpRequest) -> str:
    """Verify eSewa callback and credit wallet; return frontend redirect URL."""
    product_code, secret_key, _form_url, status_url_base = esewa_payment_settings()
    txn_ref = ""
    row: PaymentTransaction | None = None
    try:
        payload = esewa_parse_callback_payload(request)
        txn_ref = str(payload.get("transaction_uuid") or "").strip()
        if not txn_ref:
            raise ValueError("Missing transaction_uuid")
        row = PaymentTransaction.objects.select_related("customer").filter(
            txn_ref=txn_ref,
            method=PaymentTransaction.Method.ESEWA,
        ).first()
        if not row:
            raise ValueError("Payment transaction not found")
        if row.status == PaymentTransaction.Status.SUCCESS and row.wallet_transaction_id:
            return esewa_frontend_redirect_url_for_row(request, row, status="success", txn_ref=txn_ref)
        if not esewa_verify_callback_signature(payload, secret_key):
            raise ValueError("Invalid callback signature")
        if str(payload.get("status") or "").upper() != "COMPLETE":
            raise ValueError("Payment not complete")
        callback_total = esewa_amount_str(_parse_decimal(payload.get("total_amount"), "0"))
        expected_total = esewa_amount_str(row.amount)
        callback_product = str(payload.get("product_code") or "").strip()
        if callback_total != expected_total:
            raise ValueError("Amount mismatch")
        if callback_product != product_code:
            raise ValueError("Product code mismatch")
        status_data = esewa_status_verify(
            status_url_base=status_url_base,
            product_code=product_code,
            total_amount=expected_total,
            transaction_uuid=txn_ref,
        )
        if str(status_data.get("status") or "").upper() != "COMPLETE":
            raise ValueError("Status verification failed")
        w = resolve_credit_wallet_for_topup(row)
        credit_wallet_for_completed_topup(
            row,
            w,
            gateway_response_patch={
                "esewa_callback_payload": dict(payload),
                "esewa_status_verify": status_data,
            },
        )
        return esewa_frontend_redirect_url_for_row(request, row, status="success", txn_ref=txn_ref)
    except Exception as exc:
        if txn_ref:
            failed = PaymentTransaction.objects.filter(
                txn_ref=txn_ref, method=PaymentTransaction.Method.ESEWA
            ).first()
            if failed and failed.status == PaymentTransaction.Status.PENDING:
                failed.status = PaymentTransaction.Status.FAILED
                failed.gateway_response = {
                    **(failed.gateway_response or {}),
                    "esewa_callback_error": str(exc),
                }
                failed.save(update_fields=["status", "gateway_response"])
        if row:
            return esewa_frontend_redirect_url_for_row(request, row, status="failed", txn_ref=txn_ref or None)
        return _legacy_portal_wallet_esewa_redirect(request, "failed", txn_ref=txn_ref or None)


def handle_esewa_wallet_topup_failure(request: HttpRequest) -> str:
    txn_ref = (
        str(request.query_params.get("transaction_uuid") or request.data.get("transaction_uuid") or "")
        .strip()
    )
    row = None
    if txn_ref:
        row = PaymentTransaction.objects.filter(
            txn_ref=txn_ref,
            method=PaymentTransaction.Method.ESEWA,
            status=PaymentTransaction.Status.PENDING,
        ).first()
        if row:
            row.status = PaymentTransaction.Status.FAILED
            row.gateway_response = {
                **(row.gateway_response or {}),
                "esewa_failure_callback": {"query": dict(request.query_params)},
            }
            row.save(update_fields=["status", "gateway_response"])
    if row:
        return esewa_frontend_redirect_url_for_row(request, row, status="failed", txn_ref=txn_ref or None)
    return _legacy_portal_wallet_esewa_redirect(request, "failed", txn_ref=txn_ref or None)


def khalti_wallet_topup_verify_payload(*, user: User, pidx: str) -> tuple[dict, int]:
    """Returns (response_body, http_status)."""
    if not pidx:
        return ({"detail": "pidx is required", "field": "pidx"}, 400)
    row = (
        PaymentTransaction.objects.filter(
            customer=user,
            method=PaymentTransaction.Method.KHALTI,
            gateway_response__khalti_pidx=pidx,
            gateway_response__kind="wallet_topup",
        )
        .order_by("-created_at")
        .first()
    )
    if not row:
        return ({"detail": "Payment not found for this Khalti session."}, 404)
    try:
        lookup_data = lookup_payment(pidx=pidx)
    except KhaltiConfigError as e:
        return ({"detail": str(e)}, 503)
    except KhaltiApiError as e:
        return (
            {"detail": str(e), "khalti_error": (e.body or "")[:2000]},
            502,
        )
    khalti_status = extract_lookup_status(lookup_data)
    app_status = map_khalti_status_to_app(khalti_status)
    total_paisa = extract_total_amount_paisa(lookup_data)
    expected_paisa = rupees_to_paisa(row.amount)
    txn_id = str(lookup_data.get("transaction_id") or "")

    if total_paisa is not None and total_paisa != expected_paisa:
        return (
            {
                "success": False,
                "detail": "Amount mismatch with Khalti lookup.",
                "data": {
                    "status": "ERROR",
                    "khalti_status": khalti_status,
                    "pidx": pidx,
                    "expected_total_amount": expected_paisa,
                    "lookup_total_amount": total_paisa,
                },
            },
            400,
        )

    if app_status == "SUCCESS":
        if row.status == PaymentTransaction.Status.SUCCESS and row.wallet_transaction_id:
            return (
                {
                    "success": True,
                    "data": {
                        "status": "SUCCESS",
                        "khalti_status": khalti_status,
                        "pidx": pidx,
                        "transaction_id": txn_id,
                        "total_amount": total_paisa or expected_paisa,
                    },
                },
                200,
            )
        try:
            w = resolve_credit_wallet_for_topup(row)
            credit_wallet_for_completed_topup(
                row,
                w,
                gateway_response_patch={"khalti_lookup": lookup_data},
            )
        except ValueError as e:
            return ({"detail": str(e)}, 400)
        return (
            {
                "success": True,
                "data": {
                    "status": "SUCCESS",
                    "khalti_status": khalti_status,
                    "pidx": pidx,
                    "transaction_id": txn_id,
                    "total_amount": total_paisa or expected_paisa,
                },
            },
            200,
        )

    if app_status == "FAILED":
        if row.status == PaymentTransaction.Status.PENDING:
            row.status = PaymentTransaction.Status.FAILED
            row.gateway_response = {**(row.gateway_response or {}), "khalti_lookup": lookup_data}
            row.save(update_fields=["status", "gateway_response"])
        return (
            {
                "success": True,
                "data": {
                    "status": "FAILED",
                    "khalti_status": khalti_status,
                    "pidx": pidx,
                    "transaction_id": txn_id,
                    "total_amount": total_paisa or expected_paisa,
                },
            },
            200,
        )

    if app_status == "PENDING":
        return (
            {
                "success": True,
                "data": {
                    "status": "PENDING",
                    "khalti_status": khalti_status,
                    "pidx": pidx,
                    "transaction_id": txn_id,
                    "total_amount": total_paisa or expected_paisa,
                },
            },
            200,
        )

    if row.status == PaymentTransaction.Status.PENDING:
        row.status = PaymentTransaction.Status.FAILED
        row.gateway_response = {**(row.gateway_response or {}), "khalti_lookup": lookup_data}
        row.save(update_fields=["status", "gateway_response"])
    return (
        {
            "success": True,
            "data": {
                "status": "ERROR",
                "khalti_status": khalti_status,
                "pidx": pidx,
                "transaction_id": txn_id,
                "total_amount": total_paisa or expected_paisa,
            },
        },
        200,
    )
