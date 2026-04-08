"""Transactional email via SiteSettings SMTP (HTML)."""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string

from core.models import Order, SiteSettings, User
from core.services.kyc_withdraw import latest_kyc_rejection_reason

logger = logging.getLogger(__name__)


def _frontend_url() -> str:
    base = getattr(settings, "FRONTEND_URL", "http://localhost:8080")
    return str(base).rstrip("/")


def _fmt_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _coalesce_env(env_key: str, db_value: str) -> str:
    """Non-empty env overrides DB; unset or empty env keeps DB value."""
    raw = os.environ.get(env_key)
    if raw is not None:
        s = raw.strip()
        if s:
            return s
    return (db_value or "").strip()


def _effective_smtp_password(site: SiteSettings) -> str | None:
    if "KP_SMTP_PASSWORD" in os.environ:
        p = os.environ["KP_SMTP_PASSWORD"].strip()
        if p:
            return p
    raw = (site.smtp_password or "").strip()
    return raw or None


def effective_smtp_host(site: SiteSettings) -> str:
    return _coalesce_env("KP_SMTP_HOST", site.smtp_host)


def smtp_is_configured(site: SiteSettings | None = None) -> bool:
    site = site or SiteSettings.load()
    return bool(effective_smtp_host(site))


def _effective_smtp_port(site: SiteSettings) -> int:
    raw = os.environ.get("KP_SMTP_PORT")
    if raw is not None and raw.strip():
        return int(raw.strip())
    return int(site.smtp_port or 587)


def _from_email_address(site: SiteSettings) -> str:
    name = _coalesce_env("KP_SMTP_FROM_NAME", site.smtp_from_name or "")
    addr = _coalesce_env(
        "KP_SMTP_FROM_EMAIL",
        (site.smtp_from_email or "").strip() or (site.site_email or "").strip(),
    )
    if not addr:
        addr = "noreply@example.com"
    if name:
        return f"{name} <{addr}>"
    return addr


def _smtp_connection(site: SiteSettings):
    host = effective_smtp_host(site)
    if not host:
        return None
    port = _effective_smtp_port(site)
    use_ssl = port == 465
    use_tls = not use_ssl
    username = _coalesce_env("KP_SMTP_USERNAME", site.smtp_username) or None
    password = _effective_smtp_password(site)
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
    )


def send_html_email(
    subject: str,
    html_body: str,
    recipients: list[str],
    *,
    site: SiteSettings | None = None,
    raise_exceptions: bool = False,
) -> None:
    to_list = [e.strip() for e in recipients if e and str(e).strip()]
    if not to_list:
        return
    site = site or SiteSettings.load()
    conn = _smtp_connection(site)
    if conn is None:
        logger.debug("SMTP host not configured; skip email subject=%r", subject)
        return
    from_addr = _from_email_address(site)
    msg = EmailMultiAlternatives(
        subject=subject,
        body="",
        from_email=from_addr,
        to=to_list,
        connection=conn,
    )
    msg.attach_alternative(html_body, "text/html")
    try:
        msg.send()
    except Exception:
        logger.exception("Failed to send email subject=%r to=%r", subject, to_list)
        if raise_exceptions:
            raise


def _delivery_summary(order: Order) -> str:
    try:
        addr = order.delivery_address
    except ObjectDoesNotExist:
        return ""
    parts = [
        addr.full_name,
        addr.mobile,
        addr.area_location,
        (addr.landmark or "").strip(),
        (addr.delivery_notes or "").strip(),
    ]
    return "\n".join(p for p in parts if p)


def _order_line_rows(order: Order) -> list[dict]:
    rows: list[dict] = []
    for oi in order.items.all().order_by("pk"):
        rows.append(
            {
                "name": oi.product.name,
                "quantity": oi.quantity,
                "total": _fmt_money(oi.total_price),
            }
        )
    return rows


