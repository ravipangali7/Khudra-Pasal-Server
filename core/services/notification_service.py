from __future__ import annotations

from typing import Optional

from core.models import Notification, Order, ProductReview, PurchaseApprovalRequest, User


def notify_user(
    *,
    user: User,
    title: str,
    message: str,
    ntype: str = Notification.Type.SYSTEM,
    action_url: str = "",
) -> Notification:
    return Notification.objects.create(
        title=title,
        message=message,
        type=ntype,
        target=Notification.Target.CUSTOMERS,
        recipient=user,
        action_url=action_url,
    )


def notify_order_delivered(order: Order) -> Notification:
    """In-app notice for the customer when an order reaches Delivered."""
    store = order.seller.store_name if order.seller_id else "In-House"
    n_items = order.items.count()
    total = order.total
    msg = (
        f"Order {order.order_number} is delivered. "
        f"Total Rs. {total} · {n_items} item(s) · {store}"
    )
    return Notification.objects.create(
        title="Order delivered",
        message=msg,
        type=Notification.Type.ORDER,
        target=Notification.Target.CUSTOMERS,
        recipient=order.customer,
        action_url="/portal/orders",
    )


def notify_parent_purchase_approval_requested(par: PurchaseApprovalRequest) -> Notification:
    """In-app notice for the family leader when a child submits a purchase approval request."""
    child = par.child
    label = (child.name or "").strip() or (child.phone or "").strip() or f"Child #{child.pk}"
    msg = f"{label} asked to buy {par.product.name} (Rs. {par.amount})."
    note = (par.note or "").strip()
    if note:
        msg = f"{msg} Note: {note[:200]}"
    return notify_user(
        user=par.parent,
        title="Purchase approval requested",
        message=msg,
        ntype=Notification.Type.FAMILY,
        action_url="/family-portal/dashboard",
    )


def notify_child_purchase_auto_approved(par: PurchaseApprovalRequest) -> Notification:
    """In-app notice when a purchase request is auto-approved by family Auto-Approval Rules."""
    msg = (
        f"{par.product.name} (Rs. {par.amount}) was auto-approved under your family’s rules. "
        "You can add it to your cart on the shop or child portal."
    )
    note = (par.note or "").strip()
    if note:
        msg = f"{msg} Note: {note[:200]}"
    return notify_user(
        user=par.child,
        title="Purchase auto-approved",
        message=msg,
        ntype=Notification.Type.FAMILY,
        action_url="/child-portal/requests",
    )


def notify_child_purchase_approval_decision(par: PurchaseApprovalRequest) -> Notification:
    """In-app notice for the child when the parent approves or rejects a purchase request."""
    if par.status == PurchaseApprovalRequest.Status.APPROVED:
        title = "Purchase approved"
        msg = (
            f"Your parent approved {par.product.name} (Rs. {par.amount}). "
            "You can add it to your cart on the shop or child portal."
        )
    else:
        title = "Purchase request declined"
        msg = f"Your parent declined the request for {par.product.name}."
    pn = (par.parent_note or "").strip()
    if pn:
        msg = f"{msg} Message: {pn[:200]}"
    return notify_user(
        user=par.child,
        title=title,
        message=msg,
        ntype=Notification.Type.FAMILY,
        action_url="/child-portal/requests",
    )


def notify_order_status_fcm_customer(
    order_id: int,
    previous_status: str,
    new_status: str,
) -> None:
    """Send an FCM push to the customer when order status changes (vendor/admin/save paths)."""
    if previous_status == new_status:
        return
    order = Order.objects.select_related("customer").filter(pk=order_id).first()
    if not order:
        return
    cust = order.customer
    token = (getattr(cust, "fcm_token", "") or "").strip()
    if not token:
        return
    from core.services.fcm_push_service import send_fcm_to_tokens

    labels = dict(Order.Status.choices)
    new_label = labels.get(new_status, new_status)
    title = "Order update"
    body = f"{order.order_number} is now {new_label}."
    send_fcm_to_tokens([token], title, body)


def notify_new_review(review: ProductReview) -> Optional[Notification]:
    vendor_user = None
    if review.product.seller_id:
        vendor_user = review.product.seller.user
    if not vendor_user:
        return None
    return notify_user(
        user=vendor_user,
        title="New product review",
        message=f"Review pending moderation for {review.product.name}",
        ntype=Notification.Type.SYSTEM,
        action_url=f"/admin/products/{review.product_id}/",
    )
