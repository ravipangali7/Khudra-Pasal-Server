from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from django.db.models import F

from core.models import Coupon, Order, PaymentTransaction, WalletTransaction
from core.services import loyalty_service, notification_service, wallet_service
from core.services.base import get_or_create_personal_wallet


@transaction.atomic
def mark_paid_from_gateway(
    order: Order,
    payment: PaymentTransaction | None = None,
) -> None:
    fresh = Order.objects.select_for_update().filter(pk=order.pk).first()
    if not fresh or fresh.payment_status == Order.PaymentStatus.PAID:
        return
    fresh.payment_status = Order.PaymentStatus.PAID
    fresh.updated_at = timezone.now()
    fresh.save(update_fields=["payment_status", "updated_at"])
    if payment:
        PaymentTransaction.objects.filter(pk=payment.pk).update(
            verified_at=timezone.now(),
        )


@transaction.atomic
def pay_with_wallet(
    order: Order,
    customer_wallet,
    *,
    fund_source: str = "",
) -> None:
    """Debit customer wallet for order total; call when checkout uses wallet."""
    fresh = Order.objects.select_for_update().filter(pk=order.pk).first()
    if not fresh or fresh.payment_status == Order.PaymentStatus.PAID:
        return
    wallet_service.debit_wallet(
        customer_wallet,
        fresh.total,
        wtype=WalletTransaction.Type.PURCHASE,
        description=f"Order {fresh.order_number}",
        reference_type="Order",
        reference_id=str(fresh.pk),
        performed_by=fresh.customer,
        fund_source=fund_source or "Wallet balance",
    )
    fresh.payment_status = Order.PaymentStatus.PAID
    fresh.updated_at = timezone.now()
    fresh.save(update_fields=["payment_status", "updated_at"])


@transaction.atomic
def restore_order_after_cancel(order: Order) -> None:
    """Stock restore, wallet refund, coupon rollback when order moves to cancelled."""
    from core.models import OrderItem
    from core.services import product_service

    for item in OrderItem.objects.filter(order=order):
        product_service.restore_line_stock(item)

    was_paid = order.payment_status == Order.PaymentStatus.PAID
    pay_updates: dict = {}

    if was_paid and order.payment_method == Order.PaymentMethod.WALLET:
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
        refund_wallet = (
            purchase.wallet
            if purchase and purchase.wallet_id
            else get_or_create_personal_wallet(order.customer)
        )
        fs = (purchase.fund_source if purchase else "") or "Original payment wallet"
        wallet_service.credit_wallet(
            refund_wallet,
            order.total,
            wtype=WalletTransaction.Type.CREDIT,
            description=f"Refund for cancelled order {order.order_number}",
            reference_type="Order",
            reference_id=str(order.pk),
            performed_by=order.customer,
            fund_source=fs,
            skip_max_balance=True,
            allow_frozen_target=True,
        )
        pay_updates["payment_status"] = Order.PaymentStatus.REFUNDED

    if was_paid and order.coupon_id:
        Coupon.objects.filter(pk=order.coupon_id, used_count__gt=0).update(
            used_count=F("used_count") - 1
        )

    if was_paid and order.seller_id:
        from core.services import ledger_service

        ledger_service.reverse_sale_settlement_on_cancel(order)

    if pay_updates:
        pay_updates["updated_at"] = timezone.now()
        Order.objects.filter(pk=order.pk).update(**pay_updates)


@transaction.atomic
def on_payment_transaction_success(pt: PaymentTransaction) -> None:
    """Route successful gateway payment to order paid or wallet top-up."""
    if pt.order_id:
        order = Order.objects.select_for_update().get(pk=pt.order_id)
        mark_paid_from_gateway(order, pt)
    else:
        wallet_service.credit_from_payment_transaction(pt)


def on_order_delivered(order: Order) -> None:
    loyalty_service.grant_for_order(order)
    notification_service.notify_order_delivered(order)
