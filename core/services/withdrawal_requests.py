"""Pending withdrawal creation: KYC/payout gates, available balance, payout → WalletWithdrawal fields."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from django.db.models import Sum
from django.utils import timezone

from core.models import PayoutAccount, User, Wallet, WalletWithdrawal
from core.services import wallet_policy


def payout_required_block_payload(user: User) -> dict | None:
    if PayoutAccount.objects.filter(user=user).exists():
        return None
    return {
        "code": "payout_required",
        "detail": "Add at least one payout account before withdrawing.",
    }


def sum_pending_withdrawals_for_wallet(
    wallet_id: int, *, exclude_pk: int | None = None
) -> Decimal:
    qs = WalletWithdrawal.objects.filter(
        wallet_id=wallet_id,
        status=WalletWithdrawal.Status.PENDING,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    t = qs.aggregate(s=Sum("amount"))["s"]
    return t if t is not None else Decimal("0")


def available_withdrawal_amount(wallet: Wallet) -> Decimal:
    return wallet.balance - sum_pending_withdrawals_for_wallet(wallet.pk)


def payout_account_to_withdrawal_fields(account: PayoutAccount) -> dict:
    if account.type == PayoutAccount.Type.BANK:
        method = WalletWithdrawal.Method.BANK_TRANSFER
        method_account = (account.bank_account_no or "").strip()
        bank_name = (account.bank_name or "").strip()
        account_holder = (account.bank_account_holder or "").strip()
    elif account.type == PayoutAccount.Type.ESEWA:
        method = WalletWithdrawal.Method.ESEWA
        method_account = (account.phone or "").strip()
        bank_name = ""
        account_holder = (account.bank_account_holder or "").strip()
    else:
        method = WalletWithdrawal.Method.KHALTI
        method_account = (account.phone or "").strip()
        bank_name = ""
        account_holder = (account.bank_account_holder or "").strip()
    return {
        "method": method,
        "method_account": method_account[:100],
        "bank_name": bank_name[:100],
        "account_holder": account_holder[:150],
    }


def gen_withdrawal_number() -> str:
    for _ in range(30):
        cand = f"WTH-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
        if not WalletWithdrawal.objects.filter(withdrawal_number=cand).exists():
            return cand
    return f"WTH-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:8].upper()}"


def create_pending_withdrawal(
    *,
    wallet: Wallet,
    payout_user: User,
    payout_account: PayoutAccount,
    amount: Decimal,
) -> WalletWithdrawal:
    if payout_account.user_id != payout_user.pk:
        raise ValueError("Payout account does not belong to this user.")
    wallet_policy.validate_withdrawal_against_settings(wallet, amount)
    avail = available_withdrawal_amount(wallet)
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    if amount > avail:
        raise ValueError("Insufficient available balance (including pending withdrawals).")
    fields = payout_account_to_withdrawal_fields(payout_account)
    return WalletWithdrawal.objects.create(
        withdrawal_number=gen_withdrawal_number(),
        wallet=wallet,
        payout_account=payout_account,
        amount=amount,
        status=WalletWithdrawal.Status.PENDING,
        **fields,
    )
