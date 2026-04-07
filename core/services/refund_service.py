from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Order, OrderCommissionSettlement, Refund, Wallet, WalletTransaction
from core.services import vendor_service, wallet_service
from core.services.base import get_or_create_personal_wallet

Q2 = Decimal("0.01")
FEE_RATE = Decimal("0.03")


def compute_refund_breakdown(gross: Decimal) -> tuple[Decimal, Decimal]:
    """3% platform fee on gross; net is remainder (sums to gross)."""
    g = gross.quantize(Q2, rounding=ROUND_HALF_UP)
    fee = (g * FEE_RATE).quantize(Q2, rounding=ROUND_HALF_UP)
    net = (g - fee).quantize(Q2, rounding=ROUND_HALF_UP)
    return fee, net


def breakdown_for_refund(rf: Refund) -> tuple[Decimal, Decimal]:
    if rf.platform_fee_amount is not None and rf.net_credit_amount is not None:
        return rf.platform_fee_amount, rf.net_credit_amount
    return compute_refund_breakdown(rf.amount)


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
                return w, "Refund received"
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
            return purchase.wallet, "Refund received"
    w = get_or_create_personal_wallet(order.customer)
    return w, "Refund received"


def _split_clawback(
    gross: Decimal,
    *,
    total_amount: Decimal,
    vendor_amount: Decimal,
) -> tuple[Decimal, Decimal]:
    """Proportional vendor clawback; remainder on platform so vendor+platform = gross."""
    if total_amount <= 0:
        raise ValueError("Invalid settlement total for refund clawback")
    v = (gross * vendor_amount / total_amount).quantize(Q2, rounding=ROUND_HALF_UP)
    p = (gross - v).quantize(Q2, rounding=ROUND_HALF_UP)
    return v, p


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

    G = rf.amount.quantize(Q2, rounding=ROUND_HALF_UP)
    platform_fee, net_credit = breakdown_for_refund(rf)

    platform_w = wallet_service.get_or_create_platform_commission_wallet()
    settlement = OrderCommissionSettlement.objects.filter(order_id=order.pk).first()

    ref_id = str(rf.pk)
    desc_base = f"Refund {rf.refund_number} order {order.order_number}"

    if settlement and order.seller_id:
        vendor_w = vendor_service.ensure_vendor_wallet(order.seller)
        vendor_w = Wallet.objects.select_for_update().filter(pk=vendor_w.pk).first()
        platform_w = Wallet.objects.select_for_update().filter(pk=platform_w.pk).first()
        if not vendor_w or not platform_w:
            raise ValueError("Wallet not found for refund clawback")

        v_claw, p_claw = _split_clawback(
            G,
            total_amount=settlement.total_amount,
            vendor_amount=settlement.vendor_amount,
        )

        if vendor_w.balance < v_claw:
            raise ValueError("Insufficient vendor wallet balance for this refund")
        if platform_w.balance < p_claw:
            raise ValueError("Insufficient platform wallet balance for this refund")

        wallet_service.debit_wallet(
            vendor_w,
            v_claw,
            wtype=WalletTransaction.Type.REFUND_VENDOR_DEBIT,
            description=f"{desc_base} (vendor clawback)",
            reference_type="Refund",
            reference_id=ref_id,
            performed_by=order.customer,
            fund_source="Order refund — vendor share reversal",
        )
        wallet_service.debit_wallet(
            platform_w,
            p_claw,
            wtype=WalletTransaction.Type.REFUND_PLATFORM_DEBIT,
            description=f"{desc_base} (platform clawback)",
            reference_type="Refund",
            reference_id=ref_id,
            performed_by=order.customer,
            fund_source="Order refund — commission reversal",
        )
    else:
        platform_w = Wallet.objects.select_for_update().filter(pk=platform_w.pk).first()
        if not platform_w:
            raise ValueError("Platform wallet not found")
        if platform_w.balance < G:
            raise ValueError("Insufficient platform wallet balance for this refund")

        wallet_service.debit_wallet(
            platform_w,
            G,
            wtype=WalletTransaction.Type.REFUND_PLATFORM_DEBIT,
            description=f"{desc_base} (platform pool)",
            reference_type="Refund",
            reference_id=ref_id,
            performed_by=order.customer,
            fund_source="Order refund — no vendor settlement",
        )

    target_wallet, fund_source = _refund_credit_wallet_and_fund_source(order)
    wallet_service.credit_wallet(
        target_wallet,
        net_credit,
        wtype=WalletTransaction.Type.REFUND_CREDIT,
        description=f"{desc_base} (customer)",
        reference_type="Refund",
        reference_id=ref_id,
        performed_by=order.customer,
        fund_source=fund_source,
    )

    platform_w = wallet_service.get_or_create_platform_commission_wallet()
    wallet_service.credit_wallet(
        platform_w,
        platform_fee,
        wtype=WalletTransaction.Type.REFUND_PLATFORM_FEE,
        description=f"{desc_base} (3% platform fee)",
        reference_type="Refund",
        reference_id=ref_id,
        performed_by=order.customer,
        fund_source="Refund — platform fee retained",
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
