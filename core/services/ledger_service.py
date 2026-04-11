"""Vendor ledger entries — sale settlement, reversals, purchase costs."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import Order, OrderCommissionSettlement, Vendor, VendorLedgerEntry


@transaction.atomic
def record_sale_on_payment(order: Order) -> None:
    """Idempotent: one SALE_SETTLEMENT row per paid order with seller (mirrors commission settlement)."""
    if not order.seller_id:
        return
    if order.payment_status != Order.PaymentStatus.PAID:
        return
    if VendorLedgerEntry.objects.filter(
        vendor_id=order.seller_id,
        entry_type=VendorLedgerEntry.EntryType.SALE_SETTLEMENT,
        reference_type="Order",
        reference_id=str(order.pk),
    ).exists():
        return
    settlement = OrderCommissionSettlement.objects.filter(order_id=order.pk).first()
    if not settlement:
        return
    VendorLedgerEntry.objects.create(
        vendor_id=order.seller_id,
        entry_type=VendorLedgerEntry.EntryType.SALE_SETTLEMENT,
        amount=settlement.vendor_amount,
        reference_type="Order",
        reference_id=str(order.pk),
        wallet_transaction_id=settlement.vendor_wallet_txn_id,
        description=f"Sale settlement {order.order_number}",
        created_by_id=order.customer_id,
    )


@transaction.atomic
def reverse_sale_settlement_on_cancel(order: Order) -> None:
    """After a paid order is cancelled, mirror the settlement credit with a reversal line."""
    if not order.seller_id:
        return
    if VendorLedgerEntry.objects.filter(
        vendor_id=order.seller_id,
        entry_type=VendorLedgerEntry.EntryType.SALE_REVERSAL,
        reference_type="Order",
        reference_id=str(order.pk),
    ).exists():
        return
    orig = VendorLedgerEntry.objects.filter(
        vendor_id=order.seller_id,
        entry_type=VendorLedgerEntry.EntryType.SALE_SETTLEMENT,
        reference_type="Order",
        reference_id=str(order.pk),
    ).first()
    if not orig:
        return
    amt = orig.amount
    if amt is None:
        amt = Decimal("0")
    VendorLedgerEntry.objects.create(
        vendor_id=order.seller_id,
        entry_type=VendorLedgerEntry.EntryType.SALE_REVERSAL,
        amount=-amt,
        reference_type="Order",
        reference_id=str(order.pk),
        wallet_transaction_id=None,
        description=f"Reversal for cancelled order {order.order_number}",
        created_by_id=order.customer_id,
    )
