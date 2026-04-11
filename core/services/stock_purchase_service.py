"""Post vendor stock purchases: receive inventory and write ledger."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from core.models import Product, VendorStockPurchase, VendorStockPurchaseLine, VendorLedgerEntry
from core.services import product_service


def _quantize_money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recompute_purchase_totals(purchase: VendorStockPurchase) -> None:
    sub = Decimal("0")
    for line in purchase.lines.select_related("product"):
        lt = (line.unit_cost * line.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        VendorStockPurchaseLine.objects.filter(pk=line.pk).update(line_total=lt)
        sub += lt
    tax = purchase.tax or Decimal("0")
    purchase.subtotal = _quantize_money(sub)
    purchase.total = _quantize_money(sub + tax)


@transaction.atomic
def post_stock_purchase(purchase: VendorStockPurchase, *, acting_user_id: int | None) -> VendorStockPurchase:
    """Move purchase to posted: increase stock, write PURCHASE_COST ledger (idempotent)."""
    vp = (
        VendorStockPurchase.objects.select_for_update()
        .select_related("vendor", "supplier")
        .prefetch_related("lines__product")
        .filter(pk=purchase.pk)
        .first()
    )
    if not vp or vp.status == VendorStockPurchase.Status.POSTED:
        return vp or purchase

    lines = list(vp.lines.select_related("product"))
    if not lines:
        raise ValueError("Add at least one line before posting.")

    recompute_purchase_totals(vp)
    vp.refresh_from_db(fields=["subtotal", "total", "tax"])

    for line in lines:
        p = line.product
        if p.seller_id != vp.vendor_id:
            raise ValueError(f"Product {p.pk} does not belong to this vendor.")
        if p.type == Product.Type.DIGITAL:
            raise ValueError(f"Product {p.name} is digital; stock purchase not supported.")
        product_service.increase_product_stock(p.pk, line.quantity)
        p2 = Product.objects.get(pk=p.pk)
        product_service.sync_stock_status(p2)

    if not VendorLedgerEntry.objects.filter(
        vendor_id=vp.vendor_id,
        entry_type=VendorLedgerEntry.EntryType.PURCHASE_COST,
        reference_type="VendorStockPurchase",
        reference_id=str(vp.pk),
    ).exists():
        VendorLedgerEntry.objects.create(
            vendor_id=vp.vendor_id,
            entry_type=VendorLedgerEntry.EntryType.PURCHASE_COST,
            amount=-_quantize_money(vp.total),
            reference_type="VendorStockPurchase",
            reference_id=str(vp.pk),
            wallet_transaction_id=None,
            description=f"Stock purchase {vp.reference} (supplier: {vp.supplier.name})",
            created_by_id=acting_user_id,
        )

    vp.status = VendorStockPurchase.Status.POSTED
    vp.posted_at = timezone.now()
    vp.save(update_fields=["status", "posted_at", "subtotal", "total", "tax"])
    return vp


def generate_purchase_reference(vendor_id: int) -> str:
    for _ in range(30):
        cand = f"VP-{vendor_id}-{uuid4().hex[:10].upper()}"
        if len(cand) <= 40 and not VendorStockPurchase.objects.filter(reference=cand).exists():
            return cand
    return f"VP-{vendor_id}-{uuid4().hex[:10].upper()}"[:40]
