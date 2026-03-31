from __future__ import annotations

from typing import Optional

from core.models import Notification, Order, ProductReview, User


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
