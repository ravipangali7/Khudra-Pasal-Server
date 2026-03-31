from __future__ import annotations

from django.db import transaction

from core.models import Product, PurchaseOrder


@transaction.atomic
def complete_purchase_order(po: PurchaseOrder) -> None:
    """Decrement stock for completed POS / PO sales (physical products only)."""
    if po.status != PurchaseOrder.Status.COMPLETED:
        return
    for line in po.lines.select_related("product"):
        p = line.product
        if p.type != Product.Type.PHYSICAL:
            continue
        if p.stock < line.quantity:
            continue
        p.stock -= line.quantity
        p.save(update_fields=["stock"])
