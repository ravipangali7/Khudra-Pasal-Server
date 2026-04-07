from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Order, OrderCommissionSettlement, Refund, Wallet, WalletTransaction
from core.services import commission_service, vendor_service, wallet_service
from core.services.base import get_or_create_personal_wallet

Q2 = Decimal("0.01")
FEE_RATE = Decimal("0.03")


@dataclass(frozen=True)
class RefundFinancials:
    """Per refund: vendor clawback, platform debit (commission returned), fee kept on commission slice, customer credit."""

    vendor_claw: Decimal
    platform_debit: Decimal
    fee_retained: Decimal
    customer_credit: Decimal


def _ensure_commission_settlement(order: Order) -> None:
    """Idempotent: create settlement when a paid marketplace order is missing it (e.g. legacy data)."""
    if not order.seller_id:
        return
    if order.payment_status != Order.PaymentStatus.PAID:
        return
    if OrderCommissionSettlement.objects.filter(order_id=order.pk).exists():
        return
    commission_service.settle_order_commission(order)


def _commission_split_from_order(order: Order) -> tuple[Decimal, Decimal, Decimal] | None:
    """Same totals as commission settlement (read-only). Returns (total, vendor_amount, commission) or None."""
    if not order.seller_id:
        return None
    vendor = order.seller
    rate = vendor.commission_rate if vendor.commission_rate is not None else Decimal("0")
    total = order.total
    commission_base = order.subtotal
    commission = (commission_base * rate / Decimal("100")).quantize(Q2, rounding=ROUND_HALF_UP)
    vendor_amount = total - commission
    return total, vendor_amount, commission


def _financials_from_totals(
    R: Decimal,
    *,
    total_amount: Decimal,
    vendor_amount: Decimal,
) -> RefundFinancials:
    if total_amount <= 0:
        raise ValueError("Invalid settlement total for refund")
    vendor_claw = (R * vendor_amount / total_amount).quantize(Q2, rounding=ROUND_HALF_UP)
    commission_slice = (R - vendor_claw).quantize(Q2, rounding=ROUND_HALF_UP)
    fee_retained = (commission_slice * FEE_RATE).quantize(Q2, rounding=ROUND_HALF_UP)
    platform_debit = (commission_slice - fee_retained).quantize(Q2, rounding=ROUND_HALF_UP)
    customer_credit = (vendor_claw + platform_debit).quantize(Q2, rounding=ROUND_HALF_UP)
    return RefundFinancials(
        vendor_claw=vendor_claw,
        platform_debit=platform_debit,
        fee_retained=fee_retained,
        customer_credit=customer_credit,
    )


def refund_financials(order: Order, gross: Decimal, *, persist_settlement: bool = False) -> RefundFinancials:
    """
    Commission-based refund split: 3% fee applies only to the proportional commission slice of this refund.
    No settlement / no seller: full gross to customer, platform debits full gross.

    persist_settlement: when True (refund create / execute), persist missing settlement for paid seller orders.
    When False (read paths), use DB settlement or the same split in-memory without writing.
    """
    R = gross.quantize(Q2, rounding=ROUND_HALF_UP)
    if R <= 0:
        raise ValueError("Refund amount must be positive")

    if persist_settlement:
        _ensure_commission_settlement(order)

    settlement = OrderCommissionSettlement.objects.filter(order_id=order.pk).first()
    if settlement and order.seller_id:
        return _financials_from_totals(
            R,
            total_amount=settlement.total_amount,
            vendor_amount=settlement.vendor_amount,
        )

    split = _commission_split_from_order(order)
    if split:
        total_amount, vendor_amount, _commission = split
        return _financials_from_totals(R, total_amount=total_amount, vendor_amount=vendor_amount)

    return RefundFinancials(
        vendor_claw=Decimal("0"),
        platform_debit=R,
        fee_retained=Decimal("0"),
        customer_credit=R,
    )


def breakdown_for_refund(rf: Refund) -> tuple[Decimal, Decimal]:
    """(fee_retained_on_commission_slice, customer_credit) — always recomputed from order + amount."""
    fin = refund_financials(rf.order, rf.amount)
    return fin.fee_retained, fin.customer_credit


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
            return purchase.wallet, "Refund received"
    w = get_or_create_personal_wallet(order.customer)
    return w, "Refund received"


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
    fin = refund_financials(order, G, persist_settlement=True)

    platform_w = wallet_service.get_or_create_platform_commission_wallet()
    platform_w = Wallet.objects.select_for_update().filter(pk=platform_w.pk).first()
    if not platform_w:
        raise ValueError("Platform wallet not found")

    ref_id = str(rf.pk)
    desc_base = f"Refund {rf.refund_number} order {order.order_number}"

    if order.seller_id:
        vendor_w = vendor_service.ensure_vendor_wallet(order.seller)
        vendor_w = Wallet.objects.select_for_update().filter(pk=vendor_w.pk).first()
        if not vendor_w:
            raise ValueError("Wallet not found for refund clawback")

        if fin.vendor_claw > 0:
            if vendor_w.balance < fin.vendor_claw:
                raise ValueError("Insufficient vendor wallet balance for this refund")
            wallet_service.debit_wallet(
                vendor_w,
                fin.vendor_claw,
                wtype=WalletTransaction.Type.REFUND_VENDOR_DEBIT,
                description=f"{desc_base} (vendor clawback)",
                reference_type="Refund",
                reference_id=ref_id,
                performed_by=order.customer,
                fund_source="Order refund — vendor share reversal",
            )
        if fin.platform_debit > 0:
            if platform_w.balance < fin.platform_debit:
                raise ValueError("Insufficient platform wallet balance for this refund")
            wallet_service.debit_wallet(
                platform_w,
                fin.platform_debit,
                wtype=WalletTransaction.Type.REFUND_PLATFORM_DEBIT,
                description=f"{desc_base} (commission returned to customer)",
                reference_type="Refund",
                reference_id=ref_id,
                performed_by=order.customer,
                fund_source="Order refund — commission reversal (after platform retention)",
            )
    else:
        if platform_w.balance < fin.platform_debit:
            raise ValueError("Insufficient platform wallet balance for this refund")

        wallet_service.debit_wallet(
            platform_w,
            fin.platform_debit,
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
        fin.customer_credit,
        wtype=WalletTransaction.Type.REFUND_CREDIT,
        description=f"{desc_base} (customer)",
        reference_type="Refund",
        reference_id=ref_id,
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
