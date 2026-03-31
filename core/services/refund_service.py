from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Order, Refund, Wallet, WalletTransaction
from core.services import wallet_service
from core.services.base import get_or_create_personal_wallet


def _refund_credit_wallet_and_fund_source(order: Order) -> tuple[Wallet, str]:
    """Wallet paid for the order (wallet checkout); else customer personal wallet."""
    if order.payment_method == Order.PaymentMethod.WALLET:
        if order.payment_wallet_id:
            w = (
                Wallet.objects.select_for_update()
                .filter(pk=order.payment_wallet_id)
                .first()
            )
            if w:
                return w, "Refund credit"
        purchase = (
            WalletTransaction.objects.filter(
                reference_type="Order",
                reference_id=str(order.pk),
                type=WalletTransaction.Type.PURCHASE,
                status=WalletTransaction.Status.COMPLETED,
            )
            .select_related("wallet")
            .order_by("-created_at")
            .first()
        )
        if purchase and purchase.wallet_id:
            fs = (purchase.fund_source or "").strip() or "Original payment wallet"
            return purchase.wallet, fs
    w = get_or_create_personal_wallet(order.customer)
    return w, "Refund credit"


@transaction.atomic
def execute_refund(refund: Refund) -> None:
    if refund.status != Refund.Status.APPROVED:
        return
    rf = Refund.objects.select_for_update().filter(pk=refund.pk).first()
    if not rf:
        return
    if WalletTransaction.objects.filter(
        reference_type="Refund",
        reference_id=str(rf.pk),
    ).exists():
        return

    order = Order.objects.select_for_update().filter(pk=rf.order_id).first()
    if not order:
        return

    target_wallet, fund_source = _refund_credit_wallet_and_fund_source(order)
    wallet_service.credit_wallet(
        target_wallet,
        rf.amount,
        wtype=WalletTransaction.Type.CREDIT,
        description=f"Refund {rf.refund_number} for order {order.order_number}",
        reference_type="Refund",
        reference_id=str(rf.pk),
        performed_by=order.customer,
        fund_source=fund_source,
    )

    total_approved = (
        Refund.objects.filter(
            order_id=order.pk, status=Refund.Status.APPROVED
        ).aggregate(s=Sum("amount"))["s"]
        or Decimal("0")
    )
    order_updates: dict = {"updated_at": timezone.now()}
    if total_approved + Decimal("0.01") >= Decimal(order.total):
        order_updates["status"] = Order.Status.REFUNDED
        order_updates["payment_status"] = Order.PaymentStatus.REFUNDED
    Order.objects.filter(pk=order.pk).update(**order_updates)

    Refund.objects.filter(pk=rf.pk).update(
        processed_at=timezone.now(),
    )