def _order_mail_context(order: Order, site: SiteSettings) -> dict:
    lines = _order_line_rows(order)
    delivery_summary = _delivery_summary(order)
    currency = site.currency or "NPR"
    return {
        "site_name": site.site_name or "Khudra Pasal",
        "site_tagline": "",
        "order_number": order.order_number,
        "currency": currency,
        "lines": lines,
        "subtotal": _fmt_money(order.subtotal),
        "delivery_fee": _fmt_money(order.delivery_fee)
        if order.delivery_fee and order.delivery_fee > 0
        else "",
        "discount": _fmt_money(order.discount_amount)
        if order.discount_amount and order.discount_amount > 0
        else "",
        "total": _fmt_money(order.total),
        "delivery_summary": delivery_summary,
    }


def send_order_placed_emails(order_id: int) -> None:
    site = SiteSettings.load()
    if not effective_smtp_host(site):
        logger.debug("SMTP host not set; skip order emails id=%s", order_id)
        return

    order = (
        Order.objects.filter(pk=order_id)
        .select_related("customer", "seller", "seller__user")
        .prefetch_related("items__product")
        .first()
    )
    if not order:
        return

    base_ctx = _order_mail_context(order, site)
    fe = _frontend_url()

    cust = order.customer
    cust_email = (cust.email or "").strip()
    if cust_email:
        ctx = {
            **base_ctx,
            "customer_name": cust.name or cust.get_full_name() or "customer",
            "portal_orders_url": f"{fe}/portal/orders",
        }
        html = render_to_string("mail/order_customer.html", ctx)
        send_html_email(
            f"Order received — {order.order_number}",
            html,
            [cust_email],
            site=site,
        )

    seller = order.seller
    if seller and getattr(seller, "portal_email_notifications", True):
        ve = (seller.contact_email or "").strip() or (
            (seller.user.email or "").strip() if seller.user_id else ""
        )
        if ve:
            ctx = {
                **base_ctx,
                "store_name": seller.store_name,
                "vendor_orders_url": f"{fe}/vendor/all-orders",
            }
            html = render_to_string("mail/order_vendor.html", ctx)
            send_html_email(
                f"New order {order.order_number} — {seller.store_name}",
                html,
                [ve],
                site=site,
            )

    admin_email = (site.site_email or "").strip()
    if admin_email:
        addr = _delivery_summary(order)
        ctx = {
            **base_ctx,
            "store_label": seller.store_name if seller else "In-house",
            "payment_label": order.get_payment_method_display(),
            "customer_name": cust.name or cust.get_full_name() or "",
            "customer_phone": getattr(cust, "phone", "") or "",
            "customer_email": cust_email,
            "delivery_summary": addr,
        }
        html = render_to_string("mail/order_admin.html", ctx)
        send_html_email(
            f"[Admin] New order {order.order_number}",
            html,
            [admin_email],
            site=site,
        )


def _kyc_status_copy(status: str) -> tuple[str, str]:
    labels = {
        User.KYCStatus.PENDING: ("Pending", "Please complete your KYC submission when you are ready."),
        User.KYCStatus.REVIEW: ("Under review", "We are reviewing your documents. You will receive another email when the status changes."),
        User.KYCStatus.VERIFIED: ("Verified", "Your identity verification is complete. Thank you."),
        User.KYCStatus.REJECTED: ("Not approved", "Your KYC submission was not approved. Review the message below and submit updated documents if applicable."),
    }
    return labels.get(
        status,
        (status.replace("_", " ").title(), "Your verification status has been updated."),
    )


def send_kyc_status_change_email(user_id: int) -> None:
    site = SiteSettings.load()
    if not effective_smtp_host(site):
        logger.debug("SMTP host not set; skip KYC email user_id=%s", user_id)
        return
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return
    to_addr = (user.email or "").strip()
    if not to_addr:
        return
    label, message = _kyc_status_copy(user.kyc_status)
    reason = ""
    if user.kyc_status == User.KYCStatus.REJECTED:
        reason = latest_kyc_rejection_reason(user)
    ctx = {
        "site_name": site.site_name or "Khudra Pasal",
        "site_tagline": "",
        "user_name": user.name or user.get_full_name() or "there",
        "status_label": label,
        "status_message": message,
        "rejection_reason": reason,
        "kyc_url": f"{_frontend_url()}/portal",
    }
    html = render_to_string("mail/kyc_status.html", ctx)
    send_html_email(
        f"KYC update: {label} — {site.site_name or 'Khudra Pasal'}",
        html,
        [to_addr],
        site=site,
    )
