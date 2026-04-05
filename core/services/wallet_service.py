from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import (
    FamilyWalletCategory,
    PaymentTransaction,
    User,
    Wallet,
    WalletBonus,
    WalletTransaction,
    WalletWithdrawal,
)
from core.services.base import get_or_create_personal_wallet, new_wallet_txn_id


@transaction.atomic
def credit_wallet(
    wallet: Wallet,
    amount: Decimal,
    *,
    wtype: str,
    description: str,
    reference_type: str = "",
    reference_id: str = "",
    performed_by: User | None = None,
    fund_source: str = "",
) -> WalletTransaction:
    w = Wallet.objects.select_for_update().get(pk=wallet.pk)
    Wallet.objects.filter(pk=w.pk).update(balance=F("balance") + amount)
    return WalletTransaction.objects.create(
        txn_id=new_wallet_txn_id(),
        wallet=w,
        type=wtype,
        amount=amount,
        description=description,
        status=WalletTransaction.Status.COMPLETED,
        reference_type=reference_type,
        reference_id=reference_id,
        performed_by=performed_by,
        fund_source=fund_source or "",
    )


@transaction.atomic
def debit_wallet(
    wallet: Wallet,
    amount: Decimal,
    *,
    wtype: str,
    description: str,
    reference_type: str = "",
    reference_id: str = "",
    performed_by: User | None = None,
    fund_source: str = "",
) -> WalletTransaction:
    w = Wallet.objects.select_for_update().get(pk=wallet.pk)
    if w.balance < amount:
        raise ValueError("Insufficient balance")
    Wallet.objects.filter(pk=w.pk).update(balance=F("balance") - amount)
    return WalletTransaction.objects.create(
        txn_id=new_wallet_txn_id(),
        wallet=w,
        type=wtype,
        amount=amount,
        description=description,
        status=WalletTransaction.Status.COMPLETED,
        reference_type=reference_type,
        reference_id=reference_id,
        performed_by=performed_by,
        fund_source=fund_source or "",
    )


@transaction.atomic
def credit_from_payment_transaction(pt: PaymentTransaction) -> WalletTransaction | None:
    """Wallet top-up: PaymentTransaction with no order, status success."""
    if pt.status != PaymentTransaction.Status.SUCCESS:
        return None
    if pt.order_id:
        return None
    pt_locked = (
        PaymentTransaction.objects.select_for_update().filter(pk=pt.pk).first()
    )
    if not pt_locked:
        return None
    if pt_locked.wallet_transaction_id:
        return pt_locked.wallet_transaction
    wallet = get_or_create_personal_wallet(pt_locked.customer)
    wt = credit_wallet(
        wallet,
        pt_locked.amount,
        wtype=WalletTransaction.Type.TOPUP,
        description=f"Top-up via {pt_locked.get_method_display()}",
        reference_type="PaymentTransaction",
        reference_id=str(pt_locked.pk),
        performed_by=pt_locked.customer,
        fund_source=f"Payment gateway — {pt_locked.get_method_display()}",
    )
    PaymentTransaction.objects.filter(pk=pt_locked.pk).update(
        wallet_transaction=wt,
        verified_at=timezone.now(),
    )
    apply_topup_bonus_after_credit(
        wallet,
        pt_locked.amount,
        bonus_reference_id=str(pt_locked.pk),
        performed_by=pt_locked.customer,
    )
    return wt


