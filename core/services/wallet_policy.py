"""Enforce singleton WalletSettings at wallet operation boundaries.

Daily windows use the active Django timezone's calendar date (typically UTC unless
USE_TZ + TIME_ZONE differ for display only; boundaries match other wallet code).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from django.utils import timezone

from core.models import User, Wallet, WalletSettings, WalletTransaction, WalletWithdrawal


def _settings() -> WalletSettings:
    return WalletSettings.load()


def is_wallet_type_enabled(wallet_type: str) -> bool:
    ws = _settings()
    mapping = {
        Wallet.Type.PERSONAL: ws.individual_wallet_enabled,
        Wallet.Type.SHARED: ws.shared_wallet_enabled,
        Wallet.Type.CHILD: ws.child_wallet_enabled,
        Wallet.Type.PARENT: ws.family_wallet_enabled,
        Wallet.Type.VENDOR: ws.vendor_wallet_enabled,
        Wallet.Type.PLATFORM: True,
    }
    return bool(mapping.get(wallet_type, True))


def vendor_wallet_operations_allowed() -> bool:
    return bool(_settings().vendor_wallet_enabled)


def assert_wallet_type_enabled_for_wallet(w: Wallet) -> None:
    if w.type == Wallet.Type.PLATFORM:
        return
    if not is_wallet_type_enabled(w.type):
        raise ValueError(f"This wallet type ({w.get_type_display()}) is disabled by site settings.")


def assert_peer_transfer_individual_allowed(from_w: Wallet, to_w: Wallet) -> None:
    ws = _settings()
    if not ws.individual_wallet_enabled:
        if from_w.type == Wallet.Type.PERSONAL or to_w.type == Wallet.Type.PERSONAL:
            raise ValueError("Personal wallet transfers are disabled by site settings.")


def assert_hub_transfer_allowed(from_w: Wallet, to_w: Wallet) -> None:
    """Cross-portal transfer-by-code: requires site flag; relaxes personal-only ban when family→personal."""
    ws = _settings()
    if not ws.cross_portal_transfer_by_code_enabled:
        raise ValueError("Transfer by transfer ID is disabled.")
    assert_wallet_type_enabled_for_wallet(from_w)
    assert_wallet_type_enabled_for_wallet(to_w)
    if not ws.individual_wallet_enabled:
        if (
            from_w.type == Wallet.Type.PERSONAL
            and to_w.type == Wallet.Type.PERSONAL
        ):
            raise ValueError("Personal wallet transfers are disabled by site settings.")
    else:
        assert_peer_transfer_individual_allowed(from_w, to_w)


def assert_family_transfer_wallets_allowed(from_w: Wallet, to_w: Wallet) -> None:
    """Enforce shared / family / child toggles for both ends of a family-scoped transfer."""
    for w in (from_w, to_w):
        if w.type == Wallet.Type.SHARED and not _settings().shared_wallet_enabled:
            raise ValueError("Family shared wallets are disabled by site settings.")
        if w.type == Wallet.Type.CHILD and not _settings().child_wallet_enabled:
            raise ValueError("Child wallets are disabled by site settings.")
        if w.type == Wallet.Type.PARENT and not _settings().family_wallet_enabled:
            raise ValueError("Family member wallets are disabled by site settings.")


def _today():
    return timezone.localdate()


def sum_user_outbound_transfers_today(user: User) -> Decimal:
    """Sum completed/flagged outbound TRANSFER amounts from wallets owned by user today."""
    wallet_ids = Wallet.objects.filter(owner_id=user.pk).values_list("pk", flat=True)
    total = (
        WalletTransaction.objects.filter(
            wallet_id__in=wallet_ids,
            type=WalletTransaction.Type.TRANSFER,
            status__in=(
                WalletTransaction.Status.COMPLETED,
                WalletTransaction.Status.FLAGGED,
            ),
            created_at__date=_today(),
        ).aggregate(s=Sum("amount"))["s"]
    )
    return total if total is not None else Decimal("0")


def assert_daily_transfer_for_wallet(from_wallet: Wallet, additional_amount: Decimal) -> None:
    """Apply daily outbound transfer limit to the wallet's owner (if any)."""
    if not from_wallet.owner_id:
        return
    u = from_wallet.owner
    assert_daily_transfer_limit(u, additional_amount)


def assert_daily_transfer_limit(user: User, additional_amount: Decimal) -> None:
    ws = _settings()
    limit = ws.daily_transfer_limit
    if limit <= 0:
        return
    so_far = sum_user_outbound_transfers_today(user)
    if so_far + additional_amount > limit:
        raise ValueError(
            "Daily outbound transfer limit exceeded. Try a smaller amount or wait until tomorrow."
        )


