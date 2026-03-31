from __future__ import annotations

from django.db import transaction

from core.models import AuditLog, LoyaltyRule, Order
from core.services import audit_service


@transaction.atomic
def grant_for_order(order: Order) -> None:
    """Placeholder for loyalty points until a User points field exists — audit trail only."""
    rules = LoyaltyRule.objects.filter(
        event=LoyaltyRule.Event.PURCHASE,
        status=LoyaltyRule.Status.ACTIVE,
    )
    if not rules.exists():
        return
    audit_service.log(
        f"Loyalty eligible for delivered order {order.order_number}",
        log_type=AuditLog.Type.ORDER,
        object_type="Order",
        object_id=str(order.pk),
        action_kind=AuditLog.ActionKind.OTHER,
        module="loyalty",
        metadata={"order_id": str(order.pk), "order_number": order.order_number},
    )