def _bonus_value_from_rule(rule: WalletBonus, base: Decimal) -> Decimal:
    if rule.is_percentage:
        return (base * rule.amount / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return rule.amount


def _signup_referral_bonus_value(rule: WalletBonus) -> Decimal | None:
    """Computed bonus for signup/referral rules. % rules need min_topup > 0 as monetary base."""
    today = timezone.now().date()
    if rule.expires_at and rule.expires_at < today:
        return None
    if rule.is_percentage:
        if rule.min_topup <= 0:
            return None
        val = _bonus_value_from_rule(rule, rule.min_topup)
    else:
        val = _bonus_value_from_rule(rule, Decimal("1"))
    return val if val > 0 else None


def apply_topup_bonus_after_credit(
    wallet: Wallet,
    topup_amount: Decimal,
    *,
    bonus_reference_id: str,
    performed_by: User | None,
) -> WalletTransaction | None:
    """Grant best matching active TOPUP WalletBonus after a principal top-up credit.

    Idempotent per ``bonus_reference_id`` (e.g. PaymentTransaction pk or main TOPUP txn_id).
    """
    if WalletTransaction.objects.filter(
        reference_type="topup_bonus",
        reference_id=bonus_reference_id,
    ).exists():
        return None
    today = timezone.now().date()
    best_rule: WalletBonus | None = None
    best_val = Decimal("0")
    for rule in WalletBonus.objects.filter(
        type=WalletBonus.Type.TOPUP,
        status=WalletBonus.Status.ACTIVE,
    ).order_by("id"):
        if rule.expires_at and rule.expires_at < today:
            continue
        if topup_amount < rule.min_topup:
            continue
        val = _bonus_value_from_rule(rule, topup_amount)
        if val <= 0:
            continue
        if val > best_val:
            best_val = val
            best_rule = rule
    if not best_rule or best_val <= 0:
        return None
    bonus_wt = credit_wallet(
        wallet,
        best_val,
        wtype=WalletTransaction.Type.BONUS,
        description=best_rule.title,
        reference_type="topup_bonus",
        reference_id=bonus_reference_id,
        performed_by=performed_by,
    )
    WalletBonus.objects.filter(pk=best_rule.pk).update(used_count=F("used_count") + 1)
    return bonus_wt


@transaction.atomic
def complete_withdrawal(withdrawal: WalletWithdrawal) -> WalletTransaction | None:
    if withdrawal.status != WalletWithdrawal.Status.APPROVED:
        return None
    wd = (
        WalletWithdrawal.objects.select_for_update()
        .select_related("wallet")
        .filter(pk=withdrawal.pk)
        .first()
    )
    if not wd:
        return None
    existing = WalletTransaction.objects.filter(
        reference_type="WalletWithdrawal",
        reference_id=str(wd.pk),
        type=WalletTransaction.Type.WITHDRAWAL,
        status=WalletTransaction.Status.COMPLETED,
    ).first()
    if existing:
        return existing
    wt = debit_wallet(
        wd.wallet,
        wd.amount,
        wtype=WalletTransaction.Type.WITHDRAWAL,
        description=f"Withdrawal {wd.withdrawal_number}",
        reference_type="WalletWithdrawal",
        reference_id=str(wd.pk),
    )
    WalletWithdrawal.objects.filter(pk=wd.pk).update(
        processed_at=timezone.now(),
    )
    return wt


@transaction.atomic
def reject_withdrawal(withdrawal: WalletWithdrawal) -> None:
    if withdrawal.status != WalletWithdrawal.Status.REJECTED:
        return
    WalletWithdrawal.objects.filter(pk=withdrawal.pk).update(
        processed_at=timezone.now(),
    )


@transaction.atomic
def execute_transfer(
    from_wallet: Wallet,
    to_wallet: Wallet,
    amount: Decimal,
    *,
    performed_by: User,
    reference_type: str = "",
    reference_id: str = "",
    family_wallet_category: FamilyWalletCategory | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    if from_wallet.pk == to_wallet.pk:
        raise ValueError("Cannot transfer to the same wallet")
    ref = reference_id or new_wallet_txn_id()
    out_txn = debit_wallet(
        from_wallet,
        amount,
        wtype=WalletTransaction.Type.TRANSFER,
        description=f"Transfer to wallet #{to_wallet.pk}",
        reference_type=reference_type or "transfer_pair",
        reference_id=ref,
        performed_by=performed_by,
    )
    out_txn.from_wallet = from_wallet
    out_txn.to_wallet = to_wallet
    out_fields = ["from_wallet", "to_wallet"]
    if family_wallet_category is not None:
        out_txn.family_wallet_category = family_wallet_category
        out_fields.append("family_wallet_category")
    out_txn.save(update_fields=out_fields)
    in_txn = credit_wallet(
        to_wallet,
        amount,
        wtype=WalletTransaction.Type.TRANSFER,
        description=f"Transfer from wallet #{from_wallet.pk}",
        reference_type=reference_type or "transfer_pair",
        reference_id=ref,
        performed_by=performed_by,
    )
    in_txn.from_wallet = from_wallet
    in_txn.to_wallet = to_wallet
    in_fields = ["from_wallet", "to_wallet"]
    if family_wallet_category is not None:
        in_txn.family_wallet_category = family_wallet_category
        in_fields.append("family_wallet_category")
    in_txn.save(update_fields=in_fields)
    return out_txn, in_txn


@transaction.atomic
def apply_signup_bonus(user: User) -> WalletTransaction | None:
    """First personal wallet credit from active WalletBonus rules (signup)."""
    if WalletTransaction.objects.filter(
        wallet__owner=user,
        type=WalletTransaction.Type.BONUS,
        reference_type="signup_bonus",
    ).exists():
        return None
    best_rule: WalletBonus | None = None
    best_val = Decimal("0")
    for rule in WalletBonus.objects.filter(
        type=WalletBonus.Type.SIGNUP,
        status=WalletBonus.Status.ACTIVE,
    ).order_by("id"):
        val = _signup_referral_bonus_value(rule)
        if val is None:
            continue
        if val > best_val:
            best_val = val
            best_rule = rule
    if not best_rule or best_val <= 0:
        return None
    wallet = get_or_create_personal_wallet(user)
    wt = credit_wallet(
        wallet,
        best_val,
        wtype=WalletTransaction.Type.BONUS,
        description=best_rule.title,
        reference_type="signup_bonus",
        reference_id=str(best_rule.pk),
        performed_by=user,
    )
    WalletBonus.objects.filter(pk=best_rule.pk).update(used_count=F("used_count") + 1)
    return wt


@transaction.atomic
def apply_referral_wallet_bonus(new_user: User) -> WalletTransaction | None:
    """Credit the referrer's personal wallet from active WalletBonus rules (referral)."""
    rid = getattr(new_user, "referred_by_id", None)
    if not rid or rid == new_user.pk:
        return None
    if WalletTransaction.objects.filter(
        reference_type="referral_wallet_bonus",
        reference_id=str(new_user.pk),
    ).exists():
        return None
    referrer = User.objects.filter(pk=rid).first()
    if not referrer:
        return None
    best_rule: WalletBonus | None = None
    best_val = Decimal("0")
    for rule in WalletBonus.objects.filter(
        type=WalletBonus.Type.REFERRAL,
        status=WalletBonus.Status.ACTIVE,
    ).order_by("id"):
        val = _signup_referral_bonus_value(rule)
        if val is None:
            continue
        if val > best_val:
            best_val = val
            best_rule = rule
    if not best_rule or best_val <= 0:
        return None
    wallet = get_or_create_personal_wallet(referrer)
    wt = credit_wallet(
        wallet,
        best_val,
        wtype=WalletTransaction.Type.BONUS,
        description=best_rule.title,
        reference_type="referral_wallet_bonus",
        reference_id=str(new_user.pk),
        performed_by=referrer,
    )
    WalletBonus.objects.filter(pk=best_rule.pk).update(used_count=F("used_count") + 1)
    return wt


def credit_wallet_for_refund(
    user: User,
    amount: Decimal,
    *,
    reference_type: str,
    reference_id: str,
) -> WalletTransaction:
    wallet = get_or_create_personal_wallet(user)
    return credit_wallet(
        wallet,
        amount,
        wtype=WalletTransaction.Type.CREDIT,
        description="Refund credit",
        reference_type=reference_type,
        reference_id=reference_id,
        performed_by=user,
    )


@transaction.atomic
def get_or_create_platform_commission_wallet() -> Wallet:
    """Singleton platform wallet (enforced by partial unique constraint on type=platform)."""
    w = Wallet.objects.select_for_update().filter(type=Wallet.Type.PLATFORM).first()
    if w:
        return w
    return Wallet.objects.create(
        type=Wallet.Type.PLATFORM,
        label="Platform commission",
        status=Wallet.Status.ACTIVE,
    )
