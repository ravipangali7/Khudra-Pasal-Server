"""In-app notifications for portal-initiated refund requests."""

from __future__ import annotations

from urllib.parse import quote

from core.models import Notification, Refund, User
from core.portal_roles import user_allowed_for_admin_portal
from core.services.refund_service import (
    breakdown_for_refund,
    effective_refund_commission_percent,
)


def notify_admins_new_refund_request(rf: Refund) -> None:
    """One row per eligible admin (same pattern as support tickets)."""
    order_no = rf.order.order_number if rf.order_id else ""
    fee, net = breakdown_for_refund(rf)
    preview = (
        f"{rf.refund_number} — order {order_no} — gross Rs. {float(rf.amount):,.2f}, "
        f"net to customer Rs. {float(net):,.2f}"
    )
    q = quote(str(rf.refund_number), safe="")
    base_url = f"/admin/refunds?highlightRefund={q}"
    for admin_user in User.objects.filter(is_active=True).iterator():
        if not user_allowed_for_admin_portal(admin_user):
            continue
        Notification.objects.create(
            title="Refund request",
            message=preview[:500],
            type=Notification.Type.WALLET,
            target=Notification.Target.ADMINS,
            recipient=admin_user,
            action_url=base_url[:255],
        )


def notify_customer_refund_status(rf: Refund, *, approved: bool) -> None:
    cust = rf.customer
    if not cust:
        return
    if approved:
        fee, net = breakdown_for_refund(rf)
        pct = float(effective_refund_commission_percent(rf.order))
        title = "Refund approved"
        msg = (
            f"Your refund {rf.refund_number} for order {rf.order.order_number} was approved. "
            f"Rs. {float(net):,.2f} will be credited to your wallet. "
            f"The platform retains {pct:g}% of the commission portion on this refund "
            f"(Rs. {float(fee):,.2f})."
        )
    else:
        title = "Refund request declined"
        msg = f"Your refund request {rf.refund_number} for order {rf.order.order_number} was declined."
        if (rf.admin_note or "").strip():
            msg += f" Note: {(rf.admin_note or '')[:300]}"
    Notification.objects.create(
        title=title[:150],
        message=msg[:2000],
        type=Notification.Type.WALLET,
        target=Notification.Target.CUSTOMERS,
        recipient=cust,
        action_url="",
    )


def notify_vendor_refund_processed(rf: Refund) -> None:
    """Notify vendor account when a refund is approved and wallets are settled."""
    if not rf.order_id:
        return
    order = rf.order
    seller = getattr(order, "seller", None)
    if not seller:
        return
    vendor_user = getattr(seller, "user", None)
    if not vendor_user:
        return
    fee, net = breakdown_for_refund(rf)
    pct = float(effective_refund_commission_percent(order))
    msg = (
        f"Refund {rf.refund_number} for order {order.order_number} was processed. "
        f"Gross Rs. {float(rf.amount):,.2f}; customer receives Rs. {float(net):,.2f}; "
        f"platform retains Rs. {float(fee):,.2f} ({pct:g}% of commission on this refund). "
        f"Check your wallet for vendor clawback."
    )
    Notification.objects.create(
        title="Refund processed",
        message=msg[:2000],
        type=Notification.Type.WALLET,
        target=Notification.Target.VENDORS,
        recipient=vendor_user,
        action_url="",
    )
