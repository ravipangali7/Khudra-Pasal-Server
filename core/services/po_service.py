from __future__ import annotations

from django.db import transaction

from core.models import PurchaseOrder
from core.services import product_service


@transaction.atomic
def complete_purchase_order(po: PurchaseOrder) -> None:
    """Decrement stock for completed POS / PO sales (physical products only)."""
    if po.status != PurchaseOrder.Status.COMPLETED:
        return
    for line in po.lines.select_related("product"):
        product_service.decrease_product_stock(line.product_id, line.quantity)
