"""POS payment sessions: eSewa redirect and NCHL dynamic QR before order creation."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.db import transaction
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from core.models import Order, PaymentGatewaySettings, PaymentTransaction, PosPaymentSession, User, Vendor
from core.services import nchl_qr_service
from core.services.pos_order_service import create_pos_order
from core.services.wallet_gateway_topup import (
    ESEWA_SIGNED_FIELD_NAMES,
    esewa_amount_str,
    esewa_parse_callback_payload,
    esewa_payment_settings,
    esewa_signature,
    esewa_status_verify,
    esewa_verify_callback_signature,
)


def esewa_is_configured() -> bool:
    row = PaymentGatewaySettings.objects.filter(gateway=PaymentGatewaySettings.Gateway.ESEWA).first()
    if not row or not row.is_enabled:
        return False
    product_code, secret_key, form_url, _ = esewa_payment_settings()
    return bool(product_code and secret_key and form_url)


def pos_payment_methods_status() -> dict[str, Any]:
    return {
        "esewa": {
            "is_enabled": bool(
                PaymentGatewaySettings.objects.filter(
                    gateway=PaymentGatewaySettings.Gateway.ESEWA, is_enabled=True
                ).exists()
            ),
            "is_configured": esewa_is_configured(),
        },
        "nchl_qr": nchl_qr_service.nchl_qr_status_payload(),
    }


def _gen_txn_ref(prefix: str = "POS") -> str:
    return f"{prefix}-{uuid4().hex[:16].upper()}"


def _compute_cart_total(
    *,
    items: list[dict],
    tax_percent: Decimal,
    discount: Decimal,
    acting_vendor: Vendor | None,
) -> Decimal:
    from core.models import Product
    from core.services.product_pricing import effective_unit_price

    subtotal = Decimal("0")
    pids = {int(i.get("product_id")) for i in items if i.get("product_id") is not None}
    qs = Product.objects.filter(pk__in=pids)
    if acting_vendor is not None:
        qs = qs.filter(seller=acting_vendor)
    products = {p.pk: p for p in qs}
    for raw in items:
        pid = int(raw.get("product_id"))
        qty = int(raw.get("quantity") or 0)
        p = products.get(pid)
        if not p or qty < 1:
            raise ValueError(f"Invalid cart line for product {pid}.")
        up_raw = raw.get("unit_price")
        if up_raw is not None and up_raw != "":
            unit_price = Decimal(str(up_raw)).quantize(Decimal("0.01"))
        else:
            unit_price = effective_unit_price(p)
        subtotal += unit_price * qty
    tax_amount = (subtotal * tax_percent / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax_amount - discount).quantize(Decimal("0.01"))
    if total < 0:
        total = Decimal("0")
    return total


def _resolve_pos_customer(raw_cid: Any) -> User:
    from core.views.vendor.common import get_or_create_pos_walkin_user

    if raw_cid in (None, ""):
        return get_or_create_pos_walkin_user()
    try:
        customer_pk = int(str(raw_cid).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("customer_id must be a valid integer") from exc
    customer = User.objects.filter(pk=customer_pk).first()
    if not customer:
        raise ValueError("customer not found")
    return customer


def _session_to_dict(session: PosPaymentSession) -> dict[str, Any]:
    return {
        "session_id": str(session.session_id),
        "payment_method": session.payment_method,
        "status": session.status,
        "amount": float(session.amount),
        "txn_ref": session.txn_ref,
        "qr_payload": session.qr_payload or "",
        "qr_string": session.qr_string or "",
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "order_number": session.order.order_number if session.order_id else None,
    }


@transaction.atomic
def complete_pos_session(session: PosPaymentSession) -> Order:
    session = PosPaymentSession.objects.select_for_update().get(pk=session.pk)
    if session.status == PosPaymentSession.Status.SUCCESS and session.order_id:
        return session.order  # type: ignore[return-value]
    if session.status != PosPaymentSession.Status.PENDING:
        raise ValueError("Payment session is not pending.")

    payload = session.cart_payload if isinstance(session.cart_payload, dict) else {}
    items = payload.get("items") or []
    if not items:
        raise ValueError("Cart payload missing items.")

    customer = _resolve_pos_customer(payload.get("customer_id"))
    tax_percent = Decimal(str(payload.get("tax_percent") or "0"))
    discount = Decimal(str(payload.get("discount") or "0"))
    notes = str(payload.get("notes") or "")[:500]
    acting_vendor = session.vendor

    pm = (
        Order.PaymentMethod.NCHL_QR
        if session.payment_method == PosPaymentSession.Method.NCHL_QR
        else Order.PaymentMethod.ESEWA
    )

    order = create_pos_order(
        acting_vendor=acting_vendor,
        customer=customer,
        items=items,
        payment_method=pm,
        tax_percent=tax_percent,
        discount=discount,
        notes=notes,
    )
    session.status = PosPaymentSession.Status.SUCCESS
    session.order = order
    session.completed_at = timezone.now()
    session.save(update_fields=["status", "order", "completed_at"])

    if session.payment_transaction_id:
        pt = PaymentTransaction.objects.select_for_update().get(pk=session.payment_transaction_id)
        if pt.status != PaymentTransaction.Status.SUCCESS:
            pt.status = PaymentTransaction.Status.SUCCESS
            pt.order = order
            pt.verified_at = timezone.now()
            pt.save(update_fields=["status", "order", "verified_at"])
    return order


@transaction.atomic
def create_nchl_qr_session(
    *,
    request: HttpRequest,
    created_by: User,
    acting_vendor: Vendor | None,
    cart_payload: dict[str, Any],
) -> PosPaymentSession:
    items = cart_payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    tax_percent = Decimal(str(cart_payload.get("tax_percent") or "0"))
    discount = Decimal(str(cart_payload.get("discount") or "0"))
    total = _compute_cart_total(
        items=items,
        tax_percent=tax_percent,
        discount=discount,
        acting_vendor=acting_vendor,
    )
    if total <= 0:
        raise ValueError("Total must be greater than zero.")

    txn_ref = _gen_txn_ref("NQR")
    qr_data = nchl_qr_service.generate_dynamic_qr(
        amount=total,
        reference=txn_ref,
        purpose="KhudraPasal POS sale",
    )

    row = nchl_qr_service.nchl_qr_settings_row()
    expires_at = qr_data.get("expires_at") or (
        timezone.now() + timezone.timedelta(seconds=(row.qr_expiry_seconds if row else 300))
    )

    session = PosPaymentSession.objects.create(
        created_by=created_by,
        vendor=acting_vendor,
        payment_method=PosPaymentSession.Method.NCHL_QR,
        status=PosPaymentSession.Status.PENDING,
        amount=total,
        txn_ref=txn_ref,
        cart_payload=cart_payload,
        qr_payload=str(qr_data.get("qr_payload") or ""),
        qr_string=str(qr_data.get("qr_string") or ""),
        gateway_response=qr_data.get("gateway_response") if isinstance(qr_data.get("gateway_response"), dict) else {},
        expires_at=expires_at,
    )
    PaymentTransaction.objects.create(
        txn_ref=txn_ref,
        customer=created_by,
        amount=total,
        method=PaymentTransaction.Method.BANK_QR,
        status=PaymentTransaction.Status.PENDING,
        gateway_response={
            "kind": "pos_checkout",
            "pos_session_id": str(session.session_id),
            "payment_method": PosPaymentSession.Method.NCHL_QR,
        },
    )
    return session


@transaction.atomic
def create_esewa_pos_session(
    *,
    request: HttpRequest,
    created_by: User,
    acting_vendor: Vendor | None,
    cart_payload: dict[str, Any],
    success_reverse_name: str,
    failure_reverse_name: str,
) -> tuple[PosPaymentSession, dict[str, Any]]:
    items = cart_payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if not esewa_is_configured():
        raise ValueError("eSewa is not configured. Add credentials in Admin → Settings → Payment.")

    tax_percent = Decimal(str(cart_payload.get("tax_percent") or "0"))
    discount = Decimal(str(cart_payload.get("discount") or "0"))
    total = _compute_cart_total(
        items=items,
        tax_percent=tax_percent,
        discount=discount,
        acting_vendor=acting_vendor,
    )
    if total <= 0:
        raise ValueError("Total must be greater than zero.")

    product_code, secret_key, form_url, status_url_base = esewa_payment_settings()
    txn_uuid = str(uuid4())
    total_amount = esewa_amount_str(total)
    message = f"total_amount={total_amount},transaction_uuid={txn_uuid},product_code={product_code}"
    signature = esewa_signature(message, secret_key)
    success_url = request.build_absolute_uri(reverse(success_reverse_name))
    failure_url = request.build_absolute_uri(reverse(failure_reverse_name))

    return_path = "/admin/pos" if acting_vendor is None else "/vendor/pos"
    session = PosPaymentSession.objects.create(
        created_by=created_by,
        vendor=acting_vendor,
        payment_method=PosPaymentSession.Method.ESEWA,
        status=PosPaymentSession.Status.PENDING,
        amount=total,
        txn_ref=txn_uuid,
        cart_payload=cart_payload,
        gateway_response={
            "kind": "pos_checkout",
            "return_path": return_path,
            "esewa_init": {
                "total_amount": total_amount,
                "transaction_uuid": txn_uuid,
                "product_code": product_code,
            },
            "status_url_base": status_url_base,
        },
        expires_at=timezone.now() + timezone.timedelta(minutes=30),
    )
    pt = PaymentTransaction.objects.create(
        txn_ref=txn_uuid,
        customer=created_by,
        amount=total,
        method=PaymentTransaction.Method.ESEWA,
        status=PaymentTransaction.Status.PENDING,
        gateway_response={
            "kind": "pos_checkout",
            "pos_session_id": str(session.session_id),
            "return_path": return_path,
        },
    )
    session.payment_transaction = pt
    session.save(update_fields=["payment_transaction"])

    redirect = {
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
    return session, redirect


def get_session_for_user(session_id: str, user: User, vendor: Vendor | None = None) -> PosPaymentSession:
    session = PosPaymentSession.objects.filter(session_id=session_id).select_related("order").first()
    if not session:
        raise ValueError("Payment session not found.")
    if session.created_by_id != user.pk:
        raise ValueError("Payment session not found.")
    if vendor is not None and session.vendor_id != vendor.pk:
        raise ValueError("Payment session not found.")
    if vendor is None and session.vendor_id is not None:
        raise ValueError("Payment session not found.")
    return session


def refresh_session_status(session: PosPaymentSession) -> PosPaymentSession:
    if session.status != PosPaymentSession.Status.PENDING:
        return session
    if session.expires_at and timezone.now() >= session.expires_at:
        session.status = PosPaymentSession.Status.EXPIRED
        session.save(update_fields=["status"])
        return session

    if session.payment_method == PosPaymentSession.Method.NCHL_QR:
        remote = nchl_qr_service.inquire_transaction_status(reference=session.txn_ref, amount=session.amount)
        if remote == "success":
            complete_pos_session(session)
            session.refresh_from_db()
        elif remote in {"failed", "expired"}:
            session.status = (
                PosPaymentSession.Status.EXPIRED
                if remote == "expired"
                else PosPaymentSession.Status.FAILED
            )
            session.save(update_fields=["status"])
        return session

    if session.payment_method == PosPaymentSession.Method.ESEWA:
        gw = session.gateway_response if isinstance(session.gateway_response, dict) else {}
        init = gw.get("esewa_init") if isinstance(gw.get("esewa_init"), dict) else {}
        status_url_base = str(gw.get("status_url_base") or "")
        product_code = str(init.get("product_code") or "")
        total_amount = str(init.get("total_amount") or esewa_amount_str(session.amount))
        txn_uuid = str(init.get("transaction_uuid") or session.txn_ref)
        if status_url_base and product_code:
            try:
                data = esewa_status_verify(
                    status_url_base=status_url_base,
                    product_code=product_code,
                    total_amount=total_amount,
                    transaction_uuid=txn_uuid,
                )
                st = str(data.get("status") or "").upper()
                if st == "COMPLETE":
                    complete_pos_session(session)
                    session.refresh_from_db()
                elif st in {"NOT_FOUND", "CANCELED", "CANCELLED"}:
                    session.status = PosPaymentSession.Status.FAILED
                    session.save(update_fields=["status"])
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    return session


def handle_esewa_pos_callback(request: HttpRequest, *, success: bool) -> str:
    from django.conf import settings as dj_settings
    from urllib.parse import urlencode

    base = (getattr(dj_settings, "FRONTEND_URL", "") or "").strip().rstrip("/") or "http://localhost:8080"
    txn_ref = ""
    try:
        if success:
            product_code, secret_key, _, _ = esewa_payment_settings()
            payload = esewa_parse_callback_payload(request)
            txn_ref = str(payload.get("transaction_uuid") or "")
            if not esewa_verify_callback_signature(payload, secret_key):
                raise ValueError("Invalid eSewa signature.")
            pt = PaymentTransaction.objects.filter(txn_ref=txn_ref).first()
            if not pt:
                raise ValueError("Transaction not found.")
            gw = pt.gateway_response if isinstance(pt.gateway_response, dict) else {}
            if gw.get("kind") != "pos_checkout":
                raise ValueError("Not a POS payment.")
            session_id = gw.get("pos_session_id")
            session = PosPaymentSession.objects.filter(session_id=session_id).first()
            if not session:
                raise ValueError("POS session not found.")
            complete_pos_session(session)
            return_path = str(gw.get("return_path") or "/admin/pos")
            q = urlencode({"pos_payment": "success", "session_id": str(session.session_id), "order": session.order.order_number if session.order_id else ""})
            return f"{base}{return_path}?{q}"
        payload = {}
        try:
            payload = esewa_parse_callback_payload(request)
            txn_ref = str(payload.get("transaction_uuid") or "")
        except ValueError:
            txn_ref = str(request.query_params.get("transaction_uuid") or request.data.get("transaction_uuid") or "")
        if txn_ref:
            session = PosPaymentSession.objects.filter(txn_ref=txn_ref).first()
            if session and session.status == PosPaymentSession.Status.PENDING:
                session.status = PosPaymentSession.Status.FAILED
                session.save(update_fields=["status"])
            gw = {}
            if session and isinstance(session.gateway_response, dict):
                gw = session.gateway_response
            elif txn_ref:
                pt = PaymentTransaction.objects.filter(txn_ref=txn_ref).first()
                if pt and isinstance(pt.gateway_response, dict):
                    gw = pt.gateway_response
            return_path = str(gw.get("return_path") or "/admin/pos")
            q = urlencode({"pos_payment": "failed", "txn_ref": txn_ref})
            return f"{base}{return_path}?{q}"
    except ValueError:
        q = urlencode({"pos_payment": "failed", "txn_ref": txn_ref})
        return f"{base}/admin/pos?{q}"
    q = urlencode({"pos_payment": "failed"})
    return f"{base}/admin/pos?{q}"


@transaction.atomic
def confirm_demo_nchl_payment(session: PosPaymentSession) -> Order:
    row = nchl_qr_service.nchl_qr_settings_row()
    extras = row.gateway_extras if row and isinstance(row.gateway_extras, dict) else {}
    if extras.get("demo_mode") not in (True, "true", "1", 1):
        raise ValueError("Demo confirm is only available in NCHL demo mode.")
    if session.payment_method != PosPaymentSession.Method.NCHL_QR:
        raise ValueError("Invalid session type.")
    return complete_pos_session(session)
