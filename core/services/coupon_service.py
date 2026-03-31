from __future__ import annotations

from django.db import transaction
from django.db.models import F

from core.models import Coupon, Order


@transaction.atomic
def apply_coupon_use_on_payment_confirmed(order: Order) -> None:
    """Increment coupon usage when order is marked paid (idempotent per order)."""
    if not order.coupon_id:
        return
    c = Coupon.objects.select_for_update().filter(pk=order.coupon_id).first()
    if not c:
        return
    if c.usage_limit is not None and c.used_count >= c.usage_limit:
        return
    Coupon.objects.filter(pk=c.pk).update(used_count=F("used_count") + 1)
