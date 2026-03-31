"""In-app notifications for portal-initiated refund requests."""

from __future__ import annotations

from urllib.parse import quote

from core.models import Notification, Refund, User
from core.portal_roles import user_allowed_for_admin_portal


def notify_admins_new_refund_request(rf: Refund) -> None:
    """One row per eligible admin (same pattern as support tickets)."""
    order_no = rf.order.order_number if rf.order_id else ""
    preview = f"{rf.refund_number} — order {order_no} — Rs. {float(rf.amount):,.2f}"
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
        title = "Refund approved"
        msg = f"Your refund {rf.refund_number} for order {rf.order.order_number} was approved. Rs. {float(rf.amount):,.2f} will be credited to your wallet."
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