def assert_wallet_active_for_debit(wallet: Wallet) -> None:
    """Block balance decreases on frozen wallets (all wallet types)."""
    if wallet.status != Wallet.Status.ACTIVE:
        raise ValueError("Wallet is frozen.")


def assert_wallet_may_receive_credit(wallet: Wallet, *, allow_frozen_target: bool) -> None:
    """Block routine credits to frozen wallets; allow refunds/cancellations when flagged."""
    if allow_frozen_target:
        return
    if wallet.status != Wallet.Status.ACTIVE:
        raise ValueError("Wallet is frozen.")


def assert_may_credit_wallet(wallet: Wallet, amount: Decimal) -> None:
    if wallet.type == Wallet.Type.PLATFORM:
        return
    ws = _settings()
    if wallet.type == Wallet.Type.PERSONAL and not ws.individual_wallet_enabled:
        raise ValueError("Personal wallet top-ups are disabled by site settings.")
    cap = ws.max_balance_per_user
    if cap <= 0:
        return
    w = Wallet.objects.filter(pk=wallet.pk).only("balance").first()
    if not w:
        return
    if w.balance + amount > cap:
        raise ValueError(
            f"Wallet balance cannot exceed Rs. {cap} (site maximum per wallet)."
        )


def compute_peer_transfer_fee(amount: Decimal) -> Decimal:
    ws = _settings()
    if ws.transaction_fee_type == WalletSettings.FeeType.FLAT:
        fee = ws.transaction_fee_value
    else:
        fee = (amount * ws.transaction_fee_value / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    if fee < 0:
        return Decimal("0")
    if fee > amount:
        return amount
    return fee


def withdrawal_requires_otp() -> bool:
    return bool(_settings().otp_for_withdrawals)


def transfer_requires_otp(amount: Decimal) -> bool:
    ws = _settings()
    return amount >= ws.otp_for_transfers_above


def transfer_should_auto_flag(amount: Decimal) -> bool:
    ws = _settings()
    if not ws.auto_flag_suspicious:
        return False
    return amount >= ws.otp_for_transfers_above


def validate_withdrawal_against_settings(
    wallet: Wallet,
    amount: Decimal,
    *,
    exclude_withdrawal_pk: int | None = None,
) -> None:
    ws = _settings()
    if amount < ws.min_withdrawal:
        raise ValueError(
            f"Minimum withdrawal is Rs. {ws.min_withdrawal}."
        )
    max_day = ws.max_withdrawal_per_day
    if max_day <= 0:
        return
    qs = WalletWithdrawal.objects.filter(
        wallet_id=wallet.pk,
        created_at__date=_today(),
    ).exclude(status=WalletWithdrawal.Status.REJECTED)
    if exclude_withdrawal_pk is not None:
        qs = qs.exclude(pk=exclude_withdrawal_pk)
    used = qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    if used + amount > max_day:
        raise ValueError(
            f"Maximum withdrawal per day for this wallet is Rs. {max_day}."
        )


def wallet_payable_under_settings(w: Wallet) -> bool:
    if w.status != Wallet.Status.ACTIVE:
        return False
    if w.type in (Wallet.Type.VENDOR, Wallet.Type.PLATFORM):
        return False
    if not is_wallet_type_enabled(w.type):
        return False
    return True


def public_settings_snapshot() -> dict:
    """Non-sensitive subset for optional portal GET (limits / toggles)."""
    ws = _settings()
    return {
        "max_balance_per_user": float(ws.max_balance_per_user),
        "daily_transfer_limit": float(ws.daily_transfer_limit),
        "min_withdrawal": float(ws.min_withdrawal),
        "max_withdrawal_per_day": float(ws.max_withdrawal_per_day),
        "otp_for_withdrawals": bool(ws.otp_for_withdrawals),
        "otp_for_transfers_above": float(ws.otp_for_transfers_above),
        "individual_wallet_enabled": ws.individual_wallet_enabled,
        "shared_wallet_enabled": ws.shared_wallet_enabled,
        "child_wallet_enabled": ws.child_wallet_enabled,
        "family_wallet_enabled": ws.family_wallet_enabled,
        "vendor_wallet_enabled": ws.vendor_wallet_enabled,
        "cross_portal_transfer_by_code_enabled": ws.cross_portal_transfer_by_code_enabled,
    }
