from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from core.models import Order, OrderCommissionSettlement, WalletTransaction
from core.services import vendor_service, wallet_service


@transaction.atomic
def settle_order_commission(order: Order) -> None:
    """Credit platform commission wallet and vendor wallet; idempotent per order."""
    o = (
        Order.objects.select_for_update()
        .select_related("seller")
        .filter(pk=order.pk)
        .first()
    )
    if not o or o.payment_status != Order.PaymentStatus.PAID:
        return
    if not o.seller_id:
        return
    if OrderCommissionSettlement.objects.filter(order_id=o.pk).exists():
        return

    vendor = o.seller
    rate = vendor.commission_rate if vendor.commission_rate is not None else Decimal("0")
    total = o.total
    commission_base = o.subtotal
    commission = (commission_base * rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    vendor_amount = total - commission

    platform_w = wallet_service.get_or_create_platform_commission_wallet()
    vendor_w = vendor_service.ensure_vendor_wallet(vendor)

    p_txn = wallet_service.credit_wallet(
        platform_w,
        commission,
        wtype=WalletTransaction.Type.COMMISSION_IN,
        description=f"Commission order {o.order_number}",
        reference_type="Order",
        reference_id=str(o.pk),
        fund_source=f"Customer order payment — {o.order_number} (platform commission)",
    )
    v_txn = wallet_service.credit_wallet(
        vendor_w,
        vendor_amount,
        wtype=WalletTransaction.Type.VENDOR_SETTLEMENT,
        description=f"Sale settlement order {o.order_number}",
        reference_type="Order",
        reference_id=str(o.pk),
        fund_source=f"Customer order payment — {o.order_number} (vendor share)",
    )

    OrderCommissionSettlement.objects.create(
        order=o,
        vendor=vendor,
        total_amount=total,
        commission_percent=rate,
        commission_amount=commission,
        vendor_amount=vendor_amount,
        payment_status=o.payment_status,
        platform_wallet_txn=p_txn,
        vendor_wallet_txn=v_txn,
    )
